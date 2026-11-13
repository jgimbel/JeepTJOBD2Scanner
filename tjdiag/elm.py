"""ELM327 driver.

Talks the ELM327 "AT" command set and turns the adapter's text replies into
frames. Headers are always switched on, because the header's third byte is the
address of the module that answered, and knowing *which* module reports P0601
is the first thing this tool exists to find out.

Frame layout with headers on, for the protocols with a three byte header
(SAE J1850 VPW/PWM, ISO 9141-2, ISO 14230):

    48 6B 10 43 06 01 00 00 00 00 C4
    |  |  |  |                    +-- checksum (CRC-8 on J1850, byte sum on ISO)
    |  |  |  +----------------------- payload (here: mode 03 reply, DTC P0601)
    |  |  +-------------------------- source address (0x10 = engine controller)
    |  +----------------------------- target address
    +-------------------------------- priority / message type
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .transport import Transport

PROMPT = b">"

# Replies that mean "the adapter could not complete the request".
_ERROR_TOKENS = (
    "UNABLE TO CONNECT",
    "BUS ERROR",
    "BUS BUSY",
    "FB ERROR",
    "DATA ERROR",
    "RX ERROR",
    "BUFFER FULL",
    "CAN ERROR",
    "LV RESET",
    "ACT ALERT",
    "STOPPED",
    "ERR",
)
# Progress chatter that carries no information.
_NOISE_PREFIXES = ("SEARCHING", "BUS INIT")

PROTOCOL_NAMES = {
    "0": "automatic",
    "1": "SAE J1850 PWM (41.6 kbaud)",
    "2": "SAE J1850 VPW (10.4 kbaud)",
    "3": "ISO 9141-2",
    "4": "ISO 14230-4 KWP (5 baud init)",
    "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN (11 bit, 500 kbaud)",
    "7": "ISO 15765-4 CAN (29 bit, 500 kbaud)",
    "8": "ISO 15765-4 CAN (11 bit, 250 kbaud)",
    "9": "ISO 15765-4 CAN (29 bit, 250 kbaud)",
    "A": "SAE J1939 CAN",
}
J1850_VPW = "2"


class ElmError(Exception):
    """The adapter did not behave like an ELM327, or rejected a command."""


class ElmTimeout(ElmError):
    """No prompt came back in time."""


def crc8_j1850(data: bytes) -> int:
    """SAE J1850 CRC-8 (polynomial 0x1D, initial 0xFF, final XOR 0xFF)."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def _checksum_matches(body: bytes, last: int) -> bool:
    return last == crc8_j1850(body) or last == sum(body) & 0xFF


@dataclass
class Frame:
    """One reply frame from one module."""

    raw: bytes
    priority: int
    target: int
    source: int
    data: bytes
    checksum_ok: bool

    @property
    def hex(self) -> str:
        return self.raw.hex(" ").upper()


@dataclass
class Response:
    """Everything the adapter said in reply to one OBD request."""

    command: str
    lines: list[str]
    frames: list[Frame] = field(default_factory=list)
    status: str = "ok"  # "ok", "no_data" or "error"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def by_source(self) -> dict[int, list[Frame]]:
        grouped: dict[int, list[Frame]] = {}
        for frame in self.frames:
            grouped.setdefault(frame.source, []).append(frame)
        return grouped


@dataclass
class BusFrame:
    """One frame seen while passively monitoring the bus.

    Chrysler's everyday PCI traffic uses a single identifier byte rather than
    the three byte OBD header, so these are kept as raw bytes.
    """

    raw: bytes
    checksum_ok: bool

    @property
    def message_id(self) -> int:
        return self.raw[0]

    @property
    def hex(self) -> str:
        return self.raw.hex(" ").upper()


_HEX_LINE = re.compile(r"^[0-9A-F]+$")


def _hex_bytes(line: str) -> bytes | None:
    compact = line.replace(" ", "").upper()
    if not compact or len(compact) % 2 or not _HEX_LINE.match(compact):
        return None
    return bytes.fromhex(compact)


def parse_frame(line: str) -> Frame | None:
    """Parse one reply line with a three byte header. None if it is not a frame."""
    flagged_bad = False
    for marker in ("<DATA ERROR", "<RX ERROR"):
        if marker in line:
            line = line.replace(marker, "").strip()
            flagged_bad = True
    raw = _hex_bytes(line)
    if raw is None or len(raw) < 5:  # 3 header + at least 1 data + checksum
        return None
    body, last = raw[:-1], raw[-1]
    if _checksum_matches(body, last):
        data, checksum_ok = body[3:], True
    else:
        # Either the frame was corrupted, or this adapter does not print the
        # checksum byte. Keep every byte as data and say so.
        data, checksum_ok = raw[3:], False
    if flagged_bad:
        checksum_ok = False
    return Frame(raw, raw[0], raw[1], raw[2], data, checksum_ok)


def parse_bus_frame(line: str) -> BusFrame | None:
    raw = _hex_bytes(line)
    if raw is None or len(raw) < 2:
        return None
    return BusFrame(raw, raw[-1] == crc8_j1850(raw[:-1]))


class Elm327:
    def __init__(self, transport: Transport, timeout: float = 5.0):
        self.transport = transport
        self.timeout = timeout
        self.version: str | None = None
        self.reports_elm_name = True

    # -- raw exchange -----------------------------------------------------

    def _read_until_prompt(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while PROMPT not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ElmTimeout(
                    f"no prompt from the adapter within {timeout:.0f}s "
                    f"(received {bytes(buffer)!r})"
                )
            buffer += self.transport.read(min(remaining, 0.25))
        text = bytes(buffer).split(PROMPT, 1)[0]
        return text.decode("ascii", errors="replace").replace("\x00", "")

    def command(self, cmd: str, timeout: float | None = None) -> list[str]:
        """Send one command and return the reply lines (echo and blanks removed)."""
        self.transport.write(cmd.encode("ascii") + b"\r")
        text = self._read_until_prompt(timeout if timeout is not None else self.timeout)
        lines = [line.strip() for line in re.split(r"[\r\n]+", text)]
        wanted = cmd.replace(" ", "").upper()
        return [
            line
            for line in lines
            if line and line.replace(" ", "").upper() != wanted
        ]

    def at(self, cmd: str, timeout: float | None = None) -> str:
        """Send an AT command. Raises ElmError if the adapter does not know it."""
        lines = self.command("AT" + cmd, timeout)
        reply = " ".join(lines)
        if reply == "?" or not lines:
            raise ElmError(f"adapter rejected AT{cmd} (reply: {reply or 'nothing'})")
        return reply

    def supports(self, cmd: str) -> bool:
        try:
            self.at(cmd)
        except ElmError:
            return False
        return True

    # -- session ----------------------------------------------------------

    def _sync(self) -> None:
        """Get the adapter back to an idle prompt, whatever it was doing.

        Uses a complete, harmless command on purpose. A bare carriage return
        must never be sent: an ELM327 treats it as "repeat the last command",
        and the last command could have been "clear codes".
        """
        self.transport.write(b"ATI\r")
        try:
            self._read_until_prompt(2.0)
        except ElmTimeout:
            pass

    def reset(self) -> str:
        """Reset the adapter and switch it to the settings this tool relies on."""
        self._sync()
        reply = self.at("Z", timeout=max(self.timeout, 6.0))
        # Clone adapters often rename themselves ("OBDII v1.5"), so the name is
        # not the test. What matters is that the adapter obeys an ELM327 command.
        try:
            accepted = "OK" in self.at("E0").upper()  # no echo
        except ElmError:
            accepted = False
        if not accepted:
            raise ElmError(f"not an ELM327-type adapter (reset reply: {reply!r})")
        self.version = reply
        self.reports_elm_name = "ELM" in reply.upper()
        self.at("L0")  # no linefeeds
        self.at("S1")  # spaces between bytes
        self.at("H1")  # show headers: tells us which module answered
        return reply

    def set_protocol(self, protocol: str) -> None:
        self.at("SP" + protocol)

    def protocol_number(self) -> str:
        """Active protocol as the ELM327 digit, without the 'A' automatic prefix."""
        reply = self.at("DPN").upper()
        return reply[1:] if len(reply) > 1 and reply.startswith("A") else reply

    def voltage(self) -> float | None:
        """Battery voltage measured by the adapter at the diagnostic connector."""
        try:
            reply = self.at("RV")
        except ElmError:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)", reply)
        return float(match.group(1)) if match else None

    # -- OBD requests -----------------------------------------------------

    def obd(self, request: str, timeout: float | None = None) -> Response:
        """Send an OBD request such as '03' or '01 0C' and parse every reply."""
        lines = self.command(request, timeout)
        response = Response(command=request, lines=lines)
        for line in lines:
            upper = line.upper()
            if upper.startswith(_NOISE_PREFIXES):
                continue
            if upper == "?":
                response.status, response.error = "error", "adapter rejected the request"
                continue
            if upper == "NO DATA":
                if not response.frames:
                    response.status = "no_data"
                continue
            frame = parse_frame(upper)
            if frame is not None:
                response.frames.append(frame)
                continue
            token = next((t for t in _ERROR_TOKENS if t in upper), None)
            if token:
                response.status, response.error = "error", upper
        if response.frames and response.status == "no_data":
            response.status = "ok"
        if not response.frames and response.status == "ok":
            response.status = "no_data"
        return response

    # -- passive monitoring -----------------------------------------------

    def monitor(
        self, seconds: float, max_frames: int | None = None
    ) -> tuple[list[BusFrame], int]:
        """Listen to all bus traffic. Returns (frames, buffer overflow count).

        The adapter's small buffer overflows easily on a busy bus over a slow
        link; when that happens monitoring is simply restarted.
        """
        frames: list[BusFrame] = []
        overflows = 0
        deadline = time.monotonic() + seconds
        pending = ""
        self.transport.write(b"ATMA\r")
        while time.monotonic() < deadline:
            if max_frames is not None and len(frames) >= max_frames:
                break
            chunk = self.transport.read(0.1).decode("ascii", errors="replace")
            if not chunk:
                continue
            pending += chunk
            *complete, pending = re.split(r"[\r\n]", pending)
            restart = False
            for line in complete:
                line = line.strip().lstrip(">").strip()
                if not line or line.upper() == "ATMA":
                    continue
                if line == "?":
                    raise ElmError("adapter rejected ATMA (bus monitoring unsupported)")
                if "BUFFER FULL" in line.upper():
                    overflows += 1
                    restart = True
                    continue
                frame = parse_bus_frame(line)
                if frame is not None:
                    frames.append(frame)
            if ">" in pending or restart:
                # Monitoring ended on its own (overflow); start it again.
                pending = ""
                self.transport.write(b"ATMA\r")
        # Any character stops monitoring. A space is used because the adapter
        # ignores spaces if it happens to keep the character.
        self.transport.write(b" ")
        try:
            self._read_until_prompt(2.0)
        except ElmTimeout:
            pass
        self._sync()
        if max_frames is not None:
            frames = frames[:max_frames]
        return frames, overflows

"""Driver for the Arduino Mega running the open-source ChryslerScanner firmware.

The Mega is the bridge between the laptop (USB) and the engine computer's SCI
serial link. This module speaks the firmware's USB packet protocol:

    3D  LEN_HI LEN_LO  DATACODE  SUBCODE  payload...  CHECKSUM

    LEN       number of bytes from DATACODE through the last payload byte
    DATACODE  bit 7 set = packet comes from the scanner
              bits 6-4  = bus (0 scanner itself, 1 CCD, 2 engine computer, 3 TCM)
              bits 3-0  = command
    CHECKSUM  low byte of the sum of every preceding byte, 3D included

Packets that carry bus traffic start their payload with a four byte
millisecond timestamp.

This was written from the firmware source (ChryslerCCDSCIScanner.ino, v0.9.12),
not from the project's 2020 protocol PDF, which describes an older framing
(checksum without the 3D byte, acknowledgement packets that no longer exist).

Safety: this driver can only send the handful of packet types listed in
_ALLOWED_PACKETS, and every message for the engine computer passes through
sci.check_read_only first. The firmware's debug commands (bootstrap, EEPROM
restore, its own EEPROM writes) cannot be reached from here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import sci
from .transport import Transport

SYNC = 0x3D
USB_BAUD = 250000
HANDSHAKE_TEXT = b"CHRYSLERCCDSCISCANNER"
BRIDGE_HANDSHAKE_TEXT = b"TJDIAGSCIBRIDGE"

# The two firmwares this driver can talk to.
UPSTREAM = "ChryslerScanner"  # open-source firmware on an Arduino Mega 2560
BRIDGE = "tjdiag SCI bridge"  # this project's read-only firmware on an ESP32
_FIRMWARE_BY_HANDSHAKE = {HANDSHAKE_TEXT: UPSTREAM, BRIDGE_HANDSHAKE_TEXT: BRIDGE}
_MCU_BY_SIGNATURE = {bytes([0x1E, 0x98, 0x01]): "ATmega2560", b"ESP": "ESP32"}

BUS_USB, BUS_CCD, BUS_PCM, BUS_TCM = 0, 1, 2, 3

CMD_RESET = 0x00
CMD_HANDSHAKE = 0x01
CMD_STATUS = 0x02
CMD_SETTINGS = 0x03
CMD_REQUEST = 0x04
CMD_RESPONSE = 0x05
CMD_MSG_TX = 0x06
CMD_MSG_RX = 0x07
CMD_ERROR = 0x0F

SET_SCI_BUS = 0x03
SET_REPEAT = 0x04
REQ_HWFW = 0x01
REQ_BATTERY = 0x03
TX_STOP = 0x01
TX_SINGLE = 0x02
TX_LIST = 0x03
RX_LOW_SPEED = 0x01
RX_HIGH_SPEED = 0x02

# SCI settings byte.
SCI_ENABLE = 0x80
SCI_SPEED_LOW = 0x01  # 7812.5 baud
SCI_SPEED_HIGH = 0x02  # 62500 baud

# The firmware copies a message list into a 256 byte buffer without a bounds
# check, so lists are kept comfortably below that.
LIST_BUFFER_LIMIT = 250

ERROR_NAMES = {
    0x01: "invalid packet length",
    0x02: "invalid command",
    0x03: "invalid sub-command",
    0x04: "invalid payload",
    0x05: "packet checksum mismatch",
    0x06: "packet timed out",
    0x07: "buffer overflow",
    0x08: "invalid bus",
    0xF6: "no response from the engine computer (low speed)",
    0xF8: "no response from the RAM table (high speed)",
    0xF9: "invalid RAM table (high speed)",
    0xFA: "no response from the engine computer (high speed)",
    0xFE: "internal error",
    0xFF: "fatal error",
}

# (bus, command, sub-command) combinations this driver is able to send.
_ALLOWED_PACKETS = {
    (BUS_USB, CMD_HANDSHAKE, 0x00),
    (BUS_USB, CMD_STATUS, 0x00),
    (BUS_USB, CMD_SETTINGS, SET_SCI_BUS),
    (BUS_USB, CMD_SETTINGS, SET_REPEAT),
    (BUS_USB, CMD_REQUEST, REQ_HWFW),
    (BUS_USB, CMD_REQUEST, REQ_BATTERY),
    (BUS_PCM, CMD_MSG_TX, TX_STOP),
    (BUS_PCM, CMD_MSG_TX, TX_SINGLE),
    (BUS_PCM, CMD_MSG_TX, TX_LIST),
}


class ScannerError(Exception):
    """The interface board did not behave as expected."""


@dataclass
class Packet:
    from_scanner: bool
    bus: int
    command: int
    sub: int
    payload: bytes
    timestamp_ms: int | None = None

    def matches(self, bus: int, command: int, sub: int | None = None) -> bool:
        return (
            self.bus == bus
            and self.command == command
            and (sub is None or self.sub == sub)
        )


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def encode_packet(bus: int, command: int, sub: int, payload: bytes = b"") -> bytes:
    """Build a packet from the laptop to the scanner."""
    length = len(payload) + 2
    body = bytes([SYNC, length >> 8, length & 0xFF, (bus << 4) | command, sub]) + payload
    return body + bytes([checksum(body)])


def encode_scanner_packet(bus: int, command: int, sub: int, payload: bytes, timestamp_ms: int = 0) -> bytes:
    """Build a packet the way the scanner does (used by the simulator and tests)."""
    if bus:
        payload = (timestamp_ms & 0xFFFFFFFF).to_bytes(4, "big") + payload
    length = len(payload) + 2
    datacode = 0x80 | (bus << 4) | command
    body = bytes([SYNC, length >> 8, length & 0xFF, datacode, sub]) + payload
    return body + bytes([checksum(body)])


class PacketReader:
    """Reassembles packets from a byte stream, resynchronising after garbage."""

    def __init__(self):
        self._buffer = bytearray()
        self.discarded = 0

    def feed(self, data: bytes) -> list[Packet]:
        self._buffer += data
        packets = []
        while True:
            start = self._buffer.find(SYNC)
            if start < 0:
                self.discarded += len(self._buffer)
                self._buffer.clear()
                break
            if start:
                self.discarded += start
                del self._buffer[:start]
            if len(self._buffer) < 3:
                break
            length = (self._buffer[1] << 8) | self._buffer[2]
            total = 3 + length + 1
            if length < 2 or length > 1024:
                self.discarded += 1
                del self._buffer[:1]
                continue
            if len(self._buffer) < total:
                break
            raw = bytes(self._buffer[:total])
            if checksum(raw[:-1]) != raw[-1]:
                self.discarded += 1
                del self._buffer[:1]
                continue
            del self._buffer[:total]
            datacode, sub, payload = raw[3], raw[4], raw[5:-1]
            bus = (datacode >> 4) & 0x07
            timestamp = None
            if datacode & 0x80 and bus and len(payload) >= 4:
                timestamp = int.from_bytes(payload[:4], "big")
                payload = payload[4:]
            packets.append(
                Packet(bool(datacode & 0x80), bus, datacode & 0x0F, sub, payload, timestamp)
            )
        return packets


class Scanner:
    def __init__(self, transport: Transport, timeout: float = 2.0):
        self.transport = transport
        self.timeout = timeout
        self._reader = PacketReader()
        self._queue: list[Packet] = []
        self.sci_settings: int | None = None
        self.high_speed = False
        self.firmware: str | None = None

    # -- packets ------------------------------------------------------------

    def _send(self, bus: int, command: int, sub: int, payload: bytes = b"") -> None:
        if (bus, command, sub) not in _ALLOWED_PACKETS:
            raise ScannerError(
                f"refusing to send packet type bus={bus} command={command:#04x} sub={sub:#04x}"
            )
        self.transport.write(encode_packet(bus, command, sub, payload))

    def _next_packet(self, deadline: float) -> Packet | None:
        while not self._queue:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._queue += self._reader.feed(self.transport.read(min(remaining, 0.05)))
        return self._queue.pop(0)

    def _wait(self, bus: int, command: int, sub: int | None, timeout: float) -> Packet | None:
        """Next packet of the wanted kind. Unrelated packets are dropped."""
        deadline = time.monotonic() + timeout
        while True:
            packet = self._next_packet(deadline)
            if packet is None:
                return None
            if packet.matches(bus, command, sub):
                return packet
            if packet.matches(BUS_USB, CMD_SETTINGS, SET_SCI_BUS) and packet.payload:
                # The firmware announces speed changes it makes on its own.
                self.sci_settings = packet.payload[0]
            elif packet.matches(BUS_USB, CMD_ERROR) and packet.sub:
                name = ERROR_NAMES.get(packet.sub, f"code {packet.sub:#04x}")
                raise ScannerError(f"the interface board reported an error: {name}")

    def _expect(self, bus: int, command: int, sub: int | None, what: str) -> Packet:
        packet = self._wait(bus, command, sub, self.timeout)
        if packet is None:
            raise ScannerError(f"no answer from the interface board to the {what} request")
        return packet

    def drain(self, seconds: float = 0.2) -> None:
        """Throw away whatever the board sends for a moment (start-up chatter)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._reader.feed(self.transport.read(0.05))
        self._queue.clear()

    # -- board ----------------------------------------------------------------

    def open(self, boot_wait: float = 2.5, attempts: int = 5) -> None:
        """Wait for the board to boot, then confirm it runs the expected firmware.

        Opening the serial port resets a stock Mega; its bootloader takes a
        couple of seconds before the firmware starts.
        """
        self.drain(boot_wait)
        for _ in range(attempts):
            if self.handshake():
                return
            self.drain(0.5)
        raise ScannerError(
            "the board did not answer the handshake. Check the port, and that the "
            "firmware is flashed on the board."
        )

    def handshake(self) -> bool:
        self._send(BUS_USB, CMD_HANDSHAKE, 0x00)
        try:
            packet = self._wait(BUS_USB, CMD_HANDSHAKE, None, self.timeout)
        except ScannerError:
            return False
        if packet is None or packet.payload not in _FIRMWARE_BY_HANDSHAKE:
            return False
        self.firmware = _FIRMWARE_BY_HANDSHAKE[packet.payload]
        return True

    def firmware_version(self) -> str:
        self._send(BUS_USB, CMD_REQUEST, REQ_HWFW)
        payload = self._expect(BUS_USB, CMD_RESPONSE, REQ_HWFW, "firmware information").payload
        if len(payload) < 30:
            raise ScannerError("firmware information reply was too short")
        return f"{payload[26]}.{payload[27]}.{payload[28]}"

    def status(self) -> dict:
        self._send(BUS_USB, CMD_STATUS, 0x00)
        payload = self._expect(BUS_USB, CMD_STATUS, None, "status").payload
        if len(payload) < 25:
            raise ScannerError("status reply was too short")
        self.sci_settings = payload[24]
        return {
            "mcu_signature": payload[0:3].hex().upper(),
            "mcu": _MCU_BY_SIGNATURE.get(bytes(payload[0:3])),
            "free_ram": int.from_bytes(payload[10:12], "big"),
            "connected_to_vehicle": bool(payload[12]),
            "battery_volts": int.from_bytes(payload[13:15], "big") / 1000,
            "sci_settings": payload[24],
        }

    def battery_volts(self) -> float:
        """Battery voltage at the diagnostic connector, 0.0 if nothing is sensed."""
        self._send(BUS_USB, CMD_REQUEST, REQ_BATTERY)
        payload = self._expect(BUS_USB, CMD_RESPONSE, REQ_BATTERY, "battery voltage").payload
        return int.from_bytes(payload[:2], "big") / 1000

    def configure_sci(self, high_speed: bool = False) -> int:
        """Enable the engine computer's SCI link.

        The firmware's "configuration A/B" setting only drives switch chips
        that this project's hardware does not have; which connector pin is
        used is decided by the wiring.
        """
        settings = SCI_ENABLE | (SCI_SPEED_HIGH if high_speed else SCI_SPEED_LOW)
        self._send(BUS_USB, CMD_SETTINGS, SET_SCI_BUS, bytes([settings]))
        reply = self._expect(BUS_USB, CMD_SETTINGS, SET_SCI_BUS, "SCI configuration")
        if not reply.payload or reply.payload[0] != settings:
            raise ScannerError("the board did not accept the SCI configuration")
        self.sci_settings = settings
        self.high_speed = high_speed
        return settings

    def set_interval(self, milliseconds: int) -> None:
        """Pause between messages when the board works through a list."""
        payload = bytes([BUS_PCM, (milliseconds >> 8) & 0xFF, milliseconds & 0xFF])
        self._send(BUS_USB, CMD_SETTINGS, SET_REPEAT, payload)
        self._expect(BUS_USB, CMD_SETTINGS, SET_REPEAT, "message interval")

    # -- engine computer --------------------------------------------------------

    def stop(self) -> None:
        self._send(BUS_PCM, CMD_MSG_TX, TX_STOP)
        self._wait(BUS_USB, CMD_MSG_TX, TX_STOP, self.timeout)

    def send_sci(self, message: bytes, timeout: float = 1.0) -> bytes | None:
        """Send one message. Returns every byte that came back (echo, then reply).

        None means the engine computer sent nothing at all.
        """
        sci.check_read_only(message, self.high_speed)
        self._send(BUS_PCM, CMD_MSG_TX, TX_SINGLE, bytes(message))
        packet = self._wait(BUS_PCM, CMD_MSG_RX, None, timeout)
        if packet is None:
            return None
        # The firmware changes its own speed when the computer echoes a
        # speed-select byte; keep track so the read-only check stays right.
        if packet.payload[:1] == bytes([sci.CMD_HIGH_SPEED]) and not self.high_speed:
            self.high_speed = True
        elif packet.payload[:1] == bytes([sci.CMD_LOW_SPEED]) and self.high_speed:
            self.high_speed = False
        return packet.payload

    def send_sci_list(self, messages: list[bytes], timeout_each: float = 1.0) -> list[bytes]:
        """Send several messages, paced by the board. Returns the replies in order.

        The list may come back shorter than the request: the board stops
        advancing when a message gets no bytes back at all.
        """
        if not messages:
            return []
        for message in messages:
            sci.check_read_only(message, self.high_speed)
        if len(messages) == 1:
            # The firmware's list mode only works for two or more messages.
            reply = self.send_sci(messages[0], timeout_each)
            return [] if reply is None else [reply]
        payload = bytearray([len(messages)])
        for message in messages:
            payload += bytes([len(message)]) + bytes(message)
        if len(messages) > 255 or len(payload) - 1 > LIST_BUFFER_LIMIT:
            raise ScannerError("message list is too long for the board's buffer")
        self._send(BUS_PCM, CMD_MSG_TX, TX_LIST, bytes(payload))
        replies: list[bytes] = []
        for _ in messages:
            packet = self._wait(BUS_PCM, CMD_MSG_RX, None, timeout_each)
            if packet is None:
                self.stop()
                break
            replies.append(packet.payload)
        return replies

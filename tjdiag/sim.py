"""A fake ELM327 plugged into a simulated 2004 Wrangler.

This exists so the tool can be exercised and tested without the Jeep. It
imitates the adapter's text protocol (echo, prompt, headers, error strings,
"a bare carriage return repeats the last command") and a vehicle with an
engine controller at 0x10 and a transmission controller at 0x18.

It is a stand-in, not evidence: what the real Jeep and the real adapter do is
only known once the tool has been run on them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .elm import crc8_j1850

VERSION = "ELM327 v1.5"


def encode_dtc(code: str) -> bytes:
    system = "PCBU".index(code[0])
    high = (system << 6) | (int(code[1]) << 4) | int(code[2], 16)
    return bytes([high, int(code[3:5], 16)])


def _default_pids() -> dict[int, bytes]:
    return {
        0x04: bytes([0]),  # load
        0x05: bytes([21 + 40]),  # coolant 21 C
        0x06: bytes([128]),
        0x07: bytes([128]),
        0x0B: bytes([98]),  # MAP kPa
        0x0C: bytes([0, 0]),  # rpm
        0x0D: bytes([0]),
        0x0E: bytes([128]),
        0x0F: bytes([19 + 40]),
        0x11: bytes([0]),
    }


@dataclass
class SimEcu:
    address: int
    stored: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    pids: dict[int, bytes] = field(default_factory=dict)
    freeze_dtc: str | None = None
    freeze_pids: dict[int, bytes] = field(default_factory=dict)
    vin: str | None = None
    answers_obd: bool = True
    hard_faults: list[str] = field(default_factory=list)  # back at every key-on
    run_faults: list[str] = field(default_factory=list)  # back once the engine runs


@dataclass
class SimVehicle:
    ecus: list[SimEcu]
    ignition_on: bool = True
    engine_running: bool = False
    battery_voltage: float = 12.5
    running_voltage: float = 14.1
    voltage_profile: list[float] = field(default_factory=list)

    def voltage(self) -> float:
        if self.voltage_profile:
            return self.voltage_profile.pop(0)
        return self.running_voltage if self.engine_running else self.battery_voltage

    def clear_codes(self) -> None:
        for ecu in self.ecus:
            ecu.stored.clear()
            ecu.pending.clear()
            ecu.freeze_dtc = None
            ecu.freeze_pids.clear()

    def key_cycle(self) -> None:
        """Key off, key on, engine off: controllers rerun their power-up self-tests."""
        self.engine_running = False
        for ecu in self.ecus:
            for code in ecu.hard_faults:
                if code not in ecu.stored:
                    ecu.stored.append(code)
            if 0x0C in ecu.pids:
                ecu.pids[0x0C] = bytes([0, 0])

    def start_engine(self) -> None:
        self.engine_running = True
        for ecu in self.ecus:
            for code in ecu.run_faults:
                if code not in ecu.stored:
                    ecu.stored.append(code)
            if 0x0C in ecu.pids:
                ecu.pids[0x0C] = (720 * 4).to_bytes(2, "big")


# A few frames in the style of Chrysler's single-identifier PCI traffic.
_BUS_TRAFFIC = [
    bytes([0x10, 0x0B, 0x40, 0x00, 0x62]),
    bytes([0x14, 0x00, 0x00]),
    bytes([0xC0, 0x7D, 0x58, 0x3D, 0x3B]),
    bytes([0x37, 0x01, 0x00]),
    bytes([0xD0, 0x00, 0x00]),
]


class SimElm:
    """Transport that behaves like an ELM327 attached to a SimVehicle."""

    def __init__(
        self,
        vehicle: SimVehicle,
        vpw_supported: bool = True,
        unsupported_at: tuple[str, ...] = (),
        prints_checksum: bool = True,
    ):
        self.vehicle = vehicle
        self.vpw_supported = vpw_supported
        self.unsupported_at = unsupported_at
        self.prints_checksum = prints_checksum
        self.bare_cr_count = 0
        self.commands: list[str] = []
        self._in = bytearray()
        self._out = bytearray()
        self._monitoring = False
        self._traffic_index = 0
        self._last_command = ""
        self._defaults()

    def _defaults(self) -> None:
        self.echo = True
        self.linefeeds = False
        self.spaces = True
        self.headers = False
        self.protocol = "0"
        self.auto_found = False

    # -- Transport interface ------------------------------------------------

    def write(self, data: bytes) -> None:
        for byte in data:
            if self._monitoring:
                # Any character stops monitoring and is discarded.
                self._monitoring = False
                self._out += b"\r>"
                continue
            if byte == 0x0D:
                line = self._in.decode("ascii", errors="replace")
                self._in.clear()
                self._handle(line)
            else:
                self._in.append(byte)

    def read(self, timeout: float) -> bytes:
        if self._out:
            data = bytes(self._out)
            self._out.clear()
            return data
        if self._monitoring and self.vehicle.ignition_on and self.vpw_supported:
            time.sleep(0.002)
            frame = _BUS_TRAFFIC[self._traffic_index % len(_BUS_TRAFFIC)]
            self._traffic_index += 1
            frame = frame + bytes([crc8_j1850(frame)])
            return (self._format(frame) + "\r").encode("ascii")
        time.sleep(min(timeout, 0.001))
        return b""

    def close(self) -> None:
        pass

    # -- adapter behaviour ----------------------------------------------------

    def _format(self, raw: bytes) -> str:
        return (raw.hex(" ") if self.spaces else raw.hex()).upper()

    def _reply(self, command: str, lines: list[str]) -> None:
        end = "\r\n" if self.linefeeds else "\r"
        text = (command + end) if self.echo else ""
        text += end.join(lines) + end + end + ">"
        self._out += text.encode("ascii")

    def _handle(self, line: str) -> None:
        command = line.replace(" ", "").upper()
        if not command:
            # Faithful to the real chip: an empty line repeats the last command.
            self.bare_cr_count += 1
            command = self._last_command
            if not command:
                self._out += b"\r>"
                return
        self._last_command = command
        self.commands.append(command)
        if command == "ATMA" and not any(p == "MA" for p in self.unsupported_at):
            # Monitoring prints frames until interrupted; no prompt until then.
            self._monitoring = True
            if self.echo:
                self._out += line.encode("ascii") + b"\r"
        elif command.startswith("AT"):
            self._reply(line, self._at(command[2:]))
        elif all(c in "0123456789ABCDEF" for c in command) and len(command) % 2 == 0:
            self._reply(line, self._obd(bytes.fromhex(command)))
        else:
            self._reply(line, ["?"])

    def _at(self, cmd: str) -> list[str]:
        if any(cmd.startswith(prefix) for prefix in self.unsupported_at):
            return ["?"]
        if cmd == "Z":
            self._defaults()
            return [VERSION]
        if cmd == "I":
            return [VERSION]
        if cmd == "@1":
            return ["OBDII to RS232 Interpreter"]
        if cmd in ("E0", "E1"):
            self.echo = cmd == "E1"
            return ["OK"]
        if cmd in ("L0", "L1"):
            self.linefeeds = cmd == "L1"
            return ["OK"]
        if cmd in ("S0", "S1"):
            self.spaces = cmd == "S1"
            return ["OK"]
        if cmd in ("H0", "H1"):
            self.headers = cmd == "H1"
            return ["OK"]
        if cmd.startswith("SP") and len(cmd) == 3:
            self.protocol = cmd[2]
            self.auto_found = False
            return ["OK"]
        if cmd == "DPN":
            return ["A2" if self.auto_found else self.protocol]
        if cmd == "RV":
            return [f"{self.vehicle.voltage():.1f}V"]
        if cmd in ("AL", "AT0", "AT1", "AT2") or cmd.startswith(("ST", "SH")):
            return ["OK"]
        return ["?"]

    def _reachable(self) -> bool:
        return (
            self.vpw_supported
            and self.vehicle.ignition_on
            and self.protocol in ("0", "2")
        )

    def _obd(self, request: bytes) -> list[str]:
        prefix = []
        if self.protocol == "0" and not self.auto_found:
            prefix = ["SEARCHING..."]
        if not self._reachable():
            return prefix + (["UNABLE TO CONNECT"] if self.protocol != "2" else ["NO DATA"])
        if self.protocol == "0":
            self.auto_found = True
        lines = []
        for ecu in self.vehicle.ecus:
            if not ecu.answers_obd:
                continue
            for payload in self._service(ecu, request):
                if self.headers:
                    raw = bytes([0x48, 0x6B, ecu.address]) + payload
                    if self.prints_checksum:
                        raw += bytes([crc8_j1850(raw)])
                else:
                    raw = payload
                lines.append(self._format(raw))
        if request[0] == 0x04:
            self.vehicle.clear_codes()
        return prefix + (lines or ["NO DATA"])

    # -- vehicle behaviour ----------------------------------------------------

    @staticmethod
    def _bitmap(base: int, pids: set[int]) -> bytes:
        bits = 0
        for pid in pids:
            if base < pid <= base + 0x20:
                bits |= 1 << (32 - (pid - base))
        if any(pid > base + 0x20 for pid in pids):
            bits |= 1
        return bits.to_bytes(4, "big")

    @staticmethod
    def _dtc_frames(service: int, codes: list[str]) -> list[bytes]:
        frames = []
        for i in range(0, max(len(codes), 1), 3):
            body = b"".join(encode_dtc(c) for c in codes[i : i + 3])
            frames.append(bytes([service]) + body.ljust(6, b"\x00"))
        return frames

    def _service(self, ecu: SimEcu, request: bytes) -> list[bytes]:
        mode = request[0]
        if mode == 0x01 and len(request) >= 2:
            pid = request[1]
            available = set(ecu.pids) | {0x01}
            if pid in (0x00, 0x20, 0x40):
                if pid and not (self._bitmap(pid - 0x20, available)[3] & 1):
                    return []
                return [bytes([0x41, pid]) + self._bitmap(pid, available)]
            if pid == 0x01:
                a = (0x80 if ecu.stored else 0) | len(ecu.stored)
                return [bytes([0x41, 0x01, a, 0x07, 0x65, 0x00])]
            if pid in ecu.pids:
                return [bytes([0x41, pid]) + ecu.pids[pid]]
            return []
        if mode == 0x02 and len(request) >= 2:
            pid = request[1]
            available = set(ecu.freeze_pids) | {0x02}
            if pid == 0x00:
                return [bytes([0x42, 0x00, 0x00]) + self._bitmap(0x00, available)]
            if pid == 0x02:
                dtc = encode_dtc(ecu.freeze_dtc) if ecu.freeze_dtc else b"\x00\x00"
                return [bytes([0x42, 0x02, 0x00]) + dtc]
            if pid in ecu.freeze_pids:
                return [bytes([0x42, pid, 0x00]) + ecu.freeze_pids[pid]]
            return []
        if mode == 0x03:
            return self._dtc_frames(0x43, ecu.stored)
        if mode == 0x07:
            return self._dtc_frames(0x47, ecu.pending)
        if mode == 0x04:
            return [bytes([0x44])]
        if mode == 0x09 and request[1:2] == b"\x02" and ecu.vin:
            padded = b"\x00\x00\x00" + ecu.vin.encode("ascii")
            return [
                bytes([0x49, 0x02, seq + 1]) + padded[seq * 4 : seq * 4 + 4]
                for seq in range(5)
            ]
        return []


# -- ready-made scenarios -----------------------------------------------------

SCENARIOS = {
    "pcm": "Engine computer holds P0601 and it comes back at every key-on",
    "tcm": "Transmission computer holds P0601; engine computer shows P0700",
    "pcm-silent-tcm": "Engine computer holds P0601 and P0700; transmission computer does not answer OBD",
    "lowvolt": "P0601 with a low-voltage code; returns only once the engine cranks",
    "transient": "P0601 stored once; does not come back after clearing",
    "clean": "No codes stored",
    "clone": "Adapter without working J1850 VPW",
    "limited": "Adapter with J1850 VPW but no bus monitoring",
    "ignition-off": "Good adapter, ignition switched off",
}
STAGES = ("after-clear-keyon", "after-clear-running")


def _freeze(code: str) -> tuple[str, dict[int, bytes]]:
    return code, {
        0x04: bytes([46]),
        0x05: bytes([88 + 40]),
        0x0C: (1850 * 4).to_bytes(2, "big"),
        0x0D: bytes([52]),
    }


def make_sim(spec: str) -> SimElm:
    """Build a simulator from 'scenario' or 'scenario:stage'."""
    name, _, stage = spec.partition(":")
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    if stage and stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; choose from {', '.join(STAGES)}")

    pcm = SimEcu(0x10, pids=_default_pids())
    tcm = SimEcu(0x18)
    vehicle = SimVehicle([pcm, tcm])
    adapter_options: dict = {}

    if name == "pcm":
        pcm.stored, pcm.hard_faults = ["P0601"], ["P0601"]
        pcm.freeze_dtc, pcm.freeze_pids = _freeze("P0601")
    elif name == "tcm":
        pcm.stored, pcm.hard_faults = ["P0700"], ["P0700"]
        tcm.stored, tcm.hard_faults = ["P0601"], ["P0601"]
    elif name == "pcm-silent-tcm":
        pcm.stored, pcm.hard_faults = ["P0601", "P0700"], ["P0601", "P0700"]
        pcm.freeze_dtc, pcm.freeze_pids = _freeze("P0601")
        tcm.answers_obd = False
    elif name == "lowvolt":
        pcm.stored, pcm.run_faults = ["P0601", "P0562"], ["P0601", "P0562"]
        pcm.freeze_dtc, pcm.freeze_pids = _freeze("P0562")
        vehicle.battery_voltage = 11.7
        vehicle.running_voltage = 13.0
        vehicle.voltage_profile = [11.7, 11.6, 8.4, 8.9, 9.8, 12.9, 13.0]
    elif name == "transient":
        pcm.stored = ["P0601"]
        pcm.freeze_dtc, pcm.freeze_pids = _freeze("P0601")
    elif name == "clone":
        adapter_options["vpw_supported"] = False
    elif name == "limited":
        pcm.stored, pcm.hard_faults = ["P0601"], ["P0601"]
        adapter_options["unsupported_at"] = ("MA", "@1")
    elif name == "ignition-off":
        vehicle.ignition_on = False

    if stage:
        vehicle.clear_codes()
        vehicle.key_cycle()
        if stage == "after-clear-running":
            vehicle.voltage_profile = []
            vehicle.start_engine()
    return SimElm(vehicle, **adapter_options)

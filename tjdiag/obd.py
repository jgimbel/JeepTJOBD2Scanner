"""OBD-II services on top of the ELM327 driver.

Every result is kept per source address, so a code is always attributed to the
module that reported it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .elm import J1850_VPW, PROTOCOL_NAMES, Elm327, Response

# SAE J2178 physical address ranges, as far as they matter on this vehicle.
_MODULE_RANGES = (
    (0x10, 0x17, "Engine controller (PCM)"),
    (0x18, 0x1F, "Transmission controller (TCM)"),
    (0x20, 0x27, "Chassis controller"),
    (0x28, 0x2F, "Brake controller (ABS)"),
    (0x40, 0x57, "Body controller"),
    (0x58, 0x5F, "Restraints controller (airbag)"),
    (0x60, 0x6F, "Instrument cluster / displays"),
    (0xF0, 0xFD, "Diagnostic tester"),
)
PCM = 0x10
TCM = 0x18


def module_name(address: int) -> str:
    for low, high, name in _MODULE_RANGES:
        if low <= address <= high:
            return name
    return "Unknown module"


def module_label(address: int) -> str:
    return f"{module_name(address)} [0x{address:02X}]"


def is_engine(address: int) -> bool:
    return 0x10 <= address <= 0x17


def is_transmission(address: int) -> bool:
    return 0x18 <= address <= 0x1F


class VehicleNotResponding(Exception):
    """No module answered an OBD request."""


def connect(elm: Elm327) -> str:
    """Reset the adapter and establish contact with the vehicle.

    Tries SAE J1850 VPW first (what a 2004 Wrangler uses), then lets the
    adapter search. Returns the ELM327 protocol digit that worked.
    """
    elm.reset()
    elm.set_protocol(J1850_VPW)
    if elm.obd("01 00").frames:
        return J1850_VPW
    elm.set_protocol("0")
    if elm.obd("01 00", timeout=20.0).frames:
        protocol = elm.protocol_number()
        if protocol in {"6", "7", "8", "9", "A"}:
            raise VehicleNotResponding(
                f"the vehicle answered on {PROTOCOL_NAMES.get(protocol, protocol)}, "
                "which this tool does not handle; it is not a 2004 Wrangler protocol"
            )
        return protocol
    raise VehicleNotResponding(
        "no module answered. Check that the ignition is ON (the engine can be off) "
        "and the adapter is fully seated. If both are true, this adapter most "
        "likely cannot talk SAE J1850 VPW; run 'tjdiag probe' for details."
    )


# -- trouble codes ----------------------------------------------------------

DTC_DESCRIPTIONS = {
    "P0601": "Internal controller failure: memory checksum error",
    "P0600": "Serial communication link (internal)",
    "P0602": "Control module programming error",
    "P0603": "Control module keep-alive memory error",
    "P0604": "Control module RAM error",
    "P0605": "Control module ROM error",
    "P0606": "Control module processor fault",
    "P0700": "Transmission controller has stored codes and requests the MIL",
    "P0560": "System voltage malfunction",
    "P0562": "System voltage low",
    "P0563": "System voltage high",
    "P0622": "Generator field control circuit",
    "P1594": "Charging system voltage too high",
    "P1682": "Charging system voltage too low",
    "P1698": "No bus messages from the transmission controller",
    "P1694": "No bus messages from the engine controller",
    "P1695": "No bus messages from the body controller",
}

# Codes that say the controller's own self-checks failed.
INTERNAL_FAULT_CODES = {"P0600", "P0601", "P0602", "P0603", "P0604", "P0605", "P0606"}
# Codes that say supply voltage was out of range.
VOLTAGE_CODES = {"P0560", "P0562", "P0563", "P0622", "P1594", "P1682"}
# Codes that say modules lost contact with each other.
BUS_CODES = {"P1694", "P1695", "P1698"}


def decode_dtc(high: int, low: int) -> str:
    system = "PCBU"[high >> 6]
    return f"{system}{(high >> 4) & 0x3}{high & 0xF:X}{low:02X}"


def describe_dtc(code: str) -> str:
    return DTC_DESCRIPTIONS.get(code, "no description in this tool; look the code up")


@dataclass
class DtcReport:
    """Codes per module for one service (stored or pending)."""

    status: str
    error: str | None = None
    responders: list[int] = field(default_factory=list)
    codes: dict[int, list[str]] = field(default_factory=dict)
    bad_checksums: int = 0

    def all_codes(self) -> set[str]:
        return {code for codes in self.codes.values() for code in codes}

    def sources_of(self, code: str) -> list[int]:
        return [src for src, codes in self.codes.items() if code in codes]


def _count_bad(response: Response) -> int:
    return sum(1 for frame in response.frames if not frame.checksum_ok)


def read_dtcs(elm: Elm327, mode: int = 0x03) -> DtcReport:
    """Mode 03 (stored codes) or mode 07 (pending codes)."""
    response = elm.obd(f"{mode:02X}")
    report = DtcReport(response.status, response.error, bad_checksums=_count_bad(response))
    for frame in response.frames:
        if not frame.data or frame.data[0] != mode + 0x40:
            continue
        if frame.source not in report.responders:
            report.responders.append(frame.source)
        codes = report.codes.setdefault(frame.source, [])
        payload = frame.data[1:]
        for i in range(0, len(payload) - 1, 2):
            high, low = payload[i], payload[i + 1]
            if high == 0 and low == 0:
                continue  # padding
            code = decode_dtc(high, low)
            if code not in codes:
                codes.append(code)
    return report


def clear_dtcs(elm: Elm327) -> tuple[list[int], Response]:
    """Mode 04. Returns the modules that acknowledged, and the raw response."""
    response = elm.obd("04")
    acknowledged = [f.source for f in response.frames if f.data and f.data[0] == 0x44]
    return acknowledged, response


# -- parameter data ---------------------------------------------------------


@dataclass(frozen=True)
class Pid:
    key: str
    unit: str
    size: int
    decode: Callable[[bytes], float]


PIDS: dict[int, Pid] = {
    0x04: Pid("engine_load", "%", 1, lambda d: d[0] * 100 / 255),
    0x05: Pid("coolant_temp", "C", 1, lambda d: d[0] - 40),
    0x06: Pid("short_fuel_trim_1", "%", 1, lambda d: (d[0] - 128) * 100 / 128),
    0x07: Pid("long_fuel_trim_1", "%", 1, lambda d: (d[0] - 128) * 100 / 128),
    0x0B: Pid("manifold_pressure", "kPa", 1, lambda d: d[0]),
    0x0C: Pid("rpm", "rpm", 2, lambda d: (d[0] * 256 + d[1]) / 4),
    0x0D: Pid("vehicle_speed", "km/h", 1, lambda d: d[0]),
    0x0E: Pid("timing_advance", "deg", 1, lambda d: d[0] / 2 - 64),
    0x0F: Pid("intake_air_temp", "C", 1, lambda d: d[0] - 40),
    0x11: Pid("throttle_position", "%", 1, lambda d: d[0] * 100 / 255),
    0x1F: Pid("run_time", "s", 2, lambda d: d[0] * 256 + d[1]),
    0x21: Pid("distance_with_mil", "km", 2, lambda d: d[0] * 256 + d[1]),
    0x42: Pid("module_voltage", "V", 2, lambda d: (d[0] * 256 + d[1]) / 1000),
}
PID_BY_KEY = {pid.key: number for number, pid in PIDS.items()}


def _values(response: Response, prefix: bytes) -> dict[int, bytes]:
    """Payload after `prefix` for every module that answered with it."""
    out: dict[int, bytes] = {}
    for frame in response.frames:
        if frame.data[: len(prefix)] == prefix:
            out.setdefault(frame.source, frame.data[len(prefix) :])
    return out


def read_pid_raw(elm: Elm327, pid: int, freeze: bool = False) -> dict[int, bytes]:
    if freeze:
        response = elm.obd(f"02 {pid:02X} 00")
        return _values(response, bytes([0x42, pid, 0x00]))
    response = elm.obd(f"01 {pid:02X}")
    return _values(response, bytes([0x41, pid]))


def read_pid(elm: Elm327, pid: int, freeze: bool = False) -> dict[int, float]:
    """Decoded value of a known PID per module. Short replies are skipped."""
    spec = PIDS[pid]
    return {
        source: round(spec.decode(raw), 2)
        for source, raw in read_pid_raw(elm, pid, freeze).items()
        if len(raw) >= spec.size
    }


def supported_pids(elm: Elm327, freeze: bool = False) -> dict[int, list[int]]:
    """PIDs each module says it supports (mode 01, or mode 02 freeze frame)."""
    supported: dict[int, list[int]] = {}
    base = 0x00
    while base <= 0x40:
        replies = read_pid_raw(elm, base, freeze)
        more = False
        for source, raw in replies.items():
            if len(raw) < 4:
                continue
            bits = int.from_bytes(raw[:4], "big")
            pids = supported.setdefault(source, [])
            for offset in range(1, 33):
                if bits & (1 << (32 - offset)):
                    pids.append(base + offset)
            more = more or bool(bits & 1)
        if not more:
            break
        base += 0x20
    return supported


_MONITORS = (
    "catalyst",
    "heated_catalyst",
    "evap_system",
    "secondary_air",
    "ac_refrigerant",
    "oxygen_sensor",
    "oxygen_sensor_heater",
    "egr_system",
)
_CONTINUOUS = ("misfire", "fuel_system", "components")


def decode_monitor_status(raw: bytes) -> dict:
    """PID 01: MIL state, code count and readiness monitors."""
    a, b, c, d = raw[0], raw[1], raw[2], raw[3]
    monitors: dict[str, str] = {}
    for bit, name in enumerate(_CONTINUOUS):
        if b & (1 << bit):
            monitors[name] = "incomplete" if b & (1 << (bit + 4)) else "complete"
    for bit, name in enumerate(_MONITORS):
        if c & (1 << bit):
            monitors[name] = "incomplete" if d & (1 << bit) else "complete"
    return {"mil_on": bool(a & 0x80), "dtc_count": a & 0x7F, "monitors": monitors}


def read_monitor_status(elm: Elm327) -> dict[int, dict]:
    return {
        source: decode_monitor_status(raw)
        for source, raw in read_pid_raw(elm, 0x01).items()
        if len(raw) >= 4
    }


def read_live(elm: Elm327, supported: dict[int, list[int]]) -> dict[int, dict[str, float]]:
    """Every known PID that at least one module supports."""
    wanted = sorted({p for pids in supported.values() for p in pids} & set(PIDS))
    live: dict[int, dict[str, float]] = {}
    for pid in wanted:
        for source, value in read_pid(elm, pid).items():
            live.setdefault(source, {})[PIDS[pid].key] = value
    return live


def read_freeze_frame(elm: Elm327) -> dict[int, dict]:
    """Mode 02: the code that triggered the freeze frame and the data stored with it."""
    frames: dict[int, dict] = {}
    for source, raw in read_pid_raw(elm, 0x02, freeze=True).items():
        if len(raw) >= 2 and (raw[0] or raw[1]):
            frames[source] = {"dtc": decode_dtc(raw[0], raw[1]), "data": {}}
    if not frames:
        return frames
    supported = supported_pids(elm, freeze=True)
    wanted = sorted({p for pids in supported.values() for p in pids} & set(PIDS))
    for pid in wanted:
        for source, value in read_pid(elm, pid, freeze=True).items():
            if source in frames:
                frames[source]["data"][PIDS[pid].key] = value
    return frames


def read_vin(elm: Elm327) -> dict[int, str]:
    """Mode 09 PID 02. Many 2004 vehicles do not implement it; then this is empty."""
    response = elm.obd("09 02")
    parts: dict[int, dict[int, bytes]] = {}
    for frame in response.frames:
        if len(frame.data) >= 4 and frame.data[:2] == b"\x49\x02":
            parts.setdefault(frame.source, {})[frame.data[2]] = frame.data[3:]
    vins: dict[int, str] = {}
    for source, chunks in parts.items():
        raw = b"".join(chunks[seq] for seq in sorted(chunks))
        text = "".join(chr(b) for b in raw if 0x20 <= b < 0x7F)
        if text:
            vins[source] = text
    return vins

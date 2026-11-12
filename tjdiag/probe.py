"""Adapter capability test.

Answers one question with evidence instead of trust: can this particular
adapter do what the P0601 work needs on this particular Jeep?
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import obd
from .elm import J1850_VPW, PROTOCOL_NAMES, Elm327, ElmError

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"

USABLE = "usable"
LIMITED = "limited"
UNUSABLE = "unusable"

_VERDICT_TEXT = {
    USABLE: "This adapter can do everything the OBD2 diagnosis needs.",
    LIMITED: "This adapter can read and clear codes, but something optional is missing (see WARN lines).",
    UNUSABLE: "This adapter cannot be used for this job as tested (see FAIL lines).",
}


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class ProbeResult:
    checks: list[Check] = field(default_factory=list)
    verdict: str = UNUSABLE

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def summary(self) -> str:
        return _VERDICT_TEXT[self.verdict]

    def format(self) -> str:
        lines = [f"[{c.status:4}] {c.name}: {c.detail}" for c in self.checks]
        lines += ["", f"Verdict: {self.verdict.upper()}. {self.summary}"]
        return "\n".join(lines)


def run_probe(elm: Elm327, monitor_seconds: float = 3.0) -> ProbeResult:
    result = ProbeResult()

    # 1. Is it an ELM327-type device at all?
    try:
        version = elm.reset()
    except ElmError as exc:
        result.add("Adapter identity", FAIL, str(exc))
        return result
    result.add("Adapter identity", PASS, f"reports itself as '{version}'")
    try:
        result.add("Device description", INFO, elm.at("@1"))
    except ElmError:
        result.add("Device description", INFO, "not provided (common on clone chips)")

    # 2. Supply voltage at the diagnostic connector.
    voltage = elm.voltage()
    if voltage is None:
        result.add("Battery voltage", WARN, "adapter cannot measure voltage (ATRV)")
    elif voltage < 11.8:
        result.add(
            "Battery voltage",
            WARN,
            f"{voltage:.1f} V at the connector is low; charge the battery before "
            "judging the computer",
        )
    else:
        result.add("Battery voltage", INFO, f"{voltage:.1f} V at the connector")

    # 3. Can it talk SAE J1850 VPW to this vehicle?
    protocol = None
    try:
        elm.set_protocol(J1850_VPW)
        response = elm.obd("01 00")
    except ElmError as exc:
        response = None
        result.add("J1850 VPW", FAIL, f"adapter refused the protocol: {exc}")
    if response is not None and response.frames:
        protocol = J1850_VPW
        result.add("J1850 VPW", PASS, "the vehicle answered on SAE J1850 VPW")
    elif response is not None:
        elm.set_protocol("0")
        response = elm.obd("01 00", timeout=20.0)
        if response.frames:
            protocol = elm.protocol_number()
            name = PROTOCOL_NAMES.get(protocol, protocol)
            if protocol in {"6", "7", "8", "9", "A"}:
                result.add("J1850 VPW", FAIL, f"vehicle answered on {name}; not supported here")
                return result
            result.add(
                "J1850 VPW",
                WARN,
                f"no answer on VPW, but the vehicle answered on {name}; "
                "the tool will use that",
            )
        else:
            result.add(
                "J1850 VPW",
                FAIL,
                "no module answered. First make sure the ignition is ON (engine can "
                "be off) and retry. If it still fails, this adapter does not "
                "implement J1850 VPW (many clones leave it out) and cannot be used "
                "on this Jeep",
            )
            return result
    else:
        return result

    # 4. Do replies carry a usable header and checksum?
    sources = sorted(response.by_source())
    result.add(
        "Module addressing",
        PASS,
        "replies identify their sender: " + ", ".join(obd.module_label(s) for s in sources),
    )
    limited = False
    if all(frame.checksum_ok for frame in response.frames):
        result.add("Frame checksums", PASS, "every reply passed its checksum")
    else:
        limited = True
        result.add(
            "Frame checksums",
            WARN,
            "replies did not carry a valid checksum; corrupted data cannot be "
            "detected with this adapter",
        )

    # 5. Can it read codes right now?
    stored = obd.read_dtcs(elm, 0x03)
    if stored.status == "error":
        result.add("Read stored codes", FAIL, f"adapter error: {stored.error}")
        return result
    if stored.codes and stored.all_codes():
        found = "; ".join(
            f"{obd.module_label(src)}: {', '.join(codes)}"
            for src, codes in stored.codes.items()
            if codes
        )
        result.add("Read stored codes", PASS, found)
    else:
        result.add("Read stored codes", PASS, "request works; no codes stored right now")

    # 6. Passive bus monitoring (optional).
    try:
        frames, overflows = elm.monitor(monitor_seconds, max_frames=25)
    except ElmError as exc:
        frames, overflows = [], 0
        limited = True
        result.add("Bus monitoring", WARN, f"{exc}; 'tjdiag sniff' will not work")
    else:
        if frames:
            note = f" ({overflows} buffer overflows)" if overflows else ""
            result.add("Bus monitoring", PASS, f"captured {len(frames)} bus frames{note}")
        else:
            limited = True
            result.add("Bus monitoring", WARN, "no bus traffic captured; 'tjdiag sniff' may not work")

    result.add(
        "Chrysler SCI link",
        INFO,
        "not available on any ELM327-type adapter; reading the computer's memory "
        "needs the SCI interface board (docs/sci-interface-hardware.md)",
    )
    result.verdict = LIMITED if limited or protocol != J1850_VPW else USABLE
    return result

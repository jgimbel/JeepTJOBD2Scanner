"""Turns saved snapshots, clears and voltage logs into a diagnosis.

The reasoning follows docs/diagnostic-flow.md:

1. Which module reports P0601?
2. After clearing, does it come back, and when?
     - with the key on, before the engine is started  -> the module fails its
       own power-up self-test
     - only once the engine has cranked or run          -> look at its supply
     - not at all                                       -> one-time event
3. Is there independent evidence of a supply problem (voltage codes, low or
   dipping voltage)?

Everything here is a rule of thumb applied to the data on hand. It narrows the
search; it does not replace measuring the computer's power and grounds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import obd

TARGET = "P0601"

# Conventional 12 V system thresholds.
RESTING_LOW = 12.0  # engine off: below this the battery is discharged
CRANKING_LOW = 9.6  # lowest acceptable voltage while cranking
CHARGING_LOW = 13.2  # engine running: below this the system is not charging
CHARGING_HIGH = 15.0  # engine running: above this the system is overcharging

NO_CODE = "no_p0601"
NEEDS_CLEAR_TEST = "needs_clear_test"
HARD_FAULT_PCM = "pcm_hard_fault"
HARD_FAULT_TCM = "tcm_hard_fault"
SUPPLY_SUSPECTED = "supply_suspected"
RETURNED_RUNNING = "returned_while_running"
NOT_RETURNED = "not_returned"
NO_DATA = "no_data"


@dataclass
class Diagnosis:
    verdict: str
    headline: str
    evidence: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [self.headline, ""]
        if self.evidence:
            lines.append("What the data shows:")
            lines += [f"  - {item}" for item in self.evidence]
            lines.append("")
        lines.append("What to do next:")
        lines += [f"  {i}. {step}" for i, step in enumerate(self.next_steps, 1)]
        return "\n".join(lines)


def _address(key: str) -> int:
    return int(key, 16)


def holders(snapshot: dict, code: str = TARGET, pending: bool = False) -> list[int]:
    """Addresses of the modules that report `code` in this snapshot."""
    field_name = "pending" if pending else "stored"
    return [
        _address(key)
        for key, module in snapshot["modules"].items()
        if code in module[field_name]
    ]


def all_codes(snapshot: dict) -> set[str]:
    return {
        code
        for module in snapshot["modules"].values()
        for code in module["stored"] + module["pending"]
    }


def has_target(snapshot: dict) -> bool:
    return bool(holders(snapshot) or holders(snapshot, pending=True))


def _location(snapshot: dict, evidence: list[str]) -> list[int]:
    stored = holders(snapshot)
    pending = holders(snapshot, pending=True)
    where = stored or pending
    kind = "stored" if stored else "pending"
    for address in where:
        evidence.append(f"{TARGET} is {kind} in the {obd.module_label(address)}.")

    answered = [_address(key) for key in snapshot["modules"]]
    tcm_answered = any(obd.is_transmission(a) for a in answered)
    if "P0700" in all_codes(snapshot):
        if tcm_answered:
            evidence.append(
                "P0700 is set: the transmission computer has codes of its own "
                "(listed in the snapshot)."
            )
        else:
            evidence.append(
                "P0700 is set, so the transmission computer has codes of its own, "
                "but it did not answer generic OBD requests. Those codes need a "
                "Chrysler-capable scan tool to read."
            )
    elif not tcm_answered:
        evidence.append(
            "The transmission computer did not answer generic OBD requests. With no "
            "P0700 set it is not asking for the check engine lamp."
        )
    return where


def _supply_evidence(events: list[dict], evidence: list[str]) -> bool:
    """Append voltage-related findings; return True if any point at the supply."""
    suspicious = False
    snapshots = [e for e in events if e["event"] == "snapshot"]
    latest = snapshots[-1]

    voltage_codes = sorted(all_codes(latest) & obd.VOLTAGE_CODES)
    if voltage_codes:
        suspicious = True
        evidence.append(
            "Voltage-related codes are stored alongside it: " + ", ".join(voltage_codes) + "."
        )
    bus_codes = sorted(all_codes(latest) & obd.BUS_CODES)
    if bus_codes:
        evidence.append("Bus communication codes are also stored: " + ", ".join(bus_codes) + ".")

    for snap in snapshots:
        voltage = snap["adapter"].get("voltage")
        if voltage is None:
            continue
        when = snap["taken_at"]
        if snap["engine_running"]:
            if voltage < CHARGING_LOW:
                suspicious = True
                evidence.append(
                    f"With the engine running the connector showed {voltage:.1f} V ({when}); "
                    f"a healthy charging system holds {CHARGING_LOW}-{CHARGING_HIGH} V."
                )
            elif voltage > CHARGING_HIGH:
                suspicious = True
                evidence.append(
                    f"With the engine running the connector showed {voltage:.1f} V ({when}), "
                    "which is overcharging."
                )
        elif snap["engine_running"] is False and voltage < RESTING_LOW:
            suspicious = True
            evidence.append(
                f"With the engine off the connector showed {voltage:.1f} V ({when}); "
                f"a charged battery rests above {RESTING_LOW} V."
            )

    for watch in (e for e in events if e["event"] == "watch"):
        low = watch.get("min_voltage")
        if low is not None and low < CRANKING_LOW:
            suspicious = True
            evidence.append(
                f"The voltage log from {watch['taken_at']} dipped to {low:.1f} V; "
                f"it should stay above {CRANKING_LOW} V even while cranking."
            )
        if watch.get("comm_dropouts"):
            suspicious = True
            evidence.append(
                f"During that log the engine computer stopped answering "
                f"{watch['comm_dropouts']} time(s), which is what a computer does "
                "when its supply drops out."
            )
    if not suspicious:
        evidence.append("No voltage-related codes and no low or dipping voltage in the saved data.")
    return suspicious


_MULTIMETER_STEPS = [
    "Measure the battery with a multimeter: above 12.4 V resting, above 9.6 V while "
    "cranking, 13.5-14.7 V with the engine running.",
    "Voltage-drop test the grounds with the engine running and loads on: battery "
    "negative to engine block, to body, and to the computer's ground pins. More than "
    "about 0.1 V on any of them is a bad connection.",
]


def _module_word(address: int) -> str:
    return "transmission computer" if obd.is_transmission(address) else "engine computer"


def diagnose(events: list[dict]) -> Diagnosis:
    """Diagnose from saved events (snapshots, clears, watch logs), oldest first."""
    snapshots = [e for e in events if e["event"] == "snapshot"]
    if not snapshots:
        return Diagnosis(
            NO_DATA,
            "No snapshots saved yet.",
            next_steps=["Run 'tjdiag snapshot' with the ignition ON."],
        )
    latest = snapshots[-1]
    if not latest["modules"]:
        return Diagnosis(
            NO_DATA,
            "The latest snapshot contains no answers from the vehicle.",
            next_steps=["Run 'tjdiag probe' to check the adapter, then take a new snapshot."],
        )

    evidence: list[str] = []
    last_clear_index = max(
        (i for i, e in enumerate(events) if e["event"] == "clear"), default=None
    )

    # -- no clear test done yet -------------------------------------------
    if last_clear_index is None:
        if not has_target(latest):
            ever = any(has_target(s) for s in snapshots)
            evidence.append(
                f"{TARGET} is not stored or pending in the latest snapshot"
                + (", although an earlier snapshot had it." if ever else ".")
            )
            _supply_evidence(events, evidence)
            return Diagnosis(
                NO_CODE,
                f"{TARGET} is not currently set.",
                evidence,
                [
                    "If the check engine lamp is on, read the codes listed in the snapshot instead.",
                    "Take another snapshot after the next few drives to see whether it returns.",
                ],
            )
        where = _location(latest, evidence)
        _supply_evidence(events, evidence)
        who = " and ".join(_module_word(a) for a in where)
        return Diagnosis(
            NEEDS_CLEAR_TEST,
            f"{TARGET} is set by the {who}. Whether the computer is actually faulty "
            "is not known yet; the clear-and-recheck test decides that.",
            evidence,
            _MULTIMETER_STEPS
            + [
                "Key ON, engine off: run 'tjdiag clear --yes' (it saves a snapshot first).",
                "Key off for 30 seconds, key ON without starting: run 'tjdiag snapshot'.",
                "Run 'tjdiag watch', start the engine while it logs, let it idle a minute.",
                "Run 'tjdiag snapshot' again with the engine running, then 'tjdiag diagnose'.",
            ],
        )

    # -- clear test done ----------------------------------------------------
    after = [
        e for e in events[last_clear_index + 1 :] if e["event"] == "snapshot" and e["modules"]
    ]
    if not after:
        return Diagnosis(
            NEEDS_CLEAR_TEST,
            "Codes were cleared, but no snapshot has been taken since.",
            evidence,
            [
                "Key off for 30 seconds, key ON without starting: run 'tjdiag snapshot'.",
                "Run 'tjdiag watch', start the engine while it logs, let it idle a minute.",
                "Run 'tjdiag snapshot' again with the engine running, then 'tjdiag diagnose'.",
            ],
        )

    returned = next((s for s in after if has_target(s)), None)
    engine_off_checks = [s for s in after if s["engine_running"] is False]
    running_checks = [s for s in after if s["engine_running"]]
    evidence.append(
        f"Codes were cleared at {events[last_clear_index]['taken_at']}; "
        f"{len(after)} snapshot(s) taken since."
    )

    if returned is None:
        _supply_evidence(events, evidence)
        evidence.append(f"{TARGET} has not come back in any snapshot since the clear.")
        steps = []
        if not running_checks:
            steps.append("Take a snapshot with the engine running; so far none was taken after the clear.")
        steps += [
            "Drive normally for a few days (several cold starts), then take another snapshot.",
            "Fix whatever caused the original electrical event (battery, terminals, "
            "grounds) so it cannot repeat.",
            "If it returns, run 'tjdiag diagnose' again; the timing of its return is the clue.",
        ]
        return Diagnosis(
            NOT_RETURNED,
            f"{TARGET} has not returned since it was cleared. That fits a one-time "
            "upset from the electrical event, not a failed computer.",
            evidence,
            steps,
        )

    where = _location(returned, evidence)
    supply = _supply_evidence(events, evidence)
    address = where[0]
    who = _module_word(address)

    if returned["engine_running"] is False:
        evidence.append(
            f"It was back in the snapshot of {returned['taken_at']}, taken with the key on "
            "and the engine not running."
        )
        steps = [
            f"Before condemning the {who}, verify its power and ground at its own "
            "connector with a multimeter (pins from the 2004 TJ wiring diagrams): "
            "battery feed, ignition feed, and every ground, under load.",
        ]
        if supply:
            steps.append(
                "The saved data also shows a supply problem; fix that first and repeat "
                "the clear test, because a low supply can make a healthy computer fail "
                "its self-test."
            )
        if obd.is_transmission(address):
            steps.append(
                "If power and grounds are good, the transmission computer itself is "
                "the fault: repair or replace it. It is a separate module from the "
                "engine computer."
            )
            verdict = HARD_FAULT_TCM
        else:
            steps += [
                "If power and grounds are good, the fault is inside the engine computer. "
                "The memory test (docs/memory-test.md) reads its memory over the SCI "
                "link to show which blocks are affected.",
                "The repair options at that point are a reflash by a dealer or PCM "
                "specialist, or a replacement computer programmed with this Jeep's VIN, "
                "mileage and immobilizer data.",
            ]
            verdict = HARD_FAULT_PCM
        return Diagnosis(
            verdict,
            f"{TARGET} came back with the key on, before the engine was started. The "
            f"{who} is failing its own power-up self-test.",
            evidence,
            steps,
        )

    if engine_off_checks and not any(has_target(s) for s in engine_off_checks):
        evidence.append(
            "A key-on, engine-off snapshot after the clear was clean; the code only "
            "appeared once the engine had run."
        )
    else:
        evidence.append(
            "No key-on, engine-off snapshot was taken between the clear and its return, "
            "so it is not known whether it was already back before the engine started."
        )
    if supply:
        return Diagnosis(
            SUPPLY_SUSPECTED,
            f"{TARGET} came back only after the engine cranked or ran, and the data "
            f"shows a supply problem. Suspect the {who}'s power and grounds before "
            "the computer itself.",
            evidence,
            _MULTIMETER_STEPS
            + [
                "Clean and tighten both battery terminals and the engine-to-body and "
                "battery-to-block ground straps.",
                "Load-test the battery; replace it if it fails.",
                "Repeat the clear test afterwards. If the code stays away, the computer was never the fault.",
            ],
        )
    return Diagnosis(
        RETURNED_RUNNING,
        f"{TARGET} came back after the engine ran, and the saved data shows no "
        "supply problem. The cause is intermittent and not yet pinned down.",
        evidence,
        [
            "Run 'tjdiag watch' through several cold starts; the adapter samples slowly "
            "and can miss a brief dip, so more logs mean more chances to catch one.",
        ]
        + _MULTIMETER_STEPS
        + [
            f"With the engine idling, wiggle the {who}'s connectors and harness while "
            "watching for a stumble or a new code.",
            "If the supply checks out and it keeps returning, treat it as an internal "
            "fault and go to the memory test (docs/memory-test.md).",
        ],
    )

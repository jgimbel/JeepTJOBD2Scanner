"""Voltage and engine-speed logger.

P0601 that follows an electrical event is often the computer reacting to its
supply dipping, not a failed computer. This logs the voltage at the diagnostic
connector alongside engine speed, so a dip during cranking (or the computer
going silent while the voltage is low) shows up as data.
"""

from __future__ import annotations

import csv
import time
from typing import Callable, TextIO

from . import obd
from .elm import Elm327
from .snapshot import now_iso

FIELDS = ["seconds", "adapter_voltage", "rpm", "module_voltage", "pcm_answered"]


def run_watch(
    elm: Elm327,
    seconds: float,
    csv_file: TextIO,
    max_samples: int | None = None,
    on_sample: Callable[[dict], None] | None = None,
    min_interval: float = 0.0,
) -> dict:
    """Log until `seconds` have passed or `max_samples` were taken. Returns a summary."""
    writer = csv.DictWriter(csv_file, fieldnames=FIELDS)
    writer.writeheader()

    has_module_voltage = any(0x42 in pids for pids in obd.supported_pids(elm).values())
    started_at = now_iso()
    start = time.monotonic()
    samples = 0
    voltages: list[float] = []
    rpms: list[float] = []
    dropouts = 0
    lowest_while_silent: float | None = None

    while time.monotonic() - start < seconds:
        if max_samples is not None and samples >= max_samples:
            break
        elapsed = round(time.monotonic() - start, 2)
        voltage = elm.voltage()
        rpm_by_module = obd.read_pid(elm, 0x0C)
        rpm = max(rpm_by_module.values()) if rpm_by_module else None
        module_voltage = None
        if has_module_voltage:
            values = obd.read_pid(elm, 0x42)
            module_voltage = min(values.values()) if values else None

        answered = rpm is not None
        if not answered:
            dropouts += 1
            if voltage is not None and (lowest_while_silent is None or voltage < lowest_while_silent):
                lowest_while_silent = voltage
        if voltage is not None:
            voltages.append(voltage)
        if rpm is not None:
            rpms.append(rpm)

        row = {
            "seconds": elapsed,
            "adapter_voltage": voltage,
            "rpm": rpm,
            "module_voltage": module_voltage,
            "pcm_answered": int(answered),
        }
        writer.writerow(row)
        csv_file.flush()
        samples += 1
        if on_sample:
            on_sample(row)
        # A real adapter needs a few hundred milliseconds per sample anyway;
        # this only paces sources that answer instantly.
        spare = min_interval - (time.monotonic() - start - elapsed)
        if spare > 0:
            time.sleep(spare)

    return {
        "tool": "tjdiag",
        "event": "watch",
        "taken_at": started_at,
        "seconds": round(time.monotonic() - start, 1),
        "samples": samples,
        "min_voltage": min(voltages) if voltages else None,
        "max_voltage": max(voltages) if voltages else None,
        "max_rpm": max(rpms) if rpms else None,
        "engine_ran": bool(rpms) and max(rpms) > 0,
        "comm_dropouts": dropouts,
        "lowest_voltage_while_silent": lowest_while_silent,
    }


def format_watch(summary: dict) -> str:
    lines = [f"Logged {summary['samples']} samples over {summary['seconds']} s"]
    if summary["min_voltage"] is not None:
        lines.append(
            f"Voltage at the connector: lowest {summary['min_voltage']:.1f} V, "
            f"highest {summary['max_voltage']:.1f} V"
        )
    lines.append("Engine ran during the log" if summary["engine_ran"] else "Engine did not run during the log")
    if summary["comm_dropouts"]:
        detail = ""
        if summary["lowest_voltage_while_silent"] is not None:
            detail = f" (lowest voltage while silent: {summary['lowest_voltage_while_silent']:.1f} V)"
        lines.append(
            f"The engine computer failed to answer {summary['comm_dropouts']} time(s){detail}"
        )
    return "\n".join(lines)

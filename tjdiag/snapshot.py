"""Full read of everything OBD-II offers, saved as JSON with a readable summary."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import __version__, obd
from .elm import PROTOCOL_NAMES, Elm327


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def module_key(address: int) -> str:
    return f"0x{address:02X}"


def take_snapshot(elm: Elm327, protocol: str, label: str | None = None) -> dict:
    """Read codes, freeze frame, readiness and live data from every module.

    `elm` must already be connected (see obd.connect).
    """
    warnings: list[str] = []
    voltage = elm.voltage()

    stored = obd.read_dtcs(elm, 0x03)
    pending = obd.read_dtcs(elm, 0x07)
    for name, report in (("stored", stored), ("pending", pending)):
        if report.status == "error":
            warnings.append(f"reading {name} codes failed: {report.error}")
        if report.bad_checksums:
            warnings.append(
                f"{report.bad_checksums} reply frame(s) for {name} codes had no valid checksum"
            )

    supported = obd.supported_pids(elm)
    monitors = obd.read_monitor_status(elm)
    live = obd.read_live(elm, supported)
    freeze = obd.read_freeze_frame(elm)
    vins = obd.read_vin(elm)

    addresses = sorted(
        set(stored.responders)
        | set(pending.responders)
        | set(supported)
        | set(monitors)
        | set(freeze)
        | set(vins)
    )
    modules = {}
    for address in addresses:
        modules[module_key(address)] = {
            "name": obd.module_name(address),
            "stored": stored.codes.get(address, []),
            "pending": pending.codes.get(address, []),
            "monitor_status": monitors.get(address),
            "supported_pids": [f"{pid:02X}" for pid in supported.get(address, [])],
            "live": live.get(address, {}),
            "freeze_frame": freeze.get(address),
            "vin": vins.get(address),
        }

    rpms = [m["live"]["rpm"] for m in modules.values() if "rpm" in m["live"]]
    engine_running = (max(rpms) > 0) if rpms else None
    if not modules:
        warnings.append("no module answered any request")

    return {
        "tool": "tjdiag",
        "tool_version": __version__,
        "event": "snapshot",
        "taken_at": now_iso(),
        "label": label,
        "adapter": {
            "version": elm.version,
            "protocol": protocol,
            "protocol_name": PROTOCOL_NAMES.get(protocol, protocol),
            "voltage": voltage,
        },
        "engine_running": engine_running,
        "modules": modules,
        "warnings": warnings,
    }


def reserve_path(out_dir: Path, kind: str) -> Path:
    """An unused timestamped JSON path for an event of this kind."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{kind}-{stamp}.json"
    counter = 1
    while path.exists():
        counter += 1
        path = out_dir / f"{kind}-{stamp}-{counter}.json"
    return path


def save_event(event: dict, out_dir: Path, kind: str, path: Path | None = None) -> Path:
    """Write an event (snapshot, clear, watch) to a timestamped JSON file."""
    path = path or reserve_path(out_dir, kind)
    # Timestamps only resolve to a second, so order is kept explicitly.
    event["sequence"] = len(list(out_dir.glob("*.json")))
    path.write_text(json.dumps(event, indent=2) + "\n")
    return path


def load_events(out_dir: Path) -> list[dict]:
    """Every saved event, oldest first."""
    events = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            event = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) and event.get("tool") == "tjdiag":
            event["_file"] = path.name
            events.append(event)
    events = [e for e in events if e.get("event")]
    events.sort(key=lambda e: (e.get("sequence", 0), e["_file"]))
    return events


def format_snapshot(snapshot: dict) -> str:
    adapter = snapshot["adapter"]
    lines = [
        f"Snapshot taken {snapshot['taken_at']}"
        + (f" ({snapshot['label']})" if snapshot.get("label") else ""),
        f"Adapter: {adapter['version']} on {adapter['protocol_name']}",
    ]
    if adapter["voltage"] is not None:
        lines.append(f"Voltage at the connector: {adapter['voltage']:.1f} V")
    state = {True: "running", False: "not running", None: "unknown"}[snapshot["engine_running"]]
    lines.append(f"Engine: {state}")

    if not snapshot["modules"]:
        lines.append("No module answered.")
    for key, module in snapshot["modules"].items():
        lines += ["", f"{module['name']} [{key}]"]
        for title, codes in (("Stored codes", module["stored"]), ("Pending codes", module["pending"])):
            if codes:
                lines.append(f"  {title}:")
                lines += [f"    {code}  {obd.describe_dtc(code)}" for code in codes]
            else:
                lines.append(f"  {title}: none")
        status = module.get("monitor_status")
        if status:
            lamp = "ON" if status["mil_on"] else "off"
            incomplete = [n for n, s in status["monitors"].items() if s == "incomplete"]
            lines.append(
                f"  Check engine lamp: {lamp}; codes counted: {status['dtc_count']}; "
                f"readiness monitors incomplete: {', '.join(incomplete) or 'none'}"
            )
        freeze = module.get("freeze_frame")
        if freeze:
            data = ", ".join(f"{k}={v}" for k, v in freeze["data"].items())
            lines.append(f"  Freeze frame was captured for {freeze['dtc']}: {data or 'no data'}")
        if module.get("vin"):
            lines.append(f"  VIN: {module['vin']}")
        if module["live"]:
            lines.append("  Now: " + ", ".join(f"{k}={v}" for k, v in module["live"].items()))

    for warning in snapshot["warnings"]:
        lines.append(f"WARNING: {warning}")
    return "\n".join(lines)

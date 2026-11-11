"""Passive capture of the PCI bus.

Everyday Chrysler PCI traffic uses a single identifier byte per message and no
sender address, and the identifiers are proprietary. This tool therefore
records and counts them without claiming to know what each one means. The
useful signals are simpler: is the bus alive, are frames arriving intact
(checksums), and does the set of identifiers change between a good and a bad
state.
"""

from __future__ import annotations

from typing import TextIO

from .elm import BusFrame, Elm327
from .snapshot import now_iso


def run_sniff(
    elm: Elm327, seconds: float, log: TextIO | None = None, max_frames: int | None = None
) -> dict:
    frames, overflows = elm.monitor(seconds, max_frames=max_frames)
    if log is not None:
        for frame in frames:
            log.write(frame.hex + ("" if frame.checksum_ok else "   <bad checksum>") + "\n")
    return summarize(frames, overflows, seconds)


def summarize(frames: list[BusFrame], overflows: int, seconds: float) -> dict:
    by_id: dict[int, dict] = {}
    for frame in frames:
        entry = by_id.setdefault(
            frame.message_id, {"count": 0, "bad_checksums": 0, "example": frame.hex}
        )
        entry["count"] += 1
        if not frame.checksum_ok:
            entry["bad_checksums"] += 1
    return {
        "tool": "tjdiag",
        "event": "sniff",
        "taken_at": now_iso(),
        "seconds": seconds,
        "frames": len(frames),
        "bad_checksums": sum(1 for f in frames if not f.checksum_ok),
        "buffer_overflows": overflows,
        "identifiers": {f"0x{mid:02X}": by_id[mid] for mid in sorted(by_id)},
    }


def format_sniff(summary: dict) -> str:
    if not summary["frames"]:
        return (
            "No bus traffic captured. With the ignition ON the PCI bus should be busy; "
            "either the adapter cannot monitor, or the bus is silent."
        )
    lines = [
        f"Captured {summary['frames']} frames in {summary['seconds']:.0f} s "
        f"({summary['bad_checksums']} with a bad checksum, "
        f"{summary['buffer_overflows']} adapter buffer overflows)",
        "",
        "  id     count  bad  example",
    ]
    for ident, entry in summary["identifiers"].items():
        lines.append(
            f"  {ident}  {entry['count']:6}  {entry['bad_checksums']:3}  {entry['example']}"
        )
    return "\n".join(lines)

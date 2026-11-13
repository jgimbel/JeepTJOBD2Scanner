"""Reading the engine computer's memory over SCI: probing and resumable dumps.

Every read is one request for one byte, and the answer repeats the address it
belongs to. A byte is only accepted when that echo matches; anything else is
re-requested and, failing that, recorded as missing. Nothing is ever guessed
or filled in.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import sci
from .scanner import Scanner
from .snapshot import now_iso

FILL = 0xFF  # placeholder in the image file for bytes that were never read
REPLY_TIMEOUT = 1.5  # seconds; covers the firmware's 300 ms EEPROM allowance
DEFAULT_INTERVAL_MS = 25
MIN_BATTERY_VOLTS = 11.8
SILENT_LIMIT = 24  # consecutive addresses without a value before giving up


class DumpStopped(Exception):
    """A dump was interrupted in a way that can be resumed later."""


class LowBattery(DumpStopped):
    pass


class ComputerSilent(DumpStopped):
    """The engine computer stopped echoing altogether (key off, plug out)."""


class RangeUnreadable(DumpStopped):
    """The computer is still there, but this stretch of addresses gives no values."""


class MemoryLink:
    """Byte reads from a region, through the interface board."""

    def __init__(self, scanner: Scanner, interval_ms: int = DEFAULT_INTERVAL_MS):
        self.scanner = scanner
        self.interval_ms = interval_ms

    def start(self) -> None:
        self.scanner.configure_sci()
        self.scanner.set_interval(self.interval_ms)

    def alive(self) -> bool:
        """True if the engine computer echoes a harmless byte."""
        reply = self.scanner.send_sci(bytes([sci.CMD_LOW_SPEED]), REPLY_TIMEOUT)
        return bool(reply) and reply[0] == sci.CMD_LOW_SPEED

    def read_one(self, region: sci.Region, address: int) -> int | None:
        reply = self.scanner.send_sci(region.request(address), REPLY_TIMEOUT)
        return region.value_from(address, reply)

    def read_batch(self, region: sci.Region, addresses: list[int]) -> tuple[dict[int, int], int]:
        """Read several addresses in one paced list.

        Returns the clean values, and how many of the addresses were actually
        attempted (the board stops at the first one that gets no bytes back).
        """
        requests = [region.request(a) for a in addresses]
        replies = self.scanner.send_sci_list(requests, REPLY_TIMEOUT)
        values = {}
        for address, reply in zip(addresses, replies):
            value = region.value_from(address, reply)
            if value is not None:
                values[address] = value
        attempted = min(len(replies) + 1, len(addresses))
        return values, attempted

    def read_all(self, region: sci.Region, addresses: list[int]) -> dict[int, int]:
        """Read a short list of scattered addresses, tolerating silent ones."""
        values: dict[int, int] = {}
        pending = list(addresses)
        while pending:
            got, attempted = self.read_batch(region, pending[: region.batch])
            values.update(got)
            pending = pending[attempted:]
        return values


# -- probing ------------------------------------------------------------------


def _band_samples(low: int, size: int) -> list[int]:
    offsets = [0, 1, 2, 3, size // 2, size - 1]
    return sorted({low + o for o in offsets if o < size})


def probe_region(link: MemoryLink, region: sci.Region) -> dict:
    """Find out whether a region answers, and how far its contents extend.

    The address space is sampled in power-of-two bands. A band that answers
    with the same bytes as the band below it is a mirror of smaller memory,
    not more memory.
    """
    cache: dict[int, int] = {}

    def read(addresses: list[int]) -> dict[int, int]:
        need = [a for a in addresses if a not in cache]
        cache.update(link.read_all(region, need))
        return {a: cache[a] for a in addresses if a in cache}

    bands = []
    low, size = 0, 0x100
    while low < region.address_space:
        samples = _band_samples(low, size)
        values = read(samples)
        answered = len(values)
        uniform = answered == len(samples) and len(set(values.values())) == 1
        mirror = False
        if low and answered == len(samples) and not uniform:
            below = read([a - low for a in samples])
            mirror = all(below.get(a - low) == values[a] for a in samples)
        bands.append(
            {
                "start": low,
                "end": low + size,
                "samples": len(samples),
                "answered": answered,
                "uniform_value": next(iter(values.values())) if uniform else None,
                "mirror": mirror,
            }
        )
        low, size = (0x100, 0x100) if low == 0 else (low * 2, size * 2)

    real = [b for b in bands if b["answered"] == b["samples"] and not b["mirror"]]
    suggested = real[-1]["end"] if real else 0
    # Trailing bands that read as one repeated value are most likely empty
    # space past the end of the chip; do not suggest dumping them.
    for band in reversed(real):
        if band["uniform_value"] is None:
            suggested = band["end"]
            break
        suggested = band["start"]

    rate = None
    if suggested:
        count = min(region.batch, suggested)
        started = time.monotonic()
        got, _ = link.read_batch(region, list(range(count)))
        elapsed = time.monotonic() - started
        if got and elapsed > 0:
            rate = len(got) / elapsed
    return {
        "region": region.name,
        "supported": bool(real),
        "suggested_size": suggested,
        "bytes_per_second": rate,
        "bands": bands,
    }


def format_probe(result: dict) -> str:
    region = sci.REGIONS[result["region"]]
    lines = [f"{region.title} [command {region.command:02X}]"]
    if not result["supported"]:
        lines.append("  No clean answers. This command is not usable on this computer as tested.")
        return "\n".join(lines)
    lines.append("  address range          answered  note")
    for band in result["bands"]:
        note = ""
        if band["mirror"]:
            note = "repeats lower memory"
        elif band["uniform_value"] is not None:
            note = f"every sample is {band['uniform_value']:02X}"
        lines.append(
            f"  {band['start']:06X}-{band['end'] - 1:06X}   {band['answered']:>3}/{band['samples']:<3}   {note}"
        )
    size = result["suggested_size"]
    lines.append(f"  Suggested size to dump: {size} bytes ({size:#x})")
    rate = result["bytes_per_second"]
    if rate:
        minutes = size / rate / 60
        lines.append(f"  Measured {rate:.0f} bytes/s: about {minutes:.0f} min per pass")
    return "\n".join(lines)


# -- dumping ------------------------------------------------------------------


@dataclass
class DumpState:
    region: str
    start: int
    size: int
    done: int = 0  # offsets below this have been attempted
    missing: list[list[int]] = field(default_factory=list)  # [first, last+1) offset ranges
    complete: bool = False
    started_at: str = ""
    finished_at: str | None = None
    lowest_battery: float | None = None
    interval_ms: int = DEFAULT_INTERVAL_MS
    tool: str = "tjdiag"
    event: str = "memory-dump"

    def add_missing(self, offset: int) -> None:
        if self.missing and self.missing[-1][1] == offset:
            self.missing[-1][1] = offset + 1
        else:
            self.missing.append([offset, offset + 1])

    def rewind_to(self, offset: int) -> None:
        """Forget everything from `offset` on, so a resume reads it again."""
        kept = []
        for first, last in self.missing:
            if first < offset:
                kept.append([first, min(last, offset)])
        self.missing = kept
        self.done = offset

    @property
    def missing_count(self) -> int:
        return sum(last - first for first, last in self.missing)

    def missing_offsets(self) -> set[int]:
        return {o for first, last in self.missing for o in range(first, last)}


def state_path(image_path: Path) -> Path:
    return image_path.with_suffix(".json")


def load_state(image_path: Path) -> DumpState | None:
    path = state_path(image_path)
    if not path.exists() or not image_path.exists():
        return None
    data = json.loads(path.read_text())
    known = {name for name in DumpState.__dataclass_fields__}
    return DumpState(**{k: v for k, v in data.items() if k in known})


def save_state(image_path: Path, state: DumpState) -> None:
    state_path(image_path).write_text(json.dumps(asdict(state), indent=2) + "\n")


def dump_region(
    link: MemoryLink,
    region: sci.Region,
    start: int,
    size: int,
    image_path: Path,
    min_battery: float | None = MIN_BATTERY_VOLTS,
    battery_every: int = 20,
    retries: int = 2,
    progress: Callable[[DumpState], None] | None = None,
) -> DumpState:
    """Read `size` bytes from `start` into `image_path`, resuming if it exists.

    Raises a DumpStopped subclass when it has to stop early; the image
    and its state file are saved first, so running it again continues.
    """
    state = load_state(image_path)
    if state is None:
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(bytes([FILL]) * size)
        state = DumpState(region.name, start, size, started_at=now_iso(), interval_ms=link.interval_ms)
        save_state(image_path, state)
    elif (state.region, state.start, state.size) != (region.name, start, size):
        raise ValueError(
            f"{image_path.name} holds a different dump ({state.region}, start "
            f"{state.start:#x}, size {state.size:#x}); use another --label"
        )
    if state.complete:
        return state

    silent_run = 0
    batches = 0
    with open(image_path, "r+b") as image:
        while state.done < size:
            if min_battery is not None and batches % battery_every == 0:
                volts = link.scanner.battery_volts()
                if volts > 0:
                    if state.lowest_battery is None or volts < state.lowest_battery:
                        state.lowest_battery = volts
                    if volts < min_battery:
                        save_state(image_path, state)
                        raise LowBattery(
                            f"battery is at {volts:.1f} V, below the {min_battery:.1f} V limit. "
                            "Connect a charger and run the same command again to continue."
                        )
            batches += 1

            offsets = list(range(state.done, min(state.done + region.batch, size)))
            values, attempted = link.read_batch(region, [start + o for o in offsets])
            tried = offsets[:attempted]
            failed = [o for o in tried if start + o not in values]

            if failed and not link.alive():
                # The computer has gone quiet. Keep what was read before the
                # first failure and stop there, so a resume carries on cleanly.
                for offset in tried:
                    if offset >= failed[0]:
                        break
                    image.seek(offset)
                    image.write(bytes([values[start + offset]]))
                state.rewind_to(failed[0])
                image.flush()
                save_state(image_path, state)
                raise ComputerSilent(
                    f"the engine computer stopped answering at address "
                    f"{start + failed[0]:#08x}. Check that the key is still ON and the "
                    "plug is seated, then run the same command again to continue."
                )

            for offset in tried:
                value = values.get(start + offset)
                for _ in range(retries):
                    if value is not None:
                        break
                    value = link.read_one(region, start + offset)
                if value is None:
                    state.add_missing(offset)
                    silent_run += 1
                else:
                    image.seek(offset)
                    image.write(bytes([value]))
                    silent_run = 0
                state.done = offset + 1
                if silent_run >= SILENT_LIMIT:
                    first_silent = offset - SILENT_LIMIT + 1
                    state.rewind_to(first_silent)
                    image.flush()
                    save_state(image_path, state)
                    raise RangeUnreadable(
                        f"{SILENT_LIMIT} addresses in a row gave no value, starting at "
                        f"{start + first_silent:#08x}, although the computer still answers. "
                        "The dump most likely runs past the end of this memory; use a "
                        "smaller --size."
                    )
            image.flush()
            save_state(image_path, state)
            if progress:
                progress(state)

    state.complete = True
    state.finished_at = now_iso()
    save_state(image_path, state)
    return state


def pass_path(folder: Path, region: str, number: int) -> Path:
    return folder / f"{region}-pass{number}.bin"


def existing_passes(folder: Path, region: str) -> list[tuple[Path, DumpState]]:
    """Every saved pass for a region, in order, complete or not."""
    passes = []
    number = 1
    while True:
        path = pass_path(folder, region, number)
        state = load_state(path)
        if state is None:
            return passes
        passes.append((path, state))
        number += 1

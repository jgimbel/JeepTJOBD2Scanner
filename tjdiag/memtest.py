"""Block-by-block tests on saved memory dumps.

What each block is tested for, and what that proves:

    readable     every address answered                 the link and that part of memory respond
    repeatable   every pass read the same bytes         the cells and their supply are stable
    reference    identical to a known-good image        the contents are correct

Reading the same bytes twice does not prove they are the right bytes. Without
a reference image a block can reach "stable" at best, and the report says so
instead of calling it good.
"""

from __future__ import annotations

import binascii
import struct
import zlib
from dataclasses import dataclass, field

UNREADABLE = "UNREADABLE"
UNSTABLE = "UNSTABLE"
MISMATCH = "MISMATCH"
VERIFIED = "VERIFIED"
STABLE = "STABLE"
BLANK = "BLANK"
READ_ONCE = "READ ONCE"

FAILURES = (UNREADABLE, UNSTABLE, MISMATCH)

_MEANING = {
    UNREADABLE: "some addresses never answered",
    UNSTABLE: "passes disagree: the memory or its supply is not stable",
    MISMATCH: "differs from the reference image",
    VERIFIED: "identical to the reference image",
    STABLE: "readable and repeatable; contents not proven correct (no reference)",
    BLANK: "readable and repeatable, but holds a single repeated value",
    READ_ONCE: "readable; only one pass, so stability is untested",
}


@dataclass
class Pass:
    """One saved dump pass: its bytes and the offsets that were never read."""

    data: bytes
    missing: set[int] = field(default_factory=set)


@dataclass
class Difference:
    offset: int
    values: list[int]  # one per pass, or [read, reference]


@dataclass
class BlockResult:
    start: int  # absolute address
    size: int
    status: str
    missing: int = 0
    unstable: list[Difference] = field(default_factory=list)
    mismatched: int = 0
    mismatches: list[Difference] = field(default_factory=list)
    blank_value: int | None = None


@dataclass
class Report:
    region: str
    base: int
    size: int
    block_size: int
    passes: int
    has_reference: bool
    blocks: list[BlockResult]
    checksum_hits: list[str] = field(default_factory=list)
    checksum_searched: bool = False
    strings: list[tuple[int, str]] = field(default_factory=list)
    vin: str | None = None
    vin_found: bool | None = None
    reference_note: str | None = None

    def count(self, status: str) -> int:
        return sum(1 for b in self.blocks if b.status == status)

    @property
    def failed(self) -> bool:
        return any(b.status in FAILURES for b in self.blocks)


def _bit_flips(read: int, reference: int) -> str:
    cleared = bin(reference & ~read).count("1")
    set_ = bin(read & ~reference).count("1")
    parts = []
    if cleared:
        parts.append(f"{cleared} bit(s) 1->0")
    if set_:
        parts.append(f"{set_} bit(s) 0->1")
    return ", ".join(parts)


def classify_blocks(
    region: str,
    base: int,
    passes: list[Pass],
    block_size: int,
    reference: bytes | None = None,
    detail_limit: int = 8,
) -> Report:
    """Classify every block of a region from one or more passes."""
    size = len(passes[0].data)
    if any(len(p.data) != size for p in passes):
        raise ValueError("the passes are not the same size")
    reference_note = None
    if reference is not None and len(reference) != size:
        reference_note = (
            f"the reference image is {len(reference)} bytes but the dump is {size} bytes; "
            "only the overlapping part is compared"
        )

    blocks = []
    for offset in range(0, size, block_size):
        end = min(offset + block_size, size)
        result = BlockResult(base + offset, end - offset, STABLE)
        span = range(offset, end)

        result.missing = len({o for p in passes for o in p.missing if offset <= o < end})
        first = passes[0].data[offset:end]
        if any(p.data[offset:end] != first for p in passes[1:]):
            for o in span:
                values = [p.data[o] for p in passes]
                if len(set(values)) > 1:
                    result.unstable.append(Difference(base + o, values))
        if reference is not None:
            ref = reference[offset:end]
            for i, o in enumerate(span):
                if i < len(ref) and ref[i] != passes[0].data[o]:
                    result.mismatched += 1
                    if len(result.mismatches) < detail_limit:
                        result.mismatches.append(Difference(base + o, [passes[0].data[o], ref[i]]))

        covered = reference is not None and len(reference) >= end
        if result.missing:
            result.status = UNREADABLE
        elif result.unstable:
            result.status = UNSTABLE
        elif result.mismatched:
            result.status = MISMATCH
        elif covered:
            result.status = VERIFIED
        elif len(set(first)) == 1:
            result.status = BLANK
            result.blank_value = first[0]
        elif len(passes) < 2:
            result.status = READ_ONCE
        blocks.append(result)

    return Report(
        region, base, size, block_size, len(passes), reference is not None, blocks,
        reference_note=reference_note,
    )


# -- checksum search ------------------------------------------------------------


def _arc_table() -> list[int]:
    table = []
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_ARC_TABLE = _arc_table()


def _crc16_arc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = (crc >> 8) ^ _ARC_TABLE[(crc ^ byte) & 0xFF]
    return crc


def _sum_words(data: bytes, order: str) -> int:
    if len(data) % 2:
        data += b"\x00"
    words = struct.unpack(f"{'>' if order == 'big' else '<'}{len(data) // 2}H", data)
    return sum(words) & 0xFFFF


_ALGORITHMS = (
    ("16-bit sum of bytes", 2, lambda d: sum(d) & 0xFFFF),
    ("16-bit sum of big-endian words", 2, lambda d: _sum_words(d, "big")),
    ("16-bit sum of little-endian words", 2, lambda d: _sum_words(d, "little")),
    ("CRC-16/CCITT", 2, lambda d: binascii.crc_hqx(d, 0xFFFF)),
    ("CRC-16/ARC", 2, _crc16_arc),
    ("CRC-32", 4, lambda d: zlib.crc32(d) & 0xFFFFFFFF),
)


def search_checksums(data: bytes, base: int = 0, min_segment: int = 0x2000) -> list[str]:
    """Look for a segment whose checksum is stored in it, or balances to zero.

    Tries the whole image and its halves, quarters and eighths. A hit is a
    lead, not proof: with this many attempts a 16-bit match can occur by
    chance in a few percent of images.
    """
    hits = []
    segments = []
    parts = 1
    while parts <= 8 and len(data) // parts >= min_segment and len(data) % parts == 0:
        step = len(data) // parts
        segments += [(i * step, step) for i in range(parts)]
        parts *= 2
    for offset, length in segments:
        segment = data[offset : offset + length]
        if len(set(segment)) == 1:
            continue  # blank: everything "matches"
        where = f"{base + offset:06X}-{base + offset + length - 1:06X}"
        for name, width, function in _ALGORITHMS:
            total = function(segment)
            if total == 0 or total == (1 << (8 * width)) - 1:
                hits.append(f"{name} over {where} balances to {total:0{width * 2}X}")
            stored_area = segment[-width:]
            computed = function(segment[:-width])
            for order in ("big", "little"):
                if int.from_bytes(stored_area, order) == computed:
                    hits.append(
                        f"{name} over {where} matches the value stored in its last "
                        f"{width} bytes ({order}-endian)"
                    )
    return hits


def find_strings(data: bytes, base: int = 0, minimum: int = 6) -> list[tuple[int, str]]:
    """Runs of printable characters, e.g. a VIN or part number."""
    found = []
    run_start = None
    for i, byte in enumerate(data + b"\x00"):
        printable = 0x20 <= byte < 0x7F
        if printable and run_start is None:
            run_start = i
        elif not printable and run_start is not None:
            if i - run_start >= minimum:
                found.append((base + run_start, data[run_start:i].decode("ascii")))
            run_start = None
    return found


# -- report -----------------------------------------------------------------------


def _ranges(blocks: list[BlockResult]) -> list[tuple[BlockResult, BlockResult]]:
    """Group consecutive blocks that share a status."""
    groups: list[tuple[BlockResult, BlockResult]] = []
    for block in blocks:
        previous = groups[-1][1] if groups else None
        same = previous is not None and (previous.status, previous.blank_value) == (
            block.status,
            block.blank_value,
        )
        if same and block.status not in FAILURES:
            groups[-1] = (groups[-1][0], block)
        else:
            groups.append((block, block))
    return groups


def format_report(report: Report, title: str) -> str:
    lines = [
        f"{title}: {report.size} bytes from {report.base:06X}, "
        f"{len(report.blocks)} blocks of {report.block_size} bytes, {report.passes} pass(es)",
        "",
    ]
    for first, last in _ranges(report.blocks):
        span = f"{first.start:06X}-{last.start + last.size - 1:06X}"
        note = ""
        if first.status == BLANK:
            note = f" (all {first.blank_value:02X})"
        lines.append(f"  {span}  {first.status}{note}")
        if first.status == UNREADABLE:
            lines.append(f"      {first.missing} address(es) never answered")
        for diff in first.unstable[:8]:
            values = " / ".join(f"{v:02X}" for v in diff.values)
            lines.append(f"      {diff.offset:06X} read as {values}")
        if len(first.unstable) > 8:
            lines.append(f"      ... {len(first.unstable) - 8} more unstable address(es)")
        if first.status == MISMATCH:
            lines.append(f"      {first.mismatched} byte(s) differ from the reference")
            for diff in first.mismatches:
                read, ref = diff.values
                lines.append(
                    f"      {diff.offset:06X} read {read:02X}, reference {ref:02X} "
                    f"({_bit_flips(read, ref)})"
                )

    lines.append("")
    for status in (UNREADABLE, UNSTABLE, MISMATCH, VERIFIED, STABLE, READ_ONCE, BLANK):
        count = report.count(status)
        if count:
            lines.append(f"  {count:4} block(s) {status}: {_MEANING[status]}")
    if report.reference_note:
        lines.append(f"  Note: {report.reference_note}")

    if report.checksum_searched:
        lines.append("")
        if report.checksum_hits:
            lines.append("  Checksum search found candidates (a lead, not proof):")
            lines += [f"    {hit}" for hit in report.checksum_hits]
        else:
            lines.append(
                "  Checksum search: no common checksum fits. That says nothing either "
                "way; the computer's own method is unknown."
            )
    if report.strings:
        lines.append("")
        lines.append("  Readable text found:")
        lines += [f"    {address:06X}  {text}" for address, text in report.strings[:20]]
    if report.vin_found is not None:
        lines.append("")
        if report.vin_found:
            lines.append(f"  VIN {report.vin} is stored in this memory.")
        else:
            lines.append(
                f"  VIN {report.vin} was not found as plain text. It may be stored "
                "in another form, or this memory may belong to a different vehicle."
            )

    lines += ["", "Conclusion: " + conclusion(report)]
    return "\n".join(lines)


def conclusion(report: Report) -> str:
    if report.count(UNREADABLE):
        return (
            "some addresses never answered. Treat this as a communication or range "
            "problem first: repeat the dump before drawing conclusions about the memory."
        )
    if report.count(UNSTABLE):
        return (
            "the same addresses read differently between passes. With the battery "
            "on a charger, that points at failing memory in the blocks listed."
        )
    if report.count(MISMATCH):
        return (
            "the memory is stable but differs from the reference in the blocks listed. "
            "If the reference is for exactly this part number and calibration, those "
            "blocks are corrupt; if it is not, the differences may be legitimate."
        )
    if report.has_reference and report.count(VERIFIED) == len(report.blocks):
        return "every block matches the reference image. This memory is good."
    if report.passes < 2:
        return (
            "everything was readable, but with a single pass stability is untested. "
            "Run a second pass."
        )
    return (
        "every block is readable and repeatable, so no block is physically failing. "
        "Whether the contents are correct is not proven: that needs a reference image "
        "for this part number."
    )

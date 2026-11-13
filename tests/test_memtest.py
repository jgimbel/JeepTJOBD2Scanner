import random

from tjdiag import memtest
from tjdiag.memtest import (
    BLANK,
    MISMATCH,
    READ_ONCE,
    STABLE,
    UNREADABLE,
    UNSTABLE,
    VERIFIED,
    Pass,
    classify_blocks,
)

BLOCK = 0x1000
IMAGE = random.Random(7).randbytes(4 * BLOCK)


def statuses(report):
    return [block.status for block in report.blocks]


def test_two_identical_passes_are_stable_but_not_called_good():
    report = classify_blocks("rom", 0, [Pass(IMAGE), Pass(IMAGE)], BLOCK)
    assert statuses(report) == [STABLE] * 4
    assert not report.failed
    text = memtest.conclusion(report)
    assert "no block is physically failing" in text and "not proven" in text


def test_a_single_pass_only_proves_readability():
    report = classify_blocks("rom", 0, [Pass(IMAGE)], BLOCK)
    assert statuses(report) == [READ_ONCE] * 4
    assert "second pass" in memtest.conclusion(report)


def test_a_flipping_bit_is_pinned_to_its_block_and_address():
    second = bytearray(IMAGE)
    second[0x1234] ^= 0x08
    report = classify_blocks("rom", 0, [Pass(IMAGE), Pass(bytes(second))], BLOCK)
    assert statuses(report) == [STABLE, UNSTABLE, STABLE, STABLE]
    (difference,) = report.blocks[1].unstable
    assert difference.offset == 0x1234
    assert difference.values == [IMAGE[0x1234], IMAGE[0x1234] ^ 0x08]
    assert report.failed and "failing memory" in memtest.conclusion(report)
    assert "001234 read as" in memtest.format_report(report, "ROM")


def test_addresses_are_reported_relative_to_the_dump_start():
    second = bytearray(IMAGE)
    second[0x10] ^= 0x01
    report = classify_blocks("rom", 0x20000, [Pass(IMAGE), Pass(bytes(second))], BLOCK)
    assert report.blocks[0].start == 0x20000
    assert report.blocks[0].unstable[0].offset == 0x20010


def test_an_erased_block_is_reported_as_blank_not_as_good():
    image = bytearray(IMAGE)
    image[2 * BLOCK : 3 * BLOCK] = b"\xFF" * BLOCK
    report = classify_blocks("rom", 0, [Pass(bytes(image)), Pass(bytes(image))], BLOCK)
    assert statuses(report) == [STABLE, STABLE, BLANK, STABLE]
    assert report.blocks[2].blank_value == 0xFF
    assert "(all FF)" in memtest.format_report(report, "ROM")


def test_matching_the_reference_is_the_only_way_to_be_verified():
    report = classify_blocks("rom", 0, [Pass(IMAGE), Pass(IMAGE)], BLOCK, reference=IMAGE)
    assert statuses(report) == [VERIFIED] * 4
    assert memtest.conclusion(report).endswith("This memory is good.")


def test_corruption_is_located_and_described_against_the_reference():
    image = bytearray(IMAGE)
    image[0x0100] = IMAGE[0x0100] & 0xFE if IMAGE[0x0100] & 1 else IMAGE[0x0100] | 1
    image[0x3ABC] ^= 0xFF
    report = classify_blocks("rom", 0, [Pass(bytes(image)), Pass(bytes(image))], BLOCK, reference=IMAGE)
    assert statuses(report) == [MISMATCH, VERIFIED, VERIFIED, MISMATCH]
    assert report.blocks[0].mismatched == 1 and report.blocks[3].mismatched == 1
    assert report.blocks[3].mismatches[0].offset == 0x3ABC
    text = memtest.format_report(report, "ROM")
    assert "003ABC read" in text and "bit(s)" in text
    assert "corrupt" in memtest.conclusion(report)


def test_unanswered_addresses_make_a_block_unreadable():
    report = classify_blocks("rom", 0, [Pass(IMAGE, {0x2001, 0x2002}), Pass(IMAGE)], BLOCK)
    assert statuses(report) == [STABLE, STABLE, UNREADABLE, STABLE]
    assert report.blocks[2].missing == 2
    assert "repeat the dump" in memtest.conclusion(report)


def test_a_short_reference_only_verifies_what_it_covers():
    report = classify_blocks("rom", 0, [Pass(IMAGE), Pass(IMAGE)], BLOCK, reference=IMAGE[: 2 * BLOCK])
    assert statuses(report) == [VERIFIED, VERIFIED, STABLE, STABLE]
    assert "overlapping part" in report.reference_note


def test_a_fault_never_marks_a_healthy_block():
    second = bytearray(IMAGE)
    second[0x0005] ^= 0x80
    report = classify_blocks("rom", 0, [Pass(IMAGE), Pass(bytes(second))], BLOCK, reference=IMAGE)
    assert statuses(report) == [UNSTABLE, VERIFIED, VERIFIED, VERIFIED]


def test_last_partial_block_is_handled():
    data = IMAGE[: BLOCK + 100]
    report = classify_blocks("eeprom", 0, [Pass(data), Pass(data)], BLOCK)
    assert [b.size for b in report.blocks] == [BLOCK, 100]


# -- checksum search --------------------------------------------------------------


def test_checksum_search_finds_a_stored_sum():
    body = random.Random(11).randbytes(0x4000 - 2)
    image = body + (sum(body) & 0xFFFF).to_bytes(2, "big")
    hits = memtest.search_checksums(image)
    assert any("16-bit sum of bytes" in hit and "000000-003FFF" in hit for hit in hits)


def test_checksum_search_finds_a_stored_crc_in_a_half():
    half = random.Random(12).randbytes(0x2000 - 4)
    import zlib

    half += (zlib.crc32(half) & 0xFFFFFFFF).to_bytes(4, "little")
    image = random.Random(13).randbytes(0x2000) + half
    hits = memtest.search_checksums(image)
    assert any("CRC-32" in hit and "002000-003FFF" in hit and "little" in hit for hit in hits)


def test_checksum_search_is_quiet_on_ordinary_data_and_blank_segments():
    assert memtest.search_checksums(IMAGE) == []
    assert memtest.search_checksums(b"\xFF" * 0x4000) == []


def test_find_strings():
    data = b"\x00\x01ABCDEFGH\xff\x10short\x00" + b"1J4FA49S04P700001"
    found = memtest.find_strings(data, base=0x100, minimum=8)
    assert found == [(0x102, "ABCDEFGH"), (0x100 + 18, "1J4FA49S04P700001")]

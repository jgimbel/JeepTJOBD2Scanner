import pytest

from tjdiag.elm import (
    Elm327,
    ElmError,
    ElmTimeout,
    crc8_j1850,
    parse_bus_frame,
    parse_frame,
)
from tjdiag.sim import make_sim


def frame_text(*body: int) -> str:
    raw = bytes(body)
    return (raw + bytes([crc8_j1850(raw)])).hex(" ").upper()


def test_crc_matches_the_published_check_value():
    # CRC-8/SAE-J1850 of the ASCII string "123456789" is 0x4B.
    assert crc8_j1850(b"123456789") == 0x4B


def test_parse_frame_splits_header_payload_and_checksum():
    frame = parse_frame(frame_text(0x48, 0x6B, 0x10, 0x43, 0x06, 0x01, 0, 0, 0, 0))
    assert (frame.priority, frame.target, frame.source) == (0x48, 0x6B, 0x10)
    assert frame.data == bytes([0x43, 0x06, 0x01, 0, 0, 0, 0])
    assert frame.checksum_ok


def test_parse_frame_accepts_iso_byte_sum_checksum():
    body = bytes([0x48, 0x6B, 0x10, 0x41, 0x0C, 0x0B, 0x40])
    frame = parse_frame((body + bytes([sum(body) & 0xFF])).hex(" "))
    assert frame.checksum_ok
    assert frame.data == bytes([0x41, 0x0C, 0x0B, 0x40])


def test_parse_frame_keeps_all_bytes_when_checksum_does_not_match():
    frame = parse_frame("48 6B 10 43 06 01 00 00 00 00")
    assert not frame.checksum_ok
    assert frame.data == bytes([0x43, 0x06, 0x01, 0, 0, 0, 0])


def test_parse_frame_honours_the_adapters_data_error_marker():
    frame = parse_frame(frame_text(0x48, 0x6B, 0x10, 0x43, 0x06, 0x01) + " <DATA ERROR")
    assert frame is not None and not frame.checksum_ok


@pytest.mark.parametrize("line", ["NO DATA", "OK", "?", "48 6B", "ZZ 6B 10 41", "486"])
def test_parse_frame_rejects_non_frames(line):
    assert parse_frame(line) is None


def test_parse_bus_frame_checks_the_crc():
    good = parse_bus_frame(frame_text(0x10, 0x0B, 0x40))
    assert good.message_id == 0x10 and good.checksum_ok
    assert not parse_bus_frame("10 0B 40 00").checksum_ok


def test_command_strips_echo_before_reset():
    elm = Elm327(make_sim("pcm"), timeout=1.0)
    assert elm.command("ATI") == ["ELM327 v1.5"]


def test_reset_switches_headers_on_and_echo_off():
    sim = make_sim("pcm")
    elm = Elm327(sim, timeout=1.0)
    assert "ELM327" in elm.reset()
    assert sim.headers and not sim.echo and sim.spaces and not sim.linefeeds


def test_reset_rejects_a_device_that_is_not_an_elm327():
    class Modem:
        def __init__(self):
            self.pending = b""

        def write(self, data):
            self.pending = b"ERROR\r>"

        def read(self, timeout):
            data, self.pending = self.pending, b""
            return data

        def close(self):
            pass

    with pytest.raises(ElmError, match="not an ELM327"):
        Elm327(Modem(), timeout=0.5).reset()


def test_silent_adapter_times_out():
    class Dead:
        def write(self, data):
            pass

        def read(self, timeout):
            return b""

        def close(self):
            pass

    with pytest.raises(ElmTimeout):
        Elm327(Dead(), timeout=0.05).command("ATI")


def test_unknown_at_command_raises(connect):
    elm, _, _ = connect("pcm")
    with pytest.raises(ElmError):
        elm.at("XYZ")
    assert not elm.supports("XYZ")


def test_obd_reports_no_data_and_errors_distinctly(connect):
    elm, _, _ = connect("pcm")
    assert elm.obd("01 FF").status == "no_data"
    elm.set_protocol("3")  # the simulated vehicle is not on ISO 9141
    response = elm.obd("01 00")
    assert response.status == "error" and "UNABLE TO CONNECT" in response.error


def test_replies_are_grouped_by_the_module_that_sent_them(connect):
    elm, _, _ = connect("pcm")
    response = elm.obd("03")
    assert sorted(response.by_source()) == [0x10, 0x18]


def test_linefeeds_and_no_spaces_are_still_parsed(connect):
    elm, sim, _ = connect("pcm")
    sim.linefeeds, sim.spaces = True, False
    assert len(elm.obd("03").frames) == 2


def test_monitor_returns_frames_and_leaves_the_adapter_usable(connect):
    elm, _, _ = connect("pcm")
    frames, overflows = elm.monitor(2.0, max_frames=10)
    assert len(frames) == 10 and overflows == 0
    assert all(frame.checksum_ok for frame in frames)
    assert elm.voltage() == 12.5


def test_monitor_raises_when_the_adapter_cannot_monitor(connect):
    elm, _, _ = connect("limited")
    with pytest.raises(ElmError, match="ATMA"):
        elm.monitor(1.0, max_frames=5)


def test_a_bare_carriage_return_is_never_sent(connect):
    # An ELM327 repeats its last command on an empty line. If that command was
    # "04", codes would be cleared by accident.
    elm, sim, _ = connect("pcm")
    elm.obd("03")
    elm.monitor(0.5, max_frames=5)
    elm.reset()
    assert sim.bare_cr_count == 0
    assert "04" not in sim.commands

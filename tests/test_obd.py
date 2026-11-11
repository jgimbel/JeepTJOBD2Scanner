import pytest

from tjdiag import obd
from tjdiag.elm import Elm327
from tjdiag.sim import encode_dtc, make_sim


@pytest.mark.parametrize(
    "high, low, code",
    [
        (0x06, 0x01, "P0601"),
        (0x07, 0x00, "P0700"),
        (0x16, 0x98, "P1698"),
        (0x41, 0x23, "C0123"),
        (0x81, 0x23, "B0123"),
        (0xC1, 0x00, "U0100"),
        (0x01, 0x3A, "P013A"),
    ],
)
def test_decode_dtc(high, low, code):
    assert obd.decode_dtc(high, low) == code
    assert encode_dtc(code) == bytes([high, low])


def test_module_names_follow_address_ranges():
    assert obd.module_name(0x10).startswith("Engine")
    assert obd.module_name(0x18).startswith("Transmission")
    assert obd.module_name(0x99) == "Unknown module"


def test_p0601_is_attributed_to_the_engine_computer(connect):
    elm, _, _ = connect("pcm")
    report = obd.read_dtcs(elm)
    assert report.codes == {0x10: ["P0601"], 0x18: []}
    assert report.sources_of("P0601") == [0x10]


def test_p0601_is_attributed_to_the_transmission_computer(connect):
    elm, _, _ = connect("tcm")
    report = obd.read_dtcs(elm)
    assert report.sources_of("P0601") == [0x18]
    assert report.sources_of("P0700") == [0x10]


def test_silent_transmission_computer_is_not_listed(connect):
    elm, _, _ = connect("pcm-silent-tcm")
    report = obd.read_dtcs(elm)
    assert report.responders == [0x10]
    assert report.codes[0x10] == ["P0601", "P0700"]


def test_more_than_three_codes_span_several_frames(connect):
    elm, sim, _ = connect("pcm")
    sim.vehicle.ecus[0].stored = ["P0601", "P0562", "P0700", "P0301", "P0171"]
    assert obd.read_dtcs(elm).codes[0x10] == ["P0601", "P0562", "P0700", "P0301", "P0171"]


def test_pending_codes_use_mode_07(connect):
    elm, sim, _ = connect("clean")
    sim.vehicle.ecus[0].pending = ["P0601"]
    assert obd.read_dtcs(elm, 0x03).all_codes() == set()
    assert obd.read_dtcs(elm, 0x07).codes[0x10] == ["P0601"]


def test_codes_still_decode_when_the_adapter_prints_no_checksum(connect):
    elm, _, _ = connect("pcm", prints_checksum=False)
    report = obd.read_dtcs(elm)
    assert report.codes[0x10] == ["P0601"]
    assert report.bad_checksums == 2


def test_supported_pids_and_live_values(connect):
    elm, sim, _ = connect("pcm")
    supported = obd.supported_pids(elm)
    assert 0x0C in supported[0x10] and 0x05 in supported[0x10]
    assert obd.read_pid(elm, 0x0C) == {0x10: 0.0}
    sim.vehicle.start_engine()
    assert obd.read_pid(elm, 0x0C) == {0x10: 720.0}
    assert obd.read_live(elm, supported)[0x10]["coolant_temp"] == 21


def test_supported_pids_follow_the_chain_past_0x20(connect):
    elm, sim, _ = connect("pcm")
    sim.vehicle.ecus[0].pids[0x42] = (13800).to_bytes(2, "big")
    assert 0x42 in obd.supported_pids(elm)[0x10]
    assert obd.read_pid(elm, 0x42) == {0x10: 13.8}


def test_monitor_status_reports_the_lamp(connect):
    elm, _, _ = connect("pcm")
    status = obd.read_monitor_status(elm)
    assert status[0x10]["mil_on"] and status[0x10]["dtc_count"] == 1
    assert not status[0x18]["mil_on"]
    assert status[0x10]["monitors"]["catalyst"] == "complete"


def test_decode_monitor_status_marks_incomplete_monitors():
    status = obd.decode_monitor_status(bytes([0x00, 0x07, 0x65, 0x21]))
    assert status["monitors"]["catalyst"] == "incomplete"
    assert status["monitors"]["oxygen_sensor"] == "incomplete"
    assert status["monitors"]["evap_system"] == "complete"
    assert "egr_system" not in status["monitors"]


def test_freeze_frame(connect):
    elm, _, _ = connect("pcm")
    freeze = obd.read_freeze_frame(elm)
    assert list(freeze) == [0x10]
    assert freeze[0x10]["dtc"] == "P0601"
    assert freeze[0x10]["data"]["rpm"] == 1850.0
    assert freeze[0x10]["data"]["coolant_temp"] == 88


def test_no_freeze_frame_when_none_is_stored(connect):
    elm, _, _ = connect("clean")
    assert obd.read_freeze_frame(elm) == {}


def test_vin_is_read_when_offered_and_empty_otherwise(connect):
    elm, sim, _ = connect("pcm")
    assert obd.read_vin(elm) == {}
    sim.vehicle.ecus[0].vin = "1J4FA49S04P700001"
    assert obd.read_vin(elm) == {0x10: "1J4FA49S04P700001"}


def test_clear_then_key_cycle_brings_a_hard_fault_back(connect):
    elm, sim, _ = connect("pcm")
    acknowledged, _ = obd.clear_dtcs(elm)
    assert acknowledged == [0x10, 0x18]
    assert obd.read_dtcs(elm).all_codes() == set()
    assert obd.read_freeze_frame(elm) == {}
    sim.vehicle.key_cycle()
    assert obd.read_dtcs(elm).codes[0x10] == ["P0601"]


@pytest.mark.parametrize("spec", ["ignition-off", "clone"])
def test_connect_explains_when_nothing_answers(spec):
    with pytest.raises(obd.VehicleNotResponding, match="ignition"):
        obd.connect(Elm327(make_sim(spec), timeout=1.0))


def test_connect_prefers_j1850_vpw(connect):
    _, sim, protocol = connect("pcm")
    assert protocol == "2" and sim.protocol == "2"

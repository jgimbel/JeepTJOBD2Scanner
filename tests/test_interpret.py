import io

from tjdiag import interpret, obd
from tjdiag.snapshot import take_snapshot
from tjdiag.watch import run_watch


def snap(elm, protocol):
    return take_snapshot(elm, protocol)


def clear(elm):
    obd.clear_dtcs(elm)
    return {"tool": "tjdiag", "event": "clear", "taken_at": "t"}


def test_no_events_asks_for_a_snapshot():
    assert interpret.diagnose([]).verdict == interpret.NO_DATA


def test_code_present_without_clear_test_is_not_a_verdict_on_the_computer(connect):
    elm, _, protocol = connect("pcm")
    diagnosis = interpret.diagnose([snap(elm, protocol)])
    assert diagnosis.verdict == interpret.NEEDS_CLEAR_TEST
    assert "engine computer" in diagnosis.headline
    assert any("0x10" in line for line in diagnosis.evidence)
    assert any("clear --yes" in step for step in diagnosis.next_steps)


def test_no_code_is_reported_as_such(connect):
    elm, _, protocol = connect("clean")
    assert interpret.diagnose([snap(elm, protocol)]).verdict == interpret.NO_CODE


def test_return_at_key_on_blames_the_engine_computer(connect):
    elm, sim, protocol = connect("pcm")
    events = [snap(elm, protocol), clear(elm)]
    sim.vehicle.key_cycle()
    events.append(snap(elm, protocol))
    diagnosis = interpret.diagnose(events)
    assert diagnosis.verdict == interpret.HARD_FAULT_PCM
    assert any("power and ground" in step for step in diagnosis.next_steps)


def test_return_at_key_on_blames_the_transmission_computer(connect):
    elm, sim, protocol = connect("tcm")
    events = [snap(elm, protocol), clear(elm)]
    sim.vehicle.key_cycle()
    events.append(snap(elm, protocol))
    diagnosis = interpret.diagnose(events)
    assert diagnosis.verdict == interpret.HARD_FAULT_TCM
    assert "transmission computer" in diagnosis.headline


def test_return_after_cranking_with_low_voltage_points_at_the_supply(connect):
    elm, sim, protocol = connect("lowvolt")
    events = [snap(elm, protocol), clear(elm)]
    sim.vehicle.key_cycle()
    events.append(snap(elm, protocol))  # key on, engine off: clean
    assert not interpret.has_target(events[-1])
    sim.vehicle.voltage_profile = [11.6, 8.4, 9.1, 13.0]
    events.append(run_watch(elm, 5.0, io.StringIO(), max_samples=4))
    sim.vehicle.start_engine()
    events.append(snap(elm, protocol))
    diagnosis = interpret.diagnose(events)
    assert diagnosis.verdict == interpret.SUPPLY_SUSPECTED
    assert any("8.4 V" in line for line in diagnosis.evidence)
    assert any("P0562" in line for line in diagnosis.evidence)


def test_return_after_running_without_supply_evidence_stays_open(connect):
    elm, sim, protocol = connect("clean")
    sim.vehicle.ecus[0].stored = ["P0601"]
    sim.vehicle.ecus[0].run_faults = ["P0601"]
    events = [snap(elm, protocol), clear(elm)]
    sim.vehicle.key_cycle()
    events.append(snap(elm, protocol))
    sim.vehicle.start_engine()
    events.append(snap(elm, protocol))
    diagnosis = interpret.diagnose(events)
    assert diagnosis.verdict == interpret.RETURNED_RUNNING
    assert any("engine-off snapshot after the clear was clean" in e for e in diagnosis.evidence)


def test_code_that_stays_away_is_a_one_time_event(connect):
    elm, sim, protocol = connect("transient")
    events = [snap(elm, protocol), clear(elm)]
    sim.vehicle.key_cycle()
    events.append(snap(elm, protocol))
    sim.vehicle.start_engine()
    events.append(snap(elm, protocol))
    diagnosis = interpret.diagnose(events)
    assert diagnosis.verdict == interpret.NOT_RETURNED


def test_clear_without_follow_up_snapshot_asks_for_one(connect):
    elm, _, protocol = connect("pcm")
    diagnosis = interpret.diagnose([snap(elm, protocol), clear(elm)])
    assert diagnosis.verdict == interpret.NEEDS_CLEAR_TEST
    assert "no snapshot has been taken since" in diagnosis.headline


def test_unreadable_transmission_codes_are_called_out(connect):
    elm, _, protocol = connect("pcm-silent-tcm")
    diagnosis = interpret.diagnose([snap(elm, protocol)])
    assert any("did not answer generic OBD" in line for line in diagnosis.evidence)
    assert any("P0700" in line for line in diagnosis.evidence)


def test_overcharging_counts_as_supply_evidence(connect):
    elm, sim, protocol = connect("clean")
    sim.vehicle.ecus[0].stored = ["P0601"]
    sim.vehicle.ecus[0].run_faults = ["P0601"]
    sim.vehicle.running_voltage = 15.6
    events = [snap(elm, protocol), clear(elm)]
    sim.vehicle.key_cycle()
    sim.vehicle.start_engine()
    events.append(snap(elm, protocol))
    diagnosis = interpret.diagnose(events)
    assert diagnosis.verdict == interpret.SUPPLY_SUSPECTED
    assert any("overcharging" in line for line in diagnosis.evidence)


def test_format_lists_numbered_steps(connect):
    elm, _, protocol = connect("pcm")
    text = interpret.diagnose([snap(elm, protocol)]).format()
    assert "What the data shows:" in text and "  1. " in text

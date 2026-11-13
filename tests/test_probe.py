from tjdiag.elm import Elm327
from tjdiag.probe import FAIL, LIMITED, PASS, UNUSABLE, USABLE, WARN, run_probe
from tjdiag.sim import make_sim


def probe(spec: str, **adapter_options):
    sim = make_sim(spec)
    for name, value in adapter_options.items():
        setattr(sim, name, value)
    return run_probe(Elm327(sim, timeout=1.0), monitor_seconds=1.0)


def status_of(result, name):
    return next(check.status for check in result.checks if check.name == name)


def test_good_adapter_is_usable_and_shows_the_code():
    result = probe("pcm")
    assert result.verdict == USABLE
    assert status_of(result, "J1850 VPW") == PASS
    stored = next(c for c in result.checks if c.name == "Read stored codes")
    assert "P0601" in stored.detail and "0x10" in stored.detail


def test_adapter_without_vpw_is_unusable():
    result = probe("clone")
    assert result.verdict == UNUSABLE
    assert status_of(result, "J1850 VPW") == FAIL


def test_ignition_off_fails_with_a_hint_about_the_key():
    result = probe("ignition-off")
    assert result.verdict == UNUSABLE
    detail = next(c.detail for c in result.checks if c.name == "J1850 VPW")
    assert "ignition is ON" in detail


def test_adapter_without_monitoring_is_limited_not_unusable():
    result = probe("limited")
    assert result.verdict == LIMITED
    assert status_of(result, "Bus monitoring") == WARN
    assert status_of(result, "Read stored codes") == PASS


def test_adapter_without_checksums_is_limited():
    result = probe("pcm", prints_checksum=False)
    assert result.verdict == LIMITED
    assert status_of(result, "Frame checksums") == WARN


def test_low_battery_is_flagged():
    result = probe("lowvolt")
    assert status_of(result, "Battery voltage") == WARN


def test_probe_never_clears_codes():
    sim = make_sim("pcm")
    run_probe(Elm327(sim, timeout=1.0), monitor_seconds=0.5)
    assert "04" not in sim.commands
    assert sim.vehicle.ecus[0].stored == ["P0601"]


def test_format_ends_with_a_verdict_line():
    text = probe("pcm").format()
    assert text.splitlines()[-1].startswith("Verdict: USABLE")


def test_a_renamed_clone_passes_identity_and_is_judged_on_what_it_can_do():
    sim = make_sim("pcm")
    sim.version = "OBDII  v1.5"
    result = run_probe(Elm327(sim, timeout=1.0), monitor_seconds=1.0)
    identity = next(c for c in result.checks if c.name == "Adapter identity")
    assert identity.status == PASS and "OBDII  v1.5" in identity.detail
    assert "obeys ELM327" in identity.detail
    assert result.verdict == USABLE

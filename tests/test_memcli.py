import json

import pytest

from tjdiag import memory
from tjdiag.cli import main
from tjdiag.scisim import SIM_ROM_SIZE, SIM_VIN, golden_rom


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(memory, "REPLY_TIMEOUT", 0.05)


def run(tmp_path, *args):
    return main(["--dir", str(tmp_path), *args])


def dump(tmp_path, scenario, *extra):
    return run(tmp_path, "--sim", scenario, "mem", "dump", "--size", hex(SIM_ROM_SIZE), *extra)


def test_selftest_passes_with_the_loopback_jumper(tmp_path, capsys):
    assert run(tmp_path, "--sim", "bench", "mem", "selftest") == 0
    out = capsys.readouterr().out
    assert "Board is ready." in out and "0.9.12" in out


def test_selftest_fails_without_the_loopback_jumper(tmp_path, capsys):
    assert run(tmp_path, "--sim", "bench-open", "mem", "selftest") == 1
    assert "jumper" in capsys.readouterr().out


def test_probe_saves_sizes_that_dump_then_uses(tmp_path, capsys):
    assert run(tmp_path, "--sim", "healthy", "mem", "probe") == 0
    saved = json.loads((tmp_path / "mem" / "probe.json").read_text())
    assert saved["regions"]["rom"]["suggested_size"] == SIM_ROM_SIZE
    assert "mem dump --region rom" in capsys.readouterr().out
    assert run(tmp_path, "--sim", "healthy", "mem", "dump", "--region", "eeprom") == 0
    assert (tmp_path / "mem" / "default" / "eeprom-pass2.bin").stat().st_size == 0x200


def test_probe_points_at_the_16_bit_command_when_the_24_bit_one_is_missing(tmp_path, capsys):
    assert run(tmp_path, "--sim", "no-rom24", "mem", "probe") == 0
    assert "mem dump --region rom16" in capsys.readouterr().out


def test_probe_with_the_key_off_explains_what_to_check(tmp_path, capsys):
    assert run(tmp_path, "--sim", "key-off", "mem", "probe") == 1
    assert "key ON" in capsys.readouterr().out


def test_dump_needs_a_size_from_somewhere(tmp_path, capsys):
    assert run(tmp_path, "--sim", "healthy", "mem", "dump") == 2
    assert "mem probe" in capsys.readouterr().out


def test_healthy_memory_is_stable_and_the_report_is_saved(tmp_path, capsys):
    assert dump(tmp_path, "healthy") == 0
    capsys.readouterr()
    assert run(tmp_path, "mem", "test") == 0
    out = capsys.readouterr().out
    assert "000000-003FFF  STABLE" in out and "not proven" in out
    (report,) = (tmp_path / "mem" / "default").glob("report-rom-*.json")
    assert json.loads(report.read_text())["passes"] == 2


def test_healthy_memory_is_verified_against_a_reference(tmp_path, capsys):
    reference = tmp_path / "stock.bin"
    reference.write_bytes(golden_rom())
    dump(tmp_path, "healthy")
    capsys.readouterr()
    assert run(tmp_path, "mem", "test", "--region", "rom", "--reference", str(reference)) == 0
    assert "This memory is good." in capsys.readouterr().out


def test_a_flaky_bit_fails_the_test_and_names_the_address(tmp_path, capsys):
    dump(tmp_path, "flaky")
    capsys.readouterr()
    assert run(tmp_path, "mem", "test") == 1
    out = capsys.readouterr().out
    assert "001000-001FFF  UNSTABLE" in out and "001234 read as" in out
    assert "000000-000FFF  STABLE" in out


def test_an_erased_block_is_shown(tmp_path, capsys):
    dump(tmp_path, "erased")
    capsys.readouterr()
    assert run(tmp_path, "mem", "test") == 0
    assert "002000-002FFF  BLANK (all FF)" in capsys.readouterr().out


def test_corruption_is_found_against_a_reference(tmp_path, capsys):
    reference = tmp_path / "stock.bin"
    reference.write_bytes(golden_rom())
    dump(tmp_path, "corrupt")
    capsys.readouterr()
    assert run(tmp_path, "mem", "test", "--region", "rom", "--reference", str(reference)) == 1
    out = capsys.readouterr().out
    assert "000000-000FFF  MISMATCH" in out and "003000-003FFF  MISMATCH" in out
    assert "001000-002FFF  VERIFIED" in out


def test_reference_needs_a_region(tmp_path, capsys):
    reference = tmp_path / "stock.bin"
    reference.write_bytes(b"\x00")
    assert run(tmp_path, "mem", "test", "--reference", str(reference)) == 2


def test_an_interrupted_dump_resumes_until_done(tmp_path, capsys):
    # This simulated computer goes silent after 900 exchanges in every session.
    codes = [dump(tmp_path, "dies", "--passes", "1") for _ in range(30)]
    assert codes[0] == 3 and 0 in codes
    first_done = codes.index(0)
    assert all(code == 3 for code in codes[:first_done])
    assert "key is still ON" in capsys.readouterr().out
    assert (tmp_path / "mem" / "default" / "rom-pass1.bin").read_bytes() == golden_rom()


def test_low_battery_stops_the_dump_unless_overridden(tmp_path, capsys):
    assert dump(tmp_path, "lowbatt", "--passes", "1") == 3
    assert "charger" in capsys.readouterr().out
    assert dump(tmp_path, "lowbatt", "--passes", "1", "--no-battery-check") == 0


def test_separate_labels_keep_dump_sets_apart(tmp_path, capsys):
    dump(tmp_path, "healthy", "--label", "monday", "--passes", "1")
    assert (tmp_path / "mem" / "monday" / "rom-pass1.bin").exists()
    assert run(tmp_path, "mem", "test") == 2  # nothing under the default label
    assert run(tmp_path, "mem", "test", "--label", "monday") == 0


def test_eeprom_vin_check(tmp_path, capsys):
    run(tmp_path, "--sim", "healthy", "mem", "dump", "--region", "eeprom", "--size", "0x200")
    capsys.readouterr()
    run(tmp_path, "mem", "test", "--vin", SIM_VIN)
    assert f"VIN {SIM_VIN} is stored" in capsys.readouterr().out
    run(tmp_path, "mem", "test", "--vin", "1J4FA49S04P999999")
    assert "was not found as plain text" in capsys.readouterr().out


def test_faults_and_ram(tmp_path, capsys):
    assert run(tmp_path, "--sim", "healthy", "mem", "faults") == 0
    assert "Checksum: ok" in capsys.readouterr().out
    assert run(tmp_path, "--sim", "healthy", "mem", "ram") == 0
    (saved,) = (tmp_path / "mem").glob("ram-*.json")
    tables = json.loads(saved.read_text())["tables"]
    assert len(tables) == 15 and len(tables["F4"]) == 480
    # The computer was put back into low-speed mode afterwards.
    assert "not a memory test" in capsys.readouterr().out


def test_traffic_is_logged_as_hex_except_for_dumps(tmp_path):
    run(tmp_path, "--sim", "healthy", "mem", "faults")
    log = (tmp_path / "mem" / "traffic.log").read_text()
    assert ">> 3D 00 02 01 00 40" in log
    size_before = len(log)
    dump(tmp_path, "healthy", "--passes", "1")
    assert len((tmp_path / "mem" / "traffic.log").read_text()) == size_before


def test_a_board_must_be_chosen(tmp_path):
    with pytest.raises(SystemExit, match="No interface board selected"):
        run(tmp_path, "mem", "probe")


def test_unknown_scenario_is_a_clean_error(tmp_path, capsys):
    assert run(tmp_path, "--sim", "nope", "mem", "probe") == 1
    assert "unknown scenario" in capsys.readouterr().err

import csv
import json

import pytest

from tjdiag.cli import main


def run(tmp_path, *args):
    return main(["--dir", str(tmp_path), *args])


def events(tmp_path, kind):
    return sorted(tmp_path.glob(f"{kind}-*.json"))


def test_probe_exit_code_reflects_the_verdict(tmp_path, capsys):
    assert run(tmp_path, "--sim", "pcm", "probe") == 0
    assert "Verdict: USABLE" in capsys.readouterr().out
    assert run(tmp_path, "--sim", "clone", "probe") == 1
    assert "Verdict: UNUSABLE" in capsys.readouterr().out


def test_snapshot_prints_and_saves(tmp_path, capsys):
    assert run(tmp_path, "--sim", "tcm", "snapshot", "--label", "cold") == 0
    out = capsys.readouterr().out
    assert "Transmission controller (TCM) [0x18]" in out and "P0601" in out
    (path,) = events(tmp_path, "snapshot")
    saved = json.loads(path.read_text())
    assert saved["label"] == "cold"
    assert saved["modules"]["0x18"]["stored"] == ["P0601"]
    assert saved["modules"]["0x10"]["stored"] == ["P0700"]


def test_raw_traffic_is_logged(tmp_path):
    run(tmp_path, "--sim", "pcm", "snapshot")
    log = (tmp_path / "traffic.log").read_text()
    assert ">> 'ATZ\\r'" in log and "ELM327" in log


def test_clear_refuses_without_confirmation(tmp_path, capsys):
    assert run(tmp_path, "--sim", "pcm", "clear") == 2
    assert "readiness" in capsys.readouterr().out
    assert not events(tmp_path, "clear") and not events(tmp_path, "snapshot")


def test_clear_saves_a_snapshot_first(tmp_path, capsys):
    assert run(tmp_path, "--sim", "pcm", "clear", "--yes") == 0
    (before,) = events(tmp_path, "snapshot")
    assert json.loads(before.read_text())["modules"]["0x10"]["stored"] == ["P0601"]
    assert len(events(tmp_path, "clear")) == 1


def test_clear_refuses_while_the_engine_runs(tmp_path, capsys):
    assert run(tmp_path, "--sim", "pcm:after-clear-running", "clear", "--yes") == 2
    assert "engine is running" in capsys.readouterr().out
    assert not events(tmp_path, "clear")


def test_full_flow_reaches_a_hard_fault_diagnosis(tmp_path, capsys):
    run(tmp_path, "--sim", "pcm", "snapshot")
    run(tmp_path, "--sim", "pcm", "clear", "--yes")
    run(tmp_path, "--sim", "pcm:after-clear-keyon", "snapshot")
    capsys.readouterr()
    assert run(tmp_path, "diagnose") == 0
    assert "failing its own power-up self-test" in capsys.readouterr().out


def test_full_flow_reaches_a_supply_diagnosis(tmp_path, capsys):
    run(tmp_path, "--sim", "lowvolt", "clear", "--yes")
    run(tmp_path, "--sim", "lowvolt:after-clear-keyon", "snapshot")
    run(tmp_path, "--sim", "lowvolt", "watch", "--seconds", "1")
    run(tmp_path, "--sim", "lowvolt:after-clear-running", "snapshot")
    capsys.readouterr()
    run(tmp_path, "diagnose")
    out = capsys.readouterr().out
    assert "power and grounds before the computer itself" in out


def test_full_flow_reports_a_code_that_stayed_away(tmp_path, capsys):
    run(tmp_path, "--sim", "transient", "clear", "--yes")
    run(tmp_path, "--sim", "transient:after-clear-running", "snapshot")
    capsys.readouterr()
    run(tmp_path, "diagnose")
    assert "has not returned since it was cleared" in capsys.readouterr().out


def test_watch_writes_csv_and_summary(tmp_path, capsys):
    assert run(tmp_path, "--sim", "lowvolt", "watch", "--seconds", "1") == 0
    (summary_path,) = events(tmp_path, "watch")
    summary = json.loads(summary_path.read_text())
    assert summary["min_voltage"] == 8.4
    with open(tmp_path / summary["csv"], newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == summary["samples"] > 0
    assert rows[0]["pcm_answered"] == "1"


def test_sniff_reports_identifiers(tmp_path, capsys):
    assert run(tmp_path, "--sim", "pcm", "sniff", "--seconds", "0.5") == 0
    assert "0x10" in capsys.readouterr().out
    assert (tmp_path / "sniff.log").read_text().count("\n") > 2


def test_diagnose_with_nothing_saved(tmp_path, capsys):
    assert run(tmp_path / "empty", "diagnose") == 0
    assert "No snapshots saved yet" in capsys.readouterr().out


def test_vehicle_not_answering_is_a_clean_error(tmp_path, capsys):
    assert run(tmp_path, "--sim", "ignition-off", "snapshot") == 1
    assert "ignition is ON" in capsys.readouterr().err


def test_unknown_scenario_is_a_clean_error(tmp_path, capsys):
    assert run(tmp_path, "--sim", "nope", "probe") == 1
    assert "unknown scenario" in capsys.readouterr().err


def test_an_adapter_must_be_chosen(tmp_path):
    with pytest.raises(SystemExit, match="No adapter selected"):
        run(tmp_path, "snapshot")

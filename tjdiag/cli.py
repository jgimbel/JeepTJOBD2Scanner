"""Command line entry point: tjdiag <command>."""

from __future__ import annotations

import argparse
import sys
from contextlib import ExitStack
from pathlib import Path

from . import __version__, interpret, memcli, obd
from .elm import Elm327, ElmError
from .probe import UNUSABLE, run_probe
from .scanner import ScannerError
from .sci import ForbiddenCommand
from .sim import SCENARIOS, STAGES, make_sim
from .sniff import format_sniff, run_sniff
from .snapshot import (
    format_snapshot,
    load_events,
    now_iso,
    reserve_path,
    save_event,
    take_snapshot,
)
from .transport import LoggingTransport, SerialTransport, list_serial_ports
from .watch import format_watch, run_watch

DEFAULT_DIR = "tjdiag-data"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tjdiag",
        description="P0601 debugger for a 2004 Jeep Wrangler over an ELM327-type OBD2 adapter.",
    )
    parser.add_argument("--version", action="version", version=f"tjdiag {__version__}")
    parser.add_argument("--port", help="serial port of the adapter (see 'tjdiag ports')")
    parser.add_argument(
        "--baud",
        type=int,
        help="serial speed (default 38400 for the OBD2 adapter, 250000 for the interface board)",
    )
    parser.add_argument(
        "--sim",
        metavar="SCENARIO",
        help="use the built-in simulator instead of real hardware. OBD2 scenarios: "
        + ", ".join(SCENARIOS)
        + "; optionally followed by :"
        + " or :".join(STAGES)
        + ". For 'mem' commands see 'tjdiag mem --help'",
    )
    parser.add_argument(
        "--dir",
        default=DEFAULT_DIR,
        help=f"folder for snapshots, logs and the raw traffic log (default ./{DEFAULT_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("ports", help="list serial ports, to find the adapter")
    sub.add_parser("probe", help="test whether this adapter can do the job on this Jeep")

    snap = sub.add_parser("snapshot", help="read codes, freeze frame and live data from every module")
    snap.add_argument("--label", help="note stored with the snapshot, e.g. 'key on, cold'")

    watch = sub.add_parser("watch", help="log voltage and engine speed (start the engine while it runs)")
    watch.add_argument("--seconds", type=float, default=120, help="how long to log (default 120)")

    sniff = sub.add_parser("sniff", help="passively capture PCI bus traffic")
    sniff.add_argument("--seconds", type=float, default=10, help="how long to listen (default 10)")

    clear = sub.add_parser("clear", help="save a snapshot, then clear all codes")
    clear.add_argument("--yes", action="store_true", help="required: confirms you want codes cleared")

    sub.add_parser("diagnose", help="interpret everything saved so far")

    memcli.add_parser(sub)

    raw = sub.add_parser("raw", help="send one command to the OBD2 adapter and print the reply")
    raw.add_argument("text", help="for example 'ATRV' or '01 0C'")
    return parser


def _open_adapter(args: argparse.Namespace, out_dir: Path, stack: ExitStack) -> Elm327:
    if args.sim:
        transport = make_sim(args.sim)
    elif args.port:
        transport = SerialTransport(args.port, args.baud or 38400)
    else:
        raise SystemExit(
            "No adapter selected. Use --port <serial port> (run 'tjdiag ports' to find it) "
            "or --sim <scenario> to try the tool without a vehicle."
        )
    stack.callback(transport.close)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = stack.enter_context(open(out_dir / "traffic.log", "a"))
    log.write(f"--- {now_iso()} tjdiag {args.command} ---\n")
    return Elm327(LoggingTransport(transport, log))


def _cmd_ports() -> int:
    ports = list_serial_ports()
    if not ports:
        print("No serial ports found. Pair the Bluetooth adapter with this computer first.")
        return 1
    for device, description in ports:
        print(f"{device}\t{description}")
    return 0


def _cmd_probe(elm: Elm327) -> int:
    result = run_probe(elm)
    print(result.format())
    return 1 if result.verdict == UNUSABLE else 0


def _cmd_snapshot(elm: Elm327, out_dir: Path, label: str | None) -> int:
    protocol = obd.connect(elm)
    snapshot = take_snapshot(elm, protocol, label)
    path = save_event(snapshot, out_dir, "snapshot")
    print(format_snapshot(snapshot))
    print(f"\nSaved to {path}")
    return 0


def _cmd_watch(elm: Elm327, out_dir: Path, seconds: float) -> int:
    obd.connect(elm)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Logging for {seconds:.0f} s. Start the engine now if you want cranking captured.")

    def show(row: dict) -> None:
        voltage = "  ?  " if row["adapter_voltage"] is None else f"{row['adapter_voltage']:5.1f}"
        rpm = "no answer" if row["rpm"] is None else f"{row['rpm']:.0f} rpm"
        print(f"  {row['seconds']:7.1f} s  {voltage} V  {rpm}")

    path = reserve_path(out_dir, "watch")
    csv_path = path.with_suffix(".csv")
    with open(csv_path, "w", newline="") as csv_file:
        summary = run_watch(elm, seconds, csv_file, on_sample=show, min_interval=0.1)
    summary["csv"] = csv_path.name
    save_event(summary, out_dir, "watch", path)
    print()
    print(format_watch(summary))
    print(f"\nSaved to {csv_path}")
    return 0


def _cmd_sniff(elm: Elm327, out_dir: Path, seconds: float) -> int:
    elm.reset()
    elm.set_protocol("2")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "sniff.log"
    with open(log_path, "a") as log:
        log.write(f"--- {now_iso()} ---\n")
        summary = run_sniff(elm, seconds, log)
    print(format_sniff(summary))
    print(f"\nRaw frames appended to {log_path}")
    return 0 if summary["frames"] else 1


def _cmd_clear(elm: Elm327, out_dir: Path, confirmed: bool) -> int:
    if not confirmed:
        print(
            "Clearing codes also erases the freeze frame and resets the readiness "
            "monitors (the Jeep will not pass an emissions test until they complete "
            "again after some driving).\n"
            "A snapshot is saved first, so nothing is lost for this investigation.\n"
            "Run 'tjdiag clear --yes' with the key ON and the engine OFF to go ahead."
        )
        return 2
    protocol = obd.connect(elm)
    snapshot = take_snapshot(elm, protocol, "before clearing")
    path = save_event(snapshot, out_dir, "snapshot")
    print(f"Snapshot saved to {path}")
    if snapshot["engine_running"]:
        print("The engine is running. Switch it off (key ON, engine OFF) and run this again.")
        return 2

    acknowledged, response = obd.clear_dtcs(elm)
    if not acknowledged:
        print(f"No module acknowledged the clear request (adapter said: {response.lines}).")
        return 1
    save_event(
        {
            "tool": "tjdiag",
            "event": "clear",
            "taken_at": now_iso(),
            "acknowledged": [f"0x{a:02X}" for a in acknowledged],
        },
        out_dir,
        "clear",
    )
    print("Cleared. Acknowledged by: " + ", ".join(obd.module_label(a) for a in acknowledged))
    print(
        "\nNext:\n"
        "  1. Key off for 30 seconds, key ON without starting: tjdiag snapshot\n"
        "  2. tjdiag watch   (start the engine while it logs)\n"
        "  3. tjdiag snapshot   (engine running)\n"
        "  4. tjdiag diagnose"
    )
    return 0


def _cmd_diagnose(out_dir: Path) -> int:
    events = load_events(out_dir) if out_dir.is_dir() else []
    print(interpret.diagnose(events).format())
    return 0


def _cmd_raw(elm: Elm327, text: str) -> int:
    for line in elm.command(text, timeout=20.0):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    out_dir = Path(args.dir)

    if args.command == "ports":
        return _cmd_ports()
    if args.command == "diagnose":
        return _cmd_diagnose(out_dir)

    try:
        if args.command == "mem":
            return memcli.run(args, out_dir)
        with ExitStack() as stack:
            elm = _open_adapter(args, out_dir, stack)
            if args.command == "probe":
                return _cmd_probe(elm)
            if args.command == "snapshot":
                return _cmd_snapshot(elm, out_dir, args.label)
            if args.command == "watch":
                return _cmd_watch(elm, out_dir, args.seconds)
            if args.command == "sniff":
                return _cmd_sniff(elm, out_dir, args.seconds)
            if args.command == "clear":
                return _cmd_clear(elm, out_dir, args.yes)
            if args.command == "raw":
                return _cmd_raw(elm, args.text)
    except (
        ElmError,
        ScannerError,
        ForbiddenCommand,
        obd.VehicleNotResponding,
        ValueError,
        RuntimeError,
        OSError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())

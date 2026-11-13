"""The 'tjdiag mem ...' commands: memory tests through the SCI interface board."""

from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

from . import memory, memtest, sci
from .scanner import BRIDGE, UPSTREAM, USB_BAUD, Scanner
from .scisim import SCENARIOS, make_mem_sim
from .snapshot import now_iso, reserve_path
from .transport import LoggingTransport, SerialTransport

# What each firmware should report about itself: version prefix and chip.
EXPECTED = {UPSTREAM: ("0.9", "ATmega2560"), BRIDGE: ("1.", "ESP32")}


def add_parser(sub: argparse._SubParsersAction) -> None:
    mem = sub.add_parser(
        "mem",
        help="test the engine computer's memory through the SCI interface board",
        description="Memory tests through the Arduino Mega SCI interface. "
        "With --sim, scenarios are: " + ", ".join(SCENARIOS),
    )
    actions = mem.add_subparsers(dest="mem_command", required=True, metavar="action")

    actions.add_parser("selftest", help="check the interface board on the bench")
    actions.add_parser("probe", help="check the SCI link and find out what can be read")
    actions.add_parser("faults", help="read the engine computer's own fault list")
    actions.add_parser("ram", help="capture the RAM tables (informational, experimental)")

    dump = actions.add_parser("dump", help="read a memory region to a file, resumable")
    dump.add_argument("--region", choices=sorted(sci.REGIONS), default="rom")
    dump.add_argument("--start", type=lambda v: int(v, 0), default=0, help="first address (default 0)")
    dump.add_argument("--size", type=lambda v: int(v, 0), help="bytes to read (default: from 'mem probe')")
    dump.add_argument("--passes", type=int, default=2, help="how many times to read it (default 2)")
    dump.add_argument("--label", default="default", help="name for this set of dumps")
    dump.add_argument(
        "--interval-ms", type=int, default=memory.DEFAULT_INTERVAL_MS,
        help=f"pause between requests (default {memory.DEFAULT_INTERVAL_MS})",
    )
    dump.add_argument(
        "--min-volts", type=float, default=memory.MIN_BATTERY_VOLTS,
        help=f"stop below this battery voltage (default {memory.MIN_BATTERY_VOLTS})",
    )
    dump.add_argument("--no-battery-check", action="store_true", help="do not stop on low battery")
    dump.add_argument("--log-traffic", action="store_true", help="record every byte (large file)")

    test = actions.add_parser("test", help="run the block tests on saved dumps")
    test.add_argument("--region", choices=sorted(sci.REGIONS), help="default: every region dumped")
    test.add_argument("--label", default="default")
    test.add_argument("--reference", type=Path, help="known-good image to compare against")
    test.add_argument("--block-size", type=lambda v: int(v, 0), help="override the block size")
    test.add_argument("--vin", help="check that this VIN is stored in the EEPROM")


def _mem_dir(out_dir: Path) -> Path:
    return out_dir / "mem"


def _open(args: argparse.Namespace, out_dir: Path, stack: ExitStack, log: bool = True) -> Scanner:
    if args.sim:
        transport, boot_wait = make_mem_sim(args.sim), 0.05
    elif args.port:
        transport, boot_wait = SerialTransport(args.port, args.baud or USB_BAUD), 2.5
    else:
        raise SystemExit(
            "No interface board selected. Use --port <serial port of the Mega> "
            "(run 'tjdiag ports' to find it) or --sim <scenario>."
        )
    stack.callback(transport.close)
    if log:
        folder = _mem_dir(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        handle = stack.enter_context(open(folder / "traffic.log", "a"))
        handle.write(f"--- {now_iso()} tjdiag mem {args.mem_command} ---\n")
        transport = LoggingTransport(transport, handle, binary=True)
    scanner = Scanner(transport)
    scanner.open(boot_wait=boot_wait)
    return scanner


def _line(status: str, name: str, detail: str) -> None:
    print(f"[{status:4}] {name}: {detail}")


# -- selftest -------------------------------------------------------------------


def cmd_selftest(scanner: Scanner) -> int:
    _line("PASS", "Firmware handshake", f"the board answers as '{scanner.firmware}'")
    wanted_version, wanted_mcu = EXPECTED[scanner.firmware]
    version = scanner.firmware_version()
    if version.startswith(wanted_version):
        _line("PASS", "Firmware version", version)
    else:
        _line(
            "WARN", "Firmware version",
            f"{version}; this tool was written against {wanted_version}x and may not match",
        )
    status = scanner.status()
    if status["mcu"] == wanted_mcu:
        _line("PASS", "Microcontroller", wanted_mcu)
    else:
        found = status["mcu"] or f"signature {status['mcu_signature']}"
        _line("WARN", "Microcontroller", f"{found}, expected {wanted_mcu} for this firmware")
    if status["battery_volts"]:
        _line("INFO", "Battery sense", f"{status['battery_volts']:.1f} V on the divider")
    else:
        _line(
            "INFO", "Battery sense",
            "nothing sensed. Normal on the bench; at the Jeep it means the divider "
            "or the pin 16 wire is missing",
        )

    scanner.configure_sci()
    reply = scanner.send_sci(bytes([sci.CMD_LOW_SPEED]), 1.0)
    if reply == bytes([sci.CMD_LOW_SPEED]):
        _line("PASS", "Serial loopback", "a byte sent on TX2 came back on RX2")
        ok = True
    elif reply:
        _line("FAIL", "Serial loopback", f"sent FE, received {reply.hex(' ').upper()}")
        ok = False
    else:
        _line(
            "FAIL", "Serial loopback",
            "nothing came back on RX2. On the bench, fit a jumper between the OBD "
            "plug's pin 6 and pin 7 and run this again",
        )
        ok = False
    print()
    print("Board is ready." if ok else "Board is not ready; fix the FAIL line first.")
    return 0 if ok else 1


# -- probe ----------------------------------------------------------------------


def _report_battery(scanner: Scanner) -> float:
    volts = scanner.battery_volts()
    if volts:
        _line("INFO", "Battery", f"{volts:.1f} V at the connector")
    else:
        _line("WARN", "Battery", "no voltage sensed on pin 16; the low-battery guard cannot work")
    return volts


def _require_link(link: memory.MemoryLink) -> bool:
    link.start()
    if link.alive():
        _line("PASS", "SCI link", "the engine computer echoes")
        return True
    _line(
        "FAIL", "SCI link",
        "no echo from the engine computer. Check, in this order: key ON (engine "
        "off); the plug is fully seated; TX2 reaches pin 6 and pin 7 reaches RX2 "
        "with its 1k pull-up. If the wiring diagram for this Jeep shows SCI "
        "receive on pin 12, move the TX2 wire there",
    )
    return False


def cmd_probe(scanner: Scanner, out_dir: Path) -> int:
    _report_battery(scanner)
    link = memory.MemoryLink(scanner)
    if not _require_link(link):
        return 1
    faults = sci.parse_fault_list(scanner.send_sci(bytes([sci.CMD_FAULT_LIST]), 2.0))
    print()
    print("Fault list")
    print("  " + faults.format().replace("\n", "\n  "))

    results = {}
    for name, region in sci.REGIONS.items():
        print()
        print(f"Probing {region.title} ...")
        results[name] = memory.probe_region(link, region)
        print(memory.format_probe(results[name]))

    folder = _mem_dir(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "probe.json").write_text(
        json.dumps({"tool": "tjdiag", "taken_at": now_iso(), "regions": results}, indent=2) + "\n"
    )

    print()
    usable = [n for n, r in results.items() if r["supported"] and r["suggested_size"]]
    if not usable:
        print("Nothing could be read with the ordinary read commands on this computer.")
        return 1
    print("Next:")
    rom = "rom" if "rom" in usable else ("rom16" if "rom16" in usable else None)
    if rom:
        print(f"  tjdiag --port <port> mem dump --region {rom}")
    if "eeprom" in usable:
        print("  tjdiag --port <port> mem dump --region eeprom")
    print("  tjdiag mem test")
    return 0


def cmd_faults(scanner: Scanner) -> int:
    link = memory.MemoryLink(scanner)
    if not _require_link(link):
        return 1
    faults = sci.parse_fault_list(scanner.send_sci(bytes([sci.CMD_FAULT_LIST]), 2.0))
    print(faults.format())
    return 0 if faults.checksum_ok else 1


# -- dump -----------------------------------------------------------------------


def _probed_size(out_dir: Path, region: str) -> int | None:
    path = _mem_dir(out_dir) / "probe.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())["regions"][region]["suggested_size"] or None
    except (KeyError, json.JSONDecodeError):
        return None


def cmd_dump(scanner: Scanner, out_dir: Path, args: argparse.Namespace) -> int:
    region = sci.REGIONS[args.region]
    size = args.size or _probed_size(out_dir, args.region)
    if not size:
        print(
            f"The size of the {args.region} region is not known. Run 'tjdiag mem probe' "
            "first, or pass --size."
        )
        return 2
    if args.start < 0 or args.start + size > region.address_space:
        print("That range is outside the region's address space.")
        return 2

    volts = _report_battery(scanner)
    min_battery = None if args.no_battery_check or not volts else args.min_volts
    link = memory.MemoryLink(scanner, args.interval_ms)
    if not _require_link(link):
        return 1

    folder = _mem_dir(out_dir) / args.label
    last_shown = [-1]

    def progress(state: memory.DumpState) -> None:
        percent = state.done * 100 // state.size
        if percent // 5 != last_shown[0]:
            last_shown[0] = percent // 5
            print(f"    {percent:3}%  {state.done}/{state.size} bytes, {state.missing_count} missing")

    for number in range(1, args.passes + 1):
        path = memory.pass_path(folder, region.name, number)
        existing = memory.load_state(path)
        if existing and existing.complete:
            print(f"Pass {number}: already complete ({path.name})")
            continue
        verb = "resuming" if existing else "reading"
        print(f"Pass {number}: {verb} {size} bytes from {args.start:#08x} into {path}")
        last_shown[0] = -1
        try:
            state = memory.dump_region(
                link, region, args.start, size, path, min_battery=min_battery, progress=progress
            )
        except memory.DumpStopped as stop:
            print(f"\nStopped: {stop}")
            return 3
        except ValueError as error:
            print(f"\n{error}")
            return 2
        except KeyboardInterrupt:
            print("\nInterrupted. Progress is saved; run the same command again to continue.")
            return 3
        print(f"  done, {state.missing_count} address(es) missing")
    print("\nAll passes complete. Now run: tjdiag mem test")
    return 0


# -- test -----------------------------------------------------------------------


def _load_passes(folder: Path, region: str) -> tuple[list[memtest.Pass], memory.DumpState | None, int]:
    passes, first, incomplete = [], None, 0
    for path, state in memory.existing_passes(folder, region):
        if not state.complete:
            incomplete += 1
            continue
        if first is None:
            first = state
        elif (state.start, state.size) != (first.start, first.size):
            continue
        passes.append(memtest.Pass(path.read_bytes(), state.missing_offsets()))
    return passes, first, incomplete


def cmd_test(out_dir: Path, args: argparse.Namespace) -> int:
    folder = _mem_dir(out_dir) / args.label
    regions = [args.region] if args.region else list(sci.REGIONS)
    reference = None
    if args.reference:
        if not args.region:
            print("--reference needs --region, to say which memory the image is for.")
            return 2
        reference = args.reference.read_bytes()

    failed = False
    tested = 0
    for name in regions:
        region = sci.REGIONS[name]
        passes, state, incomplete = _load_passes(folder, name)
        if incomplete:
            print(f"{region.title}: {incomplete} unfinished pass(es) ignored; run 'mem dump' to finish.\n")
        if not passes or state is None:
            continue
        tested += 1
        report = memtest.classify_blocks(
            name, state.start, passes, args.block_size or region.block_size, reference
        )
        image = passes[0].data
        if name.startswith("rom") and not passes[0].missing:
            report.checksum_searched = True
            report.checksum_hits = memtest.search_checksums(image, state.start)
        if name == "eeprom":
            report.strings = memtest.find_strings(image, state.start, minimum=8)
            if args.vin:
                report.vin = args.vin.upper()
                report.vin_found = report.vin.encode("ascii") in image
        print(memtest.format_report(report, region.title))
        print()
        failed = failed or report.failed
        result_path = reserve_path(folder, f"report-{name}")
        result_path.write_text(
            json.dumps({"tool": "tjdiag", "taken_at": now_iso(), **asdict(report)}, indent=2) + "\n"
        )
    if not tested:
        print(f"No complete dumps found in {folder}. Run 'tjdiag mem dump' first.")
        return 2
    return 1 if failed else 0


# -- ram ------------------------------------------------------------------------


def cmd_ram(scanner: Scanner, out_dir: Path) -> int:
    link = memory.MemoryLink(scanner)
    if not _require_link(link):
        return 1
    reply = scanner.send_sci(bytes([sci.CMD_HIGH_SPEED]), 2.0)
    if not reply or reply[0] != sci.CMD_HIGH_SPEED:
        print("The engine computer did not accept high-speed mode; RAM tables are not available.")
        return 1
    tables = {}
    try:
        for table in sci.RAM_TABLES:
            data = scanner.send_sci(bytes([table, 0xFF]), 3.0)
            if not data or data[0] != table:
                continue
            pairs = data[1:]
            values = {pairs[i]: pairs[i + 1] for i in range(0, len(pairs) - 1, 2)}
            tables[f"{table:02X}"] = bytes(values.get(o, 0) for o in range(sci.RAM_TABLE_SIZE)).hex()
    finally:
        scanner.send_sci(bytes([sci.CMD_LOW_SPEED]), 2.0)
        scanner.configure_sci()

    if not tables:
        print("High-speed mode was accepted, but no RAM table answered.")
        return 1
    folder = _mem_dir(out_dir)
    path = reserve_path(folder, "ram")
    path.write_text(json.dumps({"tool": "tjdiag", "taken_at": now_iso(), "tables": tables}, indent=2) + "\n")
    print(f"Captured {len(tables)} RAM table(s): {', '.join(tables)}")
    print(f"Saved to {path}")
    print(
        "This is a snapshot of working values, not a memory test: RAM cannot be "
        "tested from outside."
    )
    return 0


def run(args: argparse.Namespace, out_dir: Path) -> int:
    if args.mem_command == "test":
        return cmd_test(out_dir, args)
    log = args.mem_command != "dump" or args.log_traffic
    with ExitStack() as stack:
        scanner = _open(args, out_dir, stack, log=log)
        if args.mem_command == "selftest":
            return cmd_selftest(scanner)
        if args.mem_command == "probe":
            return cmd_probe(scanner, out_dir)
        if args.mem_command == "faults":
            return cmd_faults(scanner)
        if args.mem_command == "dump":
            return cmd_dump(scanner, out_dir, args)
        if args.mem_command == "ram":
            return cmd_ram(scanner, out_dir)
    return 1

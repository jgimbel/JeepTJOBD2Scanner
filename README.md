# JeepTJOBD2Scanner
OBD2 scanner using SAE J1850 VPW. Meant to work on JTEC and 42RLE transmission computers

## tjdiag — P0601 debugger for a 2004 Jeep Wrangler

A command line tool that connects to the Jeep through an ELM327-type OBD2
adapter and works out what is behind a P0601 ("internal controller failure /
memory checksum") code, instead of guessing and replacing the computer.

It answers three questions, in order:

1. **Which computer reports P0601?** The engine computer (PCM) or the separate
   transmission computer (TCM) of the 42RLE automatic.
2. **Does it come back after clearing, and when?** At key-on before the engine
   starts, only once the engine has cranked, or not at all.
3. **Is the supply to blame?** Voltage codes, low resting voltage, cranking
   dips, or the computer going silent.

If those point at the engine computer itself, a second stage reads its memory
over Chrysler's SCI link and tests it block by block. That stage needs a small
interface board you build around an Arduino Mega 2560 or an ESP32; see
[Memory test](#memory-test).

> **Status:** every command has been run against built-in simulators only.
> Nothing has been run on the Jeep yet, and neither interface board has been
> built. The first real runs are the real test; everything the adapters say is
> recorded in log files so any surprise can be fixed from them.

## Install

Needs Python 3.10 or newer on a laptop.

```
git clone https://github.com/jgimbel/JeepTJOBD2Scanner.git
cd JeepTJOBD2Scanner
pip install -e .
```

## Connect the adapter

1. Plug the adapter into the diagnostic connector under the dash and turn the
   key to ON (the engine can stay off).
2. Pair it with the laptop in the operating system's Bluetooth settings (the
   PIN on these dongles is usually `1234` or `0000`). USB adapters need no
   pairing.
3. Find its serial port:

   ```
   tjdiag ports
   ```

   Typical names: `COM5` (Windows), `/dev/cu.OBDII-SPPDev` (macOS),
   `/dev/rfcomm0` (Linux, after `sudo rfcomm bind 0 <adapter address>`).

If the dongle pairs but no serial port appears, it is a Bluetooth Low Energy
device, which this version cannot open. See
[docs/adapter-evaluation.md](docs/adapter-evaluation.md).

## The procedure

Run these in order. Replace `COM5` with your port.

| Step | Key position | Command | Purpose |
|---|---|---|---|
| 1 | ON, engine off | `tjdiag --port COM5 probe` | Is this adapter good enough? |
| 2 | ON, engine off | `tjdiag --port COM5 snapshot` | Which computer holds the code, freeze frame, voltage |
| 3 | — | multimeter checks in [docs/diagnostic-flow.md](docs/diagnostic-flow.md) | Rule out battery and grounds |
| 4 | ON, engine off | `tjdiag --port COM5 clear --yes` | Saves a snapshot, then clears |
| 5 | off 30 s, then ON | `tjdiag --port COM5 snapshot` | Did it come back before the engine ran? |
| 6 | start while it logs | `tjdiag --port COM5 watch` | Catch a voltage dip during cranking |
| 7 | engine running | `tjdiag --port COM5 snapshot` | Did it come back once running? |
| 8 | — | `tjdiag diagnose` | Conclusion and next steps |

`tjdiag diagnose` can be run at any point; it tells you what is known so far
and what to do next.

## Commands

| Command | What it does |
|---|---|
| `ports` | Lists serial ports. |
| `probe` | Tests the adapter: identity, J1850 VPW, module addressing, checksums, code reading, bus monitoring. Clears nothing. |
| `snapshot [--label TEXT]` | Reads stored and pending codes per module, readiness, freeze frame, live data. Saves JSON. |
| `watch [--seconds N]` | Logs connector voltage and engine speed to CSV. |
| `sniff [--seconds N]` | Records raw PCI bus traffic and counts message identifiers. |
| `clear --yes` | Saves a snapshot, then clears codes. Refuses while the engine runs. |
| `diagnose` | Interprets everything saved so far. |
| `raw "TEXT"` | Sends one command to the adapter and prints the reply. |

Global options: `--port`, `--baud`, `--dir` (where files are saved, default
`./tjdiag-data`), `--sim` (see below). They go before the command name, for
example `tjdiag --port COM5 snapshot`.

## Try it without the Jeep

```
tjdiag --sim pcm probe
tjdiag --sim pcm snapshot
tjdiag --sim pcm clear --yes
tjdiag --sim pcm:after-clear-keyon snapshot
tjdiag diagnose
```

Scenarios: `pcm`, `tcm`, `pcm-silent-tcm`, `lowvolt`, `transient`, `clean`,
`clone`, `limited`, `ignition-off`. Add `:after-clear-keyon` or
`:after-clear-running` to see the vehicle after a clear. Use a separate
`--dir` for simulator runs so they do not mix with real data.

## Things to know before using it on the Jeep

- **Clearing codes resets the readiness monitors.** The Jeep will not pass an
  emissions test until they complete again after some driving.
- **The voltage shown is measured by the adapter at the connector**, not at the
  computer, and cheap adapters can be off by a few tenths of a volt. Confirm
  anything important with the multimeter.
- **The adapter samples a few times a second.** A very short dip can be missed;
  a clean `watch` log is weaker evidence than a dip is.
- **The diagnosis is a set of rules applied to the data on hand.** It narrows
  the search. It does not replace measuring the computer's power and grounds.
- **This tool only reads and clears.** It cannot write to or reprogram a
  computer.

## Memory test

No ELM327-type adapter can use Chrysler's SCI link, which is the only route to
the engine computer's own memory. If the procedure above ends at "the computer
fails its own self-test", the next step is to read that memory and test it
block by block.

**Hardware:** one of two boards, both driven by the same commands.

| Board | Firmware | Doc |
|---|---|---|
| Arduino Mega 2560 | The open-source ChryslerScanner firmware, unchanged. Proven on real Chrysler computers | [docs/sci-interface-hardware.md](docs/sci-interface-hardware.md) |
| ESP32: Waveshare ESP32-C5-WIFI6-KIT, or a WROOM-32 DevKit | This project's own, in `firmware/esp32-sci-bridge/`. Read-only by construction, but new; needs a level shifter | [docs/sci-interface-esp32.md](docs/sci-interface-esp32.md) |

**Scripts:** the `tjdiag mem` commands. The full procedure, and what each
result does and does not prove, is in
[docs/memory-test.md](docs/memory-test.md).

| Command | What it does |
|---|---|
| `mem selftest` | Checks the board on the bench: firmware, loopback, battery sense. |
| `mem probe` | At the Jeep: is the link alive, which read commands answer, how much memory there is, how long a dump will take. |
| `mem faults` | Reads the engine computer's own fault list over SCI. |
| `mem dump --region rom\|rom16\|eeprom` | Reads a region to a file, twice by default. Resumable. |
| `mem test [--reference FILE] [--vin VIN]` | Tests the saved dumps block by block and prints the report. |
| `mem ram` | Captures the RAM tables. Informational and experimental. |

These commands can only read. Every command that writes, erases or operates an
actuator is refused inside the tool before it reaches the wire.

## Layout

```
tjdiag/
  transport.py   serial port and traffic logging
  elm.py         ELM327 driver and frame parsing
  obd.py         OBD-II services, per module
  probe.py       adapter capability test
  snapshot.py    full read, JSON save/load
  watch.py       voltage logger
  sniff.py       PCI bus capture
  interpret.py   diagnosis rules
  sim.py         simulated adapter and vehicle
  scanner.py     driver for the Mega interface board
  sci.py         SCI read commands and the read-only guard
  memory.py      memory probing and resumable dumps
  memtest.py     block tests and report
  scisim.py      simulated interface board and engine computer
  memcli.py      the 'mem' commands
  cli.py         command line
firmware/
  esp32-sci-bridge/   read-only SCI bridge firmware for an ESP32 board
tests/           pytest suite (runs against the simulators and the firmware core)
docs/            diagnostic flow, adapter evaluation, interface hardware, memory test
```

## Tests

```
pip install -e ".[dev]"
pytest
```

The firmware tests compile part of the ESP32 firmware for the laptop and need
`g++` or `clang++`; they are skipped if neither is installed.

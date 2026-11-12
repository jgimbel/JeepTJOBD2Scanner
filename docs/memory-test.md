# Testing the engine computer's memory

This reads the engine computer's memory over the SCI link, one byte at a time,
and reports on it block by block. It needs an interface board: either the
[Arduino Mega version](sci-interface-hardware.md) or the
[ESP32 version](sci-interface-esp32.md). The commands are the same for both.

Do the OBD2 steps in [diagnostic-flow.md](diagnostic-flow.md) first. This is
worth the effort only if they end at "the engine computer fails its own
self-test" with its power and grounds measured good.

> **Status:** every command has been run against the built-in simulator only.
> What this particular computer answers, how much memory it exposes and how
> fast it reads are unknown until `mem probe` is run on the Jeep.

## What gets tested

| Memory | Command used | What it holds |
|---|---|---|
| ROM | `26` (24-bit address) or `15` (16-bit address) | Program and calibration |
| EEPROM | `28` | VIN, mileage, immobilizer data, learned values |
| RAM | high-speed tables | Working values; captured, not tested (see below) |

Each block is put through up to three tests:

| Test | Passes when | Proves |
|---|---|---|
| Readable | Every address in the block answered, with the right address echoed back | The link works and that part of memory responds |
| Repeatable | Two or more full reads gave identical bytes | The cells and their supply are stable |
| Matches reference | The block equals a known-good image | The contents are correct |

and ends up with one status:

| Status | Meaning |
|---|---|
| `VERIFIED` | Identical to the reference image. The only status that means "good" |
| `STABLE` | Readable and repeatable. Not physically failing, but nothing says the contents are right |
| `BLANK` | Readable and repeatable, but one repeated value (usually `FF`). Normal for unused space; a problem if it is where code should be |
| `READ ONCE` | Read a single time; stability not tested yet |
| `MISMATCH` | Stable, but differs from the reference. The differing bytes are listed with which bits flipped |
| `UNSTABLE` | The same address read differently on different passes. The addresses are listed |
| `UNREADABLE` | Some addresses never answered |

**Reading the same bytes twice does not make them the right bytes.** Without a
reference image the best possible result is `STABLE` everywhere, which rules
out failing memory cells but not corrupted contents. Since P0601 is a checksum
failure, corrupted-but-stable contents are a realistic cause, so the reference
comparison matters.

## Procedure

Replace `<port>` with the interface board's serial port (`tjdiag ports`).

### 1. Check the board

```
tjdiag --port <port> mem selftest
```

On the bench with the loopback jumper, as described in the board's hardware doc.
It also reports which firmware the board is running.

### 2. Connect and probe

Charger on the battery, plug in, key ON, engine off.

```
tjdiag --port <port> mem probe
```

This takes a few minutes. It reports:

- whether the computer answers at all
- its own fault list (Chrysler's internal fault numbers, not P-codes)
- for each read command, which address ranges answer, which merely repeat lower
  memory, a suggested size to dump, and the measured speed with a time estimate

If it says there is no echo, work through the list it prints. If it says the
`26` command gives no clean answers, the computer does not allow that read
without unlocking; use `--region rom16` (the first 64 KB only) and the EEPROM.

### 3. Dump

```
tjdiag --port <port> mem dump --region rom
tjdiag --port <port> mem dump --region eeprom
```

Each reads the region twice by default (`--passes N` for more) into
`tjdiag-data/mem/default/`. The size comes from the probe unless you pass
`--size`.

- **It is slow.** One byte per request. The probe gives the real estimate;
  expect an hour or more per pass for a ROM of a few hundred kilobytes.
- **It can be interrupted.** Ctrl-C, a dropped USB cable, the key being turned
  off, or the battery guard all leave the dump resumable: run the same command
  again and it continues where it stopped.
- **Battery guard.** It stops if the voltage at the connector falls below
  11.8 V (`--min-volts`). Fix the supply and run the command again.
- **Do not dump beyond the suggested size.** Addresses above the real memory
  may be the processor's hardware registers, and reading some registers has
  side effects. The probe touches a handful of them; a full sweep would touch
  all of them.
- `--interval-ms` sets the pause between requests (default 25). Lower is
  faster; if reads start going missing, raise it again.
- `--label NAME` starts a separate set of dumps, for example on another day.

### 4. Test

```
tjdiag mem test
tjdiag mem test --vin <the Jeep's VIN>
```

No hardware is needed for this step; it works on the saved files. It prints a
block map per region, lists any unstable or mismatching addresses, and ends
with a conclusion. Reports are also saved as JSON next to the dumps.

For the ROM it additionally tries common checksum schemes over the image and
its halves, quarters and eighths. A match is a useful lead that the image is
intact; no match means nothing, because the computer's own method is unknown.

For the EEPROM it lists readable text, and with `--vin` says whether the VIN is
stored there. A VIN from another vehicle would mean the computer was swapped.

### 5. Compare against a known-good image

This is what turns `STABLE` into `VERIFIED` or `MISMATCH`.

1. Read the part number from the sticker on the engine computer.
2. Find a stock image for exactly that part number. Sources are PCM tuning
   communities and sellers of replacement computers. An image for a different
   part number or calibration will differ legitimately and prove nothing.
3. Run:

   ```
   tjdiag mem test --region rom --reference stock.bin
   ```

If the image is a different size from the dump, only the overlapping part is
compared, and the report says so. An image file may also be laid out
differently from the address space the computer exposes; if nearly every block
mismatches, suspect the layout before the computer.

The other route to a reference is a second computer with the same part number,
dumped with the same tool under another `--label`, then used as `--reference`.

## Reading the result

| Result | What it means | Next |
|---|---|---|
| `UNREADABLE` blocks | Communication or range problem, not yet a memory finding | Repeat the dump; check the probe's suggested size |
| `UNSTABLE` blocks, battery on a charger | Those cells, or the supply inside the computer, are failing | The computer needs repair or replacement |
| `MISMATCH` against the right reference | The contents are corrupt in those blocks | A reflash may fix it; if it returns, the chip is failing |
| All `VERIFIED` | This memory is good | The cause of P0601 is elsewhere: the other memory, or the supply |
| All `STABLE`, no reference | No failing cells found; correctness unknown | Get a reference image |

## RAM

RAM cannot be tested from outside. A real RAM test writes patterns and reads
them back, which would mean running new code inside the computer. The computer
checks its own RAM when it starts.

```
tjdiag --port <port> mem ram
```

captures the high-speed RAM tables as a snapshot of working values. It is
informational and experimental: it switches the computer into its high-speed
mode and back, and whether this computer supports that mode is unknown.

## Safety

- Only read commands can be sent. Writing EEPROM, erasing fault codes,
  resetting learned values, actuator tests and the security commands are
  refused inside the tool before anything goes out, and there is no option to
  override that.
- Every byte is accepted only if the computer echoed the address it belongs to.
  Anything else is re-requested, then recorded as missing. Missing bytes are
  never filled in or guessed.
- Raw traffic for everything except dumps is logged to
  `tjdiag-data/mem/traffic.log` (add `--log-traffic` to log a dump too; the
  file gets large).

## Try it without hardware

```
tjdiag --dir sim-data --sim bench mem selftest
tjdiag --dir sim-data --sim healthy mem probe
tjdiag --dir sim-data --sim flaky mem dump --region rom
tjdiag --dir sim-data mem test
```

Scenarios: `healthy`, `flaky`, `erased`, `corrupt`, `no-rom24`, `key-off`,
`dies`, `lowbatt`, `bench`, `bench-open`.

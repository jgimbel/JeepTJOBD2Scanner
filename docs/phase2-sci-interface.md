# Phase 2 — reading the engine computer over Chrysler SCI

**Status: design notes, not a build guide.** Nothing here has been built or
tested. Several electrical details are unknown and are listed under
[Open questions](#open-questions-to-close-before-building). Do not connect
anything to the Jeep on the strength of this document alone.

## When to do this

Only if Phase 1 ends in one of these:

- `tjdiag diagnose` says the engine computer fails its own power-up self-test,
  and its power and grounds have been measured good at its connector.
- P0700 is set and the transmission computer's own codes cannot be read through
  generic OBD.

If Phase 1 points at the supply, or the code stays away, there is nothing for
Phase 2 to find.

## What it adds

The ELM327 adapter talks to the computers on the PCI bus using standard OBD2
requests. Chrysler's SCI link (SAE J2610) is a separate serial connection
between the diagnostic connector and the engine computer, used by the factory
scan tool. Over it, a tool can read the computer's EEPROM and ROM contents byte
by byte. That is as close to "which part of the computer is complaining" as can
be reached without opening the module.

What can realistically be learned:

| Read | What it tells you |
|---|---|
| EEPROM, twice | If two reads differ, the memory or its supply is unstable. If they match, the contents can be inspected (VIN, mileage, learned values) for obvious garbage such as blocks of `FF` or `00`. |
| ROM, twice | Same stability test for the program memory. |
| ROM against a known-good image | Byte-level differences show corruption directly. Needs an image for the same part number and calibration, from a tuning community or a donor computer. |
| Chrysler-specific fault list | More detail than the generic P0601. |

What it will not give you: the checksum routine the computer uses on itself is
not documented in anything I found, so "recompute the checksum and compare"
is not possible without that knowledge or a reference image.

## What is known, and where it comes from

All of the following comes from the open-source
[ChryslerScanner](https://github.com/laszlodaniel/ChryslerScanner) project
(GPLv3) and its author's blog, read on 2026-11-11. Most of the author's
write-ups describe the SBEC3 controller (cars and minivans), not the JTEC used
in Jeeps, so treat each line as a lead to confirm, not a fact about this Jeep.

| Item | Finding | Source | Applies to JTEC? |
|---|---|---|---|
| Low-speed rate | 7812.5 baud | firmware constants | Listed as the default SCI diagnostic speed |
| High-speed rate | 62500 baud, entered with command `12` | firmware, blog | Unconfirmed |
| ROM read | command `26`, low-speed mode, read only | blog (2021-07-02) | Unconfirmed |
| EEPROM read | command `28 XX YY`, reply `28 XX YY ZZ` | blog (2021-07-02) | Unconfirmed |
| EEPROM write | command `27`, needs a security unlock | blog (2021-07-02) | Not to be used |
| High-speed memory areas | `F0`–`FF` | firmware table | Unconfirmed |
| Full flash backup | "bootstrap" mode: battery voltage jumpered to the SCI receive pin at key-on, then a loader is uploaded into the computer's RAM | blog (2022-03-30), SBEC3 | The project's newer firmware lists JTEC and JTEC+ loaders; details not read |
| Controllers supported | SBEC2, SBEC3, EATX3, EATX4, JTEC, JTEC+; separate NGC mode | V2 API reference, Tindie listing | Yes, by name |
| Vehicle coverage as sold | "1995 – 2003 (OBD2)" | Tindie listing | See below |

**About 2003 versus 2004.** The seller's year range stops at 2003, but the same
listing names JTEC as a supported controller, and a 2004 Wrangler 4.0 normally
has a JTEC (three plugs). The likely reason for the cutoff is that many other
Chrysler models changed to the NGC controller in 2004; that is my inference, not
a statement from the author. Counting the plugs on the computer settles which
one this Jeep has.

**About the PDF in that repository.** `ChryslerCCDSCIScanner_UART_Protocol.pdf`
describes how a laptop talks to the scanner board over USB (the older V1.4X
board, built on an Arduino Mega). It is not the protocol between the scanner
and the car, and it says nothing about model years. It becomes useful only if
that board is built, because then the project's own desktop software can drive
it.

## Open questions to close before building

1. **Which connector pins carry SCI on this Jeep.** The blog mentions pin 6 as
   SCI receive in the scanner's "configuration A". A forum post about a 2006
   Wrangler disagrees with its own service manual (pin 6 versus pin 12). Use the
   2004 TJ factory wiring diagrams, then confirm with a continuity check from
   the diagnostic connector to the computer's plug.
2. **Which computer-side cavities.** One forum chart gives connector C3 cavity
   27 as SCI transmit, 29 as SCI receive and 30 as PCI bus, without stating the
   model years it covers. Confirm against the 2004 diagrams.
3. **Signal levels and the interface circuit.** I did not find the voltage
   levels, polarity, pull-ups or protection used on the SCI lines. They are in
   the project's schematic (the `PCB` folder), which has to be read before any
   circuit is drawn. An ESP32 is a 3.3 V part and must not be wired to the
   vehicle directly under any assumption.
4. **The exact JTEC command set**, including the address format of the ROM read
   and what the replies look like. This is in the project's desktop software
   source, which I could not browse.
5. **Whether the transmission computer has its own SCI pins** at this Jeep's
   connector. The older firmware has a TCM channel it describes as unused.

## Recommended build path

Do not design a new board. The open-source project's older V1.4X hardware is an
Arduino Mega with a small interface circuit, its PCB files and firmware are
published, and its desktop software already knows the controller. With Arduino
boards on hand, the shortest safe route is:

1. Read the V1.4X schematic and identify the SCI part of the circuit. The CCD
   transceiver chip is not needed for this Jeep, and the ELM327 adapter already
   covers the PCI bus.
2. Build only the SCI section on a Mega 2560, on a breadboard or protoboard.
3. Flash the project's firmware unchanged.
4. Before going near the Jeep, check the board's transmit and receive lines with
   the multimeter (and a scope or logic analyzer if one can be borrowed) and
   confirm it idles at the level the schematic says it should.
5. Connect ground first, then the two SCI lines, with the key off.
6. Use the project's desktop software for the reads listed above.

If no Mega is available, the newer design is ESP32-based, but it relies on
additional parts (a port expander and bus transceivers), so it is not simply
"an ESP32 and some wires".

Writing our own firmware or host software only makes sense once the project's
own tools have been seen working on this computer.

## Rules

- **Read only.** No EEPROM write command, no security unlock, no flash erase or
  programming.
- **No bootstrap mode** as a first step. It needs battery voltage on a signal
  pin and leaves the computer unable to run the engine until the key is cycled.
  Its details for JTEC are unconfirmed. The EEPROM and ROM read commands, if
  they work on JTEC as they do on SBEC, need none of that.
- **Stable supply.** A battery charger or maintainer connected throughout. A
  voltage dip during communication is exactly the kind of event that started
  this.
- **Engine off** for every read.
- **Save everything.** Raw dumps go in files named with date and time, never
  overwritten, so two reads can be compared.

## Bench option

The computer can be removed and powered on a bench, which takes the Jeep's
wiring out of the picture. That needs the battery feed, ignition feed and ground
cavities as well as the SCI cavities, all from the 2004 diagrams, and a current
limited supply. It is the right move only if the in-vehicle reads are unstable
and the wiring is suspected; otherwise it adds risk without adding information.

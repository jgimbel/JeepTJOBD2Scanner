# Is the ATOTO Bluetooth OBD2 dongle good enough?

**Short answer:** it cannot be judged from its paperwork, so the tool tests it.
I found no published chipset or protocol list for the ATOTO mini dongle. It is a
generic ELM327-compatible device, and that family ranges from full
implementations to clones that leave out the one protocol this Jeep needs.

## What the job needs from an adapter

| Need | Why | Checked by `tjdiag probe` |
|---|---|---|
| Speaks the ELM327 command set | The tool drives it with those commands | Adapter identity |
| SAE J1850 VPW | The 2004 Wrangler's diagnostic bus (connector pin 2). Many clones omit it or leave the pin unconnected | J1850 VPW |
| Shows message headers | The header carries the address of the module that answered; without it P0601 cannot be attributed to the engine or transmission computer | Module addressing |
| Prints frame checksums | Lets corrupted replies be detected | Frame checksums |
| Reads stored codes | The basic function | Read stored codes |
| Passive bus monitoring | Only for `tjdiag sniff`; optional | Bus monitoring |

## Reading the result

| Verdict | Meaning |
|---|---|
| `USABLE` | Everything the OBD2 diagnosis needs works. |
| `LIMITED` | Codes can be read and cleared; an optional feature is missing. Good enough to proceed. |
| `UNUSABLE` | Either it is not an ELM327-type device, or no module answered. |

If the J1850 VPW check fails, first confirm the key is ON and the dongle is
fully seated, and run the probe again. The tool cannot tell "ignition off" from
"adapter has no VPW"; both look like silence. If it still fails with the key on,
the adapter cannot be used on this Jeep.

One shortcut: if this dongle is what read P0601 from the Jeep in the first
place, it has already proved it can talk J1850 VPW, and the probe should pass.

## Connecting a Bluetooth dongle

The tool opens a serial port. Classic Bluetooth dongles provide one after
pairing:

- **Windows:** pair it, then look under *More Bluetooth settings → COM Ports*
  for the *Outgoing* port.
- **macOS:** pair it, then look for `/dev/cu.<name>` (`tjdiag ports` lists it).
- **Linux:** `bluetoothctl` to pair, then `sudo rfcomm bind 0 <address>` gives
  `/dev/rfcomm0`.

If the dongle is Bluetooth Low Energy only, it pairs with phones but never
offers a serial port. This version of the tool cannot open it. That would need a
BLE transport added in `tjdiag/transport.py`; the rest of the tool would not
change.

## If it is not good enough

Buy an OBDLink SX (USB, roughly $30). It implements J1850 VPW properly, appears
as an ordinary serial port with no pairing, and works with this tool unchanged:

```
tjdiag --port <port> --baud 115200 probe
```

## What no adapter of this kind can do

ELM327-type adapters speak the standard OBD2 buses only. Chrysler's SCI link,
which is how a factory tool reads the engine computer's own memory, is a
separate pair of wires with a different signalling scheme. Seeing inside the
computer needs the interface in
[sci-interface-hardware.md](sci-interface-hardware.md).

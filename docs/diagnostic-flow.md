# Diagnostic flow for P0601

Vehicle: 2004 Jeep Wrangler Unlimited, 4.0L, 42RLE automatic.
Starting point: check engine lamp on, P0601 stored, no drivability symptoms,
began after an electrical event.

P0601 means a computer's self-check of its own memory failed. Three different
situations produce it, and they need three different repairs:

| Situation | What it looks like | Repair |
|---|---|---|
| The computer's memory really is corrupt or failing | Code returns the moment the key is turned on, every time | Reflash or replace that computer |
| The computer's supply dips or its ground is poor | Code returns only after cranking or driving; voltage evidence | Battery, terminals, grounds, charging |
| A one-time upset (jump start, dead battery, disconnection) | Code does not return after clearing | Nothing, beyond fixing the cause of the event |

The flow below separates them.

## Step 0 — Which computer do you have?

Look at the engine computer on the passenger side of the firewall and count its
plugs.

- **Three plugs**: JTEC. Expected for a 2004.
- **Four plugs**: NGC. Normal for 2005–2006; a few late 2004 builds are reported
  to have it.

Phase 1 works the same either way. It matters for Phase 2.

## Step 1 — Multimeter, before any software

A weak supply can make a healthy computer fail its self-test, so rule it out
first.

| Measurement | How | Healthy |
|---|---|---|
| Resting battery | Engine off for an hour, across the posts | 12.4 V or more |
| Cranking | Across the posts while a helper cranks | Stays above 9.6 V |
| Charging | Engine at fast idle, lights and blower on | 13.5–14.7 V |
| Ground drop, engine | Negative post to engine block, engine running, loads on | Under 0.1 V |
| Ground drop, body | Negative post to body/firewall, same conditions | Under 0.1 V |
| Positive drop | Positive post to the power distribution centre feed, same conditions | Under 0.2 V |

Also look, with the battery disconnected, at both battery terminals, the
battery-to-block cable, and the engine-to-firewall ground strap for corrosion or
looseness. Given that this started with an electrical event, anything touched or
stressed during that event is the first suspect.

Fix anything out of range before going on.

## Step 2 — Check the adapter

```
tjdiag --port <port> probe
```

- `USABLE` or `LIMITED`: continue.
- `UNUSABLE`: see [adapter-evaluation.md](adapter-evaluation.md).

## Step 3 — Find out who reports it

Key ON, engine off:

```
tjdiag --port <port> snapshot --label "first read"
```

| The snapshot shows | Meaning |
|---|---|
| P0601 under Engine controller [0x10] | The engine computer set it about itself |
| P0601 under Transmission controller [0x18] | The transmission computer set it about itself |
| P0700 under the engine controller | The transmission computer has codes of its own; if it does not answer here, they need a Chrysler-capable scan tool to read |
| P0601 plus P0562, P0563, P1594, P1682 or P0622 | Voltage was out of range around the same time; supply is the prime suspect |
| P0601 plus P0602–P0606 | Several internal checks failed; points at the computer |

The freeze frame, when present, shows what the engine was doing when the lamp
was triggered (speed, load, temperature).

## Step 4 — Clear and recheck

This is the decisive test.

1. Key ON, engine off: `tjdiag --port <port> clear --yes`
2. Key off for 30 seconds.
3. Key ON, **do not start**: `tjdiag --port <port> snapshot --label "key on after clear"`
4. `tjdiag --port <port> watch` and start the engine while it logs. Let it idle a minute.
5. Engine running: `tjdiag --port <port> snapshot --label "running after clear"`
6. `tjdiag diagnose`

```
                 cleared
                    |
        back at key-on, engine off?
           /                  \
         yes                   no
          |                     |
  computer fails its      back after the engine ran?
  power-up self-test         /               \
          |                yes                no
  check its own power     /   \                |
  and grounds at the  supply    no supply   one-time event;
  connector, then     evidence  evidence    keep watching for
  Phase 2 or replace     |         |        a few days
                    fix supply  intermittent:
                    and retest  more logs, wiggle
                                test, then Phase 2
```

## Step 5 — Act on the result

**Fails at key-on.** Before buying anything, measure power and ground *at the
computer's own connectors* (pin numbers from the 2004 TJ factory wiring
diagrams): the constant battery feed, the ignition-switched feed, and every
ground pin, with the key on. A broken wire or corroded pin there looks exactly
like a dead computer from the outside. If those are good, the fault is inside
the module. Options are a reflash by a dealer or PCM specialist, or a
replacement programmed with this Jeep's VIN, mileage and, where fitted,
immobilizer data. [Phase 2](phase2-sci-interface.md) can show what in memory is
wrong first.

**Returns after cranking with supply evidence.** Battery load test, terminals,
grounds, charging system. Repeat Step 4 afterwards. If it stays away, the
computer was never at fault.

**Returns after running with no supply evidence.** Intermittent. Take more
`watch` logs across cold starts, do a wiggle test on the harness and connectors
at idle, and recheck the Step 1 numbers hot. If it keeps returning with a clean
supply, treat it as internal.

**Does not return.** Consistent with a one-time upset. Drive for several days,
take another snapshot, and make sure the cause of the original event is fixed.

## Limits of this method

- The rule "back at key-on means internal fault" assumes the computer runs its
  memory check at power-up. That is the usual design, but it has not been
  confirmed for this specific controller; if the code only ever returns after
  some driving, that alone does not prove the supply is at fault.
- The adapter's voltage reading is taken at the diagnostic connector and can
  differ from what the computer sees.
- Slow sampling can miss a brief dip.

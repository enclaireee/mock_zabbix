# Simulation configuration & modes

Everything the data-plane simulator (`make simulate` / `make backfill`) does beyond
the plain state machine lives in **one active file, `catalog/sim_config.yml`**, and
is loaded by [`otobs/sim_config.py`](../otobs/sim_config.py). This page is the full
reference: the **mode system** for picking a config, and the **six features** a config
can turn on.

For the per-parameter bands/weights/triggers themselves (a different concern), see
[sim-states.md](sim-states.md) and [band-weights.md](band-weights.md).

---

## Modes — pick a config, don't hand-edit

`catalog/sim_config.yml` is the *active* config. Rather than editing it, copy a ready-made
**preset** from [`presets/`](../presets/) over it:

The workflow is **config → (optional) backfill → simulate**:

```bash
make config                       # show the active mode + what's available
make config MODE=realistic        # activate a preset (validated against the catalog)
make check                        # confirm catalog + config are sane
make backfill                     # OPTIONAL: replay THIS mode over its past window
make simulate                     # stream live with the same mode
```

`make config MODE=x` **validates the preset against the real catalog first** (every
referenced param key / band must exist) and only then copies it to
`catalog/sim_config.yml`. A bad mode name or a broken file fails loudly and changes
nothing. With no argument it just prints status. `make config FILE=my.yml` activates
your own file.

**Backfill is always a manual step, never automatic.** No mode runs a backfill on its
own — you run `make backfill` yourself. When you do, it replays the *active* config
(continuity, correlation, trend, time-of-day all apply) over that mode's own
`backfill.days` / `speed_multiplier` window, so the history matches the live stream.
Override per run with `DAYS=` / `SPEED=`.

### The shipped modes

| Mode | For | cont. | corr. | trend | t-o-d | dropout | backfill days | `.env` note |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|---|
| **baseline** | Reference — every feature off. Byte-identical to no config file. | – | – | – | – | – | 7 | – |
| **steady** | A healthy plant on a normal day: calm, held at setpoint, few problems. | ✅ | ✅ (mild) | ✅ | ✅ (light) | – | 14 | keep high `SIM_STICKINESS` |
| **realistic** | The flagship — how an actual station behaves: setpoints, daily cycles, causal cascades. | ✅ | ✅ (web) | ✅ | ✅ | ✅ | 7 | – |
| **diurnal** | Showcase daily / shift-hour cycles tracking a moving setpoint. | ✅ | – | ✅ | ✅ (strong) | – | 14 | – |
| **stress** | Exercise monitoring: frequent problems, hard cascades, `nodata()` alerts. | ✅ | ✅ (strong) | ✅ (short) | – | ✅ (heavy) | 2 | lower `SIM_STICKINESS` ≈0.80 |
| **maintenance** | Sensors/links in and out of service — lots of `nodata()` gaps, plant stays healthy. | ✅ | – | ✅ | – | ✅ (uneven) | 3 | – |
| **demo** | Punchy 5-minute live walkthrough: fast, obvious cascades. | ✅ | ✅ (strong) | ✅ (short) | – | ✅ (light) | 1 | lower `SIM_STICKINESS` ≈0.85 |
| **ml** | Training data for Tahap 2/3 (clustering, RUL): long smooth labelled curves. | ✅ | ✅ (web) | ✅ (long) | – | – | 30 | raise `SIM_STICKINESS` ≈0.97 |

**Custom modes.** Copy any preset in [`presets/`](../presets/), edit, and point at it
with `make config FILE=…`, or just edit `catalog/sim_config.yml` directly. `make check`
validates whatever is active.

> Two knobs stay in `.env`, not here, because they're global scalars: `SIM_STICKINESS`
> (how long a stream stays in a band) and `SIM_TIME_SCALE` (live clock compression).
> The mode table notes where a mode wants a particular stickiness.

### Why `realistic` looks like a real plant

Three properties of real gas-transmission telemetry drive the `realistic` mode:

1. **Process variables are closed-loop controlled.** Pressure, flow and temperature sit
   on PID loops, so a healthy reading *hovers around its setpoint* with small, roughly
   proportional noise — it does not wander across its whole operating range. Modeled by
   `continuity.reversion` (mean-reversion to the band centre) with `step_scale` noise.
2. **Throughput is diurnal.** Gas demand tracks the electricity load it feeds — lowest
   overnight (~05:00), rising through the working day. Modeled by `time_of_day` on flow,
   compressor speed and operator load, which shifts the controlled setpoint.
3. **Faults are causal, not independent.** A real failure drags the physically-connected
   measurements with it: losing lube-oil pressure overheats the bearing, which shakes the
   rotor; a blocked filter chokes flow; a stalled fan cooks the CPU. Modeled by the
   per-host `correlation` web.

Transitions between health bands then `trend`-ramp rather than step, and the odd reading
is `dropout`-dropped so `nodata()` fires. The result is a stream that drifts, cycles,
cascades and occasionally goes quiet — like a plant, not a random number generator.

---

## The six features

Every feature defaults **off** and is a strict no-op when disabled. With all off (or
the file absent) the simulator is exactly the sticky state machine of the baseline —
`test_sim.py` asserts this byte-for-byte against a seeded RNG.

### 1. `continuity` — values move, they don't teleport

The baseline sampler draws a **fresh** `uniform(lo, hi)` inside the current band *every
tick*. That means a 28.0 barg inlet pressure can read 33.9 barg on the very next 5 s
scan, and a fault count bounces across its whole range while "stuck". Real signals don't
do that — pressure drifts, a reallocated-sector count steps on an event then holds.

`continuity` fixes it. While a stream stays inside its current band, the next value
steps from the last reading instead of being redrawn. Two behaviors, by parameter type:

- **Analog, controlled signals** (`jitter > 0` — pressure, temp, flow, vibration) are
  modeled as a real **PID control loop**: each tick the value is pulled a fraction
  `reversion` back toward its **setpoint** (the band centre), plus proportional noise
  `gauss(0, jitter × step_scale)`. So it *hovers around setpoint* like a real regulated
  process variable, rather than random-walking to a band edge. (`reversion = 0` gives a
  pure random walk; ~0.1 is a firmly-held loop.)
- **Counters / discrete gauges** (`jitter = 0` — fault counts, SMART reallocated sectors,
  NIC errors) **hold still** once in a band, stepping only on an actual state change —
  exactly how a counter behaves.
- On a state **transition** the value lands in the new band via a fresh draw (or a
  `trend` ramp, if enabled); the controlled walk resumes from there on the next tick.

```yaml
continuity:
  enabled: true
  step_scale: 0.8     # per-tick noise = jitter × this; <1 = calmer, >1 = jumpier
  reversion: 0.12     # 0 = free walk; higher = held tighter to setpoint (PID stiffness)
```

`step_scale` and `reversion` are the two calibration knobs: `jitter` was tuned as
one-shot sensor noise, so you tune the per-tick step and the control stiffness per mode
without touching every catalog band. The setpoint a signal reverts to is shifted by any
`time_of_day` multiplier (below), so a controlled value tracks the daily demand curve.

### 2. `correlation` — causal, per-host coupling

On a tick where a *trigger* param is currently in `trigger.band`, an *affected* param's
next state is forced toward `bias_band` with probability `strength`, overriding its own
weights **and** stickiness. Only couples streams on the **same host**, and reads the
trigger's *current* (pre-roll) band, so it's causal and order-independent.

```yaml
correlation:
  enabled: true
  groups:
    - name: "thermal_cascade"                     # a stalled fan drives CPU temp up
      trigger: { param: "hmi.fan.rpm", band: "failed" }
      affects:
        - { param: "hmi.cpu.temp", bias_band: "underperform", strength: 0.7 }
```

Physically-real chains shipped in the presets: `thermal_cascade` (fan → CPU temp),
`lube_starvation` (lube-oil pressure → bearing temp → vibration), `filter_choke`
(filter dP → flow), `switch_thermal` (switch fan → CRC errors).

### 3. `trend` — transitions ramp, they don't step

On a state change, ramp from the last emitted value toward a fresh target inside the new
band over `ramp_seconds` (÷ `SIM_TIME_SCALE`, like intervals), instead of jumping. After
the ramp completes, `continuity` (if on) takes over the steady-state walk.

```yaml
trend:
  enabled: true
  ramp_seconds: 1800
  overrides:
    hmi.cpu.temp: { ramp_seconds: 3600 }   # thermal mass ramps slower
```

### 4. `time_of_day` — daily operational cycles

Real gas throughput is **diurnal** — it follows demand, lowest overnight (~05:00) and
higher through the working day. This scales a value by a peak/off-peak factor by local
hour (`settings.TIMEZONE`); `peak_hours` wraps past midnight if `start > end`. With
`continuity` on, the multiplier shifts the **setpoint** the value is held to (so it
tracks the cycle smoothly); with continuity off, it scales the raw sample directly.
Best applied to demand-linked params (flow, compressor speed/load, operator CPU).

```yaml
time_of_day:
  enabled: true
  profiles:
    - param: "hmi.cpu.util"
      peak_hours: [8, 17]
      peak_multiplier: 1.4
      off_peak_multiplier: 0.6
```

### 5. `dropout` — real gaps for `nodata()`

Occasionally skip a due send: the state freezes, nothing is emitted, but `next_due`
still advances — so a genuine one-interval gap forms and `nodata()` triggers fire. A
drop is a missed reading, not a retry.

```yaml
dropout:
  enabled: true
  probability: 0.02
  overrides:
    hmi.smart.health_passed: { probability: 0.0 }   # never drop the safety verdict
```

### 6. `backfill` — history with correct timestamps

`make backfill` sweeps the same machine over `[now − days, now]` as discrete events at
each param's **real** interval, stamping every value with its historical `clock`.
`speed_multiplier` only controls how fast it's generated, not the timestamps. It replays
**the whole active config** — the backfilled history has the same continuity, cascades,
ramps and daily cycles as the live stream, so graphs are consistent across the join.

```yaml
backfill:
  enabled: false               # informational only — backfill is always run manually
  days: 14                     # this mode's default window
  speed_multiplier: 500        # override per run with DAYS= / SPEED=
```

`enabled` is **not** an auto-trigger (nothing runs a backfill for you); it just records
whether a mode is *meant* to be backfilled. `days` / `speed_multiplier` are the defaults
`make backfill` uses for that mode.

---

## Interactions worth knowing

- **continuity + trend compose.** During a ramp, the walk is suspended; when the ramp
  ends the value is in the new band and the walk resumes. You get a smooth ramp *into* a
  degraded band, then realistic drift *within* it.
- **continuity + time_of_day compose** (for `jitter > 0` params). The daily multiplier
  shifts the *setpoint* the controlled walk reverts to, so the value tracks the demand
  curve without the per-tick compounding a naïve multiply would cause. On `jitter = 0`
  counters, `time_of_day` has no effect (a counter has no setpoint).
- **correlation overrides stickiness** on a forced tick — deliberately, so the coupled
  effect is actually visible rather than being suppressed by a high `SIM_STICKINESS`.

## Validation

`make check` (and `make config`) load the active config and assert every referenced
param key and band exists in the catalog, and every number is in range (negative
probability, zero ramp, hour > 24 all fail loudly) — **before** any data is sent. The
shipped presets are additionally checked in `test_sim.py`
(`test_presets_validate_against_catalog`), so a typo in a mode file is caught in the
test suite, not at run time.

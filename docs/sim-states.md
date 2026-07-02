# Simulator states

A `Sim` has a `kind` and a list of `State` objects
([`otobs/catalog.py`](../otobs/catalog.py)).

## Sim kind

- `numeric` — readings are drawn from a numeric band (`lo`/`hi` with `jitter`).
- `enum` — readings are a fixed discrete value per state.

## State fields

| Field    | Meaning                                                            |
|----------|-------------------------------------------------------------------|
| `weight` | selection weight (see [band weights](band-weights.md))            |
| `band`   | `good` / `underperform` / `failed`, or `custom` for display-only  |
| `value`  | enum states only: the fixed value (int / float / str)            |
| `lo`,`hi`| numeric states only: inclusive band bounds                        |
| `jitter` | numeric states only: Gaussian noise added on top of the band     |

The `band` drives display and which triggers are expected to fire; `custom` is
used for enum states whose weight was given as a number rather than a band token.

## Realism layer — `catalog/sim_config.yml`

By default each `(host, parameter)` stream is an independent sticky state machine
that samples uniformly (plus jitter) inside its current band. `sim_config.yml`
([`otobs/sim_config.py`](../otobs/sim_config.py)) layers optional, per-feature
behavior on top. **Every feature defaults off**; with the file absent or all
`enabled: false`, the output is byte-for-byte the plain state machine.

| Feature | Changes | How |
|---|---|---|
| `correlation` | **state selection** | On a tick where a trigger param is in `trigger.band`, an affected param's next state is forced toward `bias_band` (prob. `strength`) instead of using its own weights. Per host; composable across groups. |
| `trend` | **value sampling** | On a state transition, ramp from the last emitted value toward a fresh target inside the new band over `ramp_seconds` (÷ `SIM_TIME_SCALE`), with jitter — no band clamp during the ramp. |
| `time_of_day` | **value sampling** | Multiply the sampled value by a peak/off-peak factor by local hour before jitter. |
| `dropout` | **emission** | Skip a due send entirely (state frozen, `next_due` still advances) so a real gap forms. |
| `backfill` | **timing** | Run the same machine over a past window, stamping each value with its historical `clock`. |

To support trends, a `Stream` also carries `last_value` and the current ramp
(`ramp_from` / `ramp_to` / `ramp_start`); these are inert when `trend` is off.

## `sim_config.yml` schema (annotated)

```yaml
correlation:
  enabled: false
  groups:
    - name: "thermal_cascade"
      trigger: { param: "hmi.fan.rpm", band: "failed" }   # the cause
      affects:
        - { param: "hmi.cpu.temp", bias_band: "underperform", strength: 0.7 }
      # strength = P(force cpu.temp toward underperform on a tick where fan.rpm is
      # currently 'failed'), instead of its own weights. Composable: a param can be
      # an affects-target of several groups.

trend:
  enabled: false
  ramp_seconds: 1800          # global ramp length (÷ SIM_TIME_SCALE, like intervals)
  overrides:
    hmi.cpu.temp: { ramp_seconds: 3600 }   # slower ramp for this one

time_of_day:
  enabled: false
  profiles:
    - param: "hmi.cpu.util"
      peak_hours: [8, 17]     # local hours (settings.TIMEZONE); wraps if start > end
      peak_multiplier: 1.4
      off_peak_multiplier: 0.6

dropout:
  enabled: false
  probability: 0.02           # per-stream, per-due-tick chance of skipping the send
  overrides:
    hmi.nic.errors: { probability: 0.0 }   # never drop this one

backfill:
  enabled: false
  days: 14
  speed_multiplier: 500       # how much faster than real time to generate
```

## Semantics worth knowing

- **Correlation is causal and per-host.** It reads each trigger param's *current*
  (pre-roll) band, so ordering within a tick doesn't matter, and it only couples
  streams on the same host. A forced roll overrides both weights *and* stickiness
  — deliberately, so the effect is visible.
- **Trend and time-of-day skip the band clamp.** The baseline clamps a sample into
  `[lo, hi] ± jitter`. A ramp deliberately traverses *between* bands, and a
  time-of-day multiplier deliberately *shifts* the value — clamping would erase
  both, so those paths don't clamp.
- **A dropout is a missed reading, not a retry.** On a drop the state is frozen and
  nothing is emitted, but `next_due` still advances normally — so a genuine
  one-interval gap forms (which is what `nodata()` needs), rather than an immediate
  re-send.
- **Backfill uses real intervals.** Live `simulate` compresses time by
  `SIM_TIME_SCALE`; backfill does not — it steps virtual time by each parameter's
  true interval so the historical `clock` spacing is physically correct.

## Validation

`make check` loads `sim_config.yml` and asserts every referenced param key and
band actually exists in the catalog, and every number is in range (e.g. negative
probability, zero ramp, hour > 24 all fail loudly) — **before** any data is sent.
`make list` and `make check` both print which features are enabled. See
[RUNNING.md](../RUNNING.md) for the commands.

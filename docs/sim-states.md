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

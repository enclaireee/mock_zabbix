# Weather engine — `otobs/weather_engine.py`

The `omega` sim mode ([sim-config.md](sim-config.md#the-shipped-modes)) adds a
regional weather feed (`catalog/bmkg.yml`) and correlates it into the other
five asset classes. This page covers the engine itself: the physics model,
why it has to be deterministic, and where it plugs into `simulate.py`.

## Why deterministic, not `random.*`

Every other stream in this catalog is a **probabilistic** state machine
(sticky Markov chain + `sim_config` realism layer). Weather is different: it's
computed once, in closed form, from a Unix timestamp — `WeatherNode.get_weather`
takes no other input and holds no state. Two reasons this matters more than it
would for an ordinary parameter:

1. **`make backfill` must be re-runnable.** A stateful accumulator (e.g. "dust
   += 1 every dry hour, reset by rain") would make the value at a given
   historical timestamp depend on call order/frequency — different on every
   backfill run, and different between the live loop and a backfill sweep
   covering the same instant. A pure function of the timestamp can't drift.
2. **Live and backfilled weather must agree at the join.** Since both paths
   call the same `get_weather(timestamp)`, the history `make backfill` writes
   and the live stream `make simulate` continues with are the same curve,
   not two independent random processes that happen to be adjacent in time.

## The model (tuned for a tropical two-season climate — South Sumatra / West Java)

| Field | Model | Range |
|---|---|---|
| `temp` | Seasonal cosine (dry-season peak ~mid-Aug) × an asymmetric double-cosine diurnal curve — fast ~9h rise to a 14:00 max, slower ~15h cool to a 05:00 min. A lightweight stand-in for the Parton & Logan sunrise/sunset solar model, which needs latitude + day-length this generator doesn't carry. | 25–35°C |
| `humidity` | Deterministic inverse of `temp` (absolute humidity ~stable through the day, so RH falls as temp rises). | 50–95% |
| `rain_intensity` | Convective-afternoon bell curve (peak ~17:00, ±2h) scaled by how deep into the wet season it is; zero in the dry season. | 0–45 mm/h |
| `lightning_event` | 0/1, gated by `rain_intensity` crossing a storm threshold, then a per-minute pure-arithmetic hash of the timestamp (Knuth multiplicative hash — **not** Python's built-in `hash()`, which is per-process salted and would make the same historical minute strike on one run and not the next). | 0 or 1 |
| `dust_index` | A true running integral (dry hours since the last rain-reset), but re-derived on demand by walking backward from the query timestamp rather than kept as mutable state — for the same backfill-determinism reason as above. Bounded to a 7-day lookback + a hard cap of 100; a shorter lookback (originally 48h) left the top of the range unreachable under realistic conditions ([`tests/test_weather.py`](../tests/test_weather.py) pins this). | 0–100 (index) |

Every field, band, and value range is declared in
[`catalog/bmkg.yml`](../catalog/bmkg.yml) exactly like any other parameter —
weather streams still have `sim: { good: [...], underperform: [...], failed: [...] }`
bands so triggers/severity and `correlation` band-matching work normally.
`weight`/`jitter` in that file are inert for these five keys specifically (see
next section) but kept so the file matches the standard catalog schema.

## How `process_stream` routes around the state machine

`otobs/simulate.py` intercepts `bmkg.*` keys at the top of `process_stream`,
before dropout/hold/trend/`next_state` ever run:

```
field = _WEATHER_FIELDS.get(s.param.key)      # "bmkg.temp" -> "temp", etc.
if field is not None:
    raw = WEATHER.get_weather(clock or now)[field]
    s.state_idx = _band_idx_for_value(s.param.sim, raw)   # so correlation can read the band
    ...
    return value                               # never reaches next_state/sample_stream
```

`s.state_idx` is still set (via `_band_idx_for_value`, the inverse of the
usual `_idx_of_band`) so `correlation_forces` can read a `bmkg.*` stream's
current band exactly like any self-rolling stream's.

### `clock` vs `now` — a real pitfall, not a style choice

`process_stream`'s existing `now` parameter is a **scheduling** clock, not a
calendar timestamp — `time.monotonic()` in the live loop (an arbitrary epoch,
unrelated to wall-clock time) vs. real historical Unix time in
`run_backfill()`. Feeding the live loop's `now` straight into `get_weather()`
would silently produce nonsense seasons/hours (a monotonic clock has no
relationship to "what month is it"). `process_stream` therefore takes a
separate `clock: float | None = None` parameter — the caller's actual
wall-clock instant — that only weather streams read:

- `run()`: computes `clock = time.time()` once per tick (shared with `hour`).
- `run_backfill()`: passes `vt` (already real historical Unix time).
- Every other caller (tests, non-weather streams) can omit it — the code
  falls back to `now`, which is only ever read if a `bmkg.*` stream happens
  to be passed a monotonic `now` with no `clock`, i.e. never in production.

## Cross-host correlation

Weather lives on **one** shared host (`BMKG-STATION`), not per-site like
every other asset class — but `correlation_forces` is otherwise strictly
per-host (`realistic.yml`'s own comment: *"trigger and affected params live
on the same host"*). `bmkg.*`-prefixed `trigger_param`s are the one
special-cased exception, resolved against a small global index instead of the
current host's stream dict — see [sim-config.md](sim-config.md#2-correlation--causal-per-host-coupling)
for the detail and the regression test that pins the same-host behavior
didn't weaken for every other trigger.

## Why some correlations in `presets/omega.yml` don't map onto literal "HVAC"/"solar" keys

This catalog is a gas-transmission SCADA site, not an office/datacenter — there's
no HVAC or solar-power asset class to correlate against. `presets/omega.yml`'s
header comment documents the substitutions in full (ambient heat →
compressor cooling stress, dust → gas-detector dropout + CPU thermal load,
lightning → a temporary `net.if.error_rate` blip, never a link-down event).
"Solar & rain" has no real equivalent at all in this catalog and was dropped
rather than invented — a fabricated `power.solar.output` key would fail
`make config`'s own catalog validation and wouldn't correspond to anything
actually provisioned in Zabbix. Add a real solar/power `catalog/*.yml` first
if that correlation is wanted.

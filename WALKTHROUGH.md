# Project Walkthrough — OT Observability Lab

A single guide to **what** this repo is, **why** each piece exists, **how** the
code works, and **how** to demo and defend it. Read top to bottom and you can
present it — and answer hard questions — as if you built it.

**How this doc is organized**

| Part | Sections | For |
|------|----------|-----|
| **The story** | 1–5 | The pitch, the domain, the health model, the realism layer |
| **The engineering** | 6–9 | Zabbix concepts, both schemas, the code tour |
| **Using it** | 10–13 | Data lifecycle, demo script, Q&A, the one-slide summary |

---

## 1. The one-sentence pitch

> "A self-contained lab that runs a **real Zabbix 7.0 monitoring stack** in Docker
> and feeds it **simulated OT/IT telemetry** for gas-transmission station gear — so
> we can watch real monitoring triggers fire across Good → Underperform → Failed
> health states with no physical hardware."

**Problem it solves:** we need to prove out a monitoring design for PGN's gas
stations (PLCs, HMIs, switches, the SCADA process) *before* there's a real plant
to plug into. This lab builds the **exact Zabbix config** production would use,
then mocks the data feed. Swapping a mock for a real collector later is a one-line
change, because the config plane is identical.

---

## 2. The core idea — one source of truth

Everything hangs off a single principle:

```
                     catalog/*.yml   (defines every parameter ONCE)
                       /                          \
              otobs.provision                 otobs.simulate
       (builds Zabbix config:               (pushes mock data:
        templates, items, triggers,          Good/Underperform/Failed
        hosts — over the JSON-RPC API)        streams — over Trapper)
```

The YAML in `catalog/` describes each monitored parameter once. Two programs read
those same files:

- **`provision`** turns the catalog into Zabbix *configuration* (what to monitor,
  what to alert on).
- **`simulate`** turns the catalog into *fake data* and streams it in.

Add a parameter in YAML → both the monitoring config **and** the data generator
pick it up. There's exactly one definition of each metric, holding both how to
*monitor* it and how to *fake* it.

**Why it matters:** a normal Zabbix setup means clicking through the UI to create
41 items × 12 hosts, then separately writing a data feed — and the two drift.
Here they can't: they're generated from the same four files. **Lead with this
when you present.**

---

## 3. The domain — what we monitor

This models a PGN gas-transmission station: four **asset classes**, each its own
catalog file, **41 parameters**, across **3 stations** (Grissik, Tebanggi Besar,
Bojonegara — defined in `catalog/sites.yml`).

| Asset class | File | Host group | Params | What it is |
|---|---|---|---|---|
| Gas Process (SCADA) | `gas_process.yml` | `OT/Process` | 20 | The physical process: pressures, flow, gas quality, valves, ESD, fire & gas, compressor health |
| PLC Siemens S7-400 | `plc_s7400.yml` | `OT/PLC` | 6 | The controller: CPU mode, diag buffer, I/O channel, rack, CP reachability |
| Workstation / HMI | `workstation_hmi.yml` | `IT/HMI` | 10 | The operator PC: CPU/RAM/disk/SMART/NIC, fan RPM, PSU rail, CPU temp |
| Switch / Router | `switch_router.yml` | `Network/Industrial` | 5 | Industrial network: interface status, errors, discards, fan |

Each of the 3 sites gets one host per asset class → **12 hosts**, all plotted on
an Indonesia geomap and streaming live.

### The "technical honesty" angle

Every parameter records **how it would really be collected** in production, and
admits what Zabbix *can't* see natively (the `collection:` field, also embedded
into the live item description):

- **Switch/router** — all native SNMP (IF-MIB, CISCO-ENVMON-MIB). Trivial.
- **HMI** — CPU/RAM/disk/SMART/NIC via **Zabbix Agent 2**; fan RPM, PSU rails,
  CPU temp need middleware (LibreHardwareMonitor over WMI, or iDRAC/iLO/IPMI).
- **PLC S7-400** — the CP443-1 comms module exposes SNMP but **can't see CPU
  memory, the diagnostic buffer, or per-channel I/O**; those need **S7comm**
  (RFC 1006, TCP 102) reading SZL lists. So `plc.cp.icmp_latency` is the only
  natively-collectable PLC metric; the rest are *needs middleware*.
- **Gas process** — all SCADA tags need an S7comm → Node-RED → Zabbix bridge.

In the lab, **every value arrives via Zabbix Trapper** (push). That's deliberate:
the production Node-RED bridge pushes over the same `zabbix_sender` protocol, so
one simulator stands in for Node-RED, Agent 2, **and** SNMP pollers at once. Going
to production only changes the **item type** on the template (Trapper → Agent/
SNMP); keys, triggers, descriptions, dashboards stay put.

---

## 4. The health model — Good / Underperform / Failed

Every parameter is a **sticky state machine** over three bands:

- **Good** — within spec, no action.
- **Underperform** — wear / partial / transient degradation, still operating.
  **The high-value signal:** these smooth degradation curves are the training data
  for later phases (clustering, predictive maintenance / RUL).
- **Failed** — primary function lost; high-severity triggers fire.

"Sticky" means: each tick, with probability `SIM_STICKINESS` the parameter
**stays**; otherwise it re-rolls by the catalog weights (defaults 90/8/2). That
turns independent coin-flips into **long, smooth runs** — realistic degradation,
not per-tick noise. (Formally: a Markov chain whose self-transition probability is
pinned to `STICKINESS`, off-diagonal mass split by the steady-state weights.)

Two **global** knobs, in `.env`:

- `SIM_STICKINESS` (0–1) — higher = longer dwell = smoother curves. Default
  **0.92** (high on purpose: smooth runs are the ML signal).
- `SIM_TIME_SCALE` — time compression. `1.0` = real intervals; ships **10.0**, so
  a `1h` SMART metric updates every ~6 min. (`run()` floors it at `0.001`.)

That's the baseline. On its own it has four gaps: parameters never influence each
other, transitions are instant steps, values ignore time of day, and no reading is
ever missed. Section 5 closes them.

---

## 5. The realism layer — `catalog/sim_config.yml`

An **optional** layer on top of the baseline, in its own file (kept out of `.env`
because these are structured, per-parameter knobs, not global scalars). Five
features, **each with its own `enabled` flag and every one off by default.**

> **The invariant:** with `sim_config.yml` absent, or every feature `enabled:
> false`, the simulator's output is **byte-for-byte identical** to the plain state
> machine of §4. This is an additive layer, not a rewrite. (`test_sim.py` asserts
> exactly this against a seeded RNG.)

| Feature | The realism gap it closes | What it does |
|---|---|---|
| `correlation` | Params were fully independent | Per host, when a *trigger* param is in a given band, it biases *affected* params' next state toward degrading — a stalled fan drives CPU temp up. |
| `trend` | Transitions were instant steps | On a state change, ramps from the last value toward a target inside the new band over `ramp_seconds`, jitter on top — a curve, not a jump. |
| `time_of_day` | Values ignored the clock | Scales the value by a peak/off-peak multiplier by local hour (shift-hour load). |
| `dropout` | Data never went missing | Occasionally skips a due send, leaving a real gap so Zabbix `nodata()` triggers finally fire. |
| `backfill` | History only existed going forward | `make backfill` sweeps the machine over a past window and pushes each value with its historical timestamp. |

### The schema (annotated)

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

### Semantics worth knowing (they come up in Q&A)

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

### Validation

`make check` loads `sim_config.yml` and asserts every referenced param key and
band actually exists in the catalog, and every number is in range (e.g. negative
probability, zero ramp, hour > 24 all fail loudly) — **before** any data is sent.
`make list` and `make check` both print which features are enabled.

---

## 6. Architecture — the moving parts

```
                     catalog/*.yml  (single source of truth)
                    /                              \
          otobs.provision                       otobs.simulate  ── sim_config.yml
        (Zabbix JSON-RPC API)                 (Trapper / zabbix_sender)   (realism)
               |                                       |
               v                                       v
   ┌───────────────────────────────────────────────────────────────┐
   │  Zabbix 7.0 stack (docker compose)                             │
   │   zabbix-web    (nginx, :8080)   ── API + UI                   │
   │   zabbix-server (:10051 trapper) ── triggers, history          │
   │   zabbix-db     (postgres 16)    ── config + history store     │
   │   zabbix-agent2                  ── monitors the lab host       │
   └───────────────────────────────────────────────────────────────┘
```

**Two planes** — keep them straight and the system explains itself:

- **Config plane** — *what to monitor and when to alert.* Built by `provision`
  (host groups, templates, items, triggers, hosts), once and idempotently.
- **Data plane** — *the measured values over time.* Fed by `simulate` /
  `backfill`. **`sim_config.yml` only touches the data plane** — the config plane,
  and therefore the production swap-in story, is untouched.

The Docker stack is **real Zabbix**, not a mock — that's the point.
`docker-compose.yml` runs four official 7.0 images plus Postgres 16, all wired
through `.env`, with a DB healthcheck so the server waits for the DB. A named
volume `zbx_db_data` means `make down` keeps data; only `make clean` (`down -v`)
wipes it.

---

## 7. Zabbix concepts you must be able to explain

- **Host** — a monitored thing. Has a *technical name* (`host`, e.g.
  `PLC-S7400-GRS`, immutable identity — the simulator addresses values by this)
  and a *visible name* (`name`).
- **Host group** — a folder of hosts (`OT/PLC`…); drives UI filtering and the
  scope `prune()` operates within.
- **Item** — one metric on a host. Has a **key**, a **value type**
  (float/unsigned/text/char/log), units, and a **type** (here **2 = trapper**,
  push). The key is shared between the item and the trapper payload — the glue.
- **Template** — a reusable bundle of items + triggers, linked to many hosts.
  41 items defined once, not 41 × 12. Config-as-code vs. click-ops.
- **Trigger** — a boolean over item history that raises a "problem", e.g.
  `last(/Template OT PLC S7-400/plc.cpu.operating_mode)>=13`, with a severity.
  `nodata()` triggers (which the `dropout` feature finally exercises) fire on the
  *absence* of data.
- **Trapper** — a push item type. A sender connects to server :10051 and pushes
  `(host, key, value[, clock])`. The server accepts a value only if a trapper item
  with that key exists on that host — which is why **you `provision` before
  `simulate`**. The optional `clock` is what makes **backfill** possible.
- **Inventory** — per-host metadata (`location`, `location_lat/lon`…). The
  **Geomap** widget reads these to plot pins.
- **Macros** — `{$NAME}` variables on hosts/templates (`{$SITE}`, `{$RACK}`), set
  per host so one template carries site-specific values.
- **SZL** (Zustandsliste) — Siemens S7 diagnostic structures read over S7comm;
  named in the PLC `collection` fields to justify "needs middleware". Documented,
  not implemented.

---

## 8. The schemas

### 8.1 Station registry — `sites.yml`

Physical stations defined **once**; each asset class generates one host per site.
Required per site: `code`, `name`, `lat`, `lon`, `location` (validated); plus
free-form fields referenced by host templates.

```yaml
sites:
  - { code: GRS, name: "Grissik", location: "Grissik Gas Plant, ...",
      lat: "-2.0500", lon: "103.4300", city: "Musi Banyuasin",
      grade: enterprise, p_out_sp: "16" }
```

`code` → host-name token; `location`/`lat`/`lon`/`city` → host **inventory** (and
the geomap pin); `grade`/`p_out_sp`/anything-else → available to `host_template`
macros. **Adding a station is one line here.**

### 8.2 Asset-class file — top level + parameter

```yaml
asset_class: "PLC Siemens S7-400"       # human label
host_group:  "OT/PLC"                    # Zabbix host group
template_name: "Template OT PLC S7-400"  # template items/triggers live on
template_group: "Templates/OT"
host_template:                           # generate one host per site...
  tech:   "PLC-S7400-{code}"             #   technical name; {field} from the site
  name:   "{name} — PLC S7-400H"
  macros: { "{$SITE}": "{code}", "{$RACK}": "0" }
parameters:
  - key: "plc.cpu.operating_mode"        # Zabbix item key AND trapper key — the glue
    name: "CPU Operating Mode"
    value_type: unsigned                 # float | unsigned | text | char | log
    units: ""
    interval: "15s"                      # 15s | 30s | 1m | 5m | 1h ...
    component: "C. Modules (CPU)"        # FMEA subsystem (baked into the item description)
    collection: "S7comm via Node-RED (SZL 0x0424, byte 11)"   # real-world method
    failure_mode: "Hardware defect, watchdog timeout, ..."
    source: "Siemens OPC SZL Diagnostics, 2024"
    sim:      { ... }                    # how the simulator generates values
    triggers: [ ... ]                    # Good/Underperform/Failed alerting
```

`component`, `collection`, `failure_mode`, `interval`, `source` are **baked into
the Zabbix item description** (`Parameter.description()`) — the running tool
documents itself. Instead of `host_template` you may hand-list `hosts:` for
one-off hosts; one of the two is required.

### 8.3 `sim` — two kinds

**numeric** — three bands; the baseline samples uniformly in the current band plus
jitter:

```yaml
sim:
  kind: numeric
  good: [40, 60]
  underperform: [66, 84]
  failed: [86, 99]
  weights: [good, underperform, failed]   # tokens → default probs, or raw numbers
  jitter: 1.5
```

**enum** — discrete states, each a fixed value with a band weight:

```yaml
sim:
  kind: enum
  states:
    - { value: 8,  weight: good,         label: "RUN (0x08)" }
    - { value: 6,  weight: underperform, label: "START-UP (0x06)" }
    - { value: 13, weight: failed,       label: "DEFECT (0x0D)" }
```

`weight` is a number or one of `good`/`underperform`/`failed` (0.90/0.08/0.02),
normalized per parameter. **This block is unchanged by the realism layer** —
`sim_config.yml` is orthogonal to it (§5).

### 8.4 `triggers`

```yaml
triggers:
  - { op: "=",  value: 6,  severity: warning, label: "CPU not in RUN" }
  - { op: ">=", value: 13, severity: high,    label: "CPU STOP/DEFECT" }
```

`op` ∈ `= <> > >= < <=`; `severity` ∈ `info|warning|average|high|disaster`; `func`
optional (default `last`). Expression: `func(/<template_name>/<key>) <op> <value>`.

---

## 9. The code — function by function

Python in `otobs/`, standard library plus two deps (`zabbix_utils`, `PyYAML`) and
stdlib `zoneinfo` for the time-of-day clock. No web framework, no ORM.

### 9.1 `settings.py` — config loader

Hand-rolled `.env` parser (`_load_env`) using `os.environ.setdefault` so **real
env vars win over `.env`**; `_f()` gives float-with-fallback for the sim knobs.
Exposes `API_*`, `SENDER_*`, `STICKINESS`, `TIME_SCALE`, and now **`TIMEZONE`**
(reuses `ZBX_TIMEZONE`, used by the time-of-day feature).

### 9.2 `catalog.py` — load + validate (the heart)

Lookup tables map human strings → Zabbix codes (`VALUE_TYPE_CODE`,
`SEVERITY_CODE`, `DEFAULT_WEIGHTS`). Dataclasses (`State`, `Trigger`, `Sim`,
`Parameter`, `Host`, `AssetClass`) are the typed model; builders (`_build_sim`,
`_build_param`, `_expand_hosts`, `load_file`) **validate at construction**, so bad
YAML fails before it reaches the API. `load_all()` loads `sites.yml`, then globs
every other `*.yml` **except `sites.yml` and `sim_config.yml`** (those load
separately) — the single call both `provision` and `simulate` consume.

### 9.3 `sim_config.py` — the realism loader (new)

Mirrors `catalog.py`'s style: typed dataclasses (`Correlation`, `Trend`,
`TimeOfDay`/`TodProfile`, `Dropout`, `Backfill`, wrapped in `SimConfig`) with
small behavior methods (`Trend.ramp_for`, `TodProfile.multiplier`,
`Dropout.prob_for`, `SimConfig.enabled_features`).

- `load_sim_config(dir=None)` — parses the file into those objects; **returns an
  all-off `SimConfig()` if the file is absent.** Range checks (`_num`) run at load.
- `validate(cfg, param_bands)` — given `{key: {bands}}` from the catalog, asserts
  every referenced param/band exists. Called by `cmd_check`.

Every dataclass defaults to disabled, and the accessor methods short-circuit when
disabled (e.g. `Dropout.prob_for` returns `0.0`), which is what preserves the §5
invariant *and* the RNG-draw order behind it.

### 9.4 `provision.py` — config-as-code (unchanged)

A `Provisioner` class, fully **idempotent** via get-or-create: `_templategroup` /
`_hostgroup` / `_template` / `_item` / `_triggers` / `_host` each fetch-or-create;
`apply()` fetches existing items/triggers/hosts **once per template** (avoids the
N+1 explosion); `prune()` deletes catalog-managed hosts no longer present, scoped
to the catalog's own host groups so it never touches unrelated hosts.
**The realism layer does not touch this file** — it's a data-plane change.

### 9.5 `simulate.py` — the mock plant

The value + state primitives:

- `sample(sim, state, value_type)` — the **baseline** sampler: enum → fixed value;
  numeric → `uniform(lo,hi) + gauss(0,jitter)`, clamped to `[lo,hi]±jitter`, typed
  (`int` for unsigned). Unchanged — it's the fall-back path.
- `next_state(sim, cur, stickiness, forced_idx=None)` — sticky transition; if
  `forced_idx` is given (correlation), it returns that directly, overriding both
  stickiness and weights.
- `sample_stream(s, st, now, scale, cfg, hour)` — the realism-aware sampler: if
  neither a trend ramp nor a time-of-day multiplier applies it **returns `sample()`
  unchanged** (identical draws); otherwise it interpolates the ramp and/or applies
  the multiplier, **without** the band clamp. `correlation_forces(cfg, by_host)`
  computes `{(host, key): bias_band}` from each trigger param's current band.
- `Stream` — per (host, parameter); now also carries `last_value` and the ramp
  (`ramp_from`/`ramp_to`/`ramp_start`), inert unless `trend` is on.

The shared per-tick body:

- `process_stream(s, now, scale, cfg, forced, hour)` — one place that advances a
  due stream: roll dropout (→ `None` = drop), resolve any correlation force, roll
  `next_state`, arm a ramp on transition, sample via `sample_stream`. **Both the
  live loop and backfill call it**, so they can't diverge.

The two drivers:

- `run(assets, cfg=None)` — the live loop: every 0.5 s, compute this tick's
  correlation forces and local hour once, then for each due stream advance
  `next_due = now + interval/scale` and `process_stream` it; batch the emitted
  `ItemValue`s into **one** `sender.send()`; print a heartbeat (processed/failed +
  any non-Good readings). Send errors are caught, not fatal.
- `run_backfill(assets, cfg=None, days=None, speed=None)` — a **discrete-event
  sweep**: seed all streams due at `now − days`, then repeatedly process every due
  stream (stamping `ItemValue(..., clock=int(virtual_time))`), advance each by its
  **real** interval, and jump virtual time to the next earliest due. Flushes in
  batches; `speed_multiplier` paces wall time (`sleep(advance / speed)`), so
  higher = quicker.

### 9.6 `__main__.py` — the CLI

`python -m otobs {provision|simulate|backfill|list|check}`.

- `cmd_list()` — every asset class, its hosts, a line per parameter, **plus the
  enabled sim-config features**.
- `cmd_check()` — the offline self-test: parse the catalog, run the generator 500×
  per parameter asserting in-band + correct type + valid triggers, **then
  `validate()` `sim_config.yml` against the catalog** and print the feature status.
- `backfill` — parses `--days` / `--speed` (tiny `_flag` helper; argparse would be
  overkill) and calls `run_backfill`.
- `main()` — dispatches, lazy-importing `provision`/`simulate` so `list`/`check`
  stay offline (no `zabbix_utils`).

### 9.7 `test_sim.py` — the realism self-check

No framework, just asserts (`.venv/bin/python test_sim.py`): the **disabled ==
legacy** invariant (seeded RNG, identical value stream), correlation lifting the
underperform rate, trend producing an intermediate below-band value (a ramp not a
step), dropout at p=0/p=1, the time-of-day multiplier (incl. the midnight-wrapping
window), and `validate()` rejecting a bad param key / band. `make check` covers
the catalog; this covers the layer.

### 9.8 `Makefile`

`make help` auto-generates its menu from `##` comments. New target:

```make
make backfill                    # uses days/speed from sim_config.yml
make backfill DAYS=7 SPEED=2000  # override per run
```

---

## 10. Data lifecycle — follow one reading end to end

Trace `hmi.cpu.temp` on the Grissik HMI, with the realism layer **on**:

1. **Definition** — `workstation_hmi.yml` defines `hmi.cpu.temp` (float, 1 m,
   good/underperform/failed bands). `sim_config.yml` puts it in the
   `thermal_cascade` correlation group and gives it a 3600 s trend ramp.
2. **Load** — `load_all()` builds the typed `Parameter` and host `HMI-GRS-WW01`;
   `load_sim_config()` builds the `SimConfig`; `make check` cross-validates them.
3. **Provision** — `apply()` creates the trapper item + triggers on the template,
   and the host linked to it with Grissik's inventory.
4. **Simulate** — each tick, `correlation_forces` sees `hmi.fan.rpm` on this host
   is `failed`, so (prob. 0.7) it forces `hmi.cpu.temp` toward `underperform`;
   `process_stream` arms a ramp from the last value; `sample_stream` emits an
   interpolated point climbing into the band; it's pushed as
   `ItemValue("HMI-GRS-WW01", "hmi.cpu.temp", "...")`.
5. **Ingest** — the server accepts it (a trapper item with that key exists on that
   host), stores history, evaluates triggers against `last()`.
6. **Observe** — the value appears in Latest data as a *ramp*; crossing 65 °C
   raises a warning Problem; the host pin colors by worst severity. Run
   `make backfill` first and the graph already has days of context behind it.

---

## 11. Live demo script

```bash
cp .env.example .env       # central config
make venv                  # build the Python env
make up                    # start Zabbix — wait ~30-60s on first boot (DB import)
make check                 # OFFLINE proof: catalog + generator + sim-config all sane
make provision             # build all Zabbix config from the catalog (idempotent)
make backfill DAYS=7 SPEED=5000   # optional: 7 days of history so graphs aren't empty
make simulate              # stream live data (Ctrl+C to stop)
```

Then at **http://localhost:8080** (`Admin` / `zabbix`):

1. **Latest data** — live values; filter by host group to walk each asset class.
2. **Problems** — triggers firing (Underperform = warning/average, Failed =
   high/disaster). Let it run; watch problems appear and clear.
3. **Geomap** — all 12 hosts across Indonesia, colored by worst severity.
4. **Templates** — open an item, show the embedded description (FMEA component +
   real collection method). "The tool documents itself."

**Two closers:**

- *Single source of truth:* add one line to `sites.yml`, `make provision`, and the
  new hosts appear on the map and start streaming — one line.
- *Realism layer:* in `sim_config.yml` set `correlation.enabled: true` (and drop
  `SIM_STICKINESS` so the fan actually reaches `failed`); `make simulate` and watch
  `hmi.cpu.temp` follow the fan into degradation on the same host. Or enable
  `trend` and show a ramp in the graph instead of a step.

---

## 12. Likely questions & crisp answers

- **"Why simulate instead of real equipment?"** No plant exists yet; the goal is to
  validate the monitoring *design*. The config plane is identical to production, so
  swapping a mock for a real collector is just changing an item's type
  (Trapper → SNMP/Agent) — keys, triggers, dashboards stay.

- **"Why Trapper for everything?"** It's the common push path; the production
  Node-RED S7comm bridge pushes over the same protocol, so one simulator stands in
  for Node-RED, Agent 2, and SNMP pollers at once.

- **"Doesn't the realism layer change the production story?"** No — it's a
  **data-plane** feature. It only affects the *values* the simulator generates; the
  items, triggers, and templates `provision` builds are untouched. And it's fully
  optional: off by default, byte-identical to the plain machine when disabled.

- **"How is correlation not just noise?"** It's causal and per-host: it reads the
  *current* band of a named trigger param and biases a named affected param toward
  degrading, with a tunable `strength`. `test_sim.py` shows it measurably raises the
  affected param's underperform rate versus the independent baseline.

- **"What exercises `nodata()` triggers?"** The `dropout` feature: it skips a due
  send while still advancing `next_due`, so a genuine one-interval gap forms —
  exactly what `nodata()` watches for. Overrides let you keep specific streams
  gap-free.

- **"How does backfill get old timestamps in?"** Trapper's per-value `clock` field.
  `run_backfill` sweeps the same state machine over `[now − days, now]` in
  discrete events at each parameter's real interval, stamping every `ItemValue` with
  its virtual timestamp. `speed_multiplier` only controls how fast it's generated,
  not the timestamps themselves.

- **"Why can't the PLC CPU be read over SNMP?"** The CP443-1 exposes only the comms
  layer over SNMP — not CPU memory, the diag buffer, or per-channel I/O. Those need
  S7comm reading SZL lists. That constraint is *why* the middleware bridge exists,
  and it's encoded in each parameter's `collection` field.

- **"What stops a bad config from corrupting Zabbix?"** Front-loaded validation:
  the catalog (fields, types, intervals, trigger op/severity, duplicate keys) *and*
  `sim_config.yml` (real param keys, real bands, in-range numbers) both validate at
  load / `make check`, before any API call or data send.

- **"How do I add a parameter / a station?"** Parameter: add it to the relevant
  `catalog/*.yml`, `make check`, `make provision`, `make simulate`. Station: one
  line in `sites.yml`, then `make provision`.

---

## 13. Thirty-second summary

> A single-source-of-truth YAML catalog drives a **real Zabbix 7.0 stack** two
> ways: one tool **provisions** it (idempotent config-as-code) and another
> **simulates** OT/IT telemetry into it (a sticky Good/Underperform/Failed state
> machine over Trapper, each metric on its own compressed clock). An optional
> `sim_config.yml` layer adds realism on demand — cross-parameter correlation,
> gradual trends, time-of-day cycles, data dropout, and one-shot historical
> backfill — each toggleable and off by default, so the baseline is unchanged. It
> models a PGN gas station across 3 sites and 41 parameters, honestly tagging what's
> natively collectable vs. what needs an S7comm/Node-RED bridge. Because the config
> plane is production-shaped, swapping mock data for real collectors is a one-line
> item-type change.

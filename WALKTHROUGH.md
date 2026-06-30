# Project Walkthrough — OT Observability Lab

> A presenter's *and* engineer's guide to this repo. Read it top to bottom and
> you'll understand **what** it is, **why** each piece exists, **how** every
> function works at the line level, and **how** to demo and defend it — enough to
> present and answer hard technical questions as if you built it yourself.
>
> Sections 1–5 are the story (the pitch, the domain, the design). Sections 6–11
> are the engineering deep-dive (Zabbix concepts, the catalog schema, and a
> function-by-function code tour). Sections 12–14 are the demo, the Q&A, and the
> summary.

---

## 1. The one-sentence pitch

> "It's a self-contained lab that runs a **real Zabbix 7.0 monitoring stack** in
> Docker and feeds it **simulated OT/IT telemetry** for gas-transmission station
> equipment — so we can watch real monitoring triggers fire across Good →
> Underperform → Failed health states without any physical hardware."

If someone asks "what problem does it solve?":

> "We need to prove out a monitoring design for PGN's gas stations (PLCs, HMIs,
> switches, the SCADA process) before there's any real plant to plug into.
> This lab builds the **exact same Zabbix config** the production system would
> use, then mocks the data feed. Swapping a mock for a real collector later is a
> one-line change because the config plane is identical."

---

## 2. The core idea (memorize this — it's the whole design)

Everything hangs off **one principle: a single source of truth.**

```
catalog/*.yml ──┬──> provision  (builds Zabbix config: templates, items, triggers, hosts)
                └──> simulate    (pushes mock data: Good / Underperform / Failed streams)
```

The YAML files in `catalog/` describe every monitored parameter **once**. Two
programs read those same files:

- **`provision`** turns the catalog into Zabbix *configuration* (what to monitor,
  what alerts to fire) over the Zabbix JSON-RPC API.
- **`simulate`** turns the catalog into *fake data* and streams it into Zabbix
  over the Trapper protocol.

Add a parameter in YAML → both the monitoring config **and** the data generator
pick it up automatically. There is exactly one definition of each metric, and it
contains both how to *monitor* it and how to *fake* it. **Lead with this when you
present.** It's the decision that makes everything else fall into place, and it's
what separates this from click-ops in the Zabbix UI.

Why it matters in practice: in a normal Zabbix setup, someone clicks through the
web UI to create 41 items × 12 hosts, then separately writes a data feed. The two
drift. Here they can't drift — they're generated from the same 4 files.

---

## 3. The domain — what we're actually monitoring

This models a PGN gas-transmission station. Four **asset classes**, each in its
own catalog file, **41 parameters total**, deployed across **3 stations**
(Grissik, Tebanggi Besar, Bojonegara — defined in `catalog/sites.yml`):

| Asset class | Catalog file | Zabbix host group | Params | What it represents |
|---|---|---|---|---|
| Gas Process (SCADA) | `gas_process.yml` | `OT/Process` | 17 | The physical process: inlet/outlet pressure, flow, gas heating value & SG, valve positions, ESD, fire & gas, H2S/LEL safety, odorant, compressor suction/discharge/vibration |
| PLC Siemens S7-400 | `plc_s7400.yml` | `OT/PLC` | ~7 | The controller: CPU operating mode, diagnostic buffer, event accumulation, I/O channel, rack module status, CP reachability |
| Workstation / HMI | `workstation_hmi.yml` | `IT/HMI` | 10 | The operator PC: CPU util/temp, RAM, disk free, SMART (reallocated sectors + health), NIC errors/state, fan RPM, PSU 12 V rail |
| Switch / Router | `switch_router.yml` | `Network/Industrial` | 5 | Industrial network: interface oper/admin status, error rate, frame discards, fan state |

Each of the 3 sites gets one host per asset class → **12 hosts**, each plotted on
a Geomap of Indonesia and each streaming its parameters live.

### The "technical honesty" angle (judges/reviewers love this)

The catalog records, per parameter, **how it would really be collected** in
production — and admits what Zabbix *can't* see natively. This isn't decoration;
it's encoded in the `collection:` field of every parameter and embedded into the
live Zabbix item description.

- **Switches/routers:** everything is native SNMP (IF-MIB `ifOperStatus`,
  `ifInErrors`/`ifOutErrors`, CISCO-ENVMON-MIB fan state). Trivially collectable.
- **HMI:** CPU/RAM/disk/SMART/NIC come from **Zabbix Agent 2** natively; fan RPM,
  PSU rails, and CPU temperature need middleware (LibreHardwareMonitor over WMI,
  or iDRAC/iLO/IPMI).
- **PLC S7-400:** the interesting one. The Siemens **CP443-1** communications
  module exposes SNMP (MIB-II, LLDP) but **cannot see CPU memory, the diagnostic
  buffer, or per-channel I/O**. Those require **S7comm** (RFC 1006 over TCP 102)
  reading the **SZL** system-status lists — e.g. SZL `0x0424` byte 11 for
  operating mode, `0x00A0` for the diagnostic buffer. So `plc.cp.icmp_latency` is
  the *only* natively-collectable PLC metric; the rest are tagged *needs
  middleware*.
- **Gas process:** all SCADA tags need an S7comm → Node-RED → Zabbix bridge — the
  same tags the HMI reads off the PLC.

In the lab, **every value arrives via Zabbix Trapper** (push). That's deliberate:
the production Node-RED bridge would push via the very same `zabbix_sender`
protocol, so one simulator can faithfully stand in for Node-RED, Agent 2, **and**
SNMP pollers at once. The only thing that changes when you go to production is the
**item type** on the template (Trapper → SNMP agent / Zabbix agent); the keys,
triggers, descriptions, and dashboards are untouched.

---

## 4. The health model — Good / Underperform / Failed

Every parameter is a **sticky state machine** over three condition bands:

- **Good** — within spec, no action.
- **Underperform** — wear / partial / transient degradation, still operating.
  **This is the high-value signal.** The smooth degradation curves here are the
  training data for later phases (clustering, predictive maintenance / RUL).
- **Failed** — primary function lost; high-severity triggers fire.

"Sticky" means: each tick, with probability `SIM_STICKINESS` the parameter
**stays** in its current state; otherwise it re-rolls by the catalog weights
(defaults 90% good / 8% underperform / 2% failed). Stickiness is what turns a
sequence of independent coin-flips into **long, smooth runs** — realistic
degradation, not per-tick noise. Mathematically it's a Markov chain whose
self-transition probability is pinned to `STICKINESS` and whose off-diagonal mass
is split by the steady-state weights.

Two tuning knobs (the "calibration knobs for the mock plant"), both in `.env`:

- `SIM_STICKINESS` (0–1) — higher = longer dwell in each state = smoother curves.
  Repo default is **0.70**.
- `SIM_TIME_SCALE` — time compression. `1.0` = real catalog intervals; the repo
  ships **1000.0**, which makes even a `1h` SMART metric tick every few seconds so
  a demo shows movement immediately. (`run()` floors this at `0.001` to avoid a
  divide-by-zero.)

> Note: the README's config table lists 0.92 / 10.0 as illustrative values, but
> the actual `.env`/`.env.example` ship 0.70 / 1000.0. Quote the real ones.

---

## 5. Architecture — the moving parts

```
                         catalog/*.yml  (single source of truth)
                          /                         \
              otobs.provision                    otobs.simulate
            (Zabbix JSON-RPC API)              (Trapper / zabbix_sender)
                   |                                   |
                   v                                   v
   ┌───────────────────────────────────────────────────────────────┐
   │  Zabbix 7.0 stack (docker compose)                             │
   │   zabbix-web    (nginx, :8080)  ── API + UI                    │
   │   zabbix-server (:10051 trapper) ── triggers, history          │
   │   zabbix-db     (postgres 16)    ── config + history store     │
   │   zabbix-agent2                  ── monitors the lab host       │
   └───────────────────────────────────────────────────────────────┘
```

Two planes, and keeping them straight is the key to explaining the system:

- **Config plane** — *what to monitor and when to alert.* `provision` builds it:
  host groups, templates, items, triggers, hosts. Built once and re-run
  idempotently after catalog edits.
- **Data plane** — *the actual measured values over time.* `simulate` feeds it: a
  continuous stream of metric readings pushed over Trapper into the server, which
  stores history and evaluates triggers against it.

The **Docker stack is real Zabbix**, not a mock — that's the whole point.
`docker-compose.yml` runs four official Zabbix 7.0 images plus Postgres 16:

- `zabbix-db` (postgres:16-alpine) — config + history store. Has a healthcheck
  (`pg_isready`) so the server only starts once the DB is accepting connections.
- `zabbix-server` — the engine: evaluates triggers, stores history, listens on
  **:10051** (the trapper port the simulator pushes to). `depends_on` the DB
  being *healthy*.
- `zabbix-web` (nginx + PHP) — the **:8080** frontend and the JSON-RPC API that
  `provision` talks to.
- `zabbix-agent2` — monitors the lab host itself, so there's one genuinely-real
  (non-simulated) host alongside the 12 mock OT hosts.

Everything is parameterized through `.env` — Postgres credentials, the two host
ports, timezone, API URL/login, sender host/port, and the two sim knobs — so
**Docker and the Python tooling read one config file**. There's a named volume
`zbx_db_data` so `make down` keeps your data and only `make clean` (`down -v`)
wipes it.

---

## 6. Zabbix concepts you must be able to explain

If you're presenting this as your own, you need the vocabulary. Here's the
minimum, mapped to where it shows up in the code:

- **Host** — a monitored thing (a PLC, an HMI). Has a *technical name* (`host`,
  e.g. `PLC-S7400-GRS`, immutable identity) and a *visible name* (`name`, e.g.
  "Grissik — PLC S7-400H"). The simulator addresses values by technical name.
- **Host group** — a folder of hosts (`OT/PLC`, `IT/HMI`…). Drives UI filtering
  and is the scope `prune()` operates within.
- **Item** — one metric on a host (`plc.cpu.operating_mode`). Has a **key**, a
  **value type** (float/unsigned/text/char/log), units, and a **type** that says
  *how it's collected*. Here the type is **2 = Zabbix trapper** (push). The key
  is shared between the item definition and the trapper payload — that's the glue.
- **Template** — a reusable bundle of items + triggers. You define monitoring
  **once on a template** and **link** it to many hosts. This is config-as-code vs.
  click-ops, and it's why the design scales: 41 items defined once, not 41 × 12.
- **Trigger** — a boolean expression over item history that creates a "problem"
  when true, e.g. `last(/Template OT PLC S7-400/plc.cpu.operating_mode)>=13`. Has
  a **severity/priority** (info → disaster).
- **Trapper** — a push item type. An external sender (`zabbix_sender`, or the
  `zabbix_utils.Sender` this repo uses) connects to server port 10051 and pushes
  `(host, key, value)` tuples. The server accepts a value only if a trapper item
  with that key exists on that host — which is why **you must `provision` before
  `simulate`**.
- **Inventory** — per-host metadata fields. We set `location`, `location_lat`,
  `location_lon`, etc., and Zabbix's **Geomap** widget reads those to plot pins.
- **Macros** — `{$NAME}` variables on hosts/templates (e.g. `{$SITE}`, `{$RACK}`).
  We set them per host so a single template can carry site-specific values.
- **SZL** (System-status Zustandsliste) — Siemens S7 diagnostic data structures
  read over S7comm. Named in the PLC catalog's `collection` fields to justify the
  "needs middleware" tagging. You don't implement S7comm here — you *document*
  that it's the real collection path.

---

## 7. The catalog schema — the data model in full

This is the contract every YAML file obeys, and `catalog.py` enforces it. Know
this cold; it's where most questions land.

### 7.1 The station registry — `sites.yml`

Physical stations are defined **once** here. Every asset class generates one host
per site. Each site requires `code`, `name`, `lat`, `lon`, `location` (validated
at load), plus free-form fields referenced by host templates:

```yaml
sites:
  - { code: GRS, name: "Grissik", location: "Grissik Gas Plant, ...",
      lat: "-2.0500", lon: "103.4300", city: "Musi Banyuasin",
      grade: enterprise, p_out_sp: "16" }
```

- `code` → the host-name token (`PLC-S7400-GRS`).
- `location` / `lat` / `lon` / `city` → become Zabbix host **inventory** (and the
  geomap pin).
- `grade`, `p_out_sp`, and **anything else you add** → available to
  `host_template` macros via `{field}` substitution.

**Adding a station is one line here.** It then appears in all four asset classes,
on the geomap, and in the simulator — no other edit.

### 7.2 An asset-class file — top level

```yaml
asset_class: "PLC Siemens S7-400"     # human label
host_group:  "OT/PLC"                  # Zabbix host group
template_name: "Template OT PLC S7-400" # the template items/triggers live on
template_group: "Templates/OT"          # template group (folder)
host_template:                          # generate one host per site...
  tech:   "PLC-S7400-{code}"            #   technical name; {field} from the site
  name:   "{name} — PLC S7-400H"        #   visible name
  macros: { "{$SITE}": "{code}", "{$RACK}": "0", "{$SLOT}": "2" }
parameters: [ ... ]                     # the metrics
```

Instead of `host_template` you may hand-list `hosts:` (each with `host`, `name`,
`macros`, optional `inventory`) — for one-off hosts that aren't a station. One of
the two is required.

### 7.3 A parameter

```yaml
- key: "plc.cpu.operating_mode"   # Zabbix item key AND the trapper key — the glue
  name: "CPU Operating Mode"
  value_type: unsigned            # float | unsigned | text | char | log
  units: ""
  interval: "15s"                 # expected cadence: 15s | 30s | 1m | 5m | 1h ...
  component: "C. Modules (CPU)"   # FMEA subsystem (from the report)
  collection: "S7comm via Node-RED (SZL 0x0424, byte 11)"  # real-world method
  failure_mode: "Hardware defect, watchdog timeout, ..."
  source: "Siemens OPC SZL Diagnostics, 2024"              # citation
  sim:   { ... }                  # how the simulator generates values
  triggers: [ ... ]               # Good/Underperform/Failed alerting
```

`component`, `collection`, `failure_mode`, `interval`, `source` are **baked into
the Zabbix item description** (see `Parameter.description()`), so the running tool
documents itself — open any item and you see the FMEA component and how it's
really collected.

### 7.4 `sim` — two kinds

**numeric** — three value bands; the simulator samples within the current band
plus jitter:

```yaml
sim:
  kind: numeric
  good:         [40, 60]
  underperform: [66, 84]
  failed:       [86, 99]
  weights: [good, underperform, failed]  # tokens → default probs, or raw numbers
  jitter: 1.5
```

`weights` is optional (defaults to `[good, underperform, failed]` → 0.90/0.08/0.02).
For `value_type: unsigned`, samples are rounded to integers.

**enum** — discrete states, each a fixed value with a band weight:

```yaml
sim:
  kind: enum
  states:
    - { value: 8,  weight: good,         label: "RUN (0x08)" }
    - { value: 6,  weight: underperform, label: "START-UP (0x06)" }
    - { value: 13, weight: failed,       label: "DEFECT (0x0D)" }
```

`weight` is a number, or one of `good`/`underperform`/`failed` (mapped to the
defaults). A "binary" metric just lists two states (e.g. link `up(1)`/`down(2)`).
Values are normalized per parameter so weights don't have to sum to 1.

### 7.5 `triggers`

```yaml
triggers:
  - { op: "=",  value: 6,  severity: warning, label: "CPU not in RUN" }
  - { op: ">=", value: 13, severity: high,    label: "CPU STOP/DEFECT" }
```

- `op` ∈ `= <> > >= < <=`; `severity` ∈ `info | warning | average | high | disaster`.
- `func` optional (default `last`) — the Zabbix function, e.g. `last`, `avg`.
- Generated expression: `func(/<template_name>/<key>) <op> <value>`. A two-sided
  limit (brownout + overvoltage) is just two trigger entries.

---

## 8. The code — function-by-function

The Python lives in `otobs/` (~600 lines). Pure standard library plus two deps:
`zabbix_utils` (official Zabbix API + Sender client) and `PyYAML`. No web
framework, no ORM, no `python-dotenv` — deliberately minimal.

### 8.1 `settings.py` — config loader (44 lines)

- `ROOT` / `CATALOG_DIR` — derived from `__file__` so paths work regardless of CWD.
- `_load_env()` — a ~10-line hand-rolled `.env` parser: skips blanks/comments,
  splits on the **first** `=`, strips inline `# comments`, and uses
  `os.environ.setdefault` so **real environment variables win over `.env`**.
  (Skips the file silently if absent — defaults still apply.)
- `_f(key, default)` — float-with-fallback for the sim knobs (bad value → default
  instead of a crash).
- Exposes typed module-level constants: `API_URL/USER/PASSWORD`,
  `SENDER_HOST/PORT`, `STICKINESS`, `TIME_SCALE`.

> Talking point: "I didn't add a dependency for a ten-line `.env` parser, and I
> kept env-var precedence so it behaves in a container."

### 8.2 `catalog.py` — load + validate (243 lines) — the heart

This is where single-source-of-truth becomes typed Python. Lookup tables up top
turn human strings into Zabbix API codes:

- `DEFAULT_WEIGHTS = {good: 0.90, underperform: 0.08, failed: 0.02}`
- `VALUE_TYPE_CODE = {float:0, char:1, log:2, unsigned:3, text:4}`
- `SEVERITY_CODE = {not_classified:0, info:1, warning:2, average:3, high:4, disaster:5}`

**Helpers:**

- `parse_interval("15s") -> 15`, `"5m" -> 300`, `"1h" -> 3600`. Reads the last
  char as the unit, requires the rest to be digits, raises on anything else.
- `_weight(w)` — passes numbers through; maps a token via `DEFAULT_WEIGHTS`;
  raises otherwise.

**Dataclasses** (the typed model):

- `State` — one outcome: `weight`, `band` (good/underperform/failed/custom for
  display), and either an enum `value` or numeric `lo`/`hi`/`jitter`.
- `Trigger` — `op`, `value`, `severity`, `label`, `func="last"`. Its
  `__post_init__` validates `severity` and `op` **at construction**, so a bad
  trigger fails the moment the YAML loads, not at provision time.
- `Sim` — `kind` + `states`, with `normalized_weights()` = each weight ÷ total.
- `Parameter` — all the metric fields, plus computed properties `interval_s`
  (seconds), `value_type_code` (Zabbix int), and `description()` which assembles
  the embedded living-doc string.
- `Host` — `host`, `name`, `macros`, `inventory`.
- `AssetClass` — ties a host group + template to its `Host`s and `Parameter`s.

**Builders (where validation happens):**

- `_build_sim(raw, where)` — branches on `kind`:
  - *enum*: each state must have `value` and `weight`; builds a `State` per entry.
  - *numeric*: requires `good`/`underperform`/`failed` bands, each a `[min, max]`
    pair; zips them with `weights`; one `State` per band carrying the shared
    `jitter`. Any other `kind` raises.
- `_build_param(raw, where)` — checks all 9 required fields, validates
  `value_type` and the interval string up front, builds triggers via
  `Trigger(**t)` (so trigger validation fires here), then constructs the
  `Parameter`. The `where` string (filename + key) makes every error message
  point at the offending line.
- `load_sites(directory)` — parses `sites.yml`, validates the five required site
  fields, returns `[]` if the file is absent.
- `_expand_hosts(tmpl, sites, where)` — the host-template engine: for each site,
  `.format(**site)` the `tech`/`name`/`macros` (so `{code}`→`GRS`), build the
  inventory dict (`location`, `location_lat/lon`, `site_city`,
  `site_country="Indonesia"`), and emit one `Host`. A `{field}` that isn't in the
  site raises a clear "unknown site field" error.
- `load_file(path, sites)` — validates top-level keys, chooses `host_template`
  expansion vs. literal `hosts`, builds every parameter, and **rejects duplicate
  item keys** within a file. Returns a fully-typed `AssetClass`.
- `load_all(directory)` — loads `sites.yml`, globs every other `*.yml` (sorted,
  deterministic), and returns the list of `AssetClass`. This single call is what
  both `provision` and `simulate` consume — the literal embodiment of "one source
  of truth."

> Talking point: "Validation is front-loaded into construction. By the time
> `load_all()` returns, the catalog is known-good — bad YAML never reaches the
> Zabbix API or the sender."

### 8.3 `provision.py` — config-as-code (140 lines)

A `Provisioner` class. The whole thing is **idempotent** via a *get-or-create*
pattern, so re-running converges Zabbix to the catalog instead of duplicating.

- `__init__` — `ZabbixAPI(url)`, `login(user, password)`, prints the negotiated
  API version. `close()` logs out best-effort.
- `_templategroup` / `_hostgroup` / `_template` — each does
  `api.X.get(filter=...)`; returns the existing id or creates it. Called once per
  asset class.
- `_item(template_id, p, existing)` — skip if `p.key` is already in the
  pre-fetched `existing` set; else `item.create` with **`type=2` (trapper)**,
  `value_type=p.value_type_code`, units, and the rich `p.description()`.
- `_triggers(template_name, p, existing)` — for each trigger, build
  `desc = "{name}: {label}"`, skip if present, else create with expression
  `f"{t.func}(/{template_name}/{p.key}){t.op}{t.value}"` and
  `priority=SEVERITY_CODE[t.severity]`.
- `_host(h, hg_id, template_id, existing)` — if the host exists, **update** its
  visible name + inventory (so editing a site's coords and re-provisioning moves
  the pin); else **create** it linked to its template, with macros and inventory.
  `inventory_mode = 0 if inv else -1` (manual vs. disabled).
- `ensure_geomap()` — `settings.update(geomaps_tile_provider="OpenStreetMap.Mapnik")`
  so the map works with zero clicks.
- `prune(assets)` — deletes catalog-managed hosts no longer in the catalog, but
  **scoped to the catalog's own host groups** (`groupids=...`), so it provably
  never touches unrelated hosts. Remove a site → its 4 hosts vanish on re-provision.
- `apply(asset)` — the per-asset orchestration, and the **performance-conscious**
  part: it fetches the existing items, triggers, and hosts **once per template**
  (three calls), then loops in memory. That avoids the N+1 explosion of asking
  Zabbix "does this exist?" for every single item/trigger/host.
- `main()` — `load_all()`, set the geomap, `apply` each asset, then `prune`, with
  `close()` in a `finally`.

> Talking point: "It's declarative: the catalog is desired state, `provision`
> reconciles — create missing, update changed, prune removed — and it batches its
> existence checks to keep the API calls O(asset classes), not O(items)."

### 8.4 `simulate.py` — the mock plant (99 lines)

- `sample(sim, state, value_type)` — the value generator. **enum** → return the
  fixed `state.value`. **numeric** → `random.uniform(lo, hi)` **+**
  `random.gauss(0, jitter)`, then clamp to `[lo - jitter, hi + jitter]` so it
  stays near the band, then `int(round(v))` for `unsigned` else `round(v, 3)`.
  The uniform gives spread across the band; the Gaussian jitter adds realistic
  sensor wobble; the clamp stops outliers crossing into the next band.
- `next_state(sim, cur, stickiness)` — the sticky transition: if we have a current
  state and `random.random() < stickiness`, **stay**; otherwise re-roll by
  cumulative `normalized_weights()`. First tick (`cur is None`) always rolls fresh.
- `Stream` — a dataclass per (host, parameter): carries `state_idx` (current
  state) and `next_due` (when this metric should next emit).
- `build_streams(assets)` — the triple loop `asset → host → parameter` → one
  `Stream` each (12 hosts × their params ≈ a few hundred streams).
- `run(assets)` — the engine loop:
  1. `scale = max(TIME_SCALE, 0.001)` (divide-by-zero guard).
  2. Every 0.5 s wall-clock: for each stream whose `next_due` has passed, advance
     its state, sample a value, append an `ItemValue(host, key, str(value))` to a
     **batch**, and schedule `next_due = now + interval_s / scale` (so each metric
     keeps its own cadence, compressed by the scale).
  3. Push the whole batch in **one** `sender.send()`, print a heartbeat with
     `processed`/`failed` counts and any non-Good readings (so you can watch
     degradation in the terminal).
  4. Send errors are caught and printed, **not fatal** — the lab survives blips.
- `main()` — runs `load_all()`, handles `KeyboardInterrupt` for a clean Ctrl+C.

> Talking point: "Each metric has its own clock and its own sticky state, all
> driven off one batched sender. The combination of uniform-in-band + Gaussian
> jitter + stickiness is what makes the Underperform curve look like real
> degradation instead of random spikes."

### 8.5 `__main__.py` — the CLI (70 lines)

`python -m otobs {provision|simulate|list|check}`. Two are **offline** (no Zabbix
needed), which is what lets you iterate on the catalog fast:

- `cmd_list()` — prints every asset class, its hosts, and a one-line summary per
  parameter (key, type, interval, sim kind/bands, trigger count) + a total.
- `cmd_check()` — the **self-test**: parse the whole catalog, then for every
  parameter run the generator **500 times** and assert each sample lands within
  `[lo − 5·jitter − 0.5, hi + 5·jitter + 0.5]` (or equals the enum value), with
  the right Python type (`unsigned` must be `int`). Also asserts every trigger
  references a real key with a valid severity/op. Prints a one-line OK with the
  sample count. **This is the "one runnable check"** — proof the generation logic
  works, and a typo-catcher before you ever touch the stack.
- `main()` — dispatches; lazy-imports `provision`/`simulate` only when needed (so
  `list`/`check` don't import `zabbix_utils.Sender`); prints usage + exits 2 on a
  bad arg.

### 8.6 `Makefile` — orchestration

The entire UX is `make` targets — `venv`, `up`, `down`, `clean`, `logs`,
`provision`, `simulate`, `list`, `check`. The data targets depend on `venv` so
they self-install. `make help` auto-generates its menu by `grep`-ing the `##`
comments. `up`/`down` keep the DB volume; `clean` (`down -v`) wipes it.

---

## 9. How it was built — a defensible build order

If asked "how did you approach this?", this order is believable and mirrors the
code's dependency structure (each step only needs the ones before it):

1. **Stand up real Zabbix first** — `docker-compose.yml`, four official 7.0
   images + Postgres, all wired through `.env`, with a DB healthcheck so the
   server waits. Confirm UI + API answer.
2. **Design the catalog schema** — decide one YAML = one asset class, and that
   each parameter carries *both* its monitoring definition *and* its simulation
   spec. Write it down in `catalog/README.md`. This is the keystone decision.
3. **Build the loader + validator** (`catalog.py`) with dataclasses so bad YAML
   fails loudly and early. Ship `list` + `check` so the catalog is iterable
   **without** Zabbix running.
4. **Fill the catalog** from the OT feature-discovery report — FMEA components,
   failure modes, real collection methods, the Good/Underperform/Failed bands,
   and the alert thresholds.
5. **Write `provision.py`** — idempotent get-or-create against the Zabbix API,
   template-centric layout. The catalog becomes live config.
6. **Write `simulate.py`** — the sticky state machine pushing Trapper data. Watch
   real triggers fire in Problems.
7. **Add the station registry** (`sites.yml` + `host_template` expansion) so
   "more stations" is one line, and wire up the geomap inventory + tile provider.
8. **Polish** — Makefile, `.env.example`, README, runbook, architecture doc.

---

## 10. Design decisions & trade-offs (be ready to defend these)

- **YAML catalog over a database or the Zabbix UI.** YAML is diffable,
  version-controlled, and reviewable in a PR. The Zabbix UI can't be code-reviewed
  and drifts from any data feed. Trade-off: no referential integrity for free, so
  `catalog.py` does the validation a schema/DB would have given.
- **Everything via Trapper in the lab.** One push path stands in for SNMP, Agent,
  and Node-RED at once, and matches the production bridge protocol. Trade-off:
  the lab doesn't exercise real SNMP polling — acceptable because the *config
  plane* (the thing being validated) is identical regardless of item type.
- **Idempotent get-or-create instead of teardown-rebuild.** Re-running converges
  rather than wiping history. Trade-off: it won't *update* an existing item's
  definition (e.g. changed units) — it only creates missing ones. For this lab,
  config edits are rare and a `make clean` resets fully; a production version
  would add update-in-place.
- **Sticky Markov sim instead of replayed real traces.** No real data exists yet,
  and stickiness produces controllable, smooth degradation curves on demand.
  Trade-off: it's a model, not ground truth — the `STICKINESS`/`TIME_SCALE` knobs
  are exactly the calibration the README calls out.
- **Standard library over dependencies.** Hand-rolled `.env` parser, no ORM, no
  web framework — two deps total. Trade-off: a few lines we own vs. a supply
  chain we don't. For a ~600-line tool that's the right side of the line.
- **Template-per-asset-class, host-per-site.** Mirrors how a real Zabbix shop
  organizes config and keeps definitions O(asset classes) not O(hosts).

---

## 11. Data lifecycle — follow one reading end to end

Trace `proc.pressure.outlet` for the Grissik process host, start to finish:

1. **Definition** — `gas_process.yml` has a parameter `proc.pressure.outlet`
   (float, 5 s interval, numeric sim with good/underperform/failed bands and
   triggers). `sites.yml` has `GRS`.
2. **Load** — `load_all()` parses both; `_expand_hosts` produces host
   `PROC-GRS`; the parameter becomes a typed `Parameter`.
3. **Provision** — `apply()` ensures the template, creates a **trapper item**
   `proc.pressure.outlet` on `Template OT Gas Process`, creates its triggers
   (`last(/Template OT Gas Process/proc.pressure.outlet)<op><value>`), and creates
   host `PROC-GRS` linked to that template with Grissik's inventory.
4. **Simulate** — a `Stream(host="PROC-GRS", param=proc.pressure.outlet)` ticks
   every `5s / TIME_SCALE`; `next_state` keeps/rolls the band, `sample` draws a
   value, it's pushed as `ItemValue("PROC-GRS", "proc.pressure.outlet", "...")`.
5. **Ingest** — the server accepts it because a trapper item with that key exists
   on that host, stores it in history, and **evaluates the triggers** against
   `last()`.
6. **Observe** — the value shows in Monitoring → Latest data; if a band crossed a
   threshold, a Problem appears at the trigger's severity; the host's pin on the
   Geomap colors by its worst active problem.

That round trip — one key, defined once, flowing through config and data — is the
system in miniature.

---

## 12. Live demo script (what to actually run and click)

```bash
cp .env.example .env       # central config
make venv                  # build the Python env (.venv + zabbix_utils + pyyaml)
make up                    # start Zabbix — wait ~30-60s on first boot (DB import)
make check                 # OFFLINE proof the catalog + generator are sane
make provision             # build all Zabbix config from the catalog (idempotent)
make simulate              # stream Good/Underperform/Failed data (Ctrl+C to stop)
```

Then open **http://localhost:8080** (`Admin` / `zabbix`) and show:

1. **Monitoring → Latest data** — live values updating. Filter by host group to
   walk through each asset class.
2. **Monitoring → Problems** — triggers firing in real time (Underperform =
   warning/average, Failed = high/disaster). Let it run a minute; watch problems
   appear and clear as states drift.
3. **Monitoring → Geomap** — all 12 hosts plotted across Indonesia, colored by
   worst active severity.
4. **Data collection → Templates** — open an item, show the embedded description
   (FMEA component + real collection method). "The tool documents itself."

**The closer:** add one station line to `catalog/sites.yml`, run `make provision`
again, and show the new hosts appear on the map and start streaming — *one line*.
That single move proves the entire single-source-of-truth thesis.

---

## 13. Likely questions & crisp answers

- **"Why simulate instead of real equipment?"** No plant exists yet, and the goal
  is to validate the monitoring *design*. The config plane is identical to
  production, so swapping a mock for a real collector is just changing an item's
  type (Trapper → SNMP/Agent) on the template — keys, triggers, dashboards stay.

- **"Why Trapper for everything?"** It's the common push path. The production
  Node-RED S7comm bridge pushes via the same `zabbix_sender` protocol, so one
  simulator stands in for Node-RED, Agent 2, and SNMP pollers. Going real only
  changes the item type.

- **"Why can't the PLC CPU be read over SNMP?"** The CP443-1 comms module only
  exposes the comms layer over SNMP — not CPU memory, the diagnostic buffer, or
  per-channel I/O. Those need S7comm reading SZL lists (`0x0424` mode, `0x00A0`
  diag). That constraint is *why* the middleware bridge exists, and it's encoded
  honestly in each parameter's `collection` field.

- **"What's the value of the Underperform state?"** It's the training signal for
  the next phases — clustering and predictive maintenance / remaining-useful-life.
  Healthy and dead are easy; the gradual degradation curve is where the ML value
  is, and stickiness is what makes those curves smooth and learnable.

- **"Is provisioning safe to re-run?"** Fully idempotent. Get-or-create for
  groups/templates/items/triggers; hosts are re-synced (name + inventory); only
  catalog-managed hosts in the catalog's own host groups are pruned. Unrelated
  hosts are never touched.

- **"How does the simulator avoid hammering the API?"** It batches every due
  reading into a single `Sender.send()` per 0.5 s tick, and provisioning fetches
  existing items/triggers/hosts once per template instead of per object.

- **"What stops a bad catalog from corrupting Zabbix?"** Validation is at load
  time — required fields, value types, interval format, trigger op/severity,
  duplicate keys — all raise before any API call. `make check` runs the generator
  500×/parameter offline to confirm values stay in-band.

- **"How do I add a parameter / a station?"** Parameter: add it to the relevant
  `catalog/*.yml`, `make check`, `make provision`, `make simulate`. Station: one
  line in `sites.yml`, then `make provision`. Both flow through automatically.

---

## 14. Thirty-second summary (if you only get one slide)

> A single-source-of-truth YAML catalog drives a **real Zabbix 7.0 stack** two
> ways: one tool **provisions** it (idempotent config-as-code — templates,
> trapper items, triggers, hosts, geomap) and another **simulates** OT/IT
> telemetry into it (a sticky Good/Underperform/Failed state machine over
> Trapper, each metric on its own compressed clock). It models a PGN gas station
> — process, PLC, HMI, network — across 3 sites and 41 parameters, honestly
> tagging what's natively SNMP/Agent-collectable vs. what needs an
> S7comm/Node-RED bridge. Because the config plane is already production-shaped,
> swapping mock data for real collectors later is a one-line item-type change.

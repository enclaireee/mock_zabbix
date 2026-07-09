# WALKTHROUGH — the complete technical reference

Read this top to bottom and you can explain, defend, and extend every part of
this repository as if you built it: every module, every schema field, every
Makefile target, every realism feature, and every Zabbix concept it touches —
including *why* each one is the way it is, and what the alternatives were.

The focused docs under [`docs/`](docs/) are quick references for a single
topic; this document is the deep, synthesized version. Where they answer
"what", this answers "what, why, how, and what else was considered".

**Contents**

1. [Overview & mental model](#1-overview--mental-model)
2. [Repository layout, file by file](#2-repository-layout-file-by-file)
3. [The catalog schema, field by field](#3-the-catalog-schema-field-by-field)
4. [Provisioning, in depth](#4-provisioning-in-depth)
5. [Simulation, in depth](#5-simulation-in-depth)
6. [Zabbix integration internals](#6-zabbix-integration-internals)
7. [Data lifecycle traces, one per asset class](#7-data-lifecycle-traces-one-per-asset-class)
8. [Testing & validation](#8-testing--validation)
9. [Operational workflows](#9-operational-workflows)
10. [Design decisions & alternatives considered](#10-design-decisions--alternatives-considered)
11. [Live demo script](#11-live-demo-script)
12. [Q&A — likely questions, crisp answers](#12-qa--likely-questions-crisp-answers)
13. [Auxiliary CLI tools: dashboards & extract](#13-auxiliary-cli-tools-dashboards--extract)

---

## 1. Overview & mental model

### The problem

PGN operates gas-transmission stations (Grissik → Terbanggi Besar → Bojonegara
on the South Sumatra–West Java line). We need to prove out an asset-health
monitoring design for the equipment in those stations — Siemens S7-400 PLCs,
Wonderware HMI workstations, industrial switches, and the gas process
instrumentation itself — *before* any real plant is wired up. And the
monitoring data has a second customer: later project phases (Tahap 2/3) will
train clustering and predictive-maintenance models on it, so the mock
telemetry has to be statistically plausible, not just present.

### The solution in one sentence

A YAML **catalog** defines every monitored parameter exactly once, and two
programs consume it: `provision` builds the real Zabbix 7.0 configuration
(templates, items, triggers, hosts) over the JSON-RPC API, and `simulate`
generates Good/Underperform/Failed telemetry for those same parameters and
pushes it in over the Zabbix Trapper protocol.

```
                catalog/*.yml  +  sites.yml     (each parameter/station defined ONCE)
                       /                \
             otobs.provision        otobs.simulate
          (JSON-RPC API: builds     (Trapper push: streams
           templates, items,         Good/Underperform/Failed
           triggers, hosts)          values, optionally shaped by
                   |                 sim_config.yml realism)
                   v                        |
        ┌──────────────────────────────────v───────────┐
        │  real Zabbix 7.0 (docker compose)             │
        │  web/API :8080 · server/trapper :10051        │
        │  Postgres 16 · Agent 2 (monitors the host)    │
        └───────────────────────────────────────────────┘
```

### The two planes

Keep these separate and the whole system explains itself:

- **Config plane** — *what to monitor and when to alert.* Built by
  `provision`, idempotently. Identical to what production would use.
- **Data plane** — *the measured values over time.* Fed by `simulate` and
  `backfill`. Everything in `sim_config.yml` (the realism layer) lives here
  and only here.

This split is the production story: going live means replacing the data
plane's source (the simulator) with real collectors — changing each item's
*type* from Trapper to SNMP/Agent on the template — while the config plane
(keys, triggers, descriptions, dashboards, maps) stays byte-for-byte the same.

### Why catalog-driven instead of hand-configuring Zabbix

The naive path is click-ops: create 41 items × 4 templates in the web UI,
attach 12 hosts, then separately write a data generator that happens to use
the same keys. Three things go wrong:

1. **Drift.** The UI config and the generator have no shared source; the
   moment someone edits one, they disagree, and the failure mode is silent
   (values rejected, or triggers watching the wrong threshold).
2. **No review, no history.** UI clicks aren't diffable or revertible. A YAML
   catalog in git is both.
3. **No scaling.** Adding a station by hand is an afternoon; here it's one
   line in `sites.yml` because hosts are *generated* from a template per site.

The catalog also carries fields Zabbix itself doesn't need (`failure_mode`,
`collection`, `source`) — the FMEA context — and bakes them into each item's
description, so the running system documents itself.

### The health model

Every parameter is a three-state machine: **Good** (in spec), **Underperform**
(degraded but operating — the high-value signal, since smooth degradation
curves are exactly what predictive-maintenance models train on), **Failed**
(function lost, high-severity triggers fire). §5 explains the machine; §3
explains how each parameter defines its bands.

---

## 2. Repository layout, file by file

```
catalog/                 the single source of truth
  ├─ sites.yml           station registry (one line per physical site)
  ├─ gas_process.yml     asset class: SCADA process tags       (20 params)
  ├─ plc_s7400.yml       asset class: Siemens S7-400 PLC       (6 params)
  ├─ workstation_hmi.yml asset class: Wonderware HMI PC        (10 params)
  ├─ switch_router.yml   asset class: industrial switch        (5 params)
  ├─ comm_links.yml      comm-link SLA system: 11 segments + 20 circuits (8 WAN techs)
  ├─ bmkg.yml            asset class: External Environment — regional weather (5 params)
  ├─ sim_config.yml      the ACTIVE realism config (a copy of a preset)
  └─ README.md           the schema quick reference
presets/                 nine ready-made sim modes for `make config`
otobs/                   the Python package (stdlib + zabbix_utils + PyYAML)
  ├─ settings.py         .env → typed settings (no python-dotenv)
  ├─ catalog.py          load + validate catalog/*.yml into dataclasses
  ├─ sim_config.py       load + validate sim_config.yml into dataclasses
  ├─ provision.py        Zabbix API reconciler (config plane)
  ├─ simulate.py         state machine + realism + Trapper push (data plane)
  ├─ weather_engine.py   deterministic (timestamp-only) weather model (`omega` mode, §5.3)
  ├─ dashboard.py        export/import hand-built dashboards to dashboard/*.json
  ├─ extract.py          read-only SLA/history/trend export to CSV/JSON/table (§13)
  └─ __main__.py         CLI: provision|simulate|backfill|config|list|check|
                          export-dashboards|import-dashboards|extract
docs/                    one-topic reference docs (see README's doc map)
tests/                   pytest suite (conftest, test_simulate, test_config,
                          test_provision, test_weather)
test_extract.py          assert-only self-checks for the extract CLI (§13.2)
dashboard/               exported dashboard JSON + _refs.json (id→name map)
Makefile                 orchestration (`make help`)
docker-compose.yml       the real Zabbix 7.0 stack
.env / .env.example      central variables (compose + Python both read it)
```

Module responsibilities are strict:

| Module | Owns | Explicitly does *not* own |
|---|---|---|
| `settings.py` | reading `.env` once, typed globals | any YAML |
| `catalog.py` | parameter/host/trigger model + validation | anything Zabbix-API- or sampling-specific |
| `sim_config.py` | realism-feature model + validation | the sampling logic itself (lives in `simulate.py`) |
| `provision.py` | config plane (API calls) | values, timing, sampling |
| `simulate.py` | data plane (state machine, sampling, sending) | creating any Zabbix object |
| `dashboard.py` | dashboard export/import + id↔name remapping | catalog, items, hosts (reads their ids, doesn't manage them) |
| `extract.py` | read-only history/trend/SLA export | anything that creates/updates/deletes in Zabbix |
| `__main__.py` | CLI dispatch + offline commands (`list`, `check`, `config`) | business logic |

`provision.py` and `simulate.py` never import each other and share nothing but
the catalog objects — the MECE split that makes the "swap the data plane for
production" story credible.

### `settings.py`

Reads the repo-root `.env` once at import: skips blank/comment lines, splits
on the first `=`, strips a trailing ` #` inline comment, and stores with
`os.environ.setdefault` — so a variable already set in the real environment
wins over the file (normal 12-factor precedence). Exposes:

| Setting | From | Used by |
|---|---|---|
| `API_URL`, `API_USER`, `API_PASSWORD` | `ZBX_API_*` | provision |
| `SENDER_HOST`, `SENDER_PORT` | `ZBX_SENDER_*` | simulate/backfill |
| `STICKINESS` (0.92), `TIME_SCALE` (10.0) | `SIM_*` | simulate |
| `TIMEZONE` | `ZBX_TIMEZONE` | simulate (`time_of_day` local hour) |
| `ROOT`, `CATALOG_DIR`, `PRESETS_DIR` | paths | everyone |

There is deliberately no `python-dotenv` dependency: the parser is ~10 lines
and this repo's `.env` grammar is trivial. `_f()`/`_i()` wrap float/int parsing
with a fallback to the default on a bad value — every setting degrades rather
than crashing, including `SENDER_PORT`: `list`/`check`/`config` never dial it,
so a typo there must not take down commands that are documented as offline.

### The Makefile

`make help` auto-generates its menu from the `##` comments. Targets:

| Target | Does | Notes |
|---|---|---|
| `venv` | create `.venv`, `pip install -r requirements.txt` | dependency of every Python target — they self-install |
| `up` / `down` / `clean` / `logs` | compose up −d / down / down −v / logs −f | `down` keeps the DB volume; only `clean` wipes it |
| `provision` | `python -m otobs provision` | idempotent reconcile (§4) |
| `simulate` | `python -m otobs simulate` | live stream, Ctrl+C to stop |
| `backfill` | `python -m otobs backfill [--days --speed]` | `DAYS=`/`SPEED=` make vars map to the flags |
| `config` | `python -m otobs config [MODE or --file FILE]` | mode switcher; no arg = status |
| `list` | `python -m otobs list` | parsed-catalog sanity view + enabled features |
| `check` | `python -m otobs check` | full offline self-test, no Zabbix needed |
| `export-dashboards` / `import-dashboards` | `python -m otobs export-dashboards` / `import-dashboards` | round-trip hand-built dashboards through `dashboard/*.json` (§13.1) |
| `extract` | `python -m otobs extract $(ARGS)` | read-only SLA/history/trend pull, `ARGS="sla --from 7d"` (§13.2) |

### The Docker stack

Four official images plus Postgres, all wired through `.env`:

- **zabbix-db** — Postgres 16 (alpine), named volume `zbx_db_data`, with a
  `pg_isready` healthcheck…
- **zabbix-server** — …which the server waits on (`condition:
  service_healthy`), because first boot imports the whole DB schema (~30–60 s)
  and the server crashes-loops against an empty DB otherwise. Exposes
  `${ZBX_TRAPPER_PORT}:10051` — the trapper socket the simulator pushes to.
- **zabbix-web** — nginx frontend + the JSON-RPC API on
  `${ZBX_WEB_PORT}:8080`.
- **zabbix-agent2** — monitors the lab host itself, so there is one *real*
  (non-mock) host next to the simulated fleet — useful to show the difference
  between real and simulated collection in the same UI.

---

## 3. The catalog schema, field by field

Two kinds of file live in `catalog/`: the **station registry** (`sites.yml`)
and one file per **asset class**. (`sim_config.yml` also lives there but is a
different concern — §5.3.)

### 3.1 `sites.yml` — the station registry

```yaml
sites:
  - { code: GRS, name: "Grissik", location: "Grissik Gas Plant, …",
      lat: "-2.0500", lon: "103.4300", city: "Musi Banyuasin",
      grade: enterprise, p_out_sp: "16" }
```

| Field | Required | Meaning |
|---|---|---|
| `code` | ✔ | short token used in generated host names (`PLC-S7400-GRS`) |
| `name` | ✔ | display name, used in generated visible names |
| `location`, `lat`, `lon` | ✔ | become Zabbix host inventory → drives the Geomap pin |
| `city` | – | inventory `site_city` |
| anything else (`grade`, `p_out_sp`, …) | – | free-form; referenced by `host_template` macros |

The three shipped sites are the real SSWJ transmission chain: **Grissik**
(South Sumatra, source) → **Terbanggi Besar** (PGN receiving/distribution
station at Bandar Gunung Agung, Lampung Tengah, operating since 2007) →
**Bojonegara** (Banten terminal, reached via the Sunda Strait offshore
section). Adding a fourth station is one line here; every asset class then
generates a host for it (§3.2), so one line yields 4 hosts and 41 streaming
items on the geomap.

### 3.2 Asset-class top level

```yaml
asset_class: "Workstation / HMI"       # human label (print/logs only)
host_group: "IT/HMI"                   # Zabbix host group (UI filtering + prune scope)
template_name: "Template OT Workstation HMI"   # where items+triggers live
template_group: "Templates/OT"         # folder for the template
host_template:                         # generate one host per site:
  tech: "HMI-{code}-WW01"              #   technical name ({field} = site field)
  name: "{name} — Operator Station"    #   visible name
  macros: { "{$SITE}": "{code}", "{$GRADE}": "{grade}" }
parameters: [ … ]                      # the metrics (3.3)
```

- `host_template.tech` is the **identity** of each generated host — the
  simulator addresses trapper values by this exact string, and provisioning
  uses it as the create/update key. `{field}` placeholders are formatted from
  each site row; referencing a field a site doesn't have fails at load with
  the offending name.
- Instead of `host_template` you may hand-list `hosts:` (each with `host`,
  `name`, optional `macros`/`inventory`) for one-off hosts that aren't a
  station. Every shipped class uses `host_template`.
- Macros (`{$SITE}`, `{$P_OUT_SP}`, …) are set per host so one template can
  carry site-specific values. In the current catalog they are documentation
  and forward provision — no shipped trigger references them yet.

### 3.3 A parameter

```yaml
- key: "hmi.cpu.temp"          # Zabbix item key == the trapper payload key
  name: "CPU Core Temperature" # item display name
  value_type: float            # float | unsigned | text | char | log
  units: "°C"                  # display units (optional, default "")
  interval: "1m"               # expected cadence: <digits><s|m|h>, must be > 0
  component: "E. Temperature"  # FMEA subsystem (from the discovery report)
  collection: "custom (LHM WMI / IPMI OEM)"  # how it's REALLY collected in production
  failure_mode: "Dried thermal paste, heatsink fouling, AC failure"
  source: "Zabbix WMI Sensors + LibreHardwareMonitor, 2024"
  sim: { … }                   # how the simulator generates it (3.4)
  triggers: [ … ]              # alerting thresholds (3.5)
```

All fields except `units` and `triggers` are required — the loader refuses a
parameter missing any of them. Four of these fields exist purely for
**technical honesty**: `component`, `collection`, `failure_mode`, and `source`
say what subsystem this measures, how a real deployment would collect it
(and, implicitly, what Zabbix can't see natively), why it fails, and where the
numbers come from. All four plus `interval` are baked into the Zabbix item
**description**, so an operator clicking any item in the UI sees the full
provenance — living documentation, not a wiki that rots.

`interval` does double duty: it documents the production cadence *and* paces
the simulator (divided by `SIM_TIME_SCALE` when live). It's parsed once per
parameter and cached.

### 3.4 The `sim` block — two kinds

**`numeric`** — three value bands, one per health state:

```yaml
sim:
  kind: numeric
  good: [40, 60]
  underperform: [66, 84]
  failed: [86, 99]
  weights: [good, underperform, failed]   # exactly 3: token or number each
  jitter: 1.5
```

- Each band is `[lo, hi]` (inclusive). Bands are deliberately **disjoint**
  with gaps (60→66 here): trigger thresholds sit inside the gaps, so a
  trending value visibly *crosses* the threshold rather than teleporting over
  it.
- `weights` maps positionally onto good/underperform/failed. A token
  (`good`/`underperform`/`failed`) means the default steady-state probability
  from `DEFAULT_WEIGHTS` in `catalog.py` — 0.90 / 0.08 / 0.02; a number is
  used as-is. Weights are normalized per parameter, so they needn't sum to 1.
  A list that isn't exactly 3 long is rejected at load (a shorter list would
  otherwise silently drop a band).
- `jitter` is Gaussian noise (σ). It plays two roles: in the baseline sampler
  it's one-shot noise on top of a uniform in-band draw (clamped to
  `[lo−jitter, hi+jitter]`); with the `continuity` feature on it becomes the
  **per-tick step size** of the value walk. That second role is why the
  catalog's jitters are sized as *tick-to-tick instrument noise* (PT
  electronics ±0.25 % of span, class-A RTD ±0.15 °C, GC repeatability
  ~0.1 %…), not as "how wide the band is" — several were retuned to this
  standard, with the justification inline next to each changed value.
- For `value_type: unsigned`, every sample is rounded to `int` — Zabbix
  rejects `"3.0"` for an unsigned item, so the sampler owns the typing.

**`enum`** — discrete states with fixed values:

```yaml
sim:
  kind: enum
  states:
    - { value: 8,  weight: good,         label: "RUN (0x08)" }
    - { value: 6,  weight: underperform, label: "START-UP (0x06)" }
    - { value: 13, weight: failed,       label: "DEFECT (0x0D)" }
```

- `value` is emitted verbatim (int/float/string — e.g. the PLC's diagnostic
  buffer text messages).
- `weight` is a token or a number, as above. **The token doubles as the
  state's band name.** A *numeric* weight has no band token, so the loader
  tags that state `custom:<value>` — keeping every such state distinct,
  because correlation and the state machine address states *by band name*.
- `label` is annotation-only: the loader accepts and ignores it. It documents
  what the raw value means (`13` = DEFECT) for the human reading the YAML.

An enum can also have **two** states instead of three — e.g. interface
oper-status is up/down (`good`/`failed` only), because "flapping" is a
time-domain pattern, not a third value; the *Underperform* network signal
lives in the error-rate/discard counters instead. The band model doesn't force
three states onto a binary reality.

### 3.5 `triggers`

```yaml
triggers:
  - { op: ">=", value: 65, severity: warning, label: "Thermal degradation" }
  - { op: ">=", value: 85, severity: high,    label: "Thermal throttling" }
```

Each entry becomes one Zabbix trigger on the template with expression
`last(/<template_name>/<key>) <op> <value>`:

- `op` ∈ `= <> > >= < <=`; `severity` ∈ `info | warning | average | high |
  disaster` (mapped to Zabbix's integer priorities 1–5); both validated at
  load.
- `label` combines with the parameter name into the trigger description
  (`"CPU Core Temperature: Thermal degradation"`) — which is also the
  trigger's *identity* for reconciliation (§4).
- `func` (optional, default `last`) selects the history function. Note the
  expression builder emits `func(/tmpl/key)` with **no argument window**, so
  only zero-argument functions like `last()` are currently expressible —
  windowed functions (`avg(…,1h)`, `nodata(…,5m)`) would need a small schema
  extension (§10.7).
- Two-sided limits are just two entries (e.g. the PSU +12 V rail has brownout
  *and* overvoltage triggers, per the ATX ±5 % spec — the sim only exercises
  the brownout side, and says so in a comment).

The convention throughout: **Underperform bands trip warning/average
triggers, Failed bands trip high/disaster** — so the UI's problem colors map
1:1 onto the health model.

### 3.6 Load-time validation (why bad YAML can't reach Zabbix)

`catalog.py` validates *at construction*, not at use: missing required
fields, unknown `value_type`, malformed/zero `interval` (a 0 interval would
spin the scheduler forever), bad trigger op/severity, unknown trigger fields
(a typo like `severit:` raises with the parameter name), missing numeric
bands, non-`[lo,hi]` band shapes, weight lists ≠ 3 entries, empty enum
states, weights summing to 0 (division-by-zero at sample time), duplicate
keys within a file, **and** duplicate keys across files (the trapper
addresses items by bare key, so a cross-file collision would silently shadow
one parameter). `load_all()` is the single entry point both consumers share:
it loads `sites.yml`, then every `*.yml` except `sites.yml`/`sim_config.yml`.

### 3.7 `discovery` — simulated Low-Level Discovery (only the switch)

Four of the switch's five parameters are **per-port**: a switch has many
interfaces, not one. Rather than hand-write five items × N ports, the switch
catalog carries an optional `discovery:` block that turns selected params into
**item/trigger prototypes** under a Zabbix Low-Level Discovery (LLD) rule — the
native mechanism that auto-creates one item per discovered instance (port, disk,
core).

```yaml
discovery:
  key: "net.if.discovery"          # the LLD rule's trapper key
  name: "Interface discovery (IF-MIB, LLD)"
  macro: "{#IFNAME}"               # LLD macro (default {#IFNAME})
  prototypes: ["net.if.oper_status", "net.if.admin_status",
               "net.if.error_rate", "net.if.discards"]   # which params are per-port
  ports: ["Gi1/0/1", …, "Gi1/0/8"]                        # the simulated walk result
```

**Technical honesty — this is a lab simulation of SNMP LLD, not a real SNMP
poll.** In production a switch is SNMP-walked (IF-MIB `ifDescr`/`ifIndex`) to
enumerate its interfaces; that walk *is* natively collectable (§ architecture,
"Switch/Router: everything"). Here there is no physical switch, so — exactly like
every other value in this lab — the port list is **pushed via trapper**: the
simulator sends `net.if.discovery` a `{"data":[{"{#IFNAME}":"Gi1/0/1"}, …]}`
payload and the server materializes one item per port from each prototype. Swap
the item type Trapper→SNMP and this same catalog block becomes a real IF-MIB
discovery with no other change.

Design points:

- `prototypes` lists **existing parameter keys** (from §3.3). A listed param
  becomes an item prototype keyed `<key>[{#IFNAME}]`, and each of its triggers
  becomes a trigger prototype — reusing that param's `sim`, `value_type`, and
  thresholds verbatim. One definition drives every port; the simulator runs one
  independent Good/Underperform/Failed machine per (host, port), so port 3 can be
  degrading while ports 1–8 stay clean. All the §5.3 realism features key off the
  **base** param key, so a single `sim_config` entry covers every port.
- Any param **not** listed stays a flat per-host item — `net.env.fan_state` is
  chassis-level (one fan tray, not per-port), so it deliberately stays flat.
- Validation is load-time and as strict as everything else in §3.6: missing
  fields, an empty or duplicate `ports` list, a `prototypes` entry that isn't a
  real param key, a `macro` not shaped `{#…}`, or a `key` colliding with a param
  key all raise at load.

---

## 4. Provisioning, in depth

`otobs/provision.py` is a **reconciler**: it makes Zabbix match the catalog —
creating what's missing, updating what drifted, deleting what the catalog no
longer defines. Re-running it against an unchanged catalog is a clean no-op.

### 4.1 Connection

`Provisioner.__init__` logs into the JSON-RPC API (`zabbix_utils.ZabbixAPI`)
with the `.env` credentials. A failed login is caught in `main()` and
reported with the two operator hints that actually matter (stack still
booting? credentials don't match the frontend?) instead of a raw traceback.

### 4.2 Per asset class: `apply()`

For each of the five asset classes (the comm-link catalog is just another asset
class — its segments and circuits are ordinary Zabbix items, so `apply()` needs
no special case; the segment→circuit *dependency* is a data-plane concern, §5):

1. **Get-or-create the containers** — template group, host group, template.
   Each helper looks the object up by name and creates it only if absent;
   these are the only objects where "exists" is the whole question.
2. **Fetch existing state once** — all items on the template (with the fields
   we manage), all triggers on the template (with `expandExpression: True` so
   the stored expression is comparable to ours), and the asset's hosts. One
   fetch per object kind instead of an N+1 existence-check per object — with
   41 parameters this is 3 API calls, not ~120.
3. **Reconcile items** — for each parameter: absent → `item.create` (type 2 =
   Trapper, plus name/value-type/units/description); present → diff the four
   managed fields in string space (the API returns everything as strings) and
   `item.update` only what changed.
4. **Reconcile triggers** — identity is the description
   (`"<param name>: <label>"`); the managed fields are `expression` and
   `priority`. Same create/diff/update logic.
5. **Prune template strays** — any trigger, then any item, that exists on the
   template but isn't in the catalog is deleted. Triggers go first because
   deleting an item cascades to its triggers and would double-delete.
   Consequence worth knowing: renaming a `key` or a trigger `label` changes
   its *identity*, so it's a delete-plus-create — history for the old item is
   gone. That's the correct single-source-of-truth semantic, but it's a
   rename-is-not-a-rename gotcha.
6. **Reconcile hosts** — absent → `host.create` with the host group, template
   link, macros, and inventory (`inventory_mode=0`, manual); present →
   `host.update` re-syncing visible name, macros, and inventory. Template
   links are set at creation and not re-synced — re-linking is destructive in
   Zabbix (unlink-and-clear deletes item history) and the template's identity
   hasn't changed if the catalog's `template_name` hasn't.

If the asset class has a `discovery:` block (§3.7), its **prototype** params are
routed through `_discovery()` instead of the flat item path: get-or-create the
LLD rule (`discoveryrule`, type 2 = trapper), then create/diff/prune item
prototypes (`<key>[{#IFNAME}]`) and trigger prototypes exactly like the flat
`item`/`trigger` reconcile — same identity keys (prototype `key_`, trigger
`description`), same "one bad object doesn't abort the run" wrapping (§4.3.1).
Flat params (`fan_state`) are untouched by this path. Migrating a param from flat
to prototype is a delete-of-the-flat-item + create-of-the-prototype (identity
change), same rename-is-not-a-rename semantics as §4.4.

### 4.3 Global steps

- **`ensure_geomap()`** — sets the server-wide Geomap tile provider to
  OpenStreetMap so the map renders with zero manual setup.
- **`prune(assets)`** — deletes catalog-managed *hosts* that no longer appear
  in the catalog (e.g. a station removed from `sites.yml`). Scoped strictly
  to the catalog's own host groups, so a host created by hand in some other
  group can never be collateral damage. Note what this means for the edge
  case "site removed while its host had manual UI changes": the host is
  deleted, manual changes and all — group membership is the contract that
  says "this object is catalog-managed".
> The comm-link **SLA services and dashboard are not provisioned** — that Zabbix
> setup is done by hand in the UI (see [comm-links-sla.md](docs/comm-links-sla.md)).
> To make that manual work easy, each circuit's "down" trigger is pre-tagged
> `link:<circuit_key>` by the loader, so a Service's problem-tag mapping is a
> copy-paste of the circuit key.

### 4.3.1 One bad object doesn't abort the run

Every create/update/delete call in `_item`, `_triggers`, `_host`,
`ensure_geomap`, and `prune` is wrapped and reported through
`Provisioner._fail`, which appends to `self.errors` and prints
`! FAILED <what>: <reason>` instead of raising. `apply()`'s own setup step
(the get-or-create containers plus the three existence fetches) is wrapped
the same way — if *that* fails, the asset class is skipped (nothing to
reconcile without it) but the loop moves on to the next one. `main()` still
runs `prune()` and closes the session either way, then prints a final
`Provisioning finished with N error(s)` summary and exits 1 if anything
failed. The alternative — one exception anywhere aborting the whole run —
means a single Zabbix-side rejection (e.g. refusing a `value_type` change on
an item that already has history) leaves every asset class after the failing
one completely untouched, with nothing but a raw traceback to show for it.
Verified live: an item with a syntactically invalid trigger function raises
`Invalid params … unknown function`, which is caught and reported, exit code
1 — but the other three asset classes and all their hosts still reconciled
in the same run.

### 4.4 What idempotent means here, precisely

| You change… | Re-provision does… |
|---|---|
| nothing | nothing (verified: zero mutating calls on a double run) |
| a parameter's `name`/`units`/`value_type` or any description field | `item.update` of just those fields |
| a trigger's `severity`/`op`/`value` | `trigger.update` of priority/expression |
| a trigger `label` or item `key` | delete old + create new (identity change) |
| a site's coordinates or a `host_template` macro | `host.update` re-sync |
| delete a parameter | trigger prune, then item prune (history gone) |
| delete a site | host prune (scoped to catalog groups) |

Everything above was exercised against the live stack: fresh provision,
no-op re-run, field edits, param deletion, restoration.

---

## 5. Simulation, in depth

The simulator has two layers: the **baseline state machine** (always on) and
the **realism layer** (`sim_config.yml`, seven features, each off by default).
The load-bearing invariant between them:

> With `sim_config.yml` absent, or every feature `enabled: false`, the output
> is **byte-for-byte identical** to the plain state machine — same values,
> same RNG draw sequence. `tests/test_simulate.py` asserts this against a seeded RNG.

That invariant is why the realism layer was safe to add and is safe to
extend: `baseline` mode *is* the reference implementation, permanently.

### 5.1 The baseline: a sticky state machine per stream

A **`Stream`** is one `(host, parameter)` pair — 3 sites × 41 parameters =
123 streams. Each holds its current state index, its next-due time, its last
emitted value, and (inert unless `trend` is on) ramp bookkeeping.

**State selection** (`next_state`): each due tick, with probability
`SIM_STICKINESS` (default 0.92) the stream *stays* in its state; otherwise it
re-rolls from the normalized catalog weights. Why sticky rather than
independent draws? Independent per-tick draws produce flicker — a disk that's
"failed" for 5 seconds and healthy the next. Real degradation *persists*.
Stickiness turns the three weights into a Markov chain whose self-transition
is pinned at 0.92, giving geometrically-distributed dwell times (mean ≈ 12.5
ticks) — long Good stretches, occasional multi-tick Underperform excursions,
rare Failed episodes. Those smooth stretches are precisely the training
signal the ML phases need. One caveat to state honestly: because the
*re-roll* uses the raw weights, the chain's long-run distribution is slightly
biased toward rarer states versus the naive weights — the weights steer, they
aren't an exact stationary distribution (§10.6).

**Value sampling** (`sample`): enum states emit their fixed value; numeric
states emit `uniform(lo, hi) + gauss(0, jitter)`, clamped to
`[lo−jitter, hi+jitter]`, rounded to int for unsigned items. This is the
"teleporting" baseline the continuity feature exists to fix.

**Timing**: the live loop wakes every 0.5 s, advances every stream whose
`next_due` has passed, and sets `next_due = now + interval_s / SIM_TIME_SCALE`
— so at the default 10×, a `5s` process tag ticks every 0.5 s and a `1h`
SMART metric every 6 minutes. All emitted values for a tick are batched into
**one** `sender.send()` (one TCP round-trip, and Zabbix's per-connection
overhead is the real cost); the heartbeat line prints processed/failed counts
plus any non-Good readings. Send failures are printed and *not* fatal — a
simulator that dies when the server restarts would be operationally useless.

### 5.2 The per-tick pipeline

Both the live loop and backfill drive every stream through the same function,
`process_stream` — one code path, so live and historical data cannot diverge:

```
dropout roll ──> correlation force ──> next_state ──> arm ramp? ──> sample_stream
 (only if           (may override        (sticky or       (on band       (see below)
  enabled —          the state roll)      weighted)        transition,
  preserves RNG      + segment force      if forced,       if trend on)
  draw order)        for comm circuits    that state)
```

The **segment force** is the comm-link dependency (§ the fifth system): before
the due streams are processed each tick, `segment_forces` resolves every physical
segment's current state and hard-forces each circuit with a `depends_on` to the
worst of its segments — merged on top of any correlation force and winning, so a
fiber cut deterministically drops every circuit on that span together. Unlike
`correlation` (probabilistic, same-host bias), this is a hard, cross-item
function; VSAT circuits (no `depends_on`) skip it and roll independently. Full
rationale: [comm-links-sla.md](docs/comm-links-sla.md).

`sample_stream` resolves the value with an explicit precedence:

1. **enum** → the state's fixed value, always.
2. **ramping** (trend armed and inside `ramp_seconds`) → linear interpolation
   from the last value toward a target drawn in the new band, times any
   time-of-day multiplier, plus jitter — **no band clamp**, because a ramp
   deliberately traverses the gap *between* bands.
3. **walking** (continuity on, not ramping, last value inside the current
   band) → analog signals (`jitter > 0`) mean-revert toward the band centre
   (× time-of-day multiplier) with `gauss(0, jitter × step_scale)` noise,
   clamped in-band; counters (`jitter = 0`) hold their value exactly.
4. **plain** → the baseline `sample()`, exactly, if no multiplier applies;
   otherwise the uniform draw × multiplier + jitter (unclamped — a multiplier
   deliberately shifts the value, clamping would erase it).

Feature responsibilities are disjoint by construction: **correlation** only
influences *state selection*, **trend/continuity/time-of-day** only influence
the *value* (with the fixed precedence above), **dropout** only influences
*emission*, **backfill** only influences *timing*. No two features compute
the same thing, so there is no "who wins" ambiguity beyond the documented
ramp > walk > plain ordering.

**One stream type skips this pipeline entirely.** `bmkg.*` weather streams
(`omega` mode) are intercepted at the very top of `process_stream`, right
after the dropout roll: the value comes from `WeatherNode.get_weather()`, a
deterministic function of the real timestamp, and `next_state`/hold/trend/
`sample_stream` never run for them. `process_stream` takes a separate `clock`
parameter for this — distinct from `now`, which is a *scheduling* clock
(`time.monotonic()` live, real time in backfill) and would feed the weather
model the wrong notion of "what time it is" if reused directly. Full detail:
[weather-engine.md](docs/weather-engine.md).

### 5.3 The seven realism features and the real behavior each models

Configured in `catalog/sim_config.yml`; full annotated schema in
[docs/sim-config.md](docs/sim-config.md). What matters here is *why each
exists*:

**1. `continuity` (`step_scale`, `reversion`)** — real process variables are
*closed-loop controlled*: a regulated pressure hovers at its setpoint with
small proportional noise; it does not wander uniformly across its operating
range, and it never legally jumps 6 barg in 5 seconds. The walk models an
AR(1) process: pulled `reversion` of the way back to the setpoint each tick,
plus Gaussian steps. `reversion 0.12` gives lag-1 autocorrelation ≈ 0.88 —
the high autocorrelation characteristic of real plant historians. And
counters (SMART reallocated sectors, fault counts) *hold* rather than bounce,
because a counter only moves on an event. The setpoint is the band centre,
shifted by any time-of-day multiplier — which is exactly how a controlled
variable tracks a moving demand target.

**2. `correlation` (groups of trigger → affects)** — real faults are
*causal*: losing lube-oil pressure overheats the bearing which shakes the
rotor; a stalled chassis fan cooks the CPU; a dying switch fan lets the ASIC
overheat and CRC errors climb. Each group reads the trigger parameter's
*current pre-roll* band (causal — this tick's effects come from last tick's
causes, so in-tick ordering can't matter) and, with probability `strength`,
forces the affected parameter's next state toward `bias_band`, overriding
both weights *and* stickiness — deliberately, so the cascade is visible
rather than suppressed by a 0.92 stickiness. Coupling is strictly per-host:
Grissik's fan failure cannot heat Bojonegara's CPU. One deliberate exception:
`omega` mode's `bmkg.*` weather triggers resolve against the single shared
regional weather station regardless of which host is being evaluated —
weather isn't scoped to one site like everything else, so a heat/dust/
lightning group can drive every site's compressor/HMI/switch at once without
duplicating the weather stream onto each host. Detail: [weather-engine.md](docs/weather-engine.md).

**3. `trend` (`ramp_seconds` + per-param overrides)** — real transitions are
*curves, not steps*: wear develops. On a band transition, the value ramps
linearly from its last value to a target in the new band over `ramp_seconds`
(÷ `SIM_TIME_SCALE`, like every duration). The shipped ramp lengths follow
the physical event, researched not guessed: enclosure heat-up after fan loss
reaches thermal equilibrium in ~1000 s (thermal-throttling studies), so
`hmi.cpu.temp` ramps in 1200 s; lube starvation cooks a journal bearing in
minutes (real trains trip in 5–15 min), so `bearing_temp` ramps in 900 s;
vibration follows bearing damage almost immediately (600 s); gas temperature
has tens of minutes of pipe-steel inertia (2400 s); media degradation
accumulates over hours (7200 s). The `ml` preset deliberately stretches these
(2–6 h) — an RUL model needs a learnable slope, and that divergence from
physical timing is documented in the preset itself.

**4. `time_of_day` (profiles: peak hours + multipliers + shoulder)** — gas
throughput is *diurnal* because it feeds power generation: lowest pre-dawn
(~05:00), high through the working day into the Java-Bali evening peak —
hence the wide 06–22 peak window on flow/compressor speed, and a 07–19 window
on operator CPU (shift hours). And demand *ramps*, it doesn't step: the
multiplier blends linearly over `shoulder_hours` at each window edge (~3 h
for grid-driven process demand, ~1 h for shift-change operator load; 0 = hard
step), fed a minute-resolution fractional hour so the ramp is smooth. Window
edges (`peak_hours`) are floats too, so a shoulder can be centred on a
half-hour boundary — not just the top of the hour. Loaded at `sim_config.yml`
parse time, `_tod()` rejects a `shoulder_hours` wider than the narrower of the
peak/off-peak span: a shoulder that overruns both edges of a narrow window
would mean the multiplier never actually reaches its nominal peak or
off-peak value anywhere, which is silent under-delivery, not a working
config, so it fails loudly instead. The multiplier shifts the continuity
*setpoint* rather than multiplying each sample (which would compound with
the walk); windows wrap past midnight when `start > end`. On `jitter = 0`
counters it does nothing — a counter has no setpoint.

**5. `dropout` (probability + per-param overrides)** — real SCADA history has
*holes*: field-link retries, RTU reboots, transmitters under calibration,
historian deadbanding. A drop freezes the state and emits nothing while
`next_due` still advances — a genuine one-interval gap, not a delayed
re-send. Safety-critical verdicts (`hmi.smart.health_passed`,
`proc.fire_gas.status`) are pinned to probability 0 in the presets — you
never black-hole the fire-and-gas panel. Honest caveat: these gaps are the
*condition* a Zabbix `nodata()` trigger alerts on, but the shipped catalog
defines only `last()` threshold triggers — gap alerting needs a hand-added
`nodata()` trigger until the schema grows windowed functions (§10.7).

**6. `hold` (per-param/prefix `[min, max]` windows per band)** — `SIM_STICKINESS`
is one symmetric scalar, so it can't say "a fiber cut stays down for hours but a
satellite rain-fade clears in minutes." On entering a band with a configured
window, a stream must dwell there for a randomized `uniform(min, max)` before it
may re-roll — real MTTR. Only **self-rolling** streams honour a dwell; a stream
forced this tick by `correlation` or by the comm-link segment dependency ignores
its own hold and follows the force, so a shared fiber cut keeps every circuit on
that span down for the *segment's* window, not a per-circuit one. Windows scale
with `SIM_TIME_SCALE` like intervals. Full detail:
[comm-links-sla.md](docs/comm-links-sla.md#repair-time-mttr-outages-last-realistically-long).

**7. `backfill` (`days`, `speed_multiplier`)** — graphs are useless without
history, and ML needs weeks of it. `run_backfill` sweeps the same state
machine over `[now − days, now]` as a discrete-event simulation: streams are
bucketed by interval (all `5s` streams share one due-clock — checking one
due-time per bucket instead of per stream), each event is stamped with its
historical `clock` and pushed in batches of 500, and virtual time jumps to
the next earliest due tick. Intervals are *real* here (no `TIME_SCALE`), so
historical spacing is physically correct; `speed_multiplier` only paces how
fast wall-clock generates it. Because it replays whatever config is active,
backfilled history has the same continuity/cascades/ramps/cycles as the live
stream that follows it — no visible seam at the join. The `enabled` flag is
metadata only ("this mode is meant to be backfilled"); backfill never runs
automatically.

### 5.4 Modes — `make config`

Rather than hand-editing `sim_config.yml`, you activate a **preset**:
`make config MODE=realistic` validates the preset against the catalog (every
referenced param key and band must exist, every number in range) and only
then copies it over `catalog/sim_config.yml`. A typo'd mode or a broken file
fails loudly and changes nothing. With no argument it prints the active mode
— detected by content-matching the live file against the presets — plus
what's available. Nine ship: `baseline` (reference, all off), `steady`,
`realistic` (flagship), `diurnal`, `stress`, `maintenance`, `demo`, `ml`,
`omega` (weather-driven cross-asset correlation, layered on `realistic`);
their intents and per-feature settings are tabulated in
[docs/sim-config.md](docs/sim-config.md). The workflow is always
**config → (optional) backfill → simulate**.

---

## 6. Zabbix integration internals

What actually happens server-side. If Zabbix is new to you, this section is
the mental model.

### 6.1 The object model

- **Host** — a monitored thing. Two names: the *technical* name (`host`,
  e.g. `PLC-S7400-GRS`) is its immutable identity — the trapper payload and
  the provisioner both address it by this — and the *visible* name (`name`)
  is free-form display. Hosts hold macros and inventory.
- **Host group** — a folder of hosts (`OT/PLC`, `IT/HMI`…). Drives UI
  filtering, permissions, and — critically here — the scope of `prune()`:
  group membership is how the tool knows an object is its own.
- **Item** — one metric on a host or template: a **key** (the join point
  between config and data planes), a **type** (here always 2 = Trapper), a
  **value type** (0 float / 1 char / 2 log / 3 unsigned / 4 text — the
  catalog's readable names map via `VALUE_TYPE_CODE`), units, and a
  description (where the FMEA fields land).
- **Template** — a reusable bundle of items + triggers. Link it to N hosts
  and each host *inherits* all of them: 41 items defined once, not 41 × 12.
  This is the mechanism that makes config-as-code cheap — the per-host cost
  of a new parameter is zero.
- **Trigger** — a boolean expression over item history with a severity,
  e.g. `last(/Template OT PLC S7-400/plc.cpu.operating_mode)>=13`. When it
  flips true, Zabbix opens a **problem** (the UI's red rows, the map's
  colored pins); when false again, the problem resolves. `nodata()` triggers
  fire on the *absence* of data.
- **Inventory** — per-host metadata fields. `location_lat`/`location_lon`
  drive the **Geomap** widget's pins; the pin color is the host's worst
  active problem severity.
- **Macros** — `{$NAME}` variables resolvable in expressions/labels, set at
  template or host level; the catalog sets them per host from site fields.

### 6.2 The two protocols

**JSON-RPC API** (provision): HTTP POST to `/api_jsonrpc.php`; login yields a
token; every call is `{"method": "item.create", "params": {…}}`. It's the
same API the web UI uses — anything you can click, you can script. The
`zabbix_utils` library wraps it as `api.item.create(...)`.

**Trapper protocol** (simulate): a raw TCP exchange on :10051 — the `ZBXD`
binary header framing a JSON body of `{host, key, value[, clock]}` items. The
server accepts a value **only if a trapper item with that key exists on that
host** (that's why `provision` must run before `simulate`; unmatched values
count as `failed` in the response, which the heartbeat surfaces). The
optional per-value `clock` is what makes backfill possible: history lands at
the stated timestamp, not at arrival time. This is the same protocol
`zabbix_sender` and a production Node-RED bridge
(`node-red-contrib-zabbix-sender`) speak — the simulator is a drop-in
stand-in for all of the real pushers at once.

### 6.3 What the server does with a value

Accepted values are written to the history table for the item's value type;
triggers referencing that item are re-evaluated on arrival; state flips
create/resolve problem events; dashboards, Latest data, and the Geomap read
from these stores. Nothing in this repo touches those internals — which is
the point: the lab exercises *real* server behavior, not a mock of it.

### 6.4 The production swap

Each catalog parameter's `collection` field names its real collector: Agent 2
for OS metrics, SNMP for the switch (IF-MIB/ENVMON-MIB), LibreHardwareMonitor
→ WMI for board sensors, and S7comm-via-Node-RED for everything the PLC only
exposes through the SZL system status lists (`0x0424` operating mode,
`0x00A0` diag buffer) — the CP443-1's SNMP stack can't see CPU internals,
which is the report's core constraint and the reason the middleware bridge
exists. Going live per parameter = change the item's type on the template
(Trapper → SNMP agent / Zabbix agent) and fill in the collector-specific
fields. Keys, triggers, severities, descriptions, hosts, maps: unchanged.

---

## 7. Data lifecycle traces, one per asset class

Each trace runs definition → load → provision → simulate → ingest → observe,
with the `realistic` mode active and `SIM_TIME_SCALE=10`.

### 7.1 HMI — `hmi.cpu.temp` on `HMI-GRS-WW01` (the thermal cascade)

1. **Definition** — `workstation_hmi.yml`: float, °C, `1m`, bands
   40–60 / 66–84 / 86–99, jitter 1.5 (CPU temps genuinely bounce a couple of
   °C tick-to-tick). Triggers: ≥65 warning, ≥85 high — inside the band gaps.
   `sim_config.yml` puts it in `thermal_cascade` (fan → temp, strength 0.7)
   with a 1200 s trend ramp (enclosure thermal equilibrium ≈ 1000 s).
2. **Load** — `load_all()` builds the `Parameter` (interval cached as 60 s)
   and expands `HMI-{code}-WW01` × 3 sites; `load_sim_config()` builds the
   `SimConfig`; `make check` cross-validates the two.
3. **Provision** — on `Template OT Workstation HMI`: trapper item
   `hmi.cpu.temp` (value_type 0) with the FMEA description, two triggers; the
   Grissik host links the template and carries Grissik's inventory.
4. **Simulate** — steady state first: continuity walks the value around the
   band centre (50 °C), σ ≈ 1.2 °C steps, reversion 0.12 — a calm, controlled
   line. Then `hmi.fan.rpm` on this host enters `failed` (0 RPM). Each
   following tick, `correlation_forces` sees the fan's band and, with p=0.7,
   forces `hmi.cpu.temp` toward `underperform`; on the transition tick a ramp
   arms from ~50 °C toward a target in 66–84, over 120 s of wall time
   (1200 s ÷ 10). Values climb: 53.1, 58.4, 64.7 — **crossing 65 °C mid-gap**
   — 71.2… The warning problem opens *during* the ramp, exactly like a real
   thermal event, then the walk resumes inside the underperform band.
5. **Ingest** — each value arrives as
   `("HMI-GRS-WW01", "hmi.cpu.temp", "64.7")`; the server matches the trapper
   item, stores history, re-evaluates both triggers on `last()`.
6. **Observe** — Latest data shows a rising curve, not a teleport; Problems
   shows "Thermal degradation" at warning; the Grissik pin on the Geomap
   turns the warning color. If the fan recovers, temp re-rolls back to good
   (no forced bias), ramps *down*, and the problem closes.

### 7.2 PLC — `plc.io.channels_faulted` on `PLC-S7400-TBB` (a counter, not a curve)

1. **Definition** — `plc_s7400.yml`: unsigned count of faulted I/O channels
   (OB82 diagnostic-interrupt data in production), `15s`, bands
   [0,0] / [1,2] / [8,32], **jitter 0**. Triggers ≥1 warning (a single 4–20 mA
   loop break — wire break or moisture short), ≥8 high (mass I/O loss).
2. **Load** — same path; note jitter 0 marks it as a *counter* to the
   continuity feature.
3. **Provision** — item value_type 3 (unsigned) — the sampler will emit ints.
4. **Simulate** — in good it emits exactly 0 every 1.5 s of wall time. On a
   transition to underperform it lands on, say, 2 — and with continuity on it
   **holds at 2**, tick after tick, exactly like a real fault count that
   registered one event and stays until the electrician fixes the loop. (The
   baseline sampler, by contrast, would bounce 1-2-1-2 across the band — the
   flicker the counter rule exists to kill.) Correlation: if it reaches
   `failed`, the `io_fault_storm` group biases `plc.diag.event_rate` on the
   same PLC toward underperform — flapping I/O floods the diagnostic buffer,
   which is how a real S7-400 presents it (the CPU logs each OB82 event; the
   event-*rate* is the actionable scalar).
5. **Ingest/Observe** — `"2"` is accepted for the unsigned item; the ≥1
   warning problem stays open continuously while the count holds — matching
   how an operator experiences a standing wire-break fault, one problem, not
   a strobe.

### 7.3 Switch — `net.if.error_rate` on `SW-BJN-IE01` (environment → integrity)

1. **Definition** — `switch_router.yml`: unsigned errors/interval (IF-MIB
   `ifInErrors` delta in production), `1m`, bands [0,0] / [1,40] /
   [300,2000]. The healthy band is **zero** — on a stable industrial link the
   norm really is zero CRC errors, and any sustained non-zero count is
   already investigation-worthy (hence ≥1 warning), with ≥300 as
   integrity-lost (high).
2. **Load/Provision** — as above; enum sibling `net.if.oper_status` is the
   two-state up/down param (no underperform — §3.4).
3. **Simulate** — 0, 0, 0… until either an independent excursion or the
   `switch_thermal` cascade: `net.env.fan_state` hits `failed` (critical fan)
   and biases error_rate and discards toward underperform — modeling an
   overheating ASIC/PHY starting to corrupt frames, EMI-like symptoms without
   EMI. Counter rule holds each value between events.
4. **Ingest/Observe** — the warning problem "CRC/FCS errors climbing"
   correlates visibly, on the same host and timeline, with the fan-critical
   problem — the kind of cross-signal pattern the later clustering phase is
   supposed to discover from history.

### 7.4 Process — `proc.comp.bearing_temp` on `PROC-GRS` (the lube-starvation chain)

1. **Definition** — `gas_process.yml`: float °C RTD, `5s`, bands
   45–75 / 76–95 / 96–120 (journal bearing, trip ~100 °C), jitter 0.3
   (bearing metal + oil film smooth the signal; fast rises are the ramp's
   job, not noise). Triggers ≥80 warning, ≥100 high (the trip).
2. **Load/Provision** — standard; `{$P_OUT_SP}` and friends land as host
   macros from the site row.
3. **Simulate** — the interesting path starts elsewhere:
   `proc.comp.lube_oil_pressure` fails (oil pump dies, header pressure to
   ~1 barg). The `lube_starvation` group forces bearing_temp toward `failed`
   with p=0.7 — and the 900 s ramp (90 s wall) matches the physics: a
   starved journal bearing cooks in minutes, not hours. Meanwhile the second
   affect biases `proc.comp.vibration` toward underperform (600 s ramp —
   near-step, as ISO 10816 zone changes are), and the `bearing_vibration`
   group can chain further: hot bearing → more vibration. Three problems
   open in causal order on one host: lube LOW-LOW (high) → bearing trip
   (high) → vibration Zone C (warning).
4. **Backfill note** — run `make backfill` first and this same chain exists
   *in history*, timestamps at real 5 s spacing: a training set where the
   cause precedes the effects by physically-plausible lags.
5. **Observe** — Problems shows the cascade; Latest data shows flow/pressure
   still hovering at their setpoints (unaffected params keep their PID hover
   — degradation is *selective*, another statistical property real datasets
   have and uniform-random mocks don't).

### 7.5 Weather — `bmkg.temp` on `BMKG-STATION` → `proc.comp.bearing_temp` (`omega` mode)

1. **Definition** — `catalog/bmkg.yml`: float, °C, `30s`, bands 25–29 /
   29–32 / 32–35, one shared host (`BMKG-STATION`, not per-site). `jitter: 0`
   is inert here — see step 3.
2. **Load/Provision** — a normal flat asset class, standard reconcile; no
   discovery, no `host_template`.
3. **Simulate** — the interesting part: `process_stream` intercepts
   `bmkg.temp` before the state machine ever runs. `WeatherNode.get_weather`
   computes temperature from `clock` alone (a seasonal cosine × an asymmetric
   diurnal curve), and `_band_idx_for_value` maps it onto the catalog's bands
   so the rest of the pipeline can read a band exactly like any other stream.
   On a hot dry-season afternoon that lands in `failed`. `omega.yml`'s
   `ambient_heat_cooling_stress` group reads that band and, with p=0.4,
   biases `proc.comp.bearing_temp` — on **every** site's PROC host at once,
   the one correlation group in this catalog that crosses hosts (§5.3) —
   toward `underperform`, ramping over the mode's shortened 600 s window
   (heat-stressed cooling degrades faster than the catalog's default fault
   ramp).
4. **Backfill note** — `WeatherNode.get_weather` is a pure function of the
   timestamp, so `make backfill` and the live stream that follows it agree on
   "what the weather was" at the join, with no re-run drift.
5. **Observe** — Latest data on `BMKG-STATION` shows a smooth diurnal/
   seasonal curve (no jitter, no scatter — it's physics, not a random walk);
   Problems on GRS/TBB/BJN's PROC hosts light up together on a hot day,
   correlated across sites by one shared cause instead of three independent
   coincidences.

---

## 8. Testing & validation

Four layers, all offline (no Zabbix required), all fast:

1. **`make check`** (`cmd_check`) — parses the whole catalog, then runs the
   *baseline* generator 500× per parameter asserting: values in band (with
   jitter margin), correct Python type for the value_type (ints for
   unsigned), enum values exact, triggers well-formed. Then loads the active
   `sim_config.yml` and `validate()`s it against the catalog: every
   referenced param key and band must exist. ~20 000 samples in a second or
   two. This is the pre-flight gate: run it after any YAML edit, before
   touching Zabbix.
2. **`tests/test_simulate.py`, `tests/test_config.py`, `tests/test_provision.py`,
   `tests/test_weather.py`** — pytest (`make test` / `.venv/bin/pytest tests/ -q`), one small test per
   load-bearing claim, using `pytest.raises`/`unittest.mock` in place of the
   hand-rolled fakes the suite used before it moved under `tests/`. The
   roster: the **disabled == legacy** invariant (seeded RNG,
   byte-identical stream — the realism layer's contract); correlation
   measurably lifts the biased state's rate; trend produces an intermediate
   below-band value (ramp, not step) and progresses; **ramp → walk handoff**
   (after the ramp expires the walk takes over in-band); continuity walks
   in small steps where the legacy path teleports; jitter-0 counters hold
   exactly; reversion converges to the setpoint and time-of-day shifts it;
   dropout at p=0/p=1; the midnight-wrapping peak window and the shoulder
   blend (edge = midpoint, monotone, wrap-safe, `shoulder_hours: 0` = the old
   hard step); `validate()` rejects bad param/band references *and* dead
   config (a trend override or ToD profile on an enum param is a silent
   no-op, so it's an error); every shipped preset validates against the real
   catalog under those strict rules (a typo in a preset fails in CI-style,
   not at demo time); the catalog loader's guards (zero intervals, zero
   weights, enum band collisions, unknown trigger fields, short weight
   lists); the backfill bucket scheduler fires *exactly* the expected event
   count (no drops, no duplicates) with a faked-out sender; the ETA formatter;
   and that a rejected `item`/`trigger`/`host`/geomap call is recorded into
   `Provisioner.errors` rather than raised, against a minimal fake API (proof
   that one bad object can't abort the objects behind it — §4.3.1).
   `test_weather.py` additionally pins: `WeatherNode.get_weather` determinism
   across instances and ranges; the 7-day dust lookback actually reaches the
   `failed` band under realistic conditions (a shorter, since-fixed lookback
   silently couldn't); `process_stream` bypasses the state machine for
   `bmkg.*` keys and ignores `now` in favor of `clock`; `correlation_forces`
   resolves a `bmkg.*` trigger across every host that has a matching
   `affects` param, while every other trigger's same-host scoping is
   unchanged (§5.3); and `presets/omega.yml` validates against the real
   catalog.
3. **Provision-time validation** — `make config` re-validates a preset
   before activating it; `provision` itself only ever sees a catalog that
   already parsed. The reconciler's happy path (create, update, prune,
   idempotent re-run) was verified against the live stack manually (§4.4);
   its failure-isolation path was verified both with a fake API (above) and
   live, by feeding it a trigger with a deliberately invalid function and
   confirming the other three asset classes still reconciled.
4. **`test_extract.py`** — same assert-only style, covers the `extract`
   CLI's (§13.2) pure pieces without a live connection: relative/absolute/
   `now` date parsing and the `--from < --to` guard; column-name validation;
   the `history.get` vs `trend.get` decision (numeric-vs-not, short-vs-long
   range, `--aggregate hourly` override, and the "no trends on this type"
   fallback); and the cursor-pagination helper against a faked-out `*.get`
   method, proving a >`_BATCH`-row pull returns every row exactly once. The
   live paths (`sla.get`/`sla.getsli`/`history.get`/`trend.get` against a
   real stack) are explicitly out of scope for this file — verify those
   manually per [docs/extract-cli.md](docs/extract-cli.md).

`test_extract.py` (§4, item 4 above) is the one holdout still on the old
assert-only/no-framework style, since it belongs to the `extract` CLI work
tracked separately from this migration.

---

## 9. Operational workflows

**First run** — `cp .env.example .env && make venv && make up`, wait for the
first-boot DB import (~30–60 s, `make logs` to watch), `make check`,
`make provision`, optionally `make config MODE=realistic && make backfill`,
then `make simulate`. UI at http://localhost:8080, `Admin`/`zabbix`.

**Add a station** — one line in `sites.yml` (code, name, lat/lon, city,
grade, p_out_sp) → `make provision`. Four new hosts appear (PLC, HMI, switch,
process), on the geomap, streaming on the next `make simulate` tick. Remove a
station by deleting its line — its hosts are pruned on the next provision.

**Edit a parameter** — edit the catalog file → `make check` (offline typo
gate) → `make provision` (reconciles: new objects created, changed fields
updated, removed objects pruned) → `make simulate` picks it up automatically.
Remember: renaming a `key`/trigger `label` is delete-plus-create (history
lost); changing thresholds/units/severity is an in-place update.

**Switch sim modes** — `make config MODE=<name>` (validated before
activation), then `make simulate`. To also rebuild history under the new
mode: `make backfill` (or `make backfill DAYS=7 SPEED=2000`) — it replays the
active mode over its window, so history and live stream match.

**Reset** — `make down` stops containers, keeps data. `make clean` wipes the
DB volume: full factory reset, re-run first-boot + provision after.

**Rebuild dashboards after a reset** — `make clean` wipes hand-built
dashboards along with everything else. `make export-dashboards` (before the
reset) snapshots them to `dashboard/*.json`; after `make provision` rebuilds
the catalog, `make import-dashboards` recreates them with ids re-resolved by
name (§13.1).

**Pull data back out** — `make extract ARGS="sla --from 7d --to now"` for the
comm-link SLA report, `make extract ARGS="table --host-group '...' --key
'...'"` for a generic history/trend export. Read-only, no provisioning
required beyond having data to pull (§13.2).

**Troubleshoot** — the short table in [RUNNING.md §7](RUNNING.md) maps
symptoms to causes; the two most common: `provision` connection errors (stack
still importing the DB — the CLI now prints exactly this hint) and
`failed > 0` in the simulate heartbeat (values sent for items that don't
exist yet — run `make provision` first).

---

## 10. Design decisions & alternatives considered

The judgment behind the shape of the thing — including what changed during
the engineering audit and why.

### 10.1 One catalog, two consumers (vs. separate configs)

The alternative — a Zabbix config *and* a simulator config — is how these
labs usually get built, and it drifts within a week. The single catalog
forces every parameter to answer both questions (how to monitor, how to fake)
in one place, and makes drift structurally impossible rather than
procedurally discouraged. The cost is a slightly awkward marriage of concerns
in one YAML block; the `sim:` sub-block keeps the simulator's share fenced.

### 10.2 Trapper for everything (vs. mixed item types now)

The lab could provision "real" types (SNMP, Agent) per parameter today —
they'd just collect nothing without real endpoints. Trapper-everywhere means
one push protocol stands in for Node-RED, Agent 2, and the SNMP pollers at
once, and every parameter's *real* collection path is recorded in
`collection` for the swap. The item type is the only field production
changes.

### 10.3 Provisioning: create-if-absent → full reconcile

The original provisioner only created missing objects. That made the
documented edit workflow a trap: change a unit, a name, or a trigger severity
and re-provision — and nothing happens, silently; rename a trigger label and
the old trigger lives forever alongside the new one. Both violate "the
catalog is the single source of truth" in the way that matters most —
*silently*. The audit rebuilt it as a reconciler (§4): diff the managed
fields, update in place, prune template strays (triggers before items,
because item deletion cascades). Alternatives considered: full delete-and-
recreate each run (simple, but destroys history every time — unacceptable);
tracking managed objects by tag instead of by group/template scope (cleaner
in theory, but more API surface for the same guarantee at this scale).

### 10.4 A sticky Markov chain (vs. independent draws, vs. scripted scenarios)

Independent per-tick draws: flicker, no dwell, useless for ML. Fully scripted
failure scenarios: maximum realism per scenario, but each scenario is bespoke
work and the data covers only what you scripted. The sticky chain is the
minimal mechanism that yields *emergent*, endlessly-varied, statistically
steerable degradation stories from three numbers per parameter — and the
realism layer then adds the physics the chain alone can't express.

### 10.5 The realism layer as seven orthogonal no-op-by-default features

The alternative was a "version 2 simulator". Rejected because: (a) each
feature maps to one named deficiency of the baseline (teleporting values,
independence, step transitions, clock-blindness, gapless data, one-size MTTR,
forward-only history) and can be reasoned about — and tested — alone; (b) the
byte-identical-when-disabled invariant means the baseline remains the
reference implementation forever, so every feature is *provably* additive;
(c) presets compose the features into intents (demo vs. ml vs. stress)
without code changes. The cost is a per-tick pipeline with documented
precedence rather than one sampling formula — priced in, and paid once, in
`process_stream`/`sample_stream`.

### 10.6 Known modeling simplifications (deliberate, documented)

- **Stickiness before weights** means the weights steer the long-run
  distribution but aren't exactly it (the re-roll can re-select the current
  state; rarer states get slightly more mass than the raw weights suggest).
  Exact stationary targeting would need a proper transition matrix per
  parameter — more math for no observable demo/ML benefit at these
  magnitudes.
- **Correlation strength is per due tick**, so a fast-interval affected param
  feels a persistent trigger more often than a slow one. Real cascades are
  also faster on fast dynamics, so the artifact points the right way.
- **The ramp is linear**; real thermal curves are exponential. At these
  timescales (and behind jitter) the difference is invisible; the ramp
  *duration* carries the physics instead.
- **`SIM_TIME_SCALE` compresses live time but backfill uses real intervals**
  — deliberate: live is for watching, history is for training, and training
  data must have physically-correct spacing.
- **The diurnal temperature curve is an asymmetric double-cosine, not full
  Parton & Logan solar geometry** — the real model needs latitude and
  day-length inputs this mock generator doesn't carry; the double-cosine
  hits the same two anchors (05:00 min, 14:00 max) with the same fast-rise/
  slow-fall asymmetry, which is what a mock generator needs, not sunrise-
  accurate physics.
- **`omega`'s weather correlations target the closest *real* catalog keys,
  not literal HVAC/solar assets** — this catalog is a gas-transmission SCADA
  site with no such asset classes; inventing fake ones would fail `make
  config`'s own validation. Full substitution rationale: `presets/omega.yml`'s
  header comment and [weather-engine.md](docs/weather-engine.md).

### 10.7 Not built, on purpose (YAGNI with a map)

- **`nodata()` triggers** — dropout already creates the gaps; alerting on
  them needs windowed trigger functions, which the expression builder
  (`func(/tmpl/key) op value`) can't express. The extension is small (a
  `window` field on triggers) and becomes worth it the moment gap-alerting is
  a demo requirement, not before.
- **Per-channel PLC I/O via LLD** — the catalog monitors the fault *count*
  (the actionable scalar); Zabbix low-level discovery could expand to
  per-channel items when a real S7 bridge exists to feed it.
- **Item history re-typing / template re-linking on rename** — renames are
  delete-plus-create (§4.4); a migration path matters only when there's
  production history worth preserving.
- **A web UI / scenario editor** — the YAML *is* the interface; it's
  reviewable, diffable, and already validated at three layers.

---

## 11. Live demo script

```bash
cp .env.example .env        # central config
make venv                   # python env
make up                     # start Zabbix — first boot imports the DB (~30-60s)
make config MODE=realistic  # pick the sim mode (validated against the catalog)
make check                  # OFFLINE proof: catalog + generator + sim-config sane
make provision              # build all Zabbix config from the catalog (idempotent)
make backfill DAYS=7 SPEED=5000   # 7 days of history so graphs have depth
make simulate               # stream live (Ctrl+C to stop)
```

At **http://localhost:8080** (`Admin` / `zabbix`):

1. **Monitoring → Latest data** — filter by host group; walk one asset class.
   Point at a value drifting around its setpoint: "that's a PID loop, not a
   random number generator."
2. **Monitoring → Problems** — let it run; problems open and clear. Severity
   maps to the health model: warning/average = Underperform, high/disaster =
   Failed.
3. **Monitoring → Geomap** — the SSWJ chain across Sumatra–Java, pins colored
   by worst problem.
4. **Data collection → Templates** — open any item, show the description:
   FMEA component, real collection path, source. "The tool documents itself."

**Two closers:**

- *Single source of truth:* add one line to `sites.yml`, `make provision` —
  four new hosts appear on the map and start streaming. One line.
- *Realism:* with `realistic` active, kill time until (or backfill so) a fan
  fails — watch `hmi.cpu.temp` ramp after it on the same host, cross the
  warning threshold mid-ramp, and settle into the degraded band. Then
  `make config MODE=baseline && make simulate` to show the teleporting
  reference for contrast — the difference *is* the pitch for the realism
  layer.

---

## 12. Q&A — likely questions, crisp answers

**"Why simulate instead of using real equipment?"** No plant is wired up yet;
the deliverable is a validated monitoring *design*. The config plane is
production-identical, so going live is an item-type change per parameter, not
a rebuild.

**"Why Zabbix?"** The recommended platform in the discovery report: real
trigger engine, templates (config-as-code), a push protocol (Trapper) that
matches the Node-RED bridge, native maps/inventory, and an API that covers
everything the UI can do.

**"Why Trapper for everything in the lab?"** One push protocol stands in for
all production collectors at once (Node-RED S7comm bridge, Agent 2, SNMP
pollers), and it's the same wire protocol `zabbix_sender` speaks. The real
collection path per parameter is recorded in its `collection` field.

**"Why can't the PLC CPU be read over SNMP?"** The CP443-1 exposes only the
comms layer over SNMP. CPU mode, the diagnostic buffer, and per-channel I/O
live in S7comm SZL lists (`0x0424`, `0x00A0`) — that constraint is why the
middleware bridge exists, and it's encoded in each PLC parameter's
`collection` field.

**"What makes `realistic` realistic?"** Three researched properties of plant
telemetry: process variables are PID-held at setpoints (continuity's
mean-reversion, AR(1)-like autocorrelation ≈ 0.9); throughput is diurnal
(time-of-day shifting the setpoint, 06–22 peak tracking the power-generation
demand curve); faults are causal and selective (per-host correlation chains;
unaffected params keep hovering). Plus physically-timed ramps — bearing
cook-off in minutes, enclosure heat-up in ~20 — and a few percent of genuine
data gaps.

**"Doesn't the realism layer change the production story?"** No — it's
data-plane only. Items, triggers, templates are untouched by it, and with
everything disabled the simulator is byte-identical to the plain machine
(asserted in `tests/test_simulate.py` against a seeded RNG).

**"How do modes and backfill relate?"** `make config MODE=x` activates a
validated preset; `make backfill` — always manual, never automatic — replays
the *active* mode over that mode's past window, so history and live stream
share one statistical signature.

**"How does backfill get old timestamps in?"** Trapper's per-value `clock`.
The sweep is discrete-event at each parameter's *real* interval (no time
compression), so historical spacing is physically correct; `SPEED` only
controls generation pace.

**"How is correlation not just noise?"** It's causal (reads the trigger's
pre-roll band), per-host, and directional (named trigger → named affected,
tunable strength). `tests/test_simulate.py` shows it measurably raises the affected
parameter's biased-state rate over the independent baseline.

**"What stops a bad config from corrupting Zabbix?"** Three validation
layers, all before any API call or send: catalog at load (§3.6), sim-config
ranges at load, cross-references at `make check`/`make config`. A preset
typo fails in `tests/test_config.py` before it can fail in a demo.

**"What happens if I edit or remove a parameter?"** Provision reconciles:
changed fields update in place, removed objects are pruned (triggers before
items), renames are delete-plus-create. Removed *sites* have their hosts
pruned, scoped to catalog-owned host groups only.

**"Where does ML training data come from?"** `make config MODE=ml && make
backfill` (30 days by default): long ramps, full causal web, zero dropout,
complete labelled curves — states are known at every tick, so the
Good/Underperform/Failed labels come free with the series.

**"What's deliberately missing?"** `nodata()` gap-alerting (schema can't
express windowed functions yet — §10.7), per-channel I/O discovery, and any
scenario scripting. Each has a named trigger condition for when it becomes
worth building.

---

## 13. Auxiliary CLI tools: dashboards & extract

Two small tools sit alongside provision/simulate/catalog, both strictly
**read-adjacent** to the core system: neither owns any catalog data or
Zabbix config-plane object, so neither is in `provision.py`'s or
`simulate.py`'s MECE split (§2).

### 13.1 Dashboard export/import (`otobs/dashboard.py`)

The comm-link Services/SLA object and the workstation/network/SLA dashboards
are built **by hand** in the Zabbix UI ([docs/comm-links-sla.md](docs/comm-links-sla.md)) —
there's nothing in the catalog to provision them from. `make clean` (`docker compose
down -v`) deletes the DB volume, and with it every hand-built dashboard.
`export_all()`/`import_all()` make that survivable:

- **Export** (`api.dashboard.get`) writes one JSON file per dashboard to
  `dashboard/*.json`, plus `dashboard/_refs.json` — every hard object id a
  widget field points at (host group/host/item/SLA), resolved to its
  **name** at export time. Ids are meaningless across a `make clean` +
  reprovision cycle (Zabbix hands out fresh ones); names are stable.
- **Import** re-resolves each name in `_refs.json` back to whatever id it
  currently has (`_remap`), rewrites every widget field, and
  `api.dashboard.create`s the recreated payload. A field whose name no
  longer resolves (catalog changed since export) is left as the old,
  now-dead id and counted in a summary — not silently dropped, not a hard
  failure either, since one stale widget field shouldn't block recreating
  the other 41.
- Both are wired the same way as every other Zabbix-touching command:
  `zabbix_utils.ZabbixAPI` login via `settings.py`, the same
  connection-error hint text as `provision.py`, `make export-dashboards` /
  `make import-dashboards`.

Workflow: §9 "Rebuild dashboards after a reset".

### 13.2 Extract (`otobs/extract.py`)

The **read-only** counterpart to provision (config plane, writes) and
simulate (data plane, writes): `python -m otobs extract sla|table` pulls
data **out** of Zabbix into CSV/JSON/table files. Every call is a `*.get`
(`sla.getsli` for the SLA report body) — no `*.create`/`*.update`/`*.delete`
anywhere in the module. Full reference: [docs/extract-cli.md](docs/extract-cli.md).

**One engine, two subcommands** (mirrors `dashboard.py`'s
export/import-share-helpers shape, not a multi-file split): date-range
parsing (`resolve_range`, accepting `now`/`Nd`/`Nh`/absolute ISO), the
Zabbix connect helper, a cursor-paginated `history.get`/`trend.get` fetcher,
and the csv/json/table writer are all shared.

- **`extract sla`** — resolves the one comm-link SLA object (or requires
  `--sla-name` if several exist), calls `sla.getsli` over the requested
  window, resolves service ids to names via `service.get`, and emits one row
  per service × period (SLA %, uptime, downtime, excluded time). `--period`
  is a sanity check only, not a passthrough — Zabbix computes `getsli`'s
  granularity from the SLA object's own configured `period`; a mismatch
  prints a note instead of silently reporting the wrong thing.
- **`extract table`** — filters items by host group / host / a `key_`
  pattern (native Zabbix `search` + `searchWildcardsEnabled`, no hand-rolled
  glob) / tag, then auto-picks `history.get` (full precision) vs
  `trend.get` (hourly aggregate, numeric items only) per value-type group:
  `--aggregate hourly`, or a range past a 7-day threshold, switches to
  trend; the choice and its reason are always printed, never silent.
  `--columns` selects from a small fixed vocabulary (`timestamp, clock,
  host, item, key, value, units, value_type`).
- **Pagination**: `history.get`/`trend.get` have no offset pagination, so
  the fetcher cursors on `clock` in pages of 5000, advancing to
  `last_clock + 1` between pages — `# ponytail:`-documented as safe at this
  lab's scale (catalog intervals ≥5s, a few hundred streams; no clock-second
  ever produces >5000 rows) and not safe at arbitrary scale (would need a
  `(clock, ns)` cursor).
- Same connection-error hint pattern as `provision.py`/`dashboard.py`; zero
  matching rows prints an explicit message instead of writing an empty file.

Tested offline in `test_extract.py` (§8, layer 4); the live API paths are
manually verified, not covered by the automated suite (§8).

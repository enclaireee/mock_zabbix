# Catalog schema

Each `*.yml` in this directory defines one **asset class**. It is the single
source of truth consumed by `otobs.provision` (Zabbix config) and
`otobs.simulate` (mock data).

> Two files here are **not** asset classes and are loaded separately:
> `sites.yml` (the station registry, below) and `sim_config.yml` (the active
> simulation realism layer — continuity, correlation, trend, time-of-day, dropout,
> backfill — swapped by `make config MODE=…`; see
> [docs/sim-config.md](../docs/sim-config.md)). `sim_config.yml` is orthogonal to
> the per-parameter `sim:` block described below and does not replace it.

## Station registry (`sites.yml`)

Physical stations are defined **once** in `sites.yml`; each asset class generates
one host per site. Adding a station here is the only edit needed for "more rows".

```yaml
sites:
  - { code: MKR, name: "Muara Karang", location: "...", lat: "-6.1086", lon: "106.7686",
      city: "Jakarta Utara", grade: enterprise, p_out_sp: "12" }
```

`location` / `lat` / `lon` / `city` become Zabbix host inventory (drives the geomap).
`grade`, `p_out_sp` (and any field you add) are referenced by `host_template` macros.

## Top level

```yaml
asset_class: "Workstation / HMI"     # human label
host_group: "IT/HMI"                  # Zabbix host group
template_name: "Template OT ..."      # Zabbix template (items+triggers live here)
template_group: "Templates/OT"        # Zabbix template group
# Generate one host per site from the registry (preferred):
host_template:
  tech: "HMI-{code}-WW01"            # technical name; {field} = site field
  name: "{name} — Operator Station"
  macros: { "{$SITE}": "{code}", "{$GRADE}": "{grade}" }
parameters: [ ... ]                    # the metrics (see below)
```

Instead of `host_template` you may still hand-list `hosts:` (each with `host`,
`name`, `macros`, optional `inventory`) — useful for one-off hosts that aren't a
station. Inventory for generated hosts is filled from the site automatically.

## A parameter

```yaml
- key: "hmi.cpu.temp"            # Zabbix item key (also the trapper key)
  name: "CPU Core Temperature"
  value_type: float              # float | unsigned | text | char | log
  units: "°C"
  interval: "1m"                 # expected cadence: 15s|30s|1m|5m|1h ...
  component: "E. Temperature"    # FMEA subsystem (from the report)
  collection: "custom (LHM WMI / IPMI OEM)"   # how it's gathered in production
  failure_mode: "Dried thermal paste, AC failure"
  source: "Zabbix WMI + LibreHardwareMonitor, 2024"
  sim: { ... }                   # how the simulator generates it
  triggers: [ ... ]              # Good/Underperform/Failed alerting
```

`component`, `collection`, `failure_mode`, `source`, `interval` are embedded into
the Zabbix item **description** — living documentation inside the tool.

## `sim` — two kinds

**numeric** — three value bands; by default the simulator samples uniformly within
the current band plus `jitter` (with the `continuity` mode feature on, it instead
walks from the last reading by `jitter` — see [docs/sim-config.md](../docs/sim-config.md)):

```yaml
sim:
  kind: numeric
  good: [40, 60]
  underperform: [66, 84]
  failed: [86, 99]
  weights: [good, underperform, failed]   # tokens -> default probs, or numbers
  jitter: 1.5
```

For `value_type: unsigned`, samples are rounded to integers automatically.

**enum** — discrete states, each with a band weight and a fixed value:

```yaml
sim:
  kind: enum
  states:
    - { value: 8,  weight: good,         label: "RUN (0x08)" }
    - { value: 6,  weight: underperform, label: "START-UP" }
    - { value: 13, weight: failed,       label: "DEFECT" }
```

`weight` is either a number or one of `good` / `underperform` / `failed`
(mapped to the defaults in `otobs/catalog.py`: 0.90 / 0.08 / 0.02). Values are
normalized per parameter. The simulator is **sticky** (`SIM_STICKINESS` in
`.env`): it tends to stay in its current state, producing smooth degradation
stretches instead of flicker.

## `triggers`

Each trigger becomes a Zabbix trigger on the template:

```yaml
triggers:
  - { op: ">=", value: 65, severity: warning, label: "Thermal degradation" }
  - { op: ">=", value: 85, severity: high,    label: "Thermal throttling" }
```

- `op`: `=  <>  >  >=  <  <=`
- `severity`: `info | warning | average | high | disaster`
- `func` (optional, default `last`): the Zabbix history function. The generated
  expression takes no argument window (`func(/tmpl/key)`), so only zero-argument
  functions like `last` work; windowed ones (`avg(…,1h)`, `nodata(…,5m)`) would
  need a schema extension.

Generated expression: `last(/<template_name>/<key>) <op> <value>`. Two-sided
limits (e.g. PSU brownout + overvoltage) are just two trigger entries.

## `discovery` — simulated Low-Level Discovery (LLD)

Optional, one per asset class (only `switch_router.yml` uses it). It turns
selected flat parameters into **per-instance item/trigger prototypes** under a
discovery rule — the same mechanism Zabbix uses to auto-create one item per
switch port, disk, or CPU core.

**This is a lab simulation of SNMP LLD, not a real SNMP walk.** On real hardware
the rule would SNMP-walk IF-MIB to enumerate interfaces; here there is no switch,
so the simulator pushes the port list to the rule's key over **trapper** (a
`{"data":[{"{#IFNAME}":"Gi1/0/1"}, …]}` payload), and the server materializes one
item per port from each prototype — see `docs/architecture.md` on collectability.

```yaml
discovery:
  key: "net.if.discovery"                 # the LLD rule's trapper key
  name: "Interface discovery (IF-MIB, LLD)"
  macro: "{#IFNAME}"                       # LLD macro (default {#IFNAME})
  prototypes: ["net.if.oper_status", "net.if.error_rate"]  # which params are per-port
  ports: ["Gi1/0/1", "Gi1/0/2", "Gi1/0/8"]                # the simulated walk result
```

- `prototypes` lists existing parameter **keys** (from `parameters:` above). Each
  named param becomes an item prototype keyed `<key>[{#IFNAME}]` plus a trigger
  prototype per trigger; its `sim`/`triggers`/`value_type` are reused unchanged —
  one state machine definition drives every port. **Any param not listed stays a
  flat per-host item** (e.g. chassis-level `net.env.fan_state`).
- `ports` is the stand-in for the SNMP walk: a non-empty, unique list of instance
  names. The simulator runs one independent state machine per (host, port).
- Validation is load-time (`otobs/catalog.py`): missing fields, empty/duplicate
  ports, a `prototypes` entry that isn't a real param key, a `macro` not shaped
  `{#…}`, or a `key` that collides with a param key all fail at load.

## `segments:` / `circuits:` — the comm-link SLA schema

One catalog file (`comm_links.yml`) uses this **instead of** `parameters:`. It
models the two-layer communication-link system: physical media that fail as a
unit, and the logical circuits that ride them. Full rationale:
[docs/comm-links-sla.md](../docs/comm-links-sla.md).

```yaml
segments:            # physical media (fiber span / MPLS backhaul) — each a Zabbix item
  - key: "seg.fiber_grissik_pgd"
    name: "Segment: Grissik–Pagardewa fiber"
    value_type: unsigned
    # ...all the normal parameter fields (interval/component/collection/…)...
    sim: { kind: enum, states: [ {value: 1, weight: good, ...}, {value: 2, weight: failed, ...} ] }
    triggers: [ { op: "=", value: 2, severity: high, label: "Segment down" } ]

circuits:            # the report's named links — also Zabbix items
  - key: "circ.grissik_pgd"
    name: "Grissik–PGD (Metro-E 4M)"
    media: "Metro-E"                       # transport label (documentation)
    depends_on: [ "seg.fiber_grissik_pgd" ]  # segment key(s) it rides
    value_type: unsigned
    sim: { kind: enum, states: [ ... up/down ... ] }   # weights ignored (state = worst segment)
    triggers: [ { op: "=", value: 2, severity: high, label: "Circuit down" } ]
  - key: "circ.vsat_pgd_mcs"
    media: "VSAT-IP"
    depends_on: []                         # no segment -> simulated independently
    value_type: float
    units: "%"
    collection: "Simple check (icmppingloss)"          # different collection = real constraint
    sim: { kind: numeric, good: [0,1], underperform: [3,20], failed: [60,100], ... }
    triggers: [ { op: ">=", value: 60, severity: high, label: "VSAT link down" } ]
```

- A **segment** is an ordinary parameter (see "A parameter" above); it becomes a
  flat Zabbix item + triggers like any other.
- A **circuit** is a parameter plus two extra fields:
  - `depends_on`: list of segment `key`s it rides. Its simulated state is the
    **worst** of those segments (any segment down ⇒ circuit down) — a hard,
    deterministic dependency resolved by `segment_forces` in the simulator, so
    circuits sharing a span drop together. An **empty** `depends_on` means the
    circuit rolls its own machine independently (VSAT-IP).
  - `media`: the report's transport label, embedded for documentation.
- The circuit's **high/disaster** trigger is auto-tagged `link:<key>` at load
  time; that tag is how the Zabbix SLA service layer (`otobs/sla.py`) attributes
  downtime to the right circuit. You don't write the tag yourself.
- Validation is load-time: a `depends_on` that names an unknown segment key, or a
  segment/circuit list that is empty, fails at load. Segment and circuit `key`s
  share the same catalog-wide uniqueness check as `parameters`.

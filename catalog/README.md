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
- `func` (optional, default `last`): the Zabbix function, e.g. `last`, `avg`.

Generated expression: `last(/<template_name>/<key>) <op> <value>`. Two-sided
limits (e.g. PSU brownout + overvoltage) are just two trigger entries.

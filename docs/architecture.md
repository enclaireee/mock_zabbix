# Architecture

## Data flow

```
                         catalog/*.yml  (single source of truth)
                          /                         \
              otobs.provision                    otobs.simulate
            (Zabbix JSON-RPC API)              (Trapper / zabbix_sender)
                   |                                   |
                   v                                   v
   ┌───────────────────────────────────────────────────────────────┐
   │  Zabbix 7.0 stack (docker compose)                             │
   │                                                                │
   │   zabbix-web (nginx, :8080) ── API + UI                        │
   │   zabbix-server (:10051 trapper) ── triggers, history          │
   │   zabbix-db (postgres 16)        ── config + history store     │
   │   zabbix-agent2                  ── monitors the lab host      │
   └───────────────────────────────────────────────────────────────┘
```

`provision` builds the **config plane** (templates, items, triggers, hosts).
`simulate` feeds the **data plane** (metric values) via the Zabbix Trapper
protocol — exactly the push path the report recommends for a Node-RED bridge
(`node-red-contrib-zabbix-sender`).

The Docker stack is **real Zabbix**, not a mock. `docker-compose.yml` runs the
four official 7.0 images above plus Postgres 16, wired through `.env`, with a
DB healthcheck so the server waits for the DB. A named volume `zbx_db_data`
means `make down` keeps data; only `make clean` (`down -v`) wipes it. Install
and run commands: [RUNNING.md](../RUNNING.md).

## Mapping to the real OT architecture

The simulator is a stand-in. In production each item's `collection` field names
the real collector:

| Lab (this repo)            | Production equivalent                              |
|----------------------------|----------------------------------------------------|
| `simulate` Trapper push    | Node-RED S7comm flow → `zabbix-sender` (Trapper)   |
| `simulate` Trapper push    | Zabbix Agent 2 (CPU/RAM/disk/SMART/NIC)            |
| `simulate` Trapper push    | LibreHardwareMonitor → WMI → Zabbix Agent          |
| `simulate` Trapper push    | SNMP poller (IF-MIB, CISCO-ENVMON-MIB)             |

Because the **config plane is identical**, swapping a mock for a real collector
is just changing the item type (Trapper → Agent/SNMP) on the template — the
keys, triggers, and dashboards stay put.

### Collectability per asset class

The catalog is honest about collectability (per the report's "technical
honesty"): each parameter records its real-world `collection` method (baked
into the live Zabbix item description).

| Asset | Native in Zabbix | Needs middleware |
|-------|------------------|------------------|
| Gas Process (SCADA) | — | all process tags (pressure, flow, gas quality, valves, F&G, compressor) — **S7comm via Node-RED**, the same tags the HMI reads off the PLC |
| PLC S7-400 | CP reachability (SNMP/ICMP) | CPU mode, diag buffer, I/O channels, rack — **S7comm via Node-RED** |
| Workstation/HMI | CPU/RAM/disk/SMART/NIC (Agent 2) | fan RPM, PSU rails, CPU temp — **LibreHardwareMonitor WMI / iDRAC/iLO** |
| Switch/Router | everything (SNMP IF-MIB / ENVMON-MIB) | — |

### Why CPU data isn't SNMP

The report's central constraint: the Siemens **CP443-1** comms module exposes
SNMP (MIB-II, LLDP, AUTOMATION-SYSTEM-MIB) but **cannot see CPU memory**, the
diagnostic buffer, or per-channel I/O. Those require **S7comm** (RFC 1006, TCP
102) reading the SZL system status lists (`0x0424` operating mode, `0x00A0`
diagnostic buffer). That's why those parameters are tagged *needs middleware*
and modeled here as Trapper items, while `plc.cp.icmp_latency` is the only
natively-collectable PLC metric.

## Condition model: Good / Underperform / Failed

Each parameter is a sticky state machine over three bands:

- **Good** — within manufacturer spec, no action.
- **Underperform** — wear / partial / transient degradation; still operating.
  This is the high-value signal: the smooth Underperform curves are the training
  data for Tahap 2 (clustering) and Tahap 3 (predictive maintenance / RUL).
- **Failed** — primary function lost; the high-severity triggers fire here.

`SIM_STICKINESS` (0.92) controls how long a parameter dwells in a state;
`SIM_TIME_SCALE` (default 10×) compresses the catalog intervals so a 1h SMART
metric updates every ~6 minutes in the lab. Both live in `.env` — the global
calibration knobs for the mock plant.

## Realism layer — `catalog/sim_config.yml`

The bare state machine is independent, memoryless-within-a-band, and always-on.
An optional layer (`otobs/sim_config.py` → `catalog/sim_config.yml`) makes it
look more like real telemetry, **each feature independently toggleable and off by
default** (file absent or all `enabled: false` ⇒ identical to the plain machine):

Pick a config with `make config MODE=<name>` (or `FILE=…`); the workflow is
config → (optional) backfill → simulate, and backfill replays the active mode. The
eight modes and six features are documented in [sim-config.md](sim-config.md).

| Feature | Effect on the data plane |
|---|---|
| `continuity` | While a stream stays in a band, the next value is a random walk from the last reading (not a fresh full-band draw) — so values drift and zero-jitter counters hold, instead of teleporting each tick. |
| `correlation` | Per host, when a trigger param is in a given band it biases correlated params' next state toward degrading (stalled fan → rising CPU temp). |
| `trend` | On a state change, ramps from the last value toward a target inside the new band over `ramp_seconds` (respecting `SIM_TIME_SCALE`) — a curve, not a step. |
| `time_of_day` | Scales a value by a peak/off-peak multiplier by local hour (`settings.TIMEZONE`) — operational cycles. |
| `dropout` | Skips a due send with some probability, leaving a genuine gap so `nodata()` triggers fire. |
| `backfill` | `otobs.backfill` mode sweeps the state machine from `now − days` to `now` and pushes each value with its historical `clock`, so graphs have depth immediately. |

This is a **data-plane** change only: the config plane (items, triggers,
templates) is untouched, so the production swap-in story is unaffected. `make
check` validates the file against the catalog. Full schema and semantics:
[sim-config.md](sim-config.md); the state machine itself: [sim-states.md](sim-states.md).

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
metric updates every ~6 minutes in the lab. Both live in `.env` — the
calibration knobs for the mock plant.

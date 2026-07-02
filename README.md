# OT Observability Lab — Zabbix for Gas Transmission Stations

A self-contained lab that runs a **real Zabbix 7.0 server locally** and feeds it
**mock OT/IT telemetry** for the asset classes in the PGNCOM Feature Discovery
catalog (PLC Siemens S7-400, Workstation/HMI, Industrial Switch/Router).

Every parameter cycles through the three condition classes from the report —
**Good → Underperform → Failed** — so you can watch real triggers fire and build
the Underperform time-series that Tahap 2/3 (clustering / predictive maintenance)
will train on.

## Single source of truth

The catalog files in [`catalog/`](catalog/) drive **everything**:
0
```
catalog/*.yml ──┬──> provision  (Zabbix API: templates, items, triggers, hosts)
                └──> simulate    (Trapper push: Good/Underperform/Failed streams)
```

Add a parameter once in YAML and both the monitoring config and the mock data
generator pick it up. No duplicated definitions.

## Quick start

```bash
git clone <this-repo> && cd magang
cp .env.example .env
make venv
make up            # start the Zabbix stack
make provision     # apply the catalog
make simulate      # stream mock telemetry (Ctrl+C to stop)
```

Then open **http://localhost:8080** → login **`Admin` / `zabbix`**.

Full prerequisites, configuration, workflows, and troubleshooting:
see **[RUNNING.md](RUNNING.md)**.

## Documentation map

Each doc has one job; none repeat another's content.

| Doc | Answers |
|-----|---------|
| **[RUNNING.md](RUNNING.md)** | How do I install, run, configure, and troubleshoot this? |
| **[WALKTHROUGH.md](WALKTHROUGH.md)** | What is this, why does it exist, and how do I present/defend it? (narrative, demo script, Q&A) |
| **[docs/architecture.md](docs/architecture.md)** | How do the pieces fit together, and how does the mock map to real OT collectors? |
| **[catalog/README.md](catalog/README.md)** | What's the YAML schema for `catalog/*.yml`? |
| **[docs/sim-states.md](docs/sim-states.md)** | How does the simulator state machine work, and what does `sim_config.yml` do? |
| **[docs/band-weights.md](docs/band-weights.md)** | What do the `good`/`underperform`/`failed` weight tokens mean? |
| **[docs/env-loading.md](docs/env-loading.md)** | How does `.env` get parsed into settings? |
| **[docs/geomap.md](docs/geomap.md)** | How does the Zabbix Geomap widget get wired up? |
| **[docs/provisioning-idempotency.md](docs/provisioning-idempotency.md)** | Why is `make provision` safe to re-run? |
| **[docs/zabbix-codes.md](docs/zabbix-codes.md)** | What integer codes does the Zabbix API expect for types/severities? |

## Layout

```
catalog/            asset-class definitions (the source of truth) + schema docs
  └─ sim_config.yml optional realism layer (correlation/trend/time-of-day/dropout/backfill)
otobs/              python package
  ├─ catalog.py     load + validate catalog/*.yml into typed objects
  ├─ provision.py   Zabbix API: templates, items, triggers, hosts (idempotent)
  ├─ simulate.py    sticky Good/Underperform/Failed state machine → Trapper (+ backfill)
  ├─ sim_config.py  load + validate sim_config.yml into typed objects
  ├─ settings.py    reads .env
  └─ __main__.py    CLI: provision | simulate | backfill | list | check
docs/               architecture + implementation-detail reference docs (see map above)
RUNNING.md          install / run / configure / troubleshoot
WALKTHROUGH.md      narrative tour, demo script, Q&A
docker-compose.yml  the real Zabbix 7.0 stack
.env / .env.example central variables
Makefile            orchestration
```

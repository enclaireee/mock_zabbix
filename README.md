# OT Observability Lab — Zabbix for Gas Transmission Stations

A self-contained lab that runs a **real Zabbix 7.0 server locally** and feeds it
**mock OT/IT telemetry** for the asset classes in the PGNCOM Feature Discovery
catalog (PLC Siemens S7-400, Workstation/HMI, Industrial Switch/Router, Gas
Process) — plus a fifth **communication-link system** that models the 24
inter-station "Media Link Komunikasi" links (fiber segments + logical circuits)
so you can build the Zabbix SLA services and dashboard on top of live link data,
and a sixth **External Environment** layer: a deterministic weather engine
(`otobs/weather_engine.py`) that drives regional temperature/humidity/rain/
lightning/dust and, in the `omega` sim mode, causally correlates it into the
other five asset classes (heat stresses compressor cooling, dust fouls a gas
detector and loads CPU, lightning blips network error rates).

Every parameter cycles through the three condition classes from the report —
**Good → Underperform → Failed** — so you can watch real triggers fire and build
the Underperform time-series that Tahap 2/3 (clustering / predictive maintenance)
will train on.

## Single source of truth

The catalog files in [`catalog/`](catalog/) drive **everything**:

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

Each doc has one job. The focused docs are quick references for a single
topic; WALKTHROUGH.md is the deep version that synthesizes across them.

| Doc | Answers |
|-----|---------|
| **[RUNNING.md](RUNNING.md)** | How do I install, run, configure, and troubleshoot this? |
| **[WALKTHROUGH.md](WALKTHROUGH.md)** | The complete technical reference: every module, schema field, feature, Zabbix concept, and design decision, from first principles — plus the demo script and Q&A. |
| **[docs/architecture.md](docs/architecture.md)** | How do the pieces fit together, and how does the mock map to real OT collectors? |
| **[catalog/README.md](catalog/README.md)** | What's the YAML schema for `catalog/*.yml`? |
| **[docs/sim-states.md](docs/sim-states.md)** | How does the simulator state machine work (states, bands, sticky transitions)? |
| **[docs/sim-config.md](docs/sim-config.md)** | The sim **modes** (`make config`) and the seven `sim_config.yml` realism features, in full. |
| **[docs/weather-engine.md](docs/weather-engine.md)** | The `omega` mode's deterministic weather model, and how it correlates into the other five asset classes. |
| **[docs/band-weights.md](docs/band-weights.md)** | What do the `good`/`underperform`/`failed` weight tokens mean? |
| **[docs/env-loading.md](docs/env-loading.md)** | How does `.env` get parsed into settings? |
| **[docs/geomap.md](docs/geomap.md)** | How does the Zabbix Geomap widget get wired up? |
| **[docs/comm-links-sla.md](docs/comm-links-sla.md)** | The fifth system: comm-link segments/circuits, the shared-fiber dependency, and how to build the Zabbix SLA services/dashboard on top of it yourself. |
| **[docs/extract-cli.md](docs/extract-cli.md)** | How do I pull SLA/history/trend data back out of Zabbix into CSV/JSON? |
| **[docs/provisioning-idempotency.md](docs/provisioning-idempotency.md)** | Why is `make provision` safe to re-run? |
| **[docs/zabbix-codes.md](docs/zabbix-codes.md)** | What integer codes does the Zabbix API expect for types/severities? |

## Layout

```
catalog/            asset-class definitions (the source of truth) + schema docs
  ├─ bmkg.yml       asset class: External Environment (regional weather, one shared station)
  └─ sim_config.yml active realism config (continuity/correlation/trend/time-of-day/dropout/hold/backfill)
presets/            ready-made sim modes copied in by `make config` (baseline/steady/realistic/diurnal/stress/maintenance/demo/ml/omega)
otobs/              python package
  ├─ catalog.py     load + validate catalog/*.yml into typed objects (incl. segments/circuits)
  ├─ provision.py   Zabbix API: templates, items, triggers, hosts (idempotent)
  ├─ simulate.py    sticky Good/Underperform/Failed state machine → Trapper (+ backfill)
  ├─ weather_engine.py  deterministic (timestamp-only) weather model for the `omega` mode
  ├─ sim_config.py  load + validate sim_config.yml into typed objects
  ├─ settings.py    reads .env
  ├─ extract.py     read-only SLA/history/trend export to CSV/JSON/table
  └─ __main__.py    CLI: provision | simulate | backfill | config | list | check | extract
docs/               architecture + implementation-detail reference docs (see map above)
RUNNING.md          install / run / configure / troubleshoot
WALKTHROUGH.md      the complete technical reference (+ demo script, Q&A)
docker-compose.yml  the real Zabbix 7.0 stack
.env / .env.example central variables
Makefile            orchestration
```

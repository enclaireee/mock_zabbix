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

```
catalog/*.yml ──┬──> provision  (Zabbix API: templates, items, triggers, hosts)
                └──> simulate    (Trapper push: Good/Underperform/Failed streams)
```

Add a parameter once in YAML and both the monitoring config and the mock data
generator pick it up. No duplicated definitions.

---

## 1. Prerequisites

- **Docker** + **Docker Compose v2** (`docker compose version`) — runs the Zabbix stack.
- **Python 3.10+** with `venv` — runs the provisioning + simulator tooling.
- Free TCP ports **8080** (web/API) and **10051** (trapper). Change them in `.env` if taken.

## 2. Install

```bash
git clone <this-repo> && cd magang
cp .env.example .env      # central config (safe defaults; see §5)
make venv                 # creates .venv and installs zabbix_utils + pyyaml
```

`make venv` is also a dependency of `provision`/`simulate`/`list`/`check`, so those
targets self-install if you skip this step.

## 3. Run

```bash
make up           # start Zabbix: db, server, web, agent (docker compose up -d)
                  # first boot imports the DB schema — wait ~30-60s
make provision    # create host groups, templates, items, triggers, hosts (idempotent)
make simulate     # stream Good/Underperform/Failed mock data (Ctrl+C to stop)
```

Open **http://localhost:8080** → login **`Admin` / `zabbix`**:

- **Monitoring → Latest data** — live values. Filter by host group `OT/Process`,
  `OT/PLC`, `IT/HMI`, or `Network/Industrial`.
- **Monitoring → Problems** — triggers firing (Underperform = warning/average,
  Failed = high/disaster).
- **Monitoring → Geomap** — every station plotted across Jabodetabek. `provision`
  sets the OpenStreetMap tile provider and each host's inventory lat/lon from
  [`catalog/sites.yml`](catalog/sites.yml) automatically.
- **Data collection → Hosts / Templates** — the provisioned config.

## 4. Make targets

| Target | What it does |
|--------|--------------|
| `make help` | List all targets |
| `make venv` | Create venv + install Python deps |
| `make up` | Start the Zabbix stack |
| `make down` | Stop the stack (**keeps** the DB volume) |
| `make clean` | Stop the stack and **delete** the DB volume (full reset) |
| `make logs` | Tail the Zabbix server logs |
| `make provision` | Apply the catalog to Zabbix (idempotent — re-run after edits) |
| `make simulate` | Stream mock telemetry via Trapper |
| `make list` | Print the parsed catalog (sanity view) |
| `make check` | Offline self-test (catalog + generator), **no Zabbix needed** |

## 5. Configuration

All settings live in **`.env`** (copied from `.env.example`). Both `docker-compose`
and the Python tooling read it. Real environment variables override `.env`.

| Variable | Default | Used by | Meaning |
|----------|---------|---------|---------|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `zabbix` | compose | Backing Postgres credentials |
| `ZBX_WEB_PORT` | `8080` | compose | Host port for the web UI + API |
| `ZBX_TRAPPER_PORT` | `10051` | compose | Host port for the trapper (simulator target) |
| `PHP_TZ` / `ZBX_TIMEZONE` | `Asia/Jakarta` | compose | Frontend timezone |
| `ZBX_API_URL` | `http://127.0.0.1:8080` | provision | Zabbix API base URL |
| `ZBX_API_USER` / `ZBX_API_PASSWORD` | `Admin` / `zabbix` | provision | API login (must match the frontend) |
| `ZBX_SENDER_HOST` / `ZBX_SENDER_PORT` | `127.0.0.1` / `10051` | simulate | Where the simulator pushes trapper data |
| `SIM_STICKINESS` | `0.92` | simulate | Probability a parameter keeps its state each tick. Higher = longer, smoother Good/Underperform/Failed stretches |
| `SIM_TIME_SCALE` | `10.0` | simulate | Time compression. `1.0` = real catalog intervals; `10.0` = a `1h` SMART metric updates every ~6 min |

> If you change `ZBX_WEB_PORT`, update `ZBX_API_URL` to match. If you change
> `ZBX_TRAPPER_PORT`, update `ZBX_SENDER_PORT` to match.

### Adding a station (more host rows)

Every site is defined **once** in [`catalog/sites.yml`](catalog/sites.yml); each
asset class generates one host from it via its `host_template`. To add a station,
add one line to `sites.yml` (code, name, lat/lon, city, grade, p_out_sp) — that
yields a PLC, HMI, switch, and process host (41 items) on the Jakarta geomap.
Then `make provision`. To remove a station, delete its line and re-provision
(its hosts are pruned automatically).

### Editing the monitored parameters

1. Edit a file in [`catalog/`](catalog/) — schema in [`catalog/README.md`](catalog/README.md).
2. `make check` — validates the catalog + generator offline (catches typos before touching Zabbix).
3. `make provision` — creates the new items/triggers (existing ones are skipped).
4. `make simulate` — the new parameter streams automatically.

---

## 6. How real is this?

The catalog is honest about collectability (per the report's "technical honesty"):
each parameter records its real-world `collection` method.

| Asset | Native in Zabbix | Needs middleware |
|-------|------------------|------------------|
| Gas Process (SCADA) | — | all process tags (pressure, flow, gas quality, valves, F&G, compressor) — **S7comm via Node-RED**, the same tags the HMI reads off the PLC |
| PLC S7-400 | CP reachability (SNMP/ICMP) | CPU mode, diag buffer, I/O channels, rack — **S7comm via Node-RED** |
| Workstation/HMI | CPU/RAM/disk/SMART/NIC (Agent 2) | fan RPM, PSU rails, CPU temp — **LibreHardwareMonitor WMI / iDRAC/iLO** |
| Switch/Router | everything (SNMP IF-MIB / ENVMON-MIB) | — |

In this lab all values arrive via **Zabbix Trapper** — the same push path the
report recommends for the Node-RED → Zabbix bridge (`zabbix_sender` protocol).
The simulator stands in for the Node-RED flows, Agent 2, and SNMP pollers. Because
the config plane is identical, swapping a mock for a real collector is just
changing the item type (Trapper → Agent/SNMP) on the template.

## 7. Layout

```
catalog/            asset-class definitions (the source of truth) + schema docs
otobs/              python package
  ├─ catalog.py     load + validate catalog/*.yml into typed objects
  ├─ provision.py   Zabbix API: templates, items, triggers, hosts (idempotent)
  ├─ simulate.py    sticky Good/Underperform/Failed state machine → Trapper
  ├─ settings.py    reads .env
  └─ __main__.py    CLI: provision | simulate | list | check
docs/               architecture.md (OT mapping) + runbook.md (operations)
docker-compose.yml  the real Zabbix 7.0 stack
.env / .env.example central variables
Makefile            orchestration
```

## 8. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `provision` connection refused | Stack not ready — wait for the first-boot DB import, then retry. `make logs`. |
| `provision` auth error | `ZBX_API_USER/PASSWORD` in `.env` must match the frontend login. |
| `simulate` sends but UI shows nothing | Run `make provision` first; the item and host technical name must exist. |
| Values rejected (`failed > 0`) | Value-type mismatch — `unsigned` items must get integers (the sampler handles this). |
| Port 8080/10051 already in use | Change `ZBX_WEB_PORT` / `ZBX_TRAPPER_PORT` (and the matching API/sender vars) in `.env`, then `make down && make up`. |

More detail in [docs/runbook.md](docs/runbook.md). Architecture and the OT mapping
are in [docs/architecture.md](docs/architecture.md).

> `kirim_mock.py` is the original one-item smoke test; `make simulate` supersedes
> it and is kept for reference only.

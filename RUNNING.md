# Running the lab

Everything needed to install, start, operate, and troubleshoot this repo. For
**what this is and why**, see [README.md](README.md); for **how it's built**,
see [WALKTHROUGH.md](WALKTHROUGH.md) and [docs/architecture.md](docs/architecture.md).

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

## 3. First run

```bash
make up           # start Zabbix: db, server, web, agent (docker compose up -d)
                  # first boot imports the DB schema — wait ~30-60s
make check        # offline sanity test: catalog + generator + sim-config, no Zabbix needed
make provision    # create host groups, templates, items, triggers, hosts (idempotent)
make backfill     # optional: populate historical data so graphs aren't empty
make simulate     # stream Good/Underperform/Failed mock data (Ctrl+C to stop)
```

Open **http://localhost:8080** → login **`Admin` / `zabbix`**:

- **Monitoring → Latest data** — live values. Filter by host group `OT/Process`,
  `OT/PLC`, `IT/HMI`, or `Network/Industrial`.
- **Monitoring → Problems** — triggers firing (Underperform = warning/average,
  Failed = high/disaster).
- **Monitoring → Geomap** — every station plotted across Jabodetabek/Indonesia.
  `provision` sets the OpenStreetMap tile provider and each host's inventory
  lat/lon from [`catalog/sites.yml`](catalog/sites.yml) automatically — see
  [docs/geomap.md](docs/geomap.md).
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
| `make simulate` | Stream mock telemetry via Trapper (live, forward in time) |
| `make backfill` | Generate backdated history in one shot (`DAYS=` / `SPEED=` to tune) |
| `make list` | Print the parsed catalog + which sim-config features are on |
| `make check` | Offline self-test (catalog + generator + sim-config), **no Zabbix needed** |

## 5. Configuration

All settings live in **`.env`** (copied from `.env.example`). Both `docker-compose`
and the Python tooling read it — see [docs/env-loading.md](docs/env-loading.md)
for the parsing rules. Real environment variables override `.env`.

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

`SIM_STICKINESS` / `SIM_TIME_SCALE` are global scalars. Richer, structured
realism knobs live in a separate file — see below.

### Realism layer — `catalog/sim_config.yml`

An optional layer on top of the per-parameter bands. Five features, **each with
its own `enabled` flag and off by default** — with the file absent or everything
`false`, the simulator behaves exactly as the plain state machine does. Full
schema and semantics: [docs/sim-states.md](docs/sim-states.md).

| Feature | What it adds |
|---------|--------------|
| `correlation` | When one param degrades, bias correlated params toward degrading too (e.g. a stalled fan drives CPU temperature up), per host |
| `trend` | Ramp gradually from the last value into the new band over `ramp_seconds` instead of stepping — realistic wear curves |
| `time_of_day` | Scale a value by a peak/off-peak multiplier by local hour (shift-hour load) |
| `dropout` | Occasionally skip a due send so Zabbix `nodata()` triggers get exercised |
| `backfill` | `make backfill` generates days/weeks of backdated history with correct timestamps in one run |

`make check` validates this file against the catalog (unknown param key, bad
band, or out-of-range number fails loudly before you touch Zabbix). Flip a flag,
re-run `make simulate` (or `make backfill`), and the feature is live.

## 6. Common workflows

### Adding a station (more host rows)

Every site is defined **once** in [`catalog/sites.yml`](catalog/sites.yml); each
asset class generates one host from it via its `host_template`. To add a station,
add one line to `sites.yml` (code, name, lat/lon, city, grade, p_out_sp) — that
yields a PLC, HMI, switch, and process host (41 items) on the geomap.
Then `make provision`. To remove a station, delete its line and re-provision
(its hosts are pruned automatically — see
[docs/provisioning-idempotency.md](docs/provisioning-idempotency.md)).

### Editing the monitored parameters

1. Edit a file in [`catalog/`](catalog/) — schema in [`catalog/README.md`](catalog/README.md).
2. `make check` — validates the catalog + generator offline (catches typos before touching Zabbix).
3. `make provision` — creates the new items/triggers (existing ones are skipped).
4. `make simulate` — the new parameter streams automatically.

### Backfilling history

`make simulate` only builds history going forward. To populate the past in one
run (so graphs and trends have depth immediately):

```bash
make provision                  # items/hosts must exist first
make backfill                   # uses days/speed_multiplier from catalog/sim_config.yml
make backfill DAYS=7 SPEED=2000 # or override per run
```

It sweeps the same state machine from `now − DAYS` to `now`, sending each value
with its real historical timestamp. `SPEED` is how much faster than real time to
generate — higher finishes sooner (wall time ≈ `DAYS × 86400 / SPEED` seconds).

## 7. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `provision` connection refused | Stack not ready — wait for the first-boot DB import, then retry. `make logs`. |
| `provision` auth error | `ZBX_API_USER/PASSWORD` in `.env` must match the frontend login. |
| `simulate` sends but UI shows nothing | Run `make provision` first; the item and host technical name must exist. |
| Values rejected (`failed > 0`) | Value-type mismatch — `unsigned` items must get integers (the sampler handles this). |
| Port 8080/10051 already in use | Change `ZBX_WEB_PORT` / `ZBX_TRAPPER_PORT` (and the matching API/sender vars) in `.env`, then `make down && make up`. |

## 8. Notes

- The `zabbix-agent2` container monitors the lab host itself, giving one real
  (non-mock) host alongside the simulated OT fleet.
- `make down` keeps the DB volume; only `make clean` (`down -v`) wipes it.

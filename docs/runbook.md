# Runbook

## First run

```bash
cp .env.example .env        # already done if you cloned with .env
make venv                   # creates .venv, installs deps
make up                     # docker compose up -d
# wait ~30-60s on first boot: the DB schema imports automatically
make provision              # idempotent — safe to re-run
make simulate               # Ctrl+C to stop
```

Frontend: http://localhost:8080 — `Admin` / `zabbix`.

## Daily use

| Goal | Command |
|------|---------|
| Start / stop stack (keep data) | `make up` / `make down` |
| Wipe everything (DB volume) | `make clean` |
| Re-apply catalog changes | `make provision` |
| Stream mock data | `make simulate` |
| Inspect parsed catalog | `make list` |
| Offline sanity test | `make check` |
| Tail server logs | `make logs` |

## Where to look in the UI

- **Monitoring → Latest data** — live values per host. Filter by host group
  `OT/PLC`, `IT/HMI`, `Network/Industrial`.
- **Monitoring → Problems** — active triggers (Underperform = warning/average,
  Failed = high/disaster).
- **Data collection → Hosts / Templates** — the provisioned config.

## Adding / changing a parameter

1. Edit the relevant `catalog/*.yml` (see `catalog/README.md` for the schema).
2. `make check` — validates the catalog and the generator offline.
3. `make provision` — creates the new item/triggers (existing ones are skipped).
4. `make simulate` — the new parameter streams automatically.

## Tuning the simulation

Edit `.env`:

- `SIM_STICKINESS` (0–1): higher = longer Good/Underperform/Failed stretches.
- `SIM_TIME_SCALE`: 1.0 = real catalog intervals; 10.0 = 10× faster demo.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `provision` connection refused | Stack not ready yet — wait for the DB import, retry. Check `make logs`. |
| API auth error | `ZBX_API_USER/PASSWORD` in `.env` must match the frontend login. |
| `simulate` sends but no data in UI | Item must exist (run `make provision` first) and host technical name must match. |
| Values rejected (`failed > 0`) | A value type mismatch — `unsigned` items must receive integers (handled by the sampler). |
| Port 8080/10051 in use | Change `ZBX_WEB_PORT` / `ZBX_TRAPPER_PORT` in `.env`, `make down && make up`. |

## Notes

- `kirim_mock.py` is the original one-item smoke test. The catalog-driven
  `make simulate` supersedes it; kept for reference only.
- The `zabbix-agent2` container monitors the lab host itself, giving one real
  (non-mock) host alongside the simulated OT fleet.

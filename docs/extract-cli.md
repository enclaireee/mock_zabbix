# Extract CLI

[`otobs/extract.py`](../otobs/extract.py) is the read-only counterpart to
`provision` (config plane) and `simulate` (data plane): `python -m otobs
extract sla|table` pulls data **out** of the live Zabbix instance into
CSV/JSON/table files. Every call it makes is a `*.get` (`sla.getsli` for the
SLA report body) — it never creates, updates, or deletes anything in Zabbix.

```bash
make extract ARGS="sla --from 7d --to now --format csv"
make extract ARGS="table --host-group \"Network/Comm Links\" --key circ.pgn_* --from 24h"
```

or directly: `python -m otobs extract sla --help` / `extract table --help`.

## Date ranges

`--from` / `--to` accept:

- `now`
- relative, "N units back from now": `7d`, `24h`, `30m`, `45s`, `2w`
- absolute ISO: `2026-06-01`, `2026-06-01T00:00:00` (interpreted in
  `ZBX_TIMEZONE`, same as the `time_of_day` realism feature)

Default is the last 7 days (`--from 7d --to now`) if omitted. `--from` must
resolve before `--to`, or the command exits with a clear error — never a
silent empty pull.

## `extract sla` — the comm-link SLA report

Reads the SLA object you build by hand per
[docs/comm-links-sla.md](comm-links-sla.md), via `sla.get` (to find/validate
the object) + `sla.getsli` (the report body) + `service.get` (to resolve
service names).

```bash
python -m otobs extract sla --from 7d --to now --format csv
python -m otobs extract sla --sla-name "Comm-Link SLA" --period monthly
```

- `--sla-name` — which SLA object, if more than one exists (default: the
  only one, if there's exactly one).
- `--period` — sanity check only. Zabbix computes `sla.getsli`'s reporting
  granularity from the **SLA object's own configured `period`**, not from a
  per-call override — the API has no such override. If `--period` doesn't
  match the object's period, `extract sla` prints a note but still reports at
  the object's real granularity, rather than silently reporting something
  other than what you asked for.
- Output columns: `service, period_start, period_end, sla_pct, uptime_s,
  downtime_s, excluded_s`. `excluded_s` sums the `excluded_downtimes` windows
  Zabbix already excludes from the SLA calculation (planned maintenance, etc).

### If you have more than one SLA object

One `extract sla` run reads **exactly one** SLA object — it never merges or
loops over several. With no `--sla-name` and more than one SLA object in
Zabbix, it refuses rather than guessing:

```
extract sla: multiple SLA objects exist (Comm-Link SLA, Backup-WAN SLA) — pick one with --sla-name.
```

Pass `--sla-name` to pick one; run the command again with a different
`--sla-name` (and `--out`) for each one you need.

This is about SLA **objects** (distinct SLO targets/schedules), not
circuits. The intended comm-link setup ([docs/comm-links-sla.md](comm-links-sla.md))
is a *single* SLA object tagged `sla_group=comm_link` that already selects
all 20 circuits — that case needs no `--sla-name` and returns all 20 as
separate rows (one per service × period) in one file. Multiple SLA objects
only come up if circuits are deliberately split across different SLO
targets.

## `extract table` — generic host/item export

Filters (all optional, combinable): `--host-group`, `--host` (comma-separated
for several), `--key` (a Zabbix `search` pattern — supports `*` wildcards,
e.g. `circ.pgn_*` or `hmi.cpu.*`), `--tag key=value`.

```bash
python -m otobs extract table --host-group "Network/Comm Links" \
    --key "circ.pgn_*" --from 24h --format table
python -m otobs extract table --host "HMI-GRS-WW01" --key "hmi.cpu.*" \
    --from 30d --aggregate hourly --format csv
```

### Column vocabulary

`--columns` picks which fields land in the output; default is `timestamp,
host,item,key,value,units`.

| Column | Meaning |
|---|---|
| `timestamp` | human-readable local time of the reading |
| `clock` | raw Unix timestamp |
| `host` | host technical name |
| `item` | item display name |
| `key` | item key (`key_`) |
| `value` | the reading — `value` from `history.get`, `value_avg` from `trend.get` |
| `units` | item units |
| `value_type` | Zabbix value-type code (see [zabbix-codes.md](zabbix-codes.md)) |

### history.get vs trend.get

Numeric items (`float`/`unsigned`) have hourly trend aggregates in Zabbix;
`char`/`log`/`text` items never do. `extract table` auto-picks:

- `--aggregate hourly` on a numeric item → `trend.get`
- range longer than **7 days** on a numeric item → `trend.get`
- otherwise → `history.get` (full precision)

It prints which it chose and why for every value-type group in the pull —
never a silent choice. The 7-day cutover (`_TREND_THRESHOLD_DAYS` in
`extract.py`) is a flat constant, not a flag — there's no shipped knob to
tune it per call today. If a mid-length range ever needs a different
cutover, add a `--trend-after DAYS` flag rather than editing the constant.

### Pagination

`history.get`/`trend.get` are pulled with a cursor over `clock`, page size
**5000** (`_BATCH` in `extract.py`; Zabbix has no offset pagination for
these). The cursor advances to `last_clock + 1` between pages — at this
lab's scale (catalog intervals ≥5s, a few hundred streams) no single second
ever produces more than 5000 rows, so this can't drop or duplicate a row. A
dataset with many items sharing 5000+ readings in the same clock-second
would need a `(clock, ns)` cursor instead; not needed here. If a single pull
just needs to go faster (fewer round trips), raising `_BATCH` is the lever —
it doesn't change the correctness argument above, since that argument
already holds for any batch size as long as no single second exceeds it.

## Output

`--format csv|json|table` (`table` pretty-prints to the terminal; `csv`/`json`
write a file). `--out PATH` overrides the auto-generated default filename
(`sla_2026-06-01_2026-06-08.csv` / `table_2026-06-01_2026-06-08.csv`). Zero
matching rows prints an explicit message instead of writing an empty file.

## Implementation notes

- **No argparse.** `_flag(argv, name, cast)` is a ~6-line `--name VALUE`
  scanner, the same pattern `otobs/__main__.py`'s own `_flag` uses for the
  rest of the CLI (`provision`/`simulate`/`backfill`/`config`). `extract` has
  more flags than any other subcommand, but they're all the same
  `--flag value` shape with no interdependencies, so a parser generator
  would add a dependency for no behavior a flat scanner doesn't already
  give. Revisit if `extract` grows flags that need real argparse features
  (subcommand-specific types, `nargs`, mutually exclusive groups).
- **Engine functions are undocumented by design in-code** — `resolve_range`,
  `pick_source`, `_paged_fetch`, and the rest carry no docstrings; their
  contracts are covered here instead (date ranges, history-vs-trend,
  pagination above) so there's one place to update, not two that can drift.
  `test_extract.py` pins the same contracts as runnable assertions.

## Testing

`test_extract.py` covers the offline-testable pieces — date parsing, column
validation, the history-vs-trend decision, and the pagination cursor — with
no live Zabbix connection, same assert-only style as `test_sim.py`. The live
API paths (`sla.get`/`sla.getsli`/`history.get`/`trend.get`/`item.get`
against a real stack) aren't covered by an automated test; verify them
against the running stack (`make up && make provision`, build the SLA object
per [comm-links-sla.md](comm-links-sla.md), then run both example commands
above).

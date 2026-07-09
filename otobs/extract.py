from __future__ import annotations
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from zabbix_utils import ModuleBaseException, ZabbixAPI

from . import settings

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    _TZ = ZoneInfo(settings.TIMEZONE)
except (ZoneInfoNotFoundError, ValueError):
    _TZ = None

_REL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_SLA_PERIODS = {"daily": 0, "weekly": 1, "monthly": 2, "quarterly": 3, "annually": 4}
_SLA_PERIOD_NAMES = {v: k for k, v in _SLA_PERIODS.items()}
_COLUMN_CHOICES = {"timestamp", "clock", "host", "item", "key", "value", "units", "value_type"}
_DEFAULT_COLUMNS = ["timestamp", "host", "item", "key", "value", "units"]
_TREND_THRESHOLD_DAYS = 7
_BATCH = 5000


def _parse_when(s: str, now: float) -> float:
    s = s.strip()
    if s.lower() == "now":
        return now
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw])", s)
    if m:
        return now - float(m.group(1)) * _REL_UNITS[m.group(2)]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"bad date/time {s!r} — use YYYY-MM-DD, "
                          f"YYYY-MM-DDTHH:MM:SS, 'now', or Nd/Nh/Nm back (e.g. 7d, 24h)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    return dt.timestamp()


def resolve_range(frm: str, to: str, now: float) -> tuple[float, float]:
    t_from, t_to = _parse_when(frm, now), _parse_when(to, now)
    if t_from >= t_to:
        raise ValueError(f"--from ({frm}) must be before --to ({to})")
    return t_from, t_to


def _fmt_ts(ts) -> str:
    return datetime.fromtimestamp(float(ts), _TZ).strftime("%Y-%m-%d %H:%M")


def _flag(argv: list[str], name: str, cast=str):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return cast(argv[i + 1])
    return None


def _has(argv: list[str], *names: str) -> bool:
    return any(n in argv for n in names)


def validate_columns(columns: list[str]) -> list[str]:
    bad = [c for c in columns if c not in _COLUMN_CHOICES]
    if bad:
        raise ValueError(f"unknown column(s) {bad} — choose from {sorted(_COLUMN_CHOICES)}")
    return columns


def _connect() -> ZabbixAPI:
    try:
        api = ZabbixAPI(url=settings.API_URL)
        api.login(user=settings.API_USER, password=settings.API_PASSWORD)
        return api
    except ModuleBaseException as e:
        raise SystemExit(
            f"Cannot log in to the Zabbix API at {settings.API_URL}: {e}\n"
            f"  - stack not up yet? `make up` (first boot imports the DB, ~30-60s; `make logs`)\n"
            f"  - auth? ZBX_API_USER/ZBX_API_PASSWORD in .env must match the frontend login")


def _default_out(prefix: str, t_from: float, t_to: float, fmt: str) -> Path:
    fd = datetime.fromtimestamp(t_from, _TZ).date()
    td = datetime.fromtimestamp(t_to, _TZ).date()
    return settings.ROOT / f"{prefix}_{fd}_{td}.{fmt}"


def _print_table(rows: list[dict], columns: list[str]) -> None:
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    def line(vals):
        return "  ".join(str(v).ljust(widths[c]) for c, v in zip(columns, vals))
    print(line(columns))
    print(line(["-" * widths[c] for c in columns]))
    for r in rows:
        print(line([r.get(c, "") for c in columns]))


def _write_rows(rows: list[dict], columns: list[str], fmt: str, out: str | None,
                prefix: str, t_from: float, t_to: float) -> None:
    if not rows:
        print("No rows matched the given filters — nothing written.", file=sys.stderr)
        return
    if fmt == "table":
        _print_table(rows, columns)
        return
    if fmt not in ("csv", "json"):
        raise SystemExit(f"extract: unknown --format {fmt!r} (csv|json|table)")
    path = Path(out) if out else _default_out(prefix, t_from, t_to, fmt)
    if fmt == "csv":
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    else:
        path.write_text(json.dumps([{c: r.get(c) for c in columns} for r in rows], indent=2) + "\n")
    print(f"wrote {len(rows)} row(s) -> {path}", file=sys.stderr)


def pick_source(value_type: int, span_days: float, aggregate: str | None) -> tuple[str, str]:
    has_trend = value_type in (0, 3)
    if aggregate == "hourly" and has_trend:
        return "trend", "--aggregate hourly"
    if aggregate == "hourly" and not has_trend:
        return "history", f"value_type {value_type} has no trends — --aggregate hourly ignored"
    if has_trend and span_days > _TREND_THRESHOLD_DAYS:
        return "trend", f"range {span_days:.1f}d > {_TREND_THRESHOLD_DAYS}d"
    return "history", f"range {span_days:.1f}d, full precision"


def _paged_fetch(method, itemids: list[str], t_from: float, t_to: float):
    since = int(t_from)
    while True:
        batch = method(itemids=itemids, time_from=since, time_till=int(t_to),
                       sortfield="clock", sortorder="ASC", limit=_BATCH, output="extend")
        if not batch:
            return
        yield from batch
        if len(batch) < _BATCH:
            return
        since = int(batch[-1]["clock"]) + 1


_TABLE_HELP = """usage: python -m otobs extract table [options]

Generic host-group/host/item-key export from history.get/trend.get.

Options:
  --from RANGE            see `extract sla --help`. Default: 7d.
  --to RANGE              see `extract sla --help`. Default: now.
  --host-group NAME       filter by Zabbix host group (e.g. "Network/Comm Links").
  --host NAME[,NAME...]   filter by host technical name(s).
  --key PATTERN           item key glob/prefix, e.g. 'circ.pgn_*' or 'hmi.cpu.*'.
  --tag KEY=VALUE         filter items by tag.
  --columns C,C,...       columns to include: timestamp,clock,host,item,key,
                          value,units,value_type. Default: timestamp,host,item,
                          key,value,units.
  --aggregate hourly      force trend.get (hourly aggregates) for numeric items.
  --format csv|json|table Default: table.
  --out PATH              output file (csv/json). Default: table_<from>_<to>.csv.

Auto-selects history.get (full precision) vs trend.get (hourly aggregates,
numeric items only) based on range length; prints which it used and why.

Examples:
  python -m otobs extract table --host-group "Network/Comm Links" \\
      --key "circ.pgn_*" --from 24h --format table
  python -m otobs extract table --host "HMI-GRS-WW01" --key "hmi.cpu.*" \\
      --from 30d --aggregate hourly --format csv
"""


def _find_items(api: ZabbixAPI, host_group, host, key_pattern, tag) -> list[dict]:
    kwargs = {"output": ["itemid", "hostid", "key_", "name", "value_type", "units"],
             "selectHosts": ["host"]}
    if host_group:
        groups = api.hostgroup.get(filter={"name": [host_group]}, output=["groupid"])
        if not groups:
            raise SystemExit(f"extract table: no host group named {host_group!r}")
        kwargs["groupids"] = [g["groupid"] for g in groups]
    if host:
        hosts = api.host.get(filter={"host": host.split(",")}, output=["hostid"])
        if not hosts:
            raise SystemExit(f"extract table: no host(s) matching {host!r}")
        kwargs["hostids"] = [h["hostid"] for h in hosts]
    if key_pattern:
        kwargs["search"] = {"key_": key_pattern}
        kwargs["searchWildcardsEnabled"] = True
    if tag:
        if "=" not in tag:
            raise SystemExit("extract table: --tag must be key=value")
        k, v = tag.split("=", 1)
        kwargs["tags"] = [{"tag": k, "value": v}]
    items = api.item.get(**kwargs)
    for it in items:
        it["host"] = it["hosts"][0]["host"] if it.get("hosts") else it["hostid"]
    return items


def _run_table(argv: list[str]) -> None:
    if not argv or _has(argv, "--help", "-h"):
        print(_TABLE_HELP)
        return
    frm = _flag(argv, "--from") or "7d"
    to = _flag(argv, "--to") or "now"
    try:
        t_from, t_to = resolve_range(frm, to, time.time())
    except ValueError as e:
        raise SystemExit(f"extract table: {e}")

    fmt = _flag(argv, "--format") or "table"
    columns_raw = _flag(argv, "--columns")
    columns = columns_raw.split(",") if columns_raw else list(_DEFAULT_COLUMNS)
    try:
        validate_columns(columns)
    except ValueError as e:
        raise SystemExit(f"extract table: {e}")

    api = _connect()
    try:
        items = _find_items(api, _flag(argv, "--host-group"), _flag(argv, "--host"),
                            _flag(argv, "--key"), _flag(argv, "--tag"))
        if not items:
            print("No items matched the given filters.", file=sys.stderr)
            return
        print(f"{len(items)} item(s) matched.", file=sys.stderr)

        by_type: dict[int, list[dict]] = {}
        for it in items:
            by_type.setdefault(int(it["value_type"]), []).append(it)

        span_days = (t_to - t_from) / 86400
        aggregate = _flag(argv, "--aggregate")
        rows: list[dict] = []
        for vtype, its in by_type.items():
            source, reason = pick_source(vtype, span_days, aggregate)
            print(f"value_type {vtype}: using {source}.get ({reason}) for {len(its)} item(s)",
                 file=sys.stderr)
            by_id = {it["itemid"]: it for it in its}
            itemids = list(by_id)
            method = (lambda **kw: api.history.get(history=vtype, **kw)) if source == "history" \
                else api.trend.get
            count = 0
            for point in _paged_fetch(method, itemids, t_from, t_to):
                it = by_id[point["itemid"]]
                rows.append({
                    "timestamp": _fmt_ts(point["clock"]),
                    "clock": point["clock"],
                    "host": it["host"],
                    "item": it["name"],
                    "key": it["key_"],
                    "value": point.get("value", point.get("value_avg")),
                    "units": it.get("units", ""),
                    "value_type": vtype,
                })
                count += 1
                if count % _BATCH == 0:
                    print(f"  ...{count} row(s) fetched for value_type {vtype}", file=sys.stderr)
    finally:
        api.logout()

    _write_rows(rows, columns, fmt, _flag(argv, "--out"), "table", t_from, t_to)


_SLA_HELP = """usage: python -m otobs extract sla [options]

Pull the per-circuit SLA report from the comm-link SLA object built by hand
in the Zabbix UI (see docs/comm-links-sla.md) via sla.getsli.

Options:
  --from RANGE       start of the window: 'now', 'Nd'/'Nh'/'Nm' back, or an
                      absolute date/datetime (2026-06-01, 2026-06-01T00:00:00).
                      Default: 7d.
  --to RANGE         end of the window, same formats. Default: now.
  --sla-name NAME    which SLA object to read (default: the only one, if
                      there's exactly one; otherwise required).
  --period NAME       expected granularity (daily|weekly|monthly|quarterly|
                      annually) — sanity-checked against the SLA object's own
                      configured period; the object's own period always
                      governs what sla.getsli actually returns (Zabbix does
                      not let a getsli call override it).
  --format csv|json|table   Default: table.
  --out PATH          output file (csv/json). Default: sla_<from>_<to>.csv.

Examples:
  python -m otobs extract sla --from 7d --to now --format csv
  python -m otobs extract sla --sla-name "Comm-Link SLA" --period monthly
"""

_SLA_COLUMNS = ["service", "period_start", "period_end", "sla_pct", "uptime_s", "downtime_s", "excluded_s"]


def _resolve_sla(api: ZabbixAPI, sla_name: str | None) -> dict:
    slas = api.sla.get(output=["slaid", "name", "period", "slo"])
    if not slas:
        raise SystemExit("extract sla: no SLA objects found in Zabbix — "
                         "build one first (docs/comm-links-sla.md).")
    if sla_name:
        slas = [s for s in slas if s["name"] == sla_name]
        if not slas:
            raise SystemExit(f"extract sla: no SLA object named {sla_name!r}.")
    if len(slas) > 1:
        names = ", ".join(s["name"] for s in slas)
        raise SystemExit(f"extract sla: multiple SLA objects exist ({names}) — pick one with --sla-name.")
    return slas[0]


def _run_sla(argv: list[str]) -> None:
    if _has(argv, "--help", "-h"):
        print(_SLA_HELP)
        return
    frm = _flag(argv, "--from") or "7d"
    to = _flag(argv, "--to") or "now"
    try:
        t_from, t_to = resolve_range(frm, to, time.time())
    except ValueError as e:
        raise SystemExit(f"extract sla: {e}")

    period = _flag(argv, "--period")
    if period and period not in _SLA_PERIODS:
        raise SystemExit(f"extract sla: --period must be one of {', '.join(_SLA_PERIODS)}")
    fmt = _flag(argv, "--format") or "table"

    api = _connect()
    try:
        sla = _resolve_sla(api, _flag(argv, "--sla-name"))
        if period and _SLA_PERIODS[period] != int(sla["period"]):
            print(f"note: --period {period} doesn't match the SLA object's own configured "
                 f"period ({_SLA_PERIOD_NAMES.get(int(sla['period']), sla['period'])}); "
                 f"sla.getsli always reports at the object's own granularity.", file=sys.stderr)
        print(f"SLA '{sla['name']}' (SLO {sla['slo']}%, "
             f"{_SLA_PERIOD_NAMES.get(int(sla['period']), sla['period'])}), "
             f"{_fmt_ts(t_from)} .. {_fmt_ts(t_to)}", file=sys.stderr)

        result = api.sla.getsli(slaid=sla["slaid"], period_from=int(t_from), period_to=int(t_to))
        service_ids = result.get("serviceids", [])
        if not service_ids:
            print("note: SLA object has no services matching its tag filter — nothing to report.",
                 file=sys.stderr)
            rows = []
        else:
            services = {s["serviceid"]: s["name"] for s in
                       api.service.get(serviceids=service_ids, output=["serviceid", "name"])}
            periods = result.get("periods", [])
            sli_by_service = result.get("sli", [])
            rows = []
            for i, sid in enumerate(service_ids):
                for p, sli in zip(periods, sli_by_service[i] if i < len(sli_by_service) else []):
                    excluded = sum((e.get("period_to", 0) - e.get("period_from", 0))
                                  for e in (sli.get("excluded_downtimes") or []))
                    rows.append({
                        "service": services.get(sid, sid),
                        "period_start": _fmt_ts(p["period_from"]),
                        "period_end": _fmt_ts(p["period_to"]),
                        "sla_pct": sli.get("sli"),
                        "uptime_s": sli.get("uptime"),
                        "downtime_s": sli.get("downtime"),
                        "excluded_s": excluded,
                    })
    finally:
        api.logout()

    _write_rows(rows, _SLA_COLUMNS, fmt, _flag(argv, "--out"), "sla", t_from, t_to)


_MAIN_HELP = """usage: python -m otobs extract <sla|table> [options]

Read-only extraction from the live Zabbix instance (history/trend/SLA data)
into CSV/JSON/table files. Never creates, updates, or deletes anything in
Zabbix — every call is a *.get (sla.getsli for the SLA report body).

Subcommands:
  sla     per-circuit SLA % report from the comm-link SLA object
  table   generic host/item/history-or-trend export

Run `python -m otobs extract sla --help` or `extract table --help` for
subcommand options and examples.
"""


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("--help", "-h"):
        print(_MAIN_HELP)
        return
    sub, rest = argv[0], argv[1:]
    if sub == "sla":
        _run_sla(rest)
    elif sub == "table":
        _run_table(rest)
    else:
        print(f"unknown extract subcommand {sub!r}\n")
        print(_MAIN_HELP)
        raise SystemExit(2)

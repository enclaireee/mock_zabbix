import time
from datetime import datetime

from otobs.extract import (resolve_range, pick_source, validate_columns,
                           _paged_fetch, _parse_when, _TZ)


def test_relative_and_now():
    now = 1_000_000.0
    assert _parse_when("now", now) == now
    assert _parse_when("7d", now) == now - 7 * 86400
    assert _parse_when("24h", now) == now - 24 * 3600
    assert _parse_when("30m", now) == now - 30 * 60


def test_absolute_date():
    now = time.time()
    ts = _parse_when("2026-06-01", now)
    assert ts == datetime(2026, 6, 1, tzinfo=_TZ).timestamp()


def test_bad_when_raises():
    try:
        _parse_when("not-a-date", time.time())
        assert False, "bad date/time string accepted"
    except ValueError:
        pass


def test_resolve_range_orders_from_before_to():
    now = time.time()
    t_from, t_to = resolve_range("7d", "now", now)
    assert t_from < t_to
    try:
        resolve_range("now", "7d", now)
        assert False, "from >= to accepted"
    except ValueError:
        pass


def test_validate_columns():
    assert validate_columns(["timestamp", "value"]) == ["timestamp", "value"]
    try:
        validate_columns(["nope"])
        assert False, "unknown column accepted"
    except ValueError:
        pass


def test_pick_source_numeric_short_range_uses_history():
    source, reason = pick_source(0, 1.0, None)
    assert source == "history" and "1.0d" in reason


def test_pick_source_numeric_long_range_uses_trend():
    source, reason = pick_source(3, 10.0, None)
    assert source == "trend" and "10.0d" in reason


def test_pick_source_aggregate_hourly_forces_trend_on_numeric():
    source, reason = pick_source(0, 0.5, "hourly")
    assert source == "trend" and reason == "--aggregate hourly"


def test_pick_source_non_numeric_never_trends():
    source, reason = pick_source(4, 100.0, None)
    assert source == "history"
    source, reason = pick_source(1, 0.1, "hourly")
    assert source == "history" and "no trends" in reason


def test_paged_fetch_cursors_past_batch_size():
    data = [{"itemid": "1", "clock": c, "value": str(c)} for c in range(1, 12001)]

    def fake(itemids, time_from, time_till, sortfield, sortorder, limit, output):
        return [d for d in data if time_from <= d["clock"] <= time_till][:limit]

    got = list(_paged_fetch(fake, ["1"], 1, 12000))
    assert len(got) == 12000
    assert [g["clock"] for g in got] == list(range(1, 12001))


def test_paged_fetch_empty_range_yields_nothing():
    def fake(**kw):
        return []
    assert list(_paged_fetch(fake, ["1"], 1, 100)) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all extract self-checks passed.")

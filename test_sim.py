"""Runnable self-check for the sim_config realism layer. No framework:
    .venv/bin/python test_sim.py
Covers the load-bearing claims: strict no-op when disabled, correlation bias,
trend ramp, dropout, and sim_config validation. `make check` covers the catalog.
"""
import random

from otobs.catalog import Parameter, Sim, State
from otobs.sim_config import (SimConfig, Correlation, CorrGroup, Affect, Trend,
                              Dropout, TodProfile, validate)
from otobs.simulate import (Stream, sample, next_state, process_stream,
                            correlation_forces, _by_host, _fmt_eta, run_backfill)


def numsim():
    return Sim("numeric", [
        State(0.90, "good", None, 40, 60, 1.5),
        State(0.08, "underperform", None, 66, 84, 1.5),
        State(0.02, "failed", None, 86, 99, 1.5),
    ])


def param(key):
    return Parameter(key, key, "float", "", "1m", "c", "col", "fm", "src", numsim(), [])


def test_disabled_is_identical():
    """All features off => byte-identical value stream to the legacy path."""
    cfg = SimConfig()  # everything off
    # legacy: next_state + sample, seeded
    random.seed(1234)
    sim, cur, legacy = numsim(), None, []
    for _ in range(300):
        cur = next_state(sim, cur, 0.92)
        legacy.append(sample(sim, sim.states[cur], "float"))
    # new path via process_stream, same seed
    random.seed(1234)
    s = Stream("h", param("k"))
    new = [process_stream(s, 0.0, 1.0, cfg, {}, 0) for _ in range(300)]
    assert new == legacy, "disabled sim_config changed the output distribution"


def test_correlation_biases():
    """fan=failed with strength 1.0 forces temp -> underperform far more often."""
    fan, temp = param("fan"), param("temp")

    def underperform_rate(cfg, force_fan_failed):
        random.seed(7)
        streams = [Stream("H", fan), Stream("H", temp)]
        bh = _by_host(streams)
        hits = 0
        for _ in range(400):
            if force_fan_failed:
                streams[0].state_idx = 2  # pin fan in 'failed'
            forced = correlation_forces(cfg, bh)
            v = process_stream(streams[1], 0.0, 1.0, cfg, forced, 0)
            if v is not None and streams[1].state_idx == 1:  # temp in underperform
                hits += 1
        return hits / 400

    grp = CorrGroup("thermal", "fan", "failed",
                    [Affect("temp", "underperform", 1.0)])
    on = underperform_rate(SimConfig(correlation=Correlation(True, [grp])), True)
    off = underperform_rate(SimConfig(), True)
    assert on > off + 0.5, f"correlation had no visible effect (on={on:.2f} off={off:.2f})"


def test_trend_ramps_not_steps():
    """On a transition the first emitted value sits near the OLD value, below the
    new band — proof of a ramp, not an instant step into the new band."""
    cfg = SimConfig(trend=Trend(True, ramp_seconds=1800))
    s = Stream("H", param("t"), state_idx=0, last_value=50.0)  # was in 'good'
    random.seed(3)
    # force transition to 'failed' (band [86,99]) at t=ramp_start
    v = process_stream(s, now=100.0, scale=1.0, cfg=cfg,
                       forced={("H", "t"): "failed"}, hour=0)
    assert s.state_idx == 2 and s.ramp_to is not None, "trend did not arm a ramp"
    assert v < 86, f"trend stepped straight into the new band ({v}), no ramp"
    # midway through the ramp the value should have climbed toward the target
    mid = process_stream(s, now=100.0 + 900.0, scale=1.0, cfg=cfg,
                         forced={("H", "t"): "failed"}, hour=0)
    assert mid > v, f"ramp did not progress ({mid} !> {v})"


def test_dropout():
    always = SimConfig(dropout=Dropout(True, 1.0))
    never = SimConfig(dropout=Dropout(True, 0.0))
    s = Stream("H", param("d"))
    assert all(process_stream(s, 0.0, 1.0, always, {}, 0) is None for _ in range(50))
    assert all(process_stream(s, 0.0, 1.0, never, {}, 0) is not None for _ in range(50))


def test_tod_profile():
    p = TodProfile(8, 17, 1.4, 0.6)
    assert p.multiplier(12) == 1.4 and p.multiplier(3) == 0.6
    wrap = TodProfile(22, 6, 2.0, 1.0)  # overnight window wraps midnight
    assert wrap.multiplier(23) == 2.0 and wrap.multiplier(12) == 1.0


def test_validate_catches_typos():
    bands = {"fan": {"good", "failed"}, "temp": {"good", "underperform"}}
    good = SimConfig(correlation=Correlation(
        True, [CorrGroup("g", "fan", "failed", [Affect("temp", "underperform", 0.5)])]))
    validate(good, bands)  # no raise
    bad_param = SimConfig(correlation=Correlation(
        True, [CorrGroup("g", "nope", "failed", [])]))
    bad_band = SimConfig(correlation=Correlation(
        True, [CorrGroup("g", "fan", "on_fire", [])]))
    for cfg in (bad_param, bad_band):
        try:
            validate(cfg, bands)
            assert False, "validate accepted a bad reference"
        except ValueError:
            pass


def test_fmt_eta():
    assert _fmt_eta(0) == "00:00"
    assert _fmt_eta(5) == "00:05"
    assert _fmt_eta(65) == "01:05"
    assert _fmt_eta(3661) == "1:01:01"


def test_backfill_bucket_scheduler_fires_every_due_tick():
    """run_backfill was rewritten from an O(n_streams)-scan-per-tick loop to
    scheduling by shared interval bucket. This proves the swap didn't drop or
    duplicate a single event: exact expected count per stream, mixing streams
    that share an interval (exercise the bucket-grouping path) with streams
    on staggered intervals (exercise independent ticks), real zabbix send()
    faked out."""
    import math
    import zabbix_utils
    from otobs.catalog import AssetClass, Host

    def mk_param(key, interval):
        return Parameter(key, key, "float", "", interval, "c", "col", "fm", "src", numsim(), [])

    intervals = {"a": 5, "b": 5, "c": 7, "d": 11}  # a,b share a bucket; c,d each their own
    asset = AssetClass("ac", "hg", "tmpl", "tg", [Host("H", "H")],
                       [mk_param(k, f"{v}s") for k, v in intervals.items()])

    sent_counts = []

    class FakeSender:
        def __init__(self, **_kw):
            pass

        def send(self, items):
            sent_counts.append(len(items))

    span_s = 97.0  # not evenly divisible by any interval above -> no boundary ambiguity
    real_sender = zabbix_utils.Sender
    zabbix_utils.Sender = FakeSender
    try:
        run_backfill([asset], cfg=SimConfig(), days=span_s / 86400.0, speed=1e6)
    finally:
        zabbix_utils.Sender = real_sender

    expected = sum(math.floor(span_s / iv) + 1 for iv in intervals.values())
    got = sum(sent_counts)
    assert got == expected, f"bucket scheduler lost/duplicated events: got {got}, want {expected}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all sim self-checks passed.")

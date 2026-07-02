"""Runnable self-check for the sim_config realism layer. No framework:
    .venv/bin/python test_sim.py
Covers the load-bearing claims: strict no-op when disabled, correlation bias,
trend ramp, dropout, and sim_config validation. `make check` covers the catalog.
"""
import random

from otobs.catalog import Parameter, Sim, State
from otobs.sim_config import (SimConfig, Continuity, Correlation, CorrGroup, Affect,
                              Trend, Dropout, TodProfile, validate)
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


def test_continuity_walks_not_teleports():
    """With continuity on, a steady-state stream steps from its last value (small
    moves) instead of re-drawing the whole band each tick — the core realism fix."""
    cfg = SimConfig(continuity=Continuity(True))
    s = Stream("H", param("k"), state_idx=0, last_value=50.0)  # good band [40,60], jitter 1.5
    random.seed(11)
    prev, steps, vals = 50.0, [], []
    for _ in range(80):
        v = process_stream(s, 0.0, 1.0, cfg, {("H", "k"): "good"}, 0)  # pin band -> no transition
        steps.append(abs(v - prev)); vals.append(v); prev = v
    assert max(steps) < 8, f"continuity teleported (max step {max(steps)} >> jitter)"
    assert all(40 <= v <= 60 for v in vals), "walk left the band"
    # sanity: the legacy path really would teleport far more than this
    random.seed(11)
    legacy = [sample(numsim(), numsim().states[0], "float") for _ in range(80)]
    assert max(abs(b - a) for a, b in zip(legacy, legacy[1:])) > 8, "test band too narrow to prove a difference"


def test_continuity_counter_holds():
    """jitter=0 (a fault count / SMART sectors) must HOLD once in a band, not
    bounce across [lo,hi] every tick."""
    sim = Sim("numeric", [State(0.9, "good", None, 0, 0, 0),
                          State(0.08, "underperform", None, 1, 40, 0),
                          State(0.02, "failed", None, 200, 800, 0)])
    p = Parameter("io", "io", "unsigned", "", "15s", "c", "col", "fm", "src", sim, [])
    cfg = SimConfig(continuity=Continuity(True))
    s = Stream("H", p, state_idx=1, last_value=23.0)  # already in underperform [1,40]
    random.seed(5)
    vals = [process_stream(s, 0.0, 1.0, cfg, {("H", "io"): "underperform"}, 0) for _ in range(20)]
    assert set(vals) == {23}, f"jitter=0 counter should hold at 23, got {sorted(set(vals))}"


def test_continuity_reversion_and_tod():
    """reversion pulls an analog value to its setpoint (band centre); time_of_day
    shifts that setpoint. This is the 'controlled process variable' realism model."""
    from otobs.sim_config import Continuity, TimeOfDay, TodProfile
    # step_scale=0 => no noise, so convergence is deterministic. Band [40,60], centre 50.
    cfg = SimConfig(continuity=Continuity(True, step_scale=0.0, reversion=0.5))
    s = Stream("H", param("k"), state_idx=0, last_value=41.0)
    v = 41.0
    for _ in range(30):
        v = process_stream(s, 0.0, 1.0, cfg, {("H", "k"): "good"}, 0)
    assert abs(v - 50.0) < 1.0, f"reversion did not hold the setpoint (v={v})"
    # an always-on x1.15 multiplier moves the setpoint to 57.5
    tod = TimeOfDay(True, {"k": TodProfile(0, 24, 1.15, 1.15)})
    cfg2 = SimConfig(continuity=Continuity(True, step_scale=0.0, reversion=0.5), time_of_day=tod)
    s2 = Stream("H", param("k"), state_idx=0, last_value=50.0)
    v2 = 50.0
    for _ in range(30):
        v2 = process_stream(s2, 0.0, 1.0, cfg2, {("H", "k"): "good"}, 12)
    assert v2 > 54.0, f"time_of_day did not shift the continuity setpoint (v={v2})"


def test_ramp_hands_off_to_walk():
    """docs/sim-config.md: 'continuity + trend compose' — during a ramp the walk
    is suspended; once the ramp ends the value is in the new band and the
    continuity walk takes over from there."""
    cfg = SimConfig(continuity=Continuity(True), trend=Trend(True, ramp_seconds=100))
    s = Stream("H", param("t"), state_idx=0, last_value=50.0)
    random.seed(9)
    v = process_stream(s, now=0.0, scale=1.0, cfg=cfg,
                       forced={("H", "t"): "failed"}, hour=0)  # arm ramp toward [86,99]
    assert v < 86, "ramp should start below the new band"
    after = process_stream(s, now=200.0, scale=1.0, cfg=cfg,
                           forced={("H", "t"): "failed"}, hour=0)  # ramp expired
    assert 86 <= after <= 99, f"post-ramp value should be in the new band ({after})"
    prev = after
    for _ in range(20):  # steady state: small continuity steps, in band
        nxt = process_stream(s, now=300.0, scale=1.0, cfg=cfg,
                             forced={("H", "t"): "failed"}, hour=0)
        assert 86 <= nxt <= 99 and abs(nxt - prev) < 8, "walk did not take over in-band"
        prev = nxt


def test_numeric_weights_length_guard():
    """A 2-entry weights list must fail at load, not silently drop the third band."""
    from otobs.catalog import _build_sim
    raw = {"kind": "numeric", "good": [0, 1], "underperform": [1, 2], "failed": [2, 3],
           "weights": ["good", "underperform"]}
    try:
        _build_sim(raw, "x")
        assert False, "short weights list accepted (band silently dropped)"
    except ValueError:
        pass


def test_presets_validate_against_catalog():
    """Every shipped preset must reference only real param keys/bands, so a typo in
    a mode file is caught here, not at `make config` time."""
    from otobs.catalog import load_all
    from otobs.sim_config import load_sim_config_file
    from otobs.settings import PRESETS_DIR
    assets = load_all()
    bands = {p.key: {st.band for st in p.sim.states} for a in assets for p in a.parameters}
    files = sorted(PRESETS_DIR.glob("*.yml"))
    assert files, "no presets found"
    for f in files:
        validate(load_sim_config_file(f), bands)  # raises on a bad reference


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


def test_catalog_guards():
    """The load-time guards: bad interval, all-zero weights, enum band collision,
    and trigger-field typos all fail loudly at load, not deep in the sim loop."""
    from otobs.catalog import parse_interval, Sim, State, _build_sim, _build_param
    for bad in ("0s", "0m", "00s"):               # 0 interval would spin forever
        try:
            parse_interval(bad); assert False, f"{bad} accepted"
        except ValueError:
            pass
    assert parse_interval("15s") == 15 and parse_interval("1h") == 3600
    try:                                            # all-zero weights -> no div/0 later
        Sim("numeric", [State(0, "good", None, 0, 1, 0)]); assert False
    except ValueError:
        pass
    sim = _build_sim({"kind": "enum",              # numeric-weight enum stays distinct
                      "states": [{"value": 8, "weight": 3}, {"value": 6, "weight": 1}]}, "x")
    assert sim.states[0].band != sim.states[1].band, "numeric-weight enum bands collided"
    try:                                            # bad trigger field -> ValueError
        _build_param({"key": "k", "name": "n", "value_type": "float", "interval": "5s",
                      "component": "c", "collection": "c", "failure_mode": "f", "source": "s",
                      "sim": {"kind": "numeric", "good": [0, 1], "underperform": [1, 2], "failed": [2, 3]},
                      "triggers": [{"op": ">=", "value": 1, "severity": "warning",
                                    "label": "l", "bogus": 1}]}, "x")
        assert False, "bad trigger field accepted"
    except ValueError:
        pass


def test_fmt_eta():
    assert _fmt_eta(0) == "00:00"
    assert _fmt_eta(5) == "00:05"
    assert _fmt_eta(65) == "01:05"
    assert _fmt_eta(3661) == "1:01:01"


def test_backfill_bucket_scheduler_fires_every_due_tick():
    """run_backfill schedules by shared interval bucket, not per stream. This
    proves the bucketing neither drops nor duplicates a single event: exact
    expected count per stream, mixing streams that share an interval (the
    bucket-grouping path) with streams on staggered intervals (independent
    ticks), real zabbix send() faked out."""
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

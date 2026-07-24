"""Pytest suite for the sim_config realism layer and the simulation engine.
Covers the load-bearing claims: strict no-op when disabled, correlation bias,
trend ramp, dropout, hold/MTTR dwell, and per-port discovery fan-out.
`tests/test_config.py` covers catalog/sim_config validation.
"""
from __future__ import annotations
import json
import math
import random
import threading
from unittest.mock import MagicMock, patch

import pytest

from otobs.catalog import AssetClass, Discovery, Host
from otobs.simulate import (Stream, sample, next_state, process_stream, build_streams,
                            discovery_payloads, correlation_forces, segment_forces,
                            _by_host, _fmt_eta, _idx_of_band, run, run_backfill)
from otobs.sim_config import (SimConfig, Continuity, Correlation, CorrGroup, Affect,
                              Trend, Dropout, TimeOfDay, TodProfile, Hold)

from .conftest import numsim, param


def test_disabled_is_identical(monkeypatch):
    """All features off => byte-identical value stream to the legacy path."""
    from otobs import settings
    monkeypatch.setattr(settings, "STICKINESS", 0.92)  # pin: .env must not sway parity
    cfg = SimConfig()  # everything off
    random.seed(1234)
    sim, cur, legacy = numsim(), None, []
    for _ in range(300):
        cur = next_state(sim, cur, 0.92)
        legacy.append(sample(sim, sim.states[cur], "float"))
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

    grp = CorrGroup("thermal", "fan", "failed", [Affect("temp", "underperform", 1.0)])
    on = underperform_rate(SimConfig(correlation=Correlation(True, [grp])), True)
    off = underperform_rate(SimConfig(), True)
    assert on > off + 0.5, f"correlation had no visible effect (on={on:.2f} off={off:.2f})"


def test_trend_ramps_not_steps():
    """On a transition the first emitted value sits near the OLD value, below the
    new band — proof of a ramp, not an instant step into the new band."""
    cfg = SimConfig(trend=Trend(True, ramp_seconds=1800))
    s = Stream("H", param("t"), state_idx=0, last_value=50.0)  # was in 'good'
    random.seed(3)
    v = process_stream(s, now=100.0, scale=1.0, cfg=cfg,
                       forced={("H", "t"): "failed"}, hour=0)
    assert s.state_idx == 2 and s.ramp_to is not None, "trend did not arm a ramp"
    assert v < 86, f"trend stepped straight into the new band ({v}), no ramp"
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
    random.seed(11)
    legacy = [sample(numsim(), numsim().states[0], "float") for _ in range(80)]
    assert max(abs(b - a) for a, b in zip(legacy, legacy[1:])) > 8, \
        "test band too narrow to prove a difference"


def test_continuity_counter_holds():
    """jitter=0 (a fault count / SMART sectors) must HOLD once in a band, not
    bounce across [lo,hi] every tick."""
    from otobs.catalog import Sim, State, Parameter
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
    cfg = SimConfig(continuity=Continuity(True, step_scale=0.0, reversion=0.5))
    s = Stream("H", param("k"), state_idx=0, last_value=41.0)
    v = 41.0
    for _ in range(30):
        v = process_stream(s, 0.0, 1.0, cfg, {("H", "k"): "good"}, 0)
    assert abs(v - 50.0) < 1.0, f"reversion did not hold the setpoint (v={v})"
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


def test_dropout():
    always = SimConfig(dropout=Dropout(True, 1.0))
    never = SimConfig(dropout=Dropout(True, 0.0))
    s = Stream("H", param("d"))
    assert all(process_stream(s, 0.0, 1.0, always, {}, 0) is None for _ in range(50))
    assert all(process_stream(s, 0.0, 1.0, never, {}, 0) is not None for _ in range(50))


def test_tod_profile():
    p = TodProfile(8, 17, 1.4, 0.6)  # default 2h shoulder
    assert p.multiplier(12) == 1.4 and p.multiplier(3) == 0.6  # deep in/out unchanged
    wrap = TodProfile(22, 6, 2.0, 1.0)  # overnight window wraps midnight
    assert wrap.multiplier(23) == 2.0 and wrap.multiplier(12) == 1.0


def test_tod_shoulder_blend():
    """Demand ramps over hours, it doesn't step: the boundary is a linear blend
    of width shoulder_hours, the exact edge is the midpoint, and shoulder 0
    restores the hard step."""
    p = TodProfile(8, 17, 1.4, 0.6, shoulder_hours=2.0)
    mid = (1.4 + 0.6) / 2
    assert abs(p.multiplier(8.0) - mid) < 1e-9, "edge should be the blend midpoint"
    assert 0.6 < p.multiplier(7.5) < mid < p.multiplier(8.5) < 1.4, "blend not monotone"
    assert p.multiplier(9.0) == 1.4 and p.multiplier(7.0) == 0.6  # shoulder ends
    hard = TodProfile(8, 17, 1.4, 0.6, shoulder_hours=0.0)
    assert hard.multiplier(8.0) == 1.4 and hard.multiplier(7.99) == 0.6
    wrap = TodProfile(22, 6, 2.0, 1.0, shoulder_hours=2.0)  # blend across midnight
    assert abs(wrap.multiplier(22.0) - 1.5) < 1e-9
    assert wrap.multiplier(0.0) == 2.0 and wrap.multiplier(12.0) == 1.0
    allday = TodProfile(0, 24, 1.15, 0.5)   # full-day window: always peak
    assert allday.multiplier(3) == 1.15 and allday.multiplier(15) == 1.15
    empty = TodProfile(0, 0, 1.4, 0.6)      # empty window: always off-peak
    assert empty.multiplier(12) == 0.6


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
    ticks), real zabbix send() faked out via unittest.mock."""
    def mk_param(key, interval):
        from otobs.catalog import Parameter
        return Parameter(key, key, "float", "", interval, "c", "col", "fm", "src", numsim(), [])

    intervals = {"a": 5, "b": 5, "c": 7, "d": 11}  # a,b share a bucket; c,d each their own
    asset = AssetClass("ac", "hg", "tmpl", "tg", [Host("H", "H")],
                       [mk_param(k, f"{v}s") for k, v in intervals.items()])

    sent_counts = []
    fake_sender = MagicMock()
    fake_sender.send.side_effect = lambda items: sent_counts.append(len(items))

    span_s = 97.0  # not evenly divisible by any interval above -> no boundary ambiguity
    with patch("zabbix_utils.Sender", return_value=fake_sender):
        run_backfill([asset], cfg=SimConfig(), days=span_s / 86400.0, speed=1e6)

    expected = sum(math.floor(span_s / iv) + 1 for iv in intervals.values())
    got = sum(sent_counts)
    assert got == expected, f"bucket scheduler lost/duplicated events: got {got}, want {expected}"


def test_run_offloads_sends_to_a_worker_thread():
    """The live loop hands sender.send() to a ThreadPoolExecutor instead of
    blocking the scheduler on it — the failure mode this guards against is a
    slow trapper reply stalling process_stream()/next_due on the main thread."""
    asset = AssetClass("ac", "hg", "tmpl", "tg", [Host("H", "H")], [param("k")])

    send_thread_name = {}
    fake_sender = MagicMock()

    def fake_send(items):
        send_thread_name["name"] = threading.current_thread().name
        return MagicMock(processed=len(items), failed=0)

    fake_sender.send.side_effect = fake_send

    sleep_calls = {"n": 0}

    def fake_sleep(_secs):
        sleep_calls["n"] += 1
        if sleep_calls["n"] > 1:  # let exactly one tick (and its submit) happen
            raise KeyboardInterrupt

    with patch("zabbix_utils.Sender", return_value=fake_sender), \
         patch("time.sleep", side_effect=fake_sleep):
        with pytest.raises(KeyboardInterrupt):
            run([asset], cfg=SimConfig())

    assert fake_sender.send.called, "no batch was ever sent"
    assert send_thread_name["name"] != "MainThread", \
        "send() ran on the main thread — I/O is blocking the scheduler again"


def test_run_drops_batch_when_send_pool_saturated(monkeypatch, caplog):
    """A saturated send pool drops the tick's batch (logged) instead of queuing
    unboundedly — every tick still submits every SIM_POLL_INTERVAL regardless
    of how many sends are still in flight, so an unguarded submit() would pile
    up forever against a slow/down Zabbix server."""
    from otobs import settings
    monkeypatch.setattr(settings, "SIM_SENDER_WORKERS", 1)

    asset = AssetClass("ac", "hg", "tmpl", "tg", [Host("H", "H")], [param("k")])

    release_first_send = threading.Event()
    fake_sender = MagicMock()

    def blocking_send(items):
        release_first_send.wait(timeout=2)  # holds the only worker slot
        return MagicMock(processed=len(items), failed=0)

    fake_sender.send.side_effect = blocking_send

    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    sleep_calls = {"n": 0}

    def fake_sleep(_secs):
        sleep_calls["n"] += 1
        clock["t"] += 100.0  # jump well past next_due so every tick is due again
        if sleep_calls["n"] == 2:
            release_first_send.set()  # let tick 1's send finish after tick 2 has tried
        if sleep_calls["n"] > 3:
            raise KeyboardInterrupt

    with patch("zabbix_utils.Sender", return_value=fake_sender), \
         patch("time.sleep", side_effect=fake_sleep), \
         patch("time.monotonic", side_effect=fake_monotonic), \
         caplog.at_level("WARNING", logger="otobs.simulate"):
        with pytest.raises(KeyboardInterrupt):
            run([asset], cfg=SimConfig())

    assert "sender queue full" in caplog.text, \
        "expected a saturated-pool tick to log a drop instead of piling up"


def _switch_asset():
    """Synthetic switch: 2 per-port prototype params + 1 flat chassis param, 2 hosts."""
    params = [param("net.if.oper_status"), param("net.if.error_rate"), param("net.env.fan_state")]
    disc = Discovery("net.if.discovery", "Interface discovery",
                     ["Gi1/0/1", "Gi1/0/2", "Gi1/0/3"],
                     ["net.if.oper_status", "net.if.error_rate"])
    return AssetClass("Switch / Router", "hg", "Template OT Switch Router", "tg",
                      [Host("SW-A", "SW-A"), Host("SW-B", "SW-B")], params, discovery=disc)


def test_discovery_payload_shape():
    """One LLD trap per host, on the rule key, listing every port under {#IFNAME}."""
    payloads = discovery_payloads([_switch_asset()])
    assert len(payloads) == 2, "expected one discovery trap per host"
    hosts = {h for h, _, _ in payloads}
    assert hosts == {"SW-A", "SW-B"}
    _, key, js = payloads[0]
    assert key == "net.if.discovery"
    assert json.loads(js) == {"data": [{"{#IFNAME}": "Gi1/0/1"},
                                       {"{#IFNAME}": "Gi1/0/2"},
                                       {"{#IFNAME}": "Gi1/0/3"}]}


def test_per_port_stream_expansion():
    """Prototype params fan out to one stream per port with a [<port>] send_key;
    the flat chassis param stays bare; every port reuses the base Parameter."""
    streams = build_streams([_switch_asset()])
    # 2 hosts * (2 protos * 3 ports + 1 flat) = 14
    assert len(streams) == 14, f"got {len(streams)}"
    keys = {s.send_key for s in streams}
    assert "net.if.oper_status[Gi1/0/2]" in keys
    assert "net.if.error_rate[Gi1/0/3]" in keys
    assert "net.env.fan_state" in keys, "flat chassis item must not be per-port"
    assert "net.if.oper_status" not in keys, "prototype must not also emit a bare key"
    op = [s for s in streams if s.param.key == "net.if.oper_status"]
    assert len(op) == 6 and all(s.param.sim is op[0].param.sim for s in op), \
        "per-port streams must share the base Parameter's state machine"


def test_per_port_streams_run_independently():
    """Each port advances its own state machine and emits under its own key."""
    streams = [s for s in build_streams([_switch_asset()])
               if s.host == "SW-A" and s.param.key == "net.if.error_rate"]
    random.seed(0)
    for s in streams:
        v = process_stream(s, 0.0, 1.0, SimConfig(), {}, 0)
        assert v is not None and s.state_idx is not None
    assert {s.send_key for s in streams} == {
        "net.if.error_rate[Gi1/0/1]", "net.if.error_rate[Gi1/0/2]", "net.if.error_rate[Gi1/0/3]"}


def test_comm_link_segment_derivation(assets):
    """A downed physical segment forces EVERY circuit riding it to 'down' together
    (shared-fiber cascade), leaves circuits on healthy segments 'up', and never
    touches independent SCPC circuits. This is the comm-link feature's core claim."""
    comm = next(a for a in assets if a.circuits)
    streams = build_streams([comm])
    by_host = _by_host(streams)
    host = comm.hosts[0].host
    sd = by_host[host]

    # Cut the SHARED Metro-E span (segment1, ridden by circuit1 + circuit2); keep
    # everything else up.
    for key, s in sd.items():
        if key.startswith("seg."):
            s.state_idx = _idx_of_band(s.param.sim, "good")
    cut = sd["seg.pgn_metroe_segment1"]
    cut.state_idx = _idx_of_band(cut.param.sim, "failed")

    forced = segment_forces([comm], by_host)
    # Both circuits on the cut span drop; one on an unrelated (MPLS) segment stays up.
    assert forced[(host, "circ.pgn_metroe_circuit1")] == "failed"
    assert forced[(host, "circ.pgn_metroe_circuit2")] == "failed"   # shares the span
    assert forced[(host, "circ.pgn_mpls_circuit4")] == "good"
    # SCPC circuits are independent — never force-derived.
    assert (host, "circ.pgn_scpc_circuit13") not in forced

    # And the force actually produces the 'down' enum value on the circuit stream.
    circ = sd["circ.pgn_metroe_circuit1"]
    v = process_stream(circ, now=0.0, scale=1.0, cfg=SimConfig(), forced=forced, hour=0.0)
    assert v == 2, f"forced-down circuit emitted {v!r}, want 2 (down)"
    assert circ.param.sim.states[circ.state_idx].band == "failed"

    # A WARNING-level (underperform) segment forces its circuits to 'degraded'
    # (impaired-but-up), not down — the gradual-degradation path.
    cut.state_idx = _idx_of_band(cut.param.sim, "underperform")
    forced = segment_forces([comm], by_host)
    assert forced[(host, "circ.pgn_metroe_circuit1")] == "underperform"
    assert forced[(host, "circ.pgn_metroe_circuit2")] == "underperform"
    circ2 = sd["circ.pgn_metroe_circuit2"]
    v2 = process_stream(circ2, now=0.0, scale=1.0, cfg=SimConfig(), forced=forced, hour=0.0)
    assert v2 == 3, f"degraded circuit emitted {v2!r}, want 3 (degraded)"


def test_hold_dwell_keeps_state_until_expiry():
    """MTTR dwell: once a stream enters a band with a hold window it stays there
    until the window expires, even with stickiness re-rolling and the force gone."""
    cfg = SimConfig(hold=Hold(enabled=True, exact={"k": {"failed": (100.0, 100.0)}}))
    s = Stream("h", param("k"))
    random.seed(0)
    # Force into failed -> arms a fixed 100s dwell (uniform(100,100)) at now=0.
    process_stream(s, 0.0, 1.0, cfg, {("h", "k"): "failed"}, 0.0)
    assert s.param.sim.states[s.state_idx].band == "failed"
    assert s.hold_until == 100.0
    # Inside the window it cannot leave failed, even unforced.
    for t in (10.0, 50.0, 99.0):
        process_stream(s, t, 1.0, cfg, {}, 0.0)
        assert s.param.sim.states[s.state_idx].band == "failed", f"left failed at t={t}"
    # After the window it's free to re-roll and does eventually leave.
    left = any(process_stream(s, float(t), 1.0, cfg, {}, 0.0) is not None
               and s.param.sim.states[s.state_idx].band != "failed"
               for t in range(101, 600))
    assert left, "never left failed after the dwell expired"


def test_hold_disabled_arms_nothing():
    """hold off => hold_until never set, no dwell — the strict no-op guarantee."""
    s = Stream("h", param("k"))
    random.seed(0)
    for t in range(50):
        process_stream(s, float(t), 1.0, SimConfig(), {("h", "k"): "failed"}, 0.0)
    assert s.hold_until == 0.0


def test_comm_link_trigger_tags_scoped_to_down(assets):
    """Only the high/disaster 'down' trigger of a circuit carries the link tag the
    SLA service matches on — a warning-level VSAT loss must not count as downtime."""
    comm = next(a for a in assets if a.circuits)
    vsat = next(c for c in comm.circuits if not c.depends_on)
    tagged = {t.severity: t.tags for t in vsat.param.triggers}
    assert tagged["high"] == [{"tag": "link", "value": vsat.param.key}]
    assert tagged["warning"] == []


def test_real_switch_catalog_has_discovery(assets):
    """The shipped switch catalog marks the four per-port params as prototypes and
    keeps fan_state flat — the load-bearing schema claim for this feature."""
    sw = next(a for a in assets if "Switch" in a.asset_class)
    assert sw.discovery is not None
    assert set(sw.discovery.prototypes) == {
        "net.if.oper_status", "net.if.admin_status", "net.if.error_rate", "net.if.discards"}
    assert "net.env.fan_state" not in sw.discovery.prototypes
    assert len(sw.discovery.ports) >= 2


def test_ladder_steps_one_band_at_a_time():
    """progression.ladder: a re-roll moves one band toward the target — never a
    good->failed teleport, never an instant failed->good heal."""
    sim = numsim()
    random.seed(2)
    from_good = {next_state(sim, 0, 0.0, None, True) for _ in range(2000)}
    assert from_good == {0, 1}, f"ladder let good jump past underperform: {from_good}"
    from_failed = {next_state(sim, 2, 0.0, None, True) for _ in range(2000)}
    assert from_failed == {1, 2}, f"ladder let failed heal past underperform: {from_failed}"


def test_maintenance_absorbing_then_repair():
    """With maintenance on, failed never self-heals; the scheduled visit forces
    every failed stream on the host back to good and re-arms the schedule."""
    from otobs.sim_config import Maintenance
    from otobs.simulate import MaintenanceCrew
    cfg = SimConfig(maintenance=Maintenance(enabled=True, interval_s=1000.0))
    s = Stream("H", param("k"), state_idx=2, last_value=90.0)  # in 'failed'
    bh = _by_host([s])
    crew = MaintenanceCrew(cfg, bh, start=0.0)
    crew.next_visit["H"] = 500.0  # pin the schedule
    random.seed(9)
    for t in range(0, 500, 50):
        forced = crew.forces(bh, float(t))
        assert forced == {}, "crew visited early"
        process_stream(s, float(t), 1.0, cfg, forced, 0)
        assert s.param.sim.states[s.state_idx].band == "failed", "failed self-healed while absorbing"
    forced = crew.forces(bh, 500.0)
    assert forced == {("H", "k"): "good"}, f"visit did not repair: {forced}"
    process_stream(s, 500.0, 1.0, cfg, forced, 0)
    assert s.param.sim.states[s.state_idx].band == "good"
    assert crew.next_visit["H"] == 1500.0, "schedule not re-armed (jitter=0)"


def test_monotonic_counter_only_resets_on_repair():
    """A monotonic counter (SMART sectors) never decreases while self-rolling;
    a forced 'good' (maintenance repair = disk swap) resets it instantly."""
    from otobs.catalog import Sim, State, Parameter
    from otobs.sim_config import Maintenance
    sim = Sim("numeric", [State(0.9, "good", None, 0, 0, 0),
                          State(0.08, "underperform", None, 1, 40, 0),
                          State(0.02, "failed", None, 200, 800, 0)], monotonic=True)
    p = Parameter("io", "io", "unsigned", "", "1h", "c", "col", "fm", "src", sim, [])
    cfg = SimConfig(maintenance=Maintenance(enabled=True))
    s = Stream("H", p, state_idx=1, last_value=25.0)
    random.seed(6)
    prev = 25.0
    for _ in range(200):  # self-rolling: whatever bands it wanders, never a decrease
        v = process_stream(s, 0.0, 1.0, cfg, {}, 0)
        assert v >= prev, f"monotonic counter decreased: {prev} -> {v}"
        prev = v
    v = process_stream(s, 0.0, 1.0, cfg, {("H", "io"): "good"}, 0)  # repair
    assert v == 0, f"repair should reset the counter to the good band, got {v}"


def test_maintenance_event_marker():
    """The event_key stream is crew-driven: 0 normally, 1 on the first reading
    after a visit, back to 0 once the reading window has passed."""
    from otobs.sim_config import Maintenance
    from otobs.simulate import MaintenanceCrew
    cfg = SimConfig(maintenance=Maintenance(enabled=True, interval_s=1000.0,
                                            event_key="k"))
    s = Stream("H", param("k"))  # interval 1m -> 60s reading window
    bh = _by_host([s])
    crew = MaintenanceCrew(cfg, bh, start=0.0)
    crew.next_visit["H"] = 100.0
    assert process_stream(s, 0.0, 1.0, cfg, {}, 0, None, crew) == 0
    crew.forces(bh, 100.0)  # the visit
    assert process_stream(s, 100.0, 1.0, cfg, {}, 0, None, crew) == 1
    assert process_stream(s, 200.0, 1.0, cfg, {}, 0, None, crew) == 0


def test_maintenance_reactive_dispatch():
    """With a dispatch window, a failure triggers an unscheduled repair inside
    [lo, hi] — long before the routine visit — and the visit marker still pings."""
    from otobs.sim_config import Maintenance
    from otobs.simulate import MaintenanceCrew
    cfg = SimConfig(maintenance=Maintenance(enabled=True, interval_s=100000.0,
                                            dispatch=(100.0, 200.0)))
    s = Stream("H", param("k"), state_idx=2, last_value=90.0)  # failed
    bh = _by_host([s])
    random.seed(4)
    crew = MaintenanceCrew(cfg, bh, start=0.0)
    crew.next_visit["H"] = 100000.0  # routine far away
    assert crew.forces(bh, 0.0) == {}  # arms the dispatch, no repair yet
    due = crew.dispatch_due["H"]
    assert 100.0 <= due <= 200.0
    assert crew.forces(bh, due - 1) == {}
    assert crew.forces(bh, due) == {("H", "k"): "good"}, "dispatch did not repair"
    assert "H" not in crew.dispatch_due, "dispatch not cleared after repair"
    assert crew.last_visit["H"] == due, "repair did not register as a visit (event marker)"
    assert crew.next_visit["H"] == 100000.0, "dispatch must not touch the routine schedule"

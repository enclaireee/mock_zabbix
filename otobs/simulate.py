"""Stream catalog-driven mock OT data into Zabbix via Trapper.

Each (host, parameter) is a Stream with a sticky state machine: with probability
STICKINESS it stays in its current Good/Underperform/Failed state, otherwise it
re-rolls by the catalog weights. Sticky states produce long, smooth degradation
stretches — the kind of Underperform curve Tahap 2/3 ML training needs, not flicker.

catalog/sim_config.yml layers optional realism on top (correlation, trend,
time-of-day, dropout, backfill). Every feature defaults OFF and is a strict no-op
when disabled: with all of them off, output is identical to the plain state machine.
"""
from __future__ import annotations
import random
import time
from dataclasses import dataclass
from datetime import datetime

from . import settings
from .catalog import AssetClass, Parameter, Sim, State, load_all
from .sim_config import SimConfig, load_sim_config

try:  # stdlib since 3.9; fall back to system-local time if tzdata is missing.
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(settings.TIMEZONE)
except Exception:  # noqa: BLE001
    _TZ = None


def _hour(unix_ts: float) -> int:
    return datetime.fromtimestamp(unix_ts, _TZ).hour


def sample(sim: Sim, state: State, value_type: str = "float"):
    """Produce one reading for the given state, typed for the Zabbix item."""
    if sim.kind == "enum":
        return state.value
    v = random.uniform(state.lo, state.hi) + random.gauss(0, state.jitter)
    v = max(state.lo - state.jitter, min(state.hi + state.jitter, v))
    return int(round(v)) if value_type == "unsigned" else round(v, 3)


def _typed(v: float, value_type: str):
    return int(round(v)) if value_type == "unsigned" else round(v, 3)


def sample_stream(s: "Stream", st: State, now: float, scale: float,
                  cfg: SimConfig, hour: int):
    """Value for this tick honoring trend ramp + time-of-day. Falls back to the
    exact original sample() when neither is engaged — the strict no-op path."""
    sim = s.param.sim
    if sim.kind == "enum":
        return st.value
    ramp_dur = cfg.trend.ramp_for(s.param.key) / scale if cfg.trend.enabled else 0.0
    ramping = (cfg.trend.enabled and s.ramp_to is not None
               and ramp_dur > 0 and (now - s.ramp_start) < ramp_dur)
    mult = cfg.time_of_day.multiplier(s.param.key, hour)
    if not ramping and mult == 1.0:
        return sample(sim, st, s.param.value_type)  # identical draws to legacy path
    if ramping:
        frac = (now - s.ramp_start) / ramp_dur
        base = s.ramp_from + (s.ramp_to - s.ramp_from) * frac
    else:
        base = random.uniform(st.lo, st.hi)
    # Ramp/time-of-day deliberately traverse or shift out of band — no band clamp here.
    v = base * mult + random.gauss(0, st.jitter)
    return _typed(v, s.param.value_type)


def next_state(sim: Sim, cur: int | None, stickiness: float,
               forced_idx: int | None = None) -> int:
    if forced_idx is not None:  # correlation force overrides stickiness + weights
        return forced_idx
    if cur is not None and random.random() < stickiness:
        return cur
    r, acc = random.random(), 0.0
    weights = sim.normalized_weights()
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return i
    return len(weights) - 1


def _idx_of_band(sim: Sim, band: str) -> int | None:
    for i, st in enumerate(sim.states):
        if st.band == band:
            return i
    return None


@dataclass
class Stream:
    host: str
    param: Parameter
    state_idx: int | None = None
    next_due: float = 0.0
    last_value: float | None = None
    ramp_from: float = 0.0
    ramp_to: float | None = None
    ramp_start: float = 0.0


def build_streams(assets: list[AssetClass]) -> list[Stream]:
    streams = []
    for a in assets:
        for h in a.hosts:
            for p in a.parameters:
                streams.append(Stream(host=h.host, param=p))
    return streams


def _by_host(streams: list[Stream]) -> dict[str, dict[str, Stream]]:
    idx: dict[str, dict[str, Stream]] = {}
    for s in streams:
        idx.setdefault(s.host, {})[s.param.key] = s
    return idx


def correlation_forces(cfg: SimConfig, by_host: dict) -> dict:
    """{(host, param_key): bias_band} for streams to force this tick, based on
    each trigger param's CURRENT (pre-roll) band. Per-host; composable — the
    first firing group wins for a given target."""
    forced: dict = {}
    if not cfg.correlation.enabled:
        return forced
    for host, sd in by_host.items():
        for g in cfg.correlation.groups:
            trig = sd.get(g.trigger_param)
            if trig is None or trig.state_idx is None:
                continue
            if trig.param.sim.states[trig.state_idx].band != g.trigger_band:
                continue
            for a in g.affects:
                key = (host, a.param)
                if a.param not in sd or key in forced:
                    continue
                if random.random() < a.strength:
                    forced[key] = a.bias_band
    return forced


def process_stream(s: Stream, now: float, scale: float, cfg: SimConfig,
                   forced: dict, hour: int):
    """Advance one due stream one tick. Returns the emitted value, or None if the
    reading was dropped. Shared by live run() and backfill(). Caller owns next_due."""
    if cfg.dropout.enabled and random.random() < cfg.dropout.prob_for(s.param.key):
        return None  # missed reading: state frozen, no emit (exercises nodata())
    fb = forced.get((s.host, s.param.key))
    forced_idx = _idx_of_band(s.param.sim, fb) if fb is not None else None
    new_idx = next_state(s.param.sim, s.state_idx, settings.STICKINESS, forced_idx)
    transitioned = new_idx != s.state_idx
    s.state_idx = new_idx
    st = s.param.sim.states[new_idx]
    if (cfg.trend.enabled and s.param.sim.kind == "numeric"
            and transitioned and s.last_value is not None):
        s.ramp_from = s.last_value
        s.ramp_to = random.uniform(st.lo, st.hi)
        s.ramp_start = now
    value = sample_stream(s, st, now, scale, cfg, hour)
    s.last_value = value
    return value


def _note(s: Stream, value) -> str | None:
    st = s.param.sim.states[s.state_idx]
    return f"{s.host}/{s.param.key}={value}({st.band})" if st.band != "good" else None


def run(assets: list[AssetClass], cfg: SimConfig | None = None) -> None:
    from zabbix_utils import ItemValue, Sender  # lazy: keeps `check`/`list` offline

    cfg = cfg or load_sim_config()
    streams = build_streams(assets)
    by_host = _by_host(streams)
    sender = Sender(server=settings.SENDER_HOST, port=settings.SENDER_PORT)
    scale = max(settings.TIME_SCALE, 0.001)
    feats = ", ".join(cfg.enabled_features()) or "none"
    print(f"Streaming {len(streams)} items -> {settings.SENDER_HOST}:{settings.SENDER_PORT} "
          f"(stickiness={settings.STICKINESS}, time_scale={scale}x, sim-config: {feats}). "
          f"Ctrl+C to stop.")

    while True:
        now = time.monotonic()
        hour = _hour(time.time()) if cfg.time_of_day.enabled else 0
        forced = correlation_forces(cfg, by_host)
        batch, notes = [], []
        for s in streams:
            if s.next_due > now:
                continue
            s.next_due = now + s.param.interval_s / scale
            value = process_stream(s, now, scale, cfg, forced, hour)
            if value is None:
                continue
            batch.append(ItemValue(s.host, s.param.key, str(value)))
            n = _note(s, value)
            if n:
                notes.append(n)

        if batch:
            try:
                resp = sender.send(batch)
                ok = getattr(resp, "processed", "?")
                fail = getattr(resp, "failed", "?")
                ts = time.strftime("%H:%M:%S")
                tail = ("  | " + ", ".join(notes[:4]) + ("…" if len(notes) > 4 else "")) if notes else ""
                print(f"{ts}  sent={len(batch)} processed={ok} failed={fail}{tail}")
            except Exception as e:  # noqa: BLE001
                print(f"send error: {e}")
        time.sleep(0.5)


def run_backfill(assets: list[AssetClass], cfg: SimConfig | None = None,
                 days: float | None = None, speed: float | None = None) -> None:
    """Discrete-event sweep of the same state machine from now-`days` to now,
    sending each value with its historical `clock`. Intervals are real (no
    TIME_SCALE) so timestamps are physically correct; `speed` compresses wall time.
    """
    from zabbix_utils import ItemValue, Sender

    cfg = cfg or load_sim_config()
    days = float(days if days is not None else cfg.backfill.days)
    speed = max(float(speed if speed is not None else cfg.backfill.speed_multiplier), 0.001)
    streams = build_streams(assets)
    by_host = _by_host(streams)
    sender = Sender(server=settings.SENDER_HOST, port=settings.SENDER_PORT)

    end = time.time()
    vt = end - days * 86400.0
    for s in streams:
        s.next_due = vt
    print(f"Backfilling {days:g}d for {len(streams)} items at {speed:g}x "
          f"-> {settings.SENDER_HOST}:{settings.SENDER_PORT} ...")

    FLUSH = 500  # ponytail: fixed batch size; raise if the trapper backpressures.
    batch, sent = [], 0

    def flush():
        nonlocal sent
        if not batch:
            return
        try:
            sender.send(batch)
            sent += len(batch)
        except Exception as e:  # noqa: BLE001
            print(f"send error: {e}")
        batch.clear()

    while vt < end:
        hour = _hour(vt) if cfg.time_of_day.enabled else 0
        forced = correlation_forces(cfg, by_host)
        for s in streams:
            if s.next_due > vt:
                continue
            s.next_due += s.param.interval_s  # real interval -> correct historical spacing
            value = process_stream(s, vt, 1.0, cfg, forced, hour)
            if value is None:
                continue
            batch.append(ItemValue(s.host, s.param.key, str(value), clock=int(vt)))
            if len(batch) >= FLUSH:
                flush()
        prev = vt
        vt = min(s.next_due for s in streams)  # ponytail: O(n) per event, fine for ~100s of streams
        time.sleep(min((vt - prev) / speed, 5.0))
    flush()
    day = datetime.fromtimestamp(end - days * 86400.0, _TZ).date()
    print(f"Backfill done: {sent} points from {day} to now.")


def main() -> None:
    try:
        run(load_all())
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()

"""Stream catalog-driven mock OT data into Zabbix via Trapper.

Each (host, parameter) is a Stream with a sticky state machine: with probability
STICKINESS it stays in its current Good/Underperform/Failed state, otherwise it
re-rolls by the catalog weights. Sticky states produce long, smooth degradation
stretches — the kind of Underperform curve Tahap 2/3 ML training needs, not flicker.
"""
from __future__ import annotations
import random
import time
from dataclasses import dataclass

from zabbix_utils import ItemValue, Sender

from . import settings
from .catalog import AssetClass, Parameter, Sim, State, load_all


def sample(sim: Sim, state: State, value_type: str = "float"):
    """Produce one reading for the given state, typed for the Zabbix item."""
    if sim.kind == "enum":
        return state.value
    v = random.uniform(state.lo, state.hi) + random.gauss(0, state.jitter)
    v = max(state.lo - state.jitter, min(state.hi + state.jitter, v))  # keep near band
    return int(round(v)) if value_type == "unsigned" else round(v, 3)


def next_state(sim: Sim, cur: int | None, stickiness: float) -> int:
    if cur is not None and random.random() < stickiness:
        return cur
    r, acc = random.random(), 0.0
    weights = sim.normalized_weights()
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return i
    return len(weights) - 1


@dataclass
class Stream:
    host: str
    param: Parameter
    state_idx: int | None = None
    next_due: float = 0.0


def build_streams(assets: list[AssetClass]) -> list[Stream]:
    streams = []
    for a in assets:
        for h in a.hosts:
            for p in a.parameters:
                streams.append(Stream(host=h.host, param=p))
    return streams


def run(assets: list[AssetClass]) -> None:
    streams = build_streams(assets)
    sender = Sender(server=settings.SENDER_HOST, port=settings.SENDER_PORT)
    scale = max(settings.TIME_SCALE, 0.001)
    print(f"Streaming {len(streams)} items -> {settings.SENDER_HOST}:{settings.SENDER_PORT} "
          f"(stickiness={settings.STICKINESS}, time_scale={scale}x). Ctrl+C to stop.")

    while True:
        now = time.monotonic()
        batch, notes = [], []
        for s in streams:
            if s.next_due > now:
                continue
            s.state_idx = next_state(s.param.sim, s.state_idx, settings.STICKINESS)
            st = s.param.sim.states[s.state_idx]
            value = sample(s.param.sim, st, s.param.value_type)
            batch.append(ItemValue(s.host, s.param.key, str(value)))
            if st.band != "good":
                notes.append(f"{s.host}/{s.param.key}={value}({st.band})")
            s.next_due = now + s.param.interval_s / scale

        if batch:
            try:
                resp = sender.send(batch)
                ok = getattr(resp, "processed", "?")
                fail = getattr(resp, "failed", "?")
                ts = time.strftime("%H:%M:%S")
                tail = ("  | " + ", ".join(notes[:4]) + ("…" if len(notes) > 4 else "")) if notes else ""
                print(f"{ts}  sent={len(batch)} processed={ok} failed={fail}{tail}")
            except Exception as e:  # noqa: BLE001 — keep the lab running through blips
                print(f"send error: {e}")
        time.sleep(0.5)


def main() -> None:
    try:
        run(load_all())
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()

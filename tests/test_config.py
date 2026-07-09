"""Pytest suite for catalog parsing/validation and sim_config schema validation —
the load-time guards that must fail loudly rather than misbehave deep in the sim loop."""
from __future__ import annotations
import pytest

from otobs.catalog import (Sim, State, parse_interval, _build_sim, _build_param,
                           _build_discovery, load_all)
from otobs.sim_config import (SimConfig, Correlation, CorrGroup, Affect, Trend,
                              Dropout, TimeOfDay, TodProfile, validate, load_sim_config_file)
from otobs.settings import PRESETS_DIR


def test_numeric_weights_length_guard():
    """A 2-entry weights list must fail at load, not silently drop the third band."""
    raw = {"kind": "numeric", "good": [0, 1], "underperform": [1, 2], "failed": [2, 3],
           "weights": ["good", "underperform"]}
    with pytest.raises(ValueError):
        _build_sim(raw, "x")


def test_presets_validate_against_catalog(assets):
    """Every shipped preset must reference only real param keys/bands, so a typo in
    a mode file is caught here, not at `make config` time."""
    bands = {p.key: {st.band for st in p.sim.states} for a in assets for p in a.parameters}
    numeric = {p.key for a in assets for p in a.parameters if p.sim.kind == "numeric"}
    files = sorted(PRESETS_DIR.glob("*.yml"))
    assert files, "no presets found"
    for f in files:
        validate(load_sim_config_file(f), bands, numeric)  # raises on a bad reference


def test_validate_rejects_dead_enum_config():
    """A trend override or ToD profile on an enum param is a silent no-op —
    with numeric_keys given, validate() must reject it. Dropout stays legal."""
    bands = {"mode": {"good", "failed"}, "temp": {"good", "underperform", "failed"}}
    numeric = {"temp"}  # "mode" is enum
    ok = SimConfig(trend=Trend(True, 1800, {"temp": 900}),
                   time_of_day=TimeOfDay(True, {"temp": TodProfile(8, 17, 1.2, 0.8)}),
                   dropout=Dropout(True, 0.1, {"mode": 0.0}))
    validate(ok, bands, numeric)  # numeric targets + enum dropout: all fine
    for bad in (SimConfig(trend=Trend(True, 1800, {"mode": 900})),
                SimConfig(time_of_day=TimeOfDay(True, {"mode": TodProfile(8, 17, 1.2, 0.8)}))):
        with pytest.raises(ValueError, match="enum"):
            validate(bad, bands, numeric)
        validate(bad, bands)  # without numeric_keys: old lenient behavior


@pytest.mark.parametrize("bad_cfg_kwargs", [
    {"trigger_param": "nope", "trigger_band": "failed"},
    {"trigger_param": "fan", "trigger_band": "on_fire"},
])
def test_validate_catches_typos(bad_cfg_kwargs):
    bands = {"fan": {"good", "failed"}, "temp": {"good", "underperform"}}
    good = SimConfig(correlation=Correlation(
        True, [CorrGroup("g", "fan", "failed", [Affect("temp", "underperform", 0.5)])]))
    validate(good, bands)  # no raise
    bad = SimConfig(correlation=Correlation(
        True, [CorrGroup("g", bad_cfg_kwargs["trigger_param"],
                         bad_cfg_kwargs["trigger_band"], [])]))
    with pytest.raises(ValueError):
        validate(bad, bands)


@pytest.mark.parametrize("bad_interval", ["0s", "0m", "00s"])
def test_bad_interval_rejected(bad_interval):
    """A 0 interval would make the backfill scheduler and live loop spin forever."""
    with pytest.raises(ValueError):
        parse_interval(bad_interval)


def test_good_intervals_parse():
    assert parse_interval("15s") == 15
    assert parse_interval("1h") == 3600


def test_all_zero_weights_rejected():
    """All-zero weights would divide by zero later in next_state()."""
    with pytest.raises(ValueError):
        Sim("numeric", [State(0, "good", None, 0, 1, 0)])


def test_numeric_weight_enum_stays_distinct():
    sim = _build_sim({"kind": "enum",
                      "states": [{"value": 8, "weight": 3}, {"value": 6, "weight": 1}]}, "x")
    assert sim.states[0].band != sim.states[1].band, "numeric-weight enum bands collided"


def test_bad_trigger_field_rejected():
    with pytest.raises(ValueError):
        _build_param({"key": "k", "name": "n", "value_type": "float", "interval": "5s",
                      "component": "c", "collection": "c", "failure_mode": "f", "source": "s",
                      "sim": {"kind": "numeric", "good": [0, 1], "underperform": [1, 2],
                              "failed": [2, 3]},
                      "triggers": [{"op": ">=", "value": 1, "severity": "warning",
                                    "label": "l", "bogus": 1}]}, "x")


def test_discovery_validation():
    pk = {"net.if.oper_status", "net.env.fan_state"}
    ok = _build_discovery({"key": "net.if.discovery", "name": "d", "ports": ["Gi1/0/1"],
                           "prototypes": ["net.if.oper_status"]}, pk, "x")
    assert ok.macro == "{#IFNAME}" and ok.ports == ["Gi1/0/1"]


@pytest.mark.parametrize("bad", [
    {"key": "net.if.discovery", "name": "d", "ports": [], "prototypes": ["net.if.oper_status"]},
    {"key": "net.if.discovery", "name": "d", "ports": ["a", "a"], "prototypes": ["net.if.oper_status"]},
    {"key": "net.if.discovery", "name": "d", "ports": ["Gi1/0/1"], "prototypes": ["nope"]},
    {"key": "net.if.oper_status", "name": "d", "ports": ["Gi1/0/1"], "prototypes": ["net.if.oper_status"]},
    {"key": "net.if.discovery", "name": "d", "ports": ["Gi1/0/1"], "prototypes": ["net.if.oper_status"],
     "macro": "IFNAME"},
])
def test_discovery_validation_rejects(bad):
    pk = {"net.if.oper_status", "net.env.fan_state"}
    with pytest.raises(ValueError):
        _build_discovery(bad, pk, "x")

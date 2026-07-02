"""Load + validate catalog/*.yml into typed objects. Single source of truth."""
from __future__ import annotations
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
import yaml

from .settings import CATALOG_DIR

DEFAULT_WEIGHTS = {"good": 0.90, "underperform": 0.08, "failed": 0.02}

_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600}

VALUE_TYPE_CODE = {"float": 0, "char": 1, "log": 2, "unsigned": 3, "text": 4}
SEVERITY_CODE = {
    "not_classified": 0, "info": 1, "warning": 2,
    "average": 3, "high": 4, "disaster": 5,
}


def parse_interval(s: str) -> int:
    """'15s' -> 15, '5m' -> 300, '1h' -> 3600. Must be > 0 (a 0 interval would
    make the backfill scheduler and live loop spin forever)."""
    s = str(s).strip()
    unit = _INTERVAL_UNITS.get(s[-1:])
    if unit is None or not s[:-1].isdigit():
        raise ValueError(f"bad interval {s!r} (use e.g. 15s, 1m, 1h)")
    secs = int(s[:-1]) * unit
    if secs <= 0:
        raise ValueError(f"interval {s!r} must be > 0")
    return secs


def _weight(w) -> float:
    if isinstance(w, (int, float)):
        return float(w)
    if w in DEFAULT_WEIGHTS:
        return DEFAULT_WEIGHTS[w]
    raise ValueError(f"bad weight {w!r}: number or one of {list(DEFAULT_WEIGHTS)}")


@dataclass
class State:
    """One discrete outcome the simulator can be in (enum value or numeric band)."""
    weight: float
    band: str
    value: object = None
    lo: float | None = None
    hi: float | None = None
    jitter: float = 0.0


@dataclass
class Trigger:
    op: str
    value: float
    severity: str
    label: str
    func: str = "last"

    def __post_init__(self):
        if self.severity not in SEVERITY_CODE:
            raise ValueError(f"bad severity {self.severity!r}")
        if self.op not in {"=", "<>", ">", ">=", "<", "<="}:
            raise ValueError(f"bad trigger op {self.op!r}")


@dataclass
class Sim:
    kind: str
    states: list[State]

    def __post_init__(self):
        # A zero total would make normalized_weights() divide by zero at sample
        # time; fail loudly at load instead, like every other bad-catalog case.
        if sum(s.weight for s in self.states) <= 0:
            raise ValueError(f"{self.kind} sim: state weights sum to 0")

    def normalized_weights(self) -> list[float]:
        total = sum(s.weight for s in self.states)
        return [s.weight / total for s in self.states]


@dataclass
class Parameter:
    key: str
    name: str
    value_type: str
    units: str
    interval: str
    component: str
    collection: str
    failure_mode: str
    source: str
    sim: Sim
    triggers: list[Trigger] = field(default_factory=list)

    @cached_property
    def interval_s(self) -> int:
        # Parsed once per parameter, not re-parsed on every tick of the live loop.
        return parse_interval(self.interval)

    @property
    def value_type_code(self) -> int:
        return VALUE_TYPE_CODE[self.value_type]

    def description(self) -> str:
        """Living documentation embedded into the Zabbix item."""
        return (
            f"[{self.component}] {self.failure_mode}\n"
            f"Collection: {self.collection}\n"
            f"Expected interval: {self.interval} | Source: {self.source}"
        )


@dataclass
class Host:
    host: str
    name: str
    macros: dict = field(default_factory=dict)
    inventory: dict = field(default_factory=dict)


@dataclass
class AssetClass:
    asset_class: str
    host_group: str
    template_name: str
    template_group: str
    hosts: list[Host]
    parameters: list[Parameter]


def _build_sim(raw: dict, where: str) -> Sim:
    kind = raw.get("kind")
    if kind == "enum":
        states = []
        for st in raw.get("states", []):
            if "value" not in st or "weight" not in st:
                raise ValueError(f"{where}: enum state needs 'value' and 'weight': {st!r}")
            # A string weight IS the band (good/underperform/failed). A numeric
            # weight has no band token, so key the band off the value to keep each
            # state distinct — otherwise every such state collapses to one "custom"
            # band and correlation/_idx_of_band can't tell them apart.
            band = st["weight"] if isinstance(st["weight"], str) else f"custom:{st['value']}"
            states.append(State(weight=_weight(st["weight"]), band=band, value=st["value"]))
        if not states:
            raise ValueError(f"{where}: enum sim has no states")
        return Sim(kind, states)
    if kind == "numeric":
        weights = raw.get("weights", ["good", "underperform", "failed"])
        states = []
        for band, w in zip(("good", "underperform", "failed"), weights):
            if band not in raw:
                raise ValueError(f"{where}: numeric sim missing band {band!r} (need good/underperform/failed)")
            try:
                lo, hi = raw[band]
            except (TypeError, ValueError):
                raise ValueError(f"{where}: band {band!r} must be [min, max], got {raw[band]!r}")
            states.append(State(weight=_weight(w), band=band,
                                 lo=float(lo), hi=float(hi),
                                 jitter=float(raw.get("jitter", 0.0))))
        return Sim(kind, states)
    raise ValueError(f"{where}: bad sim.kind {kind!r} (numeric|enum)")


def _build_param(raw: dict, where: str) -> Parameter:
    required = ["key", "name", "value_type", "interval", "component",
                "collection", "failure_mode", "source", "sim"]
    for r in required:
        if r not in raw:
            raise ValueError(f"{where}: parameter missing {r!r}")
    if raw["value_type"] not in VALUE_TYPE_CODE:
        raise ValueError(f"{where}: bad value_type {raw['value_type']!r}")
    parse_interval(raw["interval"])
    try:
        triggers = [Trigger(**t) for t in raw.get("triggers", [])]
    except TypeError as e:  # unknown/missing trigger field -> ValueError like the rest
        raise ValueError(f"{where}.{raw.get('key','?')}: bad trigger fields: {e}")
    return Parameter(
        key=raw["key"], name=raw["name"], value_type=raw["value_type"],
        units=raw.get("units", ""), interval=raw["interval"],
        component=raw["component"], collection=raw["collection"],
        failure_mode=raw["failure_mode"], source=raw["source"],
        sim=_build_sim(raw["sim"], f"{where}.{raw['key']}"),
        triggers=triggers,
    )


def load_sites(directory: Path) -> list[dict]:
    """Station registry (sites.yml). One entry per physical site; asset classes
    generate a host each from it. Empty list if the file is absent."""
    f = directory / "sites.yml"
    if not f.exists():
        return []
    raw = yaml.safe_load(f.read_text()) or {}
    sites = raw.get("sites", [])
    for s in sites:
        for req in ("code", "name", "lat", "lon", "location"):
            if req not in s:
                raise ValueError(f"sites.yml: site missing {req!r}: {s!r}")
    return sites


def _expand_hosts(tmpl: dict, sites: list[dict], where: str) -> list[Host]:
    """Build one Host per site from a host_template (fields formatted with the site)."""
    if "tech" not in tmpl:
        raise ValueError(f"{where}: host_template needs 'tech'")
    hosts = []
    for s in sites:
        try:
            tech = tmpl["tech"].format(**s)
            name = tmpl.get("name", "{name}").format(**s)
            macros = {k: str(v).format(**s) for k, v in tmpl.get("macros", {}).items()}
        except KeyError as e:
            raise ValueError(f"{where}: host_template references unknown site field {e}")
        inv = {"location": s["location"], "location_lat": s["lat"], "location_lon": s["lon"],
               "site_city": s.get("city", ""), "site_country": "Indonesia"}
        hosts.append(Host(host=tech, name=name, macros=macros, inventory=inv))
    return hosts


def load_file(path: Path, sites: list[dict] | None = None) -> AssetClass:
    sites = sites or []
    raw = yaml.safe_load(path.read_text())
    for r in ["asset_class", "host_group", "template_name", "template_group", "parameters"]:
        if r not in raw:
            raise ValueError(f"{path.name}: missing top-level {r!r}")
    if "host_template" in raw:
        if not sites:
            raise ValueError(f"{path.name}: uses host_template but sites.yml is empty/missing")
        hosts = _expand_hosts(raw["host_template"], sites, path.name)
    elif "hosts" in raw:
        hosts = [Host(host=h["host"], name=h.get("name", h["host"]),
                      macros=h.get("macros", {}), inventory=h.get("inventory", {}))
                 for h in raw["hosts"]]
    else:
        raise ValueError(f"{path.name}: needs 'host_template' (with sites.yml) or 'hosts'")
    params = [_build_param(p, path.name) for p in raw["parameters"]]
    keys = [p.key for p in params]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path.name}: duplicate item keys")
    return AssetClass(
        asset_class=raw["asset_class"], host_group=raw["host_group"],
        template_name=raw["template_name"], template_group=raw["template_group"],
        hosts=hosts, parameters=params,
    )


def load_all(directory: Path | None = None) -> list[AssetClass]:
    directory = directory or CATALOG_DIR
    sites = load_sites(directory)
    files = sorted(p for p in directory.glob("*.yml")
                   if p.name not in ("sites.yml", "sim_config.yml"))
    if not files:
        raise ValueError(f"no catalog *.yml in {directory}")
    assets = [load_file(p, sites) for p in files]
    # Keys must be unique across ALL files, not just within one: sim_config
    # validation and the trapper both address items by bare key, so a cross-file
    # collision would silently shadow one parameter.
    seen: dict[str, str] = {}
    for a in assets:
        for p in a.parameters:
            if p.key in seen:
                raise ValueError(f"duplicate item key {p.key!r} in {a.asset_class!r} "
                                 f"(already defined in {seen[p.key]!r})")
            seen[p.key] = a.asset_class
    return assets

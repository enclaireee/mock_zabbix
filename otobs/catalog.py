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
    tags: list[dict] = field(default_factory=list)  # Zabbix event tags (SLA service matching)

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
        total = sum(s.weight for s in self.states)
        if total <= 0:
            raise ValueError(f"{self.kind} sim: state weights sum to 0")
        self._normalized = [s.weight / total for s in self.states]

    def normalized_weights(self) -> list[float]:
        return self._normalized


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
    depends_on: list[str] = field(default_factory=list)  # comm-link circuits: segment keys it rides

    @cached_property
    def interval_s(self) -> int:
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
class Discovery:
    """A simulated SNMP Low-Level Discovery rule, fed by trapper. On a real switch
    an SNMP walk of IF-MIB enumerates ports; here `ports` is the lab's stand-in for
    that walk. Each param key in `prototypes` becomes a per-port item/trigger
    *prototype* keyed `<key>[<port>]`; every other param stays a flat per-host item."""
    key: str
    name: str
    ports: list[str]
    prototypes: list[str]
    macro: str = "{#IFNAME}"


@dataclass
class Segment:
    """A physical media path that can fail as a unit — a fiber span between two
    stations, or an MPLS backhaul. Its `param` is a normal Zabbix item (enum
    up/down, like net.if.oper_status). Circuits ride segments; a segment down
    drops every circuit on it together."""
    param: Parameter


@dataclass
class Circuit:
    """One of the report's named logical links. Rides one or more `depends_on`
    segments (its state = worst of them) — or, for VSAT-IP, rides none and is
    simulated independently as an ICMP ping-loss check. `media` is the report's
    transport label (Metro-E / VSAT-IP / MPLS), embedded for documentation."""
    param: Parameter
    depends_on: list[str]
    media: str = ""


@dataclass
class AssetClass:
    asset_class: str
    host_group: str
    template_name: str
    template_group: str
    hosts: list[Host]
    parameters: list[Parameter]
    discovery: Discovery | None = None
    segments: list[Segment] = field(default_factory=list)
    circuits: list[Circuit] = field(default_factory=list)


def _build_sim(raw: dict, where: str) -> Sim:
    kind = raw.get("kind")
    if kind == "enum":
        states = []
        for st in raw.get("states", []):
            if "value" not in st or "weight" not in st:
                raise ValueError(f"{where}: enum state needs 'value' and 'weight': {st!r}")
            band = st["weight"] if isinstance(st["weight"], str) else f"custom:{st['value']}"
            states.append(State(weight=_weight(st["weight"]), band=band, value=st["value"]))
        if not states:
            raise ValueError(f"{where}: enum sim has no states")
        return Sim(kind, states)
    if kind == "numeric":
        weights = raw.get("weights", ["good", "underperform", "failed"])
        if len(weights) != 3:
            raise ValueError(f"{where}: weights needs 3 entries (good/underperform/failed), "
                             f"got {len(weights)}")
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
    except TypeError as e:
        raise ValueError(f"{where}.{raw.get('key','?')}: bad trigger fields: {e}")
    return Parameter(
        key=raw["key"], name=raw["name"], value_type=raw["value_type"],
        units=raw.get("units", ""), interval=raw["interval"],
        component=raw["component"], collection=raw["collection"],
        failure_mode=raw["failure_mode"], source=raw["source"],
        sim=_build_sim(raw["sim"], f"{where}.{raw['key']}"),
        triggers=triggers,
    )


def _build_discovery(raw: dict, param_keys: set[str], where: str) -> Discovery:
    for r in ("key", "name", "ports", "prototypes"):
        if r not in raw:
            raise ValueError(f"{where}: discovery missing {r!r}")
    macro = raw.get("macro", "{#IFNAME}")
    if not (isinstance(macro, str) and macro.startswith("{#") and macro.endswith("}")):
        raise ValueError(f"{where}: discovery.macro {macro!r} must look like '{{#IFNAME}}'")
    ports = raw["ports"]
    if not isinstance(ports, list) or not ports:
        raise ValueError(f"{where}: discovery.ports must be a non-empty list")
    if not all(isinstance(p, str) and p for p in ports):
        raise ValueError(f"{where}: discovery.ports must all be non-empty strings, got {ports!r}")
    if len(ports) != len(set(ports)):
        raise ValueError(f"{where}: discovery.ports has duplicates")
    protos = raw["prototypes"]
    if not isinstance(protos, list) or not protos:
        raise ValueError(f"{where}: discovery.prototypes must be a non-empty list")
    missing = [k for k in protos if k not in param_keys]
    if missing:
        raise ValueError(f"{where}: discovery.prototypes reference unknown keys {missing}")
    if raw["key"] in param_keys:
        raise ValueError(f"{where}: discovery.key {raw['key']!r} collides with a parameter key")
    return Discovery(key=raw["key"], name=raw["name"], ports=ports,
                     prototypes=protos, macro=macro)


def _build_comm_links(raw: dict, where: str) -> tuple[list[Segment], list[Circuit], list[Parameter]]:
    """Compile the two-layer comm-link schema (segments + circuits) into typed
    objects plus the flat Parameter list the provisioner/simulator already understand.

    Segments and circuits are both ordinary Zabbix items under the hood; the only
    new data is a circuit's `depends_on` (which segments it rides) and `media`.
    Every circuit's high/disaster 'down' trigger is auto-tagged `link:<key>` so the
    SLA service layer (otobs/sla.py) can match its downtime problems."""
    segs_raw = raw.get("segments") or []
    circ_raw = raw.get("circuits") or []
    if not segs_raw:
        raise ValueError(f"{where}: comm-link catalog needs a non-empty 'segments:' list")
    if not circ_raw:
        raise ValueError(f"{where}: comm-link catalog needs a non-empty 'circuits:' list")

    segments = [Segment(param=_build_param(s, f"{where} segment")) for s in segs_raw]
    seg_keys = {s.param.key for s in segments}

    circuits: list[Circuit] = []
    for c in circ_raw:
        deps = c.get("depends_on") or []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError(f"{where}: circuit {c.get('key','?')!r} depends_on must be a list of segment keys")
        param = _build_param(c, f"{where} circuit")
        param.depends_on = list(deps)
        for t in param.triggers:  
            if t.severity in ("high", "disaster"):
                t.tags = [*t.tags, {"tag": "link", "value": param.key}]
        circuits.append(Circuit(param=param, depends_on=list(deps), media=c.get("media", "")))

    for c in circuits:
        missing = [d for d in c.depends_on if d not in seg_keys]
        if missing:
            raise ValueError(f"{where}: circuit {c.param.key!r} depends_on unknown segment(s) {missing}")

    params = [s.param for s in segments] + [c.param for c in circuits]
    return segments, circuits, params


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
    is_comm = "segments" in raw or "circuits" in raw
    required = ["asset_class", "host_group", "template_name", "template_group"]
    if not is_comm:
        required.append("parameters")
    for r in required:
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
    if is_comm:
        segments, circuits, params = _build_comm_links(raw, path.name)
    else:
        segments, circuits = [], []
        params = [_build_param(p, path.name) for p in raw["parameters"]]
    keys = [p.key for p in params]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path.name}: duplicate item keys")
    discovery = _build_discovery(raw["discovery"], set(keys), path.name) \
        if "discovery" in raw else None
    return AssetClass(
        asset_class=raw["asset_class"], host_group=raw["host_group"],
        template_name=raw["template_name"], template_group=raw["template_group"],
        hosts=hosts, parameters=params, discovery=discovery,
        segments=segments, circuits=circuits,
    )


def load_all(directory: Path | None = None) -> list[AssetClass]:
    directory = directory or CATALOG_DIR
    sites = load_sites(directory)
    files = sorted(p for p in directory.glob("*.yml")
                   if p.name not in ("sites.yml", "sim_config.yml"))
    if not files:
        raise ValueError(f"no catalog *.yml in {directory}")
    assets = [load_file(p, sites) for p in files]
    seen: dict[str, str] = {}
    for a in assets:
        for p in a.parameters:
            if p.key in seen:
                raise ValueError(f"duplicate item key {p.key!r} in {a.asset_class!r} "
                                 f"(already defined in {seen[p.key]!r})")
            seen[p.key] = a.asset_class
    return assets

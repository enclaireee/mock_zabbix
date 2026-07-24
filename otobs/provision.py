"""Provision Zabbix from the catalog: template groups, host groups, templates,
trapper items, triggers, and hosts. Idempotent — safe to re-run.

Items and triggers live on one Template per asset class; each host links its
template, so a metric is defined once regardless of how many stations exist."""
from __future__ import annotations
import logging

from zabbix_utils import ModuleBaseException, ZabbixAPI

from . import settings, sla
from .catalog import AssetClass, Parameter, SEVERITY_CODE, load_all

log = logging.getLogger(__name__)


class Provisioner:
    def __init__(self) -> None:
        self.api = ZabbixAPI(url=settings.API_URL)
        self.api.login(user=settings.API_USER, password=settings.API_PASSWORD)
        log.info("Connected to Zabbix API %s as %s", self.api.api_version(), settings.API_USER)
        self.errors: list[str] = []

    def close(self) -> None:
        try:
            self.api.logout()
        except ModuleBaseException:
            pass

    def _fail(self, where: str, e: Exception) -> None:
        """Record a failed operation and keep going — one rejected item/trigger/
        host (e.g. Zabbix refusing a value_type change on an item with history)
        must not abort every other object still waiting to be reconciled."""
        self.errors.append(f"{where}: {e}")
        log.warning("FAILED %s: %s", where, e)

    def _templategroup(self, name: str) -> str:
        got = self.api.templategroup.get(filter={"name": [name]}, output=["groupid"])
        return got[0]["groupid"] if got else \
            self.api.templategroup.create(name=name)["groupids"][0]

    def _hostgroup(self, name: str) -> str:
        got = self.api.hostgroup.get(filter={"name": [name]}, output=["groupid"])
        return got[0]["groupid"] if got else \
            self.api.hostgroup.create(name=name)["groupids"][0]

    def _template(self, name: str, tg_id: str) -> str:
        got = self.api.template.get(filter={"host": [name]}, output=["templateid"])
        return got[0]["templateid"] if got else \
            self.api.template.create(host=name, groups=[{"groupid": tg_id}])["templateids"][0]

    def _item(self, template_id: str, p: Parameter, existing: dict[str, dict]) -> None:
        # history >= the longest backfill window (180d) — the trapper rejects
        # values older than an item's history retention, silently truncating
        # long backfills to whatever the Zabbix default (90d) allows.
        want = {"name": p.name, "value_type": p.value_type_code,
                "units": p.units, "description": p.description(),
                "history": "400d"}
        got = existing.get(p.key)
        try:
            if got is None:
                self.api.item.create(hostid=template_id, key_=p.key, type=2, **want)
                log.info("+ item %s", p.key)
                return
            diff = {k: v for k, v in want.items() if str(got[k]) != str(v)}
            if diff:
                self.api.item.update(itemid=got["itemid"], **diff)
                log.info("~ item %s (updated: %s)", p.key, ", ".join(diff))
        except ModuleBaseException as e:
            self._fail(f"item {p.key}", e)

    @staticmethod
    def _norm_tags(tags) -> list:
        return sorted((x["tag"], x.get("value", "")) for x in (tags or []))

    def _triggers(self, template_name: str, p: Parameter, existing: dict[str, dict]) -> None:
        for t in p.triggers:
            desc = f"{p.name}: {t.label}"
            want = {"expression": f"{t.func}(/{template_name}/{p.key}){t.op}{t.value}",
                    "priority": SEVERITY_CODE[t.severity]}
            got = existing.get(desc)
            try:
                if got is None:
                    self.api.trigger.create(description=desc, **want,
                                            **({"tags": t.tags} if t.tags else {}))
                    log.info("! trigger [%s] %s", t.severity, t.label)
                    continue
                diff = {k: v for k, v in want.items() if str(got[k]) != str(v)}
                if t.tags and self._norm_tags(got.get("tags")) != self._norm_tags(t.tags):
                    diff["tags"] = t.tags
                if diff:
                    self.api.trigger.update(triggerid=got["triggerid"], **diff)
                    log.info("~ trigger [%s] %s (updated: %s)", t.severity, t.label, ", ".join(diff))
            except ModuleBaseException as e:
                self._fail(f"trigger '{desc}'", e)

    def _discovery(self, template_id: str, template_name: str, disc,
                   protos: list[Parameter]) -> None:
        """Reconcile an LLD rule + its item/trigger prototypes, parallel to the flat
        _item/_triggers path. Trapper LLD (type 2): the simulator feeds disc.key a
        {#IFNAME} list and the server materializes one item per port from each
        prototype. Idempotent — get-or-create the rule, diff prototypes, prune strays."""
        try:
            got = self.api.discoveryrule.get(templateids=template_id,
                filter={"key_": disc.key}, output=["itemid", "key_", "name"])
            if got:
                lld_id = got[0]["itemid"]
                if str(got[0]["name"]) != disc.name:
                    self.api.discoveryrule.update(itemid=lld_id, name=disc.name)
                    log.info("~ LLD rule %s (name)", disc.key)
            else:
                lld_id = self.api.discoveryrule.create(
                    hostid=template_id, name=disc.name, key_=disc.key, type=2)["itemids"][0]
                log.info("+ LLD rule %s", disc.key)
        except ModuleBaseException as e:
            self._fail(f"LLD rule {disc.key}", e)
            return

        existing = {i["key_"]: i for i in self.api.itemprototype.get(
            discoveryids=lld_id,
            output=["itemid", "key_", "name", "value_type", "units", "description"])}
        want_keys = set()
        for p in protos:
            pkey = f"{p.key}[{disc.macro}]"
            want_keys.add(pkey)
            want = {"name": f"{p.name} {disc.macro}", "value_type": p.value_type_code,
                    "units": p.units, "description": p.description()}
            g = existing.get(pkey)
            try:
                if g is None:
                    self.api.itemprototype.create(hostid=template_id, ruleid=lld_id,
                        key_=pkey, type=2, **want)
                    log.info("+ item prototype %s", pkey)
                else:
                    diff = {k: v for k, v in want.items() if str(g[k]) != str(v)}
                    if diff:
                        self.api.itemprototype.update(itemid=g["itemid"], **diff)
                        log.info("~ item prototype %s (updated: %s)", pkey, ", ".join(diff))
            except ModuleBaseException as e:
                self._fail(f"item prototype {pkey}", e)

        existing_tp = {t["description"]: t for t in self.api.triggerprototype.get(
            discoveryids=lld_id, expandExpression=True,
            output=["triggerid", "description", "priority", "expression"])}
        want_descs = set()
        for p in protos:
            pkey = f"{p.key}[{disc.macro}]"
            for t in p.triggers:
                desc = f"{p.name} {disc.macro}: {t.label}"
                want_descs.add(desc)
                want = {"expression": f"{t.func}(/{template_name}/{pkey}){t.op}{t.value}",
                        "priority": SEVERITY_CODE[t.severity]}
                g = existing_tp.get(desc)
                try:
                    if g is None:
                        self.api.triggerprototype.create(description=desc, **want)
                        log.info("! trigger prototype [%s] %s", t.severity, t.label)
                    else:
                        diff = {k: v for k, v in want.items() if str(g[k]) != str(v)}
                        if diff:
                            self.api.triggerprototype.update(triggerid=g["triggerid"], **diff)
                            log.info("~ trigger prototype [%s] %s (updated: %s)",
                                    t.severity, t.label, ", ".join(diff))
                except ModuleBaseException as e:
                    self._fail(f"trigger prototype '{desc}'", e)

        # Prune strays — trigger prototypes first (deleting an item prototype
        # cascades to its triggers, same rule as the flat path).
        for d, t in existing_tp.items():
            if d not in want_descs:
                try:
                    self.api.triggerprototype.delete(t["triggerid"])
                    log.info("- pruned stale trigger prototype '%s'", d)
                except ModuleBaseException as e:
                    self._fail(f"prune trigger prototype '{d}'", e)
        for k, i in existing.items():
            if k not in want_keys:
                try:
                    self.api.itemprototype.delete(i["itemid"])
                    log.info("- pruned stale item prototype %s", k)
                except ModuleBaseException as e:
                    self._fail(f"prune item prototype {k}", e)

    def _host(self, h, hg_id: str, template_id: str, existing: dict[str, str]) -> None:
        inv = h.inventory
        try:
            if h.host in existing:
                self.api.host.update(hostid=existing[h.host], name=h.name,
                                     macros=[{"macro": k, "value": v} for k, v in h.macros.items()],
                                     inventory_mode=0 if inv else -1, inventory=inv)
                log.info("~ host %s (data synced)", h.host)
                return
            self.api.host.create(
                host=h.host, name=h.name,
                groups=[{"groupid": hg_id}],
                templates=[{"templateid": template_id}],
                macros=[{"macro": k, "value": v} for k, v in h.macros.items()],
                inventory_mode=0 if inv else -1, inventory=inv,
            )
            log.info("+ host %s (%s)", h.host, h.name)
        except ModuleBaseException as e:
            self._fail(f"host {h.host}", e)

    def ensure_geomap(self) -> None:
        """Make the Geomap widget work out-of-the-box over OpenStreetMap."""
        try:
            self.api.settings.update(geomaps_tile_provider="OpenStreetMap.Mapnik")
            log.info("Geomap tile provider set: OpenStreetMap.Mapnik")
        except ModuleBaseException as e:
            self._fail("geomap tile provider", e)

    def prune(self, assets) -> None:
        """Delete catalog-managed hosts that no longer exist in the catalog.
        Scoped to the catalog's own host groups — never touches other hosts."""
        try:
            catalog_hosts = {h.host for a in assets for h in a.hosts}
            group_names = sorted({a.host_group for a in assets})
            gids = [g["groupid"] for g in
                    self.api.hostgroup.get(filter={"name": group_names}, output=["groupid"])]
            if not gids:
                return
            present = self.api.host.get(groupids=gids, output=["hostid", "host"])
            stale = [h for h in present if h["host"] not in catalog_hosts]
            if stale:
                self.api.host.delete(*[h["hostid"] for h in stale])
                for h in stale:
                    log.info("- pruned stale host %s", h["host"])
        except ModuleBaseException as e:
            self._fail("prune", e)

    def apply(self, asset: AssetClass) -> None:
        log.info("[%s]", asset.asset_class)
        try:
            tg_id = self._templategroup(asset.template_group)
            hg_id = self._hostgroup(asset.host_group)
            template_id = self._template(asset.template_name, tg_id)
            log.info("template '%s' (%d params)", asset.template_name, len(asset.parameters))

            items = {i["key_"]: i for i in self.api.item.get(
                templateids=template_id,
                output=["itemid", "key_", "name", "value_type", "units", "description"])}
            triggers = {t["description"]: t for t in self.api.trigger.get(
                templateids=template_id, expandExpression=True, selectTags=["tag", "value"],
                output=["triggerid", "description", "priority", "expression"])}
            host_names = [h.host for h in asset.hosts]
            hosts = {h["host"]: h["hostid"] for h in self.api.host.get(
                filter={"host": host_names}, output=["host", "hostid"])}
        except ModuleBaseException as e:
            self._fail(f"{asset.asset_class} (setup)", e)
            return

        protos = set(asset.discovery.prototypes) if asset.discovery else set()
        flat_params = [p for p in asset.parameters if p.key not in protos]
        proto_params = [p for p in asset.parameters if p.key in protos]

        for p in flat_params:
            self._item(template_id, p, items)
            self._triggers(asset.template_name, p, triggers)
        if asset.discovery:
            self._discovery(template_id, asset.template_name, asset.discovery, proto_params)

        want_descs = {f"{p.name}: {t.label}" for p in flat_params for t in p.triggers}
        for d, t in triggers.items():
            if d not in want_descs:
                try:
                    self.api.trigger.delete(t["triggerid"])
                    log.info("- pruned stale trigger '%s'", d)
                except ModuleBaseException as e:
                    self._fail(f"prune trigger '{d}'", e)
        want_keys = {p.key for p in flat_params}
        for k, i in items.items():
            if k not in want_keys:
                try:
                    self.api.item.delete(i["itemid"])
                    log.info("- pruned stale item %s", k)
                except ModuleBaseException as e:
                    self._fail(f"prune item {k}", e)

        for h in asset.hosts:
            self._host(h, hg_id, template_id, hosts)

        if asset.circuits:
            sla.reconcile(self, asset)


def main() -> None:
    assets = load_all()
    try:
        prov = Provisioner()
    except ModuleBaseException as e:
        raise SystemExit(
            f"Cannot log in to the Zabbix API at {settings.API_URL}: {e}\n"
            f"  - stack not up yet? `make up` (first boot imports the DB, ~30-60s; `make logs`)\n"
            f"  - auth? ZBX_API_USER/ZBX_API_PASSWORD in .env must match the frontend login")
    try:
        prov.ensure_geomap()
        for a in assets:
            prov.apply(a)
        prov.prune(assets)
    finally:
        prov.close()
    if prov.errors:
        log.error("Provisioning finished with %d error(s) — everything else still applied:",
                  len(prov.errors))
        for e in prov.errors:
            log.error("- %s", e)
        raise SystemExit(1)
    log.info("Provisioning complete. Now run:  make simulate")


if __name__ == "__main__":
    main()

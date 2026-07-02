"""Provision Zabbix from the catalog: template groups, host groups, templates,
trapper items, triggers, and hosts. Idempotent — safe to re-run.

Items and triggers live on one Template per asset class; each host links its
template, so a metric is defined once regardless of how many stations exist."""
from __future__ import annotations
from zabbix_utils import ZabbixAPI

from . import settings
from .catalog import AssetClass, Parameter, SEVERITY_CODE, load_all


class Provisioner:
    def __init__(self) -> None:
        self.api = ZabbixAPI(url=settings.API_URL)
        self.api.login(user=settings.API_USER, password=settings.API_PASSWORD)
        print(f"Connected to Zabbix API {self.api.api_version()} as {settings.API_USER}")

    def close(self) -> None:
        try:
            self.api.logout()
        except Exception:  # noqa: BLE001
            pass

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

    def _item(self, template_id: str, p: Parameter, existing: set[str]) -> None:
        if p.key in existing:
            return
        self.api.item.create(
            hostid=template_id, name=p.name, key_=p.key,
            type=2,
            value_type=p.value_type_code,
            units=p.units,
            description=p.description(),
        )
        print(f"    + item {p.key}")

    def _triggers(self, template_name: str, p: Parameter, existing: set[str]) -> None:
        for t in p.triggers:
            desc = f"{p.name}: {t.label}"
            if desc in existing:
                continue
            expr = f"{t.func}(/{template_name}/{p.key}){t.op}{t.value}"
            self.api.trigger.create(
                description=desc, expression=expr,
                priority=SEVERITY_CODE[t.severity],
            )
            print(f"      ! trigger [{t.severity}] {t.label}")

    def _host(self, h, hg_id: str, template_id: str, existing: dict[str, str]) -> None:
        inv = h.inventory
        if h.host in existing:
            self.api.host.update(hostid=existing[h.host], name=h.name,
                                 inventory_mode=0 if inv else -1, inventory=inv)
            print(f"    ~ host {h.host} (data synced)")
            return
        self.api.host.create(
            host=h.host, name=h.name,
            groups=[{"groupid": hg_id}],
            templates=[{"templateid": template_id}],
            macros=[{"macro": k, "value": v} for k, v in h.macros.items()],
            inventory_mode=0 if inv else -1, inventory=inv,
        )
        print(f"    + host {h.host} ({h.name})")

    def ensure_geomap(self) -> None:
        """Make the Geomap widget work out-of-the-box over OpenStreetMap."""
        self.api.settings.update(geomaps_tile_provider="OpenStreetMap.Mapnik")
        print("Geomap tile provider set: OpenStreetMap.Mapnik")

    def prune(self, assets) -> None:
        """Delete catalog-managed hosts that no longer exist in the catalog.
        Scoped to the catalog's own host groups — never touches other hosts."""
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
                print(f"  - pruned stale host {h['host']}")

    def apply(self, asset: AssetClass) -> None:
        print(f"\n[{asset.asset_class}]")
        tg_id = self._templategroup(asset.template_group)
        hg_id = self._hostgroup(asset.host_group)
        template_id = self._template(asset.template_name, tg_id)
        print(f"  template '{asset.template_name}' ({len(asset.parameters)} params)")

        items = {i["key_"] for i in self.api.item.get(templateids=template_id, output=["key_"])}
        triggers = {t["description"] for t in self.api.trigger.get(templateids=template_id, output=["description"])}
        host_names = [h.host for h in asset.hosts]
        hosts = {h["host"]: h["hostid"]
                 for h in self.api.host.get(filter={"host": host_names}, output=["host", "hostid"])}

        for p in asset.parameters:
            self._item(template_id, p, items)
            self._triggers(asset.template_name, p, triggers)
        for h in asset.hosts:
            self._host(h, hg_id, template_id, hosts)


def main() -> None:
    assets = load_all()
    prov = Provisioner()
    try:
        prov.ensure_geomap()
        for a in assets:
            prov.apply(a)
        prov.prune(assets)
    finally:
        prov.close()
    print("\nProvisioning complete. Now run:  make simulate")


if __name__ == "__main__":
    main()

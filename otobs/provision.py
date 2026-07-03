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
        self.errors: list[str] = []

    def close(self) -> None:
        try:
            self.api.logout()
        except Exception:  # noqa: BLE001
            pass

    def _fail(self, where: str, e: Exception) -> None:
        """Record a failed operation and keep going — one rejected item/trigger/
        host (e.g. Zabbix refusing a value_type change on an item with history)
        must not abort every other object still waiting to be reconciled."""
        self.errors.append(f"{where}: {e}")
        print(f"    ! FAILED {where}: {e}")

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
        want = {"name": p.name, "value_type": p.value_type_code,
                "units": p.units, "description": p.description()}
        got = existing.get(p.key)
        try:
            if got is None:
                self.api.item.create(hostid=template_id, key_=p.key, type=2, **want)
                print(f"    + item {p.key}")
                return
            # The API returns everything as strings; compare in string space.
            diff = {k: v for k, v in want.items() if str(got[k]) != str(v)}
            if diff:
                self.api.item.update(itemid=got["itemid"], **diff)
                print(f"    ~ item {p.key} (updated: {', '.join(diff)})")
        except Exception as e:  # noqa: BLE001
            self._fail(f"item {p.key}", e)

    def _triggers(self, template_name: str, p: Parameter, existing: dict[str, dict]) -> None:
        for t in p.triggers:
            desc = f"{p.name}: {t.label}"
            want = {"expression": f"{t.func}(/{template_name}/{p.key}){t.op}{t.value}",
                    "priority": SEVERITY_CODE[t.severity]}
            got = existing.get(desc)
            try:
                if got is None:
                    self.api.trigger.create(description=desc, **want)
                    print(f"      ! trigger [{t.severity}] {t.label}")
                    continue
                diff = {k: v for k, v in want.items() if str(got[k]) != str(v)}
                if diff:
                    self.api.trigger.update(triggerid=got["triggerid"], **diff)
                    print(f"      ~ trigger [{t.severity}] {t.label} (updated: {', '.join(diff)})")
            except Exception as e:  # noqa: BLE001
                self._fail(f"trigger '{desc}'", e)

    def _host(self, h, hg_id: str, template_id: str, existing: dict[str, str]) -> None:
        inv = h.inventory
        try:
            if h.host in existing:
                self.api.host.update(hostid=existing[h.host], name=h.name,
                                     macros=[{"macro": k, "value": v} for k, v in h.macros.items()],
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
        except Exception as e:  # noqa: BLE001
            self._fail(f"host {h.host}", e)

    def ensure_geomap(self) -> None:
        """Make the Geomap widget work out-of-the-box over OpenStreetMap."""
        try:
            self.api.settings.update(geomaps_tile_provider="OpenStreetMap.Mapnik")
            print("Geomap tile provider set: OpenStreetMap.Mapnik")
        except Exception as e:  # noqa: BLE001 — cosmetic; must not block provisioning
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
                    print(f"  - pruned stale host {h['host']}")
        except Exception as e:  # noqa: BLE001 — best-effort cleanup, not critical path
            self._fail("prune", e)

    def apply(self, asset: AssetClass) -> None:
        print(f"\n[{asset.asset_class}]")
        try:
            tg_id = self._templategroup(asset.template_group)
            hg_id = self._hostgroup(asset.host_group)
            template_id = self._template(asset.template_name, tg_id)
            print(f"  template '{asset.template_name}' ({len(asset.parameters)} params)")

            # One fetch per object kind, scoped to the template — the sets these
            # feed avoid an N+1 storm of per-object existence checks.
            items = {i["key_"]: i for i in self.api.item.get(
                templateids=template_id,
                output=["itemid", "key_", "name", "value_type", "units", "description"])}
            triggers = {t["description"]: t for t in self.api.trigger.get(
                templateids=template_id, expandExpression=True,
                output=["triggerid", "description", "priority", "expression"])}
            host_names = [h.host for h in asset.hosts]
            hosts = {h["host"]: h["hostid"] for h in self.api.host.get(
                filter={"host": host_names}, output=["host", "hostid"])}
        except Exception as e:  # noqa: BLE001
            # Without the containers/fetches above, nothing for this asset class
            # can be reconciled meaningfully — skip it, but let the other asset
            # classes (and the final prune) still run instead of aborting everything.
            self._fail(f"{asset.asset_class} (setup)", e)
            return

        for p in asset.parameters:
            self._item(template_id, p, items)
            self._triggers(asset.template_name, p, triggers)

        # Reconcile: template objects the catalog no longer defines are deleted,
        # so a renamed key or trigger label doesn't leave an orphan behind.
        # Triggers first — deleting an item cascades to its triggers, and we
        # don't want to double-delete.
        want_descs = {f"{p.name}: {t.label}" for p in asset.parameters for t in p.triggers}
        for d, t in triggers.items():
            if d not in want_descs:
                try:
                    self.api.trigger.delete(t["triggerid"])
                    print(f"    - pruned stale trigger '{d}'")
                except Exception as e:  # noqa: BLE001
                    self._fail(f"prune trigger '{d}'", e)
        want_keys = {p.key for p in asset.parameters}
        for k, i in items.items():
            if k not in want_keys:
                try:
                    self.api.item.delete(i["itemid"])
                    print(f"    - pruned stale item {k}")
                except Exception as e:  # noqa: BLE001
                    self._fail(f"prune item {k}", e)

        for h in asset.hosts:
            self._host(h, hg_id, template_id, hosts)


def main() -> None:
    assets = load_all()
    try:
        prov = Provisioner()
    except Exception as e:  # noqa: BLE001 — zabbix_utils raises several kinds here
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
        print(f"\nProvisioning finished with {len(prov.errors)} error(s) — everything else "
              f"still applied:")
        for e in prov.errors:
            print(f"  - {e}")
        raise SystemExit(1)
    print("\nProvisioning complete. Now run:  make simulate")


if __name__ == "__main__":
    main()

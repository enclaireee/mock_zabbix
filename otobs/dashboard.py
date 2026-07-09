"""Export/import live Zabbix dashboards to/from dashboard/*.json — so hand-built
dashboards (workstation/network views, SLA report, ...) survive `make clean`
and can be recreated once the catalog is reprovisioned.

Dashboard widgets store hard object ids (host group/host/item/SLA), and Zabbix
hands out fresh ids for all of those every time the catalog is reprovisioned.
dashboard/_refs.json resolves each id to its stable name at export time;
import re-resolves those names to whatever the ids are *now* before recreating
each dashboard, so a raw id copy wouldn't survive a clean+reprovision cycle."""
from __future__ import annotations
import json
import logging
import re

from zabbix_utils import ModuleBaseException, ZabbixAPI

from . import settings

log = logging.getLogger(__name__)

EXPORT_DIR = settings.ROOT / "dashboard"
REFS_FILE = EXPORT_DIR / "_refs.json"

_REF_KIND = {"groupids": "group", "hostids": "host", "itemid": "item", "slaid": "sla"}


def _slug(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _kind(field_name: str) -> str | None:
    return _REF_KIND.get(re.sub(r"\.\d+$", "", field_name))


def export_all() -> None:
    api = ZabbixAPI(url=settings.API_URL)
    api.login(user=settings.API_USER, password=settings.API_PASSWORD)
    try:
        dashboards = api.dashboard.get(
            output="extend", selectPages="extend",
            selectUsers="extend", selectUserGroups="extend")

        ids: dict[str, set] = {"group": set(), "host": set(), "item": set(), "sla": set()}
        for d in dashboards:
            for page in d["pages"]:
                for w in page["widgets"]:
                    for fld in w["fields"]:
                        k = _kind(fld["name"])
                        if k:
                            ids[k].add(fld["value"])

        refs: dict = {"group": {}, "host": {}, "item": {}, "sla": {}}
        if ids["group"]:
            refs["group"] = {g["groupid"]: g["name"] for g in api.hostgroup.get(
                groupids=list(ids["group"]), output=["groupid", "name"])}
        if ids["host"]:
            refs["host"] = {h["hostid"]: h["host"] for h in api.host.get(
                hostids=list(ids["host"]), output=["hostid", "host"])}
        if ids["item"]:
            items = api.item.get(itemids=list(ids["item"]), output=["itemid", "hostid", "key_"])
            host_names = {h["hostid"]: h["host"] for h in api.host.get(
                hostids=list({i["hostid"] for i in items}), output=["hostid", "host"])}
            refs["item"] = {i["itemid"]: {"host": host_names[i["hostid"]], "key": i["key_"]}
                             for i in items}
        if ids["sla"]:
            refs["sla"] = {s["slaid"]: s["name"] for s in api.sla.get(
                slaids=list(ids["sla"]), output=["slaid", "name"])}
    finally:
        api.logout()

    EXPORT_DIR.mkdir(exist_ok=True)
    for d in dashboards:
        path = EXPORT_DIR / f"{_slug(d['name'])}.json"
        path.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
        log.info("wrote %s", path.relative_to(settings.ROOT))
    REFS_FILE.write_text(json.dumps(refs, indent=2, sort_keys=True) + "\n")
    log.info("Exported %d dashboard(s) to %s/", len(dashboards), EXPORT_DIR.relative_to(settings.ROOT))


def _remap(api, refs: dict) -> dict[str, str]:
    """old id -> current id, resolved by name, across all four ref kinds."""
    group_names = set(refs["group"].values())
    host_names = set(refs["host"].values()) | {r["host"] for r in refs["item"].values()}
    sla_names = set(refs["sla"].values())

    group_new = {g["name"]: g["groupid"] for g in api.hostgroup.get(
        filter={"name": list(group_names)}, output=["groupid", "name"])} if group_names else {}
    host_new = {h["host"]: h["hostid"] for h in api.host.get(
        filter={"host": list(host_names)}, output=["hostid", "host"])} if host_names else {}
    sla_new = {s["name"]: s["slaid"] for s in api.sla.get(
        filter={"name": list(sla_names)}, output=["slaid", "name"])} if sla_names else {}

    by_host: dict[str, list[tuple[str, str]]] = {}
    for old, ref in refs["item"].items():
        by_host.setdefault(ref["host"], []).append((old, ref["key"]))
    item_new: dict[str, str] = {}
    for hostname, entries in by_host.items():
        hostid = host_new.get(hostname)
        if not hostid:
            continue
        got = {i["key_"]: i["itemid"] for i in api.item.get(
            hostids=[hostid], filter={"key_": [k for _, k in entries]}, output=["itemid", "key_"])}
        for old, key in entries:
            if key in got:
                item_new[old] = got[key]

    return {
        **{old: group_new[name] for old, name in refs["group"].items() if name in group_new},
        **{old: host_new[name] for old, name in refs["host"].items() if name in host_new},
        **{old: sla_new[name] for old, name in refs["sla"].items() if name in sla_new},
        **item_new,
    }


def import_all() -> None:
    if not REFS_FILE.exists():
        raise SystemExit(f"{REFS_FILE.relative_to(settings.ROOT)} missing — "
                          f"run `make export-dashboards` at least once first")
    refs = json.loads(REFS_FILE.read_text())

    api = ZabbixAPI(url=settings.API_URL)
    api.login(user=settings.API_USER, password=settings.API_PASSWORD)
    try:
        remap = _remap(api, refs)
        existing = {d["name"] for d in api.dashboard.get(output=["name"])}

        stale_refs = 0
        for path in sorted(EXPORT_DIR.glob("*.json")):
            if path.name.startswith("_"):
                continue
            d = json.loads(path.read_text())
            if d["name"] in existing:
                log.info("skip '%s' (already exists)", d["name"])
                continue

            payload = {k: v for k, v in d.items() if k not in ("dashboardid", "uuid")}
            for page in payload.get("pages", []):
                page.pop("dashboard_pageid", None)
                for w in page.get("widgets", []):
                    w.pop("widgetid", None)
                    for fld in w["fields"]:
                        if _kind(fld["name"]):
                            new_id = remap.get(fld["value"])
                            if new_id:
                                fld["value"] = new_id
                            else:
                                stale_refs += 1
            try:
                api.dashboard.create(**payload)
                log.info("created '%s'", d["name"])
            except ModuleBaseException as e:
                log.error("FAILED '%s': %s", d["name"], e)

        if stale_refs:
            log.warning("%d widget field(s) pointed at a group/host/item/SLA that no longer "
                       "resolves by name (catalog changed?) — left as the old, likely-dead id.",
                       stale_refs)
    finally:
        api.logout()


def export_main() -> None:
    try:
        export_all()
    except ModuleBaseException as e:
        raise SystemExit(
            f"Cannot log in to the Zabbix API at {settings.API_URL}: {e}\n"
            f"  - stack not up yet? `make up`\n"
            f"  - auth? ZBX_API_USER/ZBX_API_PASSWORD in .env must match the frontend login")


def import_main() -> None:
    try:
        import_all()
    except ModuleBaseException as e:
        raise SystemExit(
            f"Cannot log in to the Zabbix API at {settings.API_URL}: {e}\n"
            f"  - stack not up yet? `make up`\n"
            f"  - catalog reprovisioned yet? `make provision` (dashboards reference its "
            f"host groups/hosts/items by name)")


if __name__ == "__main__":
    export_main()

"""Provision the Zabbix-native SLA layer for the comm-link circuits.

One Zabbix **Service** per circuit, each matching that circuit's downtime
problems via the `link:<key>` event tag the provisioner puts on the circuit's
'down' trigger (see catalog.py:_build_comm_links). One **SLA** object (SLO from
the host's {$SLA_TARGET} macro, monthly, 24x7) selects all of them by a shared
`sla_group:comm_link` service tag, so Zabbix's own SLA report renders one row
per circuit — no hand-rolled uptime math. See docs/comm-links-sla.md.

Idempotent, structured like Provisioner.apply(): get-or-create, re-sync, prune
stale services. Runs on an already-logged-in Provisioner (reuses api + _fail +
errors) so `make provision` does it in one pass.
"""
from __future__ import annotations
import logging
from datetime import datetime

from zabbix_utils import ModuleBaseException

from . import settings
from .catalog import AssetClass

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    _TZ = ZoneInfo(settings.TIMEZONE)
except (ZoneInfoNotFoundError, ValueError):
    _TZ = None

log = logging.getLogger(__name__)

SLA_NAME = "Comm Links SLA"
MARKER = ("sla_group", "comm_link")
PERIOD_MONTHLY = 2
STATUS_ALGO = 1  # "Problem, if at least one child has a problem" — required nonzero or Zabbix disables status calc


def _effective_date() -> int:
    """Midnight, first of the current month — the SLA report's reporting start."""
    now = datetime.now(_TZ)
    return int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def reconcile(prov, asset: AssetClass) -> None:
    """One Service per circuit + the SLA object selecting them all.
    `prov` is a logged-in Provisioner (reuses its api / _fail / errors)."""
    if not asset.circuits:
        return
    try:
        existing = {s["name"]: s for s in prov.api.service.get(
            output=["serviceid", "name"],
            tags=[{"tag": MARKER[0], "value": MARKER[1]}])}
    except ModuleBaseException as e:
        prov._fail("SLA (fetch services)", e)
        return

    want_names = set()
    for c in asset.circuits:
        name = c.param.name
        want_names.add(name)
        want = {
            "algorithm": STATUS_ALGO,
            "sortorder": 0,
            "tags": [{"tag": MARKER[0], "value": MARKER[1]}],
            "problem_tags": [{"tag": "link", "operator": 0, "value": c.param.key}],
        }
        got = existing.get(name)
        try:
            if got is None:
                prov.api.service.create(name=name, **want)
                log.info("+ service %s", name)
            else:
                prov.api.service.update(serviceid=got["serviceid"], name=name, **want)
        except ModuleBaseException as e:
            prov._fail(f"service '{name}'", e)

    for name, s in existing.items():
        if name not in want_names:
            try:
                prov.api.service.delete(s["serviceid"])
                log.info("- pruned stale service '%s'", name)
            except ModuleBaseException as e:
                prov._fail(f"prune service '{name}'", e)

    _ensure_sla(prov, asset)
    log.info("[SLA] %d circuit service(s) + 1 SLA object", len(want_names))


def _ensure_sla(prov, asset: AssetClass) -> None:
    target = float(asset.hosts[0].macros.get("{$SLA_TARGET}", "98"))
    want = {
        "period": PERIOD_MONTHLY,
        "slo": target,
        "timezone": settings.TIMEZONE,
        "effective_date": _effective_date(),
        "status": 1,
        "service_tags": [{"tag": MARKER[0], "value": MARKER[1]}],
    }
    try:
        got = prov.api.sla.get(filter={"name": [SLA_NAME]}, output=["slaid"])
        if got:
            prov.api.sla.update(slaid=got[0]["slaid"], name=SLA_NAME, **want)
            log.info("~ SLA '%s'", SLA_NAME)
        else:
            prov.api.sla.create(name=SLA_NAME, **want)
            log.info("+ SLA '%s'", SLA_NAME)
    except ModuleBaseException as e:
        prov._fail(f"SLA '{SLA_NAME}'", e)

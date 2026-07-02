# Provisioning idempotency

`make provision` is safe to re-run: it **reconciles** Zabbix against the
catalog. Nothing is duplicated, catalog edits propagate, and stale objects are
cleaned up. The mechanics live in `Provisioner`
([`otobs/provision.py`](../otobs/provision.py)).

## Get-or-create helpers

`_templategroup`, `_hostgroup`, and `_template` each look the object up by name
and create it only if absent, returning its id either way. These run once per
asset class.

## Reconcile against a pre-fetched map

Before touching items, triggers, and hosts for an asset, `apply` fetches the
existing objects **once** (scoped to the template) and passes those maps down —
one fetch per object kind instead of an N+1 storm of existence checks. Then,
per object:

- **absent in Zabbix** → created (`+ item` / `! trigger` / `+ host`).
- **present but different** → updated in place: items re-sync `name`,
  `value_type`, `units`, `description`; triggers re-sync `expression` and
  `priority`. Identity keys are the item `key` and the trigger `description`
  (`"<param name>: <label>"`), so an unchanged catalog is a clean no-op.
- **present in Zabbix but gone from the catalog** → deleted (`- pruned`).
  Triggers are pruned before items, because deleting an item cascades to its
  triggers and would double-delete.

> Renaming an item `key` or a trigger `label` changes its identity: the old
> object is pruned (losing its history/event trail) and a new one is created.
> That is the correct single-source-of-truth semantic — the catalog defines
> what exists — but be aware a rename is a delete-plus-create, not a rename.

## Host re-sync on re-run

For a host that already exists, `_host` issues a `host.update` so the mutable
catalog data — visible name, macros, and inventory/location — is re-synced on
every run, even though the host itself isn't recreated. Manual changes made in
the Zabbix UI to those fields are overwritten; anything else on the host
(added items, extra templates, tags) is left alone.

## Prune

`prune` deletes catalog-managed hosts that are no longer in the catalog (e.g. a
site removed from `sites.yml`). It is scoped strictly to the catalog's own host
groups, so it never touches hosts created outside this tool.

# Geomap

The Zabbix **Geomap** widget plots hosts on a map using each host's inventory
location fields. Two things make it work out of the box.

## Inventory fields

When a host is expanded from a site, its inventory is populated with
`location`, `location_lat`, and `location_lon` (plus `site_city` /
`site_country`). The lat/lon pair is what the Geomap widget reads to place the
host. See `_expand_hosts` and the `Host.inventory` field in
[`otobs/catalog.py`](../otobs/catalog.py).

## Tile provider

`Provisioner.ensure_geomap` sets the global Geomap tile provider to
`OpenStreetMap.Mapnik` so the map renders without any manual configuration
([`otobs/provision.py`](../otobs/provision.py)).

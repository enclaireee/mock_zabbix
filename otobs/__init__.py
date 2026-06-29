"""OT Observability Lab — catalog-driven Zabbix provisioning + mock OT data.

The catalog (catalog/*.yml) is the single source of truth: the same file drives
Zabbix item/trigger creation (provision) and the Good/Underperform/Failed mock
data stream (simulate).
"""
__all__ = ["catalog", "settings", "provision", "simulate"]

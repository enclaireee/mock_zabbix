"""Pytest suite for the provisioner's fault isolation: one item/trigger/host that
the Zabbix API rejects must be recorded, not raised — otherwise a single bad
object would abort reconciliation of everything still queued behind it."""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest
from zabbix_utils import APIRequestError

from otobs.provision import Provisioner
from otobs.catalog import Parameter, Sim, State, Host, Trigger


def _param(key):
    sim = Sim("numeric", [State(0.9, "good", None, 0, 1, 0)])
    trig = [Trigger(">=", 1, "warning", "l")]
    return Parameter(key, key, "float", "", "1m", "c", "col", "fm", "src", sim, trig)


def _provisioner(failing: bool) -> Provisioner:
    prov = Provisioner.__new__(Provisioner)  # skip __init__: no real API login
    prov.api = MagicMock()
    if failing:
        prov.api.item.create.side_effect = APIRequestError("rejected by server")
        prov.api.item.update.side_effect = APIRequestError("rejected by server")
        prov.api.trigger.create.side_effect = APIRequestError("rejected by server")
        prov.api.trigger.update.side_effect = APIRequestError("rejected by server")
        prov.api.host.create.side_effect = APIRequestError("rejected by server")
        prov.api.host.update.side_effect = APIRequestError("rejected by server")
        prov.api.settings.update.side_effect = APIRequestError("rejected by server")
    prov.errors = []
    return prov


def test_failing_api_records_errors_without_raising():
    prov = _provisioner(failing=True)
    prov._item("tmpl", _param("k"), {})
    prov._triggers("Template X", _param("k"), {})
    prov._host(Host("H", "H"), "hg", "tmpl", {})
    prov.ensure_geomap()
    assert len(prov.errors) == 4, f"expected 4 recorded failures, got {prov.errors}"


def test_success_path_records_nothing():
    prov = _provisioner(failing=False)
    prov._item("tmpl", _param("k"), {})
    assert prov.errors == []
    prov.api.item.create.assert_called_once()


def test_non_api_bugs_are_not_swallowed():
    """_item/_triggers/_host only catch zabbix_utils' own ModuleBaseException —
    a real bug (e.g. a TypeError from a coding mistake) must still crash, not
    get silently recorded as a routine provisioning failure."""
    prov = _provisioner(failing=False)
    prov.api.item.create.side_effect = TypeError("not a Zabbix API rejection")
    with pytest.raises(TypeError):
        prov._item("tmpl", _param("k"), {})

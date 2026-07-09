"""Shared factories and fixtures for the otobs pytest suite."""
from __future__ import annotations
import pytest

from otobs.catalog import Parameter, Sim, State, load_all


def numsim() -> Sim:
    return Sim("numeric", [
        State(0.90, "good", None, 40, 60, 1.5),
        State(0.08, "underperform", None, 66, 84, 1.5),
        State(0.02, "failed", None, 86, 99, 1.5),
    ])


def param(key: str) -> Parameter:
    return Parameter(key, key, "float", "", "1m", "c", "col", "fm", "src", numsim(), [])


@pytest.fixture(scope="session")
def assets():
    """The real catalog/*.yml, parsed once and reused — tests only read it."""
    return load_all()

"""Fixture Contracts cannot silently change another test's named boundary."""

import pytest

from cruxible_client.contracts.shapes import validate_contract
from tests.support import procedures


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(procedures, "_DECLARED", {})


def test_identical_boundary_declarations_remain_valid():
    first = procedures.boundary_contract("registry.input", {"value": "int"})
    assert procedures.boundary_contract("registry.input", {"value": "int"}) == first
    assert procedures._DECLARED == {first.identity: first}


def test_different_declaration_refuses_without_rebinding_the_first():
    first = procedures.boundary_contract("registry.input", {"value": "int"})
    with pytest.raises(ValueError, match="already declared differently"):
        procedures.boundary_contract("registry.input", {"value": "string"})
    assert procedures._DECLARED == {first.identity: first}


def test_nested_conflict_leaves_no_partial_parent_registration():
    first = procedures.boundary_contract("registry.row", {"value": "int"})
    other = validate_contract({"name": first.name, "version": 1, "fields": {"value": "string"}})
    with pytest.raises(ValueError, match="already declared differently"):
        procedures.boundary_contract("registry.output", {"row": "registry.row@1"}, other)
    assert procedures._DECLARED == {first.identity: first}
    with pytest.raises(ValueError, match="already declared differently"):
        procedures.declare_boundaries(other)

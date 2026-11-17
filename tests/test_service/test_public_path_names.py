"""A definition path printed by its public name resolves with get (review F-007)."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.get_text import definition_path_name
from cruxible_core.service.discovery import get

_STORED = "playbill.database-snapshot-coordinate-v1"


def test_a_reserved_contract_path_prints_publicly_and_the_printed_name_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown = definition_path_name(f"contracts/{_STORED}/v1.json")
    assert shown == "Contract:cruxible.database-snapshot-coordinate-v1@1"
    # The version is kept apart from the name when mapping back to the stored spelling.
    assert get._stored_spelling(shown) == f"Contract:{_STORED}@1"

    tried: list[str] = []

    def as_written(_instance: Any, _coordinate: Any, value: str, **_: Any) -> Any:
        tried.append(value)
        if value == f"Contract:{_STORED}@1":
            return get.ResolvedRef("contract", f"Contract:{_STORED}/v1", value)
        raise get._not_found("artifact", value, (), surface="cli")

    monkeypatch.setattr(get, "_resolve_get_ref_as_written", as_written)
    resolved = get.resolve_get_ref(None, None, shown)  # type: ignore[arg-type]
    assert resolved.identity == f"Contract:{_STORED}/v1"
    assert tried == [shown, f"Contract:{_STORED}@1"]


def test_only_kinds_that_own_reserved_names_are_mapped() -> None:
    reserved = "playbill.coordinator-self-source-v1"
    assert definition_path_name(f"capture-contracts/{reserved}.json") == (
        "CaptureContract:cruxible.coordinator-self-source-v1"
    )
    # A Document may lawfully carry the same name: it prints as itself.
    assert definition_path_name(f"documents/{reserved}.json") == f"Document:{reserved}"
    assert definition_path_name("capabilities/web.fetch/v1.json") == "Capability:web.fetch@1"

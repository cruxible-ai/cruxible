"""A provider's completeness reaches core through one generic protocol field (D-R37 a4).

A provider that fails part of a list itself says so in its result envelope's
``incomplete_captures``, naming out-Contract ``list<capture<...>>`` fields.
Core reads that field and never a provider's or operation's own output field
for the same fact: web.fetch.pages lists its ``failed_pages`` in its typed
output, and core must not learn completeness from it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCES = (REPO / "src/cruxible_core", REPO / "packages/cruxible-client/src/cruxible_client")
#: Provider-specific output fields that report failed members.
PROVIDER_FIELDS = ("failed_pages",)


def _mentions(root: Path, word: str) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if word in path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("word", PROVIDER_FIELDS)
def test_core_never_references_a_providers_failure_field(word: str) -> None:
    assert [hit for root in SOURCES for hit in _mentions(root, word)] == []


def test_the_guard_sees_a_reference(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text('missing = output.get("failed_pages")\n')
    assert _mentions(tmp_path, "failed_pages") != []


def test_core_reads_the_generic_field() -> None:
    from cruxible_core.providers.provider_runtime_contract import ProviderRuntimeResultEnvelope

    assert "incomplete_captures" in ProviderRuntimeResultEnvelope.model_fields
    assert any(
        "envelope.incomplete_captures" in path.read_text(encoding="utf-8")
        for path in (REPO / "src/cruxible_core/procedures").rglob("*.py")
    )

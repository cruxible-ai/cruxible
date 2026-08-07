"""The frozen procedure-definition digest corpus.

Every entry is a normalized dump captured from an unmodified 0.3.2 parse, beside
the v1 digest of that dump. The corpus is the one thing standing between a
serialization change and mass runtime refusals on shipped instances: five call
sites recompute ``definition_digest`` and compare it against a stored value, so
a moved byte does not fail a test today -- it refuses every accepted procedure
on every instance that upgrades.

Regenerating a captured ``digest_v1`` is a release-blocking event requiring a
decision record. These assertions run UNREGENERATED after every batch that
touches the definition model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.primitives import canonical_json
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    compute_procedure_definition_digest,
)

CORPUS_DIR = Path(__file__).resolve().parent.parent / "data" / "procedure_digest_corpus"


def corpus_entries() -> list[dict[str, Any]]:
    """Load every frozen corpus entry, ordered by file name."""
    return [json.loads(path.read_text()) for path in sorted(CORPUS_DIR.glob("*.json"))]


def corpus_ids() -> list[str]:
    return [path.stem for path in sorted(CORPUS_DIR.glob("*.json"))]


ENTRIES = corpus_entries()
IDS = corpus_ids()


def test_corpus_is_populated_and_covers_its_declared_sources() -> None:
    labels = {entry["source_label"] for entry in ENTRIES}
    assert len(ENTRIES) >= 40
    assert any(label.startswith("tau3-retail-") for label in labels), (
        "the compat promise is made to third-party definitions; keep tau3 entries"
    )
    assert any(label.startswith("collision-") for label in labels), (
        "namespace-collision entries are the v1 killer; keep them"
    )
    assert sum(label.startswith("matrix-kind-") for label in labels) == 12, (
        "one entry per admitted step kind, repeat included"
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_g1_frozen_v1_digest_reproduces(entry: dict[str, Any]) -> None:
    definition = ProcedureDefinition.model_validate(entry["normalized_dump_v032"])
    assert compute_procedure_definition_digest(definition) == entry["digest_v1"]


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_g3_normalized_dump_round_trips(entry: dict[str, Any]) -> None:
    dump = entry["normalized_dump_v032"]
    definition = ProcedureDefinition.model_validate(dump)
    reserialized = definition.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert canonical_json(reserialized) == canonical_json(dump)

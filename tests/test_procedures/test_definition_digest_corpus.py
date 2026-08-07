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

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.primitives import canonical_json
from cruxible_core.procedure.graph_format import definition_format_version
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


CORPUS_ENTRY_COUNT = 48
"""FROZEN. A lower bound would let the corpus shrink silently, and a corpus
that quietly lost its tau3 entries or its collision entries still passes every
assertion that only counts what remains."""

COLLISION_ENTRIES = frozenset(
    {
        "collision-provider-input-graph-keys",
        "collision-query-params-arm-keys",
        "collision-relationship-state-graph-keys",
        "collision-shape-items-fields-graph-format",
    }
)
"""The v1 killers, named individually.

Each buries a v2 construct NAME inside a ``dict[str, Any]`` leaf. They are the
entries a content-sniffing format detector would misroute, so losing one loses
the regression that proves the detector does not sniff."""

CORPUS_MANIFEST_SHA256 = "896e0d0d95d89a09cb8bc2b78709977dee1200931eeb48a1929790a48b7666a0"
"""sha256 over ``<source_label>=<digest_v1>`` for every entry, in file order.

Pins MEMBERSHIP as well as content: an entry that is removed, renamed or
re-digested moves this, and any of the three is a release-blocking event
requiring a decision record."""


def test_the_corpus_membership_is_frozen() -> None:
    labels = [entry["source_label"] for entry in ENTRIES]
    assert len(ENTRIES) == CORPUS_ENTRY_COUNT
    assert len(set(labels)) == len(labels), "duplicate source labels"

    manifest = "\n".join(f"{entry['source_label']}={entry['digest_v1']}" for entry in ENTRIES)
    assert hashlib.sha256(manifest.encode("utf-8")).hexdigest() == CORPUS_MANIFEST_SHA256, (
        "corpus membership or content changed. Adding an entry is ordinary and "
        "updates this digest; REMOVING or re-digesting one is a release-blocking "
        "event requiring a decision record."
    )


def test_the_corpus_covers_its_declared_sources() -> None:
    labels = {entry["source_label"] for entry in ENTRIES}
    assert sum(label.startswith("tau3-retail-") for label in labels) == 7, (
        "the compat promise is made to third-party definitions; keep tau3 entries"
    )
    assert COLLISION_ENTRIES <= labels, (
        f"missing namespace-collision entries: {sorted(COLLISION_ENTRIES - labels)}. "
        "These are the entries a content-sniffing detector would misroute."
    )
    assert sum(label.startswith("matrix-kind-") for label in labels) == 12, (
        "one entry per admitted step kind, repeat included"
    )


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_g1_frozen_v1_digest_reproduces(entry: dict[str, Any]) -> None:
    definition = ProcedureDefinition.model_validate(entry["normalized_dump_v032"])
    assert compute_procedure_definition_digest(definition) == entry["digest_v1"]


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_g2_every_corpus_entry_is_format_v1(entry: dict[str, Any]) -> None:
    """Including every namespace-collision entry.

    A collision entry buries `next`, `parameters`, `guard`, `project`,
    `measurements`, `on_true`, `on_false` or `graph_format` inside a
    ``dict[str, Any]`` leaf. Each is an ordinary v1 definition, and any format
    detector that reads those leaves would route it through v2 digest rules --
    breaking perpetual v1 reproduction on live data.
    """
    definition = ProcedureDefinition.model_validate(entry["normalized_dump_v032"])
    assert definition_format_version(definition) == (1, [])


@pytest.mark.parametrize("entry", ENTRIES, ids=IDS)
def test_g3_normalized_dump_round_trips(entry: dict[str, Any]) -> None:
    dump = entry["normalized_dump_v032"]
    definition = ProcedureDefinition.model_validate(dump)
    reserialized = definition.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert canonical_json(reserialized) == canonical_json(dump)

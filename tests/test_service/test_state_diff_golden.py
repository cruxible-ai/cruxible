"""Pinned artifact shape for the ``state diff`` surface.

Modeled on ``tests/goldens/state_cross_section/car_parts_state_diff.json``, which
is a HARNESS golden over the cross-section differ and stays exactly as it is.
This is the new surface's own golden, under its own name, over the real
``service_state_diff`` artifact body.

Normalization is local to this module on purpose: the harness's token registry
encodes golden semantics for a different differ, and this file must be free to
pin exactly the volatile fields the diff artifact actually carries.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
)
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
)
from cruxible_core.service import (
    service_add_entities,
    service_add_relationships,
    service_lock,
    service_state_diff,
)
from cruxible_core.service.snapshots import service_create_snapshot
from tests.test_cli.conftest import CAR_PARTS_YAML

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = REPO_ROOT / "tests/goldens/state_diff/car_parts_state_diff_artifact.json"

_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SNAPSHOT", re.compile(r"^snap_[0-9a-f]{16}$")),
    ("CLAIM", re.compile(r"^CLM-[0-9a-f]{16}$")),
    ("RECEIPT", re.compile(r"^RCP-[0-9a-f]{12}$")),
    ("DIGEST", re.compile(r"^sha256:[0-9a-f]{64}$")),
    ("TIMESTAMP", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")),
)


class _Tokens:
    """Stable per-run tokens for generated values, so the golden is comparable."""

    def __init__(self) -> None:
        self._assigned: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}

    def token(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self._assigned:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            self._assigned[key] = f"<{kind}_{self._counters[kind]}>"
        return self._assigned[key]


def _normalize(value: Any, tokens: _Tokens) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item, tokens) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize(item, tokens) for item in value]
    if isinstance(value, str):
        for kind, pattern in _TOKEN_PATTERNS:
            if pattern.match(value):
                return tokens.token(kind, value)
        if "/" in value and value.endswith(".json"):
            return f"<PATH>/{Path(value).name}"
        return value
    return value


def _build_instance(tmp_path: Path) -> CruxibleInstance:
    root = tmp_path / "car-parts"
    root.mkdir()
    (root / "config.yaml").write_text(CAR_PARTS_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    service_lock(instance, force=True)
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Vehicle",
                entity_id="V-CIVIC",
                properties={
                    "vehicle_id": "V-CIVIC",
                    "year": 2024,
                    "make": "Honda",
                    "model": "Civic",
                },
            ),
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1001",
                properties={
                    "part_number": "BP-1001",
                    "name": "Ceramic Brake Pads",
                    "category": "brakes",
                    "price": 49.99,
                },
            ),
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1234",
                properties={
                    "part_number": "BP-1234",
                    "name": "Performance Brake Pads",
                    "category": "brakes",
                },
            ),
        ],
    )
    service_add_relationships(
        instance,
        [
            RelationshipInstance(
                from_type="Part",
                from_id="BP-1001",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": True, "source": "fixture"},
            )
        ],
        source="fixture",
        source_ref="seed",
    )
    return instance


def _mutate(instance: CruxibleInstance) -> None:
    """One of every classification axis: added, property change, supersession."""
    service_add_relationships(
        instance,
        [
            RelationshipInstance(
                from_type="Part",
                from_id="BP-1234",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-CIVIC",
                properties={"verified": False, "source": "fixture"},
            )
        ],
        source="fixture",
        source_ref="seed",
    )
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="Part",
                entity_id="BP-1001",
                properties={
                    "part_number": "BP-1001",
                    "name": "Ceramic Brake Pads",
                    "category": "brakes",
                    "price": 54.99,
                },
            )
        ],
    )
    graph = instance.load_graph()
    edge = graph.get_relationship("Part", "BP-1001", "Vehicle", "V-CIVIC", "fits")
    assert edge is not None
    graph.replace_relationship_state(
        "Part",
        "BP-1001",
        "Vehicle",
        "V-CIVIC",
        "fits",
        properties=dict(edge.properties),
        metadata=RelationshipMetadata(
            provenance=edge.metadata.provenance,
            assertion=RelationshipAssertion(
                review=RelationshipReviewState(status="approved", source="human"),
                lifecycle=RelationshipLifecycleState(status="superseded"),
            ),
            evidence=edge.metadata.evidence,
        ),
    )
    instance.save_graph(graph)


def test_state_diff_artifact_matches_golden(tmp_path: Path) -> None:
    instance = _build_instance(tmp_path)
    snapshot = service_create_snapshot(instance).snapshot
    _mutate(instance)

    result = service_state_diff(instance, from_coordinate=snapshot.snapshot_id)
    artifact = json.loads(Path(result.artifact_ref.path).read_text())
    actual = _normalize(artifact, _Tokens())

    if os.environ.get("CRUXIBLE_UPDATE_GOLDENS") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        return

    assert GOLDEN_PATH.exists(), f"Golden file does not exist: {GOLDEN_PATH}"
    expected = json.loads(GOLDEN_PATH.read_text())
    assert actual == expected


def test_harness_golden_is_untouched() -> None:
    """``car_parts_state_diff.json`` is a HARNESS golden and cannot become this one."""
    harness_golden = json.loads(
        (REPO_ROOT / "tests/goldens/state_cross_section/car_parts_state_diff.json").read_text()
    )
    assert set(harness_golden) == {"graph", "summary", "version"}
    assert "diff_digest" not in harness_golden

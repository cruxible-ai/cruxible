"""Capture the frozen procedure-definition digest corpus.

THIS SCRIPT IS RUN ONCE. Its output (``tests/data/procedure_digest_corpus``)
is a FROZEN archival record: each entry is the normalized dump a 0.3.2 core
produces from parsing a definition, beside the v1 digest of that dump. The
corpus exists so a serialization change is caught by a unit test instead of
surfacing as mass runtime refusals on shipped instances, and regenerating a
captured ``digest_v1`` is a release-blocking event requiring a decision record
(spec sec 2.5 / sec 7.0).

Re-running it to ADD entries is fine; re-running it after a model change so
that existing digests move is exactly the failure the corpus exists to catch.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cruxible_core.procedure.types import (  # noqa: E402
    ProcedureDefinition,
    compute_procedure_definition_digest,
)

CORPUS_DIR = REPO_ROOT / "tests" / "data" / "procedure_digest_corpus"
DEFAULT_TAU3_PROCEDURES = (
    Path.home() / "Git" / "cruxible-tau3" / "content" / "retail" / "procedures"
)
_DEFINITION_KEYS = {"name", "steps", "returns", "precondition", "budget"}


def _normalized_entry(source_label: str, payload: dict[str, Any]) -> dict[str, Any]:
    definition = ProcedureDefinition.model_validate(payload)
    normalized = definition.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {
        "source_label": source_label,
        "normalized_dump_v032": normalized,
        "digest_v1": compute_procedure_definition_digest(definition),
    }


def _slug(source_label: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in source_label).strip("-")


def collect_tau3(procedures_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """Real third-party v1 definitions from the tau3 harness content."""
    if not procedures_dir.is_dir():
        return []
    collected: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(procedures_dir.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text())
        if not isinstance(payload, dict) or not _DEFINITION_KEYS.issubset(payload):
            continue
        collected.append((f"tau3-retail-{path.stem}", payload))
    return collected


def collect_config_donor_definitions() -> list[tuple[str, dict[str, Any]]]:
    """Proposable definitions preserved in frozen config-donor fixtures.

    Empty on this line: kits ship configured workflows, not procedure
    definitions. Kept as a live scan rather than a comment so additions to a
    retained compiler fixture can be captured explicitly.
    """
    collected: list[tuple[str, dict[str, Any]]] = []
    for path in sorted((REPO_ROOT / "tests" / "data" / "config_donors").rglob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(payload, dict) or not _DEFINITION_KEYS.issubset(payload):
            continue
        collected.append((f"config-donor-{path.parent.name}-{path.stem}", payload))
    return collected


def collect_test_definitions() -> list[tuple[str, dict[str, Any]]]:
    """Every literal definition constructed by ``tests/test_procedures``.

    Harvested from the syntax tree rather than by importing the tests: a
    definition built from a constant dict literal is exactly the population the
    compat promise covers, and ``ast.literal_eval`` keeps the capture free of
    test-time side effects.
    """
    collected: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    test_dir = REPO_ROOT / "tests" / "test_procedures"
    for path in sorted(test_dir.glob("*.py")):
        tree = ast.parse(path.read_text())
        index = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            try:
                payload = ast.literal_eval(node)
            except ValueError:
                continue
            if not isinstance(payload, dict) or not _DEFINITION_KEYS.issubset(payload):
                continue
            try:
                definition = ProcedureDefinition.model_validate(payload)
            except Exception:  # noqa: BLE001 - negative fixtures are not corpus entries
                continue
            fingerprint = compute_procedure_definition_digest(definition)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            index += 1
            collected.append((f"tests-{path.stem}-{index:02d}", payload))
    return collected


def _step(**overrides: Any) -> dict[str, Any]:
    return dict(overrides)


def matrix_definitions() -> list[tuple[str, dict[str, Any]]]:
    """The hand-built coverage matrix (spec sec 2.5)."""
    budget = {"wall_clock_s": 30, "max_provider_calls": 5}
    kind_steps: dict[str, list[dict[str, Any]]] = {
        "query": [_step(id="read", query="named_query", params={"limit": 5}, **{"as": "rows"})],
        "provider": [_step(id="call", provider="scorer", input={"x": 1}, **{"as": "rows"})],
        "assert": [
            _step(id="seed", provider="scorer", input={}, **{"as": "rows"}),
            _step(
                id="guard",
                **{
                    "assert": {
                        "left": "$steps.rows.count",
                        "op": "gt",
                        "right": 0,
                        "message": "no rows",
                    }
                },
            ),
        ],
        "assert_not_truncated": [
            _step(id="seed", query="named_query", **{"as": "rows"}),
            _step(id="guard", assert_not_truncated={"step": "rows"}),
        ],
        "assert_count": [
            _step(id="seed", query="named_query", **{"as": "rows"}),
            _step(
                id="guard",
                assert_count={
                    "step": "rows",
                    "count": "returned_results",
                    "op": "gte",
                    "value": 1,
                    "message": "empty read",
                },
            ),
        ],
        "assert_exists": [
            _step(id="seed", provider="scorer", input={}, **{"as": "rows"}),
            _step(id="guard", assert_exists={"ref": "$steps.rows", "message": "missing"}),
        ],
        "shape_items": [
            _step(id="seed", provider="scorer", input={}, **{"as": "raw"}),
            _step(
                id="shape",
                shape_items={"items": "$steps.raw", "fields": {"id": "$item.id"}},
                **{"as": "rows"},
            ),
        ],
        "join_items": [
            _step(id="left", provider="scorer", input={}, **{"as": "left_rows"}),
            _step(id="right", provider="scorer", input={}, **{"as": "right_rows"}),
            _step(
                id="join",
                join_items={
                    "left_items": "$steps.left_rows",
                    "right_items": "$steps.right_rows",
                    "left_key": "$item.id",
                    "right_key": "$item.id",
                },
                **{"as": "rows"},
            ),
        ],
        "filter_items": [
            _step(id="seed", provider="scorer", input={}, **{"as": "raw"}),
            _step(
                id="filter",
                filter_items={"items": "$steps.raw", "where": {"status": "open"}},
                **{"as": "rows"},
            ),
        ],
        "aggregate_items": [
            _step(id="seed", provider="scorer", input={}, **{"as": "raw"}),
            _step(
                id="agg",
                aggregate_items={
                    "items": "$steps.raw",
                    "group_by": {"status": "$item.status"},
                    "measures": {"total": {"count": True}},
                },
                **{"as": "rows"},
            ),
        ],
        "dedupe_items": [
            _step(id="seed", provider="scorer", input={}, **{"as": "raw"}),
            _step(
                id="dedupe",
                dedupe_items={"items": "$steps.raw", "keys": ["$item.id"]},
                **{"as": "rows"},
            ),
        ],
        "repeat": [
            _step(
                id="loop",
                repeat={
                    "max_attempts": 3,
                    "until": {
                        "left": "$steps.attempt.done",
                        "op": "eq",
                        "right": True,
                        "message": "not done",
                    },
                    "steps": [
                        _step(id="attempt", provider="scorer", input={}, **{"as": "attempt"})
                    ],
                },
                **{"as": "rows"},
            )
        ],
    }

    entries: list[tuple[str, dict[str, Any]]] = []
    for kind, steps in kind_steps.items():
        entries.append(
            (
                f"matrix-kind-{kind}",
                {
                    "name": f"matrix_{kind}",
                    "steps": steps,
                    "returns": "rows",
                    "precondition": {},
                    "budget": budget,
                },
            )
        )

    entries.append(
        (
            "matrix-repeat-without-nested-alias",
            {
                "name": "matrix_repeat_bare",
                "steps": [
                    _step(
                        id="loop",
                        repeat={
                            "max_attempts": 2,
                            "until": {
                                "left": "$steps.attempt.done",
                                "op": "eq",
                                "right": True,
                                "message": "not done",
                            },
                            "steps": [
                                _step(
                                    id="attempt",
                                    provider="scorer",
                                    input={},
                                    **{"as": "attempt"},
                                )
                            ],
                        },
                        **{"as": "rows"},
                    )
                ],
                "returns": "rows",
                "precondition": {},
                "budget": budget,
            },
        )
    )

    simple_steps = [_step(id="call", provider="scorer", input={}, **{"as": "rows"})]
    base = {
        "name": "matrix_base",
        "steps": simple_steps,
        "returns": "rows",
        "precondition": {},
        "budget": budget,
    }
    entries.append(("matrix-evidence-outputs-absent", dict(base)))
    entries.append(
        (
            "matrix-evidence-outputs-present",
            {**base, "name": "matrix_evidence", "evidence_outputs": ["rows"]},
        )
    )
    entries.append(
        (
            "matrix-precondition-populated",
            {
                **base,
                "name": "matrix_precondition",
                "precondition": {"entity_type": "Task", "condition": {"status": "open"}},
            },
        )
    )
    entries.append(
        (
            "matrix-contract-out-present",
            {**base, "name": "matrix_contract_out", "contract_out": "cruxible.JsonObject"},
        )
    )
    entries.append(
        (
            "matrix-declared-tier-admin",
            {**base, "name": "matrix_admin_tier", "declared_tier": "admin"},
        )
    )
    entries.append(
        (
            "matrix-integer-wall-clock",
            {
                **base,
                "name": "matrix_integer_wall_clock",
                "budget": {"wall_clock_s": 30, "max_provider_calls": 5},
            },
        )
    )
    for contract in (
        "cruxible.EmptyInput",
        "cruxible.JsonObject",
        "cruxible.JsonItems",
        "cruxible.ParsedTabularBundle",
    ):
        entries.append(
            (
                f"matrix-contract-in-{_slug(contract)}",
                {**base, "name": f"matrix_in_{_slug(contract)}", "contract_in": contract},
            )
        )
    entries.append(
        (
            "matrix-unicode-name-and-message",
            {
                "name": "matrix_ünicode_✓_\U0001f512",
                "description": "日本語 — emoji \U0001f680",
                "steps": [
                    _step(id="seed", provider="scorer", input={}, **{"as": "rows"}),
                    _step(
                        id="guard",
                        **{
                            "assert": {
                                "left": "$steps.rows.count",
                                "op": "gt",
                                "right": 0,
                                "message": "تحذير — 空 ⚠",
                            }
                        },
                    ),
                ],
                "returns": "rows",
                "precondition": {},
                "budget": budget,
            },
        )
    )
    entries.append(
        (
            "matrix-max-procedure-steps",
            {
                "name": "matrix_max_steps",
                "steps": [
                    _step(id=f"s{index:03d}", query="named_query", **{"as": f"a{index:03d}"})
                    for index in range(100)
                ],
                "returns": "a099",
                "precondition": {},
                "budget": {"wall_clock_s": 600, "max_provider_calls": 0},
            },
        )
    )
    return entries


def collision_definitions() -> list[tuple[str, dict[str, Any]]]:
    """The v1 killer: v2 construct NAMES buried in ``dict[str, Any]`` leaves."""
    budget = {"wall_clock_s": 30, "max_provider_calls": 2}
    return [
        (
            "collision-provider-input-graph-keys",
            {
                "name": "collision_provider_input",
                "steps": [
                    _step(
                        id="call",
                        provider="scorer",
                        input={
                            "next": "somewhere",
                            "parameters": {"threshold": 3},
                            "project": {"fields": {"a": "b"}},
                            "guard": {"left": 1, "op": "eq", "right": 1},
                            "measurements": [{"name": "m"}],
                        },
                        **{"as": "rows"},
                    )
                ],
                "returns": "rows",
                "precondition": {},
                "budget": budget,
            },
        ),
        (
            "collision-query-params-arm-keys",
            {
                "name": "collision_query_params",
                "steps": [
                    _step(
                        id="read",
                        query="named_query",
                        params={"on_false": "$abort", "on_true": "next_step"},
                        **{"as": "rows"},
                    )
                ],
                "returns": "rows",
                "precondition": {},
                "budget": budget,
            },
        ),
        (
            "collision-shape-items-fields-graph-format",
            {
                "name": "collision_shape_fields",
                "steps": [
                    _step(id="seed", provider="scorer", input={}, **{"as": "raw"}),
                    _step(
                        id="shape",
                        shape_items={
                            "items": "$steps.raw",
                            "fields": {"graph_format": "$item.graph_format"},
                        },
                        **{"as": "rows"},
                    ),
                ],
                "returns": "rows",
                "precondition": {},
                "budget": budget,
            },
        ),
        (
            "collision-relationship-state-graph-keys",
            {
                "name": "collision_relationship_state",
                "steps": [
                    _step(
                        id="read",
                        query="named_query",
                        relationship_state={"graph_format": 2, "on_true": "x"},
                        **{"as": "rows"},
                    )
                ],
                "returns": "rows",
                "precondition": {},
                "budget": budget,
            },
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tau3-procedures", type=Path, default=DEFAULT_TAU3_PROCEDURES)
    args = parser.parse_args()

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    sources: list[tuple[str, dict[str, Any]]] = []
    sources.extend(collect_config_donor_definitions())
    sources.extend(collect_tau3(args.tau3_procedures))
    sources.extend(collect_test_definitions())
    sources.extend(matrix_definitions())
    sources.extend(collision_definitions())

    written = 0
    for source_label, payload in sources:
        entry = _normalized_entry(source_label, payload)
        path = CORPUS_DIR / f"{_slug(source_label)}.json"
        if path.exists():
            existing = json.loads(path.read_text())
            if existing["digest_v1"] != entry["digest_v1"]:
                raise SystemExit(
                    f"REFUSED: '{source_label}' already has a frozen digest "
                    f"{existing['digest_v1']} and would be rewritten to {entry['digest_v1']}. "
                    "A captured digest is a stored commitment; changing one is a "
                    "release-blocking event requiring a decision record."
                )
            continue
        path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        written += 1
    print(f"corpus entries: {len(sources)} scanned, {written} newly captured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

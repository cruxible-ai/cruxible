"""Where each artifact family holds another artifact's digest, field by field.

Moving a definition moves the digests that name it. Rewriting every string that
equals an old digest would also rewrite literal data that merely holds one (a
schema constant, a claimed value), so re-pinning reads and writes only the
fields listed here. A family absent from the table has no automatic re-pinning.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

_PIN = ("*", "artifact_digest")

# Paths are field names, with "*" stepping into each list element. For the
# definition families every digest field is a reference; for Claims and
# Documents only the fields naming other artifacts are, never content digests.
REFERENCE_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "capture-contracts/": (
        ("pins", *_PIN),
        ("coordinate_schema_pins", *_PIN),
        ("selector_schema_pins", *_PIN),
        ("commitment_canonicalizer", "artifact_digest"),
        ("retention_erasure_policy", "erasure_rule_digest"),
        ("replay_policy_digest",),
        ("provenance_rule_digest",),
        ("source_subject_mapping_digest",),
    ),
    "claim-types/": (
        ("pins", *_PIN),
        ("evidence_admission_policy", "rules", "*", "capture_contract_digests", "*"),
        ("evidence_admission_policy", "rules", "*", "allowed_reducer_digests", "*"),
        ("admission_policy", "corroboration_requirements", "*", "query_definition_digest"),
    ),
    "query-definitions/": (("pins", *_PIN),),
    "claims/": (
        ("statement", "claim_type_digest"),
        ("pins", *_PIN),
        ("backing", "input_claim_digests", "*"),
        ("backing", "reducer_digest"),
    ),
    "documents/": (("pins", "*", "target_digest"),),
}


def reference_fields(path: str) -> tuple[tuple[str, ...], ...] | None:
    """This path's reference fields, or None when its family has no table entry."""

    for prefix, fields in REFERENCE_FIELDS.items():
        if path.startswith(prefix):
            return fields
    return None


def _at(value: object, steps: tuple[str, ...], remap: Mapping[str, str] | None) -> Iterator[str]:
    """Yield the digest at ``steps``; with ``remap``, rewrite it in place too."""

    if not steps:
        return
    head, rest = steps[0], steps[1:]
    if head == "*":
        if isinstance(value, list):
            for index, item in enumerate(value):
                if not rest and isinstance(item, str):
                    yield item
                    if remap is not None:
                        value[index] = remap.get(item, item)
                else:
                    yield from _at(item, rest, remap)
        return
    if not isinstance(value, dict) or head not in value:
        return
    item = value[head]
    if not rest:
        if isinstance(item, str):
            yield item
            if remap is not None:
                value[head] = remap.get(item, item)
        return
    yield from _at(item, rest, remap)


def referenced_digests(path: str, payload: Mapping[str, Any]) -> Iterator[str]:
    for steps in reference_fields(path) or ():
        yield from _at(payload, steps, None)


def move_references(
    path: str, payload: Mapping[str, Any], remap: Mapping[str, str]
) -> dict[str, Any]:
    """A copy of ``payload`` with its reference fields moved through ``remap``."""

    fields = reference_fields(path)
    if fields is None:
        raise ValueError(f"{path} has no reference table; it cannot be re-pinned automatically")
    moved: dict[str, Any] = json.loads(json.dumps(payload))
    for steps in fields:
        for _ in _at(moved, steps, remap):
            pass
    return moved

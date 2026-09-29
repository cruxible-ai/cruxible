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
# A Claim's capture-contract pins are provenance -- the exact contract versions its
# evidence used -- so a Claim's references skip them and they never move.
_REQUIRED_CLAIM_PIN = ("*!role=capture-contract", "artifact_digest")

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
        # Historical exact-digest evidence rules; v6 rules name contracts by
        # identity and need no re-pinning.
        ("evidence_admission_policy", "rules", "*", "capture_contract_digests", "*"),
        ("evidence_admission_policy", "rules", "*", "allowed_reducer_digests", "*"),
        ("admission_policy", "corroboration_requirements", "*", "query_definition_digest"),
    ),
    "query-definitions/": (("pins", *_PIN),),
    "claims/": (
        ("statement", "claim_type_digest"),
        ("pins", *_REQUIRED_CLAIM_PIN),
        ("backing", "input_claim_digests", "*"),
        ("backing", "reducer_digest"),
    ),
    "documents/": (("pins", "*", "target_digest"),),
}


# Where a definition names another governed definition by identity. These never
# move -- an identity follows every version -- but whoever carries the naming
# definition must carry what it names.
IDENTITY_REFERENCE_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "claim-types/": (("evidence_admission_policy", "rules", "*", "capture_contracts", "*"),),
}


def referenced_identities(path: str, payload: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """The (kind, name) identities ``payload`` names by reference, never by digest."""

    for prefix, fields in IDENTITY_REFERENCE_FIELDS.items():
        if not path.startswith(prefix):
            continue
        for steps in fields:
            yield from _identities_at(payload, steps)


def _identities_at(value: object, steps: tuple[str, ...]) -> Iterator[tuple[str, str]]:
    if not steps:
        target = value.get("target") if isinstance(value, dict) else None
        if isinstance(target, dict) and isinstance(target.get("kind"), str):
            name = target.get("name")
            if isinstance(name, str):
                yield target["kind"], name
        return
    head, rest = steps[0], steps[1:]
    if head == "*":
        for item in value if isinstance(value, list) else ():
            yield from _identities_at(item, rest)
    elif isinstance(value, dict) and head in value:
        yield from _identities_at(value[head], rest)


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
    if head.startswith("*!"):
        # "*!field=value": every list element except those whose field equals value.
        field, _, excluded = head[2:].partition("=")
        if isinstance(value, list):
            for item in value:
                if not (isinstance(item, dict) and item.get(field) == excluded):
                    yield from _at(item, rest, remap)
        return
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

"""One Claim as a text line: the shared builder behind since, audit and coverage lines.

A line reads ``subject  field[qualifier] = value  [CLM-…]`` (design §3). The
statement comes from the record being shown, never from a second read of
current state, so a builder call costs one parsed Claim; the accepted predicate
set the field name is shortened against is read once per request. A line for a
Claim the bound projection holds (coverage cards) reads the projection's Claim
index instead: keyed reads of the rows shown, and no body parse.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ExactContentClaimObject,
    NoneClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.read_values import ClaimLine, summary_value
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.discovery.query_values import claim_values_by_path


def subject_ref(path: str) -> str:
    """``subjects/<kind>/<id>.json`` as the ``kind/id`` shorthand an agent types."""

    if path.startswith("subjects/") and path.endswith(".json"):
        return path[len("subjects/") : -len(".json")]
    return path


def live_predicates(projection: Any) -> frozenset[str]:
    """Every live accepted predicate: the vocabulary field names are shortened against."""

    return frozenset(
        str(identity).removeprefix("ClaimType:")
        for (identity,) in projection.typed.connection.execute(
            "SELECT identity FROM claim_types WHERE lifecycle='live'"
        )
    )


def claim_line(claim: ClaimArtifact, *, accepted_predicates: Collection[str]) -> ClaimLine:
    """The text line for one parsed Claim artifact."""

    statement = claim.statement
    subject = subject_ref(statement.subject.artifact_path)
    kind = subject.split("/", 1)[0]
    obj = statement.object
    value: object
    if isinstance(obj, SubjectClaimObject):
        value = subject_ref(obj.address.artifact_path)
    elif isinstance(obj, ExactContentClaimObject):
        value = obj.content_digest
    elif isinstance(obj, NoneClaimObject):
        # An inverse (ClaimType v9) shows its object as stored, never as a value.
        value = obj.model_dump(mode="json")
    else:
        value = obj.value
    return ClaimLine(
        claim=claim.identity.name,
        subject=subject,
        field=short_field_name(statement.predicate, kind, accepted_predicates),
        qualifier=statement.qualifier,
        value=summary_value(value),
    )


def _accepted_among(projection: Any, names: Collection[str]) -> frozenset[str]:
    """Which of ``names`` are live accepted predicates: keyed reads, never a vocabulary scan."""

    wanted = sorted(set(names))
    if not wanted:
        return frozenset()
    marks = ",".join("?" for _ in wanted)
    return frozenset(
        str(identity).removeprefix("ClaimType:")
        for (identity,) in projection.typed.connection.execute(
            f"SELECT identity FROM claim_types WHERE identity IN ({marks}) AND lifecycle='live'",
            tuple(f"ClaimType:{name}" for name in wanted),
        )
    )


def indexed_claim_lines(projection: Any, paths: Sequence[str]) -> tuple[ClaimLine, ...]:
    """The text lines for the Claims at ``paths``, read from the projection's Claim index.

    The index already carries each Claim's statement columns, so the lines cost
    keyed reads of the rows asked for and no Claim body parse (a literal the
    index does not carry falls back to the body). Lines follow ``paths``' order;
    a path the projection does not hold yields no line.
    """

    values = claim_values_by_path(projection, paths)
    held = [values[path] for path in paths if path in values]
    kinds = {item.identity: subject_ref(item.subject_path).split("/", 1)[0] for item in held}
    accepted = _accepted_among(
        projection,
        {
            item.predicate[len(kinds[item.identity]) + 1 :]
            for item in held
            if item.predicate.startswith(f"{kinds[item.identity]}.")
        },
    )
    return tuple(
        ClaimLine(
            claim=item.identity.removeprefix("Claim:"),
            subject=subject_ref(item.subject_path),
            field=short_field_name(item.predicate, kinds[item.identity], accepted),
            qualifier=item.qualifier,
            value=summary_value(item.value),
        )
        for item in held
    )


__all__ = ["claim_line", "indexed_claim_lines", "live_predicates", "subject_ref"]

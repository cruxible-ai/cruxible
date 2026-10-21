"""One Claim as a text line: the shared builder behind since, audit and coverage lines.

A line reads ``subject  field[qualifier] = value  [CLM-…]`` (design §3). The
statement comes from the record being shown, never from a second read of
current state, so a builder call costs one parsed Claim; the accepted predicate
set the field name is shortened against is read once per request.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ExactContentClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.read_values import ClaimLine, summary_value
from cruxible_core.service.discovery.field_names import short_field_name


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


def claim_line(claim: ClaimArtifactAny, *, accepted_predicates: Collection[str]) -> ClaimLine:
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
    else:
        value = obj.value
    return ClaimLine(
        claim=claim.identity.name,
        subject=subject,
        field=short_field_name(statement.predicate, kind, accepted_predicates),
        qualifier=statement.qualifier,
        value=summary_value(value),
    )


__all__ = ["claim_line", "live_predicates", "subject_ref"]

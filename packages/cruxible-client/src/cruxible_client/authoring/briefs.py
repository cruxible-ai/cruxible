"""Compatibility entry point for canonical ``knowledge.brief`` lowering."""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping, Sequence

from cruxible_client.authoring.inputs import BriefInput, lower_authoring_input
from cruxible_client.contracts.semantic import SemanticAddress

_SUBJECT_PATH_RE = re.compile(
    r"^subjects/(?P<kind>[a-z][a-z0-9_.]*)/(?P<id>[a-z][a-z0-9_.-]*)\.yaml$"
)


def _subject_shorthand(value: Mapping[str, Any]) -> str:
    address = SemanticAddress.model_validate(value)
    if address.selector.scheme != "artifact-v1":
        raise ValueError("Brief subject must address a whole Subject artifact")
    match = _SUBJECT_PATH_RE.fullmatch(address.artifact_path)
    if match is None:
        raise ValueError("Brief subject must use a canonical subjects/<kind>/<id>.yaml path")
    return f"{match['kind']}/{match['id']}"


def _without_tag(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: member for key, member in value.items() if key != "tag"}


def prepare_playbill_brief(
    *,
    subject: Mapping[str, Any],
    purpose: str,
    kind: Literal["brief", "guidance", "faq"] = "brief",
    claim_refs: Sequence[Mapping[str, Any]] = (),
    query_refs: Sequence[Mapping[str, Any]] = (),
    prose: str = "",
    audience: Literal["agent", "human", "both"] | None = None,
    rationale: str,
    claim_ref: str | None = None,
    existing_claim_dispositions: Sequence[Mapping[str, Any]] = (),
    accepted_tree: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """Lower through the one canonical authoring-input implementation.

    This compatibility wrapper remains for callers of the pre-SDK helper. References
    are resolved against ``accepted_tree`` by the canonical lowerer rather than trusted
    from caller-supplied digests.
    """

    authoring = BriefInput.model_validate(
        {
            "kind": "brief",
            "subject": _subject_shorthand(subject),
            "purpose": purpose,
            "brief_kind": kind,
            "claim_refs": [_without_tag(item) for item in claim_refs],
            "query_refs": [_without_tag(item) for item in query_refs],
            "prose": prose,
            "audience": audience,
            "rationale": rationale,
            "claim_id": claim_ref,
            "dispositions": [_without_tag(item) for item in existing_claim_dispositions],
        }
    )
    return lower_authoring_input(
        authoring,
        tree={} if accepted_tree is None else dict(accepted_tree),
    ).model_dump(mode="json")


__all__ = ["prepare_playbill_brief"]

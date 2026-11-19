"""Author a recommendation once; derive Claims, retained input, and prose.

Application vocabulary example, not a generic SDK record API. The caller supplies
accepted typed Subject/ClaimType refs for two string-valued normative predicates,
or refs staged in the same ChangeSetDraft.
The vocabulary must admit authored recommendations under its explicit evidence
policy. Acceptance records the recommendation; it does not adopt the policy.

Create with stage(cx, subject, fields, Recommendation(...)).prepare().submit().
Review intent.proposal through the ordinary governed workflow. After acceptance,
read the two fields from a fresh world and pass those ClaimViews to render().
Publish the resulting body using the existing block API. This renderer itself
does not produce a publication or Procedure execution receipt.

To revise, supply the previous typed record and exact Claim refs for changed
fields. Those refs must come from the coordinate used to read previous. The
helper never searches for, resolves, or silently replaces competing Claims.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from cruxible_client import ClaimRef, ClaimRole, ClaimTypeRef, Cruxible, Disposition, SubjectRef
from cruxible_client.authoring.sdk import ChangeSetDraft, ClaimView

Field = Literal["rule", "rationale"]
FIELDS: tuple[Field, ...] = ("rule", "rationale")


@dataclass(frozen=True)
class Recommendation:
    rule: str
    rationale: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in asdict(self).values()):
            raise ValueError("Recommendation rule and rationale must be nonempty strings")


def stage(
    cx: Cruxible | ChangeSetDraft,
    subject: SubjectRef,
    fields: Mapping[Field, ClaimTypeRef],
    record: Recommendation,
    *,
    previous: Recommendation | None = None,
    replacements: Mapping[Field, ClaimRef] | None = None,
) -> ChangeSetDraft:
    """Stage creation or explicitly selected replacements as one governed change.

    Pass an existing changeset to compose records and vocabulary in one proposal.
    Each replacement contradicts the named prior Claim. The caller is responsible
    for reviewing that disposition and selecting the correct Claim for each field.
    """
    if set(fields) != set(FIELDS):
        raise ValueError("Supply exactly the rule and rationale ClaimTypes")
    changed = tuple(
        name
        for name in FIELDS
        if previous is None or getattr(record, name) != getattr(previous, name)
    )
    replacements = {} if replacements is None else replacements
    expected = set() if previous is None else set(changed)
    if set(replacements) != expected:
        raise ValueError("Supply exact Claim replacements for changed fields only")
    if not changed:
        raise ValueError("Recommendation is unchanged; no submission is needed")
    if len({ref.address for ref in fields.values()}) != len(FIELDS):
        raise ValueError("Each field needs a distinct ClaimType")
    source = json.dumps(
        {"kind": "authored-recommendation", "subject": subject.address, **asdict(record)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    change = cx if isinstance(cx, ChangeSetDraft) else cx.changes(rationale=record.rationale)
    for name in changed:
        replaces = replacements.get(name)
        change.claim(
            subject=subject,
            predicate=fields[name],
            value=getattr(record, name),
            role=ClaimRole.NORMATIVE,
            rationale=record.rationale,
            self_source=source,
            revises=replaces,
            dispositions={} if replaces is None else {replaces: Disposition.CONTRADICT},
        )
    return change


def render(subject: str, fields: Mapping[Field, str], rows: Sequence[ClaimView]) -> str:
    """Render one fully observed recommendation from selected accepted state.

    The caller must obtain complete rows at one accepted coordinate and pass
    the canonical subject artifact path used by ClaimView.subject. Missing,
    competing, inactive, or unsupported Claims refuse instead of choosing a value.
    """
    if set(fields) != set(FIELDS) or len(set(fields.values())) != len(FIELDS):
        raise ValueError("Supply two distinct recommendation predicates")
    values: dict[Field, str] = {}
    for name in FIELDS:
        matches = [row for row in rows if row.subject == subject and row.predicate == fields[name]]
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one selected Claim for {name}")
        row = matches[0]
        if (
            row.verdict != "supported"
            or row.lifecycle_state != "live"
            or row.role != "normative"
            or row.qualifier is not None
            or row.object_kind != "literal"
            or not isinstance(row.value, str)
        ):
            raise ValueError(f"Expected a supported live normative string Claim for {name}")
        values[name] = row.value
    record = Recommendation(rule=values["rule"], rationale=values["rationale"])
    return f"## Recommendation\n\n{record.rule}\n\n{record.rationale}\n"

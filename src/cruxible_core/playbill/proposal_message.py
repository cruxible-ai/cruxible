"""Prose commit messages for the ledger's proposal and generation commits.

The ledger is Git, so review is Git: the message a reviewer reads on
`refs/proposals/<actor>/<name>` (and on the accepted generation that settles it)
has to say what the change set does, in the same shape a reviewer would write by
hand. It is prose and nothing else. No caller parses a commit message, and the
guardrail in `tests/test_guardrails/test_commit_messages_are_prose.py` is what
keeps it that way: every machine-readable fact about a proposal lives in the
evidence store, in the candidate record, and in the note refs projected from
them.

Because it is only prose, it is also cheap to be exact about: the whole message
is a pure function of the candidate's own members, so the advisory review-ref
projection rebuilt from stored evidence carries byte-identical text to the
commit the actor's submission created.
"""

from __future__ import annotations

from collections.abc import Sequence

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
)

CandidateMember = CandidateMemberEvidence | CandidateMemberLawEvidenceV2
"""Either candidate member shape; both name a disposition, a kind and a path."""

SUBJECT_LIMIT = 72
"""Git's own conventional subject width; a longer summary moves to the body."""

_ELLIPSIS = "..."


def _truncate_subject(text: str) -> str:
    if len(text) <= SUBJECT_LIMIT:
        return text
    return text[: SUBJECT_LIMIT - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _kind_tally(members: Sequence[CandidateMember]) -> str:
    counts: dict[str, int] = {}
    for member in members:
        counts[member.artifact_kind] = counts.get(member.artifact_kind, 0) + 1
    return ", ".join(
        f"{counts[kind]} {kind}" for kind in sorted(counts, key=lambda item: item.encode("utf-8"))
    )


def _qualifier(member: CandidateMember) -> str | None:
    """Name the one fact each member shape adds beyond disposition and kind.

    The two shapes qualify a member differently and neither qualifier is
    guessable from the other: v1 records the governance operation that
    separates, say, a principal revocation from a rotation, while v2 records
    whether the member was authored or derived by closure. `authored` is the
    unremarkable case and is left off rather than restated on every line.
    """

    if isinstance(member, CandidateMemberEvidence):
        return member.governance_operation
    return None if member.closure_role == "authored" else member.closure_role


def member_line(member: CandidateMember) -> str:
    """Render one member as `<disposition> <kind> <address> [qualifier]`."""

    line = f"{member.disposition} {member.artifact_kind} {member.path}"
    qualifier = _qualifier(member)
    if qualifier is not None:
        line = f"{line} [{qualifier}]"
    return line


def change_set_summary(members: Sequence[CandidateMember]) -> str:
    """State what this change set does in one line, before any truncation."""

    if not members:
        return "Record Playbill proposal"
    if len(members) == 1:
        return member_line(members[0])
    return f"Propose {len(members)} members: {_kind_tally(members)}"


def _message(subject_source: str, members: Sequence[CandidateMember]) -> str:
    """Assemble subject, the untruncated summary when it did not fit, then the roll.

    The per-member roll is dropped when it would only repeat the subject, which
    is exactly the one-member proposal whose summary IS its member line. A
    reviewer reading `git log --oneline` sees the same sentence either way; a
    message that said it twice would just be noise on the commonest proposal.
    """

    subject = _truncate_subject(subject_source)
    paragraphs = [subject]
    if subject != subject_source:
        paragraphs.append(subject_source)
    ordered = sorted(members, key=lambda item: item.path.encode("utf-8"))
    roll = "\n".join(member_line(member) for member in ordered)
    if roll and roll != subject_source:
        paragraphs.append(roll)
    return "\n\n".join(paragraphs) + "\n"


def proposal_commit_message(members: Sequence[CandidateMember]) -> str:
    """Render the candidate commit's message: a summary line, then one line per member."""

    return _message(change_set_summary(members), members)


def generation_commit_message(
    members: Sequence[CandidateMember],
    *,
    sequence: int,
) -> str:
    """Render the accepted generation's message: its own subject, the same body.

    A generation's subject names the sequence, because that is what an operator
    reading accepted history is looking for. The body is the proposal's, so the
    settled commit and the proposal it settles read as the same change set.
    """

    return _message(f"Accept Playbill generation {sequence}", members)


__all__ = [
    "SUBJECT_LIMIT",
    "CandidateMember",
    "change_set_summary",
    "generation_commit_message",
    "member_line",
    "proposal_commit_message",
]

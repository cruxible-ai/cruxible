"""Canonical bytes for proposal evidence, and the note refs that project it.

These renderers sit below both the evidence store and the proposal service so
that the file the daemon persists and the note a reviewer reads out of Git are
produced by one function each. Nothing here touches the filesystem or Git; it is
only the byte shapes, so neither of the two callers can drift from the other.
"""

from __future__ import annotations

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
)


def admission_bytes(record: ProposalAdmissionRecord) -> bytes:
    """Render one admission's canonical persisted bytes.

    The record's `limits` is written as the RECEIVE bounds alone, never as the
    whole `ProposalReceiveLimits` model. Two things depend on it:

    * an admission is immutable, and an idempotent re-submission rewrites the
      same path with the same bytes -- so a build that added an advertised
      ceiling would render different bytes for the same admission and trip the
      occupancy refusal on a proposal it was supposed to answer with a no-op;
    * a stored admission's canonicality is verified on every read against a
      re-render, so a record written before that ceiling existed would stop
      being readable at all.

    What an admission records is what receive enforced on it. An advertisement
    that preflight consults before lowering is not that, and is recovered from
    the model's own defaults on read.
    """

    payload = record.model_dump(mode="json")
    payload["limits"] = record.limits.receive_bound_payload()
    return canonical_bytes(payload) + b"\n"


def evaluation_bytes(record: ProposalEvaluationRecord) -> bytes:
    """Render one evaluation's canonical persisted bytes."""

    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


def proposal_evaluation_note(
    *,
    admission: ProposalAdmissionRecord,
    evaluation: ProposalEvaluationRecord,
) -> bytes:
    """Render the note that projects one proposal's evaluation onto its commit.

    The note is a PROJECTION, never a second record: it is the admission's
    stored bytes followed by the evaluation's stored bytes, each byte-identical
    to the file the store holds. Both end in a newline, so the concatenation
    stays unambiguous, and a reader with nothing but Git gets exactly what the
    daemon persisted -- the admission it was received under, the verdict, and
    every diagnostic behind a refusal.
    """

    if evaluation.proposal_id != admission.proposal_id:
        raise ProposalIntegrityError("evaluation note joins records of different proposals")
    return admission_bytes(admission) + evaluation_bytes(evaluation)


__all__ = [
    "admission_bytes",
    "evaluation_bytes",
    "proposal_evaluation_note",
]

"""One accepted generation as a read row: the generations section's and the activity feed's.

A row is one history-index locator, the generation record's small facts (parsed
once per process per generation) and its accepting admission (read once per
process by digest): it costs its own generation, never the history around it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cruxible_client.contracts.orient import ORIENT_BECAUSE_MAX_CHARS, OrientGeneration
from cruxible_core.indexes.history.history_index import (
    AcceptedGenerationLocation,
    HistoryReader,
    RetainedRecordReader,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.change_reasons import ChangeReasons


def generation_accepted_at(
    instance: PlaybillInstance,
    history: HistoryReader,
    records: RetainedRecordReader,
    location: AcceptedGenerationLocation,
) -> datetime:
    """When one generation was accepted: its record's timestamp, or genesis's commit time."""

    if location.sequence == 0:
        return instance.accepted_evaluation_time(location.git_oid)
    return _timestamp(history.read_generation_facts(location.sequence, records).timestamp)


def _timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def generation_row(
    instance: PlaybillInstance,
    history: HistoryReader,
    records: RetainedRecordReader,
    reasons: ChangeReasons,
    location: AcceptedGenerationLocation,
) -> OrientGeneration:
    """One generation as a row; genesis carries only its sequence, git oid and time."""

    if location.sequence == 0:
        return OrientGeneration(
            sequence=0,
            git_oid=location.git_oid,
            accepted_at=generation_accepted_at(instance, history, records, location),
        )
    facts = history.read_generation_facts(location.sequence, records)
    accepted_at = _timestamp(facts.timestamp)
    admission = reasons.admission(location)
    because = None if admission is None else admission.rationale or None
    cut = because is not None and len(because) > ORIENT_BECAUSE_MAX_CHARS
    return OrientGeneration(
        sequence=location.sequence,
        git_oid=location.git_oid,
        accepted_at=accepted_at,
        changeset_digest=location.source_record_digest,
        proposal=None if admission is None else admission.proposal_id,
        proposer=location.actor_id,
        approvals=tuple(dict.fromkeys(facts.approval_signers)),
        because=(
            because[: ORIENT_BECAUSE_MAX_CHARS - 1] + "…"
            if cut and because is not None
            else because
        ),
        because_truncated=cut,
        members=facts.member_count,
    )


__all__ = ["generation_accepted_at", "generation_row"]

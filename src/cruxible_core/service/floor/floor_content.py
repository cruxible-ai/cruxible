"""Searchable current state and explicitly attributed review context for floor v3.

Accepted values come only from the accepted tree. Review prose is a separate
Git-note snapshot, never a Claim or proof of adoption. No evidence bodies,
working files, or authoring-intent exhaust are read.
"""

from __future__ import annotations

import json
from collections import defaultdict

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claims import ClaimArtifactAny, claim_artifact_digest, claim_path
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.primitives import pretty_json
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
)
from cruxible_core.indexes.history.history_index import AcceptedGenerationLocation
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.proposal_notes import admission_bytes, evaluation_bytes
from cruxible_core.proposals.settlement import ChangeSetRecordAnyVersion
from cruxible_core.runtime.instance import PlaybillInstance

MAX_REVIEW_SNAPSHOT_BYTES = 64 * 1024 * 1024


def _render(value: object) -> bytes:
    return pretty_json(json.loads(canonical_bytes(value))).encode("utf-8") + b"\n"


def review_snapshot_oid(instance: PlaybillInstance) -> str | None:
    return instance._ledger.mirror_refs().get("refs/notes/playbill-eval")


def review_context(
    instance: PlaybillInstance, notes_oid: str | None
) -> tuple[dict[str, tuple[dict[str, object], ...]], str]:
    """Read canonical note pairs from the exact immutable notes commit.

    Association to an accepted candidate is not approval of the author's prose.
    Missing notes are explicit unavailable context, never invented rationale.
    """
    if notes_oid is None:
        return {}, "unavailable"
    entries = instance._ledger.list_tree_with_sizes(notes_oid)
    if sum(entry.size or 0 for entry in entries) > MAX_REVIEW_SNAPSHOT_BYTES:
        return {}, "review_snapshot_budget_exceeded"
    notes = instance._ledger.read_tree(notes_oid)
    by_candidate: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for path, content in notes.items():
        lines = content.splitlines(keepends=True)
        if len(lines) % 2:
            raise ProposalIntegrityError("floor review note has an incomplete record pair")
        for i in range(0, len(lines), 2):
            admission = ProposalAdmissionRecord.model_validate_json(lines[i])
            evaluation = ProposalEvaluationRecord.model_validate_json(lines[i + 1])
            if (
                admission_bytes(admission) != lines[i]
                or evaluation_bytes(evaluation) != lines[i + 1]
                or admission.proposal_id != evaluation.proposal_id
            ):
                raise ProposalIntegrityError("floor review note is not a canonical proposal pair")
            if evaluation.verdict != "candidate" or evaluation.candidate_digest is None:
                continue
            item: dict[str, object] = {
                "proposal_id": admission.proposal_id,
                "reported_actor": admission.actor_id,
                "rationale": admission.rationale,
                "candidate_commit_oid": admission.candidate_commit_oid,
                "notes_commit_oid": notes_oid,
                "note_path": path,
            }
            # Several note aliases may project the same proposal. Retain its
            # prose once, selecting a stable alias rather than repeating it.
            previous = by_candidate[evaluation.candidate_digest].get(admission.proposal_id)
            if previous is None:
                by_candidate[evaluation.candidate_digest][admission.proposal_id] = item
            elif {k: v for k, v in previous.items() if k != "note_path"} != {
                k: v for k, v in item.items() if k != "note_path"
            }:
                raise ProposalIntegrityError("floor note aliases disagree about a proposal")
    return (
        {
            candidate: tuple(items[key] for key in sorted(items))
            for candidate, items in by_candidate.items()
        },
        "available",
    )


def current_content(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claims: tuple[ClaimArtifactAny, ...],
    notes_oid: str | None,
) -> dict[str, bytes]:
    context, context_status = review_context(instance, notes_oid)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    relevant_changes: dict[int, tuple[AcceptedGenerationLocation, ChangeSetRecordAnyVersion]] = {}
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        for claim in claims:
            if claim.lifecycle.state != "live":
                continue
            location = history.latest_member(claim_path(claim.identity.name))
            sequence = None if location is None else location.sequence
            if location is not None and location.sequence not in relevant_changes:
                relevant_changes[location.sequence] = (
                    history.generation(location.sequence),
                    history.read_member_record(location, instance.blob_at),
                )
            grouped[claim.statement.subject.artifact_path].append(
                {
                    "claim": claim.identity.qualified,
                    "artifact_digest": claim_artifact_digest(claim).tagged,
                    "statement": claim.statement.model_dump(mode="json"),
                    "latest_change_sequence": sequence,
                }
            )
    files: dict[str, bytes] = {}
    for subject, rows in sorted(grouped.items()):
        relative = subject.removeprefix("subjects/")
        sorted_claims = sorted(rows, key=lambda row: str(row["claim"]).encode())
        files["current/" + relative] = _render(
            {
                "subject": subject,
                "scope": (
                    "all live accepted Claims; contenders are preserved; no current verdict implied"
                ),
                "claims": sorted_claims,
            }
        )
    for sequence, (generation, record) in sorted(relevant_changes.items()):
        review_entries = tuple(
            row
            for row in context.get(record.candidate_digest, ())
            if row["reported_actor"] == record.actor_binding.actor_id
        )
        files[f"provenance/changes/{sequence:020d}.json"] = _render(
            {
                "kind": "accepted-change-with-associated-review-context",
                "sequence": sequence,
                "accepted_git_oid": generation.git_oid,
                "candidate_digest": record.candidate_digest,
                "actor": record.actor_binding.actor_id,
                "timestamp": record.candidate.timestamp,
                "affected_paths": sorted(member.path for member in record.members),
                "review_context_status": context_status if review_entries else "unavailable",
                "review_context": list(review_entries),
                "interpretation": (
                    "Review rationale is attributed context, "
                    "not accepted Claim content or adoption."
                ),
                "claim_authoring_rationale": (
                    "Not inferred from change rationale; "
                    "unavailable unless represented in accepted content."
                ),
            }
        )
    files["provenance/snapshot.json"] = _render(
        {
            "accepted_git_oid": coordinate.git_oid,
            "evaluation_notes_oid": notes_oid,
            "status": context_status,
            "rebuild_inputs": "accepted ledger plus this immutable Git notes snapshot",
            "history": "Only changes introducing the current Claim revisions are exported.",
        }
    )
    files["README.md"] = (
        "# Playbill searchable floor\n\n"
        "Start with current/ for full live Claim values, including competing values. "
        "These are accepted statements, not time-relative supported verdicts.\n\n"
        "subjects/ and claim-types/ provide bounded discovery summaries. "
        "provenance/ contains the latest changes behind current Claims and separately "
        "attributed review rationale, where retained in the pinned Git notes snapshot.\n\n"
        "History, rejected proposals, full evaluation transcripts, source bodies, and "
        "authoring-intent exhaust are not exported. No match here does not prove "
        "absence from those surfaces. Evidence and exact-content bodies require "
        "their normal authorized expansion; this export never reads them.\n\n"
        "The manifest binds every file. The accepted coordinate and the notes commit "
        "in provenance/snapshot.json are separate rebuild inputs. "
        "Agent-chosen projections can provide more useful reading layouts.\n"
    ).encode()
    return files

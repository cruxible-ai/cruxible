"""Searchable current state and explicitly attributed review context for floor v3.

Accepted values come only from the accepted tree. Review prose is a separate
Git-note snapshot, never a Claim or proof of adoption. No evidence bodies,
working files, or authoring-intent exhaust are read.
"""

from __future__ import annotations

import json
from collections import defaultdict

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claims import ClaimArtifactAny
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
from cruxible_core.service.floor.floor_current import (
    PROVENANCE_SUBJECTS_PREFIX,
    ValueRenderer,
    accepted_claim_types,
    accepted_subjects,
    claim_verdicts,
    claims_by_subject,
    floor_stamp,
    render_subject,
    stamped,
)
from cruxible_core.service.floor.floor_documents import document_files, document_parts
from cruxible_core.storage.cas import BodyAccessContext

MAX_REVIEW_SNAPSHOT_BYTES = 64 * 1024 * 1024

FLOOR_README = """\
# Playbill floor

Accepted state as plain files, for grep. Search here; confirm and act with the
verbs. The floor does no matching of its own: grep is the search.

## What to grep

- `current/<kind>/<id>.yaml`: one file per Subject. Its first line is
  `# <ref>  kind=<kind>  at <git_oid> gen <n>`, the coordinate the file is as
  of. Then each field's current value under its short name (a many-valued or
  contested field lists every value), each followed by `# CLM-... CAP-...`:
  the Claim that states it and the Captures it cites. A Subject-valued field
  shows the other Subject's ref. `flags:` lists each flagged field's verdict
  problems (stale, contested, contradicted, uncovered, unsure_hold) as of
  that coordinate.
- `current/<kind>/<id>.<field>.txt`: a text value too long to inline, whole.
- `documents/<name>.<ext>`: each Document, a one-line header and its body.

## The loop

1. `grep -rn "some text" .playbill/floor/current .playbill/floor/documents`
2. Read the hit's first line: its ref, and the generation it is as of.
3. `cruxible playbill get <ref>` for the live values and verdicts (the flags
   here are as of the floor's coordinate; `orient` says how far behind it is).
4. Change state with the write verbs (`cruxible playbill set|retire|write`).

An agent without a shell asks `query --contains "some text"` instead.

## Not for grep

- `provenance/`: the digests and full statements behind every current/ value
  (`subjects/`), Document envelopes (`documents/`), the latest accepted changes
  behind current Claims with separately attributed review rationale where the
  pinned Git notes snapshot retains it (`changes/`), and `snapshot.json`.
- `manifest.json` binds every file by digest into the floor digest;
  `coverage-manifest.json` is the export's coverage boundary.
- `subjects/`, `claim-types/` and `procedures/`: the discovery cards other
  tools read; they carry digests and addresses.

History, rejected proposals, full evaluation transcripts, source and evidence
bodies, and authoring-intent exhaust are not exported, so no match here does
not prove absence. Document bodies are included only when the exporting caller
may read bodies; otherwise the file says how to read one.
"""


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
    access: BodyAccessContext | None = None,
) -> dict[str, bytes]:
    context, context_status = review_context(instance, notes_oid)
    live = tuple(claim for claim in claims if claim.lifecycle.state == "live")
    grouped = claims_by_subject(live)
    shells = accepted_subjects(instance, coordinate)
    claim_types = accepted_claim_types(instance, coordinate)
    values = ValueRenderer(instance)
    files: dict[str, bytes] = {}
    relevant_changes: dict[int, tuple[AcceptedGenerationLocation, ChangeSetRecordAnyVersion]] = {}
    records = instance.retained_record_reader()
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        stamp = floor_stamp(instance, coordinate, history)
        verdicts = claim_verdicts(instance, coordinate, live, evaluation_time=stamp.accepted_at)
        for path, shell in shells.items():
            part = render_subject(
                path=path,
                shell=shell,
                claims=grouped.get(path, ()),
                claim_types=claim_types,
                accepted_predicates=frozenset(claim_types),
                verdicts=verdicts,
                values=values,
                history=history,
            )
            files.update(stamped(part, stamp))
            files[f"{PROVENANCE_SUBJECTS_PREFIX}{part.ref}.json"] = part.provenance
            for sequence in part.sequences:
                if sequence not in relevant_changes:
                    generation = history.generation(sequence)
                    relevant_changes[sequence] = (
                        generation,
                        history.read_generation_record(sequence, records),
                    )
    for document in document_parts(
        instance,
        coordinate=coordinate,
        access=access or BodyAccessContext(principal_id="playbill-floor"),
    ):
        files.update(document_files(document, stamp))
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
    files["README.md"] = FLOOR_README.encode()
    return files

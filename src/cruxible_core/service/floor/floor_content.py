"""Searchable current state and explicitly attributed review context for floor v3.

Accepted values come only from the accepted tree. Review prose is a separate
Git-note snapshot, never a Claim or proof of adoption. No evidence bodies,
working files, or authoring-intent exhaust are read.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claims import ClaimArtifactAny, claim_path
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.primitives import pretty_json
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRecord,
    ProposalEvaluationRecord,
)
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.indexes.history.history_index import AcceptedGenerationLocation, HistoryReader
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.proposals.proposal_notes import admission_bytes, evaluation_bytes
from cruxible_core.proposals.settlement import ChangeSetRecordAnyVersion
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from cruxible_core.service.floor.floor_current import (
    PROVENANCE_SUBJECTS_PREFIX,
    SubjectPart,
    ValueRenderer,
    accepted_claim_types,
    accepted_subjects,
    claim_verdicts,
    claims_by_subject,
    floor_stamp,
    index_files,
    render_subject,
    stamped,
)
from cruxible_core.service.floor.floor_documents import (
    DocumentPart,
    document_files,
    document_parts,
)
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
- `current/<kind>/INDEX`: one tab-separated line per Subject of that kind: its
  ref, a title-like value, and its state-like fields.
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
- `manifest.json` binds every file by digest into the floor digest.
- Only with `floor export --with-discovery`: `subjects/`, `claim-types/` and
  `procedures/`, the discovery cards other tools read (they carry digests and
  addresses), and `coverage-manifest.json`, the export's coverage boundary.

History, rejected proposals, full evaluation transcripts, source and evidence
bodies, and authoring-intent exhaust are not exported, so no match here does
not prove absence. Document bodies are included only when the exporting caller
may read bodies; otherwise the file says how to read one.
"""


def _render(value: object) -> bytes:
    return pretty_json(json.loads(canonical_bytes(value))).encode("utf-8") + b"\n"


def _change_file(
    sequence: int,
    generation: AcceptedGenerationLocation,
    record: ChangeSetRecordAnyVersion,
    *,
    context: Mapping[str, tuple[dict[str, object], ...]],
    context_status: str,
) -> bytes:
    review_entries = tuple(
        row
        for row in context.get(record.candidate_digest, ())
        if row["reported_actor"] == record.actor_binding.actor_id
    )
    return _render(
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
                "Review rationale is attributed context, not accepted Claim content or adoption."
            ),
            "claim_authoring_rationale": (
                "Not inferred from change rationale; "
                "unavailable unless represented in accepted content."
            ),
        }
    )


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


@dataclass(frozen=True)
class _CurrentState:
    """One export's current/ layer, kept so the next export renders only what changed."""

    sequence: int
    git_oid: str
    parts: dict[str, SubjectPart]
    claim_ids: dict[str, tuple[str, ...]]
    verdicts: dict[str, tuple[tuple[str, str | None, str, bool], ...]]
    documents: tuple[DocumentPart, ...]
    notes_oid: str | None
    context_status: str
    changes: dict[int, bytes]
    # Whether this export started from the previous one, and what it rendered afresh.
    incremental: bool
    rendered: tuple[str, ...]


def _changed_since(
    history: HistoryReader, previous: _CurrentState | None, sequence: int
) -> frozenset[str] | None:
    """Member paths changed since the previous export, or None to render everything.

    The previous export must be an ancestor in this accepted history. A changed
    ClaimType can rename a short field or its cardinality everywhere, so it
    renders everything.
    """

    if previous is None or previous.sequence > sequence:
        return None
    if history.generation(previous.sequence).git_oid != previous.git_oid:
        return None
    changed = history.member_paths_after(previous.sequence)
    if any(path.startswith("claim-types/") for path in changed):
        return None
    return changed


def current_content(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claims: tuple[ClaimArtifactAny, ...],
    notes_oid: str | None,
    access: BodyAccessContext | None = None,
    verdict_context: ClaimVerdictReadContext | None = None,
) -> dict[str, bytes]:
    """The grep-first layer: current/, INDEX, documents/ and their provenance.

    Incremental: the last export's per-Subject renders are kept on the
    instance. A later export in the same accepted history reads the member
    paths the change records touched since then and re-renders only the
    Subjects those changes, or a moved verdict, reach. Every file is then
    re-stamped with the new coordinate, so identical accepted state still gives
    identical bytes however the floor got there.
    """

    body_access = access or BodyAccessContext(principal_id="playbill-floor")
    key = (body_access.principal_id, body_access.can_read_body)
    remembered = memo_get(instance.floor_current_memo, key)
    previous = remembered if isinstance(remembered, _CurrentState) else None
    live = tuple(claim for claim in claims if claim.lifecycle.state == "live")
    grouped = claims_by_subject(live)
    claim_types, live_predicates = accepted_claim_types(instance, coordinate)
    values = ValueRenderer(instance)
    with instance.bind_accepted_projection(coordinate) as projection:
        subject_paths = tuple(
            sorted(
                (row.path for row in projection.typed.envelopes(kind="subject")),
                key=lambda item: item.encode(),
            )
        )
    parts: dict[str, SubjectPart] = {}
    claim_ids: dict[str, tuple[str, ...]] = {}
    verdict_keys: dict[str, tuple[tuple[str, str | None, str, bool], ...]] = {}
    records = instance.retained_record_reader()
    with instance.accepted_history_reader(
        at=AcceptedCoordinate.from_internal(coordinate)
    ) as history:
        stamp = floor_stamp(instance, coordinate, history)
        changed = _changed_since(history, previous, stamp.generation)
        verdicts = claim_verdicts(
            instance,
            coordinate,
            live,
            evaluation_time=stamp.accepted_at,
            read_context=verdict_context,
        )
        for path in subject_paths:
            ids = tuple(sorted(claim.identity.name for claim in grouped.get(path, ())))
            claim_ids[path] = ids
            verdict_keys[path] = tuple(
                (item, known.verdict, known.status, known.held)
                for item in ids
                if (known := verdicts.get(item)) is not None
            )
        reusable = (
            set()
            if changed is None or previous is None
            else {
                path
                for path in subject_paths
                if path in previous.parts
                and path not in changed
                and previous.claim_ids.get(path) == claim_ids[path]
                and previous.verdicts.get(path) == verdict_keys[path]
                and not any(claim_path(item) in changed for item in claim_ids[path])
            }
        )
        fresh = tuple(path for path in subject_paths if path not in reusable)
        shells = accepted_subjects(instance, coordinate, fresh)
        for path in subject_paths:
            if path in reusable:
                assert previous is not None
                parts[path] = previous.parts[path]
                continue
            parts[path] = render_subject(
                path=path,
                shell=shells[path],
                claims=grouped.get(path, ()),
                claim_types=claim_types,
                accepted_predicates=live_predicates,
                verdicts=verdicts,
                values=values,
                history=history,
            )
        wanted = sorted({sequence for part in parts.values() for sequence in part.sequences})
        same_notes = previous is not None and previous.notes_oid == notes_oid
        changes: dict[int, bytes] = {}
        missing = [
            sequence
            for sequence in wanted
            if not (same_notes and previous is not None and sequence in previous.changes)
        ]
        if missing or not same_notes:
            context, context_status = review_context(instance, notes_oid)
        else:
            assert previous is not None
            context, context_status = {}, previous.context_status
        for sequence in wanted:
            if sequence not in missing:
                assert previous is not None
                changes[sequence] = previous.changes[sequence]
                continue
            record = history.read_generation_record(sequence, records)
            changes[sequence] = _change_file(
                sequence,
                history.generation(sequence),
                record,
                context=context,
                context_status=context_status,
            )
    documents = (
        previous.documents
        if changed is not None
        and previous is not None
        and not any(path.startswith("documents/") for path in changed)
        else document_parts(instance, coordinate=coordinate, access=body_access)
    )
    memo_put(
        instance.floor_current_memo,
        key,
        _CurrentState(
            sequence=stamp.generation,
            git_oid=coordinate.git_oid,
            parts=parts,
            claim_ids=claim_ids,
            verdicts=verdict_keys,
            documents=documents,
            notes_oid=notes_oid,
            context_status=context_status,
            changes=changes,
            incremental=changed is not None,
            rendered=fresh,
        ),
        capacity=2,
    )
    files: dict[str, bytes] = {}
    for part in parts.values():
        files.update(stamped(part, stamp))
        files[f"{PROVENANCE_SUBJECTS_PREFIX}{part.ref}.json"] = part.provenance
    files.update(index_files(parts.values(), stamp))
    for document in documents:
        files.update(document_files(document, stamp))
    for sequence, content in changes.items():
        files[f"provenance/changes/{sequence:020d}.json"] = content
    files["provenance/snapshot.json"] = _render(
        {
            "accepted_git_oid": coordinate.git_oid,
            "accepted_generation": stamp.generation,
            "evaluation_notes_oid": notes_oid,
            "status": context_status,
            "rebuild_inputs": "accepted ledger plus this immutable Git notes snapshot",
            "history": "Only changes introducing the current Claim revisions are exported.",
        }
    )
    files["README.md"] = FLOOR_README.encode()
    return files

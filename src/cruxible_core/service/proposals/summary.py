"""Disposable summary projection; cold builds stream only this candidate's members.

A cold build costs O(changed members), never O(instance), and decodes at most
BATCH_SIZE members' trees at once. It does not construct ReviewedMembers, semantic
deltas, documents, or body diffs. Immutable candidate/admission pairs key builds;
normal reads use indexed group/row keysets and bounded JSON values. Deleting this
projection is an explicit cold repair, with no effect on proposal authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from functools import partial
from typing import Any

from cruxible_client.contracts.candidates import CandidateRecord
from cruxible_client.contracts.capture_reads import BodyRestricted
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claims import ClaimArtifact, parse_claim
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.proposal_summary import (
    REVIEW_CHANGE_COUNTS_MAX,
    REVIEW_CHANGES_PAGE,
    REVIEW_DEFINITIONS_PAGE,
    ProposalReviewSummary,
    ReviewChangeCount,
    ReviewDefinition,
    ReviewSummaryCounts,
    ReviewSummaryGroup,
    ReviewSummaryRow,
    ReviewSummaryValue,
)
from cruxible_client.contracts.refusal_receipts import PriorRefusalReceipt
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.indexes.proposal_review import initialize_review_projection
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.backlinks import (
    claim_type_identity,
    review_guidance,
)
from cruxible_core.service.proposals.review_view import (
    CitedText,
    claim_change_row,
    claim_type_cardinality,
    definition_row,
    mandate_carry_note,
    review_change,
    subject_change_row,
    subject_revision_row,
)

BATCH_SIZE = 32
MAX_GROUPS = 10
MAX_ROWS = 10
MAX_FINDINGS = 20
MAX_ROW_FINDINGS = 2
MAX_SUBJECT_KINDS = 20
MAX_ITEM_REASONS = 5
MAX_VALUE_BYTES = 1024
_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_builds (
 candidate TEXT PRIMARY KEY, counts TEXT NOT NULL, group_count INTEGER NOT NULL,
 finding_count INTEGER NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_groups (
 candidate TEXT NOT NULL, id TEXT NOT NULL, priority INTEGER NOT NULL,
 data TEXT NOT NULL, PRIMARY KEY(candidate,id)
);
CREATE INDEX IF NOT EXISTS review_group_page ON review_groups(candidate,priority,id);
CREATE TABLE IF NOT EXISTS review_rows (
 candidate TEXT NOT NULL, group_id TEXT NOT NULL, problem INTEGER NOT NULL,
 path TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(candidate,group_id,path)
);
CREATE INDEX IF NOT EXISTS review_row_page ON review_rows(candidate,group_id,problem,path);
CREATE TABLE IF NOT EXISTS review_kinds (
 candidate TEXT NOT NULL, kind TEXT NOT NULL, counts TEXT NOT NULL,
 PRIMARY KEY(candidate,kind)
);
CREATE TABLE IF NOT EXISTS review_findings (
 candidate TEXT NOT NULL, position INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(candidate,position)
);
CREATE TABLE IF NOT EXISTS review_changes (
 candidate TEXT NOT NULL, position INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(candidate,position)
);
CREATE TABLE IF NOT EXISTS review_change_counts (
 candidate TEXT NOT NULL, subject_kind TEXT NOT NULL, field TEXT NOT NULL, op TEXT NOT NULL,
 count INTEGER NOT NULL, PRIMARY KEY(candidate,subject_kind,field,op)
);
CREATE TABLE IF NOT EXISTS review_definitions (
 candidate TEXT NOT NULL, position INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(candidate,position)
);
"""


def _json(value: Any) -> str:
    return canonical_json(value)


def _value(value: Any) -> ReviewSummaryValue:
    text = _json(value)
    if len(text.encode("utf-8")) <= MAX_VALUE_BYTES:
        return ReviewSummaryValue(value=value)
    return ReviewSummaryValue(
        value=text.encode("utf-8")[:512].decode("utf-8", "ignore"), truncated=True
    )


def _cursor(candidate: str, lane: str, key: Any) -> str:
    return base64.urlsafe_b64encode(_json([candidate, lane, key]).encode()).decode()


def _after(cursor: str | None, candidate: str, lane: str, default: Any) -> Any:
    if cursor is None:
        return default
    if len(cursor) > 2048:
        raise ProposalIntegrityError(
            "Invalid review cursor; use the cursor from this candidate's page"
        )
    try:
        decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(decoded, list) or len(decoded) != 3 or decoded[:2] != [candidate, lane]:
            raise ValueError
        return decoded[2]
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ProposalIntegrityError(
            "Invalid review cursor; use the cursor from this candidate's page"
        ) from exc


def _fields(artifact: dict[str, Any] | None, artifact_kind: str) -> dict[str, Any] | None:
    if artifact is None:
        return None
    if artifact_kind == "claim":
        return {
            **artifact["statement"],
            "state": artifact.get("lifecycle", {}).get("state", "live"),
            "retirement": artifact.get("retirement"),
        }
    # These are the actual fields of the already evaluated artifact. A fixed
    # whitelist would hide changes to pins, Line bindings and new definitions.
    return artifact.copy()


def _subject_kind(artifact: dict[str, Any], artifact_kind: str) -> str:
    if artifact_kind == "claim":
        path = artifact["statement"]["subject"]["artifact_path"]
        if path.startswith("subjects/"):
            return str(path.split("/", 2)[1])
        if path.startswith("claim-types/"):
            return "semantic.claim_type"
        return str(path.split("/", 1)[0])
    return str(artifact.get("subject_kind", artifact_kind))


def _settle_demotion(
    instance: PlaybillInstance, *, path: str, base_oid: str, tree_oid: str, proposal_id: str
) -> str | None:
    """Why a Mandate carried in this proposal demoted to propose (the law's own question).

    Read only for a settle grant that demoted: the base and proposed trees, the
    moved Line and its builds, and the reverse pins of the grant's ClaimTypes.
    """

    from cruxible_client.contracts.procedure_mandates import parse_procedure_mandate_any
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, parse_line_spec
    from cruxible_core.compiler.compiler import admits_revision_74
    from cruxible_core.providers.observation_carry import (
        LazyNeighbors,
        observation_carry_refusal,
    )

    if not admits_revision_74(instance.coordinate_for_oid(base_oid).compiler):
        return "compiler_revision"
    before = instance.immutable_tree_at(base_oid)
    after = instance.proposal_tree(tree_oid, proposal_id=proposal_id)
    mandate = parse_procedure_mandate_any(before[path], path=path)
    line_path = line_spec_path(mandate.line.target.name)
    if line_path not in before or line_path not in after:
        return "line_changed"
    old_line = parse_line_spec(before[line_path], path=line_path)
    new_line = parse_line_spec(after[line_path], path=line_path)
    if old_line.procedure != new_line.procedure:
        return "procedure_changed"
    neighbors = LazyNeighbors(before)
    try:
        return observation_carry_refusal(
            old_mandate=mandate,
            old_line=old_line,
            new_line=new_line,
            before=before,
            after=after,
            neighbors=neighbors,
        )
    finally:
        neighbors.close()


def _columns(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    return {
        f"{side}.{key}": value
        for side, fields in (("before", before), ("after", after))
        for key, value in (fields or {}).items()
    }


def _build(
    connection: sqlite3.Connection,
    instance: PlaybillInstance,
    candidate: CandidateRecord,
    base_oid: str,
    tree_oid: str,
    proposal_id: str,
    *,
    refusal_lookup: Callable[[ClaimArtifact], tuple[tuple[PriorRefusalReceipt, ...], bool]]
    | None = None,
    metadata: dict[str, Any] | None = None,
    cache_key: str | None = None,
) -> None:
    digest = cache_key or candidate.candidate_digest
    groups: dict[str, dict[str, Any]] = {}
    kinds: dict[str, dict[str, int]] = {}
    counts = dict(created=0, revised=0, retired=0)
    finding_count = 0
    prior_refusal_member_count = 0
    # C14: every Capture a candidate Claim cites, so a page can withhold what
    # was extracted from a restricted kind without rereading any member.
    cited: set[str] = set()
    definitions: set[str] = set()
    evidence = {item.path: item for item in candidate.law_evidence}
    # The reviewer's view, from the same members: value changes and definitions.
    change_rows: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    change_counts: dict[tuple[str, str, str], int] = {}
    definition_rows: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    cardinality = claim_type_cardinality(
        lambda path: instance.proposal_blobs(tree_oid, (path,), proposal_id=proposal_id).get(path)
    )
    # Only immutable changed paths drive this explicit cold rebuild. No population scan.
    for start in range(0, len(candidate.members), BATCH_SIZE):
        members = candidate.members[start : start + BATCH_SIZE]
        paths = tuple(member.path for member in members)
        base = instance.blobs_at(base_oid, paths)
        proposed = instance.proposal_blobs(tree_oid, paths, proposal_id=proposal_id)
        for member in members:
            old = json.loads(base[member.path]) if member.path in base else None
            new = json.loads(proposed[member.path]) if member.path in proposed else None
            artifact = new or old
            if artifact is None:
                raise ProposalIntegrityError("Review member is missing from both exact trees")
            if member.artifact_kind == "claim" and new is not None:
                cited.update(new.get("backing", {}).get("capture_digests", ()))
            if member.artifact_kind == "note":
                # A note shows its section's file and heading path (both versions
                # of a revision): the section Capture decides who may see them (C14).
                cited.update(
                    str(side["capture_digest"])
                    for side in (old, new)
                    if side is not None and side.get("capture_digest")
                )
            kind = _subject_kind(artifact, member.artifact_kind)
            definition = claim_type_identity(artifact, member.artifact_kind)
            if definition is not None:
                # Every definition touched: which of them have guidance is asked
                # at read time, so none is dropped here by member order.
                definitions.add(definition)
            receipt = artifact.get("backing", {}).get("procedure_evidence", {})
            template = (
                receipt.get("procedure_digest"),
                receipt.get("node_id"),
                receipt.get("template"),
            )
            identity = (
                kind,
                member.artifact_kind,
                member.disposition,
                member.closure_role,
                *template,
            )
            group_id = hashlib.sha256(_json(identity).encode()).hexdigest()
            action = {
                "create": "created",
                "replace": "revised",
                "retire": "retired",
                "delete": "retired",
            }[member.disposition]
            counts[action] += 1
            kinds.setdefault(kind, dict(created=0, revised=0, retired=0))[action] += 1
            before, after = _fields(old, member.artifact_kind), _fields(new, member.artifact_kind)
            if before is not None and after is not None:
                changed = {
                    key
                    for key in before.keys() | after.keys()
                    if key not in before
                    or key not in after
                    or _json(before[key]) != _json(after[key])
                }
                before = {key: value for key, value in before.items() if key in changed}
                after = {key: value for key, value in after.items() if key in changed}
            if before is not None:
                before["artifact_digest"] = member.predecessor_artifact_digest
            if after is not None:
                after["artifact_digest"] = member.candidate_artifact_digest
            columns = _columns(before, after)
            diagnostics = evidence[member.path].diagnostics
            if member.artifact_kind == "claim":
                verdict = evidence[member.path].result.get("claim_evidence")
                row_key, row_kind, row_field, op, row = claim_change_row(
                    path=member.path,
                    disposition=member.disposition,
                    old=old,
                    new=new,
                    verdict=(verdict.get("initial_verdict") if isinstance(verdict, dict) else None),
                    cardinality=cardinality,
                    base_oid=base_oid,
                )
            elif member.artifact_kind == "subject" and member.disposition != "replace":
                row_key, row_kind, row_field, op, row = subject_change_row(
                    path=member.path, disposition=member.disposition
                )
            else:
                row = None
                definition_rows.append(
                    subject_revision_row(member.path)
                    if member.artifact_kind == "subject"
                    else definition_row(
                        path=member.path,
                        artifact_kind=member.artifact_kind,
                        disposition=member.disposition,
                        old=old,
                        new=new,
                        before=before,
                        after=after,
                        carry=(
                            mandate_carry_note(
                                old,
                                new,
                                partial(
                                    _settle_demotion,
                                    instance,
                                    path=member.path,
                                    base_oid=base_oid,
                                    tree_oid=tree_oid,
                                    proposal_id=proposal_id,
                                ),
                            )
                            if member.artifact_kind == "procedure-mandate"
                            and old is not None
                            and new is not None
                            else None
                        ),
                    )
                )
            if row is not None:
                change_rows.append((row_key, row))
                count_key = (row_kind, row_field or "", op)
                change_counts[count_key] = change_counts.get(count_key, 0) + 1
            refusals: tuple[PriorRefusalReceipt, ...] = ()
            refusals_more = False
            if (
                refusal_lookup is not None
                and member.artifact_kind == "claim"
                and member.closure_role == "authored"
                and member.disposition in {"create", "replace"}
                and new is not None
                and new.get("lifecycle", {}).get("state", "live") == "live"
            ):
                refusals, refusals_more = refusal_lookup(
                    parse_claim(proposed[member.path], path=member.path)
                )
            problem = bool(diagnostics or refusals or refusals_more)
            prior_refusal_member_count += bool(refusals or refusals_more)
            group = groups.get(group_id)
            if group is None:
                group = groups[group_id] = dict(
                    subject_kind=kind,
                    artifact_kind=member.artifact_kind,
                    disposition=member.disposition,
                    closure_role=member.closure_role,
                    procedure_digest=template[0],
                    node=template[1],
                    template=template[2],
                    counts=dict(created=0, revised=0, retired=0),
                    shared=columns.copy(),
                    varying=set(),
                    priority=0 if action in {"revised", "retired"} else 1,
                )
            group["counts"][action] += 1
            for key in set(group["shared"]) | set(columns):
                if (
                    key not in group["shared"]
                    or key not in columns
                    or _json(group["shared"][key]) != _json(columns[key])
                ):
                    group["shared"].pop(key, None)
                    group["varying"].add(key)
            if problem:
                group["priority"] = -1
            findings = [
                _value(d.model_dump(mode="json")).model_dump(mode="json")
                for d in diagnostics[:MAX_ROW_FINDINGS]
            ]
            for diagnostic in diagnostics:
                connection.execute(
                    "INSERT INTO review_findings VALUES (?,?,?)",
                    (
                        digest,
                        finding_count,
                        _json(_value(diagnostic.model_dump(mode="json")).model_dump(mode="json")),
                    ),
                )
                finding_count += 1
            connection.execute(
                "INSERT INTO review_rows VALUES (?,?,?,?,?)",
                (
                    digest,
                    group_id,
                    0 if problem else 1,
                    member.path,
                    _json(
                        dict(
                            path=member.path,
                            before=None
                            if before is None
                            else {k: _value(v).model_dump(mode="json") for k, v in before.items()},
                            after=None
                            if after is None
                            else {k: _value(v).model_dump(mode="json") for k, v in after.items()},
                            findings=findings,
                            finding_count=len(diagnostics),
                            prior_refusals=[
                                _value(r.model_dump(mode="json")).model_dump(mode="json")
                                for r in refusals
                            ],
                            prior_refusals_more=refusals_more,
                        )
                    ),
                ),
            )
    for group_id, group in groups.items():
        priority = group.pop("priority")
        group["shared_fields"] = {
            key: _value(value).model_dump(mode="json") for key, value in group.pop("shared").items()
        }
        group["varying_columns"] = sorted(group.pop("varying"))
        connection.execute(
            "INSERT INTO review_groups VALUES (?,?,?,?)", (digest, group_id, priority, _json(group))
        )
    connection.executemany(
        "INSERT INTO review_kinds VALUES (?,?,?)",
        [(digest, kind, _json(value)) for kind, value in kinds.items()],
    )
    change_rows.sort(key=lambda item: item[0])
    connection.executemany(
        "INSERT INTO review_changes VALUES (?,?,?)",
        [(digest, position, _json(row)) for position, (_, row) in enumerate(change_rows)],
    )
    connection.executemany(
        "INSERT INTO review_change_counts VALUES (?,?,?,?,?)",
        [(digest, *key, count) for key, count in change_counts.items()],
    )
    definition_rows.sort(key=lambda item: item[0])
    connection.executemany(
        "INSERT INTO review_definitions VALUES (?,?,?)",
        [(digest, position, _json(row)) for position, (_, row) in enumerate(definition_rows)],
    )
    connection.execute(
        "INSERT INTO review_builds VALUES (?,?,?,?,?)",
        (
            digest,
            _json(counts),
            len(groups),
            finding_count,
            _json(
                {
                    **(metadata or {}),
                    "subject_kind_count": len(kinds),
                    "prior_refusal_member_count": prior_refusal_member_count,
                    "cited_captures": sorted(cited),
                    "guided_definitions": sorted(definitions),
                    "change_count": len(change_rows),
                    "definition_count": len(definition_rows),
                }
            ),
        ),
    )


def _rows(
    connection: sqlite3.Connection,
    digest: str,
    group: str,
    shared: dict[str, Any],
    after: str | None,
    limit: int,
) -> tuple[tuple[ReviewSummaryRow, ...], str | None]:
    key = _after(after, digest, group, [-1, ""])
    if (
        not isinstance(key, list)
        or len(key) != 2
        or not isinstance(key[0], int)
        or not isinstance(key[1], str)
    ):
        raise ProposalIntegrityError("Invalid row cursor; use this group's next_cursor")
    rows = connection.execute(
        "SELECT problem,path,data FROM review_rows WHERE candidate=? AND group_id=? "
        "AND (problem,path)>(?,?) ORDER BY problem,path LIMIT ?",
        (digest, group, *key, limit + 1),
    ).fetchall()
    result = []
    for _, _, data in rows[:limit]:
        row = json.loads(data)
        for side in ("before", "after"):
            if row[side] is not None:
                row[side] = {
                    name: value
                    for name, value in row[side].items()
                    if f"{side}.{name}" not in shared
                }
        result.append(ReviewSummaryRow.model_validate(row))
    return tuple(result), (
        _cursor(digest, group, list(rows[limit - 1][:2])) if len(rows) > limit else None
    )


def service_review_summary(
    instance: PlaybillInstance,
    *,
    proposal_id: str,
    after: str | None = None,
    group: str | None = None,
    rows_after: str | None = None,
    limit: int = 5,
    groups_limit: int = 5,
    findings_after: str | None = None,
    access: BodyAccessContext | None = None,
    changes_from: int = 0,
    definitions_from: int = 0,
    groups: bool = True,
) -> ProposalReviewSummary:
    """Read bounded groups/rows; ``access`` decides restricted-derived values (C14).

    The reviewer's view pages by position: ``changes_from`` and
    ``definitions_from`` (the previous page's ``changes_next`` and
    ``definitions_next``). ``groups=False`` leaves out the grouped rows; any
    group or row cursor asks for them. Cold reconstruction is atomic and
    candidate-local; a page reads its own rows and the text its shown changes
    cite (one body read per cited Capture body), never the instance."""
    evidence = instance.proposal_evidence()
    admission = evidence.read_admission(proposal_id)
    evaluation = evidence.read_evaluation(admission.proposal_id)
    digest = evaluation.candidate_digest
    if digest is None or evaluation.evaluated_tree_oid is None:
        raise ProposalIntegrityError("Refused proposal has no reviewable candidate")
    # Receipt cutoffs and item reasons belong to an admission, even when two
    # admissions carry the same candidate. Their caches and cursors must differ.
    # A build from before C14 records no cited Captures: its candidate values
    # are withheld from a reader without the key (fail closed).
    cache_key = f"{digest};{admission.proposal_id}"
    limit = max(1, min(limit, MAX_ROWS))
    groups_limit = max(1, min(groups_limit, MAX_GROUPS))
    if group is not None and after is not None:
        raise ProposalIntegrityError("Choose group paging or row paging, using the returned cursor")
    if rows_after is not None and group is None:
        raise ProposalIntegrityError("Row paging requires its summary group")
    path = instance.proposal_review_projection_path()
    with closing(sqlite3.connect(path, timeout=60)) as connection:
        initialize_review_projection(connection, _SCHEMA)
        built = connection.execute(
            "SELECT counts,group_count,finding_count,metadata FROM review_builds WHERE candidate=?",
            (cache_key,),
        ).fetchone()
        if built is None:
            with connection:
                # Serialize only cold builders; warm pages take no write lock.
                connection.execute("BEGIN IMMEDIATE")
                built = connection.execute(
                    "SELECT counts,group_count,finding_count,metadata FROM review_builds "
                    "WHERE candidate=?",
                    (cache_key,),
                ).fetchone()
                if built is None:
                    candidate = evidence.read_candidate(digest)
                    operation = evidence.item_reason_operation(admission)
                    reasons, reason_count, _ = (
                        ((), 0, None)
                        if operation is None
                        else evidence.item_reason_page(
                            operation, after=None, limit=MAX_ITEM_REASONS
                        )
                    )
                    with instance.refusal_index().review_receipts(
                        instance, admission.proposal_id
                    ) as (lookup, coverage):
                        _build(
                            connection,
                            instance,
                            candidate,
                            evaluation.evaluated_base_oid,
                            evaluation.evaluated_tree_oid,
                            admission.proposal_id,
                            cache_key=cache_key,
                            refusal_lookup=lookup,
                            metadata=dict(
                                required_tier=candidate.required_tier,
                                activation_policy=candidate.activation_policy,
                                approval_requirements=[
                                    r.model_dump(mode="json")
                                    for r in candidate.approval_requirements
                                ],
                                item_reasons=[
                                    _value({"path": r.path, "reason": r.reason}).model_dump(
                                        mode="json"
                                    )
                                    for r in reasons
                                ],
                                item_reason_count=reason_count,
                                prior_refusal_coverage=None
                                if coverage is None
                                else coverage.model_dump(mode="json"),
                            ),
                        )
                    built = connection.execute(
                        "SELECT counts,group_count,finding_count,metadata FROM review_builds "
                        "WHERE candidate=?",
                        (cache_key,),
                    ).fetchone()
        assert built is not None
        if group is None:
            key = _after(after, cache_key, "groups", [-2, ""])
            if (
                not isinstance(key, list)
                or len(key) != 2
                or not isinstance(key[0], int)
                or not isinstance(key[1], str)
            ):
                raise ProposalIntegrityError("Invalid group cursor; use this summary's next_cursor")
            records = connection.execute(
                "SELECT priority,id,data FROM review_groups WHERE candidate=? "
                "AND (priority,id)>(?,?) ORDER BY priority,id LIMIT ?",
                (cache_key, *key, groups_limit + 1),
            ).fetchall()
        else:
            records = connection.execute(
                "SELECT priority,id,data FROM review_groups WHERE candidate=? AND id=?",
                (cache_key, group),
            ).fetchall()
            if not records:
                raise ProposalIntegrityError(
                    "Unknown summary group; use a group from this candidate"
                )
        groups = groups or after is not None or group is not None
        result = []
        for _, group_id, raw in records[:groups_limit] if groups else ():
            data = json.loads(raw)
            rows, cursor = _rows(
                connection, cache_key, group_id, data["shared_fields"], rows_after, limit
            )
            result.append(ReviewSummaryGroup(group=group_id, rows=rows, next_cursor=cursor, **data))
        kinds = {
            kind: ReviewSummaryCounts.model_validate_json(counts)
            for kind, counts in connection.execute(
                "SELECT kind,counts FROM review_kinds WHERE candidate=? ORDER BY kind LIMIT ?",
                (cache_key, MAX_SUBJECT_KINDS),
            )
        }
        metadata = json.loads(built[3])
        cited_captures = metadata.pop("cited_captures", None)
        change_count = int(metadata.pop("change_count", 0))
        definition_count = int(metadata.pop("definition_count", 0))
        if changes_from < 0 or definitions_from < 0:
            raise ProposalIntegrityError(
                "changes_from and definitions_from are positions: 0 or a page's *_next"
            )
        change_records = connection.execute(
            "SELECT position,data FROM review_changes WHERE candidate=? AND position>=? "
            "ORDER BY position LIMIT ?",
            (cache_key, changes_from, REVIEW_CHANGES_PAGE + 1),
        ).fetchall()
        definition_records = connection.execute(
            "SELECT position,data FROM review_definitions WHERE candidate=? AND position>=? "
            "ORDER BY position LIMIT ?",
            (cache_key, definitions_from, REVIEW_DEFINITIONS_PAGE + 1),
        ).fetchall()
        count_records = connection.execute(
            "SELECT subject_kind,field,op,count FROM review_change_counts WHERE candidate=? "
            "ORDER BY subject_kind,field,op LIMIT ?",
            (cache_key, REVIEW_CHANGE_COUNTS_MAX + 1),
        ).fetchall()
        guidance = review_guidance(
            instance,
            metadata.pop("guided_definitions", ()),
            line=None
            if admission.originating_line is None
            else admission.originating_line.identity.qualified,
        )
        findings_key = _after(findings_after, cache_key, "findings", -1)
        if not isinstance(findings_key, int):
            raise ProposalIntegrityError("Invalid findings cursor; use findings_next_cursor")
        findings = connection.execute(
            "SELECT position,data FROM review_findings WHERE candidate=? "
            "AND position>? ORDER BY position LIMIT ?",
            (cache_key, findings_key, MAX_FINDINGS + 1),
        ).fetchall()
        summary = ProposalReviewSummary(
            proposal_id=admission.proposal_id,
            candidate_digest=digest,
            settlement_base=AcceptedCoordinate.from_internal(
                instance.coordinate_for_oid(evaluation.evaluated_base_oid)
            ),
            originating_line=admission.originating_line,
            rationale=None if admission.rationale is None else _value(admission.rationale),
            counts=ReviewSummaryCounts.model_validate_json(built[0]),
            subject_kinds=kinds,
            subject_kinds_truncated=metadata["subject_kind_count"] > len(kinds),
            **metadata,
            findings=tuple(
                ReviewSummaryValue.model_validate_json(row[1]) for row in findings[:MAX_FINDINGS]
            ),
            finding_count=built[2],
            findings_next_cursor=(
                _cursor(cache_key, "findings", findings[MAX_FINDINGS - 1][0])
                if len(findings) > MAX_FINDINGS
                else None
            ),
            guidance=guidance,
            groups=tuple(result),
            group_count=built[1],
            next_cursor=(
                _cursor(cache_key, "groups", list(records[groups_limit - 1][:2]))
                if groups and len(records) > groups_limit
                else None
            ),
            change_count=change_count,
            changes_next=(
                change_records[REVIEW_CHANGES_PAGE][0]
                if len(change_records) > REVIEW_CHANGES_PAGE
                else None
            ),
            change_counts=tuple(
                ReviewChangeCount(subject_kind=kind, field=field or None, op=op, count=count)
                for kind, field, op, count in count_records[:REVIEW_CHANGE_COUNTS_MAX]
            ),
            change_counts_truncated=len(count_records) > REVIEW_CHANGE_COUNTS_MAX,
            definitions=tuple(
                ReviewDefinition.model_validate_json(data)
                for _, data in definition_records[:REVIEW_DEFINITIONS_PAGE]
            ),
            definition_count=definition_count,
            definitions_next=(
                definition_records[REVIEW_DEFINITIONS_PAGE][0]
                if len(definition_records) > REVIEW_DEFINITIONS_PAGE
                else None
            ),
        )
    summary = withhold_summary_values(instance, summary, cited_captures, access)
    return _with_changes(
        instance,
        summary,
        admission,
        [json.loads(data) for _, data in change_records[:REVIEW_CHANGES_PAGE]],
        access,
    )


def _with_changes(
    instance: PlaybillInstance,
    summary: ProposalReviewSummary,
    admission: Any,
    rows: list[dict[str, Any]],
    access: BodyAccessContext | None,
) -> ProposalReviewSummary:
    """The page's value changes, each with its citations and item reason.

    A withheld summary (C14) keeps each change's op, Subject, field, Claim and
    before value; the proposed value, verdict, cited text and reason go.
    Cited text needs body read (``access.can_read_body``).
    """

    if not rows:
        return summary
    withheld = summary.withheld is not None
    evidence = instance.proposal_evidence()
    operation = None if withheld else evidence.item_reason_operation(admission)

    def reason(path: str) -> str | None:
        if operation is None:
            return None
        record = evidence.read_item_reason(operation, path)
        return None if record is None else record.reason

    cited = CitedText(
        instance,
        access or BodyAccessContext(principal_id="anonymous"),
        withheld=withheld,
    )
    return summary.model_copy(
        update={
            "changes": tuple(
                review_change(row, cited=cited, reason=reason, withheld=withheld) for row in rows
            )
        }
    )


def withhold_summary_values(
    instance: PlaybillInstance,
    summary: ProposalReviewSummary,
    cited_captures: object,
    access: BodyAccessContext | None,
) -> ProposalReviewSummary:
    """A summary without candidate values extracted from restricted Captures (C14).

    When a candidate Claim cites a Capture withheld from the reader, every
    proposed (``after``) value, shared candidate field, proposed definition
    field, finding and item reason goes (a definition keeps its kind, name and
    action); accepted (``before``) values, counts, groups, digests and the
    candidate digest a signer signs stay. A build without its cited Captures
    withholds (fail closed).
    """

    from cruxible_core.service.evidence.body_access import (
        NO_RESTRICTED_ACCESS,
        capture_access_resolver,
    )

    reader = access or NO_RESTRICTED_ACCESS
    if reader.can_read_restricted:
        return summary
    if isinstance(cited_captures, list):
        _items, notice = capture_access_resolver(instance).withheld(
            [str(item) for item in cited_captures], reader
        )
    else:
        notice = BodyRestricted(provenance_unavailable=True)
    if notice is None:
        return summary
    # A note's accepted version names the same restricted section, so a note
    # group keeps no field on either side.
    groups = tuple(
        group.model_copy(
            update={
                "shared_fields": {
                    key: value
                    for key, value in group.shared_fields.items()
                    if not key.startswith("after.") and group.artifact_kind != "note"
                },
                "rows": tuple(
                    row.model_copy(
                        update={
                            "before": None
                            if row.before is None
                            else ({} if group.artifact_kind == "note" else row.before),
                            "after": None if row.after is None else {},
                            "findings": (),
                        }
                    )
                    for row in group.rows
                ),
            }
        )
        for group in summary.groups
    )
    # A proposed definition's fields can copy restricted text too (a Runbook
    # description): it keeps its kind, name and action, like a withheld change
    # keeps its op, Subject and field.
    definitions = tuple(
        item.model_copy(update={"detail": None, "changed": ()}) for item in summary.definitions
    )
    return summary.model_copy(
        update={
            "groups": groups,
            "definitions": definitions,
            "findings": (),
            "item_reasons": (),
            "withheld": notice,
        }
    )

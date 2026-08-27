"""Deterministic, visibility-filtered accepted ChangeSet history."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
)
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.errors import PlaybillError, PlaybillSinceRequestInvalid
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.settlement import (
    ChangeSetRecord,
    ChangeSetRecordV2,
    ChangeSetRecordV3,
)


class PlaybillSinceError(PlaybillError):
    code = "playbill.since.refused"


class PlaybillSinceGenerationUnknown(PlaybillSinceError):
    code = "playbill.since.generation_unknown"


class PlaybillSinceCursorCoordinateMismatch(PlaybillSinceError):
    code = "playbill.since.cursor_coordinate_mismatch"


class PlaybillSinceRowExceedsBudget(PlaybillSinceError):
    code = "playbill.since.row_exceeds_budget"


class PlaybillSinceAcceptedStateInvalid(PlaybillSinceError):
    code = "playbill.since.accepted_state_invalid"


def validate_playbill_since_request(
    value: contracts.PlaybillSinceRequest | Mapping[str, object],
) -> contracts.PlaybillSinceRequest:
    """Validate the frozen request and expose one typed refusal family."""

    if isinstance(value, contracts.PlaybillSinceRequest):
        return value
    try:
        return contracts.PlaybillSinceRequest.model_validate(value)
    except ValidationError as exc:
        raise PlaybillSinceRequestInvalid.from_validation_errors(
            exc.errors(include_url=False)
        ) from exc


def _digest(domain: str, values: Mapping[str, object]) -> str:
    return typed_digest(Sha256Value, domain, values).tagged


def _cursor(
    *,
    instance_id: str,
    lower_generation: int,
    head_coordinate: contracts.PlaybillAcceptedCoordinate,
    access_profile: dict[str, object],
    max_rows: int,
    max_bytes: int,
    last_generation: int,
    last_member_path: str,
) -> contracts.PlaybillSinceCursor:
    values: dict[str, object] = {
        "instance_id": instance_id,
        "lower_generation": lower_generation,
        "head_coordinate": head_coordinate.model_dump(mode="json"),
        "access_profile": access_profile,
        "max_rows": max_rows,
        "max_bytes": max_bytes,
        "last_generation": last_generation,
        "last_member_path": last_member_path,
    }
    return contracts.PlaybillSinceCursor.model_validate(
        {**values, "cursor_digest": _digest("playbill-since-cursor-v1", values)}
    )


def _result(
    *,
    coordinate: contracts.PlaybillAcceptedCoordinate,
    generation: int,
    rows: list[contracts.PlaybillSinceRow],
    next_cursor: contracts.PlaybillSinceCursor | None,
    truncated: bool,
) -> contracts.PlaybillSinceResult:
    values: dict[str, object] = {
        "coordinate": coordinate.model_dump(mode="json"),
        "generation": generation,
        "rows": [row.model_dump(mode="json") for row in rows],
        "next_cursor": None if next_cursor is None else next_cursor.model_dump(mode="json"),
        "truncated": truncated,
    }
    return contracts.PlaybillSinceResult.model_validate(
        {**values, "result_digest": _digest("playbill-since-result-v1", values)}
    )


def _normalized_rows(
    instance: PlaybillInstance,
    *,
    lower_generation: int,
    head_generation: int,
    access_profile: CoverageAccessProfileV1,
) -> tuple[contracts.PlaybillSinceRow, ...]:
    # Accepted artifacts are instance-scoped. Filtering happens before either
    # budget is applied, so this branch discloses no member metadata or count.
    if not access_profile.permits("instance"):
        return ()
    rows: list[contracts.PlaybillSinceRow] = []
    for generation in instance.accepted_history():
        if not lower_generation < generation.sequence <= head_generation:
            continue
        record = generation.record
        if not isinstance(record, ChangeSetRecord | ChangeSetRecordV2 | ChangeSetRecordV3):
            raise PlaybillSinceAcceptedStateInvalid(
                f"{PlaybillSinceAcceptedStateInvalid.code}: accepted generation has no ChangeSet"
            )
        for member in record.members:
            rows.append(
                _normalized_member_row(
                    generation=generation.sequence,
                    changeset_digest=record.changeset_digest,
                    candidate_digest=record.candidate_digest,
                    member=member,
                )
            )
    return tuple(sorted(rows, key=lambda row: (row.generation, row.member_path.encode("utf-8"))))


def _normalized_member_row(
    *,
    generation: int,
    changeset_digest: str,
    candidate_digest: str,
    member: CandidateMemberEvidence | CandidateMemberLawEvidenceV2,
) -> contracts.PlaybillSinceRow:
    artifact_digest: str | None
    predecessor_artifact_digest: str | None
    if isinstance(member, CandidateMemberEvidence):
        artifact_digest = member.artifact_digest
        predecessor_artifact_digest = None
    else:
        artifact_digest = member.candidate_artifact_digest
        predecessor_artifact_digest = member.predecessor_artifact_digest
    return contracts.PlaybillSinceRow(
        generation=generation,
        changeset_digest=changeset_digest,
        candidate_digest=candidate_digest,
        member_path=member.path,
        artifact_kind=member.artifact_kind,
        disposition=member.disposition,
        artifact_digest=artifact_digest,
        predecessor_artifact_digest=predecessor_artifact_digest,
    )


def service_playbill_since(
    instance: PlaybillInstance,
    *,
    request: contracts.PlaybillSinceRequest,
) -> contracts.PlaybillSinceResult:
    """Read signed ChangeSet members in ``(generation, pinned head]``."""

    profile = CoverageAccessProfileV1.model_validate(request.access_profile)
    cursor = request.cursor
    head: contracts.PlaybillAcceptedCoordinate
    if cursor is not None:
        supplied_at = request.at
        mismatch = (
            cursor.instance_id != instance.descriptor.instance_id
            or cursor.lower_generation != request.generation
            or cursor.access_profile != request.access_profile
            or cursor.max_rows != request.max_rows
            or cursor.max_bytes != request.max_bytes
            or (supplied_at is not None and cursor.head_coordinate != supplied_at)
        )
        if mismatch:
            raise PlaybillSinceCursorCoordinateMismatch(
                f"{PlaybillSinceCursorCoordinateMismatch.code}: cursor belongs to another request"
            )
        head = cursor.head_coordinate
    else:
        head = request.at or contracts.PlaybillAcceptedCoordinate.model_validate(
            PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(
                mode="json"
            )
        )
    try:
        accepted_head = instance.resolve_accepted_coordinate(
            git_oid=head.git_oid,
            semantic_root=head.semantic_root,
            generation_root=head.generation_root,
            compiler_digest=head.compiler_digest,
        )
    except PlaybillError as exc:
        raise PlaybillSinceGenerationUnknown(
            f"{PlaybillSinceGenerationUnknown.code}: head coordinate is not accepted"
        ) from exc
    history = instance.accepted_history()
    head_generation = next(item.sequence for item in history if item.oid == accepted_head.git_oid)
    if request.generation > head_generation or not any(
        item.sequence == request.generation for item in history
    ):
        raise PlaybillSinceGenerationUnknown(
            f"{PlaybillSinceGenerationUnknown.code}: lower generation is not accepted"
        )
    rows = _normalized_rows(
        instance,
        lower_generation=request.generation,
        head_generation=head_generation,
        access_profile=profile,
    )
    start = 0
    if cursor is not None:
        keys = tuple((row.generation, row.member_path) for row in rows)
        key = (cursor.last_generation, cursor.last_member_path)
        if key not in keys:
            raise PlaybillSinceCursorCoordinateMismatch(
                f"{PlaybillSinceCursorCoordinateMismatch.code}: cursor boundary is absent"
            )
        start = keys.index(key) + 1

    page: list[contracts.PlaybillSinceRow] = []
    for row in rows[start : start + request.max_rows]:
        candidate = [*page, row]
        more = start + len(candidate) < len(rows)
        next_cursor = (
            _cursor(
                instance_id=instance.descriptor.instance_id,
                lower_generation=request.generation,
                head_coordinate=head,
                access_profile=request.access_profile,
                max_rows=request.max_rows,
                max_bytes=request.max_bytes,
                last_generation=row.generation,
                last_member_path=row.member_path,
            )
            if more
            else None
        )
        result = _result(
            coordinate=head,
            generation=head_generation,
            rows=candidate,
            next_cursor=next_cursor,
            truncated=more,
        )
        if len(canonical_bytes(result.model_dump(mode="json"))) > request.max_bytes:
            if not page:
                raise PlaybillSinceRowExceedsBudget(
                    f"{PlaybillSinceRowExceedsBudget.code}: one visible row exceeds max_bytes"
                )
            break
        page = candidate

    more = start + len(page) < len(rows)
    next_cursor = (
        _cursor(
            instance_id=instance.descriptor.instance_id,
            lower_generation=request.generation,
            head_coordinate=head,
            access_profile=request.access_profile,
            max_rows=request.max_rows,
            max_bytes=request.max_bytes,
            last_generation=page[-1].generation,
            last_member_path=page[-1].member_path,
        )
        if more and page
        else None
    )
    if more and not page:
        raise PlaybillSinceRowExceedsBudget(
            f"{PlaybillSinceRowExceedsBudget.code}: one visible row exceeds max_bytes"
        )
    return _result(
        coordinate=head,
        generation=head_generation,
        rows=page,
        next_cursor=next_cursor,
        truncated=more,
    )


__all__ = [
    "PlaybillSinceAcceptedStateInvalid",
    "PlaybillSinceCursorCoordinateMismatch",
    "PlaybillSinceError",
    "PlaybillSinceGenerationUnknown",
    "PlaybillSinceRowExceedsBudget",
    "service_playbill_since",
    "validate_playbill_since_request",
]

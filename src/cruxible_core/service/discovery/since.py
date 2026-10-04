"""Deterministic, visibility-filtered accepted ChangeSet history."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidence,
)
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.errors import CruxibleError, SinceRequestInvalid
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.proposals.settlement import (
    ChangeSetRecord,
    ChangeSetRecordV2,
    ChangeSetRecordV3,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import AcceptedCoordinate


class SinceError(CruxibleError):
    code = "cruxible.since.refused"

    @property
    def error_code(self) -> str:
        return self.code


class SinceGenerationUnknown(SinceError):
    code = "cruxible.since.generation_unknown"


class SinceCursorCoordinateMismatch(SinceError):
    code = "cruxible.since.cursor_coordinate_mismatch"


class SinceRowExceedsBudget(SinceError):
    code = "cruxible.since.row_exceeds_budget"


class SinceAcceptedStateInvalid(SinceError):
    code = "cruxible.since.accepted_state_invalid"


def validate_playbill_since_request(
    value: contracts.SinceRequest | Mapping[str, object],
) -> contracts.SinceRequest:
    """Validate the frozen request and expose one typed refusal family."""

    if isinstance(value, contracts.SinceRequest):
        return value
    try:
        return contracts.SinceRequest.model_validate(value)
    except ValidationError as exc:
        raise SinceRequestInvalid.from_validation_errors(exc.errors(include_url=False)) from exc


def _digest(domain: str, values: Mapping[str, object]) -> str:
    return typed_digest(Sha256Value, domain, values).tagged


def _cursor(
    *,
    instance_id: str,
    lower_generation: int,
    head_coordinate: contracts.AcceptedCoordinate,
    access_profile: dict[str, object],
    max_rows: int,
    max_bytes: int,
    last_generation: int,
    last_member_path: str,
) -> contracts.SinceCursor:
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
    return contracts.SinceCursor.model_validate(
        {**values, "cursor_digest": _digest("playbill-since-cursor-v1", values)}
    )


def _result(
    *,
    coordinate: contracts.AcceptedCoordinate,
    generation: int,
    rows: list[contracts.SinceRow],
    next_cursor: contracts.SinceCursor | None,
    truncated: bool,
) -> contracts.SinceResult:
    values: dict[str, object] = {
        "coordinate": coordinate.model_dump(mode="json"),
        "generation": generation,
        "rows": [row.model_dump(mode="json") for row in rows],
        "next_cursor": None if next_cursor is None else next_cursor.model_dump(mode="json"),
        "truncated": truncated,
    }
    return contracts.SinceResult.model_validate(
        {**values, "result_digest": _digest("playbill-since-result-v1", values)}
    )


def _normalized_rows(
    instance: PlaybillInstance,
    *,
    lower_generation: int,
    head_generation: int,
    access_profile: CoverageAccessProfile,
) -> tuple[contracts.SinceRow, ...]:
    # Accepted artifacts are instance-scoped. Filtering happens before either
    # budget is applied, so this branch discloses no member metadata or count.
    if not access_profile.permits("instance"):
        return ()
    rows: list[contracts.SinceRow] = []
    for generation in instance.accepted_history():
        if not lower_generation < generation.sequence <= head_generation:
            continue
        record = generation.record
        if not isinstance(record, ChangeSetRecord | ChangeSetRecordV2 | ChangeSetRecordV3):
            raise SinceAcceptedStateInvalid(
                f"{SinceAcceptedStateInvalid.code}: accepted generation has no ChangeSet"
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
    member: CandidateMemberEvidence | CandidateMemberLawEvidence,
) -> contracts.SinceRow:
    artifact_digest: str | None
    predecessor_artifact_digest: str | None
    if isinstance(member, CandidateMemberEvidence):
        artifact_digest = member.artifact_digest
        predecessor_artifact_digest = None
    else:
        artifact_digest = member.candidate_artifact_digest
        predecessor_artifact_digest = member.predecessor_artifact_digest
    return contracts.SinceRow(
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
    request: contracts.SinceRequest,
) -> contracts.SinceResult:
    """Read signed ChangeSet members in ``(generation, pinned head]``."""

    profile = CoverageAccessProfile.model_validate(request.access_profile)
    cursor = request.cursor
    head: contracts.AcceptedCoordinate
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
            raise SinceCursorCoordinateMismatch(
                f"{SinceCursorCoordinateMismatch.code}: cursor belongs to another request"
            )
        head = cursor.head_coordinate
    else:
        head = request.at or contracts.AcceptedCoordinate.model_validate(
            AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
        )
    try:
        accepted_head = instance.resolve_accepted_coordinate(
            git_oid=head.git_oid,
            semantic_root=head.semantic_root,
            generation_root=head.generation_root,
            compiler_digest=head.compiler_digest,
        )
    except CruxibleError as exc:
        raise SinceGenerationUnknown(
            f"{SinceGenerationUnknown.code}: head coordinate is not accepted"
        ) from exc
    history = instance.accepted_history()
    head_generation = next(item.sequence for item in history if item.oid == accepted_head.git_oid)
    if request.generation > head_generation or not any(
        item.sequence == request.generation for item in history
    ):
        raise SinceGenerationUnknown(
            f"{SinceGenerationUnknown.code}: lower generation is not accepted"
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
            raise SinceCursorCoordinateMismatch(
                f"{SinceCursorCoordinateMismatch.code}: cursor boundary is absent"
            )
        start = keys.index(key) + 1

    page: list[contracts.SinceRow] = []
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
                raise SinceRowExceedsBudget(
                    f"{SinceRowExceedsBudget.code}: one visible row exceeds max_bytes"
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
        raise SinceRowExceedsBudget(
            f"{SinceRowExceedsBudget.code}: one visible row exceeds max_bytes"
        )
    return _result(
        coordinate=head,
        generation=head_generation,
        rows=page,
        next_cursor=next_cursor,
        truncated=more,
    )


__all__ = [
    "SinceAcceptedStateInvalid",
    "SinceCursorCoordinateMismatch",
    "SinceError",
    "SinceGenerationUnknown",
    "SinceRowExceedsBudget",
    "service_playbill_since",
    "validate_playbill_since_request",
]

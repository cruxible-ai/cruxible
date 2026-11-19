"""Deterministic, visibility-filtered accepted ChangeSet history."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from itertools import islice

from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.candidates import (
    CandidateMemberLawEvidence,
)
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.errors import CruxibleError, SinceRequestInvalid
from cruxible_client.contracts.read_values import ClaimLine
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.proposals.settlement import (
    ChangeSetRecord,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import AcceptedCoordinate
from cruxible_core.service.discovery.claim_lines import claim_line, live_predicates


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
    after: tuple[int, str | None],
    head_generation: int,
    access_profile: CoverageAccessProfile,
) -> Iterator[contracts.SinceRow]:
    """Every visible row after ``after`` through the head, in (generation, UTF-8 path) order.

    ``after`` is a generation and, within it, the member path a page stopped
    at (``None``: the whole generation is before the range). Rows are made one
    generation at a time as they are taken, so a page reads the generations it
    shows (and the next one's first row), never the whole accepted history.
    """

    # Accepted artifacts are instance-scoped. Filtering happens before either
    # budget is applied, so this branch discloses no member metadata or count.
    if not access_profile.permits("instance"):
        return
    history = instance.accepted_history()
    generation, after_path = after
    first = generation if after_path is not None else generation + 1
    for sequence in range(first, head_generation + 1):
        record = history[sequence].record
        if not isinstance(record, ChangeSetRecord):
            raise SinceAcceptedStateInvalid(
                f"{SinceAcceptedStateInvalid.code}: accepted generation has no ChangeSet"
            )
        rows = sorted(
            (
                _normalized_member_row(
                    generation=sequence,
                    changeset_digest=record.changeset_digest,
                    candidate_digest=record.candidate_digest,
                    member=member,
                )
                for member in record.members
            ),
            key=lambda row: row.member_path.encode("utf-8"),
        )
        if sequence == generation and after_path is not None:
            boundary = after_path.encode("utf-8")
            if not any(row.member_path == after_path for row in rows):
                raise SinceCursorCoordinateMismatch(
                    f"{SinceCursorCoordinateMismatch.code}: cursor boundary is absent"
                )
            rows = [row for row in rows if row.member_path.encode("utf-8") > boundary]
        yield from rows


def _normalized_member_row(
    *,
    generation: int,
    changeset_digest: str,
    candidate_digest: str,
    member: CandidateMemberLawEvidence,
) -> contracts.SinceRow:
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


def _with_statements(
    instance: PlaybillInstance,
    rows: list[contracts.SinceRow],
    *,
    head: AcceptedProjectionCoordinate,
) -> list[contracts.SinceRow]:
    """Each Claim row's statement, read from its own artifact at its generation.

    One batched blob read per generation the rows touch, so the cost is the
    rows taken (a page and one more), never the generations' whole membership.
    """

    claim_paths: dict[int, list[str]] = {}
    for row in rows:
        if row.artifact_kind == "claim" and row.artifact_digest is not None:
            claim_paths.setdefault(row.generation, []).append(row.member_path)
    if not claim_paths:
        return rows
    history = instance.accepted_history()
    lines: dict[tuple[int, str], ClaimLine] = {}
    with instance.bind_accepted_projection(head) as projection:
        accepted = live_predicates(projection)
    for generation, paths in claim_paths.items():
        blobs = instance.blobs_at(history[generation].oid, paths)
        for path, content in blobs.items():
            try:
                claim = parse_claim(content, path=path)
            except (CruxibleError, ValueError):
                continue
            lines[(generation, path)] = claim_line(claim, accepted_predicates=accepted)
    return [
        row
        if (line := lines.get((row.generation, row.member_path))) is None
        else row.model_copy(update={"statement": line})
        for row in rows
    ]


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
    # The head is the newest generation or near it: found from the end.
    head_generation = next(
        item.sequence
        for item in reversed(instance.accepted_history())
        if item.oid == accepted_head.git_oid
    )
    if not 0 <= request.generation <= head_generation:
        raise SinceGenerationUnknown(
            f"{SinceGenerationUnknown.code}: lower generation is not accepted"
        )
    after: tuple[int, str | None] = (request.generation, None)
    if cursor is not None:
        if not request.generation < cursor.last_generation <= head_generation:
            raise SinceCursorCoordinateMismatch(
                f"{SinceCursorCoordinateMismatch.code}: cursor boundary is absent"
            )
        after = (cursor.last_generation, cursor.last_member_path)
    # One page and one row more, which says whether another page follows.
    rows = _with_statements(
        instance,
        list(
            islice(
                _normalized_rows(
                    instance, after=after, head_generation=head_generation, access_profile=profile
                ),
                request.max_rows + 1,
            )
        ),
        head=accepted_head,
    )

    page: list[contracts.SinceRow] = []
    for row in rows[: request.max_rows]:
        candidate = [*page, row]
        more = len(candidate) < len(rows)
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

    more = len(page) < len(rows)
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

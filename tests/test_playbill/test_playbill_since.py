"""PC-G8c accepted-history orientation laws."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_since import (
    PlaybillSinceAcceptedStateInvalid,
    PlaybillSinceCursorCoordinateMismatch,
    PlaybillSinceGenerationUnknown,
    PlaybillSinceRowExceedsBudget,
    _cursor,
    _normalized_member_row,
    _normalized_rows,
    service_playbill_since,
)
from tests.test_playbill._adoption_fixture import _Builder
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_wire_succession_boundary import (
    V1,
    V2,
    V3,
    _members,
)

pytest_plugins = ("tests.test_playbill.test_wire_succession_boundary",)


def _profile(*classes: str) -> dict[str, object]:
    return {
        "tag": "playbill-coverage-access-profile-v1",
        "profile_id": "since-test",
        "permitted_access_classes": sorted(classes),
        "disclose_restricted_existence": False,
    }


def _request(**values: object) -> contracts.PlaybillSinceRequest:
    request_values = {
        "generation": 0,
        "access_profile": _profile("instance", "public"),
        **values,
    }
    return contracts.PlaybillSinceRequest(
        **request_values,
    )


def test_mixed_signed_v1_v2_v3_history_preserves_each_member_wire(
    crossed_ledger: tuple[object, _Builder],
) -> None:
    original, _builder = crossed_ledger
    assert isinstance(original, PlaybillInstance)
    instance = PlaybillInstance.open(original.root, trust_root=original.trust_root)
    current = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    result = service_playbill_since(current, request=_request())
    assert result.rows

    assert result.generation == 6
    assert [row.generation for row in result.rows] == sorted(row.generation for row in result.rows)
    first = next(row for row in result.rows if row.generation == 1)
    assert first.disposition == "replacement"
    assert first.artifact_digest is not None
    assert first.predecessor_artifact_digest is None
    assert {row.disposition for row in result.rows if row.generation >= 2} <= {
        "create",
        "replace",
        "retire",
        "delete",
    }
    assert result.result_digest.startswith("sha256:")


def test_genesis_equal_head_future_and_visibility_filtering(
    crossed_ledger: tuple[object, _Builder],
) -> None:
    original, _builder = crossed_ledger
    assert isinstance(original, PlaybillInstance)
    instance = PlaybillInstance.open(original.root, trust_root=original.trust_root)
    current = instance.accepted_history()[-1]
    head = contracts.PlaybillAcceptedCoordinate.model_validate(
        PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(
            mode="json"
        )
    )

    empty = service_playbill_since(
        instance,
        request=_request(generation=current.sequence, at=head),
    )
    assert empty.rows == []
    assert empty.truncated is False
    hidden = service_playbill_since(
        instance,
        request=contracts.PlaybillSinceRequest(
            generation=0,
            access_profile=_profile("public"),
        ),
    )
    assert hidden.rows == []
    with pytest.raises(PlaybillSinceGenerationUnknown):
        service_playbill_since(
            instance,
            request=_request(generation=current.sequence + 1),
        )


def test_row_and_byte_budgets_cursor_binding_and_head_pin(
    crossed_ledger: tuple[object, _Builder],
) -> None:
    original, _builder = crossed_ledger
    assert isinstance(original, PlaybillInstance)
    instance = PlaybillInstance.open(original.root, trust_root=original.trust_root)
    first = service_playbill_since(
        instance,
        request=_request(max_rows=2),
    )
    assert len(first.rows) == 2
    assert first.truncated and first.next_cursor is not None
    second = service_playbill_since(
        instance,
        request=_request(max_rows=2, cursor=first.next_cursor),
    )
    assert not {row.member_path for row in first.rows}.intersection(
        row.member_path for row in second.rows
    )
    assert second.coordinate == first.coordinate

    with pytest.raises(PlaybillSinceCursorCoordinateMismatch):
        service_playbill_since(
            instance,
            request=_request(max_rows=3, cursor=first.next_cursor),
        )
    wrong_instance = _cursor(
        instance_id="another-instance",
        lower_generation=0,
        head_coordinate=first.next_cursor.head_coordinate,
        access_profile=first.next_cursor.access_profile,
        max_rows=2,
        max_bytes=65_536,
        last_generation=first.next_cursor.last_generation,
        last_member_path=first.next_cursor.last_member_path,
    )
    with pytest.raises(PlaybillSinceCursorCoordinateMismatch):
        service_playbill_since(instance, request=_request(max_rows=2, cursor=wrong_instance))
    with pytest.raises(PlaybillSinceCursorCoordinateMismatch):
        service_playbill_since(
            instance,
            request=_request(generation=1, max_rows=2, cursor=first.next_cursor),
        )
    with pytest.raises(PlaybillSinceCursorCoordinateMismatch):
        service_playbill_since(
            instance,
            request=_request(
                access_profile={**_profile("instance", "public"), "profile_id": "other"},
                max_rows=2,
                cursor=first.next_cursor,
            ),
        )
    genesis = instance.accepted_history()[0]
    genesis_coordinate = contracts.PlaybillAcceptedCoordinate.model_validate(
        PlaybillAcceptedCoordinate.from_internal(
            instance.coordinate_for_oid(genesis.oid)
        ).model_dump(mode="json")
    )
    with pytest.raises(PlaybillSinceCursorCoordinateMismatch):
        service_playbill_since(
            instance,
            request=_request(
                at=genesis_coordinate,
                max_rows=2,
                cursor=first.next_cursor,
            ),
        )
    bad_boundary = _cursor(
        instance_id=first.next_cursor.instance_id,
        lower_generation=0,
        head_coordinate=first.next_cursor.head_coordinate,
        access_profile=first.next_cursor.access_profile,
        max_rows=2,
        max_bytes=65_536,
        last_generation=first.next_cursor.last_generation,
        last_member_path="documents/not-present.yaml",
    )
    with pytest.raises(PlaybillSinceCursorCoordinateMismatch):
        service_playbill_since(
            instance,
            request=_request(max_rows=2, cursor=bad_boundary),
        )
    with pytest.raises(PlaybillSinceRowExceedsBudget):
        service_playbill_since(
            instance,
            request=_request(max_bytes=1),
        )
    values = first.next_cursor.model_dump(mode="json")
    values["cursor_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValidationError, match="digest"):
        contracts.PlaybillSinceCursor.model_validate(values)


def test_current_v3_history_is_ordered_by_utf8_member_path(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    builder = _Builder(instance, owner)
    builder.accept(_members(builder, 1, V1), phase="since", wire_version=V1)
    builder.accept(_members(builder, 2, V2), phase="since", wire_version=V2)
    builder.accept(_members(builder, 3, V3), phase="since", wire_version=V3)

    result = service_playbill_since(instance, request=_request())
    keys = [(row.generation, row.member_path.encode("utf-8")) for row in result.rows]
    assert keys == sorted(keys)


def test_cursor_pins_head_across_advancement_without_duplicates_or_omissions(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    builder = _Builder(instance, owner)
    for index in range(1, 4):
        builder.accept(_members(builder, index, V3), phase="since", wire_version=V3)

    current = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    first = service_playbill_since(current, request=_request(max_rows=2))
    assert first.next_cursor is not None
    pinned_paths = {
        row.member_path for row in service_playbill_since(current, request=_request()).rows
    }
    builder.accept(_members(builder, 4, V3), phase="since", wire_version=V3)
    advanced = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    rows = list(first.rows)
    cursor = first.next_cursor
    while cursor is not None:
        page = service_playbill_since(
            advanced,
            request=_request(max_rows=2, cursor=cursor),
        )
        assert page.coordinate == first.coordinate
        rows.extend(page.rows)
        cursor = page.next_cursor
    assert len(rows) == len({(row.generation, row.member_path) for row in rows})
    assert {row.member_path for row in rows} == pinned_paths
    assert all(row.generation <= first.generation for row in rows)


@pytest.mark.parametrize(
    ("disposition", "candidate_is_none"),
    [
        ("create", False),
        ("replace", False),
        ("retire", False),
        ("delete", True),
    ],
)
def test_v2_v3_normalization_preserves_every_disposition_and_null_delete(
    disposition: str,
    candidate_is_none: bool,
) -> None:
    digest = "sha256:" + "a" * 64
    member = CandidateMemberLawEvidenceV2.model_construct(
        path=f"documents/{disposition}.yaml",
        artifact_kind="Document",
        disposition=disposition,
        predecessor_artifact_digest=digest,
        candidate_artifact_digest=None if candidate_is_none else digest,
    )
    row = _normalized_member_row(
        generation=2,
        changeset_digest="sha256:" + "b" * 64,
        candidate_digest="sha256:" + "c" * 64,
        member=member,
    )
    assert row.disposition == disposition
    if candidate_is_none:
        assert row.artifact_digest is None
    else:
        assert row.artifact_digest == digest
    assert row.predecessor_artifact_digest == digest


@pytest.mark.parametrize(
    "disposition",
    ["generated-successor", "hand-authored-successor", "invalidation", "replacement"],
)
def test_v1_normalization_preserves_disposition_and_never_infers_predecessor(
    disposition: str,
) -> None:
    digest = "sha256:" + "a" * 64
    member = CandidateMemberEvidence.model_construct(
        path=f"documents/{disposition}.yaml",
        artifact_kind="Document",
        disposition=disposition,
        artifact_digest=digest,
    )
    row = _normalized_member_row(
        generation=1,
        changeset_digest="sha256:" + "b" * 64,
        candidate_digest="sha256:" + "c" * 64,
        member=member,
    )
    assert row.disposition == disposition
    assert row.artifact_digest == digest
    assert row.predecessor_artifact_digest is None


def test_non_changeset_accepted_source_is_rejected() -> None:
    fake = SimpleNamespace(accepted_history=lambda: (SimpleNamespace(sequence=1, record=object()),))
    with pytest.raises(
        PlaybillSinceAcceptedStateInvalid,
        match="accepted generation has no ChangeSet",
    ) as raised:
        _normalized_rows(
            cast(Any, fake),
            lower_generation=0,
            head_generation=1,
            access_profile=CoverageAccessProfileV1(profile_id="since-test"),
        )
    assert getattr(raised.value, "code") == "playbill.since.accepted_state_invalid"

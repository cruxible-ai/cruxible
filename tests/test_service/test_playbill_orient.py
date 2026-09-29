"""The orient read: a bounded map of accepted state, one call deep."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV3,
    ClaimEvidenceAdmissionRuleV3,
)
from cruxible_core.service.discovery import orient as orient_module
from cruxible_core.service.discovery.orient import OrientCaller, service_playbill_orient
from cruxible_core.service.list_pages import PlaybillListCursorMismatch
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._candidate_support import submit_query_definition_candidate
from tests.core_support._knowledge_loop_support import (
    PREDICATE,
    QUERY_NAME,
    SUBJECT_KIND,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)
from tests.test_claims.test_claims import _claim_type

OWNER = OrientCaller("owner", "active", "admin")
UPGRADE_NOTE = "1 ClaimType still names CaptureContracts by digest; run evidence_rules_upgrade"


@pytest.fixture
def seeded(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = seed_claims(tmp_path)
    for name, description in ((QUERY_NAME, "Every work item."), ("project.work_items_b", None)):
        inspection = submit_query_definition_candidate(
            instance,
            query=work_item_query(name).model_copy(update={"description": description}),
            actor_id="owner",
            proposal_name=f"query-{name.rpartition('.')[2]}",
            timestamp=TIMESTAMP,
        )
        accept_proposal(instance, owner, inspection)
    return instance


def test_default_orient_names_each_kind_with_its_predicates_as_values(seeded) -> None:  # type: ignore[no-untyped-def]
    result = service_playbill_orient(seeded, caller=OWNER)

    assert result.instance == seeded.descriptor.instance_id
    assert result.coordinate.git_oid == seeded.accepted_coordinate().git_oid
    assert result.generation == len(seeded.accepted_history()) - 1
    (kind,) = result.kinds or ()
    assert kind.kind == SUBJECT_KIND and kind.subjects == 2
    (predicate,) = kind.predicates
    assert predicate.name == "status" and predicate.predicate == PREDICATE
    assert (predicate.cardinality, predicate.type) == ("one", "enum")
    assert predicate.members == ("blocked", "done", "ready")
    # Evidence is named by contract identity; the digest the v5 rule carries is not shown.
    assert predicate.evidence == ("playbill.foreign-source.fixture.work-items",)
    assert result.artifacts is not None
    assert (result.artifacts.claim_types, result.artifacts.queries) == (1, 2)
    assert [item.name for item in result.queries or ()] == [QUERY_NAME, "project.work_items_b"]
    assert (result.queries or ())[0].description == "Every work item."
    assert result.you is not None and result.you.can_author is True
    assert result.attention is not None and result.attention.open_proposals == 0
    assert result.truncated is False and result.next_cursor is None

    wire = result.model_dump(mode="json")
    # Absent optional parts are left off the wire rather than sent as nulls.
    assert "kind_detail" not in wire and "reason" not in wire["you"]
    assert "description" not in wire["kinds"][0]["predicates"][0]
    assert "sha256:" not in json.dumps(wire["kinds"])


def test_attention_names_digest_named_rules_and_suggests_the_upgrade(seeded) -> None:  # type: ignore[no-untyped-def]
    result = service_playbill_orient(seeded, caller=OWNER, surface="mcp")

    assert result.attention is not None
    assert result.attention.notes == (UPGRADE_NOTE,)
    assert "cruxible_playbill_evidence_rules_upgrade()" in result.next


def test_attention_reuses_a_next_item_that_already_surfaces_the_upgrade(
    seeded,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(
        severity="warning",
        reason="claim_uncovered",
        subject_identity="ClaimType:project.work_item.status",
        repair=SimpleNamespace(
            command="cruxible playbill claim-type upgrade-evidence-rules", required_change="x"
        ),
    )
    other = SimpleNamespace(
        severity="repair",
        reason="proposal_stale",
        subject_identity="sha256:" + "0123456789ab" + "c" * 52,
        repair=SimpleNamespace(command=None, required_change="readmit"),
    )
    monkeypatch.setattr(
        orient_module,
        "service_playbill_next",
        lambda *args, **kwargs: SimpleNamespace(items=(other, item), total_items=7),
    )

    attention = service_playbill_orient(seeded, caller=OWNER).attention

    assert attention is not None and attention.next_items == 7
    assert attention.top == (
        # A full digest shortens to the 12-hex prefix every selector accepts.
        "repair proposal_stale: sha256:0123456789ab",
        "warning claim_uncovered: ClaimType:project.work_item.status",
    )
    # next's own line is reused; orient does not add a second upgrade note.
    assert attention.notes == ("warning claim_uncovered: ClaimType:project.work_item.status",)


@pytest.mark.parametrize(
    ("caller", "reason"),
    [
        (None, "no authenticated actor"),
        (OrientCaller("stranger", "absent", "admin"), "has no active principal"),
        (OrientCaller("gone", "revoked", "admin"), "registration: revoked"),
        (OrientCaller("owner", "active", "read_only"), "read_only"),
    ],
)
def test_you_cannot_author_without_an_active_principal_and_says_why(
    seeded,  # type: ignore[no-untyped-def]
    caller: OrientCaller | None,
    reason: str,
) -> None:
    you = service_playbill_orient(seeded, caller=caller).you

    assert you is not None and you.can_author is False
    assert you.reason is not None and reason in you.reason


def test_next_suggestions_are_rendered_for_each_surface(seeded) -> None:  # type: ignore[no-untyped-def]
    rendered = {
        surface: service_playbill_orient(seeded, caller=OWNER, surface=surface).next
        for surface in ("mcp", "cli", "sdk")
    }

    assert rendered["mcp"][:2] == (
        f'cruxible_playbill_orient(kind="{SUBJECT_KIND}")',
        f'cruxible_playbill_query(kind="{SUBJECT_KIND}", select=["status"], limit=10)',
    )
    assert rendered["cli"][:2] == (
        f"cruxible playbill orient --kind {SUBJECT_KIND}",
        f"cruxible playbill query {SUBJECT_KIND} --select status --limit 10",
    )
    assert rendered["sdk"][:2] == (
        f'pb.orient(kind="{SUBJECT_KIND}")',
        f'pb.query(kind="{SUBJECT_KIND}", select=["status"], limit=10)',
    )


def test_orient_kind_reads_every_predicate_in_full_with_sample_subjects(seeded) -> None:  # type: ignore[no-untyped-def]
    result = service_playbill_orient(seeded, kind=SUBJECT_KIND, surface="mcp")

    detail = result.kind_detail
    assert detail is not None and result.kinds is None and result.you is None
    assert detail.sample_subject_ids == ("wi-42", "wi-43")
    (predicate,) = detail.predicates
    assert predicate.subject_kinds == (SUBJECT_KIND,)
    assert predicate.roles == ("normative", "observation")
    assert predicate.live_claims == 2
    assert result.next == (
        f'cruxible_playbill_query(kind="{SUBJECT_KIND}", select=["status"], limit=10)',
        f'cruxible_playbill_query(kind="{SUBJECT_KIND}", where=[{{"field": "status", '
        '"eq": "blocked"}])',
        f'cruxible_playbill_get(ref="{SUBJECT_KIND}/wi-42")',
    )


def test_a_wrong_kind_is_refused_with_the_nearest_kinds(seeded) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ReadRefusalError) as refused:
        service_playbill_orient(seeded, kind="project.work_itm")

    assert refused.value.error_code == "playbill.orient.kind_not_found"
    assert refused.value.http_status == 404
    assert refused.value.candidates == (SUBJECT_KIND,)
    assert refused.value.repair is not None and refused.value.repair.operation == "playbill.orient"
    assert SUBJECT_KIND in str(refused.value)


def test_kind_and_section_are_one_view_each(seeded) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ReadRefusalError, match="not both") as both:
        service_playbill_orient(seeded, kind=SUBJECT_KIND, section="queries")
    assert both.value.error_code == "playbill.orient.request_invalid"
    with pytest.raises(ReadRefusalError, match="takes no cursor"):
        service_playbill_orient(seeded, kind=SUBJECT_KIND, cursor="c")


def test_a_section_pages_compact_rows_with_a_bound_cursor(seeded) -> None:  # type: ignore[no-untyped-def]
    whole = service_playbill_orient(seeded, section="queries")
    assert [row.name for row in whole.queries or ()] == [QUERY_NAME, "project.work_items_b"]
    assert whole.truncated is False and whole.you is None and whole.kinds is None

    first = service_playbill_orient(seeded, section="queries", limit=1, surface="cli")
    assert first.truncated is True and first.next_cursor is not None
    assert first.next == (
        f"cruxible playbill get query:{QUERY_NAME}",
        f"cruxible playbill orient --section queries --cursor {first.next_cursor}",
    )
    second = service_playbill_orient(seeded, section="queries", limit=1, cursor=first.next_cursor)
    assert [row.name for row in (*(first.queries or ()), *(second.queries or ()))] == [
        row.name for row in whole.queries or ()
    ]
    assert second.truncated is False and second.next_cursor is None
    assert second.coordinate == first.coordinate

    # A cursor continues its own view only, and only at its own coordinate.
    with pytest.raises(PlaybillListCursorMismatch):
        service_playbill_orient(seeded, section="documents", cursor=first.next_cursor)
    with pytest.raises(PlaybillListCursorMismatch):
        service_playbill_orient(seeded, cursor=first.next_cursor)
    with pytest.raises(PlaybillListCursorMismatch, match="different coordinate"):
        service_playbill_orient(
            seeded,
            section="queries",
            cursor=first.next_cursor,
            at=seeded.accepted_history()[1].oid,
        )


def test_the_claim_types_section_shows_full_descriptors(seeded) -> None:  # type: ignore[no-untyped-def]
    result = service_playbill_orient(seeded, section="claim_types")

    (row,) = result.claim_types or ()
    assert row.predicate == PREDICATE and row.subject_kinds == (SUBJECT_KIND,)
    assert row.evidence == ("playbill.foreign-source.fixture.work-items",)


def test_orient_reads_an_earlier_coordinate_by_git_oid(seeded) -> None:  # type: ignore[no-untyped-def]
    earlier = seeded.accepted_history()[1]

    result = service_playbill_orient(seeded, at=earlier.oid)

    assert result.coordinate.git_oid == earlier.oid and result.generation == earlier.sequence


def test_identity_rules_name_their_contracts_and_unknown_digests_stay_short(
    seeded,  # type: ignore[no-untyped-def]
) -> None:
    identity_named = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV3(
                rules=(
                    ClaimEvidenceAdmissionRuleV3(
                        rule_id="by-identity",
                        claim_roles=("observation",),
                        capture_contracts=(
                            ArtifactRef(
                                role="capture-contract",
                                target=ArtifactIdentity(
                                    kind="CaptureContract", name="sec.advisory-feed"
                                ),
                            ),
                        ),
                        evidence_kinds=("self_asserted",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    coordinate = seeded.accepted_coordinate()
    with seeded.bind_accepted_projection(coordinate) as projection:
        names = orient_module._ContractNames(seeded, coordinate, projection.typed.connection)
        assert orient_module._evidence_names(identity_named, names) == (
            ("sec.advisory-feed",),
            False,
        )
        assert names.name("sha256:" + "ab" * 32) == "unresolved:abababababab"


def test_a_decommissioned_instance_still_orients_and_says_why(seeded) -> None:  # type: ignore[no-untyped-def]
    seeded.decommission(reason="migrated to a new host", decommissioned_by="owner")

    attention = service_playbill_orient(seeded, caller=OWNER).attention

    assert attention is not None
    assert any(
        "decommissioned" in note and "migrated to a new host" in note for note in attention.notes
    )


def test_kinds_name_shared_evidence_once(seeded) -> None:  # type: ignore[no-untyped-def]
    status = _claim_type()
    owner = status.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=f"{SUBJECT_KIND}.owner"),
            "predicate": f"{SUBJECT_KIND}.owner",
        }
    )
    state = orient_module._State(
        claim_types=(status, owner),
        subjects_by_kind={SUBJECT_KIND: 2},
        evidence={status.predicate: ("feed",), owner.predicate: ("feed",)},
        digest_named=0,
        procedures=(),
        documents=(),
        queries=(),
    )

    shared = orient_module._kind_row(state, SUBJECT_KIND)
    assert shared.evidence == ("feed",)
    assert [item.evidence for item in shared.predicates] == [(), ()]
    assert "evidence" not in shared.model_dump(mode="json")["predicates"][0]

    differing = orient_module._kind_row(
        orient_module._State(**{**state.__dict__, "evidence": {status.predicate: ("feed",)}}),
        SUBJECT_KIND,
    )
    assert differing.evidence == ()
    assert [item.evidence for item in differing.predicates] == [(), ("feed",)]

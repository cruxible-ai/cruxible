"""The orient read: a bounded map of accepted state, one call deep."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV3,
    ClaimEvidenceAdmissionRuleV3,
)
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.service.discovery import orient as orient_module
from cruxible_core.service.discovery.contract_names import CaptureContractNames
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

OWNER = OrientCaller("owner", "admin")
UPGRADE_NOTE = "1 ClaimType still names CaptureContracts by digest; run evidence_rules_upgrade"


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """One world for the module's read-only orient tests.

    Tests that mutate the instance or patch what orient folds take
    ``own_seeded`` instead, so no memo or state crosses between them.
    """

    return _seeded_world(tmp_path_factory.mktemp("orient-seeded"))


@pytest.fixture
def own_seeded(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A world of this test's own, for tests that decommission or patch."""

    return _seeded_world(tmp_path)


def _seeded_world(tmp_path: Path):  # type: ignore[no-untyped-def]
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
    assert kind.evidence == ("playbill.foreign-source.fixture.work-items",)
    assert predicate.evidence is None
    assert result.artifacts is not None
    assert (result.artifacts.claim_types, result.artifacts.queries) == (1, 2)
    assert [item.name for item in result.queries or ()] == [QUERY_NAME, "project.work_items_b"]
    assert (result.queries or ())[0].description == "Every work item."
    assert result.you is not None and result.you.can_author is True
    assert result.attention is not None and result.attention.open_proposals == 0
    assert result.truncated is False and result.next_cursor is None

    wire = result.model_dump(mode="json")
    # Absent optional parts are left off the wire rather than sent as nulls.
    assert "kind_detail" not in wire and "authoring_refusal" not in wire["you"]
    assert "description" not in wire["kinds"][0]["predicates"][0]
    assert "sha256:" not in json.dumps(wire["kinds"])


def test_attention_names_digest_named_rules_and_suggests_the_upgrade(seeded) -> None:  # type: ignore[no-untyped-def]
    result = service_playbill_orient(seeded, caller=OWNER, surface="mcp")

    assert result.attention is not None
    assert result.attention.notes == (UPGRADE_NOTE,)
    assert "cruxible_playbill_evidence_rules_upgrade()" in result.next


def test_attention_reuses_a_next_item_that_already_surfaces_the_upgrade(
    own_seeded,  # type: ignore[no-untyped-def]
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
        "summarize_playbill_next",
        lambda *args, **kwargs: SimpleNamespace(
            items=(other, item), total_items=7, matching_item=item
        ),
    )

    attention = service_playbill_orient(own_seeded, caller=OWNER).attention

    assert attention is not None and attention.next_items == 7
    assert attention.top == (
        # A full digest shortens to the 12-hex prefix every selector accepts.
        "repair proposal_stale: sha256:0123456789ab",
        "warning claim_uncovered: ClaimType:project.work_item.status",
    )
    # next's own line is reused; orient does not add a second upgrade note.
    assert attention.notes == ("warning claim_uncovered: ClaimType:project.work_item.status",)


@pytest.mark.parametrize(
    ("caller", "code"),
    [
        (None, "playbill.identity.credential_unbound"),
        (OrientCaller("stranger", "admin"), "playbill.identity.principal_absent"),
        (
            OrientCaller("operator", "admin", configured=False),
            "playbill.identity.principal_unconfigured",
        ),
        (OrientCaller("owner", "read_only"), "playbill.identity.permission_insufficient"),
    ],
)
def test_you_cannot_author_without_an_active_principal_and_says_why(
    seeded,  # type: ignore[no-untyped-def]
    caller: OrientCaller | None,
    code: str,
) -> None:
    from cruxible_core.service.identity import authoring_refusal

    you = service_playbill_orient(seeded, caller=caller).you

    assert you is not None and you.can_author is False
    assert you.authoring_refusal is not None and you.authoring_refusal.code == code
    # The same refusal whoami reports and authoring returns.
    assert you.authoring_refusal == authoring_refusal(
        seeded,
        actor_id=None if caller is None else caller.actor_id,
        configured=True if caller is None else caller.configured,
        credential_id=None,
        credential_label=None,
        permission_mode=PermissionMode[
            (caller.credential_permission_mode if caller else "read_only").upper()
        ],
    )


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
    second = service_playbill_orient(
        seeded,
        section="queries",
        limit=1,
        cursor=first.next_cursor,
        at=first.coordinate.git_oid[:12],
    )
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


def test_an_unaccepted_at_refuses_with_the_shared_read_code(seeded) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ReadRefusalError) as refused:
        service_playbill_orient(seeded, at="0" * 40)

    assert refused.value.error_code == "playbill.read.coordinate_not_accepted"
    assert refused.value.http_status == 404


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
        names = CaptureContractNames(seeded, coordinate, connection=projection.typed.connection)
        assert names.admitted(identity_named) == ("sec.advisory-feed",)
        assert not names.names_by_digest(identity_named)
        assert names.name("sha256:" + "ab" * 32) == "unresolved:abababababab"


def test_a_decommissioned_instance_still_orients_and_says_why(own_seeded) -> None:  # type: ignore[no-untyped-def]
    own_seeded.decommission(reason="migrated to a new host", decommissioned_by="owner")

    attention = service_playbill_orient(own_seeded, caller=OWNER).attention

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
    assert [item.evidence for item in shared.predicates] == [None, None]
    assert "evidence" not in shared.model_dump(mode="json")["predicates"][0]

    differing = orient_module._kind_row(
        orient_module._State(**{**state.__dict__, "evidence": {status.predicate: ("feed",)}}),
        SUBJECT_KIND,
    )
    assert differing.evidence == ()
    assert [item.evidence for item in differing.predicates] == [None, ("feed",)]


def test_every_advertised_field_name_resolves_back_to_its_predicate() -> None:
    """Addendum 2: shorten only when the short string names no accepted predicate."""

    from cruxible_core.service.discovery.field_names import resolve_field, short_field_name

    vocabulary = {
        SUBJECT_KIND: (
            "other.status",
            "third.status",
            f"{SUBJECT_KIND}.other.status",
            f"{SUBJECT_KIND}.status",
            f"{SUBJECT_KIND}.owner",
        ),
        "other": ("other.status",),
    }
    accepted = frozenset(item for predicates in vocabulary.values() for item in predicates)

    shown = {
        (kind, predicate): short_field_name(predicate, kind, accepted)
        for kind, predicates in vocabulary.items()
        for predicate in predicates
    }

    assert shown[(SUBJECT_KIND, f"{SUBJECT_KIND}.other.status")] == (f"{SUBJECT_KIND}.other.status")
    assert shown[(SUBJECT_KIND, f"{SUBJECT_KIND}.status")] == "status"
    assert shown[(SUBJECT_KIND, "other.status")] == "other.status"
    assert shown[(SUBJECT_KIND, "third.status")] == "third.status"
    for (kind, predicate), name in shown.items():
        assert resolve_field(name, kind, frozenset(vocabulary[kind])) == predicate
    assert resolve_field("missing", SUBJECT_KIND, frozenset(vocabulary[SUBJECT_KIND])) is None


def test_orient_advertises_names_by_the_shared_rule(seeded) -> None:  # type: ignore[no-untyped-def]
    status = _claim_type()
    shadowed = status.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=f"{SUBJECT_KIND}.other.status"),
            "predicate": f"{SUBJECT_KIND}.other.status",
        }
    )
    other = status.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="other.status"),
            "predicate": "other.status",
        }
    )
    state = orient_module._State(
        claim_types=(status, shadowed, other),
        subjects_by_kind={SUBJECT_KIND: 2},
        evidence={},
        digest_named=0,
        procedures=(),
        documents=(),
        queries=(),
    )

    row = orient_module._kind_row(state, SUBJECT_KIND)

    assert {item.predicate: item.name for item in row.predicates} == {
        PREDICATE: "status",
        f"{SUBJECT_KIND}.other.status": f"{SUBJECT_KIND}.other.status",
        "other.status": "other.status",
    }


def test_sdk_suggestions_are_python_literals_and_mcp_keeps_json() -> None:
    call = orient_module._Call(
        "query",
        (
            ("kind", SUBJECT_KIND),
            ("where", [{"field": "flag", "eq": True}, {"field": "note", "eq": None}]),
            ("limit", 10),
        ),
    )

    sdk = orient_module.render_orient_call(call, "sdk")
    parsed = ast.parse(sdk, mode="eval").body
    assert isinstance(parsed, ast.Call)
    arguments = {item.arg: ast.literal_eval(item.value) for item in parsed.keywords}
    assert arguments == {
        "kind": SUBJECT_KIND,
        "where": [{"field": "flag", "eq": True}, {"field": "note", "eq": None}],
        "limit": 10,
    }
    assert orient_module.render_orient_call(call, "mcp") == (
        f'cruxible_playbill_query(kind="{SUBJECT_KIND}", where=[{{"field": "flag", "eq": true}}, '
        '{"field": "note", "eq": null}], limit=10)'
    )


def test_a_decommissioned_instance_cannot_be_authored_even_by_an_active_writer(own_seeded) -> None:  # type: ignore[no-untyped-def]
    own_seeded.decommission(reason="migrated to a new host", decommissioned_by="owner")

    you = service_playbill_orient(own_seeded, caller=OWNER).you

    assert you is not None and you.can_author is False
    assert you.actor == "owner" and you.principal == "owner"
    assert you.authoring_refusal is not None
    assert you.authoring_refusal.code == "playbill.instance.decommissioned"
    assert "migrated to a new host" in you.authoring_refusal.detail


def _accept_interfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Accept the demo interface with its Provider, plus an acquisition interface with none."""

    import cruxible_core.proposals.proposals as proposal_module
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.provider_interfaces import (
        ProviderBucketVocabularyV1,
        provider_bucket_vocabulary_digest,
        provider_interface_definition_digest,
        provider_interface_path,
        render_provider_interface,
    )
    from cruxible_client.contracts.providers import render_provider
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
    from tests.core_support._p2b1_support import (
        accepted_interface,
        accepted_provider,
        interface_fixture,
        interface_registration,
    )
    from tests.core_support._support import initialize_local

    instance, owner = initialize_local(tmp_path)
    fixture = interface_fixture()
    monkeypatch.setattr(
        proposal_module,
        "core_provider_bucket_conformance_fixtures",
        lambda: {fixture.fixture_id: fixture},
    )
    demo, provider = accepted_interface(), accepted_provider()
    definition = canonical_bytes(
        {
            "interface_id": "demo.fetch",
            "version": 1,
            "effect_class": "external_read",
            "contracts": {
                "input": {
                    "fields": {
                        "url": {"type": "string"},
                        "max_bytes": {"type": "integer", "optional": True},
                    }
                },
                "output": "playbill-provider-result-to-external-capture-v1",
            },
        }
    ).hex()
    base = interface_registration()
    vocabulary = canonical_bytes(
        ProviderBucketVocabularyV1.model_validate_json(bytes.fromhex(base.vocabulary_bytes_hex))
        .model_copy(
            update={
                "interface_id": "demo.fetch",
                "description": "Fetch one resource over HTTP. Buckets size the payload.",
            }
        )
        .model_dump(mode="json")
    ).hex()
    fetch = base.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ProviderInterface", name="demo.fetch"),
            "interface_id": "demo.fetch",
            "interface_bytes_hex": definition,
            "interface_digest": provider_interface_definition_digest(definition),
            "vocabulary_bytes_hex": vocabulary,
            "vocabulary_digest": provider_bucket_vocabulary_digest(vocabulary),
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[demo.path] = render_provider_interface(demo.registration)
    tree[provider_interface_path("demo.fetch")] = render_provider_interface(fetch)
    tree[provider.path] = render_provider(provider.provider)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/orient-interfaces",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert proposed.evaluation.verdict == "candidate", proposed.evaluation.diagnostics
    accept_proposal(
        instance,
        owner,
        service_inspect_playbill_proposal(instance, proposal_id=proposed.admission.proposal_id),
    )
    return instance


def test_orient_pages_the_provider_interfaces_a_procedure_can_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.discovery.discovery import service_discover_playbill_semantic

    instance = _accept_interfaces(tmp_path, monkeypatch)

    first = service_playbill_orient(instance, section="interfaces", limit=1, surface="mcp")
    assert first.section == "interfaces" and first.truncated and first.next_cursor
    (fetch,) = first.interfaces or ()
    assert (fetch.name, fetch.description, fetch.input, fetch.output, fetch.effect) == (
        "demo.fetch",
        "Fetch one resource over HTTP.",
        ("max_bytes?: integer", "url: string"),
        ("playbill-provider-result-to-external-capture-v1",),
        "external_read",
    )
    # A row carries what a Procedure node pins: the interface digest and its
    # operation contract; demo.fetch has no Provider yet.
    assert fetch.interface_digest.startswith("sha256:") and fetch.providers == ()
    assert fetch.operation_contract is not None
    assert first.next == (
        'cruxible_playbill_get(ref="ProviderInterface:demo.fetch")',
        f'cruxible_playbill_orient(section="interfaces", cursor="{first.next_cursor}")',
    )
    rest = service_playbill_orient(instance, section="interfaces", cursor=first.next_cursor)
    assert rest.interfaces is not None
    ((demo),) = rest.interfaces
    assert (demo.name, demo.input, demo.output, demo.effect) == (
        "demo.interface",
        (),
        (),
        "external_read",
    )
    ((implementation),) = demo.providers
    assert implementation.provider == "demo-provider"
    assert implementation.implementation_digest.startswith("sha256:")

    # The rows come from discover's own inventory: the same interfaces, in order.
    inventory = service_discover_playbill_semantic(
        instance, evaluation_time="2026-08-16T21:00:00Z", profile="interfaces"
    )
    assert [item.identity.removeprefix("ProviderInterface:") for item in inventory.interfaces] == [  # type: ignore[union-attr]
        "demo.fetch",
        "demo.interface",
    ]

    # The map counts them and points at the section.
    default = service_playbill_orient(instance, surface="mcp")
    assert default.artifacts is not None and default.artifacts.interfaces == 2
    assert 'cruxible_playbill_orient(section="interfaces")' in default.next


def test_orient_without_interfaces_counts_none_and_suggests_no_section(
    seeded,  # type: ignore[no-untyped-def]
) -> None:
    instance = seeded
    result = service_playbill_orient(instance, surface="mcp")

    assert result.artifacts is not None and result.artifacts.interfaces == 0
    assert not any("interfaces" in line for line in result.next)
    empty = service_playbill_orient(instance, section="interfaces")
    assert empty.interfaces == () and empty.next == ()


def test_modal_evidence_hoists_in_both_views_and_round_trips_empty_exceptions(
    seeded,  # type: ignore[no-untyped-def]
) -> None:
    from cruxible_client.contracts.orient import PlaybillOrientKindV1

    evidence = (("feed-a", "feed-b"), ("feed-a", "feed-b"), (), ("other",))
    types = tuple(
        _claim_type().model_copy(
            update={
                "identity": ArtifactIdentity(kind="ClaimType", name=f"{SUBJECT_KIND}.p{i}"),
                "predicate": f"{SUBJECT_KIND}.p{i}",
            }
        )
        for i in range(len(evidence))
    )
    state = orient_module._State(
        claim_types=types,
        subjects_by_kind={SUBJECT_KIND: 2},
        evidence={item.predicate: value for item, value in zip(types, evidence, strict=True)},
        digest_named=0,
        procedures=(),
        documents=(),
        queries=(),
    )
    for row in (
        orient_module._kind_row(state, SUBJECT_KIND),
        orient_module._kind_detail(seeded, seeded.accepted_coordinate(), state, SUBJECT_KIND),
    ):
        assert row.evidence == evidence[0]
        assert [item.evidence for item in row.predicates] == [None, None, (), ("other",)]
        wire = row.model_dump(mode="json")
        assert "evidence" not in wire["predicates"][0]
        assert wire["predicates"][2]["evidence"] == []
        restored = type(row).model_validate_json(row.model_dump_json())
        assert (
            tuple(
                restored.evidence if item.evidence is None else item.evidence
                for item in restored.predicates
            )
            == evidence
        )
        # Inheritance has the same typed meaning in the compact model.
        assert (
            PlaybillOrientKindV1.model_validate(
                {key: value for key, value in wire.items() if key != "sample_subject_ids"}
            )
            .predicates[0]
            .evidence
            is None
        )


def test_modal_evidence_ties_are_independent_of_predicate_order() -> None:
    from cruxible_client.contracts.orient import PlaybillOrientPredicateV1

    rows = tuple(
        PlaybillOrientPredicateV1(
            name=str(i), predicate=str(i), cardinality="one", type="string", evidence=value
        )
        for i, value in enumerate((("z",), ("a",)))
    )
    assert orient_module._hoist_evidence(rows)[0] == ("a",)
    assert orient_module._hoist_evidence(tuple(reversed(rows)))[0] == ("a",)
    assert orient_module._hoist_evidence(()) == ((), ())


def test_attention_summary_preserves_complete_orient_bytes(
    own_seeded,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_core.service.claims.claims import _claim_law_evidence_index
    from cruxible_core.service.discovery import next as next_module
    from cruxible_core.service.discovery.next import PlaybillNextSummary, service_playbill_next

    moment = datetime(2026, 9, 29, tzinfo=UTC)
    optimized = service_playbill_orient(own_seeded, caller=OWNER, evaluation_time=moment)
    dependency_fold = next_module._claim_dependency_items

    def original_dependencies(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("claims", None)
        return dependency_fold(*args, **kwargs)

    def full_queue(instance, *, request, caller_principal_id, caller_rung, match):  # type: ignore[no-untyped-def]
        result = service_playbill_next(
            instance,
            request=request,
            caller_principal_id=caller_principal_id,
            caller_rung=caller_rung,
        )
        matching = next((item for item in result.items if match(item)), None)
        return PlaybillNextSummary(result.items, result.total_items, matching)

    # The previous path constructed public Claim cards, all dependency facts,
    # the entire law-evidence map, health facets and a digested next page.
    monkeypatch.setattr(next_module, "_claim_dependency_items", original_dependencies)
    monkeypatch.setattr(next_module, "_claim_threshold_evidence", _claim_law_evidence_index)
    monkeypatch.setattr(orient_module, "summarize_playbill_next", full_queue)
    previous = service_playbill_orient(own_seeded, caller=OWNER, evaluation_time=moment)
    assert canonical_bytes(optimized.model_dump(mode="json")) == canonical_bytes(
        previous.model_dump(mode="json")
    )


def test_get_reads_one_provider_interface_card_and_its_inventory_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
    from cruxible_core.service.discovery.discovery import accepted_provider_interfaces
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext

    instance = _accept_interfaces(tmp_path, monkeypatch)
    access = BodyAccessContext(principal_id="reader", can_read_body=False)

    card = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref="ProviderInterface:demo.interface"),
        access=access,
    )
    assert card.kind == "provider_interface" and card.card is not None
    shown = card.card.model_dump(mode="json")
    assert [item["provider"] for item in shown["providers"]] == ["demo-provider"]
    proof = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref="ProviderInterface:demo.interface", detail="proof"),
        access=access,
    )
    (entry,) = (
        item.entry
        for item in accepted_provider_interfaces(instance, instance.accepted_coordinate())
        if item.entry.identity == "ProviderInterface:demo.interface"
    )
    assert proof.proof is not None and proof.proof["entry"] == entry.model_dump(mode="json")

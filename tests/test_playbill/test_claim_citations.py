"""Claim-v2 citation identity, wire succession, and legacy interpretation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.candidates import CandidateRecordV3
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    AcceptedCaptureContract,
    CaptureContractV1,
    DirectByteSpanSelectionV1,
    DirectForeignSourceSelectionV1,
    build_coordinator_self_source_capture,
    build_direct_claim_capture,
    build_direct_claim_selection_capture,
    build_foreign_source_capture,
    capture_contract_digest,
    capture_contract_path,
    classify_capture_reuse,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, render_claim_type
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    ClaimBackingV2,
    ClaimLawEvidenceV1,
    LegacyCitationReferenceV1,
    _capture_is_explicitly_eligible,
    _copy_capture_admitted_by_rule,
    build_claim_citation,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    merge_claim_citations,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_client.contracts.subjects import render_subject, subject_digest, subject_path
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.service.playbill_claims import service_get_playbill_claim
from cruxible_core.service.playbill_coverage import build_accepted_evidence_index_v2
from tests.test_playbill._pc_c_support import capture_contract as _capture_contract
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim, _claim_type, _subject

CLAIM_ID = "CLM-0123456789abcdef0123456789abcdef"
CAPTURE_DIGEST = "sha256:" + "ab" * 32
SOURCE_DIGEST = "sha256:" + "cd" * 32


def _legacy_claim() -> ClaimArtifactV2:
    claim = _claim(
        claim_id=CLAIM_ID,
        capture_digest=CAPTURE_DIGEST,
        source_digest=SOURCE_DIGEST,
        source_length=12,
    )
    return claim.model_copy(update={"backing": claim.backing.model_copy(update={"citations": ()})})


def _v2_claim(*, role: str = "evidence", origin: str = "independent") -> ClaimArtifactV2:
    legacy = _legacy_claim()
    citation = build_claim_citation(
        legacy.identity,
        capture_digest=CAPTURE_DIGEST,
        role=role,  # type: ignore[arg-type]
        origin=origin,  # type: ignore[arg-type]
    )
    return ClaimArtifactV2(
        identity=legacy.identity,
        statement=legacy.statement,
        backing=ClaimBackingV2(
            referent_context=legacy.backing.referent_context,
            capture_digests=legacy.backing.capture_digests,
            citations=(citation,),
            source_mappings=legacy.backing.source_mappings,
        ),
        pins=legacy.pins,
    )


def test_citation_id_uses_the_exact_frozen_preimage_and_retry_is_idempotent() -> None:
    claim = _v2_claim()
    citation = claim.backing.citations[0]
    expected = typed_digest(
        Sha256Value,
        "playbill-claim-citation-v1",
        {
            "claim_identity": {"kind": "Claim", "name": CLAIM_ID},
            "capture_digest": CAPTURE_DIGEST,
            "origin": "independent",
            "role": "evidence",
        },
    ).tagged

    assert citation.citation_id == expected
    assert merge_claim_citations((citation,), (citation,)) == (citation,)


def test_claim_v2_round_trips_and_rejects_a_forged_citation_id() -> None:
    claim = _v2_claim()

    assert parse_claim(render_claim(claim), path=claim_path(CLAIM_ID)) == claim
    with pytest.raises(ValidationError, match="citation ID does not reproduce"):
        ClaimArtifactV2.model_validate(
            claim.model_copy(
                update={
                    "backing": claim.backing.model_copy(
                        update={
                            "citations": (
                                claim.backing.citations[0].model_copy(
                                    update={"citation_id": "sha256:" + "00" * 32}
                                ),
                            )
                        }
                    )
                }
            ).model_dump(mode="json")
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        claim.backing.citations[0].__class__.model_validate(
            {
                **claim.backing.citations[0].model_dump(mode="json"),
                "observation_trust": "provider_receipted",
            }
        )


def test_legacy_reference_is_derived_without_fabricating_role_or_origin() -> None:
    legacy = _legacy_claim()
    references = claim_citation_references(legacy)

    assert len(references) == 1
    reference = references[0]
    assert isinstance(reference, LegacyCitationReferenceV1)
    assert reference.legacy_semantics is True
    assert reference.model_dump(mode="json") == {
        "tag": "playbill-legacy-claim-citation-v1",
        "citation_id": typed_digest(
            Sha256Value,
            "playbill-legacy-claim-citation-v1",
            {
                "claim_identity": {"kind": "Claim", "name": CLAIM_ID},
                "capture_digest": CAPTURE_DIGEST,
            },
        ).tagged,
        "claim_identity": {"kind": "Claim", "name": CLAIM_ID},
        "capture_digest": CAPTURE_DIGEST,
        "legacy_semantics": True,
    }


def test_v2_backing_successor_keeps_uncited_legacy_capture_implicit() -> None:
    legacy = _legacy_claim()
    successor = ClaimArtifactV2(
        identity=legacy.identity,
        statement=legacy.statement,
        backing=ClaimBackingV2(
            referent_context=legacy.backing.referent_context.model_copy(
                update={"observed_at": datetime(2026, 8, 20, tzinfo=timezone.utc)}
            ),
            capture_digests=legacy.backing.capture_digests,
            citations=(),
            source_mappings=legacy.backing.source_mappings,
        ),
        pins=legacy.pins,
        lifecycle=ArtifactLifecycle(predecessor_digest=claim_artifact_digest(legacy).tagged),
    )

    references = claim_citation_references(successor)
    assert len(references) == 1
    assert isinstance(references[0], LegacyCitationReferenceV1)
    assert parse_claim(render_claim(successor), path=claim_path(CLAIM_ID)) == successor


def test_one_capture_can_have_different_roles_on_different_claims() -> None:
    evidence = _v2_claim(role="evidence")
    other_identity = ArtifactIdentity(kind="Claim", name="CLM-abcdefabcdefabcdefabcdefabcdefab")
    copy = build_claim_citation(
        other_identity,
        capture_digest=CAPTURE_DIGEST,
        role="copy",
        origin="independent",
    )

    assert evidence.backing.citations[0].capture_digest == copy.capture_digest
    assert evidence.backing.citations[0].citation_id != copy.citation_id
    assert evidence.backing.citations[0].role == "evidence"
    assert copy.role == "copy"


def test_mixed_wire_succession_is_deterministic_and_citations_are_append_only(
    tmp_path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    first_capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=CLAIM_ID,
        value="ready",
        rationale="initial v1 evidence",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
    )
    assert first_capture.envelope.commitment.byte_length is not None
    legacy = _claim(
        claim_id=CLAIM_ID,
        capture_digest=first_capture.capture_digest,
        source_digest=first_capture.source_body_digest,
        source_length=first_capture.envelope.commitment.byte_length,
    )
    shell = _subject()
    claim_type = _claim_type()
    initial_tree = {
        **instance.tree_at(base.git_oid),
        subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
        "claim-types/project.work_item/status.json": render_claim_type(claim_type),
        capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
            render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
        ),
        claim_path(CLAIM_ID): render_claim(legacy),
    }
    initial = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/citation-v1",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=initial_tree,
        timestamp="2026-08-20T12:00:00.000000Z",
    )
    assert isinstance(initial.candidate, CandidateRecordV3)
    _activate(instance, owner, initial.candidate, initial.evaluation.evaluated_tree_oid, sequence=1)

    accepted_v1 = instance.accepted_coordinate()
    second_capture = build_coordinator_self_source_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=CLAIM_ID,
        body=b"new v2 backing",
        observed_at=datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(accepted_v1),
    )
    citation = build_claim_citation(
        legacy.identity,
        capture_digest=second_capture.capture_digest,
        role="evidence",
        origin="self_source",
    )
    substrate = b"status: ready\n"
    substrate_digest = instance.body_store().store(substrate).digest
    selection_capture = build_direct_claim_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=CLAIM_ID,
        rationale="existing source copy",
        observed_at=datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(accepted_v1),
        selection=DirectByteSpanSelectionV1(
            span=ContentSpan(
                content_digest=substrate_digest,
                start_byte=0,
                end_byte=len(substrate),
            )
        ),
    )
    assert (
        classify_capture_reuse(
            selection_capture.envelope,
            contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
            store=instance.body_store(),
            claim_id=CLAIM_ID,
        )
        == "claim_bound"
    )
    assert (
        classify_capture_reuse(
            selection_capture.envelope,
            contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
            store=instance.body_store(),
            claim_id="CLM-abcdefabcdefabcdefabcdefabcdefab",
        )
        == "claim_bound_mismatch"
    )
    copy_citation = build_claim_citation(
        legacy.identity,
        capture_digest=selection_capture.capture_digest,
        role="copy",
        origin="independent",
    )
    v2 = ClaimArtifactV2(
        identity=legacy.identity,
        statement=legacy.statement,
        backing=ClaimBackingV2(
            referent_context=legacy.backing.referent_context.model_copy(
                update={"observed_at": datetime(2026, 8, 20, 12, 1, tzinfo=timezone.utc)}
            ),
            capture_digests=tuple(
                sorted(
                    (
                        first_capture.capture_digest,
                        second_capture.capture_digest,
                        selection_capture.capture_digest,
                    )
                )
            ),
            citations=merge_claim_citations(
                legacy.backing.citations,
                (citation,),
                (copy_citation,),
            ),
            source_mappings=legacy.backing.source_mappings,
        ),
        pins=tuple(
            sorted(
                (
                    *legacy.pins,
                    ArtifactPin(
                        role="capture-contract",
                        target=COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity,
                        artifact_digest=capture_contract_digest(
                            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
                        ).tagged,
                    ),
                ),
                key=lambda item: (item.role.encode(), item.target.qualified.encode()),
            )
        ),
        lifecycle=ArtifactLifecycle(predecessor_digest=claim_artifact_digest(legacy).tagged),
    )
    successor_tree = {
        **instance.tree_at(accepted_v1.git_oid),
        capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name): (
            render_capture_contract(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT)
        ),
        claim_path(CLAIM_ID): render_claim(v2),
    }
    forged_origin = v2.model_copy(
        update={
            "backing": v2.backing.model_copy(
                update={
                    "citations": merge_claim_citations(
                        legacy.backing.citations,
                        (
                            build_claim_citation(
                                legacy.identity,
                                capture_digest=second_capture.capture_digest,
                                role="copy",
                                origin="independent",
                            ),
                        ),
                        (copy_citation,),
                    )
                }
            )
        }
    )
    forged = _submit(
        instance,
        {
            **instance.tree_at(accepted_v1.git_oid),
            capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name): (
                render_capture_contract(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT)
            ),
            claim_path(CLAIM_ID): render_claim(forged_origin),
        },
        accepted_v1.git_oid,
        "citation-v2-forged-origin",
        "2026-08-20T12:01:00.000000Z",
    )
    assert {item.code for item in forged.evaluation.diagnostics} == {
        "playbill.claim.self_source_origin_mismatch"
    }
    first_evaluation = _submit(
        instance,
        successor_tree,
        accepted_v1.git_oid,
        "citation-v2-a",
        "2026-08-20T12:01:00.000000Z",
    )
    second_evaluation = _submit(
        instance,
        successor_tree,
        accepted_v1.git_oid,
        "citation-v2-b",
        "2026-08-20T12:01:00.000000Z",
    )
    assert first_evaluation.evaluation.diagnostics == second_evaluation.evaluation.diagnostics == ()
    assert first_evaluation.candidate is not None
    assert second_evaluation.candidate is not None
    assert first_evaluation.candidate.law_evidence == second_evaluation.candidate.law_evidence
    initial_evidence = _claim_evidence(initial.candidate)
    successor_evidence = _claim_evidence(first_evaluation.candidate)
    assert successor_evidence.verdict_captures == ()
    assert successor_evidence.evidence_basis == initial_evidence.evidence_basis
    assert successor_evidence.verdict_result is not None
    assert initial_evidence.verdict_result is not None
    assert successor_evidence.verdict_result.supporting_evidence_digests == (
        initial_evidence.verdict_result.supporting_evidence_digests
    )
    assert successor_evidence.verdict_result.provenance_grades == (
        initial_evidence.verdict_result.provenance_grades
    )
    assert successor_evidence.verdict_result.control_components == (
        initial_evidence.verdict_result.control_components
    )
    _activate(
        instance,
        owner,
        first_evaluation.candidate,
        first_evaluation.evaluation.evaluated_tree_oid,
        sequence=2,
    )

    accepted_v2 = instance.accepted_coordinate()
    read = service_get_playbill_claim(instance, identity=CLAIM_ID)
    accounts = {item.citation_id: item for item in read.admission_accounts}
    assert accounts[copy_citation.citation_id].status == "not_evidence"
    assert accounts[copy_citation.citation_id].decisions == ()
    assert accounts[citation.citation_id].status == "not_admitted"
    assert accounts[citation.citation_id].decisions[0].closest_rule_id is None
    assert accounts[citation.citation_id].decisions[0].refusal_code == (
        "playbill.evidence.undeclared_contract_kind"
    )
    coverage_index = build_accepted_evidence_index_v2(
        instance,
        at=PlaybillAcceptedCoordinate.from_internal(accepted_v2),
    )
    rebuilt_coverage_index = build_accepted_evidence_index_v2(
        instance,
        at=PlaybillAcceptedCoordinate.from_internal(accepted_v2),
    )
    assert canonical_bytes(coverage_index.model_dump(mode="json")) == canonical_bytes(
        rebuilt_coverage_index.model_dump(mode="json")
    )
    coverage_associations = {
        association.reference.citation_id: association
        for row in coverage_index.citations
        for association in row.citation_associations
    }
    assert set(coverage_associations) == {
        reference.citation_id for reference in claim_citation_references(v2)
    }
    assert coverage_associations[citation.citation_id].reference.role == "evidence"
    assert coverage_associations[citation.citation_id].reference.origin == "self_source"
    assert coverage_associations[copy_citation.citation_id].reference.role == "copy"
    assert coverage_associations[copy_citation.citation_id].observation_trust == (
        "proposer_observed"
    )
    dropped = v2.model_copy(
        update={
            "backing": v2.backing.model_copy(update={"citations": ()}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_artifact_digest(v2).tagged),
        }
    )
    refused = _submit(
        instance,
        {**instance.tree_at(accepted_v2.git_oid), claim_path(CLAIM_ID): render_claim(dropped)},
        accepted_v2.git_oid,
        "citation-v2-drop",
        "2026-08-20T12:02:00.000000Z",
    )
    assert {item.code for item in refused.evaluation.diagnostics} == {
        "playbill.claim.legacy_capture_set_changed"
    }


def _submit(instance, tree, base_oid: str, name: str, timestamp: str):
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}",
            proposed_base_oid=base_oid,
        ),
        candidate_tree=tree,
        timestamp=timestamp,
    )


def _activate(instance, owner, candidate, evaluated_oid, *, sequence: int) -> None:
    assert evaluated_oid is not None
    base = instance.accepted_coordinate()
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=sequence,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def _claim_evidence(candidate: CandidateRecordV3) -> ClaimLawEvidenceV1:
    return ClaimLawEvidenceV1.model_validate(
        next(
            item.result["claim_evidence"]
            for item in candidate.law_evidence
            if item.path == claim_path(CLAIM_ID)
        )
    )


def _accepted(contract: CaptureContractV1) -> AcceptedCaptureContract:
    return AcceptedCaptureContract(
        path=capture_contract_path(contract.identity.name),
        contract=contract,
        artifact_digest=capture_contract_digest(contract).tagged,
    )


def _page_source_contract() -> CaptureContractV1:
    """A declared source contract, the way a governed page is captured.

    Not one of the compiler's own self-assertion contracts: the bytes exist
    whether or not this Claim is authored, which is what makes a `copy` of them
    something a ClaimType can honestly admit.
    """

    return _capture_contract(name="docs.page-capture-v1")


def _type_admitting(contract: CaptureContractV1) -> ClaimType:
    return _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="page-copy",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(capture_contract_digest(contract).tagged,),
                        evidence_kinds=contract.evidence_kinds,
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )


def test_a_copy_citation_is_never_evidence_by_its_shape_alone() -> None:
    """Card 120: the eligibility gate admits an independent evidence role and nothing else."""

    assert not _capture_is_explicitly_eligible(
        _v2_claim(role="copy"),
        capture_digest=CAPTURE_DIGEST,
    )


def test_a_claim_type_that_names_the_contract_admits_a_copy_citation() -> None:
    """The ruling's own word for a page citation can cover the Claim that cites it.

    `copied_from` lowers as role `copy`, and the eligibility gate skipped it one
    citation-role before any admission policy was read -- so the honest spelling
    of "the object bytes ARE these bytes" made the Claim permanently uncoverable
    and the only repair was to describe a copy as evidence.
    """

    contract = _page_source_contract()

    assert _copy_capture_admitted_by_rule(
        _v2_claim(role="copy"),
        claim_type=_type_admitting(contract),
        capture_contract=_accepted(contract),
        capture_digest=CAPTURE_DIGEST,
    )


def test_a_claim_type_that_does_not_name_the_contract_still_reads_uncovered() -> None:
    """No loophole: the rule decides, and a type that never named the contract says no."""

    contract = _page_source_contract()
    silent = _claim_type().model_copy(
        update={"evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(rules=())}
    )

    assert not _copy_capture_admitted_by_rule(
        _v2_claim(role="copy"),
        claim_type=silent,
        capture_contract=_accepted(contract),
        capture_digest=CAPTURE_DIGEST,
    )


@pytest.mark.parametrize("origin", ["self_source", "self_published"])
def test_a_copy_of_bytes_this_claim_published_is_admitted_by_no_rule(origin: str) -> None:
    """Attesting your own page into concrete is what the pages-are-sources ruling refused."""

    contract = _page_source_contract()

    assert not _copy_capture_admitted_by_rule(
        _v2_claim(role="copy", origin=origin),
        claim_type=_type_admitting(contract),
        capture_contract=_accepted(contract),
        capture_digest=CAPTURE_DIGEST,
    )


def test_a_copy_of_the_compilers_own_self_assertion_is_admitted_by_no_rule() -> None:
    """The Claim quoting its own authoring is not a page citing a source.

    Every domain ClaimType already names the direct self-asserted contract, so
    without this the carve-out would hand every one of them a way to cover
    itself with a copy of the value it just authored.
    """

    for builtin in (
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    ):
        assert not _copy_capture_admitted_by_rule(
            _v2_claim(role="copy"),
            claim_type=_type_admitting(builtin),
            capture_contract=_accepted(builtin),
            capture_digest=CAPTURE_DIGEST,
        )


PAGE_SOURCE_IDENTITY = "docs.governed-page"
PAGE_BODY = b"a copy is the ruling's own word for what a page block is\n"


def _page_capture(instance, base):
    """Capture a governed page's bytes the way a page-as-source is captured.

    A foreign logical source: bytes the proposer presents and the daemon commits
    to exactly. Its contract is DECLARED and is not one of the compiler's two
    self-assertion contracts, which is the whole shape item 16 exists to open.
    """

    stored = instance.body_store().store(PAGE_BODY)
    return build_foreign_source_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=CLAIM_ID,
        rationale="the governed page block this Claim is about",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
        selection=DirectForeignSourceSelectionV1(
            logical_source_identity=PAGE_SOURCE_IDENTITY,
            span=ContentSpan(
                content_digest=stored.digest,
                start_byte=0,
                end_byte=len(PAGE_BODY),
            ),
        ),
    )


def _type_naming(contract: CaptureContractV1) -> ClaimType:
    """The domain ClaimType, plus one rule naming `contract` for its own roles."""

    base = _claim_type()
    return base.model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    *base.evidence_admission_policy.rules,
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="page-copy",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(capture_contract_digest(contract).tagged,),
                        evidence_kinds=contract.evidence_kinds,
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )


def _claim_citing(capture, *, contract: CaptureContractV1, claim_type: ClaimType, role: str):
    shell = _subject()
    assert capture.envelope.commitment.byte_length is not None
    claim = _claim(
        claim_id=CLAIM_ID,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=capture.envelope.commitment.byte_length,
    )
    claim_type_digest_value = claim_type_digest(claim_type).tagged
    return claim.model_copy(
        update={
            "statement": claim.statement.model_copy(
                update={"claim_type_digest": claim_type_digest_value}
            ),
            "backing": ClaimBackingV2(
                referent_context=claim.backing.referent_context,
                capture_digests=(capture.capture_digest,),
                citations=(
                    build_claim_citation(
                        claim.identity,
                        capture_digest=capture.capture_digest,
                        role=role,  # type: ignore[arg-type]
                        origin="independent",
                    ),
                ),
                source_mappings=claim.backing.source_mappings,
            ),
            "pins": tuple(
                sorted(
                    (
                        ArtifactPin(
                            role="capture-contract",
                            target=contract.identity,
                            artifact_digest=capture_contract_digest(contract).tagged,
                        ),
                        ArtifactPin(
                            role="claim-type",
                            target=claim_type.identity,
                            artifact_digest=claim_type_digest_value,
                        ),
                        ArtifactPin(
                            role="subject",
                            target=shell.identity,
                            artifact_digest=subject_digest(shell).tagged,
                        ),
                    ),
                    key=lambda item: (item.role, item.target.qualified),
                )
            ),
        }
    )


def _verdict(
    instance,
    base,
    *,
    capture,
    contract: CaptureContractV1,
    claim_type: ClaimType,
    name: str,
):
    """Submit one Claim citing `capture` as a copy, and read the coverage verdict."""

    shell = _subject()
    claim = _claim_citing(capture, contract=contract, claim_type=claim_type, role="copy")
    proposed = _submit(
        instance,
        {
            **instance.tree_at(base.git_oid),
            subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
            "claim-types/project.work_item/status.json": render_claim_type(claim_type),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
            claim_path(CLAIM_ID): render_claim(claim),
        },
        base.git_oid,
        name,
        "2026-08-20T12:00:00.000000Z",
    )
    assert proposed.evaluation.diagnostics == (), proposed.evaluation.diagnostics
    assert isinstance(proposed.candidate, CandidateRecordV3)
    return _claim_evidence(proposed.candidate)


def _direct_selection_capture(instance, base):
    """A capture under the compiler's own direct self-assertion contract.

    Deliberately the SELECTION builder, not the value builder. A direct
    self-source capture is `verified_self_source`, so the citation-origin gate
    forces `origin: self_source` on it and it can never wear `independent` at
    all. A direct SELECTION capture is only `direct_selection_bound`: it may
    legitimately carry `origin: independent`, and every domain ClaimType already
    names the direct contract. So this is the exact shape where the copy
    carve-out would hand a Claim a way to cover itself with a copy of the value
    it just authored, and the contract-identity gate is the only thing standing
    in the way.
    """

    substrate = b"status: ready\n"
    substrate_digest = instance.body_store().store(substrate).digest
    return build_direct_claim_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=CLAIM_ID,
        rationale="a copy of the value this Claim itself authored",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
        selection=DirectByteSpanSelectionV1(
            span=ContentSpan(
                content_digest=substrate_digest,
                start_byte=0,
                end_byte=len(substrate),
            )
        ),
    )


def test_a_copy_of_a_page_covers_only_when_the_claim_type_names_its_contract(tmp_path) -> None:
    """Card 120's ruling, read as a coverage VERDICT rather than a predicate.

    The four predicate tests above prove what `_copy_capture_admitted_by_rule`
    answers; none of them proves it is WIRED. Delete the
    `and not _copy_capture_admitted_by_rule(...)` clause from the eligibility
    gate in `evaluate_claim_law` and every one of them still passes, because the
    helper is intact and nothing reads the verdict. This reads the verdict.
    """

    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    contract = foreign_source_capture_contract(PAGE_SOURCE_IDENTITY)

    covered = _verdict(
        instance,
        base,
        capture=_page_capture(instance, base),
        contract=contract,
        claim_type=_type_naming(contract),
        name="page-copy-named",
    )
    uncovered = _verdict(
        instance,
        base,
        capture=_page_capture(instance, base),
        contract=contract,
        claim_type=_claim_type(),
        name="page-copy-unnamed",
    )

    assert covered.initial_verdict == "supported"
    assert covered.evidence_basis == ("direct",)
    assert uncovered.initial_verdict == "uncovered"
    assert uncovered.evidence_basis == ("origin_only",)


def test_a_copy_of_the_compilers_own_self_assertion_never_reaches_a_verdict(tmp_path) -> None:
    """A domain type names the direct contract already; the gate still holds.

    So the carve-out cannot be turned into a way for any Claim to cover itself
    with a copy of the value it just authored -- the case the maintainer named.
    """

    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()

    evidence = _verdict(
        instance,
        base,
        capture=_direct_selection_capture(instance, base),
        contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        claim_type=_claim_type(),
        name="direct-copy",
    )

    assert evidence.initial_verdict == "uncovered"
    assert evidence.evidence_basis == ("origin_only",)

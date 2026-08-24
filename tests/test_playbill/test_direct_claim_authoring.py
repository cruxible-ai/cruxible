"""PC-B one-call Claim authoring and native read surfaces."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    CaptureFormatError,
    DirectByteSpanSelectionV1,
    DirectExternalSelectionV1,
    DirectForeignSourceSelectionV1,
    build_direct_claim_selection_capture,
    capture_contract_digest,
    capture_contract_is_self_asserted,
    capture_contract_path,
    evaluate_capture_contract_law,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_types import claim_type_digest
from cruxible_client.contracts.claims import (
    ClaimStatement,
    LiteralClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.descriptor_claim_types import descriptor_claim_type
from cruxible_client.contracts.discovery import ExpandRequestV1
from cruxible_client.contracts.errors import (
    ClaimNotFoundError,
    ProposalIntegrityError,
    SettlementIntegrityError,
)
from cruxible_client.contracts.policies import (
    ActorRequirementV1,
    ClaimAdmissionPolicyV1,
    FreezeRequirementV1,
    TransitionRequirementV1,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_client.contracts.source_references import EvidenceCommitmentV1, OpenSourceRequestV1
from cruxible_client.contracts.subjects import subject_digest
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.service.playbill_claims import (
    DirectClaimAuthoringV1,
    ExistingStatementHandoffV1,
    service_expand_playbill_semantic,
    service_explain_playbill_claim,
    service_get_playbill_claim,
    service_list_playbill_claims,
    service_open_playbill_source,
    service_playbill_claim_history,
    service_propose_playbill_claim,
    service_query_playbill_claims,
)
from tests.test_playbill._support import FIXED_TIMESTAMP, generate_client, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim_type, _subject

TIMESTAMP = "2026-08-16T20:00:00.000000Z"


def _authoring() -> DirectClaimAuthoringV1:
    shell = _subject()
    claim_type = _claim_type()
    return DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(
                f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
            ),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        rationale="The review inputs are complete.",
        subject_shell=shell,
        claim_type_artifact=claim_type,
    )


def _instance_with_reviewer(tmp_path: Path):
    managed = tmp_path / "managed"
    owner = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )
    reviewer = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="reviewer",
        roles=("reviewer",),
    )
    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_claim_policy_test",
        client_principals=(owner.principal, reviewer.principal),
        workspace_roots=(tmp_path / "workspace",),
        timestamp=FIXED_TIMESTAMP,
    )
    return instance, owner, reviewer


def test_direct_authoring_refuses_caller_authored_observed_at() -> None:
    payload = _authoring().model_dump(mode="json")
    payload["observed_at"] = TIMESTAMP
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DirectClaimAuthoringV1.model_validate(payload)


def test_subject_level_freeze_policy_refuses_an_adjacent_claim_change(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    status_type = _claim_type().model_copy(
        update={
            "admission_policy": ClaimAdmissionPolicyV1(
                freeze_requirements=(
                    FreezeRequirementV1(
                        requirement_id="ready-freeze",
                        while_predicate="project.work_item.status",
                        while_values=("ready",),
                        frozen_predicates=("project.work_item.summary",),
                    ),
                )
            )
        }
    )
    summary_type = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(
                kind="ClaimType",
                name="project.work_item.summary",
            ),
            "predicate": "project.work_item.summary",
            "literal_schema": {"type": "string"},
        }
    )
    ready = service_propose_playbill_claim(
        instance,
        authoring=_authoring().model_copy(
            update={
                "statement": _authoring().statement.model_copy(
                    update={
                        "claim_type_digest": claim_type_digest(status_type).tagged,
                    }
                ),
                "claim_type_artifact": status_type,
                "dependency_claim_types": (summary_type,),
            }
        ),
        actor_id="owner",
        proposal_name="ready-with-freeze",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(instance, owner, ready)

    summary = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=ClaimStatement(
                subject=_authoring().statement.subject,
                claim_type=summary_type.identity,
                claim_type_digest=claim_type_digest(summary_type).tagged,
                predicate=summary_type.predicate,
                object=LiteralClaimObject(value="A changed summary"),
                role="observation",
            ),
            rationale="Attempt to mutate a field frozen by accepted status policy.",
        ),
        actor_id="owner",
        proposal_name="summary-while-ready",
        timestamp="2026-08-16T20:01:00.000000Z",
    )
    assert summary.proposal.proposal.candidate is None
    assert tuple(item.code for item in summary.proposal.proposal.evaluation.diagnostics) == (
        "playbill.claim_policy.freeze_active",
    )


def test_claim_policy_signer_constraint_is_rechecked_at_settlement(tmp_path: Path) -> None:
    instance, owner, reviewer = _instance_with_reviewer(tmp_path)
    status_type = _claim_type().model_copy(
        update={
            "literal_schema": {"enum": ["approved", "open"], "type": "string"},
            "admission_policy": ClaimAdmissionPolicyV1(
                transition_requirements=(
                    TransitionRequirementV1(
                        requirement_id="approve-transition",
                        when_predicate="project.work_item.status",
                        from_values=("open",),
                        to_value="approved",
                        require=("reviewer-role",),
                    ),
                ),
                actor_requirements=(
                    ActorRequirementV1(
                        requirement_id="reviewer-role",
                        signer_roles=("reviewer",),
                        signer_distinct_from_lineage_creation_actor=True,
                    ),
                ),
            ),
        }
    )
    opened = service_propose_playbill_claim(
        instance,
        authoring=_authoring().model_copy(
            update={
                "statement": _authoring().statement.model_copy(
                    update={
                        "claim_type_digest": claim_type_digest(status_type).tagged,
                        "object": LiteralClaimObject(value="open"),
                    }
                ),
                "claim_type_artifact": status_type,
            }
        ),
        actor_id="owner",
        proposal_name="open-review",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(instance, owner, opened)

    approved = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=_authoring().statement.model_copy(
                update={
                    "claim_type_digest": claim_type_digest(status_type).tagged,
                    "object": LiteralClaimObject(value="approved"),
                }
            ),
            rationale="A distinct reviewer has approved this transition.",
            claim_id=opened.claim_identity.removeprefix("Claim:"),
            predecessor_artifact_digest=opened.artifact_digest,
            existing_statement_handoffs=(
                ExistingStatementHandoffV1(
                    statement_digest=opened.statement_digest,
                    disposition="not_tested",
                ),
            ),
        ),
        actor_id="owner",
        proposal_name="approve-review",
        timestamp="2026-08-16T20:01:00.000000Z",
    )
    candidate = approved.proposal.proposal.candidate
    assert candidate is not None
    admission = next(
        item.result["claim_admission"]
        for item in candidate.law_evidence
        if item.path == approved.claim_path
    )
    assert admission[0]["candidate_result"]["required_signers"][0]["roles"] == ["reviewer"]
    base = instance.accepted_coordinate()
    evaluated_oid = approved.proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    with pytest.raises(SettlementIntegrityError, match="signer constraints are unsatisfied"):
        instance.prepare_generation(
            base=base,
            candidate_tree=instance.proposal_tree(evaluated_oid),
            candidate=candidate,
            approvals=(_sign(owner, candidate.candidate_digest, base.semantic_root),),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            sequence=2,
        )
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(
            _sign(owner, candidate.candidate_digest, base.semantic_root),
            _sign(reviewer, candidate.candidate_digest, base.semantic_root),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=2,
    )
    assert tuple(item.signer_id for item in bundle.approvals) == ("owner", "reviewer")


def test_one_call_claim_proposal_activation_query_history_explain_and_source(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    proposed = service_propose_playbill_claim(
        instance,
        authoring=_authoring(),
        actor_id="owner",
        proposal_name="direct-ready",
        timestamp=TIMESTAMP,
    )
    candidate = proposed.proposal.proposal.candidate
    assert candidate is not None
    assert proposed.observed_at.isoformat() == "2026-08-16T20:00:00+00:00"
    assert not proposed.existing_statements
    evaluated_oid = proposed.proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(_sign(owner, candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=1,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    view = service_get_playbill_claim(instance, identity=proposed.claim_identity, at=accepted)
    assert view.tag == "playbill-claim-read-v2"
    assert view.admission_evaluation_time.isoformat() == "2026-08-16T20:00:00+00:00"
    assert len(view.admission_accounts) == 1
    assert view.admission_accounts[0].status == "admitted"
    assert view.admission_accounts[0].decisions[0].rule_id == "direct-self-asserted"
    assert view.envelope["artifact_digest"] == proposed.artifact_digest
    bare_identity = proposed.claim_identity.removeprefix("Claim:")
    assert service_get_playbill_claim(instance, identity=bare_identity, at=accepted) == view
    assert tuple(
        item.envelope["identity"] for item in service_list_playbill_claims(instance).claims
    ) == (proposed.claim_identity,)
    query = service_query_playbill_claims(
        instance,
        subject=_authoring().statement.subject,
        predicate=_authoring().statement.predicate,
    )
    assert query.status == "resolved"
    assert query.selected_claim_identities == (proposed.claim_identity,)
    history = service_playbill_claim_history(instance, identity=proposed.claim_identity)
    assert history.entries[0].statement_digest == proposed.statement_digest

    explanation = service_explain_playbill_claim(instance, identity=proposed.claim_identity)
    assert explanation.tag == "playbill-claim-explanation-v2"
    assert explanation.admission_accounts == view.admission_accounts
    assert (
        service_explain_playbill_claim(instance, identity=bare_identity).claim == explanation.claim
    )
    assert explanation.law_evidence.initial_verdict == "supported"
    assert explanation.approval_coverage == "containing_change_set"
    assert len(explanation.source_handles) == 1
    opened = service_open_playbill_source(
        instance,
        request=OpenSourceRequestV1(
            source_handle=explanation.source_handles[0],
            resource_budget_bytes=64 * 1024,
        ),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert opened.status == "verified"
    assert opened.commitment_verified

    with pytest.raises(ClaimNotFoundError) as missing:
        service_get_playbill_claim(instance, identity="CLM-" + "f" * 32)
    assert "Claim:CLM-<32 lowercase hex> or CLM-<32 lowercase hex>" in str(missing.value)

    claim_capsule = service_expand_playbill_semantic(
        instance,
        request=ExpandRequestV1(
            address=SemanticAddress.claim_statement(proposed.claim_path),
            at=accepted,
            evaluation_time=TIMESTAMP,
            facets=(
                "claim_context",
                "governance",
                "provenance",
                "relations",
                "sources",
                "summary",
            ),
        ),
    )
    assert claim_capsule.attestation_coverage == "containing_change_set"
    assert claim_capsule.claim_context is not None
    assert claim_capsule.next_reads[0].operation == "open_source"
    subject_capsule = service_expand_playbill_semantic(
        instance,
        request=ExpandRequestV1(
            address=_authoring().statement.subject,
            at=accepted,
            evaluation_time=TIMESTAMP,
            facets=("governance", "profile", "relations", "summary"),
        ),
    )
    # PC-F formalized the ad-hoc profile dict as the SubjectProfileV1 projection:
    # coordinate-pure structure, one row per predicate, no verdict without a time.
    profile = subject_capsule.subject_profile
    assert isinstance(profile, dict)
    assert profile["tag"] == "playbill-subject-profile-v1"
    assert profile["verdict_relative"] is False
    assert profile["evaluation_time"] is None
    assert [row["predicate"] for row in profile["predicates"]] == ["project.work_item.status"]
    assert profile["predicates"][0]["claim_count"] == 1
    assert profile["predicates"][0]["resolution"] == "single"
    assert profile["predicates"][0]["verdict"] is None
    claim_type_capsule = service_expand_playbill_semantic(
        instance,
        request=ExpandRequestV1(
            address=SemanticAddress.whole_artifact("claim-types/project.work_item/status.yaml"),
            at=accepted,
            evaluation_time=TIMESTAMP,
            facets=("claim_type_card", "governance", "summary"),
        ),
    )
    card = claim_type_capsule.claim_type_card
    assert isinstance(card, dict)
    assert card["tag"] == "playbill-claim-type-card-v1"
    assert card["predicate"] == "project.work_item.status"
    assert card["usage"]["claim_count"] == 1
    # Policies travel as digests plus one-line facts, never as policy bodies.
    assert [item["policy"] for item in card["policies"]] == [
        "admission",
        "evidence_admission",
        "resolution",
    ]


def test_non_materialized_direct_source_stays_attested_only(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    authoring = _authoring().model_copy(update={"materialize_source": False})
    proposed = service_propose_playbill_claim(
        instance,
        authoring=authoring,
        actor_id="owner",
        proposal_name="direct-no-body",
        timestamp=TIMESTAMP,
    )
    assert proposed.proposal.proposal.candidate is not None
    assert instance.body_store().verify(proposed.capture_digest)
    assert proposed.observed_at == datetime.fromisoformat("2026-08-16T20:00:00+00:00")


def _activate_direct_claim(
    instance: object,
    owner: object,
    proposed: object,
    *,
    sequence: int = 1,
) -> None:
    base = instance.accepted_coordinate()
    candidate = proposed.proposal.proposal.candidate
    assert candidate is not None
    evaluated_oid = proposed.proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(_sign(owner, candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=sequence,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def test_one_claim_from_exact_cas_span_does_not_require_document_artifact(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    source = b"status: ready\n"
    stored = instance.body_store().store(source)
    selection = DirectByteSpanSelectionV1(
        span=ContentSpan(
            content_digest=stored.digest,
            start_byte=8,
            end_byte=13,
        ),
        media_type="text/plain",
    )
    proposed = service_propose_playbill_claim(
        instance,
        authoring=_authoring().model_copy(
            update={"materialize_source": False, "source_selection": selection}
        ),
        actor_id="owner",
        proposal_name="ready-from-span",
        timestamp=TIMESTAMP,
    )
    assert len(proposed.capture_digests) == 2
    evaluated_oid = proposed.proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    assert not any(path.startswith("documents/") for path in instance.proposal_tree(evaluated_oid))
    _activate_direct_claim(instance, owner, proposed)
    explanation = service_explain_playbill_claim(instance, identity=proposed.claim_identity)
    selected = next(handle for handle in explanation.source_handles if handle.source.kind == "cas")
    assert selected.exact_spans == (selection.span,)
    opened = service_open_playbill_source(
        instance,
        request=OpenSourceRequestV1(
            source_handle=selected,
            resource_budget_bytes=len(source),
        ),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert opened.status == "verified"


def test_typed_external_selection_is_retained_as_attested_metadata(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    selection = DirectExternalSelectionV1(
        logical_source_identity="commerce.production.orders",
        coordinate_type="postgres-lsn-v1",
        coordinate="0/16B6C50",
        selector_type="relation-primary-key-v1",
        selector={"key": {"order_id": "ord-482"}, "relation": "orders"},
        commitment=EvidenceCommitmentV1(
            digest_kind="canonical_value",
            digest="sha256:" + "ab" * 32,
            materialization="none",
        ),
    )
    proposed = service_propose_playbill_claim(
        instance,
        authoring=_authoring().model_copy(update={"source_selection": selection}),
        actor_id="owner",
        proposal_name="ready-from-order",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(instance, owner, proposed)
    explanation = service_explain_playbill_claim(instance, identity=proposed.claim_identity)
    external = next(
        handle for handle in explanation.source_handles if handle.source.kind == "external"
    )
    assert external.source.coordinate == {
        "logical_source_identity": "commerce.production.orders",
        "source_coordinate": "0/16B6C50",
        "source_coordinate_type": "postgres-lsn-v1",
    }
    assert external.source.selector == {
        "claim_id": proposed.claim_identity.removeprefix("Claim:"),
        "source_selector": {
            "key": {"order_id": "ord-482"},
            "relation": "orders",
        },
        "source_selector_type": "relation-primary-key-v1",
    }
    opened = service_open_playbill_source(
        instance,
        request=OpenSourceRequestV1(
            source_handle=external,
            resource_budget_bytes=4096,
        ),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert opened.status == "attested_only"
    assert opened.coverage.reason_codes == ("external_attested_only",)


def test_foreign_source_contract_satisfies_the_unexempted_capture_component_laws() -> None:
    """The per-source contract earns acceptance; it does not inherit an exemption.

    `DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT` is exempted from the component and
    rule registry checks because it *is* the built-in constant. A foreign-source
    contract is an ordinary artifact and passes both checks on its own, which is
    only possible because reviewed code registered honestly-named components for
    it -- a proposer-asserted provenance rule rather than the daemon-fetched one.
    """

    contract = foreign_source_capture_contract("corpus.handbook")

    law = evaluate_capture_contract_law(
        contract,
        path=capture_contract_path(contract.identity.name),
        actor_roles=("owner",),
        predecessor=None,
    )

    assert law.verdict == "accepted"
    assert law.artifact_digest == capture_contract_digest(contract).tagged
    assert law.required_tier == "governed_write"
    assert contract.logical_source_identities == ("corpus.handbook",)
    assert contract.allowed_source_kinds == ("external",)
    assert contract.evidence_kinds == ("self_asserted",)
    # The grade is read off the declared rule, so proposer-supplied bytes can
    # never be reported as though a daemon had fetched them.
    assert capture_contract_is_self_asserted(contract) is True


def test_foreign_source_identity_that_cannot_be_path_addressed_is_refused() -> None:
    with pytest.raises(ValidationError):
        DirectForeignSourceSelectionV1(
            logical_source_identity="Corpus.Handbook",
            span=ContentSpan(content_digest="sha256:" + "ab" * 32, start_byte=0, end_byte=4),
        )
    with pytest.raises(ValidationError):
        DirectForeignSourceSelectionV1(
            logical_source_identity="c" * 250,
            span=ContentSpan(content_digest="sha256:" + "ab" * 32, start_byte=0, end_byte=4),
        )


def test_direct_selection_builder_refuses_a_logical_source_selection(tmp_path: Path) -> None:
    """The direct contract declares one logical source and it is not a corpus file."""

    instance, _ = initialize_local(tmp_path)
    stored = instance.body_store().store(b"status: ready\n")

    with pytest.raises(CaptureFormatError):
        build_direct_claim_selection_capture(
            store=instance.body_store(),
            actor_id="owner",
            claim_id="CLM-" + "0" * 32,
            rationale="never signed under the wrong contract",
            observed_at=datetime.fromisoformat("2026-08-16T20:00:00+00:00"),
            accepted_coordinate=instance.accepted_coordinate(),
            selection=DirectForeignSourceSelectionV1(  # type: ignore[arg-type]
                logical_source_identity="corpus.handbook",
                span=ContentSpan(
                    content_digest=stored.digest,
                    start_byte=0,
                    end_byte=6,
                ),
            ),
        )


def test_foreign_source_selection_commits_to_the_selected_span_under_its_own_contract(
    tmp_path: Path,
) -> None:
    """One authored Claim, two Captures, and only one of them names a source."""

    instance, owner = initialize_local(tmp_path)
    presented = b"# handbook\n\nstatus: ready\n"
    stored = instance.body_store().store(presented)
    start = presented.index(b"status: ready\n")
    selection = DirectForeignSourceSelectionV1(
        logical_source_identity="corpus.handbook",
        span=ContentSpan(
            content_digest=stored.digest,
            start_byte=start,
            end_byte=start + len(b"status: ready\n"),
        ),
        media_type="text/markdown",
    )

    proposed = service_propose_playbill_claim(
        instance,
        authoring=_authoring().model_copy(update={"source_selection": selection}),
        actor_id="owner",
        proposal_name="ready-from-corpus",
        timestamp=TIMESTAMP,
    )
    evaluated_oid = proposed.proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    contract = foreign_source_capture_contract("corpus.handbook")
    assert capture_contract_path(contract.identity.name) in instance.proposal_tree(evaluated_oid)

    _activate_direct_claim(instance, owner, proposed)
    explanation = service_explain_playbill_claim(instance, identity=proposed.claim_identity)
    external = next(
        handle for handle in explanation.source_handles if handle.source.kind == "external"
    )

    # The coordinate names the presented snapshot; the selector names the window
    # inside it; the commitment is over the selected bytes alone.
    assert external.source.source_identity == "corpus.handbook"
    assert external.source.coordinate == {
        "source_byte_length": len(presented),
        "source_content_digest": stored.digest,
    }
    assert external.source.selector == {
        "claim_id": proposed.claim_identity.removeprefix("Claim:"),
        "end_byte": start + len(b"status: ready\n"),
        "start_byte": start,
    }
    assert external.source.replayability == "attested_only"
    assert external.commitment.digest_kind == "exact_bytes"
    assert external.commitment.byte_length == len(b"status: ready\n")
    assert external.commitment.materialization == "cas"
    # Dereference asks whether the *source* can be re-read, and it cannot: the
    # proposer presented these bytes and the daemon can reach `corpus.handbook`
    # never. The retained material is what coverage matches against; it is not a
    # licence to report a replay that did not happen.
    opened = service_open_playbill_source(
        instance,
        request=OpenSourceRequestV1(
            source_handle=external,
            resource_budget_bytes=len(presented),
        ),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert opened.status == "attested_only"
    assert opened.coverage.reason_codes == ("external_attested_only",)
    grades = {
        item.capture_digest: item.provenance_grade
        for item in explanation.law_evidence.verdict_captures
    }
    assert set(grades.values()) == {"self-asserted"}


def test_a_second_claim_against_an_accepted_foreign_source_reuses_its_contract(
    tmp_path: Path,
) -> None:
    """Governing one source repeatedly is the ordinary case, not a succession.

    The per-source contract is written by whichever authoring reaches the source
    first. A later authoring writes byte-identical content, so the contract is
    not a changed member at all -- which is what keeps a growing corpus from
    needing a CaptureContract succession per file.
    """

    instance, owner = initialize_local(tmp_path)
    presented = b"# handbook\n\nstatus: ready\nstatus: blocked\n"
    stored = instance.body_store().store(presented)
    contract = foreign_source_capture_contract("corpus.handbook")
    contract_path = capture_contract_path(contract.identity.name)

    def _authoring_for(window: bytes) -> DirectClaimAuthoringV1:
        start = presented.index(window)
        return _authoring().model_copy(
            update={
                "source_selection": DirectForeignSourceSelectionV1(
                    logical_source_identity="corpus.handbook",
                    span=ContentSpan(
                        content_digest=stored.digest,
                        start_byte=start,
                        end_byte=start + len(window),
                    ),
                )
            }
        )

    first = service_propose_playbill_claim(
        instance,
        authoring=_authoring_for(b"status: ready\n"),
        actor_id="owner",
        proposal_name="first-corpus-claim",
        timestamp=TIMESTAMP,
    )
    first_oid = first.proposal.proposal.evaluation.evaluated_tree_oid
    assert first_oid is not None
    assert contract_path in instance.proposal_tree(first_oid)
    _activate_direct_claim(instance, owner, first)

    second = service_propose_playbill_claim(
        instance,
        authoring=_authoring_for(b"status: blocked\n").model_copy(
            update={
                "existing_statement_handoffs": (
                    ExistingStatementHandoffV1(
                        statement_digest=first.statement_digest,
                        disposition="not_tested",
                    ),
                ),
            }
        ),
        actor_id="owner",
        proposal_name="second-corpus-claim",
        timestamp=TIMESTAMP,
    )

    second_oid = second.proposal.proposal.evaluation.evaluated_tree_oid
    assert second_oid is not None
    assert contract_path in instance.proposal_tree(second_oid)
    _activate_direct_claim(instance, owner, second, sequence=2)

    # The contract is present in the accepted tree and absent from the second
    # change set: identical bytes are not a change, so no succession law is ever
    # consulted and no CaptureContract predecessor has to be tracked.
    record = instance.accepted_history()[-1].record
    assert record is not None
    assert contract_path not in {member.path for member in record.members}

    explanation = service_explain_playbill_claim(instance, identity=second.claim_identity)
    external = next(
        handle for handle in explanation.source_handles if handle.source.kind == "external"
    )
    assert external.source.source_identity == "corpus.handbook"


def test_competing_claims_require_handoffs_report_conflict_and_preserve_lineage(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    first = service_propose_playbill_claim(
        instance,
        authoring=_authoring(),
        actor_id="owner",
        proposal_name="ready-first",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(instance, owner, first)

    blocked_statement = _authoring().statement.model_copy(
        update={"object": LiteralClaimObject(value="blocked")}
    )
    competing = _authoring().model_copy(update={"statement": blocked_statement})
    with pytest.raises(ProposalIntegrityError, match="explicitly disposition every existing"):
        service_propose_playbill_claim(
            instance,
            authoring=competing,
            actor_id="owner",
            proposal_name="blocked-without-handoff",
            timestamp="2026-08-16T20:01:00.000000Z",
        )
    competing = competing.model_copy(
        update={
            "existing_statement_handoffs": (
                ExistingStatementHandoffV1(
                    statement_digest=first.statement_digest,
                    disposition="contradict",
                ),
            )
        }
    )
    second = service_propose_playbill_claim(
        instance,
        authoring=competing,
        actor_id="owner",
        proposal_name="blocked-second",
        timestamp="2026-08-16T20:01:00.000000Z",
    )
    assert tuple(item.statement_digest for item in second.existing_statements) == (
        first.statement_digest,
    )
    _activate_direct_claim(instance, owner, second, sequence=2)
    conflict = service_query_playbill_claims(
        instance,
        subject=_authoring().statement.subject,
        predicate=_authoring().statement.predicate,
    )
    assert conflict.status == "unresolved"
    assert set(conflict.contender_claim_identities) == {
        first.claim_identity,
        second.claim_identity,
    }

    handoffs = tuple(
        sorted(
            (
                ExistingStatementHandoffV1(
                    statement_digest=first.statement_digest,
                    disposition="support",
                ),
                ExistingStatementHandoffV1(
                    statement_digest=second.statement_digest,
                    disposition="contradict",
                ),
            ),
            key=lambda item: item.statement_digest.encode("ascii"),
        )
    )
    successor = service_propose_playbill_claim(
        instance,
        authoring=_authoring().model_copy(
            update={
                "claim_id": first.claim_identity.removeprefix("Claim:"),
                "predecessor_artifact_digest": first.artifact_digest,
                "existing_statement_handoffs": handoffs,
            }
        ),
        actor_id="owner",
        proposal_name="ready-stronger-backing",
        timestamp="2026-08-16T20:02:00.000000Z",
    )
    assert successor.statement_digest == first.statement_digest
    assert successor.artifact_digest != first.artifact_digest
    assert first.capture_digest in successor.capture_digests
    _activate_direct_claim(instance, owner, successor, sequence=3)
    history = service_playbill_claim_history(instance, identity=first.claim_identity)
    assert [entry.sequence for entry in history.entries] == [1, 3]
    assert history.entries[-1].predecessor_digest == first.artifact_digest


def test_governed_alias_is_an_ordinary_claim_without_mutating_target_digest(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    shell = _subject()
    target_digest = subject_digest(shell).tagged
    alias_type = descriptor_claim_type("semantic.alias")
    proposed = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=ClaimStatement(
                subject=SemanticAddress.whole_artifact(
                    f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
                ),
                claim_type=alias_type.identity,
                claim_type_digest=claim_type_digest(alias_type).tagged,
                predicate=alias_type.predicate,
                object=LiteralClaimObject(value="review-ready work item"),
                role="normative",
            ),
            rationale="This is the accepted alternate expression used by the team.",
            subject_shell=shell,
            claim_type_artifact=alias_type,
        ),
        actor_id="owner",
        proposal_name="alias-work-item",
        timestamp=TIMESTAMP,
    )
    candidate = proposed.proposal.proposal.candidate
    assert candidate is not None
    assert target_digest == subject_digest(shell).tagged
    assert any(member.artifact_kind == "claim" for member in candidate.members)
    _activate_direct_claim(instance, owner, proposed)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    expanded = service_expand_playbill_semantic(
        instance,
        request=ExpandRequestV1(
            address=SemanticAddress.whole_artifact(
                f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
            ),
            at=accepted,
            evaluation_time=TIMESTAMP,
            facets=("relations", "summary"),
        ),
    )
    assert expanded.relations[0]["predicate"] == "semantic.alias"

    proposed_statement = ClaimStatement(
        subject=SemanticAddress.whole_artifact(
            f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
        ),
        claim_type=alias_type.identity,
        claim_type_digest=claim_type_digest(alias_type).tagged,
        predicate=alias_type.predicate,
        object=LiteralClaimObject(value="review-ready work item"),
        role="normative",
    )
    retired = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=proposed_statement,
            rationale="Retire the alternate expression from discovery.",
            claim_id=proposed.claim_identity.removeprefix("Claim:"),
            predecessor_artifact_digest=proposed.artifact_digest,
            retire=True,
            subject_shell=shell,
            claim_type_artifact=alias_type,
            existing_statement_handoffs=(
                ExistingStatementHandoffV1(
                    statement_digest=proposed.statement_digest,
                    disposition="not_tested",
                ),
            ),
        ),
        actor_id="owner",
        proposal_name="retire-alias-work-item",
        timestamp="2026-08-16T20:04:00.000000Z",
    )
    assert proposed_statement.predicate == "semantic.alias"
    _activate_direct_claim(instance, owner, retired, sequence=2)
    rebuilt = service_expand_playbill_semantic(
        instance,
        request=ExpandRequestV1(
            address=SemanticAddress.whole_artifact(
                f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
            ),
            at=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time="2026-08-16T20:04:00.000000Z",
            facets=("relations", "summary"),
        ),
    )
    assert rebuilt.relations == ()
    assert target_digest == subject_digest(shell).tagged


def test_new_distinct_atomically_persists_the_exact_relation_for_near_claim_type(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    first = service_propose_playbill_claim(
        instance,
        authoring=_authoring(),
        actor_id="owner",
        proposal_name="install-status",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(instance, owner, first)

    state_type = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(
                kind="ClaimType",
                name="project.work_item.state",
            ),
            "predicate": "project.work_item.state",
        }
    )
    distinct_type = descriptor_claim_type("semantic.distinct_from")
    proposed = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=ClaimStatement(
                subject=SemanticAddress.whole_artifact("claim-types/project.work_item/state.yaml"),
                claim_type=distinct_type.identity,
                claim_type_digest=claim_type_digest(distinct_type).tagged,
                predicate=distinct_type.predicate,
                object=SubjectClaimObject(
                    address=SemanticAddress.whole_artifact(
                        "claim-types/project.work_item/status.yaml"
                    )
                ),
                role="normative",
            ),
            rationale="State is the broad lifecycle concept; status is its current value.",
            claim_type_artifact=distinct_type,
            dependency_claim_types=(state_type,),
        ),
        actor_id="owner",
        proposal_name="state-distinct-from-status",
        timestamp="2026-08-16T20:03:00.000000Z",
    )
    candidate = proposed.proposal.proposal.candidate
    assert candidate is not None
    reuse = next(
        item.result["reuse"]
        for item in candidate.law_evidence
        if item.path == "claim-types/project.work_item/state.yaml"
    )
    assert reuse["verdict"] == "satisfied"
    assert reuse["distinct_relation_members"] == [
        {
            "claim_address": SemanticAddress.claim_statement(proposed.claim_path).model_dump(
                mode="json"
            ),
            "claim_artifact_digest": proposed.artifact_digest,
            "subject": SemanticAddress.whole_artifact(
                "claim-types/project.work_item/state.yaml"
            ).model_dump(mode="json"),
            "object": SemanticAddress.whole_artifact(
                "claim-types/project.work_item/status.yaml"
            ).model_dump(mode="json"),
        }
    ]

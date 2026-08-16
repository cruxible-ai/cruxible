from __future__ import annotations

from datetime import timedelta

from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.captures import capture_contract_digest
from cruxible_core.playbill.claim_types import claim_type_digest
from cruxible_core.playbill.providers import provider_digest
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.standing_mandates import (
    MandateGrantV1,
    MandateInvocationV1,
    MandateRuntimeCapV1,
    StandingMandate,
    evaluate_standing_mandate,
)
from tests.test_playbill._pc_c_support import NOW, capture_contract, digest, provider
from tests.test_playbill.test_claims import _claim_type


def _mandate(*, settlement: str = "settle_named_deltas") -> StandingMandate:
    contract = capture_contract()
    provider_artifact = provider(contract)
    claim_type = _claim_type()
    subject = SemanticAddress.whole_artifact("subjects/project.work_item/wi-42.yaml")
    operations = (
        ("compile_capture", "propose_change_set")
        if settlement == "propose_only"
        else ("activate_change_set", "compile_capture", "propose_change_set")
    )
    pins = (
        ArtifactPin(
            role="capture-contract",
            target=contract.identity,
            artifact_digest=capture_contract_digest(contract).tagged,
        ),
        ArtifactPin(
            role="claim-type",
            target=claim_type.identity,
            artifact_digest=claim_type_digest(claim_type).tagged,
        ),
        ArtifactPin(
            role="provider",
            target=provider_artifact.identity,
            artifact_digest=provider_digest(provider_artifact).tagged,
        ),
    )
    return StandingMandate(
        identity=ArtifactIdentity(kind="StandingMandate", name="refresh-work-item"),
        provider=provider_artifact.identity,
        capture_contract_digest=capture_contract_digest(contract).tagged,
        claim_type_scope=(claim_type.identity,),
        subject_scope=(subject,),
        permitted_delta_classes=("claim.backing_refresh",),
        authority_ceiling=MandateGrantV1(
            settlement=settlement,  # type: ignore[arg-type]
            permitted_operations=operations,
        ),
        valid_from=NOW,
        valid_until=NOW + timedelta(days=30),
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        pins=tuple(sorted(pins, key=lambda item: (item.role, item.target.qualified))),
    )


def _invocation(mandate: StandingMandate, *, operation: str):
    assert mandate.subject_scope is not None
    return MandateInvocationV1(
        provider=mandate.provider,
        capture_contract_digest=mandate.capture_contract_digest,
        claim_type=mandate.claim_type_scope[0],
        subject=mandate.subject_scope[0],
        delta_class="claim.backing_refresh",
        operation=operation,  # type: ignore[arg-type]
        evaluation_time=NOW + timedelta(days=1),
        accepted_authority_digest=(
            digest("authority", "accepted") if operation == "activate_change_set" else None
        ),
    )


def test_named_delta_activation_requires_settlement_and_accepted_authority() -> None:
    mandate = _mandate()
    allowed = evaluate_standing_mandate(
        mandate,
        _invocation(mandate, operation="activate_change_set"),
    )
    assert allowed.verdict == "permitted"
    missing_authority = _invocation(mandate, operation="activate_change_set").model_copy(
        update={"accepted_authority_digest": None}
    )
    refused = evaluate_standing_mandate(mandate, missing_authority)
    assert refused.refusal_codes == ("playbill.mandate.accepted_authority_missing",)


def test_propose_only_and_scope_expiry_fail_closed() -> None:
    mandate = _mandate(settlement="propose_only")
    proposal = evaluate_standing_mandate(
        mandate,
        _invocation(mandate, operation="propose_change_set"),
    )
    assert proposal.verdict == "permitted"
    out_of_scope = _invocation(mandate, operation="propose_change_set").model_copy(
        update={
            "claim_type": ArtifactIdentity(kind="ClaimType", name="other.predicate"),
            "evaluation_time": NOW + timedelta(days=31),
        }
    )
    refused = evaluate_standing_mandate(mandate, out_of_scope)
    assert refused.verdict == "refused"
    assert set(refused.refusal_codes) == {
        "playbill.mandate.claim_type_out_of_scope",
        "playbill.mandate.expired",
    }


def test_runtime_caps_can_narrow_but_never_widen_mandate() -> None:
    mandate = _mandate()
    invocation = _invocation(mandate, operation="activate_change_set")
    narrowed = evaluate_standing_mandate(
        mandate,
        invocation,
        runtime_caps=(
            MandateRuntimeCapV1(
                cap_kind="safety",
                permitted_operations=("compile_capture",),
            ),
        ),
    )
    assert narrowed.verdict == "refused"
    assert narrowed.refusal_codes == ("playbill.mandate.safety_operation_capped",)

    propose_only = _mandate(settlement="propose_only")
    attempted_widening = evaluate_standing_mandate(
        propose_only,
        _invocation(propose_only, operation="activate_change_set").model_copy(
            update={"accepted_authority_digest": digest("authority", "accepted")}
        ),
        runtime_caps=(
            MandateRuntimeCapV1(
                cap_kind="calibration",
                permitted_operations=("activate_change_set",),
            ),
        ),
    )
    assert attempted_widening.verdict == "refused"
    assert {
        "playbill.mandate.operation_out_of_scope",
        "playbill.mandate.propose_only",
    }.issubset(attempted_widening.refusal_codes)

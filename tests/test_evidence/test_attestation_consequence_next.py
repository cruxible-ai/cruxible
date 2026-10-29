"""ClaimType-v4 attestation consequences are queue-only mechanical facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationStatementV1,
    VerifiedClaimAttestationV1,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicy,
    ClaimAttestationConsequenceRule,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import claim_path, claim_statement_digest
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.service.claims.claims import (
    _claim_from_view,
    _claim_law_evidence,
    service_list_playbill_claims,
)
from cruxible_core.service.discovery.next import (
    NextRequest,
    service_playbill_next,
)
from cruxible_core.service.evidence.claim_attestations import service_append_claim_attestation
from tests.core_support._adoption_fixture import _Builder
from tests.core_support._claim_authoring_support import service_propose_playbill_claim
from tests.core_support._knowledge_loop_support import activate, authoring, subject_shell
from tests.core_support._support import initialize_local
from tests.test_claims.test_claim_attestation_service import _request
from tests.test_claims.test_claims import _claim_type

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)


def _access() -> CoverageAccessProfile:
    return CoverageAccessProfile(
        profile_id="attestation-consequence-test",
        permitted_access_classes=("instance", "public"),
    )


def _verified(
    instance,  # type: ignore[no-untyped-def]
    claim,  # type: ignore[no-untyped-def]
    *,
    suffix: str,
    control_domain: str,
    principal_name: str | None = None,
    signer_kind: Literal["Principal", "Provider"] = "Principal",
    observed_at: datetime = EVALUATION_TIME - timedelta(minutes=5),
    valid_until: datetime | None = None,
    subject_content_digest: str | None = None,
) -> VerifiedClaimAttestationV1:
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    statement = ClaimAttestationStatementV1(
        instance_id=instance.descriptor.instance_id,
        referent_coordinate=coordinate,
        subject=claim.statement.subject,
        subject_content_digest=(
            subject_content_digest or claim.backing.referent_context.subject_content_digest
        ),
        object_subject=None,
        object_content_digest=claim.backing.referent_context.object_content_digest,
        claim_statement_digest=claim_statement_digest(claim.statement).tagged,
        stance="unsure",
        provider_or_principal=ArtifactIdentity(
            kind=signer_kind,
            name=principal_name or f"reviewer-{suffix}",
        ),
        signing_key_id="sha256:" + suffix * 64,
        capture_digests=(),
        observed_at=observed_at,
        valid_until=valid_until,
    )
    return VerifiedClaimAttestationV1(
        attestation_digest="sha256:" + suffix * 64,
        statement=statement,
        attestation_grade=(
            "verified_principal" if signer_kind == "Principal" else "verified_provider"
        ),
        control_domain=control_domain,
        coverage="exact_subject",
        current=True,
    )


def threshold_world(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    minimum: int = 2,
    attestation_mutator=None,  # type: ignore[no-untyped-def]
):
    from cruxible_client.contracts.captures import builtin_contract_for_write

    instance, owner = initialize_local(root)
    contract = builtin_contract_for_write(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, None)
    base_type = _claim_type(contract=contract)
    claim_type = base_type.model_copy(
        update={
            "artifact_format": "playbill-claim-type-v4",
            "attestation_consequence_policy": ClaimAttestationConsequencePolicy(
                rules=(
                    ClaimAttestationConsequenceRule(
                        rule_id="two-independent-unsure",
                        stance="unsure",
                        minimum_independent_control_components=minimum,
                    ),
                )
            ),
        }
    )
    shell = subject_shell("wi-42")
    _Builder(instance, owner).accept(
        {
            subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
            claim_type_path(claim_type.predicate): render_claim_type(claim_type),
            capture_contract_path(contract.identity.name): (render_capture_contract(contract)),
        },
        phase="attestation-consequence-dependencies",
    )
    instance.refresh()
    proposed = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "ready", with_claim_type=False).model_copy(
            update={
                "statement": authoring(
                    "wi-42", "ready", with_claim_type=False
                ).statement.model_copy(
                    update={"claim_type_digest": claim_type_digest(claim_type).tagged}
                )
            }
        ),
        actor_id="owner",
        proposal_name="attestation-consequence-claim",
        timestamp="2026-08-24T17:00:02.000000Z",
    )
    activate(instance, owner, proposed)
    claim = _claim_from_view(service_list_playbill_claims(instance).claims[0])
    law = _claim_law_evidence(
        instance,
        path=claim_path(claim.identity.name),
        at=instance.accepted_coordinate(),
    )
    attestations = (
        _verified(instance, claim, suffix="a", control_domain="independent-a"),
        _verified(instance, claim, suffix="b", control_domain="independent-b"),
    )
    if attestation_mutator is not None:
        attestations = attestation_mutator(instance, claim, attestations)
    patched = law.model_copy(update={"verified_attestations": attestations})
    monkeypatch.setattr(
        "cruxible_core.service.discovery.next._claim_threshold_evidence",
        lambda _instance, *, at: {claim_path(claim.identity.name): patched},
    )
    return instance, owner, claim


def accept_signed_threshold_attestations(
    instance,  # type: ignore[no-untyped-def]
    owner,  # type: ignore[no-untyped-def]
    claim,  # type: ignore[no-untyped-def]
    root: Path,
    *,
    signer_count: int = 2,
    attested_at: tuple[datetime, ...] | None = None,
    valid_until: datetime | None = None,
) -> None:
    """Accept ``signer_count`` independent principals' unsure attestations as signed artifacts.

    The bootstrap client fixture names exactly two principals (``owner`` and
    ``reviewer``), so ``signer_count`` of 0, 1, or 2 selects a prefix of
    them: zero accepts nothing, one accepts only the instance owner, and two
    (the historical default) adds the reviewer as a second independent
    control component.
    """

    from cruxible_client.contracts.accepted_attestations import (
        attestation_path,
        render_accepted_attestation,
    )
    from cruxible_client.contracts.claim_attestations import claim_attestation_v2_envelope_digest
    from tests.core_support._support import client_material
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    if signer_count == 0:
        return

    signers = (owner, client_material(root, instance))[:signer_count]
    if attested_at is None:
        attested_at = (EVALUATION_TIME,) * signer_count

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    for signer, at in zip(signers, attested_at, strict=True):
        envelope = _request(
            instance,
            signer,
            claim.identity.name,
            root,
            stance="unsure",
            attested_at=at,
            valid_until=valid_until,
        ).attestation
        tree[attestation_path(claim_attestation_v2_envelope_digest(envelope))] = (
            render_accepted_attestation(envelope)
        )
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=max(attested_at).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        proposal_name="threshold-attestations",
    )


def _rows(instance) -> tuple:  # type: ignore[no-untyped-def]
    return tuple(
        item
        for item in service_playbill_next(
            instance,
            request=NextRequest(
                evaluation_time=EVALUATION_TIME,
                access_profile=_access(),
            ),
        ).items
        if item.reason == "claim_attestation_threshold_met"
    )


@pytest.mark.parametrize(
    ("minimum", "signer_count", "expected_rows"),
    # A zero threshold disables the rule: it escalates nothing, however many sign.
    ((0, 0, 0), (0, 2, 0), (1, 1, 1), (2, 1, 0), (2, 2, 1)),
)
def test_thresholds_zero_one_and_two_count_distinct_signed_principals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    minimum: int,
    signer_count: int,
    expected_rows: int,
) -> None:
    instance, owner, claim = threshold_world(
        tmp_path,
        monkeypatch,
        minimum=minimum,
        attestation_mutator=lambda _instance, _claim, _items: (),
    )
    accept_signed_threshold_attestations(
        instance, owner, claim, tmp_path, signer_count=signer_count
    )

    rows = _rows(instance)
    assert len(rows) == expected_rows
    if rows:
        assert rows[0].detail["independent_control_component_count"] == signer_count


def test_threshold_fold_refuses_missing_law_evidence_as_typed_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner, _claim = threshold_world(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "cruxible_core.service.discovery.next._claim_threshold_evidence",
        lambda _instance, *, at: {},
    )

    with pytest.raises(
        ProposalIntegrityError,
        match="accepted Claim has no reproducible Claim law evidence",
    ):
        _rows(instance)


def test_a_consequence_policy_weighs_contradictions_itself_with_no_neutral_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, owner, claim = threshold_world(tmp_path, monkeypatch)
    service_append_claim_attestation(
        instance,
        request=_request(
            instance,
            owner,
            claim.identity.name,
            tmp_path,
            stance="contradict",
            attested_at=EVALUATION_TIME - timedelta(minutes=4),
        ),
        actor_id="owner",
        recorded_at=EVALUATION_TIME - timedelta(minutes=3),
    )

    rows = service_playbill_next(
        instance,
        request=NextRequest(evaluation_time=EVALUATION_TIME, access_profile=_access()),
    ).items
    assert not [
        item
        for item in rows
        if item.reason == "claim_contradicting_evidence_available"
        and isinstance(item.detail, dict)
        and item.detail.get("attestation_basis") == "examined_existing"
    ]

"""ClaimType-v4 attestation consequences are queue-only mechanical facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationStatement,
    VerifiedClaimAttestationV1,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicyV1,
    ClaimAttestationConsequenceRuleV1,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import claim_path, claim_statement_digest
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    _claim_law_evidence,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_evidence import service_evaluate_playbill_claim_verdict
from cruxible_core.service.playbill_next import PlaybillNextRequestV1, service_playbill_next
from tests.test_playbill._adoption_fixture import _Builder
from tests.test_playbill._claim_authoring_support import service_propose_playbill_claim
from tests.test_playbill._knowledge_loop_support import activate, authoring, subject_shell
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_claims import _claim_type

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)


def _access() -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
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
    statement = ClaimAttestationStatement(
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
    instance, owner = initialize_local(root)
    base_type = _claim_type()
    claim_type = base_type.model_copy(
        update={
            "artifact_format": "playbill-claim-type-v4",
            "attestation_consequence_policy": ClaimAttestationConsequencePolicyV1(
                rules=(
                    ClaimAttestationConsequenceRuleV1(
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
            capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
                render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
            ),
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
        "cruxible_core.service.playbill_next._claim_law_evidence",
        lambda _instance, *, path, at: patched,
    )
    return instance, owner, claim


def _rows(instance) -> tuple:  # type: ignore[no-untyped-def]
    return tuple(
        item
        for item in service_playbill_next(
            instance,
            request=PlaybillNextRequestV1(
                evaluation_time=EVALUATION_TIME,
                access_profile=_access(),
            ),
        ).items
        if item.reason == "claim_attestation_threshold_met"
    )


def test_two_distinct_current_principals_emit_one_deterministic_queue_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner, claim = threshold_world(tmp_path, monkeypatch)
    before_coordinate = instance.accepted_coordinate()
    before_verdict = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=claim.identity.qualified,
        evaluation_time=EVALUATION_TIME,
    )
    before_claims = service_list_playbill_claims(instance)

    first = _rows(instance)
    retry = _rows(instance)

    assert retry == first
    assert len(first) == 1
    row = first[0]
    assert row.repair.operation == "playbill.authoring.create"
    assert row.repair.required_change == "resolve_attestation_threshold"
    assert row.detail["independent_control_component_count"] == 2
    assert row.detail["attestation_digests"] == [
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    ]
    assert instance.accepted_coordinate() == before_coordinate
    assert service_list_playbill_claims(instance) == before_claims
    assert (
        service_evaluate_playbill_claim_verdict(
            instance,
            claim_identity=claim.identity.qualified,
            evaluation_time=EVALUATION_TIME,
        )
        == before_verdict
    )


@pytest.mark.parametrize(
    "case", ["duplicate_principal", "provider", "expired", "shell_stale", "future", "below"]
)
def test_nonqualifying_attestation_sets_are_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    def mutate(instance, claim, items):  # type: ignore[no-untyped-def]
        if case == "duplicate_principal":
            return (
                items[0],
                items[1].model_copy(
                    update={
                        "statement": items[1].statement.model_copy(
                            update={
                                "provider_or_principal": items[0].statement.provider_or_principal
                            }
                        )
                    }
                ),
            )
        if case == "provider":
            return (
                items[0],
                _verified(
                    instance,
                    claim,
                    suffix="b",
                    control_domain="independent-b",
                    signer_kind="Provider",
                ),
            )
        if case == "expired":
            return (
                items[0],
                _verified(
                    instance,
                    claim,
                    suffix="b",
                    control_domain="independent-b",
                    valid_until=EVALUATION_TIME,
                ),
            )
        if case == "shell_stale":
            return (
                items[0],
                _verified(
                    instance,
                    claim,
                    suffix="b",
                    control_domain="independent-b",
                    subject_content_digest="sha256:" + "f" * 64,
                ),
            )
        if case == "future":
            return (
                items[0],
                _verified(
                    instance,
                    claim,
                    suffix="b",
                    control_domain="independent-b",
                    observed_at=EVALUATION_TIME + timedelta(seconds=1),
                ),
            )
        return (items[0],)

    instance, _owner, _claim = threshold_world(
        tmp_path,
        monkeypatch,
        attestation_mutator=mutate,
    )

    assert _rows(instance) == ()


@pytest.mark.parametrize(
    ("minimum", "attestation_count", "expected_rows"),
    ((0, 0, 1), (1, 1, 1), (2, 1, 0), (2, 2, 1)),
)
def test_thresholds_zero_one_and_two_count_distinct_principals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    minimum: int,
    attestation_count: int,
    expected_rows: int,
) -> None:
    instance, _owner, _claim = threshold_world(
        tmp_path,
        monkeypatch,
        minimum=minimum,
        attestation_mutator=lambda _instance, _claim, items: items[:attestation_count],
    )

    rows = _rows(instance)
    assert len(rows) == expected_rows
    if rows:
        assert rows[0].detail["independent_control_component_count"] == attestation_count

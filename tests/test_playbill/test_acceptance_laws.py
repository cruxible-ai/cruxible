"""Family-neutral, historical acceptance-law registry tests."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.laws import (
    APPROVAL_POLICY_LAW,
    CLAIM_LAW_V2,
    CLAIM_LAW_V3,
    CLAIM_LAW_V3_REVISION_7,
    CLAIM_TYPE_LAW,
    CLAIM_TYPE_LAW_V3,
    CLAIM_TYPE_LAW_V4,
    DOCUMENT_LAW,
    LINE_LAW,
    LINE_LAW_V2,
    PLAYBILL_ACCEPTANCE_LAWS,
    PROCEDURE_LAW,
    PROCEDURE_LAW_REVISION_5,
    PROCEDURE_LAW_V2,
    PROCEDURE_LAW_V2_REVISION_5,
    PROCEDURE_RUNTIME_POLICY_LAW,
    PROVIDER_INTERFACE_LAW,
    PROVIDER_LAW_V2,
)
from cruxible_core.playbill.proposals import ROLE_DEMOTED_MEMBER_FAMILIES


def test_document_law_resolves_from_artifact_tag_and_replays_by_exact_digest() -> None:
    resolved = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-document-v1")

    assert resolved.coordinate == DOCUMENT_LAW
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.require_historical(
            identifier=DOCUMENT_LAW.identifier,
            digest=DOCUMENT_LAW.digest,
        )
        == resolved
    )


def test_approval_policy_law_resolves_as_the_governed_singleton() -> None:
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
            artifact_tag="playbill-approval-policy-v1"
        ).coordinate
        == APPROVAL_POLICY_LAW
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
            artifact_tag="playbill-procedure-runtime-policy-v1"
        ).coordinate
        == PROCEDURE_RUNTIME_POLICY_LAW
    )


def test_unknown_or_substituted_acceptance_law_refuses() -> None:
    with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-rail-v1")
    with pytest.raises(ProposalIntegrityError, match="cannot be reproduced"):
        PLAYBILL_ACCEPTANCE_LAWS.require_historical(
            identifier=DOCUMENT_LAW.identifier,
            digest="sha256:" + "00" * 32,
        )


def test_pc_d_procedure_and_line_laws_are_exact_historical_coordinates() -> None:
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-procedure-v1").coordinate
        == PROCEDURE_LAW
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-procedure-v2").coordinate
        == PROCEDURE_LAW_V2
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.require_historical(
            identifier=PROCEDURE_LAW_REVISION_5.identifier,
            digest=PROCEDURE_LAW_REVISION_5.digest,
        ).coordinate
        == PROCEDURE_LAW_REVISION_5
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.require_historical(
            identifier=PROCEDURE_LAW_V2_REVISION_5.identifier,
            digest=PROCEDURE_LAW_V2_REVISION_5.digest,
        ).coordinate
        == PROCEDURE_LAW_V2_REVISION_5
    )


def test_p2_b1_provider_interface_and_line_successor_laws_are_current() -> None:
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-provider-v2").coordinate
        == PROVIDER_LAW_V2
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(
            artifact_tag="playbill-provider-interface-v1"
        ).coordinate
        == PROVIDER_INTERFACE_LAW
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-line-v2").coordinate
        == LINE_LAW_V2
    )


def test_claim_v2_and_v3_laws_remain_independently_replayable() -> None:
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-v2").coordinate
        == CLAIM_LAW_V2
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-v3").coordinate
        == CLAIM_LAW_V3
    )
    assert CLAIM_LAW_V2.identifier == "playbill.claim.v2"
    assert CLAIM_LAW_V3.identifier == "playbill.claim.v3"
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.require_historical(
            identifier=CLAIM_LAW_V3_REVISION_7.identifier,
            digest=CLAIM_LAW_V3_REVISION_7.digest,
        ).coordinate
        == CLAIM_LAW_V3_REVISION_7
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-line-v1").coordinate
        == LINE_LAW
    )


def test_claim_type_v1_v3_and_v4_survive_but_removed_v2_has_no_acceptance_law() -> None:
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v1").coordinate
        == CLAIM_TYPE_LAW
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v3").coordinate
        == CLAIM_TYPE_LAW_V3
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v4").coordinate
        == CLAIM_TYPE_LAW_V4
    )
    with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v2")


def test_role_demotion_inventory_covers_every_candidate_member_family() -> None:
    assert ROLE_DEMOTED_MEMBER_FAMILIES == (
        "approval-policy",
        "procedure-runtime-policy",
        "procedure",
        "exhaust-promotion",
        "line",
        "query-definition",
        "provider",
        "provider-interface",
        "source-acquisition-policy",
        "standing-mandate",
        "procedure-mandate",
        "capture-contract",
        "claim",
        "claim-type",
        "subject",
        "document",
        "principal",
    )

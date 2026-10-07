"""Family-neutral, historical acceptance-law registry tests."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.laws import (
    ACCEPTANCE_LAWS,
    APPROVAL_POLICY_LAW,
    CLAIM_LAW_V2,
    CLAIM_LAW_V3,
    CLAIM_LAW_V3_REVISION_7,
    CLAIM_TYPE_LAW,
    CLAIM_TYPE_LAW_V3,
    CLAIM_TYPE_LAW_V4,
    DOCUMENT_LAW,
    LINE_V6_ACCEPTANCE_LAW,
    PROCEDURE_LAW_V2,
    PROCEDURE_RUNTIME_POLICY_LAW,
    PROVIDER_INTERFACE_LAW,
    PROVIDER_LAW_V2,
)
from cruxible_core.proposals.proposals import ROLE_DEMOTED_MEMBER_FAMILIES


def test_document_law_resolves_from_artifact_tag_and_replays_by_exact_digest() -> None:
    resolved = ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-document-v1")

    assert resolved.coordinate == DOCUMENT_LAW
    assert (
        ACCEPTANCE_LAWS.require_historical(
            identifier=DOCUMENT_LAW.identifier,
            digest=DOCUMENT_LAW.digest,
        )
        == resolved
    )


def test_approval_policy_law_resolves_as_the_governed_singleton() -> None:
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-approval-policy-v1").coordinate
        == APPROVAL_POLICY_LAW
    )
    assert (
        ACCEPTANCE_LAWS.resolve_member(
            artifact_tag="playbill-procedure-runtime-policy-v1"
        ).coordinate
        == PROCEDURE_RUNTIME_POLICY_LAW
    )


def test_unknown_or_substituted_acceptance_law_refuses() -> None:
    with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-rail-v1")
    with pytest.raises(ProposalIntegrityError, match="cannot be reproduced"):
        ACCEPTANCE_LAWS.require_historical(
            identifier=DOCUMENT_LAW.identifier,
            digest="sha256:" + "00" * 32,
        )


def test_procedure_law_is_the_one_v2_envelope_coordinate() -> None:
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-procedure-v2").coordinate
        == PROCEDURE_LAW_V2
    )
    assert (
        ACCEPTANCE_LAWS.require_historical(
            identifier=PROCEDURE_LAW_V2.identifier,
            digest=PROCEDURE_LAW_V2.digest,
        ).coordinate
        == PROCEDURE_LAW_V2
    )


def test_p2_b1_provider_interface_and_line_successor_laws_are_current() -> None:
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-provider-v2").coordinate
        == PROVIDER_LAW_V2
    )
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-provider-interface-v1").coordinate
        == PROVIDER_INTERFACE_LAW
    )
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-line-v6").coordinate
        == LINE_V6_ACCEPTANCE_LAW.coordinate
    )


def test_claim_v2_and_v3_laws_remain_independently_replayable() -> None:
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-v2").coordinate == CLAIM_LAW_V2
    )
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-v3").coordinate == CLAIM_LAW_V3
    )
    assert CLAIM_LAW_V2.identifier == "playbill.claim.v2"
    assert CLAIM_LAW_V3.identifier == "playbill.claim.v3"
    assert (
        ACCEPTANCE_LAWS.require_historical(
            identifier=CLAIM_LAW_V3_REVISION_7.identifier,
            digest=CLAIM_LAW_V3_REVISION_7.digest,
        ).coordinate
        == CLAIM_LAW_V3_REVISION_7
    )


def test_claim_type_v1_v3_and_v4_survive_but_removed_v2_has_no_acceptance_law() -> None:
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v1").coordinate
        == CLAIM_TYPE_LAW
    )
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v3").coordinate
        == CLAIM_TYPE_LAW_V3
    )
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v4").coordinate
        == CLAIM_TYPE_LAW_V4
    )
    with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-type-v2")


def test_role_demotion_inventory_covers_every_candidate_member_family() -> None:
    assert ROLE_DEMOTED_MEMBER_FAMILIES == (
        "compiler-upgrade",
        "resolution-contract",
        "attestation",
        "approval-policy",
        "procedure-runtime-policy",
        "procedure",
        "exhaust-promotion",
        "line",
        "trigger",
        "query-definition",
        "provider",
        "provider-interface",
        "source-acquisition-policy",
        "procedure-mandate",
        "capture-contract",
        "claim",
        "claim-type",
        "subject",
        "document",
        "principal",
    )

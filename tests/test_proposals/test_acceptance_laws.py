"""Family-neutral acceptance-law registry tests."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.laws import (
    ACCEPTANCE_LAWS,
    APPROVAL_POLICY_LAW,
    CLAIM_LAW,
    CLAIM_TYPE_LAW,
    DOCUMENT_LAW,
    LIGHT_PROCEDURE_LAW,
    LINE_V6_ACCEPTANCE_LAW,
    PROCEDURE_RUNTIME_POLICY_LAW,
    PROVIDER_LAW_V2,
    _artifact_law_coordinate,
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


def test_procedure_law_is_the_one_named_envelope_coordinate() -> None:
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="cruxible-procedure-v4") is LIGHT_PROCEDURE_LAW
    )
    with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-procedure-v2")


def test_p2_b1_provider_and_line_successor_laws_are_current() -> None:
    # The provider-interface law itself is gone (B2 noun removal, C13 cut
    # clean): there is no historical-replay obligation since 0.5 never
    # shipped, so "playbill-provider-interface-v1" has no acceptance law at
    # all any more.
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-provider-v2").coordinate
        == PROVIDER_LAW_V2
    )
    with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-provider-interface-v1")
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-line-v6").coordinate
        == LINE_V6_ACCEPTANCE_LAW.coordinate
    )


def test_one_claim_law_is_installed_and_removed_claim_formats_have_none() -> None:
    assert ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-claim-v4").coordinate == CLAIM_LAW
    assert CLAIM_LAW.identifier == "playbill.claim.v4"
    for removed in ("playbill-claim-v2", "playbill-claim-v3"):
        with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
            ACCEPTANCE_LAWS.resolve_member(artifact_tag=removed)
    # Another revision is not installed: replay refuses it.
    earlier = _artifact_law_coordinate(
        CLAIM_LAW.identifier, "playbill-claim-v4", semantic_revision=1
    )
    with pytest.raises(ProposalIntegrityError, match="cannot be reproduced"):
        ACCEPTANCE_LAWS.require_historical(identifier=earlier.identifier, digest=earlier.digest)


def test_one_claim_type_law_is_installed_and_removed_claim_type_formats_have_none() -> None:
    assert (
        ACCEPTANCE_LAWS.resolve_member(artifact_tag="cruxible-claim-type-v11").coordinate
        == CLAIM_TYPE_LAW
    )
    for removed in (
        "playbill-claim-type-v1",
        "playbill-claim-type-v2",
        "playbill-claim-type-v3",
        "playbill-claim-type-v4",
        "playbill-claim-type-v5",
        "playbill-claim-type-v6",
        "playbill-claim-type-v7",
        "playbill-claim-type-v8",
        "cruxible-claim-type-v9",
        "cruxible-claim-type-v10",
    ):
        with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
            ACCEPTANCE_LAWS.resolve_member(artifact_tag=removed)
    # Another revision is not installed: replay refuses it.
    earlier = _artifact_law_coordinate(
        CLAIM_TYPE_LAW.identifier, "cruxible-claim-type-v11", semantic_revision=2
    )
    with pytest.raises(ProposalIntegrityError, match="cannot be reproduced"):
        ACCEPTANCE_LAWS.require_historical(identifier=earlier.identifier, digest=earlier.digest)


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
        "note",
        "runbook",
        "subject-kind",
        "contract",
        "capability",
        "query-definition",
        "provider",
        "procedure-mandate",
        "capture-contract",
        "claim",
        "claim-type",
        "subject",
        "document",
        "principal",
    )


def test_document_law_revision_4_is_current_and_revision_3_replays_its_receipts() -> None:
    """Revision 4 closes the indexed link vocabulary; revision-3 receipts replay as recorded."""

    from types import SimpleNamespace

    from cruxible_client.contracts.documents import (
        DocumentLifecycle,
        DocumentLink,
        DocumentShell,
        document_path,
        evaluate_document_law,
        render_document,
    )
    from cruxible_client.contracts.laws import DOCUMENT_LAW_V3
    from cruxible_core.proposals.proposals import _document_member

    assert DOCUMENT_LAW != DOCUMENT_LAW_V3
    assert ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-document-v1").coordinate == (
        DOCUMENT_LAW
    )
    replay = ACCEPTANCE_LAWS.require_historical(
        identifier=DOCUMENT_LAW_V3.identifier, digest=DOCUMENT_LAW_V3.digest
    )
    assert replay.coordinate == DOCUMENT_LAW_V3 and replay.artifact_tag == "playbill-document-v1"

    shell = DocumentShell(
        identity="document:guide",
        document_kind="guidance",
        title="Guide",
        media_type="text/markdown",
        body_digest="sha256:" + "11" * 32,
        links=(DocumentLink(relation="guides", target_identity="document:elsewhere"),),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    path = document_path("guide")
    bodies = SimpleNamespace(verify=lambda _digest: True)
    current = evaluate_document_law(shell, path=path, bodies=bodies, predecessor=None)
    assert [item.code for item in current.diagnostics] == ["cruxible.document.link_target_invalid"]
    former = evaluate_document_law(
        shell, path=path, bodies=bodies, predecessor=None, link_rules=False
    )
    assert former.verdict == "accepted"

    def member(historical: tuple[str, str] | None):  # type: ignore[no-untyped-def]
        return _document_member(
            SimpleNamespace(
                path=path,
                content=render_document(shell),
                parent_content=None,
                bodies=bodies,
                historical_law_coordinate=historical,
                rechecking=historical is not None,
                acceptance_laws=ACCEPTANCE_LAWS,
            )
        )

    refused = member(None)
    assert refused.member is None
    assert [item.code for item in refused.diagnostics] == ["cruxible.document.link_target_invalid"]
    replayed = member((DOCUMENT_LAW_V3.identifier, DOCUMENT_LAW_V3.digest))
    assert replayed.member is not None
    assert (replayed.member.law_identifier, replayed.member.law_digest) == (
        DOCUMENT_LAW_V3.identifier,
        DOCUMENT_LAW_V3.digest,
    )

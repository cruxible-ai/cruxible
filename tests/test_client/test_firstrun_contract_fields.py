"""Shared first-run contract fields: additive, absent by default, typed when present."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    BlockSyncItem,
    BlockSyncReason,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDrafts,
    SelfSourceBody,
)
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.public_names import public_name_map
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.semantic import SemanticAddress

CLAIM = "CLM-" + "a" * 32


def _repair(**extra: object) -> contracts.NextRepair:
    return contracts.NextRepair(
        operation="cruxible.claim.retire", target=CLAIM, required_change="retire", **extra
    )


def test_a_repair_with_no_alternatives_keeps_its_bytes() -> None:
    assert "alternatives" not in _repair().model_dump(mode="json")
    rebind = contracts.NextRepair(
        operation="cruxible.rebind",
        target=CLAIM,
        required_change="rebind_the_drifted_citation",
        alternatives=(_repair(),),
    )
    dumped = rebind.model_dump(mode="json")
    assert [item["operation"] for item in dumped["alternatives"]] == ["cruxible.claim.retire"]
    assert contracts.NextRepair.model_validate(dumped) == rebind
    with pytest.raises(ValidationError, match="no alternatives of its own"):
        _repair(alternatives=(_repair(alternatives=(_repair(),)),))


def test_internal_next_repair_alternatives_stay_out_of_the_item_preimage() -> None:
    from cruxible_core.service.discovery.next import PlaybillNextRepairV1

    plain = PlaybillNextRepairV1(
        operation="cruxible.get", target="Capture:x", required_change="read_the_capture"
    )
    assert "alternatives" not in plain.model_dump(mode="json")
    assert "cruxible.get" in get_args(contracts.NextRepairOperation)
    assert "cruxible.rebind" in get_args(contracts.NextRepairOperation)


def test_new_lint_codes_are_closed_vocabulary_members() -> None:
    codes = get_args(contracts.ClaimTypeLintCode)
    assert "cruxible.claim_type.field_name_reserved" in codes
    assert "cruxible.claim_type.permitted_role_unadmitted" in codes
    warning = contracts.ClaimTypeLintWarning(
        code="playbill.claim_type.field_name_reserved",
        field_path="predicate",
        replacement_rule_fragment={},
    )
    assert warning.code == "cruxible.claim_type.field_name_reserved"


def test_statement_lines_are_absent_until_a_producer_reads_them() -> None:
    line = contracts.ClaimLine(claim=CLAIM, subject="acme/widget", field="price", value=42)
    coverage = contracts.CoverageResult(
        coordinate=contracts.AcceptedCoordinate(
            git_oid="4" * 64,
            semantic_root="sha256:" + "1" * 64,
            generation_root="sha256:" + "2" * 64,
            compiler_digest="sha256:" + "3" * 64,
        ),
        result={},
    )
    assert "claims" not in coverage.model_dump(mode="json")
    with_lines = coverage.model_copy(update={"claims": (line,)})
    assert with_lines.model_dump(mode="json")["claims"][0]["field"] == "price"
    assert "statement" in contracts.SinceRow.model_fields
    assert "statement" in contracts.AuditRow.model_fields


def test_server_info_and_proposal_outcome_are_absent_by_default() -> None:
    assert contracts.ServerInfoResult.model_fields["public_names"].default is None
    assert contracts.ProposalInspection.model_fields["outcome"].default is None
    served = contracts.ServerInfoResult.model_json_schema()
    assert "public_names" in served["properties"]
    assert contracts.PublicNameMap.model_validate(public_name_map().model_dump(mode="json"))


def _payload(**extra: object) -> ClaimAuthoringPayloadV2:
    return ClaimAuthoringPayloadV2(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact("subjects/acme/widget.json"),
            predicate="acme.price",
            object=LiteralClaimObject(value=42),
            role="observation",
        ),
        rationale="Stated.",
        source=SelfSourceBody(content_base64="b3du"),
        dependency_drafts=ClaimDependencyDrafts(),
        **extra,
    )


def test_replaces_citations_is_absent_by_default_and_names_a_revised_claim() -> None:
    assert "replaces_citations" not in _payload().model_dump(mode="json")
    digests = ("sha256:" + "1" * 64, "sha256:" + "2" * 64)
    with pytest.raises(ValidationError, match="names what it revises"):
        _payload(replaces_citations=digests)
    with pytest.raises(ValidationError, match="sorted and unique"):
        _payload(replaces_citations=tuple(reversed(digests)))


def test_detach_refuses_a_still_published_block_with_the_depublish_repair() -> None:
    assert "block_still_published" in get_args(BlockSyncReason)
    item = BlockSyncItem.model_validate(
        {"path": "docs/a.md", "outcome": "refused", "reason": "block_still_published"}
    )
    assert item.repair == RepairOperation(operation="cruxible.block.depublish")
    assert "registered" not in contracts.BlockSyncReadResult.model_validate(
        {"status": "refused", "reason": "block_backing_missing"}
    ).model_dump(mode="json")

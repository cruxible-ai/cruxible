"""Every reader takes a code written before the rename as today's code.

Records keep the ``playbill.`` spelling they were written with. Closed code
vocabularies, code-told unions and the retained-run normalizer read it as the
``cruxible.`` code; code-keyed logic compares on ``normalize_code``. Each case
runs once per prefix and must behave identically.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

PREFIXES = ("cruxible", "playbill")


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_retained_procedure_refusal_reads_as_todays_code(prefix: str) -> None:
    from cruxible_client.contracts.procedures.results import (
        ProcedureNodeRefusal,
        current_refusal_code,
    )

    refusal = ProcedureNodeRefusal.model_validate(
        {"code": f"{prefix}.acquisition.unavailable", "message": "missing", "node_id": "source"}
    )
    assert refusal.code == "cruxible.acquisition.unavailable"
    assert current_refusal_code(f"{prefix}.acquisition.stale") == "cruxible.acquisition.stale"


@pytest.mark.parametrize("prefix", PREFIXES)
def test_write_warnings_select_their_member_on_either_spelling(prefix: str) -> None:
    from cruxible_client.contracts.write import (
        NewerCaptureNotCitableWarning,
        VerdictNotSupportedWarning,
        WriteOutcome,
    )

    outcome = WriteOutcome.model_validate(
        {
            "status": "accepted",
            "coordinate": {"git_oid": "a" * 12, "generation": 3},
            "warnings": [
                {
                    "code": f"{prefix}.write.verdict_not_supported",
                    "change": 0,
                    "verdict": "uncovered",
                    "message": "not admitted",
                },
                {
                    "code": f"{prefix}.write.newer_capture_not_citable",
                    "change": 1,
                    "capture": "CAP-0123456789ab",
                    "message": "older capture cited",
                },
            ],
        }
    )
    verdict, newer = outcome.warnings
    assert isinstance(verdict, VerdictNotSupportedWarning)
    assert verdict.code == "cruxible.write.verdict_not_supported"
    assert isinstance(newer, NewerCaptureNotCitableWarning)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_closed_code_fields_read_either_spelling(prefix: str) -> None:
    from cruxible_client.contracts import NextRepair
    from cruxible_client.contracts.authoring.models import PublicationPrepareWarning
    from cruxible_client.contracts.principals import AuthoringRefusal
    from cruxible_client.contracts.repairs import RepairOperation
    from cruxible_core.claims.claim_type_inputs import ClaimTypeLintWarningV1
    from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationWarningV1
    from cruxible_core.proposals.proposals import RebaseMemberConflictV2

    assert (
        NextRepair(operation=f"{prefix}.block.sync", target="t", required_change="c").operation
        == "cruxible.block.sync"
    )
    assert (
        PublicationPrepareWarning(
            code=f"{prefix}.authoring.publication_citation_anchor_collision",
            source_id="docs.runbook",
            citation_ids=("sha256:" + "a" * 64,),
        ).code
        == "cruxible.authoring.publication_citation_anchor_collision"
    )
    assert (
        AuthoringRefusal(
            code=f"{prefix}.identity.credential_unbound",
            detail="unbound",
            repair=RepairOperation(operation="cruxible.whoami"),
        ).code
        == "cruxible.identity.credential_unbound"
    )
    lint = ClaimTypeLintWarningV1(
        code=f"{prefix}.claim_type.attestation_threshold_disabled",  # type: ignore[arg-type]
        field_path="attestation",
        replacement_rule_fragment={},
    )
    assert lint.code == "cruxible.claim_type.attestation_threshold_disabled"
    migration = ClaimTypeMigrationWarningV1(
        code=f"{prefix}.claim_type.invalidation_deprecated",  # type: ignore[arg-type]
        field_path="invalidation",
        repair_operation=f"{prefix}.claim_type.migrate",  # type: ignore[arg-type]
    )
    assert migration.repair_operation == "cruxible.claim_type.migrate"
    conflict = RebaseMemberConflictV2(
        code=f"{prefix}.rebase.member_conflict",  # type: ignore[arg-type]
        path="claims/x.json",
        old_parent_digest=None,
        proposed_digest=None,
        new_parent_digest=None,
    )
    assert conflict.code == "cruxible.rebase.member_conflict"


@pytest.mark.parametrize("prefix", PREFIXES)
def test_code_keyed_repairs_resolve_either_spelling(prefix: str) -> None:
    from cruxible_client.contracts.repairs import RepairOperation, served_repair_for_refusal
    from cruxible_core.cli.commands.playbill import _cli_repair
    from cruxible_core.errors import PrincipalRefusedError
    from cruxible_core.service.refusals import repair_for_refusal

    assert served_repair_for_refusal(f"{prefix}.next.cursor_mismatch") == RepairOperation(
        operation="cruxible.next"
    )
    assert repair_for_refusal(f"{prefix}.next.cursor_mismatch") == RepairOperation(
        operation="cruxible.next"
    )
    assert (
        _cli_repair(RepairOperation(operation=f"{prefix}.line.run", arguments={"line": "hourly"}))
        == "cruxible line run hourly"
    )
    refused = PrincipalRefusedError(
        f"{prefix}.identity.principal_claim_invalid",  # type: ignore[arg-type]
        "bad claim",
        repair=RepairOperation(operation="cruxible.whoami"),
    )
    assert refused.http_status == 400


@pytest.mark.parametrize("prefix", PREFIXES)
def test_the_sdk_refreshes_its_coordinate_on_a_slot_changed_refusal_either_spelling(
    prefix: str,
) -> None:
    from cruxible_client import Cruxible
    from cruxible_client.authoring.sdk import WriteRefusalError
    from cruxible_client.contracts.write import WriteOutcome, WriteRefusal

    observed: list[object] = []
    fake = SimpleNamespace(_observe_read=lambda coordinate, expected: observed.append(coordinate))
    outcome = WriteOutcome.model_construct(
        status="refused",
        accepted_coordinate={
            "git_oid": "a" * 40,
            "semantic_root": "sha256:" + "1" * 64,
            "generation_root": "sha256:" + "2" * 64,
            "compiler_digest": "sha256:" + "3" * 64,
        },
        refusal=WriteRefusal(code=f"{prefix}.write.slot_changed", message="moved"),
    )
    with pytest.raises(WriteRefusalError):
        Cruxible._written(fake, outcome)  # type: ignore[arg-type]
    assert len(observed) == 1

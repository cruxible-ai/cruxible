"""Every reader takes a code written before the rename as today's code.

Records keep the ``playbill.`` spelling they were written with. Closed code
vocabularies, code-told unions and the retained-run normalizer read it as the
``cruxible.`` code; code-keyed logic compares on ``normalize_code``. Each case
runs once per prefix and must behave identically.
"""

from __future__ import annotations

from collections import UserDict
from collections.abc import Callable
from types import MappingProxyType, SimpleNamespace

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


def _verdict(prefix: str) -> dict[str, object]:
    return {
        "code": f"{prefix}.write.verdict_not_supported",
        "change": 0,
        "verdict": "uncovered",
        "message": "not admitted",
    }


def _newer(prefix: str) -> dict[str, object]:
    return {
        "code": f"{prefix}.write.newer_capture_not_citable",
        "change": 1,
        "capture": "CAP-0123456789ab",
        "message": "older capture cited",
    }


@pytest.mark.parametrize("prefix", PREFIXES)
def test_write_warnings_read_either_spelling_at_every_reader(prefix: str) -> None:
    """Each member alone, the exported union, and the outcome that carries it."""

    from pydantic import TypeAdapter

    from cruxible_client.contracts.write import (
        NewerCaptureNotCitableWarning,
        VerdictNotSupportedWarning,
        WriteOutcome,
        WriteWarning,
    )

    verdict = VerdictNotSupportedWarning.model_validate(_verdict(prefix))
    assert verdict.code == "cruxible.write.verdict_not_supported"
    newer = NewerCaptureNotCitableWarning.model_validate(_newer(prefix))
    assert newer.code == "cruxible.write.newer_capture_not_citable"

    adapter: TypeAdapter[object] = TypeAdapter(WriteWarning)
    assert isinstance(adapter.validate_python(_verdict(prefix)), VerdictNotSupportedWarning)
    assert isinstance(adapter.validate_python(_newer(prefix)), NewerCaptureNotCitableWarning)

    outcome = WriteOutcome.model_validate(
        {
            "status": "accepted",
            "coordinate": {"git_oid": "a" * 12, "generation": 3},
            "warnings": [_verdict(prefix), _newer(prefix)],
        }
    )
    first, second = outcome.warnings
    assert isinstance(first, VerdictNotSupportedWarning)
    assert isinstance(second, NewerCaptureNotCitableWarning)
    assert first.code == "cruxible.write.verdict_not_supported"


@pytest.mark.parametrize("wrap", [MappingProxyType, UserDict], ids=["proxy", "userdict"])
@pytest.mark.parametrize("prefix", PREFIXES)
def test_write_warnings_read_any_mapping(prefix: str, wrap: Callable[[dict], object]) -> None:
    """The union reads its code from any mapping, as each member does, and from a model."""

    from pydantic import TypeAdapter

    from cruxible_client.contracts.write import (
        NewerCaptureNotCitableWarning,
        VerdictNotSupportedWarning,
        WriteOutcome,
        WriteWarning,
    )

    adapter: TypeAdapter[object] = TypeAdapter(WriteWarning)
    for payload, model, code in (
        (_verdict(prefix), VerdictNotSupportedWarning, "cruxible.write.verdict_not_supported"),
        (_newer(prefix), NewerCaptureNotCitableWarning, "cruxible.write.newer_capture_not_citable"),
    ):
        alone = model.model_validate(wrap(payload))
        assert alone.code == code
        selected = adapter.validate_python(wrap(payload))
        assert isinstance(selected, model) and selected.code == code
        # A model instance is read by attribute.
        assert adapter.validate_python(alone) == alone

    outcome = WriteOutcome.model_validate(
        {
            "status": "accepted",
            "coordinate": {"git_oid": "a" * 12, "generation": 3},
            "warnings": [wrap(_verdict(prefix)), wrap(_newer(prefix))],
        }
    )
    assert [type(item) for item in outcome.warnings] == [
        VerdictNotSupportedWarning,
        NewerCaptureNotCitableWarning,
    ]
    assert outcome.warnings[0].code == "cruxible.write.verdict_not_supported"


def test_the_write_warning_schema_keeps_its_code_discriminator() -> None:
    from pydantic import TypeAdapter

    from cruxible_client.contracts.write import WriteWarning

    schema = TypeAdapter(WriteWarning).json_schema()
    assert schema["discriminator"] == {
        "mapping": {
            "cruxible.write.verdict_not_supported": "#/$defs/VerdictNotSupportedWarning",
            "cruxible.write.newer_capture_not_citable": "#/$defs/NewerCaptureNotCitableWarning",
        },
        "propertyName": "code",
    }


@pytest.mark.parametrize("prefix", PREFIXES)
def test_closed_code_fields_read_either_spelling(prefix: str) -> None:
    from cruxible_client.contracts import NextRepair
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

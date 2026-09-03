"""Closed served refusal vocabularies always carry structured repair."""

from __future__ import annotations

from cruxible_client.contracts.authoring.models import PlaybillBlockSyncItemV1
from cruxible_client.contracts.procedures.results import ProcedureAdmissionRefusalV1
from cruxible_client.contracts.repairs import (
    DECLARED_HAND_EDIT_CHANGES,
    UNDECLARED_HAND_EDIT_CHANGE,
    HandEditRepairV1,
    RepairOperationV1,
    ServedRepairEnvelopeV1,
)
from cruxible_core.cli.main import CLI_COMMANDS, LazyCommandSpec
from cruxible_core.service.playbill_refusal_catalog import (
    ALL_SERVED_REFUSAL_CODES,
    CLOSED_SERVED_REFUSAL_VOCABULARIES,
    RUNNABLE_REFUSAL_REPAIRS,
    UNDECLARED_REFUSAL_CODE_COUNT,
    hand_edit_next_reasons,
    repair_for_refusal,
    undeclared_refusal_codes,
)


def _cli_leaves(commands: dict[str, LazyCommandSpec], path: tuple[str, ...] = ()) -> set[str]:
    leaves: set[str] = set()
    for name, spec in commands.items():
        if spec.commands:
            leaves |= _cli_leaves(spec.commands, (*path, name))
        else:
            leaves.add(".".join((*path, name)))
    return leaves


def test_every_registered_refusal_resolves_without_prose_parsing() -> None:
    assert ALL_SERVED_REFUSAL_CODES
    assert all(CLOSED_SERVED_REFUSAL_VOCABULARIES.values())

    for code in ALL_SERVED_REFUSAL_CODES:
        repair = repair_for_refusal(code)
        assert ServedRepairEnvelopeV1(repair=repair).repair == repair
        if isinstance(repair, HandEditRepairV1):
            assert repair.hand_edit.target == f"refusal/{code}"
            assert repair.hand_edit.required_change


def test_every_runnable_repair_names_a_command_the_cli_actually_serves() -> None:
    """A repair naming a command that does not exist is worse than none."""

    leaves = _cli_leaves(CLI_COMMANDS)
    assert "playbill.line.run" in leaves  # the map really is the served inventory
    for code, repair in RUNNABLE_REFUSAL_REPAIRS.items():
        assert code in ALL_SERVED_REFUSAL_CODES, code
        assert isinstance(repair, RepairOperationV1)
        assert repair.operation in leaves, f"{code} names unserved command {repair.operation}"


def test_server_envelope_repairs_name_commands_the_cli_actually_serves() -> None:
    """The daemon-scope and authentication emitters carry runnable repairs."""

    from cruxible_core.errors import AuthenticationError, DaemonOperationScopeError
    from cruxible_core.server.errors import _repair_for_error

    leaves = _cli_leaves(CLI_COMMANDS)
    scope = _repair_for_error(DaemonOperationScopeError("cruxible_server_info", "inst_a"))
    assert isinstance(scope, RepairOperationV1)
    assert scope.operation in leaves
    assert scope.arguments["refused_operation"] == "cruxible_server_info"

    unauthenticated = _repair_for_error(AuthenticationError("no credential"))
    assert isinstance(unauthenticated, RepairOperationV1)
    assert unauthenticated.operation in leaves


def test_the_served_refusal_models_read_the_declared_change() -> None:
    """A producer that carries no repair still projects the declared change."""

    from cruxible_client.contracts.procedures.results import ProcedureSettlementRefusalV1

    refusal = ProcedureSettlementRefusalV1.model_validate(
        {
            "code": "settlement_candidate_scope_mismatch",
            "message": "the candidate scope differs from its admission",
            "node_id": "settlement",
        }
    )
    assert isinstance(refusal.repair, HandEditRepairV1)
    assert (
        refusal.repair.hand_edit.required_change
        == (DECLARED_HAND_EDIT_CHANGES["settlement_candidate_scope_mismatch"])
    )


def test_declared_hand_edits_are_membership_not_derivation() -> None:
    for code, required_change in DECLARED_HAND_EDIT_CHANGES.items():
        assert code in ALL_SERVED_REFUSAL_CODES, code
        assert code not in RUNNABLE_REFUSAL_REPAIRS, code
        assert required_change != UNDECLARED_HAND_EDIT_CHANGE
        # The declared change may not be the code restated: `repair_<code>` and
        # its kin read like an instruction while carrying none.
        assert required_change != f"repair_{code}"
        assert required_change != code


def test_undeclared_repair_debt_is_pinned_and_can_only_shrink() -> None:
    """A new closed refusal member cannot join without being classified."""

    undeclared = undeclared_refusal_codes()
    assert len(undeclared) == UNDECLARED_REFUSAL_CODE_COUNT
    for code in undeclared:
        repair = repair_for_refusal(code)
        assert isinstance(repair, HandEditRepairV1)
        assert repair.hand_edit.required_change == UNDECLARED_HAND_EDIT_CHANGE


def test_hand_edit_next_membership_is_client_owned_and_positive() -> None:
    assert hand_edit_next_reasons() == {
        "instance_decommissioned",
        "procedure_projection_missing",
        "provider_lane_unavailable",
    }


def test_unregistered_free_string_is_not_promoted_to_authority() -> None:
    try:
        repair_for_refusal("compiler diagnostic prose")
    except KeyError as exc:
        assert "unregistered served refusal" in str(exc)
    else:  # pragma: no cover - decisive guard
        raise AssertionError("free diagnostic prose entered the v1 refusal catalog")


def _admission_refusal_repair(code: str) -> RepairOperationV1 | HandEditRepairV1:
    """Build the served admission refusal exactly as a producer that carries none."""

    return ProcedureAdmissionRefusalV1.model_validate({"code": code, "message": "refused"}).repair


def _block_sync_repair(code: str) -> RepairOperationV1 | HandEditRepairV1:
    """Build the served block-sync item exactly as a producer that carries none."""

    item = PlaybillBlockSyncItemV1.model_validate(
        {"path": ".", "outcome": "refused", "reason": code}
    )
    assert item.repair is not None
    return item.repair


def _prediction_refusal_repair(code: str) -> object:
    from cruxible_core.server.errors import error_to_response
    from cruxible_core.service.playbill_predictions import _refuse

    _status, response = error_to_response(_refuse(code, "refused"))  # type: ignore[arg-type]
    return response.repair


def test_every_runnable_repair_reaches_a_live_wire_response() -> None:
    """A declared runnable repair that never reaches a served payload is prose."""

    from cruxible_core.service.playbill_next import _repair_command

    seen: dict[str, str] = {}
    for code, declared in RUNNABLE_REFUSAL_REPAIRS.items():
        owners = {
            name for name, members in CLOSED_SERVED_REFUSAL_VOCABULARIES.items() if code in members
        }
        assert owners, code
        if "procedure_admission_refusal" in owners:
            built = _admission_refusal_repair(code)
            assert built == declared, code
            seen[code] = "procedure_admission_refusal"
        elif owners & {"block_sync_reason", "block_sync_read_reason"}:
            built = _block_sync_repair(code)
            assert built == declared, code
            seen[code] = "block_sync"
        elif "prediction_refusal" in owners:
            built = _prediction_refusal_repair(code)
            assert isinstance(built, RepairOperationV1), code
            assert built.operation == declared.operation, code
            seen[code] = "prediction_refusal"
        elif "playbill_next_reason" in owners:
            # The next lane carries its own structured repair; the catalog entry
            # is honest only if that lane really composes a runnable command for
            # the same served operation.
            command = _repair_command(declared.operation, arguments=declared.arguments)
            assert command is not None and command.startswith("cruxible "), code
            seen[code] = "playbill_next_reason"
        else:  # pragma: no cover - decisive guard
            raise AssertionError(f"{code} has no live served carrier")
    assert set(seen) == set(RUNNABLE_REFUSAL_REPAIRS)

"""Source-owned inventory of closed refusal and next-reason vocabularies."""

from __future__ import annotations

from typing import get_args

from cruxible_client.contracts import (
    NextReason,
    NextRefusalCode,
    ProviderLaneUnavailableCode,
)
from cruxible_client.contracts.authoring.models import (
    BlockSyncReadReason,
    BlockSyncReason,
)
from cruxible_client.contracts.codes import normalize_code
from cruxible_client.contracts.predictions import PredictionRefusalCode
from cruxible_client.contracts.procedures.readings import ProcedureMeasurementRefusalCode
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalCode,
    ProcedureInternalFailureCode,
    ProcedureNodeRefusalCode,
    ProcedureOperationalFailureCode,
)
from cruxible_client.contracts.repairs import (
    DECLARED_HAND_EDIT_CHANGES,
    RUNNABLE_REFUSAL_REPAIRS,
    UNDECLARED_HAND_EDIT_CHANGE,
    ServedRepair,
    served_repair_for_refusal,
)
from cruxible_client.contracts.workspace_advertisement import WorkspaceAdvertisementFailureCode

CLOSED_SERVED_REFUSAL_VOCABULARIES: dict[str, frozenset[str]] = {
    "playbill_next_reason": frozenset(get_args(NextReason)),
    "playbill_next_refusal": frozenset(get_args(NextRefusalCode)),
    "provider_lane_unavailable": frozenset(get_args(ProviderLaneUnavailableCode)),
    "workspace_advertisement_failure": frozenset(get_args(WorkspaceAdvertisementFailureCode)),
    "block_sync_read_reason": frozenset(get_args(BlockSyncReadReason)),
    "block_sync_reason": frozenset(get_args(BlockSyncReason)),
    "procedure_admission_refusal": frozenset(get_args(ProcedureAdmissionRefusalCode)),
    "procedure_node_refusal": frozenset(get_args(ProcedureNodeRefusalCode)),
    "procedure_operational_failure": frozenset(get_args(ProcedureOperationalFailureCode)),
    "procedure_internal_failure": frozenset(get_args(ProcedureInternalFailureCode)),
    "prediction_refusal": frozenset(get_args(PredictionRefusalCode)),
    "procedure_measurement_refusal": frozenset(get_args(ProcedureMeasurementRefusalCode)),
}

ALL_SERVED_REFUSAL_CODES = frozenset().union(*CLOSED_SERVED_REFUSAL_VOCABULARIES.values())

# Everything else resolves to the truthful undeclared hand edit. The count is
# pinned by the guardrail so a new closed refusal member cannot join silently:
# adding one forces either a declared repair or an explicit re-pin here.
UNDECLARED_REFUSAL_CODE_COUNT = 148


def repair_for_refusal(code: str) -> ServedRepair:
    """Resolve one registered code without interpreting diagnostic prose."""

    code = normalize_code(code)
    if code not in ALL_SERVED_REFUSAL_CODES:
        raise KeyError(f"unregistered served refusal code: {code}")
    return served_repair_for_refusal(code)


def undeclared_refusal_codes() -> frozenset[str]:
    """Expose exactly the codes still carrying no specific declared repair."""

    return frozenset(
        code
        for code in ALL_SERVED_REFUSAL_CODES
        if code not in RUNNABLE_REFUSAL_REPAIRS and code not in DECLARED_HAND_EDIT_CHANGES
    )


__all__ = [
    "ALL_SERVED_REFUSAL_CODES",
    "CLOSED_SERVED_REFUSAL_VOCABULARIES",
    "DECLARED_HAND_EDIT_CHANGES",
    "RUNNABLE_REFUSAL_REPAIRS",
    "UNDECLARED_HAND_EDIT_CHANGE",
    "UNDECLARED_REFUSAL_CODE_COUNT",
    "repair_for_refusal",
    "undeclared_refusal_codes",
]

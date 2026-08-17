"""Frozen v1 Procedure run-input records and plane correspondence checks."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.playbill.canonical import ArtifactDigest, Sha256Value
from cruxible_core.playbill.procedures.models import (
    ExhaustTapNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.source_references import (
    SemanticReadCoordinateV1,
    validate_local_read_coordinate,
)

_INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


class _StrictInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _artifact_digest(value: str) -> str:
    ArtifactDigest.from_tagged(value)
    return value


def _sha256(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


class AcceptedStateRunInputV1(_StrictInputModel):
    tag: Literal["playbill-accepted-state-run-input-v1"] = "playbill-accepted-state-run-input-v1"
    kind: Literal["accepted_state"] = "accepted_state"
    input_name: str
    read_coordinate: SemanticReadCoordinateV1
    query_definition_digest: str
    parameters_digest: str
    result_digest: str

    _query = field_validator("query_definition_digest")(_artifact_digest)
    _payloads = field_validator("parameters_digest", "result_digest")(_sha256)

    @field_validator("input_name")
    @classmethod
    def _input_name(cls, value: str) -> str:
        if not _INPUT_NAME_RE.fullmatch(value):
            raise ValueError("Procedure input_name is not canonical")
        return value


class LandedCaptureRunInputV1(_StrictInputModel):
    tag: Literal["playbill-landed-capture-run-input-v1"] = "playbill-landed-capture-run-input-v1"
    kind: Literal["landed_capture"] = "landed_capture"
    input_name: str
    capture_digest: str
    capture_contract_digest: str
    landing_cursor: str

    _digests = field_validator("capture_digest", "capture_contract_digest")(_artifact_digest)

    @field_validator("input_name")
    @classmethod
    def _input_name(cls, value: str) -> str:
        if not _INPUT_NAME_RE.fullmatch(value):
            raise ValueError("Procedure input_name is not canonical")
        return value


class ExhaustRunInputV1(_StrictInputModel):
    tag: Literal["playbill-exhaust-run-input-v1"] = "playbill-exhaust-run-input-v1"
    kind: Literal["exhaust"] = "exhaust"
    input_name: str
    journal_identity: str
    first_cursor: str
    last_cursor: str
    reducer_or_query_digest: str
    result_digest: str

    _digests = field_validator("reducer_or_query_digest")(_artifact_digest)
    _result = field_validator("result_digest")(_sha256)

    @field_validator("input_name")
    @classmethod
    def _input_name(cls, value: str) -> str:
        if not _INPUT_NAME_RE.fullmatch(value):
            raise ValueError("Procedure input_name is not canonical")
        return value


ProcedureRunInputV1 = Annotated[
    AcceptedStateRunInputV1 | LandedCaptureRunInputV1 | ExhaustRunInputV1,
    Field(discriminator="kind"),
]


def validate_run_input_vector(
    inputs: tuple[ProcedureRunInputV1, ...],
    *,
    expected_accepted: AcceptedCoordinate,
) -> None:
    names = tuple(item.input_name for item in inputs)
    if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
        raise ValueError("Procedure run inputs must be sorted and unique by input_name")
    for item in inputs:
        if isinstance(item, AcceptedStateRunInputV1):
            validate_local_read_coordinate(
                item.read_coordinate,
                expected_accepted=expected_accepted,
            )
        if isinstance(item, LandedCaptureRunInputV1) and not _CURSOR_RE.fullmatch(
            item.landing_cursor
        ):
            raise ValueError("capture landing cursor is not canonical")
        if isinstance(item, ExhaustRunInputV1):
            if not _CURSOR_RE.fullmatch(item.first_cursor) or not _CURSOR_RE.fullmatch(
                item.last_cursor
            ):
                raise ValueError("exhaust cursor range is not canonical")
            if item.first_cursor > item.last_cursor:
                raise ValueError("exhaust cursor range must be increasing")


def validate_node_input_plane(
    node: StateTapNodeV3 | SourceNodeV3 | ExhaustTapNodeV3,
    run_input: ProcedureRunInputV1,
) -> None:
    """Refuse any attempt to relabel evidence between the three input planes."""

    expected = {
        "state_tap": "accepted_state",
        "source": "landed_capture",
        "exhaust_tap": "exhaust",
    }[node.kind]
    if run_input.kind != expected:
        raise ValueError(
            f"Procedure node {node.node_id!r} requires {expected!r}, got {run_input.kind!r}"
        )


__all__ = [
    "AcceptedStateRunInputV1",
    "ExhaustRunInputV1",
    "LandedCaptureRunInputV1",
    "ProcedureRunInputV1",
    "validate_node_input_plane",
    "validate_run_input_vector",
]

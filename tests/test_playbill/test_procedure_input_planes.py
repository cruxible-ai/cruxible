"""Procedure v3 input planes remain distinct at admission."""

from __future__ import annotations

import pytest

from cruxible_core.playbill.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import ArtifactDigest, typed_digest
from cruxible_core.playbill.procedures.input_planes import (
    AcceptedStateRunInputV1,
    ExhaustRunInputV1,
    LandedCaptureRunInputV1,
    validate_node_input_plane,
    validate_run_input_vector,
)
from cruxible_core.playbill.procedures.models import (
    ExhaustTapNodeV3,
    ProcedurePinSlotRefV1,
    SourceNodeV3,
    StateTapNodeV3,
)
from cruxible_core.playbill.projection import AcceptedCoordinate


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-input-test-v1", {"label": label}).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _coordinate(seed: str) -> AcceptedCoordinate:
    digit = "1" if seed == "local" else "2"
    return AcceptedCoordinate(
        git_oid=digit * 40,
        semantic_root=_digest(f"{seed}-semantic"),
        generation_root=_digest(f"{seed}-generation"),
        compiler_digest=_digest(f"{seed}-compiler"),
    )


def test_state_tap_accepts_only_the_bound_local_accepted_coordinate() -> None:
    local = _coordinate("local")
    accepted_input = AcceptedStateRunInputV1(
        input_name="claims",
        read_coordinate=local,
        query_definition_digest=_digest("query"),
        parameters_digest=_digest("parameters"),
        result_digest=_digest("result"),
    )
    validate_run_input_vector((accepted_input,), expected_accepted=local)

    remote = accepted_input.model_copy(update={"read_coordinate": _coordinate("remote")})
    with pytest.raises(ValueError, match="remote or unverified"):
        validate_run_input_vector((remote,), expected_accepted=local)


def test_capture_and_exhaust_cannot_be_relabelled_as_canonical_state() -> None:
    local = _coordinate("local")
    capture = LandedCaptureRunInputV1(
        input_name="world",
        capture_digest=_digest("capture"),
        capture_contract_digest=_digest("capture-contract"),
        landing_cursor="partition:0001",
    )
    exhaust = ExhaustRunInputV1(
        input_name="receipts",
        journal_identity="procedure-exhaust",
        first_cursor="0001",
        last_cursor="0009",
        reducer_or_query_digest=_digest("reducer"),
        result_digest=_digest("exhaust-result"),
    )
    validate_run_input_vector((exhaust, capture), expected_accepted=local)

    state_node = StateTapNodeV3(
        node_id="state",
        query=_pin("query", "QueryDefinition", "claims"),
        parameters={},
        as_="claims",
    )
    source_node = SourceNodeV3(
        node_id="source",
        capture_contract=_pin("capture-contract", "CaptureContract", "world"),
        provider=ProcedurePinSlotRefV1(slot_name="provider"),
        request={},
        as_="capture",
    )
    exhaust_node = ExhaustTapNodeV3(
        node_id="exhaust",
        reducer_or_query=_pin("reducer", "Procedure", "reduce-exhaust"),
        journal_identity="procedure-exhaust",
        as_="receipts",
    )

    with pytest.raises(ValueError, match="requires 'accepted_state'"):
        validate_node_input_plane(state_node, capture)
    validate_node_input_plane(source_node, capture)
    validate_node_input_plane(exhaust_node, exhaust)


def test_run_input_vector_refuses_duplicate_or_insertion_order_names() -> None:
    local = _coordinate("local")
    first = LandedCaptureRunInputV1(
        input_name="zeta",
        capture_digest=_digest("capture-z"),
        capture_contract_digest=_digest("contract-z"),
        landing_cursor="z:1",
    )
    second = LandedCaptureRunInputV1(
        input_name="alpha",
        capture_digest=_digest("capture-a"),
        capture_contract_digest=_digest("contract-a"),
        landing_cursor="a:1",
    )
    with pytest.raises(ValueError, match="sorted and unique"):
        validate_run_input_vector((first, second), expected_accepted=local)

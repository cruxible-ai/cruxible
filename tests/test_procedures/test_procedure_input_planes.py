"""Procedure input planes remain distinct at admission."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, CasDigest, typed_digest
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ReadNode,
)
from cruxible_client.contracts.query.grammar import QueryBudgets
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.procedures.input_planes import (
    AcceptedClaimRunInputV1,
    AcceptedStateRunInputV2,
    ExhaustRunInputV1,
    LandedCaptureRunInputV1,
    validate_node_input_plane,
    validate_run_input_vector,
)
from tests.support.procedures import named


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
    accepted_input = AcceptedStateRunInputV2(
        input_name="claims",
        read_coordinate=local,
        query_definition_digest=_digest("query"),
        parameters_digest=_digest("parameters"),
        result_digest=_digest("result"),
        effective_query_budgets=QueryBudgets(max_results=10, max_traversal_depth=1),
        material_body_digest=typed_digest(
            CasDigest, "playbill-input-test-v1", {"label": "local"}
        ).tagged,
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

    state_node = ReadNode(
        node_id="state",
        query=named(_pin("query", "QueryDefinition", "claims")),
        parameters={},
        as_="claims",
    )
    source_node = CallNode(
        node_id="source",
        captures={"capture": named(_pin("capture-contract", "CaptureContract", "world"))},
        slot="fetch",
        input={},
        as_="capture",
    )
    with pytest.raises(ValueError, match="requires 'accepted_state'"):
        validate_node_input_plane(state_node, capture)
    validate_node_input_plane(source_node, capture)
    # No node reads the exhaust plane: an exhaust input relabels as no node's.
    with pytest.raises(ValueError, match="requires 'accepted_state'"):
        validate_node_input_plane(state_node, exhaust)
    with pytest.raises(ValueError, match="requires 'landed_capture'"):
        validate_node_input_plane(source_node, exhaust)


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


@pytest.mark.parametrize("model", [AcceptedStateRunInputV2, AcceptedClaimRunInputV1])
def test_every_accepted_plane_validates_its_name_and_material_on_every_interpreter(
    model: type[AcceptedStateRunInputV2] | type[AcceptedClaimRunInputV1],
) -> None:
    """The shared validators take the value, never ``ValidationInfo``, on 3.11-3.13.

    A classmethod validator reused across models bound to its bare ``(cls, value)``
    function on Python 3.12, so every admitted claim input failed to reparse.
    """

    definition = (
        {"claim_type_digest": _digest("claim-type")}
        if model is AcceptedClaimRunInputV1
        else {"query_definition_digest": _digest("query")}
    )
    payload = {
        "input_name": "claims",
        "read_coordinate": _coordinate("local").model_dump(mode="json"),
        "parameters_digest": _digest("parameters"),
        "result_digest": _digest("result"),
        "effective_query_budgets": QueryBudgets(max_results=10, max_traversal_depth=1).model_dump(
            mode="json"
        ),
        "material_body_digest": typed_digest(
            CasDigest, "playbill-input-test-v1", {"label": "body"}
        ).tagged,
        **definition,
    }
    accepted = model.model_validate(payload)
    assert accepted.input_name == "claims"
    assert model.model_validate(accepted.model_dump(mode="json")) == accepted

    with pytest.raises(ValueError, match="input_name is not canonical"):
        model.model_validate({**payload, "input_name": "Not Canonical"})
    with pytest.raises(ValueError, match="material_body_digest"):
        model.model_validate({**payload, "material_body_digest": "not-a-digest"})

"""Core-produced nested numbers satisfy the same strict boundaries as caller values."""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.captures import CaptureDecoding, CaptureDecodingLimits
from cruxible_client.contracts.procedures.contracts import AcceptedProcedureContractValidator
from cruxible_client.contracts.procedures.extract import ExtractNode
from cruxible_client.contracts.shapes import ContractRef
from cruxible_core.procedures.execution import ProcedureExecutor, _RunState
from cruxible_core.procedures.extract import run_extract
from tests.support.procedures import boundary_contract, contract_pin, contract_resolver
from tests.test_procedures.test_procedure_execution import (
    _Authority,
    _Contracts,
    _fixture,
    _prepare,
    _state_procedure,
    _StateReader,
)


@pytest.mark.parametrize(("lexeme", "canonical"), [("7.0", 7), ("1E+1", 10), ("7.50", "7.5")])
def test_nested_extract_numbers_pass_procedure_output_and_operation_input(
    tmp_path: Path, lexeme: str, canonical: int | str
) -> None:
    detail = boundary_contract(
        "test.number-detail", {"value": "number", "series": "list<number>{max_items: 10}"}
    )
    row = boundary_contract("test.number-row", {"nested": "test.number-detail@1"}, detail)
    boundary = boundary_contract("test.number-record", {"record": "record<test.number-row@1>"}, row)
    resolve = contract_resolver(detail, row, boundary)
    node = ExtractNode.model_validate(
        {
            "node_id": "numbers",
            "capture": "$steps.source.capture",
            "records": "/*",
            "row": {"name": row.name, "version": 1},
            "fields": {"nested": {"at": "nested", "presence": "required"}},
            "as": "numbers",
        }
    )
    content = ('[{"nested":{"value":' + lexeme + ',"series":[' + lexeme + "]}}]").encode()
    outcome = run_extract(
        node,
        capture_digest="sha256:" + hashlib.sha256(content).hexdigest(),
        content=content,
        decoding=CaptureDecoding(
            format="json", limits=CaptureDecodingLimits(decoded_bytes=4096, depth=16, records=10)
        ),
        row=row,
        resolve=resolve,
    )
    payload = {"record": outcome.value()["records"][0]}
    assert payload["record"]["fields"]["nested"]["value"] == {
        "value": canonical,
        "series": [canonical],
    }
    validator = AcceptedProcedureContractValidator(resolve)
    assert (
        validator.validate_contract(
            contract=contract_pin("contract-out", boundary), payload=payload, direction="output"
        )
        == payload
    )

    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    )
    occurrence = SimpleNamespace(
        contracts=(detail, row, boundary),
        operation=SimpleNamespace(in_=ContractRef(name=boundary.name, version=1), name="consume"),
    )
    assert (
        executor._validate_operation_value(
            occurrence,
            payload,
            direction="in",
            admission=prepared.admission,
            state=_RunState(outputs={}, input_payload={}, parameters={}),
            node_id="consume",
        )
        == payload
    )

"""Procedure-egress self-attacks for the Capture-v2 producer edge."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV2,
    ProcedureEgressCaptureEvidenceV1,
    capture_contract_digest,
    parse_capture_envelope,
    verify_capture,
)
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.procedures.egress import (
    CaptureTerminalEgressSink,
    ProcedureProducerReceiptV1,
    TerminalEgressError,
    TerminalEgressReceiptV2,
    build_terminal_egress_request_v2,
    procedure_producer_receipt_digest,
    verify_terminal_egress_receipt,
)
from tests.test_playbill._pc_c_support import capture_contract
from tests.test_playbill.test_procedure_effectful_terminals import (
    _admission,
    _base_request,
    _item,
)


def test_procedure_capture_is_below_the_exact_pre_egress_producer_receipt(
    tmp_path: Path,
) -> None:
    admission = _admission(tmp_path)
    contract = capture_contract()
    contract_digest = capture_contract_digest(contract).tagged
    pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=contract_digest,
    )
    base = _base_request(
        "emit_capture",
        admission=admission,
        item=_item("result", value={"answer": 42}),
        bound=pin,
    )
    request = build_terminal_egress_request_v2(
        base,
        admission=admission,
        procedure_mandate_digest=None,
        calibration_reading_digests=(),
        target_paths=(),
    )
    assert isinstance(request.producer_receipt, ProcedureProducerReceiptV1)
    expected_producer_digest = procedure_producer_receipt_digest(request.producer_receipt)
    cas_root = tmp_path / "egress-cas"
    cas_root.mkdir()
    store = ContentAddressedBodyStore(cas_root)
    sink = CaptureTerminalEgressSink(
        store=store,
        contracts={contract_digest: contract},
        producer=admission.procedure_identity,
        producer_binding_digest=admission.procedure_artifact_digest,
    )

    receipt = sink.deliver_terminal_egress(request=request)

    assert isinstance(receipt, TerminalEgressReceiptV2)
    assert receipt.producer_receipt_digest == expected_producer_digest
    verify_terminal_egress_receipt(request, receipt)
    capture = parse_capture_envelope(
        store.read(
            receipt.children[0].egress_digest,
            access=BodyAccessContext(principal_id="unit-test", can_read_body=True),
        )
    )
    assert isinstance(capture, CaptureEnvelopeV2)
    assert capture.producer_receipt_digest == expected_producer_digest
    assert capture.producer == admission.procedure_identity
    assert capture.producer_binding_digest == admission.procedure_artifact_digest
    assert capture.observed_at == request.evaluation_time
    assert capture.source_effective_time is None
    assert capture.production_evidence == ProcedureEgressCaptureEvidenceV1(
        procedure_producer_receipt_digest=expected_producer_digest
    )
    assert (
        verify_capture(
            receipt.children[0].egress_digest,
            store=store,
            contract=contract,
            producer_artifact_digests={
                admission.procedure_identity.qualified: admission.procedure_artifact_digest
            },
            producer_receipt_resolver=lambda value: (
                request.producer_receipt if value == expected_producer_digest else None
            ),
        )
        == capture
    )


def test_v2_capture_sink_refuses_a_configured_producer_mismatch(tmp_path: Path) -> None:
    admission = _admission(tmp_path)
    contract = capture_contract()
    contract_digest = capture_contract_digest(contract).tagged
    request = build_terminal_egress_request_v2(
        _base_request(
            "emit_capture",
            admission=admission,
            item=_item("result", value={"answer": 42}),
            bound=ArtifactPin(
                role="capture-contract",
                target=contract.identity,
                artifact_digest=contract_digest,
            ),
        ),
        admission=admission,
        procedure_mandate_digest=None,
        calibration_reading_digests=(),
        target_paths=(),
    )
    cas_root = tmp_path / "mismatched-egress-cas"
    cas_root.mkdir()
    sink = CaptureTerminalEgressSink(
        store=ContentAddressedBodyStore(cas_root),
        contracts={contract_digest: contract},
        producer=ArtifactIdentity(kind="Procedure", name="substituted"),
        producer_binding_digest=admission.procedure_artifact_digest,
    )

    with pytest.raises(TerminalEgressError, match="producer differs"):
        sink.deliver_terminal_egress(request=request)

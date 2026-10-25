"""Current authored contracts for external records and retained Procedure sources."""

from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    lower_capture_contract_input,
)
from cruxible_client.contracts.captures import CaptureContractAny


def record_contract(
    *, name="test.orders-v1", erasure=False, self_asserted=False, replay=None
) -> CaptureContractAny:
    return lower_capture_contract_input(
        CaptureContractInputRecord(
            name=name,
            shape="foreign_span" if self_asserted else "database_snapshot",
            replay=replay or ("attested" if self_asserted else "exact"),
            max_bytes=4096,
            erasable=erasure,
        ),
        named=True,
    )


def http_contract(
    *, name="test.orders-v1", max_bytes=1_048_576, replay="attested"
) -> CaptureContractAny:
    return lower_capture_contract_input(
        CaptureContractInputRecord(
            name=name, shape="http_response", replay=replay, max_bytes=max_bytes
        ),
        named=True,
    )


def procedure_contract(*, name="test.procedure-output", max_bytes=4096) -> CaptureContractAny:
    return lower_capture_contract_input(
        CaptureContractInputRecord(name=name, shape="procedure_output", max_bytes=max_bytes),
        named=True,
    )

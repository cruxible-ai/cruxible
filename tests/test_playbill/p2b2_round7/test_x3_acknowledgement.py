"""Round-7 regression for disposition-gated recovery acknowledgement."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

DEAD_PID = 99_999_991


def test_fold_failed_disposition_cannot_release_a_pending_recovery(
    short_root: Path,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    invocation_id = "sha256:" + "4" * 64
    record_path, _control_path = store.paths(invocation_id)
    record_path.write_bytes(
        canonical_bytes(
            {
                "invocation_id": invocation_id,
                "pid": DEAD_PID,
                "process_group_id": DEAD_PID,
                "session_id": None,
                "boot_id": None,
                "process_start_time": None,
            }
        )
    )
    result = operator.recover_all()
    assert result.completion_invocation_ids == (invocation_id,)
    assert record_path.exists()

    with pytest.raises(ProviderLocalRuntimeRefused) as refusal:
        operator.acknowledge_recovery({invocation_id: "fold_failed"})

    assert refusal.value.code == "provider_runtime_recovery_failed"
    assert invocation_id in str(refusal.value)
    assert record_path.exists()
    operator.acknowledge_recovery({invocation_id: "handled"})
    assert not record_path.exists()

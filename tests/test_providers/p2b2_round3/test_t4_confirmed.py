"""Round 3 - re-establish the round-1 (C-*) and round-2 (K-*) CONFIRMED lists.

Only the checks `p2b2_round5/test_v5_confirmed.py` does not supersede remain here
(the static C-4 scan, the foreign-invocation C-7 refusal, the C-8 key-collision
check, the C-14 envelope guards and the two executor projection checks).
"""

from __future__ import annotations

import inspect
import json
import os
import re
from pathlib import Path
from typing import get_args

import pytest

import cruxible_core.providers.provider_local_runtime as runtime_module
import cruxible_core.service.procedures.procedure_runs as procedure_run_service
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.results import (
    ProcedureInternalFailureCodeV1,
    ProcedureOperationalFailureCodeV1,
)
from cruxible_client.contracts.provider_execution import (
    ProviderSecretReferenceV1,
)
from cruxible_core.procedures.execution import ProcedureExecutor
from cruxible_core.providers.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.providers.provider_local_runtime import (
    FileProviderSecretStore,
    ProviderLocalRuntimeRefused,
    provider_environment_secret_key,
)
from cruxible_core.providers.provider_outcomes import (
    ABSORBABLE_PROVIDER_REFUSALS,
    map_provider_refusal,
)
from cruxible_core.providers.provider_process_leases import (
    ProviderProcessLeaseStore,
)
from tests.core_support._p2b1_support import install_demo_classifier
from tests.test_procedures.test_procedure_execution import _Authority, _Contracts
from tests.test_providers.test_provider_invocation_journal import (
    _accepted_one_provider,
    _Invoker,
    _prepared_v5,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------ C-4 / C-7


def test_c7_lease_record_integrity(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    record = store.publish("sha256:" + "c" * 64, pid=os.getpid(), process_group_id=os.getpgid(0))
    assert oct(record.stat().st_mode)[-3:] == "600"
    assert oct((short_root / "l").stat().st_mode)[-3:] == "700"
    raw = record.read_bytes()
    assert canonical_bytes(json.loads(raw)) == raw
    document = json.loads(raw)
    assert set(document) == {
        "invocation_id",
        "pid",
        "process_group_id",
        "session_id",
        "boot_id",
        "process_start_time",
    }
    # A record naming another invocation is refused.
    other_record, _control = store.paths("sha256:" + "d" * 64)
    other_record.write_bytes(raw)
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        store.require("sha256:" + "d" * 64, timeout_seconds=0.2)
    assert caught.value.code == "provider_process_lease_invalid"
    # Non-canonical bytes are refused.
    record.write_bytes(b'{"pid": 1,\n "invocation_id": "x"}')
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        store.require("sha256:" + "c" * 64, timeout_seconds=0.2)
    assert caught.value.code == "provider_process_lease_invalid"


def test_c4_no_secret_reaches_argv_env_or_the_child_environment() -> None:
    source = inspect.getsource(runtime_module._run_child)
    environment = re.search(r"environment = \{(.*?)\n    \}", source, re.S)
    assert environment is not None
    assert "CRUXIBLE_PROVIDER_SECRET" not in environment.group(1)
    assert set(re.findall(r'"([A-Z_]+)":', environment.group(1))) == {
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
    }
    command = re.search(r"command = \[(.*?)\]", source, re.S)
    assert command is not None
    assert "secret_resolver" not in command.group(1).lower()
    assert "secret_values" not in command.group(1).lower()
    scan = inspect.getsource(runtime_module._assert_no_secret)
    assert "raw[::-1]" in scan and "b64encode" in scan
    invoke = inspect.getsource(runtime_module.LocalProviderExecutionDriver.invoke)
    assert invoke.count("_assert_no_secret") == 3
    assert 'where="provider stdout"' in invoke and 'where="provider stderr"' in invoke
    assert 'where="run context"' in invoke


# ------------------------------------------------------------------ C-8


def test_c8_custody_store_permissions_and_traversal(short_root: Path) -> None:
    store = FileProviderSecretStore(short_root / "secrets")
    assert oct((short_root / "secrets").stat().st_mode)[-3:] == "700"
    for bad in ("a/b", "a\\b", "..", ".", "a\x00b"):
        with pytest.raises(Exception):
            ProviderSecretReferenceV1(
                resolver_kind="environment", realm=bad, name="n", epoch="e", purpose="p"
            )
    key_a = provider_environment_secret_key(
        ProviderSecretReferenceV1(
            resolver_kind="environment", realm="billing", name="api_key", epoch="v1", purpose="p"
        )
    )
    key_b = provider_environment_secret_key(
        ProviderSecretReferenceV1(
            resolver_kind="environment", realm="billing", name="api", epoch="key_v1", purpose="p"
        )
    )
    assert key_a != key_b
    assert store is not None


# ------------------------------------------------------------------ C-14


def test_c14_wire_law_guards_still_hold() -> None:
    from cruxible_core.providers import provider_outcomes
    from cruxible_core.providers.provider_runtime_contract import (
        ProviderRuntimeResultEnvelopeV1,
        ProviderRuntimeRunContextV1,
    )

    for model in (ProviderRuntimeResultEnvelopeV1, ProviderRuntimeRunContextV1):
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["frozen"] is True
    guard = inspect.getsource(provider_outcomes)
    assert "Provider outcome mapping does not equal the mirrored runtime vocabulary" in guard
    assert all(
        provider_outcomes._MAPPING[code] == ("node_refusal", "input")
        for code in ABSORBABLE_PROVIDER_REFUSALS
    )


# ------------------------------------------------------------------ N-4 / task 7


def test_the_three_fence_codes_are_public_and_project_exactly(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes = (
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
        "provider_process_lease_echo_failed",
        "provider_process_lease_echo_mismatch",
        "provider_process_group_survived_recovery",
    )
    public = set(get_args(ProcedureInternalFailureCodeV1)) | set(
        get_args(ProcedureOperationalFailureCodeV1)
    )
    assert [code for code in codes if code not in public] == []
    for code in codes:
        assert map_provider_refusal(code, message="m", detail={}).code == code

    for code in codes[2:]:
        terminal = _project(short_root, monkeypatch, code)
        assert terminal.code == code, (code, terminal.code)


def _project(root: Path, monkeypatch: pytest.MonkeyPatch, code: str):  # type: ignore[no-untyped-def]
    class _Refusing:
        def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
            return _Invoker().bind_provider(occurrence=occurrence)

        def invoke_provider(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise ProviderLocalRuntimeRefused(code, "fence refusal")

    accepted = _accepted_one_provider()
    workspace = root / code
    workspace.mkdir(parents=True, exist_ok=True)
    prepared, fixture = _prepared_v5(accepted, workspace)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_Refusing(),
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    records = tuple(
        fixture.journal.all_records(
            prepared.admission.journal_stream, prepared.admission.journal_partition_id
        )
    )
    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_a, **_k: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(
        _Instance(),  # type: ignore[arg-type]
        run_id=prepared.admission.run_id,
    )
    assert state.terminal is not None
    return state.terminal


def test_a_degraded_lane_refusal_preserves_its_typed_reason_at_projection(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's typed reason reaches `ProviderLocalRuntimeRefused.details`, but
    `provider_unavailable` maps to a NODE refusal and `_RunRefusal` carries no detail,
    so the projected run never names why the lane is down."""

    from cruxible_core.runtime.provider_runtime import _UnavailableProviderRuntimeInvoker

    invoker = _UnavailableProviderRuntimeInvoker(
        code="provider_process_lease_invalid",
        detail="provider_process_lease_invalid: too long",
    )

    accepted = _accepted_one_provider()
    workspace = short_root / "degraded"
    workspace.mkdir(parents=True)
    prepared, fixture = _prepared_v5(accepted, workspace)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    records = tuple(
        fixture.journal.all_records(
            prepared.admission.journal_stream, prepared.admission.journal_partition_id
        )
    )
    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_a, **_k: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(
        _Instance(),  # type: ignore[arg-type]
        run_id=prepared.admission.run_id,
    )
    assert state.terminal is not None
    assert state.terminal.code == "provider_unavailable"
    rendered = json.dumps(state.terminal.model_dump(mode="json"))
    assert "too long" in rendered, rendered
    assert "provider_process_lease_invalid" in rendered

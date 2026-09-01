"""Active drift guard for core's exact provider-runtime v1 wire mirror."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from cruxible_core.playbill.provider_runtime_contract import (
    PROVIDER_RUNTIME_CONTRACT_COMMIT,
    PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS,
    PROVIDER_RUNTIME_PROTOCOL,
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeProtocolVersionV1,
    ProviderRuntimeProviderErrorPayloadV1,
    ProviderRuntimeRefusalV1,
    ProviderRuntimeResultEnvelopeV1,
    ProviderRuntimeRunContextV1,
    ProviderRuntimeSecretChannelSpecV1,
    ProviderRuntimeSecretRefV1,
    ProviderRuntimeTraceV1,
    ProviderRuntimeWireError,
    parse_provider_runtime_context,
    parse_provider_runtime_result,
    provider_runtime_refusal_codes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "tests/fixtures/provider_runtime_contract_v1.json"
PROVIDER_RUNTIME_CONTRACT_FIXTURE_DIGEST = (
    "sha256:56b1d2799515c84f3848d08c79b33a3297280ebef82232701612ccf3ce4488c7"
)

_MODEL_MAP: dict[str, type[BaseModel]] = {
    "Budgets": ProviderRuntimeBudgetsV1,
    "ProtocolVersion": ProviderRuntimeProtocolVersionV1,
    "ProviderErrorPayload": ProviderRuntimeProviderErrorPayloadV1,
    "Refusal": ProviderRuntimeRefusalV1,
    "ResultEnvelope": ProviderRuntimeResultEnvelopeV1,
    "RunContext": ProviderRuntimeRunContextV1,
    "SecretChannelSpec": ProviderRuntimeSecretChannelSpecV1,
    "SecretRef": ProviderRuntimeSecretRefV1,
    "Trace": ProviderRuntimeTraceV1,
}


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _normalize_schema(
    value: Any,
    *,
    root: dict[str, Any],
    names: dict[str, str],
) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            target: Any = root
            for part in value["$ref"].removeprefix("#/").split("/"):
                target = target[part]
            return _normalize_schema(target, root=root, names=names)
        return {
            names.get(key, key): _normalize_schema(item, root=root, names=names)
            for key, item in value.items()
            if key not in {"$defs", "description", "title"}
        }
    if isinstance(value, list):
        return [_normalize_schema(item, root=root, names=names) for item in value]
    if isinstance(value, str):
        for core_name, provider_name in names.items():
            value = value.replace(core_name, provider_name)
        return value
    return value


def test_provider_runtime_fixture_is_the_atomic_pinned_catalog() -> None:
    raw = FIXTURE_PATH.read_bytes()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == PROVIDER_RUNTIME_CONTRACT_FIXTURE_DIGEST
    fixture = _fixture()
    assert fixture["provider_commit"] == PROVIDER_RUNTIME_CONTRACT_COMMIT
    assert fixture["protocol_version"] == PROVIDER_RUNTIME_PROTOCOL
    assert tuple(fixture["dynamic_endpoint_forms"]) == PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS
    assert tuple(fixture["refusal_codes"]) == provider_runtime_refusal_codes()


def test_provider_runtime_schemas_match_the_pinned_provider_models() -> None:
    fixture = _fixture()
    names = {model.__name__: provider_name for provider_name, model in _MODEL_MAP.items()}
    actual = {}
    for provider_name, model in sorted(_MODEL_MAP.items()):
        schema = model.model_json_schema()
        actual[provider_name] = _normalize_schema(schema, root=schema, names=names)
    expected = {}
    for name, schema in fixture["schemas"].items():
        expected[name] = _normalize_schema(schema, root=schema, names={})
    assert actual == expected


def test_provider_runtime_valid_vectors_round_trip_exactly() -> None:
    fixture = _fixture()["valid_vectors"]
    context = parse_provider_runtime_context(
        json.dumps(fixture["run_context"], separators=(",", ":")).encode()
    )
    assert context.model_dump(mode="json") == fixture["run_context"]
    for payload in fixture["results"].values():
        result = parse_provider_runtime_result(json.dumps(payload, separators=(",", ":")).encode())
        assert result.model_dump(mode="json") == payload


def test_provider_runtime_invalid_vectors_fail_closed() -> None:
    fixture = _fixture()["invalid_vectors"]
    with pytest.raises(ProviderRuntimeWireError) as unknown:
        parse_provider_runtime_context(json.dumps(fixture["context_unknown_field"]).encode())
    assert unknown.value.code == "unknown_run_context_field"
    with pytest.raises(ProviderRuntimeWireError) as malformed:
        parse_provider_runtime_result(json.dumps(fixture["result_missing_output"]).encode())
    assert malformed.value.code == "provider_protocol_violation"


def test_only_run_context_additive_is_open() -> None:
    fixture = _fixture()["valid_vectors"]["run_context"]
    ProviderRuntimeRunContextV1.model_validate(
        {**fixture, "additive": {"future_field": {"nested": True}}}
    )
    with pytest.raises(ValidationError):
        ProviderRuntimeRunContextV1.model_validate({**fixture, "future_field": True})
    result = _fixture()["valid_vectors"]["results"]["ok"]
    with pytest.raises(ValidationError):
        ProviderRuntimeResultEnvelopeV1.model_validate({**result, "future_field": True})


def test_provider_runtime_mapping_and_number_failures_are_not_silently_normalized() -> None:
    fixture = _fixture()["valid_vectors"]
    with pytest.raises(ProviderRuntimeWireError, match="mapping keys"):
        ProviderRuntimeRunContextV1.model_validate({**fixture["run_context"], 1: "not-a-key"})
    payload = fixture["results"]["ok"]
    with pytest.raises(ProviderRuntimeWireError) as nonfinite:
        ProviderRuntimeResultEnvelopeV1.model_validate(
            {**payload, "output": {"nested": [float("nan")]}}
        )
    assert nonfinite.value.code == "non_finite_output"

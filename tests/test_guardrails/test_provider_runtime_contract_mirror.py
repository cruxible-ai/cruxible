"""Active drift guard for core's exact provider-runtime protocol 2.0 wire mirror."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from cruxible_client.contracts.provider_operations import (
    OPERATION_SIGNATURE_DOMAIN,
    OperationRefusal,
    OperationSignature,
    operation_signature_digest,
)
from cruxible_client.contracts.provider_registration import (
    DeclaredOperation,
    PackageRegistration,
    RegistrationRuntimeRequirement,
    evaluate_registration_law,
)
from cruxible_client.contracts.providers import (
    PROVIDER_IMPLEMENTATION_DOMAIN,
    ProviderImplementationManifest,
    ProviderRuntimeManifest,
    provider_implementation_digest,
)
from cruxible_client.contracts.shapes import ContractRef
from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_REFUSAL_CODE_RE,
    PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS,
    PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES,
    PROVIDER_RUNTIME_PROTOCOL,
    PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES,
    ProviderRuntimeBudgets,
    ProviderRuntimeProtocolVersion,
    ProviderRuntimeProviderErrorPayload,
    ProviderRuntimeRefusal,
    ProviderRuntimeResultEnvelope,
    ProviderRuntimeRunContext,
    ProviderRuntimeSecretChannelSpec,
    ProviderRuntimeSecretRef,
    ProviderRuntimeTrace,
    ProviderRuntimeWireError,
    parse_provider_runtime_context,
    parse_provider_runtime_result,
    provider_runtime_refusal_codes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "tests/fixtures/provider_runtime_contract_v2.json"
#: The vendored bytes: cruxible-providers ``PROVIDER_RUNTIME_CONTRACT_COMMIT``'s
#: packages/cruxible-provider-runtime/tests/fixtures/provider_runtime_contract_v2.json,
#: exactly: registration schema 3 (operations that serve capabilities, with
#: carried shapes) and the result's generic ``incomplete_captures``.
PROVIDER_RUNTIME_CONTRACT_FIXTURE_DIGEST = (
    "sha256:dea885c2e80000ec26553ed73ff264d5ca3e9c902335d329eb83d6c7dcadb257"
)

_MODEL_MAP: dict[str, type[BaseModel]] = {
    # The package-side declarations, which core reads through its own models.
    # ``PackageRegistration`` is not compared schema for schema: core types the
    # carried Contracts and kinds with its own grammar, which the runtime holds
    # loosely (``test_registration_documents_validate_under_cores_model``).
    "ContractRef": ContractRef,
    "ImplementationManifest": ProviderImplementationManifest,
    "OperationRefusal": OperationRefusal,
    "ProviderManifest": ProviderRuntimeManifest,
    "ProviderOperation": DeclaredOperation,
    "RuntimeRequirement": RegistrationRuntimeRequirement,
    # The wire.
    "Budgets": ProviderRuntimeBudgets,
    "ProtocolVersion": ProviderRuntimeProtocolVersion,
    "ProviderErrorPayload": ProviderRuntimeProviderErrorPayload,
    "Refusal": ProviderRuntimeRefusal,
    "ResultEnvelope": ProviderRuntimeResultEnvelope,
    "RunContext": ProviderRuntimeRunContext,
    "SecretChannelSpec": ProviderRuntimeSecretChannelSpec,
    "SecretRef": ProviderRuntimeSecretRef,
    "Trace": ProviderRuntimeTrace,
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
    assert fixture["protocol_version"] == PROVIDER_RUNTIME_PROTOCOL
    assert tuple(fixture["dynamic_endpoint_forms"]) == PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS
    assert tuple(fixture["runtime_refusal_codes"]) == provider_runtime_refusal_codes()
    assert frozenset(fixture["runtime_refusal_codes"]) == PROVIDER_RUNTIME_RESERVED_REFUSAL_CODES
    assert frozenset(fixture["harness_refusal_codes"]) == PROVIDER_RUNTIME_HARNESS_REFUSAL_CODES
    assert fixture["operation_refusal_code_pattern"] == PROVIDER_REFUSAL_CODE_RE.pattern


def test_implementation_digests_reproduce_the_runtime_goldens() -> None:
    fixture = _fixture()
    assert fixture["domains"]["implementation"] == PROVIDER_IMPLEMENTATION_DOMAIN
    for name, case in fixture["implementation_cases"].items():
        assert set(case) == {"entrypoint", "distribution_sha256"}
        assert (
            provider_implementation_digest(**case)
            == fixture["digest_goldens"]["implementation"][name]
        )


def test_operation_signature_digests_reproduce_the_runtime_goldens() -> None:
    fixture = _fixture()
    assert fixture["domains"]["operation_signature"] == OPERATION_SIGNATURE_DOMAIN
    for name, case in fixture["operation_signature_cases"].items():
        signature = OperationSignature.model_validate(
            {
                "in": {"name": "fixture.in", "version": 1, "digest": case["in_digest"]},
                "out": {"name": "fixture.out", "version": 1, "digest": case["out_digest"]},
                "effect": case["effect"],
            }
        )
        assert (
            operation_signature_digest(signature)
            == fixture["digest_goldens"]["operation_signature"][name]
        )


def test_provider_runtime_schemas_match_the_pinned_provider_models() -> None:
    fixture = _fixture()
    names = {model.__name__: provider_name for provider_name, model in _MODEL_MAP.items()}
    actual = {}
    for provider_name, model in sorted(_MODEL_MAP.items()):
        schema = model.model_json_schema()
        actual[provider_name] = _normalize_schema(schema, root=schema, names=names)
    expected = {}
    for name, schema in fixture["schemas"].items():
        if name in _MODEL_MAP:
            expected[name] = _normalize_schema(schema, root=schema, names={})
    assert actual == expected
    assert set(fixture["schemas"]) - set(_MODEL_MAP) == {"PackageRegistration"}


def test_the_registration_document_has_cores_top_level_fields() -> None:
    """The runtime's registration and core's agree on every top-level field and its need."""

    runtime = _fixture()["schemas"]["PackageRegistration"]
    core = PackageRegistration.model_json_schema()
    assert set(runtime["properties"]) == set(core["properties"])
    assert set(runtime.get("required", ())) == set(core.get("required", ()))


def test_registration_documents_validate_under_cores_model() -> None:
    """Every schema-3 registration the providers checkout ships is valid, and lawful, in core.

    The runtime writes the registration; core reads it with its own stricter
    model (typed Contracts and layouts) and judges it with the registration law.
    """

    from tests.support.provider_checkout import provider_checkout_path

    checkout = provider_checkout_path()
    if checkout is None:
        pytest.skip("set CRUXIBLE_PROVIDERS_CHECKOUT to validate its registrations")
    read = 0
    for path in sorted(checkout.glob("packages/*/src/*/registration.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema_version") != 3:
            continue  # a package still pending its schema-3 rewrite
        registration = PackageRegistration.model_validate(document)
        assert evaluate_registration_law(registration) == (), path
        read += 1
    assert read


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
    # Another major refuses as the version mismatch it is, before any field is read.
    with pytest.raises(ProviderRuntimeWireError) as context_major:
        parse_provider_runtime_context(json.dumps(fixture["context_protocol_1"]).encode())
    assert context_major.value.code == "unsupported_protocol"
    with pytest.raises(ProviderRuntimeWireError) as result_major:
        parse_provider_runtime_result(json.dumps(fixture["result_protocol_1"]).encode())
    assert result_major.value.code == "unsupported_protocol"
    with pytest.raises(ProviderRuntimeWireError) as unknown:
        parse_provider_runtime_context(json.dumps(fixture["context_unknown_field"]).encode())
    assert unknown.value.code == "unknown_run_context_field"
    with pytest.raises(ProviderRuntimeWireError) as malformed:
        parse_provider_runtime_result(json.dumps(fixture["result_missing_output"]).encode())
    assert malformed.value.code == "provider_protocol_violation"
    # Only an ok result may say which capture lists it left incomplete.
    with pytest.raises(ProviderRuntimeWireError) as incomplete:
        parse_provider_runtime_result(
            json.dumps(fixture["result_incomplete_captures_not_ok"]).encode()
        )
    assert incomplete.value.code == "provider_protocol_violation"


def test_only_run_context_additive_is_open() -> None:
    fixture = _fixture()["valid_vectors"]["run_context"]
    ProviderRuntimeRunContext.model_validate(
        {**fixture, "additive": {"future_field": {"nested": True}}}
    )
    with pytest.raises(ValidationError):
        ProviderRuntimeRunContext.model_validate({**fixture, "future_field": True})
    result = _fixture()["valid_vectors"]["results"]["ok"]
    with pytest.raises(ValidationError):
        ProviderRuntimeResultEnvelope.model_validate({**result, "future_field": True})


def test_provider_runtime_mapping_and_number_failures_are_not_silently_normalized() -> None:
    fixture = _fixture()["valid_vectors"]
    with pytest.raises(ProviderRuntimeWireError, match="mapping keys"):
        ProviderRuntimeRunContext.model_validate({**fixture["run_context"], 1: "not-a-key"})
    payload = fixture["results"]["ok"]
    with pytest.raises(ProviderRuntimeWireError) as nonfinite:
        ProviderRuntimeResultEnvelope.model_validate(
            {**payload, "output": {"nested": [float("nan")]}}
        )
    assert nonfinite.value.code == "non_finite_output"

"""Tests for workflow helper modules and provider registry."""

from __future__ import annotations

import sys
from datetime import date
from math import pi

import httpx
import pytest

from cruxible_core.config.schema import (
    ContractSchema,
    CoreConfig,
    EntityTypeSchema,
    EnumSchema,
    PropertySchema,
    ProviderSchema,
)
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.provider.registry import resolve_provider
from cruxible_core.provider.types import ProviderContext
from cruxible_core.workflow.contracts import validate_contract_payload
from cruxible_core.workflow.refs import preview_value, resolve_value


def _base_config(**overrides: object) -> CoreConfig:
    config = CoreConfig(
        name="workflow_helpers",
        entity_types={
            "Thing": EntityTypeSchema(
                properties={"id": PropertySchema(type="string", primary_key=True)}
            )
        },
        relationships=[],
        **overrides,
    )
    return config


def _provider_context() -> ProviderContext:
    return ProviderContext(
        workflow_name="wf",
        step_id="step",
        provider_name="provider",
        provider_version="1.0.0",
    )


class TestContractValidation:
    def test_applies_defaults_and_normalizes_dates(self) -> None:
        config = _base_config(
            contracts={
                "Input": ContractSchema(
                    fields={
                        "name": PropertySchema(type="string"),
                        "run_date": PropertySchema(type="date"),
                        "score": PropertySchema(type="float", default=1.25),
                        "tag": PropertySchema(type="string", optional=True),
                    }
                )
            }
        )

        payload = validate_contract_payload(
            config,
            "Input",
            {"name": "alpha", "run_date": date(2026, 3, 21)},
            subject="Workflow input",
            error_factory=ConfigError,
        )

        assert payload == {
            "name": "alpha",
            "run_date": "2026-03-21",
            "score": 1.25,
        }

    def test_rejects_extra_fields(self) -> None:
        config = _base_config(
            contracts={"Input": ContractSchema(fields={"name": PropertySchema(type="string")})}
        )

        with pytest.raises(ConfigError, match="unexpected field 'extra'"):
            validate_contract_payload(
                config,
                "Input",
                {"name": "alpha", "extra": True},
                subject="Workflow input",
                error_factory=ConfigError,
            )

    def test_accepts_builtin_json_object_contract(self) -> None:
        config = _base_config()

        payload = validate_contract_payload(
            config,
            "cruxible.JsonObject",
            {"name": "alpha", "metadata": {"tier": 1}},
            subject="Provider input",
            error_factory=ConfigError,
        )

        assert payload == {"name": "alpha", "metadata": {"tier": 1}}

    def test_accepts_inline_contract(self) -> None:
        config = _base_config()

        payload = validate_contract_payload(
            config,
            ContractSchema(fields={"items": PropertySchema(type="json")}),
            {"items": [{"id": "A"}]},
            subject="Provider output",
            error_factory=ConfigError,
        )

        assert payload["items"] == [{"id": "A"}]

    def test_accepts_json_contract_fields(self) -> None:
        config = _base_config(
            contracts={
                "Input": ContractSchema(
                    fields={
                        "members": PropertySchema(type="json"),
                        "facts": PropertySchema(type="json", optional=True),
                    }
                )
            }
        )

        payload = validate_contract_payload(
            config,
            "Input",
            {
                "members": [{"from_id": "A", "to_id": "B"}],
                "facts": {"source": "catalog"},
            },
            subject="Provider output",
            error_factory=ConfigError,
        )

        assert payload["members"][0]["from_id"] == "A"

    def test_contract_fields_support_enum_ref(self) -> None:
        config = _base_config(
            enums={"status": EnumSchema(values=["open", "closed"])},
            contracts={
                "Input": ContractSchema(
                    fields={"status": PropertySchema(type="string", enum_ref="status")}
                )
            },
        )

        payload = validate_contract_payload(
            config,
            "Input",
            {"status": "open"},
            subject="Workflow input",
            error_factory=ConfigError,
        )
        assert payload["status"] == "open"

        with pytest.raises(ConfigError, match="value must be one of: open, closed"):
            validate_contract_payload(
                config,
                "Input",
                {"status": "pending"},
                subject="Workflow input",
                error_factory=ConfigError,
            )


class TestProviderRegistry:
    def test_rejects_unsupported_runtime(self) -> None:
        with pytest.raises(ValueError, match="runtime"):
            ProviderSchema(
                kind="function",
                contract_in="Input",
                contract_out="Output",
                ref="tests.support.workflow_test_providers.lift_predictor",
                version="1.0.0",
                runtime="node",
            )

    def test_rejects_invalid_ref_without_module_separator(self) -> None:
        provider = ProviderSchema(
            kind="function",
            contract_in="Input",
            contract_out="Output",
            ref="not_a_valid_ref",
            version="1.0.0",
        )

        with pytest.raises(ConfigError, match="invalid ref"):
            resolve_provider("provider", provider)

    def test_rejects_non_callable_ref(self) -> None:
        provider = ProviderSchema(
            kind="function",
            contract_in="Input",
            contract_out="Output",
            ref="math.pi",
            version="1.0.0",
        )

        assert pi == 3.141592653589793
        with pytest.raises(ConfigError, match="is not callable"):
            resolve_provider("provider", provider)

    def test_http_json_runtime_posts_json_and_returns_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"echo": "ok"}

        class FakeClient:
            def __init__(self, *, timeout: float) -> None:
                captured["timeout"] = timeout

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return None

            def post(
                self, url: str, *, json: dict[str, object], headers: dict[str, str]
            ) -> FakeResponse:
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return FakeResponse()

        monkeypatch.setattr("cruxible_core.provider.registry.httpx.Client", FakeClient)
        provider = ProviderSchema(
            kind="tool",
            contract_in="Input",
            contract_out="Output",
            ref="https://example.test/run",
            version="1.0.0",
            runtime="http_json",
            config={"headers": {"Authorization": "Bearer token"}, "timeout_s": 9},
        )

        result = resolve_provider("provider", provider)({"value": 7}, _provider_context())

        assert result == {"echo": "ok"}
        assert captured == {
            "timeout": 9.0,
            "url": "https://example.test/run",
            "json": {"value": 7},
            "headers": {"Authorization": "Bearer token"},
        }

    def test_http_json_runtime_rejects_invalid_json_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                raise ValueError("bad json")

        class FakeClient:
            def __init__(self, *, timeout: float) -> None:
                self.timeout = timeout

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return None

            def post(
                self, url: str, *, json: dict[str, object], headers: dict[str, str]
            ) -> FakeResponse:
                return FakeResponse()

        monkeypatch.setattr("cruxible_core.provider.registry.httpx.Client", FakeClient)
        provider = ProviderSchema(
            kind="tool",
            contract_in="Input",
            contract_out="Output",
            ref="https://example.test/run",
            version="1.0.0",
            runtime="http_json",
        )

        with pytest.raises(QueryExecutionError, match="response was not valid JSON"):
            resolve_provider("provider", provider)({"value": 7}, _provider_context())

    def test_http_json_runtime_surfaces_http_status_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request = httpx.Request("POST", "https://example.test/run")
        response = httpx.Response(503, request=request)

        class FakeResponse:
            def raise_for_status(self) -> None:
                raise httpx.HTTPStatusError("bad status", request=request, response=response)

            def json(self) -> dict[str, object]:
                return {"ok": False}

        class FakeClient:
            def __init__(self, *, timeout: float) -> None:
                self.timeout = timeout

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return None

            def post(
                self, url: str, *, json: dict[str, object], headers: dict[str, str]
            ) -> FakeResponse:
                return FakeResponse()

        monkeypatch.setattr("cruxible_core.provider.registry.httpx.Client", FakeClient)
        provider = ProviderSchema(
            kind="tool",
            contract_in="Input",
            contract_out="Output",
            ref="https://example.test/run",
            version="1.0.0",
            runtime="http_json",
        )

        with pytest.raises(QueryExecutionError, match="failed with status 503"):
            resolve_provider("provider", provider)({"value": 7}, _provider_context())

    def test_http_json_runtime_surfaces_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeClient:
            def __init__(self, *, timeout: float) -> None:
                self.timeout = timeout

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                return None

            def post(
                self, url: str, *, json: dict[str, object], headers: dict[str, str]
            ) -> dict[str, object]:
                raise httpx.TimeoutException("too slow")

        monkeypatch.setattr("cruxible_core.provider.registry.httpx.Client", FakeClient)
        provider = ProviderSchema(
            kind="tool",
            contract_in="Input",
            contract_out="Output",
            ref="https://example.test/run",
            version="1.0.0",
            runtime="http_json",
            config={"timeout_s": 1},
        )

        with pytest.raises(QueryExecutionError, match="timed out after 1.0s"):
            resolve_provider("provider", provider)({"value": 7}, _provider_context())

    def test_command_runtime_executes_and_reads_json_stdout(self) -> None:
        provider = ProviderSchema(
            kind="tool",
            contract_in="Input",
            contract_out="Output",
            ref=sys.executable,
            version="1.0.0",
            runtime="command",
            config={
                "args": [
                    "-c",
                    (
                        "import json, sys; "
                        "payload = json.load(sys.stdin); "
                        "print(json.dumps({'echo': payload['value']}))"
                    ),
                ]
            },
        )

        result = resolve_provider("provider", provider)({"value": 7}, _provider_context())

        assert result == {"echo": 7}

    def test_command_runtime_rejects_invalid_stdout_json(self) -> None:
        provider = ProviderSchema(
            kind="tool",
            contract_in="Input",
            contract_out="Output",
            ref=sys.executable,
            version="1.0.0",
            runtime="command",
            config={"args": ["-c", "print('not-json')"]},
        )

        with pytest.raises(QueryExecutionError, match="output was not valid JSON"):
            resolve_provider("provider", provider)({"value": 7}, _provider_context())

    def test_command_runtime_surfaces_nonzero_exit(self) -> None:
        provider = ProviderSchema(
            kind="tool",
            contract_in="Input",
            contract_out="Output",
            ref=sys.executable,
            version="1.0.0",
            runtime="command",
            config={"args": ["-c", "import sys; sys.stderr.write('boom'); sys.exit(7)"]},
        )

        with pytest.raises(QueryExecutionError, match="exited with status 7: boom"):
            resolve_provider("provider", provider)({"value": 7}, _provider_context())

    def test_command_runtime_surfaces_timeout(self) -> None:
        provider = ProviderSchema(
            kind="tool",
            contract_in="Input",
            contract_out="Output",
            ref=sys.executable,
            version="1.0.0",
            runtime="command",
            config={"timeout_s": 0.01, "args": ["-c", "import time; time.sleep(1); print('{}')"]},
        )

        with pytest.raises(QueryExecutionError, match="timed out after 0.01s"):
            resolve_provider("provider", provider)({"value": 7}, _provider_context())


class TestWorkflowRefs:
    def test_preview_value_only_resolves_input_refs(self) -> None:
        value = {
            "sku": "$input.sku",
            "nested": ["$input.dates[1]", "$steps.lift.predicted_lift_pct"],
        }

        preview = preview_value(
            value,
            {"sku": "SKU-123", "dates": ["2026-03-01", "2026-03-07"]},
        )

        assert preview == {
            "sku": "SKU-123",
            "nested": ["2026-03-07", "$steps.lift.predicted_lift_pct"],
        }

    def test_resolve_value_supports_nested_step_paths_and_indices(self) -> None:
        resolved = resolve_value(
            {
                "sku": "$input.sku",
                "category": "$steps.context.results[0].properties.category",
                "lift": "$steps.lift.predicted_lift_pct",
            },
            {"sku": "SKU-123"},
            {
                "context": {
                    "results": [
                        {"properties": {"category": "soda"}},
                    ]
                },
                "lift": {"predicted_lift_pct": 0.12},
            },
        )

        assert resolved == {"sku": "SKU-123", "category": "soda", "lift": 0.12}

    def test_resolve_value_rejects_unknown_alias(self) -> None:
        with pytest.raises(QueryExecutionError, match="Unknown workflow step alias 'missing'"):
            resolve_value("$steps.missing.score", {}, {})

    def test_resolve_value_rejects_out_of_range_index(self) -> None:
        with pytest.raises(QueryExecutionError, match="index \\[1\\] is out of range"):
            resolve_value(
                "$steps.context.results[1].properties.category",
                {},
                {"context": {"results": [{"properties": {"category": "soda"}}]}},
            )

    def test_resolve_value_supports_item_refs_when_enabled(self) -> None:
        resolved = resolve_value(
            {
                "sku": "$item.product_sku",
                "reason": "$item.reason",
                "campaign_id": "$input.campaign_id",
            },
            {"campaign_id": "CMP-1"},
            {},
            item_payload={"product_sku": "SKU-123", "reason": "north bestseller"},
            allow_item=True,
        )

        assert resolved == {
            "sku": "SKU-123",
            "reason": "north bestseller",
            "campaign_id": "CMP-1",
        }

    def test_resolve_value_rejects_item_ref_when_disabled(self) -> None:
        with pytest.raises(
            QueryExecutionError,
            match="Unsupported workflow reference '\\$item.id'",
        ):
            resolve_value("$item.id", {}, {})

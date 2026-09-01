"""Generate the reviewed provider-runtime mirror fixture from a pinned checkout.

This updater is intentionally not part of ordinary CI.  It imports the provider
runtime only while producing bytes for review; core tests consume the committed
fixture and never import the provider package.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ROOT = Path(
    os.environ.get("CRUXIBLE_PROVIDERS_ROOT", "/Users/robertmalone/Git/cruxible-providers")
).resolve()
EXPECTED_COMMIT = "389e9f44de56c1adebae731228cf4628c6fbeca8"
RUNTIME_SRC = PROVIDER_ROOT / "packages/cruxible-provider-runtime/src"


def _schema(model: type[Any]) -> dict[str, Any]:
    return json.loads(json.dumps(model.model_json_schema(), sort_keys=True))


def main() -> None:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROVIDER_ROOT, text=True
    ).strip()
    if commit != EXPECTED_COMMIT:
        raise SystemExit(f"provider checkout is {commit}, expected {EXPECTED_COMMIT}")
    sys.path.insert(0, str(RUNTIME_SRC))

    from cruxible_provider_runtime.errors import ProviderErrorPayload, Refusal, RefusalCode
    from cruxible_provider_runtime.protocol import (
        Budgets,
        ProtocolVersion,
        ResultEnvelope,
        RunContext,
        SecretChannelSpec,
        SecretRef,
        Trace,
    )

    models = {
        "Budgets": Budgets,
        "ProtocolVersion": ProtocolVersion,
        "ProviderErrorPayload": ProviderErrorPayload,
        "Refusal": Refusal,
        "ResultEnvelope": ResultEnvelope,
        "RunContext": RunContext,
        "SecretChannelSpec": SecretChannelSpec,
        "SecretRef": SecretRef,
        "Trace": Trace,
    }
    context = RunContext(
        protocol_version="1.0",
        run_id="RUN-contract-vector",
        interface_id="demo.echo",
        interface_digest="sha256:" + "1a" * 32,
        implementation_digest="sha256:" + "2b" * 32,
        entrypoint="demo.echo:run",
        coordinates={"generation": 7},
        input={"value": "hello"},
        input_bucket="shape=text",
        capture_contract=None,
        budgets=Budgets(wall_clock_seconds=3.0, output_bytes=4096),
        declared_endpoints=("https://example.test",),
        secret_channel=SecretChannelSpec(
            fd=3, refs=(SecretRef(ref="operator/demo", purpose="test"),)
        ),
        additive={"future": {"kept": True}},
    )
    result_vectors = {
        "ok": ResultEnvelope(
            protocol_version="1.0", run_id=context.run_id, status="ok", output={"echo": "hello"}
        ),
        "refused": ResultEnvelope(
            protocol_version="1.0",
            run_id=context.run_id,
            status="refused",
            refusal=Refusal(
                code=RefusalCode.PROVIDER_DECLINED,
                message="declined by fixture",
                detail={"reason": "fixture"},
            ),
        ),
        "error": ResultEnvelope(
            protocol_version="1.0",
            run_id=context.run_id,
            status="error",
            error=ProviderErrorPayload(kind="FixtureError", message="fixture failure"),
        ),
    }
    provider_golden = json.loads(
        (
            PROVIDER_ROOT
            / "packages/cruxible-provider-runtime/tests/fixtures/golden/expected-digests.json"
        ).read_text(encoding="utf-8")
    )
    implementation_cases = json.loads(
        (
            PROVIDER_ROOT
            / "packages/cruxible-provider-runtime/tests/fixtures/golden/implementation-cases.json"
        ).read_text(encoding="utf-8")
    )
    document = {
        "provider_commit": commit,
        "protocol_version": "1.0",
        "schemas": {name: _schema(model) for name, model in sorted(models.items())},
        "valid_vectors": {
            "run_context": json.loads(context.model_dump_json()),
            "results": {
                name: json.loads(value.model_dump_json())
                for name, value in sorted(result_vectors.items())
            },
        },
        "invalid_vectors": {
            "context_unknown_field": {
                **json.loads(context.model_dump_json()),
                "not_additive": True,
            },
            "result_missing_output": {
                "protocol_version": "1.0",
                "run_id": context.run_id,
                "status": "ok",
                "output": None,
                "refusal": None,
                "error": None,
                "trace": {"endpoints_contacted": [], "events": [], "metrics": {}},
            },
        },
        "refusal_codes": sorted(code.value for code in RefusalCode),
        "dynamic_endpoint_forms": ["dynamic:target-from-run-input"],
        "provider_digest_goldens": provider_golden,
        "provider_implementation_cases": implementation_cases,
        "invocation_outcome_receipt_fields": [
            "duration_seconds",
            "dynamic_endpoint_forms",
            "endpoints_contacted",
            "implementation_digest",
            "input_bucket",
            "materialization_digest",
            "protocol_version",
            "status",
        ],
    }
    destination = REPO_ROOT / "tests/fixtures/provider_runtime_contract_v1.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {destination.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()

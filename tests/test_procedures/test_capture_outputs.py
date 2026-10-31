"""Capture outputs: a call commits each capture<K> field, and a trigger names exactly one.

An acquiring call's out Contract may carry several ``capture<K>`` fields; run
normally, each is committed byte-exact as its own Capture and the call's value
names every one. A Line's trigger substitutes one capture output by
``<alias>.<field>``: a bare alias naming a call with several capture fields is
``trigger_target_ambiguous``. Single-field substitution itself (direct ``line
run`` with an event, and the Line lane through a Trigger) is covered on the
workspace world in ``test_trigger_capture_inputs`` and ``test_manual_line_runs``.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.capture_outputs import (
    TriggerTargetRefused,
    capture_output_descriptors,
    trigger_capture_output,
)
from tests.support.operation_world import Invoker
from tests.test_procedures.test_b2_operations import run_line

LINE_NAME = "test.paired-line"


def _paired_world(tmp_path: Path, *, trigger_input: str | None = None) -> Any:
    from cruxible_client.authoring.inputs import LineInput
    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from cruxible_client.contracts.procedures.authored import ProcedureSpec
    from cruxible_client.contracts.providers import (
        ProviderDistributionRef,
        ProviderImplementationManifest,
        ProviderLocalDistributionPin,
        ProviderLocalEnvBackendPin,
        ProviderRuntimeManifest,
    )
    from cruxible_client.contracts.shapes import (
        Contract,
        contract_digest,
        contract_path,
        render_contract,
    )
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import record_contract
    from tests.core_support._support import initialize_local
    from tests.support.operation_world import (
        REFUSAL,
        VALUE,
        author,
        author_procedure,
        build_files,
        contract_files,
        operation,
    )

    capture_contract = record_contract(name="test.events-v1", replay="exact")
    request = Contract.model_validate(
        {"name": "demo.pair.request", "version": 1, "fields": {"query": {"type": "string"}}}
    )
    result = Contract.model_validate(
        {
            "name": "demo.pair.result",
            "version": 1,
            "fields": {
                "left": {"type": "capture<test.events-v1>"},
                "right": {"type": "capture<test.events-v1>"},
                "note": {"type": "string"},
            },
        }
    )
    served = (
        operation(
            "pair", effect="external_read", signature=("demo.pair.request@1", "demo.pair.result@1")
        ),
    )
    manifest = ProviderRuntimeManifest(
        provider_id="pairer",
        distribution=ProviderDistributionRef(name="pairer", version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Provider", backends=("local_env",), deterministic=True
            ),
        ),
    )
    document = PackageRegistrationDocumentV1(schema_version=2, manifest=manifest, operations=served)

    def digests(ref):  # type: ignore[no-untyped-def]
        for item in (VALUE, REFUSAL, request, result):
            if (ref.name, ref.version) == (item.name, item.version):
                return contract_digest(item).tagged
        return None

    build = document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name="pairer",
            version="1.0.0",
            filename="pairer-1.0.0-py3-none-any.whl",
            sha256="sha256:" + ("a" * 64),
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain="pairer",
        operations=document.pinned_operations(digests),
    )
    instance, _owner = initialize_local(tmp_path)
    accept_tree(
        instance,
        {
            **contract_files(),
            contract_path(request.name, request.version): render_contract(request),
            contract_path(result.name, result.version): render_contract(result),
            capture_contract_path(capture_contract.identity.name): render_capture_contract(
                capture_contract
            ),
        },
        name="pair-contracts",
    )
    accept_tree(instance, build_files(build), name="pair-build")
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.paired",
                "input": {},
                "output": {"left": "string", "right": "string", "note": "string"},
                "slots": {
                    "pair": {
                        "in": "demo.pair.request@1",
                        "out": "demo.pair.result@1",
                        "effect": "external_read",
                    }
                },
                "steps": [
                    {
                        "call": "doc",
                        "captures": {"left": "test.events-v1", "right": "test.events-v1"},
                        "slot": "pair",
                        "input": {"query": "orders"},
                    },
                    {
                        "project": "out",
                        "fields": {"left": "$doc.left", "right": "$doc.right", "note": "$doc.note"},
                    },
                ],
                "returns": "out",
                "budget": {"provider_calls": 1, "capture_bytes": 65536},
            }
        ),
    )
    line: dict[str, Any] = {
        "kind": "line",
        "name": LINE_NAME,
        "procedure_name": "test.paired",
        "parameters": {},
    }
    if trigger_input is not None:
        line["trigger_input"] = trigger_input
    author(instance, LineInput.model_validate(line), name="line")
    return instance


def _wire(content: bytes) -> dict[str, Any]:
    return {
        "source_identity": "demo.events",
        "coordinate_type": "database-snapshot-v1",
        "coordinate": "workspace",
        "selector_type": "relation-primary-key-v1",
        "selector": "orders",
        "replayability": "exact",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "byte_length": len(content),
        "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "observed_at": "2026-10-29T12:00:00Z",
    }


def test_a_call_with_two_capture_fields_commits_both_and_runs_normally(tmp_path: Path) -> None:
    instance = _paired_world(tmp_path)
    invoker = Invoker(
        output=lambda context: {
            "left": _wire(b'{"side": "left"}'),
            "right": _wire(b'{"side": "right"}'),
            "note": "paired",
        }
    )

    state, _ = run_line(instance, invoker=invoker, name=LINE_NAME)

    assert state.status == "succeeded", state.terminal
    assert state.result["note"] == "paired"
    left, right = state.result["left"], state.result["right"]
    assert left.startswith("sha256:") and right.startswith("sha256:") and left != right


def test_the_descriptors_name_each_capture_output_and_a_bare_alias_is_ambiguous(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path

    instance = _paired_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path("test.paired")
    definition = parse_procedure(tree[path], path=path).definition

    assert [
        (item.node_path, item.field, item.capture_kind)
        for item in capture_output_descriptors(definition)
    ] == [
        ("doc", "left", "test.events-v1"),
        ("doc", "right", "test.events-v1"),
    ]
    assert trigger_capture_output(definition, "doc.right").field == "right"
    with pytest.raises(TriggerTargetRefused) as caught:
        trigger_capture_output(definition, "doc")
    assert caught.value.code == "trigger_target_ambiguous"


def test_a_line_trigger_aimed_at_a_two_field_call_by_alias_is_refused(tmp_path: Path) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    with pytest.raises(AuthoringLoweringError, match="trigger_target_ambiguous"):
        _paired_world(tmp_path, trigger_input="doc")


def test_a_line_trigger_naming_one_capture_field_is_accepted(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec

    instance = _paired_world(tmp_path, trigger_input="doc.right")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line = parse_line_spec(tree[f"lines/{LINE_NAME}.json"], path=f"lines/{LINE_NAME}.json")

    assert line.trigger_input == "doc.right"
    assert line.trigger_event is not None
    assert line.trigger_event.capture_contract_identity.name == "test.events-v1"

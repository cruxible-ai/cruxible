"""List Captures (D-R37 a): one fetch returns a ``list<capture<*>>`` of Captures.

The provider's out Contract declares the field ``list<capture<*>>`` and the
call's ``captures:`` map binds the CaptureKind (a1). Each member is its own
Capture: committed under the bound kind, journaled with its own acquisition
and produced-Capture records (``<field>/<index>``, the index for order only)
and named by its identity, the request digest its coordinate carries (a2).
The Contract's ``max_items`` bounds the list and every member counts toward
the run's Capture bytes: past either, the call refuses before any member is
committed (a3). A failed member, oversized or off the permitted origins, is
dropped and its list is incomplete (a4).

The run's value is returned through ``returns`` (lowered to the internal
projection), so these Procedures stay inside the template.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import CallStep, ReturnFields
from tests.support.operation_world import Invoker
from tests.support.procedures import procedure_limits, procedure_spec
from tests.test_procedures.test_b2_operations import run_line

LINE_NAME = "test.pages-line"
KIND = "test.page-v1"
ORIGIN = "https://pages.test"


def pages_world(
    tmp_path: Path,
    *,
    max_items: int = 3,
    capture_bytes: int = 65536,
    acquisition: dict[str, Any] | None = None,
    trigger_input: str | None = None,
) -> Any:
    from cruxible_client.authoring.inputs import LineInput
    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
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
    from tests.core_support._light_capture_support import JSON_DECODING, http_contract
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

    kind = http_contract(name=KIND, max_bytes=4096, decoding=JSON_DECODING)
    request = Contract.model_validate(
        {"name": "demo.pages.request", "version": 1, "fields": {"feed": {"type": "string"}}}
    )
    result = Contract.model_validate(
        {
            "name": "demo.pages.result",
            "version": 1,
            "fields": {
                "pages": {"type": f"list<capture<*>>{{max_items: {max_items}}}"},
                "note": {"type": "string"},
            },
        }
    )
    served = (
        operation(
            "pages",
            effect="external_read",
            signature=("demo.pages.request@1", "demo.pages.result@1"),
        ),
    )
    manifest = ProviderRuntimeManifest(
        provider_id="pager",
        distribution=ProviderDistributionRef(name="pager", version="1.0.0"),
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
            name="pager",
            version="1.0.0",
            filename="pager-1.0.0-py3-none-any.whl",
            sha256="sha256:" + ("a" * 64),
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain="pager",
        operations=document.pinned_operations(digests),
    )
    instance, _owner = initialize_local(tmp_path)
    accept_tree(
        instance,
        {
            **contract_files(),
            contract_path(request.name, request.version): render_contract(request),
            contract_path(result.name, result.version): render_contract(result),
            capture_contract_path(KIND): render_capture_contract(kind),
        },
        name="pages-contracts",
    )
    accept_tree(instance, build_files(build), name="pages-build")
    author_procedure(
        instance,
        procedure_spec(
            reference_text=True,
            procedure="test.pages",
            input={},
            output={
                "pages": f"list<capture<*>>{{max_items: {max_items}}}",
                "note": "string",
            },
            slots={
                "pages": {
                    "in": "demo.pages.request@1",
                    "out": "demo.pages.result@1",
                    "effect": "external_read",
                }
            },
            steps=[
                CallStep(call="doc", captures={"pages": KIND}, slot="pages", input={"feed": "kev"}),
            ],
            returns=ReturnFields(fields={"pages": "$doc.pages", "note": "$doc.note"}),
            hard_caps=procedure_limits(provider_calls=1, capture_bytes=capture_bytes),
        ),
    )
    line: dict[str, Any] = {
        "kind": "line",
        "name": LINE_NAME,
        "procedure_name": "test.pages",
        "parameters": {},
    }
    if acquisition is not None:
        line["acquisition"] = acquisition
    if trigger_input is not None:
        line["trigger_input"] = trigger_input
    author(instance, LineInput.model_validate(line), name="line")
    return instance


def page_url(index: int) -> str:
    return f"{ORIGIN}/kev/{index}"


def page(
    content: bytes, index: int, *, url: str | None = None, request: bool = True
) -> dict[str, Any]:
    """One member's Capture wire: an http response whose coordinate names its request."""

    where = url or page_url(index)
    coordinate: dict[str, Any] = {
        "url": where,
        "final_url": where,
        "content_type": "application/json",
        "source": "packaged-recording",
        "status": 200,
    }
    if request:
        coordinate["request_digest"] = "sha256:" + hashlib.sha256(where.encode()).hexdigest()
    return {
        "source_identity": "test.kev-pages",
        "coordinate_type": "http-response-v1",
        "coordinate": coordinate,
        "selector_type": "whole-response-v1",
        "selector": {},
        "replayability": "attested_only",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "byte_length": len(content),
        "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "observed_at": "2026-10-29T12:00:00Z",
    }


def bodies(count: int) -> list[bytes]:
    return [f'{{"page": {index}}}'.encode() for index in range(count)]


def answer(members: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    return lambda context: {"pages": members, "note": "paged"}


def journal(instance: Any, run_id: str, event_kind: str) -> list[dict[str, Any]]:
    from tests.test_procedures.test_capture_outputs import _journal

    return _journal(instance, run_id, event_kind)


def test_each_list_member_is_its_own_capture_committed_in_order(tmp_path: Path) -> None:
    from cruxible_client.contracts.capture_reads import CaptureReadRequest
    from cruxible_client.contracts.captures import capture_contract_digest
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.core_support._light_capture_support import JSON_DECODING, http_contract

    instance = pages_world(tmp_path)
    content = bodies(3)
    members = [page(body, index) for index, body in enumerate(content)]
    state, _ = run_line(instance, invoker=Invoker(output=answer(members)), name=LINE_NAME)

    assert state.status == "succeeded", state.terminal
    assert state.result["note"] == "paged"
    produced = journal(instance, state.run_id, "produced_capture")
    # One produced Capture per member, keyed <field>/<index>, under the bound kind.
    assert [item["field"] for item in produced] == ["pages/0", "pages/1", "pages/2"]
    assert {item["input_name"] for item in produced} == {"doc.pages"}
    kind = capture_contract_digest(http_contract(name=KIND, max_bytes=4096, decoding=JSON_DECODING))
    assert {item["capture_contract_digest"] for item in produced} == {kind.tagged}
    # A member's identity is its request digest; the list keeps the provider's order.
    assert [item["member_request_digest"] for item in produced] == [
        member["coordinate"]["request_digest"] for member in members
    ]
    assert state.result["pages"] == [item["capture_digest"] for item in produced]
    decisions = [
        (item["field"], item["decision"]["disposition"])
        for item in journal(instance, state.run_id, "source_acquisition")
    ]
    assert decisions == [("pages/0", "selected"), ("pages/1", "selected"), ("pages/2", "selected")]
    (final,) = journal(instance, state.run_id, "attempt_finalized")
    assert final["capture_bytes"] == sum(len(body) for body in content)
    assert state.completeness is None
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    for item, body in zip(produced, content, strict=True):
        read = service_read_playbill_capture(
            reopened,
            request=CaptureReadRequest(capture_digest=item["capture_digest"], max_bytes=4096),
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
        assert read.status == "verified", (item["field"], read)
        assert read.envelope is not None
        assert read.envelope.commitment.digest == "sha256:" + hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("failure", ["oversized", "origin"])
def test_a_failed_member_is_dropped_and_its_list_is_incomplete(
    tmp_path: Path, failure: str
) -> None:
    """a4: an oversized or origin-ineligible member is dropped, whatever the rule says."""

    acquisition = {"doc.pages": {"origins": [ORIGIN]}} if failure == "origin" else None
    instance = pages_world(tmp_path, acquisition=acquisition)
    content = bodies(3)
    members = [page(body, index) for index, body in enumerate(content)]
    if failure == "oversized":
        members[1] = page(b"x" * 8192, 1)  # past the kind's 4096 bytes
    else:
        members[1] = page(content[1], 1, url="https://elsewhere.test/kev/1")
    state, _ = run_line(instance, invoker=Invoker(output=answer(members)), name=LINE_NAME)

    assert state.status == "succeeded", state.terminal
    produced = journal(instance, state.run_id, "produced_capture")
    assert [item["field"] for item in produced] == ["pages/0", "pages/2"]
    assert state.result["pages"] == [item["capture_digest"] for item in produced]
    decisions = {
        item["field"]: (item["decision"]["disposition"], item["decision"]["reason_codes"])
        for item in journal(instance, state.run_id, "source_acquisition")
    }
    reason = (
        "cruxible.acquisition.oversized"
        if failure == "oversized"
        else "cruxible.acquisition.origin_ineligible"
    )
    assert decisions == {
        "pages/0": ("selected", []),
        "pages/1": ("omitted", [reason]),
        "pages/2": ("selected", []),
    }
    # The list is incomplete: nothing downstream may read it as the whole source.
    assert state.completeness is not None and not state.completeness.complete
    assert "doc" in {reason.node for reason in state.completeness.reasons}


def test_a_list_past_max_items_refuses_before_any_member_is_committed(tmp_path: Path) -> None:
    instance = pages_world(tmp_path, max_items=2)
    members = [page(body, index) for index, body in enumerate(bodies(3))]
    state, _ = run_line(instance, invoker=Invoker(output=answer(members)), name=LINE_NAME)

    assert state.status != "succeeded"
    assert state.terminal.code == "contract_output_refused", state.terminal
    assert "max_items" in state.terminal.message
    assert journal(instance, state.run_id, "produced_capture") == []
    assert journal(instance, state.run_id, "source_acquisition") == []


def test_members_count_toward_capture_bytes_and_past_it_nothing_is_committed(
    tmp_path: Path,
) -> None:
    content = [bytes([65 + index]) * 1000 for index in range(3)]
    instance = pages_world(tmp_path, capture_bytes=2500)  # two members fit, three do not
    members = [page(body, index) for index, body in enumerate(content)]
    state, _ = run_line(instance, invoker=Invoker(output=answer(members)), name=LINE_NAME)

    assert state.status != "succeeded"
    assert state.terminal.code == "budget_exhausted", state.terminal
    assert "max_capture_bytes" in str(state.terminal), state.terminal
    assert journal(instance, state.run_id, "produced_capture") == []


@pytest.mark.parametrize("defect", ["no_request", "repeated_request"])
def test_a_members_identity_is_its_request_digest(tmp_path: Path, defect: str) -> None:
    """a2: a member names its request; one request answered twice is not two members."""

    instance = pages_world(tmp_path)
    content = bodies(3)
    members = [page(body, index) for index, body in enumerate(content)]
    if defect == "no_request":
        members[2] = page(content[2], 2, request=False)
    else:
        members[2] = page(content[2], 0)
    state, _ = run_line(instance, invoker=Invoker(output=answer(members)), name=LINE_NAME)

    assert state.status != "succeeded"
    assert state.terminal.code == "provider_protocol_violation", state.terminal
    assert journal(instance, state.run_id, "produced_capture") == []


def test_a_trigger_on_a_list_field_is_refused(tmp_path: Path) -> None:
    """a7: a capture_landing Trigger substitutes one Capture, never a list."""

    from cruxible_core.authoring.lowering import AuthoringLoweringError

    with pytest.raises(AuthoringLoweringError) as caught:
        pages_world(tmp_path, trigger_input="doc.pages")
    assert caught.value.code == "cruxible.authoring.line_trigger_target_list"

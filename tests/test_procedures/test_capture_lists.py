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
from tests.support.capabilities import capability, capability_files
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
            **capability_files(
                capability("demo.pages", in_=request, out=result, effect="external_read")
            ),
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
            slots={"pages": {"capability": "demo.pages@1"}},
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


# -- the absence witness over a list (a6) -----------------------------------------


def _witness(**changes: Any) -> Any:
    from cruxible_client.contracts.claim_evidence import AbsenceWitness
    from cruxible_client.contracts.projection import AcceptedCoordinate

    digest = "sha256:" + "1" * 64
    return AbsenceWitness.model_validate(
        {
            "run_id": "run-1",
            "procedure_digest": digest,
            "node_id": "write",
            "capture": "sha256:" + "a" * 64,
            "extract_spec": digest,
            "extractor": "json-pointer-v1",
            "logical_source": "test.kev-pages",
            "enumerated_key_digest": digest,
            "expect_coordinate": AcceptedCoordinate(
                git_oid="0" * 40,
                semantic_root=digest,
                generation_root=digest,
                compiler_digest=digest,
            ),
            **changes,
        }
    )


def test_one_capture_is_the_witness_one_tuple_case_byte_for_byte() -> None:
    from cruxible_client.contracts.canonical import canonical_bytes

    single = _witness()
    assert single.captures == (single.capture,)
    # The single-Capture witness keeps its exact shape: no members key at all.
    assert "members" not in single.model_dump(mode="json")
    listed = _witness(members=[single.capture, "sha256:" + "b" * 64])
    assert listed.captures == (single.capture, "sha256:" + "b" * 64)
    assert canonical_bytes(listed.model_dump(mode="json")) != canonical_bytes(
        single.model_dump(mode="json")
    )


@pytest.mark.parametrize(
    "members",
    [
        ["sha256:" + "b" * 64, "sha256:" + "a" * 64],  # capture is not the first member
        ["sha256:" + "a" * 64],  # one member is the plain one-Capture form
        ["sha256:" + "a" * 64, "sha256:" + "a" * 64],  # members are distinct
    ],
)
def test_a_witness_lists_two_or_more_distinct_members_led_by_its_capture(
    members: list[str],
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _witness(members=members)


def test_delivery_admits_a_list_witness_only_for_exactly_its_members() -> None:
    from types import SimpleNamespace

    from cruxible_core.procedures.terminal_dependencies import witnessed_list_capture

    first, second, other = ("sha256:" + c * 64 for c in "abc")
    witness = _witness(members=[first, second])

    def receipt(cited: str, absence: Any = witness) -> Any:
        return SimpleNamespace(value=SimpleNamespace(capture_digest=cited), absence=absence)

    assert witnessed_list_capture((second, first), receipt(first)) == first
    assert witnessed_list_capture((first, second, other), receipt(first)) is None
    assert witnessed_list_capture((first,), receipt(first)) is None
    assert witnessed_list_capture((first, second), receipt(second)) is None
    assert witnessed_list_capture((first, second), receipt(first, absence=_witness())) is None
    assert witnessed_list_capture((first, second), None) is None


# -- List cost: work counts at two sizes (review-template-b, finding 2) -------------
#
# Each count is taken from the production function at 10 members / 10 records (or
# absence changes) and at 100 / 100; each must grow with members + items, never
# with their product.

SIZES = [(10, 10), (100, 100)]


class _Digest(str):
    """A Capture digest that counts every equality comparison made against it."""

    compared = 0

    def __eq__(self, other: object) -> bool:
        type(self).compared += 1
        return str.__eq__(self, other)

    __hash__ = str.__hash__


class _Counted(list):  # type: ignore[type-arg]
    """A list that counts the elements every iteration over it visits."""

    visits = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        for item in super().__iter__():
            type(self).visits += 1
            yield item


def _digests(count: int, tag: str = "m") -> list[str]:
    return [
        "sha256:" + hashlib.sha256(f"{tag}{index}".encode()).hexdigest() for index in range(count)
    ]


@pytest.mark.parametrize(("members", "records"), SIZES)
def test_many_records_over_one_list_compare_each_root_once(members: int, records: int) -> None:
    """Membership: the extract's list is indexed once, and each record's root is one lookup."""

    from cruxible_core.procedures.claims_terminal import listed_capture_members

    _Digest.compared = 0
    _Counted.visits = 0
    listed = _Counted(_Digest(digest) for digest in _digests(members))
    index: dict[str, frozenset[str]] = {}
    # Each record's root is its own (equal, not identical) digest, as a receipt carries it.
    roots = [_Digest(str(listed[(record * 7) % members])) for record in range(records)]
    _Counted.visits = 0
    _Digest.compared = 0
    for root in roots:
        assert root in listed_capture_members(index, "entries", listed)
    # The list is read once for every record, and each record's root is compared once.
    assert _Counted.visits == members
    assert _Digest.compared == records


@pytest.mark.parametrize(("members", "records"), SIZES)
def test_many_records_over_one_list_visit_each_call_token_once(members: int, records: int) -> None:
    """Token visits: the call's tokens are indexed once for every member's lineage."""

    from cruxible_core.procedures.execution import _member_tokens
    from cruxible_core.procedures.terminal_dependencies import (
        policy_token,
        produced_capture_token,
    )

    digests = _digests(members)
    tokens = _Counted(
        [produced_capture_token(digest) for digest in digests]
        + [policy_token(digest) for digest in _digests(records, "p")]
    )
    _Counted.visits = 0
    lineage = _member_tokens(tokens, digests)
    assert _Counted.visits == members + records
    assert lineage == [frozenset({produced_capture_token(digest)}) for digest in digests]


@pytest.mark.parametrize(("members", "records"), SIZES)
def test_a_list_records_its_acquisition_outcome_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: int, records: int
) -> None:
    """Acquisition outcomes: one outcome per list, built once over every member."""

    from cruxible_core.procedures import execution

    built: list[int] = []
    outcome = execution.AcquisitionInputOutcomeV1

    def counting(**fields: Any) -> Any:
        if fields.get("input_name") == "doc.pages":
            built.append(len(fields.get("capture_digests", ())))
        return outcome(**fields)

    monkeypatch.setattr(execution, "AcquisitionInputOutcomeV1", counting)
    instance = pages_world(tmp_path, max_items=members)
    listed = [page(body, index) for index, body in enumerate(bodies(members))]
    state, _ = run_line(instance, invoker=Invoker(output=answer(listed)), name=LINE_NAME)
    assert state.status == "succeeded", state.terminal
    assert len(state.result["pages"]) == members
    assert built == [members]


@pytest.mark.parametrize(("members", "changes"), SIZES)
def test_many_absence_changes_on_one_witness_check_its_members_once(
    members: int, changes: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Witness/closure validation at preparation: the member set is built once per run."""

    from types import SimpleNamespace

    from cruxible_core.procedures import terminal_dependencies
    from cruxible_core.procedures.terminal_dependencies import (
        CaptureRootCache,
        witnessed_list_capture,
    )

    visited: list[int] = []
    build = terminal_dependencies._witness_member_set

    def counting(listed: Any) -> frozenset[str]:
        visited.append(len(listed))
        return build(listed)

    monkeypatch.setattr(terminal_dependencies, "_witness_member_set", counting)
    digests = _digests(members)
    cache = CaptureRootCache()
    for _ in range(changes):
        # Each item carries its own copy of the witness and its own closure.
        receipt = SimpleNamespace(
            value=SimpleNamespace(capture_digest=digests[0]),
            absence=_witness(capture=digests[0], members=list(digests)),
        )
        assert witnessed_list_capture(tuple(digests), receipt, cache=cache) == digests[0]
    assert visited == [members]


class _EpochResolver:
    """A request verification context with a body-change epoch and no memo of its own."""

    def __init__(self, epoch: object) -> None:
        self.epoch = epoch

    def _capture_proof_epoch(self) -> object:
        return self.epoch

    def recall_verified(self, capture_digest: str) -> None:
        return None


@pytest.mark.parametrize(("members", "changes"), SIZES)
def test_many_absence_changes_on_one_witness_verify_each_member_once(
    members: int, changes: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof verifications at acceptance: N Claims on one witness verify its M members once."""

    from types import SimpleNamespace

    from cruxible_client.contracts import claims

    digests = _digests(members)
    verified: list[str] = []

    def verify(capture: str, **kwargs: Any) -> Any:
        assert kwargs["require_production_proof"] is True
        verified.append(capture)
        return SimpleNamespace(source=SimpleNamespace(source_identity="test.kev-pages"))

    monkeypatch.setattr(claims, "verify_capture", verify)
    monkeypatch.setattr(claims, "capture_retained_body_digests", lambda envelope: ("body",))
    monkeypatch.setattr(claims, "_named_capture_contract", lambda store, digest: "contract")
    store = SimpleNamespace(verify=lambda digest: True)
    contracts = {"contract": [SimpleNamespace(contract=None)]}

    def judge(memo: dict[object, None], resolver: Any) -> None:
        for _ in range(changes):
            witness = _witness(capture=digests[0], members=list(digests))
            assert (
                claims.absence_witness_refusal(
                    witness,
                    capture_store=store,  # type: ignore[arg-type]
                    contracts_by_digest=contracts,  # type: ignore[arg-type]
                    historical_capture_contract=None,
                    ledger_resolver=None,
                    producer_artifact_digests=None,
                    producer_receipt_resolver=resolver,
                    verified=memo,
                )
                is None
            )

    memo: dict[object, None] = {}
    judge(memo, _EpochResolver("epoch-1"))
    assert verified == digests
    # Under a new body-change epoch the witness is verified again, once.
    verified.clear()
    judge(memo, _EpochResolver("epoch-2"))
    assert verified == digests
    # Without an epoch nothing is reused: the warm answer never outlives a body change.
    verified.clear()
    judge(memo, None)
    assert len(verified) == members * changes


@pytest.mark.parametrize(("members", "changes"), SIZES)
def test_many_absence_reobservations_on_one_witness_verify_each_member_once(
    members: int, changes: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof verifications at re-observation delivery: once per later member, per witness."""

    from types import SimpleNamespace

    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore
    from cruxible_core.service.evidence import capture_reads

    digests = _digests(members)
    verified: list[str] = []

    def verify(instance: Any, coordinate: Any, digest: str, **kwargs: Any) -> Any:
        verified.append(digest)
        return SimpleNamespace(
            envelope=SimpleNamespace(source=SimpleNamespace(source_identity="test.kev-pages"))
        )

    monkeypatch.setattr(capture_reads, "verify_accepted_capture", verify)
    store = ClaimReobservationStore.__new__(ClaimReobservationStore)
    store.instance = SimpleNamespace(  # type: ignore[assignment]
        body_store=lambda: SimpleNamespace(landings=SimpleNamespace(head=lambda: "landed")),
        capture_verification_context=lambda: None,
    )
    cited = SimpleNamespace(source=SimpleNamespace(source_identity="test.kev-pages"))
    memo: dict[object, None] = {}
    for _ in range(changes):
        refusal = store._witness_refusal(
            _witness(capture=digests[0], members=list(digests)),
            envelope=cited,  # type: ignore[arg-type]
            coordinate=SimpleNamespace(git_oid="0" * 40),  # type: ignore[arg-type]
            verified=memo,
        )
        assert refusal is None
    # The cited first member is verified by the caller; every later one once.
    assert verified == digests[1:]


# -- The provider's own completeness word (a4, provider side) -----------------------


def test_a_list_the_provider_says_is_incomplete_is_incomplete(tmp_path: Path) -> None:
    """Three members land, but the provider names ``pages`` incomplete: the call is."""

    instance = pages_world(tmp_path)
    members = [page(body, index) for index, body in enumerate(bodies(3))]
    invoker = Invoker(output=answer(members), incomplete_captures=["pages"])
    state, _ = run_line(instance, invoker=invoker, name=LINE_NAME)
    assert state.status == "succeeded", state.terminal
    # Every member is still committed; the list is incomplete all the same.
    assert len(state.result["pages"]) == 3
    assert state.completeness is not None and not state.completeness.complete
    (completed,) = journal(instance, state.run_id, "provider_invocation_completed")
    assert completed["receipt"]["incomplete_captures"] == ["pages"]
    decisions = journal(instance, state.run_id, "source_acquisition")
    assert {item["decision"]["disposition"] for item in decisions} == {"selected"}


def test_a_complete_answer_journals_no_incomplete_captures(tmp_path: Path) -> None:
    instance = pages_world(tmp_path)
    members = [page(body, index) for index, body in enumerate(bodies(3))]
    state, _ = run_line(instance, invoker=Invoker(output=answer(members)), name=LINE_NAME)
    assert state.status == "succeeded", state.terminal
    assert state.completeness is None
    (completed,) = journal(instance, state.run_id, "provider_invocation_completed")
    # Omitted when empty: an earlier receipt's bytes are unchanged.
    assert "incomplete_captures" not in completed["receipt"]


@pytest.mark.parametrize("named", [["note"], ["elsewhere"], ["pages", "pages"]])
def test_incomplete_captures_names_only_declared_capture_lists(
    tmp_path: Path, named: list[str]
) -> None:
    """A string field, an undeclared field or a repeat: provider_protocol_violation."""

    instance = pages_world(tmp_path)
    members = [page(body, index) for index, body in enumerate(bodies(3))]
    invoker = Invoker(output=answer(members), incomplete_captures=named)
    state, _ = run_line(instance, invoker=invoker, name=LINE_NAME)
    assert state.status != "succeeded"
    assert state.terminal.code == "provider_protocol_violation", state.terminal
    (completed,) = journal(instance, state.run_id, "provider_invocation_completed")
    assert completed["receipt"]["outcome"]["code"] == "provider_protocol_violation"
    assert completed["receipt"].get("output") is None
    assert journal(instance, state.run_id, "produced_capture") == []


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_a_later_witness_members_unreadable_envelope_refuses_as_unavailable(
    damage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-R37 a6, cold context: when a LATER member's envelope cannot be read
    (missing, or bytes that fail their address), naming its contract raises a
    storage error. The witness check answers "no longer retained" instead of
    letting the error escape evaluation, for inverse and retirement witnesses
    alike (both go through this one check)."""

    from types import SimpleNamespace

    from cruxible_client.contracts import claims
    from cruxible_client.contracts.errors import CasError

    digests = _digests(3)
    later = digests[-1]

    def read(digest: str, **kwargs: Any) -> bytes:
        if digest == later:
            raise CasError(f"{damage} envelope {digest}")
        raise AssertionError("only the later member is read cold in this test")

    original = claims._named_capture_contract

    def named(store: Any, digest: str) -> str | None:
        return "contract" if digest != later else original(store, digest)

    monkeypatch.setattr(claims, "_named_capture_contract", named)
    monkeypatch.setattr(
        claims,
        "verify_capture",
        lambda capture, **kwargs: SimpleNamespace(
            source=SimpleNamespace(source_identity="test.kev-pages")
        ),
    )
    monkeypatch.setattr(claims, "capture_retained_body_digests", lambda envelope: ("body",))
    store = SimpleNamespace(read=read, verify=lambda digest: True)
    refusal = claims.absence_witness_refusal(
        _witness(capture=digests[0], members=list(digests)),
        capture_store=store,  # type: ignore[arg-type]
        contracts_by_digest={"contract": [SimpleNamespace(contract=None)]},  # type: ignore[dict-item]
        historical_capture_contract=None,
        ledger_resolver=None,
        producer_artifact_digests=None,
        producer_receipt_resolver=None,
    )
    assert refusal == "Absence witness bytes are no longer retained; acquire the source again."

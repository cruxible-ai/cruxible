"""Capture outputs: each capture<K> field of a call is its own capture output.

An acquiring call's out Contract may carry several ``capture<K>`` fields, each
pinned to its own CaptureContract. Run normally, each field is its own capture
output ``<alias>.<field>``: committed byte-exact under its own CaptureContract,
decided by its own acquisition rule (a stale field omits or refuses while its
sibling is selected), journaled with its own produced-Capture record naming
its field path, associated with the occurrence per field, charged to the
Capture budget per field, and verifiable as evidence after the instance is
reopened. A trigger aimed at a call with several capture fields is refused
``trigger_target_ambiguous`` by any name. Single-field substitution itself is
covered on the workspace world: the manual ``line run`` with an event in
``test_manual_line_runs::test_a_manual_run_passes_its_event_as_the_lines_source_input``
and the Trigger lane in
``test_trigger_capture_inputs::test_a_trigger_substitutes_its_one_capture_output_by_name``.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import (
    CallStep,
    GuardStep,
    HaltStep,
    ProjectStep,
)
from cruxible_client.contracts.procedures.capture_outputs import (
    TriggerTargetRefused,
    capture_output_descriptors,
    trigger_capture_output,
)
from tests.support.capabilities import capability, capability_files
from tests.support.operation_world import Invoker
from tests.support.procedures import procedure_limits, procedure_spec
from tests.test_procedures.test_b2_operations import run_line

# Executor-only fixtures: these tests author Procedure step kinds the template
# closes at authoring (D-R37 h1) to cover their executor handlers (h3).
pytestmark = pytest.mark.executor_only

LINE_NAME = "test.paired-line"


def _paired_world(
    tmp_path: Path,
    *,
    trigger_input: str | None = None,
    acquisition: dict[str, Any] | None = None,
    returned: tuple[str, ...] = ("left", "note"),
    halt_after_call: bool = False,
    note_type: str = "string",
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

    # The two capture fields are pinned to two different CaptureContracts.
    capture_contract = record_contract(name="test.events-v1", replay="exact")
    other_contract = record_contract(name="test.other-v1", replay="exact")
    request = Contract.model_validate(
        {"name": "demo.pair.request", "version": 1, "fields": {"query": {"type": "string"}}}
    )
    result = Contract.model_validate(
        {
            "name": "demo.pair.result",
            "version": 1,
            "fields": {
                "left": {"type": "capture<test.events-v1>"},
                "right": {"type": "capture<test.other-v1>"},
                "note": {"type": note_type},
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
            **capability_files(
                capability("demo.pair", in_=request, out=result, effect="external_read")
            ),
            **{
                capture_contract_path(item.identity.name): render_capture_contract(item)
                for item in (capture_contract, other_contract)
            },
        },
        name="pair-contracts",
    )
    accept_tree(instance, build_files(build), name="pair-build")
    author_procedure(
        instance,
        procedure_spec(
            reference_text=True,
            procedure="test.paired",
            input={},
            output={name: result.fields[name].spelling for name in returned},
            slots={"pair": {"capability": "demo.pair@1"}},
            steps=[
                CallStep(
                    call="doc",
                    captures={"left": "test.events-v1", "right": "test.other-v1"},
                    slot="pair",
                    input={"query": "orders"},
                ),
                *(
                    [
                        # A statically-true guard always takes the halt branch
                        # at runtime; ``out`` stays statically reachable (R3)
                        # through the branch never taken.
                        GuardStep(
                            guard={
                                "op": "eq",
                                "left": {"literal": True},
                                "right": {"literal": True},
                            },
                            id="gate",
                            then={"goto": "stop"},
                            else_={"goto": "out"},
                        ),
                        HaltStep(halt="stop after fetch", id="stop"),
                    ]
                    if halt_after_call
                    else []
                ),
                ProjectStep(
                    project="out",
                    fields={name: f"$doc.{name}" for name in returned},
                ),
            ],
            returns="out",
            hard_caps=procedure_limits(provider_calls=1, capture_bytes=65536),
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
    if acquisition is not None:
        line["acquisition"] = acquisition
    author(instance, LineInput.model_validate(line), name="line")
    return instance


def _wire(content: bytes, observed_at: str = "2026-10-29T12:00:00Z") -> dict[str, Any]:
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
        "observed_at": observed_at,
    }


def _journal(instance: Any, run_id: str, event_kind: str) -> list[dict[str, Any]]:
    from cruxible_core.exhaust.payloads import read_payload
    from cruxible_core.service.procedures.measurements import _journal as journal_of
    from cruxible_core.storage.cas import BodyAccessContext

    journal, stream = journal_of(instance)
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    return [
        read_payload(instance.body_store(), stored.record.payload_digest, access=access)
        for stored in journal.select_records(stream, run_id=run_id, event_kind=event_kind)
    ]


def _paired_output(*, right_observed_at: str = "2026-10-29T12:00:00Z"):  # type: ignore[no-untyped-def]
    return lambda context: {
        "left": _wire(b'{"side": "left"}'),
        "right": _wire(b'{"side": "right"}', observed_at=right_observed_at),
        "note": "paired",
    }


def test_each_capture_field_is_committed_journaled_and_verified_on_its_own(
    tmp_path: Path,
) -> None:
    """F6, deviations 1-2: two fields, two CaptureContracts, two production proofs."""

    from cruxible_client.contracts.capture_reads import CaptureReadRequest
    from cruxible_client.contracts.captures import capture_contract_digest
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.core_support._light_capture_support import record_contract

    instance = _paired_world(tmp_path)
    state, _ = run_line(instance, invoker=Invoker(output=_paired_output()), name=LINE_NAME)

    assert state.status == "succeeded", state.terminal
    assert state.result["note"] == "paired"
    produced = _journal(instance, state.run_id, "produced_capture")
    by_field = {item["field"]: item for item in produced}
    assert set(by_field) == {"left", "right"}
    assert {field: item["input_name"] for field, item in by_field.items()} == {
        "left": "doc.left",
        "right": "doc.right",
    }
    # Each field is committed under its own pinned CaptureContract.
    assert by_field["left"]["capture_contract_digest"] == (
        capture_contract_digest(record_contract(name="test.events-v1", replay="exact")).tagged
    )
    assert by_field["right"]["capture_contract_digest"] == (
        capture_contract_digest(record_contract(name="test.other-v1", replay="exact")).tagged
    )
    assert state.result["left"] == by_field["left"]["capture_digest"]
    # One acquisition decision per capture output, and one observation per field.
    decisions = {
        item["input_name"]: item["decision"]["disposition"]
        for item in _journal(instance, state.run_id, "source_acquisition")
    }
    assert decisions == {"doc.left": "selected", "doc.right": "selected"}
    assert [(item.field, item.input_name) for item in state.source_observations] == [
        ("left", "doc.left"),
        ("right", "doc.right"),
    ]
    # Both Captures verify as new evidence (production proof required) after reopening.
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    for item in produced:
        read = service_read_playbill_capture(
            reopened,
            request=CaptureReadRequest(capture_digest=item["capture_digest"], max_bytes=4096),
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
        assert read.status == "verified", (item["field"], read)


@pytest.mark.parametrize(
    ("on_stale", "status"), [("omit", "succeeded"), ("refuse", "node_refused")]
)
def test_a_stale_field_is_decided_by_its_own_rule_while_its_sibling_is_selected(
    tmp_path: Path, on_stale: str, status: str
) -> None:
    """Per-field eligibility: only doc.right is bound by max_age, and only it is stale."""

    instance = _paired_world(
        tmp_path, acquisition={"doc.right": {"max_age": "PT1M", "on_stale": on_stale}}
    )
    output = _paired_output(right_observed_at="2026-10-29T11:00:00Z")

    state, _ = run_line(instance, invoker=Invoker(output=output), name=LINE_NAME)

    assert state.status == status, state.terminal
    decisions = {
        item["input_name"]: (item["decision"]["disposition"], item["decision"]["reason_codes"])
        for item in _journal(instance, state.run_id, "source_acquisition")
    }
    expected = "omitted" if on_stale == "omit" else "refused"
    assert decisions == {
        "doc.left": ("selected", []),
        "doc.right": (expected, ["cruxible.acquisition.stale"]),
    }
    if on_stale == "refuse":
        assert state.terminal.code == "cruxible.acquisition.stale"
        assert state.terminal.details["input_name"] == "doc.right"
        return
    # The omitted field is absent from the call's value; its sibling is produced,
    # and only its bytes are charged to the run's Capture budget.
    (produced,) = _journal(instance, state.run_id, "produced_capture")
    assert produced["field"] == "left"
    (final,) = _journal(instance, state.run_id, "attempt_finalized")
    assert final["capture_bytes"] == len(b'{"side": "left"}')
    assert [item.field for item in final_associations(final)] == ["left"]


def final_associations(final: dict[str, Any]) -> list[Any]:
    from cruxible_client.contracts.procedures.results import ProcedureSourceCaptureAssociation

    return [
        ProcedureSourceCaptureAssociation.model_validate(item)
        for item in final["source_capture_associations"]
    ]


def test_line_acquisition_names_capture_outputs_not_aliases(tmp_path: Path) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    with pytest.raises(AuthoringLoweringError, match="names no capture output"):
        _paired_world(tmp_path, acquisition={"doc": {"max_age": "PT1M"}})


def test_the_descriptors_name_each_capture_output_and_a_trigger_cannot_split_the_call(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path

    instance = _paired_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path("test.paired")
    definition = parse_procedure(tree[path], path=path).definition

    assert [
        (item.node_path, item.field, item.capture_kind, item.acquisition_key)
        for item in capture_output_descriptors(definition)
    ] == [
        ("doc", "left", "test.events-v1", "doc.left"),
        ("doc", "right", "test.other-v1", "doc.right"),
    ]
    for named in ("doc", "doc.right", "doc.left"):
        with pytest.raises(TriggerTargetRefused) as caught:
            trigger_capture_output(definition, named)
        assert caught.value.code == "trigger_target_ambiguous", named


@pytest.mark.parametrize("trigger_input", ["doc", "doc.right"])
def test_a_line_trigger_aimed_at_a_two_field_call_is_refused_by_any_name(
    tmp_path: Path, trigger_input: str
) -> None:
    """F7: substitution of a multi-field call is refused, explicit field target included."""

    from cruxible_core.authoring.lowering import AuthoringLoweringError

    with pytest.raises(AuthoringLoweringError, match="trigger_target_ambiguous"):
        _paired_world(tmp_path, trigger_input=trigger_input)


@pytest.mark.parametrize("reason", ["stale", "oversized"])
def test_a_call_whose_every_capture_is_omitted_still_binds_its_typed_fields(
    tmp_path: Path, reason: str
) -> None:
    """Re-review F6: omitting the captures omits only them; the operation's own
    fields (the required note) stay readable, with no Capture produced or charged."""

    rule = (
        {"max_age": "PT1M", "on_stale": "omit"} if reason == "stale" else {"on_oversized": "omit"}
    )
    instance = _paired_world(
        tmp_path,
        acquisition={"doc.left": rule, "doc.right": rule},
        returned=("note",),
    )
    old, big = "2026-10-29T11:00:00Z", b"x" * 8192  # past max_age; past 4096 max_bytes

    def output(context):  # type: ignore[no-untyped-def]
        if reason == "stale":
            return {
                "left": _wire(b'{"side": "left"}', observed_at=old),
                "right": _wire(b'{"side": "right"}', observed_at=old),
                "note": "paired",
            }
        return {"left": _wire(big), "right": _wire(big), "note": "paired"}

    state, _ = run_line(instance, invoker=Invoker(output=output), name=LINE_NAME)

    assert state.status == "succeeded", state.terminal
    assert state.result == {"note": "paired"}
    decisions = {
        item["input_name"]: item["decision"]["disposition"]
        for item in _journal(instance, state.run_id, "source_acquisition")
    }
    assert decisions == {"doc.left": "omitted", "doc.right": "omitted"}
    assert _journal(instance, state.run_id, "produced_capture") == []
    (final,) = _journal(instance, state.run_id, "attempt_finalized")
    assert final["capture_bytes"] == 0
    assert final["source_capture_associations"] == []


def _result_digests(tmp_path: Path, name: str, left: bytes) -> tuple[str, str, list[str]]:
    """Run the paired Line once in a fresh world: its replay key, result digest, Captures.

    The Procedure returns only the typed ``note``, so its output never names a Capture.
    """

    root = tmp_path / name
    root.mkdir()
    instance = _paired_world(root, returned=("note",))
    output = lambda context: {  # noqa: E731
        "left": _wire(left),
        "right": _wire(b'{"side": "right"}'),
        "note": "paired",
    }
    state, _ = run_line(instance, invoker=Invoker(output=output), name=LINE_NAME)
    assert state.status == "succeeded", state.terminal
    assert state.result == {"note": "paired"}
    (final,) = _journal(instance, state.run_id, "attempt_finalized")
    assert final["semantic_result_digest"] == state.semantic_result_digest
    return (
        state.semantic_replay_key_digest,
        state.semantic_result_digest,
        sorted(final["consumption"]["captures"]),
    )


def test_the_result_digest_commits_the_fetched_captures(tmp_path: Path) -> None:
    """D-R37 f1/f2: a run is identified by its inputs and its fetched evidence.

    The recorded result digest is the replay key and typed output plus the
    sorted fetched Captures: the same Captures recompute it exactly, in any
    order, and any other evidence under the same key and output does not.
    Without fetched evidence the preimage is the pre-(f) one, byte for byte.
    """

    from cruxible_core.procedures.execution import procedure_semantic_result_digest

    key, digest, captures = _result_digests(tmp_path, "first", b'{"side": "left"}')
    assert len(captures) == 2
    output = {"note": "paired"}

    def result(fetched: Any = ()) -> str:
        return procedure_semantic_result_digest(
            semantic_replay_key_digest=key,
            status="succeeded",
            output=output,
            refusal=None,
            fetched_captures=fetched,
        )

    assert digest == result(captures) == result(reversed(captures))
    assert result(captures[:1]) != digest
    assert result([*captures[:1], "sha256:" + "0" * 64]) != digest
    unfetched = procedure_semantic_result_digest(
        semantic_replay_key_digest=key, status="succeeded", output=output, refusal=None
    )
    assert unfetched == result(()) != digest


def _spy_on_fetched_captures(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record what ``_fetched_captures`` actually computes, without changing it.

    The production call site looks up the name in the module's globals each
    time it runs, so patching it here is observed at the real call (inside
    the finalize path), not a copy.
    """

    import cruxible_core.procedures.execution as execution_module

    observed: list[list[str]] = []
    real = execution_module._fetched_captures

    def spy(state: Any) -> list[str]:
        value = real(state)
        observed.append(value)
        return value

    monkeypatch.setattr(execution_module, "_fetched_captures", spy)
    return observed


def test_a_refused_runs_result_digest_commits_the_selected_siblings_fetched_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-R37 f1 fix: a sibling's refusal does not erase Captures already fetched.

    Both ``doc.left`` and ``doc.right`` are built and journaled as acquired
    before either field's disposition is decided; ``doc.right`` then refuses
    for staleness, before either field is bound or consumed
    (``consumed_captures`` stays empty, proven below). The recorded result
    digest still commits both fetched Captures: changing either field's
    bytes -- simulated by swapping its digest -- changes it, and so does
    dropping the selected sibling entirely, which is what the pre-fix
    ``consumed_captures``-only set did on a refusal.
    """

    from cruxible_core.procedures.execution import procedure_semantic_result_digest

    observed = _spy_on_fetched_captures(monkeypatch)
    instance = _paired_world(
        tmp_path, acquisition={"doc.right": {"max_age": "PT1M", "on_stale": "refuse"}}
    )
    output = _paired_output(right_observed_at="2026-10-29T11:00:00Z")
    state, _ = run_line(instance, invoker=Invoker(output=output), name=LINE_NAME)

    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "cruxible.acquisition.stale"
    assert _journal(instance, state.run_id, "produced_capture") == []
    (final,) = _journal(instance, state.run_id, "attempt_finalized")
    assert final["consumption"]["captures"] == []
    assert final["semantic_result_digest"] == state.semantic_result_digest

    (fetched,) = observed
    # Both Captures were built and journaled as acquired before the refusal,
    # even though neither was ever bound or consumed.
    assert len(fetched) == 2
    digest = state.semantic_result_digest

    def result(fetched_captures: Any) -> str:
        from cruxible_core.procedures.execution import ProcedureRunRefusalV1

        return procedure_semantic_result_digest(
            semantic_replay_key_digest=state.semantic_replay_key_digest,
            status="refused",
            output=None,
            refusal=ProcedureRunRefusalV1.model_validate(final["refusal"]),
            fetched_captures=fetched_captures,
        )

    assert result(sorted(fetched)) == digest
    assert result([fetched[0], "sha256:" + "0" * 64]) != digest
    assert result(["sha256:" + "0" * 64, fetched[1]]) != digest
    assert result(()) != digest


def test_a_halted_runs_result_digest_commits_an_omitted_fields_fetched_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-R37 f1 fix: a fetch-then-halt still commits what it fetched.

    ``doc.right`` is stale and omitted: built and journaled as acquired, but
    never bound or consumed (only ``doc.left`` is selected; ``_bind_omission``
    never touches ``consumed_captures``). The Procedure halts right after the
    call. The halted run's result digest still commits the omitted field's
    fetched bytes: changing them -- simulated by swapping its digest --
    changes the digest, even though ``doc.right`` was never selected or
    consumed.
    """

    from cruxible_core.procedures.execution import procedure_semantic_result_digest

    observed = _spy_on_fetched_captures(monkeypatch)
    instance = _paired_world(
        tmp_path,
        acquisition={"doc.right": {"max_age": "PT1M", "on_stale": "omit"}},
        halt_after_call=True,
    )
    output = _paired_output(right_observed_at="2026-10-29T11:00:00Z")
    state, _ = run_line(instance, invoker=Invoker(output=output), name=LINE_NAME)

    assert state.status == "halted", state.terminal
    decisions = {
        item["input_name"]: item["decision"]["disposition"]
        for item in _journal(instance, state.run_id, "source_acquisition")
    }
    assert decisions == {"doc.left": "selected", "doc.right": "omitted"}
    produced = _journal(instance, state.run_id, "produced_capture")
    assert [item["field"] for item in produced] == ["left"]
    (final,) = _journal(instance, state.run_id, "attempt_finalized")
    assert sorted(final["consumption"]["captures"]) == [produced[0]["capture_digest"]]
    assert final["semantic_result_digest"] == state.semantic_result_digest

    (fetched,) = observed
    assert len(fetched) == 2
    digest = state.semantic_result_digest

    def result(fetched_captures: Any) -> str:
        return procedure_semantic_result_digest(
            semantic_replay_key_digest=state.semantic_replay_key_digest,
            status="halted",
            output=None,
            refusal=None,
            halt=final["halt"],
            fetched_captures=fetched_captures,
        )

    assert result(sorted(fetched)) == digest
    assert result([fetched[0], "sha256:" + "0" * 64]) != digest
    assert result(["sha256:" + "0" * 64, fetched[1]]) != digest
    # The omitted field's fetched bytes are not dropped just because they
    # were never selected or consumed.
    assert result([produced[0]["capture_digest"]]) != digest


def test_an_out_contract_refusal_commits_the_captures_built_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-R37 f1 fix 2: fetched tracking starts when a Capture's builder returns.

    Both Capture fields are valid wires; only the unrelated typed ``note``
    (``"paired"`` against a declared ``int``) fails the out-Contract, which is
    checked after each Capture's builder has returned. Neither Capture is
    selected or consumed. The refused run's result digest still commits both
    fetched Captures: under the same replay key and the same refusal, either
    Capture replaced by one built from changed bytes changes it, and so
    does an empty fetched set, which is what tracking from the acquisition
    loop (after the out-Contract check) recorded.
    """

    from cruxible_core.procedures.execution import (
        ProcedureRunRefusalV1,
        procedure_semantic_result_digest,
    )

    observed = _spy_on_fetched_captures(monkeypatch)
    runs = []
    for name, left in (("first", b'{"side": "left"}'), ("changed", b'{"side": "LEFT"}')):
        root = tmp_path / name
        root.mkdir()
        instance = _paired_world(root, returned=("note",), note_type="int")
        output = lambda context, left=left: {  # noqa: E731
            "left": _wire(left),
            "right": _wire(b'{"side": "right"}'),
            "note": "paired",
        }
        state, _ = run_line(instance, invoker=Invoker(output=output), name=LINE_NAME)
        assert state.status == "node_refused", state.terminal
        assert state.terminal.code == "contract_output_refused", state.terminal
        assert _journal(instance, state.run_id, "produced_capture") == []
        (final,) = _journal(instance, state.run_id, "attempt_finalized")
        assert final["consumption"]["captures"] == []
        assert final["semantic_result_digest"] == state.semantic_result_digest
        runs.append((state, final))

    first, changed = observed
    # Both Captures were built before the out-Contract refusal.
    assert len(first) == len(changed) == 2
    (state, final), _ = runs
    # The second world built its Captures from the changed bytes.
    assert not set(first) & set(changed)

    def result(fetched_captures: Any) -> str:
        return procedure_semantic_result_digest(
            semantic_replay_key_digest=state.semantic_replay_key_digest,
            status="refused",
            output=None,
            refusal=ProcedureRunRefusalV1.model_validate(final["refusal"]),
            fetched_captures=fetched_captures,
        )

    digest = state.semantic_result_digest
    assert result(first) == digest
    # Either fetched Capture replaced by one built from other bytes changes it.
    assert result([first[0], *changed[1:]]) != digest
    assert result([*changed[:1], first[1]]) != digest
    assert result(()) != digest

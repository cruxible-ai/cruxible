"""A workspace read is typed against its capability's request, like any call (finding F2).

A call on a ``workspace.file`` or ``workspace.files`` slot writes the request the
base kit's capability declares, and the stored call input is exactly that: no
coordinate, selector or tag is defaulted into it. Core derives those from the
kind the read lands under. A request field core's builtin does not read is
refused when the Procedure is authored, naming the field, and so is a kind core
cannot derive a read's coordinate and selector from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import CallStep, ProjectStep
from cruxible_core.authoring.lowering import AuthoringLoweringError
from tests.support.capabilities import WORKSPACE_FILE, capability, capability_files, slot
from tests.support.operation_world import author_procedure, operation_build, world
from tests.support.procedures import procedure_limits, procedure_spec

#: The fixture Procedure's project step is executor-only (D-R37 h1).
pytestmark = pytest.mark.executor_only

KIND = "test.orders-v1"
REQUEST = {"logical_source": "commerce.production.orders", "relative_path": "data/doc.json"}


def _world(tmp_path: Path, *, kind: Any = None) -> Any:
    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import file_contract

    instance = world(tmp_path, operation_build())
    contract = file_contract() if kind is None else kind
    accept_tree(
        instance,
        {capture_contract_path(contract.identity.name): render_capture_contract(contract)},
        name="capture-contract",
    )
    return instance


def _spec(
    request: dict[str, Any],
    *,
    capability_spelling: str = f"{WORKSPACE_FILE}@1",
    field: str = "capture",
) -> Any:
    return procedure_spec(
        reference_text=True,
        procedure="test.read",
        input={},
        output={"document": f"capture<{KIND}>"},
        slots={"read": slot(capability_spelling)},
        steps=[
            CallStep(call="doc", captures={field: KIND}, slot="read", input=request),
            ProjectStep(project="out", fields={"document": f"$doc.{field}"}),
        ],
        returns="out",
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=65536),
    )


def _stored_input(instance: Any) -> Any:
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path

    path = procedure_path("test.read")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    (call,) = (
        node
        for node in parse_procedure(tree[path], path=path).definition.nodes
        if node.kind == "call"
    )
    return call.input


def test_the_stored_input_is_the_authored_request_and_nothing_more(tmp_path: Path) -> None:
    instance = _world(tmp_path)
    author_procedure(instance, _spec({**REQUEST, "revision": "working"}))
    assert _stored_input(instance) == {**REQUEST, "revision": "working"}


@pytest.mark.parametrize(
    ("request_", "element", "code"),
    [
        # The old handoff and default fields are not the capability's request.
        ({**REQUEST, "coordinate_type": "http-response-v1"}, "coordinate_type", "ref_unknown"),
        ({**REQUEST, "glob": "data/*.json"}, "glob", "ref_unknown"),
        ({"logical_source": "commerce.production.orders"}, "input", "ref_unknown"),
        ({**REQUEST, "logical_source": "Not A Source"}, "logical_source", "type_mismatch"),
        ({**REQUEST, "revision": "HEAD"}, "revision", "type_mismatch"),
    ],
)
def test_a_workspace_file_call_is_typed_against_its_request(
    tmp_path: Path, request_: dict[str, Any], element: str, code: str
) -> None:
    instance = _world(tmp_path)
    with pytest.raises(AuthoringLoweringError) as refused:
        author_procedure(instance, _spec(request_))
    assert code in refused.value.code
    assert refused.value.offending_element.endswith(element)


def test_a_glob_reads_through_workspace_files_into_its_list_field(tmp_path: Path) -> None:
    instance = _world(tmp_path)
    glob = {"logical_source": "commerce.production.orders", "glob": "data/*.json", "max_files": 5}
    author_procedure(
        instance,
        procedure_spec(
            reference_text=True,
            procedure="test.read",
            input={},
            output={"documents": f"list<capture<{KIND}>>{{max_items: 1000}}"},
            slots={"read": slot("workspace.files@1")},
            steps=[
                CallStep(call="doc", captures={"captures": KIND}, slot="read", input=glob),
                ProjectStep(project="out", fields={"documents": "$doc.captures"}),
            ],
            returns="out",
            hard_caps=procedure_limits(provider_calls=8, capture_bytes=65536),
        ),
    )
    assert _stored_input(instance) == glob
    # The one-path request is not a glob's: it names no glob, and a glob no path.
    with pytest.raises(AuthoringLoweringError) as refused:
        author_procedure(
            instance, _spec(REQUEST, capability_spelling="workspace.files@1", field="captures")
        )
    assert refused.value.offending_element == "$.steps[0].input.relative_path"


def _version_2(instance: Any, *, request: Any = None, result: Any = None) -> None:
    """Accept an adopter's ``workspace.file@2`` over ``request`` and/or ``result``."""

    from cruxible_client.contracts.capabilities import capability_digest
    from cruxible_client.contracts.shapes import contract_path, render_contract
    from tests.core_support._identity_support import accept_tree
    from tests.support.capabilities import head_tree, held_capability

    v1 = held_capability(head_tree(instance), WORKSPACE_FILE)
    assert v1 is not None
    v2 = capability(
        WORKSPACE_FILE,
        in_=v1.in_ if request is None else request,
        out=v1.out if result is None else result,
        effect="pure",
    ).model_copy(update={"version": 2, "predecessor_digest": capability_digest(v1).tagged})
    accept_tree(
        instance,
        {
            **{
                contract_path(item.name, item.version): render_contract(item)
                for item in (request, result)
                if item is not None
            },
            **capability_files(v2),
        },
        name="workspace-file-v2",
    )


def _encoding_request() -> Any:
    from cruxible_client.contracts.shapes import Contract

    return Contract.model_validate(
        {
            "name": "test.file-request",
            "version": 1,
            "fields": {
                "logical_source": {"type": "string"},
                "relative_path": {"type": "string"},
                "encoding": {"type": "string", "optional": True},
            },
        }
    )


def _renamed_result() -> Any:
    from cruxible_client.contracts.shapes import Contract

    return Contract.model_validate(
        {"name": "test.file-result", "version": 1, "fields": {"document": {"type": "capture<*>"}}}
    )


def test_a_request_field_core_does_not_read_is_refused_naming_it(tmp_path: Path) -> None:
    """An adopter may author workspace.file@2; core serves it by name, so its request
    may declare only fields the builtin reads."""

    instance = _world(tmp_path)
    _version_2(instance, request=_encoding_request())
    with pytest.raises(AuthoringLoweringError) as refused:
        author_procedure(instance, _spec(REQUEST, capability_spelling=f"{WORKSPACE_FILE}@2"))
    assert refused.value.code == "cruxible.authoring.workspace_request_field_unread"
    assert "'encoding'" in str(refused.value)
    assert refused.value.offending_element == "$.steps[0].input"


def test_a_result_field_core_does_not_return_is_refused_naming_it(tmp_path: Path) -> None:
    """A workspace.file@2 whose result renames ``capture`` cannot be served: the
    builtin returns exactly ``capture``, and no mapping renames it."""

    instance = _world(tmp_path)
    _version_2(instance, result=_renamed_result())
    with pytest.raises(AuthoringLoweringError) as refused:
        author_procedure(
            instance,
            _spec(REQUEST, capability_spelling=f"{WORKSPACE_FILE}@2", field="document"),
        )
    assert refused.value.code == "cruxible.authoring.workspace_result_field_unserved"
    assert "'document'" in str(refused.value) and "'capture'" in str(refused.value)
    assert refused.value.offending_element == "$.steps[0].captures"


def test_a_kind_core_cannot_derive_a_read_source_from_is_refused(tmp_path: Path) -> None:
    from tests.core_support._light_capture_support import record_contract

    # A database snapshot declares two selector schemas: a file read has no choice to make.
    instance = _world(tmp_path, kind=record_contract())
    with pytest.raises(AuthoringLoweringError) as refused:
        author_procedure(instance, _spec(REQUEST))
    assert refused.value.code == "cruxible.authoring.workspace_read_kind_unsupported"
    assert refused.value.offending_element == "$.steps[0].captures.capture"
    assert KIND in str(refused.value)


# -- the same checks at acceptance: a stored Procedure (a kit's, raw admission) -------


def _submit_raw(instance: Any, edit: Any, *, extra: dict[str, bytes] | None = None) -> Any:
    """Submit a successor of the stored ``test.read`` artifact, edited by ``edit``.

    A kit install submits its rendered Procedures through the same admission and
    Procedure law, so this is the path a kit's Procedure takes too.
    """

    from cruxible_client.contracts.procedures.artifacts import (
        parse_procedure,
        procedure_artifact_digest,
        procedure_path,
        render_procedure,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import ProcedureDefinition
    from cruxible_core.proposals.proposals import (
        AuthenticatedActor,
        ProposalAdmissionRequest,
    )

    base = instance.accepted_coordinate()
    tree = dict(instance.tree_at(base.git_oid))
    path = procedure_path("test.read")
    stored = parse_procedure(tree[path], path=path)
    raw = stored.definition.model_dump(mode="json", by_alias=True)
    edit(raw)
    definition = ProcedureDefinition.model_validate(raw)
    # A successor of the accepted test.read: the stored bytes a kit or a raw
    # change set would carry.
    artifact = stored.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "lifecycle": stored.lifecycle.model_copy(
                update={"predecessor_digest": procedure_artifact_digest(stored).tagged}
            ),
        }
    )
    tree[path] = render_procedure(artifact)
    tree.update(extra or {})
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/raw-procedure", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-10-09T11:59:00.000000Z",
    )


def _pin_version_2(instance: Any) -> Any:
    from cruxible_client.contracts.capabilities import capability_digest
    from tests.support.capabilities import head_tree, held_capability

    v2 = held_capability(head_tree(instance), WORKSPACE_FILE, 2)
    assert v2 is not None

    def edit(definition: dict[str, Any]) -> None:
        definition["slots"]["read"]["capability"] = {
            "name": WORKSPACE_FILE,
            "version": 2,
            "digest": capability_digest(v2).tagged,
        }

    return edit


def _codes(result: Any) -> set[str]:
    assert result.candidate is None, "the stored Procedure was accepted"
    return {item.code for item in result.evaluation.diagnostics}


def test_acceptance_refuses_a_stored_call_whose_request_declares_an_unread_field(
    tmp_path: Path,
) -> None:
    instance = _world(tmp_path)
    author_procedure(instance, _spec(REQUEST))
    _version_2(instance, request=_encoding_request())
    refused = _submit_raw(instance, _pin_version_2(instance))
    assert "cruxible.procedure.workspace_request_field_unread" in _codes(refused)


def test_acceptance_refuses_a_stored_call_whose_result_renames_the_capture(
    tmp_path: Path,
) -> None:
    instance = _world(tmp_path)
    author_procedure(instance, _spec(REQUEST))
    _version_2(instance, result=_renamed_result())
    pin = _pin_version_2(instance)

    def edit(definition: dict[str, Any]) -> None:
        pin(definition)
        (call,) = (node for node in definition["nodes"] if node["kind"] == "call")
        call["captures"] = {"document": call["captures"]["capture"]}
        (out,) = (node for node in definition["nodes"] if node["kind"] == "project")
        out["fields"] = {"document": "$steps.doc.document"}

    assert "cruxible.procedure.workspace_result_field_unserved" in _codes(
        _submit_raw(instance, edit)
    )


def test_acceptance_refuses_a_stored_call_landing_under_an_unsupported_kind(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from tests.core_support._light_capture_support import record_contract

    instance = _world(tmp_path)
    author_procedure(instance, _spec(REQUEST))
    snapshot = record_contract(name="test.snapshot-v1")

    def edit(definition: dict[str, Any]) -> None:
        (call,) = (node for node in definition["nodes"] if node["kind"] == "call")
        call["captures"]["capture"]["target"]["name"] = snapshot.identity.name

    refused = _submit_raw(
        instance,
        edit,
        extra={capture_contract_path(snapshot.identity.name): render_capture_contract(snapshot)},
    )
    assert "cruxible.procedure.workspace_read_kind_unsupported" in _codes(refused)


# -- the run's Capture-byte budget holds before any byte is read past it ---------------


def _budget_world(tmp_path: Path, *, glob: bool, capture_bytes: int) -> tuple[Any, Path]:
    from tests.support.operation_world import author_line

    instance = _world(tmp_path)
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True, exist_ok=True)
    if glob:
        read: dict[str, Any] = {
            "logical_source": "commerce.production.orders",
            "glob": "data/*.json",
            "max_files": 10,
        }
        output = {"documents": f"list<capture<{KIND}>>{{max_items: 1000}}"}
        step = CallStep(call="doc", captures={"captures": KIND}, slot="read", input=read)
        project = ProjectStep(project="out", fields={"documents": "$doc.captures"})
    else:
        output = {"document": f"capture<{KIND}>"}
        step = CallStep(call="doc", captures={"capture": KIND}, slot="read", input=REQUEST)
        project = ProjectStep(project="out", fields={"document": "$doc.capture"})
    author_procedure(
        instance,
        procedure_spec(
            reference_text=True,
            procedure="test.read",
            input={},
            output=output,
            slots={"read": slot("workspace.files@1" if glob else f"{WORKSPACE_FILE}@1")},
            steps=[step, project],
            returns="out",
            hard_caps=procedure_limits(provider_calls=10, capture_bytes=capture_bytes),
        ),
    )
    author_line(instance, name="test.read-line", procedure="test.read", parameters={})
    return instance, root


def _run_counted(instance: Any, root: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from cruxible_core.storage.material_reservations import ReservedCaptureStore
    from tests.support.operation_world import NOW, actor
    from tests.test_procedures.test_completeness_transfer import _Clock

    counted: dict[str, list[int]] = {"reads": [], "stores": []}
    real_read, real_store = WorkspaceFileReader.read, ReservedCaptureStore.store

    def read(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_read(self, *args, **kwargs)
        counted["reads"].append(result.receipt.byte_length)
        return result

    def store(self: Any, content: bytes, *args: Any, **kwargs: Any) -> Any:
        counted["stores"].append(len(content))
        return real_store(self, content, *args, **kwargs)

    monkeypatch.setattr(WorkspaceFileReader, "read", read)
    monkeypatch.setattr(ReservedCaptureStore, "store", store)
    state = service_run_playbill_line(
        instance,
        path_identity_digest="test.read-line",
        request=LineRunRequest(line="test.read-line"),
        actor_context=actor().model_copy(update={"timestamp": NOW}),
        caller_rung=2,
        daemon_clock=_Clock(NOW),
        provider_runtime_operator=ProviderRuntimeOperator(root.parent / "state"),
        workspace_file_reader=WorkspaceFileReader(
            instance_id=instance.descriptor.instance_id,
            operating_profile="local",
            attached_roots=(root,),
            managed_roots=(instance.root,),
        ),
    )
    return state, counted


def _budget_refusal(state: Any) -> Any:
    assert state.status != "succeeded", state
    terminal = state.terminal
    assert "max_capture_bytes" in str(terminal.model_dump(mode="json")), terminal
    return terminal


def test_a_glob_stops_reading_at_the_runs_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each file fits its kind (4096 bytes); together they exceed the run's 7000."""

    instance, root = _budget_world(tmp_path, glob=True, capture_bytes=7000)
    for index in range(6):
        (root / "data" / f"{index}.json").write_text('{"v": "' + "x" * 2990 + '"}')
    state, counted = _run_counted(instance, root, monkeypatch)
    _budget_refusal(state)
    # Two members fit (6000 of 7000); the third read stopped past the budget,
    # nothing further was read, and no body was stored for the refused run.
    assert len(counted["reads"]) == 2 and sum(counted["reads"]) <= 7000
    assert counted["stores"] == []


def test_a_glob_within_the_runs_byte_budget_reads_every_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, root = _budget_world(tmp_path, glob=True, capture_bytes=65536)
    for index in range(3):
        (root / "data" / f"{index}.json").write_text('{"v": "' + "x" * 2990 + '"}')
    state, counted = _run_counted(instance, root, monkeypatch)
    assert state.status == "succeeded", state.terminal
    assert len(counted["reads"]) == 3


def test_one_file_past_the_runs_byte_budget_is_refused_before_it_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file fits its kind (4096 bytes) but not the run's 2000."""

    instance, root = _budget_world(tmp_path, glob=False, capture_bytes=2000)
    (root / "data/doc.json").write_text('{"v": "' + "x" * 2990 + '"}')
    state, counted = _run_counted(instance, root, monkeypatch)
    _budget_refusal(state)
    assert counted["reads"] == [] and counted["stores"] == []

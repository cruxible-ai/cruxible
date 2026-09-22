"""Public source requests are symbolic; preview and prepare share backend resolution."""

from types import SimpleNamespace

from cruxible_client.authoring.inputs import CarriedContractInput
from cruxible_client.authoring.source import procedure
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.source_requests import ProcedureSourcePreviewRequestV1
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.service.procedures.source_preview import service_preview_procedure_source
from tests.test_authoring.test_authoring_procedures import _world_holding_a_document
from tests.test_procedures.test_procedure_execution import _budget, _hard_caps


def blueprint():
    Request = CarriedContractInput(
        name="assessment.request", fields={"count": PropertySchema(type="int")}
    )
    Result = CarriedContractInput(
        name="assessment.result", fields={"positive": PropertySchema(type="bool")}
    )

    @procedure(
        name="assessment",
        input=Request,
        output=Result,
        budget=_budget().model_copy(update={"max_items": None}),
        hard_caps=_hard_caps(),
    )
    def assess(request):
        if request.count > 0:
            return Result.value(positive=True)
        return Result.value(positive=False)

    return assess


def test_preview_and_prepare_accept_the_same_symbolic_source(tmp_path):
    coordinator, actor = _world_holding_a_document(tmp_path)
    source = blueprint()
    request = source._at(SimpleNamespace())
    assert "sha256:" not in request.model_dump_json()
    assert "artifact_digest" not in request.model_dump_json()
    coordinate = coordinator.instance.accepted_coordinate()
    at = AcceptedCoordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler.rule_digest,
    )
    preview = service_preview_procedure_source(
        coordinator.instance, request=ProcedureSourcePreviewRequestV1(source=request, at=at)
    )
    assert preview.ready_for_prepare, preview.errors
    assert preview.definition.source.text == source.source
    assert preview.nodes[-1].kind == "return"
    assert preview.source_map[-1].span.filename == __file__
    from cruxible_client.authoring.procedures import ProcedurePreview

    client = SimpleNamespace(preview_playbill_procedure_source=lambda *args, **kwargs: preview)
    world = SimpleNamespace(
        coordinate=at, _playbill=SimpleNamespace(_client=client, _instance_id="test")
    )
    inspected = source.preview(world=world)
    assert isinstance(inspected, ProcedurePreview)
    assert inspected.source == preview.definition.source
    assert len(inspected.return_paths) == 2
    assert all(path.kind == "pure" for path in inspected.return_paths)
    assert inspected.branch_values[0].kind == "guard"
    assert inspected.nodes[-1].kind == "return"
    assert (
        inspected.model_dump(mode="json")["return_paths"][0]["contract"]["target"]["kind"]
        == "Contract"
    )
    # Preview is read-only and authoring keeps the input symbolic, even after preview.
    assert coordinator.instance.accepted_coordinate() == coordinate
    authored = source.build(world=world)
    assert "sha256:" not in authored.model_dump_json()
    compiled = coordinator.compile(
        actor=actor,
        payload=lower_authoring_input(authored),
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    )
    assert compiled.verdict == "passed", compiled.frontier.model_dump_json()


def test_source_failure_is_localized_and_does_not_execute(tmp_path):
    coordinator, actor = _world_holding_a_document(tmp_path)
    request = (
        blueprint()
        ._at(SimpleNamespace())
        .model_copy(
            update={
                "text": "def assess(request):\n    open('should-never-run', 'w')\n",
                "first_line": 30,
            }
        )
    )
    from cruxible_client.authoring.inputs import ProcedureInput

    authored = ProcedureInput(
        kind="procedure",
        activation_policy="snapshot",
        definition={
            "name": request.name,
            "source_request": request.model_dump(mode="json", by_alias=True),
        },
    )
    compiled = coordinator.compile(
        actor=actor,
        payload=lower_authoring_input(authored),
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    )
    assert compiled.verdict == "refused"
    assert any(":31:" in diagnostic.message for diagnostic in compiled.frontier.diagnostics), (
        compiled.frontier.model_dump_json()
    )


def test_source_inspection_without_context_is_explicitly_unresolved():
    import pytest

    from cruxible_client.authoring.procedures import ProcedureCompositionError

    candidate = blueprint()
    assert candidate.filename == __file__
    assert candidate.contract_in.name == "assessment.request"
    assert not candidate.preview().ready_for_prepare
    with pytest.raises(ProcedureCompositionError, match="backend"):
        candidate.build()
    with pytest.raises(ValueError, match="Unknown"):
        candidate.bind(typo=object())

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


def test_source_relocation_preserves_identity_and_local_diagnostics():
    import pytest

    from cruxible_client.contracts.procedures.source_compiler import (
        SourceCompileError,
        compile_source,
        verify_source_graph,
    )
    from cruxible_core.authoring.procedure_source import resolve_source, verify_source_bindings
    from tests.test_procedures.test_source_compiler import accepted

    request = blueprint()._at(SimpleNamespace()).model_copy(update={"name": "example"})
    relocated = request.model_copy(
        update={"filename": "/other/machine/moved.py", "first_line": 800}
    )

    def lookup(identity):
        return None

    original = resolve_source(request, lookup=lookup, claim_types=())
    moved = resolve_source(relocated, lookup=lookup, claim_types=())
    assert original.definition == moved.definition
    assert accepted(original).artifact_digest == accepted(moved).artifact_digest
    assert original.source_map[0].span.filename == request.filename
    assert moved.source_map[0].span.filename == relocated.filename
    assert moved.source_map[0].span.line - original.source_map[0].span.line == (
        relocated.first_line - request.first_line
    )
    verify_source_graph(accepted(moved).procedure)
    verify_source_bindings(accepted(moved).procedure, lookup=lookup, claim_types=())

    # Old envelopes still reproduce their original, location-bearing digest.
    historical = compile_source(
        original.definition.source.model_copy(
            update={"filename": request.filename, "first_line": request.first_line}
        ),
        name=request.name,
        input=request.input,
        output=request.output,
        budget=request.budget,
        hard_caps=request.hard_caps,
        description=request.description,
    )
    old = accepted(historical)
    assert old.artifact_digest != accepted(original).artifact_digest
    verify_source_graph(old.procedure)
    verify_source_bindings(old.procedure, lookup=lookup, claim_types=())
    assert old.artifact_digest == accepted(historical).artifact_digest

    changed = resolve_source(
        request.model_copy(
            update={
                "text": request.text.replace("request.count > 0", "request.count > 1"),
            }
        ),
        lookup=lookup,
        claim_types=(),
    )
    assert accepted(changed).artifact_digest != accepted(original).artifact_digest
    with pytest.raises(SourceCompileError) as error:
        resolve_source(
            relocated.model_copy(
                update={
                    "text": "def assess(request):\n    open('never', 'w')\n",
                }
            ),
            lookup=lookup,
            claim_types=(),
        )
    assert error.value.diagnostic.span.filename == relocated.filename
    assert error.value.diagnostic.span.line == 801


def test_source_and_sequence_refuse_nonexistent_standalone_contracts():
    import pytest

    from cruxible_client.authoring.inputs import AcceptedReferenceInput
    from cruxible_client.authoring.procedures import Sequence

    reference = AcceptedReferenceInput(
        kind="accepted", target="Contract:missing", role="contract-in"
    )
    with pytest.raises(TypeError, match="owner-carried"):
        procedure(
            name="invalid",
            input=reference,
            output=blueprint().contract_out,
            budget=_budget(),
            hard_caps=_hard_caps(),
        )(blueprint)
    with pytest.raises(TypeError, match="owner-carried"):
        Sequence(
            name="invalid",
            contract_in=reference,
            contract_out=blueprint().contract_out,
            steps=(),
            budget=_budget(),
            hard_caps=_hard_caps(),
        ).preview()


def _field_request():
    return (
        blueprint()
        ._at(SimpleNamespace())
        .model_copy(
            update={
                "text": (
                    "def assess(request, world):\n"
                    "    item = world.project.work_item['wi-42']\n"
                    "    status = item.status.one()\n"
                    "    if status.value == 'ready':\n"
                    "        return Result.value(positive=True)\n"
                    "    return Result.value(positive=False)\n"
                )
            }
        )
    )


def _source_payload(request):
    from cruxible_client.contracts.authoring.models import ProcedureAuthoringPayloadV2

    return ProcedureAuthoringPayloadV2(
        activation_policy="snapshot",
        owned_contracts=(),
        definition={"name": request.name, "source_request": request.model_dump(mode="json")},
    )


def test_source_ontology_lookup_reads_only_matching_definitions_and_preserves_ambiguity(
    tmp_path, monkeypatch
):
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_core.indexes.typed_state import OWNER_BY_KIND
    from tests.core_support._support import initialize_local
    from tests.test_claims.test_claims import _claim_type
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, owner = initialize_local(tmp_path)
    claim_type = _claim_type()
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    definitions = [claim_type] + [
        claim_type.model_copy(
            update={
                "identity": ArtifactIdentity(kind="ClaimType", name=f"other.type{i}.field{i}"),
                "predicate": f"other.type{i}.field{i}",
                "allowed_subject_kinds": (f"other.type{i}",),
            }
        )
        for i in range(24)
    ]
    for item in definitions:
        tree[claim_type_path(item.identity.name)] = render_claim_type(item)
    _accept_tree(
        instance, owner, tree, proposal_name="ontology", timestamp="2026-08-21T12:00:00.000000Z"
    )
    # Authenticate once before counting the request's selected definition parses.
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        projection.require_source_authentication()
    codec = OWNER_BY_KIND["claim-type"]
    parse = codec.parse
    paths = []

    def counted(content, *, path, **kwargs):
        paths.append(path)
        return parse(content, path=path, **kwargs)

    from dataclasses import replace

    monkeypatch.setitem(OWNER_BY_KIND, "claim-type", replace(codec, parse=counted))
    at = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    preview = service_preview_procedure_source(
        instance, request=ProcedureSourcePreviewRequestV1(source=_field_request(), at=at)
    )
    assert preview.ready_for_prepare, preview.errors
    assert paths == [claim_type_path(claim_type.identity.name)]
    assert tuple(preview.definition.source.claim_types) == (claim_type.predicate,)

    import pytest

    from cruxible_core.authoring import lowering
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    with monkeypatch.context() as guarded:
        guarded.setattr(
            lowering,
            "_parse_reference_tree",
            lambda *a, **k: pytest.fail("source parsed whole tree"),
        )
        for request in (
            _field_request(),
            _field_request().model_copy(
                update={
                    "name": "kind-only",
                    "text": (
                        "def assess(request, world):\n"
                        "    item = world.project.work_item['wi-42']\n"
                        "    return Result.value(positive=True)\n"
                    ),
                }
            ),
        ):
            compiled = coordinator.compile(
                actor=AuthenticatedActor(actor_id="owner"),
                payload=_source_payload(request),
                canonical_timestamp="2026-08-21T12:01:00.000000Z",
            )
            assert compiled.verdict == "passed", compiled.frontier.model_dump_json()

    # A distinct predicate with the same field suffix on the same subject is ambiguous.
    collision = claim_type.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="other.work_item.status"),
            "predicate": "other.work_item.status",
        }
    )

    from cruxible_client.contracts.procedures.source_compiler import SourceCompileError
    from cruxible_core.authoring.procedure_source import resolve_indexed_source
    from cruxible_core.indexes.evaluated_state import EvaluationRows

    # The compiler must preserve ambiguity even before the vocabulary reuse law runs.
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        selected = EvaluationRows(projection).overlay(
            {claim_type_path(collision.identity.name): render_claim_type(collision)}
        )
        with pytest.raises(SourceCompileError, match="ambiguous"):
            resolve_indexed_source(_field_request(), selected)


def test_source_prepare_uses_staged_claim_type_and_query_versions(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.authoring.models import (
        ChangeSetAuthoringPayloadV1,
        ClaimTypeAuthoringPayloadV1,
        QueryDefinitionAuthoringPayloadV1,
    )
    from cruxible_client.contracts.claim_types import claim_type_digest
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path
    from cruxible_client.contracts.procedures.source_requests import SourceQuerySelection
    from cruxible_client.contracts.query.definitions import query_definition_digest
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.preflight import compute_preflight
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._support import initialize_local
    from tests.test_authoring.test_authoring_procedures import _change_set_query
    from tests.test_claims.test_claims import _claim_type
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    claim_type, query = _claim_type(), _change_set_query()
    request = _field_request()
    # Use a governed query and a field read in the same source; neither yet exists.
    request = request.model_copy(
        update={
            "text": request.text.replace("request, world", "request, world, bindings").replace(
                "    item =",
                "    rows = query(bindings.items, parameters=bindings.items.parameters())\n"
                "    item =",
            ),
            "bindings": {"items": SourceQuerySelection(name=query.identity.name)},
        }
    )
    for generation in range(2):
        if generation:
            query = query.model_copy(
                update={
                    "description": "Revised query description",
                    "lifecycle": ArtifactLifecycle(
                        predecessor_digest=query_definition_digest(query).tagged
                    ),
                }
            )
        payload = ChangeSetAuthoringPayloadV1(
            members=(
                *((ClaimTypeAuthoringPayloadV1(claim_type=claim_type),) if not generation else ()),
                _source_payload(request),
                QueryDefinitionAuthoringPayloadV1(query_definition=query),
            )
        )
        before = instance.accepted_coordinate()
        result = coordinator.compile(
            actor=actor,
            payload=payload,
            canonical_timestamp=f"2026-08-21T12:0{generation}:00.000000Z",
        )
        assert result.verdict == "passed", result.frontier.model_dump_json()
        pending = coordinator.get(result.certificate.intent_id, actor=actor).intent
        lowered = compute_preflight(instance, intent=pending, actor=actor).lowered
        assert lowered is not None
        assert instance.accepted_coordinate() == before
        path = procedure_path(request.name)
        procedure = parse_procedure(lowered.proposed_tree[path], path=path)
        source = procedure.definition.source
        assert (
            source.claim_types[claim_type.predicate].version == claim_type_digest(claim_type).tagged
        )
        assert source.bindings["items"].version == query_definition_digest(query).tagged
        _accept_tree(
            instance,
            owner,
            lowered.proposed_tree,
            proposal_name=f"staged-{generation}",
            timestamp=f"2026-08-21T12:0{generation}:00.000000Z",
        )


def test_source_same_changeset_child_procedure_remains_unresolved(tmp_path):
    from cruxible_client.contracts.authoring.models import ChangeSetAuthoringPayloadV1
    from cruxible_client.contracts.procedures.source_requests import SourceProcedureSelection
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._support import initialize_local
    from tests.test_procedures.test_nested_source_runs import blueprints

    instance, _ = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    child, parent = (item._at(SimpleNamespace()) for item in blueprints())
    parent = parent.model_copy(
        update={"bindings": {"child": SourceProcedureSelection(name="child")}}
    )
    result = coordinator.compile(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=ChangeSetAuthoringPayloadV1(
            members=(_source_payload(child), _source_payload(parent))
        ),
        canonical_timestamp="2026-08-21T12:00:00.000000Z",
    )
    assert result.verdict == "refused"
    assert any(
        "Procedure:child is not uniquely accepted" in diagnostic.message
        for diagnostic in result.frontier.diagnostics
    )

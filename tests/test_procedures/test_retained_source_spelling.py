"""Accepted Procedure source keeps compiling after the public rename.

Retained source is provenance: cold replay and source verification recompile it
under the rules it was accepted with. Source written before the rename spells
query budgets ``QueryBudgetsV1``; renaming the Python class must not change the
language that source is written in.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    parse_procedure,
    procedure_path,
)
from cruxible_client.contracts.procedures.source_compiler import verify_source_graph
from cruxible_core.ledger.checkpoints import (
    CHECKPOINT_DIRECTORY,
    checkpoint_body,
    checkpoint_path,
    write_checkpoint,
)
from cruxible_core.runtime.instance import PlaybillInstance

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pre_rename_source_procedure.json"


def test_a_procedure_accepted_before_the_rename_still_verifies() -> None:
    """The artifact bytes were accepted at the pre-rename base (``aea0de813``)."""

    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "QueryBudgetsV1(" in raw["definition"]["source"]["text"]
    verify_source_graph(ProcedureArtifact.model_validate(raw))


def _accept_source_with_historical_spelling(tmp_path: Path) -> tuple[PlaybillInstance, str]:
    from cruxible_client.contracts.authoring.models import (
        ChangeSetAuthoringPayload,
        ClaimTypeAuthoringPayload,
        QueryDefinitionAuthoringPayload,
    )
    from cruxible_client.contracts.procedures.source_requests import SourceQuerySelection
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.preflight import compute_preflight
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._support import initialize_local
    from tests.test_authoring.test_authoring_procedures import _change_set_query
    from tests.test_authoring.test_procedure_source_authoring import (
        _field_request,
        _source_payload,
    )
    from tests.test_claims.test_claims import _claim_type
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    query = _change_set_query()
    request = _field_request()
    request = request.model_copy(
        update={
            "text": request.text.replace("request, world", "request, world, bindings").replace(
                "    item =",
                "    rows = query(bindings.items, parameters=bindings.items.parameters(),"
                " budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0))\n"
                "    item =",
            ),
            "bindings": {"items": SourceQuerySelection(name=query.identity.name)},
        }
    )
    payload = ChangeSetAuthoringPayload(
        members=(
            ClaimTypeAuthoringPayload(claim_type=_claim_type()),
            _source_payload(request),
            QueryDefinitionAuthoringPayload(query_definition=query),
        )
    )
    result = coordinator.compile(
        actor=actor, payload=payload, canonical_timestamp="2026-08-21T12:00:00.000000Z"
    )
    assert result.verdict == "passed", result.frontier.model_dump_json()
    pending = coordinator.get(result.certificate.intent_id, actor=actor).intent
    lowered = compute_preflight(instance, intent=pending, actor=actor).lowered
    assert lowered is not None
    _accept_tree(
        instance,
        owner,
        lowered.proposed_tree,
        proposal_name="historical-spelling",
        timestamp="2026-08-21T12:00:00.000000Z",
    )
    return instance, request.name


def test_accepted_source_with_the_historical_spelling_replays_cold_and_from_a_checkpoint(
    tmp_path: Path,
) -> None:
    instance, name = _accept_source_with_historical_spelling(tmp_path)
    path = procedure_path(name)
    head = instance.accepted_coordinate()
    tree = instance._ledger.read_tree(head.git_oid)
    procedure = parse_procedure(tree[path], path=path)
    assert "QueryBudgetsV1(" in procedure.definition.source.text
    verify_source_graph(procedure)

    # Cold: no checkpoint, every generation replays and recompiles retained source.
    shutil.rmtree(instance.root / CHECKPOINT_DIRECTORY, ignore_errors=True)
    cold = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert cold.accepted_coordinate() == head

    # Warm: a checkpoint at head hydrates the reopen.
    history = cold.accepted_history()
    generation, parent = history[-1], history[-2]
    write_checkpoint(
        cold.root / CHECKPOINT_DIRECTORY,
        checkpoint_body(
            instance_id=cold.descriptor.instance_id,
            object_format=cold.descriptor.git_object_format,
            compiler=cold.descriptor.compiler,
            genesis=cold.descriptor.genesis,
            sequence=generation.sequence,
            git_oid=generation.oid,
            semantic_root=generation.semantic_root.tagged,
            generation_root=generation.generation_root.tagged,
            parent_generation_root=parent.generation_root.tagged,
            tree=cold._ledger.read_tree(generation.oid),
        ),
        written_at="2026-01-01T00:00:00.000000Z",
    )
    assert checkpoint_path(cold.root / CHECKPOINT_DIRECTORY).exists()
    warm = PlaybillInstance.open(cold.root, trust_root=cold.trust_root)
    assert warm.accepted_coordinate() == head

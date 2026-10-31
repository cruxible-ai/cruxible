"""A Line's nested children post to the root Line's inbox (review F-002)."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import ProjectNode
from cruxible_client.contracts.procedures.models import ProjectNode as NamedConstantNode
from cruxible_core.exhaust.line_inbox import LineInboxStore
from cruxible_core.service.procedures.procedure_runs import _accepted_procedure
from tests.core_support._support import initialize_local
from tests.support.inbox import contract_members, inbox_node
from tests.test_procedures.test_nested_source_runs import accept_blueprint, blueprints

NOTE = {"note": "child ready"}


def _accepted(instance, name: str) -> ProcedureArtifact:  # type: ignore[no-untyped-def]
    return _accepted_procedure(
        instance, name=name, coordinate=instance.accepted_coordinate()
    ).procedure


def _redigest(artifact: ProcedureArtifact) -> ProcedureArtifact:
    return artifact.model_copy(
        update={
            "definition_digest": compute_procedure_definition_digest(artifact.definition).tagged
        }
    )


def _roomy(artifact: ProcedureArtifact, *, seconds: int = 60) -> ProcedureArtifact:
    """The artifact with a wall-clock budget and hard cap that fit a slow host.

    The blueprints' 1 s budget and 2 s cap fit three nested runs on a fast
    workstation (about 0.9 s) but not on a loaded CI runner; this test is about
    where the note lands, not about time.
    """

    wall_clock = CanonicalDuration(microseconds=seconds * 1_000_000)
    definition = artifact.definition.model_copy(
        update={
            "budget": artifact.definition.budget.model_copy(update={"wall_clock": wall_clock}),
            "hard_caps": artifact.definition.hard_caps.model_copy(
                update={"max_wall_clock": wall_clock}
            ),
        }
    )
    return _redigest(artifact.model_copy(update={"definition": definition}))


def _rewritten(
    artifact: ProcedureArtifact, replacements: dict[str, str], *, name: str | None = None
) -> ProcedureArtifact:
    """The same artifact with exact strings (digests, names) replaced, digest recomputed."""

    text = json.dumps(artifact.model_dump(mode="json", by_alias=True))
    for old, new in replacements.items():
        text = text.replace(old, new)
    data = json.loads(text)
    if name is not None:
        data["identity"]["name"] = data["definition"]["name"] = name
    # Its source bindings named the child it was authored against; as a graph it
    # names the rewritten one by its exact pin.
    data["definition"].pop("source", None)
    definition = type(artifact.definition).model_validate(data["definition"])
    data["definition_digest"] = compute_procedure_definition_digest(definition).tagged
    return type(artifact).model_validate(data)


def _inbox_child(child: ProcedureArtifact) -> ProcedureArtifact:
    """The child, posting a note to the inbox once it has its result.

    A return is a leaf, so the result is the same record made by a constant
    node the Procedure returns, which then flows into the inbox terminal.
    """

    nodes: list[object] = []
    returned = None
    for node in child.definition.nodes:
        if isinstance(node, ProjectNode):
            returned = node.as_
            node = NamedConstantNode(
                node_id=node.node_id,
                fields=node.fields,
                contract_out=node.contract_out,
                **{"as": node.as_},
                next="inbox",
            )
        nodes.append(node)
    definition = child.definition.model_copy(
        update={
            "nodes": (*nodes, inbox_node(fields=NOTE)),
            "returns": returned,
            # A hand-built graph: the authored source's explicit-return rule no longer applies.
            "source": None,
        }
    )
    return _redigest(child.model_copy(update={"definition": definition}))


def test_a_grandchild_of_a_line_posts_to_the_root_lines_inbox(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
    from cruxible_client.contracts.procedure_mandates import (
        ProcedureMandate,
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import (
        LineSpec,
        line_spec_digest,
        line_spec_path,
        render_line_spec,
    )
    from tests.test_procedures.test_procedure_source_runs import _accept_more, _run_line

    instance, owner = initialize_local(tmp_path)
    child_blueprint, parent_blueprint = blueprints()
    accept_blueprint(instance, owner, child_blueprint)
    accept_blueprint(instance, owner, parent_blueprint, child="child")
    old_child, old_parent = _accepted(instance, "child"), _accepted(instance, "parent")

    # New identities, so nothing is a successor: `grand` (run by the Line) invokes
    # `relay`, which invokes the posting `inbox_child` -- two invocation levels.
    child = _roomy(_rewritten(_inbox_child(old_child), {}, name="inbox_child"))
    child_digest = procedure_artifact_digest(child).tagged
    relay = _roomy(
        _rewritten(
            old_parent,
            {
                procedure_artifact_digest(old_child).tagged: child_digest,
                '"name": "child"': '"name": "inbox_child"',
            },
            name="relay",
        )
    )
    relay_digest = procedure_artifact_digest(relay).tagged
    grand = _rewritten(
        relay,
        {child_digest: relay_digest, '"name": "inbox_child"': '"name": "relay"'},
        name="grand",
    )
    _accept_more(
        instance,
        owner,
        {
            **contract_members(),
            procedure_path("inbox_child"): render_procedure(child),
            procedure_path("relay"): render_procedure(relay),
            procedure_path("grand"): render_procedure(grand),
        },
        name="inbox-children",
    )
    accepted_grand = _accepted(instance, "grand")
    caps = accepted_grand.definition.hard_caps
    # "grand"/"relay"/"inbox_child" are a pure invoke-chain graph (no provider
    # slots), unlike the workspace.file advisory procedure _served_line/
    # _line_mandate are built for -- a plain, slot-free Line and Mandate here.
    line = LineSpec(
        identity=ArtifactIdentity(kind="Line", name="nested-inbox-line"),
        occurrence_epoch=1,
        procedure=ArtifactPin(
            role="procedure",
            target=accepted_grand.identity,
            artifact_digest=procedure_artifact_digest(accepted_grand).tagged,
        ),
        parameters={"positive": True},
        max_authority="observe",
        budgets={
            "max_wall_clock_microseconds": caps.max_wall_clock.microseconds,
            "max_provider_calls": caps.max_provider_calls,
            "max_capture_bytes": caps.max_capture_bytes,
            "max_items": caps.max_items,
        },
        epsilon={"$decimal": "0"},
    )
    mandate = ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name="nested-inbox-line-mandate"),
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        grants="propose",
        resource_ceiling=caps,
        namespace=("claims",),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    _accept_more(
        instance,
        owner,
        {
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="nested-inbox-line",
    )

    result, _ = _run_line(instance, tmp_path / "workspace", line)

    assert result.status == "succeeded", result.model_dump_json()
    store = LineInboxStore(instance)
    (entry,), _cursor = store.page(line=None, status=None, limit=10, cursor=None)
    envelope = entry.envelope
    assert envelope.line == line.identity.qualified
    assert envelope.procedure == "Procedure:inbox_child"
    assert store.get(envelope.item_id).payload == NOTE
    assert store.unread_by_line() == ((line.identity.qualified, 1),)

    # A direct (actor-origin) run of the same graph stays dark for the terminal.
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )
    from tests.test_procedures.test_procedure_source_runs import _actor

    direct = service_run_playbill_procedure(
        instance,
        name="grand",
        request=ProcedureRunRequest(input={"positive": True}),
        actor_context=_actor(instance),
    )
    assert direct.status != "succeeded"
    assert store.unread_by_line() == ((line.identity.qualified, 1),)

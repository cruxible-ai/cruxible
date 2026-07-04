"""Chokepoint enforcement for refuse_direct_writes (governance boundary).

A type marked ``write_policy: proposal_only`` refuses bare direct graph-write
verbs and forces state in through the governed proposal/workflow path. These
tests pin the adversarially-reviewed invariants:

  - direct add / batch / lifecycle direct write of a proposal_only type is
    REFUSED with ``DirectWriteRefusedError``;
  - a ``pending=True`` relationship write is ALLOWED (it stages, it is not live);
  - governed verbs (``group_resolve`` via propose->resolve, ``workflow_apply``)
    are ALWAYS allowed — no governed path is ever accidentally refused;
  - instance-default proposal_only and the ``CRUXIBLE_REFUSE_DIRECT_WRITES``
    env kill-switch both refuse, and an explicit per-type ``direct`` cannot opt
    out of the env kill-switch;
  - the constraint is HARD: ``CRUXIBLE_MODE=admin`` does NOT bypass it;
  - ``DirectWriteRefusedError`` maps to HTTP 403.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DirectWriteRefusedError
from cruxible_core.graph.assertion_state import RelationshipLifecycleState
from cruxible_core.graph.operations import (
    apply_entity,
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.types import EntityInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.server.errors import error_to_response
from cruxible_core.service import (
    service_propose_group,
    service_resolve_group,
)
from cruxible_core.service.mutations import (
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_batch_direct_write,
)
from cruxible_core.service.types import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
)

# ``fits`` (relationship) and ``Part`` (entity) are proposal_only; ``replaces``
# and ``Vehicle`` stay direct. ``fits`` carries a proposal_policy so it can be
# driven through propose -> resolve.
PROPOSAL_ONLY_CONFIG = """\
version: "1.0"
name: refuse_direct_writes_test
description: proposal_only governance fixture

entity_types:
  Vehicle:
    properties:
      vehicle_id: {type: string, primary_key: true}
  Part:
    write_policy: proposal_only
    properties:
      part_number: {type: string, primary_key: true}
      name: {type: string, optional: true}

relationships:
  - name: fits
    from: Part
    to: Vehicle
    write_policy: proposal_only
    properties:
      verified: {type: bool, default: false}
    proposal_policy:
      signals:
        check_v1:
          role: required
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_only
  - name: replaces
    from: Part
    to: Part
    properties:
      direction: {type: string, optional: true}

constraints: []
"""

# Same shape but everything direct except runtime default flips to proposal_only,
# and ``replaces`` opts back out with an explicit ``write_policy: direct``.
INSTANCE_DEFAULT_CONFIG = """\
version: "1.0"
name: refuse_direct_writes_default_test
description: instance-default proposal_only fixture

runtime:
  default_write_policy: proposal_only

entity_types:
  Vehicle:
    properties:
      vehicle_id: {type: string, primary_key: true}
  Part:
    properties:
      part_number: {type: string, primary_key: true}
      name: {type: string, optional: true}

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified: {type: bool, default: false}
  - name: replaces
    from: Part
    to: Part
    write_policy: direct
    properties:
      direction: {type: string, optional: true}

constraints: []
"""

# A synthetic ``mint_only`` entity type (``Token``). mint_only is stricter than
# proposal_only: it refuses EVERY source except ``token_mint`` — including the
# governed verbs ``workflow_apply`` / ``group_resolve``. (Synthetic only; the
# real Actor type is NOT marked mint_only until Stage 2.)
MINT_ONLY_CONFIG = """\
version: "1.0"
name: refuse_direct_writes_mint_only_test
description: mint_only governance fixture

entity_types:
  Vehicle:
    properties:
      vehicle_id: {type: string, primary_key: true}
  Part:
    properties:
      part_number: {type: string, primary_key: true}
      name: {type: string, optional: true}
  Token:
    write_policy: mint_only
    properties:
      token_id: {type: string, primary_key: true}
      label: {type: string, optional: true}

constraints: []
"""

# All types direct (no per-type or instance-default proposal_only) — only the
# env kill-switch can make this refuse.
ALL_DIRECT_CONFIG = """\
version: "1.0"
name: refuse_direct_writes_env_test
description: all-direct fixture for the env kill-switch

entity_types:
  Vehicle:
    properties:
      vehicle_id: {type: string, primary_key: true}
  Part:
    write_policy: direct
    properties:
      part_number: {type: string, primary_key: true}
      name: {type: string, optional: true}

relationships:
  - name: fits
    from: Part
    to: Vehicle
    write_policy: direct
    properties:
      verified: {type: bool, default: false}

constraints: []
"""


def _seed(instance: CruxibleInstance) -> None:
    graph = instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1",
            properties={"part_number": "BP-1", "name": "Pads"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1"},
        )
    )
    instance.save_graph(graph)


def _instance(tmp_path: Path, config_yaml: str) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(config_yaml)
    inst = CruxibleInstance.init(tmp_path, "config.yaml")
    _seed(inst)
    return inst


@pytest.fixture
def proposal_only_instance(tmp_path: Path) -> CruxibleInstance:
    return _instance(tmp_path, PROPOSAL_ONLY_CONFIG)


@pytest.fixture
def instance_default_instance(tmp_path: Path) -> CruxibleInstance:
    return _instance(tmp_path, INSTANCE_DEFAULT_CONFIG)


@pytest.fixture
def all_direct_instance(tmp_path: Path) -> CruxibleInstance:
    return _instance(tmp_path, ALL_DIRECT_CONFIG)


@pytest.fixture
def mint_only_instance(tmp_path: Path) -> CruxibleInstance:
    return _instance(tmp_path, MINT_ONLY_CONFIG)


def _fits_input(*, pending: bool = False, lifecycle=None) -> RelationshipWriteInput:
    return RelationshipWriteInput(
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        properties={"verified": True},
        pending=pending,
        lifecycle=lifecycle,
    )


# ---------------------------------------------------------------------------
# Direct writes to proposal_only types are REFUSED
# ---------------------------------------------------------------------------


def test_direct_relationship_add_refused(proposal_only_instance: CruxibleInstance) -> None:
    with pytest.raises(DirectWriteRefusedError) as exc:
        service_add_relationship_inputs(
            proposal_only_instance,
            [_fits_input()],
            source="add_relationship",
            source_ref="add_relationship",
        )
    assert exc.value.kind == "relationship"
    assert exc.value.type_name == "fits"


def test_direct_entity_add_refused(proposal_only_instance: CruxibleInstance) -> None:
    with pytest.raises(DirectWriteRefusedError) as exc:
        service_add_entity_inputs(
            proposal_only_instance,
            [
                EntityWriteInput(
                    entity_type="Part",
                    entity_id="BP-9",
                    properties={"part_number": "BP-9"},
                )
            ],
        )
    assert exc.value.kind == "entity"
    assert exc.value.type_name == "Part"


def test_batch_direct_write_relationship_refused(
    proposal_only_instance: CruxibleInstance,
) -> None:
    with pytest.raises(DirectWriteRefusedError):
        service_batch_direct_write(
            proposal_only_instance,
            BatchDirectWriteInput(
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-1",
                        properties={"verified": True},
                    )
                ]
            ),
        )


def test_batch_direct_write_entity_refused(
    proposal_only_instance: CruxibleInstance,
) -> None:
    with pytest.raises(DirectWriteRefusedError):
        service_batch_direct_write(
            proposal_only_instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Part",
                        entity_id="BP-9",
                        properties={"part_number": "BP-9"},
                    )
                ]
            ),
        )


def test_single_relationship_dry_run_also_refused(
    proposal_only_instance: CruxibleInstance,
) -> None:
    # The single-relationship add path (what cruxible_add_relationship uses) must
    # refuse a proposal_only direct write in dry-run identically to the live write.
    # Regression: the dry_run branch used to early-return AddRelationshipResult(
    # added=1) before reaching the chokepoint, so the preview disagreed with live.
    with pytest.raises(DirectWriteRefusedError) as exc:
        service_add_relationship_inputs(
            proposal_only_instance,
            [_fits_input()],
            source="add_relationship",
            source_ref="add_relationship",
            dry_run=True,
        )
    assert exc.value.kind == "relationship"
    assert exc.value.type_name == "fits"


def test_single_entity_dry_run_also_refused(
    proposal_only_instance: CruxibleInstance,
) -> None:
    # Symmetric to the relationship case: a proposal_only entity dry-run must
    # refuse identically to live. (The entity path already refused in dry-run via
    # apply_entity running before its own early return — this pins that it stays
    # symmetric with the single-relationship fix.)
    with pytest.raises(DirectWriteRefusedError) as exc:
        service_add_entity_inputs(
            proposal_only_instance,
            [
                EntityWriteInput(
                    entity_type="Part",
                    entity_id="BP-9",
                    properties={"part_number": "BP-9"},
                )
            ],
            dry_run=True,
        )
    assert exc.value.kind == "entity"
    assert exc.value.type_name == "Part"


def test_single_relationship_pending_dry_run_allowed(
    proposal_only_instance: CruxibleInstance,
) -> None:
    # A pending=True dry-run of a proposal_only relationship must NOT be
    # over-refused — pending stages for review, it is not a live direct write, so
    # the dry-run refusal tightening must leave it allowed (matches live).
    result = service_add_relationship_inputs(
        proposal_only_instance,
        [_fits_input(pending=True)],
        source="add_relationship",
        source_ref="add_relationship",
        dry_run=True,
    )
    assert result.added == 1
    # Dry-run must not persist anything.
    assert (
        proposal_only_instance.load_graph().get_relationship(
            "Part", "BP-1", "Vehicle", "V-1", "fits"
        )
        is None
    )


def test_batch_direct_write_dry_run_also_refused(
    proposal_only_instance: CruxibleInstance,
) -> None:
    # Dry-run preview must refuse identically to the live write (no guards here).
    with pytest.raises(DirectWriteRefusedError):
        service_batch_direct_write(
            proposal_only_instance,
            BatchDirectWriteInput(
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-1",
                        properties={"verified": True},
                    )
                ]
            ),
            dry_run=True,
        )


def test_lifecycle_direct_write_refused(proposal_only_instance: CruxibleInstance) -> None:
    # First stage the edge via the governed pending path so it exists, then prove
    # a direct (non-pending) lifecycle write on the proposal_only edge is refused.
    service_add_relationship_inputs(
        proposal_only_instance,
        [_fits_input(pending=True)],
        source="add_relationship",
        source_ref="add_relationship",
    )
    with pytest.raises(DirectWriteRefusedError):
        service_add_relationship_inputs(
            proposal_only_instance,
            [
                _fits_input(
                    lifecycle=RelationshipLifecycleState(status="retracted", reason="x"),  # type: ignore[arg-type]
                )
            ],
            source="add_relationship",
            source_ref="add_relationship",
        )


def test_direct_writes_to_non_governed_type_still_allowed(
    proposal_only_instance: CruxibleInstance,
) -> None:
    # ``Vehicle`` / ``replaces`` are not proposal_only — direct writes succeed.
    result = service_add_entity_inputs(
        proposal_only_instance,
        [
            EntityWriteInput(
                entity_type="Vehicle",
                entity_id="V-2",
                properties={"vehicle_id": "V-2"},
            )
        ],
    )
    assert result.added == 1


# ---------------------------------------------------------------------------
# pending=True is ALLOWED even under proposal_only (stages, not live)
# ---------------------------------------------------------------------------


def test_pending_relationship_write_allowed(
    proposal_only_instance: CruxibleInstance,
) -> None:
    result = service_add_relationship_inputs(
        proposal_only_instance,
        [_fits_input(pending=True)],
        source="add_relationship",
        source_ref="add_relationship",
    )
    assert result.added == 1
    edge = proposal_only_instance.load_graph().get_relationship(
        "Part", "BP-1", "Vehicle", "V-1", "fits"
    )
    assert edge is not None
    # Staged for review, not approved/live.
    assert edge.metadata.assertion.review.status == "pending"


# ---------------------------------------------------------------------------
# Governed verbs are ALWAYS allowed — no governed path is accidentally refused
# ---------------------------------------------------------------------------


def test_group_resolve_creates_proposal_only_edge(
    proposal_only_instance: CruxibleInstance,
) -> None:
    # propose -> resolve is the governed path; the resulting group_resolve write
    # must NOT be refused even though ``fits`` is proposal_only.
    proposed = service_propose_group(
        proposal_only_instance,
        "fits",
        [
            CandidateMember(
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                relationship_type="fits",
                signals=[CandidateSignal(signal_source="check_v1", signal="support")],
                properties={},
            )
        ],
        thesis_text="t",
        thesis_facts={"k": "v"},
    )
    resolved = service_resolve_group(
        proposal_only_instance, proposed.group_id, "approve", expected_pending_version=1
    )
    assert resolved.edges_created == 1
    edge = proposal_only_instance.load_graph().get_relationship(
        "Part", "BP-1", "Vehicle", "V-1", "fits"
    )
    assert edge is not None
    assert edge.metadata.provenance is not None
    assert edge.metadata.provenance.source == "group_resolve"


def test_workflow_apply_source_not_refused_at_chokepoint(
    proposal_only_instance: CruxibleInstance,
) -> None:
    # The ``workflow_apply`` governed source must pass the chokepoint for a
    # proposal_only relationship type (this is what the guard-preview and the
    # live canonical workflow write rely on).
    config = proposal_only_instance.load_config()
    graph = proposal_only_instance.load_graph()
    validated = validate_relationship(
        config, graph, "Part", "BP-1", "fits", "Vehicle", "V-1", {"verified": True}
    )
    apply_relationship(graph, validated, "workflow_apply", "workflow:w:s", config=config)
    assert graph.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits") is not None


def test_workflow_apply_entity_source_not_refused_at_chokepoint(
    proposal_only_instance: CruxibleInstance,
) -> None:
    # The apply.py:551 guard-preview path: a proposal_only entity type written
    # under ``workflow_apply`` must NOT be refused.
    config = proposal_only_instance.load_config()
    graph = proposal_only_instance.load_graph()
    validated = validate_entity(config, graph, "Part", "BP-9", {"part_number": "BP-9"})
    apply_entity(graph, validated, config=config, source="workflow_apply")
    assert graph.has_entity("Part", "BP-9")


def test_non_governed_source_refused_at_chokepoint(
    proposal_only_instance: CruxibleInstance,
) -> None:
    config = proposal_only_instance.load_config()
    graph = proposal_only_instance.load_graph()
    validated = validate_relationship(
        config, graph, "Part", "BP-1", "fits", "Vehicle", "V-1", {"verified": True}
    )
    with pytest.raises(DirectWriteRefusedError):
        apply_relationship(graph, validated, "add_relationship", "x", config=config)


# ---------------------------------------------------------------------------
# Instance default + env override rows
# ---------------------------------------------------------------------------


def test_instance_default_proposal_only_refuses_unset_type(
    instance_default_instance: CruxibleInstance,
) -> None:
    # ``fits`` has no per-type policy; the instance default is proposal_only.
    with pytest.raises(DirectWriteRefusedError):
        service_add_relationship_inputs(
            instance_default_instance,
            [_fits_input()],
            source="add_relationship",
            source_ref="add_relationship",
        )


def test_explicit_direct_opts_out_of_instance_default(
    instance_default_instance: CruxibleInstance,
) -> None:
    # ``replaces`` explicitly opts out with write_policy: direct.
    graph = instance_default_instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-2",
            properties={"part_number": "BP-2"},
        )
    )
    instance_default_instance.save_graph(graph)
    result = service_add_relationship_inputs(
        instance_default_instance,
        [
            RelationshipWriteInput(
                from_type="Part",
                from_id="BP-1",
                relationship_type="replaces",
                to_type="Part",
                to_id="BP-2",
                properties={"direction": "upgrade"},
            )
        ],
        source="add_relationship",
        source_ref="add_relationship",
    )
    assert result.added == 1


def test_env_kill_switch_refuses_all_direct_config(
    all_direct_instance: CruxibleInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Everything is explicitly ``direct``; only the env kill-switch can refuse.
    monkeypatch.setenv("CRUXIBLE_REFUSE_DIRECT_WRITES", "1")
    with pytest.raises(DirectWriteRefusedError):
        service_add_relationship_inputs(
            all_direct_instance,
            [_fits_input()],
            source="add_relationship",
            source_ref="add_relationship",
        )


def test_env_kill_switch_overrides_explicit_direct_entity(
    all_direct_instance: CruxibleInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRUXIBLE_REFUSE_DIRECT_WRITES", "on")
    with pytest.raises(DirectWriteRefusedError):
        service_add_entity_inputs(
            all_direct_instance,
            [
                EntityWriteInput(
                    entity_type="Part",
                    entity_id="BP-9",
                    properties={"part_number": "BP-9"},
                )
            ],
        )


def test_env_unset_all_direct_config_allows(
    all_direct_instance: CruxibleInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CRUXIBLE_REFUSE_DIRECT_WRITES", raising=False)
    result = service_add_relationship_inputs(
        all_direct_instance,
        [_fits_input()],
        source="add_relationship",
        source_ref="add_relationship",
    )
    assert result.added == 1


# ---------------------------------------------------------------------------
# HARD constraint: permission tier does not bypass; error -> 403
# ---------------------------------------------------------------------------


def test_admin_mode_does_not_bypass(
    proposal_only_instance: CruxibleInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # refuse_direct_writes is independent of the permission tier ladder.
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    with pytest.raises(DirectWriteRefusedError):
        service_add_relationship_inputs(
            proposal_only_instance,
            [_fits_input()],
            source="add_relationship",
            source_ref="add_relationship",
        )


def test_direct_write_refused_error_maps_to_403() -> None:
    status, body = error_to_response(
        DirectWriteRefusedError("relationship", "fits", "add_relationship")
    )
    assert status == 403
    assert body.error_type == "DirectWriteRefusedError"
    assert body.error_code == "direct_write_refused"
    assert body.context == {
        "kind": "relationship",
        "type_name": "fits",
        "source": "add_relationship",
    }


# ---------------------------------------------------------------------------
# mint_only: writable ONLY by token_mint — stricter than proposal_only
# (governed verbs workflow_apply / group_resolve are REFUSED, not allowed)
# ---------------------------------------------------------------------------


def _token_entity(entity_id: str = "TK-1") -> EntityWriteInput:
    return EntityWriteInput(
        entity_type="Token",
        entity_id=entity_id,
        properties={"token_id": entity_id},
    )


def test_mint_only_direct_entity_add_refused(mint_only_instance: CruxibleInstance) -> None:
    # The direct add path uses source="add_entity" — not token_mint — so refused.
    with pytest.raises(DirectWriteRefusedError) as exc:
        service_add_entity_inputs(mint_only_instance, [_token_entity()])
    assert exc.value.kind == "entity"
    assert exc.value.type_name == "Token"
    message = str(exc.value)
    assert "mint_only" in message
    assert "credential mint" in message
    assert "proposal_only" not in message


def test_mint_only_batch_entity_write_refused(mint_only_instance: CruxibleInstance) -> None:
    with pytest.raises(DirectWriteRefusedError) as exc:
        service_batch_direct_write(
            mint_only_instance,
            BatchDirectWriteInput(entities=[_token_entity()]),
        )
    assert exc.value.kind == "entity"
    assert exc.value.type_name == "Token"


def test_mint_only_mcp_write_refused(mint_only_instance: CruxibleInstance) -> None:
    # The MCP add tool funnels through the same chokepoint with a non-token_mint
    # source; pin it directly at the chokepoint.
    config = mint_only_instance.load_config()
    graph = mint_only_instance.load_graph()
    validated = validate_entity(config, graph, "Token", "TK-1", {"token_id": "TK-1"})
    with pytest.raises(DirectWriteRefusedError) as exc:
        apply_entity(graph, validated, config=config, source="mcp_add")
    assert exc.value.kind == "entity"
    assert exc.value.type_name == "Token"


def test_mint_only_token_mint_source_allowed(mint_only_instance: CruxibleInstance) -> None:
    # token_mint is the sole permitted source — the chokepoint must let it write.
    config = mint_only_instance.load_config()
    graph = mint_only_instance.load_graph()
    validated = validate_entity(config, graph, "Token", "TK-1", {"token_id": "TK-1"})
    apply_entity(graph, validated, config=config, source="token_mint")
    assert graph.has_entity("Token", "TK-1")


def test_mint_only_workflow_apply_source_refused(mint_only_instance: CruxibleInstance) -> None:
    # KEY difference from proposal_only: the governed ``workflow_apply`` verb is
    # REFUSED for a mint_only type (it would be allowed for proposal_only).
    config = mint_only_instance.load_config()
    graph = mint_only_instance.load_graph()
    validated = validate_entity(config, graph, "Token", "TK-1", {"token_id": "TK-1"})
    with pytest.raises(DirectWriteRefusedError) as exc:
        apply_entity(graph, validated, config=config, source="workflow_apply")
    assert exc.value.kind == "entity"
    assert exc.value.type_name == "Token"
    assert not graph.has_entity("Token", "TK-1")


def test_mint_only_group_resolve_source_refused(mint_only_instance: CruxibleInstance) -> None:
    # KEY difference from proposal_only: the governed ``group_resolve`` verb is
    # REFUSED for a mint_only type as well.
    config = mint_only_instance.load_config()
    graph = mint_only_instance.load_graph()
    validated = validate_entity(config, graph, "Token", "TK-1", {"token_id": "TK-1"})
    with pytest.raises(DirectWriteRefusedError) as exc:
        apply_entity(graph, validated, config=config, source="group_resolve")
    assert exc.value.kind == "entity"
    assert exc.value.type_name == "Token"
    assert not graph.has_entity("Token", "TK-1")


def test_mint_only_admin_mode_does_not_bypass(
    mint_only_instance: CruxibleInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # mint_only is independent of the permission tier ladder — admin is refused.
    monkeypatch.setenv("CRUXIBLE_MODE", "admin")
    with pytest.raises(DirectWriteRefusedError) as exc:
        service_add_entity_inputs(mint_only_instance, [_token_entity()])
    assert exc.value.kind == "entity"
    assert exc.value.type_name == "Token"

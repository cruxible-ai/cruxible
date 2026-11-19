"""A kit upgrade carries a Line onto a Procedure whose new params Contract it adds.

Carried-Line validation reads the final tree: the accepted tree with exactly
the change's own members laid over it. A params Contract version the same
release adds is therefore visible to it; one the change does not add is not,
and a refused upgrade leaves nothing behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.kits import KitAddRequest, KitBuildRequest, KitBundle
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.line_specs import parse_line_spec
from cruxible_client.contracts.shapes import contract_path
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.kits import build_kit, service_add_kit
from tests.core_support._support import initialize_local
from tests.support.operation_world import author_line, author_procedure
from tests.support.procedures import procedure_spec

# The fixture Procedure returns a constant through a closed project step: no
# provider or vocabulary is needed to exercise the carry law.
pytestmark = pytest.mark.executor_only

STAMP = "2026-11-10T12:00:00.000000Z"
PROCEDURE = "acme.count"
LINE = "user.count"
PROCEDURE_PATH = f"procedures/{PROCEDURE}.json"
LINE_PATH = f"lines/{LINE}.json"
PARAMS_V2 = contract_path(f"{PROCEDURE}.params", 2)


def _world(root: Path) -> PlaybillInstance:
    root.mkdir()
    instance, _ = initialize_local(root)
    return instance


def _tree(instance: PlaybillInstance) -> dict[str, bytes]:
    return dict(instance.immutable_tree_at(instance.accepted_coordinate().git_oid))


def _procedure(instance: PlaybillInstance, params: dict[str, Any]) -> None:
    """The fixture Procedure (a constant result): only its params Contract matters here."""

    author_procedure(instance, procedure_spec(PROCEDURE, params=params), name="procedure")


def _build(instance: PlaybillInstance, version: str) -> KitBundle:
    return build_kit(
        _tree(instance), KitBuildRequest(kit_id="acme", version=version, owns=("acme.",))
    )


def _add(instance: PlaybillInstance, bundle: KitBundle, *, dry_run: bool = False) -> Any:
    return service_add_kit(
        instance,
        KitAddRequest(bundle=bundle, dry_run=dry_run),
        actor_id="owner",
        timestamp=STAMP,
    )


def _consumer_with_line(tmp_path: Path, v2_params: dict[str, Any]) -> tuple[Any, KitBundle]:
    """A consumer on kit 1.0.0 with its own Line over the kit's Procedure, and 1.0.1.

    1.0.1's Procedure pins a params Contract version (``@2``) that 1.0.1 itself
    adds: the consumer has never accepted it.
    """

    publisher = _world(tmp_path / "publisher")
    consumer = _world(tmp_path / "consumer")
    _procedure(publisher, {"n": "int{0..100}"})
    v1 = _build(publisher, "1.0.0")
    assert _add(consumer, v1).status == "accepted"
    author_line(
        consumer,
        name=LINE,
        procedure=PROCEDURE,
        parameters={"n": 2},
        max_authority="observe",
        label="line",
    )
    _procedure(publisher, v2_params)
    v2 = _build(publisher, "1.0.1")
    assert PARAMS_V2 in v2.contents()
    assert PARAMS_V2 not in _tree(consumer)
    return consumer, v2


def test_an_upgrade_carries_a_line_onto_a_params_contract_the_release_adds(
    tmp_path: Path,
) -> None:
    consumer, v2 = _consumer_with_line(tmp_path, {"n": "int{0..100}", "m": "int{0..9}?"})

    preview = _add(consumer, v2, dry_run=True)
    assert preview.status == "would_propose", preview.detail
    added = _add(consumer, v2)
    assert added.status == "accepted", added.detail
    assert [(item.identity, item.phase) for item in added.automation_carries] == [
        (f"Line:{LINE}", "validated")
    ]
    after = _tree(consumer)
    assert PARAMS_V2 in after
    line = parse_line_spec(after[LINE_PATH], path=LINE_PATH)
    procedure = parse_procedure(after[PROCEDURE_PATH], path=PROCEDURE_PATH)
    assert line.procedure.artifact_digest == procedure_artifact_digest(procedure).tagged
    assert procedure.definition.parameter_contract is not None
    assert procedure.definition.parameter_contract.target.name == f"{PROCEDURE}.params/v2"


def test_a_line_the_added_contract_refuses_blocks_and_leaves_no_trace(tmp_path: Path) -> None:
    # 1.0.1 narrows n so the Line's n=2 no longer fits the Contract it adds.
    consumer, v2 = _consumer_with_line(tmp_path, {"n": "int{0..1}", "m": "int{0..9}?"})
    before = _tree(consumer)

    added = _add(consumer, v2)
    assert added.status == "blocked"
    assert f"Line:{LINE}" in (added.detail or "")
    assert "refuses parameters.n" in (added.detail or "")
    assert _tree(consumer) == before
    assert PARAMS_V2 not in _tree(consumer)


def test_only_the_changes_own_members_are_laid_over_the_tree(tmp_path: Path) -> None:
    """A Procedure successor without the Contract it pins is still refused."""

    from cruxible_core.providers.build_moves import settle_changes

    consumer, v2 = _consumer_with_line(tmp_path, {"n": "int{0..100}", "m": "int{0..9}?"})
    tree = consumer.immutable_tree_at(consumer.accepted_coordinate().git_oid)
    procedure = v2.contents()[PROCEDURE_PATH]
    old = parse_procedure(tree[PROCEDURE_PATH], path=PROCEDURE_PATH)
    new = parse_procedure(procedure, path=PROCEDURE_PATH)
    successor = new.model_copy(
        update={
            "lifecycle": new.lifecycle.model_copy(
                update={"predecessor_digest": procedure_artifact_digest(old).tagged}
            )
        }
    )
    from cruxible_client.contracts.procedures.artifacts import render_procedure

    with consumer.bind_accepted_projection(consumer.accepted_coordinate()) as projection:
        connection = projection.typed.connection
        without = settle_changes(
            tree,
            writes={PROCEDURE_PATH: render_procedure(successor)},
            connection=connection,
            by_name=True,
        )
        with_contract = settle_changes(
            tree,
            writes={
                PROCEDURE_PATH: render_procedure(successor),
                PARAMS_V2: v2.contents()[PARAMS_V2],
            },
            connection=connection,
            by_name=True,
        )
    assert any(
        f"Line:{LINE}" in item and f"{PROCEDURE}.params@2" in item for item in without.refused
    ), without.refused
    assert without.writes == {}
    assert not with_contract.refused, with_contract.refused
    assert LINE_PATH in with_contract.writes and PARAMS_V2 in with_contract.writes

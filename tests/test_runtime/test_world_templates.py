"""Session world templates: a copied world is the world a fresh build makes.

`tests/core_support/_world_templates.py` hands most tests a copy of a
session-built genesis or seeded world instead of building one from nothing.
These tests pin that the copy is not a lesser world: it reopens through the
ordinary replaying open path to the same head, principals and history as the
fresh build it was copied from, a genesis-rooted replay reproduces it, the
next accepted write lands on the same semantic roots, and nothing in it still
names the template. They also pin when a template must not be used.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import cruxible_core.runtime.instance as instance_module
import tests.core_support._support as support_module
from cruxible_core.compiler.compiler import P2_B5_COMPILER
from cruxible_core.ledger.checkpoints import CHECKPOINT_DIRECTORY
from cruxible_core.ledger.recovery import RecoveredInstanceState
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support import _world_templates
from tests.core_support._knowledge_loop_support import (
    TIMESTAMP,
    activate,
    authoring,
    seed_claims,
    seed_claims_into,
)
from tests.core_support._support import (
    TemplateWorld,
    initialize_fresh,
    initialize_local,
    template_world,
)
from tests.core_support._world_templates import FRESH_WORLDS_ENV, WorldTemplates, copy_template


@pytest.fixture(autouse=True)
def templates_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise templates, so a suite run with the opt-out set keeps them on."""

    monkeypatch.delenv(FRESH_WORLDS_ENV, raising=False)


def _observable(state: RecoveredInstanceState) -> dict[str, Any]:
    """Everything a reader can take off a recovery, minus the copy's own path."""

    coordinate = state.coordinate.model_dump(mode="json")
    coordinate.pop("repository_path")
    return {
        "head": state.head.oid,
        "coordinate": coordinate,
        "principals": state.head.principals.model_dump(mode="json"),
        "history": [
            {
                "sequence": generation.sequence,
                "oid": generation.oid,
                "semantic_root": generation.semantic_root.tagged,
                "generation_root": generation.generation_root.tagged,
                "descriptor": generation.descriptor.model_dump(mode="json"),
                "principals": generation.principals.model_dump(mode="json"),
                "record": (
                    None if generation.record is None else generation.record.model_dump(mode="json")
                ),
            }
            for generation in state.history
        ],
        "projection": (
            None
            if state.projection is None
            else {
                "logical_digest": state.projection.logical_digest,
                "semantic_root": state.projection.semantic_root,
                "generation_root": state.projection.generation_root,
                "row_counts": state.projection.row_counts,
            }
        ),
    }


def _reopened(root: Path, world: TemplateWorld) -> PlaybillInstance:
    return PlaybillInstance.open(root / world.managed, trust_root=world.trust_root)


def _seeded(root: Path) -> TemplateWorld:
    instance, owner = initialize_fresh(root)
    seed_claims_into(instance, owner)
    return TemplateWorld.capture(instance, owner)


def _genesis(root: Path) -> TemplateWorld:
    return TemplateWorld.capture(*initialize_fresh(root))


def _private_copy(
    tmp_path: Path, build: Any, *, copies: int = 1
) -> tuple[_world_templates.Template[TemplateWorld], list[PlaybillInstance]]:
    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")
    template = templates.template(("private",), build)
    assert template is not None
    opened = []
    for index in range(copies):
        destination = tmp_path / f"copy-{index}"
        destination.mkdir()
        copied = copy_template(template, destination)
        assert copied is not None
        opened.append(_reopened(copied, template.value))
    return template, opened


def _names_template(root: Path, template_root: Path) -> list[str]:
    needle = str(template_root).encode()
    return [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes()
    ]


@pytest.mark.parametrize("build", [_genesis, _seeded], ids=["genesis", "seeded"])
def test_a_copy_reopens_to_the_fresh_build_it_was_copied_from(tmp_path: Path, build: Any) -> None:
    template, (copy,) = _private_copy(tmp_path, build)
    fresh = _reopened(template.root, template.value)

    assert _observable(copy._recovered) == _observable(fresh._recovered)
    assert copy.trust_root == fresh.trust_root
    assert copy.inspect().head_oid == fresh.inspect().head_oid
    assert copy.root == (tmp_path / "copy-0" / template.value.managed).resolve()
    assert _names_template(tmp_path / "copy-0", template.root) == []


@pytest.mark.parametrize("build", [_genesis, _seeded], ids=["genesis", "seeded"])
def test_a_genesis_rooted_replay_of_a_copy_reproduces_it(tmp_path: Path, build: Any) -> None:
    template, (copy,) = _private_copy(tmp_path, build)
    shutil.rmtree(copy.root / CHECKPOINT_DIRECTORY, ignore_errors=True)
    replayed = _reopened(tmp_path / "copy-0", template.value)

    assert _observable(replayed._recovered) == _observable(copy._recovered)


def test_the_next_accepted_write_lands_the_same_way_on_a_copy_and_its_fresh_build(
    tmp_path: Path,
) -> None:
    """A copy accepts the next write exactly as the world it was copied from.

    A Claim's capture carries per-write material, so the new Claim's id and the
    signed change set differ between any two writes, fresh or copied; every other
    accepted path must be byte-identical.
    """

    from tests.core_support._claim_authoring_support import service_propose_playbill_claim

    template, (copy,) = _private_copy(tmp_path, _seeded)
    fresh = _reopened(template.root, template.value)
    owners = (
        template.value.owner,
        template_world_owner(tmp_path / "copy-0", template.value),
    )
    trees = []
    for instance, owner in zip((fresh, copy), owners, strict=True):
        proposed = service_propose_playbill_claim(
            instance,
            authoring=authoring("wi-44", "ready", with_claim_type=False),
            actor_id="owner",
            proposal_name="after-copy",
            timestamp=TIMESTAMP,
        )
        activate(instance, owner, proposed)
        trees.append(instance._ledger.read_tree(instance.accepted_coordinate().git_oid))

    per_write = ("claims/", "cards/claims/", "changesets/")
    stable = [
        {path: body for path, body in tree.items() if not path.startswith(per_write)}
        for tree in trees
    ]
    assert stable[0] == stable[1]
    assert len(trees[0]) == len(trees[1])
    assert len(copy.accepted_history()) == len(fresh.accepted_history())


def template_world_owner(root: Path, world: TemplateWorld) -> Any:
    from cruxible_core.governance.keys import GeneratedKeyMaterial

    return GeneratedKeyMaterial(
        principal=world.owner.principal,
        private_key_path=root / world.owner_private,
        public_key_path=root / world.owner_public,
    )


def test_a_seeded_copy_leaves_a_clean_proposal_checkpoint_for_its_own_root(
    tmp_path: Path,
) -> None:
    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")
    template = templates.template(("private",), _seeded)
    assert template is not None
    destination = tmp_path / "copy"
    destination.mkdir()
    opened = template_world(template, destination)
    assert opened is not None
    evidence = opened[0].proposal_evidence()
    marker = json.loads((evidence.root / ".proposal-source.json").read_bytes())

    assert marker["clean"] is True
    inventory = [
        [item.stat().st_dev, item.stat().st_ino]
        for item in (
            evidence.proposals,
            evidence.evaluations,
            evidence.candidates,
            evidence.withdrawals,
        )
    ]
    assert [entry[:2] for entry in marker["inventory"]] == inventory


def test_two_worlds_in_one_test_never_share_keys_or_genesis(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, first_owner = initialize_local(tmp_path / "a")
    second, second_owner = initialize_local(tmp_path / "b")

    assert first.trust_root.daemon_public_key != second.trust_root.daemon_public_key
    assert first_owner.principal.public_key != second_owner.principal.public_key
    assert first.inspect().head_oid != second.inspect().head_oid


def test_the_suites_autouse_isolation_patches_leave_templates_on(tmp_path: Path) -> None:
    """Every test carries conftest's isolation patches (binding discovery, daemon
    auto-start refusal); none of them may count as a runtime patch, or the whole
    suite silently builds every world fresh."""

    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")

    assert templates._patched() is None


def test_a_runtime_patched_before_the_build_gets_a_fresh_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instance_module, "current_compiler_coordinate", lambda: P2_B5_COMPILER)
    instance, _owner = initialize_local(tmp_path)

    assert instance.descriptor.compiler == P2_B5_COMPILER


def test_a_git_environment_change_gets_a_fresh_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")

    assert templates.template(("private",), _genesis) is None
    assert templates.fallbacks == 1
    assert not (tmp_path / "templates").exists()


def test_the_opt_out_builds_every_world_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")
    monkeypatch.setenv(FRESH_WORLDS_ENV, "1")

    assert templates.template(("private",), _genesis) is None
    assert not (tmp_path / "templates").exists()


def test_a_destination_that_already_holds_a_world_is_not_overwritten(tmp_path: Path) -> None:
    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")
    template = templates.template(("private",), _genesis)
    assert template is not None
    (tmp_path / "copy" / template.value.managed).mkdir(parents=True)

    assert copy_template(template, tmp_path / "copy") is None


def test_seed_claims_serves_its_two_accepted_claims_from_a_copy(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)

    assert len(instance.accepted_history()) == 4
    assert instance.root.is_relative_to(tmp_path.resolve())
    assert owner.private_key_path.is_relative_to(tmp_path)
    assert owner.private_key_path.is_file()


_PATCHED_TIMESTAMP = "2020-01-02T03:04:05+00:00"


def _genesis_time(instance: PlaybillInstance) -> str:
    return subprocess.run(
        [
            "git",
            "--git-dir",
            str(instance.root / "ledger.git"),
            "log",
            "-1",
            "--format=%cI",
            instance.descriptor.genesis.git_oid,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_a_constant_mock_patched_during_the_first_build_is_never_shared(tmp_path: Path) -> None:
    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")
    with mock.patch.object(support_module, "FIXED_TIMESTAMP", _PATCHED_TIMESTAMP):
        assert templates.template(("private",), _genesis) is None
    assert not (tmp_path / "templates").exists()

    clean = templates.template(("private",), _genesis)
    assert clean is not None
    assert _genesis_time(_reopened(clean.root, clean.value)) == "2026-08-10T12:00:00Z"
    with mock.patch.object(support_module, "FIXED_TIMESTAMP", _PATCHED_TIMESTAMP):
        assert templates.template(("private",), _genesis) is None


def test_a_constant_reassigned_after_a_clean_build_does_not_reuse_it(tmp_path: Path) -> None:
    """A plain reassignment is no patch anyone can see, so the inputs key the world."""

    templates = WorldTemplates()
    templates.configure(tmp_path / "templates")
    shared = support_module.TEMPLATES
    original = support_module.FIXED_TIMESTAMP
    (tmp_path / "clean").mkdir()
    (tmp_path / "patched").mkdir()
    # Direct assignment, not monkeypatch: a live MonkeyPatch would force a fresh
    # world by itself and hide what this pins.
    support_module.TEMPLATES = templates
    try:
        clean, _owner = initialize_local(tmp_path / "clean")
        templates._ordinal_test = None  # the next request is a later test's first world
        support_module.FIXED_TIMESTAMP = _PATCHED_TIMESTAMP
        try:
            patched, _owner = initialize_local(tmp_path / "patched")
        finally:
            support_module.FIXED_TIMESTAMP = original
    finally:
        support_module.TEMPLATES = shared

    assert _genesis_time(clean) == "2026-08-10T12:00:00Z"
    assert _genesis_time(patched) == "2020-01-02T03:04:05Z"

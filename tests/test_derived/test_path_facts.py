"""Carried path facts and fork receive equal the whole-tree receive oracle."""

from __future__ import annotations

import random

import pytest

from cruxible_client.contracts.proposal_models import ProposalReceiveLimits
from cruxible_core.derived.derived_state import (
    SnapshotTree,
    advance_accepted_tree,
    changed_paths,
    path_facts,
    without_cards,
)
from cruxible_core.proposals import proposals as proposals_module
from cruxible_core.proposals.proposals import validate_proposal_tree

_POOL = (
    "claims/00/CLM-{:032x}.json",
    "subjects/project.work_item/{}.json",
    "Subjects/project.work_item/{}.json",
    "subjects/project.work_item/A{}.json",
    "subjects/project.work_item/a{}.json",
    "cards/claims/00/CLM-{:032x}.json",
    "changesets/cs-{:020d}.json",
    "misc/{}.json",
    "subjects/project.work_item/deep/er/{}.json",
    "subjects/project.work_item/Café{}.json",
)


def _path(generator: random.Random) -> str:
    template = generator.choice(_POOL)
    return template.format(generator.randint(0, 12))


def _content(generator: random.Random) -> bytes:
    roll = generator.random()
    if roll < 0.05:
        return b"version https://git-lfs.github.com/spec/v1\n"
    return generator.randbytes(generator.choice((2, 5, 40)))


def _set(fork, path: str, content: bytes) -> None:
    # Forks refuse non-canonical paths outright; only a root can hold one.
    try:
        fork[path] = content
    except ValueError:
        pass


def _limits(generator: random.Random) -> ProposalReceiveLimits:
    defaults = ProposalReceiveLimits()
    return defaults.model_copy(
        update={
            "max_files": generator.choice((defaults.max_files, 30)),
            "max_changed_members": generator.choice((defaults.max_changed_members, 3)),
            "max_file_bytes": generator.choice((defaults.max_file_bytes, 30)),
            "max_total_bytes": generator.choice((defaults.max_total_bytes, 400)),
            "max_path_depth": generator.choice((defaults.max_path_depth, 4)),
        }
    )


def _outcome(call):
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - the oracle compares any refusal exactly
        return type(exc), str(exc)
    return "passed"


@pytest.mark.parametrize("seed", range(120))
def test_fork_receive_equals_the_whole_tree_receive(seed: int, monkeypatch) -> None:
    generator = random.Random(seed)
    # Accepted roots are collision-free, so the root is drawn from lowercase names.
    root = SnapshotTree(
        {
            _path(generator).replace("Subjects/", "subjects/").replace("/A", "/a"): _content(
                generator
            )
            for _ in range(generator.randint(0, 25))
        }
    )
    root._accepted = True
    if generator.random() < 0.3:
        root = advance_accepted_tree(
            root, {_path(generator).lower(): _content(generator) for _ in range(3)}
        )
    fork = root.fork()
    for _ in range(generator.randint(0, 6)):
        paths = list(fork)
        if paths and generator.random() < 0.3:
            del fork[generator.choice(paths)]
        else:
            _set(fork, _path(generator), _content(generator))
    tree = without_cards(fork.snapshot()) if generator.random() < 0.5 else fork.snapshot()
    limits = _limits(generator)

    fast_answers: list[bool] = []
    real = proposals_module._receives_as_fork

    def observed(*args, **kwargs):
        answer = real(*args, **kwargs)
        fast_answers.append(answer)
        return answer

    # The oracle is the whole-tree pass over the same snapshot rows.
    monkeypatch.setattr(proposals_module, "_receives_as_fork", lambda *a, **k: False)
    expected = _outcome(lambda: validate_proposal_tree(tree, limits=limits, base_tree=root))
    monkeypatch.setattr(proposals_module, "_receives_as_fork", observed)
    actual = _outcome(lambda: validate_proposal_tree(tree, limits=limits, base_tree=root))
    assert actual == expected
    # The fork path answers every passing tree itself.
    assert fast_answers[-1] == (expected == "passed")


@pytest.mark.parametrize("seed", range(30))
def test_carried_facts_equal_facts_built_from_scratch(seed: int) -> None:
    generator = random.Random(seed)
    root = SnapshotTree({_path(generator): _content(generator) for _ in range(20)})
    path_facts(root).oversize_count(30, root._rows)
    for _ in range(4):
        fork = root.fork()
        for _ in range(5):
            paths = list(fork)
            if paths and generator.random() < 0.4:
                del fork[generator.choice(paths)]
            else:
                _set(fork, _path(generator), _content(generator))
        root = advance_accepted_tree(
            root, {path: fork._rows.get(path) for path in changed_paths(root, fork)}
        )
        carried = root._path_facts
        assert carried is not None
        fresh = path_facts(SnapshotTree(dict(root._rows.items())))
        fresh.oversize_count(30, root._rows)
        assert carried.content_bytes == fresh.content_bytes
        assert carried.noncanonical == fresh.noncanonical
        assert carried.colliding == fresh.colliding
        assert carried.max_depth == fresh.max_depth
        assert {k: v for k, v in carried.depths.items() if v} == fresh.depths
        assert dict(carried.folded.items()) == dict(fresh.folded.items())
        assert carried.oversize == fresh.oversize

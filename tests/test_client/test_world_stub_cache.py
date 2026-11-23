"""The per-instance world stub cache behind `cruxible python --check`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.authoring.world_stub_cache import (
    WORLD_STUB_MODULE,
    cached_world_stub,
    world_stub_cache_directory,
)
from cruxible_client.contracts.orient import OrientPredicate, OrientSubjectKind


def _stub(tag: str) -> str:
    digest = "sha256:" + tag * 64
    return f"# playbill-world-stub-v2: test\n# x\n# Vocabulary digest: {digest}\nclass World: ...\n"


class _Client:
    """Answers head and the two vocabulary orient sections; counts what was read."""

    def __init__(self) -> None:
        self.git_oid = "a" * 40
        self.severity_members: tuple[str, ...] = ("high", "low")
        self.live_claims = 3
        self.heads = 0
        self.orients: list[str] = []

    def head(self, instance_id: str) -> Any:
        self.heads += 1
        return SimpleNamespace(coordinate=SimpleNamespace(git_oid=self.git_oid))

    def orient(self, instance_id: str, *, section: str, **_: Any) -> Any:
        self.orients.append(section)
        rows: dict[str, Any] = {
            "claim_types": [
                OrientPredicate(
                    name="severity",
                    predicate="sec.vuln.severity",
                    cardinality="one",
                    type="enum",
                    members=self.severity_members,
                    revision=1,
                    live_claims=self.live_claims,
                )
            ],
            "subject_kinds": [OrientSubjectKind(kind="sec.vulnerability", rule="minted")],
        }
        page = {"claim_types": None, "subject_kinds": None, section: rows[section]}
        return SimpleNamespace(**page, truncated=False, next_cursor=None)


class _Render:
    def __init__(self) -> None:
        self.calls = 0
        self.tag = "1"

    def __call__(self, client: Any, instance_id: str) -> str:
        self.calls += 1
        return _stub(self.tag)


@pytest.fixture
def context_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "user" / "client-context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(path))
    return path


def test_the_stub_lives_beside_the_cli_context_keyed_by_instance(context_path: Path) -> None:
    client, render = _Client(), _Render()
    cached = cached_world_stub(client, "inst_a", transport="unix:///s", render=render)  # type: ignore[arg-type]

    assert cached.path == context_path.parent.resolve() / "stubs" / "inst_a" / "world.pyi"
    assert cached.directory == world_stub_cache_directory("inst_a")
    assert WORLD_STUB_MODULE == "world"
    assert cached.status == "rendered"
    assert cached.path.read_text(encoding="utf-8") == _stub("1")
    assert cached.vocabulary_digest == "sha256:" + "1" * 64
    # An id that is not a safe path component never names a path of its own.
    assert world_stub_cache_directory("../x").name.startswith("sha256-")


def test_an_unchanged_head_reuses_the_stub_from_any_cwd_with_one_head_read(
    context_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, render = _Client(), _Render()
    first_repo, second_worktree = tmp_path / "repo", tmp_path / "worktree"
    first_repo.mkdir()
    second_worktree.mkdir()

    monkeypatch.chdir(first_repo)
    first = cached_world_stub(client, "inst_a", transport="unix:///s", render=render)  # type: ignore[arg-type]
    orients_after_first = len(client.orients)
    monkeypatch.chdir(second_worktree)
    second = cached_world_stub(client, "inst_a", transport="unix:///s", render=render)  # type: ignore[arg-type]

    assert second.status == "reused"
    assert second.path == first.path
    assert render.calls == 1
    assert client.heads == 2
    assert len(client.orients) == orients_after_first


def test_a_moved_head_rereads_the_listing_and_renders_only_when_the_vocabulary_moved(
    context_path: Path,
) -> None:
    client, render = _Client(), _Render()
    cached_world_stub(client, "inst_a", transport="unix:///s", render=render)  # type: ignore[arg-type]

    # Claims were written: the head moved and the per-Claim counters moved.
    client.git_oid = "b" * 40
    client.live_claims = 40
    moved = cached_world_stub(client, "inst_a", transport="unix:///s", render=render)  # type: ignore[arg-type]
    assert moved.status == "revalidated"
    assert render.calls == 1
    # The new head was recorded: the next use is a plain reuse.
    again = cached_world_stub(client, "inst_a", transport="unix:///s", render=render)  # type: ignore[arg-type]
    assert again.status == "reused"

    # An enum member was added: the vocabulary moved, so the stub renders again.
    client.git_oid = "c" * 40
    client.severity_members = ("critical", "high", "low")
    render.tag = "2"
    changed = cached_world_stub(client, "inst_a", transport="unix:///s", render=render)  # type: ignore[arg-type]
    assert changed.status == "rendered"
    assert render.calls == 2
    assert changed.path.read_text(encoding="utf-8") == _stub("2")


def test_another_endpoint_or_a_damaged_stub_renders_again(context_path: Path) -> None:
    client, render = _Client(), _Render()
    cached = cached_world_stub(client, "inst_a", transport="unix:///s", render=render)  # type: ignore[arg-type]

    # Instance ids are not unique across daemons.
    other = cached_world_stub(client, "inst_a", transport="http://remote", render=render)  # type: ignore[arg-type]
    assert other.status == "rendered"
    assert render.calls == 2

    cached.path.write_text("edited by hand\n", encoding="utf-8")
    repaired = cached_world_stub(client, "inst_a", transport="http://remote", render=render)  # type: ignore[arg-type]
    assert repaired.status == "rendered"
    assert repaired.path.read_text(encoding="utf-8") == _stub("1")

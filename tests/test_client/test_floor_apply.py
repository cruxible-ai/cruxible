"""The one shared floor apply: verify everything first, write atomically, manifest last."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cruxible_client import contracts
from cruxible_client.authoring import floor_apply
from cruxible_client.authoring.floor_apply import (
    PlaybillFloorApplyError,
    apply_floor_delta,
    read_floor_manifest,
)
from cruxible_client.contracts.floor import PlaybillFloorDeltaV1
from tests.support.floor_exports import floor_v5_delta

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="4" * 64,
    semantic_root="sha256:" + "1" * 64,
    generation_root="sha256:" + "2" * 64,
    compiler_digest="sha256:" + "3" * 64,
)
BASE = {
    "README.md": (b"readme\n", 0),
    "current/k/a.yaml": (b"# k/a  changed gen 2\nstatus: ready\n", 2),
    "current/k/b.yaml": (b"# k/b  changed gen 3\nstatus: done\n", 3),
    "current/k/b.status.txt": (b"long\n", 3),
}
HEAD = {
    "README.md": (b"readme\n", 0),
    "current/k/a.yaml": (b"# k/a  changed gen 2\nstatus: ready\n", 2),
    "current/k/b.yaml": (b"# k/b  changed gen 5\nstatus: blocked\n", 5),
    "current/k/c.yaml": (b"# k/c  changed gen 4\ntitle: C\n", 4),
}


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _base_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "floor"
    full = floor_v5_delta(BASE, coordinate=COORDINATE, generation=3)
    assert apply_floor_delta(directory, full).status == "applied"
    return directory


def _expected(tmp_path: Path) -> dict[str, bytes]:
    directory = tmp_path / "expected"
    apply_floor_delta(directory, floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5))
    return _tree(directory)


def _delta() -> PlaybillFloorDeltaV1:
    return floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5, base=(3, BASE))


def test_a_delta_brings_the_base_to_the_head_and_twice_is_a_no_op(tmp_path: Path) -> None:
    directory = _base_dir(tmp_path)
    delta = _delta()
    assert {item.path for item in delta.files} == {"current/k/b.yaml", "current/k/c.yaml"}
    assert delta.tombstones == ("current/k/b.status.txt",)
    applied = apply_floor_delta(directory, delta)
    assert (applied.status, applied.written, applied.removed) == ("applied", 2, 1)
    assert _tree(directory) == _expected(tmp_path)
    manifest = read_floor_manifest(directory)
    assert manifest is not None and manifest.generation == 5
    again = apply_floor_delta(directory, delta)
    assert (again.status, again.written, again.removed) == ("unchanged", 0, 0)
    assert _tree(directory) == _expected(tmp_path)


def test_a_wrong_base_refuses_before_writing_anything(tmp_path: Path) -> None:
    directory = _base_dir(tmp_path)
    before = _tree(directory)
    other = floor_v5_delta(
        HEAD,
        coordinate=COORDINATE,
        generation=5,
        base=(3, {**BASE, "current/k/a.yaml": (b"other\n", 2)}),
    )
    assert apply_floor_delta(directory, other).status == "base_mismatch"
    assert _tree(directory) == before
    older = floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5, base=(4, BASE))
    assert apply_floor_delta(directory, older).status == "base_mismatch"
    foreign = floor_v5_delta(
        HEAD, coordinate=COORDINATE, generation=5, base=(3, BASE), renderer="sha256:" + "9" * 64
    )
    assert apply_floor_delta(directory, foreign).status == "base_mismatch"
    assert _tree(directory) == before
    # An empty directory holds no base either.
    assert apply_floor_delta(tmp_path / "empty", _delta()).status == "base_mismatch"


def test_a_delta_that_does_not_reach_its_head_refuses_before_writing(tmp_path: Path) -> None:
    directory = _base_dir(tmp_path)
    before = _tree(directory)
    honest = _delta()
    lying = floor_v5_delta(
        {**HEAD, "current/k/c.yaml": (b"# k/c  changed gen 4\ntitle: D\n", 4)},
        coordinate=COORDINATE,
        generation=5,
        base=(3, BASE),
    )
    forged = lying.model_copy(update={"head_manifest_digest": honest.head_manifest_digest})
    from cruxible_client.contracts.floor import floor_delta_digest

    payload = forged.model_dump(mode="json")
    payload["delta_digest"] = floor_delta_digest(payload)
    with pytest.raises(PlaybillFloorApplyError, match="head manifest digest"):
        apply_floor_delta(directory, PlaybillFloorDeltaV1.model_validate(payload))
    assert _tree(directory) == before


def test_paths_outside_the_floor_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes its root"):
        floor_v5_delta({"../escape": (b"x\n", 1)}, coordinate=COORDINATE, generation=1)
    with pytest.raises(ValueError):
        floor_v5_delta({"manifest.json": (b"{}\n", 1)}, coordinate=COORDINATE, generation=1)
    directory = _base_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "current/k").rename(tmp_path / "moved")
    os.symlink(outside, directory / "current/k")
    with pytest.raises(PlaybillFloorApplyError, match="escapes the floor"):
        apply_floor_delta(directory, _delta())
    assert not any(outside.iterdir())
    assert isinstance(_delta(), PlaybillFloorDeltaV1)


@pytest.mark.parametrize("crash_after", range(1, 5))
def test_a_crash_after_any_single_write_resumes_to_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_after: int
) -> None:
    directory = _base_dir(tmp_path)
    delta = _delta()
    write = floor_apply._write
    unlink = Path.unlink
    done: list[str] = []

    class Crash(Exception):
        pass

    def step(name: str) -> None:
        done.append(name)
        if len(done) == crash_after:
            raise Crash(name)

    def crashing_write(target: Path, content: bytes) -> None:
        write(target, content)
        step(target.name)

    def crashing_unlink(self: Path, missing_ok: bool = False) -> None:
        unlink(self, missing_ok=missing_ok)
        # A staged temporary file's cleanup is not a step of the apply.
        if not self.name.startswith(".floor-"):
            step(self.name)

    monkeypatch.setattr(floor_apply, "_write", crashing_write)
    monkeypatch.setattr(Path, "unlink", crashing_unlink)
    with pytest.raises(Crash):
        apply_floor_delta(directory, delta)
    monkeypatch.undo()
    # The manifest is the commit point: until it is written the base still holds.
    resumed = apply_floor_delta(directory, delta)
    assert resumed.status in {"applied", "unchanged"}
    assert _tree(directory) == _expected(tmp_path)
    assert apply_floor_delta(directory, delta).status == "unchanged"


def test_a_full_floor_replaces_whatever_the_directory_held(tmp_path: Path) -> None:
    directory = _base_dir(tmp_path)
    (directory / "stray.txt").write_text("not the floor\n", encoding="utf-8")
    (directory / "projections").mkdir()
    (directory / "projections/INDEX").write_text("# client-written\n", encoding="utf-8")
    full = floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5)
    applied = apply_floor_delta(directory, full)
    assert applied.status == "applied"
    tree = _tree(directory)
    # The client's own projections/INDEX is not the daemon's to remove.
    assert tree.pop("projections/INDEX") == b"# client-written\n"
    assert tree == _expected(tmp_path)

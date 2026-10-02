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
    # A link inside the floor is not the floor: a delta refuses it, and a full
    # floor removes the link itself, never what it points at.
    assert apply_floor_delta(directory, _delta()).status == "base_mismatch"
    assert not any(outside.iterdir())
    full = floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5)
    assert apply_floor_delta(directory, full).status == "applied"
    assert not any(outside.iterdir())
    assert not (directory / "current/k").is_symlink()
    assert _tree(directory) == _expected(tmp_path)


@pytest.mark.parametrize("crash_after", range(1, 5))
def test_a_crash_after_any_single_write_resumes_to_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_after: int
) -> None:
    directory = _base_dir(tmp_path)
    delta = _delta()
    write = floor_apply._write_file
    unlink = floor_apply._unlink_file
    done: list[str] = []

    class Crash(Exception):
        pass

    def step(name: str) -> None:
        done.append(name)
        if len(done) == crash_after:
            raise Crash(name)

    def crashing_write(root: int, path: str, content: bytes) -> None:
        write(root, path, content)
        step(path)

    def crashing_unlink(root: int, path: str) -> bool:
        removed = unlink(root, path)
        step(path)
        return removed

    monkeypatch.setattr(floor_apply, "_write_file", crashing_write)
    monkeypatch.setattr(floor_apply, "_unlink_file", crashing_unlink)
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
    (directory / ".gitignore").write_bytes(b"*\n")
    full = floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5)
    applied = apply_floor_delta(directory, full)
    assert applied.status == "applied"
    tree = _tree(directory)
    # The client's own projections/INDEX is not the daemon's to remove.
    assert tree.pop("projections/INDEX") == b"# client-written\n"
    assert tree.pop(".gitignore") == b"*\n"
    assert tree == _expected(tmp_path)


# -- review r1: the apply proves the installed floor, through no link ---------------


def _resealed(delta: PlaybillFloorDeltaV1, **changes: object) -> dict[str, object]:
    from cruxible_client.contracts.floor import floor_delta_digest

    payload = {**delta.model_dump(mode="json"), **changes}
    payload["delta_digest"] = floor_delta_digest(payload)
    return payload


def test_a_forged_replay_at_the_installed_head_refuses_before_writing(tmp_path: Path) -> None:
    import base64
    import hashlib

    directory = _base_dir(tmp_path)
    honest = _delta()
    assert apply_floor_delta(directory, honest).status == "applied"
    installed = _tree(directory)
    forged_bytes = b"# k/c  changed gen 4\ntitle: FORGED\n"
    files = [
        {
            **item.model_dump(mode="json"),
            "content_b64": base64.b64encode(forged_bytes).decode("ascii"),
            "sha256": "sha256:" + hashlib.sha256(forged_bytes).hexdigest(),
        }
        if item.path == "current/k/c.yaml"
        else item.model_dump(mode="json")
        for item in honest.files
    ]
    forged = PlaybillFloorDeltaV1.model_validate(_resealed(honest, files=files))
    assert forged.head_manifest_digest == honest.head_manifest_digest
    with pytest.raises(PlaybillFloorApplyError, match="head manifest digest"):
        apply_floor_delta(directory, forged)
    assert _tree(directory) == installed
    # A head naming another coordinate under the honest digest refuses too.
    other = {**honest.head.model_dump(mode="json"), "semantic_root": "sha256:" + "8" * 64}
    moved = PlaybillFloorDeltaV1.model_validate(_resealed(honest, head=other))
    with pytest.raises(PlaybillFloorApplyError, match="head manifest digest"):
        apply_floor_delta(directory, moved)
    # A replayed tombstone of a file the head still holds refuses as well.
    removing = PlaybillFloorDeltaV1.model_validate(
        _resealed(honest, tombstones=["current/k/a.yaml", "current/k/b.status.txt"])
    )
    with pytest.raises(PlaybillFloorApplyError, match="head manifest digest"):
        apply_floor_delta(directory, removing)
    assert _tree(directory) == installed


@pytest.mark.parametrize("step", ["write", "tombstone"])
def test_a_parent_swapped_for_a_symlink_mid_apply_is_never_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    directory = _base_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.status.txt").write_bytes(b"outside, not the floor's\n")
    name = "_write_file" if step == "write" else "_unlink_file"
    original = getattr(floor_apply, name)
    swapped: list[bool] = []

    def barrier(root: int, path: str, *args: object) -> object:
        # Between the proof and the first mutation, current/k becomes a link out.
        if not swapped and path.startswith("current/k/"):
            (directory / "current/k").rename(tmp_path / "moved")
            os.symlink(outside, directory / "current/k")
            swapped.append(True)
        return original(root, path, *args)

    monkeypatch.setattr(floor_apply, name, barrier)
    with pytest.raises(PlaybillFloorApplyError, match="escapes the floor"):
        apply_floor_delta(directory, _delta())
    assert swapped
    assert sorted(path.name for path in outside.iterdir()) == ["b.status.txt"]
    assert (outside / "b.status.txt").read_bytes() == b"outside, not the floor's\n"


@pytest.mark.parametrize(
    "paths",
    [
        ("current/k/A.yaml", "current/k/a.yaml"),
        ("current/k/é.yaml", "current/k/é.yaml"),
        ("current/k", "current/k/a.yaml"),
        ("MANIFEST.JSON",),
        (".gitignore",),
        (".GITIGNORE",),
        (".gitIgnore",),
        ("Projections/index",),
        ("projections",),
    ],
)
def test_paths_one_filesystem_cannot_hold_apart_are_refused(paths: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        floor_v5_delta({path: (b"x\n", 1) for path in paths}, coordinate=COORDINATE, generation=1)


@pytest.mark.parametrize(
    "tombstone",
    [
        "manifest.json",
        "Manifest.Json",
        "projections/INDEX",
        ".gitignore",
        ".GITIGNORE",
        ".gitIgnore",
    ],
)
def test_a_reserved_tombstone_is_refused_before_any_mutation(
    tmp_path: Path, tombstone: str
) -> None:
    directory = _base_dir(tmp_path)
    before = _tree(directory)
    honest = _delta()
    payload = _resealed(honest, tombstones=sorted({*honest.tombstones, tombstone}))
    with pytest.raises(ValueError, match="reserved"):
        PlaybillFloorDeltaV1.model_validate(payload)
    assert _tree(directory) == before
    # A crash between writes never loses the manifest, so the honest delta resumes.
    assert apply_floor_delta(directory, honest).status == "applied"
    assert _tree(directory) == _expected(tmp_path)


@pytest.mark.parametrize("damage", ["edit", "remove", "stray"])
def test_a_delta_refuses_an_installed_floor_that_differs_from_its_manifest(
    tmp_path: Path, damage: str
) -> None:
    directory = _base_dir(tmp_path)
    untouched = directory / "current/k/a.yaml"
    if damage == "edit":
        untouched.write_bytes(b"hand edit\n")
    elif damage == "remove":
        untouched.unlink()
    else:
        (directory / "current/k/stray.yaml").write_bytes(b"not the floor's\n")
    before = _tree(directory)
    result = apply_floor_delta(directory, _delta())
    assert result.status == "base_mismatch"
    assert _tree(directory) == before
    # The full floor the caller then asks for repairs it.
    full = floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5)
    assert apply_floor_delta(directory, full).status == "applied"
    assert _tree(directory) == _expected(tmp_path)


def test_a_full_floor_at_its_head_still_repairs_stray_and_edited_files(tmp_path: Path) -> None:
    directory = tmp_path / "floor"
    full = floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5)
    assert apply_floor_delta(directory, full).status == "applied"
    (directory / "stray.txt").write_text("stray\n", encoding="utf-8")
    (directory / "current/k/a.yaml").write_bytes(b"hand edit\n")
    (directory / "projections").mkdir()
    (directory / "projections/INDEX").write_text("# client-written\n", encoding="utf-8")
    (directory / ".gitignore").write_bytes(b"*\n")
    repaired = apply_floor_delta(directory, full)
    assert (repaired.status, repaired.written, repaired.removed) == ("applied", 1, 1)
    tree = _tree(directory)
    assert tree.pop("projections/INDEX") == b"# client-written\n"
    assert tree.pop(".gitignore") == b"*\n"
    assert tree == _expected(tmp_path)
    assert apply_floor_delta(directory, full).status == "unchanged"


def test_an_empty_delta_at_the_head_refuses_a_damaged_floor(tmp_path: Path) -> None:
    directory = _base_dir(tmp_path)
    assert apply_floor_delta(directory, _delta()).status == "applied"
    (directory / "current/k/a.yaml").write_bytes(b"hand edit\n")
    at_head = floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5, base=(5, HEAD))
    assert at_head.files == () and at_head.tombstones == ()
    assert apply_floor_delta(directory, at_head).status == "base_mismatch"


# -- review r2 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore/child",
        ".GITIGNORE/child",
        "manifest.json/child",
        "Manifest.JSON/child",
        "projections/INDEX/child",
        "projections/index/x",
    ],
)
def test_nothing_may_live_under_a_reserved_file(tmp_path: Path, path: str) -> None:
    from cruxible_client.contracts.floor import build_floor_manifest

    # In a delta's files ...
    with pytest.raises(ValueError, match="reserved"):
        floor_v5_delta({path: (b"x\n", 1)}, coordinate=COORDINATE, generation=1)
    # ... in its tombstones ...
    directory = _base_dir(tmp_path)
    before = _tree(directory)
    honest = _delta()
    with pytest.raises(ValueError, match="reserved"):
        PlaybillFloorDeltaV1.model_validate(
            _resealed(honest, tombstones=sorted({*honest.tombstones, path}))
        )
    # ... and in a manifest inventory, before anything is written.
    with pytest.raises(ValueError, match="reserved"):
        build_floor_manifest(
            renderer="sha256:" + "5" * 64,
            coordinate=COORDINATE,
            generation=1,
            notes_digest="sha256:" + "6" * 64,
            files={path: ("sha256:" + "0" * 64, 1, 1)},
        )
    assert _tree(directory) == before


def test_a_tombstoned_link_is_removed_without_following_it(tmp_path: Path) -> None:
    directory = _base_dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    (directory / "current/k/b.status.txt").unlink()
    os.symlink(outside, directory / "current/k/b.status.txt")
    applied = apply_floor_delta(directory, _delta())
    assert (applied.status, applied.removed) == ("applied", 1)
    assert not (directory / "current/k/b.status.txt").is_symlink()
    assert outside.read_bytes() == b"outside\n"
    assert _tree(directory) == _expected(tmp_path)
    assert apply_floor_delta(directory, _delta()).status == "unchanged"


@pytest.mark.parametrize("nested", [False, True])
def test_a_directory_where_a_floor_file_goes_is_repaired_by_a_full_floor(
    tmp_path: Path, nested: bool
) -> None:
    directory = _base_dir(tmp_path)
    (directory / "projections").mkdir()
    (directory / "projections/INDEX").write_text("# client-written\n", encoding="utf-8")
    (directory / ".gitignore").write_bytes(b"*\n")
    managed = directory / "current/k/a.yaml"
    managed.unlink()
    managed.mkdir()
    if nested:
        (managed / "deeper").mkdir()
        (managed / "deeper/stray.yaml").write_bytes(b"stray\n")
    before = _tree(directory)
    # A delta refuses it before writing; the full floor it asks for repairs it.
    assert apply_floor_delta(directory, _delta()).status == "base_mismatch"
    assert _tree(directory) == before
    full = floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5)
    assert apply_floor_delta(directory, full).status == "applied"
    tree = _tree(directory)
    assert tree.pop("projections/INDEX") == b"# client-written\n"
    assert tree.pop(".gitignore") == b"*\n"
    assert tree == _expected(tmp_path)
    assert apply_floor_delta(directory, full).status == "unchanged"


def test_a_delta_refuses_a_directory_where_it_writes_or_removes(tmp_path: Path) -> None:
    directory = _base_dir(tmp_path)
    tombstoned = directory / "current/k/b.status.txt"
    tombstoned.unlink()
    tombstoned.mkdir()
    before = _tree(directory)
    assert apply_floor_delta(directory, _delta()).status == "base_mismatch"
    assert _tree(directory) == before and tombstoned.is_dir()


# -- review r3: installed spellings are matched exactly, never by alias ---------------


def _full() -> PlaybillFloorDeltaV1:
    return floor_v5_delta(HEAD, coordinate=COORDINATE, generation=5)


def _spellings(directory: Path) -> set[str]:
    return {path.relative_to(directory).as_posix() for path in directory.rglob("*")}


def _assert_repaired(tmp_path: Path, directory: Path) -> None:
    applied = apply_floor_delta(directory, _full())
    assert applied.status == "applied"
    assert _tree(directory) == _expected(tmp_path)
    expected = tmp_path / "expected"
    assert _spellings(directory) == _spellings(expected)
    assert apply_floor_delta(directory, _full()).status == "unchanged"


@pytest.mark.parametrize(
    ("managed", "alias"),
    [
        # A case alias of a file the delta writes, as an empty directory.
        ("current/k/b.yaml", "current/k/B.YAML"),
        # ... of a file the delta removes.
        ("current/k/b.status.txt", "current/k/B.STATUS.TXT"),
    ],
)
def test_a_directory_aliasing_a_floor_file_refuses_a_delta_and_is_repaired(
    tmp_path: Path, managed: str, alias: str
) -> None:
    directory = _base_dir(tmp_path)
    (directory / managed).unlink()
    (directory / alias).mkdir()
    before = _tree(directory), _spellings(directory)
    assert apply_floor_delta(directory, _delta()).status == "base_mismatch"
    assert (_tree(directory), _spellings(directory)) == before
    _assert_repaired(tmp_path, directory)


@pytest.mark.parametrize(
    ("managed", "alias"),
    [
        ("current/k/a.yaml", "current/k/A.YAML"),
        ("current/k", "current/K"),
        ("manifest.json", "MANIFEST.JSON"),
    ],
)
def test_a_floor_entry_under_another_spelling_is_never_taken_for_it(
    tmp_path: Path, managed: str, alias: str
) -> None:
    directory = _base_dir(tmp_path)
    (directory / managed).rename(directory / alias)
    before = _tree(directory), _spellings(directory)
    assert apply_floor_delta(directory, _delta()).status == "base_mismatch"
    assert (_tree(directory), _spellings(directory)) == before
    _assert_repaired(tmp_path, directory)


def test_delta_and_replay_preserve_the_local_gitignore(tmp_path):
    directory = _base_dir(tmp_path)
    ignore = directory / ".gitignore"
    ignore.write_bytes(b"*\n")
    inode = ignore.stat().st_ino
    assert apply_floor_delta(directory, _delta()).status == "applied"
    assert ignore.read_bytes() == b"*\n" and ignore.stat().st_ino == inode
    assert apply_floor_delta(directory, _delta()).status == "unchanged"
    assert ignore.read_bytes() == b"*\n" and ignore.stat().st_ino == inode


@pytest.mark.parametrize("normalization", ["NFC", "NFD"])
def test_gitignore_aliases_are_reserved_in_manifests(normalization):
    import unicodedata

    from cruxible_client.contracts.floor import build_floor_manifest, check_floor_paths

    for alias in (".gitignore", ".GITIGNORE", ".gitIgnore"):
        path = unicodedata.normalize(normalization, alias)
        with pytest.raises(ValueError, match="reserved"):
            check_floor_paths([path], label="test paths")
        with pytest.raises(ValueError, match="floor manifest may not list"):
            build_floor_manifest(
                renderer="sha256:" + "5" * 64,
                coordinate=COORDINATE,
                generation=1,
                notes_digest="sha256:" + "6" * 64,
                files={path: ("sha256:" + "0" * 64, 1, 1)},
            )


def test_fifo_manifest_read_returns_promptly_without_a_valid_manifest(tmp_path):
    from tests.support.fifos import call_with_fifo_timeout

    directory = _base_dir(tmp_path)
    fifo = directory / "manifest.json"
    fifo.unlink()
    os.mkfifo(fifo)
    assert call_with_fifo_timeout(fifo, lambda: read_floor_manifest(directory)) is None


@pytest.mark.parametrize("relative", ["manifest.json", "current/k/a.yaml"])
def test_floor_verification_refuses_a_fifo_swapped_after_stat(tmp_path, monkeypatch, relative):
    from tests.support.fifos import call_with_fifo_timeout

    directory = _base_dir(tmp_path)
    fifo = directory / relative
    parent_identity = fifo.parent.stat().st_ino
    open_file = os.open
    swapped = False

    def open_at(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            not swapped
            and path == fifo.name
            and dir_fd is not None
            and os.fstat(dir_fd).st_ino == parent_identity
        ):
            fifo.unlink()
            os.mkfifo(fifo)
            swapped = True
        return open_file(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(floor_apply.os, "open", open_at)
    with pytest.raises(PlaybillFloorApplyError, match="not a regular file"):
        call_with_fifo_timeout(fifo, lambda: apply_floor_delta(directory, _delta()))
    assert swapped

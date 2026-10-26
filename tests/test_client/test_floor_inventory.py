"""The carried floor manifest: the reference bytes and digests, at the cost of the change."""

from __future__ import annotations

import random
import unicodedata
from collections.abc import Mapping
from typing import Any

import pytest

from cruxible_client import contracts
from cruxible_client.contracts import floor as floor_contracts
from cruxible_client.contracts.floor import (
    FloorEntry,
    build_floor_manifest,
    check_floor_paths,
    content_digest,
    floor_manifest_digest,
    render_floor_manifest,
)
from cruxible_client.contracts.floor_inventory import (
    FloorInventory,
    floor_manifest_state,
    known_floor_manifest,
)
from tests.support.floor_exports import TEST_NOTES, TEST_RENDERER

COORDINATE = contracts.AcceptedCoordinate(
    git_oid="4" * 64,
    semantic_root="sha256:" + "1" * 64,
    generation_root="sha256:" + "2" * 64,
    compiler_digest="sha256:" + "3" * 64,
)
Row = tuple[str, int, int]
# Directory names that sort around each other, a non-ASCII one, and one whose
# case- and NFC-folded spelling another may take.
_DIRECTORIES = ("current/k", "current/kind", "current/kä", "changes", "ontology/kinds", "")


def _reference(files: Mapping[str, Row], generation: int) -> floor_contracts.FloorManifest:
    return build_floor_manifest(
        renderer=TEST_RENDERER,
        coordinate=COORDINATE,
        generation=generation,
        notes_digest=TEST_NOTES,
        files=dict(files),
    )


def _state(inventory: FloorInventory, generation: int) -> Any:
    return floor_manifest_state(
        inventory,
        renderer=TEST_RENDERER,
        coordinate=COORDINATE,
        generation=generation,
        notes_digest=TEST_NOTES,
    )


def _assert_same(inventory: FloorInventory, files: Mapping[str, Row], generation: int) -> None:
    """The carried manifest equals the reference build in model, bytes and both digests."""

    reference = _reference(files, generation)
    state = _state(inventory, generation)
    assert inventory.entries() == reference.files
    assert list(inventory) == [item.path for item in reference.files]
    assert len(inventory) == len(reference.files)
    assert inventory.floor_digest == reference.floor_digest
    assert state.manifest == reference
    assert state.content == render_floor_manifest(reference)
    assert state.digest == floor_manifest_digest(reference)
    for path, (digest, size, changed) in files.items():
        assert inventory.get(path) == FloorEntry(
            path=path, content_digest=digest, byte_length=size, changed_at=changed
        )


def _row(rng: random.Random, generation: int) -> Row:
    return (
        content_digest(rng.randbytes(8)),
        rng.randrange(0, 5000),
        rng.randrange(0, generation + 1),
    )


def _path(rng: random.Random) -> str:
    directory = rng.choice(_DIRECTORIES)
    name = f"{rng.choice(('a', 'b', 'é', 'z', 'Z'))}{rng.randrange(0, 4000):04d}.yaml"
    return f"{directory}/{name}" if directory else name


def test_an_empty_floor_and_a_floor_emptied_by_its_removals_match_the_reference() -> None:
    empty = FloorInventory.build({})
    _assert_same(empty, {}, 0)
    files = {
        "README.md": (content_digest(b"r"), 1, 0),
        "current/k/a.yaml": (content_digest(b"a"), 1, 1),
    }
    built = empty.patch(files)
    _assert_same(built, files, 1)
    _assert_same(built.patch({}, list(files)), {}, 2)
    # Removing what the floor does not hold changes nothing.
    assert built.patch({}, ["current/k/zz.yaml"]) is built


@pytest.mark.parametrize("seed", range(6))
def test_any_sequence_of_patches_matches_the_reference_build(seed: int) -> None:
    """Splits, merges, emptied blocks and inserts before the first path all keep the format."""

    rng = random.Random(seed)
    generation = 3
    files: dict[str, Row] = {}
    size = rng.choice((5, 300, 900))
    while len(files) < size:
        files[_path(rng)] = _row(rng, generation)
    check_floor_paths(files, label="seed")
    inventory = FloorInventory.build(files)
    _assert_same(inventory, files, generation)
    for _step in range(12):
        generation += 1
        held = sorted(files)
        removed = rng.sample(held, k=min(len(held), rng.choice((0, 1, 10, 200))))
        written: dict[str, Row] = {}
        for path in rng.sample(held, k=min(len(held), rng.choice((0, 3, 40)))):
            written[path] = _row(rng, generation)
        added = len(written) + rng.choice((0, 2, 30, 260))
        while len(written) < added:
            written[_path(rng)] = _row(rng, generation)
        candidate = {path: row for path, row in files.items() if path not in removed}
        candidate.update(written)
        try:
            check_floor_paths(candidate, label="candidate")
        except ValueError:
            with pytest.raises(ValueError):
                inventory.patch(written, removed)
            continue
        inventory = inventory.patch(written, removed)
        files = candidate
        _assert_same(inventory, files, generation)


@pytest.mark.parametrize(
    ("held", "written", "removed"),
    [
        # A case alias, an NFC alias and a casefold alias of a held file.
        (("current/k/a.yaml",), ("current/k/A.yaml",), ()),
        (("current/k/é.yaml",), ("current/k/" + unicodedata.normalize("NFD", "é") + ".yaml",), ()),
        (("current/k/straße",), ("current/k/STRASSE",), ()),
        # A file where a directory is, and a directory where a file is.
        (("current/k/a.yaml",), ("current/k",), ()),
        (("current/k",), ("current/K/a.yaml",), ()),
        # The client's own files, their directories and anything under the manifest.
        ((), ("sources",), ()),
        ((), ("Projections",), ()),
        ((), ("manifest.json/x",), ()),
        ((), ("MANIFEST.JSON",), ()),
        ((), ("../escape",), ()),
        ((), ("current/k/.floor-0123456789abcdef.tmp",), ()),
        # Two new paths that clash with each other.
        ((), ("current/k/b", "current/k/B"), ()),
        ((), ("current/k/b", "current/k/b/c"), ()),
    ],
)
def test_a_patch_refuses_exactly_what_the_whole_floor_check_refuses(
    held: tuple[str, ...], written: tuple[str, ...], removed: tuple[str, ...]
) -> None:
    row = (content_digest(b"x"), 1, 1)
    inventory = FloorInventory.build(dict.fromkeys(held, row))
    with pytest.raises(ValueError):
        check_floor_paths(
            [*(path for path in held if path not in removed), *written], label="reference"
        )
    with pytest.raises(ValueError):
        inventory.patch(dict.fromkeys(written, row), removed)


def test_a_clash_the_same_patch_removes_is_no_clash() -> None:
    row = (content_digest(b"x"), 1, 1)
    inventory = FloorInventory.build({"current/k/a.yaml": row, "current/k/d/e": row})
    renamed = inventory.patch(
        {"current/k/A.yaml": row, "current/k/d": row}, ["current/k/a.yaml", "current/k/d/e"]
    )
    _assert_same(renamed, {"current/k/A.yaml": row, "current/k/d": row}, 1)


def test_a_file_changed_after_the_generation_is_refused() -> None:
    inventory = FloorInventory.build({"a": (content_digest(b"x"), 1, 5)})
    with pytest.raises(ValueError, match="after the floor's generation"):
        _state(inventory, 4)


def test_only_a_manifest_that_reads_back_as_itself_is_remembered() -> None:
    row = (content_digest(b"x"), 1, 1)
    nfc = _state(FloorInventory.build({"current/k/é.yaml": row}), 1)
    assert known_floor_manifest(nfc.content) is None
    nfc.digest
    assert known_floor_manifest(nfc.content) is nfc
    # A non-NFC path renders NFC: those bytes parse to another floor.
    nfd = _state(FloorInventory.build({unicodedata.normalize("NFD", "current/k/é.yaml"): row}), 1)
    nfd.digest
    assert nfd.content == nfc.content
    assert known_floor_manifest(nfd.content) is nfc
    assert known_floor_manifest(b"{}") is None

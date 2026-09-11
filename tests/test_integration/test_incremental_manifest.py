"""Wire-neutrality laws for carried-digest manifest, root, and semantic-diff builds.

Every value the incremental replay path produces must be byte-identical to the
value the from-scratch path produces. These are the equivalence proofs for that
claim, plus the refusal proofs that the carry never widens the accepted path set.
"""

from __future__ import annotations

import random
import unicodedata

import pytest

from cruxible_client.contracts import canonical
from cruxible_client.contracts.canonical import (
    Manifest,
    file_digest,
    manifest_for_tree,
    manifest_for_tree_carrying,
    manifest_root,
    manifest_root_from_members,
    semantic_diff,
    semantic_diff_from_members,
    semantic_projection,
)
from cruxible_client.contracts.errors import CanonicalEncodingError

# One constant drives every randomized law below. Replay determinism is the
# property under test, so the generator must never read a clock.
PROPERTY_SEED = 20260818
GENERATIONS = 40

NFC_PATH = unicodedata.normalize("NFC", "documents/café.json")
NFD_PATH = unicodedata.normalize("NFD", "documents/café.json")


def _cold(tree: dict[str, bytes]) -> Manifest:
    return manifest_for_tree(semantic_projection(tree))


def _member(rng: random.Random) -> bytes:
    return b"tag: member\nnonce: %d\n" % rng.getrandbits(48)


def _ordinary_paths() -> tuple[str, ...]:
    return (
        *(f"documents/d{index:03d}.json" for index in range(12)),
        *(f"documents/nested/deep/d{index:03d}.json" for index in range(6)),
        *(f"subjects/s{index:03d}.json" for index in range(6)),
    )


def _changeset_path(sequence: int) -> str:
    return f"changesets/cs-{sequence:020d}.json"


def test_carried_manifest_reproduces_the_from_scratch_manifest_over_a_seeded_walk() -> None:
    rng = random.Random(PROPERTY_SEED)
    candidates = _ordinary_paths()
    tree: dict[str, bytes] = {path: _member(rng) for path in candidates[:8]}
    tree[_changeset_path(0)] = b"{}\n"
    manifest = _cold(tree)
    unicode_spelling: str | None = None
    saw_add = saw_update = saw_remove = saw_unicode_flip = False

    for sequence in range(1, GENERATIONS + 1):
        successor = dict(tree)
        for _ in range(rng.randint(1, 5)):
            choice = rng.random()
            if choice < 0.35:
                path = rng.choice(candidates)
                if path not in successor:
                    saw_add = True
                else:
                    saw_update = True
                successor[path] = _member(rng)
            elif choice < 0.55 and len(successor) > 4:
                path = rng.choice(sorted(successor))
                if path.startswith("changesets/"):
                    continue
                del successor[path]
                if path in {NFC_PATH, NFD_PATH}:
                    unicode_spelling = None
                saw_remove = True
            elif choice < 0.8:
                # One logical member with two Unicode spellings: the slot is
                # emptied before it is refilled, so the tree never holds both.
                spelling = rng.choice((NFC_PATH, NFD_PATH))
                if unicode_spelling is not None:
                    if unicode_spelling != spelling:
                        saw_unicode_flip = True
                    successor.pop(unicode_spelling, None)
                successor[spelling] = _member(rng)
                unicode_spelling = spelling
            else:
                # Change-set records live outside the semantic projection.
                successor[_changeset_path(sequence)] = b'{"sequence": %d}\n' % sequence

        successor[_changeset_path(sequence)] = b'{"sequence": %d}\n' % sequence

        members = manifest_for_tree_carrying(
            semantic_projection(successor),
            previous_tree=tree,
            previous_manifest=manifest,
        )
        assert members == _cold(successor)
        assert list(members) == list(_cold(successor))
        assert (
            manifest_root_from_members(members).tagged
            == manifest_root(semantic_projection(successor)).tagged
        )
        expected_digest, expected_scope = semantic_diff(tree, successor)
        digest, scope = semantic_diff_from_members(manifest, members)
        assert digest.tagged == expected_digest.tagged
        assert scope == expected_scope
        assert all(not path.startswith("changesets/") for path in scope)

        tree = successor
        manifest = members

    assert saw_add and saw_update and saw_remove and saw_unicode_flip
    assert any(path.startswith("changesets/") for path in tree)
    assert unicode_spelling is None or unicode_spelling in tree


def test_the_seeded_walk_is_reproducible_across_independent_runs() -> None:
    def walk() -> str:
        rng = random.Random(PROPERTY_SEED)
        candidates = _ordinary_paths()
        tree = {path: _member(rng) for path in candidates[:8]}
        manifest = _cold(tree)
        for _ in range(8):
            successor = dict(tree)
            successor[rng.choice(candidates)] = _member(rng)
            manifest = manifest_for_tree_carrying(
                semantic_projection(successor),
                previous_tree=tree,
                previous_manifest=manifest,
            )
            tree = successor
        return manifest_root_from_members(manifest).tagged

    assert walk() == walk()


def test_carrying_hashes_only_the_members_whose_bytes_changed(monkeypatch) -> None:
    tree = {
        f"documents/d{index:04d}.json": b"revision: 1\nindex: %d\n" % index for index in range(200)
    }
    manifest = _cold(tree)
    successor = dict(tree)
    for index in range(3):
        successor[f"documents/d{index:04d}.json"] = b"revision: 2\nindex: %d\n" % index
    successor["documents/d9999.json"] = b"revision: 1\nindex: 9999\n"

    hashed: list[bytes] = []

    def counting_file_digest(content: bytes):
        hashed.append(content)
        return file_digest(content)

    monkeypatch.setattr(canonical, "file_digest", counting_file_digest)
    members = manifest_for_tree_carrying(
        semantic_projection(successor),
        previous_tree=tree,
        previous_manifest=manifest,
    )
    monkeypatch.undo()

    assert len(hashed) == 4
    assert members == _cold(successor)


def test_changed_bytes_are_rehashed_even_when_a_stale_digest_is_carried() -> None:
    tree = {"documents/d.json": b"revision: 1\n"}
    stale: Manifest = {"documents/d.json": file_digest(b"revision: 1\n").value}
    successor = {"documents/d.json": b"revision: 2\n"}
    members = manifest_for_tree_carrying(
        semantic_projection(successor),
        previous_tree=tree,
        previous_manifest=stale,
    )
    assert members == {"documents/d.json": file_digest(b"revision: 2\n").value}


def test_a_late_nfc_path_collision_is_refused_exactly_as_a_cold_build_refuses_it() -> None:
    tree = {NFC_PATH: b"revision: 1\n"}
    manifest = _cold(tree)
    successor = {**tree, NFD_PATH: b"revision: 1\n"}

    with pytest.raises(CanonicalEncodingError, match="collide after NFC normalization"):
        manifest_for_tree(semantic_projection(successor))
    with pytest.raises(CanonicalEncodingError, match="collide after NFC normalization"):
        manifest_for_tree_carrying(
            semantic_projection(successor),
            previous_tree=tree,
            previous_manifest=manifest,
        )


def test_a_late_case_fold_sibling_collision_is_refused_by_the_carried_build() -> None:
    tree = {"documents/Alpha/x.json": b"revision: 1\n"}
    manifest = _cold(tree)
    successor = {**tree, "documents/alpha/y.json": b"revision: 1\n"}

    with pytest.raises(CanonicalEncodingError, match="case-fold-colliding siblings"):
        manifest_for_tree(semantic_projection(successor))
    with pytest.raises(CanonicalEncodingError, match="case-fold-colliding siblings"):
        manifest_for_tree_carrying(
            semantic_projection(successor),
            previous_tree=tree,
            previous_manifest=manifest,
        )


def test_a_late_non_canonical_path_is_refused_by_the_carried_build() -> None:
    tree = {"documents/d.json": b"revision: 1\n"}
    manifest = _cold(tree)
    successor = {**tree, "documents/../escape.json": b"revision: 1\n"}

    with pytest.raises(CanonicalEncodingError):
        manifest_for_tree_carrying(
            successor,
            previous_tree=tree,
            previous_manifest=manifest,
        )


def test_an_empty_carry_reproduces_the_cold_manifest_exactly() -> None:
    tree = {f"documents/d{index:03d}.json": b"index: %d\n" % index for index in range(32)}
    assert manifest_for_tree_carrying(tree, previous_tree={}, previous_manifest={}) == (
        manifest_for_tree(tree)
    )

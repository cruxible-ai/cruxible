"""Warm flat directories encode changed children, independent of sibling count."""

from __future__ import annotations

import os
import random
from collections import Counter
from unittest.mock import patch

import pytest

from cruxible_client.contracts import merkle
from cruxible_client.contracts.errors import CanonicalEncodingError
from cruxible_core.ledger import git


@pytest.mark.parametrize(
    "domains", [merkle.MANIFEST_MERKLE_DOMAINS, merkle.DEPENDENCY_EDGE_DOMAINS]
)
def test_merkle_splice_matches_cold_build_over_edits_and_pruning(domains):
    rng = random.Random(23)
    members = {f"subjects/k/s{i}.json": f"{i:064x}" for i in range(20)}
    tree = merkle.build_merkle_tree(members, domains=domains)
    for i in range(35):
        path = f"subjects/k/s{rng.randrange(30)}.json"
        removed = [path] if path in members and i % 3 == 0 else []
        updated = {} if removed else {path: f"{i + 100:064x}"}
        tree = merkle.update_merkle_tree(tree, updated=updated, removed=removed)
        members.update(updated)
        for path in removed:
            del members[path]
        cold = merkle.build_merkle_tree(members, domains=domains)
        assert tree.root == cold.root
        assert dict(tree.nodes) == dict(cold.nodes)
    with pytest.raises(CanonicalEncodingError, match="case-fold"):
        merkle.update_merkle_tree(tree, updated={"SUBJECTS/other/x": "a" * 64})
    tree = merkle.update_merkle_tree(tree, removed=list(members))
    assert tree.root == merkle.build_merkle_tree({}, domains=domains).root
    assert set(tree.nodes) == {""}


def test_one_subject_write_has_flat_python_work_at_ten_times_siblings(tmp_path):
    counts = []
    for n in (80, 800):
        members = {f"subjects/k/s{i:04}.json": f"{i:064x}" for i in range(n)}
        tree = merkle.build_merkle_manifest(members)
        ledger = git.GitLedger.initialize(
            tmp_path / f"ledger-{n}.git",
            object_format="sha256",
            signing_key_path=tmp_path / "unused",
            allowed_signers_path=tmp_path / "unused",
        )
        base = ledger._write_tree({path: path.encode() for path in members})
        work = Counter()
        encode = merkle.canonical_bytes
        entry = git._encode_tree_entry
        row = merkle._row_node

        def encoded(value):
            work["merkle_entries"] += 1
            return encode(value)

        def git_entry(*args):
            work["git_entries"] += 1
            return entry(*args)

        def row_node(value):
            work["child_gathers"] += 1
            return row(value)

        with (
            patch.object(merkle, "canonical_bytes", encoded),
            patch.object(merkle, "_row_node", row_node),
            patch.object(git, "_encode_tree_entry", git_entry),
        ):
            merkle.update_merkle_manifest(tree, updated={"subjects/k/s0000.json": "f" * 64})
            ledger._commit_changes_to_tree(
                base, {"subjects/k/s0000.json": ledger._blob_oid(b"new")}, {}
            )
        counts.append(work)
    assert counts[0] == counts[1]
    assert counts[1]["merkle_entries"] == counts[1]["git_entries"] == 3


def test_pack_file_work_is_constant_per_batch(tmp_path):
    ledger = git.GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format="sha256",
        signing_key_path=tmp_path / "unused",
        allowed_signers_path=tmp_path / "unused",
    )
    calls = []
    sync = os.fsync
    for n in (1, 100):
        with patch("os.fsync", side_effect=lambda fd: (calls.append(fd), sync(fd))[-1]):
            ledger._store_objects(
                {ledger._blob_oid(str(i).encode()): ("blob", str(i).encode()) for i in range(n)}
            )
        assert len(calls) == 3
        calls.clear()
    assert not list((ledger.path / "objects").glob("[0-9a-f][0-9a-f]/*"))

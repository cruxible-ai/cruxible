"""Compare isolated batch hot paths; not an end-to-end SDK benchmark.

Run from the repository root with PYTHONPATH=.:src:packages/cruxible-client/src.
Baseline methods are loaded from the pre-batch commit; fixture setup is untimed.
"""

from __future__ import annotations

import ast
import json
import statistics
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from tests.test_playbill.test_git_mirror_snapshots import MAIN, commit, refs
from tests.test_playbill.test_prepared_evaluation import retained, world

from cruxible_core.playbill import git as git_module
from cruxible_core.playbill import prepared_evaluation as prepared_module
from cruxible_core.playbill.checkpoints import checkpoint_body, members_for_tree
from cruxible_core.playbill.derived_state import DerivedState, SnapshotTree
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.prepared_evaluation import PreparedEvaluationAdapter

BASE = "8eb5e9b15f0a2f439eb9a29d413cbd6e90d94e07"


def baseline(module, cls, method):
    path = Path(module.__file__).relative_to(Path.cwd())
    source = subprocess.check_output(["git", "show", f"{BASE}:{path}"]).decode()
    owner = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == cls)
    node = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == method)
    namespace = vars(module).copy()
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[method]


def measure(fn, repetitions=7, setup=lambda: None):
    samples = []
    result = None
    for _ in range(repetitions):
        setup()
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {"median_ms": round(statistics.median(samples), 4)}, result


def run(root):
    (root / "world").mkdir()
    fixture = world.__wrapped__(root / "world")
    instance = fixture[0]
    coordinate = instance.accepted_coordinate()
    adapter = PreparedEvaluationAdapter(DerivedState())
    old_retain = baseline(prepared_module, "PreparedEvaluationScope", "retain")
    old_push = baseline(git_module, "GitLedger", "push_mirror")
    output = {}
    with adapter.scope() as scope:
        args = retained(fixture, adapter, scope)
        fields = {k: v for k, v in args.items() if k not in {"owner", "tree", "bodies"}}
        for size in (1000, 10000):
            rows = instance.tree_at(coordinate.git_oid)
            rows.update({f"documents/bench-{i}.md": b"x" * 4096 for i in range(size)})
            parent = SnapshotTree(rows)
            candidate = parent.fork()
            candidate["documents/bench-0.md"] = b"changed"
            candidate["cards/bench.md"] = b"card"
            outcome = replace(fixture[-1].evaluation, tree=candidate.snapshot())
            before, _ = measure(lambda: old_retain(scope, outcome, operation=b"bench", **fields))
            after, _ = measure(lambda: scope.retain(outcome, operation=b"bench", **fields))
            output[f"handoff_{size}"] = {"before": before, "after": after}
            body_args = dict(
                instance_id=coordinate.instance_id,
                object_format=coordinate.git_object_format,
                compiler=coordinate.compiler,
                genesis=instance.descriptor.genesis,
                sequence=1,
                git_oid=coordinate.git_oid,
                semantic_root=coordinate.semantic_root,
                generation_root=coordinate.generation_root,
                parent_generation_root=coordinate.generation_root,
                tree=rows,
            )
            members = members_for_tree(rows)
            before, old_body = measure(lambda: checkpoint_body(**body_args))
            after, new_body = measure(lambda: checkpoint_body(**body_args, members=members))
            assert old_body == new_body
            output[f"checkpoint_body_{size}"] = {"before": before, "after": after}
            # The changed warm read operation, excluding unchanged acceptance proof.
            before, _ = measure(lambda: dict(rows).get("documents/bench-0.md"), 101)
            after, _ = measure(lambda: rows.get("documents/bench-0.md"), 101)
            output[f"warm_mapping_lookup_{size}"] = {"before": before, "after": after}
            # No application tree memo; OS/Git object caches may remain warm.
            tree_oid = instance._ledger._write_tree(rows)
            oid = (
                instance._ledger._git(
                    ["-c", "commit.gpgsign=false", "commit-tree", tree_oid, "-m", "probe"]
                )
                .decode()
                .strip()
            )
            before, old_bytes = measure(
                lambda: instance._ledger.read_tree(oid).get("documents/bench-0.md")
            )
            after, new_bytes = measure(
                lambda: instance._ledger.blob_at(oid, "documents/bench-0.md")
            )
            assert old_bytes == new_bytes
            output[f"uncached_git_lookup_{size}"] = {"before": before, "after": after}
    for size in (20, 300):

        def ledger(name):
            return GitLedger.initialize(
                root / f"{name}-{size}.git",
                object_format="sha1",
                signing_key_path=root / "unused",
                allowed_signers_path=root / "unused",
            )

        local, remote = ledger("local"), ledger("remote")
        first = commit(local, "first")
        refs(local, **{MAIN: first})
        assert local.push_mirror(str(remote.path)) is None
        archive = {f"refs/settled/{i:064x}": first for i in range(size)}
        commands = "".join(f"create {ref} {oid}\n" for ref, oid in archive.items()).encode()
        for repo in (local, remote):
            repo._git(["update-ref", "--stdin"], input_bytes=commands)
        expected = local.mirror_refs()
        later = commit(local, "later", first)
        refs(local, **{MAIN: later})
        for label, push in (("before", old_push), ("after", GitLedger.push_mirror)):

            def invoke():
                return push(local, str(remote.path), expected_remote=expected)

            timing, error = measure(invoke, setup=lambda: refs(remote, **{MAIN: first}))
            output[f"mirror_{size}_{label}"] = {**timing, "error": error}
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="playbill-hotpath-probe-") as directory:
        run(Path(directory))

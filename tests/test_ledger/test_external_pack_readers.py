"""Public Git readers survive advisory append and superseded-pair retirement.

Ported from the F-001 reviewer reproduction, with unique-name publication hooks
and successful mirror transport, retirement and long-lived reader coverage.
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from cruxible_core.governance.keys import generate_daemon_key
from cruxible_core.ledger.git import GitLedger, _batch_reader, _command_environment


@pytest.fixture(params=["sha1", "sha256"])
def repos(tmp_path, request):
    key = generate_daemon_key(tmp_path / "credentials")
    signers = tmp_path / "allowed"
    signers.write_bytes(b"daemon " + key.public_key_path.read_bytes())

    def initialize(name):
        return GitLedger.initialize(
            tmp_path / name,
            object_format=request.param,
            signing_key_path=key.private_key_path,
            allowed_signers_path=signers,
        )

    ledger, remote = initialize("ledger.git"), initialize("mirror.git")
    base = ledger.create_signed_genesis({"one": b"base"}, timestamp="2026-10-15T12:00:00Z")
    ledger.set_main_genesis(base)
    return ledger, remote, base


def _proposal(ledger, base):
    proposal, _ = ledger.create_proposal_commit(
        {"one": b"changed"},
        base_oid=base,
        target_ref="refs/proposals/owner/test",
        actor_id="owner",
        timestamp="2026-10-15T12:01:00Z",
        expected_ref_oid=None,
        message="Change one",
    )
    ledger.retain_proposal_review("sha256:" + "a" * 64, proposal)
    return proposal


@pytest.mark.parametrize("reader", ["fsck", "cat-file", "compact", "mirror", "grep"])
@pytest.mark.parametrize("checkpoint", ["pack", "index", "retire-index", "retire-pack"])
def test_public_git_reader_survives_advisory_pack_append(repos, monkeypatch, reader, checkpoint):
    ledger, remote, base = repos
    ready, gap, resume = Event(), Event(), Event()
    published = {}
    original_replace, original_unlink = os.replace, Path.unlink

    def pause():
        gap.set()
        assert resume.wait(30), "reader did not resume publication"

    def replace(source, destination):
        original_replace(source, destination)
        destination = Path(destination)
        old = published.get("pack")
        if old is None or destination.parent != old.parent:
            return
        if destination.suffix == ".pack" and destination != old:
            published["new_pack"] = destination
            assert destination.with_suffix(".pending").exists()
            assert destination.with_suffix(".keep").exists()
            assert not destination.with_suffix(".idx").exists()
            assert old.read_bytes() == published["old_bytes"]
            if checkpoint == "pack":
                pause()
        elif destination == published.get("new_pack", old).with_suffix(".idx"):
            if checkpoint == "index":
                pause()

    def unlink(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        old = published.get("pack")
        if old is not None and (
            (checkpoint == "retire-index" and path == old.with_suffix(".idx"))
            or (checkpoint == "retire-pack" and path == old)
        ):
            pause()
        return result

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)

    def append():
        with ledger.request_pack():
            proposal = _proposal(ledger, base)
            published["proposal"] = proposal
            published["snapshot"] = ledger.mirror_refs()
            published["pack"] = _batch_reader(ledger.path)._transaction.pack
            published["old_bytes"] = published["pack"].read_bytes()
            ready.set()
            ledger.write_proposal_note("evaluation", proposal, b"evaluation\n")
            transaction = _batch_reader(ledger.path)._transaction
            assert transaction.pack == published["new_pack"]
            assert transaction.keeps == [transaction.pack]

    with ThreadPoolExecutor(max_workers=1) as writer:
        writing = writer.submit(append)
        try:
            assert ready.wait(10), "proposal was not published"
            assert gap.wait(10), "did not reach publication checkpoint"
            proposal = published["proposal"]
            if reader == "fsck":
                # Deliberately independent of unreachable_commits' store lock.
                ledger._git(["fsck", "--unreachable", "--no-reflogs"])
            elif reader == "cat-file":
                assert ledger._git(["cat-file", "-t", proposal]) == b"commit\n"
                assert ledger._git(["show", f"{proposal}:one"]) == b"changed"
            elif reader == "compact":
                ledger.compact_objects()
            elif reader == "mirror":
                assert ledger.push_mirror(str(remote.path), snapshot=published["snapshot"]) is None
                assert remote.mirror_refs() == published["snapshot"]
                assert remote._git(["show", f"{proposal}:one"]) == b"changed"
                remote._git(["fsck", "--no-dangling"])
            elif reader == "grep":
                assert ledger.tree_paths_containing_literal(
                    proposal, literal="changed", paths=["one"]
                ) == ("one",)
        finally:
            resume.set()
            writing.result(timeout=15)
    old, new = published["pack"], published["new_pack"]
    assert old != new
    assert not any(
        old.with_suffix(suffix).exists() for suffix in (".idx", ".pack", ".pending", ".keep")
    )
    assert _batch_reader(ledger.path).store.locations[proposal][0] == new
    assert old not in _batch_reader(ledger.path).store.members
    assert old not in _batch_reader(ledger.path).store.maps
    ledger._git(["fsck", "--no-dangling"])


@pytest.mark.parametrize("prime", ["mapped-pack", "discovered-index"])
def test_long_lived_git_reader_spans_retirement(repos, prime):
    ledger, _, base = repos
    with ledger.request_pack():
        proposal = _proposal(ledger, base)
        store = _batch_reader(ledger.path).store
        old = _batch_reader(ledger.path)._transaction.pack
        expected = ledger._git(["cat-file", "commit", proposal])
        # A missing lookup inventories all pack indexes without reading this
        # proposal's pack payload. The other case opens/maps the old pack too.
        first = proposal if prime == "mapped-pack" else "0" * len(proposal)
        with subprocess.Popen(
            ["git", f"--git-dir={ledger.path}", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_command_environment(),
        ) as process:
            try:
                process.stdin.write((first + "\n").encode())
                process.stdin.flush()
                header = process.stdout.readline()
                if prime == "mapped-pack":
                    assert header == f"{proposal} commit {len(expected)}\n".encode()
                    assert process.stdout.read(len(expected) + 1) == expected + b"\n"
                else:
                    assert header == f"{first} missing\n".encode()
                # Also prime the resident mapping so retirement must close it.
                assert store.read(proposal) == ("commit", expected)
                assert old in store.maps
                ledger.write_proposal_note("evaluation", proposal, b"evaluation\n")
                assert not old.exists()
                assert old not in store.maps
                body_oid = ledger._blob_oid(b"evaluation\n")
                output, errors = process.communicate(
                    (proposal + "\n" + body_oid + "\n").encode(), timeout=15
                )
                assert process.returncode == 0, errors
                assert output == (
                    f"{proposal} commit {len(expected)}\n".encode()
                    + expected
                    + b"\n"
                    + f"{body_oid} blob 11\n".encode()
                    + b"evaluation\n\n"
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
    ledger._git(["fsck", "--no-dangling"])

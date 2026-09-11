"""Complete physical deltas retain full-tree bytes and refusal checks."""

import pytest

from cruxible_client.contracts.errors import PlaybillGitError, SettlementIntegrityError
from cruxible_core.ledger.git import GitLedger
from tests.test_ledger.test_activation import _instance
from tests.test_ledger.test_activation_handoff_guards import _input


@pytest.fixture(params=("sha1", "sha256"))
def ledger(tmp_path, request):
    return GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=request.param,
        signing_key_path=tmp_path / "unused-key",
        allowed_signers_path=tmp_path / "unused-signers",
    )


def test_delta_matches_full_tree_and_reads_only_changed_blobs(ledger, monkeypatch):
    parent = {f"kept/{i}": f"payload-{i}".encode() for i in range(100)}
    parent.update({"remove": b"remove", "edit": b"before", "move": b"move"})
    before = ledger._write_tree(parent)
    parent = ledger.read_tree(before)
    successor = {
        **parent,
        "edit": b"after",
        "odd/space\tnewline\n雪": b"\0\xff",
        "moved": parent["move"],
    }
    del successor["remove"]
    del successor["move"]
    after = ledger._write_tree(successor)
    expected = ledger.read_tree(after)
    read = ledger.read_blobs
    objects = []

    def counted(oids):
        objects.extend(oids)
        return read(oids)

    monkeypatch.setattr(ledger, "read_blobs", counted)
    actual = ledger.read_tree_delta(before, after, parent_tree=parent)
    assert actual == expected
    assert tuple(actual) == tuple(expected)
    assert len(objects) == 3
    assert parent["edit"] == b"before" and "remove" in parent
    objects.clear()
    assert ledger.read_tree_delta(after, after, parent_tree=actual) == expected
    assert objects == []
    assert ledger.read_tree_delta(after, before, parent_tree=actual) == parent


@pytest.mark.parametrize("mode", ["100755", "120000", "160000"])
def test_delta_refuses_mode_and_type_changes_like_full_read(ledger, mode):
    parent = {"entry": b"bytes"}
    before = ledger._write_tree(parent)
    blob = ledger._blob_oid(parent["entry"])
    kind = "commit" if mode == "160000" else "blob"
    if kind == "commit":
        blob = "f" * len(blob)
    # Missing gitlink target is legal to mktree; both readers reject its mode.
    after = (
        ledger._git(["mktree", "--missing"], input_bytes=f"{mode} {kind} {blob}\tentry\n".encode())
        .decode()
        .strip()
    )
    with pytest.raises(PlaybillGitError, match="unsupported"):
        ledger.read_tree(after)
    with pytest.raises(PlaybillGitError, match="unsupported"):
        ledger.read_tree_delta(before, after, parent_tree=parent)


def test_settlement_checks_all_stored_bytes_and_avoids_full_tree_read(tmp_path, monkeypatch):
    instance, _, reviewer = _instance(tmp_path)
    inputs = _input(instance, reviewer)
    instance.immutable_tree_at(inputs["base"].git_oid)

    def full_read(*args, **kwargs):
        pytest.fail("warm settlement must read only changed physical blobs")

    monkeypatch.setattr(instance._ledger, "read_tree", full_read)
    bundle = instance.prepare_generation(**inputs, sequence=1)
    assert bundle.tree[bundle.record_path]
    original = instance._ledger.create_signed_generation

    def changed(tree, **kwargs):
        return original({**tree, "unrelated/injected": b"tampered"}, **kwargs)

    monkeypatch.setattr(instance._ledger, "create_signed_generation", changed)
    with pytest.raises(SettlementIntegrityError, match="stored generation tree differs"):
        instance.prepare_generation(**inputs, sequence=1)
    assert instance._ledger.read_main() == inputs["base"].git_oid

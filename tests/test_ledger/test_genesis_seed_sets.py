"""Generation zero verifies against every historical genesis seed set, and only those."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import Path

import pytest

import cruxible_core.runtime.instance as instance_module
from cruxible_client.contracts.approval_policy import ApprovalPolicy
from cruxible_client.contracts.errors import BootstrapError
from cruxible_client.contracts.types import TrustRoot
from cruxible_core.governance.keys import (
    ALLOWED_SIGNERS_FILE,
    GeneratedKeyMaterial,
    generate_daemon_key,
)
from cruxible_core.ledger.bootstrap import (
    GENESIS_SEED_SETS,
    GenesisSeedSet,
    genesis_seed_files,
    genesis_seed_set,
    genesis_tree,
    verify_genesis,
)
from cruxible_core.ledger.git import GitLedger
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._support import FIXED_TIMESTAMP, generate_client, initialize_fresh

#: The frozen prefix of the seed-set table: set ID -> SHA-256 over its sorted
#: (path, digest) lines. Appending a set appends a line here; editing an old
#: line, or the bytes behind it, is the regression this guards.
FROZEN_SEED_SETS = (
    ("empty", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
    ("triggers-3", "8cb042bc848fdee33bd027357e2457b750899b2977600192b8da4c000cd6b9c4"),
    ("triggers-4", "1fdfd48c7ac8e052884d5715c2f00d38486170497c7209f5f918d1f931f321ee"),
)


def _set_fingerprint(seed_set: GenesisSeedSet) -> str:
    lines = "".join(f"{path} {digest}\n" for path, digest in sorted(seed_set.files))
    return hashlib.sha256(lines.encode()).hexdigest()


def test_historical_seed_sets_are_append_only_and_their_bytes_never_change() -> None:
    ids = [seed_set.set_id for seed_set in GENESIS_SEED_SETS]
    assert len(ids) == len(set(ids))
    recorded = [(item.set_id, _set_fingerprint(item)) for item in GENESIS_SEED_SETS]
    assert recorded[: len(FROZEN_SEED_SETS)] == list(FROZEN_SEED_SETS)
    assert len(recorded) == len(FROZEN_SEED_SETS), "pin the new seed set in FROZEN_SEED_SETS"

    root = Path(str(files("cruxible_core.governance.seed_artifacts").joinpath("genesis")))
    on_disk = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    declared = {
        f"{seed_set.set_id}/{path}" for seed_set in GENESIS_SEED_SETS for path in seed_set.paths
    }
    assert on_disk == declared
    for seed_set in GENESIS_SEED_SETS:
        loaded = genesis_seed_files(seed_set)
        assert {
            path: hashlib.sha256(content).hexdigest() for path, content in loaded.items()
        } == dict(seed_set.files)


@pytest.mark.parametrize("set_id", [item.set_id for item in GENESIS_SEED_SETS])
def test_an_instance_created_under_each_historical_seed_set_still_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_id: str
) -> None:
    seed_set = genesis_seed_set(set_id)
    monkeypatch.setattr(instance_module, "current_genesis_seed_set", lambda _kinds: seed_set)
    instance, _owner = initialize_fresh(tmp_path)
    monkeypatch.undo()

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert reopened._verified_genesis.seed_set == seed_set
    assert {path for path in reopened._verified_genesis.tree if path.startswith("triggers/")} == {
        path for path in seed_set.paths if path.startswith("triggers/")
    }


def test_a_new_instance_starts_with_the_newest_seed_set(tmp_path: Path) -> None:
    instance, _owner = initialize_fresh(tmp_path)

    assert instance._verified_genesis.seed_set == GENESIS_SEED_SETS[-1]


def _trust(tmp_path: Path) -> tuple[TrustRoot, GeneratedKeyMaterial]:
    managed = tmp_path / "managed"
    owner = generate_client(tmp_path, managed_root=managed, principal_id="owner", roles=("owner",))
    credentials = tmp_path / "daemon-custody"
    daemon = generate_daemon_key(credentials)
    trust = TrustRoot(
        instance_id="inst_seed_sets",
        daemon_public_key=daemon.principal.public_key,
        principals=tuple(
            sorted((daemon.principal, owner.principal), key=lambda item: item.principal_id)
        ),
    )
    return trust, daemon


def _signed_genesis(
    tmp_path: Path,
    trust: TrustRoot,
    daemon: GeneratedKeyMaterial,
    seeds: dict[str, bytes],
    name: str,
) -> tuple[GitLedger, str]:
    ledger = GitLedger.initialize(
        tmp_path / f"{name}.git",
        object_format="sha256",
        signing_key_path=daemon.private_key_path,
        allowed_signers_path=daemon.private_key_path.parent / ALLOWED_SIGNERS_FILE,
    )
    tree = genesis_tree(
        trust.principals,
        approval_policy=ApprovalPolicy(mode="self_approval_allowed"),
        seeds=seeds,
    )
    return ledger, ledger.create_signed_genesis(tree, timestamp=FIXED_TIMESTAMP)


def test_a_genesis_matching_no_historical_seed_set_is_refused(tmp_path: Path) -> None:
    trust, daemon = _trust(tmp_path)
    newest = genesis_seed_files(genesis_seed_set("triggers-4"))
    three = genesis_seed_files(genesis_seed_set("triggers-3"))
    unknown = {
        # A subset no build ever seeded.
        "subset": {"triggers/evidence-sweep.json": three["triggers/evidence-sweep.json"]},
        # A historical path set with one byte changed.
        "edited": {
            **newest,
            "triggers/floor-refresh.json": newest["triggers/floor-refresh.json"].replace(
                b"\n}", b"}"
            ),
        },
        # A historical set plus an unseeded artifact.
        "extra": {**three, "triggers/curation-detect.json": newest["triggers/curation-detect.json"]}
        | {"queries/stray.json": b"{}"},
    }
    for name, seeds in unknown.items():
        ledger, oid = _signed_genesis(tmp_path, trust, daemon, seeds, name)
        with pytest.raises(BootstrapError, match="match no historical genesis seed set"):
            verify_genesis(ledger, oid, trust_root=trust)

    for seed_set in GENESIS_SEED_SETS:
        ledger, oid = _signed_genesis(
            tmp_path, trust, daemon, genesis_seed_files(seed_set), f"ok-{seed_set.set_id}"
        )
        assert verify_genesis(ledger, oid, trust_root=trust).seed_set == seed_set

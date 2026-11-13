"""Generation zero holds exactly this release's genesis seeds, and verification admits no other."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import Path

import pytest

from cruxible_client.contracts.approval_policy import ApprovalPolicy
from cruxible_client.contracts.errors import BootstrapError
from cruxible_client.contracts.types import TrustRoot
from cruxible_core.governance.keys import (
    ALLOWED_SIGNERS_FILE,
    GeneratedKeyMaterial,
    generate_daemon_key,
)
from cruxible_core.ledger.bootstrap import (
    GENESIS_SEEDS,
    genesis_seed_files,
    genesis_tree,
    seeded_procedure_runtime_policy,
    verify_genesis,
)
from cruxible_core.ledger.git import GitLedger
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._support import FIXED_TIMESTAMP, generate_client, initialize_fresh


def test_the_checked_in_seed_bytes_are_exactly_the_declared_genesis_seeds() -> None:
    root = Path(str(files("cruxible_core.governance.seed_artifacts").joinpath("genesis")))
    on_disk = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert on_disk == {path for path, _digest in GENESIS_SEEDS}
    assert {
        path: hashlib.sha256(content).hexdigest() for path, content in genesis_seed_files().items()
    } == dict(GENESIS_SEEDS)


def test_a_new_instance_starts_with_the_genesis_seeds_and_reopens(tmp_path: Path) -> None:
    instance, _owner = initialize_fresh(tmp_path)
    seeds = genesis_seed_files()

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    genesis = reopened._verified_genesis.tree
    assert {path: content for path, content in genesis.items() if path in seeds} == seeds
    # A fresh reopen, before any proposal, reproduces every recorded coordinate.
    verified = reopened._verified_genesis
    assert reopened.descriptor.genesis == instance.descriptor.genesis
    assert reopened.descriptor.genesis.semantic_root == verified.semantic_root.tagged
    assert reopened.descriptor.genesis.generation_root == verified.generation_root.tagged
    assert reopened.descriptor.genesis.bootstrap_root == verified.bootstrap_root.tagged
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()


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
        procedure_runtime_policy=seeded_procedure_runtime_policy(),
        seeds=seeds,
    )
    return ledger, ledger.create_signed_genesis(tree, timestamp=FIXED_TIMESTAMP)


def test_a_genesis_seeded_with_anything_but_the_genesis_seeds_is_refused(tmp_path: Path) -> None:
    trust, daemon = _trust(tmp_path)
    current = genesis_seed_files()
    alias = "claim-types/semantic/alias.json"
    unknown = {
        # An earlier build's genesis: no seeds at all.
        "empty": {},
        # A subset of the seeds.
        "subset": {path: content for path, content in current.items() if path != alias},
        # The seeds with one byte changed.
        "edited": {
            **current,
            "triggers/floor-refresh.json": current["triggers/floor-refresh.json"].replace(
                b"\n}", b"}"
            ),
        },
        # The seeds plus a retired ProviderInterface registration an earlier build seeded.
        "extra": {**current, "provider-interfaces/workspace.file.json": b"{}\n"},
    }
    for name, seeds in unknown.items():
        ledger, oid = _signed_genesis(tmp_path, trust, daemon, seeds, name)
        with pytest.raises(BootstrapError, match="differ from this release's genesis seeds"):
            verify_genesis(ledger, oid, trust_root=trust)

    ledger, oid = _signed_genesis(tmp_path, trust, daemon, current, "current")
    verified = verify_genesis(ledger, oid, trust_root=trust)
    assert {path: verified.tree[path] for path in current} == current

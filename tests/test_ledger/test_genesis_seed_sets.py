"""Generation zero holds one known genesis seed set; open admits any set in the history."""

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
from cruxible_core.ledger import genesis_seeds
from cruxible_core.ledger.bootstrap import (
    GENESIS_SEEDS,
    genesis_seed_files,
    genesis_tree,
    prepare_genesis,
    seeded_procedure_runtime_policy,
    verify_genesis,
)
from cruxible_core.ledger.genesis_seeds import (
    GenesisSeedSet,
    current_genesis_seed_set,
    held_seed_table,
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
    assert reopened._verified_genesis.seed_set == current_genesis_seed_set()
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


def _fabricated_second_set() -> GenesisSeedSet:
    """Test-only entry 2: entry 1 without one Trigger (a later release dropping a seed)."""

    first = current_genesis_seed_set()
    return GenesisSeedSet(
        ordinal=first.ordinal + 1,
        name="test-only-2",
        seeds=tuple(
            seed for seed in first.seeds if seed.path != "triggers/prediction-anchor-retry.json"
        ),
    )


def test_an_instance_keeps_its_set_when_a_newer_one_is_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "created-under-1").mkdir()
    (tmp_path / "created-under-2").mkdir()
    instance, _owner = initialize_fresh(tmp_path / "created-under-1")
    first = current_genesis_seed_set()
    second = _fabricated_second_set()
    monkeypatch.setattr(genesis_seeds, "GENESIS_SEED_HISTORY", (first, second))

    # Created under entry 1, it still opens once entry 2 is the newest.
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened._verified_genesis.seed_set == first
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()

    # Creation writes the newest set.
    trust, daemon = _trust(tmp_path / "created-under-2")
    ledger = GitLedger.initialize(
        tmp_path / "created-under-2" / "ledger.git",
        object_format="sha256",
        signing_key_path=daemon.private_key_path,
        allowed_signers_path=daemon.private_key_path.parent / ALLOWED_SIGNERS_FILE,
    )
    created = prepare_genesis(
        ledger,
        trust_root=trust,
        approval_policy=ApprovalPolicy(mode="self_approval_allowed"),
        timestamp=FIXED_TIMESTAMP,
    )
    assert created.seed_set == second
    held = {path: content for path, content in created.tree.items() if path in first.table()}
    assert held_seed_table(held) == second.table()


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
    named = {
        "empty": f"{alias} (missing) and 5 more path(s)",
        "subset": f"{alias} (missing).",
        "edited": "triggers/floor-refresh.json (different bytes).",
        "extra": "provider-interfaces/workspace.file.json (unexpected).",
    }
    for name, seeds in unknown.items():
        ledger, oid = _signed_genesis(tmp_path, trust, daemon, seeds, name)
        with pytest.raises(BootstrapError) as refused:
            verify_genesis(ledger, oid, trust_root=trust)
        message = str(refused.value)
        assert message.startswith(
            "genesis seeded artifacts match no known genesis seed set; the closest is set 1 "
            "(0.5.0), which differs at " + named[name]
        ), message
        assert "this release" not in message

    ledger, oid = _signed_genesis(tmp_path, trust, daemon, current, "current")
    verified = verify_genesis(ledger, oid, trust_root=trust)
    assert {path: verified.tree[path] for path in current} == current
    assert verified.seed_set == current_genesis_seed_set()

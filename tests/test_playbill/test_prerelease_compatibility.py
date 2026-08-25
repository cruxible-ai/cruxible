"""Removed prerelease artifacts refuse before cold replay or checkpoint hydration."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import (
    PlaybillInstanceIncompatiblePrereleaseContent,
    SettlementIntegrityError,
)
from cruxible_core.playbill.bootstrap import render_principal
from cruxible_core.playbill.checkpoints import CHECKPOINT_DIRECTORY, checkpoint_path
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import ALLOWED_SIGNERS_FILE, generate_daemon_key
from tests.test_playbill._support import FIXED_TIMESTAMP, initialize_local
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface
from tests.test_playbill.test_evidence_freshness import _fresh_world


@pytest.mark.parametrize(
    "artifact_path", ["claim-types/knowledge/brief.yaml", "claims/ab/old.yaml"]
)
@pytest.mark.parametrize("warm_checkpoint", [False, True])
@pytest.mark.parametrize("lifecycle", ["live", "retired"])
def test_removed_brief_refuses_before_cold_or_checkpointed_replay(
    tmp_path: Path,
    artifact_path: str,
    warm_checkpoint: bool,
    lifecycle: str,
) -> None:
    instance, owner = initialize_local(tmp_path)
    if warm_checkpoint:
        _seed_claim_surface(instance, owner)
        instance = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
        assert checkpoint_path(instance.root / CHECKPOINT_DIRECTORY).exists()

    base = instance.accepted_coordinate().git_oid
    tree = instance.tree_at(base)
    if artifact_path.startswith("claims/"):
        tree[artifact_path] = (
            '{"lifecycle":{"state":"'
            + lifecycle
            + '"},"statement":{"predicate":"knowledge.brief"}}\n'
        ).encode()
    else:
        tree[artifact_path] = (
            b'{"artifact_format":"playbill-claim-type-v2","predicate":"knowledge.brief"}\n'
        )
    successor = instance._ledger.create_signed_generation(
        tree,
        parent_oid=base,
        sequence=instance._recovered.head.sequence + 1,
        timestamp=FIXED_TIMESTAMP,
    )
    assert instance._ledger.compare_and_set_main(successor, expected_oid=base)

    with pytest.raises(PlaybillInstanceIncompatiblePrereleaseContent) as refusal:
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert refusal.value.error_code == "playbill.instance.incompatible_prerelease_content"
    assert refusal.value.artifact_class == "knowledge.brief"


def test_invalid_generation_signature_precedes_prerelease_incompatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate().git_oid
    tree = instance.tree_at(base)
    tree["claim-types/knowledge/brief.yaml"] = (
        b'{"artifact_format":"playbill-claim-type-v2","predicate":"knowledge.brief"}\n'
    )
    successor = instance._ledger.create_signed_generation(
        tree,
        parent_oid=base,
        sequence=1,
        timestamp=FIXED_TIMESTAMP,
    )
    assert instance._ledger.compare_and_set_main(successor, expected_oid=base)
    monkeypatch.setattr(
        "cruxible_core.playbill.git.GitLedger.verify_commit_with_public_key",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(SettlementIntegrityError, match="daemon signature does not verify"):
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def test_removed_brief_after_daemon_key_rotation_is_incompatible_not_corrupt(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    ledger = instance._ledger
    base = instance.accepted_coordinate().git_oid
    rotated = generate_daemon_key(tmp_path / "rotated-daemon-custody")
    tree = instance.tree_at(base)
    tree["principals/daemon.yaml"] = render_principal(rotated.principal)

    rotation = ledger.create_signed_generation(
        tree,
        parent_oid=base,
        sequence=1,
        timestamp=FIXED_TIMESTAMP,
    )
    assert ledger.compare_and_set_main(rotation, expected_oid=base)

    ledger._signing_key_path = rotated.private_key_path
    ledger._allowed_signers_path = rotated.private_key_path.parent / ALLOWED_SIGNERS_FILE
    ledger.configure_signing()
    tree["claim-types/knowledge/brief.yaml"] = (
        b'{"artifact_format":"playbill-claim-type-v2","predicate":"knowledge.brief"}\n'
    )
    incompatible = ledger.create_signed_generation(
        tree,
        parent_oid=rotation,
        sequence=2,
        timestamp=FIXED_TIMESTAMP,
    )
    assert ledger.compare_and_set_main(incompatible, expected_oid=rotation)

    with pytest.raises(PlaybillInstanceIncompatiblePrereleaseContent) as refusal:
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert refusal.value.artifact_class == "knowledge.brief"


@pytest.mark.parametrize("warm_checkpoint", [False, True])
def test_precut_v3_freshness_world_reopens_byte_identically(
    tmp_path: Path,
    warm_checkpoint: bool,
) -> None:
    instance, _claim_id = _fresh_world(tmp_path)
    expected_coordinate = instance.accepted_coordinate()
    expected_tree = instance.tree_at(expected_coordinate.git_oid)
    path = checkpoint_path(instance.root / CHECKPOINT_DIRECTORY)
    if warm_checkpoint:
        PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
        assert path.exists()
    elif path.exists():
        path.unlink()

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert reopened.accepted_coordinate() == expected_coordinate
    assert reopened.tree_at(expected_coordinate.git_oid) == expected_tree

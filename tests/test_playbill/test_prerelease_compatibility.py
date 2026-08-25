"""Removed prerelease artifacts refuse before cold replay or checkpoint hydration."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import (
    PlaybillInstanceIncompatiblePrereleaseContent,
    SettlementIntegrityError,
)
from cruxible_core.playbill.checkpoints import CHECKPOINT_DIRECTORY, checkpoint_path
from cruxible_core.playbill.instance import PlaybillInstance
from tests.test_playbill._support import FIXED_TIMESTAMP, initialize_local
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface


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

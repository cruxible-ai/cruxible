"""Reject proposal garbage cheaply without broadening eligible deletion."""

from pathlib import Path

import pytest

from cruxible_client.contracts.laws import PLAYBILL_ACCEPTANCE_LAWS
from cruxible_core.playbill.recovery import _clean_unaccepted_generations
from tests.test_playbill._support import FIXED_TIMESTAMP, initialize_local


def _clean(instance):
    _clean_unaccepted_generations(
        instance._ledger,
        history=instance.accepted_history(),
        repository_path=str(instance._ledger.path),
        object_format=instance.descriptor.git_object_format,
        instance_id=instance.descriptor.instance_id,
        compiler=instance.descriptor.compiler,
        bodies=instance.body_store(),
        laws=PLAYBILL_ACCEPTANCE_LAWS,
        promotion_verifier=None,
        producer_receipt_resolver=None,
        query_facts_builder=None,
    )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_unsigned_orphans_do_not_materialize_parent_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, object_format: str
) -> None:
    instance, _owner = initialize_local(tmp_path, object_format=object_format)
    ledger = instance._ledger
    base = instance.accepted_coordinate()
    oid = ledger.proposal_review_commit(
        tree_oid=ledger.tree_oid(base.git_oid),
        base_oid=base.git_oid,
        actor_id="owner",
        timestamp=FIXED_TIMESTAMP,
        message="Unreferenced unsigned proposal",
    )
    assert oid in ledger.unreachable_commits()

    def unexpected(*args, **kwargs):
        pytest.fail("unsigned proposal garbage must not read a complete parent tree")

    monkeypatch.setattr(ledger, "read_tree", unexpected)
    _clean(instance)
    assert ledger.object_exists(oid)  # No new permission to delete proposal debris.
    assert instance.accepted_coordinate() == base


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_valid_daemon_signature_alone_does_not_authorize_collection(
    tmp_path: Path, object_format: str
) -> None:
    instance, _owner = initialize_local(tmp_path, object_format=object_format)
    ledger = instance._ledger
    base = instance.accepted_coordinate()
    # A signed child that omits a change-set record is not a valid generation.
    oid = ledger.create_signed_generation(
        instance.tree_at(base.git_oid),
        parent_oid=base.git_oid,
        sequence=1,
        timestamp=FIXED_TIMESTAMP,
        message="Signed but invalid generation",
    )
    assert oid in ledger.unreachable_commits()
    _clean(instance)
    assert ledger.object_exists(oid)
    assert instance.accepted_coordinate() == base

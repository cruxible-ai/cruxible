"""Only the head's change-set record stays resident; the rest are read back from Git."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_core.ledger import recovery as recovery_module
from cruxible_core.proposals.settlement import parse_change_set_record
from tests.core_support._knowledge_loop_support import seed_claims


def test_recovered_history_keeps_only_the_head_record_resident(tmp_path):
    instance, _owner = seed_claims(tmp_path)
    history = instance._recovered.history
    assert len(history) > 2
    assert history[-1].retained_record is not None
    assert all(item.retained_record is None for item in history[:-1])
    for item in history[1:-1]:
        path = f"changesets/cs-{item.sequence:020d}.json"
        expected = parse_change_set_record(instance.blob_at(item.oid, path), path=path)
        assert item.record == expected
        assert item.record.changeset_digest == item.record_digest


def test_a_released_record_that_differs_from_its_digest_is_refused(tmp_path):
    instance, _owner = seed_claims(tmp_path)
    released = instance._recovered.history[1]
    forged = replace(released, record_digest="sha256:" + "00" * 32)
    recovery_module._RELEASED_RECORDS.clear()
    with pytest.raises(SettlementIntegrityError, match="differs from its verified digest"):
        _ = forged.record

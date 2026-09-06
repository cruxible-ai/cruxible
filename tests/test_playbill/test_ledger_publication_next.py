"""Expected publication lag is visible without diagnosing a broken credential."""

from types import SimpleNamespace

import pytest

from cruxible_core.playbill.ledger_mirror import LedgerMirrorStateV1
from cruxible_core.service.playbill_next import _ledger_mirror_items


@pytest.mark.parametrize("status", ("pending", "publishing", "behind"))
def test_next_names_publication_status_and_the_appropriate_follow_up(status, tmp_path):
    state = LedgerMirrorStateV1(
        url=str(tmp_path / "test-mirror.git"),
        status=status,
        attempted_at="2026-09-05T12:00:00Z",
        requested_sequence=3,
        published_sequence=2,
    )
    instance = SimpleNamespace(
        ledger_mirror_url=lambda: state.url, ledger_mirror_state=lambda: state
    )
    (row,) = _ledger_mirror_items(instance, coordinate=SimpleNamespace(git_oid="a"))
    assert row.reason == "ledger_mirror_behind"
    assert row.detail["status"] == status
    assert row.detail["requested_sequence"] == 3
    assert row.detail["published_sequence"] == 2
    assert row.detail["publication_command"] == "cruxible playbill ledger publish --json"
    assert row.repair.required_change == (
        "restore_the_ledger_mirror_remote_or_its_credential"
        if status == "behind"
        else "wait_for_or_request_ledger_publication"
    )

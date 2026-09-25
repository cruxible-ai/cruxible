"""Expected publication lag is visible without diagnosing a broken credential."""

from types import SimpleNamespace

import pytest

from cruxible_core.ledger.ledger_mirror import LedgerMirrorStateV1
from cruxible_core.service.discovery.next import _ledger_mirror_health


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
        ledger_mirror_url=lambda: state.url,
        ledger_mirror_state=lambda: state,
        accepted_coordinate=lambda: SimpleNamespace(git_oid="a"),
    )

    health = _ledger_mirror_health(instance)

    if status == "behind":
        # A failed push needs the remote or its credential restored, off this host.
        assert health.state == "behind"
        assert health.detail["requested_sequence"] == 3
        assert health.detail["published_sequence"] == 2
        assert health.detail["publication_command"] == "cruxible playbill ledger publish --json"
        assert health.repair is not None
        assert health.repair.required_change == (
            "restore_the_ledger_mirror_remote_or_its_credential"
        )
    else:
        # A push still in flight is informational: nothing to repair.
        assert health.state == "publishing"
        assert health.detail["status"] == status
        assert health.repair is None

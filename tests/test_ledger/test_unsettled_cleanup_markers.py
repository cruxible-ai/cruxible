"""The whole-object-store scan for unsettled generations runs only when one could exist."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.ledger.git import GitLedger
from cruxible_core.runtime.instance import PlaybillInstance
from tests.test_ledger.test_recovery import _prepared


def _count_scans(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    scans: list[int] = []
    original = GitLedger.unreachable_commits

    def counted(self):  # type: ignore[no-untyped-def]
        scans.append(1)
        return original(self)

    monkeypatch.setattr(GitLedger, "unreachable_commits", counted)
    return scans


def test_a_clean_restart_never_scans_and_a_left_generation_is_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, base, bundle = _prepared(tmp_path)
    ledger = instance._ledger
    # The prepared generation commit exists but never reached main: in flight.
    assert ledger.unaccepted_cleanup_due() == (bundle.oid,)
    scans = _count_scans(monkeypatch)

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert scans == [1]
    assert reopened.accepted_coordinate() == base
    assert not reopened._ledger.object_exists(bundle.oid)
    assert reopened._ledger.unaccepted_cleanup_due() is None

    PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert scans == [1]  # nothing was in flight: no scan


def test_a_ledger_that_never_completed_a_scan_runs_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _base, _bundle = _prepared(tmp_path)
    ledger = instance._ledger
    (ledger.path / "playbill-unsettled-cleanup-v1").unlink()
    for marker in (ledger.path / "playbill-generations-in-flight").iterdir():
        marker.unlink()
    assert ledger.unaccepted_cleanup_due() == ()
    scans = _count_scans(monkeypatch)
    PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert scans == [1]


def test_a_won_activation_leaves_nothing_in_flight(tmp_path: Path) -> None:
    instance, base, bundle = _prepared(tmp_path)
    publisher = instance.activation_publisher()
    result = publisher.activate(bundle, publisher.prebuild(bundle, base=base), base=base)
    assert result.status == "accepted"
    assert instance._ledger.unaccepted_cleanup_due() is None

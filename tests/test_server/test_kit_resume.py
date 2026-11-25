"""A kit add stopped at awaiting_providers (recorded by an earlier version) proposes its
definitions once the installs land; a fresh add records no wait."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import Duration
from cruxible_client.contracts.kits import KitAddRequest
from cruxible_client.kits import stage_kit_providers
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import PROVIDER_RUNTIME_CONFIG_PATH
from cruxible_core.service import kit_resume
from tests.support.provider_checkout import ProviderCheckout
from tests.test_server.test_kit_providers import (
    PROCEDURE,
    _accept_local_call_contract,
    _add,
    _author,
    _build,
    _client,
    _install,
    _package,
    _tree,
)


def _open_worlds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, independent: bool
) -> Iterator[tuple[Any, Any]]:
    from tests.test_server.test_playbill_kits import _fresh_open_worlds

    config = tmp_path / "server-state" / PROVIDER_RUNTIME_CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {"provider_index_urls": ["https://pypi.org/simple", "https://files.pythonhosted.org/"]}
        )
    )
    yield from _fresh_open_worlds(tmp_path, monkeypatch, independent=independent)


def _resumes(world: Any, *, kit_id: str | None = None) -> tuple[Any, ...]:
    rows = playbill_api.playbill_kit_status(world.instance_id).resumes
    return tuple(row for row in rows if kit_id is None or row.kit_id == kit_id)


def _release(publisher: Any, bundled: Path, **options: Any) -> Any:
    _install(publisher, bundled, control_domain="publisher")
    _author(publisher)
    return _build(publisher, bundled, **options)


def test_a_fresh_add_records_no_resume_and_proposes_one_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    """0.6.0: the install and the definitions are one proposal, so nothing waits.

    The resume reader stays for a wait an earlier version recorded (below).
    """

    opened = _open_worlds(tmp_path, monkeypatch, independent=True)
    publisher, consumer = next(opened)
    try:
        bundled = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        release = _release(publisher, bundled)
        _accept_local_call_contract(consumer)

        added = _add(consumer, release, dry_run=False)

        assert added.status == "proposed", added
        assert added.proposal_id is not None
        (step,) = added.providers
        assert step.action == "awaiting_approval" and step.proposal_id == added.proposal_id
        assert "proposed automatically" not in (added.detail or "")
        # Under independent approval creation's base kit add stopped at its
        # proposal: kit status shows that row, and none for acme.
        (base,) = _resumes(consumer, kit_id="base")
        assert base.state == "proposed" and base.waiting_on == ()
        assert _resumes(consumer, kit_id="acme") == ()
        assert kit_resume._any_waiting(get_playbill_manager().get(consumer.instance_id)) is False

        assert f"procedures/{PROCEDURE}.json" not in _tree(consumer)
        consumer.approve(added.proposal_id)
        assert f"procedures/{PROCEDURE}.json" in _tree(consumer)
        assert _resumes(consumer, kit_id="acme") == ()
    finally:
        for _ in opened:
            pass


def test_a_recorded_resume_lands_blocks_or_closes_against_the_head_it_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_checkout: ProviderCheckout
) -> None:
    opened = _open_worlds(tmp_path, monkeypatch, independent=False)
    publisher, consumer = next(opened)
    try:
        bundled = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
        release = _release(publisher, bundled)
        older = _build(publisher, bundled, version="0.9.0")
        instance = get_playbill_manager().get(consumer.instance_id)
        staged = stage_kit_providers(_client(consumer.http), consumer.instance_id, release)
        request = KitAddRequest(bundle=staged, source="test")
        # The provider is live here already: each record below stands for a kit
        # add whose install activated.
        _install(consumer, bundled)

        def record(actor: str = "operator", add: KitAddRequest = request) -> None:
            kit_resume.record_kit_resume(
                instance, add, actor_id=actor, waiting_on=add.bundle.manifest.providers
            )

        # An activation that completes no kit does no extra work: no waits, no read.
        assert kit_resume._any_waiting(instance) is False
        touched: list[bool] = []
        with monkeypatch.context() as patched:
            patched.setattr(kit_resume, "retained_records", lambda *a, **k: touched.append(True))
            kit_resume.resume_kits_after_activation(
                instance, (f"providers/{release.manifest.providers[0].build}.json",)
            )
        assert touched == []

        # The principal who ran kit add can no longer author: blocked, and next says so.
        record(actor="nobody")
        kit_resume.resume_kits_after_activation(
            instance, (f"providers/{release.manifest.providers[0].build}.json",)
        )
        (blocked,) = _resumes(consumer)
        assert blocked.state == "blocked"
        assert (blocked.detail or "").startswith("actor_no_longer_authoring")

        # A crash between the install's activation and the resume: the daemon-start
        # pass finds the satisfied row and proposes; no approval needed, so it lands.
        record()
        assert kit_resume.resume_pending_kits(instance) == ("acme",)
        # It landed, so its proposal is closed and kit status shows no pending row.
        assert _resumes(consumer) == ()
        assert f"procedures/{PROCEDURE}.json" in _tree(consumer)

        # The kit is already in place (as after a manual kit add): unchanged, row gone.
        record()
        kit_resume.resume_kits_after_activation(
            instance, (f"providers/{release.manifest.providers[0].build}.json",)
        )
        assert _resumes(consumer) == ()

        # A manual commit kit add of the same kit replaces the record.
        record()
        assert _add(consumer, release, dry_run=False).status == "unchanged"
        assert _resumes(consumer) == ()

        # The head moved into a conflict (1.0.0 is installed; this records 0.9.0):
        # blocked, with a neutral next row naming kit add at the recorded source.
        older_staged = stage_kit_providers(_client(consumer.http), consumer.instance_id, older)
        record(add=KitAddRequest(bundle=older_staged, source="test"))
        kit_resume.resume_kits_after_activation(
            instance, (f"providers/{release.manifest.providers[0].build}.json",)
        )
        (conflicted,) = _resumes(consumer)
        assert conflicted.state == "blocked" and "older than installed" in (conflicted.detail or "")
        page = consumer.pb.next(expiring_within=Duration.days(count=7))
        rows = [item for item in page.items if item.reason == "kit_resume_blocked"]
        assert len(rows) == 1
        assert rows[0].subject_identity == "kit:acme"
        assert rows[0].detail["detail"] == conflicted.detail
    finally:
        for _ in opened:
            pass


def test_blocked_resumes_reads_by_index_not_the_full_view(tmp_path: Path) -> None:
    """``next``'s blocked-row read is a direct, indexed ``state`` selection.

    Before: it built every stored resume's status (``kit_resume_statuses``,
    which fetches every resume and wait row) and filtered in Python, so a
    ``next`` read grew with every completed (``proposed``) kit, never just the
    blocked ones.
    """

    from types import SimpleNamespace

    world = SimpleNamespace(
        root=tmp_path, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )

    def _insert(kit_id: str, state: str) -> None:
        with kit_resume.retained_records(world, write=True) as connection:
            assert connection is not None
            connection.execute(
                "INSERT INTO kit_resumes (kit_id,version,request_digest,actor_id,"
                "recorded_at,recorded_head,state,proposal_id,detail,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    kit_id,
                    "1.0.0",
                    "sha256:" + "0" * 64,
                    "operator",
                    "2026-01-01T00:00:00.000000Z",
                    "deadbeef",
                    state,
                    f"proposal-{kit_id}" if state == "proposed" else None,
                    None,
                    "test",
                ),
            )

    for n in range(50):
        _insert(f"completed-{n}", "proposed")
    _insert("the-blocked-one", "blocked")

    # The state selection is a SEARCH on the new index, never a table SCAN.
    with kit_resume.retained_records(world, write=False) as connection:
        assert connection is not None
        plan = [
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT kit_id FROM kit_resumes WHERE state='blocked'"
            ).fetchall()
        ]
    assert any("kit_resumes_by_state" in detail for detail in plan)
    assert not any("SCAN kit_resumes" in detail for detail in plan)

    # The population-loop guard: blocked_kit_resumes never builds the all-rows view.
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(
            kit_resume,
            "kit_resume_statuses",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("loaded the full resume view")),
        )
        (blocked,) = kit_resume.blocked_kit_resumes(world)
    assert blocked.kit_id == "the-blocked-one"
    assert blocked.state == "blocked"
    assert blocked.waiting_on == ()

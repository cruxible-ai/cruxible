"""One request reuses each bound projection instead of re-binding it per lookup (perf B3)."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import WriteRequest
from cruxible_core.indexes.evaluated_state import EvaluationRows
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._write_support import KIND, caller, seed_write_surface


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_write_surface(tmp_path)[0]


def _counted_binds(instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    binds: list[str] = []
    real = instance._bind_accepted_projection

    def bind(coordinate: Any) -> Any:
        binds.append(coordinate.git_oid)
        return real(coordinate)

    monkeypatch.setattr(instance, "_bind_accepted_projection", bind)
    return binds


def test_a_scope_hands_a_closed_handle_out_again_and_closes_all_at_exit(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = instance.accepted_coordinate()
    binds = _counted_binds(instance, monkeypatch)
    with instance.projection_scope():
        with instance.bind_accepted_projection(head) as first:
            # A handle is exclusive while held: a second holder gets its own.
            with instance.bind_accepted_projection(head) as second:
                assert second is not first
        with instance.bind_accepted_projection(head) as again:
            assert again in (first, second)
            assert not again._closed
        with instance.projection_scope():  # a nested scope joins the outer one
            with instance.bind_accepted_projection(head) as nested:
                assert nested in (first, second)
        assert len(binds) == 2
    assert first._closed and second._closed
    # Outside a scope every bind is fresh and really closes.
    with instance.bind_accepted_projection(head) as fresh:
        assert fresh not in (first, second)
    assert fresh._closed and len(binds) == 3


def test_a_handle_carrying_a_candidate_overlay_is_closed_never_reused(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = instance.accepted_coordinate()
    binds = _counted_binds(instance, monkeypatch)
    with instance.projection_scope():
        rows = EvaluationRows(instance.bind_accepted_projection(head))
        rows.overlay({})  # TEMP rows on the handle's connection
        overlaid = rows.projection
        rows.close()
        assert overlaid._closed
        with instance.bind_accepted_projection(head) as clean:
            assert clean is not overlaid
            assert (
                clean._connection.execute("SELECT count(*) FROM temp.sqlite_master").fetchone()[0]
                == 0
            )
        assert len(binds) == 2


def test_a_double_close_returns_a_handle_once_and_never_to_two_holders(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F-003: one checkout, one return, however often it is closed."""

    head = instance.accepted_coordinate()
    with instance.projection_scope():
        handle = instance.bind_accepted_projection(head)
        handle.close()
        handle.close()
        first = instance.bind_accepted_projection(head)
        second = instance.bind_accepted_projection(head)
        assert first is handle and second is not first
        first.close()
        second.close()


def test_scope_exit_closes_handles_still_checked_out_and_unwinds_on_error(
    instance: PlaybillInstance,
) -> None:
    head = instance.accepted_coordinate()
    with instance.projection_scope():
        outstanding = instance.bind_accepted_projection(head)
        returned = instance.bind_accepted_projection(head)
        returned.close()
    assert outstanding._closed and returned._closed
    assert outstanding._release is None and returned._release is None
    with pytest.raises(RuntimeError, match="unwind"):
        with instance.projection_scope():
            held = instance.bind_accepted_projection(head)
            raise RuntimeError("unwind")
    assert held._closed and held._release is None


def test_work_deferred_to_scope_exit_runs_once_after_every_hold_is_released(
    instance: PlaybillInstance,
) -> None:
    from cruxible_core.indexes import sqlite
    from cruxible_core.runtime.instance import _PROJECTION_POOL

    head = instance.accepted_coordinate()
    seen: list[tuple[bool, int]] = []
    with instance.projection_scope():
        handle = instance.bind_accepted_projection(head)
        handle.close()  # idle in the pool: still holding its piece
        key = sqlite._hold_key(handle.manifest_path)
        pool = _PROJECTION_POOL.get()
        assert pool is not None
        for _twice in range(2):
            assert pool.after_close(
                "evict", lambda: seen.append((handle._closed, sqlite._HOLDS.counts.get(key, 0)))
            )
        assert seen == []
    assert seen == [(True, 0)]
    # Outside a scope (or on a closed pool) the caller runs the work itself.
    assert not pool.after_close("evict", lambda: None)


def test_a_write_reads_the_same_answers_with_far_fewer_binds(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = WriteRequest.model_validate(
        {
            "because": "Counted binds.",
            "changes": [
                {"op": "set", "subject": f"{KIND}/scope-{n}", "field": "title", "value": f"t{n}"}
                for n in range(6)
            ],
            "dry_run": True,
        }
    )
    binds = _counted_binds(instance, monkeypatch)
    scoped = service_playbill_write(instance, request=request, caller=caller())
    scoped_binds = len(binds)
    binds.clear()
    monkeypatch.setattr(instance, "projection_scope", nullcontext)
    unscoped = service_playbill_write(instance, request=request, caller=caller())
    assert scoped.status == unscoped.status == "would_accept"

    def same_answer(outcome):  # type: ignore[no-untyped-def]
        # A dry run mints fresh Claim IDs each time; everything else is the answer.
        dumped = outcome.model_dump(mode="json")
        for change in dumped.get("changes") or ():
            change.pop("claim", None)
        return dumped

    assert same_answer(scoped) == same_answer(unscoped)
    assert scoped_binds < len(binds), (scoped_binds, len(binds))


def test_a_bind_only_scope_reuses_handles_and_keeps_nothing_else(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long consumer pass binds once, without a request carrier or derived values."""

    from cruxible_client.contracts import content_memo
    from cruxible_core.runtime import instance as runtime_instance

    head = instance.accepted_coordinate()
    binds = _counted_binds(instance, monkeypatch)
    with instance.projection_bind_scope():
        pool = runtime_instance._PROJECTION_POOL.get()
        assert pool is not None
        assert pool.values() is None  # no holder may keep a derived value
        assert content_memo._REQUEST_CARRIER.get() is None  # no parses carried
        for _ in range(3):
            with instance.bind_accepted_projection(head) as handle:
                assert not handle._closed
        with instance.projection_scope():  # a request inside it joins the pool
            assert runtime_instance._PROJECTION_POOL.get() is pool
    assert len(binds) == 1 and handle._closed

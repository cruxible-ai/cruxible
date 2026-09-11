"""Concrete local registry bounds, lifecycle and retained-root accounting."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cruxible_core.derived.derived_runtime import BoundedCache, BuildCapacityError, Registry


def test_cache_lru_byte_accounting_replacement_and_budget_changes() -> None:
    cache: BoundedCache[str] = BoundedCache(max_entries=3, max_bytes=8)
    assert cache.put("a", "a", weight=3)
    assert cache.put("b", "b", weight=3)
    assert cache.get("a") == "a"
    assert cache.put("c", "c", weight=4)
    assert cache.get("b") is None
    assert cache.status()["estimated_bytes"] == 7
    assert cache.put("a", "new", weight=2)
    assert cache.status()["estimated_bytes"] == 6
    cache.configure(max_entries=1, max_bytes=8)
    assert cache.values() == ("new",)
    assert cache.pop("missing") is None
    assert cache.pop("a") == "new"
    assert cache.status()["estimated_bytes"] == 0
    assert not cache.put("huge", "huge", weight=9)
    assert len(cache) == 0
    cache.configure(max_entries=0, max_bytes=8)
    assert not cache.put("empty", "empty", weight=0)
    with pytest.raises(ValueError):
        cache.put("bad", "bad", weight=-1)


def test_cache_clear_fences_inflight_publication_and_registry_owns_memos() -> None:
    owner = Registry()
    cache = owner.memo("prepared", max_entries=4, max_bytes=20)
    other_owner = Registry()
    assert other_owner.memo("prepared", max_entries=4, max_bytes=20) is not cache
    generation = cache.generation
    cache.put("a", 1, weight=4)
    owner.clear("prepared")
    assert len(cache) == 0
    assert not cache.put("old", 2, weight=4, expected_generation=generation)
    assert cache.put("new", 3, weight=4, expected_generation=cache.generation)
    assert owner.memo("prepared", max_entries=1, max_bytes=2) is cache
    assert len(cache) == 0
    assert owner.status()["memos"]["prepared"]["estimated_bytes"] == 0


def test_registration_clears_only_requested_adapter_outside_owner_lock() -> None:
    owner = Registry()
    cleared = []

    def clear() -> None:
        with ThreadPoolExecutor(max_workers=1) as workers:
            # This would deadlock if clear held the registry lock while calling us.
            workers.submit(owner.status).result(timeout=2)
        cleared.append("first")

    adapter = object()
    assert owner.register("first", "accepted", "1", adapter, clear=clear) is adapter
    owner.register("second", "operational", "2", object(), clear=lambda: cleared.append("second"))
    with pytest.raises(ValueError):
        owner.register("first", "candidate", "1", object())
    owner.clear("first")
    assert cleared == ["first"]
    owner.clear()
    assert cleared == ["first", "first", "second"]
    assert owner.status()["registrations"] == (
        {"name": "first", "namespace": "accepted", "version": "1"},
        {"name": "second", "namespace": "operational", "version": "2"},
    )


def test_build_bounds_pending_capacity_and_releases_after_failure() -> None:
    owner = Registry(max_builds=2, max_pending=1)
    started = [threading.Event(), threading.Event()]
    release = threading.Event()

    def work(key: str, event: threading.Event) -> None:
        with owner.build(key):
            event.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=3) as workers:
        first = workers.submit(work, "a", started[0])
        second = workers.submit(work, "b", started[1])
        try:
            assert all(event.wait(2) for event in started)
            third_started = threading.Event()
            third = workers.submit(work, "c", third_started)
            # Wait until the queued call has reached the admission boundary.
            with owner._condition:
                assert owner._condition.wait_for(lambda: len(owner._pending) == 1, timeout=2)
            assert owner.status()["active_builds"] == 2
            assert not third_started.is_set()
            with pytest.raises(BuildCapacityError):
                with owner.build("d"):
                    pytest.fail("exceeded pending budget")
        finally:
            release.set()
        first.result(timeout=2)
        second.result(timeout=2)
        third.result(timeout=2)
    with pytest.raises(RuntimeError, match="body failed"):
        with owner.build("failure"):
            raise RuntimeError("body failed")
    assert owner.status()["active_builds"] == 0
    assert owner.status()["pending_builds"] == 0
    assert owner.status()["failed_builds"] == 1
    assert owner.status()["refused_builds"] == 1
    with owner.build("recovery"):
        with ThreadPoolExecutor(max_workers=1) as workers:
            workers.submit(owner.status).result(timeout=2)


def test_same_key_serializes_while_other_keys_can_use_free_capacity() -> None:
    owner = Registry(max_builds=2, max_pending=1)
    first_started, second_started, unrelated_started = (threading.Event() for _ in range(3))
    release = threading.Event()

    def work(key: str, event: threading.Event) -> None:
        with owner.build(key):
            event.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=3) as workers:
        first = workers.submit(work, "same", first_started)
        try:
            assert first_started.wait(2)
            second = workers.submit(work, "same", second_started)
            with owner._condition:
                assert owner._condition.wait_for(lambda: len(owner._pending) == 1, timeout=2)
            unrelated = workers.submit(work, "other", unrelated_started)
            assert unrelated_started.wait(2)
            assert not second_started.is_set()
        finally:
            release.set()
        first.result(timeout=2)
        second.result(timeout=2)
        unrelated.result(timeout=2)
    assert second_started.is_set()
    assert owner.status()["active_builds"] == 0


def test_explicit_leases_survive_clear_and_close_accounts_once_without_enumeration() -> None:
    owner = Registry()

    class Root:
        def __iter__(self):
            raise AssertionError("lease enumerated root")

    root = Root()
    lease = owner.lease(root, estimated_bytes=12)
    owner.clear()
    assert lease.root is root
    assert owner.status()["active_leases"] == 1
    assert owner.status()["leased_estimated_bytes"] == 12
    with owner.lease(root, estimated_bytes=7) as borrowed:
        assert borrowed is root
        assert owner.status()["active_leases"] == 2
        assert owner.status()["leased_estimated_bytes"] == 19
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda _: lease.close(), range(8)))
    assert owner.status()["active_leases"] == 0
    assert owner.status()["leased_estimated_bytes"] == 0
    with pytest.raises(RuntimeError, match="closed"):
        _ = lease.root
    with pytest.raises(ValueError):
        owner.lease(root, estimated_bytes=-1)


def test_owner_clear_during_build_prevents_old_epoch_retention() -> None:
    from cruxible_core.derived.derived_state import DerivedState

    owner = DerivedState()
    started, release = threading.Event(), threading.Event()
    loads = 0

    def load() -> dict[str, bytes]:
        nonlocal loads
        loads += 1
        started.set()
        assert release.wait(3)
        return {"claims/a": b"body"}

    with ThreadPoolExecutor(max_workers=1) as workers:
        inflight = workers.submit(owner.accepted_tree, b"binding", load)
        try:
            assert started.wait(2)
            owner.clear()
        finally:
            release.set()
        returned = inflight.result(timeout=2)
    assert returned["claims/a"] == b"body"
    assert owner.status()["retained_roots"] == 0
    assert owner.status()["budget_fallbacks"] == 1
    rebuilt = owner.accepted_tree(b"binding", load)
    assert rebuilt is not returned
    assert loads == 2
    assert owner.status()["retained_roots"] == 1


def test_owner_root_byte_eviction_and_oversized_fallback_preserve_leased_roots() -> None:
    from cruxible_core.derived.derived_state import DerivedState

    owner = DerivedState(max_roots=2, max_input_bytes=5)
    first = owner.accepted_tree(b"first", lambda: {"a": b"123"})
    lease = owner.lease(first)
    second = owner.accepted_tree(b"second", lambda: {"a": b"456"})
    assert owner.status()["retained_roots"] == 1
    assert owner.status()["input_bytes_upper_bound"] == 4
    assert lease.root["a"] == b"123"
    assert second["a"] == b"456"
    assert owner.status()["runtime"]["leased_estimated_bytes"] == 4
    oversized = owner.accepted_tree(b"oversized", lambda: {"a": b"12345"})
    assert oversized["a"] == b"12345"
    assert owner.status()["retained_roots"] == 1
    assert owner.status()["budget_fallbacks"] == 1
    owner.clear()
    assert owner.status()["retained_roots"] == 0
    assert lease.root["a"] == b"123"
    lease.close()
    assert owner.status()["runtime"]["active_leases"] == 0


def test_owner_clear_invokes_registered_lifecycle_and_clears_its_memos() -> None:
    from cruxible_core.derived.derived_state import DerivedState, IndexDefinition

    owner = DerivedState()

    class Adapter:
        def __init__(self) -> None:
            self.clears = 0

        def clear(self) -> None:
            self.clears += 1

    adapter = Adapter()
    owner.register(IndexDefinition("derived-test", "operational", "1", "fresh-source"), adapter)
    memo = owner.memo("prepared", max_entries=2, max_bytes=10)
    memo.put("value", object(), weight=3)
    owner.clear()
    assert adapter.clears == 1
    assert len(memo) == 0
    assert len(owner.status()["definitions"]) == 1


def test_build_capacity_is_explicit_retryable_service_unavailability():
    from cruxible_core.server.errors import error_to_response

    status, body = error_to_response(
        BuildCapacityError("derived-state pending build budget exhausted")
    )
    assert status == 503
    assert body.error_code == "playbill.derived.capacity"
    assert body.context["retryable"] is True
    assert body.repair is None
    assert "budget exhausted" in body.message

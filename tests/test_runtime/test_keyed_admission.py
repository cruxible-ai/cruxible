"""Keyed admission: one holder per key across the loop and threads, no idle entries,
no finished loop kept alive."""

from __future__ import annotations

import asyncio
import gc
import threading
import weakref

import pytest

from cruxible_core.runtime.admission import KeyedAdmission


def test_idle_keys_leave_no_entries() -> None:
    admission = KeyedAdmission()

    async def run() -> None:
        for index in range(1000):
            async with admission.admit(f"inst_{index}"):
                assert admission.active_keys() == 1
        assert admission.active_keys() == 0

    asyncio.run(run())
    assert admission.active_keys() == 0
    assert admission.active_loops() == 0


def test_contenders_share_one_lock_and_leave_nothing_behind() -> None:
    admission = KeyedAdmission()
    order: list[str] = []

    async def run() -> None:
        first_in = asyncio.Event()
        release = asyncio.Event()

        async def first() -> None:
            async with admission.admit("inst_a"):
                order.append("first in")
                first_in.set()
                await release.wait()
                order.append("first out")

        async def second() -> None:
            await first_in.wait()
            async with admission.admit("inst_a"):
                order.append("second in")

        async def cancelled() -> None:
            await first_in.wait()
            async with admission.admit("inst_a"):
                order.append("cancelled in")  # pragma: no cover - never admitted

        tasks = [asyncio.create_task(job()) for job in (first, second, cancelled)]
        await first_in.wait()
        # Let both waiters reach the lock; still exactly one entry for the key.
        for _ in range(3):
            await asyncio.sleep(0)
        assert admission.active_keys() == 1
        tasks[2].cancel()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[2], asyncio.CancelledError)

    asyncio.run(run())
    assert order == ["first in", "first out", "second in"]
    assert admission.active_keys() == 0
    assert admission.active_loops() == 0


def test_a_contended_loop_is_collected() -> None:
    """A contended asyncio.Lock binds its loop; the entry must not outlive it."""

    admission = KeyedAdmission()
    loop = asyncio.new_event_loop()

    async def contend() -> None:
        holder_in = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with admission.admit("inst_a"):
                holder_in.set()
                await release.wait()

        async def waiter() -> None:
            await holder_in.wait()
            async with admission.admit("inst_a"):
                pass

        tasks = [asyncio.create_task(holder()), asyncio.create_task(waiter())]
        await holder_in.wait()
        for _ in range(3):
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)

    loop.run_until_complete(contend())
    loop.close()
    collected = weakref.ref(loop)
    del loop
    gc.collect()

    assert collected() is None
    assert admission.active_loops() == 0


# -- threads and the loop share one exclusion -----------------------------------------

_BOUND = 30.0


def _run_loop_in_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


def _stop(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    thread.join(_BOUND)
    loop.close()


def test_a_thread_holder_and_a_route_on_one_key_are_serialized() -> None:
    admission = KeyedAdmission()
    order: list[str] = []
    loop, runner = _run_loop_in_thread()
    held = threading.Event()
    release = threading.Event()

    def worker() -> None:
        with admission.hold("inst_a"):
            order.append("thread in")
            held.set()
            assert release.wait(_BOUND)
            order.append("thread out")

    async def route() -> None:
        async with admission.admit("inst_a"):
            order.append("route in")

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert held.wait(_BOUND)
        routed = asyncio.run_coroutine_threadsafe(route(), loop)
        # The route is queued behind the thread and the loop is still free.
        assert asyncio.run_coroutine_threadsafe(asyncio.sleep(0, "free"), loop).result(_BOUND)
        assert order == ["thread in"]
        assert admission.active_keys() == 1 and admission.active_loops() == 1
    finally:
        release.set()
        thread.join(_BOUND)
    routed.result(_BOUND)
    _stop(loop, runner)
    assert order == ["thread in", "thread out", "route in"]
    assert admission.active_keys() == 0 and admission.active_loops() == 0


def test_a_route_holder_makes_a_thread_wait_and_hands_it_the_key() -> None:
    admission = KeyedAdmission()
    order: list[str] = []
    loop, runner = _run_loop_in_thread()
    route_in = threading.Event()
    release = threading.Event()

    async def route() -> None:
        async with admission.admit("inst_a"):
            order.append("route in")
            route_in.set()
            while not release.is_set():
                await asyncio.sleep(0.001)
            order.append("route out")

    def worker() -> None:
        with admission.hold("inst_a"):
            order.append("thread in")

    routed = asyncio.run_coroutine_threadsafe(route(), loop)
    assert route_in.wait(_BOUND)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(0.05)
    assert thread.is_alive() and order == ["route in"]
    release.set()
    routed.result(_BOUND)
    thread.join(_BOUND)
    _stop(loop, runner)
    assert order == ["route in", "route out", "thread in"]
    assert admission.active_keys() == 0


def test_different_keys_proceed_across_threads_and_the_loop() -> None:
    admission = KeyedAdmission()
    loop, runner = _run_loop_in_thread()
    held = threading.Event()
    release = threading.Event()

    def worker() -> None:
        with admission.hold("inst_a"):
            held.set()
            assert release.wait(_BOUND)

    async def route(key: str) -> str:
        async with admission.admit(key):
            return key

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert held.wait(_BOUND)
        assert asyncio.run_coroutine_threadsafe(route("inst_b"), loop).result(_BOUND) == "inst_b"
        with admission.hold("inst_c"):
            assert admission.active_keys() == 2
    finally:
        release.set()
        thread.join(_BOUND)
    _stop(loop, runner)
    assert admission.active_keys() == 0


def test_a_cancelled_route_behind_a_thread_leaves_the_queue() -> None:
    admission = KeyedAdmission()
    order: list[str] = []
    loop, runner = _run_loop_in_thread()
    held = threading.Event()
    release = threading.Event()

    def worker() -> None:
        with admission.hold("inst_a"):
            held.set()
            assert release.wait(_BOUND)

    async def route(name: str) -> None:
        async with admission.admit("inst_a"):
            order.append(name)

    thread = threading.Thread(target=worker)
    thread.start()
    assert held.wait(_BOUND)
    cancelled = asyncio.run_coroutine_threadsafe(route("cancelled"), loop)
    kept = asyncio.run_coroutine_threadsafe(route("kept"), loop)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(_BOUND)
    cancelled.cancel()
    release.set()
    thread.join(_BOUND)
    kept.result(_BOUND)
    _stop(loop, runner)
    assert order == ["kept"]
    assert admission.active_keys() == 0 and admission.active_loops() == 0


def test_a_route_cancelled_as_the_key_reaches_it_passes_the_key_on() -> None:
    admission = KeyedAdmission()
    order: list[str] = []

    async def run() -> None:
        release = asyncio.Event()

        async def holder() -> None:
            async with admission.admit("inst_a"):
                await release.wait()

        async def waiter(name: str) -> None:
            async with admission.admit("inst_a"):
                order.append(name)

        first = asyncio.create_task(holder())
        await asyncio.sleep(0)
        doomed = asyncio.create_task(waiter("doomed"))
        survivor = asyncio.create_task(waiter("survivor"))
        await asyncio.sleep(0)
        release.set()
        await first  # the key is handed to `doomed` and its wake-up is scheduled
        doomed.cancel()  # cancelled before it ever runs again
        results = await asyncio.gather(doomed, survivor, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)

    asyncio.run(run())
    assert order == ["survivor"]
    assert admission.active_keys() == 0 and admission.active_loops() == 0


def test_hold_refuses_to_block_a_running_loop() -> None:
    admission = KeyedAdmission()

    async def run() -> None:
        with pytest.raises(RuntimeError, match="event loop"):
            with admission.hold("inst_a"):
                pass  # pragma: no cover - refused before entry

    asyncio.run(run())
    assert admission.active_keys() == 0


def test_the_floor_admission_is_one_shared_object() -> None:
    from cruxible_core.runtime import admission as module
    from cruxible_core.server.routes import playbill as routes

    assert isinstance(module.FLOOR_ADMISSION, KeyedAdmission)
    assert routes.FLOOR_ADMISSION is module.FLOOR_ADMISSION

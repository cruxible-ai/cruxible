"""Keyed admission keeps no idle entries and never keeps a finished loop alive."""

from __future__ import annotations

import asyncio
import gc
import weakref

from cruxible_core.server.admission import KeyedAdmission


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

"""A floor export runs off the event loop, and its log line says how long it took.

`export_floor` was `async def` around a fully synchronous export, so a 3-15 s
export on real state stalled every other request on the daemon. The export here
is replaced by one that blocks until the test releases it. While it is blocked,
cheap requests must still be answered: if the export held the loop, they
could only finish after the export gave up waiting, and the export would
report that it timed out instead of being released. No wall-clock assertion is
made; the timeout only bounds the failure path.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio.to_thread
import pytest
import structlog
from fastapi.testclient import TestClient

from cruxible_client import contracts
from cruxible_client.contracts.floor import PlaybillFloorDeltaV1
from cruxible_core.errors import FloorAdmissionMisuse
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.admission import FLOOR_ADMISSION, HTTP_REQUEST_CONTEXT
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry
from cruxible_core.server.request_context import HTTPRequestContextMiddleware
from cruxible_core.server.routes import playbill as playbill_routes
from tests.support.floor_exports import floor_v5_delta

_FAILURE_BOUND_SECONDS = 30.0
_DIGEST = "sha256:" + "0" * 64
_COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="0" * 40,
    semantic_root=_DIGEST,
    generation_root=_DIGEST,
    compiler_digest=_DIGEST,
)


@pytest.fixture
def request_log_buffer() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.JSONRenderer()],
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )
    yield buffer
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    # One portal for the client's lifetime, so requests from two threads share
    # one event loop, as they do in the daemon.
    with TestClient(create_app()) as test_client:
        yield test_client


def _runtime_request_events(buffer: io.StringIO) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in buffer.getvalue().splitlines():
        if line.startswith("{"):
            payload = json.loads(line)
            if payload.get("event") == "runtime_request":
                events.append(payload)
    return events


def test_a_slow_floor_export_does_not_block_a_concurrent_cheap_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    request_log_buffer: io.StringIO,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    released_by_test: list[bool] = []

    def blocking_export(instance_id: str, **_: object) -> contracts.PlaybillFloorExport:
        entered.set()
        released_by_test.append(release.wait(_FAILURE_BOUND_SECONDS))
        coordinate = contracts.PlaybillAcceptedCoordinate(
            git_oid="0" * 40,
            semantic_root=_DIGEST,
            generation_root=_DIGEST,
            compiler_digest=_DIGEST,
        )
        return contracts.PlaybillFloorExport(
            tag="playbill-floor-export-v5", coordinate=coordinate, manifest={}, files=[]
        )

    monkeypatch.setattr(playbill_routes, "resolve_server_instance_id", lambda value: value)
    monkeypatch.setattr(playbill_api, "playbill_export_floor", blocking_export)

    export_status: list[int] = []

    def export() -> None:
        response = client.post("/api/v1/inst_floor_slow/playbill/floor/export", json={})
        export_status.append(response.status_code)

    exporter = threading.Thread(target=export)
    exporter.start()
    try:
        assert entered.wait(_FAILURE_BOUND_SECONDS), "the export never reached its handler"
        # Unlogged and answered on the loop itself.
        health = client.get("/health")
        # Logged, and answered from the threadpool beside the export.
        info = client.get("/api/v1/server/info")
    finally:
        release.set()
        exporter.join(_FAILURE_BOUND_SECONDS)

    assert health.status_code == 200
    assert info.status_code == 200, info.text
    assert released_by_test == [True], (
        "the cheap request was answered only after the export stopped waiting, "
        "so the export was holding the event loop"
    )
    assert export_status == [200]

    events = {event["route"]: event for event in _runtime_request_events(request_log_buffer)}
    floor = events["/api/v1/{instance_id}/playbill/floor/export"]
    cheap = events["/api/v1/server/info"]
    assert isinstance(floor["duration_ms"], float)
    assert isinstance(cheap["duration_ms"], float)
    # The cheap request arrived after the export did and was logged before the
    # export was released, so the export's measured span contains it.
    assert floor["duration_ms"] >= cheap["duration_ms"]


def test_queued_exports_hold_no_worker_thread_while_they_wait(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With two worker threads, one running export and three queued behind it
    leave a worker free: cheap reads and another instance's export still run.

    Queued exports used to wait on a lock inside a sync handler, each holding
    a worker thread, so a slow export plus one waiter exhausted the pool.
    """

    async def two_workers() -> None:
        anyio.to_thread.current_default_thread_limiter().total_tokens = 2

    assert client.portal is not None
    client.portal.call(two_workers)

    first_entered = threading.Event()
    release = threading.Event()
    other_instance_entered = threading.Event()
    released_by_test: list[bool] = []
    resolved: list[str] = []
    resolved_lock = threading.Lock()
    running: dict[str, int] = {}
    most_running: dict[str, int] = {}
    running_lock = threading.Lock()

    def resolve(instance_id: str) -> str:
        with resolved_lock:
            resolved.append(instance_id)
        return instance_id

    def blocking_export(instance_id: str, **_: object) -> contracts.PlaybillFloorExport:
        with running_lock:
            running[instance_id] = running.get(instance_id, 0) + 1
            most_running[instance_id] = max(most_running.get(instance_id, 0), running[instance_id])
            first = instance_id == "inst_a" and not first_entered.is_set()
        try:
            if first:
                first_entered.set()
                released_by_test.append(release.wait(_FAILURE_BOUND_SECONDS))
            if instance_id == "inst_b":
                other_instance_entered.set()
            return contracts.PlaybillFloorExport(
                tag="playbill-floor-export-v5",
                coordinate=contracts.PlaybillAcceptedCoordinate(
                    git_oid="0" * 40,
                    semantic_root=_DIGEST,
                    generation_root=_DIGEST,
                    compiler_digest=_DIGEST,
                ),
                manifest={},
                files=[],
            )
        finally:
            with running_lock:
                running[instance_id] -= 1

    monkeypatch.setattr(playbill_routes, "resolve_server_instance_id", resolve)
    monkeypatch.setattr(playbill_api, "playbill_export_floor", blocking_export)

    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def export(instance_id: str) -> None:
        response = client.post(f"/api/v1/{instance_id}/playbill/floor/export", json={})
        with statuses_lock:
            statuses.append(response.status_code)

    exporters = [threading.Thread(target=export, args=("inst_a",))]
    exporters[0].start()
    try:
        assert first_entered.wait(_FAILURE_BOUND_SECONDS), "the first export never started"
        for _ in range(3):
            exporters.append(threading.Thread(target=export, args=("inst_a",)))
            exporters[-1].start()
        # Every queued export has passed its threadpool hop and is now waiting
        # for admission. Waiting in a worker thread instead, the second would
        # take the last worker and the others could never get this far.
        for _ in range(int(_FAILURE_BOUND_SECONDS * 100)):
            if len(resolved) >= 4:
                break
            time.sleep(0.01)
        info = client.get("/api/v1/server/info")
        exporters.append(threading.Thread(target=export, args=("inst_b",)))
        exporters[-1].start()
        assert other_instance_entered.wait(_FAILURE_BOUND_SECONDS), (
            "another instance's export waited behind this instance's queue"
        )
    finally:
        release.set()
        for thread in exporters:
            thread.join(_FAILURE_BOUND_SECONDS)

    assert info.status_code == 200, info.text
    assert released_by_test == [True], (
        "server/info was answered only after the running export stopped waiting, "
        "so queued exports had exhausted the worker threads"
    )
    assert sorted(statuses) == [200] * 5
    # Admission still runs one export per instance at a time.
    assert most_running == {"inst_a": 1, "inst_b": 1}


def test_a_delta_waits_behind_an_export_of_its_instance_only(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An export and a delta of one instance share one admission, so the delta
    renders only after the export finishes; another instance's delta does not
    wait for either."""

    export_entered = threading.Event()
    release = threading.Event()
    other_delta_entered = threading.Event()
    released_by_test: list[bool] = []
    order: list[str] = []
    order_lock = threading.Lock()
    resolved: list[str] = []
    resolved_lock = threading.Lock()

    def resolve(instance_id: str) -> str:
        with resolved_lock:
            resolved.append(instance_id)
        return instance_id

    def blocking_export(instance_id: str, **_: object) -> contracts.PlaybillFloorExport:
        with order_lock:
            order.append(f"export {instance_id} in")
        export_entered.set()
        released_by_test.append(release.wait(_FAILURE_BOUND_SECONDS))
        with order_lock:
            order.append(f"export {instance_id} out")
        return contracts.PlaybillFloorExport(
            tag="playbill-floor-export-v5", coordinate=_COORDINATE, manifest={}, files=[]
        )

    def delta(instance_id: str, **_: object) -> PlaybillFloorDeltaV1:
        with order_lock:
            order.append(f"delta {instance_id}")
        if instance_id == "inst_b":
            other_delta_entered.set()
        return floor_v5_delta({}, coordinate=_COORDINATE, generation=1)

    monkeypatch.setattr(playbill_routes, "resolve_server_instance_id", resolve)
    monkeypatch.setattr(playbill_api, "playbill_export_floor", blocking_export)
    monkeypatch.setattr(playbill_api, "playbill_floor_delta", delta)

    statuses: dict[str, int] = {}

    def post(name: str, instance_id: str, route: str) -> None:
        response = client.post(f"/api/v1/{instance_id}/playbill/floor/{route}", json={})
        statuses[name] = response.status_code

    threads = [threading.Thread(target=post, args=("export a", "inst_a", "export"))]
    threads[0].start()
    try:
        assert export_entered.wait(_FAILURE_BOUND_SECONDS), "the export never started"
        threads.append(threading.Thread(target=post, args=("delta a", "inst_a", "delta")))
        threads[-1].start()
        # The delta has resolved its instance and is now waiting for admission.
        for _ in range(int(_FAILURE_BOUND_SECONDS * 100)):
            if resolved.count("inst_a") >= 2:
                break
            time.sleep(0.01)
        threads.append(threading.Thread(target=post, args=("delta b", "inst_b", "delta")))
        threads[-1].start()
        assert other_delta_entered.wait(_FAILURE_BOUND_SECONDS), (
            "another instance's delta waited behind this instance's export"
        )
        with order_lock:
            assert "delta inst_a" not in order, "a delta rendered beside its instance's export"
    finally:
        release.set()
        for thread in threads:
            thread.join(_FAILURE_BOUND_SECONDS)

    assert released_by_test == [True]
    assert statuses == {"export a": 200, "delta a": 200, "delta b": 200}
    assert order.index("export inst_a out") < order.index("delta inst_a")
    assert order.index("delta inst_b") < order.index("export inst_a out")


def test_an_in_process_floor_holder_on_a_thread_excludes_that_instances_routes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer thread holding `FLOOR_ADMISSION` (in-process floor delivery)
    makes a delta route of its instance wait, without holding the event loop or
    a worker; another instance's delta and cheap requests still run."""

    held = threading.Event()
    release = threading.Event()
    other_delta_entered = threading.Event()
    order: list[str] = []
    order_lock = threading.Lock()
    resolved: list[str] = []
    resolved_lock = threading.Lock()

    def resolve(instance_id: str) -> str:
        with resolved_lock:
            resolved.append(instance_id)
        return instance_id

    def delta(instance_id: str, **_: object) -> PlaybillFloorDeltaV1:
        with order_lock:
            order.append(f"delta {instance_id}")
        if instance_id == "inst_b":
            other_delta_entered.set()
        return floor_v5_delta({}, coordinate=_COORDINATE, generation=1)

    monkeypatch.setattr(playbill_routes, "resolve_server_instance_id", resolve)
    monkeypatch.setattr(playbill_api, "playbill_floor_delta", delta)

    def consumer() -> None:
        with FLOOR_ADMISSION.hold("inst_a"):
            with order_lock:
                order.append("consumer in")
            held.set()
            release.wait(_FAILURE_BOUND_SECONDS)
            with order_lock:
                order.append("consumer out")

    statuses: dict[str, int] = {}

    def post(name: str, instance_id: str) -> None:
        response = client.post(f"/api/v1/{instance_id}/playbill/floor/delta", json={})
        statuses[name] = response.status_code

    threads = [threading.Thread(target=consumer)]
    threads[0].start()
    try:
        assert held.wait(_FAILURE_BOUND_SECONDS), "the consumer never took the admission"
        threads.append(threading.Thread(target=post, args=("delta a", "inst_a")))
        threads[-1].start()
        for _ in range(int(_FAILURE_BOUND_SECONDS * 100)):
            if "inst_a" in resolved:
                break
            time.sleep(0.01)
        health = client.get("/health")
        threads.append(threading.Thread(target=post, args=("delta b", "inst_b")))
        threads[-1].start()
        assert other_delta_entered.wait(_FAILURE_BOUND_SECONDS), (
            "another instance's delta waited behind this instance's consumer"
        )
        with order_lock:
            assert "delta inst_a" not in order, "a delta rendered beside the consumer's refresh"
    finally:
        release.set()
        for thread in threads:
            thread.join(_FAILURE_BOUND_SECONDS)

    assert health.status_code == 200
    assert statuses == {"delta a": 200, "delta b": 200}
    assert order.index("consumer out") < order.index("delta inst_a")
    assert FLOOR_ADMISSION.active_keys() == 0


def test_a_route_cancelled_mid_render_keeps_its_instance_until_the_render_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling an admitted export while its worker renders must not let a queued
    delta route or consumer thread of that instance in beside the running render."""

    import asyncio

    from cruxible_core.server.playbill_request_models import (
        PlaybillFloorDeltaRequest,
        PlaybillFloorExportRequest,
    )

    export_entered = threading.Event()
    release = threading.Event()
    running = 0
    overlapped: list[str] = []
    order: list[str] = []
    lock = threading.Lock()

    def enter(name: str) -> None:
        nonlocal running
        with lock:
            running += 1
            if running > 1:
                overlapped.append(name)
            order.append(f"{name} in")

    def leave(name: str) -> None:
        nonlocal running
        with lock:
            running -= 1
            order.append(f"{name} out")

    def blocking_export(instance_id: str, **_: object) -> contracts.PlaybillFloorExport:
        enter("export")
        export_entered.set()
        release.wait(_FAILURE_BOUND_SECONDS)
        leave("export")
        return contracts.PlaybillFloorExport(
            tag="playbill-floor-export-v5", coordinate=_COORDINATE, manifest={}, files=[]
        )

    def delta(instance_id: str, **_: object) -> PlaybillFloorDeltaV1:
        enter("delta")
        leave("delta")
        return floor_v5_delta({}, coordinate=_COORDINATE, generation=1)

    monkeypatch.setattr(playbill_routes, "resolve_server_instance_id", lambda value: value)
    monkeypatch.setattr(playbill_api, "playbill_export_floor", blocking_export)
    monkeypatch.setattr(playbill_api, "playbill_floor_delta", delta)

    def consumer() -> None:
        with FLOOR_ADMISSION.hold("inst_cancel"):
            enter("consumer")
            leave("consumer")

    async def scenario() -> None:
        export = asyncio.create_task(
            playbill_routes.export_floor("inst_cancel", PlaybillFloorExportRequest())
        )
        while not export_entered.is_set():
            await asyncio.sleep(0.005)
        queued = asyncio.create_task(
            playbill_routes.floor_delta("inst_cancel", PlaybillFloorDeltaRequest())
        )
        thread = threading.Thread(target=consumer)
        thread.start()
        await asyncio.sleep(0.05)
        export.cancel()
        # Give a wrongly released key every chance to be taken.
        await asyncio.sleep(0.1)
        with lock:
            assert order == ["export in"], order
        release.set()
        await queued
        await asyncio.to_thread(thread.join, _FAILURE_BOUND_SECONDS)
        try:
            await export
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert overlapped == []
    assert order[:2] == ["export in", "export out"]
    assert sorted(order[2:]) == ["consumer in", "consumer out", "delta in", "delta out"]
    assert FLOOR_ADMISSION.active_keys() == 0


def test_a_route_cancelled_before_its_worker_starts_releases_the_key() -> None:
    """The ticket's other side: a route that leaves before any worker took the
    key releases it, and a late worker never runs the call."""

    import asyncio

    from cruxible_core.runtime.admission import KeyedAdmission

    admission = KeyedAdmission()
    ran: list[str] = []

    async def scenario() -> Any:
        async with admission.admit("inst_x") as ticket:
            pass
        assert admission.active_keys() == 0
        return ticket

    ticket = asyncio.run(scenario())
    with pytest.raises(FloorAdmissionMisuse, match="left before its call started"):
        ticket.run(lambda: ran.append("late"))
    assert ran == [] and admission.active_keys() == 0


def test_attachment_and_delivery_requests_wait_without_worker_tokens(monkeypatch):
    """Pause an admitted export before offload, then queue two pool-sized toggles.

    A synchronous route reaching hold would occupy both workers, preventing the
    export from starting. Delta, deliver-now and detach share that same queue.
    """
    import asyncio
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from cruxible_core.consumers import floor
    from cruxible_core.runtime import host_api
    from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND
    from cruxible_core.server.routes import hosted_instances

    record = SimpleNamespace(
        backend=GOVERNED_DAEMON_BACKEND, workspace_root="/workspace", floor_delivery=True
    )

    def toggle(instance_id, enabled):
        record.floor_delivery = enabled

    def detach(instance_id, **kwargs):
        return SimpleNamespace(workspace_root=None)

    registry = SimpleNamespace(
        get=lambda _: record, set_floor_delivery=toggle, detach_governed_workspace=detach
    )
    monkeypatch.setattr(host_api, "get_registry", lambda: registry)
    monkeypatch.setattr(host_api, "check_permission", lambda *a, **kw: None)
    monkeypatch.setattr(host_api, "_refuse_detach_with_registered_blocks", lambda _: None)
    monkeypatch.setattr(
        host_api, "get_playbill_manager", lambda: SimpleNamespace(get=lambda _: None)
    )
    monkeypatch.setattr(
        hosted_instances, "resolve_server_settings", lambda: SimpleNamespace(server_socket="socket")
    )
    for routes in (playbill_routes, hosted_instances):
        monkeypatch.setattr(routes, "resolve_server_instance_id", lambda value: value)
    delta = floor_v5_delta({}, coordinate=_COORDINATE, generation=1)
    delivered = contracts.PlaybillFloorDeliveryResultV1(
        delta=delta,
        written=contracts.PlaybillWorkspaceFloorWriteResult(
            path=".playbill/floor",
            destination="/workspace/.playbill/floor",
            floor_digest=_DIGEST,
            coordinate=_COORDINATE,
            file_count=1,
        ),
    )
    monkeypatch.setattr(floor, "_refresh_floor_admitted", lambda *a, **kw: delivered)
    monkeypatch.setattr(playbill_api, "playbill_floor_delta", lambda *a, **kw: delta)
    monkeypatch.setattr(
        playbill_api,
        "playbill_export_floor",
        lambda *a, **kw: contracts.PlaybillFloorExport(
            tag="playbill-floor-export-v5", coordinate=_COORDINATE, manifest={}, files=[]
        ),
    )
    app = FastAPI()
    app.add_middleware(HTTPRequestContextMiddleware)
    app.include_router(playbill_routes.router)
    app.include_router(hosted_instances.router)
    offload = playbill_routes.run_in_threadpool
    enter = FLOOR_ADMISSION._enter

    async def scenario():
        admitted = asyncio.Event()
        release = asyncio.Event()
        toggles_queued = asyncio.Event()
        arrivals = 0
        loop = asyncio.get_running_loop()

        def observe(*args, **kwargs):
            nonlocal arrivals
            waiter = enter(*args, **kwargs)
            arrivals += 1
            if arrivals >= 3:
                loop.call_soon_threadsafe(toggles_queued.set)
            return waiter

        async def pause(call, *args, **kwargs):
            if getattr(call, "__name__", None) == "run":
                admitted.set()
                await release.wait()
            return await offload(call, *args, **kwargs)

        monkeypatch.setattr(FLOOR_ADMISSION, "_enter", observe)
        monkeypatch.setattr(playbill_routes, "run_in_threadpool", pause)
        limiter = anyio.to_thread.current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 2
        tasks = []
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=None), base_url="http://test"
            ) as http:
                prefix = "/api/v1/inst_pool/playbill"
                tasks.append(asyncio.create_task(http.post(prefix + "/floor/export", json={})))
                await asyncio.wait_for(admitted.wait(), 5)
                for _ in range(2):
                    tasks.append(
                        asyncio.create_task(
                            http.post(prefix + "/workspace/floor-delivery", json={"enabled": True})
                        )
                    )
                await asyncio.wait_for(toggles_queued.wait(), 5)
                assert await asyncio.wait_for(offload(lambda: "free worker"), 2) == "free worker"
                for suffix in ("/floor/delta", "/floor/deliver-now", "/workspace-detach"):
                    tasks.append(asyncio.create_task(http.post(prefix + suffix, json={})))
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 10)
                assert [r.status_code for r in responses] == [200] * 6, [r.text for r in responses]
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            limiter.total_tokens = previous

    asyncio.run(scenario())
    assert FLOOR_ADMISSION.active_keys() == 0


@pytest.mark.parametrize("form", ["direct", "helper", "lambda_alias"])
def test_http_hold_fails_before_admission_for_every_call_form(client, monkeypatch, form):
    """Worker context survives indirection; a forbidden hold never starts a wait."""
    error = "floor admission from an HTTP request must use async admit with the ticket"

    def blocking():
        with FLOOR_ADMISSION.hold("inst_http_hold"):
            return {"entered": True}

    def direct():
        with FLOOR_ADMISSION.hold("inst_http_hold"):
            return {"entered": True}

    def helper():
        return blocking()

    def lambda_alias():
        invoke = lambda: blocking()  # noqa: E731 - regression for the review's lambda alias
        return invoke()

    def must_not_enter(*args, **kwargs):
        pytest.fail("an HTTP hold reached admission bookkeeping instead of failing fast")

    monkeypatch.setattr(FLOOR_ADMISSION, "_enter", must_not_enter)
    client.app.get("/test-http-hold")(
        {"direct": direct, "helper": helper, "lambda_alias": lambda_alias}[form]
    )
    response = client.get("/test-http-hold")
    assert response.status_code == 500
    body = response.json()
    assert body["error_type"] == "FloorAdmissionMisuse"
    assert body["error_code"] == "internal.floor_admission_misuse"
    assert error in body["message"]
    assert body["repair"] is None
    assert "Traceback" not in response.text and "RuntimeError" not in response.text
    assert FLOOR_ADMISSION.active_keys() == 0
    assert not HTTP_REQUEST_CONTEXT.get()


def test_http_ticket_workers_keep_request_context_and_consumers_can_hold(client):

    from starlette.concurrency import run_in_threadpool

    worker_entered = threading.Event()
    consumer_finished = threading.Event()
    observed = []

    def consumer():
        if not worker_entered.wait(_FAILURE_BOUND_SECONDS):
            return
        with FLOOR_ADMISSION.hold("inst_consumer"):
            observed.append((HTTP_REQUEST_CONTEXT.get(), FLOOR_ADMISSION.active_keys()))
        consumer_finished.set()

    def worker():
        assert HTTP_REQUEST_CONTEXT.get()
        # A ticket is already admitted, but even its body must not acquire
        # another key through hold. The check belongs to hold, not ticket.run.
        with pytest.raises(FloorAdmissionMisuse, match="HTTP request must use async admit"):
            with FLOOR_ADMISSION.hold("inst_illegal_nested_hold"):
                pass
        worker_entered.set()
        assert consumer_finished.wait(_FAILURE_BOUND_SECONDS)
        return {"admitted": True}

    async def route():
        async with FLOOR_ADMISSION.admit("inst_http_ticket") as ticket:
            return await run_in_threadpool(ticket.run, worker)

    client.app.get("/test-http-ticket")(route)
    # Created outside the request, as the daemon's consumer runner is.
    thread = threading.Thread(target=consumer)
    thread.start()
    try:
        response = client.get("/test-http-ticket")
    finally:
        worker_entered.set()
        thread.join(_FAILURE_BOUND_SECONDS)
    assert not thread.is_alive()
    assert response.status_code == 200 and response.json() == {"admitted": True}
    assert observed == [(False, 2)]
    assert FLOOR_ADMISSION.active_keys() == 0
    assert not HTTP_REQUEST_CONTEXT.get()


@pytest.mark.parametrize("ending", ["success", "exception", "cancellation"])
def test_http_context_lasts_through_response_and_resets_on_every_exit(ending):
    import asyncio

    seen = []

    async def send(message):
        seen.append((message["type"], HTTP_REQUEST_CONTEXT.get()))

    async def receive():
        return {"type": "http.disconnect"}

    async def downstream(scope, receive, send):
        assert HTTP_REQUEST_CONTEXT.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(0)
        await send({"type": "http.response.body", "body": b"ok"})
        if ending == "exception":
            raise RuntimeError("request failed")
        if ending == "cancellation":
            raise asyncio.CancelledError

    async def scenario():
        assert not HTTP_REQUEST_CONTEXT.get()
        middleware = HTTPRequestContextMiddleware(downstream)
        if ending == "success":
            await middleware({"type": "http"}, receive, send)
        else:
            error = RuntimeError if ending == "exception" else asyncio.CancelledError
            with pytest.raises(error):
                await middleware({"type": "http"}, receive, send)
        assert not HTTP_REQUEST_CONTEXT.get()

    asyncio.run(scenario())
    assert seen == [("http.response.start", True), ("http.response.body", True)]


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_have_no_http_admission_context(scope_type):
    import asyncio

    seen = []

    async def downstream(scope, receive, send):
        seen.append(HTTP_REQUEST_CONTEXT.get())

    asyncio.run(HTTPRequestContextMiddleware(downstream)({"type": scope_type}, None, None))
    assert seen == [False]


def test_framework_error_handlers_keep_http_context(client, monkeypatch):
    from fastapi.responses import JSONResponse
    from starlette.middleware.errors import ServerErrorMiddleware

    observed = []

    def fail():
        raise RuntimeError("route failed")

    def handler(request, exc):
        observed.append(HTTP_REQUEST_CONTEXT.get())
        with pytest.raises(FloorAdmissionMisuse, match="HTTP request must use async admit"):
            with FLOOR_ADMISSION.hold("inst_error_handler"):
                pass
        return JSONResponse(status_code=500, content={"error": "failed"})

    middleware = client.app.middleware_stack
    while not isinstance(middleware, ServerErrorMiddleware):
        middleware = middleware.app
    monkeypatch.setattr(middleware, "handler", handler)
    client.app.get("/test-error-context")(fail)
    with pytest.raises(RuntimeError, match="route failed"):
        client.get("/test-error-context")
    assert observed == [True]
    assert FLOOR_ADMISSION.active_keys() == 0


def test_request_launched_detached_work_uses_fresh_contexts(client, monkeypatch):
    """Emulate inheriting threads even on Python builds that start them empty."""
    from contextvars import ContextVar, copy_context
    from types import SimpleNamespace

    from cruxible_core.consumers.protocol import ConsumerWork
    from cruxible_core.consumers.runner import ConsumerRunner
    from cruxible_core.ledger.checkpoints import QuietCheckpointWriter

    original_thread = threading.Thread

    class InheritingThread(original_thread):
        def __init__(self, *, target, args=(), **kwargs):
            super().__init__(target=copy_context().run, args=(target, *args), **kwargs)

    monkeypatch.setattr(threading, "Thread", InheritingThread)
    owner: ContextVar[str | None] = ContextVar("detached_test_owner", default=None)
    release = threading.Event()
    loop_entered = threading.Event()
    work_finished = threading.Event()
    checkpoint_finished = threading.Event()
    observed = {}
    held = []

    def run_work(*args, **kwargs):
        try:
            assert release.wait(5)
            observed["pool"] = (HTTP_REQUEST_CONTEXT.get(), owner.get())
            with FLOOR_ADMISSION.hold("inst_detached_pool"):
                held.append("pool")
        finally:
            work_finished.set()

    kind = SimpleNamespace(name="detached-test", workers=1, run=run_work)
    runner = ConsumerRunner(SimpleNamespace(), kinds=(kind,))

    def loop():
        observed["runner"] = (HTTP_REQUEST_CONTEXT.get(), owner.get())
        loop_entered.set()
        runner.stop_event.wait(5)

    monkeypatch.setattr(runner, "_run", loop)
    writer = QuietCheckpointWriter(quiet_seconds=0, name="detached-checkpoint-test")

    def checkpoint():
        try:
            assert release.wait(5)
            observed["checkpoint"] = (HTTP_REQUEST_CONTEXT.get(), owner.get())
            with FLOOR_ADMISSION.hold("inst_detached_checkpoint"):
                held.append("checkpoint")
        finally:
            checkpoint_finished.set()

    def launch():
        assert HTTP_REQUEST_CONTEXT.get()
        token = owner.set("request-owner")
        try:
            runner.start()
            assert loop_entered.wait(5)
            # Submit directly from the request, not just from the fresh loop.
            runner._schedule("inst_detached", kind, ConsumerWork(key="unit", item=None))
            writer.defer(checkpoint)
            assert HTTP_REQUEST_CONTEXT.get() and owner.get() == "request-owner"
            return {"launched": True}
        finally:
            owner.reset(token)

    client.app.get("/test-detached-launch")(launch)
    try:
        response = client.get("/test-detached-launch")
        assert response.status_code == 200
        release.set()
        assert work_finished.wait(5) and checkpoint_finished.wait(5)
    finally:
        release.set()
        runner.close()
        writer.flush(timeout=5)
    assert observed == {"runner": (False, None), "pool": (False, None), "checkpoint": (False, None)}
    assert sorted(held) == ["checkpoint", "pool"]
    assert FLOOR_ADMISSION.active_keys() == 0

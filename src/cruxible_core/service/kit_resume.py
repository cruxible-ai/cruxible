"""A ``kit add`` that stopped at awaiting_providers resumes once its installs activate.

The commit that stops records the request (content-addressed in the body store)
and one wait per bundled Provider whose install awaits approval, in the trigger
journal beside the fires: retained operational state, never rebuilt. The
activation that lands the last of those Providers resumes it, after that
activation has committed and isolated from it: the same ``kit add`` runs again
at the head it lands on, as the principal who ran it, so every decision is
re-made against current state. A daemon start re-checks the pending rows only,
covering a crash between that commit and the resume.

Cost: an activation with no waiting kit costs one in-process flag check. With
waits, it looks up only the activation's own changed ``providers/`` paths.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitChangeResult,
    KitProvider,
    KitResumeStatus,
)
from cruxible_client.contracts.temporal import format_datetime, utc_now
from cruxible_core.errors import CoreError
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.triggers.journal import retained_records

_log = structlog.get_logger(__name__)
_REQUEST_ACCESS = BodyAccessContext(principal_id="kit-resume", can_read_body=True)

#: Per instance root: whether any kit waits. Unknown until first read; set on
#: record, recomputed whenever waits are removed. The fast exit every activation
#: takes when no kit waits.
_WAITING: dict[str, bool] = {}
_RESUMING: ContextVar[bool] = ContextVar("cruxible_kit_resuming", default=False)


def _provider_path(provider_id: str) -> str:
    return f"providers/{provider_id}.json"


def _key(instance: PlaybillInstance) -> str:
    return str(instance.root)


def _rows(connection: sqlite3.Connection | None, sql: str, params: tuple[object, ...] = ()) -> list:  # type: ignore[type-arg]
    """Rows of a kit-resume query; an older journal without the tables has none."""

    if connection is None:
        return []
    try:
        return connection.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def _any_waiting(instance: PlaybillInstance) -> bool:
    key = _key(instance)
    known = _WAITING.get(key)
    if known is None:
        with retained_records(instance, write=False) as connection:
            known = bool(_rows(connection, "SELECT 1 FROM kit_resume_waits LIMIT 1"))
        _WAITING[key] = known
    return known


def _refresh_waiting(instance: PlaybillInstance, connection: sqlite3.Connection) -> None:
    _WAITING[_key(instance)] = bool(
        connection.execute("SELECT 1 FROM kit_resume_waits LIMIT 1").fetchone()
    )


def record_kit_resume(
    instance: PlaybillInstance,
    request: KitAddRequest,
    *,
    actor_id: str,
    waiting_on: Iterable[KitProvider],
) -> None:
    """Record a kit add stopped at awaiting_providers, replacing any earlier one."""

    resumed = request.model_copy(update={"dry_run": False, "at": None})
    stored = instance.body_store().store(canonical_bytes(resumed.model_dump(mode="json")))
    manifest = request.bundle.manifest
    with retained_records(instance, write=True) as connection:
        assert connection is not None
        connection.execute("DELETE FROM kit_resume_waits WHERE kit_id=?", (manifest.kit_id,))
        connection.execute(
            "INSERT OR REPLACE INTO kit_resumes (kit_id,version,request_digest,actor_id,"
            "recorded_at,recorded_head,state,proposal_id,detail,source) "
            "VALUES (?,?,?,?,?,?,'waiting',NULL,NULL,?)",
            (
                manifest.kit_id,
                manifest.version,
                stored.digest,
                actor_id,
                format_datetime(utc_now()),
                instance.accepted_coordinate().git_oid,
                request.source,
            ),
        )
        connection.executemany(
            "INSERT INTO kit_resume_waits (provider_path,kit_id,provider_id) VALUES (?,?,?)",
            [
                (_provider_path(provider.provider_id), manifest.kit_id, provider.provider_id)
                for provider in waiting_on
            ],
        )
        _refresh_waiting(instance, connection)


def resuming() -> bool:
    """Whether this call is a recorded kit add being resumed (it records nothing)."""

    return _RESUMING.get()


def forget_kit_resume(instance: PlaybillInstance, kit_id: str) -> None:
    """A later commit kit add of the same kit replaces any recorded resume."""

    with retained_records(instance, write=False) as connection:
        if not _rows(connection, "SELECT 1 FROM kit_resumes WHERE kit_id=?", (kit_id,)):
            return
    with retained_records(instance, write=True) as connection:
        assert connection is not None
        connection.execute("DELETE FROM kit_resume_waits WHERE kit_id=?", (kit_id,))
        connection.execute("DELETE FROM kit_resumes WHERE kit_id=?", (kit_id,))
        _refresh_waiting(instance, connection)


@contextmanager
def _resuming() -> Iterator[None]:
    token = _RESUMING.set(True)
    try:
        yield
    finally:
        _RESUMING.reset(token)


def resume_kits_after_activation(instance: PlaybillInstance, changed_paths: Iterable[str]) -> None:
    """Resume each kit whose last waited Provider this activation landed.

    Called after the activation committed; never raises into it.
    """

    if _RESUMING.get() or not _any_waiting(instance):
        return
    providers = sorted({path for path in changed_paths if path.startswith("providers/")})
    if not providers:
        return
    try:
        ready = _satisfy(instance, providers)
        for kit_id in ready:
            _resume(instance, kit_id)
    except Exception as exc:  # noqa: BLE001 - the activation is already committed
        _log.warning("kit_resume_deferred", error=f"{type(exc).__name__}: {exc}")


def _satisfy(instance: PlaybillInstance, provider_paths: list[str]) -> list[str]:
    """Clear the waits these Provider paths now satisfy; the kits left waiting on none."""

    from cruxible_core.service.kits import _provider_here

    head = instance.accepted_coordinate().git_oid
    marks = ",".join("?" for _ in provider_paths)
    with retained_records(instance, write=False) as connection:
        waits = _rows(
            connection,
            f"SELECT provider_path,kit_id,provider_id FROM kit_resume_waits "
            f"WHERE provider_path IN ({marks})",
            tuple(provider_paths),
        )
    if not waits:
        return []
    cleared: list[tuple[str, str]] = []
    for path, kit_id, provider_id in waits:
        request = _request(instance, kit_id)
        if request is None:
            continue
        provider = next(
            item for item in request.bundle.manifest.providers if item.provider_id == provider_id
        )
        raw = instance.blob_at(head, path)
        tree = {} if raw is None else {path: raw}
        if _provider_here(tree, provider)[0] == "installed":
            cleared.append((path, kit_id))
    if not cleared:
        return []
    with retained_records(instance, write=True) as connection:
        assert connection is not None
        connection.executemany(
            "DELETE FROM kit_resume_waits WHERE provider_path=? AND kit_id=?", cleared
        )
        ready = [
            kit_id
            for kit_id in sorted({kit_id for _path, kit_id in cleared})
            if connection.execute(
                "SELECT 1 FROM kit_resume_waits WHERE kit_id=? LIMIT 1", (kit_id,)
            ).fetchone()
            is None
        ]
        _refresh_waiting(instance, connection)
    return ready


def _request(instance: PlaybillInstance, kit_id: str) -> KitAddRequest | None:
    with retained_records(instance, write=False) as connection:
        found = _rows(
            connection,
            "SELECT request_digest FROM kit_resumes WHERE kit_id=? AND state='waiting'",
            (kit_id,),
        )
    if not found:
        return None
    raw = instance.body_store().read(found[0][0], access=_REQUEST_ACCESS)
    return KitAddRequest.model_validate_json(raw)


def _settle(
    instance: PlaybillInstance,
    kit_id: str,
    *,
    state: str | None,
    proposal_id: str | None = None,
    detail: str | None = None,
) -> None:
    with retained_records(instance, write=True) as connection:
        assert connection is not None
        if state is None:
            connection.execute("DELETE FROM kit_resume_waits WHERE kit_id=?", (kit_id,))
            connection.execute("DELETE FROM kit_resumes WHERE kit_id=?", (kit_id,))
        else:
            connection.execute(
                "UPDATE kit_resumes SET state=?,proposal_id=?,detail=? WHERE kit_id=?",
                (state, proposal_id, detail, kit_id),
            )
        _refresh_waiting(instance, connection)


def _resume(instance: PlaybillInstance, kit_id: str) -> KitChangeResult | None:
    """Run the recorded kit add at the current head, as the principal who ran it."""

    from cruxible_core.service.identity import require_authoring_principal
    from cruxible_core.service.kits import service_add_kit

    request = _request(instance, kit_id)
    if request is None:
        return None
    with retained_records(instance, write=False) as connection:
        ((actor_id,),) = _rows(
            connection, "SELECT actor_id FROM kit_resumes WHERE kit_id=?", (kit_id,)
        )
    try:
        require_authoring_principal(instance, actor_id)
    except CoreError as exc:
        _settle(
            instance,
            kit_id,
            state="blocked",
            detail=f"actor_no_longer_authoring: {actor_id} cannot propose here: {exc}",
        )
        return None
    try:
        with _resuming():
            result = service_add_kit(
                instance,
                request,
                actor_id=actor_id,
                timestamp=canonical_candidate_timestamp(utc_now()),
                install_provider=None,
            )
    except CoreError as exc:
        _settle(instance, kit_id, state="blocked", detail=str(exc))
        return None
    if result.status == "unchanged":
        _settle(instance, kit_id, state=None)
    elif result.status in {"proposed", "accepted"}:
        _settle(instance, kit_id, state="proposed", proposal_id=result.proposal_id)
    else:
        _settle(
            instance,
            kit_id,
            state="blocked",
            detail=result.detail or f"kit add {result.status}",
        )
    _log.info("kit_resumed", kit_id=kit_id, status=result.status, actor_id=actor_id)
    return result


def resume_pending_kits(instance: PlaybillInstance) -> tuple[str, ...]:
    """Daemon start: resume a waiting kit whose waits the head already satisfies.

    Reads the pending rows only, never the tree: the crash this covers is one
    between an activation's commit and its resume.
    """

    with retained_records(instance, write=False) as connection:
        waits = _rows(
            connection,
            "SELECT w.provider_path FROM kit_resume_waits w JOIN kit_resumes k "
            "USING (kit_id) WHERE k.state='waiting'",
        )
        empty = _rows(
            connection,
            "SELECT kit_id FROM kit_resumes WHERE state='waiting' AND kit_id NOT IN "
            "(SELECT kit_id FROM kit_resume_waits)",
        )
    ready = _satisfy(instance, sorted({path for (path,) in waits})) if waits else []
    resumed = []
    for kit_id in sorted({*ready, *(kit_id for (kit_id,) in empty)}):
        if _resume(instance, kit_id) is not None:
            resumed.append(kit_id)
    return tuple(resumed)


def kit_resume_statuses(instance: PlaybillInstance) -> tuple[KitResumeStatus, ...]:
    with retained_records(instance, write=False) as connection:
        rows = _rows(
            connection,
            "SELECT kit_id,version,state,actor_id,recorded_at,proposal_id,detail,source "
            "FROM kit_resumes ORDER BY kit_id",
        )
        waits = _rows(connection, "SELECT kit_id,provider_id FROM kit_resume_waits")
    waiting: dict[str, list[str]] = {}
    for kit_id, provider_id in waits:
        waiting.setdefault(kit_id, []).append(provider_id)
    return tuple(
        KitResumeStatus(
            kit_id=kit_id,
            version=version,
            state=state,
            actor_id=actor_id,
            recorded_at=recorded_at,
            waiting_on=tuple(sorted(waiting.get(kit_id, ()))),
            proposal_id=proposal_id,
            detail=detail,
            source=source,
        )
        for kit_id, version, state, actor_id, recorded_at, proposal_id, detail, source in rows
    )


def blocked_kit_resumes(instance: PlaybillInstance) -> tuple[KitResumeStatus, ...]:
    return tuple(item for item in kit_resume_statuses(instance) if item.state == "blocked")


__all__ = [
    "resuming",
    "blocked_kit_resumes",
    "forget_kit_resume",
    "kit_resume_statuses",
    "record_kit_resume",
    "resume_kits_after_activation",
    "resume_pending_kits",
]

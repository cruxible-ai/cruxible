"""Receipted install-ledger writes and the ownership queries an installer needs.

WHAT THIS IS. Composition ownership (``config/composition_ownership.py``)
answers "upstream or local?" and nothing finer. Once installable artifacts can
each contribute a contract, a named query, a procedure, or an enum to the same
local layer, that answer stops being enough: selective uninstall, dependency-
blocked removal, and customer-edit-preserving updates all need to know WHICH
install put a name there. This module is the authoritative record of that, plus
the phase machine that says how far an install got.

WHAT THIS IS NOT (phase 1). There is no installer here. Nothing in this module
reads, writes, or validates config; nothing computes a digest from a file.
Every write takes the digest its caller computed, so the ledger can be
exercised — and its guarantees tested — before the orchestration that will use
it exists. The write functions are deliberately service-internal: no MCP tool,
no HTTP write route. They become reachable when the installer lands.

WHY EVERY WRITE IS RECEIPTED. An install ledger whose rows cannot be attributed
is a worse artifact than no ledger: it would let an unattributed process claim
ownership of a name and thereby block or license later changes to config.
Each write therefore runs inside :func:`mutation_receipt`, in ONE unit of work,
with refusals receipted too.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

from cruxible_core.errors import (
    ConfigError,
    InstallNotFoundError,
    InstallOwnershipCollisionError,
    InstallPhaseRequirementError,
    InstallPhaseTransitionError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.installs.types import (
    UNOBSERVABLE_REFERENCE_SOURCES,
    ArtifactRef,
    CustomizationReport,
    InstallDetail,
    InstallPhase,
    InstallPhaseEvent,
    InstallRecord,
    ObjectReference,
    OwnedObject,
    OwnedObjectKind,
    OwnershipCollision,
    UninstallBlocker,
    UninstallPreconditionReport,
    legal_next_phases,
    new_install_id,
)
from cruxible_core.instance_protocol import InstallLedgerStoreProtocol, InstanceProtocol
from cruxible_core.primitives import new_id
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import ListResult, list_truncated
from cruxible_core.temporal import format_datetime, utc_now

# ---------------------------------------------------------------------------
# Writes (service-internal until the installer lands)
# ---------------------------------------------------------------------------


def service_create_install(
    instance: InstanceProtocol,
    *,
    artifact_kind: str,
    artifact_id: str,
    artifact_version: str,
    artifact_digest: str,
    actor_context: GovernedActorContext | None,
    install_id: str | None = None,
) -> InstallRecord:
    """Open one install in ``preparing`` and seed its phase history.

    The seed event has ``from_phase=None``: an install's history starts with
    its creation, so "how did this reach preparing" is answerable without
    inferring it from the absence of rows.
    """
    with mutation_receipt(
        instance,
        "install_create",
        {
            "artifact_kind": artifact_kind,
            "artifact_id": artifact_id,
            "artifact_version": artifact_version,
            "artifact_digest": artifact_digest,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        store: InstallLedgerStoreProtocol = ctx.uow.installs

        resolved_id = install_id or new_install_id()
        if store.get_install(resolved_id) is not None:
            _refuse(ctx.builder, f"install '{resolved_id}' already exists")

        now = _now_iso()
        record = InstallRecord(
            install_id=resolved_id,
            artifact=ArtifactRef(
                artifact_kind=artifact_kind,
                artifact_id=artifact_id,
                artifact_version=artifact_version,
                artifact_digest=artifact_digest,
            ),
            phase="preparing",
            created_at=now,
            updated_at=now,
            actor_context=actor_context,
            receipt_id=ctx.builder.receipt_id,
        )
        store.save_install(record)
        store.append_phase_event(
            InstallPhaseEvent(
                event_id=new_id("inst-evt"),
                install_id=resolved_id,
                sequence=store.next_phase_sequence(resolved_id),
                from_phase=None,
                to_phase="preparing",
                occurred_at=now,
                actor_context=actor_context,
                reason="install created",
                receipt_id=ctx.builder.receipt_id,
            )
        )
        ctx.builder.record_validation(
            passed=True,
            detail={"install_id": resolved_id, "phase": "preparing"},
        )
        ctx.set_result(record)
        return record


def service_record_owned_object(
    instance: InstanceProtocol,
    install_id: str,
    *,
    object_kind: OwnedObjectKind,
    object_name: str,
    installed_digest: str,
    references: Sequence[ObjectReference] = (),
    actor_context: GovernedActorContext | None = None,
) -> OwnedObject:
    """Record that *install_id* owns one config object, refusing collisions.

    Ownership may only be claimed while the install is ``preparing``. That is
    the RFC's ordering, not a convenience: preflight (which includes the
    collision check) must complete before anything mutates, so an install that
    has already been proposed for acceptance cannot quietly widen what it owns.
    """
    with mutation_receipt(
        instance,
        "install_record_owned_object",
        {
            "install_id": install_id,
            "object_kind": object_kind,
            "object_name": object_name,
            "installed_digest": installed_digest,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        store: InstallLedgerStoreProtocol = ctx.uow.installs

        install = _require_install(store, install_id)
        if install.phase != "preparing":
            ctx.builder.record_validation(
                passed=False,
                detail={
                    "reason": "ownership may only be claimed while preparing",
                    "install_id": install_id,
                    "actual_phase": install.phase,
                },
            )
            raise InstallPhaseRequirementError(
                install_id,
                "recording an owned object",
                install.phase,
                ("preparing",),
            )

        collision = store.find_live_owner(object_kind=object_kind, object_name=object_name)
        if collision is not None:
            ctx.builder.record_validation(
                passed=False,
                detail={
                    "reason": "ownership collision",
                    "object_kind": object_kind,
                    "object_name": object_name,
                    "owning_install_id": collision.owning_install_id,
                },
            )
            raise InstallOwnershipCollisionError(
                object_kind,
                object_name,
                collision.owning_install_id,
                collision.owning_install_phase,
            )

        owned = OwnedObject(
            install_id=install_id,
            object_kind=object_kind,
            object_name=object_name,
            installed_digest=installed_digest,
            references=list(references),
            recorded_at=_now_iso(),
            receipt_id=ctx.builder.receipt_id,
        )
        store.save_owned_object(owned)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "install_id": install_id,
                "object_kind": object_kind,
                "object_name": object_name,
            },
        )
        ctx.set_result(owned)
        return owned


def service_advance_install_phase(
    instance: InstanceProtocol,
    install_id: str,
    *,
    to_phase: InstallPhase,
    actor_context: GovernedActorContext | None = None,
    reason: str | None = None,
) -> InstallRecord:
    """Move one install to *to_phase*, refusing every illegal transition.

    The refusal is typed and names the phase the install is ACTUALLY in; an
    installer resuming after a crash reads its position off the refusal rather
    than guessing it. The phase update, the appended history event, and the
    receipt all land in the same unit of work, so a phase can never be observed
    without the event that explains it.
    """
    with mutation_receipt(
        instance,
        "install_phase_advance",
        {"install_id": install_id, "to_phase": to_phase, "reason": reason},
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        store: InstallLedgerStoreProtocol = ctx.uow.installs

        install = _require_install(store, install_id)
        allowed = legal_next_phases(install.phase)
        if to_phase not in allowed:
            ctx.builder.record_validation(
                passed=False,
                detail={
                    "reason": "illegal install phase transition",
                    "install_id": install_id,
                    "actual_phase": install.phase,
                    "requested_phase": to_phase,
                    "legal_phases": list(allowed),
                },
            )
            raise InstallPhaseTransitionError(install_id, install.phase, to_phase, allowed)

        now = _now_iso()
        failure_reason = reason if to_phase == "failed" else install.failure_reason
        store.set_install_phase(
            install_id,
            phase=to_phase,
            updated_at=now,
            failure_reason=failure_reason,
            receipt_id=ctx.builder.receipt_id,
        )
        store.append_phase_event(
            InstallPhaseEvent(
                event_id=new_id("inst-evt"),
                install_id=install_id,
                sequence=store.next_phase_sequence(install_id),
                from_phase=install.phase,
                to_phase=to_phase,
                occurred_at=now,
                actor_context=actor_context,
                reason=reason,
                receipt_id=ctx.builder.receipt_id,
            )
        )
        ctx.builder.record_validation(
            passed=True,
            detail={
                "install_id": install_id,
                "from_phase": install.phase,
                "to_phase": to_phase,
            },
        )
        advanced = install.model_copy(
            update={"phase": to_phase, "updated_at": now, "failure_reason": failure_reason}
        )
        ctx.set_result(advanced)
        return advanced


def service_record_object_customization(
    instance: InstanceProtocol,
    install_id: str,
    *,
    object_kind: OwnedObjectKind,
    object_name: str,
    current_digest: str,
    actor_context: GovernedActorContext | None = None,
) -> CustomizationReport:
    """Persist the customization verdict for one owned object.

    ``current_digest`` is supplied by the CALLER. This module never reads the
    deployed config: doing so would give the ledger a second, competing opinion
    about what the config contains, and the config reader belongs to the
    installer. The comparison itself is the whole rule — a digest that differs
    from what the install put there means the customer edited it, and an update
    that overwrites it destroys their work.
    """
    with mutation_receipt(
        instance,
        "install_object_customization",
        {
            "install_id": install_id,
            "object_kind": object_kind,
            "object_name": object_name,
            "current_digest": current_digest,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        store: InstallLedgerStoreProtocol = ctx.uow.installs

        _require_install(store, install_id)
        owned = store.get_owned_object(install_id, object_kind=object_kind, object_name=object_name)
        if owned is None:
            _refuse(
                ctx.builder,
                f"install '{install_id}' does not own {object_kind} '{object_name}'",
            )

        customized = owned.installed_digest != current_digest
        store.set_owned_object_customization(
            install_id,
            object_kind=object_kind,
            object_name=object_name,
            customized=customized,
            current_digest=current_digest,
        )
        report = CustomizationReport(
            install_id=install_id,
            object_kind=object_kind,
            object_name=object_name,
            installed_digest=owned.installed_digest,
            current_digest=current_digest,
            customized=customized,
        )
        ctx.builder.record_validation(
            passed=True,
            detail=report.model_dump(mode="json", exclude_none=True),
        )
        ctx.set_result(report)
        return report


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def service_list_installs(
    instance: InstanceProtocol,
    *,
    phase: InstallPhase | None = None,
    artifact_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ListResult:
    """List install records newest-first with the standard envelope."""
    store = instance.get_install_ledger_store()
    total = store.count_installs(phase=phase, artifact_id=artifact_id)
    records = store.list_installs(
        phase=phase,
        artifact_id=artifact_id,
        limit=limit,
        offset=offset,
    )
    return ListResult(
        items=[record.model_dump(mode="json", exclude_none=True) for record in records],
        total=total,
        limit=limit,
        offset=offset,
        truncated=list_truncated(total=total, offset=offset, returned=len(records)),
        read_revision=instance.get_read_revision(),
    )


def service_get_install(instance: InstanceProtocol, install_id: str) -> InstallDetail:
    """Return one install with its owned objects and full phase history."""
    store = instance.get_install_ledger_store()
    install = _require_install(store, install_id)
    return InstallDetail(
        install=install,
        owned_objects=store.list_owned_objects(install_id),
        phase_history=store.list_phase_events(install_id),
    )


def service_objects_owned_by_install(
    instance: InstanceProtocol,
    install_id: str,
) -> list[OwnedObject]:
    """Return every object *install_id* claims, ordered by kind then name."""
    store = instance.get_install_ledger_store()
    _require_install(store, install_id)
    return store.list_owned_objects(install_id)


def service_install_owning_object(
    instance: InstanceProtocol,
    *,
    object_kind: OwnedObjectKind,
    object_name: str,
) -> InstallRecord | None:
    """Return the install that owns (kind, name), or None if nothing does.

    None is the honest answer for BOTH "no install ever installed this" and
    "the install that did has failed or rolled back". Neither is an install
    that can be asked to give the name up.
    """
    store = instance.get_install_ledger_store()
    collision = store.find_live_owner(object_kind=object_kind, object_name=object_name)
    if collision is None:
        return None
    return store.get_install(collision.owning_install_id)


def service_check_ownership_collision(
    instance: InstanceProtocol,
    *,
    object_kind: OwnedObjectKind,
    object_name: str,
) -> OwnershipCollision | None:
    """Return the live claim that would collide with installing (kind, name).

    Preflight calls this BEFORE anything mutates. It reports only ledger-known
    ownership: a name already present in hand-written config is invisible here
    and must be caught by the composer's own refuse-on-collision rule.
    """
    store = instance.get_install_ledger_store()
    return store.find_live_owner(object_kind=object_kind, object_name=object_name)


def service_detect_object_customization(
    instance: InstanceProtocol,
    install_id: str,
    *,
    object_kind: OwnedObjectKind,
    object_name: str,
    current_digest: str,
) -> CustomizationReport:
    """Compare a caller-supplied current digest against the installed one.

    Pure read: it persists nothing. Use
    :func:`service_record_object_customization` to durably record the verdict.
    """
    store = instance.get_install_ledger_store()
    _require_install(store, install_id)
    owned = store.get_owned_object(install_id, object_kind=object_kind, object_name=object_name)
    if owned is None:
        raise ConfigError(f"install '{install_id}' does not own {object_kind} '{object_name}'")
    return CustomizationReport(
        install_id=install_id,
        object_kind=object_kind,
        object_name=object_name,
        installed_digest=owned.installed_digest,
        current_digest=current_digest,
        customized=owned.installed_digest != current_digest,
    )


def service_uninstall_preconditions(
    instance: InstanceProtocol,
    install_id: str,
) -> UninstallPreconditionReport:
    """Report what the LEDGER says blocks removing *install_id*.

    A blocker is a reference DECLARED by an owned object of another install
    that still holds its ownership claims, pointing at an object this install
    owns. That is a real, checkable dependency and the reason phase 2 can offer
    dependency-blocked removal at all.

    THE LIMIT, STATED PLAINLY. ``blocked=False`` does NOT mean "safe to
    delete". This check sees only what an installer wrote into this ledger. It
    does not read config, so a hand-written named query referencing an
    installed contract is invisible. It does not read accepted procedure pins
    or graph state, so a live procedure compiled against an installed query is
    invisible too. Those sources are listed in
    ``unobservable_reference_sources`` on every report so a caller cannot
    mistake silence for safety, and closing them is uninstaller work, not a
    query these tables can answer.
    """
    store = instance.get_install_ledger_store()
    install = _require_install(store, install_id)
    owned_objects = store.list_owned_objects(install_id)
    owned_keys = {(item.object_kind, item.object_name) for item in owned_objects}

    blockers: list[UninstallBlocker] = []
    for other, other_phase in store.list_referencing_objects(exclude_install_id=install_id):
        for reference in other.references:
            key = (reference.object_kind, reference.object_name)
            if key not in owned_keys:
                continue
            blockers.append(
                UninstallBlocker(
                    object_kind=reference.object_kind,
                    object_name=reference.object_name,
                    referencing_install_id=other.install_id,
                    referencing_install_phase=other_phase,
                    referencing_object_kind=other.object_kind,
                    referencing_object_name=other.object_name,
                )
            )

    return UninstallPreconditionReport(
        install_id=install_id,
        install_phase=install.phase,
        blocked=bool(blockers),
        blockers=blockers,
        customized_objects=[item for item in owned_objects if item.customized],
        unobservable_reference_sources=list(UNOBSERVABLE_REFERENCE_SOURCES),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 UTC stamp for ledger rows."""
    return format_datetime(utc_now()) or ""


def _require_install(store: InstallLedgerStoreProtocol, install_id: str) -> InstallRecord:
    install = store.get_install(install_id)
    if install is None:
        raise InstallNotFoundError(install_id)
    return install


def _refuse(builder: ReceiptBuilder, reason: str) -> NoReturn:
    builder.record_validation(passed=False, detail={"reason": reason})
    raise ConfigError(reason)


__all__ = [
    "service_advance_install_phase",
    "service_check_ownership_collision",
    "service_create_install",
    "service_detect_object_customization",
    "service_get_install",
    "service_install_owning_object",
    "service_list_installs",
    "service_objects_owned_by_install",
    "service_record_object_customization",
    "service_record_owned_object",
    "service_uninstall_preconditions",
]

"""Receipted compute-slot binding: bind, rebind, retire, resolve, and read.

A binding is a DEPLOYMENT RECORD, not configuration. A procedure pins the slot
INTERFACE (the contract names it consumes and produces); which provider fills
that slot on THIS install is state, written here, receipted like every other
governed mutation, and revised in place rather than re-declared. Rebinding is a
governed update against the ledger — no config file changes, and no acceptance
is re-run, because nothing the procedure pinned has moved.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It never reads config and never
resolves a provider registry. Both sides of every compatibility check are
supplied BY THE CALLER: the slot interface as the procedure pinned it, and the
provider's own declaration. That keeps the ledger honest about what it verified
(two declarations it was handed, recorded on the row) and keeps the slot
identity opaque — plain strings, no dependency on whatever schema declares them.

IN-FLIGHT RUNS ARE NOT AFFECTED by a rebind. Run start resolves the slot once,
records the resolved binding on the run receipt, and the run finishes on that
binding whatever the ledger does afterwards. Nothing here reaches into a running
executor; wiring resolution into run start is the integration phase.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from cruxible_core.bindings.store import BindingStoreProtocol
from cruxible_core.bindings.types import (
    BindingChangeKind,
    BindingStatus,
    BindingWriteResult,
    NearMatchCandidate,
    NearMatchReport,
    ProviderDescriptor,
    SlotBinding,
    SlotBindingHistoryResult,
    SlotBindingListResult,
    SlotBindingRevision,
    SlotInterface,
)
from cruxible_core.errors import (
    BindingBillingModeRefusedError,
    BindingConsentRequiredError,
    BindingContractMismatchError,
    BindingNotFoundError,
    ConfigError,
    SlotAlreadyBoundError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import list_truncated
from cruxible_core.storage import StorageIntegrityError
from cruxible_core.temporal import utc_now

# ---------------------------------------------------------------------------
# Compatibility checking + near-match reporting
# ---------------------------------------------------------------------------


def evaluate_candidate(slot: SlotInterface, provider: ProviderDescriptor) -> NearMatchCandidate:
    """Return why (or whether) one provider fails to satisfy a slot interface.

    A candidate with no mismatches satisfies the slot. Every reason is
    collected, never just the first: an operator who fixes one mismatch and
    resubmits only to be told about the next has been sent round the loop for
    information the checker already had.

    Third-party CONSENT is deliberately not a mismatch. Consent is an operator
    act, not a property of the provider: a third-party provider that fits the
    interface is bindable the moment consent is recorded, so listing it as a
    near-match failure would name a provider the operator can in fact use.
    """
    mismatches: list[str] = []
    matched_in = provider.contract_in == slot.contract_in
    matched_out = provider.contract_out == slot.contract_out
    if not matched_in:
        mismatches.append(
            f"contract_in mismatch (declares '{provider.contract_in}', "
            f"slot requires '{slot.contract_in}')"
        )
    if not matched_out:
        mismatches.append(
            f"contract_out mismatch (declares '{provider.contract_out}', "
            f"slot requires '{slot.contract_out}')"
        )
    if (
        slot.allowed_billing_modes is not None
        and provider.billing_mode not in slot.allowed_billing_modes
    ):
        allowed = ", ".join(slot.allowed_billing_modes)
        mismatches.append(
            f"billing_mode '{provider.billing_mode}' not in the slot's allowed set [{allowed}]"
        )
    return NearMatchCandidate(
        provider_name=provider.provider_name,
        contract_in=provider.contract_in,
        contract_out=provider.contract_out,
        billing_mode=provider.billing_mode,
        matched_contract_in=matched_in,
        matched_contract_out=matched_out,
        mismatches=tuple(mismatches),
    )


def build_near_match_report(
    slot: SlotInterface,
    *,
    requested: ProviderDescriptor,
    candidates: Sequence[ProviderDescriptor] = (),
) -> NearMatchReport:
    """Rank every offered provider against the slot and explain each failure.

    The REQUESTED provider is always in the report — it is the candidate the
    caller actually reached for, and omitting it would make the refusal read as
    though it were about someone else. Candidates that satisfy the slot outright
    are excluded: the report answers "why can nothing bind here", and a provider
    that fits is not part of that answer.

    The ordering is total (contract sides matched desc, mismatch count asc,
    provider name asc), so the same inputs always render the same report
    regardless of the order candidates were offered in.
    """
    seen: dict[str, ProviderDescriptor] = {requested.provider_name: requested}
    for candidate in candidates:
        seen.setdefault(candidate.provider_name, candidate)
    evaluated = [evaluate_candidate(slot, provider) for provider in seen.values()]
    failures = [candidate for candidate in evaluated if candidate.mismatches]
    failures.sort(
        key=lambda candidate: (
            -candidate.matched_sides,
            len(candidate.mismatches),
            candidate.provider_name,
        )
    )
    return NearMatchReport(
        slot_name=slot.slot_name,
        contract_in=slot.contract_in,
        contract_out=slot.contract_out,
        allowed_billing_modes=slot.allowed_billing_modes,
        candidates=tuple(failures),
    )


def _check_bindable(
    slot: SlotInterface,
    provider: ProviderDescriptor,
    *,
    install_id: str,
    candidates: Sequence[ProviderDescriptor],
    third_party_consent: bool,
    builder: ReceiptBuilder,
) -> None:
    """Refuse an unbindable provider, ordered so the report is the best answer.

    Contract equality is checked FIRST and reported with near matches, because
    a contract mismatch means the operator must pick a different provider and
    the ranked list is what they need. Billing and consent are checked after:
    both are fixable on the provider they already chose, so they get a targeted
    error that echoes the allowed values rather than a list of alternatives.
    """
    outcome = evaluate_candidate(slot, provider)
    if not outcome.matched_contract_in or not outcome.matched_contract_out:
        report = build_near_match_report(slot, requested=provider, candidates=candidates)
        text = report.render()
        builder.record_validation(
            passed=False,
            detail={
                "reason": "binding_contract_mismatch",
                "slot_name": slot.slot_name,
                "install_id": install_id,
                "provider_name": provider.provider_name,
                "near_match_report": text,
            },
        )
        raise BindingContractMismatchError(
            install_id=install_id,
            slot_name=slot.slot_name,
            report_text=text,
            near_matches=[candidate.model_dump(mode="json") for candidate in report.candidates],
        )

    if (
        slot.allowed_billing_modes is not None
        and provider.billing_mode not in slot.allowed_billing_modes
    ):
        allowed = list(slot.allowed_billing_modes)
        builder.record_validation(
            passed=False,
            detail={
                "reason": "binding_billing_mode_refused",
                "slot_name": slot.slot_name,
                "install_id": install_id,
                "provider_name": provider.provider_name,
                "billing_mode": provider.billing_mode,
                "allowed_billing_modes": allowed,
            },
        )
        raise BindingBillingModeRefusedError(
            install_id=install_id,
            slot_name=slot.slot_name,
            provider_name=provider.provider_name,
            billing_mode=provider.billing_mode,
            allowed_billing_modes=allowed,
        )

    if provider.third_party and slot.requires_third_party_consent and not third_party_consent:
        builder.record_validation(
            passed=False,
            detail={
                "reason": "binding_consent_required",
                "slot_name": slot.slot_name,
                "install_id": install_id,
                "provider_name": provider.provider_name,
            },
        )
        raise BindingConsentRequiredError(
            install_id=install_id,
            slot_name=slot.slot_name,
            provider_name=provider.provider_name,
        )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def service_create_slot_binding(
    instance: InstanceProtocol,
    *,
    install_id: str,
    slot: SlotInterface,
    provider: ProviderDescriptor,
    candidates: Sequence[ProviderDescriptor] = (),
    third_party_consent: bool = False,
    note: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> BindingWriteResult:
    """Bind one compute slot on one install to a provider, receipted.

    The binding records the SLOT INTERFACE it was validated against, not the
    provider's declaration of it. They are equal at bind time by construction;
    keeping the slot's copy is what lets a later rebind be checked against what
    the pinned procedures actually expect.
    """
    _require_identifiers(install_id=install_id, slot_name=slot.slot_name)
    with mutation_receipt(
        instance,
        "slot_binding_bind",
        {
            "install_id": install_id,
            "slot_name": slot.slot_name,
            "provider_name": provider.provider_name,
            "contract_in": slot.contract_in,
            "contract_out": slot.contract_out,
            "billing_mode": provider.billing_mode,
            "third_party": provider.third_party,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        store: BindingStoreProtocol = ctx.uow.bindings

        existing = store.get_active_binding(install_id=install_id, slot_name=slot.slot_name)
        if existing is not None:
            ctx.builder.record_validation(
                passed=False,
                detail={
                    "reason": "slot_already_bound",
                    "install_id": install_id,
                    "slot_name": slot.slot_name,
                    "binding_id": existing.binding_id,
                    "provider_name": existing.provider_name,
                },
            )
            raise SlotAlreadyBoundError(
                install_id=install_id,
                slot_name=slot.slot_name,
                binding_id=existing.binding_id,
                provider_name=existing.provider_name,
            )

        _check_bindable(
            slot,
            provider,
            install_id=install_id,
            candidates=candidates,
            third_party_consent=third_party_consent,
            builder=ctx.builder,
        )

        now = utc_now()
        binding = SlotBinding(
            install_id=install_id,
            slot_name=slot.slot_name,
            provider_name=provider.provider_name,
            contract_in=slot.contract_in,
            contract_out=slot.contract_out,
            billing_mode=provider.billing_mode,
            **_consent_fields(
                provider=provider,
                third_party_consent=third_party_consent,
                actor_context=actor_context,
                now=now,
            ),
            revision=1,
            status="active",
            bound_at=now,
            updated_at=now,
            actor_context=actor_context,
            receipt_id=ctx.builder.receipt_id,
        )
        try:
            store.save_binding(binding)
        except StorageIntegrityError as exc:
            # The read above missed a writer that committed between it and here.
            # The partial unique index is the authority; translate its refusal
            # into the same typed error the service-level check produces, so
            # racing callers and sequential callers see one failure mode.
            ctx.builder.record_validation(
                passed=False,
                detail={
                    "reason": "slot_already_bound",
                    "install_id": install_id,
                    "slot_name": slot.slot_name,
                    "detected_by": "unique_index",
                },
            )
            raise SlotAlreadyBoundError(
                install_id=install_id,
                slot_name=slot.slot_name,
            ) from exc

        store.save_revision(_revision_of(binding, change_kind="bind", note=note, recorded_at=now))
        ctx.builder.record_validation(
            passed=True,
            detail={
                "binding_id": binding.binding_id,
                "install_id": install_id,
                "slot_name": slot.slot_name,
                "provider_name": provider.provider_name,
                "revision": 1,
            },
        )
        result = BindingWriteResult(binding=binding, change_kind="bind")
        ctx.set_result(result)
    return result


def service_rebind_slot(
    instance: InstanceProtocol,
    *,
    install_id: str,
    slot: SlotInterface,
    provider: ProviderDescriptor,
    candidates: Sequence[ProviderDescriptor] = (),
    third_party_consent: bool = False,
    note: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> BindingWriteResult:
    """Move an existing slot binding to another provider, as a new revision.

    The ledger row keeps its identity and gains a revision; the previous
    revision stays readable in history. Nothing about the procedure changes and
    nothing is re-accepted — a rebind is a deployment decision, recorded as one.
    Runs already in flight keep the binding they resolved at start.
    """
    _require_identifiers(install_id=install_id, slot_name=slot.slot_name)
    with mutation_receipt(
        instance,
        "slot_binding_rebind",
        {
            "install_id": install_id,
            "slot_name": slot.slot_name,
            "provider_name": provider.provider_name,
            "contract_in": slot.contract_in,
            "contract_out": slot.contract_out,
            "billing_mode": provider.billing_mode,
            "third_party": provider.third_party,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        store: BindingStoreProtocol = ctx.uow.bindings

        current = store.get_active_binding(install_id=install_id, slot_name=slot.slot_name)
        if current is None:
            ctx.builder.record_validation(
                passed=False,
                detail={
                    "reason": "binding_not_found",
                    "install_id": install_id,
                    "slot_name": slot.slot_name,
                },
            )
            raise BindingNotFoundError(install_id=install_id, slot_name=slot.slot_name)

        _check_bindable(
            slot,
            provider,
            install_id=install_id,
            candidates=candidates,
            third_party_consent=third_party_consent,
            builder=ctx.builder,
        )

        now = utc_now()
        rebound = current.model_copy(
            update={
                "provider_name": provider.provider_name,
                "contract_in": slot.contract_in,
                "contract_out": slot.contract_out,
                "billing_mode": provider.billing_mode,
                **_consent_fields(
                    provider=provider,
                    third_party_consent=third_party_consent,
                    actor_context=actor_context,
                    now=now,
                    previous=current,
                ),
                "revision": current.revision + 1,
                "status": "active",
                "updated_at": now,
                "actor_context": actor_context,
                "receipt_id": ctx.builder.receipt_id,
            }
        )
        store.update_binding(rebound)
        store.save_revision(_revision_of(rebound, change_kind="rebind", note=note, recorded_at=now))
        ctx.builder.record_validation(
            passed=True,
            detail={
                "binding_id": rebound.binding_id,
                "install_id": install_id,
                "slot_name": slot.slot_name,
                "previous_provider_name": current.provider_name,
                "provider_name": provider.provider_name,
                "revision": rebound.revision,
            },
        )
        result = BindingWriteResult(
            binding=rebound,
            change_kind="rebind",
            previous_provider_name=current.provider_name,
            previous_revision=current.revision,
        )
        ctx.set_result(result)
    return result


def service_retire_slot_binding(
    instance: InstanceProtocol,
    *,
    install_id: str,
    slot_name: str,
    note: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> BindingWriteResult:
    """Retire the active binding for a slot, leaving the slot unbound.

    Retirement is a revision like any other, so the row and its whole history
    survive: what an install USED to run on is exactly the question an incident
    review asks. Once retired, the slot is free to bind again — and the next
    bind mints a new binding id, because it is a new deployment decision.
    """
    _require_identifiers(install_id=install_id, slot_name=slot_name)
    with mutation_receipt(
        instance,
        "slot_binding_retire",
        {"install_id": install_id, "slot_name": slot_name},
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        store: BindingStoreProtocol = ctx.uow.bindings

        current = store.get_active_binding(install_id=install_id, slot_name=slot_name)
        if current is None:
            ctx.builder.record_validation(
                passed=False,
                detail={
                    "reason": "binding_not_found",
                    "install_id": install_id,
                    "slot_name": slot_name,
                },
            )
            raise BindingNotFoundError(install_id=install_id, slot_name=slot_name)

        now = utc_now()
        retired = current.model_copy(
            update={
                "revision": current.revision + 1,
                "status": "retired",
                "updated_at": now,
                "retired_at": now,
                "actor_context": actor_context,
                "receipt_id": ctx.builder.receipt_id,
            }
        )
        store.update_binding(retired)
        store.save_revision(_revision_of(retired, change_kind="retire", note=note, recorded_at=now))
        ctx.builder.record_validation(
            passed=True,
            detail={
                "binding_id": retired.binding_id,
                "install_id": install_id,
                "slot_name": slot_name,
                "provider_name": retired.provider_name,
                "revision": retired.revision,
            },
        )
        result = BindingWriteResult(
            binding=retired,
            change_kind="retire",
            previous_provider_name=current.provider_name,
            previous_revision=current.revision,
        )
        ctx.set_result(result)
    return result


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def service_resolve_slot_binding(
    instance: InstanceProtocol,
    *,
    install_id: str,
    slot_name: str,
) -> SlotBinding:
    """Resolve one slot to the provider bound on this install. Run start calls this.

    Raises rather than returning ``None``: an unbound slot cannot be executed,
    and returning a null here would push the same refusal into the executor with
    less to say about it. The caller records the returned binding (its id AND
    its revision) on the run receipt, which is what makes a later rebind
    non-retroactive — the run states the binding it ran on.
    """
    _require_identifiers(install_id=install_id, slot_name=slot_name)
    store = instance.get_bindings_store()
    try:
        binding = store.get_active_binding(install_id=install_id, slot_name=slot_name)
    finally:
        store.close()
    if binding is None:
        raise BindingNotFoundError(install_id=install_id, slot_name=slot_name)
    return binding


def service_list_slot_bindings(
    instance: InstanceProtocol,
    *,
    install_id: str | None = None,
    slot_name: str | None = None,
    status: BindingStatus | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> SlotBindingListResult:
    """List binding rows with the standard envelope.

    Ordered by install, then slot, then binding id — a total order on stable
    keys, so paging through the ledger cannot skip or repeat a row the way an
    ordering on a mutable column (``updated_at``, ``revision``) would.
    """
    _validate_page(limit=limit, offset=offset)
    if status is not None and status not in ("active", "retired"):
        raise ConfigError(f"status must be one of: active, retired (got '{status}')")
    store = instance.get_bindings_store()
    try:
        total = store.count_bindings(install_id=install_id, slot_name=slot_name, status=status)
        items = store.list_bindings(
            install_id=install_id,
            slot_name=slot_name,
            status=status,
            limit=limit,
            offset=offset,
        )
    finally:
        store.close()
    return SlotBindingListResult(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        truncated=list_truncated(total=total, offset=offset, returned=len(items)),
        read_revision=instance.get_read_revision(),
    )


def service_slot_binding_history(
    instance: InstanceProtocol,
    *,
    binding_id: str,
    limit: int | None = None,
    offset: int = 0,
) -> SlotBindingHistoryResult:
    """Return every revision of one binding, oldest first.

    Ascending order on purpose: history here is a narrative of one slot's
    deployment, and reading it forwards is how "what did we change, and when"
    is actually asked.
    """
    _validate_page(limit=limit, offset=offset)
    store = instance.get_bindings_store()
    try:
        if store.get_binding(binding_id) is None:
            raise BindingNotFoundError(binding_id=binding_id)
        total = store.count_revisions(binding_id)
        items = store.list_revisions(binding_id, limit=limit, offset=offset)
    finally:
        store.close()
    return SlotBindingHistoryResult(
        binding_id=binding_id,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        truncated=list_truncated(total=total, offset=offset, returned=len(items)),
        read_revision=instance.get_read_revision(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_identifiers(*, install_id: str, slot_name: str) -> None:
    if not install_id.strip():
        raise ConfigError("install_id must not be blank")
    if not slot_name.strip():
        raise ConfigError("slot_name must not be blank")


def _validate_page(*, limit: int | None, offset: int) -> None:
    if offset < 0:
        raise ConfigError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ConfigError("limit must be >= 0")


_NO_CONSENT: dict[str, Any] = {
    "third_party_consent": False,
    "consent_actor_id": None,
    "consent_org_id": None,
    "consent_at": None,
}
"""The cleared consent stamp. A revision either names who consented, or names nobody."""


def _consent_fields(
    *,
    provider: ProviderDescriptor,
    third_party_consent: bool,
    actor_context: GovernedActorContext | None,
    now: datetime,
    previous: SlotBinding | None = None,
) -> dict[str, Any]:
    """Consent stamps for a binding revision.

    CONSENT NEVER OUTLIVES THE PROVIDER IT WAS GIVEN FOR. It is recorded only
    when it means something -- a third-party provider, consented to by a named
    actor at a named time -- and it is cleared whenever a revision moves to a
    provider consent was not given for, including a first-party one. Carrying a
    stamp across a provider change would record an approval of a vendor nobody
    approved; on a slot that does not DEMAND consent, that is exactly the silent
    case where nothing else would catch it.
    """
    if not provider.third_party:
        return dict(_NO_CONSENT)
    if not third_party_consent:
        # Reachable only when the slot does not demand consent. Keep the stamp
        # if (and only if) this is the same provider it was given for; a
        # different vendor starts unconsented rather than inheriting.
        if previous is None or previous.provider_name != provider.provider_name:
            return dict(_NO_CONSENT)
        return {
            "third_party_consent": previous.third_party_consent,
            "consent_actor_id": previous.consent_actor_id,
            "consent_org_id": previous.consent_org_id,
            "consent_at": previous.consent_at,
        }
    return {
        "third_party_consent": True,
        "consent_actor_id": None if actor_context is None else actor_context.actor_id,
        "consent_org_id": None if actor_context is None else actor_context.org_id,
        "consent_at": now,
    }


def _revision_of(
    binding: SlotBinding,
    *,
    change_kind: BindingChangeKind,
    note: str | None,
    recorded_at: datetime,
) -> SlotBindingRevision:
    return SlotBindingRevision(
        binding_id=binding.binding_id,
        revision=binding.revision,
        change_kind=change_kind,
        install_id=binding.install_id,
        slot_name=binding.slot_name,
        provider_name=binding.provider_name,
        contract_in=binding.contract_in,
        contract_out=binding.contract_out,
        billing_mode=binding.billing_mode,
        third_party_consent=binding.third_party_consent,
        consent_actor_id=binding.consent_actor_id,
        consent_org_id=binding.consent_org_id,
        consent_at=binding.consent_at,
        status=binding.status,
        note=note,
        recorded_at=recorded_at,
        actor_context=binding.actor_context,
        receipt_id=binding.receipt_id,
    )


__all__ = [
    "build_near_match_report",
    "evaluate_candidate",
    "service_create_slot_binding",
    "service_list_slot_bindings",
    "service_rebind_slot",
    "service_resolve_slot_binding",
    "service_retire_slot_binding",
    "service_slot_binding_history",
]

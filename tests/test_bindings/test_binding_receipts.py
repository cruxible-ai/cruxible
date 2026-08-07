"""Every binding write is receipted, including the refusals.

A binding is a deployment decision, so the receipt is the record of who decided
what against which state. Refusals matter as much as commits here: "we tried to
bind this provider and the ledger said no" is exactly the negative experience an
incident review reaches for.
"""

from __future__ import annotations

import pytest

from cruxible_core.bindings.types import ProviderDescriptor, SlotInterface
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import (
    BindingConsentNotAttributableError,
    BindingConsentRequiredError,
    BindingContractMismatchError,
    BindingSlotInterfaceMismatchError,
    SlotAlreadyBoundError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.receipt.types import Receipt
from cruxible_core.service.bindings import (
    service_create_slot_binding,
    service_rebind_slot,
    service_retire_slot_binding,
)

INSTALL = "inst-prod-1"


def _receipt(instance: CruxibleInstance, receipt_id: str | None) -> Receipt:
    assert receipt_id is not None
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
    finally:
        store.close()
    assert receipt is not None
    return receipt


class TestCommittedReceipts:
    def test_bind_writes_a_committed_receipt_naming_the_decision(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        result = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        receipt = _receipt(instance, result.receipt_id)

        assert receipt.operation_type == "slot_binding_bind"
        assert receipt.committed is True
        assert receipt.actor_context is not None
        assert receipt.actor_context.actor_id == "agent-alpha"

        # The committed payload is shed to a digest under the default
        # ``mutation_payloads="metadata"`` retention; the decision itself is
        # recorded on the validation node, which retention never touches.
        validated = next(
            node.detail for node in receipt.nodes if node.detail and node.detail.get("binding_id")
        )
        assert validated["install_id"] == INSTALL
        assert validated["slot_name"] == "summarize"
        assert validated["provider_name"] == "summarizer-core"
        assert validated["revision"] == 1

    def test_the_receipt_id_is_stamped_onto_the_persisted_row(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        result = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        store = instance.get_bindings_store()
        try:
            stored = store.get_binding(result.binding.binding_id)
        finally:
            store.close()
        assert stored is not None
        assert stored.receipt_id == result.receipt_id

    def test_rebind_and_retire_each_mint_their_own_operation_type(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        rebound = service_rebind_slot(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider.model_copy(update={"provider_name": "summarizer-fast"}),
            actor_context=actor,
        )
        retired = service_retire_slot_binding(
            instance,
            install_id=INSTALL,
            slot_name="summarize",
            actor_context=actor,
        )

        assert _receipt(instance, rebound.receipt_id).operation_type == "slot_binding_rebind"
        assert _receipt(instance, retired.receipt_id).operation_type == "slot_binding_retire"

    def test_binding_writes_advance_the_read_revision(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        """Bindings are STATE: a read taken before a rebind must look stale."""
        before = instance.get_read_revision()
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        assert instance.get_read_revision() > before


class TestRefusalReceipts:
    def test_a_contract_mismatch_leaves_a_refusal_receipt_with_the_report(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        actor: GovernedActorContext,
    ) -> None:
        provider = ProviderDescriptor(
            provider_name="summarizer-pro",
            contract_in="doc.v1",
            contract_out="summary.v2",
            billing_mode="included",
        )
        with pytest.raises(BindingContractMismatchError) as exc_info:
            service_create_slot_binding(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=provider,
                actor_context=actor,
            )

        receipt = _receipt(instance, exc_info.value.mutation_receipt_id)
        assert receipt.operation_type == "slot_binding_bind"
        assert receipt.committed is False
        details = [node.detail for node in receipt.nodes if node.detail]
        reasons = [detail.get("reason") for detail in details]
        assert "binding_contract_mismatch" in reasons
        reported = next(
            detail["near_match_report"] for detail in details if "near_match_report" in detail
        )
        assert "contract_out mismatch" in reported

    def test_a_refused_bind_writes_no_ledger_row(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        actor: GovernedActorContext,
    ) -> None:
        provider = ProviderDescriptor(
            provider_name="vendor-summarize",
            contract_in="doc.v1",
            contract_out="summary.v1",
            billing_mode="metered",
            third_party=True,
        )
        with pytest.raises(BindingConsentRequiredError):
            service_create_slot_binding(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=provider,
                actor_context=actor,
            )
        store = instance.get_bindings_store()
        try:
            assert store.count_bindings() == 0
        finally:
            store.close()

    def test_a_redefined_interface_leaves_a_refusal_receipt_holding_both_readings(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        """The receipt must show what was asserted AND what the ledger pinned."""
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        with pytest.raises(BindingSlotInterfaceMismatchError) as exc_info:
            service_rebind_slot(
                instance,
                install_id=INSTALL,
                slot=summarize_slot.model_copy(
                    update={"allowed_billing_modes": ("included", "metered", "byo_key")}
                ),
                provider=fitting_provider.model_copy(
                    update={"provider_name": "summarizer-byok", "billing_mode": "byo_key"}
                ),
                actor_context=actor,
            )

        receipt = _receipt(instance, exc_info.value.mutation_receipt_id)
        assert receipt.operation_type == "slot_binding_rebind"
        assert receipt.committed is False
        details = [node.detail for node in receipt.nodes if node.detail]
        refusal = next(
            detail
            for detail in details
            if detail.get("reason") == "binding_slot_interface_mismatch"
        )
        assert refusal["pinned_interface"]["allowed_billing_modes"] == ["included", "metered"]
        assert refusal["asserted_interface"]["allowed_billing_modes"] == [
            "included",
            "metered",
            "byo_key",
        ]

    def test_anonymous_consent_leaves_a_refusal_receipt(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        actor: GovernedActorContext,
    ) -> None:
        provider = ProviderDescriptor(
            provider_name="vendor-summarize",
            contract_in="doc.v1",
            contract_out="summary.v1",
            billing_mode="metered",
            third_party=True,
        )
        with pytest.raises(BindingConsentNotAttributableError) as exc_info:
            service_create_slot_binding(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=provider,
                third_party_consent=True,
                actor_context=None,
            )

        receipt = _receipt(instance, exc_info.value.mutation_receipt_id)
        assert receipt.committed is False
        details = [node.detail for node in receipt.nodes if node.detail]
        assert any(detail.get("reason") == "binding_consent_not_attributable" for detail in details)

    def test_a_duplicate_bind_refusal_names_the_incumbent(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        with pytest.raises(SlotAlreadyBoundError) as exc_info:
            service_create_slot_binding(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=fitting_provider.model_copy(update={"provider_name": "other"}),
                actor_context=actor,
            )

        receipt = _receipt(instance, exc_info.value.mutation_receipt_id)
        assert receipt.committed is False
        details = [node.detail for node in receipt.nodes if node.detail]
        assert any(detail.get("reason") == "slot_already_bound" for detail in details)

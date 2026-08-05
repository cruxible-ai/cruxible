"""Bind / rebind / retire / resolve behaviour of the binding ledger service."""

from __future__ import annotations

import pytest

from cruxible_core.bindings.types import ProviderDescriptor, SlotInterface
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import (
    BindingBillingModeRefusedError,
    BindingConsentRequiredError,
    BindingContractMismatchError,
    BindingNotFoundError,
    ConfigError,
    SlotAlreadyBoundError,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.service.bindings import (
    service_create_slot_binding,
    service_list_slot_bindings,
    service_rebind_slot,
    service_resolve_slot_binding,
    service_retire_slot_binding,
    service_slot_binding_history,
)

INSTALL = "inst-prod-1"


class TestCreateBinding:
    def test_binds_a_fitting_provider(
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

        assert result.change_kind == "bind"
        assert result.binding.revision == 1
        assert result.binding.status == "active"
        assert result.binding.provider_name == "summarizer-core"
        assert result.binding.binding_id.startswith("bnd_")
        assert result.receipt_id is not None

    def test_records_the_slot_interface_not_the_provider_restatement(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        actor: GovernedActorContext,
    ) -> None:
        """The row pins what the PROCEDURE expects, which is the rebind check."""
        provider = ProviderDescriptor(
            provider_name="summarizer-core",
            contract_in="doc.v1",
            contract_out="summary.v1",
            billing_mode="metered",
        )
        result = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=provider,
            actor_context=actor,
        )
        assert result.binding.contract_in == summarize_slot.contract_in
        assert result.binding.contract_out == summarize_slot.contract_out
        assert result.binding.billing_mode == "metered"

    def test_refuses_a_second_active_binding_for_the_same_slot(
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
        other = fitting_provider.model_copy(update={"provider_name": "summarizer-alt"})

        with pytest.raises(SlotAlreadyBoundError) as exc_info:
            service_create_slot_binding(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=other,
                actor_context=actor,
            )

        assert exc_info.value.slot_name == "summarize"
        assert exc_info.value.provider_name == "summarizer-core"
        assert "use rebind" in str(exc_info.value)

    def test_the_same_slot_name_binds_independently_per_install(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        """Uniqueness is per install+slot, never per slot name globally."""
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        second = service_create_slot_binding(
            instance,
            install_id="inst-staging-1",
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        assert second.binding.install_id == "inst-staging-1"
        assert second.binding.revision == 1

    def test_refuses_blank_identifiers(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
    ) -> None:
        with pytest.raises(ConfigError, match="install_id must not be blank"):
            service_create_slot_binding(
                instance,
                install_id="   ",
                slot=summarize_slot,
                provider=fitting_provider,
                actor_context=None,
            )


class TestValidation:
    def test_refuses_a_contract_in_mismatch(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        actor: GovernedActorContext,
    ) -> None:
        provider = ProviderDescriptor(
            provider_name="doc-classifier",
            contract_in="doc.v2",
            contract_out="summary.v1",
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
        assert "contract_in mismatch" in str(exc_info.value)
        assert exc_info.value.near_matches

    def test_refuses_a_billing_mode_outside_the_allowed_set_and_echoes_it(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        actor: GovernedActorContext,
    ) -> None:
        provider = ProviderDescriptor(
            provider_name="summarizer-core",
            contract_in="doc.v1",
            contract_out="summary.v1",
            billing_mode="byo_key",
        )
        with pytest.raises(BindingBillingModeRefusedError) as exc_info:
            service_create_slot_binding(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=provider,
                actor_context=actor,
            )
        assert exc_info.value.allowed_billing_modes == ["included", "metered"]
        assert "allowed values: included, metered" in str(exc_info.value)

    def test_unconstrained_slot_accepts_any_billing_mode(
        self,
        instance: CruxibleInstance,
        actor: GovernedActorContext,
    ) -> None:
        slot = SlotInterface(
            slot_name="classify",
            contract_in="doc.v1",
            contract_out="label.v1",
        )
        provider = ProviderDescriptor(
            provider_name="anything",
            contract_in="doc.v1",
            contract_out="label.v1",
            billing_mode="whatever-the-vendor-calls-it",
        )
        result = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=slot,
            provider=provider,
            actor_context=actor,
        )
        assert result.binding.billing_mode == "whatever-the-vendor-calls-it"

    def test_slot_cannot_declare_an_empty_billing_allowlist(self) -> None:
        with pytest.raises(ValueError, match="at least one mode"):
            SlotInterface(
                slot_name="summarize",
                contract_in="doc.v1",
                contract_out="summary.v1",
                allowed_billing_modes=(),
            )


class TestThirdPartyConsent:
    def test_refuses_a_third_party_provider_without_consent(
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
        with pytest.raises(BindingConsentRequiredError) as exc_info:
            service_create_slot_binding(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=provider,
                actor_context=actor,
            )
        assert exc_info.value.provider_name == "vendor-summarize"

    def test_records_the_consenting_actor_and_timestamp(
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
        result = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=provider,
            third_party_consent=True,
            actor_context=actor,
        )
        binding = result.binding
        assert binding.third_party_consent is True
        assert binding.consent_actor_id == "agent-alpha"
        assert binding.consent_org_id == "org-acme"
        assert binding.consent_at is not None

    def test_rebinding_to_a_first_party_provider_clears_stale_consent(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        """A consent stamp must never outlive the provider it was given for."""
        third_party = ProviderDescriptor(
            provider_name="vendor-summarize",
            contract_in="doc.v1",
            contract_out="summary.v1",
            billing_mode="metered",
            third_party=True,
        )
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=third_party,
            third_party_consent=True,
            actor_context=actor,
        )
        rebound = service_rebind_slot(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        assert rebound.binding.third_party_consent is False
        assert rebound.binding.consent_actor_id is None
        assert rebound.binding.consent_at is None

    def test_consent_does_not_ride_along_to_a_different_vendor(
        self,
        instance: CruxibleInstance,
        actor: GovernedActorContext,
    ) -> None:
        """On a slot that does not DEMAND consent, nothing else would catch it."""
        slot = SlotInterface(
            slot_name="summarize",
            contract_in="doc.v1",
            contract_out="summary.v1",
        )
        first = ProviderDescriptor(
            provider_name="vendor-a",
            contract_in="doc.v1",
            contract_out="summary.v1",
            billing_mode="metered",
            third_party=True,
        )
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=slot,
            provider=first,
            third_party_consent=True,
            actor_context=actor,
        )
        rebound = service_rebind_slot(
            instance,
            install_id=INSTALL,
            slot=slot,
            provider=first.model_copy(update={"provider_name": "vendor-b"}),
            actor_context=actor,
        )
        assert rebound.binding.provider_name == "vendor-b"
        assert rebound.binding.third_party_consent is False
        assert rebound.binding.consent_actor_id is None

    def test_consent_survives_a_rebind_that_keeps_the_same_vendor(
        self,
        instance: CruxibleInstance,
        actor: GovernedActorContext,
    ) -> None:
        slot = SlotInterface(
            slot_name="summarize",
            contract_in="doc.v1",
            contract_out="summary.v1",
        )
        vendor = ProviderDescriptor(
            provider_name="vendor-a",
            contract_in="doc.v1",
            contract_out="summary.v1",
            billing_mode="metered",
            third_party=True,
        )
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=slot,
            provider=vendor,
            third_party_consent=True,
            actor_context=actor,
        )
        rebound = service_rebind_slot(
            instance,
            install_id=INSTALL,
            slot=slot,
            provider=vendor.model_copy(update={"billing_mode": "included"}),
            actor_context=actor,
        )
        assert rebound.binding.billing_mode == "included"
        assert rebound.binding.third_party_consent is True
        assert rebound.binding.consent_actor_id == "agent-alpha"


class TestRebind:
    def test_rebind_bumps_the_revision_and_keeps_the_binding_id(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        created = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        replacement = fitting_provider.model_copy(
            update={"provider_name": "summarizer-fast", "billing_mode": "metered"}
        )
        rebound = service_rebind_slot(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=replacement,
            note="cost tuning",
            actor_context=actor,
        )

        assert rebound.binding.binding_id == created.binding.binding_id
        assert rebound.binding.revision == 2
        assert rebound.change_kind == "rebind"
        assert rebound.previous_provider_name == "summarizer-core"
        assert rebound.previous_revision == 1
        assert rebound.binding.status == "active"

    def test_rebind_refuses_a_contract_incompatible_provider(
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
        incompatible = ProviderDescriptor(
            provider_name="summarizer-next",
            contract_in="doc.v1",
            contract_out="summary.v2",
            billing_mode="included",
        )
        with pytest.raises(BindingContractMismatchError):
            service_rebind_slot(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=incompatible,
                actor_context=actor,
            )

        still = service_resolve_slot_binding(instance, install_id=INSTALL, slot_name="summarize")
        assert still.provider_name == "summarizer-core"
        assert still.revision == 1

    def test_rebind_refuses_when_the_slot_was_never_bound(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        with pytest.raises(BindingNotFoundError):
            service_rebind_slot(
                instance,
                install_id=INSTALL,
                slot=summarize_slot,
                provider=fitting_provider,
                actor_context=actor,
            )


class TestRetire:
    def test_retire_leaves_the_slot_unbound_and_rebindable(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        created = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        retired = service_retire_slot_binding(
            instance,
            install_id=INSTALL,
            slot_name="summarize",
            note="decommissioned",
            actor_context=actor,
        )
        assert retired.binding.status == "retired"
        assert retired.binding.revision == 2
        assert retired.binding.retired_at is not None

        with pytest.raises(BindingNotFoundError):
            service_resolve_slot_binding(instance, install_id=INSTALL, slot_name="summarize")

        rebound = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        assert rebound.binding.binding_id != created.binding.binding_id
        assert rebound.binding.revision == 1

    def test_retire_refuses_an_unbound_slot(
        self,
        instance: CruxibleInstance,
        actor: GovernedActorContext,
    ) -> None:
        with pytest.raises(BindingNotFoundError):
            service_retire_slot_binding(
                instance,
                install_id=INSTALL,
                slot_name="summarize",
                actor_context=actor,
            )


class TestResolve:
    def test_resolve_returns_the_active_binding(
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
        resolved = service_resolve_slot_binding(instance, install_id=INSTALL, slot_name="summarize")
        assert resolved.provider_name == "summarizer-core"
        assert resolved.status == "active"

    def test_resolve_names_the_slot_it_could_not_resolve(
        self,
        instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(BindingNotFoundError) as exc_info:
            service_resolve_slot_binding(instance, install_id=INSTALL, slot_name="summarize")
        assert exc_info.value.slot_name == "summarize"
        assert exc_info.value.install_id == INSTALL
        assert "bind the slot to a provider" in str(exc_info.value)

    def test_resolve_after_rebind_returns_the_new_provider(
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
        service_rebind_slot(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider.model_copy(update={"provider_name": "summarizer-fast"}),
            actor_context=actor,
        )
        resolved = service_resolve_slot_binding(instance, install_id=INSTALL, slot_name="summarize")
        assert resolved.provider_name == "summarizer-fast"
        assert resolved.revision == 2


class TestListAndHistory:
    def test_history_retains_every_revision(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        created = service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider,
            actor_context=actor,
        )
        service_rebind_slot(
            instance,
            install_id=INSTALL,
            slot=summarize_slot,
            provider=fitting_provider.model_copy(update={"provider_name": "summarizer-fast"}),
            note="cost tuning",
            actor_context=actor,
        )
        service_retire_slot_binding(
            instance,
            install_id=INSTALL,
            slot_name="summarize",
            actor_context=actor,
        )

        history = service_slot_binding_history(instance, binding_id=created.binding.binding_id)
        assert history.total == 3
        assert [row.revision for row in history.items] == [1, 2, 3]
        assert [row.change_kind for row in history.items] == ["bind", "rebind", "retire"]
        assert [row.provider_name for row in history.items] == [
            "summarizer-core",
            "summarizer-fast",
            "summarizer-fast",
        ]
        assert history.items[1].note == "cost tuning"
        assert history.items[2].status == "retired"
        assert len({row.receipt_id for row in history.items}) == 3

    def test_history_refuses_an_unknown_binding(self, instance: CruxibleInstance) -> None:
        with pytest.raises(BindingNotFoundError) as exc_info:
            service_slot_binding_history(instance, binding_id="bnd_missing")
        assert exc_info.value.binding_id == "bnd_missing"

    def test_list_filters_and_carries_the_standard_envelope(
        self,
        instance: CruxibleInstance,
        summarize_slot: SlotInterface,
        fitting_provider: ProviderDescriptor,
        actor: GovernedActorContext,
    ) -> None:
        classify_slot = SlotInterface(
            slot_name="classify",
            contract_in="doc.v1",
            contract_out="label.v1",
        )
        classify_provider = ProviderDescriptor(
            provider_name="classifier-core",
            contract_in="doc.v1",
            contract_out="label.v1",
            billing_mode="included",
        )
        for install in (INSTALL, "inst-staging-1"):
            service_create_slot_binding(
                instance,
                install_id=install,
                slot=summarize_slot,
                provider=fitting_provider,
                actor_context=actor,
            )
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=classify_slot,
            provider=classify_provider,
            actor_context=actor,
        )
        service_retire_slot_binding(
            instance,
            install_id="inst-staging-1",
            slot_name="summarize",
            actor_context=actor,
        )

        everything = service_list_slot_bindings(instance)
        assert everything.total == 3
        assert everything.truncated is False
        assert everything.read_revision is not None

        active = service_list_slot_bindings(instance, status="active")
        assert active.total == 2
        assert {row.install_id for row in active.items} == {INSTALL}

        per_install = service_list_slot_bindings(instance, install_id=INSTALL)
        assert [row.slot_name for row in per_install.items] == ["classify", "summarize"]

        page = service_list_slot_bindings(instance, install_id=INSTALL, limit=1)
        assert page.total == 2
        assert len(page.items) == 1
        assert page.truncated is True

    def test_list_rejects_an_unknown_status(self, instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="status must be one of"):
            service_list_slot_bindings(instance, status="paused")  # type: ignore[arg-type]

    def test_list_rejects_a_negative_offset(self, instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="offset must be >= 0"):
            service_list_slot_bindings(instance, offset=-1)

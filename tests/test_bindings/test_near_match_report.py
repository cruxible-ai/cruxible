"""Near-match reporting for an unbindable slot.

An unbindable slot must not just say "no". The RFC requires it to list the
candidates that nearly matched and name why each one failed, so the operator
picks a provider on the next action rather than after a round trip per reason.
"""

from __future__ import annotations

import pytest

from cruxible_core.bindings.types import ProviderDescriptor, SlotInterface
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import BindingContractMismatchError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.service.bindings import (
    build_near_match_report,
    evaluate_candidate,
    service_create_slot_binding,
)

INSTALL = "inst-prod-1"

SLOT = SlotInterface(
    slot_name="summarize",
    contract_in="doc.v1",
    contract_out="summary.v1",
    allowed_billing_modes=("included",),
)

# Ordered worst-to-best on purpose: the report must re-rank them.
CANDIDATES = (
    ProviderDescriptor(
        provider_name="doc-classifier",
        contract_in="doc.v2",
        contract_out="label.v1",
        billing_mode="included",
    ),
    ProviderDescriptor(
        provider_name="legacy-summarizer",
        contract_in="doc.v1",
        contract_out="summary.v0",
        billing_mode="metered",
    ),
    ProviderDescriptor(
        provider_name="summarizer-pro",
        contract_in="doc.v1",
        contract_out="summary.v2",
        billing_mode="included",
    ),
)

EXPECTED_REPORT = """\
no provider satisfies slot 'summarize' (contract_in='doc.v1', contract_out='summary.v1'); \
3 candidate(s) nearly matched:
  1. 'summarizer-pro' [1/2 contract sides matched]: contract_out mismatch \
(declares 'summary.v2', slot requires 'summary.v1')
  2. 'legacy-summarizer' [1/2 contract sides matched]: contract_out mismatch \
(declares 'summary.v0', slot requires 'summary.v1'); billing_mode 'metered' not in \
the slot's allowed set [included]
  3. 'doc-classifier' [0/2 contract sides matched]: contract_in mismatch \
(declares 'doc.v2', slot requires 'doc.v1'); contract_out mismatch \
(declares 'label.v1', slot requires 'summary.v1')"""


def test_report_ranks_and_explains_every_candidate() -> None:
    report = build_near_match_report(
        SLOT,
        requested=CANDIDATES[2],
        candidates=CANDIDATES,
    )
    assert report.render() == EXPECTED_REPORT


def test_ranking_is_independent_of_submission_order() -> None:
    forwards = build_near_match_report(SLOT, requested=CANDIDATES[0], candidates=CANDIDATES)
    backwards = build_near_match_report(
        SLOT, requested=CANDIDATES[0], candidates=tuple(reversed(CANDIDATES))
    )
    assert forwards.render() == backwards.render()


def test_the_requested_provider_is_always_reported() -> None:
    """Even when the caller offers no candidate list at all."""
    report = build_near_match_report(SLOT, requested=CANDIDATES[0])
    assert [candidate.provider_name for candidate in report.candidates] == ["doc-classifier"]
    assert "1 candidate(s) nearly matched" in report.render()


def test_providers_that_satisfy_the_slot_are_left_out_of_the_report() -> None:
    """The report answers 'why can nothing bind', not 'here is everyone'."""
    fitting = ProviderDescriptor(
        provider_name="summarizer-core",
        contract_in="doc.v1",
        contract_out="summary.v1",
        billing_mode="included",
    )
    report = build_near_match_report(
        SLOT, requested=CANDIDATES[0], candidates=(*CANDIDATES, fitting)
    )
    assert "summarizer-core" not in report.render()


def test_evaluate_candidate_collects_every_reason_not_just_the_first() -> None:
    outcome = evaluate_candidate(
        SLOT,
        ProviderDescriptor(
            provider_name="worst-case",
            contract_in="doc.v9",
            contract_out="summary.v9",
            billing_mode="byo_key",
        ),
    )
    assert len(outcome.mismatches) == 3
    assert outcome.matched_sides == 0


def test_third_party_consent_is_not_a_near_match_failure() -> None:
    """Consent is an operator act, so a consent-pending provider still fits."""
    slot = SLOT.model_copy(update={"requires_third_party_consent": True})
    vendor = ProviderDescriptor(
        provider_name="vendor-summarize",
        contract_in="doc.v1",
        contract_out="summary.v1",
        billing_mode="included",
        third_party=True,
    )
    assert evaluate_candidate(slot, vendor).mismatches == ()


def test_refusal_carries_the_report_text_and_machine_readable_matches(
    instance: CruxibleInstance,
    actor: GovernedActorContext,
) -> None:
    with pytest.raises(BindingContractMismatchError) as exc_info:
        service_create_slot_binding(
            instance,
            install_id=INSTALL,
            slot=SLOT,
            provider=CANDIDATES[2],
            candidates=CANDIDATES,
            actor_context=actor,
        )

    error = exc_info.value
    assert error.report_text == EXPECTED_REPORT
    assert str(error).startswith("no provider satisfies slot 'summarize'")
    assert [match["provider_name"] for match in error.near_matches] == [
        "summarizer-pro",
        "legacy-summarizer",
        "doc-classifier",
    ]
    assert error.near_matches[0]["matched_contract_in"] is True
    assert error.near_matches[0]["matched_contract_out"] is False


def test_report_says_so_when_no_candidate_was_offered() -> None:
    report = build_near_match_report(
        SLOT,
        requested=ProviderDescriptor(
            provider_name="summarizer-core",
            contract_in="doc.v1",
            contract_out="summary.v1",
            billing_mode="included",
        ),
    )
    assert report.render().endswith("; no candidate providers were offered")

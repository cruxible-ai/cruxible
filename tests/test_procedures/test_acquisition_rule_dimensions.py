"""F05: Line acquisition carries the policy dimensions the removed
SourceAcquisitionPolicy had -- on_stale, on_oversized and
permitted_replayability -- without reintroducing that noun.

Before this fix, AcquisitionDefault/AcquisitionRule (extra="forbid") rejected
these fields outright, a stale or oversized acquisition always refused
(no omit choice), and nothing checked a Source's Capture against a
permitted replay class at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionDefault,
    AcquisitionRule,
    resolve_acquisition_rules,
)
from cruxible_client.contracts.source_references import ExternalSourceReference
from cruxible_core.procedures.acquisition import (
    ACQUISITION_OVERSIZED,
    ACQUISITION_REPLAYABILITY_INELIGIBLE,
    ACQUISITION_STALE,
    ProcedureSourceAcquisitionResultV1,
    apply_acquisition_result,
    capture_selection_failure,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _external_source(replayability: str) -> ExternalSourceReference:
    return ExternalSourceReference(
        source_identity="demo.fetch",
        producer_binding_digest="sha256:" + "0" * 64,
        coordinate_type="cruxible.web_fetch.coordinate",
        coordinate={"path": "demo"},
        selector_type="cruxible.web_fetch.selector",
        selector={"path": "demo"},
        replayability=replayability,  # type: ignore[arg-type]
    )


def test_acquisition_default_and_rule_declare_the_removed_policy_dimensions():
    """Before this fix these were rejected: extra="forbid" had no such fields."""

    default = AcquisitionDefault(
        on_stale="omit",
        on_oversized="omit",
        permitted_replayability=("exact",),
    )
    assert default.on_stale == "omit"
    assert default.on_oversized == "omit"
    assert default.permitted_replayability == ("exact",)

    rule = AcquisitionRule(
        input_name="doc",
        on_stale="omit",
        on_oversized="omit",
        permitted_replayability=("attested_only", "exact"),
    )
    assert rule.on_stale == "omit"
    assert rule.on_oversized == "omit"
    # Declared unsorted; the model normalizes.
    assert rule.permitted_replayability == ("attested_only", "exact")


def test_an_undeclared_permitted_replayability_must_not_be_empty():
    with pytest.raises(ValidationError):
        AcquisitionDefault(permitted_replayability=())


def test_resolve_acquisition_rules_carries_the_new_dimensions_from_defaults():
    rules = resolve_acquisition_rules(
        ("doc",),
        defaults={
            "doc": AcquisitionDefault(
                on_stale="omit", on_oversized="omit", permitted_replayability=("exact",)
            )
        },
        overrides={},
    )
    assert rules["doc"].on_stale == "omit"
    assert rules["doc"].on_oversized == "omit"
    assert rules["doc"].permitted_replayability == ("exact",)


def _result(outcome: str) -> ProcedureSourceAcquisitionResultV1:
    reason = {"stale": ACQUISITION_STALE, "oversized": ACQUISITION_OVERSIZED}[outcome]
    return ProcedureSourceAcquisitionResultV1(
        node_id="doc",
        input_name="doc",
        outcome=outcome,  # type: ignore[arg-type]
        reason_code=reason,
        detail="probe",
    )


def test_a_stale_result_is_omitted_only_when_the_rule_says_so():
    refusing = AcquisitionRule(input_name="doc")
    omitting = AcquisitionRule(input_name="doc", on_stale="omit")
    assert (
        apply_acquisition_result(refusing, _result("stale"), evaluation_time=NOW).disposition
        == "refused"
    )
    assert (
        apply_acquisition_result(omitting, _result("stale"), evaluation_time=NOW).disposition
        == "omitted"
    )


def test_an_oversized_result_is_omitted_only_when_the_rule_says_so():
    refusing = AcquisitionRule(input_name="doc")
    omitting = AcquisitionRule(input_name="doc", on_oversized="omit")
    assert (
        apply_acquisition_result(refusing, _result("oversized"), evaluation_time=NOW).disposition
        == "refused"
    )
    assert (
        apply_acquisition_result(omitting, _result("oversized"), evaluation_time=NOW).disposition
        == "omitted"
    )


def test_permitted_replayability_is_a_hard_eligibility_gate_not_an_omit_choice():
    """The old replayability eligibility check (F05): a Source whose Capture
    replays outside the declared set is ineligible, and no on_* choice
    about it exists -- it never omits, it is simply never selected."""

    rule = AcquisitionRule(input_name="doc", permitted_replayability=("exact",))
    eligible_envelope = SimpleNamespace(observed_at=NOW, source=_external_source("exact"))
    ineligible_envelope = SimpleNamespace(observed_at=NOW, source=_external_source("attested_only"))

    assert capture_selection_failure(rule, eligible_envelope, evaluation_time=NOW) is None
    assert (
        capture_selection_failure(rule, ineligible_envelope, evaluation_time=NOW)
        == ACQUISITION_REPLAYABILITY_INELIGIBLE
    )


def test_an_unrestricted_rule_permits_every_replay_class():
    rule = AcquisitionRule(input_name="doc")
    envelope = SimpleNamespace(observed_at=NOW, source=_external_source("attested_only"))
    assert capture_selection_failure(rule, envelope, evaluation_time=NOW) is None

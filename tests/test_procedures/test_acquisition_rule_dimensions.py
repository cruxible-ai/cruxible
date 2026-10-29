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
    disallowed_origin,
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


# --- F05 fix round 2: acquisition.origins (§4.1, §4.4) -----------------------


def test_acquisition_default_and_rule_declare_origins():
    """Codex's reproduction: ``AcquisitionDefault.model_validate({'origins':
    [...]})`` was rejected outright (extra="forbid" had no such field)."""

    default = AcquisitionDefault.model_validate({"origins": ["https://www.cisa.gov"]})
    assert default.origins == ("https://www.cisa.gov",)

    rule = AcquisitionRule(input_name="doc", origins=("https://b.example", "https://a.example"))
    # Declared unsorted; the model normalizes, like permitted_replayability.
    assert rule.origins == ("https://a.example", "https://b.example")


def test_an_undeclared_origins_must_not_be_empty():
    with pytest.raises(ValidationError):
        AcquisitionDefault(origins=())


def test_an_origin_without_a_scheme_or_host_is_refused():
    with pytest.raises(ValidationError):
        AcquisitionDefault(origins=("www.cisa.gov",))
    with pytest.raises(ValidationError):
        AcquisitionDefault(origins=("https://www.cisa.gov/path",))


def test_resolve_acquisition_rules_carries_origins_from_defaults_and_overrides():
    rules = resolve_acquisition_rules(
        ("doc",),
        defaults={"doc": AcquisitionDefault(origins=("https://www.cisa.gov",))},
        overrides={},
    )
    assert rules["doc"].origins == ("https://www.cisa.gov",)

    overridden = resolve_acquisition_rules(
        ("doc",),
        defaults={"doc": AcquisitionDefault(origins=("https://www.cisa.gov",))},
        overrides={"doc": AcquisitionDefault(origins=("https://override.example",))},
    )
    assert overridden["doc"].origins == ("https://override.example",)


def test_disallowed_origin_is_a_hard_eligibility_gate_on_url_and_final_url_fields():
    """F05/§4.1,§4.4: core checks every field named ``url``/``final_url``,
    at any depth, never an omit choice -- like permitted_replayability."""

    rule = AcquisitionRule(input_name="doc", origins=("https://permitted.example",))
    assert disallowed_origin(rule, {"url": "https://permitted.example/feed.json"}) is None
    assert (
        disallowed_origin(rule, {"url": "https://evil.example/feed.json"})
        == "https://evil.example/feed.json"
    )
    # Reached at any depth, and under the "final_url" spelling too.
    assert (
        disallowed_origin(rule, {"retrieved": {"final_url": "https://evil.example/x"}})
        == "https://evil.example/x"
    )
    # A list of records is walked too.
    assert disallowed_origin(rule, [{"url": "https://permitted.example/a"}]) is None
    assert disallowed_origin(rule, [{"url": "https://evil.example/a"}]) == "https://evil.example/a"
    # Fields spelled otherwise are never origin-checked.
    assert disallowed_origin(rule, {"homepage": "https://evil.example"}) is None


def test_an_unrestricted_rule_permits_any_origin():
    rule = AcquisitionRule(input_name="doc")
    assert disallowed_origin(rule, {"url": "https://anywhere.example/x"}) is None

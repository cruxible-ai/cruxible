"""Lowering a blueprint into overlay fragment + ProcedureDefinitions."""

from __future__ import annotations

import pytest

from cruxible_core.blueprint import (
    BlueprintBindingError,
    BlueprintUnsupportedError,
    BlueprintValidationError,
    ProviderCandidate,
    lower_blueprint,
    parse_blueprint,
)
from cruxible_core.procedure.types import ProcedureDefinition

SCORER = ProviderCandidate(
    name="widget_scorer",
    contract_in="acme.widget-triage.ScoreInput",
    contract_out="acme.widget-triage.ScoreResult",
    billing=["platform"],
    capabilities=["deterministic"],
)


def _lower(document, **kwargs):
    kwargs.setdefault("bindings", {"scorer": "widget_scorer"})
    kwargs.setdefault("candidates", [SCORER])
    return lower_blueprint(parse_blueprint(document), **kwargs)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_lowering_resolves_slot_references_to_config_names(document):
    lowered = _lower(document, candidates=[SCORER])

    (procedure,) = lowered.procedures
    assert isinstance(procedure, ProcedureDefinition)
    assert procedure.steps[0].query == "acme__widget_triage__subject_rows"
    assert procedure.steps[1].provider == "widget_scorer"
    assert procedure.referenced_providers() == {"widget_scorer"}


def test_lowering_preserves_the_rest_of_the_definition(document):
    lowered = _lower(document)

    (procedure,) = lowered.procedures
    original = parse_blueprint(document).procedures[0].definition
    assert procedure.name == original.name
    assert procedure.returns == original.returns
    assert procedure.budget == original.budget
    assert procedure.contract_in == original.contract_in
    assert procedure.declared_tier == original.declared_tier
    assert procedure.static_expansion() == original.static_expansion()


def test_overlay_carries_contracts_and_installed_named_queries(document):
    lowered = _lower(document)

    assert set(lowered.overlay.contracts) == {
        "acme.widget-triage.ScopeInput",
        "acme.widget-triage.QueryEnvelope",
        "acme.widget-triage.ScoreInput",
        "acme.widget-triage.ScoreResult",
    }
    assert set(lowered.overlay.named_queries) == {"acme__widget_triage__subject_rows"}
    assert lowered.query_slot_installs == {"subject_rows": "acme__widget_triage__subject_rows"}


def test_overlay_serializes_to_a_composable_config_mapping(document):
    fragment = _lower(document).overlay.as_config_dict()

    assert set(fragment) == {"contracts", "named_queries"}
    subject_id = fragment["contracts"]["acme.widget-triage.ScopeInput"]["fields"]["subject_id"]
    assert subject_id["type"] == "string"
    assert fragment["named_queries"]["acme__widget_triage__subject_rows"]["mode"] == "collection"


def test_lowering_records_the_resolved_bindings_and_coordinate(document):
    lowered = _lower(document, candidates=[SCORER], digest="sha256:abc")

    assert lowered.coordinate == "acme/widget-triage@1.0.0"
    assert lowered.digest == "sha256:abc"
    (binding,) = lowered.slot_bindings
    assert binding.slot == "scorer"
    assert binding.provider == "widget_scorer"
    assert binding.contract_in == "acme.widget-triage.ScoreInput"


def test_lowering_rewrites_providers_inside_repeat_steps(document):
    document["procedures"][0]["steps"][1] = {
        "id": "score",
        "as": "score",
        "repeat": {
            "max_attempts": 2,
            "until": {
                "left": "$steps.attempt.done",
                "op": "eq",
                "right": True,
                "message": "scoring did not settle",
            },
            "steps": [
                {
                    "id": "attempt",
                    "provider": "scorer",
                    "input": {"rows": "$steps.rows.results"},
                    "as": "attempt",
                }
            ],
        },
    }
    document["procedures"][0]["budget"]["max_provider_calls"] = 2

    lowered = _lower(document)

    assert lowered.procedures[0].referenced_providers() == {"widget_scorer"}


def test_inline_query_bodies_are_left_alone(document):
    document["procedures"][0]["steps"][0] = {
        "id": "rows",
        "query": {"mode": "collection", "returns": "Widget", "result_shape": "entity"},
        "as": "rows",
    }

    lowered = _lower(document)

    assert lowered.procedures[0].steps[0].query is not None
    assert not isinstance(lowered.procedures[0].steps[0].query, str)


# ---------------------------------------------------------------------------
# Binding refusals
# ---------------------------------------------------------------------------


def test_unbound_required_slot_lists_exact_near_matches(document):
    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, bindings={}, candidates=[SCORER])

    error = excinfo.value
    assert error.slot == "scorer"
    assert error.near_match_names == ["widget_scorer"]
    assert "contracts match exactly" in str(error)
    assert "acme.widget-triage.ScoreInput" in str(error)


def test_unbound_slot_ranks_exact_then_input_then_output_matches(document):
    input_only = ProviderCandidate(
        name="a_input_only",
        contract_in="acme.widget-triage.ScoreInput",
        contract_out="acme.widget-triage.QueryEnvelope",
    )
    output_only = ProviderCandidate(
        name="a_output_only",
        contract_in="acme.widget-triage.QueryEnvelope",
        contract_out="acme.widget-triage.ScoreResult",
    )

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, bindings={}, candidates=[output_only, input_only, SCORER])

    assert excinfo.value.near_match_names == ["widget_scorer", "a_input_only", "a_output_only"]


def test_unrelated_candidates_are_not_reported_as_near_matches(document):
    unrelated = ProviderCandidate(
        name="pdf_to_text",
        contract_in="cruxible.EmptyInput",
        contract_out="cruxible.JsonItems",
    )

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, bindings={}, candidates=[unrelated])

    error = excinfo.value
    assert error.near_match_names == []
    assert "No candidate provider declared either contract" in str(error)
    assert "bindings={'scorer': '<provider name>'}" in str(error)


def test_bound_provider_with_mismatched_contracts_refused(document):
    wrong = ProviderCandidate(
        name="widget_scorer",
        contract_in="acme.widget-triage.QueryEnvelope",
        contract_out="acme.widget-triage.QueryEnvelope",
    )

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, candidates=[wrong])

    message = str(excinfo.value)
    assert "does not satisfy the slot interface" in message
    assert "contract_in is 'acme.widget-triage.QueryEnvelope'" in message
    assert "contract_out is 'acme.widget-triage.QueryEnvelope'" in message


def test_binding_a_provider_absent_from_an_empty_catalog_refused(document):
    # The failure the reviewer reproduced: a provider nobody offered used to
    # sail through and be reported as satisfying the slot's own contracts.
    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, bindings={"scorer": "totally_incompatible_provider"}, candidates=[])

    error = excinfo.value
    assert error.slot == "scorer"
    assert error.paths == ["bindings.scorer"]
    message = str(error)
    assert "is not among the 0 candidate provider(s) offered" in message
    assert "will not certify a provider it has never seen" in message
    assert "candidates=[] was passed" in message


def test_binding_a_provider_absent_from_a_catalog_names_what_was_offered(document):
    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, bindings={"scorer": "ghost_scorer"}, candidates=[SCORER])

    error = excinfo.value
    assert error.paths == ["bindings.scorer"]
    assert error.issues[0].expected == "one of: widget_scorer"
    assert "is not among the 1 candidate provider(s) offered" in str(error)
    # The catalog it *was* given still gets mined for the suggestion.
    assert error.near_match_names == ["widget_scorer"]


def test_bound_provider_with_incompatible_billing_refused(document):
    document["slots"]["scorer"]["billing"] = ["byok"]
    platform_only = ProviderCandidate(
        name="widget_scorer",
        contract_in="acme.widget-triage.ScoreInput",
        contract_out="acme.widget-triage.ScoreResult",
        billing=["platform"],
        capabilities=["deterministic"],
    )

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, candidates=[platform_only])

    error = excinfo.value
    assert error.paths == ["slots.scorer.billing"]
    assert error.issues[0].expected == "at least one of: byok"
    assert "billing modes ['platform'] do not intersect the slot's ['byok']" in str(error)


def test_bound_provider_declaring_no_billing_mode_refused(document):
    silent = ProviderCandidate(
        name="widget_scorer",
        contract_in="acme.widget-triage.ScoreInput",
        contract_out="acme.widget-triage.ScoreResult",
        capabilities=["deterministic"],
    )

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, candidates=[silent])

    expected = excinfo.value.issues[0].expected
    assert expected is not None
    assert "an undeclared billing mode is not a wildcard" in expected


def test_bound_provider_missing_a_required_capability_refused(document):
    document["slots"]["scorer"]["capabilities"] = ["deterministic", "no_side_effects"]
    partial = ProviderCandidate(
        name="widget_scorer",
        contract_in="acme.widget-triage.ScoreInput",
        contract_out="acme.widget-triage.ScoreResult",
        billing=["platform"],
        capabilities=["deterministic"],
    )

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, candidates=[partial])

    error = excinfo.value
    assert error.paths == ["slots.scorer.capabilities"]
    assert error.issues[0].expected == "every tag in: deterministic, no_side_effects"
    assert "does not claim ['no_side_effects']" in str(error)


def test_every_violated_constraint_gets_its_own_issue(document):
    document["slots"]["scorer"]["billing"] = ["byok"]
    document["slots"]["scorer"]["capabilities"] = ["deterministic", "no_side_effects"]
    wrong_everywhere = ProviderCandidate(
        name="widget_scorer",
        contract_in="acme.widget-triage.QueryEnvelope",
        contract_out="acme.widget-triage.QueryEnvelope",
        billing=["platform"],
    )

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, candidates=[wrong_everywhere])

    assert excinfo.value.paths == [
        "slots.scorer.contract_in",
        "slots.scorer.contract_out",
        "slots.scorer.billing",
        "slots.scorer.capabilities",
    ]


def test_an_exact_contract_match_that_fails_billing_is_not_advertised_as_bindable(document):
    document["slots"]["scorer"]["billing"] = ["byok"]
    platform_only = ProviderCandidate(
        name="platform_scorer",
        contract_in="acme.widget-triage.ScoreInput",
        contract_out="acme.widget-triage.ScoreResult",
        billing=["platform"],
        capabilities=["deterministic"],
    )

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, bindings={}, candidates=[platform_only])

    message = str(excinfo.value)
    assert "contracts match exactly, but billing modes ['platform']" in message
    assert "bind it explicitly" not in message


def test_binding_an_undeclared_slot_refused(document):
    with pytest.raises(BlueprintValidationError) as excinfo:
        _lower(document, bindings={"scorer": "widget_scorer", "forecaster": "x"})

    assert excinfo.value.paths == ["bindings.forecaster"]
    issue = excinfo.value.issues[0]
    assert issue.expected is not None
    assert "scorer" in issue.expected


def test_binding_an_empty_provider_name_refused(document):
    with pytest.raises(BlueprintValidationError) as excinfo:
        _lower(document, bindings={"scorer": "  "})

    assert excinfo.value.paths == ["bindings.scorer"]


def test_optional_unreferenced_slot_may_stay_unbound(document):
    document["slots"]["notifier"] = {
        "contract_in": "acme.widget-triage.ScoreResult",
        "contract_out": "acme.widget-triage.QueryEnvelope",
        "billing": ["platform"],
        "required": False,
    }

    lowered = _lower(document)

    assert [binding.slot for binding in lowered.slot_bindings] == ["scorer"]


def test_optional_slot_referenced_by_a_step_must_still_be_bound(document):
    document["slots"]["scorer"]["required"] = False

    with pytest.raises(BlueprintBindingError) as excinfo:
        _lower(document, bindings={})

    assert "a procedure step references the slot" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Not-yet-supported refusals
# ---------------------------------------------------------------------------


def _as_pipeline(document):
    document["pipelines"] = [{**document["procedures"][0], "name": "widget_ingest"}]
    document.pop("procedures")
    return document


def test_triggers_refuse_to_lower_and_name_the_work_item(document):
    document = _as_pipeline(document)
    document["triggers"] = {
        "scan_uploaded": {
            "kind": "artifact",
            "accepts": ["application/pdf"],
            "pipeline": "widget_ingest",
        }
    }

    with pytest.raises(BlueprintUnsupportedError) as excinfo:
        _lower(document)

    error = excinfo.value
    assert error.feature == "triggers"
    assert error.work_item == "wi-034"
    assert "no trigger, webhook, or schedule surface exists in core" in str(error).lower()


def test_pipelines_refuse_to_lower(document):
    with pytest.raises(BlueprintUnsupportedError) as excinfo:
        _lower(_as_pipeline(document))

    assert excinfo.value.feature == "pipelines"
    assert "widget_ingest" in str(excinfo.value)


def test_document_level_triggered_invocation_refuses_to_lower(document):
    document["invocation"] = "triggered"

    with pytest.raises(BlueprintUnsupportedError) as excinfo:
        _lower(document)

    assert excinfo.value.feature == "invocation: triggered"


def test_procedure_level_triggered_invocation_refuses_to_lower(document):
    document["procedures"][0]["invocation"] = "triggered"

    with pytest.raises(BlueprintUnsupportedError) as excinfo:
        _lower(document)

    assert "widget_score" in str(excinfo.value)


def test_procedure_level_manual_overrides_a_triggered_document(document):
    document["invocation"] = "triggered"
    document["procedures"][0]["invocation"] = "manual"

    with pytest.raises(BlueprintUnsupportedError) as excinfo:
        _lower(document)

    # The document-level mode is refused first: a triggered document has no
    # manual entry point to lower onto even if one procedure opts back in.
    assert excinfo.value.feature == "invocation: triggered"

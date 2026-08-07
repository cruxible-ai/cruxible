"""Blueprint document validation: shapes, namespacing, and cross-references."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.blueprint import (
    BlueprintValidationError,
    parse_blueprint,
)
from cruxible_core.blueprint.schema import KNOWN_OUTCOME_METRICS


def _refuse(document: dict[str, Any]) -> BlueprintValidationError:
    with pytest.raises(BlueprintValidationError) as excinfo:
        parse_blueprint(document)
    return excinfo.value


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_minimal_document_parses(document):
    blueprint = parse_blueprint(document)

    assert blueprint.coordinate == "acme/widget-triage@1.0.0"
    assert blueprint.invocation == "manual"
    assert blueprint.procedure_invocation(blueprint.procedures[0]) == "manual"
    assert blueprint.query_slots["subject_rows"].installed_name("subject_rows") == (
        "acme__widget_triage__subject_rows"
    )
    assert blueprint.blueprint.contract_namespace == "acme.widget-triage."


def test_query_slot_without_install_as_installs_under_its_own_name(document):
    del document["query_slots"]["subject_rows"]["install_as"]

    blueprint = parse_blueprint(document)

    assert blueprint.query_slots["subject_rows"].installed_name("subject_rows") == "subject_rows"


def test_builtin_contract_references_resolve(document):
    document["query_slots"]["subject_rows"]["param_contract"] = "cruxible.EmptyInput"
    document["procedures"][0]["steps"][0].pop("params")

    blueprint = parse_blueprint(document)

    assert blueprint.query_slots["subject_rows"].param_contract == "cruxible.EmptyInput"


def test_document_must_be_a_mapping():
    error = _refuse(["not", "a", "mapping"])  # type: ignore[arg-type]

    assert "must be a mapping" in str(error)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "2026.30", "1.0.0.1"])
def test_non_semver_version_refused(document, version):
    document["blueprint"]["version"] = version

    error = _refuse(document)

    assert error.paths == ["blueprint.version"]
    assert "semver" in str(error)


def test_float_version_refused_with_quoting_guidance(document):
    document["blueprint"]["version"] = 1.0

    error = _refuse(document)

    assert error.paths == ["blueprint.version"]


@pytest.mark.parametrize("identity", ["Widget Triage", "-leading", "widget/triage"])
def test_non_catalog_identity_refused(document, identity):
    document["blueprint"]["id"] = identity

    error = _refuse(document)

    assert error.paths == ["blueprint.id"]


def test_unknown_top_level_key_refused(document):
    document["deployment"] = {"payer": "acme"}

    error = _refuse(document)

    assert error.paths == ["deployment"]
    assert "forbid unknown fields" in str(error)


def test_provenance_evidence_requires_a_ref_grammar(document):
    document["blueprint"]["provenance"]["evidence"] = ["some receipt"]

    error = _refuse(document)

    assert error.paths == ["blueprint.provenance"]
    assert "<kind>:<value>" in str(error)


def test_provenance_evidence_accepts_known_kinds(document):
    document["blueprint"]["provenance"]["evidence"] = ["receipt:RCP-1", "eval:EVL-2"]

    blueprint = parse_blueprint(document)

    assert blueprint.blueprint.provenance is not None
    assert blueprint.blueprint.provenance.evidence == ["receipt:RCP-1", "eval:EVL-2"]


# ---------------------------------------------------------------------------
# Contract namespacing (RFC §10.1)
# ---------------------------------------------------------------------------


def test_unqualified_contract_name_refused(document):
    document["contracts"]["ScopeInput"] = document["contracts"].pop("acme.widget-triage.ScopeInput")
    document["query_slots"]["subject_rows"]["param_contract"] = "ScopeInput"
    document["procedures"][0]["contract_in"] = "ScopeInput"

    error = _refuse(document)

    assert "contracts.ScopeInput" in error.paths
    issue = next(item for item in error.issues if item.path == "contracts.ScopeInput")
    assert issue.expected is not None
    assert "acme.widget-triage.<LocalName>" in issue.expected


def test_contract_qualified_to_another_publisher_refused(document):
    document["contracts"]["other.widget-triage.ScopeInput"] = document["contracts"].pop(
        "acme.widget-triage.ScopeInput"
    )
    document["query_slots"]["subject_rows"]["param_contract"] = "other.widget-triage.ScopeInput"
    document["procedures"][0]["contract_in"] = "other.widget-triage.ScopeInput"

    error = _refuse(document)

    assert "contracts.other.widget-triage.ScopeInput" in error.paths


def test_contract_local_segment_must_be_one_segment(document):
    document["contracts"]["acme.widget-triage.scope.Input"] = document["contracts"].pop(
        "acme.widget-triage.ScopeInput"
    )
    document["query_slots"]["subject_rows"]["param_contract"] = "acme.widget-triage.scope.Input"
    document["procedures"][0]["contract_in"] = "acme.widget-triage.scope.Input"

    error = _refuse(document)

    assert "contracts.acme.widget-triage.scope.Input" in error.paths
    assert "single valid segment" in str(error)


def test_undeclared_contract_reference_refused(document):
    document["slots"]["scorer"]["contract_out"] = "acme.widget-triage.Missing"

    error = _refuse(document)

    assert error.paths == ["slots.scorer.contract_out"]
    issue = error.issues[0]
    assert issue.expected is not None
    assert "cruxible.EmptyInput" in issue.expected


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", [">=2026.30", "~1.2", "^2", "1.0 - 2.0"])
def test_reference_state_version_range_refused(document, version):
    document["dependencies"]["reference_states"] = [
        {"alias": "widget-reference", "version": version}
    ]

    error = _refuse(document)

    assert error.paths == ["dependencies.reference_states[0]"]
    assert "ranges do not parse" in str(error)


def test_reference_state_accepts_exact_alias_at_release(document):
    document["dependencies"]["reference_states"] = [
        {"alias": "widget-reference", "version": "2026.30"}
    ]

    blueprint = parse_blueprint(document)

    assert blueprint.dependencies.reference_states[0].resolved_ref == "widget-reference@2026.30"


def test_reference_state_requires_exactly_one_of_state_ref_or_alias(document):
    document["dependencies"]["reference_states"] = [
        {"state_ref": "widget-reference", "alias": "widget-reference"}
    ]

    error = _refuse(document)

    assert "exactly one of" in str(error)


def test_reference_state_release_declared_twice_refused(document):
    document["dependencies"]["reference_states"] = [
        {"state_ref": "widget-reference@2026.30", "version": "2026.31"}
    ]

    error = _refuse(document)

    assert "declare the release once" in str(error)


def test_reference_state_charset_enforced(document):
    document["dependencies"]["reference_states"] = [{"state_ref": "widget reference"}]

    error = _refuse(document)

    assert "must match" in str(error)


def test_unknown_enum_ordering_refused(document):
    document["dependencies"]["enums"] = [{"name": "criticality", "ordered": "ascending"}]

    error = _refuse(document)

    assert error.paths == ["dependencies.enums[0].ordered"]


# ---------------------------------------------------------------------------
# Compute slots
# ---------------------------------------------------------------------------


def test_billing_modes_are_constrained(document):
    document["slots"]["scorer"]["billing"] = ["invoice"]

    error = _refuse(document)

    assert error.paths == ["slots.scorer.billing[0]"]


def test_empty_billing_refused(document):
    document["slots"]["scorer"]["billing"] = []

    error = _refuse(document)

    assert error.paths == ["slots.scorer.billing"]


def test_duplicate_billing_modes_refused(document):
    document["slots"]["scorer"]["billing"] = ["platform", "platform"]

    error = _refuse(document)

    assert "must not repeat" in str(error)


def test_capability_tag_charset_enforced(document):
    document["slots"]["scorer"]["capabilities"] = ["No Side Effects"]

    error = _refuse(document)

    assert error.paths == ["slots.scorer"]


def test_duplicate_capability_tags_refused(document):
    document["slots"]["scorer"]["capabilities"] = ["deterministic", "deterministic"]

    error = _refuse(document)

    assert "capability tags must not repeat" in str(error)


def test_outcome_metric_requires_exactly_one_target(document):
    document["slots"]["scorer"]["outcome_metric"] = {
        "outcome_profile": "widget_resolution",
        "contract": "acme.widget-triage.ScoreResult",
        "metric": "brier",
    }

    error = _refuse(document)

    assert "exactly one of 'outcome_profile'" in str(error)


def test_outcome_metric_requires_a_target(document):
    document["slots"]["scorer"]["outcome_metric"] = {"metric": "brier"}

    error = _refuse(document)

    assert "exactly one of 'outcome_profile'" in str(error)


def test_unknown_outcome_metric_lists_allowed_values(document):
    document["slots"]["scorer"]["outcome_metric"] = {
        "outcome_profile": "widget_resolution",
        "metric": "vibes",
    }

    error = _refuse(document)

    for metric in KNOWN_OUTCOME_METRICS:
        assert metric in str(error)


def test_slot_name_charset_enforced(document):
    document["slots"]["Scorer"] = document["slots"].pop("scorer")
    document["procedures"][0]["steps"][1]["provider"] = "Scorer"

    error = _refuse(document)

    assert "slots.Scorer" in error.paths


# ---------------------------------------------------------------------------
# Query slots
# ---------------------------------------------------------------------------


def test_query_slot_default_must_be_explicit_engine_schema(document):
    document["query_slots"]["subject_rows"]["default"] = {
        "mode": "traversal",
        "entry_point": "Widget",
        "traverse": "widget_owned_by",
        "returns": "Owner",
    }

    error = _refuse(document)

    assert "compact-grammar key" in str(error)


def test_query_slot_default_accepts_and_strips_the_explicit_marker(document):
    document["query_slots"]["subject_rows"]["default"]["explicit"] = True

    blueprint = parse_blueprint(document)

    assert blueprint.query_slots["subject_rows"].default.mode == "collection"


def test_installed_query_name_collision_refused(document):
    document["query_slots"]["other_rows"] = {
        **document["query_slots"]["subject_rows"],
        "description": "A second socket that installs over the first.",
    }

    error = _refuse(document)

    assert "query_slots.other_rows.install_as" in error.paths
    assert "collides with query slot" in str(error)


def test_query_slot_and_compute_slot_name_collision_refused(document):
    document["query_slots"]["scorer"] = document["query_slots"].pop("subject_rows")
    document["procedures"][0]["steps"][0]["query"] = "scorer"

    error = _refuse(document)

    assert "query_slots.scorer" in error.paths
    assert "both a query slot and a compute slot" in str(error)


def test_row_contract_reference_is_validated(document):
    document["query_slots"]["subject_rows"]["row_contract"] = "acme.widget-triage.Missing"

    error = _refuse(document)

    assert error.paths == ["query_slots.subject_rows.row_contract"]


# ---------------------------------------------------------------------------
# Procedures
# ---------------------------------------------------------------------------


def test_provider_step_must_name_a_declared_slot(document):
    document["procedures"][0]["steps"][1]["provider"] = "some_config_provider"

    error = _refuse(document)

    assert error.paths == ["procedures[0].steps"]
    issue = error.issues[0]
    assert issue.expected is not None
    assert "scorer" in issue.expected
    assert "never concrete providers" in issue.expected


def test_named_query_step_must_name_a_declared_query_slot(document):
    document["procedures"][0]["steps"][0]["query"] = "kit_owned_query"

    error = _refuse(document)

    assert error.paths == ["procedures[0].steps"]
    assert "not a declared query slot" in str(error)


def test_inline_query_steps_are_legal_plumbing_reads(document):
    document["procedures"][0]["steps"][0] = {
        "id": "rows",
        "query": {"mode": "collection", "returns": "Widget", "result_shape": "entity"},
        "as": "rows",
    }

    blueprint = parse_blueprint(document)

    assert blueprint.procedures[0].referenced_query_names() == []


def test_duplicate_procedure_names_refused(document):
    document["procedures"].append({**document["procedures"][0]})

    error = _refuse(document)

    assert error.paths == ["procedures[1].name"]
    assert "one live definition per name" in str(error)


def test_procedure_body_is_validated_by_core(document):
    document["procedures"][0]["budget"]["max_provider_calls"] = 0

    error = _refuse(document)

    assert error.paths == ["procedures[0]"]
    assert "max_provider_calls" in str(error)


def test_procedure_step_path_survives_the_union_tag(document):
    document["procedures"][0]["steps"][1]["as"] = None

    error = _refuse(document)

    assert any(path.startswith("procedures[0].steps") for path in error.paths)


def test_blueprint_with_no_procedures_refused(document):
    document.pop("procedures")

    error = _refuse(document)

    assert error.paths == ["procedures"]
    assert "installs nothing" in str(error)


def test_blank_install_check_refused(document):
    document["install_checks"] = ["   "]

    error = _refuse(document)

    assert error.paths == ["install_checks[0]"]


# ---------------------------------------------------------------------------
# Triggers and pipelines (parsed, not executable)
# ---------------------------------------------------------------------------


def _with_pipeline(document: dict[str, Any]) -> dict[str, Any]:
    pipeline = {
        **document["procedures"][0],
        "name": "widget_ingest",
    }
    document["pipelines"] = [pipeline]
    document.pop("procedures")
    return document


def test_triggers_and_pipelines_parse(document):
    document = _with_pipeline(document)
    document["triggers"] = {
        "widget_webhook": {
            "kind": "webhook",
            "contract_in": "acme.widget-triage.ScopeInput",
            "pipeline": "widget_ingest",
        }
    }

    blueprint = parse_blueprint(document)

    assert blueprint.triggers["widget_webhook"].kind == "webhook"
    assert [body.name for body in blueprint.pipelines] == ["widget_ingest"]


def test_trigger_targeting_an_undeclared_pipeline_refused(document):
    document = _with_pipeline(document)
    document["triggers"] = {
        "scan_uploaded": {
            "kind": "artifact",
            "accepts": ["application/pdf"],
            "pipeline": "not_declared",
        }
    }

    error = _refuse(document)

    assert error.paths == ["triggers.scan_uploaded.pipeline"]
    assert "widget_ingest" in str(error)


def test_trigger_kind_requires_its_own_field(document):
    document = _with_pipeline(document)
    document["triggers"] = {
        "scan_uploaded": {"kind": "artifact", "pipeline": "widget_ingest"},
    }

    error = _refuse(document)

    assert "'artifact' triggers require 'accepts'" in str(error)


def test_trigger_may_not_borrow_another_kinds_field(document):
    document = _with_pipeline(document)
    document["triggers"] = {
        "scan_uploaded": {
            "kind": "artifact",
            "accepts": ["application/pdf"],
            "schedule": "0 * * * *",
            "pipeline": "widget_ingest",
        },
    }

    error = _refuse(document)

    assert "belongs to 'schedule' triggers" in str(error)


def test_pipeline_and_procedure_names_share_one_namespace(document):
    pipeline = {**document["procedures"][0]}
    document["pipelines"] = [pipeline]

    error = _refuse(document)

    assert error.paths == ["pipelines[0].name"]

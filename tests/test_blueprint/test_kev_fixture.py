"""Conformance round-trip for the KEV triage blueprint.

The fixture is the wi-038 conformance spike authored against RFC §3 and the
real ``kev-triage`` kit. It is a *format test*, not an installable artifact --
its compute slots are unbindable on core today (the kit's providers are Python
definitions, which procedures refuse) and it writes nothing, because procedures
cannot emit proposal steps. What it proves is that the format parses, digests
stably, and lowers into objects core's own models accept.

The fixture is checked in verbatim: if a schema change here breaks it, that is
the signal the format moved under a real publisher's document.
"""

from __future__ import annotations

import json

import pytest

from cruxible_core.blueprint import (
    BlueprintBindingError,
    ProviderCandidate,
    canonical_document,
    canonical_yaml,
    compute_blueprint_digest,
    load_blueprint,
    load_blueprint_text,
    lower_blueprint,
    parse_blueprint,
)
from cruxible_core.procedure.types import ProcedureDefinition
from tests.test_blueprint.conftest import KEV_BLUEPRINT_PATH

EXPECTED_PROCEDURES = [
    "kev_blast_radius_brief",
    "kev_owner_action_queue",
    "kev_exposure_rescore",
    "kev_stale_exposure_sweep",
]
EXPECTED_SLOTS = ["affected_assessment", "exposure_assessment", "reconciliation_assessment"]
EXPECTED_QUERY_SLOTS = ["blast_radius_services", "open_posture_work", "owner_action_queue"]


def _load():
    return load_blueprint(KEV_BLUEPRINT_PATH)


def _fake_bindings(blueprint):
    return {slot: f"provider_{slot}" for slot in blueprint.slots}


def _fake_candidates(blueprint):
    """Offer one candidate per slot, carrying every fact the slot constrains.

    Lowering is fail-closed on the whole slot interface, so a stand-in provider
    has to declare its billing modes and capability tags the way a real catalog
    entry would -- contracts alone no longer bind.
    """
    return [
        ProviderCandidate(
            name=f"provider_{name}",
            contract_in=slot.contract_in,
            contract_out=slot.contract_out,
            billing=list(slot.billing),
            capabilities=list(slot.capabilities),
        )
        for name, slot in blueprint.slots.items()
    ]


def _lower_fixture(loaded, **kwargs):
    blueprint = loaded.blueprint
    kwargs.setdefault("bindings", _fake_bindings(blueprint))
    kwargs.setdefault("candidates", _fake_candidates(blueprint))
    return lower_blueprint(blueprint, **kwargs)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def test_kev_fixture_parses_unmodified():
    blueprint = _load().blueprint

    assert blueprint.coordinate == "cruxible-ai/kev-triage-ops@0.1.0"
    assert len(blueprint.contracts) == 11
    assert sorted(blueprint.slots) == EXPECTED_SLOTS
    assert sorted(blueprint.query_slots) == EXPECTED_QUERY_SLOTS
    assert [body.name for body in blueprint.procedures] == EXPECTED_PROCEDURES
    assert blueprint.invocation == "manual"
    assert blueprint.triggers == {}
    assert blueprint.pipelines == []


def test_kev_fixture_declares_every_contract_fully_qualified():
    blueprint = _load().blueprint

    assert all(name.startswith("cruxible-ai.kev-triage-ops.") for name in blueprint.contracts)


def test_kev_fixture_query_slots_install_under_namespaced_names():
    blueprint = _load().blueprint

    installed = {name: slot.installed_name(name) for name, slot in blueprint.query_slots.items()}
    assert installed == {
        "blast_radius_services": "cruxible_ai__kev_triage_ops__blast_radius_services",
        "open_posture_work": "cruxible_ai__kev_triage_ops__open_posture_work",
        "owner_action_queue": "cruxible_ai__kev_triage_ops__owner_action_queue",
    }


def test_kev_fixture_outcome_metric_hooks_name_outcome_profiles():
    blueprint = _load().blueprint

    hook = blueprint.slots["exposure_assessment"].outcome_metric
    assert hook is not None
    assert hook.outcome_profile == "asset_vulnerability_posture_resolution"
    assert hook.metric == "precision_recall"


def test_kev_fixture_depends_on_an_exact_state_ref():
    blueprint = _load().blueprint

    (dependency,) = blueprint.dependencies.reference_states
    assert dependency.resolved_ref == "kev-reference"
    assert blueprint.dependencies.kits[0].kit_id == "kev-triage"


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def test_kev_fixture_digest_is_stable_across_reserialization():
    loaded = _load()

    through_yaml = load_blueprint_text(canonical_yaml(loaded.blueprint))
    through_json = parse_blueprint(json.loads(json.dumps(canonical_document(loaded.blueprint))))

    assert compute_blueprint_digest(through_yaml) == loaded.digest
    assert compute_blueprint_digest(through_json) == loaded.digest
    assert compute_blueprint_digest(load_blueprint_text(canonical_yaml(through_yaml))) == (
        loaded.digest
    )


def test_kev_fixture_digest_moves_on_a_semantic_edit():
    loaded = _load()
    edited = canonical_document(loaded.blueprint)
    edited["slots"]["exposure_assessment"]["billing"] = ["platform"]

    assert compute_blueprint_digest(parse_blueprint(edited)) != loaded.digest


def test_kev_fixture_digest_ignores_comments_and_key_order():
    loaded = _load()
    reordered = dict(reversed(list(canonical_document(loaded.blueprint).items())))

    assert compute_blueprint_digest(parse_blueprint(reordered)) == loaded.digest


# ---------------------------------------------------------------------------
# Lowering
# ---------------------------------------------------------------------------


def test_kev_fixture_lowers_into_valid_procedure_definitions():
    loaded = _load()

    lowered = _lower_fixture(loaded, digest=loaded.digest)

    assert lowered.digest == loaded.digest
    assert [procedure.name for procedure in lowered.procedures] == EXPECTED_PROCEDURES
    for procedure in lowered.procedures:
        assert isinstance(procedure, ProcedureDefinition)
        # Re-validating the dumped body proves the lowered object is a
        # first-class ProcedureDefinition, not just a shape that happened to
        # survive construction.
        ProcedureDefinition.model_validate(
            procedure.model_dump(mode="json", by_alias=True, exclude_none=True)
        )


def test_kev_fixture_lowering_leaves_no_slot_names_in_the_procedures():
    loaded = _load()
    blueprint = loaded.blueprint

    lowered = _lower_fixture(loaded)

    bound_providers = {binding.provider for binding in lowered.slot_bindings}
    installed_queries = set(lowered.query_slot_installs.values())
    for procedure in lowered.procedures:
        assert procedure.referenced_providers() <= bound_providers
        assert not procedure.referenced_providers() & set(blueprint.slots)
        for step in procedure.steps:
            query = getattr(step, "query", None)
            if isinstance(query, str):
                assert query in installed_queries


def test_kev_fixture_overlay_matches_the_declared_objects():
    loaded = _load()
    blueprint = loaded.blueprint

    lowered = _lower_fixture(loaded)

    assert set(lowered.overlay.contracts) == set(blueprint.contracts)
    assert set(lowered.overlay.named_queries) == set(lowered.query_slot_installs.values())
    fragment = lowered.overlay.as_config_dict()
    assert len(fragment["contracts"]) == 11
    assert len(fragment["named_queries"]) == 3


def test_kev_fixture_heaviest_procedure_keeps_its_expansion_bounds():
    loaded = _load()

    lowered = _lower_fixture(loaded)

    rescore = next(p for p in lowered.procedures if p.name == "kev_exposure_rescore")
    expansion = rescore.static_expansion()
    assert expansion.total_steps == 19
    assert expansion.expanded_provider_calls == 2
    assert rescore.budget.max_provider_calls >= expansion.expanded_provider_calls


def test_kev_fixture_refuses_a_provider_nobody_offered():
    """The reviewer's reproduction: bind every slot to a provider, offer none.

    This used to lower cleanly into three ``ResolvedSlotBinding`` rows that
    reported the *slot's own* contracts as though the provider satisfied them.
    """
    loaded = _load()
    blueprint = loaded.blueprint
    bindings = {slot: "totally_incompatible_provider" for slot in blueprint.slots}

    with pytest.raises(BlueprintBindingError) as excinfo:
        lower_blueprint(blueprint, bindings=bindings, candidates=[])

    error = excinfo.value
    assert error.slot == "affected_assessment"  # first slot in sorted order
    assert error.paths == ["bindings.affected_assessment"]
    assert "totally_incompatible_provider" in str(error)
    assert "is not among the 0 candidate provider(s) offered" in str(error)


def test_kev_fixture_refuses_a_candidate_that_cannot_meet_the_capability_tags():
    loaded = _load()
    blueprint = loaded.blueprint
    stripped = [
        candidate.model_copy(update={"capabilities": ()})
        for candidate in _fake_candidates(blueprint)
    ]

    with pytest.raises(BlueprintBindingError) as excinfo:
        lower_blueprint(blueprint, bindings=_fake_bindings(blueprint), candidates=stripped)

    assert excinfo.value.paths == ["slots.affected_assessment.capabilities"]
    assert "deterministic" in str(excinfo.value)

"""``scope: observations`` reads the pair ends a Procedure's Subject specs create.

A composite-key (reified) Subject such as an occurrence names its ends as
nested ``{kind, key, create}`` specs. The runtime creates a nested end with
``create: if_absent`` only when the spec enclosing it creates too, recursively
(``claims_context.named``), so the grant's creatable kinds follow the same rule.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.observation_settle import (
    ObservationSettleRefusal,
    ObservationSettleScope,
    created_subject_kinds,
    observation_settle_scope,
    spec_created_kinds,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.procedure_mandates import (
    parse_procedure_mandate,
    procedure_mandate_path,
)
from cruxible_client.contracts.procedures.claims import ClaimsNode, ClaimsSubject
from cruxible_core.authoring.lowering import AuthoringLoweringError
from tests.core_support._identity_support import accept_tree
from tests.core_support._pair_procedure_support import (
    APP_EXPOSURE,
    CAPTURE,
    COMPONENT,
    INVENTORY,
    NOTE,
    OCCURRENCE,
    VULN,
    end,
    occurrence_key,
    pair_spec,
    rows,
    subjects_of,
    vocabulary,
    write_doc,
)
from tests.support.claim_types import claim_type as build_claim_type
from tests.support.operation_world import (
    NOW,
    author,
    author_line,
    author_procedure,
    operation_build,
    world,
)

LINE = "test.nested-grant"


# -- the pure rule ---------------------------------------------------------------------


def _exposure(*, outer: str = "if_absent", occurrence: str = "if_absent", ends: str = "if_absent"):
    """``app_exposure(occurrence(inventory, component), vulnerability)``: two levels of ends."""

    return {
        "kind": APP_EXPOSURE,
        "key": {
            "occurrence": end(OCCURRENCE, occurrence_key(create=ends), occurrence),
            "vulnerability": end(VULN, {"vid": "$item.key.vid"}, ends),
        },
        "create": outer,
    }


def test_a_creating_spec_creates_every_creating_end_it_names_recursively() -> None:
    assert spec_created_kinds(_exposure()) == {APP_EXPOSURE, OCCURRENCE, INVENTORY, COMPONENT, VULN}


def test_a_model_spec_reads_like_its_mapping() -> None:
    spec = ClaimsSubject.model_validate(_exposure())
    assert spec_created_kinds(spec) == spec_created_kinds(_exposure())


@pytest.mark.parametrize("outer", ["never", None])
def test_an_end_under_a_spec_that_does_not_create_is_never_collected(outer: str | None) -> None:
    spec = _exposure(outer=outer or "never")
    if outer is None:
        spec.pop("create")
    assert spec_created_kinds(spec) == set()


def test_a_non_creating_middle_spec_cuts_off_the_ends_below_it() -> None:
    # The occurrence is not created, so its inventory and component never are,
    # whatever they say; the vulnerability end still is.
    assert spec_created_kinds(_exposure(occurrence="never")) == {APP_EXPOSURE, VULN}


def test_template_values_count_with_their_ends() -> None:
    node = ClaimsNode.model_validate(
        {
            "node_id": "write",
            "for_each": "$steps.entries.records",
            "subject": {"kind": INVENTORY, "key": {"name": "$item.key.inv"}},
            "claims": [
                {
                    "type": {"target": {"kind": "ClaimType", "name": "pp.inventory.holds"}},
                    "value": end(OCCURRENCE, occurrence_key()),
                },
                {
                    "type": {"target": {"kind": "ClaimType", "name": "pp.inventory.other"}},
                    "value": end(OCCURRENCE, occurrence_key(), "never"),
                },
            ],
        }
    )
    # The row Subject is not created; the first value creates the pair and both ends.
    assert created_subject_kinds(node) == {OCCURRENCE, INVENTORY, COMPONENT}


def _node(subject: dict[str, Any]) -> ClaimsNode:
    return ClaimsNode.model_validate(
        {
            "node_id": "write",
            "for_each": "$steps.entries.records",
            "subject": subject,
            "claims": [
                {
                    "type": {"target": {"kind": "ClaimType", "name": NOTE}},
                    "value": "$item.fields.inventory",
                }
            ],
        }
    )


def _observation(name: str) -> Any:
    return type("Typed", (), {"permitted_roles": ("observation",)})() if name == NOTE else None


def test_subject_creation_may_name_a_nested_creatable_kind() -> None:
    scope = observation_settle_scope(
        [_node(_exposure())], _observation, subject_creation=(INVENTORY, VULN)
    )
    assert isinstance(scope, ObservationSettleScope)
    assert scope.creatable_subject_kinds == tuple(
        sorted((APP_EXPOSURE, COMPONENT, INVENTORY, OCCURRENCE, VULN))
    )


def test_subject_creation_naming_an_uncreatable_kind_still_refuses() -> None:
    nested_cut = observation_settle_scope(
        [_node(_exposure(occurrence="never"))], _observation, subject_creation=(INVENTORY,)
    )
    assert isinstance(nested_cut, ObservationSettleRefusal)
    assert nested_cut.code == (
        "cruxible.authoring.procedure_mandate_observations_creation_unavailable"
    )
    assert nested_cut.names == (INVENTORY,)
    outer_never = observation_settle_scope(
        [_node(_exposure(outer="never"))], _observation, subject_creation=(VULN,)
    )
    assert isinstance(outer_never, ObservationSettleRefusal) and outer_never.names == (VULN,)


# -- authoring lowering and a settled run ------------------------------------------------


def _observation_note() -> Any:
    return build_claim_type(
        predicate=NOTE,
        allowed_subject_kinds=[APP_EXPOSURE],
        value_type={"type": "string"},
        permitted_roles=["observation"],
        default_role="observation",
        revision_evidence="replace",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="fixture",
                    claim_roles=("observation",),
                    capture_contracts=(CAPTURE,),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )


def _nested_world(tmp_path: Path, subject: dict[str, Any]) -> tuple[Any, Path]:
    instance = world(tmp_path, operation_build())
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True)
    tree = vocabulary()
    tree[claim_type_path(NOTE)] = render_claim_type(_observation_note())
    accept_tree(instance, tree, name="nested-vocabulary")
    spec = pair_spec(
        "test.nested",
        doc="app",
        source="test.app",
        by=None,
        subject=subject,
        claims=[{"type": NOTE, "value": "$item.fields.inventory", "rationale": "Seen."}],
        vid_key=True,
    )
    author_procedure(instance, spec, name=LINE + "-procedure")
    author_line(
        instance,
        name=LINE,
        procedure=spec.procedure,
        parameters={},
        max_authority="settle",
        findings={"inbox": []},
    )
    return instance, root


def _grant(instance: Any, subject_creation: tuple[str, ...]) -> dict[str, bytes]:
    return author(
        instance,
        ProcedureMandateInput.model_validate(
            {
                "kind": "procedure_mandate",
                "name": LINE + "-settle",
                "line_name": LINE,
                "grants": "settle",
                "valid_from": NOW - timedelta(days=1),
                "expires_at": NOW + timedelta(days=90),
                "scope": "observations",
                "subject_creation": subject_creation,
            }
        ),
        name="nested-grant",
    )


def test_a_two_level_composite_key_lowers_and_settles_its_nested_creations(
    tmp_path: Path,
) -> None:
    from tests.test_procedures.test_claims_terminal import run

    instance, root = _nested_world(tmp_path, _exposure())
    every = tuple(sorted((APP_EXPOSURE, COMPONENT, INVENTORY, OCCURRENCE, VULN)))
    changed = _grant(instance, every)
    path = procedure_mandate_path(LINE + "-settle")
    mandate = parse_procedure_mandate(changed[path], path=path)
    assert mandate.subject_creation == every
    assert [item.claim_type.target.name for item in mandate.scope] == [NOTE]

    write_doc(root, "app", [{**row, "vid": "CVE-1"} for row in rows("web", ["pkg:pypi/a@1"])])
    state = run(instance, root, line=LINE)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "settled", egress
    assert egress.outside_scope == ()
    for kind in every:
        assert len(subjects_of(instance, kind)) == 1, kind


def test_a_nested_end_under_a_never_spec_is_not_creatable_at_lowering(tmp_path: Path) -> None:
    instance, _root = _nested_world(tmp_path, _exposure(occurrence="never"))
    with pytest.raises(AuthoringLoweringError) as refused:
        _grant(instance, (INVENTORY,))
    assert refused.value.code == (
        "cruxible.authoring.procedure_mandate_observations_creation_unavailable"
    )
    changed = _grant(instance, (APP_EXPOSURE, VULN))
    path = procedure_mandate_path(LINE + "-settle")
    assert parse_procedure_mandate(changed[path], path=path).subject_creation == (
        APP_EXPOSURE,
        VULN,
    )

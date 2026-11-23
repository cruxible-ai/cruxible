"""One settle grant over a Line's observations (D-R50, ``scope: observations``).

It lowers to an ordinary per-ClaimType ``cruxible-procedure-mandate-v4`` scope,
so the Mandate law and the settle verifier stay exactly as they are.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts import is_observation_only_claim_type
from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
from cruxible_client.contracts.claim_types import ClaimInverse
from cruxible_client.contracts.observation_settle import (
    ObservationSettleRefusal,
    ObservationSettleScope,
    observation_settle_scope,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.procedure_mandates import (
    parse_procedure_mandate,
    procedure_mandate_path,
)
from cruxible_client.contracts.procedures.claims import ClaimsNode
from cruxible_client.contracts.procedures.nodes import InvokeNode
from cruxible_core.authoring.lowering import AuthoringLoweringError
from tests.support.operation_world import NOW, author
from tests.test_procedures.test_claims_terminal import (
    KIND,
    PREDICATE,
    claim_type,
    claims_spec,
    claims_world,
    install,
    run,
)

LINE = "test.claims-line"


def _observation_type(name: str = PREDICATE, value: str = "enum[open, closed, unknown]", **kw: Any):
    return claim_type(
        name,
        value,
        permitted_roles=["observation"],
        default_role="observation",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="fixture",
                    claim_roles=("observation",),
                    capture_contracts=("test.orders-v1",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        **kw,
    )


# -- the shared predicate ----------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (("observation",), True),
        (("normative", "observation"), False),
        (("normative",), False),
        (("derivation",), False),
        (("derivation", "observation"), False),
        ((), False),
    ],
)
def test_observation_only_means_the_observation_role_and_nothing_else(roles, expected) -> None:
    assert is_observation_only_claim_type(SimpleNamespace(permitted_roles=roles)) is expected


def test_the_predicate_reads_a_real_claim_type() -> None:
    assert is_observation_only_claim_type(_observation_type())
    # The fixture default permits derivation, normative and observation.
    assert not is_observation_only_claim_type(claim_type())


# -- the pure lowering ---------------------------------------------------------------


def _node(*templates: dict[str, Any], expect: bool = False, create: str = "never") -> ClaimsNode:
    return ClaimsNode.model_validate(
        {
            "node_id": "write",
            "for_each": "$steps.entries.records",
            "subject": {"kind": KIND, "key": {"cve_id": "$item.key.cve_id"}, "create": create},
            "claims": [
                {"type": {"target": {"kind": "ClaimType", "name": t.pop("type")}}, **t}
                for t in templates
            ],
            **({"expect": "$steps.entries.keys"} if expect else {}),
        }
    )


def _roles(**by_name: tuple[str, ...]):
    return lambda name: SimpleNamespace(permitted_roles=by_name[name]) if name in by_name else None


def _changes(scope: ObservationSettleScope) -> dict[str, dict[str, Any]]:
    return {item.claim_type: item.changes.model_dump(mode="json") for item in scope.claim_types}


def test_value_templates_settle_creates_and_revises_on_extracted_and_measured() -> None:
    scope = observation_settle_scope(
        [_node({"type": "a.x", "value": "$item.fields.x"}, {"type": "a.y", "value": 1})],
        _roles(**{"a.x": ("observation",), "a.y": ("observation",)}),
    )
    assert isinstance(scope, ObservationSettleScope)
    both = {"basis": ["extracted", "measured"]}
    assert _changes(scope) == {
        "a.x": {"create": both, "revise": both},
        "a.y": {"create": both, "revise": both},
    }
    assert scope.creatable_subject_kinds == ()


def test_derived_is_opt_in() -> None:
    nodes = [_node({"type": "a.x", "value": "$item.fields.x"})]
    roles = _roles(**{"a.x": ("observation",)})
    off = observation_settle_scope(nodes, roles)
    on = observation_settle_scope(nodes, roles, include_derived=True)
    assert isinstance(off, ObservationSettleScope) and isinstance(on, ObservationSettleScope)
    assert _changes(off)["a.x"]["create"] == {"basis": ["extracted", "measured"]}
    assert _changes(on)["a.x"] == {
        "create": {"basis": ["extracted", "measured", "derived"]},
        "revise": {"basis": ["extracted", "measured", "derived"]},
    }


def test_mirror_retires_on_absence_and_inverse_writes_on_absence() -> None:
    scope = observation_settle_scope(
        [
            _node(
                {"type": "a.mirror", "value": "$item.fields.x", "when_absent": "mirror"},
                {"type": "a.inverse", "value": True, "when_absent": "inverse"},
                {"type": "a.keep", "value": "$item.fields.y", "when_absent": "keep"},
                expect=True,
            )
        ],
        _roles(
            **{
                "a.mirror": ("observation",),
                "a.inverse": ("observation",),
                "a.keep": ("observation",),
            }
        ),
    )
    assert isinstance(scope, ObservationSettleScope)
    value = {"basis": ["extracted", "measured"]}
    absent = {"basis": ["extracted", "measured", "absence"]}
    assert _changes(scope) == {
        "a.inverse": {"create": absent, "revise": absent, "retire": {"basis": ["absence"]}},
        "a.keep": {"create": value, "revise": value},
        "a.mirror": {"create": value, "revise": value, "retire": {"basis": ["absence"]}},
    }


@pytest.mark.parametrize("roles", [("normative", "observation"), ("normative",), ("derivation",)])
def test_a_claim_type_that_is_not_observation_only_refuses_by_name(roles) -> None:
    refused = observation_settle_scope(
        [_node({"type": "a.x", "value": 1}, {"type": "a.judged", "value": 2})],
        _roles(**{"a.x": ("observation",), "a.judged": roles}),
    )
    assert isinstance(refused, ObservationSettleRefusal)
    assert refused.code == "cruxible.authoring.procedure_mandate_observations_not_observation"
    assert refused.names == ("a.judged",)
    assert "a.judged permits" in refused.message


def test_an_invoke_node_refuses_because_its_templates_are_not_visible() -> None:
    invoke = InvokeNode.model_validate(
        {
            "node_id": "child",
            "procedure": {"target": {"kind": "Procedure", "name": "child"}},
            "input": {},
            "as": "child",
        }
    )
    refused = observation_settle_scope(
        [_node({"type": "a.x", "value": 1}), invoke], _roles(**{"a.x": ("observation",)})
    )
    assert isinstance(refused, ObservationSettleRefusal)
    assert refused.code == "cruxible.authoring.procedure_mandate_observations_invoke"


def test_missing_claim_types_and_empty_procedures_refuse() -> None:
    missing = observation_settle_scope([_node({"type": "a.x", "value": 1})], _roles())
    empty = observation_settle_scope([], _roles())
    assert isinstance(missing, ObservationSettleRefusal) and missing.names == ("a.x",)
    assert missing.code == "cruxible.authoring.procedure_mandate_claim_type_missing"
    assert isinstance(empty, ObservationSettleRefusal)
    assert empty.code == "cruxible.authoring.procedure_mandate_observations_empty"


def test_subject_creation_names_only_kinds_the_procedure_creates() -> None:
    nodes = [
        _node(
            {
                "type": "a.x",
                "value": {
                    "kind": "a.part",
                    "key": {"id": "$item.key.cve_id"},
                    "create": "if_absent",
                },
            },
            create="if_absent",
        )
    ]
    roles = _roles(**{"a.x": ("observation",)})
    scope = observation_settle_scope(nodes, roles, subject_creation=(KIND,))
    stray = observation_settle_scope(nodes, roles, subject_creation=("other.kind",))
    assert isinstance(scope, ObservationSettleScope)
    assert scope.creatable_subject_kinds == tuple(sorted(("a.part", KIND)))
    assert isinstance(stray, ObservationSettleRefusal) and stray.names == ("other.kind",)


# -- authoring lowering and a real settle ------------------------------------------


def _grant(instance: Any, **overrides: Any) -> dict[str, bytes]:
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
                **overrides,
            }
        ),
        name="observation-grant",
    )


def _settle_world(tmp_path: Path, *types: Any) -> tuple[Any, Path]:
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": "open"}]},
        types=types or (_observation_type(),),
    )
    install(instance, claims_spec(), authority="settle")
    return instance, root


def test_scope_observations_lowers_to_the_exact_v4_scope_and_settles_a_run(tmp_path: Path) -> None:
    instance, root = _settle_world(tmp_path)
    changed = _grant(instance)
    path = procedure_mandate_path(LINE + "-settle")
    mandate = parse_procedure_mandate(changed[path], path=path)
    assert mandate.artifact_format == "cruxible-procedure-mandate-v4"
    assert mandate.grants == "settle" and mandate.namespace == ("claims",)
    (item,) = mandate.scope
    assert item.claim_type.target.name == PREDICATE
    assert item.claim_type.artifact_digest.startswith("sha256:")
    assert item.changes.model_dump(mode="json") == {
        "create": {"basis": ["extracted", "measured"]},
        "revise": {"basis": ["extracted", "measured"]},
    }
    assert mandate.subject_creation == () and mandate.condition is None

    before = instance.accepted_coordinate()
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "settled", egress
    assert egress.outside_scope == ()
    assert instance.accepted_coordinate() != before


def test_include_derived_and_an_explicit_namespace_lower_as_written(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    changed = _grant(instance, include_derived=True, namespace=("claims",))
    path = procedure_mandate_path(LINE + "-settle")
    (item,) = parse_procedure_mandate(changed[path], path=path).scope
    assert item.changes.model_dump(mode="json")["create"] == {
        "basis": ["extracted", "measured", "derived"]
    }


def test_a_normative_claim_type_in_the_procedure_refuses_the_grant(tmp_path: Path) -> None:
    # The fixture default permits normative and derivation besides observation.
    instance, _root = _settle_world(tmp_path, claim_type())
    with pytest.raises(AuthoringLoweringError) as refused:
        _grant(instance)
    assert refused.value.code == (
        "cruxible.authoring.procedure_mandate_observations_not_observation"
    )
    assert PREDICATE in str(refused.value)


def test_subject_creation_is_explicit_and_checked_against_the_procedure(tmp_path: Path) -> None:
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": "open"}]},
        types=(_observation_type(),),
        seed_subjects=False,
    )
    install(instance, claims_spec(subject_create="if_absent"), authority="settle")
    with pytest.raises(AuthoringLoweringError) as refused:
        _grant(instance, subject_creation=("other.kind",))
    assert refused.value.code == (
        "cruxible.authoring.procedure_mandate_observations_creation_unavailable"
    )
    changed = _grant(instance, subject_creation=(KIND,))
    path = procedure_mandate_path(LINE + "-settle")
    assert parse_procedure_mandate(changed[path], path=path).subject_creation == (KIND,)
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress[0].settle_outcome == "settled", state.terminal_egress


@pytest.mark.parametrize("field", ["include_derived", "scope"])
def test_a_propose_grant_refuses_the_observation_form(tmp_path: Path, field: str) -> None:
    instance, _root = _settle_world(tmp_path)
    overrides: dict[str, Any] = {"grants": "propose", "namespace": ("claims",)}
    if field == "include_derived":
        overrides.update(scope=(), include_derived=True)
    with pytest.raises(AuthoringLoweringError) as refused:
        _grant(instance, **overrides)
    assert refused.value.code == "cruxible.authoring.procedure_mandate_propose_scope"


def test_include_derived_without_scope_observations_refuses(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    with pytest.raises(AuthoringLoweringError) as refused:
        _grant(
            instance,
            namespace=("claims",),
            include_derived=True,
            scope=[{"claim_type": PREDICATE, "changes": {"create": {"basis": ["extracted"]}}}],
        )
    assert refused.value.code == "cruxible.authoring.procedure_mandate_include_derived_unscoped"


def test_an_inverse_line_settles_its_absence_writes_under_one_grant(tmp_path: Path) -> None:
    """``when_absent: inverse`` lowers to creates and revises on absence (and retire)."""

    import json

    from tests.test_pilot.support import FIXTURES, KEV, KEV_ROW, _watched, pilot_records
    from tests.test_procedures.test_claims_absence import absence_spec

    original = json.loads((FIXTURES / "kev.json").read_bytes())
    inverse = ClaimInverse(key={"kind": "slot"}, evidence_rules=("fixture",))
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": row["cveID"]} for row in original["vulnerabilities"]]},
        types=(
            _observation_type(KEV + "listed", "bool", inverse=inverse),
            _observation_type(KEV + "date_added", "date"),
            _observation_type(KEV + "due_date", "date"),
            _observation_type(KEV + "ransomware_use", "enum[Known, Unknown]"),
        ),
        rows=KEV_ROW,
        records=pilot_records(True),
    )
    _watched(instance)
    (root / "data/kev.json").write_text(json.dumps({**original, "vulnerabilities": [], "count": 0}))
    install(
        instance,
        absence_spec("inverse"),
        authority="settle",
        parameters={"logical_source": "cisa.kev.catalog", "relative_path": "data/kev.json"},
    )
    changed = _grant(instance)
    path = procedure_mandate_path(LINE + "-settle")
    scope = {
        item.claim_type.target.name: item.changes.model_dump(mode="json")
        for item in parse_procedure_mandate(changed[path], path=path).scope
    }
    assert scope[KEV + "listed"]["create"] == {"basis": ["extracted", "measured", "absence"]}
    assert scope[KEV + "listed"]["retire"] == {"basis": ["absence"]}
    before = instance.accepted_coordinate()
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "settled" and egress.outside_scope == (), egress
    assert instance.accepted_coordinate() != before

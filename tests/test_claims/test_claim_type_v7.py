"""ClaimType v7: descriptions, a default role, an evidence requirement, revision evidence.

Every earlier format keeps its exact bytes, digest and ``model_dump``: the new
fields exist on the shared model but are null for them and never written.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.claim_types import (
    V7_FIELDS,
    ClaimType,
    ClaimTypeMemberDescription,
    claim_type_digest,
    claim_type_path,
    effective_evidence_requirement,
    effective_revision_evidence,
    evaluate_claim_type_law,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    ClaimAdjudicationRule,
    ClaimAdjudicationRuleV1,
    claim_adjudication_rule,
    claim_adjudication_rule_digest,
    evaluate_claim_verdict,
)
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from tests.test_claims._claim_type_format_fixtures import fingerprints, historical_claim_types
from tests.test_claims.test_claim_verdicts import (
    STATEMENT_DIGEST,
    _attestation,
    _attestation_capture,
    _capture,
)

# Computed by the code before ClaimType v7 (playbill 15a55995a) from the same
# fixtures: (sha256(render), digest, sha256(canonical model_dump)).
PRE_V7_FINGERPRINTS = {
    "v1": (
        "0ff26ca481a9af51feef769957c1eeb2853a06e9cef2f03b54f8101f83c9bf66",
        "sha256:5646d64dbd932b90af1cd6335f0954ff6a03bbd64c9fe56889f193dcb15d4b6f",
        "052122fe93d7f1f388c8edd8e3689114451db2a89a995c14272d6e98c4bdb14b",
    ),
    "v3": (
        "f4df696d87cfa0d1fb9cb5c38624522c05e319c04c2ab046727b3784bdf217e6",
        "sha256:43e53fa5546ea4941a0ac6fc03dba6add467354aa9b39b3328b6b1e189158274",
        "69aa1a6f2e5f394658e263ab29fa25a8b3ef268141db019e48580c4d2d9e522b",
    ),
    "v4": (
        "19f0c1610ed5e692f80c57e66e1042798ecc8fb6e7651cf0e4388278e54f7b93",
        "sha256:202fbc7eefb4e11af6b210575cd652cd923ec4781a8d7d0dd99d48994e0c5d41",
        "0bacd878ef8aa78bef5bc8d1c15238637b63e2507d357db3d1b2863b92a5fbba",
    ),
    "v5": (
        "610408c23708a0527f743b6a911d097a3834ef8b3b169f2045738a2f983d7f82",
        "sha256:9b02a2095e2d3b39d216f407aa9cd8581a76fa4c00220e458e11788e7a6efe13",
        "dc7370c0ba44db49fcfd2e3e287f3188998b77cb242cacd042d062ea445e2197",
    ),
    "v5-hold": (
        "909dcbfa907009b4895731ea509c516329ff3002f8310637991c4711d8116a46",
        "sha256:a66652e784dde01e779318a41c0dc1ae08827ca3aa7dfdc0c0ff51959bd36ab6",
        "eb2798f0afd8627f6318d09917ade4ea6e66e3fb2886bd044007ea2dd52f1019",
    ),
    "v6": (
        "4502b5cd5c6244c3d58302f0360c3240f619cf8663780fec58d67856ce6d7a62",
        "sha256:5dfa6a753d447aae842d7614f04d1e9e077979bc924552809fc2593a691608af",
        "c6de22dca67681bef0dc5bb8f8c023d91137bc79e9709018570ff98ae38fcf48",
    ),
}
PATH = claim_type_path("project.work_item.status")


def v7(**update: object) -> ClaimType:
    """The v6 fixture moved to v7 with today's meaning, then ``update`` applied."""

    base = historical_claim_types()["v6"].model_dump(mode="python")
    base.update(
        artifact_format="playbill-claim-type-v7",
        evidence_requirement="self",
        revision_evidence="replace",
    )
    base.update(update)
    return ClaimType.model_validate(base)


# --- Byte identity -------------------------------------------------------------


def test_every_historical_format_keeps_its_bytes_digest_and_model_dump() -> None:
    assert fingerprints() == PRE_V7_FINGERPRINTS
    for name, claim_type in historical_claim_types().items():
        dumped = claim_type.model_dump(mode="json")
        assert not set(V7_FIELDS) & set(dumped), name
        assert parse_claim_type(render_claim_type(claim_type), path=PATH) == claim_type
        assert effective_revision_evidence(claim_type) == "accumulate"
        assert effective_evidence_requirement(claim_type) == "self"


def test_v7_always_writes_all_five_fields_and_round_trips() -> None:
    claim_type = v7()
    dumped = claim_type.model_dump(mode="json")
    assert {field: dumped[field] for field in V7_FIELDS} == {
        "description": None,
        "member_descriptions": [],
        "default_role": None,
        "evidence_requirement": "self",
        "revision_evidence": "replace",
    }
    rendered = render_claim_type(claim_type)
    assert parse_claim_type(rendered, path=PATH) == claim_type
    # The description is part of what the ClaimType is.
    described = v7(description="Where the work item stands.")
    assert claim_type_digest(described) != claim_type_digest(claim_type)


def test_earlier_formats_refuse_the_v7_fields() -> None:
    v6 = historical_claim_types()["v6"].model_dump(mode="python")
    for field, value in (
        ("description", "x"),
        ("default_role", "observation"),
        ("evidence_requirement", "self"),
        ("revision_evidence", "replace"),
        ("member_descriptions", ({"member": "done", "description": "x"},)),
    ):
        with pytest.raises(ValidationError, match="only ClaimType v7"):
            ClaimType.model_validate({**v6, field: value})


def test_v7_states_its_evidence_semantics_explicitly() -> None:
    with pytest.raises(ValidationError, match="explicitly"):
        v7(evidence_requirement=None)
    with pytest.raises(ValidationError, match="explicitly"):
        v7(revision_evidence=None)


# --- default_role ----------------------------------------------------------------


def test_default_role_must_be_permitted_and_never_derivation() -> None:
    assert v7(default_role="observation").default_role == "observation"
    with pytest.raises(ValidationError, match="permitted_roles"):
        v7(default_role="environment_binding")
    with pytest.raises(ValidationError, match="derivation"):
        v7(permitted_roles=("derivation", "observation"), default_role="derivation")


def test_the_v7_law_refuses_an_unpermitted_default_role() -> None:
    forged = v7().model_copy(update={"default_role": "environment_binding"})
    result = evaluate_claim_type_law(forged, path=PATH, predecessor=None)
    assert [item.code for item in result.diagnostics] == [
        "cruxible.claim_type.default_role_not_permitted"
    ]


# --- Descriptions ------------------------------------------------------------------


def _member(member: object, text: str = "means something") -> dict[str, object]:
    return {"member": member, "description": text}


def test_member_descriptions_name_enum_members_in_canonical_order() -> None:
    described = v7(
        description="Where the work item stands.",
        member_descriptions=(_member("blocked"), _member("done"), _member("ready")),
    )
    assert [item.member for item in described.member_descriptions] == [
        "blocked",
        "done",
        "ready",
    ]
    with pytest.raises(ValidationError, match="sorted and unique"):
        v7(member_descriptions=(_member("done"), _member("blocked")))
    with pytest.raises(ValidationError, match="sorted and unique"):
        v7(member_descriptions=(_member("done"), _member("done")))
    with pytest.raises(ValidationError, match="not an enum member"):
        v7(member_descriptions=(_member("shipped"),))
    with pytest.raises(ValidationError, match="top-level enum"):
        v7(literal_schema={"type": "string"}, member_descriptions=(_member("done"),))


def test_a_boolean_member_is_not_its_integer_twin() -> None:
    schema = {"enum": [0, 1]}
    with pytest.raises(ValidationError, match="not an enum member"):
        v7(literal_schema=schema, member_descriptions=(_member(True),))
    assert v7(literal_schema=schema, member_descriptions=(_member(1),)).member_descriptions


def test_descriptions_are_canonical_text_within_bounds() -> None:
    with pytest.raises(ValidationError, match="whitespace"):
        v7(description=" padded")
    with pytest.raises(ValidationError, match="NFC"):
        v7(description="café")
    with pytest.raises(ValidationError, match="1..1024"):
        v7(description="x" * 1025)
    with pytest.raises(ValidationError, match="1..256"):
        ClaimTypeMemberDescription(member="done", description="x" * 257)
    with pytest.raises(ValidationError, match="1..1024"):
        v7(description="")


# --- The evidence requirement ------------------------------------------------------


def _self_only_rules() -> ClaimEvidenceAdmissionPolicy:
    return ClaimEvidenceAdmissionPolicy(
        rules=(
            ClaimEvidenceAdmissionRule(
                rule_id="own-words",
                claim_roles=("normative", "observation"),
                capture_contracts=(
                    ArtifactRef(
                        role=CAPTURE_CONTRACT_REF_ROLE,
                        target=ArtifactIdentity(
                            kind="CaptureContract", name="playbill.coordinator-self-source-v1"
                        ),
                    ),
                ),
                evidence_kinds=("self_asserted",),
                admission="direct",
                subject_binding="exact_claim_subject",
            ),
        )
    )


def test_an_unsatisfiable_requirement_is_refused_by_the_claim_type_law() -> None:
    def codes(claim_type: ClaimType) -> list[str]:
        result = evaluate_claim_type_law(claim_type, path=PATH, predecessor=None)
        return [item.code for item in result.diagnostics]

    assert codes(v7(evidence_requirement="captured")) == []
    assert codes(
        v7(evidence_requirement="captured", evidence_admission_policy=_self_only_rules())
    ) == ["cruxible.claim_type.evidence_requirement_unsatisfiable"]
    assert codes(v7(evidence_requirement="none")) == []
    resolution = v7().resolution_policy.model_copy(update={"required_basis_kinds": ("direct",)})
    assert codes(v7(evidence_requirement="none", resolution_policy=resolution)) == [
        "cruxible.claim_type.evidence_requirement_unsatisfiable"
    ]


def test_only_none_compiles_the_origin_supporting_rule() -> None:
    def rule(claim_type: ClaimType) -> object:
        return claim_adjudication_rule(
            claim_type, claim_type_digest=claim_type_digest(claim_type).tagged
        )

    for claim_type in (
        *historical_claim_types().values(),
        v7(),
        v7(evidence_requirement="captured"),
    ):
        assert isinstance(rule(claim_type), ClaimAdjudicationRuleV1)
    origin = rule(v7(evidence_requirement="none"))
    assert isinstance(origin, ClaimAdjudicationRule) and origin.origin_supports
    # The v1 rule's digest domain is unchanged; v2 has its own.
    v1_rule = rule(historical_claim_types()["v6"])
    assert isinstance(v1_rule, ClaimAdjudicationRuleV1)
    as_v2 = ClaimAdjudicationRule(**v1_rule.model_dump(exclude={"tag"}))
    assert claim_adjudication_rule_digest(as_v2) != claim_adjudication_rule_digest(v1_rule)


def _origin_rule(**update: object) -> ClaimAdjudicationRule:
    claim_type = v7(evidence_requirement="none")
    rule = claim_adjudication_rule(
        claim_type, claim_type_digest=claim_type_digest(claim_type).tagged
    )
    assert isinstance(rule, ClaimAdjudicationRule)
    return rule.model_copy(update=update)


def test_under_none_the_origin_supports_and_a_contradiction_still_contradicts() -> None:
    from tests.core_support._pc_c_support import NOW

    origin = _capture("origin", admission="origin_only")
    supported = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_origin_rule(max_evidence_age=None),
        evaluation_time=NOW,
        captures=(origin,),
        attestations=(),
        providers={},
    )
    assert supported.verdict == "supported"
    contradict = _attestation("contradict", stance="contradict", control_domain="lab-b")
    contradicted = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_origin_rule(max_evidence_age=None, conflict_behavior="contradiction_precedence"),
        evaluation_time=NOW,
        captures=(origin, _attestation_capture("contradict", control_domain="lab-b")),
        attestations=(contradict,),
        providers={},
    )
    assert contradicted.verdict == "contradicted"


def test_under_none_the_origin_goes_stale_past_the_freshness_horizon() -> None:
    from tests.core_support._pc_c_support import NOW

    origin = _capture("origin", admission="origin_only")
    rule = _origin_rule(max_evidence_age=CanonicalDuration(microseconds=60_000_000))
    fresh = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=rule,
        evaluation_time=NOW,
        captures=(origin,),
        attestations=(),
        providers={},
    )
    assert fresh.verdict == "supported"
    aged = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=rule,
        evaluation_time=NOW + timedelta(minutes=5),
        captures=(origin,),
        attestations=(),
        providers={},
    )
    assert aged.verdict == "stale_evidence"


# --- Lowering authored input ------------------------------------------------------


def _input(**update: object) -> object:
    from cruxible_core.claims.claim_type_inputs import (
        ClaimTypeInputRecord,
        claim_type_input_template,
    )

    return ClaimTypeInputRecord.model_validate(
        {**claim_type_input_template().model_dump(mode="json"), **update}
    )


def _lower(value: object, tree: dict[str, bytes], *, identity_rules: bool = True) -> ClaimType:
    from cruxible_core.claims.claim_type_inputs import lower_claim_type_input

    return lower_claim_type_input(value, tree=tree, identity_rules=identity_rules)  # type: ignore[arg-type]


def _as_v6(claim_type: ClaimType) -> ClaimType:
    payload = claim_type.model_dump(mode="python")
    payload.update(
        artifact_format="playbill-claim-type-v6",
        description=None,
        member_descriptions=(),
        default_role=None,
        evidence_requirement=None,
        revision_evidence=None,
    )
    return ClaimType.model_validate(payload)


def test_a_new_claim_type_lowers_to_v7_replace_and_self() -> None:
    lowered = _lower(_input(), {})
    assert lowered.artifact_format == "playbill-claim-type-v7"
    assert (lowered.evidence_requirement, lowered.revision_evidence) == ("self", "replace")
    # The template's own bytes do not name any v7 field.
    from cruxible_core.claims.claim_type_inputs import claim_type_input_template

    assert not set(V7_FIELDS) & set(claim_type_input_template().model_dump(mode="json"))


def test_an_edit_of_a_pre_v7_claim_type_keeps_accumulate_and_self_unless_named() -> None:
    predecessor = _as_v6(_lower(_input(), {}))
    tree = {PATH: render_claim_type(predecessor)}
    edited = _lower(_input(permitted_roles=["normative"]), tree)
    assert edited.artifact_format == "playbill-claim-type-v7"
    assert (edited.evidence_requirement, edited.revision_evidence) == ("self", "accumulate")
    assert edited.lifecycle.predecessor_digest == claim_type_digest(predecessor).tagged
    chosen = _lower(_input(revision_evidence="replace", evidence_requirement="captured"), tree)
    assert (chosen.evidence_requirement, chosen.revision_evidence) == ("captured", "replace")
    # A v7 predecessor's own choice is what an unrelated edit keeps.
    tree = {PATH: render_claim_type(chosen)}
    again = _lower(_input(description="Status."), tree)
    assert (again.evidence_requirement, again.revision_evidence) == ("captured", "replace")


def test_authored_descriptions_are_normalized_and_members_sorted() -> None:
    lowered = _lower(
        _input(
            literal_schema={"enum": ["ready", "done"], "type": "string"},
            description="  Where the work stands. ",
            member_descriptions=[
                {"member": "ready", "description": " Can start. "},
                {"member": "done", "description": "Finished."},
            ],
            default_role="observation",
        ),
        {},
    )
    assert lowered.description == "Where the work stands."
    assert [(item.member, item.description) for item in lowered.member_descriptions] == [
        ("done", "Finished."),
        ("ready", "Can start."),
    ]
    assert lowered.default_role == "observation"


def test_lowering_never_falls_back_to_v5() -> None:
    """Every input lowers to v7 with identity rules; an unaccepted exact digest refuses."""

    from cruxible_core.claims.claim_type_inputs import ClaimTypeInputReferenceError

    unaccepted = {
        "rules": [
            {
                "rule_id": "exact",
                "claim_roles": ["normative", "observation"],
                "capture_contract_digests": ["sha256:" + "7" * 64],
                "evidence_kinds": ["self_asserted"],
                "admission": "direct",
                "subject_binding": "exact_claim_subject",
            }
        ]
    }
    # An exact version no accepted contract carries has no identity to follow:
    # it is refused, never authored as a v5 exact-digest rule.
    with pytest.raises(
        ClaimTypeInputReferenceError, match="name the contract in capture_contracts"
    ):
        _lower(_input(evidence_admission_policy=unaccepted, anticipated_source_ids=[]), {})
    tree = {PATH: render_claim_type(_lower(_input(), {}))}
    with pytest.raises(
        ClaimTypeInputReferenceError, match="name the contract in capture_contracts"
    ):
        _lower(_input(evidence_admission_policy=unaccepted, anticipated_source_ids=[]), tree)
    with pytest.raises(ClaimTypeInputReferenceError, match="need ClaimType v7"):
        _lower(_input(description="Status."), {}, identity_rules=False)


def _described_v7() -> ClaimType:
    return _lower(
        _input(
            literal_schema={"enum": ["done", "ready"], "type": "string"},
            description="Where the work stands.",
            member_descriptions=[
                {"member": "done", "description": "Finished."},
                {"member": "ready", "description": "Can start."},
            ],
            default_role="normative",
            revision_evidence="accumulate",
        ),
        {},
    )


def test_an_edit_that_omits_the_v7_fields_keeps_every_one_of_them() -> None:
    predecessor = _described_v7()
    tree = {PATH: render_claim_type(predecessor)}
    # An unrelated edit: an extra permitted role, nothing about meaning.
    edited = _lower(
        _input(
            literal_schema={"enum": ["done", "ready"], "type": "string"},
            permitted_roles=["environment_binding", "normative", "observation"],
        ),
        tree,
    )
    for field in V7_FIELDS:
        assert getattr(edited, field) == getattr(predecessor, field), field
    assert edited.permitted_roles == ("environment_binding", "normative", "observation")


def test_an_explicit_null_clears_a_v7_field_and_survives_the_wire() -> None:
    from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord

    tree = {PATH: render_claim_type(_described_v7())}
    clearing = _input(
        literal_schema={"enum": ["done", "ready"], "type": "string"},
        description=None,
        member_descriptions=None,
        default_role=None,
    )
    # What the CLI and the HTTP client send is model_dump: null stays null.
    wire = clearing.model_dump(mode="json")  # type: ignore[attr-defined]
    assert {"description": None, "member_descriptions": None, "default_role": None}.items() <= (
        wire.items()
    )
    cleared = _lower(ClaimTypeInputRecord.model_validate(wire), tree)
    assert (cleared.description, cleared.member_descriptions, cleared.default_role) == (
        None,
        (),
        None,
    )
    assert cleared.revision_evidence == "accumulate"  # still kept from the predecessor
    emptied = _lower(
        _input(
            literal_schema={"enum": ["done", "ready"], "type": "string"}, member_descriptions=[]
        ),
        tree,
    )
    assert emptied.member_descriptions == ()
    assert emptied.description == "Where the work stands."
    # An input that states nothing v7 dumps exactly as before.
    assert not set(V7_FIELDS) & set(_input().model_dump(mode="json"))  # type: ignore[attr-defined]
    with pytest.raises(ValidationError, match="cannot be null"):
        _input(revision_evidence=None)


def test_inherited_descriptions_of_members_the_enum_dropped_are_refused_by_name() -> None:
    from cruxible_core.claims.claim_type_inputs import ClaimTypeMemberDescriptionsStale

    tree = {PATH: render_claim_type(_described_v7())}
    with pytest.raises(ClaimTypeMemberDescriptionsStale) as refused:
        _lower(_input(literal_schema={"enum": ["done", "shipped"], "type": "string"}), tree)
    assert refused.value.error_code == "cruxible.claim_type.member_descriptions_stale"
    assert "'ready'" in str(refused.value) and "'done'" not in str(refused.value)
    with pytest.raises(ClaimTypeMemberDescriptionsStale):
        _lower(_input(literal_schema={"type": "string"}), tree)
    # Restating or clearing them is the way through.
    _lower(
        _input(
            literal_schema={"enum": ["done", "shipped"], "type": "string"},
            member_descriptions=None,
        ),
        tree,
    )


def test_an_inherited_default_role_the_edit_no_longer_permits_is_refused() -> None:
    from cruxible_core.claims.claim_type_inputs import ClaimTypeDefaultRoleNotPermitted

    tree = {PATH: render_claim_type(_described_v7())}
    edited_roles = {
        "literal_schema": {"enum": ["done", "ready"], "type": "string"},
        "permitted_roles": ["observation"],
    }
    with pytest.raises(ClaimTypeDefaultRoleNotPermitted) as refused:
        _lower(_input(**edited_roles), tree)
    assert refused.value.error_code == "cruxible.claim_type.default_role_not_permitted"
    assert "'normative'" in str(refused.value)
    assert _lower(_input(**edited_roles, default_role=None), tree).default_role is None
    assert (
        _lower(_input(**edited_roles, default_role="observation"), tree).default_role
        == "observation"
    )


def test_over_a_pre_v7_predecessor_descriptions_start_empty() -> None:
    predecessor = _as_v6(_lower(_input(), {}))
    edited = _lower(_input(), {PATH: render_claim_type(predecessor)})
    assert (edited.description, edited.member_descriptions, edited.default_role) == (
        None,
        (),
        None,
    )
    assert (edited.evidence_requirement, edited.revision_evidence) == ("self", "accumulate")

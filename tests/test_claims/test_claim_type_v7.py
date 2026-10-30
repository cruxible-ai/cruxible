"""A ClaimType's meaning: descriptions, a default role, an evidence requirement, revision evidence.

There is one ClaimType format; an earlier format is refused, never reinterpreted.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
from cruxible_client.contracts.captures import CanonicalDuration, foreign_source_capture_contract
from cruxible_client.contracts.claim_types import (
    CLAIM_TYPE_FORMAT,
    ClaimEvidenceFreshness,
    ClaimFreshnessDuration,
    ClaimType,
    ClaimTypeFormatError,
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
    claim_adjudication_rule,
    claim_adjudication_rule_digest,
    evaluate_claim_verdict,
)
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from tests.test_claims.test_claim_verdicts import (
    STATEMENT_DIGEST,
    _attestation,
    _attestation_capture,
    _capture,
)
from tests.test_claims.test_claims import _claim_type

PATH = claim_type_path("project.work_item.status")


#: What a ClaimType says about its meaning, beside its structure and policies.
MEANING_FIELDS = (
    "description",
    "member_descriptions",
    "default_role",
    "evidence_requirement",
    "revision_evidence",
)
_SOURCE = foreign_source_capture_contract("repo.work-items")
_FRESHNESS = ClaimEvidenceFreshness(stale_after=ClaimFreshnessDuration(microseconds=86_400_000_000))


def v7(**update: object) -> ClaimType:
    """A ClaimType naming a foreign source by identity, stating ``self`` and ``replace``."""

    base = _claim_type().model_dump(mode="python")
    base.update(
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="source",
                    claim_roles=("normative", "observation"),
                    capture_contracts=(
                        ArtifactRef(role=CAPTURE_CONTRACT_REF_ROLE, target=_SOURCE.identity),
                    ),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        evidence_freshness=_FRESHNESS,
        evidence_requirement="self",
        revision_evidence="replace",
    )
    base.update(update)
    return ClaimType.model_validate(base)


# --- One format --------------------------------------------------------------------


@pytest.mark.parametrize(
    "removed",
    [
        "playbill-claim-type-v1",
        "playbill-claim-type-v3",
        "playbill-claim-type-v4",
        "playbill-claim-type-v5",
        "playbill-claim-type-v6",
        "playbill-claim-type-v7",
        "playbill-claim-type-v8",
        "cruxible-claim-type-v9",
    ],
)
def test_a_removed_claim_type_format_is_refused_not_reinterpreted(removed: str) -> None:
    rendered = render_claim_type(v7()).replace(
        CLAIM_TYPE_FORMAT.encode("ascii"), removed.encode("ascii")
    )
    with pytest.raises(ClaimTypeFormatError, match="unsupported ClaimType artifact format"):
        parse_claim_type(rendered, path=PATH)


def test_a_claim_type_omits_absent_meaning_and_states_the_rest() -> None:
    claim_type = v7()
    dumped = claim_type.model_dump(mode="json")
    assert dumped["artifact_format"] == CLAIM_TYPE_FORMAT
    assert not {"description", "default_role"} & set(dumped)
    assert {field: dumped[field] for field in MEANING_FIELDS if field in dumped} == {
        "member_descriptions": [],
        "evidence_requirement": "self",
        "revision_evidence": "replace",
    }
    assert effective_evidence_requirement(claim_type) == "self"
    assert effective_revision_evidence(claim_type) == "replace"
    rendered = render_claim_type(claim_type)
    assert parse_claim_type(rendered, path=PATH) == claim_type
    # The description is part of what the ClaimType is.
    described = v7(description="Where the work item stands.")
    assert claim_type_digest(described) != claim_type_digest(claim_type)


def test_a_claim_type_states_its_evidence_semantics_never_null() -> None:
    with pytest.raises(ValidationError, match="evidence_requirement"):
        v7(evidence_requirement=None)
    with pytest.raises(ValidationError, match="revision_evidence"):
        v7(revision_evidence=None)


# --- default_role ----------------------------------------------------------------


def test_default_role_must_be_permitted_and_never_derivation() -> None:
    assert v7(default_role="observation").default_role == "observation"
    with pytest.raises(ValidationError, match="permitted_roles"):
        v7(default_role="environment_binding")
    with pytest.raises(ValidationError, match="derivation"):
        v7(permitted_roles=("derivation", "observation"), default_role="derivation")


def test_the_claim_type_law_refuses_an_unpermitted_default_role() -> None:
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

    for claim_type in (v7(), v7(evidence_requirement="captured")):
        compiled = rule(claim_type)
        assert isinstance(compiled, ClaimAdjudicationRule)
        assert not compiled.origin_supports and not compiled.origin_replay_exempt
    origin = rule(v7(evidence_requirement="none"))
    assert isinstance(origin, ClaimAdjudicationRule) and origin.origin_supports
    # Whether the origin supports is part of the rule's digest.
    plain = rule(v7())
    assert isinstance(plain, ClaimAdjudicationRule)
    supporting = plain.model_copy(update={"origin_supports": True})
    assert claim_adjudication_rule_digest(supporting) != claim_adjudication_rule_digest(plain)
    with pytest.raises(ValidationError, match="exempt it from replay"):
        ClaimAdjudicationRule.model_validate(
            {**plain.model_dump(mode="json"), "origin_replay_exempt": True}
        )


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
    from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord
    from tests.core_support._claim_type_support import defaulted_claim_type_input_example

    # A complete record (every structural field stated, no meaning field).
    return ClaimTypeInputRecord.model_validate(
        {
            **defaulted_claim_type_input_example().model_dump(mode="json"),
            **update,
        }
    )


def _lower(value: object, tree: dict[str, bytes]) -> ClaimType:
    from cruxible_core.claims.claim_type_inputs import lower_claim_type_input

    return lower_claim_type_input(value, tree=tree)  # type: ignore[arg-type]


def test_a_new_claim_type_takes_replace_and_self() -> None:
    lowered = _lower(_input(canonical_form=None), {})
    assert lowered.artifact_format == CLAIM_TYPE_FORMAT
    assert (lowered.evidence_requirement, lowered.revision_evidence) == ("self", "replace")
    # The template's own bytes do not name any meaning field.
    from cruxible_core.claims.claim_type_inputs import claim_type_input_template

    assert not set(MEANING_FIELDS) & set(claim_type_input_template().model_dump(mode="json"))


def test_an_edit_keeps_the_predecessors_accumulate_and_self_unless_named() -> None:
    predecessor = _lower(_input(revision_evidence="accumulate"), {})
    tree = {PATH: render_claim_type(predecessor)}
    edited = _lower(_input(permitted_roles=["normative"]), tree)
    assert edited.artifact_format == CLAIM_TYPE_FORMAT
    assert (edited.evidence_requirement, edited.revision_evidence) == ("self", "accumulate")
    assert edited.lifecycle.predecessor_digest == claim_type_digest(predecessor).tagged
    chosen = _lower(_input(revision_evidence="replace", evidence_requirement="captured"), tree)
    assert (chosen.evidence_requirement, chosen.revision_evidence) == ("captured", "replace")
    # The predecessor's own choice is what an unrelated edit keeps.
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


def test_lowering_never_authors_an_exact_digest_rule() -> None:
    """Every input lowers to identity rules; an unaccepted exact digest refuses."""

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
    # it is refused, never authored as an exact-digest rule.
    with pytest.raises(
        ClaimTypeInputReferenceError, match="name the contract in capture_contracts"
    ):
        _lower(_input(evidence_admission_policy=unaccepted, anticipated_source_ids=[]), {})
    tree = {PATH: render_claim_type(_lower(_input(), {}))}
    with pytest.raises(
        ClaimTypeInputReferenceError, match="name the contract in capture_contracts"
    ):
        _lower(_input(evidence_admission_policy=unaccepted, anticipated_source_ids=[]), tree)


def _described_v7() -> ClaimType:
    return _lower(
        _input(
            canonical_form=None,
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


def test_an_edit_that_omits_the_meaning_fields_keeps_every_one_of_them() -> None:
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
    for field in MEANING_FIELDS:
        assert getattr(edited, field) == getattr(predecessor, field), field
    assert edited.permitted_roles == ("environment_binding", "normative", "observation")


def test_an_explicit_null_clears_a_meaning_field_and_survives_the_wire() -> None:
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
    # An input that states no meaning field dumps none of them.
    assert not set(MEANING_FIELDS) & set(_input().model_dump(mode="json"))  # type: ignore[attr-defined]
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


def test_over_an_undescribed_predecessor_descriptions_stay_empty() -> None:
    predecessor = _lower(_input(revision_evidence="accumulate"), {})
    edited = _lower(_input(), {PATH: render_claim_type(predecessor)})
    assert (edited.description, edited.member_descriptions, edited.default_role) == (
        None,
        (),
        None,
    )
    assert (edited.evidence_requirement, edited.revision_evidence) == ("self", "accumulate")

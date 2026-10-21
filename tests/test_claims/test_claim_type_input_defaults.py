"""ClaimType input: progressive defaults, merge-patch edits, public names and lints."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.authoring.inputs import (
    AuthoringInputError,
    ChangeSetInput,
    ClaimTypeSuccessionInput,
    lower_authoring_input,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayload,
    ClaimTypeSuccessionDependent,
    ClaimTypeSuccessionMember,
)
from cruxible_client.contracts.authoring_profiles import _contract_ref as profile_contract_ref
from cruxible_client.contracts.builtin_claim_types import SEMANTIC_ALIAS_CLAIM_TYPE
from cruxible_client.contracts.captures import COORDINATOR_SELF_SOURCE_CONTRACT_ID
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.subject_kinds import KeyClaimTypeShape, key_claim_type_problem
from cruxible_client.contracts.temporal import parse_iso_duration_microseconds
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeEvidenceRulesRequired,
    ClaimTypeInputIncomplete,
    ClaimTypeInputRecord,
    ClaimTypeNumberSchemaIntegerOnly,
    claim_type_input_defaults,
    claim_type_input_template,
    lint_claim_type_input,
    lower_claim_type_input,
)
from cruxible_core.service.claims.claim_types import (
    BuiltinClaimTypeAlreadyAdopted,
    BuiltinClaimTypeSuperseded,
    service_propose_playbill_builtin_claim_type,
    service_propose_playbill_claim_type_input,
)
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import TIMESTAMP
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world, _claim_type

_MINIMAL = {
    "predicate": "catalog.part.price",
    "allowed_subject_kinds": ["catalog.part"],
    "object_kind": "literal",
    "literal_schema": {"type": "integer"},
    "cardinality": "one",
}


def _lower(record: dict[str, object], tree: dict[str, bytes] | None = None):  # type: ignore[no-untyped-def]
    return lower_claim_type_input(
        ClaimTypeInputRecord.model_validate(record), tree=tree or {}, identity_rules=True
    )


def test_a_minimal_claim_type_and_its_spelled_out_form_are_one_digest() -> None:
    minimal = ClaimTypeInputRecord.model_validate(_MINIMAL)
    expanded = claim_type_input_defaults(minimal, None)

    assert expanded.permitted_roles == ("normative",)
    assert expanded.admission_policy == {
        "corroboration_requirements": [],
        "freeze_requirements": [],
    }
    assert expanded.resolution_policy is not None
    assert expanded.resolution_policy["selector"] == "only_contender"
    (rule,) = expanded.evidence_admission_policy["rules"]  # type: ignore[index]
    # The default own-words rule names the contract by its public name.
    assert rule["capture_contracts"] == ["cruxible.coordinator-self-source-v1"]
    lowered = _lower(_MINIMAL)
    assert claim_type_digest(lowered) == claim_type_digest(
        lower_claim_type_input(expanded, tree={}, identity_rules=True)
    )
    (stored,) = lowered.evidence_admission_policy.rules  # type: ignore[union-attr]
    assert stored.capture_contracts[0].target.name == COORDINATOR_SELF_SOURCE_CONTRACT_ID
    assert lowered.evidence_requirement == "self"
    assert lowered.revision_evidence == "replace"
    # The wire carries exactly the stated fields, so a server re-reads the same record.
    assert set(minimal.model_dump(mode="json")) == set(_MINIMAL)


def test_defaults_follow_the_stated_role_cardinality_and_sources() -> None:
    lowered = _lower(
        {
            **_MINIMAL,
            "cardinality": "many",
            "default_role": "observation",
            "anticipated_source_ids": ["repo.parts"],
        }
    )

    assert lowered.permitted_roles == ("observation",)
    assert lowered.resolution_policy.selector == "all"
    assert [rule.rule_id for rule in lowered.evidence_admission_policy.rules] == [
        "self",
        "source-repo.parts",
    ]


def test_a_new_claim_type_states_what_no_default_can_fill() -> None:
    with pytest.raises(ClaimTypeInputIncomplete, match="allowed_subject_kinds, cardinality"):
        _lower({"predicate": "catalog.part.price", "object_kind": "literal"})
    with pytest.raises(ClaimTypeEvidenceRulesRequired, match="anticipated_source_ids"):
        _lower({**_MINIMAL, "evidence_requirement": "captured"})
    with pytest.raises(ValueError, match="cannot be null"):
        ClaimTypeInputRecord.model_validate({**_MINIMAL, "cardinality": None})


def test_an_edit_states_only_what_changes_and_keeps_the_rest() -> None:
    original = _lower(
        {**_MINIMAL, "cardinality": "many", "permitted_roles": ["normative", "observation"]}
    )
    tree = {claim_type_path(original.predicate): render_claim_type(original)}

    edited = _lower({"predicate": original.predicate, "description": "Price in cents."}, tree)

    assert edited.description == "Price in cents."
    assert edited.lifecycle.predecessor_digest == claim_type_digest(original).tagged
    before = original.model_dump(mode="json")
    after = edited.model_dump(mode="json")
    assert {key for key in after if after[key] != before.get(key)} == {
        "description",
        "lifecycle",
    }


def test_durations_take_iso_8601_and_store_microseconds() -> None:
    lowered = _lower(
        {**_MINIMAL, "evidence_freshness": {"stale_after": "P30D"}, "unsure_hold_for": "PT12H"}
    )

    assert lowered.evidence_freshness is not None
    assert lowered.evidence_freshness.stale_after.microseconds == 30 * 86_400_000_000
    assert lowered.unsure_hold_for is not None
    assert lowered.unsure_hold_for.microseconds == 12 * 3_600_000_000
    assert parse_iso_duration_microseconds("P1W") == 7 * 86_400_000_000
    assert parse_iso_duration_microseconds("PT1.5S") == 1_500_000
    with pytest.raises(ValueError, match="P30D"):
        parse_iso_duration_microseconds("30 days")


def test_a_number_schema_refuses_at_definition_and_an_integer_lands() -> None:
    with pytest.raises(ClaimTypeNumberSchemaIntegerOnly, match="type: integer"):
        _lower({**_MINIMAL, "literal_schema": {"type": "number"}})
    with pytest.raises(ClaimTypeNumberSchemaIntegerOnly):
        _lower({**_MINIMAL, "literal_schema": {"type": "array", "items": {"type": "number"}}})
    assert _lower(_MINIMAL).literal_schema == {"type": "integer"}


def test_a_rule_names_a_reserved_contract_by_either_spelling() -> None:
    def rule_with(name: str) -> dict[str, object]:
        return {
            **_MINIMAL,
            "evidence_admission_policy": {
                "rules": [
                    {
                        "rule_id": "self",
                        "claim_roles": ["normative"],
                        "capture_contracts": [name],
                        "evidence_kinds": ["self_asserted"],
                        "admission": "direct",
                        "subject_binding": "exact_claim_subject",
                    }
                ]
            },
        }

    public = _lower(rule_with("cruxible.coordinator-self-source-v1"))
    stored = _lower(rule_with(COORDINATOR_SELF_SOURCE_CONTRACT_ID))
    qualified = _lower(rule_with("CaptureContract:cruxible.coordinator-self-source-v1"))
    assert claim_type_digest(public) == claim_type_digest(stored) == claim_type_digest(qualified)
    assert (
        profile_contract_ref({"contract": "cruxible.foreign-source.repo.x"}, "contract").target.name
        == "playbill.foreign-source.repo.x"
    )


def test_the_template_is_minimal_and_keys_a_subject_kind() -> None:
    template = claim_type_input_template()
    dumped = template.model_dump(mode="json")

    assert dumped["canonical_form"] == []
    assert "evidence_admission_policy" not in dumped
    assert "playbill." not in str(claim_type_input_template(expanded=True).model_dump())
    lowered = lower_claim_type_input(template, tree={}, identity_rules=True)
    assert lowered.artifact_format == "playbill-claim-type-v8"
    v7 = KeyClaimTypeShape(
        predicate=template.predicate,
        live=True,
        artifact_format="playbill-claim-type-v7",
        object_kind="literal",
        cardinality="one",
        allowed_subject_kinds=("project.work_item",),
    )
    problem = key_claim_type_problem("project.work_item", v7, template.predicate)
    assert problem is not None
    assert "canonical_form ([] for none)" in problem


def test_lint_is_quiet_for_own_words_on_a_fresh_instance(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)

    lint = lint_claim_type_input(
        instance,
        ClaimTypeInputRecord.model_validate(_MINIMAL),
        coordinate=instance.accepted_coordinate(),
    )

    assert lint.warnings == ()


def test_lint_names_an_unadmitted_role_and_a_reserved_field_name(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    record = ClaimTypeInputRecord.model_validate(
        {
            **_MINIMAL,
            "predicate": "catalog.part.kind",
            "permitted_roles": ["normative", "observation"],
            "evidence_admission_policy": {
                "rules": [
                    {
                        "rule_id": "self",
                        "claim_roles": ["normative"],
                        "capture_contracts": ["cruxible.coordinator-self-source-v1"],
                        "evidence_kinds": ["self_asserted"],
                        "admission": "direct",
                        "subject_binding": "exact_claim_subject",
                    }
                ]
            },
        }
    )

    lint = lint_claim_type_input(instance, record, coordinate=instance.accepted_coordinate())

    by_code = {item.code: item for item in lint.warnings}
    assert set(by_code) == {
        "cruxible.claim_type.field_name_reserved",
        "cruxible.claim_type.permitted_role_unadmitted",
    }
    assert by_code["cruxible.claim_type.permitted_role_unadmitted"].field_path == (
        "$.permitted_roles[1]"
    )
    assert by_code["cruxible.claim_type.field_name_reserved"].replacement_rule_fragment == {
        "reserved_field_name": "kind"
    }


def test_adopting_a_builtin_the_instance_holds_refuses_with_the_read(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = claim_type_path(SEMANTIC_ALIAS_CLAIM_TYPE.predicate)
    if path not in tree:
        pytest.skip("this genesis seeds no semantic.alias")
    held = parse_claim_type(tree[path], path=path)
    error = (
        BuiltinClaimTypeAlreadyAdopted
        if held == SEMANTIC_ALIAS_CLAIM_TYPE
        else BuiltinClaimTypeSuperseded
    )

    with pytest.raises(error, match="cruxible get ClaimType:semantic.alias"):
        service_propose_playbill_builtin_claim_type(
            instance,
            builtin="semantic.alias",
            actor_id="owner",
            proposal_name="adopt-alias",
            timestamp=TIMESTAMP,
        )


def test_an_edit_of_a_claim_type_with_live_claims_carries_them(tmp_path: Path) -> None:
    instance, claim_identity, _owner = _accepted_claim_world(tmp_path)

    result = service_propose_playbill_claim_type_input(
        instance,
        input=ClaimTypeInputRecord.model_validate(
            {"predicate": _claim_type().predicate, "description": "Where the item stands."}
        ),
        actor_id="owner",
        proposal_name="describe-status",
        timestamp="2026-08-21T12:30:00.000000Z",
    )

    proposal = result.proposal.proposal
    assert proposal.evaluation.verdict == "candidate", proposal.evaluation.diagnostics
    assert proposal.candidate is not None
    changed = {member.path for member in proposal.candidate.members}
    assert claim_type_path(_claim_type().predicate) in changed
    assert any(claim_identity.removeprefix("Claim:") in path for path in changed)


def test_unsorted_succession_dependents_are_sorted_and_a_shape_error_is_typed() -> None:
    successor = SEMANTIC_ALIAS_CLAIM_TYPE.model_copy(
        update={
            "lifecycle": SEMANTIC_ALIAS_CLAIM_TYPE.lifecycle.model_copy(
                update={"predecessor_digest": "sha256:" + "0" * 64}
            )
        }
    )
    dependents = tuple(
        ClaimTypeSuccessionDependent(
            identity=ArtifactIdentity(kind="Claim", name=name), disposition="successor"
        )
        for name in ("CLM-" + "b" * 32, "CLM-" + "a" * 32)
    )
    lowered = lower_authoring_input(
        ChangeSetInput(
            kind="change_set",
            members=(
                ClaimTypeSuccessionInput(
                    kind="claim_type_succession", successor=successor, dependents=dependents
                ),
            ),
        )
    )
    assert isinstance(lowered, ChangeSetAuthoringPayload)
    (member,) = lowered.members
    assert isinstance(member, ClaimTypeSuccessionMember)
    assert [item.identity.name for item in member.dependents] == sorted(
        item.identity.name for item in dependents
    )

    with pytest.raises(AuthoringInputError) as refused:
        lower_authoring_input(
            ChangeSetInput(
                kind="change_set",
                members=(
                    ClaimTypeSuccessionInput(
                        kind="claim_type_succession", successor=SEMANTIC_ALIAS_CLAIM_TYPE
                    ),
                ),
            )
        )
    assert refused.value.code == "cruxible.authoring.input_invalid"
    assert refused.value.field_path == "input.members[0]"
    assert "names the predecessor it succeeds" in refused.value.message

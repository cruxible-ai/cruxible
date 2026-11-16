"""Evidence rules are for shaped contracts; workspace citations need no rule.

Maintainer ruling 2026-11-16 (coldkit-16, revised): the lowered default admits own
words and workspace file and section citations together; the short form is
``evidence: [<shaped contract>, ...]``; anticipated_source_ids, authored family
rules and newly added per-file rules refuse; a ClaimType already holding a
per-file or family rule stays editable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayload,
    ClaimTypeAuthoringPayload,
)
from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_client.contracts.claim_type_inputs import (
    ClaimTypeEvidenceRulesRequired,
    ClaimTypeFamilyRuleNotAuthored,
    ClaimTypePerFileEvidenceRule,
    ClaimTypeSourcesRetired,
)
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_core.authoring.lowering import (
    AuthoringLoweringError,
    _refuse_added_per_file_evidence_rules,
)
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeInputRecord,
    lint_claim_type_input,
    lower_claim_type_input,
)
from cruxible_core.claims.claim_type_migrations import resolve_claim_type_succession
from cruxible_core.service.claims.claim_types import refuse_new_workspace_rules
from tests.core_support._support import initialize_local
from tests.support.claim_types import claim_type as build_claim_type

PREDICATE = "catalog.part.lead_time"
PAGE = foreign_source_capture_contract("quotes")
PER_FILE_RULE = {
    "rule_id": "supplier-quote",
    "claim_roles": ["observation"],
    "capture_contracts": ["cruxible.foreign-source.quotes"],
    "admission": "direct",
    "subject_binding": "exact_claim_subject",
}
_RECORD = {
    "predicate": PREDICATE,
    "allowed_subject_kinds": ["catalog.part"],
    "value": "int",
    "cardinality": "one",
    "permitted_roles": ["observation"],
}


def _held_tree() -> dict[str, bytes]:
    """An accepted ClaimType that already holds a per-file rule (from before the ruling)."""

    held = build_claim_type(
        predicate=PREDICATE,
        allowed_subject_kinds=("catalog.part",),
        value_type={"type": "integer"},
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="supplier-quote",
                    claim_roles=("observation",),
                    capture_contracts=(PAGE.identity.name,),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )
    return {claim_type_path(PREDICATE): render_claim_type(held)}


def _rules(claim_type):  # type: ignore[no-untyped-def]
    return [
        (
            rule.rule_id,
            rule.claim_roles,
            rule.admission,
            tuple(item.target.name for item in rule.capture_contracts),
            rule.capture_contract_families,
        )
        for rule in claim_type.evidence_admission_policy.rules
    ]


def test_the_default_admits_own_words_and_workspace_citations_in_one_rule() -> None:
    lowered = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate({**_RECORD, "permitted_roles": ["normative"]}),
        tree={},
    )
    assert _rules(lowered) == [
        (
            "self",
            ("normative",),
            "direct",
            ("playbill.coordinator-self-source-v1",),
            ("workspace_file",),
        )
    ]


def test_evidence_lists_shaped_contracts_for_the_fields_roles() -> None:
    lowered = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate(
            {
                **_RECORD,
                "permitted_roles": ["derivation", "observation"],
                "evidence": ["acme.quote"],
            }
        ),
        tree={},
    )
    assert _rules(lowered) == [
        ("derivation", ("derivation",), "derivational", ("acme.quote",), ()),
        ("evidence", ("observation",), "direct", ("acme.quote",), ()),
        (
            "self",
            ("observation",),
            "direct",
            ("playbill.coordinator-self-source-v1",),
            ("workspace_file",),
        ),
    ]
    # captured: a shaped payload only, no own words and no workspace citations.
    captured = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate({**_RECORD, "evidence": ["acme.quote"]}), tree={}
    )
    assert captured.evidence_requirement == "captured"
    assert _rules(captured) == [("evidence", ("observation",), "direct", ("acme.quote",), ())]
    with pytest.raises(ClaimTypeEvidenceRulesRequired, match=r"evidence: \[<contract name>"):
        lower_claim_type_input(ClaimTypeInputRecord.model_validate(_RECORD), tree={})


def test_retired_and_conflicting_short_forms_refuse_naming_evidence() -> None:
    retired = ClaimTypeInputRecord.model_validate({**_RECORD, "anticipated_source_ids": ["quotes"]})
    with pytest.raises(ClaimTypeSourcesRetired) as caught:
        lower_claim_type_input(retired, tree={})
    assert caught.value.error_code == "cruxible.claim_type.sources_retired"
    assert "evidence: [<contract name>, ...]" in str(caught.value)
    assert caught.value.repair.hand_edit.target == "anticipated_source_ids"
    assert "evidence: [<contract name>, ...]" in caught.value.repair.hand_edit.required_change
    with pytest.raises(ValueError, match="not both"):
        ClaimTypeInputRecord.model_validate(
            {
                **_RECORD,
                "evidence": ["acme.quote"],
                "evidence_admission_policy": {"rules": [PER_FILE_RULE]},
            }
        )


def test_an_authored_family_rule_refuses_but_a_held_one_stays() -> None:
    family = {
        "rule_id": "files",
        "claim_roles": ["observation"],
        "capture_contract_families": ["workspace_file"],
        "admission": "direct",
        "subject_binding": "exact_claim_subject",
    }
    authored = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate(
            {**_RECORD, "evidence_admission_policy": {"rules": [family]}}
        ),
        tree={},
    )
    with pytest.raises(ClaimTypeFamilyRuleNotAuthored, match="needs no rule"):
        refuse_new_workspace_rules(authored, {})
    held = build_claim_type(
        predicate=PREDICATE,
        allowed_subject_kinds=("catalog.part",),
        value_type={"type": "integer"},
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(ClaimEvidenceAdmissionRule.model_validate(family),)
        ),
    )
    held_tree = {claim_type_path(PREDICATE): render_claim_type(held)}
    edited = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate({"predicate": PREDICATE, "description": "Days."}),
        tree=held_tree,
    )
    refuse_new_workspace_rules(edited, held_tree)
    assert edited.evidence_admission_policy.rules[0].capture_contract_families == (
        "workspace_file",
    )


def test_a_new_rule_naming_a_per_file_contract_refuses_with_the_shaped_repair() -> None:
    record = ClaimTypeInputRecord.model_validate(
        {**_RECORD, "evidence_admission_policy": {"rules": [PER_FILE_RULE]}}
    )
    with pytest.raises(ClaimTypePerFileEvidenceRule) as caught:
        refuse_new_workspace_rules(lower_claim_type_input(record, tree={}), {})
    message = str(caught.value)
    assert message.startswith("cruxible.claim_type.per_file_evidence_rule:")
    assert "cruxible.foreign-source.quotes" in message
    assert "needs no rule" in message
    assert "evidence: [<contract name>, ...]" in message


def test_a_claim_type_already_holding_a_per_file_rule_stays_editable() -> None:
    tree = _held_tree()
    edited = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate({"predicate": PREDICATE, "description": "Days."}),
        tree=tree,
    )
    assert edited.description == "Days."
    refuse_new_workspace_rules(edited, tree)
    assert edited.evidence_admission_policy.rules[0].capture_contracts[0].target.name == (
        PAGE.identity.name
    )
    # The standalone migration road keeps it too.
    resolve_claim_type_succession(
        tree, ClaimTypeInputRecord.model_validate({"predicate": PREDICATE, "description": "D."})
    )
    # A second per-file contract added beside it is new, and refuses.
    second = {
        **PER_FILE_RULE,
        "rule_id": "z-other",
        "capture_contracts": ["cruxible.foreign-source.other"],
    }
    widened = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate(
            {
                "predicate": PREDICATE,
                "evidence_admission_policy": {"rules": [PER_FILE_RULE, second]},
            }
        ),
        tree=tree,
    )
    with pytest.raises(ClaimTypePerFileEvidenceRule, match="foreign-source.other"):
        refuse_new_workspace_rules(widened, tree)


def test_authoring_refuses_a_change_set_member_newly_naming_one_and_keeps_a_held_one() -> None:
    held_tree = _held_tree()
    from cruxible_client.contracts.claim_types import parse_claim_type

    held = parse_claim_type(held_tree[claim_type_path(PREDICATE)], path=claim_type_path(PREDICATE))
    fresh = held.model_copy(
        update={
            "predicate": "catalog.part.price",
            "identity": held.identity.model_copy(update={"name": "catalog.part.price"}),
        }
    )
    payload = ChangeSetAuthoringPayload(
        rationale="Add a price field.", members=(ClaimTypeAuthoringPayload(claim_type=fresh),)
    )
    with pytest.raises(AuthoringLoweringError) as caught:
        _refuse_added_per_file_evidence_rules(payload, held_tree)
    assert caught.value.code == "cruxible.claim_type.per_file_evidence_rule"
    assert caught.value.offending_element == (
        "members[0].claim_type.evidence_admission_policy.rules[0].capture_contracts"
    )
    assert caught.value.repairs[0].replacement == {
        "drop_contract": "cruxible.foreign-source.quotes"
    }
    # The same ClaimType re-proposed unchanged over its accepted self passes.
    _refuse_added_per_file_evidence_rules(
        ChangeSetAuthoringPayload(
            rationale="Re-propose.", members=(ClaimTypeAuthoringPayload(claim_type=held),)
        ),
        held_tree,
    )


def test_the_source_intent_lint_proposes_no_per_source_rule(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    record = ClaimTypeInputRecord.model_validate({**_RECORD, "permitted_roles": ["normative"]})
    lint = lint_claim_type_input(
        instance,
        record,
        coordinate=instance.accepted_coordinate(),
        anticipated_source_ids=("quotes",),
    )
    codes = {item.code for item in lint.warnings}
    assert "cruxible.claim_type.anticipated_source_contract_omitted" not in codes
    assert not any(
        "foreign-source" in str(item.replacement_rule_fragment) for item in lint.warnings
    )


def test_vocabulary_export_says_a_short_form_policy_as_evidence() -> None:
    from cruxible_client.authoring.vocabulary_file import _minimal_input

    lowered = lower_claim_type_input(
        ClaimTypeInputRecord.model_validate({**_RECORD, "evidence": ["acme.quote"]}), tree={}
    )
    minimal = _minimal_input(lowered)
    assert minimal.get("evidence") == ["acme.quote"]
    assert "evidence_admission_policy" not in minimal

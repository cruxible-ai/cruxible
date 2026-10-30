"""A ClaimType's inverse: how a Claim of the type states that a unit holds nothing."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.claim_types import (
    CLAIM_TYPE_FORMAT,
    ClaimFreshnessDuration,
    ClaimInverse,
    ClaimInverseObjectKey,
    ClaimInverseObjectKeyPart,
    ClaimInverseSlotKey,
    ClaimType,
    claim_type_digest,
    claim_type_format_version,
    claim_type_path,
    effective_inverse_stale_after,
    evaluate_claim_type_law,
    parse_claim_type,
    render_claim_type,
)
from tests.test_claims.test_claim_type_v7 import v7
from tests.test_claims.test_claim_type_v8 import v8

PATH = claim_type_path("project.work_item.status")
DAY = ClaimFreshnessDuration(microseconds=86_400_000_000)


def slot_inverse(**update: object) -> ClaimInverse:
    return ClaimInverse.model_validate(
        {"key": {"kind": "slot"}, "evidence_rules": ["source"], **update}
    )


def v9(inverse: ClaimInverse | None = None, **update: object) -> ClaimType:
    base = v8(()).model_dump(mode="python")
    base.update(inverse=inverse or slot_inverse())
    base.update(update)
    return ClaimType.model_validate(base)


def many_subject(inverse: ClaimInverse, **update: object) -> ClaimType:
    """A many-valued, Subject-valued type with an inverse: ``project.work_item.blocks``."""

    resolution = v7().resolution_policy.model_copy(
        update={"cardinality": "many", "selector": "all"}
    )
    return v9(
        inverse,
        identity={"kind": "ClaimType", "name": "project.work_item.blocks"},
        predicate="project.work_item.blocks",
        object_kind="subject",
        literal_schema=None,
        member_descriptions=(),
        allowed_object_subject_kinds=("project.release",),
        cardinality="many",
        resolution_policy=resolution,
        **update,
    )


# --- the format ----------------------------------------------------------------


def test_a_claim_type_without_an_inverse_never_ages_one() -> None:
    for claim_type in (v7(), v8()):
        assert "inverse" not in claim_type.model_dump(mode="json")
        assert effective_inverse_stale_after(claim_type) is None


def test_an_inverse_is_written_round_trips_and_is_digested() -> None:
    claim_type = v9(slot_inverse(stale_after={"microseconds": 3_600_000_000}))
    dumped = claim_type.model_dump(mode="json")
    assert dumped["artifact_format"] == CLAIM_TYPE_FORMAT
    assert dumped["inverse"]["tag"] == "cruxible-claim-inverse-v1"
    assert dumped["inverse"]["key"] == {"kind": "slot"}
    assert parse_claim_type(render_claim_type(claim_type), path=PATH) == claim_type
    assert claim_type_digest(claim_type) != claim_type_digest(v8(()))
    assert claim_type_format_version(claim_type) == 10
    assert effective_inverse_stale_after(claim_type) == ClaimFreshnessDuration(
        microseconds=3_600_000_000
    )
    # Without its own horizon an inverse ages on the type's evidence freshness.
    assert effective_inverse_stale_after(v9()) == DAY


def test_a_type_without_an_inverse_omits_it() -> None:
    claim_type = v9(inverse=None)
    plain = ClaimType.model_validate({**claim_type.model_dump(mode="python"), "inverse": None})
    assert "inverse" not in plain.model_dump(mode="json")
    assert parse_claim_type(render_claim_type(plain), path=PATH) == plain


def test_any_type_may_declare_an_inverse_and_still_states_its_canonical_form() -> None:
    formed = v8(inverse=slot_inverse())
    assert formed.inverse == slot_inverse() and formed.canonical_form == ("trim", "casefold")
    with pytest.raises(ValidationError, match="canonical_form"):
        v9(canonical_form=None)


def test_the_key_fits_the_cardinality_and_object() -> None:
    with pytest.raises(ValidationError, match="keyed by object needs a many-valued"):
        v9(ClaimInverse(key=ClaimInverseObjectKey(kind="object"), evidence_rules=("source",)))
    with pytest.raises(ValidationError, match="keyed by slot needs a one-valued"):
        many_subject(slot_inverse())
    literal_many = many_subject(
        ClaimInverse(key=ClaimInverseObjectKey(kind="object"), evidence_rules=("source",))
    ).model_dump(mode="python")
    literal_many.update(
        object_kind="literal",
        literal_schema={"type": "string"},
        allowed_object_subject_kinds=(),
        inverse=ClaimInverse(
            key=ClaimInverseObjectKeyPart(kind="object_key", predicate="project.release.name"),
            evidence_rules=("source",),
        ),
    )
    with pytest.raises(ValidationError, match="object_key needs a Subject-valued"):
        ClaimType.model_validate(literal_many)
    assert many_subject(
        ClaimInverse(key=ClaimInverseObjectKey(kind="object"), evidence_rules=("source",))
    )


def test_inverse_rules_are_sorted_unique_and_nonempty() -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        slot_inverse(evidence_rules=["source", "a"])
    with pytest.raises(ValidationError):
        slot_inverse(evidence_rules=[])
    with pytest.raises(ValidationError, match="must be positive"):
        slot_inverse(stale_after={"microseconds": 0})


# --- the law -------------------------------------------------------------------


def test_the_one_claim_type_law_accepts_an_inverse() -> None:
    from cruxible_client.contracts.laws import ACCEPTANCE_LAWS, CLAIM_TYPE_LAW

    accepted = evaluate_claim_type_law(v9(), path=PATH, predecessor=None)
    assert accepted.verdict == "accepted", accepted
    installed = ACCEPTANCE_LAWS.resolve_member(artifact_tag=CLAIM_TYPE_FORMAT)
    assert installed.coordinate == CLAIM_TYPE_LAW


def test_the_law_refuses_rules_the_type_does_not_declare() -> None:
    refused = evaluate_claim_type_law(
        v9(slot_inverse(evidence_rules=["scan"])), path=PATH, predecessor=None
    )
    assert refused.verdict == "refused"
    assert refused.diagnostics[0].code == "cruxible.claim_type.inverse_rule_unresolved"


def test_the_law_refuses_an_exact_content_inverse() -> None:
    exact = v9(
        ClaimInverse(key=ClaimInverseObjectKey(kind="object"), evidence_rules=("source",)),
        object_kind="exact_content",
        literal_schema=None,
        member_descriptions=(),
        cardinality="many",
        resolution_policy=v7().resolution_policy.model_copy(
            update={"cardinality": "many", "selector": "all"}
        ),
    )
    refused = evaluate_claim_type_law(exact, path=PATH, predecessor=None)
    assert refused.diagnostics[0].code == "cruxible.claim_type.inverse_object_kind"


def test_an_object_key_must_be_a_key_of_every_object_kind() -> None:
    keyed = many_subject(
        ClaimInverse(
            key=ClaimInverseObjectKeyPart(kind="object_key", predicate="project.release.name"),
            evidence_rules=("source",),
        )
    )
    path = claim_type_path(keyed.predicate)
    accepted = evaluate_claim_type_law(
        keyed,
        path=path,
        predecessor=None,
        subject_kind_keys={"project.release": ("project.release.name",)},
    )
    assert accepted.verdict == "accepted", accepted
    for keys in ({"project.release": ("project.release.version",)}, {"project.release": None}):
        refused = evaluate_claim_type_law(
            keyed, path=path, predecessor=None, subject_kind_keys=keys
        )
        assert refused.diagnostics[0].code == "cruxible.claim_type.inverse_key_unresolved"
    unread = evaluate_claim_type_law(keyed, path=path, predecessor=None)
    assert unread.diagnostics[0].code == "cruxible.claim_type.inverse_key_unresolved"


# --- lowering authored input -----------------------------------------------------


def test_lowering_states_an_inverse_and_an_edit_keeps_it_unless_named() -> None:
    from tests.test_claims.test_claim_type_v7 import _input, _lower

    rule = "source-repo.replace-me"
    stated = _lower(
        _input(inverse={"key": {"kind": "slot"}, "evidence_rules": [rule], "stale_after": "P7D"}),
        {},
    )
    assert stated.artifact_format == CLAIM_TYPE_FORMAT
    assert stated.canonical_form == ("nfc", "trim")
    assert stated.inverse == ClaimInverse(
        key=ClaimInverseSlotKey(kind="slot"),
        evidence_rules=(rule,),
        stale_after=ClaimFreshnessDuration(microseconds=7 * 86_400_000_000),
    )
    tree = {claim_type_path(stated.predicate): render_claim_type(stated)}
    kept = _lower(_input(description="Where it stands."), tree)
    assert kept.inverse == stated.inverse
    assert kept.canonical_form == stated.canonical_form
    cleared = _lower(_input(inverse=None), tree)
    assert cleared.inverse is None
    assert "inverse" not in cleared.model_dump(mode="json")
    assert cleared.lifecycle.predecessor_digest == claim_type_digest(stated).tagged
    # Over a type without an inverse, leaving the field out declares none.
    formed = _lower(_input(canonical_form=["trim"]), {})
    over = _lower(_input(), {claim_type_path(formed.predicate): render_claim_type(formed)})
    assert over.inverse is None
    lifted = _lower(
        _input(inverse={"key": {"kind": "slot"}, "evidence_rules": [rule]}),
        {claim_type_path(formed.predicate): render_claim_type(formed)},
    )
    assert lifted.inverse is not None and lifted.canonical_form == ("trim",)


def test_lint_warns_when_an_enum_member_already_states_the_negative(tmp_path: Path) -> None:
    from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord, lint_claim_type_input
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    record = {
        "predicate": "project.work_item.phase",
        "description": "The work item's phase.",
        "allowed_subject_kinds": ["project.work_item"],
        "object_kind": "literal",
        "literal_schema": {"enum": ["done", "not_done"], "type": "string"},
        "cardinality": "one",
        "inverse": {"key": {"kind": "slot"}, "evidence_rules": ["self"]},
    }

    lint = lint_claim_type_input(
        instance,
        ClaimTypeInputRecord.model_validate(record),
        coordinate=instance.accepted_coordinate(),
    )

    assert [item.code for item in lint.warnings] == [
        "cruxible.claim_type.inverse_on_enum_or_boolean"
    ]
    plain = lint_claim_type_input(
        instance,
        ClaimTypeInputRecord.model_validate({**record, "literal_schema": {"type": "string"}}),
        coordinate=instance.accepted_coordinate(),
    )
    assert plain.warnings == ()


def test_a_subject_kind_cannot_drop_a_key_an_inverse_names(tmp_path: Path) -> None:
    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        ClaimTypeInput,
        SubjectKindInput,
        lower_authoring_input,
    )
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from tests.core_support._identity_support import OWNER, identity_claim_type
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    form = ("nfkc", "trim", "collapse_whitespace", "casefold")
    slug = identity_claim_type(
        "slug",
        canonical_form=form,
        literal_schema={"type": "string"},
        predicate="dev.decision.slug",
        subject_kinds=("dev.decision",),
    )
    code = identity_claim_type(
        "code",
        canonical_form=form,
        literal_schema={"type": "string"},
        predicate="dev.decision.code",
        subject_kinds=("dev.decision",),
    )
    decided_by = identity_claim_type(
        "decided_by",
        canonical_form=(),
        object_kind="subject",
        cardinality="many",
        object_kinds=("dev.decision",),
        predicate="dev.item.decided_by",
        subject_kinds=("dev.item",),
    ).model_dump(mode="python")
    decided_by.update(
        inverse={
            "key": {"kind": "object_key", "predicate": "dev.decision.slug"},
            "evidence_rules": ["self"],
        },
    )

    def submit(*members: object) -> object:
        return AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
            actor=OWNER,
            payload=lower_authoring_input(ChangeSetInput(kind="change_set", members=members)),
            canonical_timestamp="2026-10-22T12:00:00.000000Z",
        )

    submitted = submit(
        ClaimTypeInput(kind="claim_type", claim_type=slug),
        ClaimTypeInput(kind="claim_type", claim_type=code),
        SubjectKindInput(
            kind="subject_kind",
            name="dev.decision",
            keys=("dev.decision.slug",),
            uniqueness="unique",
        ),
        ClaimTypeInput(kind="claim_type", claim_type=ClaimType.model_validate(decided_by)),
    )
    assert submitted.status.proposal_id is not None, submitted  # type: ignore[attr-defined]
    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=submitted.status.proposal_id,  # type: ignore[attr-defined]
        activated_by="owner",
    )
    assert receipt.status == "accepted", receipt

    # Re-keying the kind by code alone would leave the inverse keyed by nothing.
    rekeyed = submit(
        SubjectKindInput(
            kind="subject_kind",
            name="dev.decision",
            keys=("dev.decision.code",),
            uniqueness="unique",
        )
    )
    assert "cruxible.subject_kind.inverse_dependents_not_settled" in repr(rekeyed)
    assert rekeyed.status.proposal_id is None or "refused" in repr(rekeyed.status)  # type: ignore[attr-defined]


def test_lint_warns_when_stale_is_eligible_but_current_is_required(tmp_path: Path) -> None:
    from cruxible_core.claims.claim_type_inputs import (
        CLAIM_TYPE_LINT_TEXT,
        ClaimTypeInputRecord,
        lint_claim_type_input,
    )
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    resolution = {
        "cardinality": "one",
        "eligible_verdicts": ["stale", "supported"],
        "required_basis_kinds": [],
        "require_current": True,
        "selector": "only_contender",
        "conflict_result": "unresolved",
    }
    record = {
        "predicate": "project.work_item.phase",
        "description": "The work item's phase.",
        "allowed_subject_kinds": ["project.work_item"],
        "object_kind": "literal",
        "literal_schema": {"type": "string"},
        "cardinality": "one",
        "resolution_policy": resolution,
    }

    def codes(value: dict[str, object]) -> list[str]:
        lint = lint_claim_type_input(
            instance,
            ClaimTypeInputRecord.model_validate(value),
            coordinate=instance.accepted_coordinate(),
        )
        return [item.code for item in lint.warnings]

    assert codes(record) == ["cruxible.claim_type.stale_eligible_never_current"]
    assert CLAIM_TYPE_LINT_TEXT["cruxible.claim_type.stale_eligible_never_current"] == (
        "eligible_verdicts lists stale while require_current is true: stale contenders "
        "never resolve"
    )
    assert codes({**record, "resolution_policy": {**resolution, "require_current": False}}) == []

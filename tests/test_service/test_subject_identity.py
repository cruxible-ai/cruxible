"""Core names every new Subject by its kind's declared rule (identity D1, D4, D6).

- Creation requires a kind an accepted SubjectKind declares, on every route.
- A keyed kind's Subject is named by the readable rendering of its canonical key
  and states each key part as a Claim; a minted kind's id is minted.
- A unique kind holds each canonical key once, checked at candidate evaluation
  (admission and settlement), so two concurrent changes cannot both land a key.
- Existing Subjects of an undeclared kind are grandfathered: readable,
  claimable and retirable, never re-created.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import (
    ChangeSetInput,
    ClaimTypeInput,
    SubjectInput,
    SubjectKindInput,
    SubjectSuccessionInput,
    lower_authoring_input,
)
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.subjects import parse_subject, subject_path
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from tests.core_support._identity_support import OWNER, accept_tree, identity_claim_type
from tests.core_support._support import initialize_local

DECISION = "dev.decision"
TICKET = "dev.ticket"
NOTE = "dev.note"
PAIR = "dev.pair"
LEGACY = "dev.legacy"
FORM = ("nfkc", "trim", "collapse_whitespace", "casefold")


def _key(kind: str, field: str, **changes: Any) -> Any:
    fields: dict[str, Any] = {
        "canonical_form": FORM,
        "literal_schema": {"type": "string"},
        "predicate": f"{kind}.{field}",
        "subject_kinds": (kind,),
    }
    fields.update(changes)
    return identity_claim_type(field, **fields)


def _field(kind: str, field: str) -> Any:
    return identity_claim_type(
        field,
        canonical_form=None,
        literal_schema={"type": "string"},
        predicate=f"{kind}.{field}",
        subject_kinds=(kind,),
    )


def submit(instance: PlaybillInstance, payload: Any) -> Any:
    return AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )


def compile_(instance: PlaybillInstance, payload: Any) -> Any:
    return AuthoringIntentCoordinator.for_instance(instance).compile(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )


def accept(instance: PlaybillInstance, payload: Any) -> Any:
    submitted = submit(instance, payload)
    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt
    return submitted


def change_set(*members: Any) -> Any:
    return lower_authoring_input(ChangeSetInput(kind="change_set", members=members))


def write(instance: PlaybillInstance, *changes: dict[str, Any], **request: Any) -> WriteOutcome:
    body = {"because": "Recorded by hand.", "changes": list(changes), **request}
    return service_playbill_write(
        instance,
        request=WriteRequest.model_validate(body),
        caller=WriteCaller(actor=OWNER, may_activate=True, may_approve=True),
    )


def tree(instance: PlaybillInstance) -> Any:
    return instance.tree_at(instance.accepted_coordinate().git_oid)


def key_claims(instance: PlaybillInstance, kind: str, subject_id: str) -> dict[str, object]:
    found: dict[str, object] = {}
    path = subject_path(kind, subject_id)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        for identity, predicate in projection.typed.connection.execute(
            "SELECT identity, predicate FROM claims WHERE subject_path=? AND lifecycle='live'",
            (path,),
        ):
            claim_id = str(identity).removeprefix("Claim:")
            claim = parse_claim(
                tree(instance)[f"claims/{claim_id[4:6]}/{claim_id}.json"],
                path=f"claims/{claim_id[4:6]}/{claim_id}.json",
            )
            found[str(predicate)] = claim.statement.object.value  # type: ignore[union-attr]
    return found


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    instance, _owner = initialize_local(tmp_path)
    accept(
        instance,
        change_set(
            ClaimTypeInput(kind="claim_type", claim_type=_key(DECISION, "slug")),
            ClaimTypeInput(kind="claim_type", claim_type=_field(DECISION, "title")),
            SubjectKindInput(
                kind="subject_kind", name=DECISION, keys=(f"{DECISION}.slug",), uniqueness="unique"
            ),
            ClaimTypeInput(kind="claim_type", claim_type=_field(TICKET, "title")),
            SubjectKindInput(kind="subject_kind", name=TICKET, minted=True),
            ClaimTypeInput(kind="claim_type", claim_type=_key(NOTE, "title")),
            SubjectKindInput(
                kind="subject_kind", name=NOTE, keys=(f"{NOTE}.title",), uniqueness="hint"
            ),
            ClaimTypeInput(kind="claim_type", claim_type=_key(PAIR, "left")),
            ClaimTypeInput(kind="claim_type", claim_type=_key(PAIR, "right")),
            SubjectKindInput(
                kind="subject_kind",
                name=PAIR,
                keys=(f"{PAIR}.left", f"{PAIR}.right"),
                uniqueness="unique",
            ),
            ClaimTypeInput(kind="claim_type", claim_type=_field(LEGACY, "label")),
        ),
    )
    return instance


# -- item 2: creation requires a declared kind ---------------------------------------


def test_creating_a_subject_of_an_undeclared_kind_refuses_on_every_route(
    instance: PlaybillInstance,
) -> None:
    refused = write(
        instance, {"op": "set", "subject": f"{LEGACY}/one", "field": "label", "value": "x"}
    )
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.subject.kind_undeclared"
    assert '"kind": "subject_kind"' in (refused.refusal.repair or "")

    result = compile_(
        instance,
        lower_authoring_input(SubjectInput(kind="subject", subject_kind=LEGACY)),
    )
    assert result.verdict == "refused"
    assert [item.code for item in result.frontier.diagnostics] == [
        "cruxible.subject.kind_undeclared"
    ]

    from cruxible_client.contracts.subjects import render_subject
    from cruxible_core.proposals.proposals import ProposalAdmissionRequest
    from tests.core_support._identity_support import shell

    base = instance.accepted_coordinate()
    raw = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/raw-legacy", proposed_base_oid=base.git_oid
        ),
        candidate_tree={
            **tree(instance),
            subject_path(LEGACY, "one"): render_subject(shell("one", LEGACY)),
        },
        timestamp="2026-10-09T12:00:00.000000Z",
    )
    assert raw.candidate is None
    assert [item.code for item in raw.evaluation.diagnostics] == [
        "cruxible.subject.kind_undeclared"
    ]


def test_a_subject_of_a_kind_declared_later_is_grandfathered(tmp_path: Path) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import render_subject, subject_digest
    from tests.core_support._identity_support import shell

    instance, _owner = initialize_local(tmp_path)
    accept(
        instance, change_set(ClaimTypeInput(kind="claim_type", claim_type=_field(LEGACY, "label")))
    )
    # Created by core under a declaration (keyed by slug), then the declaration
    # retired: its Subjects stay, readable, claimable and retirable, and none is
    # created again.
    accept_tree(
        instance,
        {
            subject_path(LEGACY, "old"): render_subject(shell("old", LEGACY)),
            subject_path(LEGACY, "older"): render_subject(shell("older", LEGACY)),
        },
        name="legacy",
    )
    accept(
        instance,
        lower_authoring_input(
            SubjectKindInput(
                kind="subject_kind",
                name=LEGACY,
                keys=(f"{LEGACY}.slug",),
                uniqueness="unique",
                retire=True,
            )
        ),
    )
    claimed = write(
        instance, {"op": "set", "subject": f"{LEGACY}/old", "field": "label", "value": "kept"}
    )
    assert claimed.status == "accepted", claimed
    old = shell("older", LEGACY)
    retired = old.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(old).tagged
            )
        }
    )
    from cruxible_client.contracts.authoring.inputs import ClaimRetirementInput

    # Its key Claim depends on it: retire that Claim first, then the Subject.
    (key_claim,) = [
        path.rsplit("/", 1)[1].removesuffix(".json")
        for path, content in tree(instance).items()
        if path.startswith("claims/")
        and parse_claim(content, path=path).statement.subject.artifact_path
        == subject_path(LEGACY, "older")
    ]
    accept(
        instance,
        change_set(
            ClaimRetirementInput(kind="claim_retirement", retires=key_claim, reason="superseded")
        ),
    )
    accept(
        instance,
        lower_authoring_input(SubjectSuccessionInput(kind="subject_succession", subject=retired)),
    )
    assert (
        parse_subject(
            tree(instance)[subject_path(LEGACY, "older")], path=subject_path(LEGACY, "older")
        ).lifecycle.state
        == "retired"
    )
    refused = write(
        instance, {"op": "set", "subject": f"{LEGACY}/new", "field": "label", "value": "x"}
    )
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.subject.kind_undeclared"


# -- item 3: core assigns the id ---------------------------------------------------


def test_a_write_naming_a_missing_subject_of_a_single_key_kind_creates_it_by_key(
    instance: PlaybillInstance,
) -> None:
    outcome = write(
        instance, {"op": "set", "subject": f"{DECISION}/ship-it", "field": "title", "value": "Ship"}
    )
    assert outcome.status == "accepted", outcome
    assert outcome.subjects_added == (f"{DECISION}/ship-it",)
    assert key_claims(instance, DECISION, "ship-it") == {
        f"{DECISION}.slug": "ship-it",
        f"{DECISION}.title": "Ship",
    }
    # The same reference now names that Subject: no second creation.
    again = write(
        instance, {"op": "set", "subject": f"{DECISION}/ship-it", "field": "title", "value": "Go"}
    )
    assert again.status == "accepted", again
    assert again.subjects_added == ()


def test_a_reference_whose_key_a_subject_holds_names_it(instance: PlaybillInstance) -> None:
    write(instance, {"op": "set", "subject": f"{DECISION}/ship_it", "field": "title", "value": "A"})
    # The key "ship_it" renders to the id "ship-it"; a reference by the key value
    # finds it through the key index, and one by the id finds it exactly.
    assert set(key_claims(instance, DECISION, "ship-it")) == {
        f"{DECISION}.slug",
        f"{DECISION}.title",
    }
    for ref in (f"{DECISION}/ship_it", f"{DECISION}/ship-it"):
        outcome = write(instance, {"op": "set", "subject": ref, "field": "title", "value": "B"})
        assert outcome.status == "accepted", outcome
        assert outcome.subjects_added == ()


def test_a_write_creates_only_a_single_key_kind_by_its_key(instance: PlaybillInstance) -> None:
    for kind, field in ((TICKET, "title"), (PAIR, "left")):
        refused = write(
            instance, {"op": "set", "subject": f"{kind}/one", "field": field, "value": "x"}
        )
        assert refused.refusal is not None, kind
        assert refused.refusal.code == "cruxible.write.subject_not_found", kind
        # 4g: the repair is the same-write create, its key predicates named.
        repair = refused.refusal.repair or ""
        assert f'{{"op": "create", "label": "new", "subject_kind": "{kind}"' in repair, kind
        assert '{"created": "new"}' in repair, kind
    pair_repair = write(
        instance, {"op": "set", "subject": f"{PAIR}/one", "field": "left", "value": "x"}
    ).refusal
    assert pair_repair is not None
    assert (
        f'"key": [{{"predicate": "{PAIR}.left", "value": "<value>"}}, '
        f'{{"predicate": "{PAIR}.right", "value": "<value>"}}]'
    ) in (pair_repair.repair or "")
    # Following the repair lands the write.
    followed = write(
        instance,
        {"op": "create", "label": "new", "subject_kind": TICKET},
        {"op": "set", "subject": {"created": "new"}, "field": "title", "value": "x"},
    )
    assert followed.status == "accepted", followed
    # A single-key hint kind is created by key like a unique one.
    created = write(
        instance, {"op": "add", "subject": f"{NOTE}/one", "field": "semantic.alias", "value": "o"}
    )
    assert created.status == "accepted", created
    assert created.subjects_added == (f"{NOTE}/one",)


def test_authoring_creates_minted_and_composite_subjects_and_reports_their_ids(
    instance: PlaybillInstance,
) -> None:
    submitted = accept(
        instance,
        change_set(
            SubjectInput(kind="subject", subject_kind=TICKET, rationale="A new ticket."),
            SubjectInput(
                kind="subject",
                subject_kind=PAIR,
                key={"left": "Alpha  One", f"{PAIR}.right": "Beta"},
            ),
        ),
    )
    created = sorted(item.subject for item in submitted.members if item.subject is not None)
    assert created[0] == f"{PAIR}/alpha-one--beta"
    assert created[1].startswith(f"{TICKET}/sub-") and len(created[1]) == len(TICKET) + 1 + 36
    assert key_claims(instance, PAIR, "alpha-one--beta") == {
        f"{PAIR}.left": "alpha one",
        f"{PAIR}.right": "beta",
    }


def test_a_write_creates_minted_composite_and_further_hint_subjects(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.subject_kinds import is_distinct_holder_id

    _create(instance, NOTE, "Same")
    outcome = write(
        instance,
        {"op": "create", "label": "t1", "subject_kind": TICKET},
        {"op": "create", "label": "t2", "subject_kind": TICKET},
        {"op": "set", "subject": {"created": "t1"}, "field": "title", "value": "First"},
        {"op": "set", "subject": {"created": "t2"}, "field": "title", "value": "Second"},
        {
            "op": "create",
            "label": "pair",
            "subject_kind": PAIR,
            "key": [
                {"predicate": f"{PAIR}.left", "value": "Alpha  One"},
                {"predicate": f"{PAIR}.right", "value": "Beta"},
            ],
        },
        {"op": "create", "label": "note", "subject_kind": NOTE, "key": "same", "create_new": True},
    )
    assert outcome.status == "accepted", outcome
    first, second, title_one, title_two, pair, note = outcome.changes
    assert first.subject is not None and second.subject is not None
    assert first.subject.startswith(f"{TICKET}/sub-") and first.subject != second.subject
    assert (title_one.subject, title_two.subject) == (first.subject, second.subject)
    assert pair.subject == f"{PAIR}/alpha-one--beta"
    assert note.subject is not None and note.subject != f"{NOTE}/same"
    assert is_distinct_holder_id(note.subject.partition("/")[2], ["same"])
    assert set(outcome.subjects_added) == {
        first.subject,
        second.subject,
        pair.subject,
        note.subject,
    }
    assert key_claims(instance, TICKET, first.subject.partition("/")[2]) == {
        f"{TICKET}.title": "First"
    }
    assert key_claims(instance, PAIR, "alpha-one--beta") == {
        f"{PAIR}.left": "alpha one",
        f"{PAIR}.right": "beta",
    }
    # A composite kind takes each key part; one bare value names only a one-key kind.
    bare = write(instance, {"op": "create", "label": "p", "subject_kind": PAIR, "key": "x"})
    assert bare.refusal is not None and bare.refusal.code == "cruxible.subject.key_invalid"
    keyed = write(instance, {"op": "create", "label": "t", "subject_kind": TICKET, "key": "x"})
    assert keyed.refusal is not None and keyed.refusal.code == "cruxible.subject.key_invalid"


def test_an_author_chosen_id_for_a_new_subject_refuses(instance: PlaybillInstance) -> None:
    from tests.core_support._identity_support import shell

    result = compile_(
        instance,
        lower_authoring_input(
            SubjectSuccessionInput(kind="subject_succession", subject=shell("chosen", TICKET))
        ),
    )
    assert result.verdict == "refused"
    (diagnostic,) = result.frontier.diagnostics
    assert diagnostic.code == "cruxible.authoring.subject_id_not_assigned"
    assert '"kind": "subject"' in diagnostic.repairs[0].description


def test_two_keys_rendering_alike_take_the_digest_suffix(instance: PlaybillInstance) -> None:
    from cruxible_client.contracts.subject_kinds import key_digest_hex

    accept(
        instance,
        lower_authoring_input(
            SubjectInput(kind="subject", subject_kind=DECISION, key={"slug": "a b"})
        ),
    )
    submitted = accept(
        instance,
        lower_authoring_input(
            SubjectInput(kind="subject", subject_kind=DECISION, key={"slug": "a-b"})
        ),
    )
    (created,) = [item.subject for item in submitted.members if item.subject is not None]
    assert created == f"{DECISION}/a-b-{key_digest_hex(['a-b'])[:8]}"


def _create(instance: PlaybillInstance, kind: str, key: Any, **fields: Any) -> Any:
    return accept(
        instance,
        lower_authoring_input(SubjectInput(kind="subject", subject_kind=kind, key=key, **fields)),
    )


def _subjects_of(result: Any) -> list[str]:
    return [item.subject for item in result.members if item.subject is not None]


def _alias_member(kind: str, subject_id: str, value: str) -> Any:
    from cruxible_client.contracts.authoring.inputs import (
        ClaimInput,
        LiteralObjectInput,
        SelfSourceInput,
    )

    return ClaimInput(
        kind="claim",
        subject=f"{kind}/{subject_id}",
        predicate="semantic.alias",
        object=LiteralObjectInput(kind="literal", value=value),
        role="normative",
        rationale="Another name for it.",
        source=SelfSourceInput(kind="self_source", body=f"also known as {value}"),
    )


def test_create_by_key_resolves_one_holder_in_both_modes(instance: PlaybillInstance) -> None:
    """0 holders -> create; exactly 1 -> resolve to it, creating nothing (an upsert)."""

    for kind, field in ((DECISION, "slug"), (NOTE, "title")):
        first = _create(instance, kind, {field: "Same"})
        assert _subjects_of(first) == [f"{kind}/same"]
        # Created again by its key (another spelling of it) beside a Claim naming
        # it as the key was given: the key resolves to the Subject holding it.
        again = accept(
            instance,
            change_set(
                SubjectInput(kind="subject", subject_kind=kind, key=" same "),
                _alias_member(kind, "same", f"{kind}-again"),
            ),
        )
        assert _subjects_of(again) == [f"{kind}/same"]
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            (count,) = projection.typed.connection.execute(
                "SELECT COUNT(*) FROM subjects WHERE subject_kind=?", (kind,)
            ).fetchone()
        assert count == 1


def test_create_new_makes_a_further_hint_holder_and_is_refused_on_a_unique_kind(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.subject_kinds import is_distinct_holder_id

    _create(instance, NOTE, "Same")
    second = _create(instance, NOTE, " same ", create_new=True)
    (further,) = _subjects_of(second)
    assert further != f"{NOTE}/same" and is_distinct_holder_id(further.partition("/")[2], ["same"])
    # Two holders now: a creation by the key is ambiguous until one is named by id.
    ambiguous = compile_(
        instance, lower_authoring_input(SubjectInput(kind="subject", subject_kind=NOTE, key="SAME"))
    )
    assert [item.code for item in ambiguous.frontier.diagnostics] == [
        "cruxible.subject.key_ambiguous"
    ]
    assert "name one by id" in ambiguous.frontier.diagnostics[0].message

    _create(instance, DECISION, "only")
    unique = compile_(
        instance,
        lower_authoring_input(
            SubjectInput(kind="subject", subject_kind=DECISION, key="only", create_new=True)
        ),
    )
    assert [item.code for item in unique.frontier.diagnostics] == [
        "cruxible.subject.create_new_refused"
    ]


def test_a_reference_by_a_key_two_subjects_hold_is_ambiguous(instance: PlaybillInstance) -> None:
    _create(instance, NOTE, "Twin  Peak")
    _create(instance, NOTE, "twin peak", create_new=True)
    # "twin peak" is no id ("twin-peak" is); by its key two Subjects hold it.
    refused = write(
        instance,
        {"op": "add", "subject": f"{NOTE}/twin_peak", "field": "semantic.alias", "value": "x"},
    )
    assert refused.status == "accepted", refused  # "twin_peak" is another key: created
    ambiguous = compile_(
        instance,
        change_set(
            _alias_member(NOTE, "twin-peak", "y"),
            SubjectInput(kind="subject", subject_kind=NOTE, key="Twin Peak"),
        ),
    )
    assert "cruxible.subject.key_ambiguous" in [
        item.code for item in ambiguous.frontier.diagnostics
    ]
    # By its exact id the first holder is named, whatever its key says.
    by_id = write(
        instance,
        {"op": "add", "subject": f"{NOTE}/twin-peak", "field": "semantic.alias", "value": "z"},
    )
    assert by_id.status == "accepted", by_id


def test_making_a_hint_kind_unique_refuses_while_two_subjects_share_a_key(
    instance: PlaybillInstance,
) -> None:
    _create(instance, NOTE, {"title": "twin"})
    _create(instance, NOTE, {"title": "Twin"}, create_new=True)
    result = compile_(
        instance,
        lower_authoring_input(
            SubjectKindInput(
                kind="subject_kind", name=NOTE, keys=(f"{NOTE}.title",), uniqueness="unique"
            )
        ),
    )
    assert result.verdict == "refused"
    assert {item.code for item in result.frontier.diagnostics} == {"cruxible.subject.key_conflict"}


def _pairs(instance: PlaybillInstance, rights: range) -> None:
    accept(
        instance,
        change_set(
            *(
                SubjectInput(
                    kind="subject",
                    subject_kind=PAIR,
                    key={"left": "common", "right": f"r{index}"},
                )
                for index in rights
            )
        ),
    )


def _counted_lookup(instance: PlaybillInstance, key: Any) -> tuple[tuple[str, ...], int]:
    from cruxible_core.indexes.subject_identity import subjects_with_key

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        connection = projection.typed.connection
        steps: list[int] = []
        connection.set_progress_handler(lambda: steps.append(1) or 0, 1)
        found = subjects_with_key(connection, PAIR, key)
        connection.set_progress_handler(None, 1)
    return found, len(steps)


def test_the_key_index_answers_a_composite_key_in_one_seek(instance: PlaybillInstance) -> None:
    """Counted work: a whole key -- composite parts sharing a first part -- is one probe.

    The index holds each Subject's whole canonical key (``subject_keys``), so a
    lookup never walks the Subjects that share the first part.
    """

    from cruxible_core.indexes.subject_identity import key_encoding

    key = ((f"{PAIR}.left", "common"), (f"{PAIR}.right", "r3"))
    _pairs(instance, range(5))
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        plan = projection.typed.connection.execute(
            "EXPLAIN QUERY PLAN SELECT subject_path FROM subject_keys WHERE kind=? "
            "AND key_encoding=? ORDER BY subject_path",
            (PAIR, key_encoding(key)),
        ).fetchall()
    assert any("subject_keys_by_key" in str(row[-1]) for row in plan), plan
    found, few = _counted_lookup(instance, key)
    assert found == (subject_path(PAIR, "common--r3"),)
    _pairs(instance, range(5, 45))
    again, many = _counted_lookup(instance, key)
    assert again == found
    assert many <= few + 2, (few, many)


def test_a_boolean_key_and_an_integer_key_never_meet(tmp_path: Path) -> None:
    """Typed keys compare by canonical encoding (``true`` is not ``1``), in one change or two."""

    instance, _owner = initialize_local(tmp_path)
    typed = _key("dev.typed", "flag", literal_schema={"enum": [True, 1]})
    accept(
        instance,
        change_set(
            ClaimTypeInput(kind="claim_type", claim_type=typed),
            SubjectKindInput(
                kind="subject_kind",
                name="dev.typed",
                keys=("dev.typed.flag",),
                uniqueness="unique",
            ),
            SubjectInput(kind="subject", subject_kind="dev.typed", key={"flag": True}),
            SubjectInput(kind="subject", subject_kind="dev.typed", key={"flag": 1}),
        ),
    )
    ids = sorted(path for path in tree(instance) if path.startswith("subjects/dev.typed/"))
    assert ids == ["subjects/dev.typed/true.json", "subjects/dev.typed/x1.json"]


# -- migration help: next names the SubjectKind to author ----------------------------


def test_next_names_the_subject_kind_a_kind_with_subjects_but_no_declaration_needs(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An instance from before D6 holds Subjects of kinds nothing declares.

    Its Subjects are grandfathered; `next` says what to author so new ones can be
    created, with the runnable example. A kind declared (even retired) is not
    reported.
    """

    import cruxible_core.proposals.proposals as proposals_module
    from cruxible_client.contracts.subjects import render_subject
    from cruxible_core.proposals.proposals import ProposalAdmissionRequest
    from cruxible_core.service.discovery.next import service_playbill_next
    from tests.core_support._identity_support import shell
    from tests.test_integration.test_next_closed_loop import _request

    # History accepted before Subject identity existed (an older build): no rule
    # judged its Subjects.
    monkeypatch.setattr(proposals_module, "candidate_identity_issues", lambda *_a, **_k: ())
    base = instance.accepted_coordinate()
    raw = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/pre-d6", proposed_base_oid=base.git_oid
        ),
        candidate_tree={
            **tree(instance),
            subject_path(LEGACY, "old"): render_subject(shell("old", LEGACY)),
        },
        timestamp="2026-10-09T12:00:00.000000Z",
    )
    assert raw.candidate is not None, raw.evaluation.diagnostics
    service_activate_playbill_proposal(
        instance, proposal_id=raw.admission.proposal_id, activated_by="owner"
    )
    monkeypatch.undo()

    result = service_playbill_next(instance, request=_request(instance))
    (item,) = [row for row in result.items if row.reason == "subject_kind_undeclared"]
    assert item.subject_identity == f"Subject:{LEGACY}/old"
    assert item.repair is not None and item.repair.target == f"SubjectKind:{LEGACY}"
    assert item.detail["live_subjects"] == 1  # type: ignore[index]
    assert item.detail["author"] == {  # type: ignore[index]
        "kind": "subject_kind",
        "name": LEGACY,
        "keys": [f"{LEGACY}.slug"],
        "uniqueness": "unique",
    }
    assert item.repair is not None
    assert item.repair.command == "cruxible authoring example subject-kind"


def test_key_text_as_written_names_the_subject_on_every_reference_route(
    instance: PlaybillInstance,
) -> None:
    """`kind/<key text>` in any case or spacing: write, get, authoring and inbox resolve it."""

    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_client.contracts.line_inbox import InboxLinkSubject
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.procedures.line_inbox import _existing_target
    from cruxible_core.storage.cas import BodyAccessContext

    created = write(
        instance,
        {"op": "set", "subject": f"{DECISION}/Ship It  Now", "field": "title", "value": "A"},
    )
    assert created.status == "accepted", created
    assert created.subjects_added == (f"{DECISION}/ship-it-now",)

    again = write(
        instance,
        {"op": "set", "subject": f"{DECISION}/SHIP IT NOW", "field": "title", "value": "B"},
    )
    assert again.status == "accepted", again
    assert again.subjects_added == ()
    assert key_claims(instance, DECISION, "ship-it-now")[f"{DECISION}.title"] == "B"

    read = service_playbill_get(
        instance,
        request=GetRequest(ref=f"{DECISION}/Ship it now"),
        access=BodyAccessContext(principal_id="test", can_read_body=True),
    )
    assert read.ref == f"{DECISION}/ship-it-now"

    # An authoring Claim names it by key text too: an alias stated on it lands there.
    accept(instance, change_set(_alias_member(DECISION, "Ship It Now", "launch")))
    by_alias = write(
        instance, {"op": "set", "subject": f"{DECISION}/launch", "field": "title", "value": "C"}
    )
    assert by_alias.status == "accepted", by_alias
    assert by_alias.changes[0].subject == f"{DECISION}/ship-it-now"

    assert _existing_target(
        instance, InboxLinkSubject(subject=f"{DECISION}/SHIP it now")
    ) == InboxLinkSubject(subject=f"{DECISION}/ship-it-now")


# Review H F-002: every route reads a reference in the common order -- exact live
# id, alias, key, exact retired id -- so a retired id never shadows a live holder.


def _retire(instance: PlaybillInstance, kind: str, subject_id: str) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import subject_digest

    path = subject_path(kind, subject_id)
    shell = parse_subject(tree(instance)[path], path=path)
    retired = shell.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(shell).tagged
            )
        }
    )
    accept(instance, change_set(SubjectSuccessionInput(kind="subject_succession", subject=retired)))


def _get(instance: PlaybillInstance, ref: str) -> Any:
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext

    return service_playbill_get(
        instance,
        request=GetRequest(ref=ref),
        access=BodyAccessContext(principal_id="test", can_read_body=True),
    )


def test_a_reused_key_names_its_live_holder_on_every_route_not_the_retired_id(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.line_inbox import InboxLinkSubject
    from cruxible_core.service.procedures.line_inbox import _existing_target

    _create(instance, DECISION, "foo")
    _retire(instance, DECISION, "foo")
    (live,) = _subjects_of(_create(instance, DECISION, "foo"))
    assert live.startswith(f"{DECISION}/foo-") and live != f"{DECISION}/foo"

    written = write(
        instance, {"op": "set", "subject": f"{DECISION}/foo", "field": "title", "value": "Live"}
    )
    assert written.status == "accepted", written
    assert written.changes[0].subject == live
    assert _get(instance, f"{DECISION}/foo").ref == live
    assert _get(instance, f"Subject:{DECISION}/foo").ref == live
    assert _existing_target(
        instance, InboxLinkSubject(subject=f"{DECISION}/foo")
    ) == InboxLinkSubject(subject=live)
    accept(instance, change_set(_alias_member(DECISION, "foo", "the survivor")))
    assert _get(instance, f"{DECISION}/the survivor").ref == live
    # The retired Subject stays readable by its artifact path.
    assert _get(instance, subject_path(DECISION, "foo")).ref == f"{DECISION}/foo"


def test_an_ambiguous_key_in_front_of_a_retired_id_refuses_on_get(
    instance: PlaybillInstance,
) -> None:
    from cruxible_core.service.read_refusals import ReadRefusalError

    _create(instance, NOTE, "foo")
    _retire(instance, NOTE, "foo")
    _create(instance, NOTE, "foo")
    _create(instance, NOTE, "foo", create_new=True)

    with pytest.raises(ReadRefusalError) as refused:
        _get(instance, f"{NOTE}/foo")
    assert len(refused.value.candidates) == 2
    ambiguous = write(
        instance, {"op": "add", "subject": f"{NOTE}/foo", "field": "semantic.alias", "value": "x"}
    )
    assert ambiguous.refusal is not None
    assert ambiguous.refusal.code == "cruxible.subject.key_ambiguous"


# -- set/history review: one complete creation assignment -----------------------------


def _write_create(
    label: str, key: object = None, kind: str = DECISION, **options: Any
) -> dict[str, Any]:
    return {"op": "create", "label": label, "subject_kind": kind, "key": key, **options}


def _write_title(label: str, value: str) -> dict[str, Any]:
    return {"op": "set", "subject": {"created": label}, "field": "title", "value": value}


@pytest.mark.parametrize("ambiguous", [False, True])
def test_planned_key_reuse_preserves_each_spellings_live_alias(
    instance: PlaybillInstance, ambiguous: bool
) -> None:
    kind = "dev.punct"
    accept(
        instance,
        change_set(
            ClaimTypeInput(
                kind="claim_type",
                claim_type=_key(
                    kind, "slug", canonical_form=("nfkc", "trim", "casefold", "strip_punctuation")
                ),
            ),
            ClaimTypeInput(kind="claim_type", claim_type=_field(kind, "title")),
            SubjectKindInput(
                kind="subject_kind", name=kind, keys=(f"{kind}.slug",), uniqueness="unique"
            ),
        ),
    )
    holder = write(instance, _write_create("holder", "other", kind))
    assert holder.status == "accepted", holder
    address = holder.changes[0].subject
    assert address is not None
    aliased = write(
        instance, {"op": "add", "subject": address, "field": "semantic.alias", "value": "a-b"}
    )
    assert aliased.status == "accepted", aliased
    if ambiguous:
        other = write(instance, _write_create("holder", "another", kind))
        assert other.status == "accepted", other
        aliased = write(
            instance,
            {
                "op": "add",
                "subject": other.changes[0].subject,
                "field": "semantic.alias",
                "value": "a-b",
            },
        )
        assert aliased.status == "accepted", aliased
    head = instance.accepted_coordinate()
    result = write(
        instance,
        _write_create("plain", "ab", kind),
        _write_create("aliased", "a-b", kind),
        _write_title("aliased", "Belongs to alias holder"),
    )
    if ambiguous:
        assert result.status == "refused", result
        assert result.refusal is not None
        assert (result.refusal.code, result.refusal.change) == ("cruxible.write.alias_ambiguous", 1)
        assert instance.accepted_coordinate() == head
    else:
        assert result.status == "accepted", result
        assert (result.changes[1].subject, result.changes[1].resolved) == (address, "existing")
        assert (
            key_claims(instance, kind, address.partition("/")[2])[f"{kind}.title"]
            == "Belongs to alias holder"
        )


def test_lowering_keeps_original_key_spelling_for_alias_precedence(
    instance: PlaybillInstance,
) -> None:
    kind = "dev.punct"
    accept(
        instance,
        change_set(
            ClaimTypeInput(
                kind="claim_type",
                claim_type=_key(
                    kind, "slug", canonical_form=("nfkc", "trim", "casefold", "strip_punctuation")
                ),
            ),
            ClaimTypeInput(kind="claim_type", claim_type=_field(kind, "title")),
            SubjectKindInput(
                kind="subject_kind", name=kind, keys=(f"{kind}.slug",), uniqueness="unique"
            ),
        ),
    )
    holder = write(instance, _write_create("holder", "other", kind))
    assert holder.status == "accepted", holder
    alias = write(
        instance,
        {
            "op": "add",
            "subject": holder.changes[0].subject,
            "field": "semantic.alias",
            "value": "ab",
        },
    )
    assert alias.status == "accepted", alias
    result = write(
        instance, _write_create("punctuated", "a-b", kind), _write_title("punctuated", "New holder")
    )
    assert result.status == "accepted", result
    assert result.changes[0].subject != holder.changes[0].subject
    assert result.changes[0].resolved == "created"
    assert result.changes[1].subject == result.changes[0].subject


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("implicit", [False, True])
def test_rendering_collisions_keep_labels_and_subject_values_bound(
    instance: PlaybillInstance, reverse: bool, implicit: bool
) -> None:
    from cruxible_client.contracts.claims import SubjectClaimObject

    predicate = f"{DECISION}.peer"
    accept(
        instance,
        change_set(
            ClaimTypeInput(
                kind="claim_type",
                claim_type=identity_claim_type(
                    "peer",
                    canonical_form=None,
                    object_kind="subject",
                    object_kinds=(DECISION,),
                    predicate=predicate,
                    subject_kinds=(DECISION,),
                ),
            )
        ),
    )
    labels = [("hyphen", "a-b", "Hyphen"), ("space", "a b", "Space")]
    if reverse:
        labels.reverse()
    creates = [_write_create(label, key) for label, key, _title in labels]
    fields = [_write_title(label, title) for label, _key_text, title in labels]
    if implicit:
        # The second creation uses the string shorthand; it shares complete assignment.
        creates.pop()
        label, key, title = labels[1]
        fields[1] = {"op": "set", "subject": f"{DECISION}/{key}", "field": "title", "value": title}
    result = write(
        instance,
        *creates,
        *fields,
        {
            "op": "set",
            "subject": {"created": labels[0][0]},
            "field": "peer",
            "value": f"{DECISION}/{labels[1][1]}" if implicit else {"created": labels[1][0]},
        },
        {
            "op": "set",
            "subject": f"{DECISION}/{labels[1][1]}" if implicit else {"created": labels[1][0]},
            "field": "peer",
            "value": {"created": labels[0][0]},
        },
    )
    assert result.status == "accepted", result
    titles = result.changes[len(creates) : len(creates) + 2]
    addresses = [item.subject for item in titles]
    assert len(set(addresses)) == 2
    for index, ((_label, key, title), item) in enumerate(zip(labels, titles, strict=True)):
        assert item.subject is not None
        path = subject_path(DECISION, item.subject.partition("/")[2])
        with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
            rows = projection.typed.connection.execute(
                "SELECT identity, predicate FROM claims WHERE subject_path=? AND lifecycle='live'",
                (path,),
            ).fetchall()
        values: dict[str, object] = {}
        for identity, field in rows:
            claim_id = str(identity).removeprefix("Claim:")
            claim_path = f"claims/{claim_id[4:6]}/{claim_id}.json"
            claim = parse_claim(tree(instance)[claim_path], path=claim_path)
            obj = claim.statement.object
            values[str(field)] = (
                obj.address.artifact_path.removeprefix("subjects/").removesuffix(".json")
                if isinstance(obj, SubjectClaimObject)
                else obj.value
            )
        assert values == {
            f"{DECISION}.slug": key,
            f"{DECISION}.title": title,
            predicate: addresses[1 - index],
        }
        if not implicit:
            assert result.changes[index].subject == item.subject
    assert (
        next(
            item.subject
            for (_label, key, _title), item in zip(labels, titles, strict=True)
            if key == "a b"
        )
        == f"{DECISION}/a-b"
    )
    assert set(result.subjects_added) == set(addresses)


@pytest.mark.parametrize("reverse", [False, True])
def test_hint_first_and_further_holders_in_one_write(
    instance: PlaybillInstance, reverse: bool
) -> None:
    creates = [
        _write_create("first", "same", NOTE),
        _write_create("further", "same", NOTE, create_new=True),
    ]
    if reverse:
        creates.reverse()
    result = write(instance, *creates)
    assert result.status == "accepted", result
    by_label = {
        change["label"]: outcome.subject
        for change, outcome in zip(creates, result.changes, strict=True)
    }
    assert by_label["first"] == f"{NOTE}/same"
    assert by_label["further"] != by_label["first"]
    assert set(result.subjects_added) == set(by_label.values())
    for address in by_label.values():
        assert address is not None
        assert key_claims(instance, NOTE, address.partition("/")[2]) == {f"{NOTE}.title": "same"}


def test_two_labels_for_one_pending_key_share_slots(instance: PlaybillInstance) -> None:
    result = write(
        instance,
        _write_create("one", "Same"),
        _write_create("two", "SAME"),
        _write_title("one", "One"),
        _write_title("two", "Two"),
    )
    assert result.refusal is not None
    assert result.refusal.code == "cruxible.write.slot_set_twice"
    accepted = write(
        instance,
        _write_create("one", "Same"),
        _write_create("two", "SAME"),
        _write_title("two", "One"),
    )
    assert accepted.status == "accepted", accepted
    assert len(accepted.subjects_added) == 1
    assert all(item.subject == f"{DECISION}/same" for item in accepted.changes)


@pytest.mark.parametrize("mode", ["minted", "further_existing", "further_batch"])
def test_dry_run_keeps_every_pending_subject_outcome_unassigned(
    instance: PlaybillInstance, mode: str
) -> None:
    kind = TICKET if mode == "minted" else NOTE
    accept(
        instance,
        change_set(
            ClaimTypeInput(kind="claim_type", claim_type=_field(kind, "body")),
            ClaimTypeInput(
                kind="claim_type",
                claim_type=identity_claim_type(
                    "peer",
                    canonical_form=None,
                    object_kind="subject",
                    object_kinds=(kind,),
                    predicate=f"{DECISION}.peer",
                    subject_kinds=(DECISION,),
                ),
            ),
        ),
    )
    root = write(instance, _write_create("root", "root"))
    assert root.status == "accepted", root
    if mode == "further_existing":
        first = write(instance, _write_create("first", "same", NOTE))
        assert first.status == "accepted", first
    head = instance.accepted_coordinate()
    label = "ticket" if mode == "minted" else "further"
    creation = (
        _write_create(label, kind=kind)
        if mode == "minted"
        else _write_create(label, "same", kind, create_new=True)
    )
    changes = [
        creation,
        {"op": "set", "subject": {"created": label}, "field": "body", "value": "Body"},
        {
            "op": "add",
            "subject": {"created": label},
            "field": "semantic.alias",
            "value": "draft alias",
        },
    ]
    if mode == "minted":
        changes.append(_write_title(label, "Ticket"))
    pending_count = len(changes)
    changes.append(
        {
            "op": "set",
            "subject": root.changes[0].subject,
            "field": "peer",
            "value": {"created": label},
        }
    )
    if mode == "further_batch":
        changes.append(_write_create("first", "same", NOTE))
    preview = write(instance, *changes, dry_run=True)
    assert preview.status == "would_accept", preview
    assert all(item.subject is None for item in preview.changes[:pending_count]), preview
    assert preview.changes[pending_count].after is None
    assert preview.subjects_added == ((f"{NOTE}/same",) if mode == "further_batch" else ())
    assert f"<{label}>" not in preview.model_dump_json()
    assert instance.accepted_coordinate() == head
    accepted = write(instance, *changes)
    assert accepted.status == "accepted", accepted
    address = accepted.changes[0].subject
    assert address is not None
    assert all(item.subject == address for item in accepted.changes[:pending_count])
    assert accepted.changes[pending_count].after == address

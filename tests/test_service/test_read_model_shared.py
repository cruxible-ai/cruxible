"""The three read verbs share one answer for what they all show.

``orient``, ``query`` and ``get`` each show a ClaimType's accepted evidence as
CaptureContract names, and each shows verdict problems as flags. Both come from
one shared derivation, so on the same state the verbs agree.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1, PlaybillQueryResult
from cruxible_client.contracts.get_reads import (
    PlaybillGetClaimCardV1,
    PlaybillGetRequestV1,
    PlaybillGetResultV1,
    PlaybillGetSubjectCardV1,
)
from cruxible_client.contracts.query.definitions import QueryDefinitionSpecV1
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._exact_content_support import EXACT_KIND, seed_exact_content
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    SUBJECT_KIND,
    seed_claims,
    work_item_query,
)

_WHEN = datetime.fromisoformat(EVALUATION_TIME)
_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


@pytest.fixture
def instance(tmp_path: Path) -> Any:
    seeded, _owner = seed_claims(tmp_path)
    return seeded


def test_every_verb_names_the_same_capture_contracts(instance: Any) -> None:
    query_row = service_playbill_query(
        instance,
        request=PlaybillQueryRequestV1.model_validate(
            {"kind": "ClaimType", "where": [{"field": "namespace", "eq": SUBJECT_KIND}]}
        ),
    ).rows[0]
    orient_kind = next(
        item for item in service_playbill_orient(instance).kinds if item.kind == SUBJECT_KIND
    )
    orient_evidence = orient_kind.evidence or next(
        item.evidence for item in orient_kind.predicates if item.predicate == PREDICATE
    )
    card = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=f"ClaimType:{PREDICATE}", evaluation_time=_WHEN),
        access=_ACCESS,
    ).card
    assert card is not None

    assert query_row["evidence"]
    assert tuple(query_row["evidence"]) == orient_evidence
    assert card.model_dump()["evidence"] == tuple(
        f"CaptureContract:{name}" for name in orient_evidence
    )
    assert not any(name.startswith("sha256:") for name in orient_evidence)


def test_query_rows_and_get_cards_carry_the_same_flags_for_a_held_conflict(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.get_reads import (
        PlaybillGetClaimCardV1,
        PlaybillGetSubjectCardV1,
    )
    from tests.core_support._candidate_support import submit_query_definition_candidate
    from tests.core_support._knowledge_loop_support import (
        QUERY_NAME,
        TIMESTAMP,
        accept_proposal,
        work_item_query,
    )
    from tests.test_integration.test_next_closed_loop import EVALUATION_TIME as LATER
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _all_claims, _attest
    from tests.test_service.test_get_reads import _contend

    instance, owner = seed_claims(tmp_path)
    accept_proposal(
        instance,
        owner,
        submit_query_definition_candidate(
            instance,
            query=work_item_query(),
            actor_id="owner",
            proposal_name="work-item-query",
            timestamp=TIMESTAMP,
        ),
    )
    first = _current_claim(instance)
    _contend(instance, owner, first, "blocked", "hold-conflict")
    contenders = [
        claim
        for claim in _all_claims(instance)
        if claim.statement.subject.artifact_path.endswith("wi-42.json")
    ]
    for offset, claim in enumerate(contenders):
        _attest(instance, owner, claim, tmp_path, at=LATER - timedelta(minutes=2 - offset))

    def get_card(ref: str) -> Any:
        return service_playbill_get(
            instance,
            request=PlaybillGetRequestV1(ref=ref, evaluation_time=LATER),
            access=_ACCESS,
        ).card

    def query_row(**fields: Any) -> dict[str, Any]:
        result = service_playbill_query(
            instance,
            request=PlaybillQueryRequestV1.model_validate({**fields, "evaluation_time": LATER}),
        )
        return next(
            row for row in result.rows if row.get("subject_id", row.get("item_id")) == "wi-42"
        )

    claim_card = get_card(first.identity.name)
    subject_card = get_card(f"{SUBJECT_KIND}/wi-42")
    assert isinstance(claim_card, PlaybillGetClaimCardV1)
    assert isinstance(subject_card, PlaybillGetSubjectCardV1)
    assert "unsure_hold" in claim_card.flags

    compact = query_row(kind=SUBJECT_KIND, select=["status"])
    named = query_row(name=QUERY_NAME)
    (subject_row,) = subject_card.claims

    assert tuple(compact["flags"]) == subject_row.flags
    assert tuple(named["flags"]) == subject_row.flags
    assert set(claim_card.flags) <= set(subject_row.flags)


def test_governed_query_flags_are_derived_over_the_whole_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named or spec query reads some Claims; their flags see every slot contender."""

    from cruxible_core.service.discovery import compact_query as compact_module
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _all_claims
    from tests.test_service.test_get_reads import _contend

    instance, owner = seed_claims(tmp_path)
    first = _current_claim(instance)
    _contend(instance, owner, first, "blocked", "slot-contender")
    slot = {
        claim.identity.qualified
        for claim in _all_claims(instance)
        if claim.statement.subject.artifact_path.endswith("wi-42.json")
    }
    assert len(slot) == 2
    seen: list[set[str]] = []

    def capture(*_args: Any, identities: Any, **_kwargs: Any) -> dict[str, tuple[str, ...]]:
        seen.append(set(identities))
        return {identity: ("unsure_hold",) for identity in identities}

    monkeypatch.setattr(compact_module, "claim_flags", capture)
    from cruxible_client.contracts.claims import claim_path

    flags = compact_module._flags_for_paths(
        instance,
        instance.accepted_coordinate(),
        {claim_path(first.identity.name)},
        _WHEN,
    )

    assert seen == [slot]
    assert flags == {claim_path(first.identity.name): ("unsure_hold",)}


def _accept_tree(instance: Any, tree: dict[str, bytes], name: str) -> None:
    """Accept a hand-built tree as one generation (no authoring reuse review)."""

    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from cruxible_core.proposals.settlement import ChangeActorBinding
    from tests.core_support._support import client_material
    from tests.test_ledger.test_activation import _sign

    base = instance.accepted_coordinate()
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-08-24T16:00:00.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    assert proposed.evaluation.evaluated_tree_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(proposed.evaluation.evaluated_tree_oid),
        candidate=proposed.candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                proposed.candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=len(instance.accepted_history()),
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def test_get_cards_name_predicates_by_the_shared_rule_as_orient_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Addendum 2 on get: no last-segment names, and the same names orient shows."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.claim_types import (
        ClaimType,
        claim_type_digest,
        claim_type_path,
        render_claim_type,
    )
    from cruxible_client.contracts.get_reads import (
        PlaybillGetClaimCardV1,
        PlaybillGetSubjectCardV1,
    )
    from cruxible_core.proposals import proposals as proposals_module
    from cruxible_core.service.discovery.field_names import resolve_field
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._knowledge_loop_support import activate, authoring

    instance, owner = seed_claims(tmp_path)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        template = projection.typed.source(f"ClaimType:{PREDICATE}")
    assert isinstance(template, ClaimType)
    collisions = {
        predicate: template.model_copy(
            update={
                "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
                "predicate": predicate,
            }
        )
        for predicate in (
            "other.status",
            "third.status",
            f"{SUBJECT_KIND}.other.status",
            f"{SUBJECT_KIND}.subject_id",
        )
    }
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    for predicate, claim_type in collisions.items():
        tree[claim_type_path(predicate)] = render_claim_type(claim_type)
    # The collision fixture is deliberately near-duplicate vocabulary (every leaf
    # is ``status``); the reuse law's distinction review is not under test here.
    reuse = proposals_module.evaluate_vocabulary_reuse
    with monkeypatch.context() as patch:
        patch.setattr(
            proposals_module,
            "evaluate_vocabulary_reuse",
            lambda request, **kw: reuse(request, **{**kw, "accepted_interfaces": ()}),
        )
        _accept_tree(instance, tree, "collision-claim-types")
    for index, (predicate, claim_type) in enumerate(collisions.items()):
        request = authoring("wi-42", "ready", with_claim_type=False)
        request = request.model_copy(
            update={
                "statement": request.statement.model_copy(
                    update={
                        "claim_type": claim_type.identity,
                        "claim_type_digest": claim_type_digest(claim_type).tagged,
                        "predicate": predicate,
                    }
                )
            }
        )
        activate(
            instance,
            owner,
            service_propose_playbill_claim(
                instance,
                authoring=request,
                actor_id="owner",
                proposal_name=f"collision-{index}",
                timestamp=f"2026-08-24T17:00:0{index}.000000Z",
            ),
        )

    subject = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=f"{SUBJECT_KIND}/wi-42", evaluation_time=_WHEN),
        access=_ACCESS,
    ).card
    assert isinstance(subject, PlaybillGetSubjectCardV1)
    orient_kind = next(
        item for item in service_playbill_orient(instance).kinds if item.kind == SUBJECT_KIND
    )
    advertised = {item.predicate: item.name for item in orient_kind.predicates}

    assert advertised == {
        PREDICATE: "status",
        "other.status": "other.status",
        "third.status": "third.status",
        f"{SUBJECT_KIND}.other.status": f"{SUBJECT_KIND}.other.status",
        f"{SUBJECT_KIND}.subject_id": f"{SUBJECT_KIND}.subject_id",
    }
    assert sorted(row.predicate for row in subject.claims) == sorted(advertised.values())
    for predicate, name in advertised.items():
        assert resolve_field(name, SUBJECT_KIND, frozenset(advertised)) == predicate

    for row in subject.claims:
        assert isinstance(row.claim, str)
        claim = service_playbill_get(
            instance,
            request=PlaybillGetRequestV1(ref=row.claim, evaluation_time=_WHEN),
            access=_ACCESS,
        ).card
        assert isinstance(claim, PlaybillGetClaimCardV1)
        assert claim.predicate == advertised[claim.predicate_full] == row.predicate


# -- exact-content values read as text on every verb --------------------------

_RULING = b"The ruling, exactly as written.\n"
_LONG_RULING = ("A long method law. " * 40).encode()
_BINARY = b"\xff\xfe\x00opaque"


@pytest.fixture(scope="module")
def exact_world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, dict[str, Any]]:
    return seed_exact_content(
        tmp_path_factory.mktemp("exact-content"),
        {"wi-42": _RULING, "wi-long": _LONG_RULING, "wi-bin": _BINARY},
    )


def _exact_get(
    instance: Any, ref: str, *, access: BodyAccessContext = _ACCESS, **fields: Any
) -> PlaybillGetResultV1:
    return service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=ref, evaluation_time=_WHEN, **fields),
        access=access,
    )


def _exact_query(instance: Any, **fields: Any) -> PlaybillQueryResult:
    return service_playbill_query(
        instance,
        request=PlaybillQueryRequestV1.model_validate({"evaluation_time": _WHEN, **fields}),
    )


def test_get_and_query_show_an_exact_content_value_as_its_text(
    exact_world: tuple[Any, dict[str, Any]],
) -> None:
    from cruxible_client.contracts.get_reads import (
        GET_SUMMARY_TEXT_MAX_CHARS,
        PlaybillExactContentRefV1,
        PlaybillGetTruncatedTextV1,
    )

    instance, seeded = exact_world
    ruling, long_ruling, binary = seeded["wi-42"], seeded["wi-long"], seeded["wi-bin"]
    text = _RULING.decode()
    cut = PlaybillGetTruncatedTextV1(
        value=_LONG_RULING.decode()[:GET_SUMMARY_TEXT_MAX_CHARS], length=len(_LONG_RULING)
    )
    marker = PlaybillExactContentRefV1(
        exact_content="binary", content_digest=binary.digest, length=len(_BINARY)
    )

    # get: the Claim card and the Subject row read the text, the digest beside it.
    claim = _exact_get(instance, ruling.claim_id).card
    assert isinstance(claim, PlaybillGetClaimCardV1)
    assert (claim.value, claim.content_digest) == (text, ruling.digest)
    subject = _exact_get(instance, ruling.subject).card
    assert isinstance(subject, PlaybillGetSubjectCardV1)
    ((row),) = subject.claims
    assert (row.claim, row.value, row.content_digest) == (ruling.claim_id, text, ruling.digest)

    # A long value is cut on the card by the card rule; evidence reads it whole.
    long_card = _exact_get(instance, long_ruling.claim_id).card
    assert isinstance(long_card, PlaybillGetClaimCardV1) and long_card.value == cut
    evidence = _exact_get(instance, long_ruling.claim_id, detail="evidence").evidence
    assert evidence is not None
    assert (evidence.value, evidence.content_digest) == (_LONG_RULING.decode(), long_ruling.digest)

    # Bytes that are not UTF-8 text show as a typed marker, never an error.
    binary_card = _exact_get(instance, binary.claim_id).card
    assert isinstance(binary_card, PlaybillGetClaimCardV1)
    assert binary_card.value == marker and binary_card.content_digest == binary.digest

    # History reads each revision's value the same way.
    history = _exact_get(instance, ruling.claim_id, detail="history").history
    assert history is not None
    assert [(item.value, item.content_digest) for item in history.revisions] == [
        (text, ruling.digest)
    ]

    # A long revision value is cut by the same card rule.
    long_history = _exact_get(instance, long_ruling.claim_id, detail="history").history
    assert long_history is not None
    assert [(item.value, item.content_digest) for item in long_history.revisions] == [
        (cut, long_ruling.digest)
    ]

    # query: the same values, on compact and spec rows alike.
    compact = {
        row["subject"]: row
        for row in _exact_query(instance, kind=EXACT_KIND, select=["status"]).rows
    }
    declared = work_item_query("project.exact").model_dump(mode="json")
    spec = {
        row["subject"]: row
        for row in _exact_query(
            instance, spec=QueryDefinitionSpecV1.model_validate({**declared, "pins": []})
        ).rows
    }
    for seeded_claim, shown in ((ruling, text), (long_ruling, cut), (binary, marker)):
        for rows in (compact, spec):
            row_of = rows[seeded_claim.subject]
            assert row_of["status"] == shown
            # Query stays values-first: the digest is get's, not a row key.
            assert not any("digest" in key for key in row_of)
        card = _exact_get(instance, seeded_claim.subject).card
        assert isinstance(card, PlaybillGetSubjectCardV1)
        assert card.claims[0].content_digest == seeded_claim.digest
        assert card.claims[0].value == compact[seeded_claim.subject]["status"]
        assert card.claims[0].flags == tuple(compact[seeded_claim.subject]["flags"])


def test_contains_matches_exact_content_text_never_its_digest(
    exact_world: tuple[Any, dict[str, Any]],
) -> None:
    instance, seeded = exact_world
    ruling = seeded["wi-42"]

    (row,) = _exact_query(instance, contains="exactly as written").rows
    assert (row["claim"], row["value"]) == (ruling.claim_id, _RULING.decode())
    assert set(row) == {"subject", "subject_id", "kind", "predicate", "value", "claim", "flags"}
    (kind_row,) = _exact_query(instance, kind=EXACT_KIND, contains="exactly as written").rows
    assert kind_row["subject"] == ruling.subject
    # A digest is proof, not a value: no search matches it.
    assert _exact_query(instance, contains=ruling.digest.split(":")[1][:16]).rows == ()


def test_a_caller_who_may_not_read_bodies_still_reads_exact_content_as_text(
    exact_world: tuple[Any, dict[str, Any]],
) -> None:
    """Ruling exact-content-read-only: the value is a Claim value, not a body read."""

    instance, seeded = exact_world
    ruling = seeded["wi-42"]
    text = _RULING.decode()
    reader = BodyAccessContext(principal_id="reader", can_read_body=False)

    card = _exact_get(instance, ruling.claim_id, access=reader).card
    assert isinstance(card, PlaybillGetClaimCardV1)
    assert (card.value, card.content_digest) == (text, ruling.digest)
    history = _exact_get(instance, ruling.claim_id, access=reader, detail="history").history
    assert history is not None
    assert [item.value for item in history.revisions] == [text]
    rows = _exact_query(instance, kind=EXACT_KIND, select=["status"]).rows
    assert {row["subject"]: row["status"] for row in rows}[ruling.subject] == text
    (found,) = _exact_query(instance, contains="exactly as written").rows
    assert found["claim"] == ruling.claim_id


def test_query_reserves_no_digest_row_key() -> None:
    from cruxible_core.service.discovery.field_names import RESERVED_FIELD_NAMES

    assert RESERVED_FIELD_NAMES == frozenset(
        {"subject_id", "subject", "kind", "predicate", "claim", "flags"}
    )


def test_query_cuts_every_long_string_by_the_card_rule(tmp_path: Path) -> None:
    from cruxible_client.contracts.get_reads import (
        GET_SUMMARY_TEXT_MAX_CHARS,
        PlaybillGetTruncatedTextV1,
    )
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._knowledge_loop_support import activate, authoring, subject_shell
    from tests.test_claims.test_claims import _claim_type

    note = _claim_type().model_copy(update={"literal_schema": {"type": "string"}})
    instance, owner = seed_claims(tmp_path, claim_type_override=note)
    long_note = "a long literal note " * 40
    activate(
        instance,
        owner,
        service_propose_playbill_claim(
            instance,
            authoring=authoring("wi-44", long_note, with_claim_type=False).model_copy(
                update={"subject_shell": subject_shell("wi-44")}
            ),
            actor_id="owner",
            proposal_name="long-note",
            timestamp="2026-08-16T20:10:00.000000Z",
        ),
    )
    cut = PlaybillGetTruncatedTextV1(
        value=long_note[:GET_SUMMARY_TEXT_MAX_CHARS], length=len(long_note)
    )

    rows = {
        row["subject_id"]: row
        for row in _exact_query(instance, kind=SUBJECT_KIND, select=["status"]).rows
    }
    assert rows["wi-44"]["status"] == cut and rows["wi-42"]["status"] == "ready"
    (found,) = _exact_query(instance, contains="long literal note").rows
    assert found["value"] == cut
    spec = QueryDefinitionSpecV1.model_validate(
        {**work_item_query("project.notes").model_dump(mode="json"), "pins": []}
    )
    by_id = {row["item_id"]: row for row in _exact_query(instance, spec=spec).rows}
    assert by_id["wi-44"]["status"] == cut

    # get agrees: the card is cut the same way, evidence reads it whole.
    card = _exact_get(instance, f"{SUBJECT_KIND}/wi-44").card
    assert isinstance(card, PlaybillGetSubjectCardV1) and card.claims[0].value == cut
    assert isinstance(card.claims[0].claim, str)
    evidence = _exact_get(instance, card.claims[0].claim, detail="evidence").evidence
    assert evidence is not None and evidence.value == long_note
    history = _exact_get(instance, card.claims[0].claim, detail="history").history
    assert history is not None and [item.value for item in history.revisions] == [cut]


def test_projected_exact_content_follows_the_engines_visibility_policy(
    exact_world: tuple[Any, dict[str, Any]],
) -> None:
    """Regression (Codex F-001): text is shown only for Claims the engine selected.

    Under visible_verdicts=["contradicted"] the engine answers no value for a
    supported ruling, so the row shows none: the reader never adds a value.
    """

    instance, seeded = exact_world
    declared = work_item_query("project.exact.contradicted").model_dump(mode="json")
    declared["evaluation_policy"]["visible_verdicts"] = ["contradicted"]
    rows = _exact_query(
        instance, spec=QueryDefinitionSpecV1.model_validate({**declared, "pins": []})
    ).rows

    assert {row["item_id"]: row["status"] for row in rows} == {
        claim.subject.split("/", 1)[1]: None for claim in seeded.values()
    }


def test_two_spans_of_one_body_are_two_values_on_query_and_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Codex F-002): exact-content values are keyed by digest AND span.

    Two Claims in one slot that select different spans of the same body are two
    values: both are shown, and a one-cardinality slot holding them is contested.
    Accepted authoring always states a whole-body span, so the second Claim's
    live value is re-pointed at the first Claim's body to stand the case up.
    """

    from cruxible_client.contracts.claims import ExactContentClaimObject
    from cruxible_client.contracts.semantic import ContentSpan
    from cruxible_core.service.discovery import get as get_module
    from cruxible_core.service.discovery import query_values

    instance, seeded = seed_exact_content(tmp_path, {"wi-42": b"first second", "wi-43": b"x"})
    first, second = seeded["wi-42"], seeded["wi-43"]
    home = f"subjects/{first.subject}.json"
    spans = {first.claim_id: (0, 5), second.claim_id: (6, 12)}

    real_values = query_values.read_live_values

    def live_values(*args: Any, **kwargs: Any) -> list[Any]:
        from dataclasses import replace

        values = []
        for item in real_values(*args, **kwargs):
            claim_id = item.identity.removeprefix("Claim:")
            if claim_id in spans:
                item = replace(item, subject_path=home, value=first.digest, span=spans[claim_id])
            values.append(item)
        return values

    monkeypatch.setattr(query_values, "read_live_values", live_values)
    wanted = {home, f"subjects/{second.subject}.json"}
    real_slot = get_module._slot_values

    def slot_values(instance: Any, coordinate: Any, *, subject_path: str, **kw: Any) -> Any:
        rows = []
        for path in sorted(wanted) if subject_path == home else [subject_path]:
            for row in real_slot(instance, coordinate, subject_path=path, **kw):
                start, end = spans[row.claim_id]
                rows.append(
                    row.model_copy(
                        update={
                            "subject_path": home,
                            "value": first.digest,
                            "object": ExactContentClaimObject(
                                content_digest=first.digest,
                                span=ContentSpan(
                                    content_digest=first.digest, start_byte=start, end_byte=end
                                ),
                            ),
                        }
                    )
                )
        return tuple(rows)

    monkeypatch.setattr(get_module, "_slot_values", slot_values)

    rows = {
        row["subject_id"]: row
        for row in _exact_query(instance, kind=EXACT_KIND, select=["status"]).rows
    }
    assert sorted(rows["wi-42"]["status"]) == ["first", "second"]
    assert "contested" in rows["wi-42"]["flags"]

    card = _exact_get(instance, first.subject).card
    assert isinstance(card, PlaybillGetSubjectCardV1)
    (row,) = card.claims
    assert sorted(row.value) == ["first", "second"]
    assert "contested" in row.flags


@pytest.fixture(scope="module")
def competing_worlds(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One Subject whose one-cardinality status has two supported, competing Claims.

    ``exact``: two different rulings; ``literal``: ``ready`` and ``blocked``.
    """

    from cruxible_client.contracts.captures import foreign_source_capture_contract
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._exact_content_support import (
        add_exact_claim,
        seed_exact_content_into,
    )
    from tests.core_support._knowledge_loop_support import activate, authoring
    from tests.core_support._support import initialize_local
    from tests.test_authoring.test_authoring_preflight import _seed_claim_surface

    exact, owner = initialize_local(tmp_path_factory.mktemp("competing-exact"))
    (first,) = seed_exact_content_into(exact, owner, {"wi-42": b"first ruling"}).values()
    add_exact_claim(
        exact,
        owner,
        "wi-42",
        b"second ruling",
        claim_id="CLM-" + "9" * 32,
        existing=(first.claim_id,),
    )
    literal, literal_owner = initialize_local(tmp_path_factory.mktemp("competing-literal"))
    _seed_claim_surface(
        literal, literal_owner, contract=foreign_source_capture_contract("fixture.work-items")
    )
    for index, value in enumerate(("ready", "blocked")):
        activate(
            literal,
            literal_owner,
            service_propose_playbill_claim(
                literal,
                authoring=authoring("wi-42", value, with_claim_type=False),
                actor_id="owner",
                proposal_name=f"compete-{index}",
                timestamp=f"2026-08-16T20:0{index}:00.000000Z",
            ),
        )
    return {"exact": exact, "literal": literal}


def _one_result_spec(conflict_behavior: str) -> QueryDefinitionSpecV1:
    declared = work_item_query("project.one").model_dump(mode="json")
    declared["result_cardinality"] = "one"
    declared["default_budgets"]["max_results"] = 1
    declared["maximum_budgets"]["max_results"] = 1
    declared["evaluation_policy"]["conflict_behavior"] = conflict_behavior
    return QueryDefinitionSpecV1.model_validate({**declared, "pins": []})


@pytest.mark.parametrize("conflict_behavior", ["surface_conflicts", "refuse_on_conflict"])
def test_competing_exact_content_follows_conflict_behavior_as_literals_do(
    competing_worlds: dict[str, Any], conflict_behavior: str
) -> None:
    """Regression (Codex F-003): the engine's conflict path decides exact content too.

    Two supported, competing exact-content Claims answer a one-cardinality read
    exactly as two literals do: ``refuse_on_conflict`` refuses with the engine's
    ``playbill.query.claim_conflict``, and ``surface_conflicts`` answers no value
    with the ``contested`` flag. Neither shows both texts.
    """

    from cruxible_core.service.read_refusals import ReadRefusalError

    def answer(kind: str) -> object:
        try:
            rows = _exact_query(
                competing_worlds[kind], spec=_one_result_spec(conflict_behavior)
            ).rows
        except ReadRefusalError as refusal:
            return ("refused", refusal.error_code)
        return [(row["status"], row["flags"]) for row in rows]

    exact, literal = answer("exact"), answer("literal")

    assert exact == literal
    if conflict_behavior == "refuse_on_conflict":
        assert exact == ("refused", "playbill.query.claim_conflict")
    else:
        assert exact == [(None, ["contested"])]

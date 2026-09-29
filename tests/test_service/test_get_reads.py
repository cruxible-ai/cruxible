"""``get`` resolves every reference form to one thing and answers values first."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.captures import (
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.get_reads import (
    PlaybillByteRangeV1,
    PlaybillGetClaimCardV1,
    PlaybillGetRequestV1,
    PlaybillGetResultV1,
    PlaybillGetSubjectCardV1,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_propose_playbill_document,
    service_store_playbill_body,
)
from cruxible_core.service.discovery import get as get_module
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.read_flags import verdict_flags
from cruxible_core.service.list_pages import PlaybillListCursorMismatch
from cruxible_core.service.read_refusals import ReadRefusalError
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._knowledge_loop_support import EVALUATION_TIME, PREDICATE, seed_claims
from tests.test_proposals.test_proposal_readmit import _accept, _shell

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)
_WHEN = datetime.fromisoformat(EVALUATION_TIME)
_SUBJECT = "project.work_item/wi-42"
_CONTRACT = foreign_source_capture_contract("fixture.work-items")
_CONTRACT_NAME = _CONTRACT.identity.qualified
_BODY = b"# Design\n\nThe body of the design document.\n"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root: Path = tmp_path_factory.mktemp("get-world")
    instance, owner = seed_claims(root)
    before = instance.accepted_coordinate()
    body = service_store_playbill_body(instance, content=_BODY).digest
    accepted = service_propose_playbill_document(
        instance,
        shell=_shell("design", body, title="Design"),
        actor_id="owner",
        proposal_name="get-design",
        timestamp="2026-08-16T20:30:00.000000Z",
    )
    _accept(instance, owner, accepted)
    # A Document named like the predicate leaf makes the bare name ambiguous.
    clash = service_propose_playbill_document(
        instance,
        shell=_shell("status", body, title="Status"),
        actor_id="owner",
        proposal_name="get-status",
        timestamp="2026-08-16T20:31:00.000000Z",
    )
    _accept(instance, owner, clash)
    pending = service_propose_playbill_document(
        instance,
        shell=_shell("pending", body, title="Pending"),
        actor_id="owner",
        proposal_name="get-pending",
        timestamp="2026-08-16T20:32:00.000000Z",
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        claim_ids = {
            str(row["subject_path"]): str(row["identity"]).removeprefix("Claim:")
            for row in projection.typed.connection.execute(
                "SELECT identity, subject_path FROM claims"
            )
        }
    return {
        "instance": instance,
        "before": before,
        "claim": claim_ids[f"subjects/{_SUBJECT}.json"],
        "pending": pending.proposal.admission.proposal_id,
    }


def _get(instance: PlaybillInstance, ref: str, **fields: Any) -> PlaybillGetResultV1:
    return service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=ref, **{"evaluation_time": _WHEN, **fields}),
        access=_ACCESS,
    )


def _refusal(instance: PlaybillInstance, ref: str, **fields: Any) -> ReadRefusalError:
    with pytest.raises(ReadRefusalError) as caught:
        _get(instance, ref, **fields)
    return caught.value


def test_every_claim_reference_form_reads_the_same_values_first_card(world: dict[str, Any]) -> None:
    instance, claim = world["instance"], world["claim"]
    forms = (
        claim,
        f"Claim:{claim}",
        claim[:12],
        f"claims/{claim[4:6]}/{claim}.json",
    )
    cards = [_get(instance, form).card for form in forms]

    card = cards[0]
    assert isinstance(card, PlaybillGetClaimCardV1)
    assert all(item == card for item in cards)
    assert card.claim == claim
    assert card.subject == _SUBJECT
    assert (card.predicate, card.predicate_full) == ("status", PREDICATE)
    assert card.value == "ready"
    assert (card.verdict, card.status, card.revision) == ("supported", "accepted", 1)
    assert card.accepted is not None and card.contenders == () and card.flags == ()
    assert card.next[0] == f'cruxible_playbill_get(ref="{claim}", detail="evidence")'


def test_next_steps_are_rendered_for_the_callers_surface(world: dict[str, Any]) -> None:
    instance, claim = world["instance"], world["claim"]

    cli = _get(instance, claim, surface="cli").card
    sdk = _get(instance, claim, surface="sdk").card

    assert cli is not None and cli.next[0] == f"cruxible playbill get {claim} --detail evidence"
    assert sdk is not None and sdk.next[0] == f'pb.get("{claim}", detail="evidence")'


def test_a_subject_card_carries_its_claims_as_values(world: dict[str, Any]) -> None:
    instance = world["instance"]
    for form in (_SUBJECT, f"Subject:{_SUBJECT}", f"subjects/{_SUBJECT}.json"):
        result = _get(instance, form)
        card = result.card
        assert isinstance(card, PlaybillGetSubjectCardV1)
        assert result.ref == _SUBJECT and result.kind == "subject"
        assert card.kind == "project.work_item" and card.lifecycle == "live"
        assert [(row.predicate, row.value, row.flags) for row in card.claims] == [
            ("status", "ready", ())
        ]
        assert card.incoming_count == 0


def test_a_claim_type_card_names_evidence_by_contract_identity(world: dict[str, Any]) -> None:
    instance = world["instance"]
    for form in (PREDICATE, f"ClaimType:{PREDICATE}"):
        result = _get(instance, form)
        assert result.ref == f"ClaimType:{PREDICATE}"
        card = result.card
        assert card is not None and card.model_dump()["evidence"] == (_CONTRACT_NAME,)
        dumped = json.dumps(result.model_dump(mode="json")["card"])
        assert "sha256:" not in dumped
    card = _get(instance, f"ClaimType:{PREDICATE}").card
    assert card is not None
    fields = card.model_dump()
    assert fields["object"] == "string" and fields["cardinality"] == "one"
    assert fields["live_claims"] == 2 and "description" not in card.model_dump(mode="json")


def test_an_unresolvable_digest_rule_shows_unresolved_not_the_digest(
    world: dict[str, Any],
) -> None:
    instance = world["instance"]
    names = CaptureContractNames(instance, instance.accepted_coordinate())

    assert names.name("sha256:" + "a" * 64) == "unresolved:aaaaaaaaaaaa"


def test_a_capture_contract_card_names_its_version_and_admitting_claim_types(
    world: dict[str, Any],
) -> None:
    instance = world["instance"]
    result = _get(instance, _CONTRACT_NAME)
    card = result.card
    assert card is not None
    fields = card.model_dump()
    assert fields["version"] == 1 and fields["lifecycle"] == "live"
    assert fields["admitted_by"] == (PREDICATE,)
    assert fields["captures"]["sources"] == ["fixture.work-items"]


def test_evidence_names_each_capture_by_contract_identity_and_version(
    world: dict[str, Any],
) -> None:
    instance, claim = world["instance"], world["claim"]
    evidence = _get(instance, claim, detail="evidence").evidence
    assert evidence is not None

    (capture,) = evidence.captures
    assert capture.contract == _CONTRACT_NAME
    assert capture.version == 1
    assert capture.source == "fixture.work-items"
    assert capture.admitted is True
    assert len(capture.capture) == len("sha256:") + 12
    digest = capture_contract_digest(_CONTRACT).tagged
    assert digest not in json.dumps(evidence.model_dump(mode="json"))


def test_why_history_and_proof_reuse_todays_services(world: dict[str, Any]) -> None:
    instance, claim = world["instance"], world["claim"]

    why = _get(instance, claim, detail="why").why
    proof = _get(instance, claim, detail="proof").proof
    history = _get(instance, claim, detail="history").history
    subject_why = _get(instance, _SUBJECT, detail="why").why

    assert why is not None and why["tag"].startswith("playbill-claim-explanation-")
    assert proof is not None and proof["tag"] == "playbill-claim-read-v2"
    assert subject_why is not None and subject_why["tag"] == "playbill-explain-v1"
    assert history is not None
    (revision,) = history.revisions
    assert (revision.revision, revision.value, revision.actor) == (1, "ready", "owner")
    assert revision.lifecycle == "live"


def test_a_document_reads_as_metadata_then_body_by_range(world: dict[str, Any]) -> None:
    instance = world["instance"]
    card = _get(instance, "Document:design").card
    assert card is not None
    assert card.model_dump()["size"] == len(_BODY)
    assert _get(instance, "document:design").card == card

    whole = _get(instance, "Document:design", detail="body").body
    part = _get(
        instance, "Document:design", detail="body", range=PlaybillByteRangeV1(start=2, end=8)
    ).body
    assert whole is not None and whole.text == _BODY.decode()
    assert part is not None and part.text == _BODY[2:8].decode()
    assert (part.range.start, part.range.end, part.size) == (2, 8, len(_BODY))
    history = _get(instance, "Document:design", detail="history").history
    assert history is not None and len(history.revisions) == 1


def test_a_body_over_the_cap_refuses_with_the_range_repair(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = world["instance"]
    monkeypatch.setattr(get_module, "GET_BODY_DEFAULT_MAX_BYTES", 10)

    refused = _refusal(instance, "Document:design", detail="body")

    assert refused.error_code == "playbill.get.body_too_large"
    assert refused.repair is not None
    assert refused.repair.arguments == {
        "ref": "Document:design",
        "detail": "body",
        "range": "0:10",
    }
    assert "range=" in str(refused)
    beyond = _refusal(
        instance, "Document:design", detail="body", range=PlaybillByteRangeV1(start=999, end=1000)
    )
    assert beyond.error_code == "playbill.get.range_out_of_bounds"


def test_wrong_names_refuse_with_the_nearest_names(world: dict[str, Any]) -> None:
    instance = world["instance"]

    subject = _refusal(instance, "project.work_item/wi-4")
    predicate = _refusal(instance, "ClaimType:project.work_item.statu")
    kind = _refusal(instance, "project.work_iten/wi-42")
    unknown = _refusal(instance, "no_such_thing_anywhere")

    assert subject.error_code == "playbill.get.ref_not_found"
    assert subject.http_status == 404
    assert "project.work_item/wi-42" in subject.candidates
    assert predicate.candidates[0] == f"ClaimType:{PREDICATE}"
    assert kind.candidates == ("project.work_item",)
    # A typo in a short name still finds the predicate by its last segment.
    assert f"ClaimType:{PREDICATE}" in _refusal(instance, "statu").candidates
    assert unknown.candidates == () and unknown.repair is not None
    assert unknown.repair.operation == "playbill.orient"


def test_an_ambiguous_name_refuses_listing_every_candidate(world: dict[str, Any]) -> None:
    refused = _refusal(world["instance"], "status")

    assert refused.error_code == "playbill.get.ref_ambiguous"
    assert refused.http_status == 409
    assert set(refused.candidates) == {f"ClaimType:{PREDICATE}", "Document:status"}


def test_a_detail_that_does_not_apply_refuses_naming_the_ones_that_do(
    world: dict[str, Any],
) -> None:
    refused = _refusal(world["instance"], "Document:design", detail="evidence")

    assert refused.error_code == "playbill.get.detail_unsupported"
    assert refused.context["allowed"] == ["summary", "history", "proof", "body"]


def test_a_proposal_reads_by_id_prefix_with_its_next_step(world: dict[str, Any]) -> None:
    instance, proposal = world["instance"], world["pending"]
    result = _get(instance, proposal[:20])
    card = result.card
    assert result.kind == "proposal" and card is not None
    fields = card.model_dump()
    assert fields["proposal"] == proposal
    assert fields["status"] == "open" and fields["verdict"] == "candidate"
    assert {"path": "documents/pending.json", "change": "create"} in [
        dict(item) for item in fields["changes"]
    ]
    assert fields["next"] == (f'cruxible_playbill_review(proposal_id="{proposal}")',)


def test_at_reads_an_earlier_generation_by_git_oid(world: dict[str, Any]) -> None:
    instance, before = world["instance"], world["before"]

    earlier = _get(instance, "Document:design", at=None)
    assert earlier.coordinate.git_oid == instance.accepted_coordinate().git_oid[:12]
    refused = _refusal(instance, "Document:design", at=before.git_oid)
    assert refused.error_code == "playbill.get.ref_not_found"
    at_before = _get(
        instance,
        _SUBJECT,
        at=AcceptedCoordinate.from_internal(before).model_dump(mode="json"),
    )
    assert at_before.coordinate.git_oid == before.git_oid[:12]
    bogus = _refusal(instance, _SUBJECT, at="0" * 40)
    assert bogus.error_code == "playbill.read.coordinate_not_accepted"


def test_flags_come_from_the_verdict_and_slot_status() -> None:
    assert verdict_flags("supported", "accepted", held=False) == ()
    assert verdict_flags("stale_evidence", "conflicted", held=True) == (
        "stale",
        "contested",
        "unsure_hold",
    )
    assert verdict_flags("contradicted", "overturned", held=False) == ("contradicted",)
    # A Claim whose own evidence both supports and contradicts it is contested
    # on every verb, even though resolution refuses it rather than conflicting.
    assert verdict_flags("unresolved", "refused") == ("contested",)


# -- identity evidence rules (ClaimType v6 and succession) -------------------------


def test_contract_succession_reads_by_identity_and_version(tmp_path: Path) -> None:
    from cruxible_client.contracts.captures import render_capture_contract
    from tests.test_claims.test_identity_evidence_rules import (
        CONTRACT_PATH,
        IDENTITY,
        ORIGINAL,
        _successor,
        _v6_type,
        _World,
    )
    from tests.test_claims.test_identity_evidence_rules import PREDICATE as V6_PREDICATE
    from tests.test_claims.test_superseded_contract_reads import _observe

    world = _World(tmp_path)
    world.seed(_v6_type())
    first = _observe(world, b"status: ready")
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(_successor(ORIGINAL))
    world.accept(tree, name="improve-contract")
    _observe(world, b"status: done", revises=first)
    instance = world.instance

    evidence = _get(instance, first, detail="evidence").evidence
    claim_type = _get(instance, V6_PREDICATE).card
    contract = _get(instance, IDENTITY.qualified).card
    history = _get(instance, IDENTITY.qualified, detail="history").history

    assert evidence is not None
    assert sorted((item.contract, item.version) for item in evidence.captures) == [
        (IDENTITY.qualified, 1),
        (IDENTITY.qualified, 2),
    ]
    assert claim_type is not None and claim_type.model_dump()["evidence"] == (IDENTITY.qualified,)
    assert contract is not None and contract.model_dump()["version"] == 2
    assert contract.model_dump()["admitted_by"] == (V6_PREDICATE,)
    # Newest first; revision numbers count from the oldest.
    assert history is not None and [item.revision for item in history.revisions] == [2, 1]
    assert _get(instance, first, detail="proof").proof is not None
    assert _get(instance, first, detail="why").why is not None


def test_a_digest_rule_resolves_to_the_contract_it_names(tmp_path: Path) -> None:
    from tests.test_claims.test_identity_evidence_rules import (
        IDENTITY,
        ORIGINAL,
        _digest,
        _digest_rule,
        _v5_type,
        _World,
    )
    from tests.test_claims.test_identity_evidence_rules import PREDICATE as V5_PREDICATE

    world = _World(tmp_path)
    world.seed(_v5_type(_digest_rule(_digest(ORIGINAL))))

    card = _get(world.instance, f"ClaimType:{V5_PREDICATE}").card

    assert card is not None and card.model_dump()["evidence"] == (IDENTITY.qualified,)


def test_an_unsure_attestation_with_nothing_to_hold_shows_no_hold(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.test_integration.test_next_closed_loop import EVALUATION_TIME as LATER
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _attest, _next

    instance, owner = seed_claims(tmp_path)
    claim = _current_claim(instance)
    _attest(instance, owner, claim, tmp_path, at=LATER - timedelta(minutes=1))

    card = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=claim.identity.name, evaluation_time=LATER),
        access=_ACCESS,
    ).card

    # next parks no row for a supported Claim, so there is no hold to show.
    assert _next(instance).status.held == 0
    assert isinstance(card, PlaybillGetClaimCardV1) and card.flags == ()


def test_a_contested_slot_shows_every_live_value_with_the_contested_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.claims import claim_statement_digest
    from tests.core_support._claim_authoring_support import (
        ExistingStatementHandoffV1,
        service_propose_playbill_claim,
    )
    from tests.core_support._knowledge_loop_support import activate, authoring
    from tests.test_integration.test_next_closed_loop import EVALUATION_TIME as LATER
    from tests.test_integration.test_next_closed_loop import _current_claim

    instance, owner = seed_claims(tmp_path)
    first = _current_claim(instance)
    activate(
        instance,
        owner,
        service_propose_playbill_claim(
            instance,
            authoring=authoring("wi-42", "blocked", with_claim_type=False).model_copy(
                update={
                    "existing_statement_handoffs": (
                        ExistingStatementHandoffV1(
                            statement_digest=claim_statement_digest(first.statement).tagged,
                            disposition="contradict",
                        ),
                    )
                }
            ),
            actor_id="owner",
            proposal_name="get-conflict",
            timestamp="2026-08-24T17:00:03.000000Z",
        ),
    )

    # Before the contender's evidence is observed, resolution still selects one.
    resolved = _get(instance, _SUBJECT).card
    assert isinstance(resolved, PlaybillGetSubjectCardV1)
    assert [(row.value, row.flags) for row in resolved.claims] == [("ready", ())]
    subject = _get(instance, _SUBJECT, evaluation_time=LATER).card
    claim = _get(instance, first.identity.name, evaluation_time=LATER).card

    assert isinstance(subject, PlaybillGetSubjectCardV1)
    (row,) = subject.claims
    assert sorted(row.value) == ["blocked", "ready"]
    assert "contested" in row.flags
    assert isinstance(claim, PlaybillGetClaimCardV1)
    assert "contested" in claim.flags
    assert [item.value for item in claim.contenders] == ["blocked"]

    # A cut contender in a multi-value Subject row must point to that Claim,
    # not the Subject (which has no evidence detail) or the short winning Claim.
    original = get_module._claim_value
    monkeypatch.setattr(
        get_module,
        "_claim_value",
        lambda row: "long " * 200 if row.claim_id != first.identity.name else original(row),
    )
    subject = _get(instance, _SUBJECT, evaluation_time=LATER).card
    claim = _get(instance, first.identity.name, evaluation_time=LATER).card
    assert isinstance(subject, PlaybillGetSubjectCardV1)
    assert isinstance(claim, PlaybillGetClaimCardV1)
    contender = claim.contenders[0].claim
    expected = f'cruxible_playbill_get(ref="{contender}", detail="evidence")'
    assert subject.next[0] == expected
    assert claim.next[1] == expected


def _contend(instance: PlaybillInstance, owner: Any, against: Any, value: str, name: str) -> None:
    from cruxible_client.contracts.claims import claim_statement_digest
    from tests.core_support._claim_authoring_support import (
        ExistingStatementHandoffV1,
        service_propose_playbill_claim,
    )
    from tests.core_support._knowledge_loop_support import activate, authoring

    activate(
        instance,
        owner,
        service_propose_playbill_claim(
            instance,
            authoring=authoring("wi-42", value, with_claim_type=False).model_copy(
                update={
                    "existing_statement_handoffs": (
                        ExistingStatementHandoffV1(
                            statement_digest=claim_statement_digest(against.statement).tagged,
                            disposition="contradict",
                        ),
                    )
                }
            ),
            actor_id="owner",
            proposal_name=name,
            timestamp="2026-08-24T17:00:03.000000Z",
        ),
    )


def test_a_new_contender_ends_the_unsure_hold_exactly_as_next_decides(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.test_integration.test_next_closed_loop import EVALUATION_TIME as LATER
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _all_claims, _attest, _next, _rows

    instance, owner = seed_claims(tmp_path)
    first = _current_claim(instance)
    _contend(instance, owner, first, "blocked", "hold-conflict")
    contenders = [
        claim
        for claim in _all_claims(instance)
        if claim.statement.subject.artifact_path.endswith("wi-42.json")
    ]
    for offset, claim in enumerate(contenders):
        _attest(instance, owner, claim, tmp_path, at=LATER - timedelta(minutes=2 - offset))

    def flags() -> tuple[str, ...]:
        card = _get(instance, first.identity.name, evaluation_time=LATER).card
        assert isinstance(card, PlaybillGetClaimCardV1)
        return card.flags

    assert not _rows(_next(instance), "claim_conflicted")
    assert "unsure_hold" in flags()

    # A contender nobody examined brings the conflict back; the flag follows next.
    _contend(instance, owner, contenders[-1], "done", "hold-conflict-new")
    assert _rows(_next(instance), "claim_conflicted")
    assert "unsure_hold" not in flags()


def test_a_held_stale_dependency_shows_the_unsure_hold_next_parks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from cruxible_core.service.discovery import next as next_module
    from tests.test_integration.test_next_closed_loop import EVALUATION_TIME as LATER
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _attest, _next, _rows

    instance, owner = seed_claims(tmp_path)
    source = _current_claim(instance)
    dependent = _current_claim(instance, subject_id="wi-43")

    def flags() -> tuple[str, ...]:
        card = _get(instance, dependent.identity.name, evaluation_time=LATER).card
        assert isinstance(card, PlaybillGetClaimCardV1)
        return card.flags

    # The dependent records an earlier version of the source as its backing
    # input; everything else -- the Claims, the attestation store, the hold
    # coverage -- is the instance's own.
    earlier = "sha256:" + "0" * 64

    def recorded(facts: Any) -> Any:
        rows = []
        for row in facts.claims:
            claim = row.accepted.claim
            if claim.identity.qualified == dependent.identity.qualified:
                backing = claim.backing.model_copy(update={"input_claim_digests": (earlier,)})
                claim = claim.model_copy(update={"backing": backing})
                row = row.model_copy(
                    update={"accepted": row.accepted.model_copy(update={"claim": claim})}
                )
            rows.append(row)
        return facts.model_copy(update={"claims": tuple(rows)})

    real_facts = next_module.build_accepted_query_facts
    real_read = next_module._AcceptedQueryFactsRead.build
    real_lineages = next_module._bounded_claim_lineages

    def lineages(*args: Any, **kwargs: Any) -> Any:
        found, incomplete = real_lineages(*args, **kwargs)
        return {
            path: (*digests, earlier) if path.endswith(f"{source.identity.name}.json") else digests
            for path, digests in found.items()
        }, incomplete

    monkeypatch.setattr(
        next_module,
        "build_accepted_query_facts",
        lambda *args, **kwargs: recorded(real_facts(*args, **kwargs)),
    )
    monkeypatch.setattr(
        next_module._AcceptedQueryFactsRead,
        "build",
        lambda self, **kwargs: recorded(real_read(self, **kwargs)),
    )
    monkeypatch.setattr(next_module, "_bounded_claim_lineages", lineages)

    (row,) = _rows(_next(instance), "claim_dependency_stale", dependent.identity.qualified)
    assert row.related_identities == (source.identity.qualified,)
    assert "unsure_hold" not in flags()

    # An examined unsure attestation on the dependent parks the row in next, and
    # get shows the same hold rather than deciding from fewer row families.
    _attest(instance, owner, dependent, tmp_path, at=LATER - timedelta(minutes=1))
    parked = _next(instance)
    assert not _rows(parked, "claim_dependency_stale")
    assert parked.status.held >= 1
    assert "unsure_hold" in flags()
    # The hold is the dependent's; its upstream input is not held by it.
    card = _get(instance, source.identity.name, evaluation_time=LATER).card
    assert isinstance(card, PlaybillGetClaimCardV1) and "unsure_hold" not in card.flags


@pytest.mark.parametrize("missing", ("admission", "evaluation", "candidate"))
def test_a_partial_proposal_reads_as_incomplete_not_an_integrity_error(
    tmp_path: Path, missing: str
) -> None:
    from cruxible_client.contracts.proposal_models import ProposalWithdrawalRecordV1
    from tests.core_support._support import initialize_local
    from tests.test_proposals.test_grouped_proposal_notes import _submit

    instance, _ = initialize_local(tmp_path)
    partial = _submit(instance, "partial")
    proposal_id = partial.admission.proposal_id
    evidence = instance.proposal_evidence()
    if missing == "admission":
        evidence.write_withdrawal(
            ProposalWithdrawalRecordV1(
                proposal_id=proposal_id,
                actor_id="owner",
                reason="retain this row",
                withdrawn_at="2026-08-11T12:31:00.000000Z",
            )
        )
        evidence.index.locate(evidence, proposal_id)
        path = evidence.proposals / f"{proposal_id.removeprefix('sha256:')}.json"
    elif missing == "evaluation":
        path = evidence.root / evidence.index.locate(evidence, proposal_id)["evaluation_path"]
    else:
        path = (
            evidence.candidates
            / f"{partial.candidate.candidate_digest.removeprefix('sha256:')}.json"
        )
    path.unlink()

    for ref in (proposal_id, proposal_id[:20], f"Proposal:{proposal_id}"):
        result = _get(instance, ref)
        card = result.card
        assert result.kind == "proposal" and card is not None
        fields = card.model_dump()
        assert fields["status"] == "incomplete"
        assert fields["incomplete"] == (f"missing_{missing}",)
        assert fields["next"] == ()
    proof = _get(instance, proposal_id, detail="proof").proof
    assert proof is not None
    assert proof["status"]["incomplete_reasons"] == [f"missing_{missing}"]
    assert (proof.get("admission") is None) == (missing == "admission")


def test_an_empty_document_reads_as_an_empty_body(tmp_path: Path) -> None:
    from tests.core_support._support import initialize_local

    instance, owner = initialize_local(tmp_path)
    body = service_store_playbill_body(instance, content=b"").digest
    _accept(
        instance,
        owner,
        service_propose_playbill_document(
            instance,
            shell=_shell("empty", body, title="Empty"),
            actor_id="owner",
            proposal_name="get-empty",
            timestamp="2026-08-16T20:30:00.000000Z",
        ),
    )

    whole = _get(instance, "Document:empty", detail="body").body
    ranged = _get(
        instance, "Document:empty", detail="body", range=PlaybillByteRangeV1(start=0, end=10)
    ).body
    beyond = _refusal(
        instance, "Document:empty", detail="body", range=PlaybillByteRangeV1(start=5, end=10)
    )

    for read in (whole, ranged):
        assert read is not None and read.size == 0
        assert read.text == "" and read.range is None
    assert beyond.error_code == "playbill.get.range_out_of_bounds"


def test_a_proposal_is_read_only_at_the_current_head(world: dict[str, Any]) -> None:
    instance, proposal, before = world["instance"], world["pending"], world["before"]
    head = instance.accepted_coordinate()

    refused = _refusal(instance, proposal, at=before.git_oid)
    at_head = _get(instance, proposal, at=head.git_oid, detail="proof")

    assert refused.error_code == "playbill.get.historical_read_unsupported"
    assert refused.repair is not None and refused.repair.arguments == {
        "ref": f"Proposal:{proposal}"
    }
    assert at_head.coordinate.git_oid == head.git_oid[:12]
    assert at_head.accepted_coordinate is not None
    assert at_head.accepted_coordinate.git_oid == head.git_oid
    # One generation per response: the proof carries no other accepted coordinate.
    assert "accepted_coordinate" not in json.dumps(at_head.proof)


# -- card shape: long values, row claims, paged history, compact coordinate ----------


def test_a_summary_card_cuts_a_long_value_and_evidence_reads_it_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.get_reads import (
        GET_SUMMARY_TEXT_MAX_CHARS,
        PlaybillGetTruncatedTextV1,
    )

    instance, _owner = seed_claims(tmp_path)
    long_value = "note " * 300
    # The fixture ClaimType is an enum, so stand a long note in for its value.
    monkeypatch.setattr(get_module, "_artifact_value", lambda _claim: long_value)
    monkeypatch.setattr(get_module, "_claim_value", lambda _row: long_value)
    cut = PlaybillGetTruncatedTextV1(
        value=long_value[:GET_SUMMARY_TEXT_MAX_CHARS], length=len(long_value)
    )

    subject = _get(instance, _SUBJECT).card
    assert isinstance(subject, PlaybillGetSubjectCardV1)
    (row,) = subject.claims
    assert row.value == cut
    assert row.model_dump(mode="json")["value"] == {
        "value": long_value[:GET_SUMMARY_TEXT_MAX_CHARS],
        "truncated": True,
        "length": len(long_value),
    }
    assert isinstance(row.claim, str)
    assert subject.next[0] == f'cruxible_playbill_get(ref="{row.claim}", detail="evidence")'
    claim = _get(instance, row.claim).card
    assert isinstance(claim, PlaybillGetClaimCardV1) and claim.value == cut

    assert claim.next[0] == subject.next[0]
    history = _get(instance, row.claim, detail="history").history
    assert history is not None
    assert history.revisions[0].next[0].startswith(subject.next[0][:-1] + ", at=")

    evidence = _get(instance, row.claim, detail="evidence").evidence
    assert evidence is not None and evidence.value == long_value


def test_summary_value_cuts_only_long_strings() -> None:
    from cruxible_client.contracts.get_reads import (
        GET_SUMMARY_TEXT_MAX_CHARS,
        PlaybillGetTruncatedTextV1,
        summary_value,
    )

    edge = "x" * GET_SUMMARY_TEXT_MAX_CHARS
    assert summary_value(edge) == edge
    assert summary_value(["ready", 3, {"k": edge + "y"}]) == ["ready", 3, {"k": edge + "y"}]
    assert summary_value([edge + "y"]) == [
        PlaybillGetTruncatedTextV1(value=edge, length=GET_SUMMARY_TEXT_MAX_CHARS + 1)
    ]


def test_subject_rows_name_the_claim_behind_each_value(world: dict[str, Any]) -> None:
    card = _get(world["instance"], _SUBJECT).card

    assert isinstance(card, PlaybillGetSubjectCardV1)
    assert [(row.claim, row.value) for row in card.claims] == [(world["claim"], "ready")]


def test_history_pages_newest_first_with_a_bound_cursor(tmp_path: Path) -> None:
    from tests.test_claims.test_identity_evidence_rules import _v6_type, _World
    from tests.test_claims.test_superseded_contract_reads import _observe

    world = _World(tmp_path)
    world.seed(_v6_type())
    first = _observe(world, b"status: ready")
    _observe(world, b"status: done", revises=first)
    _observe(world, b"status: blocked", revises=first)
    instance = world.instance

    whole = _get(instance, first, detail="history")
    assert whole.history is not None and whole.truncated is False and whole.next_cursor is None
    sequences = [item.sequence for item in whole.history.revisions]
    assert len(sequences) == 3 and sequences == sorted(sequences, reverse=True)
    assert [item.revision for item in whole.history.revisions] == [3, 2, 1]

    pages: list[int] = []
    cursor: str | None = None
    while True:
        page = _get(
            instance, first, detail="history", limit=1, cursor=cursor, at=whole.coordinate.git_oid
        )
        assert page.history is not None and len(page.history.revisions) == 1
        pages.extend(item.sequence for item in page.history.revisions)
        if not page.truncated:
            assert page.next_cursor is None
            break
        cursor = page.next_cursor
        assert cursor is not None
    assert pages == sequences

    first_page = _get(instance, first, detail="history", limit=1)
    assert first_page.next_cursor is not None
    # A cursor continues only the listing it was cut from.
    with pytest.raises(PlaybillListCursorMismatch):
        _get(instance, "project.work_item/wi-42", detail="history", cursor=first_page.next_cursor)
    with pytest.raises(ValueError, match="page detail=.history. only"):
        PlaybillGetRequestV1(ref=first, limit=5)


def test_a_summary_names_its_coordinate_compactly_and_proof_in_full(
    world: dict[str, Any],
) -> None:
    instance = world["instance"]
    head = instance.accepted_coordinate()
    generation = len(instance.accepted_history()) - 1

    summary = _get(instance, _SUBJECT)
    assert summary.coordinate.model_dump() == {
        "git_oid": head.git_oid[:12],
        "generation": generation,
    }
    assert summary.accepted_coordinate is None
    dumped = summary.model_dump(mode="json")
    assert "accepted_coordinate" not in dumped
    assert "semantic_root" not in json.dumps(dumped)

    proof = _get(instance, _SUBJECT, detail="proof")
    assert proof.accepted_coordinate is not None
    assert proof.accepted_coordinate.git_oid == head.git_oid
    asked = _get(instance, _SUBJECT, full_coordinate=True)
    assert asked.accepted_coordinate == proof.accepted_coordinate


@pytest.mark.parametrize("long_value", ["x" * 300, "earlier note " * 100])
def test_history_evidence_suggestions_read_the_cut_revision_not_the_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, long_value: str
) -> None:
    import shlex

    from tests.test_claims.test_identity_evidence_rules import _v6_type, _World
    from tests.test_claims.test_superseded_contract_reads import _observe

    world = _World(tmp_path)
    world.seed(_v6_type())
    claim = _observe(world, b"status: ready")
    _observe(world, b"status: done", revises=claim)
    monkeypatch.setattr(
        get_module,
        "_artifact_value",
        lambda item: long_value if item.lifecycle.predecessor_digest is None else "done",
    )
    history = _get(world.instance, claim, detail="history", surface="cli").history
    assert history is not None
    current, old = history.revisions
    assert current.value == "done" and current.next == ()
    (step,) = old.next
    args = shlex.split(step)
    assert args[:6] == ["cruxible", "playbill", "get", claim, "--detail", "evidence"]
    assert args[6] == "--at"
    read = _get(world.instance, claim, detail="evidence", at=args[7]).evidence
    assert read is not None and read.value == long_value
    assert _get(world.instance, claim, detail="evidence").evidence.value == "done"


@pytest.mark.parametrize(
    "value", ["x" * 300, "line\n" * 30, {"note": "x" * 200}, ["x" * 70, "y" * 70]]
)
def test_cli_width_cuts_offer_evidence_without_changing_other_surfaces(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    monkeypatch.setattr(get_module, "_claim_value", lambda _row: value)
    monkeypatch.setattr(get_module, "_artifact_value", lambda _claim: value)
    instance = world["instance"]
    claim_id = world["claim"]
    for surface in ("cli", "mcp", "sdk"):
        subject = _get(instance, _SUBJECT, surface=surface).card
        history = _get(instance, claim_id, detail="history", surface=surface).history
        assert isinstance(subject, PlaybillGetSubjectCardV1)
        assert history is not None
        if surface == "cli":
            assert subject.next[0] == f"cruxible playbill get {claim_id} --detail evidence"
            assert history.revisions[0].next[0].startswith(subject.next[0] + " --at ")
        else:
            assert not any("evidence" in step for step in subject.next)
            assert history.revisions[0].next == ()

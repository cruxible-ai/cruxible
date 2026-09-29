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
from cruxible_core.service.discovery.get import (
    CaptureContractNames,
    service_playbill_get,
    verdict_flags,
)
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
    assert earlier.coordinate.git_oid == instance.accepted_coordinate().git_oid
    refused = _refusal(instance, "Document:design", at=before.git_oid)
    assert refused.error_code == "playbill.get.ref_not_found"
    at_before = _get(
        instance,
        _SUBJECT,
        at=AcceptedCoordinate.from_internal(before).model_dump(mode="json"),
    )
    assert at_before.coordinate.git_oid == before.git_oid
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
    assert history is not None and [item.revision for item in history.revisions] == [1, 2]
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


def test_an_unsure_examined_attestation_shows_as_an_unsure_hold(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.test_integration.test_next_closed_loop import EVALUATION_TIME as LATER
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _attest

    instance, owner = seed_claims(tmp_path)
    claim = _current_claim(instance)
    _attest(instance, owner, claim, tmp_path, at=LATER - timedelta(minutes=1))

    def flags(when: datetime) -> tuple[str, ...]:
        card = service_playbill_get(
            instance,
            request=PlaybillGetRequestV1(ref=claim.identity.name, evaluation_time=when),
            access=_ACCESS,
        ).card
        assert isinstance(card, PlaybillGetClaimCardV1)
        return card.flags

    assert "unsure_hold" in flags(LATER)
    # Before the attestation there is no hold; a standing hold lapses after its default.
    assert "unsure_hold" not in flags(LATER - timedelta(minutes=2))
    assert "unsure_hold" not in flags(LATER + timedelta(days=31))


def test_a_contested_slot_shows_every_live_value_with_the_contested_flag(tmp_path: Path) -> None:
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

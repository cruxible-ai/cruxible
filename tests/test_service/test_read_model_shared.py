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

from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    SUBJECT_KIND,
    seed_claims,
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

"""next's dependency fold keeps its retired-row adjudication refusal when it short-circuits.

The fold used to build retired query facts unconditionally, and assembling a
retired row reproduces that Claim's accepted adjudication rule. It now skips the
build when no Claim consumes another; the skip must not also skip the refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.claims.claim_type_migrations import ClaimTypeDependentDispositionV2
from cruxible_core.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.service.discovery import next as playbill_next
from cruxible_core.service.discovery import query as playbill_query
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from tests.test_claims.test_claim_type_migrations import (
    _accept_claim_type_only,
    _accepted_claim_world,
    _activate_migration,
    _decision_only_successor,
    _policy,
)

EVALUATION_TIME = datetime(2026, 8, 26, 12, tzinfo=UTC)
PROFILE = CoverageAccessProfileV1(
    profile_id="retired-adjudication",
    permitted_access_classes=("instance", "public"),
)
NOT_REPRODUCED = "adjudication rule does not reproduce"


def _retired_world(tmp_path: Path) -> Any:
    """One accepted Claim retired by a decision-only ClaimType migration."""

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    _activate_migration(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]),
        (
            ClaimTypeDependentDispositionV2(
                identity=ArtifactIdentity(kind="Claim", name=claim_id),
                disposition="retire",
            ),
        ),
    )
    return instance, owner


def _short_circuited_population(instance: Any) -> Any:
    coordinate = instance.accepted_coordinate()
    claims = ClaimVerdictReadContext(instance, coordinate).claims()
    assert any(claim.lifecycle.state == "retired" for claim in claims)
    # The fold's short-circuit precondition: nothing consumes another Claim.
    assert not any(claim.backing.input_claim_digests for claim in claims)
    return coordinate, claims


def _forbid_retired_build(monkeypatch: pytest.MonkeyPatch) -> None:
    original = playbill_query._AcceptedQueryFactsRead.build

    def build(self: Any, *, include_retired: bool = False) -> Any:
        if include_retired:
            pytest.fail("the short-circuit built retired query facts")
        return original(self, include_retired=include_retired)

    monkeypatch.setattr(playbill_query._AcceptedQueryFactsRead, "build", build)


def test_short_circuit_refuses_where_the_retired_build_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = _retired_world(tmp_path)
    # A verdict-bearing schema change the retired Claim's receipt cannot reproduce.
    _accept_claim_type_only(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "closed", "ready"]),
        proposal_name="schema-divergence",
    )
    coordinate, claims = _short_circuited_population(instance)

    with pytest.raises(ProposalIntegrityError, match=NOT_REPRODUCED):
        playbill_query.build_accepted_query_facts(
            instance, coordinate=coordinate, include_retired=True
        )

    _forbid_retired_build(monkeypatch)
    with pytest.raises(ProposalIntegrityError, match=NOT_REPRODUCED):
        playbill_next._claim_dependency_items(
            instance,
            coordinate=coordinate,
            evaluation_time=EVALUATION_TIME,
            access_profile=PROFILE,
            claims=claims,
        )
    # Full next and orient's bounded summary share the fold, so both refuse.
    request = playbill_next.PlaybillNextRequestV2(
        evaluation_time=EVALUATION_TIME, access_profile=PROFILE
    )
    with pytest.raises(ProposalIntegrityError, match=NOT_REPRODUCED):
        playbill_next.service_playbill_next(instance, request=request)
    with pytest.raises(ProposalIntegrityError, match=NOT_REPRODUCED):
        playbill_next.summarize_playbill_next(instance, request=request)
    # orient reports an unreadable queue rather than failing the whole answer.
    attention = service_playbill_orient(instance, evaluation_time=EVALUATION_TIME).attention
    assert attention is not None
    assert "the next queue could not be read: " in "\n".join(attention.notes)


def test_short_circuit_reproduces_every_retired_rule_without_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = _retired_world(tmp_path)
    # Queue-only policy changes leave the retired receipt reproducible.
    _accept_claim_type_only(
        instance,
        owner,
        _decision_only_successor(instance, enum=["blocked", "ready"]).model_copy(
            update={"attestation_consequence_policy": _policy(2)}
        ),
        proposal_name="policy-only",
    )
    coordinate, claims = _short_circuited_population(instance)
    retired = [claim for claim in claims if claim.lifecycle.state == "retired"]

    reproduced: list[str] = []
    original = playbill_query._reproduced_claim_adjudication_rule

    def spy(**kwargs: Any) -> Any:
        reproduced.append(kwargs["evidence_digest"])
        return original(**kwargs)

    monkeypatch.setattr(playbill_query, "_reproduced_claim_adjudication_rule", spy)
    _forbid_retired_build(monkeypatch)
    assert (
        playbill_next._claim_dependency_items(
            instance,
            coordinate=coordinate,
            evaluation_time=EVALUATION_TIME,
            access_profile=PROFILE,
            claims=claims,
        )
        == ()
    )
    # Not vacuous: the retired Claim's recorded rule was actually reproduced.
    assert len(reproduced) == len(retired) == 1

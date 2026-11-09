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
from cruxible_core.claims.claim_type_migrations import ClaimTypeDependentDisposition
from cruxible_core.coverage.contracts import CoverageAccessProfile
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
PROFILE = CoverageAccessProfile(
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
            ClaimTypeDependentDisposition(
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
    request = playbill_next.NextRequest(evaluation_time=EVALUATION_TIME, access_profile=PROFILE)
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


def test_a_retired_claim_short_circuits_unless_a_pair_end_lost_its_last_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retired Claims alone keep the short-circuit; a judgment on an unobserved end does not."""

    from cruxible_core.service.discovery.next import SUBJECT_END_UNOBSERVED
    from tests.core_support._pair_world import (
        APP_EXPOSURE,
        COMPONENT,
        INVENTORY,
        OCCURRENCE,
        VULN,
        accepted,
        create,
        pair_world,
        write,
    )

    def claim_of(outcome: Any, label_field: str) -> str:
        return next(
            item.claim
            for item in outcome.changes
            if item.op == "set" and item.field == label_field and item.claim is not None
        )

    instance = pair_world(tmp_path)
    judged = accepted(
        write(
            instance,
            create("c", COMPONENT, "pkg:maven/log4j-core@2.14.1"),
            create("i", INVENTORY, "web"),
            create("v", VULN, "CVE-2021-44832"),
            create("o", OCCURRENCE, {"inventory": {"created": "i"}, "component": {"created": "c"}}),
            {"op": "set", "subject": {"created": "o"}, "field": "scope", "value": "required"},
            create(
                "a",
                APP_EXPOSURE,
                {"occurrence": {"created": "o"}, "vulnerability": {"created": "v"}},
            ),
            {
                "op": "set",
                "subject": {"created": "a"},
                "field": "justification",
                "value": "vulnerable_code_not_in_execute_path",
            },
        )
    )
    # A second occurrence no judgment stands on loses its only observation
    # (the component's create lands on the live one holding its key).
    other = accepted(
        write(
            instance,
            create("c", COMPONENT, "pkg:maven/log4j-core@2.14.1"),
            create("i2", INVENTORY, "batch"),
            create(
                "o2",
                OCCURRENCE,
                {"inventory": {"created": "i2"}, "component": {"created": "c"}},
            ),
            {"op": "set", "subject": {"created": "o2"}, "field": "scope", "value": "optional"},
        )
    )
    accepted(
        write(
            instance,
            {"op": "retire", "target": claim_of(other, "scope"), "reason": "was-rescinded"},
        )
    )

    def dependency_items() -> tuple[Any, ...]:
        coordinate = instance.accepted_coordinate()
        claims = ClaimVerdictReadContext(instance, coordinate).claims()
        assert any(claim.lifecycle.state == "retired" for claim in claims)
        assert not any(claim.backing.input_claim_digests for claim in claims)
        return playbill_next._claim_dependency_items(
            instance,
            coordinate=coordinate,
            evaluation_time=EVALUATION_TIME,
            access_profile=PROFILE,
            claims=claims,
        )

    original = playbill_query._AcceptedQueryFactsRead.build
    retired_builds: list[bool] = []

    def build(self: Any, *, include_retired: bool = False) -> Any:
        retired_builds.append(include_retired)
        return original(self, include_retired=include_retired)

    monkeypatch.setattr(playbill_query._AcceptedQueryFactsRead, "build", build)
    # Every end of a judged pair is still observed: no retired build.
    assert dependency_items() == ()
    assert True not in retired_builds

    # The judged occurrence's scope is retired: its judgment stands on nothing current.
    accepted(
        write(
            instance,
            {"op": "retire", "target": claim_of(judged, "scope"), "reason": "was-rescinded"},
        )
    )
    (item,) = dependency_items()
    assert True in retired_builds
    assert item.reason == "claim_dependency_stale"
    judgment = claim_of(judged, "justification")
    assert item.subject_identity == f"Claim:{judgment.removeprefix('Claim:')}"
    (entry,) = item.detail["stale_inputs"]
    assert entry["impact_reasons"] == [SUBJECT_END_UNOBSERVED]
    assert entry["subject_end"].startswith(f"Subject:{OCCURRENCE}/")

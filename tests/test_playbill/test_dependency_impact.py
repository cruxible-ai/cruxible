"""PC-F dependency impact: what is standing on a Claim that moved.

The law under test is the one that makes this surface usable at all: an impact
rendering pairs the exact artifact each dependent *used* -- historical and
immutable -- with the *current* standing of the source at one explicit
evaluation time. A later backing successor must expose the downstream impacts
and the repair candidates without retroactively relabelling a single recorded
dependency coordinate.

The rest is ordinary Playbill discipline: one accepted coordinate, one absolute
instant, deterministic order, stated truncation, and a read that writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_digest
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifact,
    ClaimBacking,
    ClaimReferentContext,
    ClaimStatement,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
)
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpecV1,
    LineSpecV1,
    line_spec_digest,
)
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinitionV1,
    QueryDefinitionV1,
    query_definition_digest,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.backends import ClaimFactRowV1, ClaimQueryFactsV1
from cruxible_core.playbill.query.impact import (
    SOURCE_SUPERSEDED,
    SOURCE_UNCOVERED,
    DependencyImpactBudgetV1,
    DependencyImpactError,
    DependencyImpactRequestV1,
    DependencyImpactV1,
    build_dependency_impact,
)
from tests.test_playbill._line_runtime_support import (
    accepted_line,
    accepted_procedure,
)
from tests.test_playbill._line_runtime_support import (
    acquisition_policy as line_acquisition_policy,
)
from tests.test_playbill.test_claim_query_engine import (
    NOW,
    coordinate,
    rule_for,
    subject,
)
from tests.test_playbill.test_query_definitions import (
    OWNER_AUTHORITY,
    STATUS_PREDICATE,
    accepted_query,
    active_work_query,
    claim_type,
)

SOURCE_INDEX = 1
DERIVED_INDEX = 2
UNRELATED_INDEX = 3
SOURCE_PATH = claim_path(f"CLM-{SOURCE_INDEX:032x}")
DERIVED_PATH = claim_path(f"CLM-{DERIVED_INDEX:032x}")
REDUCER_DIGEST = claim_type_digest(claim_type(STATUS_PREDICATE)).tagged
PIN_ROLE = "input-claim"


# -- fixtures -------------------------------------------------------------


def _claim(
    index: int,
    *,
    item: str,
    value: str,
    lifecycle: ArtifactLifecycle = ArtifactLifecycle(),
    input_digests: tuple[str, ...] = (),
    supported: bool = True,
) -> ClaimFactRowV1:
    """One accepted Claim with explicit lifecycle and derivation backing.

    ``supported=False`` withholds the resolved authority basis, which is the
    cheapest honest way to make a source stop being supported without inventing
    Capture evidence the fixture does not need.
    """

    contract = claim_type(STATUS_PREDICATE)
    digest = claim_type_digest(contract).tagged
    claim_id = f"CLM-{index:032x}"
    subject_row = subject("project.work_item", item)
    artifact = ClaimArtifact(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_row.path),
            claim_type=contract.identity,
            claim_type_digest=digest,
            predicate=STATUS_PREDICATE,
            object=LiteralClaimObject(value=value),
            role="derivation" if input_digests else "normative",
        ),
        backing=ClaimBacking(
            referent_context=ClaimReferentContext(
                subject_content_digest=subject_row.artifact_digest,
                observed_at=NOW,
            ),
            input_claim_digests=input_digests,
            reducer_digest=REDUCER_DIGEST if input_digests else None,
        ),
        authority=OWNER_AUTHORITY,
        pins=(ArtifactPin(role="claim-type", target=contract.identity, artifact_digest=digest),),
        lifecycle=lifecycle,
    )
    return ClaimFactRowV1(
        accepted=AcceptedClaim(
            path=claim_path(claim_id),
            claim=artifact,
            statement_digest=claim_statement_digest(artifact.statement).tagged,
            artifact_digest=claim_artifact_digest(artifact).tagged,
        ),
        rule=rule_for(STATUS_PREDICATE),
        resolved_authority_basis=("authority:owner",) if supported else (),
    )


def _source_v1() -> ClaimFactRowV1:
    return _claim(SOURCE_INDEX, item="wi-1", value="ready")


def _source_v2() -> ClaimFactRowV1:
    """The later backing successor: same path, new digest, no longer supported."""

    return _claim(
        SOURCE_INDEX,
        item="wi-1",
        value="blocked",
        lifecycle=ArtifactLifecycle(predecessor_digest=_source_v1().accepted.artifact_digest),
        supported=False,
    )


def _derived() -> ClaimFactRowV1:
    return _claim(
        DERIVED_INDEX,
        item="wi-2",
        value="derived-from-wi-1",
        input_digests=(_source_v1().accepted.artifact_digest,),
    )


def _claim_pin(row: ClaimFactRowV1) -> ArtifactPin:
    return ArtifactPin(
        role=PIN_ROLE,
        target=row.accepted.claim.identity,
        artifact_digest=row.accepted.artifact_digest,
    )


def _pinning_query(row: ClaimFactRowV1) -> AcceptedQueryDefinitionV1:
    base = active_work_query()
    pins = tuple(
        sorted(
            (*base.pins, _claim_pin(row)),
            key=lambda item: (item.role.encode("utf-8"), item.target.qualified.encode("utf-8")),
        )
    )
    payload = base.model_dump(mode="json")
    payload["pins"] = [item.model_dump(mode="json") for item in pins]
    query = QueryDefinitionV1.model_validate(payload)
    return AcceptedQueryDefinitionV1(
        path=accepted_query(base).path,
        query=query,
        artifact_digest=query_definition_digest(query).tagged,
    )


def _pinning_procedure(row: ClaimFactRowV1) -> AcceptedProcedureV1:
    return accepted_procedure(extra_pins=(_claim_pin(row),))


def _pinning_line(procedure: AcceptedProcedureV1, row: ClaimFactRowV1) -> AcceptedLineSpecV1:
    base = accepted_line(procedure, line_acquisition_policy())
    pins = tuple(
        sorted(
            (*base.line.pins, _claim_pin(row)),
            key=lambda item: (item.role.encode("utf-8"), item.target.qualified.encode("utf-8")),
        )
    )
    payload = base.line.model_dump(mode="json")
    payload["pins"] = [item.model_dump(mode="json") for item in pins]
    line = LineSpecV1.model_validate(payload)
    return AcceptedLineSpecV1(
        path=base.path,
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


def _facts(claims: tuple[ClaimFactRowV1, ...], *, generation: str = "22") -> ClaimQueryFactsV1:
    subjects = tuple(
        sorted(
            (subject("project.work_item", item) for item in ("wi-1", "wi-2", "wi-3")),
            key=lambda item: item.path.encode("utf-8"),
        )
    )
    return ClaimQueryFactsV1(
        coordinate=coordinate(generation=generation),
        subjects=subjects,
        claims=tuple(sorted(claims, key=lambda item: item.accepted.path.encode("utf-8"))),
    )


def _request(facts: ClaimQueryFactsV1, **overrides: object) -> DependencyImpactRequestV1:
    fields: dict[str, object] = {
        "at": AcceptedCoordinate.from_internal(facts.coordinate),
        "address": SemanticAddress.claim_statement(SOURCE_PATH),
        "evaluation_time": NOW,
    }
    fields.update(overrides)
    return DependencyImpactRequestV1(**fields)  # type: ignore[arg-type]


def _impact(facts: ClaimQueryFactsV1, source: ClaimFactRowV1, **overrides: object):
    procedure = _pinning_procedure(source)
    return build_dependency_impact(
        _request(facts, **overrides),
        facts=facts,
        definitions=(_pinning_query(source),),
        procedures=(procedure,),
        line_specs=(_pinning_line(procedure, source),),
    )


def _historical(result: DependencyImpactV1) -> list[tuple[str, str, str, str | None, str]]:
    """The recorded dependency coordinates, which no later read may rewrite."""

    return [
        (
            item.kind,
            item.address.artifact_path,
            item.dependency_kind,
            item.used_pin_role,
            item.used_artifact_digest,
        )
        for item in result.dependents
    ]


# -- the walk --------------------------------------------------------------


def test_the_walk_reaches_every_dependent_family_through_its_own_edge_kind() -> None:
    v1 = _source_v1()
    facts = _facts((v1, _derived(), _claim(UNRELATED_INDEX, item="wi-3", value="unrelated")))
    result = _impact(facts, v1)

    assert [item.kind for item in result.dependents] == [
        "Claim",
        "LineSpec",
        "Procedure",
        "QueryDefinition",
    ]
    claim_dependent = result.dependents[0]
    assert claim_dependent.address.artifact_path == DERIVED_PATH
    assert claim_dependent.dependency_kind == "backing_input"
    assert claim_dependent.used_pin_role is None
    assert all(item.dependency_kind == "pin" for item in result.dependents[1:])
    assert {item.used_pin_role for item in result.dependents[1:]} == {PIN_ROLE}
    # A Claim that neither derives from the source nor pins it is not a
    # dependent; an impact surface that returned it would be noise.
    assert all(
        item.address.artifact_path != claim_path(f"CLM-{UNRELATED_INDEX:032x}")
        for item in result.dependents
    )
    assert result.sources[0].claim_path == SOURCE_PATH
    assert result.sources[0].verdict == "supported"


def test_a_supported_unmoved_source_produces_no_repair_candidate() -> None:
    v1 = _source_v1()
    result = _impact(_facts((v1, _derived())), v1)

    assert all(item.stale is False for item in result.dependents)
    assert all(item.impact_reasons == () for item in result.dependents)
    assert result.repair_candidates == ()


def test_only_a_claim_dependent_carries_an_evidence_relative_verdict() -> None:
    v1 = _source_v1()
    result = _impact(_facts((v1, _derived())), v1)
    claim_dependent = result.dependents[0]

    assert (claim_dependent.dependent_verdict, claim_dependent.dependent_currency) == (
        "supported",
        "current",
    )
    assert all(
        item.dependent_verdict is None and item.dependent_currency is None
        for item in result.dependents
        if item.kind != "Claim"
    )


def test_a_source_can_be_named_by_statement_digest_instead_of_address() -> None:
    v1 = _source_v1()
    facts = _facts((v1, _derived()))
    by_address = _impact(facts, v1)
    by_statement = _impact(
        facts,
        v1,
        address=None,
        statement_digest=v1.accepted.statement_digest,
    )

    assert canonical_bytes(by_address.model_dump(mode="json")) == canonical_bytes(
        by_statement.model_dump(mode="json")
    )


# -- the successor scenario ------------------------------------------------


def test_a_later_successor_exposes_impacts_without_relabelling_what_was_used() -> None:
    v1 = _source_v1()
    v2 = _source_v2()
    before = _impact(_facts((v1, _derived())), v1)
    after = _impact(_facts((v2, _derived()), generation="33"), v1)

    # The historical half of every dependent is byte-for-byte what it was: the
    # generation each dependent recorded did not move because the source did.
    assert _historical(after) == _historical(before)
    assert all(
        item.used_artifact_digest == v1.accepted.artifact_digest for item in after.dependents
    )

    # The current half moved, and says exactly how.
    assert after.sources[0].accepted_artifact_digest == v2.accepted.artifact_digest
    assert after.sources[0].predecessor_digest == v1.accepted.artifact_digest
    assert after.sources[0].verdict == "uncovered"
    assert all(
        item.current_artifact_digest == v2.accepted.artifact_digest for item in after.dependents
    )
    assert all(item.stale is True for item in after.dependents)
    expected = tuple(sorted((SOURCE_SUPERSEDED, SOURCE_UNCOVERED)))
    assert all(item.impact_reasons == expected for item in after.dependents)
    assert after.repair_candidates == after.dependents


def test_the_successor_lineage_the_walk_searched_is_stated_not_implied() -> None:
    v1 = _source_v1()
    v2 = _source_v2()
    after = _impact(_facts((v2, _derived()), generation="33"), v1)

    # Backing edges name inputs by digest alone, so the source states the exact
    # digest set the walk matched rather than implying a lineage it cannot read.
    assert after.sources[0].searched_artifact_digests == tuple(
        sorted(
            (v1.accepted.artifact_digest, v2.accepted.artifact_digest),
            key=lambda item: item.encode("utf-8"),
        )
    )


def test_explicit_bounded_lineage_reaches_a_two_successor_old_backing_input() -> None:
    v1 = _source_v1()
    v2 = _source_v2()
    v3 = _claim(
        SOURCE_INDEX,
        item="wi-1",
        value="done",
        lifecycle=ArtifactLifecycle(predecessor_digest=v2.accepted.artifact_digest),
        supported=False,
    )
    facts = _facts((v3, _derived()), generation="44")

    result = build_dependency_impact(
        _request(facts),
        facts=facts,
        source_lineages={
            SOURCE_PATH: (
                v1.accepted.artifact_digest,
                v2.accepted.artifact_digest,
                v3.accepted.artifact_digest,
            )
        },
    )

    (dependent,) = result.dependents
    assert dependent.identity == _derived().accepted.claim.identity.qualified
    assert dependent.used_artifact_digest == v1.accepted.artifact_digest
    assert dependent.current_artifact_digest == v3.accepted.artifact_digest
    assert dependent.stale is True


def test_explicit_retired_source_scope_keeps_only_live_claim_dependents() -> None:
    v1 = _source_v1()
    retired = _claim(
        SOURCE_INDEX,
        item="wi-1",
        value="ready",
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=v1.accepted.artifact_digest,
        ),
        supported=False,
    )
    retired_dependent = _claim(
        UNRELATED_INDEX,
        item="wi-3",
        value="old derivative",
        lifecycle=ArtifactLifecycle(state="retired"),
        input_digests=(v1.accepted.artifact_digest,),
    )
    facts = _facts((retired, _derived(), retired_dependent), generation="55")

    result = build_dependency_impact(
        _request(facts),
        facts=facts,
        source_lineages={
            SOURCE_PATH: (
                v1.accepted.artifact_digest,
                retired.accepted.artifact_digest,
            )
        },
        include_retired_sources=True,
    )

    assert result.sources[0].lifecycle_state == "retired"
    assert [item.identity for item in result.dependents] == [
        _derived().accepted.claim.identity.qualified
    ]


def test_a_verdict_change_alone_makes_dependents_repair_candidates() -> None:
    unsupported = _claim(SOURCE_INDEX, item="wi-1", value="ready", supported=False)
    result = _impact(_facts((unsupported, _derived())), unsupported)

    # No successor: the artifact is unchanged, so nothing is superseded. The
    # source simply no longer stands, and that alone is an impact.
    assert result.sources[0].verdict == "uncovered"
    assert all(item.stale is False for item in result.dependents)
    assert all(item.impact_reasons == (SOURCE_UNCOVERED,) for item in result.dependents)
    assert result.repair_candidates == result.dependents


# -- determinism, budgets, and refusals -----------------------------------


def test_the_same_inputs_yield_a_byte_identical_impact_and_write_nothing() -> None:
    v1 = _source_v1()
    facts = _facts((v1, _derived()))
    before = canonical_bytes(facts.model_dump(mode="json"))

    first = _impact(facts, v1)
    second = _impact(_facts((_source_v1(), _derived())), _source_v1())

    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )
    assert first.receipt_digest == second.receipt_digest
    assert canonical_bytes(facts.model_dump(mode="json")) == before


def test_a_clipped_impact_states_what_it_dropped() -> None:
    v1 = _source_v1()
    facts = _facts((v1, _derived()))
    result = _impact(facts, v1, budget=DependencyImpactBudgetV1(max_dependents=2))

    assert len(result.dependents) == 2
    assert result.candidate_dependent_count == 4
    assert result.coverage.truncated_facets == ("Procedure", "QueryDefinition")
    assert result.coverage.reason_codes == ("dependent_budget_exceeded",)
    assert result.coverage.available_facets == ("Claim", "LineSpec")


def test_a_byte_clipped_impact_states_that_too() -> None:
    v1 = _source_v1()
    facts = _facts((v1, _derived()))
    result = _impact(facts, v1, budget=DependencyImpactBudgetV1(max_bytes=1_200))

    assert result.candidate_dependent_count > len(result.dependents)
    assert "byte_budget_exceeded" in result.coverage.reason_codes


def test_impact_refuses_an_unreadable_target_coordinate_or_instant() -> None:
    v1 = _source_v1()
    facts = _facts((v1, _derived()))

    with pytest.raises(DependencyImpactError, match="not live"):
        build_dependency_impact(
            _request(facts, address=SemanticAddress.claim_statement(claim_path("CLM-" + "f" * 32))),
            facts=facts,
        )
    with pytest.raises(DependencyImpactError, match="statement digest"):
        build_dependency_impact(
            _request(
                facts,
                address=None,
                statement_digest="sha256:" + "0" * 64,
            ),
            facts=facts,
        )
    with pytest.raises(DependencyImpactError, match="one accepted coordinate"):
        build_dependency_impact(
            _request(_facts((v1,), generation="44")),
            facts=facts,
        )
    with pytest.raises(ValueError, match="absolute instant"):
        _request(facts, evaluation_time=datetime(2026, 8, 16, 12, 0))
    with pytest.raises(ValueError, match="exactly one address or statement digest"):
        _request(facts, address=None)
    with pytest.raises(ValueError, match="Claim artifact or statement"):
        _request(
            facts, address=SemanticAddress.whole_artifact(subject_path("project.work_item", "wi-1"))
        )


def test_impact_evaluation_time_is_explicit_and_carried_into_the_result() -> None:
    v1 = _source_v1()
    facts = _facts((v1, _derived()))
    later = datetime(2027, 1, 1, tzinfo=UTC)

    assert _impact(facts, v1).evaluated_at == NOW
    assert _impact(facts, v1, evaluation_time=later).evaluated_at == later

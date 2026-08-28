"""Daemon-bound corroboration queries in Claim admission."""

from __future__ import annotations

from cruxible_client.contracts.claim_types import (
    AcceptedClaimType,
    claim_type_digest,
    claim_type_path,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    CorroborationRequirementV1,
)
from cruxible_client.contracts.query.definitions import query_definition_digest
from cruxible_client.contracts.query.grammar import (
    QueryEntryV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
)
from cruxible_core.playbill.proposals import (
    _corroboration_parameters,
    _run_corroboration_requirements,
)
from tests.test_playbill.test_claim_query_engine import (
    NOW,
    coordinate,
    facts,
    status_claim,
    subject,
)
from tests.test_playbill.test_query_definitions import (
    STATUS_PREDICATE,
    accepted_query,
    claim_type,
    single_status_query,
)


def _accepted_type() -> AcceptedClaimType:
    contract = claim_type(STATUS_PREDICATE)
    return AcceptedClaimType(
        path=claim_type_path(contract.predicate),
        claim_type=contract,
        artifact_digest=claim_type_digest(contract).tagged,
    )


def _bound_query(*, value_type: str = "string"):  # type: ignore[no-untyped-def]
    query = single_status_query(
        entry=QueryEntryV1(
            binding="item",
            subject_kinds=("project.work_item",),
            subject_id=QueryParameterRefV1(parameter="claim_subject_id"),
        ),
        parameters=(
            QueryParameterDeclarationV1(
                name="claim_subject_id",
                value_type=value_type,  # type: ignore[arg-type]
            ),
        ),
    )
    return accepted_query(query)


def _policy(query_digest: str, *, min_count: int = 1) -> ClaimAdmissionPolicyV1:
    return ClaimAdmissionPolicyV1(
        corroboration_requirements=(
            CorroborationRequirementV1(
                requirement_id="status-present",
                query_definition_digest=query_digest,
                min_count=min_count,
            ),
        )
    )


def _run(*, min_count: int = 1):  # type: ignore[no-untyped-def]
    definition = _bound_query()
    accepted_type = _accepted_type()
    return _run_corroboration_requirements(
        policy=_policy(definition.artifact_digest, min_count=min_count),
        accepted_type=accepted_type,
        subject=subject("project.work_item", "wi-1"),
        definitions={definition.artifact_digest: definition},
        facts=facts((status_claim(1, "wi-1", "ready"),)),
        current=coordinate(),
        timestamp=NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )


def test_corroboration_query_is_daemon_bound_and_byte_deterministic() -> None:
    first = _run()
    second = _run()

    assert first == second
    results, issues = first
    assert issues == ()
    assert len(results) == 1
    result = results[0]
    assert result.query_verdict == "completed"
    assert result.observed_count == 1
    assert result.satisfied is True
    assert result.parameter_digest.startswith("sha256:")
    assert result.result_digest.startswith("sha256:")


def test_corroboration_insufficient_commits_the_observed_result() -> None:
    results, issues = _run(min_count=2)

    assert results[0].observed_count == 1
    assert results[0].satisfied is False
    assert [code for code, _message in issues] == [
        "playbill.claim.corroboration_insufficient"
    ]


def test_corroboration_unresolved_digest_is_a_typed_issue() -> None:
    definition = _bound_query()
    results, issues = _run_corroboration_requirements(
        policy=_policy(definition.artifact_digest),
        accepted_type=_accepted_type(),
        subject=subject("project.work_item", "wi-1"),
        definitions={},
        facts=facts((status_claim(1, "wi-1", "ready"),)),
        current=coordinate(),
        timestamp=NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )

    assert results == ()
    assert [code for code, _message in issues] == [
        "playbill.claim.corroboration_query_unresolved"
    ]


def test_reserved_parameter_type_mismatch_is_detected_before_query_execution() -> None:
    definition = _bound_query(value_type="integer")
    parameters, invalid = _corroboration_parameters(
        definition,
        subject=subject("project.work_item", "wi-1"),
        predicate=STATUS_PREDICATE,
    )

    assert parameters is None
    assert invalid == ("claim_subject_id", "string", "integer")


def test_nonreserved_required_parameter_uses_the_query_refusal() -> None:
    definition = accepted_query(single_status_query())
    accepted_type = _accepted_type()
    results, issues = _run_corroboration_requirements(
        policy=_policy(query_definition_digest(definition.query).tagged),
        accepted_type=accepted_type,
        subject=subject("project.work_item", "wi-1"),
        definitions={definition.artifact_digest: definition},
        facts=facts((status_claim(1, "wi-1", "ready"),)),
        current=coordinate(),
        timestamp=NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )

    assert results[0].query_verdict == "refused"
    assert results[0].query_refusal_code == "playbill.query.parameter_missing"
    assert [code for code, _message in issues] == [
        "playbill.claim.corroboration_query_refused"
    ]

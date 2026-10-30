from __future__ import annotations

from datetime import timedelta

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationStatementV1,
    VerifiedClaimAttestationV1,
)
from cruxible_client.contracts.claim_types import claim_type_digest
from cruxible_client.contracts.claim_verdicts import (
    CaptureVerdictEvidence,
    ClaimAdjudicationRule,
    ClaimVerdictResult,
    claim_adjudication_rule,
    claim_adjudication_rule_digest,
    evaluate_claim_verdict,
    evidence_control_components,
    observation_trust_grade,
    verify_claim_verdict_freshness,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.indexes.projection import AcceptedCoordinate
from tests.core_support._pc_c_support import NOW, artifact_digest, digest
from tests.test_claims.test_claims import _claim_type

STATEMENT_DIGEST = digest("claim-statement", "one")


def _coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="11" * 32,
        semantic_root=digest("semantic", "one"),
        generation_root=digest("generation", "one"),
        compiler_digest=digest("compiler", "one"),
    )


def _rule(**updates: object) -> ClaimAdjudicationRule:
    claim_type = _claim_type()
    rule = claim_adjudication_rule(
        claim_type,
        claim_type_digest=claim_type_digest(claim_type).tagged,
    )
    return rule.model_copy(update=updates)


def _capture(
    value: str,
    *,
    admission: str = "direct",
    control_domain: str = "provider-a",
    upstream: tuple[ArtifactIdentity, ...] = (),
    observed_offset: int = 0,
    replay: bool = True,
) -> CaptureVerdictEvidence:
    return CaptureVerdictEvidence(
        capture_digest=digest("capture", value),
        admission=admission,  # type: ignore[arg-type]
        basis_kind="replay_verified" if replay else "origin_only",
        producer=ArtifactIdentity(kind="Provider", name=control_domain),
        control_domain=control_domain,
        upstream_provenance=upstream,
        epistemic_grade="observed",
        provenance_grade="provider-signed",
        observed_at=NOW + timedelta(seconds=observed_offset),
        current_replay_available=replay,
    )


def _attestation(
    value: str,
    *,
    stance: str,
    control_domain: str,
    upstream: tuple[ArtifactIdentity, ...] = (),
    current: bool = True,
) -> VerifiedClaimAttestationV1:
    statement = ClaimAttestationStatementV1(
        instance_id="inst-verdict",
        referent_coordinate=_coordinate(),
        subject=SemanticAddress.whole_artifact("subjects/project.work_item/wi-42.json"),
        subject_content_digest=artifact_digest("subject", "wi-42"),
        claim_statement_digest=STATEMENT_DIGEST,
        stance=stance,  # type: ignore[arg-type]
        provider_or_principal=ArtifactIdentity(kind="Provider", name=control_domain),
        signing_key_id="key-1",
        capture_digests=(digest("capture", f"attestation-{value}"),),
        observed_at=NOW,
    )
    return VerifiedClaimAttestationV1(
        attestation_digest=digest("attestation", value),
        statement=statement,
        attestation_grade="verified_provider",
        control_domain=control_domain,
        upstream_provenance=upstream,
        coverage="exact_subject" if current else "shell_stale",
        current=current,
    )


def _attestation_capture(value: str, *, control_domain: str) -> CaptureVerdictEvidence:
    return _capture(f"attestation-{value}", control_domain=control_domain)


def test_knowledge_compounds_uncovered_supported_then_contradicted() -> None:
    origin = _capture("origin", admission="origin_only")
    uncovered = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(),
        evaluation_time=NOW,
        captures=(origin,),
        attestations=(),
        providers={},
    )
    assert uncovered.verdict == "uncovered"
    support = _attestation("support", stance="support", control_domain="lab-a")
    support_capture = _attestation_capture("support", control_domain="lab-a")
    supported = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(),
        evaluation_time=NOW,
        captures=(origin, support_capture),
        attestations=(support,),
        providers={},
    )
    assert supported.verdict == "supported"
    contradict = _attestation(
        "contradict",
        stance="contradict",
        control_domain="lab-b",
    )
    contradict_capture = _attestation_capture("contradict", control_domain="lab-b")
    contradicted = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(conflict_behavior="contradiction_precedence"),
        evaluation_time=NOW,
        captures=(origin, support_capture, contradict_capture),
        attestations=(support, contradict),
        providers={},
    )
    assert contradicted.verdict == "contradicted"
    assert support.attestation_digest in contradicted.supporting_evidence_digests
    assert contradict.attestation_digest in contradicted.contradicting_evidence_digests


def test_ambiguity_is_unresolved_and_unsure_is_not_negative_evidence() -> None:
    support = _attestation("support", stance="support", control_domain="lab-a")
    contradict = _attestation("contradict", stance="contradict", control_domain="lab-b")
    support_capture = _attestation_capture("support", control_domain="lab-a")
    contradict_capture = _attestation_capture("contradict", control_domain="lab-b")
    mixed = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(conflict_behavior="unresolved"),
        evaluation_time=NOW,
        captures=(support_capture, contradict_capture),
        attestations=(support, contradict),
        providers={},
    )
    assert mixed.verdict == "unresolved"
    unsure = _attestation("unsure", stance="unsure", control_domain="lab-c")
    no_claim = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(),
        evaluation_time=NOW,
        captures=(),
        attestations=(unsure,),
        providers={},
    )
    assert no_claim.verdict == "uncovered"
    assert no_claim.unsure_evidence_digests == (unsure.attestation_digest,)


def test_attestation_cannot_use_an_unrelated_capture_as_support() -> None:
    support = _attestation("support", stance="support", control_domain="lab-a")
    unrelated = _capture("unrelated", admission="origin_only", control_domain="lab-a")
    verdict = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(),
        evaluation_time=NOW,
        captures=(unrelated,),
        attestations=(support,),
        providers={},
    )
    assert verdict.verdict == "uncovered"
    assert verdict.basis_kinds == ()


def test_shared_upstream_does_not_manufacture_independent_evidence() -> None:
    upstream = (ArtifactIdentity(kind="Provider", name="shared-dataset"),)
    first = _capture("one", control_domain="reseller-a", upstream=upstream)
    second = _capture("two", control_domain="reseller-b", upstream=upstream)
    components = evidence_control_components((first, second), (), providers={})
    assert len(components) == 1
    threshold = _rule(minimum_supporting_control_domains=2)
    correlated = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=threshold,
        evaluation_time=NOW,
        captures=(first, second),
        attestations=(),
        providers={},
    )
    assert correlated.verdict == "uncovered"
    independent = _capture("three", control_domain="independent-lab")
    supported = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=threshold,
        evaluation_time=NOW,
        captures=(first, independent),
        attestations=(),
        providers={},
    )
    assert supported.verdict == "supported"


def test_duplicate_evidence_digest_refuses_before_independence_counting() -> None:
    first = _capture("duplicate", control_domain="lab-a")
    relabeled = first.model_copy(update={"control_domain": "lab-b"})
    with pytest.raises(ValueError, match="globally unique"):
        evidence_control_components((first, relabeled), (), providers={})
    with pytest.raises(ValueError, match="globally unique"):
        evaluate_claim_verdict(
            claim_statement_digest=STATEMENT_DIGEST,
            rule=_rule(minimum_supporting_control_domains=2),
            evaluation_time=NOW,
            captures=(first, relabeled),
            attestations=(),
            providers={},
        )


def test_shell_drift_gates_capture_support_only_for_shell_sensitive_claims() -> None:
    evidence = _capture("shell-bound")
    stale = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(shell_sensitive=True),
        evaluation_time=NOW,
        captures=(evidence,),
        attestations=(),
        providers={},
        referent_current=False,
    )
    identity_sensitive = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(shell_sensitive=False),
        evaluation_time=NOW,
        captures=(evidence,),
        attestations=(),
        providers={},
        referent_current=False,
    )
    assert stale.verdict == "stale"
    assert stale.currency == "stale"
    assert identity_sensitive.verdict == "supported"


def test_evaluation_time_reproduces_currency_without_rewriting_evidence() -> None:
    evidence = _capture("time-bound")
    rule = _rule(max_evidence_age=CanonicalDuration(microseconds=5_000_000))
    current = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=rule,
        evaluation_time=NOW + timedelta(seconds=4),
        captures=(evidence,),
        attestations=(),
        providers={},
    )
    stale = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=rule,
        evaluation_time=NOW + timedelta(seconds=6),
        captures=(evidence,),
        attestations=(),
        providers={},
    )
    assert current.verdict == "supported"
    assert stale.verdict == "stale_evidence"
    assert isinstance(current, ClaimVerdictResult)
    assert isinstance(stale, ClaimVerdictResult)
    assert stale.freshness_expirations[0].expires_at == NOW + timedelta(seconds=5)
    assert claim_adjudication_rule_digest(rule) == current.adjudication_rule_digest
    assert current.supporting_evidence_digests == stale.supporting_evidence_digests

    forged = stale.model_copy(
        update={
            "freshness_expirations": (
                stale.freshness_expirations[0].model_copy(
                    update={"expires_at": NOW + timedelta(seconds=7)}
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="cruxible.claim.evidence_freshness_invalid"):
        verify_claim_verdict_freshness(forged, rule=rule, captures=(evidence,))
    # One result for horizon-free and horizon-bearing Claims: without a horizon
    # the committed vector is empty, and a vector without a horizon is refused.
    horizon_free = _rule(max_evidence_age=None)
    plain = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=horizon_free,
        evaluation_time=NOW + timedelta(seconds=6),
        captures=(evidence,),
        attestations=(),
        providers={},
    )
    assert isinstance(plain, ClaimVerdictResult)
    assert plain.tag == stale.tag
    assert plain.freshness_expirations == ()
    with pytest.raises(ValueError, match="commits no expirations"):
        verify_claim_verdict_freshness(stale, rule=horizon_free, captures=(evidence,))


def test_freshness_horizon_applies_only_to_admitted_capture_evidence() -> None:
    origin = _capture("origin-freshness", admission="origin_only")
    rule = _rule(max_evidence_age=CanonicalDuration(microseconds=5_000_000))
    result = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=rule,
        evaluation_time=NOW + timedelta(seconds=6),
        captures=(origin,),
        attestations=(),
        providers={},
    )

    assert isinstance(result, ClaimVerdictResult)
    assert result.verdict == "uncovered"
    assert result.freshness_expirations == ()


def test_future_evidence_is_uncovered_and_pre_effective_claim_is_not_applicable() -> None:
    future = _capture("future", observed_offset=5)
    verdict = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(),
        evaluation_time=NOW,
        captures=(future,),
        attestations=(),
        providers={},
    )
    not_effective = evaluate_claim_verdict(
        claim_statement_digest=STATEMENT_DIGEST,
        rule=_rule(),
        evaluation_time=NOW,
        captures=(),
        attestations=(),
        providers={},
        claim_effective_from=NOW + timedelta(seconds=5),
    )
    assert verdict.verdict == "uncovered"
    assert verdict.currency == "current"
    assert not_effective.verdict == "uncovered"
    assert not_effective.currency == "not_applicable"


def test_observation_trust_presentation_is_a_separate_fixed_axis() -> None:
    assert observation_trust_grade("self-asserted") == "proposer_observed"
    assert observation_trust_grade("daemon-fetched") == "daemon_fetched"
    assert observation_trust_grade("provider-signed") == "provider_receipted"
    assert observation_trust_grade("witnessed") == "witnessed"


def test_the_replay_exemption_spares_a_none_types_origin_only_evidence() -> None:
    claim_type = _claim_type()
    none_type = claim_type.model_copy(update={"evidence_requirement": "none"})
    type_digest = claim_type_digest(claim_type).tagged
    supporting = claim_adjudication_rule(none_type, claim_type_digest=type_digest).model_copy(
        update={"require_current_replay": True}
    )
    exempt = claim_adjudication_rule(
        none_type, claim_type_digest=type_digest, origin_replay_exempt=True
    ).model_copy(update={"require_current_replay": True})
    assert supporting.origin_supports and not supporting.origin_replay_exempt
    assert exempt.origin_supports and exempt.origin_replay_exempt
    assert claim_adjudication_rule_digest(supporting) != claim_adjudication_rule_digest(exempt)
    # A type whose Claims need evidence compiles the same rule either way.
    assert claim_adjudication_rule(
        claim_type, claim_type_digest=type_digest, origin_replay_exempt=True
    ) == claim_adjudication_rule(claim_type, claim_type_digest=type_digest)

    # An origin-only copy whose body was erased (no current replay).
    copy = _capture("copy", admission="origin_only", replay=False)

    def verdict(rule: object) -> str:
        return evaluate_claim_verdict(
            claim_statement_digest=STATEMENT_DIGEST,
            rule=rule,  # type: ignore[arg-type]
            evaluation_time=NOW,
            captures=(copy,),
            attestations=(),
            providers={},
        ).verdict

    assert verdict(supporting) == "stale"
    assert verdict(exempt) == "supported"
    # Direct evidence still needs a current replay under the exemption.
    direct = _capture("direct", replay=False)
    assert (
        evaluate_claim_verdict(
            claim_statement_digest=STATEMENT_DIGEST,
            rule=exempt,
            evaluation_time=NOW,
            captures=(direct,),
            attestations=(),
            providers={},
        ).verdict
        == "stale"
    )


def test_an_inverse_ages_on_its_own_horizon_and_a_value_on_the_types() -> None:
    """A none (inverse) is judged by ``effective_inverse_stale_after``; values are not."""

    from tests.test_claims.test_claim_type_v9 import DAY, slot_inverse, v9

    hour = {"microseconds": 3_600_000_000}
    claim_type = v9(slot_inverse(stale_after=hour))
    type_digest = claim_type_digest(claim_type).tagged
    positive = claim_adjudication_rule(claim_type, claim_type_digest=type_digest)
    inverse = claim_adjudication_rule(claim_type, claim_type_digest=type_digest, inverse=True)
    assert positive.max_evidence_age == CanonicalDuration(microseconds=DAY.microseconds)
    assert inverse.max_evidence_age == CanonicalDuration(**hour)
    assert claim_adjudication_rule_digest(inverse) != claim_adjudication_rule_digest(positive)
    # Two hours on: the none's evidence is stale, a value on the same Capture is not.
    evidence = (_capture("inverse-horizon"),)
    verdicts = {
        name: evaluate_claim_verdict(
            claim_statement_digest=STATEMENT_DIGEST,
            rule=rule,
            evaluation_time=NOW + timedelta(hours=2),
            captures=evidence,
            attestations=(),
            providers={},
        )
        for name, rule in (("positive", positive), ("inverse", inverse))
    }
    assert verdicts["inverse"].verdict in {"stale", "stale_evidence"}
    assert verdicts["positive"].verdict == "supported"
    # Without its own horizon an inverse compiles the very rule a value does.
    plain = v9()
    plain_digest = claim_type_digest(plain).tagged
    assert claim_adjudication_rule(
        plain, claim_type_digest=plain_digest, inverse=True
    ) == claim_adjudication_rule(plain, claim_type_digest=plain_digest)

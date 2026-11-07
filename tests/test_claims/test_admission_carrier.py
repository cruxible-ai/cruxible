"""Admission reuse follows the complete derived question, never the Claim identity."""

from copy import deepcopy
from datetime import datetime, timezone

from cruxible_client.contracts import claims
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    AcceptedCaptureContract,
    DirectForeignSourceSelection,
    build_foreign_source_capture,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.content_memo import artifact_request
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_core.indexes.projection import AcceptedCoordinate
from tests.core_support._support import initialize_local
from tests.core_support.legacy_capture_builders import build_direct_claim_capture
from tests.test_claims.test_claims import OBSERVED_AT, _claim, _claim_type


def test_admission_carrier_matches_cold_and_releases_at_request_exit(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)
    claim_id = "CLM-" + "a" * 32
    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="ready",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    claim = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=1,
    )
    contract = DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
    accepted = AcceptedCaptureContract(
        path=capture_contract_path(contract.identity.name),
        contract=contract,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    claim_type = _claim_type()
    kwargs = dict(
        claim_type=claim_type,
        capture_digest=capture.capture_digest,
        capture_contract=accepted,
        envelope=capture.envelope,
        verified_attestations=(),
    )
    cold = claims.evaluate_capture_evidence_admissions(claim, **kwargs)
    assert cold[0].trace.result.verdict == "eligible"
    original = claims.evaluate_claim_evidence_admission_trace
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(claims, "evaluate_claim_evidence_admission_trace", counted)
    with artifact_request():
        first = claims.evaluate_capture_evidence_admissions(claim, **kwargs)
        assert first == cold
        again = claims.evaluate_capture_evidence_admissions(deepcopy(claim), **kwargs)
        assert again[0] is first[0]
        assert len(calls) == 1
        other_role = claim.model_copy(
            update={"statement": claim.statement.model_copy(update={"role": "normative"})}
        )
        changed = claims.evaluate_capture_evidence_admissions(other_role, **kwargs)
        assert changed == cold
        assert changed[0] is not first[0]
        assert len(calls) == 2
        # Unrelated ClaimType metadata does not change the policy question.
        kwargs["claim_type"] = claim_type.model_copy(update={"description": "changed"})
        assert claims.evaluate_capture_evidence_admissions(claim, **kwargs) == cold
        assert len(calls) == 2
        # The entire policy, including the selected rule's identity, is keyed.
        changed_type = claim_type.model_copy(
            update={
                "evidence_admission_policy": claim_type.evidence_admission_policy.model_copy(
                    update={
                        "rules": (
                            claim_type.evidence_admission_policy.rules[0].model_copy(
                                update={"rule_id": "renamed"}
                            ),
                        )
                    }
                )
            }
        )
        kwargs["claim_type"] = changed_type
        changed_policy = claims.evaluate_capture_evidence_admissions(claim, **kwargs)
        assert changed_policy[0].trace.result.rule_id == "renamed"
        assert len(calls) == 3
        silent = claim_type.model_copy(
            update={
                "evidence_admission_policy": claim_type.evidence_admission_policy.model_copy(
                    update={"rules": ()}
                )
            }
        )
        kwargs["claim_type"] = silent
        refused = claims.evaluate_capture_evidence_admissions(claim, **kwargs)
        assert refused[0].trace.result.verdict == "refused"
        assert claims.evaluate_capture_evidence_admissions(claim, **kwargs) == refused
        assert len(calls) == 5
    kwargs["claim_type"] = claim_type
    assert claims.evaluate_capture_evidence_admissions(claim, **kwargs) == cold
    assert len(calls) == 6


def _family_claim_type() -> object:
    """A ClaimType whose one evidence rule names only the workspace_file family."""

    return _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="workspace",
                        claim_roles=("normative", "observation"),
                        capture_contract_families=("workspace_file",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )


def _foreign_capture(instance, *, source_id: str, text: bytes):
    store = instance.body_store()
    stored = store.store(text)
    return build_foreign_source_capture(
        store=store,
        actor_id="owner",
        claim_id="CLM-" + "a" * 32,
        rationale=f"cited from catalogued file {source_id}",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        selection=DirectForeignSourceSelection(
            logical_source_identity=source_id,
            span=ContentSpan(content_digest=stored.digest, start_byte=0, end_byte=len(text)),
        ),
    )


def test_a_family_rule_admits_two_catalogued_files_and_refuses_a_non_file_capture(
    tmp_path,
) -> None:
    """D1 (2026-11-06), exercised through the real admission entry point.

    ``claims.evaluate_capture_evidence_admissions`` is what a Claim's actual
    write and verdict paths call; this proves the family selector works there,
    not only in the pure policy evaluator.
    """

    instance, _ = initialize_local(tmp_path)
    claim_id = "CLM-" + "a" * 32
    claim_type = _family_claim_type()

    for source_id, text in (("repo.file-a", b"status: ready"), ("repo.file-b", b"status: done")):
        built = _foreign_capture(instance, source_id=source_id, text=text)
        contract = foreign_source_capture_contract(source_id)
        accepted = AcceptedCaptureContract(
            path=capture_contract_path(contract.identity.name),
            contract=contract,
            artifact_digest=capture_contract_digest(contract).tagged,
        )
        claim = _claim(
            claim_id=claim_id,
            capture_digest=built.capture_digest,
            source_digest=built.source_body_digest,
            source_length=len(text),
            contract=contract,
        )
        decisions = claims.evaluate_capture_evidence_admissions(
            claim,
            claim_type=claim_type,
            capture_digest=built.capture_digest,
            capture_contract=accepted,
            envelope=built.envelope,
            verified_attestations=(),
        )
        assert decisions[0].trace.result.verdict == "eligible"
        assert decisions[0].trace.result.rule_id == "workspace"

    # A Capture under the coordinator self-source contract (not a catalogued
    # workspace file -- a different declared shape) is refused.
    direct = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="ready",
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    direct_contract = DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
    direct_accepted = AcceptedCaptureContract(
        path=capture_contract_path(direct_contract.identity.name),
        contract=direct_contract,
        artifact_digest=capture_contract_digest(direct_contract).tagged,
    )
    direct_claim = _claim(
        claim_id=claim_id,
        capture_digest=direct.capture_digest,
        source_digest=direct.source_body_digest,
        source_length=1,
        contract=direct_contract,
    )
    refused = claims.evaluate_capture_evidence_admissions(
        direct_claim,
        claim_type=claim_type,
        capture_digest=direct.capture_digest,
        capture_contract=direct_accepted,
        envelope=direct.envelope,
        verified_attestations=(),
    )
    assert refused[0].trace.result.verdict == "refused"


def _copy_citation_claim(base_claim, *, capture_digest: str):
    """``base_claim`` restated as an independent copy citation of ``capture_digest``."""

    return base_claim.model_copy(
        update={
            "backing": base_claim.backing.model_copy(
                update={
                    "capture_digests": (capture_digest,),
                    "citations": (
                        claims.build_claim_citation(
                            base_claim.identity,
                            capture_digest=capture_digest,
                            role="copy",
                            origin="independent",
                        ),
                    ),
                }
            )
        }
    )


def test_a_family_rule_admits_an_independent_copy_citation(tmp_path) -> None:
    """D1 review F2: the copy-eligibility gate must see the same verified
    family membership the admission path does, or a family-backed copy
    citation is silently excluded (reads uncovered) even though the same
    Capture would be admitted as ordinary evidence.
    """

    instance, _ = initialize_local(tmp_path)
    claim_id = "CLM-" + "b" * 32
    source_id = "repo.copy-source"
    text = b"a page's own words, captured verbatim"
    claim_type = _family_claim_type()

    built = _foreign_capture(instance, source_id=source_id, text=text)
    contract = foreign_source_capture_contract(source_id)
    accepted = AcceptedCaptureContract(
        path=capture_contract_path(contract.identity.name),
        contract=contract,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    base_claim = _claim(
        claim_id=claim_id,
        capture_digest=built.capture_digest,
        source_digest=built.source_body_digest,
        source_length=len(text),
        contract=contract,
    )
    copy_claim = _copy_citation_claim(base_claim, capture_digest=built.capture_digest)

    assert claims.capture_citation_eligible(
        copy_claim,
        claim_type=claim_type,
        capture_contract=accepted,
        capture_digest=built.capture_digest,
        envelope=built.envelope,
    )
    # Without the envelope (so without a verified shape), the same family-only
    # rule cannot tell this copy apart from an arbitrary one and refuses it --
    # the gap F2 found: a caller that forgot to thread the envelope silently
    # excludes every family-backed copy.
    assert not claims.capture_citation_eligible(
        copy_claim,
        claim_type=claim_type,
        capture_contract=accepted,
        capture_digest=built.capture_digest,
    )


def test_a_non_catalogued_foreign_span_capture_is_refused_by_admission_and_eligibility(
    tmp_path,
) -> None:
    """D1 review F1: the public ``foreign_span`` shape alone must never admit a
    Capture whose contract was not built, byte-for-byte, by core's own
    ``foreign_source_capture_contract`` builder for its own claimed source.
    An independently authored CaptureContract that merely declares the same
    shape (never produced by that builder, so never catalogue-bound) is
    refused through both the admission path and the independent-copy
    eligibility gate, even though it is an otherwise well-formed, valid
    CaptureContract and a well-formed Capture under it.
    """

    instance, _ = initialize_local(tmp_path)
    claim_id = "CLM-" + "c" * 32
    source_id = "other.remote-span"
    text = b"an independently asserted foreign span, not from the catalog"
    claim_type = _family_claim_type()

    built = _foreign_capture(instance, source_id=source_id, text=text)
    # A hand-authored CaptureContract that shares the public foreign_span
    # shape but was never produced by foreign_source_capture_contract: its
    # identity carries no `playbill.foreign-source.` provenance at all.
    forged = foreign_source_capture_contract(source_id).model_copy(
        update={"identity": ArtifactIdentity(kind="CaptureContract", name=source_id)}
    )
    assert forged.shape == "foreign_span"
    forged_accepted = AcceptedCaptureContract(
        path=capture_contract_path(forged.identity.name),
        contract=forged,
        artifact_digest=capture_contract_digest(forged).tagged,
    )
    base_claim = _claim(
        claim_id=claim_id,
        capture_digest=built.capture_digest,
        source_digest=built.source_body_digest,
        source_length=len(text),
        contract=forged,
    )

    decisions = claims.evaluate_capture_evidence_admissions(
        base_claim,
        claim_type=claim_type,
        capture_digest=built.capture_digest,
        capture_contract=forged_accepted,
        envelope=built.envelope,
        verified_attestations=(),
    )
    assert decisions[0].trace.result.verdict == "refused"

    copy_claim = _copy_citation_claim(base_claim, capture_digest=built.capture_digest)
    assert not claims.capture_citation_eligible(
        copy_claim,
        claim_type=claim_type,
        capture_contract=forged_accepted,
        capture_digest=built.capture_digest,
        envelope=built.envelope,
    )

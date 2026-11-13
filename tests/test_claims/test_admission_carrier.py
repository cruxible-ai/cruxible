"""Admission reuse follows the complete derived question, never the Claim identity."""

from copy import deepcopy
from datetime import datetime, timezone

from cruxible_client.contracts import claims
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    AcceptedCaptureContract,
    CaptureContractAny,
    DirectForeignSourceSelection,
    build_foreign_source_capture,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.content_memo import artifact_request
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._subject_kind_support import core_assigned
from tests.core_support._support import initialize_local
from tests.core_support.legacy_capture_builders import build_direct_claim_capture
from tests.test_claims.test_claims import OBSERVED_AT, _claim, _claim_type, _subject
from tests.test_claims.test_identity_evidence_rules import _World


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


def _seed_family_world(
    tmp_path, *, claim_type: ClaimType, contracts: tuple[CaptureContractAny, ...] = ()
) -> _World:
    """A real, governed instance with ``claim_type`` and ``contracts`` accepted.

    Mirrors what a workspace file's write-time lowering does: the ClaimType
    and every catalogued file's foreign-source CaptureContract are proposed
    and accepted together, as one governed change -- not merely computed in
    RAM. A source this call leaves out of ``contracts`` is never accepted
    anywhere in this instance, exactly like an uncatalogued or self-asserted
    one (D1 review round 2, F1).
    """

    world = _World(tmp_path)
    tree = world.tree()
    shell = _subject()
    tree[subject_path(shell.subject_kind, shell.subject_id)] = render_subject(shell)
    core_assigned(world.instance, tree)
    tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    for contract in contracts:
        tree[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
    world.accept(tree, name="seed")
    return world


def _accepted_resolver(instance: PlaybillInstance):
    """A real, keyed, per-digest accepted-history lookup, built from a live instance.

    Exactly what a daemon call site threads as ``historical_capture_contract``
    (``instance.accepted_capture_contract_version``): one targeted lookup per
    digest, never a catalog walk.
    """

    base = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    return lambda digest: instance.accepted_capture_contract_version(base, digest)


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
    """D1 (2026-11-06), exercised through the real admission entry point against
    REAL accepted state (D1 review round 2): both foreign-source
    CaptureContracts are genuinely proposed and accepted into the instance,
    not merely computed in RAM, and membership is proved against that
    accepted state through ``historical_capture_contract``.

    ``claims.evaluate_capture_evidence_admissions`` is what a Claim's actual
    write and verdict paths call; this proves the family selector works there,
    not only in the pure policy evaluator.
    """

    claim_id = "CLM-" + "a" * 32
    claim_type = _family_claim_type()
    source_ids = ("repo.file-a", "repo.file-b")
    contracts = tuple(foreign_source_capture_contract(source_id) for source_id in source_ids)
    world = _seed_family_world(tmp_path, claim_type=claim_type, contracts=contracts)
    instance = world.instance
    resolver = _accepted_resolver(instance)

    for source_id, text, contract in zip(
        source_ids, (b"status: ready", b"status: done"), contracts, strict=True
    ):
        built = _foreign_capture(instance, source_id=source_id, text=text)
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
            historical_capture_contract=resolver,
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
    Capture would be admitted as ordinary evidence. Against REAL accepted
    state (D1 review round 2): the source's CaptureContract is genuinely
    accepted into the instance, not merely computed in RAM.
    """

    claim_id = "CLM-" + "b" * 32
    source_id = "repo.copy-source"
    text = b"a page's own words, captured verbatim"
    claim_type = _family_claim_type()
    contract = foreign_source_capture_contract(source_id)
    world = _seed_family_world(tmp_path, claim_type=claim_type, contracts=(contract,))
    instance = world.instance
    resolver = _accepted_resolver(instance)

    built = _foreign_capture(instance, source_id=source_id, text=text)
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
        historical_capture_contract=resolver,
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
        historical_capture_contract=resolver,
    )
    # Without the accepted-state resolver (so without a way to prove catalog
    # membership), the same family-only rule also refuses -- a caller that
    # forgot to thread it is no more permissive than one that forgot the
    # envelope (D1 review round 2).
    assert not claims.capture_citation_eligible(
        copy_claim,
        claim_type=claim_type,
        capture_contract=accepted,
        capture_digest=built.capture_digest,
        envelope=built.envelope,
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
    CaptureContract and a well-formed Capture under it -- even with a real,
    working accepted-state resolver present (proving the refusal is the
    identity mismatch, not merely a missing resolver).
    """

    claim_id = "CLM-" + "c" * 32
    source_id = "other.remote-span"
    text = b"an independently asserted foreign span, not from the catalog"
    claim_type = _family_claim_type()
    # A hand-authored CaptureContract that shares the public foreign_span
    # shape but was never produced by foreign_source_capture_contract: its
    # identity carries no `playbill.foreign-source.` provenance at all.
    forged = foreign_source_capture_contract(source_id).model_copy(
        update={"identity": ArtifactIdentity(kind="CaptureContract", name=source_id)}
    )
    assert forged.shape == "foreign_span"
    # The world accepts a DIFFERENT, genuine catalogued file, so the resolver
    # is real and working -- it simply never accepted this forged identity.
    genuine = foreign_source_capture_contract("repo.unrelated-genuine-file")
    world = _seed_family_world(tmp_path, claim_type=claim_type, contracts=(genuine,))
    instance = world.instance
    resolver = _accepted_resolver(instance)

    built = _foreign_capture(instance, source_id=source_id, text=text)
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
        historical_capture_contract=resolver,
    )
    assert decisions[0].trace.result.verdict == "refused"

    copy_claim = _copy_citation_claim(base_claim, capture_digest=built.capture_digest)
    assert not claims.capture_citation_eligible(
        copy_claim,
        claim_type=claim_type,
        capture_contract=forged_accepted,
        capture_digest=built.capture_digest,
        envelope=built.envelope,
        historical_capture_contract=resolver,
    )


def test_an_uncatalogued_unmodified_builtin_contract_is_refused_through_a_real_instance(
    tmp_path,
) -> None:
    """D1 review round 2, BLOCKING F1: Codex's exact reproduction.

    Byte-identity with ``foreign_source_capture_contract``'s deterministic
    output is NOT, by itself, proof of catalog membership: with no catalog
    at all, a manually uploaded, never-catalogued span built under its own
    UNMODIFIED built-in contract (no forged identity, no altered field --
    exactly what ``foreign_source_capture_contract("external.upload...")``
    produces) must still be refused, because that exact CaptureContract was
    never proposed and accepted anywhere in this instance. Reproduced
    against a REAL instance with REAL accepted state (not RAM alone): a
    genuinely catalogued sibling file is accepted into the SAME instance
    and IS admitted, side by side with the uncatalogued one being refused.
    """

    claim_type = _family_claim_type()
    catalogued_id = "repo.genuinely-catalogued"
    never_catalogued_id = "external.upload.never-catalogued"
    catalogued_contract = foreign_source_capture_contract(catalogued_id)
    world = _seed_family_world(tmp_path, claim_type=claim_type, contracts=(catalogued_contract,))
    instance = world.instance
    resolver = _accepted_resolver(instance)

    # The genuinely catalogued sibling: accepted, so admitted.
    catalogued_text = b"status: ready"
    catalogued_built = _foreign_capture(instance, source_id=catalogued_id, text=catalogued_text)
    catalogued_accepted = AcceptedCaptureContract(
        path=capture_contract_path(catalogued_contract.identity.name),
        contract=catalogued_contract,
        artifact_digest=capture_contract_digest(catalogued_contract).tagged,
    )
    catalogued_claim = _claim(
        claim_id="CLM-" + "d" * 32,
        capture_digest=catalogued_built.capture_digest,
        source_digest=catalogued_built.source_body_digest,
        source_length=len(catalogued_text),
        contract=catalogued_contract,
    )
    catalogued_decisions = claims.evaluate_capture_evidence_admissions(
        catalogued_claim,
        claim_type=claim_type,
        capture_digest=catalogued_built.capture_digest,
        capture_contract=catalogued_accepted,
        envelope=catalogued_built.envelope,
        verified_attestations=(),
        historical_capture_contract=resolver,
    )
    assert catalogued_decisions[0].trace.result.verdict == "eligible"

    # The never-catalogued one: its contract is the exact, unmodified builtin
    # profile for its own claimed source -- never accepted anywhere.
    uncatalogued_text = b"a manually uploaded span, never catalogued"
    uncatalogued_contract = foreign_source_capture_contract(never_catalogued_id)
    uncatalogued_built = _foreign_capture(
        instance, source_id=never_catalogued_id, text=uncatalogued_text
    )
    uncatalogued_accepted = AcceptedCaptureContract(
        path=capture_contract_path(uncatalogued_contract.identity.name),
        contract=uncatalogued_contract,
        artifact_digest=capture_contract_digest(uncatalogued_contract).tagged,
    )
    uncatalogued_claim = _claim(
        claim_id="CLM-" + "e" * 32,
        capture_digest=uncatalogued_built.capture_digest,
        source_digest=uncatalogued_built.source_body_digest,
        source_length=len(uncatalogued_text),
        contract=uncatalogued_contract,
    )
    uncatalogued_decisions = claims.evaluate_capture_evidence_admissions(
        uncatalogued_claim,
        claim_type=claim_type,
        capture_digest=uncatalogued_built.capture_digest,
        capture_contract=uncatalogued_accepted,
        envelope=uncatalogued_built.envelope,
        verified_attestations=(),
        historical_capture_contract=resolver,
    )
    assert uncatalogued_decisions[0].trace.result.verdict == "refused"

    uncatalogued_copy_claim = _copy_citation_claim(
        uncatalogued_claim, capture_digest=uncatalogued_built.capture_digest
    )
    assert not claims.capture_citation_eligible(
        uncatalogued_copy_claim,
        claim_type=claim_type,
        capture_contract=uncatalogued_accepted,
        capture_digest=uncatalogued_built.capture_digest,
        envelope=uncatalogued_built.envelope,
        historical_capture_contract=resolver,
    )


def test_a_compatible_accepted_successor_is_still_admitted_by_a_family_rule(tmp_path) -> None:
    """D1 review round 2, non-blocking follow-up: accept any accepted successor,
    not only the genesis digest. A Capture taken under a catalogued source's
    ORIGINAL accepted contract version stays admitted after a compatible
    successor (widened selection budget) replaces it as the live head --
    ``historical_capture_contract`` still resolves the superseded-but-valid
    digest, so membership never regresses to "only today's head".
    """

    claim_type = _family_claim_type()
    source_id = "repo.succeeded-file"
    original = foreign_source_capture_contract(source_id)
    world = _seed_family_world(tmp_path, claim_type=claim_type, contracts=(original,))
    instance = world.instance

    text = b"status: ready"
    built = _foreign_capture(instance, source_id=source_id, text=text)
    original_accepted = AcceptedCaptureContract(
        path=capture_contract_path(original.identity.name),
        contract=original,
        artifact_digest=capture_contract_digest(original).tagged,
    )
    claim = _claim(
        claim_id="CLM-" + "f" * 32,
        capture_digest=built.capture_digest,
        source_digest=built.source_body_digest,
        source_length=len(text),
        contract=original,
    )

    # Succeed the contract with a compatible, widened successor (the same
    # pattern as a real workspace-catalog budget increase): same identity,
    # a new digest, predecessor-linked.
    successor = original.model_copy(
        update={
            "selection_budget": original.selection_budget.model_copy(
                update={"max_bytes": original.selection_budget.max_bytes + 1024}
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=original_accepted.artifact_digest),
        }
    )
    tree = world.tree()
    tree[capture_contract_path(successor.identity.name)] = render_capture_contract(successor)
    world.accept(tree, name="succeed-contract")

    resolver = _accepted_resolver(instance)
    decisions = claims.evaluate_capture_evidence_admissions(
        claim,
        claim_type=claim_type,
        capture_digest=built.capture_digest,
        capture_contract=original_accepted,
        envelope=built.envelope,
        verified_attestations=(),
        historical_capture_contract=resolver,
    )
    assert decisions[0].trace.result.verdict == "eligible"

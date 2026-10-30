"""The Claim law for inverse Claims (ClaimType v9): what a none states and what backs it.

Each case judges one constructed Claim directly against the law, with one
own-words (coordinator self-source) Capture as its evidence, so the inverse
checks are seen in isolation: the object names the unit the inverse is keyed
by, an inverse evidence rule must admit a cited Capture, and a flip keeps its
unit and carries exactly the evidence it cites.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
    ArtifactRef,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    AcceptedCaptureContract,
    build_coordinator_self_source_capture,
    capture_contract_digest,
    capture_contract_path,
)
from cruxible_client.contracts.claim_types import (
    AcceptedClaimType,
    ClaimInverse,
    ClaimType,
    claim_type_digest,
    claim_type_path,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifact,
    ClaimBacking,
    ClaimLawResult,
    ClaimReferentContext,
    ClaimStatement,
    ClaimUnitKey,
    LiteralClaimObject,
    NoneClaimObject,
    build_claim_citation,
    claim_artifact_digest,
    claim_path,
    claim_polarity,
    claim_statement_address,
    claim_statement_digest,
    claim_unit_key,
    evaluate_claim_law,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_client.contracts.subjects import (
    AcceptedSubject,
    SubjectShell,
    subject_digest,
    subject_path,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from tests.core_support._support import initialize_local
from tests.core_support.legacy_capture_builders import build_direct_claim_capture

OBSERVED_AT = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CLAIM_ID = "CLM-0123456789abcdef0123456789abcdef"
PREDICATE = "project.work_item.owner"


def _shell() -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-42"),
        subject_kind="project.work_item",
        subject_id="wi-42",
    )


def _rule(rule_id: str) -> ClaimEvidenceAdmissionRule:
    return ClaimEvidenceAdmissionRule(
        rule_id=rule_id,
        claim_roles=("observation",),
        capture_contracts=(
            ArtifactRef(
                role="capture-contract", target=COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity
            ),
        ),
        admission="direct",
        subject_binding="exact_claim_subject",
    )


def _claim_type(*, inverse_rules: tuple[str, ...] | None = ("scan",)) -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
        predicate=PREDICATE,
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(_rule("other"), _rule("scan"))
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one", eligible_verdicts=("supported",), selector="only_contender"
        ),
        evidence_requirement="self",
        revision_evidence="accumulate",
        canonical_form=(),
        inverse=(
            None
            if inverse_rules is None
            else ClaimInverse.model_validate(
                {"key": {"kind": "slot"}, "evidence_rules": list(inverse_rules)}
            )
        ),
    )


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.instance, _owner = initialize_local(tmp_path)
        self.store = self.instance.body_store()
        self.coordinate = AcceptedCoordinate.from_internal(self.instance.accepted_coordinate())

    def capture(self, value: object) -> tuple[str, str, int]:
        body = f"An inventory read that looked for an owner: {value}.".encode()
        built = build_coordinator_self_source_capture(
            store=self.store,
            actor_id="owner",
            claim_id=CLAIM_ID,
            body=body,
            observed_at=OBSERVED_AT,
            accepted_coordinate=self.coordinate,
        )
        return built.capture_digest, built.commitment_digest, len(body)

    def claim(
        self,
        claim_type: ClaimType,
        obj: Any,
        *,
        value: object = "none",
        predecessor: AcceptedClaim | None = None,
        object_shell: SubjectShell | None = None,
    ) -> ClaimArtifact:
        shell = _shell()
        capture, source, length = self.capture(value)
        path = claim_path(CLAIM_ID)
        identity = ArtifactIdentity(kind="Claim", name=CLAIM_ID)
        citation = build_claim_citation(
            identity, capture_digest=capture, role="evidence", origin="self_source"
        )
        mapping = SourceMapping(
            subject=claim_statement_address(path),
            spans=(ContentSpan(content_digest=source, start_byte=0, end_byte=length),),
        )
        captures: tuple[str, ...] = (capture,)
        citations = (citation,)
        if predecessor is not None and isinstance(predecessor.claim.backing, ClaimBacking):
            captures = tuple(sorted({*predecessor.claim.backing.capture_digests, capture}))
            citations = tuple(
                sorted(
                    {*predecessor.claim.backing.citations, citation},
                    key=lambda item: item.citation_id,
                )
            )
        return ClaimArtifact(
            identity=identity,
            statement=ClaimStatement(
                subject=SemanticAddress.whole_artifact(
                    subject_path(shell.subject_kind, shell.subject_id)
                ),
                claim_type=claim_type.identity,
                claim_type_digest=claim_type_digest(claim_type).tagged,
                predicate=claim_type.predicate,
                object=obj,
                role="observation",
            ),
            backing=ClaimBacking(
                referent_context=ClaimReferentContext(
                    subject_content_digest=subject_digest(shell).tagged,
                    object_content_digest=(
                        None if object_shell is None else subject_digest(object_shell).tagged
                    ),
                    observed_at=OBSERVED_AT,
                ),
                capture_digests=captures,
                citations=citations,
                source_mappings=(mapping,),
            ),
            pins=tuple(
                sorted(
                    (
                        ArtifactPin(
                            role="capture-contract",
                            target=COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity,
                            artifact_digest=capture_contract_digest(
                                COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
                            ).tagged,
                        ),
                        ArtifactPin(
                            role="claim-type",
                            target=claim_type.identity,
                            artifact_digest=claim_type_digest(claim_type).tagged,
                        ),
                        ArtifactPin(
                            role="subject",
                            target=shell.identity,
                            artifact_digest=subject_digest(shell).tagged,
                        ),
                        *(
                            ()
                            if object_shell is None
                            else (
                                ArtifactPin(
                                    role="object-subject",
                                    target=object_shell.identity,
                                    artifact_digest=subject_digest(object_shell).tagged,
                                ),
                            )
                        ),
                    ),
                    key=lambda item: (item.role.encode(), item.target.qualified.encode()),
                )
            ),
            lifecycle=ArtifactLifecycle(
                predecessor_digest=None if predecessor is None else predecessor.artifact_digest
            ),
        )

    def judge(
        self,
        claim: Any,
        claim_type: ClaimType,
        *,
        predecessor: AcceptedClaim | None = None,
        object_shell: SubjectShell | None = None,
        retirement_shape_exemption: bool = False,
        extra_contracts: tuple[Any, ...] = (),
        erasure_records: Any = None,
        erasure_contract: Any = None,
    ) -> ClaimLawResult:
        shell = _shell()
        contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
        shells = (shell,) if object_shell is None else (shell, object_shell)
        return evaluate_claim_law(
            claim,
            path=claim_path(CLAIM_ID),
            principals=self.instance.accepted_principal_registry(
                self.instance.accepted_coordinate()
            ),
            predecessor=predecessor,
            subjects={
                subject_path(item.subject_kind, item.subject_id): AcceptedSubject(
                    path=subject_path(item.subject_kind, item.subject_id),
                    shell=item,
                    artifact_digest=subject_digest(item).tagged,
                )
                for item in shells
            },
            claim_types={
                claim_type.identity.qualified: AcceptedClaimType(
                    path=claim_type_path(claim_type.predicate),
                    claim_type=claim_type,
                    artifact_digest=claim_type_digest(claim_type).tagged,
                )
            },
            capture_contracts={
                item.identity.qualified: AcceptedCaptureContract(
                    path=capture_contract_path(item.identity.name),
                    contract=item,
                    artifact_digest=capture_contract_digest(item).tagged,
                )
                for item in (contract, *extra_contracts)
            },
            capture_store=self.store,
            law_digest="sha256:" + "ab" * 32,
            evaluation_time=OBSERVED_AT,
            claim_type_v7_semantics=True,
            require_new_citation_proof=False,
            claim_rebind=True,
            allow_claim_type_retirement_shape_exemption=retirement_shape_exemption,
            erasure_records=erasure_records,
            erasure_contract=erasure_contract,
            accepted_coordinate=self.coordinate,
            accepted_referent_coordinates=frozenset({self.coordinate}),
        )


def _accepted(claim: ClaimArtifact) -> AcceptedClaim:
    return AcceptedClaim(
        path=claim_path(CLAIM_ID),
        claim=claim,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
    )


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def _code(result: ClaimLawResult) -> str | None:
    return None if result.verdict == "accepted" else result.diagnostics[0].code


def test_an_inverse_cited_on_an_inverse_rule_is_accepted(world: _World) -> None:
    claim_type = _claim_type()
    claim = world.claim(claim_type, NoneClaimObject())
    assert claim.statement.object.model_dump(mode="json") == {"kind": "none"}
    assert claim_polarity(claim.statement) == "inverse"

    result = world.judge(claim, claim_type)

    assert result.verdict == "accepted", result.diagnostics


def test_an_inverse_needs_a_type_that_declares_one(world: _World) -> None:
    claim_type = _claim_type(inverse_rules=None)
    result = world.judge(world.claim(claim_type, NoneClaimObject()), claim_type)
    assert _code(result) == "cruxible.claim.inverse_undeclared"


def test_a_slot_inverse_names_no_object(world: _World) -> None:
    claim_type = _claim_type()
    claim = world.claim(claim_type, NoneClaimObject(of=LiteralClaimObject(value="alice")))
    assert _code(world.judge(claim, claim_type)) == "cruxible.claim.inverse_key_mismatch"


def test_only_an_inverse_rule_establishes_a_none(world: _World) -> None:
    # The Capture is admitted by the type's other rule, not by an inverse rule.
    claim_type = _claim_type(inverse_rules=("other",)).model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(rules=(_rule("scan"),)),
            "inverse": ClaimInverse.model_validate(
                {"key": {"kind": "slot"}, "evidence_rules": ["other"]}
            ),
        }
    )
    result = world.judge(world.claim(claim_type, NoneClaimObject()), claim_type)
    assert _code(result) == "cruxible.claim.inverse_evidence_required"


def test_a_flip_is_judged_as_replace_and_keeps_its_unit(world: _World) -> None:
    claim_type = _claim_type()
    positive = world.claim(claim_type, LiteralClaimObject(value="alice"), value="alice")
    assert world.judge(positive, claim_type).verdict == "accepted"
    accepted = _accepted(positive)

    # Under accumulate a revision keeps its predecessor's evidence; a flip carries
    # only what it cites, so dropping the Capture that showed the value passes.
    flip = world.claim(claim_type, NoneClaimObject()).model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=accepted.artifact_digest),
        }
    )
    result = world.judge(flip, claim_type, predecessor=accepted)
    assert result.verdict == "accepted", result.diagnostics

    moved = flip.model_copy(
        update={"statement": flip.statement.model_copy(update={"qualifier": "elsewhere"})}
    )
    assert _code(world.judge(moved, claim_type, predecessor=accepted)) == (
        "cruxible.claim.flip_unit_moved"
    )


def test_unit_keys_name_the_slot_the_object_or_the_key_value() -> None:
    claim_type = _claim_type()
    assert claim_type.inverse is not None
    statement = ClaimStatement(
        subject=SemanticAddress.whole_artifact("subjects/project.work_item/wi-42.json"),
        claim_type=claim_type.identity,
        claim_type_digest=claim_type_digest(claim_type).tagged,
        predicate=PREDICATE,
        object=LiteralClaimObject(value="alice"),
        role="observation",
    )
    assert claim_unit_key(statement, claim_type.inverse) == ClaimUnitKey(kind="slot")
    by_object = ClaimInverse.model_validate({"key": {"kind": "object"}, "evidence_rules": ["scan"]})
    assert claim_unit_key(statement, by_object) == ClaimUnitKey(
        kind="object", value={"kind": "literal", "value": "alice"}
    )
    by_key = ClaimInverse.model_validate(
        {
            "key": {"kind": "object_key", "predicate": "project.release.name"},
            "evidence_rules": ["scan"],
        }
    )
    none_of = statement.model_copy(
        update={"object": NoneClaimObject(of=LiteralClaimObject(value="gh"))}
    )
    assert claim_unit_key(none_of, by_key) == ClaimUnitKey(kind="object_key", value="gh")
    # A positive's key value is read from state; unread, the unit is unknown.
    assert claim_unit_key(statement, by_key) is None


def test_a_flip_that_keeps_its_backing_is_still_judged_as_a_flip(world: _World) -> None:
    # The positive's Capture is admitted by a rule that is not an inverse rule.
    claim_type = _claim_type().model_copy(
        update={
            "inverse": ClaimInverse.model_validate(
                {"key": {"kind": "slot"}, "evidence_rules": ["other"]}
            ),
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(rules=(_rule("scan"),)),
        }
    )
    positive = world.claim(claim_type, LiteralClaimObject(value="alice"), value="alice")
    assert world.judge(positive, claim_type).verdict == "accepted"
    accepted = _accepted(positive)

    # Same backing, new polarity: not a carry that skips the inverse's checks.
    flip = positive.model_copy(
        update={
            "statement": positive.statement.model_copy(update={"object": NoneClaimObject()}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=accepted.artifact_digest),
        }
    )
    assert flip.backing == positive.backing
    assert _code(world.judge(flip, claim_type, predecessor=accepted)) == (
        "cruxible.claim.inverse_evidence_required"
    )
    moved = flip.model_copy(
        update={"statement": flip.statement.model_copy(update={"qualifier": "elsewhere"})}
    )
    assert _code(world.judge(moved, claim_type, predecessor=accepted)) == (
        "cruxible.claim.flip_unit_moved"
    )


def test_own_words_admitted_origin_only_never_establish_a_none(world: _World) -> None:
    origin_only = _rule("scan").model_copy(update={"admission": "origin_only"})
    claim_type = _claim_type().model_copy(
        update={
            "evidence_requirement": "none",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(_rule("other"), origin_only)
            ),
        }
    )
    result = world.judge(world.claim(claim_type, NoneClaimObject()), claim_type)
    assert _code(result) == "cruxible.claim.inverse_evidence_required"

    from cruxible_client.contracts.claim_types import evaluate_claim_type_law

    refused = evaluate_claim_type_law(
        claim_type, path=claim_type_path(claim_type.predicate), predecessor=None
    )
    assert refused.diagnostics[0].code == "cruxible.claim_type.inverse_rule_not_evidential"


def test_retiring_a_subject_inverse_after_the_type_drops_its_inverse(world: _World) -> None:
    from cruxible_client.contracts.claims import (
        ClaimArtifact,
        ClaimRetirementAttribution,
        SubjectClaimObject,
    )

    release = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.release/r-1"),
        subject_kind="project.release",
        subject_id="r-1",
    )
    blocks = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.blocks"),
            "predicate": "project.work_item.blocks",
            "object_kind": "subject",
            "literal_schema": None,
            "allowed_object_subject_kinds": ("project.release",),
            "cardinality": "many",
            "resolution_policy": ClaimResolutionPolicy(
                cardinality="many", eligible_verdicts=("supported",), selector="all"
            ),
            "inverse": ClaimInverse.model_validate(
                {"key": {"kind": "object"}, "evidence_rules": ["scan"]}
            ),
        }
    )
    none_of = NoneClaimObject(
        of=SubjectClaimObject(
            address=SemanticAddress.whole_artifact(subject_path("project.release", "r-1"))
        )
    )
    inverse = world.claim(blocks, none_of, object_shell=release)
    assert world.judge(inverse, blocks, object_shell=release).verdict == "accepted"
    accepted = _accepted(inverse)

    # The type's successor drops its inverse; the inverse is retired with it.
    successor = blocks.model_copy(
        update={
            "inverse": None,
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(blocks).tagged),
        }
    )
    successor_digest = claim_type_digest(successor).tagged
    retirement = ClaimArtifact(
        identity=inverse.identity,
        statement=inverse.statement.model_copy(update={"claim_type_digest": successor_digest}),
        backing=inverse.backing,
        pins=tuple(
            pin.model_copy(update={"artifact_digest": successor_digest})
            if pin.role == "claim-type"
            else pin
            for pin in inverse.pins
        ),
        lifecycle=ArtifactLifecycle(state="retired", predecessor_digest=accepted.artifact_digest),
        retirement=ClaimRetirementAttribution(reason="was-rescinded"),
    )

    result = world.judge(
        retirement,
        successor,
        predecessor=accepted,
        object_shell=release,
        retirement_shape_exemption=True,
    )

    assert result.verdict == "accepted", result.diagnostics


PRIOR_HEAD = AcceptedCoordinate(
    git_oid="11" * 20,
    semantic_root="sha256:" + "22" * 32,
    generation_root="sha256:" + "33" * 32,
    compiler_digest="sha256:" + "44" * 32,
)


def _shared_erasure(body: str, *, head: AcceptedCoordinate = PRIOR_HEAD) -> tuple[Any, Any]:
    """An erasable contract and its erasure of ``body``, recorded under ``head``."""

    from cruxible_client.contracts.capture_erasure import (
        CaptureErasureRecord,
        RecordedCaptureErasure,
    )
    from cruxible_client.contracts.captures import authorize_erasure
    from tests.core_support._pc_c_support import capture_contract

    erasable = authorize_erasure(capture_contract())
    assert erasable.retention_erasure_policy.erasure_rule_digest is not None
    record = CaptureErasureRecord(
        capture_digest="sha256:" + "ab" * 32,
        capture_contract=erasable.identity.qualified,
        capture_contract_digest=capture_contract_digest(erasable).tagged,
        erasure_rule_digest=erasable.retention_erasure_policy.erasure_rule_digest,
        body_digests=(body,),
        erased_by="owner",
        erased_at=OBSERVED_AT + timedelta(hours=1),
        because="The shared bytes held a secret.",
    )
    return erasable, RecordedCaptureErasure(record=record, coordinate=head, sequence=1)


def _naming(body: str, *items: Any) -> Any:
    """An erasure lookup answering, for ``body``, the latest of ``items`` (keyed by body)."""

    return lambda bodies: {body: items[-1]} if items and body in bodies else {}


def _retirement(claim: Any, accepted: AcceptedClaim) -> Any:
    from cruxible_client.contracts.claims import ClaimArtifact, ClaimRetirementAttribution

    return ClaimArtifact(
        identity=claim.identity,
        statement=claim.statement,
        backing=claim.backing,
        pins=claim.pins,
        lifecycle=ArtifactLifecycle(state="retired", predecessor_digest=accepted.artifact_digest),
        retirement=ClaimRetirementAttribution(reason="was-rescinded"),
    )


def _body(claim: Any) -> str:
    (body,) = {
        span.content_digest for mapping in claim.backing.source_mappings for span in mapping.spans
    }
    return str(body)


def test_an_erasure_of_a_shared_body_counts_for_every_capture_holding_it(world: _World) -> None:
    claim_type = _claim_type()
    positive = world.claim(claim_type, LiteralClaimObject(value="alice"), value="alice")
    assert world.judge(positive, claim_type).verdict == "accepted"
    accepted = _accepted(positive)
    body = _body(positive)
    # Another Capture, under a contract that authorizes erasure, held the same bytes
    # and was erased: the content-addressed body is gone for this Claim's Capture too.
    erasable, other = _shared_erasure(body)
    world.store.erase(body)
    retirement = _retirement(positive, accepted)

    stranded = world.judge(
        retirement, claim_type, predecessor=accepted, erasure_records=_naming(body)
    )
    assert _code(stranded) == "cruxible.claim.capture_unverified"
    # The record names the body, not this Capture: it still counts, and only under
    # the erasing contract's own authority.
    retired = world.judge(
        retirement,
        claim_type,
        predecessor=accepted,
        extra_contracts=(erasable,),
        erasure_records=_naming(body, other),
    )
    assert retired.verdict == "accepted", retired.diagnostics
    forged = other.model_copy(
        update={
            "record": other.record.model_copy(update={"erasure_rule_digest": "sha256:" + "0" * 64})
        }
    )
    refused = world.judge(
        retirement,
        claim_type,
        predecessor=accepted,
        extra_contracts=(erasable,),
        erasure_records=_naming(body, forged),
    )
    assert _code(refused) == "cruxible.claim.capture_unverified"


def test_an_erasure_never_enters_the_verdict_so_a_judgment_reproduces_after_it(
    world: _World,
) -> None:
    """The Claim stands on its evidence digest: erased or not, the judgment is the same."""

    claim_type = _claim_type()
    positive = world.claim(claim_type, LiteralClaimObject(value="alice"), value="alice")
    accepted = _accepted(positive)
    body = _body(positive)
    erasable, erased = _shared_erasure(body)
    retirement = _retirement(positive, accepted)
    before = world.judge(
        retirement,
        claim_type,
        predecessor=accepted,
        extra_contracts=(erasable,),
        erasure_records=_naming(body),
    )
    assert before.verdict == "accepted", before.diagnostics
    world.store.erase(body)

    after = world.judge(
        retirement,
        claim_type,
        predecessor=accepted,
        extra_contracts=(erasable,),
        erasure_records=_naming(body, erased),
    )

    assert after.verdict == "accepted", after.diagnostics
    assert after.evidence == before.evidence
    assert after.evidence is not None
    (capture,) = after.evidence.verdict_captures
    assert capture.current_replay_available


def test_erasure_authority_is_read_at_the_erasures_own_head(world: _World) -> None:
    claim_type = _claim_type()
    positive = world.claim(claim_type, LiteralClaimObject(value="alice"), value="alice")
    accepted = _accepted(positive)
    body = _body(positive)
    # The authorizing contract was accepted after this judgment's dependencies: it
    # is not among them, only at the head the erasure was recorded under.
    erasable, later = _shared_erasure(body)
    world.store.erase(body)
    retirement = _retirement(positive, accepted)

    unknown = world.judge(
        retirement, claim_type, predecessor=accepted, erasure_records=_naming(body, later)
    )
    assert _code(unknown) == "cruxible.claim.capture_unverified"
    accepted_erasable = AcceptedCaptureContract(
        path=capture_contract_path(erasable.identity.name),
        contract=erasable,
        artifact_digest=capture_contract_digest(erasable).tagged,
    )
    retired = world.judge(
        retirement,
        claim_type,
        predecessor=accepted,
        erasure_records=_naming(body, later),
        erasure_contract=lambda item: accepted_erasable if item.coordinate == PRIOR_HEAD else None,
    )
    assert retired.verdict == "accepted", retired.diagnostics


def test_a_direct_self_source_whose_shared_body_was_erased_can_be_retired(
    world: _World,
) -> None:
    from cruxible_client.contracts.captures import (
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    )
    from tests.test_claims.test_claims import _claim
    from tests.test_claims.test_claims import _claim_type as _direct_type

    direct_type = _direct_type()
    built = build_direct_claim_capture(
        store=world.store,
        actor_id="owner",
        claim_id=CLAIM_ID,
        value="ready",
        rationale="The work item is ready for review.",
        observed_at=OBSERVED_AT,
        accepted_coordinate=world.coordinate,
    )
    assert built.envelope.commitment.byte_length is not None
    claim = _claim(
        claim_id=CLAIM_ID,
        capture_digest=built.capture_digest,
        source_digest=built.source_body_digest,
        source_length=built.envelope.commitment.byte_length,
    )
    shell = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-42"),
        subject_kind="project.work_item",
        subject_id="wi-42",
    )

    def judge(candidate: Any, **extra: Any) -> ClaimLawResult:
        contracts = (DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT, *extra.pop("contracts", ()))
        return evaluate_claim_law(
            candidate,
            path=claim_path(CLAIM_ID),
            principals=world.instance.accepted_principal_registry(
                world.instance.accepted_coordinate()
            ),
            subjects={
                subject_path(shell.subject_kind, shell.subject_id): AcceptedSubject(
                    path=subject_path(shell.subject_kind, shell.subject_id),
                    shell=shell,
                    artifact_digest=subject_digest(shell).tagged,
                )
            },
            claim_types={
                direct_type.identity.qualified: AcceptedClaimType(
                    path=claim_type_path(direct_type.predicate),
                    claim_type=direct_type,
                    artifact_digest=claim_type_digest(direct_type).tagged,
                )
            },
            capture_contracts={
                item.identity.qualified: AcceptedCaptureContract(
                    path=capture_contract_path(item.identity.name),
                    contract=item,
                    artifact_digest=capture_contract_digest(item).tagged,
                )
                for item in contracts
            },
            capture_store=world.store,
            law_digest="sha256:" + "ab" * 32,
            evaluation_time=OBSERVED_AT,
            require_new_citation_proof=False,
            accepted_coordinate=world.coordinate,
            accepted_referent_coordinates=frozenset({world.coordinate}),
            **extra,
        )

    first = judge(claim, predecessor=None)
    assert first.verdict == "accepted", first.diagnostics
    accepted = _accepted(claim)
    erasable, shared = _shared_erasure(built.source_body_digest)
    world.store.erase(built.source_body_digest)

    # The inherited citation keeps the origin and binding its acceptance verified;
    # re-deriving them would read the destroyed body.
    retired = judge(
        _retirement(claim, accepted),
        predecessor=accepted,
        contracts=(erasable,),
        erasure_records=_naming(built.source_body_digest, shared),
    )
    assert retired.verdict == "accepted", retired.diagnostics

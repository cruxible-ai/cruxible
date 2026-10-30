"""F03: exact producer pins resolve to the version that actually ran.

A Capture records the exact Procedure version that produced it
(``run_coordinate.executable_digest``). A Claim citing that Capture must pin
that exact version -- never the producer's current head, which may have
succeeded it. Before this fix, the Claim law's pin check unconditionally
added the producer's head digest as an acceptable alternative, so a Claim
pinning the head (not what actually ran) passed anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    CaptureRunCoordinate,
    ProducerVersions,
    _procedure_producer_receipt_digest,
    build_procedure_capture_v2,
    capture_contract_digest,
)
from cruxible_client.contracts.claim_types import AcceptedClaimType, ClaimType, claim_type_digest
from cruxible_client.contracts.claims import (
    AcceptedSubject,
    ClaimArtifact,
    ClaimBacking,
    ClaimReferentContext,
    ClaimStatement,
    LiteralClaimObject,
    build_claim_citation,
    claim_path,
    evaluate_claim_law,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.principals import PrincipalRegistrySnapshot
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, subject_digest, subject_path
from cruxible_client.contracts.types import PrincipalRecord
from tests.core_support._pc_c_support import body_store
from tests.core_support._pc_c_support import capture_contract as build_capture_contract

OBSERVED_AT = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
PROCEDURE_IDENTITY = ArtifactIdentity(kind="Procedure", name="test.proc")
GENERATION_ROOT = typed_digest(Sha256Value, "gen", {"v": "1"}).tagged
ACCEPTED_COORDINATE = AcceptedCoordinate(
    git_oid="a" * 40,
    semantic_root="sha256:" + "b" * 64,
    generation_root=GENERATION_ROOT,
    compiler_digest="sha256:" + "c" * 64,
)
P1 = typed_digest(Sha256Value, "test-procedure", {"v": "1"}).tagged  # the version that ran
P2 = typed_digest(Sha256Value, "test-procedure", {"v": "2"}).tagged  # its successor, the head


@dataclass(frozen=True)
class _FakeProcedureReceipt:
    """Minimal ``ProcedureProducerReceiptProtocol`` for one procedure capture."""

    capture_contract_digest_value: str
    tag: str = "playbill-procedure-producer-receipt-v1"
    admission_binding_digest: str = "sha256:" + "d" * 64
    run_id: str = "run-1"
    accepted_coordinate: object = ACCEPTED_COORDINATE
    procedure_identity: object = PROCEDURE_IDENTITY
    procedure_artifact_digest: str = P1
    terminal_node_id: str = "emit"
    item_manifest_digests: tuple = ()

    @property
    def capture_contract_digest(self) -> str:
        return self.capture_contract_digest_value


@dataclass(frozen=True)
class _FakeReceiptResolver:
    """Resolves exactly one receipt by digest; proves no new-citation gate."""

    receipt: object
    receipt_digest: str

    def producer_receipt(self, digest, *, run_id):  # type: ignore[no-untyped-def]
        return self.receipt if digest == self.receipt_digest else None

    def produced_capture(self, capture_digest, *, run_id):  # type: ignore[no-untyped-def]
        return ()

    def recall_verified(self, capture_digest):  # type: ignore[no-untyped-def]
        return None

    def remember_verified(self, verified):  # type: ignore[no-untyped-def]
        pass


def _subject() -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-42"),
        subject_kind="project.work_item",
        subject_id="wi-42",
    )


def _claim_type(contract: str) -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"enum": ["blocked", "done", "ready"], "type": "string"},
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="direct-independent",
                    claim_roles=("normative", "observation"),
                    capture_contracts=(contract,),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _judge_claim_pinning(
    tmp_path: Path,
    *,
    pinned_procedure_digest: str,
    providers: object = None,
    uncited: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Build a Procedure-produced Capture (recorded at P1), a Claim citing it
    and pinning ``pinned_procedure_digest``, with the producer's head at P2;
    return the law's verdict ("accepted" or "refused") and its diagnostic codes.

    ``providers`` is passed straight through to ``evaluate_claim_law`` (F08):
    this Claim's producer is a Procedure, never looked up in it, so it proves
    nothing about ``providers`` is ever read merely by being passed."""

    store = body_store(tmp_path)
    contract = build_capture_contract(name="test.events-v1", epistemic_grade="observed")
    contract_digest = capture_contract_digest(contract).tagged
    shell = _subject()
    claim_type = _claim_type(contract.identity.name)

    run_coordinate = CaptureRunCoordinate(
        run_kind="procedure",
        run_id="run-1",
        bound_generation=GENERATION_ROOT,
        executable_identity=PROCEDURE_IDENTITY,
        executable_digest=P1,
    )
    receipt = _FakeProcedureReceipt(capture_contract_digest_value=contract_digest)
    receipt_digest = _procedure_producer_receipt_digest(receipt)
    built = build_procedure_capture_v2(
        store=store,
        contract=contract,
        source_body=b'{"value": 1}',
        run_coordinate=run_coordinate,
        producer_receipt_digest=receipt_digest,
        producer=PROCEDURE_IDENTITY,
        producer_binding_digest=P1,
        observed_at=OBSERVED_AT,
    )

    claim_id = "CLM-" + "a" * 32
    path = claim_path(claim_id)
    claim = ClaimArtifact(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(
                subject_path(shell.subject_kind, shell.subject_id)
            ),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        backing=ClaimBacking(
            referent_context=ClaimReferentContext(
                subject_content_digest=subject_digest(shell).tagged, observed_at=OBSERVED_AT
            ),
            capture_digests=(built.capture_digest,),
            citations=()
            if uncited
            else (
                build_claim_citation(
                    ArtifactIdentity(kind="Claim", name=claim_id),
                    capture_digest=built.capture_digest,
                    role="evidence",
                    origin="independent",
                ),
            ),
            source_mappings=(),
        ),
        pins=tuple(
            sorted(
                (
                    ArtifactPin(
                        role="capture-contract",
                        target=contract.identity,
                        artifact_digest=contract_digest,
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
                    ArtifactPin(
                        role="procedure",
                        target=PROCEDURE_IDENTITY,
                        artifact_digest=pinned_procedure_digest,
                    ),
                ),
                key=lambda item: (item.role.encode(), item.target.qualified.encode()),
            )
        ),
    )

    # The producer's accepted head is P2 (a successor to P1); retained
    # history still admits P1, the version that actually produced the
    # Capture -- exactly the ruling C4/F03 scenario.
    producer_artifact_digests = ProducerVersions(
        {PROCEDURE_IDENTITY.qualified: P2}, lambda q, d: d == P1
    )

    result = evaluate_claim_law(
        claim,
        path=path,
        principals=PrincipalRegistrySnapshot(
            semantic_root="sha256:" + "0" * 64,
            principals=(
                PrincipalRecord(principal_id="daemon", public_key="11" * 32, kind="daemon"),
            ),
        ),
        predecessor=None,
        subjects={
            subject_path(shell.subject_kind, shell.subject_id): AcceptedSubject(
                path=subject_path(shell.subject_kind, shell.subject_id),
                shell=shell,
                artifact_digest=subject_digest(shell).tagged,
            )
        },
        claim_types={
            claim_type.identity.qualified: AcceptedClaimType(
                path="claim-types/project.work_item/status.json",
                claim_type=claim_type,
                artifact_digest=claim_type_digest(claim_type).tagged,
            )
        },
        capture_contracts={
            contract.identity.qualified: AcceptedCaptureContract(
                path="capture-contracts/test.events-v1.json",
                contract=contract,
                artifact_digest=contract_digest,
            )
        },
        capture_store=store,
        law_digest="sha256:" + "1" * 64,
        producer_artifact_digests=producer_artifact_digests,
        producer_receipt_resolver=_FakeReceiptResolver(
            receipt=receipt, receipt_digest=receipt_digest
        ),
        require_new_citation_proof=False,
        providers=providers,  # type: ignore[arg-type]
    )
    return result.verdict, tuple(d.code for d in result.diagnostics)


def test_a_claim_pinning_the_exact_version_that_produced_its_capture_is_accepted(
    tmp_path: Path,
) -> None:
    verdict, codes = _judge_claim_pinning(tmp_path, pinned_procedure_digest=P1)
    assert verdict == "accepted", codes


def test_a_backing_capture_without_an_explicit_citation_is_refused(tmp_path: Path) -> None:
    """No legacy reference is synthesized: every backing Capture names its citation."""

    verdict, codes = _judge_claim_pinning(tmp_path, pinned_procedure_digest=P1, uncited=True)
    assert verdict == "refused"
    assert "cruxible.claim.citation_set_incomplete" in codes


def test_a_claim_pinning_the_producers_head_instead_of_what_ran_is_refused(
    tmp_path: Path,
) -> None:
    """F03: before the fix, the head (P2) was an accepted alternative to the
    exact version that ran (P1); after it, only P1 satisfies the pin."""

    verdict, codes = _judge_claim_pinning(tmp_path, pinned_procedure_digest=P2)
    assert verdict == "refused"
    assert "cruxible.claim.procedure_pin_missing" in codes


# --- F08 fix round 2: a lazy ``providers`` view is never enumerated ----------


@pytest.mark.parametrize("population", [20, 200])
def test_claim_law_never_enumerates_unrelated_providers(tmp_path: Path, population: int) -> None:
    """Codex's reproduction: ``providers or {}`` (claims.py) called
    ``SelectedRows.__len__``, which walks every key -- an unrestricted
    owner-table SELECT in production (``indexes/evaluated_state.py``). This
    Claim's producer is a Procedure, so ``providers`` is never looked up at
    all; passing the production-shaped ``mapped_values(SelectedRows(...), ...)``
    view must not even be iterated or measured, at any population size.
    """

    from cruxible_core.indexes.evaluated_state import SelectedRows, mapped_values

    # A population of this size exists (as production's resolved-Provider
    # table would), but none of it names this Claim's producer (a Procedure):
    # a correct point lookup for "Procedure:test.proc" simply misses, exactly
    # as it would in production.
    population_keys = tuple(f"Provider:unrelated-{i}" for i in range(population))

    def _trap_keys():  # type: ignore[no-untyped-def]
        raise AssertionError(f"enumerated the full {population}-Provider population via truthiness")

    def _get(qualified: str) -> object:  # type: ignore[no-untyped-def]
        if qualified not in population_keys:
            raise KeyError(qualified)
        raise AssertionError(f"looked up unrelated Provider {qualified!r}")

    rows: SelectedRows[object] = SelectedRows(get=_get, keys=_trap_keys)
    providers = mapped_values(rows, lambda accepted: accepted.provider)

    verdict, codes = _judge_claim_pinning(tmp_path, pinned_procedure_digest=P1, providers=providers)
    assert verdict == "accepted", codes

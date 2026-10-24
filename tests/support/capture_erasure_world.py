"""A world with one Procedure-fetched Capture a live Claim cites, under a chosen erasure policy.

A workspace.file Source run lands the Capture (envelope and retained body) under
``capture_contract(erasure=...)``; the write vocabulary's work items carry one
more field, ``project.work_item.severity``, whose rule admits that contract, and
``set`` cites the Capture for ``wi-1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    CaptureContract,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._candidate_support import submit_member_candidate
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.core_support._light_capture_support import record_contract as capture_contract
from tests.core_support._write_support import KIND, caller, seed_write_vocabulary

FIELD = f"{KIND}.severity"
SUBJECT = f"{KIND}/wi-1"
READ = BodyAccessContext(principal_id="erasure-test", can_read_body=True)


@dataclass
class ErasureWorld:
    instance: PlaybillInstance
    owner: Any
    root: Path
    contract: CaptureContract
    capture: str
    body: str
    claim: str


def _severity_type(contract: CaptureContract) -> ClaimType:
    return ClaimType(
        artifact_format="playbill-claim-type-v7",
        evidence_requirement="captured",
        revision_evidence="replace",
        identity=ArtifactIdentity(kind="ClaimType", name=FIELD),
        predicate=FIELD,
        allowed_subject_kinds=(KIND,),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="scan",
                    claim_roles=("observation",),
                    capture_contracts=(contract.identity.name,),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one", eligible_verdicts=("supported",), selector="only_contender"
        ),
    )


def run_source(instance: PlaybillInstance, root: Path) -> str:
    """Run the world's Source once; the digest of the Capture it lands."""

    from tests.test_procedures.test_procedure_source_runs import _run

    state, _invoker = _run(instance, root)
    assert state.status == "succeeded", state.terminal
    digest = state.source_observations[0].capture_digest
    assert digest is not None
    return digest


def write(instance: PlaybillInstance, *changes: dict[str, Any], because: str) -> WriteOutcome:
    request = WriteRequest.model_validate({"because": because, "changes": list(changes)})
    return service_playbill_write(instance, request=request, caller=caller())


def cite(instance: PlaybillInstance, capture: str, value: str) -> WriteOutcome:
    """``set`` the severity of wi-1 on one Capture."""

    return write(
        instance,
        {
            "op": "set",
            "subject": SUBJECT,
            "field": "severity",
            "value": value,
            "evidence": {"kind": "capture", "capture": capture},
        },
        because="The scanner reported it.",
    )


def erasure_world(tmp_path: Path, *, erasure: bool = True) -> ErasureWorld:
    from tests.test_procedures.test_procedure_source_runs import _world

    contract = capture_contract(erasure=erasure)
    instance, owner, _procedure, root, _policy = _world(tmp_path, contract=contract)
    seed_write_vocabulary(instance)
    inspection = submit_member_candidate(
        instance,
        members={claim_type_path(FIELD): render_claim_type(_severity_type(contract))},
        actor_id="owner",
        proposal_name="severity-field",
        proposal_family="claim-type",
        timestamp="2026-09-12T11:45:00.000000Z",
    )
    accept_proposal(instance, owner, inspection)
    capture = run_source(instance, root)
    outcome = cite(instance, capture, "high")
    assert outcome.refusal is None, outcome
    claim = outcome.changes[0].claim
    assert claim is not None
    envelope = parse_capture_envelope(instance.body_store().read(capture, access=READ))
    assert envelope.commitment.materialization == "cas"
    return ErasureWorld(
        instance=instance,
        owner=owner,
        root=root,
        contract=contract,
        capture=capture,
        body=envelope.commitment.digest,
        claim=claim,
    )


def rebind(instance: PlaybillInstance, claim: str, capture: str) -> WriteOutcome:
    """Move ``claim``'s citation onto ``capture`` (same contract); the value stays."""

    return write(
        instance,
        {"op": "rebind", "target": claim, "evidence": {"kind": "capture", "capture": capture}},
        because="Re-scanned after the token was rotated.",
    )


def rescan(world: ErasureWorld) -> str:
    """Observe the source again with different bytes: a fresh Capture, a fresh body."""

    (world.root / "data/osv-advisory.json").write_text(
        '{"advisory_id":"OSV-2026-0001","rescanned":true,"severity":"high"}', encoding="utf-8"
    )
    return run_source(world.instance, world.root)


__all__ = [
    "ErasureWorld",
    "FIELD",
    "SUBJECT",
    "cite",
    "erasure_world",
    "rebind",
    "rescan",
    "run_source",
    "write",
]

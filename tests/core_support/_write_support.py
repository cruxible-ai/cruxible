"""An accepted vocabulary for the write verbs: one kind, a field of every shape.

``project.work_item`` carries:

- ``status``: an enum, single-valued, one role (``observation``);
- ``priority``: a string, single-valued, two roles;
- ``title``: a string, single-valued;
- ``governs``: another work item, many-valued;
- ``labels``: a string, many-valued, that only captured evidence under ``repo.reports`` backs;
- ``ruling``: exact content, single-valued, one role (``normative``);
- ``measured``: an integer that only captured evidence under ``repo.reports`` backs.

Everything is accepted through the ordinary proposal and activation path, so the
verbs read accepted state rather than a fixture. ``self_approval`` chooses the
approval policy: with it the writer may activate its own proposal; without it
an independent approver (``reviewer``) must sign first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    ApprovalPolicy,
    render_approval_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import WriteCaller
from tests.core_support._support import initialize_local

KIND = "project.work_item"
SUBJECTS = ("wi-1", "wi-2", "wi-3")
REPORTS = foreign_source_capture_contract("repo.reports")
OWNER = AuthenticatedActor(actor_id="owner")


def _rule(
    rule_id: str, roles: tuple[str, ...], contract_digest: str
) -> ClaimEvidenceAdmissionRuleV1:
    return ClaimEvidenceAdmissionRuleV1(
        rule_id=rule_id,
        claim_roles=roles,  # type: ignore[arg-type]
        capture_contract_digests=(contract_digest,),
        evidence_kinds=("self_asserted",),
        admission="direct",
        subject_binding="exact_claim_subject",
    )


def _claim_type(
    field: str,
    *,
    object_kind: str = "literal",
    literal_schema: dict[str, object] | None = None,
    cardinality: str = "one",
    roles: tuple[str, ...] = ("observation",),
    object_kinds: tuple[str, ...] = (),
    captured_only: bool = False,
) -> ClaimType:
    predicate = f"{KIND}.{field}"
    contract = REPORTS if captured_only else COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=(KIND,),
        object_kind=object_kind,  # type: ignore[arg-type]
        literal_schema=literal_schema,
        allowed_object_subject_kinds=object_kinds,
        cardinality=cardinality,  # type: ignore[arg-type]
        permitted_roles=roles,  # type: ignore[arg-type]
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(_rule("source", roles, capture_contract_digest(contract).tagged),)
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality=cardinality,  # type: ignore[arg-type]
            eligible_verdicts=("supported",),
            selector="all" if cardinality == "many" else "only_contender",
        ),
    )


CLAIM_TYPES = (
    _claim_type("status", literal_schema={"enum": ["blocked", "done", "ready"], "type": "string"}),
    _claim_type(
        "priority",
        literal_schema={"type": "string"},
        roles=("normative", "observation"),
    ),
    _claim_type("title", literal_schema={"type": "string"}),
    _claim_type(
        "governs",
        object_kind="subject",
        cardinality="many",
        roles=("normative",),
        object_kinds=(KIND,),
    ),
    _claim_type("ruling", object_kind="exact_content", roles=("normative",)),
    _claim_type(
        "labels", literal_schema={"type": "string"}, cardinality="many", captured_only=True
    ),
    _claim_type("measured", literal_schema={"type": "integer"}, captured_only=True),
)


def _shell(subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{KIND}/{subject_id}"),
        subject_kind=KIND,
        subject_id=subject_id,
    )


def seed_write_vocabulary(instance: PlaybillInstance, *, actor_id: str = "owner") -> None:
    """Accept the write vocabulary and three work items into ``instance``.

    The genesis approval policy lets the proposer activate its own proposal, so
    this is one ordinary proposal and one activation.
    """

    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    for subject_id in SUBJECTS:
        tree[subject_path(KIND, subject_id)] = render_subject(_shell(subject_id))
    for claim_type in CLAIM_TYPES:
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    for contract in (COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, REPORTS):
        tree[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/write-seed",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-09-29T11:59:00.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by=actor_id
    )
    assert receipt.status == "accepted"


def seed_write_surface(
    tmp_path: Path, *, self_approval: bool = True
) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    """A local instance holding the write vocabulary and the chosen approval policy."""

    instance, owner = initialize_local(tmp_path)
    seed_write_vocabulary(instance)
    if not self_approval:
        # The genesis policy lets a writer activate its own proposal; tightening
        # it is its own governed change.
        tightening = instance.proposal_service().submit(
            actor=OWNER,
            request=ProposalAdmissionRequest(
                target_ref="refs/proposals/owner/write-seed-policy",
                proposed_base_oid=instance.accepted_coordinate().git_oid,
            ),
            candidate_tree={
                **instance.tree_at(instance.accepted_coordinate().git_oid),
                APPROVAL_POLICY_PATH: render_approval_policy(
                    ApprovalPolicy(mode="independent_approval_required")
                ),
            },
            timestamp="2026-09-29T11:59:30.000000Z",
        )
        assert tightening.candidate is not None, tightening.evaluation
        receipt = service_activate_playbill_proposal(
            instance, proposal_id=tightening.admission.proposal_id, activated_by="owner"
        )
        assert receipt.status == "accepted"
    return instance, owner


_REPORTS_CATALOG = """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: repo.reports
    locator: reports.md
    document_id: reports
    document_kind: note
    title: Reports
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:reports]
"""


def report_evidence(workspace: Path, anchor: str) -> dict[str, Any]:
    """File evidence under repo.reports, observed on the writer's side."""

    from cruxible_client.authoring.write_evidence import observe_evidence
    from cruxible_client.contracts.write import FileEvidence

    (workspace / ".playbill").mkdir(parents=True, exist_ok=True)
    (workspace / ".playbill" / "sources.yaml").write_text(_REPORTS_CATALOG, encoding="utf-8")
    reports = workspace / "reports.md"
    text = reports.read_text(encoding="utf-8") if reports.exists() else "# Reports\n"
    reports.write_text(text + f"\n{anchor}\n", encoding="utf-8")
    observed = observe_evidence(FileEvidence(file=f"reports.md#{anchor}"), workspace=workspace)
    assert observed is not None
    return observed.model_dump(mode="json")


def cited_captures(instance: PlaybillInstance, claim: str) -> set[str]:
    """The digests of the Captures an accepted Claim cites (a revision keeps its predecessor's)."""

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        rows = projection.typed.connection.execute(
            "SELECT capture_digest FROM citation_uses WHERE owner_kind='Claim' AND owner_key=?",
            (f"Claim:{claim}",),
        ).fetchall()
    return {str(row[0]) for row in rows}


def caller(*, may_activate: bool = True) -> WriteCaller:
    return WriteCaller(actor=OWNER, may_activate=may_activate)


__all__ = [
    "CLAIM_TYPES",
    "KIND",
    "OWNER",
    "REPORTS",
    "SUBJECTS",
    "caller",
    "cited_captures",
    "report_evidence",
    "seed_write_surface",
    "seed_write_vocabulary",
]

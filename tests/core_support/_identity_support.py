"""An accepted vocabulary with declared canonical forms (ClaimType v8) and aliases.

``inv.package`` carries:

- ``code``: a string, single-valued, canonical ``nfkc, trim, collapse_whitespace, casefold``;
- ``state``: an enum (``closed``, ``open``), single-valued, canonical ``trim, casefold``;
- ``tags``: a string, many-valued, canonical ``trim, casefold``;
- ``plain``: a string, single-valued, with no canonical form (ClaimType v7);
- ``depends_on``: another package, many-valued.

``semantic.alias`` is the builtin alias ClaimType, accepted from its seed bytes.
Every artifact is accepted through the ordinary proposal and activation path.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    builtin_contract_for_write,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from tests.core_support._subject_kind_support import declare_created_kinds
from tests.core_support._support import initialize_local

KIND = "inv.package"
SUBJECTS = ("alpha", "beta")
OWNER = AuthenticatedActor(actor_id="owner")
CODE_FORM = ("nfkc", "trim", "collapse_whitespace", "casefold")
STATE_FORM = ("trim", "casefold")


def identity_claim_type(
    field: str,
    *,
    canonical_form: tuple[str, ...] | None,
    object_kind: str = "literal",
    literal_schema: dict[str, object] | None = None,
    cardinality: str = "one",
    object_kinds: tuple[str, ...] = (),
    predicate: str | None = None,
    subject_kinds: tuple[str, ...] = (KIND,),
) -> ClaimType:
    """A ClaimType v8 (v7 when ``canonical_form`` is None) admitting self evidence."""

    name = predicate or f"{KIND}.{field}"
    roles = ("observation",)
    return ClaimType.model_validate(
        {
            "artifact_format": (
                "playbill-claim-type-v7" if canonical_form is None else "playbill-claim-type-v8"
            ),
            "identity": ArtifactIdentity(kind="ClaimType", name=name),
            "predicate": name,
            "allowed_subject_kinds": subject_kinds,
            "object_kind": object_kind,
            "literal_schema": literal_schema,
            "allowed_object_subject_kinds": object_kinds,
            "cardinality": cardinality,
            "permitted_roles": roles,
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="self",
                        claim_roles=roles,
                        capture_contracts=(
                            ArtifactRef(
                                role=CAPTURE_CONTRACT_REF_ROLE,
                                target=COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity,
                            ),
                        ),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            ),
            "admission_policy": ClaimAdmissionPolicy(),
            "resolution_policy": ClaimResolutionPolicy(
                cardinality=cardinality,  # type: ignore[arg-type]
                eligible_verdicts=("supported",),
                selector="all" if cardinality == "many" else "only_contender",
            ),
            "evidence_requirement": "self",
            "revision_evidence": "replace",
            "canonical_form": canonical_form,
        }
    )


IDENTITY_CLAIM_TYPES = (
    identity_claim_type("code", canonical_form=CODE_FORM, literal_schema={"type": "string"}),
    identity_claim_type(
        "state",
        canonical_form=STATE_FORM,
        literal_schema={"enum": ["closed", "open"], "type": "string"},
    ),
    identity_claim_type(
        "tags", canonical_form=STATE_FORM, literal_schema={"type": "string"}, cardinality="many"
    ),
    identity_claim_type("plain", canonical_form=None, literal_schema={"type": "string"}),
    identity_claim_type(
        "depends_on",
        canonical_form=None,
        object_kind="subject",
        cardinality="many",
        object_kinds=(KIND,),
    ),
)


def shell(subject_id: str, kind: str = KIND) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{kind}/{subject_id}"),
        subject_kind=kind,
        subject_id=subject_id,
    )


def accept_tree(
    instance: PlaybillInstance, updates: dict[str, bytes], *, name: str, actor_id: str = "owner"
) -> None:
    """Accept ``updates`` over the head through one ordinary proposal and activation."""

    base = instance.accepted_coordinate()
    accepted = instance.tree_at(base.git_oid)
    tree = {**accepted, **declare_created_kinds(accepted, dict(updates), instance=instance)}
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/{name}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-10-09T11:59:00.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by=actor_id
    )
    assert receipt.status == "accepted"


def retire_subject(instance: PlaybillInstance, path: str, *, name: str) -> None:
    """Retire one Subject; core retires its key Claims with it in the same change."""

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subjects import parse_subject, subject_digest

    shell = parse_subject(instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path)
    retired = shell.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(shell).tagged
            )
        }
    )
    accept_tree(instance, {path: render_subject(retired)}, name=name)


def seed_identity_vocabulary(instance: PlaybillInstance) -> None:
    updates: dict[str, bytes] = {
        subject_path(KIND, subject_id): render_subject(shell(subject_id)) for subject_id in SUBJECTS
    }
    for claim_type in IDENTITY_CLAIM_TYPES:
        updates[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    contract = builtin_contract_for_write(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, None)
    updates[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
    accept_tree(instance, updates, name="identity-seed")


def seed_identity_surface(tmp_path: Path) -> PlaybillInstance:
    instance, _owner = initialize_local(tmp_path)
    seed_identity_vocabulary(instance)
    return instance


__all__ = [
    "CODE_FORM",
    "IDENTITY_CLAIM_TYPES",
    "KIND",
    "OWNER",
    "STATE_FORM",
    "SUBJECTS",
    "accept_tree",
    "retire_subject",
    "identity_claim_type",
    "seed_identity_surface",
    "seed_identity_vocabulary",
    "shell",
]

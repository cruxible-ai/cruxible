"""PC-A2 final ClaimType v1 canonical model tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
)
from cruxible_client.contracts.authoring_profiles import (
    CLAIM_TYPE_AUTHORING_PROFILES,
    AuthoringProfileError,
    AuthorityProfileParametersV1,
    ClaimTypeProfileInputV1,
    expand_claim_type_profile,
    verify_claim_type_expansion_evidence,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import (
    AcceptedClaimType,
    ClaimEvidenceFreshnessV1,
    ClaimFreshnessDurationV1,
    ClaimType,
    ClaimTypeFormatError,
    ClaimTypeFreshnessHorizonInvalid,
    claim_type_digest,
    claim_type_path,
    evaluate_claim_type_law,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.compiler import current_compiler_coordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.review import service_review_playbill_proposal
from tests.test_playbill._support import initialize_local


def literal_claim_type() -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "enum": ["blocked", "done", "ready"],
            "type": "string",
        },
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        authority=ArtifactAuthority(
            propose_roles=("owner",),
            approve_roles=("owner",),
        ),
    )


def test_claim_type_parse_render_digest_and_path_match_frozen_golden() -> None:
    fixture_path = Path(__file__).parents[1] / "goldens" / "playbill" / "claim-type-v1.json"
    fixture = json.loads(fixture_path.read_bytes())
    claim_type = ClaimType.model_validate(fixture["claim_type"])

    assert claim_type == literal_claim_type()
    assert claim_type_path(claim_type.predicate) == "claim-types/project.work_item/status.yaml"
    rendered = render_claim_type(claim_type)
    assert rendered.decode() == fixture["canonical_wire"]
    assert claim_type_digest(claim_type).tagged == fixture["artifact_digest"]
    assert (
        parse_claim_type(
            rendered,
            path="claim-types/project.work_item/status.yaml",
        )
        == claim_type
    )


def test_claim_type_combines_structure_and_all_three_policy_surfaces() -> None:
    claim_type = literal_claim_type()
    assert claim_type.structure.predicate == claim_type.predicate
    assert claim_type.evidence_admission_policy.tag == (
        "playbill-claim-evidence-admission-policy-v1"
    )
    assert claim_type.admission_policy.tag == "playbill-claim-admission-policy-v1"
    assert claim_type.resolution_policy.tag == "playbill-claim-resolution-policy-v1"
    assert not hasattr(claim_type.admission_policy, "backing_requirements")


def test_claim_type_refuses_identity_path_cardinality_and_policy_tag_drift() -> None:
    claim_type = literal_claim_type()
    with pytest.raises(ValidationError, match="identity"):
        claim_type.model_copy(
            update={"identity": ArtifactIdentity(kind="ClaimType", name="project.other")}
        ).__class__.model_validate(
            {
                **claim_type.model_dump(mode="json"),
                "identity": {"kind": "ClaimType", "name": "project.other"},
            }
        )
    with pytest.raises(ValidationError, match="cardinality"):
        ClaimType.model_validate(
            {
                **claim_type.model_dump(mode="json"),
                "resolution_policy": {
                    **claim_type.resolution_policy.model_dump(mode="json"),
                    "cardinality": "many",
                    "selector": "all",
                },
            }
        )
    with pytest.raises(ClaimTypeFormatError, match="identity/path"):
        parse_claim_type(render_claim_type(claim_type), path="claim-types/project/other.yaml")
    payload = claim_type.model_dump(mode="json")
    payload["evidence_admission_policy"]["tag"] = "unknown"
    with pytest.raises(ValidationError):
        ClaimType.model_validate(payload)


def test_claim_type_successor_requires_exact_predecessor_digest_shape() -> None:
    claim_type = literal_claim_type()
    successor = claim_type.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(claim_type).tagged)
        }
    )
    assert successor.lifecycle.predecessor_digest == claim_type_digest(claim_type).tagged


def test_claim_type_authority_widens_only_and_requires_owner_approval(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    original = literal_claim_type()
    predecessor = AcceptedClaimType(
        path=claim_type_path(original.predicate),
        claim_type=original,
        artifact_digest=claim_type_digest(original).tagged,
    )
    widened = original.model_copy(
        update={
            "authority": ArtifactAuthority(
                propose_roles=("owner",),
                approve_roles=("owner", "reviewer"),
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(original).tagged),
        }
    )

    accepted = evaluate_claim_type_law(
        widened,
        path=predecessor.path,
        principals=instance.accepted_history()[-1].principals,
        actor_id="owner",
        predecessor=predecessor,
    )
    assert accepted.verdict == "accepted"
    assert accepted.approval_scope == ("owner",)

    narrowed = original.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(widened).tagged)
        }
    )
    refused = evaluate_claim_type_law(
        narrowed,
        path=predecessor.path,
        principals=instance.accepted_history()[-1].principals,
        actor_id="owner",
        predecessor=AcceptedClaimType(
            path=predecessor.path,
            claim_type=widened,
            artifact_digest=claim_type_digest(widened).tagged,
        ),
    )
    assert refused.verdict == "refused"
    assert refused.diagnostics[0].code == "playbill.claim_type.authority_change_unsupported"


def test_claim_type_v3_adds_only_a_positive_freshness_horizon() -> None:
    original = literal_claim_type()
    successor = ClaimType.model_validate(
        {
            **original.model_dump(mode="json"),
            "artifact_format": "playbill-claim-type-v3",
            "evidence_freshness": ClaimEvidenceFreshnessV1(
                stale_after=ClaimFreshnessDurationV1(microseconds=30_000_000)
            ).model_dump(mode="json"),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_type_digest(original).tagged
            ).model_dump(mode="json"),
        }
    )

    rendered = render_claim_type(successor)
    assert parse_claim_type(rendered, path=claim_type_path(successor.predicate)) == successor
    assert claim_type_digest(successor).tagged != claim_type_digest(original).tagged
    assert successor.structure == original.structure
    assert render_claim_type(original) == render_claim_type(literal_claim_type())


def test_claim_type_v1_and_v3_preserve_the_exact_precut_wire_and_digest() -> None:
    original = literal_claim_type()
    successor = ClaimType.model_validate(
        {
            **original.model_dump(mode="json"),
            "artifact_format": "playbill-claim-type-v3",
            "evidence_freshness": ClaimEvidenceFreshnessV1(
                stale_after=ClaimFreshnessDurationV1(microseconds=30_000_000)
            ).model_dump(mode="json"),
        }
    )

    assert hashlib.sha256(render_claim_type(original)).hexdigest() == (
        "e0fa400a6ad96e7deb5d8b64c98984d0b392af24de4dfd42dfef784240b6832b"
    )
    assert claim_type_digest(original).tagged == (
        "sha256:f1ed3e5b2833246ec71275ca212677a6585c0a4cad514b3436d9b29db91f2d23"
    )
    assert hashlib.sha256(render_claim_type(successor)).hexdigest() == (
        "2c1de8eee8e43a1e18e4e1977228ea1be45ae499b33bbe219c38e31901b472cf"
    )
    assert claim_type_digest(successor).tagged == (
        "sha256:08790f07585b846742d2ed3148a83b0380cda241b3fe49c6cf1ccbadb4961f67"
    )
    assert b'"subject_scope":null' in render_claim_type(successor)
    assert b'"slot_policy":null' in render_claim_type(successor)
    assert b'"subject_scope"' not in render_claim_type(original)
    assert b'"slot_policy"' not in render_claim_type(original)


@pytest.mark.parametrize("field", ["subject_scope", "slot_policy"])
def test_claim_type_rejects_removed_non_null_profile_fields(field: str) -> None:
    payload = literal_claim_type().model_dump(mode="json")
    payload[field] = {}

    with pytest.raises(ValidationError, match=field):
        ClaimType.model_validate(payload)


def test_claim_type_rejects_the_removed_v2_envelope() -> None:
    payload = literal_claim_type().model_dump(mode="json")
    payload["artifact_format"] = "playbill-claim-type-v2"

    with pytest.raises(ValidationError, match="artifact_format"):
        ClaimType.model_validate(payload)
    with pytest.raises(ClaimTypeFormatError, match="unsupported ClaimType artifact format"):
        parse_claim_type(
            canonical_bytes(payload) + b"\n",
            path=claim_type_path(literal_claim_type().predicate),
        )


def test_claim_type_v3_refuses_missing_or_zero_freshness_horizon() -> None:
    payload = literal_claim_type().model_dump(mode="json")
    payload["artifact_format"] = "playbill-claim-type-v3"
    payload["evidence_freshness"] = {
        "tag": "playbill-claim-evidence-freshness-v1",
        "stale_after": {"tag": "playbill-duration-v1", "microseconds": 0},
    }
    malformed = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    with pytest.raises(ClaimTypeFreshnessHorizonInvalid):
        parse_claim_type(malformed, path=claim_type_path(literal_claim_type().predicate))


def test_claim_type_v3_horizon_proposal_uses_the_frozen_refusal_code(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    payload = literal_claim_type().model_dump(mode="json")
    payload["artifact_format"] = "playbill-claim-type-v3"
    payload["evidence_freshness"] = {
        "tag": "playbill-claim-evidence-freshness-v1",
        "stale_after": {"tag": "playbill-duration-v1", "microseconds": 0},
    }
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    tree[claim_type_path(literal_claim_type().predicate)] = canonical_bytes(payload) + b"\n"

    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/invalid-freshness",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-24T21:00:00.000000Z",
    )

    assert result.evaluation.diagnostics[0].code == (
        "playbill.claim_type.freshness_horizon_invalid"
    )


def test_compact_ordinary_profile_and_expert_input_expand_to_identical_bytes() -> None:
    direct = literal_claim_type()
    profile = next(
        item
        for item in CLAIM_TYPE_AUTHORING_PROFILES
        if item.profile_id == "ordinary-project-fact-v1"
    )
    expanded = expand_claim_type_profile(
        ClaimTypeProfileInputV1(
            profile_id=profile.profile_id,
            profile_digest=profile.profile_digest,
            authoring_source_digest="sha256:" + "61" * 32,
            compiler_digest="sha256:" + "62" * 32,
            structure=direct.structure,
            authority_parameters=AuthorityProfileParametersV1(
                propose_roles=("owner",),
                approve_roles=("owner",),
            ),
        )
    )

    assert render_claim_type(expanded.claim_type) == render_claim_type(direct)
    assert expanded.evidence.expanded_artifact_digest == claim_type_digest(direct).tagged
    assert expanded.evidence.profile_digest == profile.profile_digest
    assert expanded.evidence.authoring_source_digest == "sha256:" + "61" * 32
    assert expanded.evidence.compiler_digest == "sha256:" + "62" * 32


def test_profile_expansion_refuses_unknown_missing_authority_and_open_overrides() -> None:
    direct = literal_claim_type()
    profile = next(
        item
        for item in CLAIM_TYPE_AUTHORING_PROFILES
        if item.profile_id == "ordinary-project-fact-v1"
    )
    values = {
        "profile_id": profile.profile_id,
        "profile_digest": profile.profile_digest,
        "authoring_source_digest": "sha256:" + "63" * 32,
        "compiler_digest": "sha256:" + "64" * 32,
        "structure": direct.structure,
        "authority_parameters": AuthorityProfileParametersV1(
            propose_roles=("owner",),
            approve_roles=("owner",),
        ),
    }
    with pytest.raises(AuthoringProfileError, match="unknown"):
        expand_claim_type_profile(
            ClaimTypeProfileInputV1(**{**values, "profile_id": "invented-profile-v1"})
        )
    with pytest.raises(AuthoringProfileError, match="authority"):
        expand_claim_type_profile(
            ClaimTypeProfileInputV1(**{**values, "authority_parameters": None})
        )
    with pytest.raises(AuthoringProfileError, match="override"):
        expand_claim_type_profile(
            ClaimTypeProfileInputV1(
                **values,
                overrides={"selector": "authority_rule"},
            )
        )


def test_profile_evidence_refuses_forged_expansion_or_override_digest() -> None:
    direct = literal_claim_type()
    profile = next(
        item
        for item in CLAIM_TYPE_AUTHORING_PROFILES
        if item.profile_id == "ordinary-project-fact-v1"
    )
    expansion = expand_claim_type_profile(
        ClaimTypeProfileInputV1(
            profile_id=profile.profile_id,
            profile_digest=profile.profile_digest,
            authoring_source_digest="sha256:" + "65" * 32,
            compiler_digest="sha256:" + "66" * 32,
            structure=direct.structure,
            authority_parameters=AuthorityProfileParametersV1(
                propose_roles=("owner",),
                approve_roles=("owner",),
            ),
        )
    )
    forged_output = expansion.evidence.model_copy(
        update={"expanded_output_digest": "sha256:" + "00" * 32}
    )
    with pytest.raises(AuthoringProfileError, match="output digest"):
        verify_claim_type_expansion_evidence(
            forged_output,
            claim_type=direct,
            compiler_digest=expansion.evidence.compiler_digest,
        )
    with pytest.raises(ValidationError, match="overrides digest"):
        expansion.evidence.__class__.model_validate(
            {
                **expansion.evidence.model_dump(mode="json"),
                "overrides": {"conflict_result": "refuse"},
            }
        )


def test_profile_seed_list_is_exact_and_digest_pinned() -> None:
    assert tuple(item.profile_id for item in CLAIM_TYPE_AUTHORING_PROFILES) == (
        "append-only-source-observation-v1",
        "governed-single-valued-status-transition-v1",
        "ordinary-project-fact-v1",
        "policy-owner-normative-claim-v1",
        "replay-verifiable-derivation-v1",
        "source-backed-scientific-result-v1",
    )
    assert all(item.profile_digest.startswith("sha256:") for item in CLAIM_TYPE_AUTHORING_PROFILES)


def test_profile_evidence_and_complete_expansion_are_visible_in_atomic_review(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    direct = literal_claim_type()
    profile = next(
        item
        for item in CLAIM_TYPE_AUTHORING_PROFILES
        if item.profile_id == "ordinary-project-fact-v1"
    )
    expansion = expand_claim_type_profile(
        ClaimTypeProfileInputV1(
            profile_id=profile.profile_id,
            profile_digest=profile.profile_digest,
            authoring_source_digest="sha256:" + "71" * 32,
            compiler_digest=current_compiler_coordinate().rule_digest,
            structure=direct.structure,
            authority_parameters=AuthorityProfileParametersV1(
                propose_roles=("owner",),
                approve_roles=("owner",),
            ),
        )
    )
    base = instance.accepted_coordinate()
    path = claim_type_path(direct.predicate)
    proposal = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/profile-review",
            proposed_base_oid=base.git_oid,
            claim_type_expansions=(expansion.evidence,),
        ),
        candidate_tree={**instance.tree_at(base.git_oid), path: render_claim_type(direct)},
        timestamp="2026-08-16T17:00:00.000000Z",
    )
    assert proposal.candidate is not None

    review = service_review_playbill_proposal(
        instance,
        proposal_id=proposal.admission.proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=False),
    )
    assert len(review.members) == 1
    result = review.members[0].law_evidence["result"]
    assert result["expanded_claim_type"] == direct.model_dump(mode="json")
    assert result["authoring_expansion"] == expansion.evidence.model_dump(mode="json")
    assert review.members[0].closure_role == "authored"

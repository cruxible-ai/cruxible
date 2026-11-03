"""`get` by a ClaimType's dotted tail: live vocabulary only, exact names any lifecycle.

A short name (``status``, ``work_item.status``, ``ClaimType:status``) is
shorthand for discovering the live vocabulary: it matches the live predicates it
is the dotted tail of, through the reversed-name index. An exact predicate, bare
or ``ClaimType:``, reads the definition whatever its lifecycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.errors import ReadRefusalError
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicy
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.discovery.get import service_playbill_get
from tests.core_support import _write_support as support
from tests.core_support._support import initialize_local
from tests.support.claim_types import claim_type as build_claim_type

KIND = support.KIND
_READER = BodyAccessContext(principal_id="tails-test", can_read_body=False)


def named_claim_type(predicate: str):  # type: ignore[no-untyped-def]
    """A string ClaimType about work items under any dotted predicate."""

    return build_claim_type(
        predicate=predicate,
        allowed_subject_kinds=(KIND,),
        value_type={"type": "string"},
        cardinality="one",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(support._rule("source", ("observation",), support.SELF_SOURCE.identity.name),)
        ),
    )


def retire_claim_types(instance, *predicates: str) -> None:  # type: ignore[no-untyped-def]
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    for predicate in predicates:
        path = claim_type_path(predicate)
        live = parse_claim_type(tree[path], path=path)
        tree[path] = render_claim_type(
            live.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(
                        state="retired", predecessor_digest=claim_type_digest(live).tagged
                    )
                }
            )
        )
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/retire-types", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-29T12:40:00.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt


def get(instance, ref: str) -> str:  # type: ignore[no-untyped-def]
    return service_playbill_get(instance, request=GetRequest(ref=ref), access=_READER).ref


@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, _ = initialize_local(tmp_path)
    support.seed_write_vocabulary(
        instance,
        claim_types=(*support.CLAIM_TYPES, named_claim_type("project.archive.measured")),
    )
    retire_claim_types(instance, f"{KIND}.measured", f"{KIND}.ruling")
    return instance


def test_a_retired_only_tail_refuses_while_its_exact_name_reads(world) -> None:  # type: ignore[no-untyped-def]
    for ref in ("ruling", "work_item.ruling", "ClaimType:ruling"):
        with pytest.raises(ReadRefusalError) as refused:
            get(world, ref)
        assert refused.value.error_code == "cruxible.get.ref_not_found", ref
    assert get(world, f"{KIND}.ruling") == f"ClaimType:{KIND}.ruling"
    assert get(world, f"ClaimType:{KIND}.ruling") == f"ClaimType:{KIND}.ruling"


def test_a_tail_shared_by_live_and_retired_names_the_live_one(world) -> None:  # type: ignore[no-untyped-def]
    assert get(world, "measured") == "ClaimType:project.archive.measured"
    assert get(world, "ClaimType:measured") == "ClaimType:project.archive.measured"
    # The retired definition is still read by its exact predicate.
    assert get(world, f"{KIND}.measured") == f"ClaimType:{KIND}.measured"
    # Its own multi-segment tail names no live predicate.
    with pytest.raises(ReadRefusalError) as refused:
        get(world, "work_item.measured")
    assert refused.value.error_code == "cruxible.get.ref_not_found"

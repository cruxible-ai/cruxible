"""Seed accepted exact-content Claims (rulings, method laws) for read-verb tests.

The ClaimType ``project.work_item.status`` is re-declared with
``object_kind="exact_content"``, and each Claim's self-source body is exactly
its content, so the Claim's own Capture commits the bytes its object names.
Everything travels through the AuthoringIntent coordinator and accepted
activation, so the reads see accepted state rather than a fixture.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringExactContentObjectV1,
    SelfSourceBodyV1,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from tests.core_support._knowledge_loop_support import activate
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import _seed_claim_surface, _self_source_payload
from tests.test_claims.test_claims import _claim_type

EXACT_PREDICATE = "project.work_item.status"
EXACT_KIND = "project.work_item"


@dataclass(frozen=True)
class ExactClaim:
    subject: str
    claim_id: str
    content: bytes

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()


def _shell(subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{EXACT_KIND}/{subject_id}"),
        subject_kind=EXACT_KIND,
        subject_id=subject_id,
    )


def seed_exact_content(
    tmp_path: Path, contents: Mapping[str, bytes]
) -> tuple[PlaybillInstance, dict[str, ExactClaim]]:
    """Accept one exact-content status Claim per ``subject_id -> content``.

    ``wi-42`` is the fixture's own Subject; every other id is added to it.
    """

    instance, owner = initialize_local(tmp_path)
    return instance, seed_exact_content_into(instance, owner, contents)


def seed_exact_content_into(
    instance: PlaybillInstance, owner: GeneratedKeyMaterial, contents: Mapping[str, bytes]
) -> dict[str, ExactClaim]:
    """Accept the exact-content surface and one Claim per content into ``instance``."""

    _seed_claim_surface(
        instance,
        owner,
        claim_type_override=_claim_type().model_copy(
            update={"object_kind": "exact_content", "literal_schema": None}
        ),
        additional_subjects=tuple(_shell(item) for item in contents if item != "wi-42"),
    )
    actor = AuthenticatedActor(actor_id="owner")
    template = _self_source_payload()
    seeded: dict[str, ExactClaim] = {}
    for index, (subject_id, content) in enumerate(contents.items(), start=1):
        claim_id = "CLM-" + f"{index:x}" * 32
        coordinator = AuthoringIntentCoordinator(
            instance=instance,
            store=AuthoringIntentCoordinator.for_instance(instance).store,
            claim_id_factory=lambda claim_id=claim_id: claim_id,
        )
        encoded = base64.b64encode(content).decode("ascii")
        payload = template.model_copy(
            update={
                "source": SelfSourceBodyV1(content_base64=encoded),
                "statement": template.statement.model_copy(
                    update={
                        "subject": SemanticAddress.whole_artifact(
                            f"subjects/{EXACT_KIND}/{subject_id}.json"
                        ),
                        "object": AuthoringExactContentObjectV1(content_base64=encoded),
                    }
                ),
            }
        )
        created = coordinator.create(
            actor=actor, payload=payload, canonical_timestamp="2026-08-16T20:00:00.000000Z"
        )
        submitted = coordinator.submit(created.intent.intent_id, actor=actor)
        if submitted.status.proposal_id is None:
            raise AssertionError(submitted.intent.last_preflight)
        inspection = service_inspect_playbill_proposal(
            instance, proposal_id=submitted.status.proposal_id
        )
        activate(instance, owner, SimpleNamespace(proposal=inspection))
        seeded[subject_id] = ExactClaim(
            subject=f"{EXACT_KIND}/{subject_id}", claim_id=claim_id, content=content
        )
    return seeded


__all__ = [
    "EXACT_KIND",
    "EXACT_PREDICATE",
    "ExactClaim",
    "seed_exact_content",
    "seed_exact_content_into",
]

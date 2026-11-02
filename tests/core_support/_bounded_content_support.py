"""Seed accepted bounded-string Claims (rulings, method laws) for read-verb tests.

The ClaimType ``project.work_item.status`` is re-declared with
``value_type={"type": "string", "maxLength": 256}``, and each Claim's self-source body is exactly
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
    AuthoringExistingClaimDisposition,
    SelfSourceBody,
)
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from tests.core_support._knowledge_loop_support import activate
from tests.core_support._support import initialize_local
from tests.support.claim_types import claim_type_changes
from tests.test_authoring.test_authoring_preflight import _seed_claim_surface, _self_source_payload
from tests.test_claims.test_claims import _claim_type

VALUE_PREDICATE = "project.work_item.status"
VALUE_KIND = "project.work_item"


@dataclass(frozen=True)
class BoundedClaim:
    subject: str
    claim_id: str
    content: bytes

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()


def _shell(subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{VALUE_KIND}/{subject_id}"),
        subject_kind=VALUE_KIND,
        subject_id=subject_id,
    )


def seed_bounded_content(
    tmp_path: Path, contents: Mapping[str, bytes]
) -> tuple[PlaybillInstance, dict[str, BoundedClaim]]:
    """Accept one bounded-string status Claim per ``subject_id -> content``.

    ``wi-42`` is the fixture's own Subject; every other id is added to it.
    """

    instance, owner = initialize_local(tmp_path)
    return instance, seed_bounded_content_into(instance, owner, contents)


def seed_bounded_content_into(
    instance: PlaybillInstance, owner: GeneratedKeyMaterial, contents: Mapping[str, bytes]
) -> dict[str, BoundedClaim]:
    """Accept the bounded-string surface and one Claim per content into ``instance``."""

    _seed_claim_surface(
        instance,
        owner,
        claim_type_override=_claim_type().model_copy(
            update=claim_type_changes(value_type={"type": "string", "maxLength": 256})
        ),
        additional_subjects=tuple(_shell(item) for item in contents if item != "wi-42"),
    )
    seeded: dict[str, BoundedClaim] = {}
    for index, (subject_id, content) in enumerate(contents.items(), start=1):
        seeded[subject_id] = add_bounded_claim(
            instance, owner, subject_id, content, claim_id="CLM-" + f"{index:x}" * 32
        )
    return seeded


def add_bounded_claim(
    instance: PlaybillInstance,
    owner: GeneratedKeyMaterial,
    subject_id: str,
    content: bytes,
    *,
    claim_id: str,
    existing: tuple[str, ...] = (),
) -> BoundedClaim:
    """Accept one bounded-string status Claim; ``existing`` same-slot Claims stay untested."""

    actor = AuthenticatedActor(actor_id="owner")
    template = _self_source_payload()
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentCoordinator.for_instance(instance).store,
        claim_id_factory=lambda: claim_id,
    )
    encoded = base64.b64encode(content).decode("ascii")
    payload = template.model_copy(
        update={
            "source": SelfSourceBody(content_base64=encoded),
            "statement": template.statement.model_copy(
                update={
                    "subject": SemanticAddress.whole_artifact(
                        f"subjects/{VALUE_KIND}/{subject_id}.json"
                    ),
                    "object": LiteralClaimObject(value=content.decode("utf-8")),
                }
            ),
            "existing_claim_dispositions": tuple(
                AuthoringExistingClaimDisposition(claim_id=item, disposition="not_tested")
                for item in sorted(existing)
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
    return BoundedClaim(subject=f"{VALUE_KIND}/{subject_id}", claim_id=claim_id, content=content)


__all__ = [
    "add_bounded_claim",
    "VALUE_KIND",
    "VALUE_PREDICATE",
    "BoundedClaim",
    "seed_bounded_content",
    "seed_bounded_content_into",
]

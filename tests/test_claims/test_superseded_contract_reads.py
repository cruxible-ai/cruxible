"""A Claim keeps the contract version its evidence used, and still reads.

A compatible CaptureContract successor replaces the contract in the accepted
tree, but a Claim revised after it cites Captures under both versions. Reading
that Claim must resolve the superseded version from accepted history rather
than refuse as though the contract had vanished.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV1,
    WorkingAnchorWindow,
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.captures import render_capture_contract
from cruxible_client.contracts.claims import parse_claim
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.claims.claims import (
    service_explain_playbill_claim,
    service_get_playbill_claim,
)
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_claims.test_identity_evidence_rules import (
    CONTRACT_PATH,
    IDENTITY,
    ORIGINAL,
    SOURCE,
    _digest,
    _successor,
    _v6_type,
    _World,
)


def _observe(world: Any, text: bytes, *, revises: str | None = None) -> str:
    """The identity-rule world's observation, spelled with today's ``revises`` field."""

    digest = "sha256:" + hashlib.sha256(text).hexdigest()
    payload = ClaimAuthoringPayloadV1(
        statement=_self_source_payload().statement,
        rationale="The repository snapshot says the work is ready.",
        source=WorkingSelectionObservation(
            source_id=SOURCE,
            coordinate=WorkingDigestCoordinate(
                source_content_digest=digest, source_byte_length=len(text)
            ),
            selected_content_base64=base64.b64encode(text).decode("ascii"),
            selected_bytes_digest=digest,
            selector=WorkingAnchorWindow(
                anchor=text.decode("ascii"),
                start_byte=0,
                end_byte=len(text),
                observed_occurrence_count=1,
            ),
        ),
        citation_role="evidence",
        revises=revises,
    )
    intent = world.coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=payload,
        canonical_timestamp=world.timestamp(),
    ).intent
    lowered = lower_authoring(world.instance, intent=intent, actor_id="owner")
    path = next(p for p, _content in lowered.changed_members if p.startswith("claims/"))
    world.accept(
        dict(lowered.proposed_tree), name=f"get-observe-{len(text)}-{0 if revises is None else 1}"
    )
    return parse_claim(lowered.proposed_tree[path], path=path).identity.name


def test_a_claim_citing_a_superseded_contract_version_still_reads(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.seed(_v6_type())
    first = _observe(world, b"status: ready")
    improved = _successor(ORIGINAL)
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(improved)
    world.accept(tree, name="improve-contract")
    _observe(world, b"status: done", revises=first)

    view = service_get_playbill_claim(world.instance, identity=first)
    explanation = service_explain_playbill_claim(
        world.instance, identity=first, evaluation_time=datetime.now(UTC)
    )

    accounts = sorted(
        (item.capture_contract_identity, item.capture_contract_digest, item.status)
        for item in view.admission_accounts
    )
    assert accounts == sorted(
        [
            (IDENTITY.qualified, _digest(ORIGINAL), "admitted"),
            (IDENTITY.qualified, _digest(improved), "admitted"),
        ]
    )
    assert explanation.admission_accounts == view.admission_accounts

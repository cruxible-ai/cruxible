"""Playbill.get resolves references on the daemon, keeping the SDK's typed views."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import ClaimView, Playbill
from cruxible_client.authoring.sdk_types import ClaimRef, ClaimTypeRef, RefKind, SubjectRef
from cruxible_client.contracts import PlaybillClaimViewV2
from cruxible_client.contracts.get_reads import (
    PlaybillGetClaimTypeCardV1,
    PlaybillGetEvidenceV1,
    PlaybillGetSubjectCardV1,
)
from cruxible_client.errors import CoreError
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._knowledge_loop_support import PREDICATE, seed_claims_into
from tests.test_server.test_playbill_procedure_measurements import (  # noqa: F401
    owned_playbill_http,
)


def _sdk(client: TestClient, instance_id: str, tmp_path: Path) -> Playbill:
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / "sdk-workspace"
    workspace.mkdir()
    return Playbill._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 8, 16, 21, tzinfo=UTC),
    )


def test_get_resolves_strings_and_typed_refs_directly(
    owned_playbill_http: tuple[TestClient, str, Path],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, instance_id, key = owned_playbill_http
    instance = get_playbill_manager().get(instance_id)
    reviewer = instance._recovered.head.principals.require_active("reviewer")  # noqa: SLF001
    seed_claims_into(
        instance,
        GeneratedKeyMaterial(
            principal=reviewer, private_key_path=key, public_key_path=key.with_suffix(".pub")
        ),
    )
    pb = _sdk(client, instance_id, tmp_path)
    # get never routes through search any more.
    monkeypatch.setattr(pb, "_search", lambda **_kw: pytest.fail("get must not search"))

    subject = pb.get("project.work_item/wi-42")
    assert subject.kind is RefKind.SUBJECT and subject.identity == "project.work_item/wi-42"
    assert isinstance(subject.value, PlaybillGetSubjectCardV1)
    assert [(row.predicate, row.value) for row in subject.value.claims] == [("status", "ready")]
    assert isinstance(subject.ref, SubjectRef)
    # Summaries answer a compact coordinate; the SDK asks for the full one to pin.
    assert subject.coordinate == pb.coordinate

    by_leaf = pb.get("status")
    assert by_leaf.kind is RefKind.CLAIM_TYPE and by_leaf.identity == PREDICATE
    assert isinstance(by_leaf.value, PlaybillGetClaimTypeCardV1)
    assert pb.get(ClaimTypeRef(PREDICATE, pb.coordinate)).value == by_leaf.value

    # A Claim summary keeps the SDK's typed ClaimView, from a prefix or a ref.
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        (row,) = projection.typed.connection.execute(
            "SELECT identity FROM claims WHERE subject_path LIKE '%wi-42%'"
        ).fetchall()
    claim_id = str(row[0]).removeprefix("Claim:")
    card = pb.get(claim_id[:12])
    assert card.kind is RefKind.CLAIM and card.identity == claim_id
    assert isinstance(card.value, ClaimView)
    assert (card.value.value, card.value.verdict) == ("ready", "supported")
    assert isinstance(card.ref, ClaimRef)
    assert pb.get(ClaimRef(claim_id, pb.coordinate)).value == card.value
    # A subject resolving to a Claim only on the daemon still returns ClaimView.
    assert pb.get(f"claims/{claim_id[4:6]}/{claim_id}.json").value == card.value

    evidence = pb.get(claim_id, detail="evidence")
    assert isinstance(evidence.value, PlaybillGetEvidenceV1)
    proof = pb.get(claim_id, detail="proof")
    assert isinstance(proof.value, PlaybillClaimViewV2)

    with pytest.raises(CoreError, match="playbill.get.ref_not_found"):
        pb.get("project.work_item/wi-4")

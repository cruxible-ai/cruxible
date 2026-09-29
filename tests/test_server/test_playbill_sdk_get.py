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


def test_an_exact_content_claim_view_carries_the_text_the_daemon_reads(
    owned_playbill_http: tuple[TestClient, str, Path],  # noqa: F811
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.get_reads import PlaybillExactContentRefV1
    from tests.core_support._exact_content_support import seed_exact_content_into

    client, instance_id, key = owned_playbill_http
    instance = get_playbill_manager().get(instance_id)
    reviewer = instance._recovered.head.principals.require_active("reviewer")  # noqa: SLF001
    seeded = seed_exact_content_into(
        instance,
        GeneratedKeyMaterial(
            principal=reviewer, private_key_path=key, public_key_path=key.with_suffix(".pub")
        ),
        {"wi-42": b"The ruling, exactly as written.\n", "wi-bin": b"\xff\xfe\x00opaque"},
    )
    ruling, binary = seeded["wi-42"], seeded["wi-bin"]
    pb = _sdk(client, instance_id, tmp_path)

    card = pb.get(ruling.claim_id)
    assert isinstance(card.value, ClaimView)
    assert card.value.object_kind == "exact_content"
    assert card.value.value == "The ruling, exactly as written.\n"
    assert card.value.content_digest == ruling.digest
    # claim_view and the batch read agree with get, and with the CLI and MCP card.
    assert pb.claim_view(ruling.claim_id) == card.value
    (batched, marked) = pb.claim_views([ruling.claim_id, binary.claim_id])
    assert batched == card.value
    assert marked.value == PlaybillExactContentRefV1(
        exact_content="binary", content_digest=binary.digest, length=9
    )
    assert marked.content_digest == binary.digest


def test_cut_values_offer_runnable_evidence_on_every_surface(
    owned_playbill_http: tuple[TestClient, str, Path],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import shlex

    from click.testing import CliRunner

    from cruxible_client.contracts import PlaybillAcceptedCoordinate
    from cruxible_client.contracts.get_reads import PlaybillGetHistoryV1
    from cruxible_core.cli.main import cli
    from cruxible_core.mcp import handlers
    from tests.core_support._exact_content_support import seed_exact_content_into

    client, instance_id, key = owned_playbill_http
    instance = get_playbill_manager().get(instance_id)
    reviewer = instance._recovered.head.principals.require_active("reviewer")  # noqa: SLF001
    whole = "a long ruling " * 100
    seeded = seed_exact_content_into(
        instance,
        GeneratedKeyMaterial(
            principal=reviewer, private_key_path=key, public_key_path=key.with_suffix(".pub")
        ),
        {"wi-42": whole.encode()},
    )["wi-42"]
    pb = _sdk(client, instance_id, tmp_path)
    transport = pb._client  # noqa: SLF001
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    monkeypatch.setattr(
        handlers, "_dispatch_remote_or_local", lambda remote, _local, **_kw: remote(transport)
    )
    prefix = ["--server-url", "http://testserver", "--instance-id", instance_id]
    url = f"/api/v1/{instance_id}/playbill/get"
    for ref, detail in (
        (seeded.subject, "summary"),
        (seeded.claim_id, "summary"),
        (seeded.claim_id, "history"),
    ):
        # HTTP and MCP retain the service's surface-specific suggestion, including
        # the exact historical generation when a revision value was cut.
        payload = client.post(url, json={"ref": ref, "detail": detail}).json()
        section = payload["history"]["revisions"][0] if detail == "history" else payload["card"]
        step = section["next"][0]
        assert seeded.claim_id in step and 'detail="evidence"' in step
        mcp = handlers.handle_playbill_get(instance_id, ref=ref, detail=detail)
        mcp_section = mcp.history.revisions[0] if mcp.history else mcp.card
        assert mcp_section is not None and mcp_section.next[0] == step
        evidence = eval(
            step,
            {"cruxible_playbill_get": lambda **kw: handlers.handle_playbill_get(instance_id, **kw)},
        )
        assert evidence.evidence.value == whole

        result = CliRunner().invoke(cli, [*prefix, "playbill", "get", ref, "--detail", detail])
        assert result.exit_code == 0, result.output
        assert f"({len(whole)} chars; --detail evidence for all)" in result.output
        step = next(
            line.removeprefix("next: ")
            for line in result.output.splitlines()
            if line.startswith("next: ")
        )
        read = CliRunner().invoke(cli, [*prefix, *shlex.split(step)[1:], "--json"])
        assert read.exit_code == 0, read.output
        assert json.loads(read.output)["evidence"]["value"] == whole

        # SDK Claim summaries are already whole typed ClaimViews; Subject and
        # history cards use the service's suggestions.
        card = pb.get(ref, detail=detail)
        if isinstance(card.value, ClaimView):
            assert card.value.value == whole
            continue
        assert isinstance(card.value, PlaybillGetSubjectCardV1 | PlaybillGetHistoryV1)
        section_sdk = (
            card.value.revisions[0] if isinstance(card.value, PlaybillGetHistoryV1) else card.value
        )
        read_sdk = eval(
            section_sdk.next[0],
            {"pb": pb, "PlaybillAcceptedCoordinate": PlaybillAcceptedCoordinate},
        )
        assert read_sdk.value.value == whole

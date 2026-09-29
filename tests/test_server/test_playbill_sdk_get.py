"""Playbill.get resolves references on the daemon, keeping the SDK's typed views."""

from __future__ import annotations

import json
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
    PlaybillGetRequestV1,
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


def test_a_read_only_caller_reads_exact_content_text_on_every_surface(
    owned_playbill_http: tuple[TestClient, str, Path],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling exact-content-read-only: HTTP, SDK, MCP and CLI read the text at READ_ONLY.

    Capture reads and Document bodies keep their body-read boundary.
    """

    from click.testing import CliRunner

    from cruxible_core.cli.main import cli
    from cruxible_core.mcp import handlers
    from cruxible_core.runtime.permissions import reset_permissions
    from tests.core_support._exact_content_support import EXACT_KIND, seed_exact_content_into
    from tests.test_server.test_playbill_get_route import _accept_document

    client, instance_id, key = owned_playbill_http
    instance = get_playbill_manager().get(instance_id)
    reviewer = instance._recovered.head.principals.require_active("reviewer")  # noqa: SLF001
    text = "The ruling, exactly as written.\n"
    (ruling,) = seed_exact_content_into(
        instance,
        GeneratedKeyMaterial(
            principal=reviewer, private_key_path=key, public_key_path=key.with_suffix(".pub")
        ),
        {"wi-42": text.encode()},
    ).values()
    _accept_document(client, instance_id, key)
    pb = _sdk(client, instance_id, tmp_path)
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    get_url = f"/api/v1/{instance_id}/playbill/get"
    capture_url = f"/api/v1/{instance_id}/playbill/captures/read"

    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    try:
        # HTTP: the Claim card, its history and a query row read the text.
        card = client.post(get_url, json={"ref": ruling.claim_id})
        assert card.status_code == 200, card.text
        assert card.json()["card"]["value"] == text
        history = client.post(get_url, json={"ref": ruling.claim_id, "detail": "history"})
        assert history.status_code == 200, history.text
        assert [item["value"] for item in history.json()["history"]["revisions"]] == [text]
        rows = client.post(
            f"/api/v1/{instance_id}/playbill/query",
            json={"kind": EXACT_KIND, "select": ["status"]},
        )
        assert rows.status_code == 200, rows.text
        assert {row["subject"]: row["status"] for row in rows.json()["rows"]}[
            ruling.subject
        ] == text

        # SDK: the typed ClaimView, history and query agree.
        view = pb.get(ruling.claim_id).value
        assert isinstance(view, ClaimView) and view.value == text
        sdk_history = pb.get(ruling.claim_id, detail="history").value
        assert [item.value for item in sdk_history.revisions] == [text]  # type: ignore[union-attr]
        sdk_rows = {row["subject"]: row for row in pb.query(kind=EXACT_KIND, select=["status"])}
        assert sdk_rows[ruling.subject]["status"] == text

        # MCP (local dispatch through the same facade).
        mcp_card = handlers.handle_playbill_get(instance_id, ref=ruling.claim_id).card
        assert mcp_card is not None and mcp_card.value == text  # type: ignore[union-attr]
        mcp_history = handlers.handle_playbill_get(
            instance_id, ref=ruling.claim_id, detail="history"
        ).history
        assert mcp_history is not None
        assert [item.value for item in mcp_history.revisions] == [text]
        mcp_rows = handlers.handle_playbill_query(
            instance_id, kind=EXACT_KIND, select=["status"]
        ).rows
        assert {row["subject"]: row["status"] for row in mcp_rows}[ruling.subject] == text

        # CLI, over the daemon.
        prefix = ["--server-url", "http://testserver", "--instance-id", instance_id]
        printed = CliRunner().invoke(cli, [*prefix, "playbill", "get", ruling.claim_id])
        assert printed.exit_code == 0, printed.output
        assert "The ruling, exactly as written." in printed.output
        printed_history = CliRunner().invoke(
            cli, [*prefix, "playbill", "get", ruling.claim_id, "--detail", "history"]
        )
        assert printed_history.exit_code == 0, printed_history.output
        assert "The ruling, exactly as written." in printed_history.output
        table = CliRunner().invoke(
            cli, [*prefix, "playbill", "query", EXACT_KIND, "--select", "status"]
        )
        assert table.exit_code == 0, table.output
        assert "The ruling, exactly as written." in table.output

        # The body-read boundary still holds for Capture reads and Document bodies.
        capture = client.post(capture_url, json={"capture_digest": ruling.digest})
        assert capture.status_code == 403, capture.text
        body = client.post(
            get_url,
            json={"ref": "Document:design", "detail": "body", "range": {"start": 0, "end": 8}},
        )
        assert body.status_code == 403, body.text
        assert client.post(get_url, json={"ref": "Document:design"}).status_code == 200
    finally:
        monkeypatch.delenv("CRUXIBLE_MODE")
        reset_permissions()

    # The same reads pass at the body-read tier, so the refusals above are the tier's.
    assert client.post(capture_url, json={"capture_digest": ruling.digest}).status_code == 404
    opened = client.post(
        get_url,
        json={"ref": "Document:design", "detail": "body", "range": {"start": 0, "end": 8}},
    )
    assert opened.status_code == 200, opened.text


def test_the_compact_coordinate_passes_back_as_at_on_every_surface(
    owned_playbill_http: tuple[TestClient, str, Path],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling at-accepts-oid-prefix, over HTTP, the SDK, MCP and the CLI."""

    from click.testing import CliRunner

    from cruxible_core.cli.main import cli
    from cruxible_core.mcp import handlers

    client, instance_id, key = owned_playbill_http
    instance = get_playbill_manager().get(instance_id)
    reviewer = instance._recovered.head.principals.require_active("reviewer")  # noqa: SLF001
    seed_claims_into(
        instance,
        GeneratedKeyMaterial(
            principal=reviewer, private_key_path=key, public_key_path=key.with_suffix(".pub")
        ),
    )
    earlier = instance.accepted_history()[-2]
    get_url = f"/api/v1/{instance_id}/playbill/get"
    ref = f"ClaimType:{PREDICATE}"

    printed = client.post(get_url, json={"ref": ref, "at": earlier.oid})
    assert printed.status_code == 200, printed.text
    compact = printed.json()["coordinate"]["git_oid"]
    assert len(compact) == 12 and earlier.oid.startswith(compact)

    # HTTP: get, query and orient read at the compact coordinate.
    again = client.post(get_url, json={"ref": ref, "at": compact})
    assert again.status_code == 200, again.text
    assert again.json()["coordinate"] == printed.json()["coordinate"]
    queried = client.post(
        f"/api/v1/{instance_id}/playbill/query", json={"kind": "ClaimType", "at": compact}
    )
    assert queried.status_code == 200, queried.text
    assert queried.json()["receipt"]["coordinate"]["git_oid"] == earlier.oid
    oriented = client.get(f"/api/v1/{instance_id}/playbill/orient", params={"at": compact})
    assert oriented.status_code == 200, oriented.text
    assert oriented.json()["generation"] == earlier.sequence

    # A too-short prefix is the resolver's coded refusal, not a request-shape fault.
    short = client.post(get_url, json={"ref": ref, "at": compact[:8]})
    assert short.status_code == 400, short.text
    assert short.json()["error_code"] == "playbill.read.coordinate_prefix_too_short"
    short_orient = client.get(f"/api/v1/{instance_id}/playbill/orient", params={"at": compact[:8]})
    assert short_orient.json()["error_code"] == "playbill.read.coordinate_prefix_too_short"
    short_query = client.post(
        f"/api/v1/{instance_id}/playbill/query", json={"kind": "ClaimType", "at": compact[:8]}
    )
    assert short_query.json()["error_code"] == "playbill.read.coordinate_prefix_too_short"

    # SDK and MCP.
    pb = _sdk(client, instance_id, tmp_path)
    assert pb.query(kind="ClaimType", at=compact).page.receipt.coordinate.git_oid == earlier.oid
    assert handlers.handle_playbill_get(instance_id, ref=ref, at=compact).coordinate.git_oid == (
        compact
    )
    assert handlers.handle_playbill_orient(instance_id, at=compact).generation == earlier.sequence
    assert (
        handlers.handle_playbill_query(
            instance_id, kind="ClaimType", at=compact
        ).receipt.coordinate.git_oid
        == earlier.oid
    )

    # CLI --at, over the daemon.
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    assert (
        transport.playbill_get(
            instance_id, request=PlaybillGetRequestV1(ref=ref, at=compact)
        ).coordinate.git_oid
        == compact
    )
    assert transport.orient_playbill(instance_id, at=compact).generation == earlier.sequence
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    prefix = ["--server-url", "http://testserver", "--instance-id", instance_id]
    as_json = CliRunner().invoke(cli, [*prefix, "playbill", "get", ref, "--at", compact, "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output)["coordinate"]["git_oid"] == compact
    queried_cli = CliRunner().invoke(
        cli, [*prefix, "playbill", "query", "ClaimType", "--at", compact, "--json"]
    )
    assert queried_cli.exit_code == 0, queried_cli.output
    assert json.loads(queried_cli.output)["receipt"]["coordinate"]["git_oid"] == earlier.oid
    oriented_cli = CliRunner().invoke(
        cli, [*prefix, "playbill", "orient", "--at", compact, "--json"]
    )
    assert oriented_cli.exit_code == 0, oriented_cli.output
    assert json.loads(oriented_cli.output)["generation"] == earlier.sequence

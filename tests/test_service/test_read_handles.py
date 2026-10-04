"""Handles a read prints can be passed back: CAP- handles, history OIDs, generation numbers.

``get(detail="evidence")`` names each Capture by its ``CAP-<12 hex>`` handle,
and both ``get`` and ``read_capture`` accept it (a prefix must be unique among
the Captures the write verbs resolve too: cited, or retained and verifying).
A history row carries its generation's git oid beside the sequence, and ``at``
accepts either: the oid (or a 12+ hex prefix) or the generation number.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.capture_reads import CaptureReadRequest
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.errors import ReadRefusalError
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
from cruxible_core.storage.cas import BodyAccessContext

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    from tests.test_consumers.test_prediction_settlement import FIXED_CLOSES, drain, fixed_world

    instance, _owner, capture_digest, contract = fixed_world(tmp_path_factory.mktemp("handles"))
    drain(instance, now=FIXED_CLOSES + timedelta(minutes=1))
    return instance, capture_digest, contract


def _get(instance: Any, ref: str, **fields: Any):  # type: ignore[no-untyped-def]
    return service_playbill_get(instance, request=GetRequest(ref=ref, **fields), access=_ACCESS)


def test_an_evidence_handle_reads_back_through_get_and_read_capture(world) -> None:  # type: ignore[no-untyped-def]
    instance, capture_digest, contract = world
    hypothesis = _get(instance, contract.identity.qualified).card.hypothesis  # type: ignore[union-attr]

    evidence = _get(instance, hypothesis, detail="evidence").evidence
    assert evidence is not None
    handle = "CAP-" + capture_digest.removeprefix("sha256:")[:12]
    assert handle in [item.capture for item in evidence.captures]

    assert _get(instance, handle).ref == f"Capture:{capture_digest}"
    read = service_read_playbill_capture(
        instance, request=CaptureReadRequest(capture_digest=handle), access=_ACCESS
    )
    assert read.status == "verified" and read.capture_digest == capture_digest
    prefixed = service_read_playbill_capture(
        instance,
        request=CaptureReadRequest(capture_digest=capture_digest[: len("sha256:") + 14]),
        access=_ACCESS,
    )
    assert prefixed.capture_digest == capture_digest


def test_read_capture_refuses_an_unknown_or_ambiguous_prefix(
    world,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _capture, _contract = world

    with pytest.raises(ReadRefusalError) as unknown:
        service_read_playbill_capture(
            instance, request=CaptureReadRequest(capture_digest="CAP-" + "0" * 12), access=_ACCESS
        )
    assert unknown.value.error_code == "playbill.capture.not_found"
    assert unknown.value.http_status == 404
    assert unknown.value.repair is not None
    assert unknown.value.repair.arguments == {"section": "captures"}

    from cruxible_core.service.discovery import operational

    twins = ("sha256:" + "1" * 64, "sha256:" + "1" * 63 + "2")
    monkeypatch.setattr(operational, "captures_with_prefix", lambda *_a, **_k: twins)
    with pytest.raises(ReadRefusalError) as ambiguous:
        service_read_playbill_capture(
            instance, request=CaptureReadRequest(capture_digest="CAP-" + "1" * 12), access=_ACCESS
        )
    assert ambiguous.value.error_code == "playbill.capture.ref_ambiguous"
    assert ambiguous.value.http_status == 409
    assert tuple(ambiguous.value.candidates) == twins


def test_a_handle_the_bounded_lookup_cannot_settle_refuses_in_every_read(
    world,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reads resolve handles through the write verbs' bounded resolver, refusal included.

    A lookup that runs out of budget never calls a partial answer unique; a
    full digest names itself and needs no lookup at all.
    """

    from cruxible_core.service.evidence import capture_reads

    instance, capture_digest, _contract = world
    handle = "CAP-" + capture_digest.removeprefix("sha256:")[:12]
    monkeypatch.setattr(capture_reads, "CAPTURE_HANDLE_SCAN_BUDGET", 0)

    for read in (
        lambda: _get(instance, handle),
        lambda: service_read_playbill_capture(
            instance, request=CaptureReadRequest(capture_digest=handle), access=_ACCESS
        ),
    ):
        with pytest.raises(ReadRefusalError) as refused:
            read()
        assert refused.value.error_code == "playbill.capture.ref_scan_exhausted"
        assert refused.value.http_status == 409
        assert "longer handle" in str(refused.value.context["repair_line"])
    assert _get(instance, f"Capture:{capture_digest}").ref == f"Capture:{capture_digest}"


def test_read_capture_still_checks_permission_before_resolving_a_prefix() -> None:
    from cruxible_core.errors import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        service_read_playbill_capture(
            None,  # type: ignore[arg-type]
            request=CaptureReadRequest(capture_digest="CAP-" + "a" * 12),
            access=BodyAccessContext(principal_id="reader", can_read_body=False),
        )


def test_a_generation_number_reads_the_same_generation_as_its_oid(world) -> None:  # type: ignore[no-untyped-def]
    instance, _capture, contract = world
    history = instance.accepted_history()
    older = history[-2]

    oriented = service_playbill_orient(instance, section="claim_types", at=str(older.sequence))
    assert oriented.generation == older.sequence
    assert oriented.coordinate.git_oid == older.oid
    assert oriented.claim_types
    ref = f"ClaimType:{oriented.claim_types[0].predicate}"

    by_oid = _get(instance, ref, at=older.oid, detail="proof")
    by_number = _get(instance, ref, at=str(older.sequence), detail="proof")
    assert by_number.coordinate == by_oid.coordinate
    assert by_number.coordinate.generation == older.sequence

    queried = service_playbill_query(
        instance, request=QueryRequest(kind="ClaimType", at=str(older.sequence))
    )
    assert queried.receipt.coordinate.git_oid == older.oid

    with pytest.raises(ReadRefusalError) as missing:
        _get(instance, contract.identity.qualified, at=str(history[-1].sequence + 5))
    assert missing.value.error_code == "playbill.read.coordinate_not_accepted"
    assert str(history[-1].sequence) in missing.value.candidates


def test_a_history_row_names_its_oid_and_it_reads_back(world) -> None:  # type: ignore[no-untyped-def]
    instance, _capture, contract = world

    history = _get(instance, contract.identity.qualified, detail="history").history
    assert history is not None
    (revision,) = history.revisions
    assert len(revision.git_oid) == 12

    by_prefix = _get(instance, contract.identity.qualified, at=revision.git_oid)
    by_number = _get(instance, contract.identity.qualified, at=str(revision.sequence))
    assert by_prefix.coordinate.git_oid == revision.git_oid == by_number.coordinate.git_oid


def test_the_mcp_read_tools_take_a_generation_number(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from cruxible_core.mcp.server import create_server

    seen: list[Any] = []
    monkeypatch.setattr(handlers, "handle_playbill_get", lambda _id, **values: seen.append(values))
    monkeypatch.setattr(
        handlers, "handle_playbill_orient", lambda _id, **values: seen.append(values)
    )
    server = create_server()

    for name, arguments in (
        ("cruxible_playbill_get", {"instance_id": "i", "ref": "x", "at": 7}),
        ("cruxible_playbill_orient", {"instance_id": "i", "at": 7}),
    ):
        try:
            asyncio.run(server.call_tool(name, arguments))
        except Exception:  # noqa: BLE001 -- the stub answers None, which is not a result
            pass
    assert [values["at"] for values in seen] == ["7", "7"]


def test_the_cli_passes_a_generation_and_a_capture_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    requests: list[Any] = []

    class _Stub:
        def get(self, instance_id: str, *, request: GetRequest) -> Any:
            requests.append(request)
            raise ReadRefusalError("playbill.get.ref_not_found", "stub", http_status=404)

        def read_capture(self, instance_id: str, request: CaptureReadRequest) -> Any:
            requests.append(request)
            raise ReadRefusalError("playbill.capture.not_found", "stub", http_status=404)

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: _Stub())
    prefix = ["--server-url", "http://server", "--instance-id", "inst"]

    CliRunner().invoke(cli, [*prefix, "playbill", "get", "Line:x", "--at", "12"])
    CliRunner().invoke(cli, [*prefix, "playbill", "capture", "read", "CAP-" + "a" * 12])

    assert requests[0].at == "12"
    assert requests[1].capture_digest == "CAP-" + "a" * 12

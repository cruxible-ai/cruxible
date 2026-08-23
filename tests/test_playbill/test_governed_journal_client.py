"""Open governed-journal protocol and untrusted HTTP peer behavior."""

from __future__ import annotations

import base64
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_core.playbill.canonical import ArtifactDigest, typed_digest
from cruxible_core.playbill.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    GovernedJournalClientProtocol,
    HttpGovernedJournalClient,
    JournalCoverageState,
    JournalHeadManifestV1,
    JournalHeadVectorV1,
    JournalPartitionHeadV1,
    JournalRangeV1,
    JournalStreamIdentityV1,
    JournalTransfer,
    RemoteJournalConflict,
    RemoteJournalError,
    RemoteJournalRefusal,
    RemoteJournalTransportError,
    RemoteJournalVerificationError,
    StoredProcedureJournalRecordV1,
    build_journal_export,
    build_journal_head_manifest,
    governed,
    journal_genesis_digest,
    procedure_journal_record_digest,
    render_journal_export,
)
from cruxible_core.playbill.exhaust.records import ProcedureJournalRecordV1, journal_head_key
from tests.test_playbill.test_journal_backends import (
    NOW,
    _activate,
    _backend,
    _draft,
    _HeadSigner,
    _stream,
)

FORMAT_VERSION = "governed-journal-test-v1"
ENDPOINT_ROOT = "https://journal.example/already-scoped"
HOME_STREAM_ID = "home-stream-42"
PARTITION = "runs-2026-08"
AUTHORIZATION = "Bearer opaque-authorization-material"
WRITE_PROOF_HEADER = "X-Journal-Write-Proof"
WRITE_PROOF = "opaque-write-proof"


@dataclass(frozen=True)
class _PeerFixture:
    genesis: JournalPartitionHeadV1
    stored: StoredProcedureJournalRecordV1
    head: JournalPartitionHeadV1
    journal_range: JournalRangeV1
    content: dict[str, object]
    payload: bytes
    head_manifest: JournalHeadManifestV1
    public_key: str


def _peer_fixture(tmp_path: Path) -> _PeerFixture:
    backend = _backend(tmp_path, "remote-source")
    genesis = backend.read_head(_stream(), PARTITION)
    _activate(backend)
    draft = _draft("first")
    stored = backend.append(draft, expected_head=genesis, fencing_token="writer-a")
    head = backend.read_head(_stream(), PARTITION)
    journal_range = backend.range_from_sequences(
        _stream(),
        PARTITION,
        first_sequence=1,
        last_sequence=1,
    )
    signer = _HeadSigner(Ed25519PrivateKey.generate())
    head_manifest = build_journal_head_manifest(
        JournalHeadVectorV1(partitions=(head,)),
        asserted_at=NOW,
        signer=signer,
    )
    bundle = build_journal_export(
        backend,
        ranges=(journal_range,),
        head_manifest=head_manifest,
    )
    content = draft.model_dump(
        mode="json",
        exclude={"tag", "stream", "partition_id", "actor_context"},
        exclude_none=True,
    )
    return _PeerFixture(
        genesis=genesis,
        stored=stored,
        head=head,
        journal_range=journal_range,
        content=content,
        payload=render_journal_export(bundle),
        head_manifest=head_manifest,
        public_key=signer.private_key.public_key().public_bytes_raw().hex(),
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    identity: JournalStreamIdentityV1 | None = None,
) -> tuple[HttpGovernedJournalClient, httpx.Client]:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        HttpGovernedJournalClient(
            http,
            endpoint_root=ENDPOINT_ROOT,
            home_stream_id=HOME_STREAM_ID,
            identity=identity or _stream(),
            authorization_header=AUTHORIZATION,
            format_version=FORMAT_VERSION,
            write_headers={WRITE_PROOF_HEADER: WRITE_PROOF},
        ),
        http,
    )


def _response(body: dict[str, object], *, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={**body, "format_version": FORMAT_VERSION},
    )


def _request_json(request: httpx.Request) -> dict[str, object]:
    raw = json.loads(request.content)
    assert isinstance(raw, dict)
    return raw


def _transfer_response(fixture: _PeerFixture) -> dict[str, object]:
    return {
        "export": {
            "payload_base64": base64.b64encode(fixture.payload).decode("ascii"),
            "byte_length": len(fixture.payload),
            "segment_count": 1,
            "record_count": 1,
        },
        "head_manifest": fixture.head_manifest.model_dump(mode="json"),
        "expected_head_public_key": fixture.public_key,
        "operation_id": "operation-export",
    }


def _head_document(head: JournalPartitionHeadV1) -> dict[str, object]:
    return {"head": head.model_dump(mode="json")}


def test_governed_client_public_contract_has_no_private_service_vocabulary() -> None:
    public = [governed.__doc__ or "", *governed.__all__]
    for name in governed.__all__:
        symbol = getattr(governed, name)
        public.append(symbol.__doc__ or "")
        try:
            public.append(str(inspect.signature(symbol)))
        except (TypeError, ValueError):
            pass
        for member_name, member in getattr(symbol, "__dict__", {}).items():
            if member_name.startswith("_"):
                continue
            public.extend((member_name, getattr(member, "__doc__", "") or ""))
            if callable(member):
                public.append(str(inspect.signature(member)))
    for name, value in vars(governed).items():
        if name.isupper():
            public.extend((name, repr(value)))
    words = set(re.findall(r"[a-z]+", " ".join(public).lower()))
    assert words.isdisjoint({"tenant", "account", "quota", "credit", "billing", "org"})
    assert (
        "idempotency_key" not in inspect.signature(GovernedJournalClientProtocol.append).parameters
    )


def test_remote_refusals_compose_with_the_journal_error_family() -> None:
    refusal = RemoteJournalRefusal(remote_status=429, refusal_id="home-refusal-17")
    conflict = RemoteJournalConflict(remote_status=409, refusal_id="journal_law_refused")

    assert isinstance(refusal, PlaybillJournalError)
    assert isinstance(conflict, RemoteJournalRefusal)
    assert refusal.remote_status == 429
    assert refusal.refusal_id == "home-refusal-17"
    assert issubclass(RemoteJournalTransportError, RemoteJournalError)
    assert issubclass(RemoteJournalVerificationError, RemoteJournalError)


def test_http_peer_round_trip_verifies_and_reuses_deterministic_append_identity(
    tmp_path: Path,
) -> None:
    fixture = _peer_fixture(tmp_path)
    current = fixture.genesis
    append_documents: list[dict[str, object]] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current
        requests.append(request)
        path = request.url.path
        stream_path = f"/already-scoped/journal/streams/{HOME_STREAM_ID}"
        partition_path = f"{stream_path}/partitions/{PARTITION}"
        if request.method == "GET" and path == f"{partition_path}/head":
            return _response(_head_document(current))
        if request.method == "POST" and path == f"{partition_path}/lease":
            body = _request_json(request)
            return _response(
                {
                    "writer_lease": {
                        "journal_stream_id": HOME_STREAM_ID,
                        "partition_id": PARTITION,
                        "status": "active",
                        "lease_generation": 1,
                        "expected_head_sequence": body["expected_head_sequence"],
                        "expected_head_record_digest": body["expected_head_record_digest"],
                    },
                    "fencing_token": "fence-one",
                }
            )
        if request.method == "POST" and path == f"{partition_path}/records":
            body = _request_json(request)
            append_documents.append(body)
            replayed = len(append_documents) > 1
            current = fixture.head
            return _response(
                {
                    "record": fixture.stored.model_dump(mode="json"),
                    "head": fixture.head.model_dump(mode="json"),
                    "replayed": replayed,
                    "operation_id": "operation-replay" if replayed else "operation-append",
                }
            )
        if request.method == "GET" and path == f"{partition_path}/records":
            assert request.url.params["first_sequence"] == "1"
            assert request.url.params["last_sequence"] == "1"
            return _response(
                {
                    "range": fixture.journal_range.model_dump(mode="json"),
                    "records": [fixture.stored.model_dump(mode="json")],
                }
            )
        if request.method == "GET" and path == f"{stream_path}/coverage":
            return _response(
                {
                    "coverage": {
                        "coverage": "exact",
                        "partitions": [
                            {
                                "partition_id": PARTITION,
                                "coverage": "exact",
                                "head_sequence": fixture.head.sequence,
                                "head_record_digest": fixture.head.record_digest,
                                "reason": None,
                            }
                        ],
                        "reason": None,
                    }
                }
            )
        if request.method == "POST" and path == f"{stream_path}/export":
            return _response(_transfer_response(fixture))
        if request.method == "POST" and path == f"{stream_path}/import":
            return _response(
                {
                    "imported_heads": [fixture.head.model_dump(mode="json")],
                }
            )
        if request.method == "POST" and path == f"{stream_path}/handoff/begin":
            return _response(
                {
                    **_transfer_response(fixture),
                    "moving_partitions": [PARTITION],
                }
            )
        if request.method == "POST" and path == f"{partition_path}/lease/fence":
            return _response(
                {
                    "writer_lease": {
                        "journal_stream_id": HOME_STREAM_ID,
                        "partition_id": PARTITION,
                        "status": "fenced",
                        "lease_generation": 1,
                    }
                }
            )
        if request.method == "POST" and path == f"{stream_path}/handoff/complete":
            return _response(
                {
                    "released_partitions": [PARTITION],
                    "fenced_leases": [
                        {
                            "journal_stream_id": HOME_STREAM_ID,
                            "partition_id": PARTITION,
                            "status": "fenced",
                        }
                    ],
                    "export_remains_available": True,
                }
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client, http = _client(handler)
    try:
        assert isinstance(client, GovernedJournalClientProtocol)
        assert client.read_head(PARTITION) == fixture.genesis
        assert client.read_head_vector((PARTITION,)).partitions == (fixture.genesis,)
        grant = client.acquire_writer(PARTITION, expected_head=fixture.genesis)
        first = client.append(
            PARTITION,
            content=fixture.content,
            expected_head=fixture.genesis,
            fencing_token=grant.fencing_token,
        )
        replay = client.append(
            PARTITION,
            content=dict(reversed(tuple(fixture.content.items()))),
            expected_head=fixture.genesis,
            fencing_token=grant.fencing_token,
        )
        assert first.record == fixture.stored
        assert first.replayed is False
        assert replay.record == first.record
        assert replay.replayed is True
        assert client.read_exact_range(fixture.journal_range) == (fixture.stored,)
        coverage = client.coverage((PARTITION,))
        assert coverage.state is JournalCoverageState.EXACT
        assert coverage.partitions == (fixture.head,)
        transfer = client.export((PARTITION,))
        assert transfer.payload == fixture.payload
        assert client.import_transfer(transfer) == (fixture.head,)
        proof = client.head_proof((PARTITION,))
        assert proof.verify().partitions == (fixture.head,)
        client.fence_writer(
            PARTITION,
            fencing_token=grant.fencing_token,
            expected_generation=grant.generation,
        )
        client.complete_handoff(
            target_proof=proof,
            source_fencing_tokens={PARTITION: grant.fencing_token},
            partition_ids=(PARTITION,),
        )
    finally:
        http.close()

    assert len(append_documents) == 2
    assert append_documents[0]["idempotency_key"] == append_documents[1]["idempotency_key"]
    assert str(append_documents[0]["idempotency_key"]).startswith("sha256:")
    assigned = {
        "actor_context",
        "partition_id",
        "previous_record_digest",
        "sequence",
        "stream",
    }
    sent_content = append_documents[0]["content"]
    assert isinstance(sent_content, dict)
    assert assigned.isdisjoint(sent_content)
    assert grant.fencing_token not in repr(grant)
    assert repr(transfer.payload) not in repr(transfer)
    assert all(request.headers["Authorization"] == AUTHORIZATION for request in requests)
    write_paths = {"/lease", "/records", "/lease/fence"}
    for request in requests:
        is_write = any(request.url.path.endswith(suffix) for suffix in write_paths) and (
            request.method == "POST"
        )
        assert (request.headers.get(WRITE_PROOF_HEADER) == WRITE_PROOF) is is_write
        assert request.url.path.startswith("/already-scoped/journal/")


@pytest.mark.parametrize(
    "assigned_field",
    [
        "actor_context",
        "partition_id",
        "previous_record_digest",
        "sequence",
        "stream",
        "tag",
    ],
)
def test_append_refuses_home_assigned_content_before_transport(
    assigned_field: str,
) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"transport must not run for {request.url}")

    client, http = _client(unexpected)
    try:
        genesis = JournalPartitionHeadV1(
            stream=_stream(),
            partition_id=PARTITION,
            sequence=0,
            record_digest=journal_genesis_digest(_stream(), PARTITION),
        )
        with pytest.raises(RemoteJournalVerificationError, match="home-assigned"):
            client.append(
                PARTITION,
                content={assigned_field: "caller-value"},
                expected_head=genesis,
                fencing_token="fence-one",
            )
    finally:
        http.close()


def test_head_and_range_coordinate_substitution_are_typed_refusals(tmp_path: Path) -> None:
    fixture = _peer_fixture(tmp_path)
    other_stream = JournalStreamIdentityV1(
        instance_id=_stream().instance_id,
        journal_family=_stream().journal_family,
        stream_id="substituted",
    )
    substituted_head = JournalPartitionHeadV1(
        stream=other_stream,
        partition_id=PARTITION,
        sequence=0,
        record_digest=journal_genesis_digest(other_stream, PARTITION),
    )

    def head_handler(_request: httpx.Request) -> httpx.Response:
        return _response(_head_document(substituted_head))

    client, http = _client(head_handler)
    try:
        with pytest.raises(RemoteJournalVerificationError, match="substituted stream"):
            client.read_head(PARTITION)
    finally:
        http.close()

    changed_range = fixture.journal_range.model_copy(
        update={
            "expected_head_digest": typed_digest(
                ArtifactDigest,
                "governed-journal-test-range-substitution-v1",
                {"value": "other"},
            ).tagged
        }
    )

    def range_handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            {
                "range": changed_range.model_dump(mode="json"),
                "records": [fixture.stored.model_dump(mode="json")],
            }
        )

    client, http = _client(range_handler)
    try:
        with pytest.raises(RemoteJournalVerificationError, match="requested coordinate"):
            client.read_exact_range(fixture.journal_range)
    finally:
        http.close()


def test_truncated_range_is_verification_failure_not_empty_history(tmp_path: Path) -> None:
    fixture = _peer_fixture(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            {
                "range": fixture.journal_range.model_dump(mode="json"),
                "records": [],
            }
        )

    client, http = _client(handler)
    try:
        with pytest.raises(RemoteJournalVerificationError, match="chain verification"):
            client.read_exact_range(fixture.journal_range)
    finally:
        http.close()


def test_append_refuses_record_scope_head_commit_extension_and_content_changes(
    tmp_path: Path,
) -> None:
    fixture = _peer_fixture(tmp_path)
    other_stream = JournalStreamIdentityV1(
        instance_id=_stream().instance_id,
        journal_family=_stream().journal_family,
        stream_id="substituted",
    )
    other_draft = _draft("first", stream=other_stream)
    other_record = ProcedureJournalRecordV1.bind(
        other_draft,
        sequence=1,
        previous_record_digest=journal_genesis_digest(other_stream, PARTITION),
    )
    other_stored = StoredProcedureJournalRecordV1(
        record=other_record,
        record_digest=procedure_journal_record_digest(other_record),
    )
    substituted_record_head = JournalPartitionHeadV1(
        stream=_stream(),
        partition_id=PARTITION,
        sequence=1,
        record_digest=other_stored.record_digest,
    )
    unrelated_previous = typed_digest(
        ArtifactDigest,
        "governed-journal-test-previous-v1",
        {"value": "unrelated"},
    ).tagged
    later_record = ProcedureJournalRecordV1.bind(
        _draft("first"),
        sequence=2,
        previous_record_digest=unrelated_previous,
    )
    later_stored = StoredProcedureJournalRecordV1(
        record=later_record,
        record_digest=procedure_journal_record_digest(later_record),
    )
    later_head = JournalPartitionHeadV1(
        stream=_stream(),
        partition_id=PARTITION,
        sequence=2,
        record_digest=later_stored.record_digest,
    )
    different_content = _draft("different").model_dump(
        mode="json",
        exclude={"tag", "stream", "partition_id", "actor_context"},
        exclude_none=True,
    )
    cases = (
        (
            other_stored,
            substituted_record_head,
            fixture.content,
            "record substituted",
        ),
        (fixture.stored, fixture.genesis, fixture.content, "does not commit"),
        (later_stored, later_head, fixture.content, "exact expected head"),
        (fixture.stored, fixture.head, different_content, "changed caller-supplied"),
    )
    for stored, head, content, message in cases:

        def handler(_request: httpx.Request, *, stored=stored, head=head) -> httpx.Response:
            return _response(
                {
                    "record": stored.model_dump(mode="json"),
                    "head": head.model_dump(mode="json"),
                    "replayed": False,
                    "operation_id": "operation-append",
                }
            )

        client, http = _client(handler)
        try:
            with pytest.raises(RemoteJournalVerificationError, match=message):
                client.append(
                    PARTITION,
                    content=content,
                    expected_head=fixture.genesis,
                    fencing_token="fence-one",
                )
        finally:
            http.close()


def test_replayed_append_is_success_when_it_extends_expected_head(tmp_path: Path) -> None:
    fixture = _peer_fixture(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            {
                "record": fixture.stored.model_dump(mode="json"),
                "head": fixture.head.model_dump(mode="json"),
                "replayed": True,
                "operation_id": "operation-replay",
            }
        )

    client, http = _client(handler)
    try:
        outcome = client.append(
            PARTITION,
            content=fixture.content,
            expected_head=fixture.genesis,
            fencing_token="fence-one",
        )
        assert outcome.replayed is True
        assert outcome.record == fixture.stored
    finally:
        http.close()


def test_replayed_append_without_expected_head_extension_is_refused(tmp_path: Path) -> None:
    fixture = _peer_fixture(tmp_path)
    unrelated_previous = typed_digest(
        ArtifactDigest,
        "governed-journal-test-replay-previous-v1",
        {"value": "prior"},
    ).tagged
    replayed_record = ProcedureJournalRecordV1.bind(
        _draft("first"),
        sequence=2,
        previous_record_digest=unrelated_previous,
    )
    replayed_stored = StoredProcedureJournalRecordV1(
        record=replayed_record,
        record_digest=procedure_journal_record_digest(replayed_record),
    )
    replayed_head = JournalPartitionHeadV1(
        stream=_stream(),
        partition_id=PARTITION,
        sequence=2,
        record_digest=replayed_stored.record_digest,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            {
                "record": replayed_stored.model_dump(mode="json"),
                "head": replayed_head.model_dump(mode="json"),
                "replayed": True,
                "operation_id": "operation-replay",
            }
        )

    client, http = _client(handler)
    try:
        with pytest.raises(RemoteJournalVerificationError, match="exact expected head"):
            client.append(
                PARTITION,
                content=fixture.content,
                expected_head=fixture.genesis,
                fencing_token="fence-one",
            )
    finally:
        http.close()


def test_idempotency_key_changes_only_with_append_coordinates_or_content(
    tmp_path: Path,
) -> None:
    fixture = _peer_fixture(tmp_path)
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _request_json(request)
        key = body.get("idempotency_key")
        assert isinstance(key, str)
        keys.append(key)
        return httpx.Response(
            409,
            json={"error_code": "home-stop", "message": "fixture stop"},
        )

    first_content = fixture.content
    second_content = _draft("different").model_dump(
        mode="json",
        exclude={"tag", "stream", "partition_id", "actor_context"},
        exclude_none=True,
    )
    first_client, first_http = _client(handler)
    second_client, second_http = _client(handler)
    try:
        for client, expected_head, content, fencing_token in (
            (first_client, fixture.genesis, first_content, "fence-one"),
            (
                second_client,
                fixture.genesis,
                dict(reversed(tuple(first_content.items()))),
                "different-fence",
            ),
            (first_client, fixture.genesis, second_content, "fence-one"),
            (second_client, fixture.head, second_content, "another-fence"),
        ):
            with pytest.raises(RemoteJournalRefusal):
                client.append(
                    PARTITION,
                    content=content,
                    expected_head=expected_head,
                    fencing_token=fencing_token,
                )
    finally:
        first_http.close()
        second_http.close()

    assert keys[0] == keys[1]
    assert len(set(keys)) == 3


def test_export_and_import_reject_unverified_bundle_metadata(tmp_path: Path) -> None:
    fixture = _peer_fixture(tmp_path)
    wrong_key = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()

    def export_handler(_request: httpx.Request) -> httpx.Response:
        body = _transfer_response(fixture)
        body["expected_head_public_key"] = wrong_key
        return _response(body)

    client, http = _client(export_handler)
    try:
        with pytest.raises(RemoteJournalVerificationError, match="head-proof verification"):
            client.export((PARTITION,))
    finally:
        http.close()

    transfer = JournalTransfer(
        payload=fixture.payload,
        head_manifest=fixture.head_manifest,
        expected_head_public_key=wrong_key,
        segment_count=1,
        record_count=1,
    )

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unverified transfer reached {request.url}")

    client, http = _client(unexpected)
    try:
        with pytest.raises(RemoteJournalVerificationError, match="head-proof verification"):
            client.import_transfer(transfer)
    finally:
        http.close()


def test_handoff_partition_arrays_are_exact_sets_not_ordered_vectors(tmp_path: Path) -> None:
    backend = _backend(tmp_path, "two-partition-source")
    requested = ("z-partition", "a-partition")
    heads: list[JournalPartitionHeadV1] = []
    ranges: list[JournalRangeV1] = []
    for index, partition_id in enumerate(requested):
        genesis = backend.read_head(_stream(), partition_id)
        token = f"writer-{index}"
        backend.activate_writer(
            _stream(),
            partition_id,
            fencing_token=token,
            expected_head=genesis,
        )
        draft = _draft(partition_id).model_copy(update={"partition_id": partition_id})
        backend.append(draft, expected_head=genesis, fencing_token=token)
        heads.append(backend.read_head(_stream(), partition_id))
        ranges.append(
            backend.range_from_sequences(
                _stream(),
                partition_id,
                first_sequence=1,
                last_sequence=1,
            )
        )
    signer = _HeadSigner(Ed25519PrivateKey.generate())
    head_vector = JournalHeadVectorV1(partitions=tuple(sorted(heads, key=journal_head_key)))
    manifest = build_journal_head_manifest(head_vector, asserted_at=NOW, signer=signer)
    bundle = build_journal_export(
        backend,
        ranges=tuple(ranges),
        head_manifest=manifest,
    )
    payload = render_journal_export(bundle)
    public_key = signer.private_key.public_key().public_bytes_raw().hex()
    ordered = tuple(sorted(requested))

    def transfer_response() -> dict[str, object]:
        return {
            "export": {
                "payload_base64": base64.b64encode(payload).decode("ascii"),
                "byte_length": len(payload),
                "segment_count": len(bundle.manifest.segments),
                "record_count": sum(item.record_count for item in bundle.manifest.segments),
            },
            "head_manifest": manifest.model_dump(mode="json"),
            "expected_head_public_key": public_key,
            "operation_id": "operation-handoff",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/handoff/begin"):
            assert _request_json(request)["partition_ids"] == list(requested)
            return _response(
                {
                    **transfer_response(),
                    "moving_partitions": list(requested),
                }
            )
        if request.url.path.endswith("/handoff/complete"):
            assert _request_json(request)["partition_ids"] == list(requested)
            return _response(
                {
                    "released_partitions": list(ordered),
                    "fenced_leases": [
                        {
                            "journal_stream_id": HOME_STREAM_ID,
                            "partition_id": partition_id,
                            "status": "fenced",
                        }
                        for partition_id in ordered
                    ],
                    "export_remains_available": True,
                }
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client, http = _client(handler)
    try:
        proof = client.head_proof(requested)
        client.complete_handoff(
            target_proof=proof,
            source_fencing_tokens={
                partition_id: f"fence-{partition_id}" for partition_id in requested
            },
            partition_ids=requested,
        )
    finally:
        http.close()


@pytest.mark.parametrize(
    ("operation", "status", "refusal_id"),
    [
        ("append", 409, "journal_law_refused"),
        ("fence", 403, "journal_writer_lease_invalid"),
    ],
)
def test_writer_and_expected_head_conflicts_are_distinguishable(
    operation: str,
    status: int,
    refusal_id: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error_code": refusal_id,
                "message": "opaque home refusal",
            },
        )

    client, http = _client(handler)
    genesis = JournalPartitionHeadV1(
        stream=_stream(),
        partition_id=PARTITION,
        sequence=0,
        record_digest=journal_genesis_digest(_stream(), PARTITION),
    )
    try:
        with pytest.raises(RemoteJournalConflict) as refused:
            if operation == "append":
                client.append(
                    PARTITION,
                    content=_draft("first").model_dump(
                        mode="json",
                        exclude={"tag", "stream", "partition_id", "actor_context"},
                        exclude_none=True,
                    ),
                    expected_head=genesis,
                    fencing_token="fence-one",
                )
            else:
                client.fence_writer(
                    PARTITION,
                    fencing_token="fence-one",
                    expected_generation=1,
                )
        assert refused.value.remote_status == status
        assert refused.value.refusal_id == refusal_id
    finally:
        http.close()


def test_unknown_home_refusal_remains_opaque() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error_code": "home-policy-refusal-17",
                "message": "do not interpret this text",
            },
        )

    client, http = _client(handler)
    genesis = JournalPartitionHeadV1(
        stream=_stream(),
        partition_id=PARTITION,
        sequence=0,
        record_digest=journal_genesis_digest(_stream(), PARTITION),
    )
    try:
        with pytest.raises(RemoteJournalRefusal) as refused:
            client.acquire_writer(PARTITION, expected_head=genesis)
        assert type(refused.value) is RemoteJournalRefusal
        assert refused.value.remote_status == 429
        assert refused.value.refusal_id == "home-policy-refusal-17"
        assert "do not interpret" not in str(refused.value)
    finally:
        http.close()


def test_missing_coverage_is_unavailable_and_range_refusal_is_not_empty(
    tmp_path: Path,
) -> None:
    fixture = _peer_fixture(tmp_path)
    call = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call
        call += 1
        if call == 1:
            return _response(
                {
                    "coverage": {
                        "coverage": "unavailable",
                        "partitions": [],
                        "reason": "history could not be proved",
                    }
                }
            )
        return httpx.Response(
            503,
            json={"error_code": "home-history-unavailable", "message": "unavailable"},
        )

    client, http = _client(handler)
    try:
        coverage = client.coverage((PARTITION,))
        assert coverage.state is JournalCoverageState.UNAVAILABLE
        assert coverage.partitions == ()
        with pytest.raises(RemoteJournalRefusal) as refused:
            client.read_exact_range(fixture.journal_range)
        assert refused.value.refusal_id == "home-history-unavailable"
    finally:
        http.close()


def test_reported_unavailable_coverage_does_not_require_a_head_read() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/coverage"):
            return _response(
                {
                    "coverage": {
                        "coverage": "unavailable",
                        "partitions": [
                            {
                                "partition_id": PARTITION,
                                "coverage": "unavailable",
                                "head_sequence": None,
                                "head_record_digest": None,
                                "reason": "authoritative prefix is unavailable",
                            }
                        ],
                        "reason": None,
                    }
                }
            )
        return httpx.Response(
            503,
            json={"error_code": "home-history-unavailable", "message": "unavailable"},
        )

    client, http = _client(handler)
    try:
        coverage = client.coverage((PARTITION,))
        assert coverage.state is JournalCoverageState.UNAVAILABLE
        assert coverage.partitions == ()
        assert coverage.reason == "authoritative prefix is unavailable"
    finally:
        http.close()

    assert [request.url.path for request in requests] == [
        f"/already-scoped/journal/streams/{HOME_STREAM_ID}/coverage"
    ]


def test_coverage_head_refusal_remains_unavailable(tmp_path: Path) -> None:
    fixture = _peer_fixture(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/coverage"):
            return _response(
                {
                    "coverage": {
                        "coverage": "exact",
                        "partitions": [
                            {
                                "partition_id": PARTITION,
                                "coverage": "exact",
                                "head_sequence": fixture.head.sequence,
                                "head_record_digest": fixture.head.record_digest,
                                "reason": None,
                            }
                        ],
                        "reason": None,
                    }
                }
            )
        return httpx.Response(
            503,
            json={"error_code": "home-history-unavailable", "message": "unavailable"},
        )

    client, http = _client(handler)
    try:
        coverage = client.coverage((PARTITION,))
        assert coverage.state is JournalCoverageState.UNAVAILABLE
        assert coverage.partitions == ()
        assert coverage.reason == f"authoritative head is unavailable for partition {PARTITION!r}"
    finally:
        http.close()


@pytest.mark.parametrize("failure_kind", ["substituted-identity", "transport"])
def test_coverage_propagates_head_verification_and_transport_failures(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    fixture = _peer_fixture(tmp_path)
    other_stream = JournalStreamIdentityV1(
        instance_id=_stream().instance_id,
        journal_family=_stream().journal_family,
        stream_id="substituted",
    )
    substituted_head = JournalPartitionHeadV1(
        stream=other_stream,
        partition_id=PARTITION,
        sequence=0,
        record_digest=journal_genesis_digest(other_stream, PARTITION),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/coverage"):
            return _response(
                {
                    "coverage": {
                        "coverage": "exact",
                        "partitions": [
                            {
                                "partition_id": PARTITION,
                                "coverage": "exact",
                                "head_sequence": fixture.head.sequence,
                                "head_record_digest": fixture.head.record_digest,
                                "reason": None,
                            }
                        ],
                        "reason": None,
                    }
                }
            )
        if failure_kind == "substituted-identity":
            return _response(_head_document(substituted_head))
        raise httpx.ConnectError("peer unavailable", request=request)

    client, http = _client(handler)
    try:
        error_type = (
            RemoteJournalVerificationError
            if failure_kind == "substituted-identity"
            else RemoteJournalTransportError
        )
        with pytest.raises(error_type):
            client.coverage((PARTITION,))
    finally:
        http.close()


@pytest.mark.parametrize("failure_kind", ["connect", "invalid-url"])
def test_http_failures_are_typed_without_exposing_authorization(failure_kind: str) -> None:
    def disconnected(request: httpx.Request) -> httpx.Response:
        detail = f"peer failure included {AUTHORIZATION}"
        if failure_kind == "connect":
            raise httpx.ConnectError(detail, request=request)
        raise httpx.InvalidURL(detail)

    client, http = _client(disconnected)
    try:
        with pytest.raises(RemoteJournalTransportError, match="transport failed") as refused:
            client.read_head(PARTITION)
        assert AUTHORIZATION not in str(refused.value)
    finally:
        http.close()


def test_success_format_drift_is_a_typed_verification_failure() -> None:

    def drifted(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "format_version": "different-format",
                "head": {},
            },
        )

    client, http = _client(drifted)
    try:
        with pytest.raises(RemoteJournalVerificationError, match="format version"):
            client.read_head(PARTITION)
    finally:
        http.close()

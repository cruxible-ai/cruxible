"""Capture uploads: parts staged under a daemon-owned root, read once as a Capture, deleted.

The store tests drive the session law directly (principal binding, part sizes,
retries, expiry, assembly, cleanup); the HTTP tests land a file through an acquire
Procedure's ``workspace.file`` read on a fresh instance with no workspace attached.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.captures import capture_contract_digest
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureOwnedContract,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    SourceNode,
)
from cruxible_client.contracts.uploads import (
    UPLOAD_PART_SIZE,
    CaptureUploadCompleteRequest,
    CaptureUploadOpenRequest,
)
from cruxible_client.contracts.workspace_file import WorkspaceFileSourceRequestV2
from cruxible_client.errors import CoreError
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from cruxible_core.service.uploads import CaptureUploadRefused, CaptureUploadStore, UploadLimits
from tests.core_support._pc_c_support import capture_contract
from tests.support.procedures import MAX_WALL_CLOCK, RUN_WALL_CLOCK
from tests.test_server.test_playbill_procedure_rung2_public import _authored, _policy
from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate
from tests.test_server.test_provider_installation import interface_entry

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
PROCEDURE = "advisory-acquire"
SECRET = b"TOPSECRET-BODY-BYTES"


def _sha(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _store(tmp_path: Path, **limits: Any) -> CaptureUploadStore:
    return CaptureUploadStore(
        tmp_path / "uploads-root",
        "inst_uploads",
        UploadLimits(
            max_bytes=limits.get("max_bytes", 4 * UPLOAD_PART_SIZE),
            session_seconds=limits.get("session_seconds", 600.0),
            max_open_sessions=limits.get("max_open_sessions", 4),
        ),
    )


def _open(store: CaptureUploadStore, content: bytes, *, principal: str = "alice", **fields: Any):
    return store.open(
        principal_id=principal,
        request=CaptureUploadOpenRequest(
            byte_length=fields.get("byte_length", len(content)),
            sha256=fields.get("sha256", _sha(content)),
            filename=fields.get("filename", "scan.sarif"),
        ),
        now=NOW,
    )


def _send(store: CaptureUploadStore, session: Any, content: bytes, principal: str = "alice"):
    for part in range(session.part_count):
        start = part * UPLOAD_PART_SIZE
        session = store.put_part(
            principal_id=principal,
            upload=session.upload_id,
            part=part,
            content=content[start : start + UPLOAD_PART_SIZE],
            now=NOW,
        )
    return session


def _sessions(root: Path) -> list[str]:
    """What the instance's staging area holds besides its lock file."""

    return sorted(name for name in os.listdir(root) if name != ".lock")


def _refusal(caught: pytest.ExceptionInfo[CaptureUploadRefused]) -> str:
    assert SECRET not in str(caught.value).encode()
    return caught.value.code


def test_parts_assemble_into_the_declared_bytes_and_staging_is_removed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = SECRET * (UPLOAD_PART_SIZE // len(SECRET) + 7)  # two parts, the last short
    session = _open(store, content)
    assert (session.part_count, session.missing_parts) == (2, (0, 1))
    session = _send(store, session, content)
    assert session.missing_parts == ()
    with store.staged(principal_id="alice", upload=session.upload_id, now=NOW) as (root, view):
        assert root.name == "uploads"
        staged = root / view.filename
        assert staged.read_bytes() == content
        assert staged.stat().st_mode & 0o777 == 0o600
    assert not (store.instance_root / session.upload_id).exists()
    with pytest.raises(CaptureUploadRefused) as caught:
        store.session(principal_id="alice", upload=session.upload_id, now=NOW)
    assert _refusal(caught) == "cruxible.upload.not_found"


def test_staging_is_removed_when_the_run_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _send(store, _open(store, SECRET), SECRET)
    with pytest.raises(RuntimeError):
        with store.staged(principal_id="alice", upload=session.upload_id, now=NOW):
            raise RuntimeError("the run failed")
    assert _sessions(store.instance_root) == []


def test_another_principal_never_sees_or_feeds_an_upload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _open(store, SECRET)
    for call in (
        lambda: store.session(principal_id="mallory", upload=session.upload_id, now=NOW),
        lambda: store.put_part(
            principal_id="mallory", upload=session.upload_id, part=0, content=SECRET, now=NOW
        ),
        lambda: store.staged(principal_id="mallory", upload=session.upload_id, now=NOW).__enter__(),
    ):
        with pytest.raises(CaptureUploadRefused) as caught:
            call()
        assert _refusal(caught) == "cruxible.upload.not_found"


@pytest.mark.parametrize("value", ["../escape", "a" * 31, "A" * 32, "../../" + "a" * 26])
def test_an_upload_id_is_never_a_path(tmp_path: Path, value: str) -> None:
    with pytest.raises(CaptureUploadRefused) as caught:
        _store(tmp_path).session(principal_id="alice", upload=value, now=NOW)
    assert _refusal(caught) == "cruxible.upload.not_found"


@pytest.mark.parametrize("name", ["../x", ".hidden", "a/b", "a\\b", "", "x" * 129, "a..b"])
def test_a_filename_is_one_plain_name(name: str) -> None:
    with pytest.raises(ValueError):
        CaptureUploadOpenRequest(byte_length=1, sha256=_sha(b"x"), filename=name)


def test_sizes_are_bounded_per_upload_and_per_part(tmp_path: Path) -> None:
    store = _store(tmp_path, max_bytes=10)
    with pytest.raises(CaptureUploadRefused) as caught:
        _open(store, b"x" * 11)
    assert (_refusal(caught), caught.value.http_status) == ("cruxible.upload.too_large", 413)
    session = _open(store, b"x" * 10)
    for part, content, code in (
        (1, b"x" * 10, "cruxible.upload.part_out_of_range"),
        (0, b"x" * 9, "cruxible.upload.part_size_mismatch"),
    ):
        with pytest.raises(CaptureUploadRefused) as caught:
            store.put_part(
                principal_id="alice", upload=session.upload_id, part=part, content=content, now=NOW
            )
        assert _refusal(caught) == code


def test_a_retried_part_must_be_the_same_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _open(store, SECRET)
    _send(store, session, SECRET)
    again = _send(store, session, SECRET)  # an identical retry is accepted
    assert again.missing_parts == ()
    with pytest.raises(CaptureUploadRefused) as caught:
        store.put_part(
            principal_id="alice",
            upload=session.upload_id,
            part=0,
            content=SECRET[::-1],
            now=NOW,
        )
    assert _refusal(caught) == "cruxible.upload.part_conflict"


def test_bytes_that_miss_the_declared_sha256_are_refused_and_dropped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _open(store, SECRET, sha256=_sha(b"something else"))
    _send(store, session, SECRET)
    with pytest.raises(CaptureUploadRefused) as caught:
        with store.staged(principal_id="alice", upload=session.upload_id, now=NOW):
            pytest.fail("a mismatched upload never reaches its run")
    assert _refusal(caught) == "cruxible.upload.digest_mismatch"
    assert _sessions(store.instance_root) == []


def test_an_incomplete_upload_stays_open_for_its_missing_parts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    content = b"y" * (UPLOAD_PART_SIZE + 1)
    session = _open(store, content)
    store.put_part(
        principal_id="alice",
        upload=session.upload_id,
        part=1,
        content=content[UPLOAD_PART_SIZE:],
        now=NOW,
    )
    with pytest.raises(CaptureUploadRefused) as caught:
        store.staged(principal_id="alice", upload=session.upload_id, now=NOW).__enter__()
    assert _refusal(caught) == "cruxible.upload.incomplete"
    assert "0" in str(caught.value)
    assert store.session(principal_id="alice", upload=session.upload_id, now=NOW).missing_parts == (
        0,
    )


def test_a_link_planted_in_a_session_is_never_followed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(SECRET)
    session = _open(store, SECRET)
    (store.instance_root / session.upload_id / "parts" / "0").symlink_to(outside)
    assert store.session(principal_id="alice", upload=session.upload_id, now=NOW).missing_parts == (
        0,
    )
    with pytest.raises(CaptureUploadRefused) as caught:
        store.staged(principal_id="alice", upload=session.upload_id, now=NOW).__enter__()
    assert _refusal(caught) == "cruxible.upload.incomplete"
    assert outside.read_bytes() == SECRET


def test_sessions_expire_and_are_swept_at_the_next_open(tmp_path: Path) -> None:
    store = _store(tmp_path, session_seconds=60.0, max_open_sessions=1)
    first = _open(store, SECRET)
    with pytest.raises(CaptureUploadRefused) as caught:
        _open(store, SECRET)
    assert (_refusal(caught), caught.value.http_status) == (
        "cruxible.upload.sessions_exhausted",
        429,
    )
    later = NOW + timedelta(seconds=61)
    with pytest.raises(CaptureUploadRefused) as caught:
        store.put_part(
            principal_id="alice", upload=first.upload_id, part=0, content=SECRET, now=later
        )
    assert (_refusal(caught), caught.value.http_status) == ("cruxible.upload.expired", 410)
    stale = _open(store, SECRET)
    (store.instance_root / "stray").write_bytes(b"not a session")
    store.open(
        principal_id="alice",
        request=CaptureUploadOpenRequest(byte_length=1, sha256=_sha(b"z"), filename="z"),
        now=NOW + timedelta(seconds=200),
    )
    remaining = _sessions(store.instance_root)
    assert stale.upload_id not in remaining and "stray" not in remaining
    assert len(remaining) == 1


def test_a_second_completion_is_refused_while_the_first_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _send(store, _open(store, SECRET), SECRET)
    with store.staged(principal_id="alice", upload=session.upload_id, now=NOW):
        with pytest.raises(CaptureUploadRefused) as caught:
            store.staged(principal_id="alice", upload=session.upload_id, now=NOW).__enter__()
        assert _refusal(caught) == "cruxible.upload.in_progress"
        with pytest.raises(CaptureUploadRefused) as caught:
            store.put_part(
                principal_id="alice", upload=session.upload_id, part=0, content=SECRET, now=NOW
            )
        assert _refusal(caught) == "cruxible.upload.in_progress"


def test_concurrent_opens_never_exceed_the_session_cap(tmp_path: Path, monkeypatch) -> None:
    """UPLOAD-001: the count and the creation are one decision under the instance lock."""

    from cruxible_core.service import uploads as module

    real_sweep = module.CaptureUploadStore._sweep

    def slow_sweep(self: CaptureUploadStore, now: datetime) -> int:
        counted = real_sweep(self, now)
        time.sleep(0.05)  # the window two unlocked openers both saw as free
        return counted

    monkeypatch.setattr(module.CaptureUploadStore, "_sweep", slow_sweep)
    start = threading.Barrier(6)
    outcomes: list[str] = []

    def opener() -> None:
        # A fresh store per call, as the manager builds them.
        store = _store(tmp_path, max_open_sessions=2)
        start.wait()
        try:
            _open(store, SECRET)
            outcomes.append("opened")
        except CaptureUploadRefused as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["cruxible.upload.sessions_exhausted"] * 4 + ["opened"] * 2
    assert len(_sessions(_store(tmp_path).instance_root)) == 2


def test_an_open_mid_initialization_is_never_swept(tmp_path: Path, monkeypatch) -> None:
    """UPLOAD-001: a second opener cannot see (and delete) a session before its record lands."""

    from cruxible_core.service import uploads as module

    real_write = module._write_new
    first_writing = threading.Event()

    def slow_write(path: Path, content: bytes) -> None:
        if path.name == "session.json" and not first_writing.is_set():
            first_writing.set()
            time.sleep(0.2)
        real_write(path, content)

    monkeypatch.setattr(module, "_write_new", slow_write)
    results: dict[str, object] = {}

    def first() -> None:
        try:
            results["first"] = _open(_store(tmp_path), SECRET)
        except Exception as exc:  # noqa: BLE001 - the regression is any failure here
            results["first"] = exc

    thread = threading.Thread(target=first)
    thread.start()
    assert first_writing.wait(5)
    second = _open(_store(tmp_path), SECRET, principal="bob")
    thread.join()
    first_session = results["first"]
    assert not isinstance(first_session, Exception), first_session
    store = _store(tmp_path)
    assert store.session(principal_id="alice", upload=first_session.upload_id, now=NOW)  # type: ignore[attr-defined]
    assert store.session(principal_id="bob", upload=second.upload_id, now=NOW)


def test_a_completion_that_outlives_its_expiry_keeps_its_staged_file(tmp_path: Path) -> None:
    """UPLOAD-002: expiry (a sweep or the owner's poll) never takes a live completion's file."""

    store = _store(tmp_path, session_seconds=1.0, max_open_sessions=4)
    session = _send(store, _open(store, SECRET), SECRET)
    later = NOW + timedelta(seconds=2)
    with store.staged(principal_id="alice", upload=session.upload_id, now=NOW) as (root, view):
        staged = root / view.filename
        # Another caller's open sweeps after Alice's expiry...
        _store(tmp_path).open(
            principal_id="bob",
            request=CaptureUploadOpenRequest(
                byte_length=5, sha256=_sha(b"other"), filename="other"
            ),
            now=later,
        )
        assert staged.read_bytes() == SECRET
        # ...and so does the owner's own poll and late part.
        for call in (
            lambda: store.session(principal_id="alice", upload=session.upload_id, now=later),
            lambda: store.put_part(
                principal_id="alice", upload=session.upload_id, part=0, content=SECRET, now=later
            ),
        ):
            with pytest.raises(CaptureUploadRefused) as caught:
                call()
            assert _refusal(caught) == "cruxible.upload.in_progress"
        assert staged.read_bytes() == SECRET
    assert session.upload_id not in _sessions(store.instance_root)


def test_a_lease_a_crashed_completion_left_is_recovered(tmp_path: Path) -> None:
    """UPLOAD-002: a lease nobody holds is not a live completion."""

    store = _store(tmp_path, session_seconds=60.0)
    session = _send(store, _open(store, SECRET), SECRET)
    directory = store.instance_root / session.upload_id
    (directory / "completing").write_bytes(b"")  # left by a process that died
    (directory / "uploads").mkdir()
    with store.staged(principal_id="alice", upload=session.upload_id, now=NOW) as (root, view):
        assert (root / view.filename).read_bytes() == SECRET
    abandoned = _send(store, _open(store, SECRET), SECRET)
    (store.instance_root / abandoned.upload_id / "completing").write_bytes(b"")
    _open(store, SECRET, principal="bob")  # within its expiry: kept
    assert abandoned.upload_id in _sessions(store.instance_root)
    store.open(
        principal_id="bob",
        request=CaptureUploadOpenRequest(byte_length=1, sha256=_sha(b"z"), filename="z"),
        now=NOW + timedelta(seconds=61),
    )
    assert abandoned.upload_id not in _sessions(store.instance_root)


class _Inner:
    def __init__(self) -> None:
        self.reached = False
        self.body = b""

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        self.reached = True
        while True:
            message = await receive()
            self.body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _drive(limiter_app, *, chunks: int, chunk: int, headers=()) -> tuple[int, int]:  # noqa: ANN001
    """Run one part request through the limiter; return (status, chunks consumed)."""

    import asyncio

    consumed = 0
    statuses: list[int] = []

    async def receive():  # noqa: ANN202
        nonlocal consumed
        consumed += 1
        return {"type": "http.request", "body": b"\x00" * chunk, "more_body": consumed < chunks}

    async def send(message) -> None:  # noqa: ANN001
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/api/v1/inst_uploads/uploads/{'a' * 32}/parts/0",
        "headers": list(headers),
    }
    asyncio.run(limiter_app(scope, receive, send))
    return statuses[0], consumed


def test_a_part_body_is_bounded_before_it_is_buffered() -> None:
    """UPLOAD-003: reading stops at the bound; the handler is never reached."""

    from cruxible_core.server.upload_part_limit import UploadPartBodyLimit

    # A chunked stream with no Content-Length: 64 chunks of 64 KiB offered.
    inner = _Inner()
    status, consumed = _drive(UploadPartBodyLimit(inner), chunks=64, chunk=65_536)
    assert (status, inner.reached) == (413, False)
    assert consumed == UPLOAD_PART_SIZE // 65_536 + 1  # the bound plus the one chunk past it
    # A declared length above the bound refuses before reading a byte.
    inner = _Inner()
    status, consumed = _drive(
        UploadPartBodyLimit(inner),
        chunks=64,
        chunk=65_536,
        headers=[(b"content-length", str(64 * 65_536).encode())],
    )
    assert (status, consumed, inner.reached) == (413, 0, False)
    # A body within the bound reaches the handler unchanged.
    inner = _Inner()
    status, _consumed = _drive(UploadPartBodyLimit(inner), chunks=8, chunk=65_536)
    assert (status, inner.reached, len(inner.body)) == (200, True, UPLOAD_PART_SIZE)


# -- over HTTP --------------------------------------------------------------------

ADVISORY = b"high"


def _contracts() -> tuple[ProcedureOwnedContract, ProcedureOwnedContract]:
    from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema

    return (
        ProcedureOwnedContract(
            identity=ArtifactIdentity(kind="Contract", name=f"{PROCEDURE}-input"),
            schema=ContractSchema(fields={"relative_path": PropertySchema(type="string")}),
        ),
        ProcedureOwnedContract(
            identity=ArtifactIdentity(kind="Contract", name=f"{PROCEDURE}-output"),
            schema=ContractSchema(fields={"severity": PropertySchema(type="string")}),
        ),
    )


def _definition(interface: dict[str, Any], provider: dict[str, Any], contract_pin: ArtifactPin):
    input_contract, output_contract = _contracts()

    def pin(role: str, contract: ProcedureOwnedContract) -> ArtifactPin:
        return ArtifactPin(
            role=role,
            target=contract.identity,
            artifact_digest=procedure_owned_contract_digest(contract).tagged,
        )

    request = WorkspaceFileSourceRequestV2(
        logical_source="commerce.production.orders",
        relative_path="placeholder",
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "workspace"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
        replayability="exact",
    ).model_dump(mode="json")
    request["relative_path"] = "$input.relative_path"
    return ProcedureDefinition(
        name=PROCEDURE,
        description="Read an uploaded advisory and return its severity.",
        contract_in=pin("contract-in", input_contract),
        contract_out=pin("contract-out", output_contract),
        nodes=(
            SourceNode(
                node_id="read",
                capture_contract=contract_pin,
                provider=ArtifactPin(
                    role="provider",
                    target=ArtifactIdentity(
                        kind="Provider", name=provider["provider_identity"].split(":", 1)[1]
                    ),
                    artifact_digest=provider["provider_artifact_digest"],
                ),
                interface=ArtifactPin(
                    role="provider-interface",
                    target=ArtifactIdentity(
                        kind="ProviderInterface", name=interface["identity"].split(":", 1)[1]
                    ),
                    artifact_digest=interface["artifact_digest"],
                ),
                interface_digest=interface["interface_digest"],
                implementation_digest=provider["implementation_digest"],
                request=request,
                as_="advisory",
                next="shape",
            ),
            ProjectNode(
                node_id="shape",
                fields={"severity": "$steps.advisory.content.text"},
                contract_out=pin("contract-out", output_contract),
                as_="result",
            ),
        ),
        returns="result",
        budget=ProcedureBudget(
            wall_clock=RUN_WALL_CLOCK,
            max_provider_calls=2,
            max_capture_bytes=65_536,
            max_items=None,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=MAX_WALL_CLOCK,
            max_provider_calls=4,
            max_capture_bytes=131_072,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )


@pytest.fixture
def upload_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[tuple[CruxibleClient, str, Path]]:
    """A fresh instance with no workspace, and (unless marked) an operator upload root."""

    state = tmp_path / "server-state"
    upload_root = tmp_path / "upload-root"
    if getattr(request, "param", True):
        (state / "daemon").mkdir(parents=True)
        (state / "daemon" / "provider-runtime.json").write_text(
            json.dumps({"upload_root": str(upload_root.resolve())})
        )
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    registered = get_registry().create_governed_instance_with_id("inst_uploads")
    instance_id = registered.record.instance_id
    managed = Path(registered.record.location)
    owner = generate_client_principal_key(
        tmp_path / "owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    reviewer = generate_client_principal_key(
        tmp_path / "reviewer-custody",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    with TestClient(create_app()) as http:
        initialized = http.post(
            f"/api/v1/{instance_id}/init",
            json={
                "principals": [
                    owner.principal.model_dump(mode="json"),
                    reviewer.principal.model_dump(mode="json"),
                ]
            },
        )
        assert initialized.status_code == 200, initialized.text
        transport = CruxibleClient(base_url="http://cruxible")
        transport._client = http  # type: ignore[assignment]
        transport.reviewer_key = reviewer.private_key_path  # type: ignore[attr-defined]
        yield transport, instance_id, upload_root
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def _author_acquire(transport: CruxibleClient, instance_id: str) -> None:
    from pydantic import TypeAdapter

    from cruxible_client.contracts.authoring.models import (
        AuthoringChangeSetMember,
        authoring_member_identity,
    )

    interface = interface_entry(transport, instance_id, "workspace.file")
    (provider,) = interface["providers"]
    contract = capture_contract()
    contract_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    definition = _definition(interface, provider, contract_pin)
    policy = _policy().model_copy(
        update={
            "identity": ArtifactIdentity(kind="SourceAcquisitionPolicy", name="advisory-uploads")
        }
    )
    members: list[dict[str, Any]] = [
        {
            "tag": "playbill-capture-contract-authoring-payload-v1",
            "capture_contract": contract.model_dump(mode="json"),
        },
        {
            "tag": "playbill-source-acquisition-policy-authoring-payload-v1",
            "acquisition_policy": policy.model_dump(mode="json"),
        },
        {
            "tag": "playbill-procedure-authoring-payload-v2",
            "definition": _authored(definition, same_set_kinds={"CaptureContract"}),
            "activation_policy": "drain",
            "owned_contracts": [
                item.model_dump(mode="json", by_alias=True) for item in _contracts()
            ],
            "acquisition_policy": "advisory-uploads",
            "retire": False,
        },
    ]
    adapter = TypeAdapter(AuthoringChangeSetMember)
    members.sort(
        key=lambda item: authoring_member_identity(adapter.validate_python(item)).encode("utf-8")
    )
    compiled = transport.compile_authoring(
        instance_id,
        payload={
            "tag": "playbill-change-set-authoring-payload-v1",
            "members": members,
            "rationale": "An acquire Procedure that reads uploaded advisories.",
        },
    )
    assert compiled.verdict == "passed", compiled.frontier
    submitted = transport.submit_authoring_intent(
        instance_id, str(compiled.certificate.intent_id), accept="never"
    )
    assert submitted.status.proposal_id is not None, submitted
    _approve_and_activate(
        transport._client,  # type: ignore[arg-type]
        instance_id,
        transport.reviewer_key,  # type: ignore[attr-defined]
        submitted.status.proposal_id,
    )


def test_an_upload_lands_as_a_capture_through_the_acquire_procedure(
    upload_host: tuple[CruxibleClient, str, Path],
) -> None:
    transport, instance_id, upload_root = upload_host
    _author_acquire(transport, instance_id)

    # A refusal before staging leaves the upload open for a corrected retry.
    session = transport.open_capture_upload(
        instance_id, byte_length=len(ADVISORY), sha256=_sha(ADVISORY), filename="advisory.txt"
    )
    transport.put_capture_upload_part(instance_id, session.upload_id, 0, ADVISORY)
    with pytest.raises(CoreError) as caught:
        transport.complete_capture_upload(
            instance_id,
            session.upload_id,
            CaptureUploadCompleteRequest(procedure=PROCEDURE, input={"relative_path": "x"}),
        )
    assert getattr(caught.value, "error_code", None) == "cruxible.upload.input_path_conflict"
    with pytest.raises(CoreError):
        transport.complete_capture_upload(
            instance_id, session.upload_id, CaptureUploadCompleteRequest(procedure="missing")
        )
    assert transport.capture_upload_status(instance_id, session.upload_id).missing_parts == ()

    completed = transport.complete_capture_upload(
        instance_id, session.upload_id, CaptureUploadCompleteRequest(procedure=PROCEDURE)
    )
    run = completed.run
    assert run.status == "succeeded", run.model_dump_json(indent=2)
    assert run.result == {"severity": "high"}
    (observation,) = run.source_observations
    assert observation.capture_digest is not None
    assert observation.source_read_receipt is not None
    assert observation.source_read_receipt.relative_path == "advisory.txt"
    assert observation.source_read_receipt.bytes_digest == _sha(ADVISORY)
    # Staging is gone, and the upload with it.
    assert _sessions(upload_root / instance_id) == []
    with pytest.raises(CoreError) as caught:
        transport.capture_upload_status(instance_id, session.upload_id)
    assert getattr(caught.value, "error_code", None) == "cruxible.upload.not_found"

    # The one-call client path does the same.
    again = transport.upload_capture(
        instance_id, b"low", filename="second.txt", procedure=PROCEDURE
    )
    assert again.run.status == "succeeded"
    assert again.run.result == {"severity": "low"}


def test_a_part_travels_as_raw_octets_under_the_edge_cap(
    upload_host: tuple[CruxibleClient, str, Path],
) -> None:
    transport, instance_id, _root = upload_host
    content = b"\x00\xff" * (UPLOAD_PART_SIZE // 2) + b"ZQXJ"
    session = transport.open_capture_upload(
        instance_id, byte_length=len(content), sha256=_sha(content), filename="scan.bin"
    )
    assert (session.part_size, session.part_count) == (UPLOAD_PART_SIZE, 2)
    held = transport.put_capture_upload_part(
        instance_id, session.upload_id, 0, content[:UPLOAD_PART_SIZE]
    )
    assert held.parts_received == (0,)
    oversized = transport._client.post(  # type: ignore[attr-defined]
        f"/api/v1/{instance_id}/uploads/{session.upload_id}/parts/1",
        content=content[UPLOAD_PART_SIZE:] + b"!",
        headers={"content-type": "application/octet-stream"},
    )
    assert oversized.status_code == 400
    assert oversized.json()["error_code"] == "cruxible.upload.part_size_mismatch"
    assert b"ZQXJ" not in oversized.content
    # Raw octets are admitted on the part route alone; a CORS-simple type and any
    # other route keep the browser guard.
    http = transport._client  # type: ignore[attr-defined]
    part_route = f"/api/v1/{instance_id}/uploads/{session.upload_id}/parts/1"
    for route, media in (
        (part_route, "text/plain"),
        (f"/api/v1/{instance_id}/bodies", "application/octet-stream"),
    ):
        refused = http.post(route, content=b"tail", headers={"content-type": media})
        assert refused.json()["error_type"] == "OriginNotAllowedError", route
    hostile = http.post(
        part_route,
        content=content[UPLOAD_PART_SIZE:],
        headers={"content-type": "application/octet-stream", "origin": "https://evil.test"},
    )
    assert hostile.status_code == 403


def test_an_oversized_part_is_refused_before_any_handler_or_permission_runs(
    upload_host: tuple[CruxibleClient, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UPLOAD-003 over the real app: the limit runs before the route, so before its gate."""

    from cruxible_core.runtime import playbill_api

    transport, instance_id, _root = upload_host

    def unreachable(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the part handler ran on an oversized body")

    monkeypatch.setattr(playbill_api, "playbill_capture_upload_part", unreachable)
    http = transport._client  # type: ignore[attr-defined]
    route = f"/api/v1/{instance_id}/uploads/{'b' * 32}/parts/0"
    for body in (b"\x00" * (UPLOAD_PART_SIZE + 1), iter([b"\x00" * 65_536] * 9)):
        refused = http.post(
            route, content=body, headers={"content-type": "application/octet-stream"}
        )
        assert refused.status_code == 413
        assert refused.json()["error_code"] == "cruxible.upload.part_too_large"


@pytest.mark.parametrize("upload_host", [False], indirect=True)
def test_a_daemon_without_an_upload_root_refuses_uploads(
    upload_host: tuple[CruxibleClient, str, Path],
) -> None:
    transport, instance_id, _root = upload_host
    with pytest.raises(CoreError) as caught:
        transport.open_capture_upload(
            instance_id, byte_length=1, sha256=_sha(b"x"), filename="x.txt"
        )
    assert getattr(caught.value, "error_code", None) == "cruxible.upload.not_configured"

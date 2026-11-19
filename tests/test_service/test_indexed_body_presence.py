"""Every read surface takes presence from sequenced landings, with no file probes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_core.coverage.adapter import observe_working_source
from cruxible_core.coverage.contracts import LogicalSourceIdentity
from cruxible_core.service.discovery import claim_status, coverage
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.discovery.query import build_accepted_query_facts
from cruxible_core.service.evidence.evidence import (
    _current_replay_available,
    service_evaluate_playbill_claim_verdict,
)
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.storage.read_positions import read_positions, reading_at
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world

WHEN = datetime(2026, 8, 21, 14, tzinfo=UTC)
ACCESS = BodyAccessContext(principal_id="presence-test", can_read_body=True)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, str, str]:
    instance, _owner, _actor, claim, *_rest = shared_capture_world(
        tmp_path_factory.mktemp("indexed-presence")
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        artifact = projection.typed.source(f"Claim:{claim}")
    return instance, claim, artifact.backing.capture_digests[0]


def _read(world: tuple[Any, str, str], path: str) -> Any:
    instance, claim, capture = world
    coordinate = instance.accepted_coordinate()
    if path == "get":
        return service_playbill_get(
            instance,
            request=GetRequest(ref=f"Capture:{capture}", evaluation_time=WHEN),
            access=ACCESS,
        )
    if path == "evidence":
        return service_playbill_get(
            instance,
            request=GetRequest(ref=claim, detail="evidence", evaluation_time=WHEN),
            access=ACCESS,
        )
    if path == "claim":
        return service_evaluate_playbill_claim_verdict(
            instance, claim_identity=claim, evaluation_time=WHEN
        )
    if path == "query":
        return service_playbill_query(
            instance, request=QueryRequest(kind="project.work_item", evaluation_time=WHEN)
        )
    if path == "facts":
        return build_accepted_query_facts(instance, coordinate=coordinate)
    if path == "orient":
        return service_playbill_orient(instance, evaluation_time=WHEN)
    if path == "coverage":
        result = coverage.service_resolve_playbill_coverage(
            instance,
            instance_id=instance.descriptor.instance_id,
            observations=(
                observe_working_source(
                    LogicalSourceIdentity(plane="external", identity="repo.work-items"),
                    b"status: ready",
                ),
            ),
        )
        return result.model_dump(exclude={"epoch", "manifest_digest"})
    raise AssertionError(path)


def _presence_probes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    probes: list[str] = []
    byte_reads = 0
    original_read = ContentAddressedBodyStore.read

    def read_bytes(self: Any, *args: Any, **kwargs: Any) -> bytes:
        nonlocal byte_reads
        byte_reads += 1
        try:
            return original_read(self, *args, **kwargs)
        finally:
            byte_reads -= 1

    monkeypatch.setattr(ContentAddressedBodyStore, "read", read_bytes)
    # Byte reads are permitted and keep their descriptor verification. These
    # methods inspect body files to answer only presence or metadata availability.
    for name in ("verify", "file_identity", "_object_status", "metadata", "_known"):
        original = getattr(ContentAddressedBodyStore, name)

        def counted(
            self: Any, *args: Any, _name: str = name, _call: Any = original, **kwargs: Any
        ) -> Any:
            if not byte_reads:
                probes.append(_name)
            return _call(self, *args, **kwargs)

        monkeypatch.setattr(ContentAddressedBodyStore, name, counted)
    return probes


PATHS = ("get", "evidence", "claim", "query", "facts", "orient", "coverage")


@pytest.mark.parametrize("path", PATHS)
def test_reads_do_no_body_file_probes_for_presence(
    world: Any, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _read(world, path)  # immutable metadata may need verified bytes on a cold miss
    probes = _presence_probes(monkeypatch)
    _read(world, path)
    assert probes == []


@pytest.mark.parametrize("damage", ("missing", "corrupt"))
@pytest.mark.parametrize("path", PATHS)
def test_recorded_damage_is_unavailable_and_repair_restores_each_read(
    world: Any, damage: str, path: str
) -> None:
    instance, _claim, capture = world
    store = instance.body_store()
    body = store._path(capture)
    original = body.read_bytes()
    before = _read(world, path)
    positions = read_positions(instance)
    assert _current_replay_available(instance, capture, readers={})
    try:
        if damage == "missing":
            body.unlink()
        else:
            body.write_bytes(b"corrupt")
        # No file check on metadata reads: damage takes effect when observed.
        assert _read(world, path) == before
        assert store.availability(capture) == damage
        after = _read(world, path)
        if path not in {"claim", "query", "orient"}:
            assert after != before
        assert not _current_replay_available(instance, capture, readers={})
        # A fresh derivation at the same position agrees with the served answer.
        claim_status.reset_claim_resolution_memo()
        assert _read(world, path) == after
        with reading_at(positions):
            assert _current_replay_available(instance, capture, readers={})
    finally:
        body.write_bytes(original)
        assert store.availability(capture) == "present"
    assert _read(world, path) == before


def test_nested_get_keeps_the_position_it_started_at(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.discovery import get

    instance, _claim, capture = world
    store = instance.body_store()
    before = _read(world, "get")
    resolve = get.resolve_get_ref
    original = store._path(capture).read_bytes()

    def damage_after_resolution(*args: Any, **kwargs: Any) -> Any:
        ref = resolve(*args, **kwargs)
        store._path(capture).write_bytes(b"rot")
        assert store.availability(capture) == "corrupt"
        return ref

    try:
        with monkeypatch.context() as patch:
            patch.setattr(get, "resolve_get_ref", damage_after_resolution)
            assert _read(world, "get") == before
        assert _read(world, "get").card.status == "unavailable"
    finally:
        store._path(capture).write_bytes(original)
        assert store.availability(capture) == "present"


def test_write_dry_run_keeps_new_bytes_and_checks_existing_presence_without_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.write import WriteRequest
    from cruxible_core.service.authoring.write_verbs import service_playbill_write
    from tests.core_support._write_support import KIND, caller, seed_write_surface

    instance, _owner = seed_write_surface(tmp_path)
    request = WriteRequest.model_validate(
        {
            "because": "Use the retained self evidence.",
            "dry_run": True,
            "changes": [
                {"op": "set", "subject": f"{KIND}/wi-1", "field": "title", "value": "A title"}
            ],
        }
    )
    expected = service_playbill_write(instance, request=request, caller=caller())
    probes = _presence_probes(monkeypatch)
    actual = service_playbill_write(instance, request=request, caller=caller())
    assert actual.status == expected.status == "would_accept"
    assert probes == []


def test_preflight_reuse_checks_indexed_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_proposals.test_prepared_lowering import _setup

    instance, coordinator, actor, intent = _setup(tmp_path)
    expected = coordinator.preflight(intent.intent_id, actor=actor)
    probes = _presence_probes(monkeypatch)
    assert coordinator.preflight(intent.intent_id, actor=actor) == expected
    assert probes == []


@pytest.mark.parametrize("damage", ("missing", "corrupt"))
def test_existing_capture_preview_refuses_recorded_damage_and_recovers(
    tmp_path: Path, damage: str
) -> None:
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from tests.test_authoring.test_authoring_preflight import TIMESTAMP

    instance, _owner, actor, claim, _other, _coordinator, payload = shared_capture_world(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    payload = payload.model_copy(
        update={"statement": payload.statement.model_copy(update={"qualifier": "preview-presence"})}
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        digest = projection.typed.source(f"Claim:{claim}").backing.capture_digests[0]
    store = instance.body_store()
    path = store._path(digest)
    original = path.read_bytes()

    def preview() -> str:
        _intent, computed = coordinator.preview(
            actor=actor, payload=payload, canonical_timestamp=TIMESTAMP
        )
        return computed.result.verdict

    assert preview() == "passed"
    try:
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"corrupt")
        assert store.availability(digest) == damage
        assert preview() == "refused"
    finally:
        path.write_bytes(original)
        assert store.availability(digest) == "present"
    assert preview() == "passed"

"""Source routing reads the Capture's recorded source, with no historical backfill."""

from datetime import timedelta

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV1,
    CaptureRunCoordinateV1,
    render_capture_envelope,
)
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.procedures.windows import CaptureEventWindow, TriggerEventReference
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    EvidenceCommitment,
    ExternalSourceReference,
)
from cruxible_client.contracts.triggers import CaptureLandingSchedule, WindowCloseSchedule
from cruxible_core.service.procedures.line_dispatch import service_evaluate_line
from cruxible_core.service.procedures.procedure_runs import _journal, _stream
from cruxible_core.service.procedures.resolution_contracts import bind_window, read_capture_event
from tests.support.lines import line_trigger, trigger_members
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME


def _landing(instance, procedure, source, *, partition, at=READ_TIME):
    """Store a real envelope; the event itself carries no source field."""
    store = instance.body_store()
    body = store.store(b"{}")
    digest = "sha256:" + "a" * 64
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=SELECTOR.capture_contract_digest,
        source=(
            ExternalSourceReference(
                source_identity=source,
                producer_binding_digest=digest,
                coordinate_type="http-response-v1",
                coordinate={},
                selector_type="whole-response-v1",
                selector={},
                replayability="exact",
            )
            if source is not None
            else CasSourceReference(content_digest=body.digest)
        ),
        commitment=EvidenceCommitment(
            digest_kind="exact_bytes", digest=body.digest, byte_length=2, materialization="cas"
        ),
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="procedure",
            run_id="source-routing",
            bound_generation=instance.accepted_coordinate().generation_root,
            executable_identity=procedure.procedure.identity,
            executable_digest=procedure.artifact_digest,
        ),
        run_receipt_digest=digest,
        producer=procedure.procedure.identity,
        producer_binding_digest=digest,
        observed_at=at,
    )
    stored = store.store(render_capture_envelope(envelope))
    return capture(instance, procedure, partition=partition, at=at, capture_digest=stored.digest)


def _reference(stored):
    return TriggerEventReference(
        run_id=stored.record.run_id,
        partition_id=stored.record.partition_id,
        sequence=stored.record.sequence,
        record_digest=stored.record_digest,
    )


@pytest.mark.parametrize("window", [False, True])
def test_two_lines_route_one_contract_by_source_and_unrestricted_gets_both(tmp_path, window):
    def schedule(selector):
        return (
            WindowCloseSchedule(window=CaptureEventWindow(event=selector, duration_seconds=60))
            if window
            else CaptureLandingSchedule(event=selector)
        )

    instance, base, procedure, owner = line_world(
        tmp_path, schedule(SELECTOR), triggers=(), with_owner=True
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    lines = []
    for name, sources in (
        ("repo", ("github.repo.snapshot",)),
        ("sarif", ("sarif.log",)),
        ("all", ()),
    ):
        line = base.model_copy(update={"identity": ArtifactIdentity(kind="Line", name=name)})
        lines.append(line)
        tree[line_spec_path(name)] = render_line_spec(line)
        tree.update(
            trigger_members(
                line_trigger(
                    "on-" + name,
                    line=name,
                    schedule=schedule(SELECTOR.model_copy(update={"logical_sources": sources})),
                )
            )
        )
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T15:01:00.000000Z", proposal_name="route"
    )
    repo = _landing(instance, procedure, "github.repo.snapshot", partition="run:repo")
    sarif = _landing(instance, procedure, "sarif.log", partition="run:sarif")
    cas = _landing(instance, procedure, None, partition="run:cas")
    until = READ_TIME + timedelta(seconds=62)
    for line, expected in zip(
        lines,
        (
            {repo.record_digest},
            {sarif.record_digest},
            {repo.record_digest, sarif.record_digest, cas.record_digest},
        ),
        strict=True,
    ):
        result = service_evaluate_line(
            instance,
            line.identity.name,
            LineEvaluateRequest(until=until, dry_run=True),
            actor=None,
            now=until,
        )
        assert result.status == "met", result
        from cruxible_client.contracts.compact_query import QueryRequest
        from cruxible_core.service.discovery.compact_query import service_playbill_query
        from tests.test_service.test_operational_get import _get

        card = _get(instance, "Trigger:on-" + line.identity.name, evaluation_time=until).card
        event = card.schedule["window"]["event"] if window else card.schedule["event"]
        sources = {"repo": ["github.repo.snapshot"], "sarif": ["sarif.log"], "all": []}[
            line.identity.name
        ]
        assert event.get("logical_sources", []) == sources
        line_card = _get(instance, line.identity.qualified, evaluation_time=until).card
        assert all(source in line_card.triggers[0].fires for source in sources)
        listed = service_playbill_query(
            instance,
            request=QueryRequest(
                kind="Trigger",
                where=[{"field": "name", "eq": "on-" + line.identity.name}],
                select=["logical_sources"],
                evaluation_time=until,
            ),
        )
        assert listed.rows[0]["logical_sources"] == sources
        assert {item.binding.event.record_digest for item in result.occurrences} == expected
        if window:
            assert all(
                item.binding.window.ends_at == READ_TIME + timedelta(seconds=60)
                for item in result.occurrences
            )


def test_exact_event_and_window_refuse_a_different_source(tmp_path):
    instance, _line, procedure = line_world(tmp_path, CaptureLandingSchedule(event=SELECTOR))
    repo = _landing(instance, procedure, "github.repo.snapshot", partition="run:repo")
    cas = _landing(instance, procedure, None, partition="run:cas")
    selector = SELECTOR.model_copy(update={"logical_sources": ("github.repo.snapshot",)})
    assert read_capture_event(instance, selector, _reference(repo), now=READ_TIME)[0] == repo.record
    policy = CaptureEventWindow(event=selector, duration_seconds=60)
    assert bind_window(
        instance, policy, _reference(repo), now=READ_TIME
    ).ends_at == READ_TIME + timedelta(seconds=60)
    wrong = selector.model_copy(update={"logical_sources": ("sarif.log",)})
    for event in (repo, cas):
        with pytest.raises(ExecutionError, match="logical_sources"):
            read_capture_event(instance, wrong, _reference(event), now=READ_TIME)
        with pytest.raises(ExecutionError, match="logical_sources"):
            bind_window(
                instance,
                policy.model_copy(update={"event": wrong}),
                _reference(event),
                now=READ_TIME,
            )


def test_v6_index_migration_preserves_rows_and_progress_without_backfill(tmp_path, monkeypatch):
    from cruxible_core.exhaust import capture_selectors
    from cruxible_core.indexes.resident import close_idle_connections

    instance, _line, procedure = line_world(tmp_path, CaptureLandingSchedule(event=SELECTOR))
    old = _landing(instance, procedure, "sarif.log", partition="run:old")
    journal, _ = _journal(instance)
    index = journal.index
    args = dict(
        bodies=instance.body_store(),
        contract_digest=SELECTOR.capture_contract_digest,
        since=None,
        until=READ_TIME + timedelta(seconds=2),
        limit=10,
    )
    assert index.captures(_stream(instance), **args)[0] == (old,)
    with index.connection() as conn:
        identity = conn.execute("SELECT identity FROM index_identity").fetchone()[0]
        progress = tuple(conn.execute("SELECT * FROM capture_progress").fetchone())
        # Remove every v9- and v8-only object before simulating the actual v6 schema.
        for trigger in (
            "capture_landing_attributed",
            "capture_landing_unattributed",
            "capture_landing_lost",
            "capture_landing_found",
        ):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DROP INDEX capture_source_latest")
        conn.execute("DROP TABLE capture_source_tallies")
        conn.execute("DROP TABLE capture_unattributed")
        for column in ("source_lost", "landed_at", "stream"):
            conn.execute(f"ALTER TABLE capture_selectors DROP COLUMN {column}")
        conn.execute("DROP INDEX capture_name")
        conn.execute("DROP TABLE capture_contract_names")
        conn.execute("DROP TABLE capture_name_progress")
        conn.execute("ALTER TABLE capture_selectors DROP COLUMN contract_identity")
        conn.execute("DROP INDEX capture_contract_source")
        conn.execute("ALTER TABLE capture_selectors DROP COLUMN logical_source")
        conn.execute("PRAGMA user_version=6")
    close_idle_connections()
    with monkeypatch.context() as patch:
        patch.setattr(
            capture_selectors,
            "capture_envelope",
            lambda *a, **k: pytest.fail("historical envelope backfill"),
        )
        with index.connection() as conn:
            assert conn.execute("SELECT identity FROM index_identity").fetchone()[0] == identity
            assert tuple(conn.execute("SELECT * FROM capture_progress").fetchone()) == progress
            assert (
                conn.execute("SELECT logical_source FROM capture_selectors").fetchone()[0] is None
            )
        assert index.captures(_stream(instance), **args, logical_sources=("sarif.log",))[0] == ()
        assert index.captures(_stream(instance), **args)[0] == (old,)
    new = _landing(
        instance, procedure, "sarif.log", partition="run:new", at=READ_TIME + timedelta(seconds=1)
    )
    assert index.captures(_stream(instance), **args, logical_sources=("sarif.log",))[0] == (new,)


@pytest.mark.parametrize("size", [10, 100])
def test_landing_projection_handles_only_the_new_envelope_at_two_sizes(tmp_path, monkeypatch, size):
    from cruxible_core.exhaust import capture_selectors

    instance, _line, procedure = line_world(tmp_path, CaptureLandingSchedule(event=SELECTOR))
    journal, _ = _journal(instance)
    args = dict(
        bodies=instance.body_store(),
        contract_digest=SELECTOR.capture_contract_digest,
        since=None,
        until=READ_TIME + timedelta(seconds=2),
        limit=2,
        logical_sources=("sarif.log",),
    )
    for i in range(size):
        _landing(instance, procedure, "github.repo.snapshot", partition=f"run:background-{i}")
    assert journal.index.captures(_stream(instance), **args)[0] == ()
    reads = []
    original = capture_selectors.capture_envelope

    def tracked(*args, **kwargs):
        reads.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(capture_selectors, "capture_envelope", tracked)
    wanted = _landing(instance, procedure, "sarif.log", partition="run:wanted")
    assert journal.index.captures(_stream(instance), **args)[0] == (wanted,)
    assert len(reads) == 1
    assert journal.index.captures(_stream(instance), **args)[0] == (wanted,)
    assert len(reads) == 1
    with journal.index.connection() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT record_id FROM capture_selectors "
            "WHERE contract_digest=? AND logical_source IN (?)",
            (SELECTOR.capture_contract_digest, "sarif.log"),
        ).fetchall()
    assert any("capture_contract_source" in row[3] for row in plan)


def test_unavailable_envelope_preserves_unrestricted_event_discovery(tmp_path):
    instance, _line, procedure = line_world(tmp_path, CaptureLandingSchedule(event=SELECTOR))
    stored = _landing(instance, procedure, "sarif.log", partition="run:lost")
    journal, _ = _journal(instance)
    bodies = instance.body_store()
    from cruxible_core.exhaust.payloads import read_payload
    from cruxible_core.storage.cas import BodyAccessContext

    payload = read_payload(
        bodies,
        stored.record.payload_digest,
        access=BodyAccessContext(principal_id="test", can_read_body=True),
    )
    bodies.erase(payload["capture_digest"])
    args = dict(
        bodies=bodies,
        contract_digest=SELECTOR.capture_contract_digest,
        since=None,
        until=READ_TIME + timedelta(seconds=1),
        limit=10,
    )
    assert journal.index.captures(_stream(instance), **args)[0] == (stored,)
    assert (
        journal.index.captures(_stream(instance), **args, logical_sources=("sarif.log",))[0] == ()
    )
    with pytest.raises(ExecutionError, match="unavailable"):
        read_capture_event(
            instance,
            SELECTOR.model_copy(update={"logical_sources": ("sarif.log",)}),
            _reference(stored),
            now=READ_TIME,
        )

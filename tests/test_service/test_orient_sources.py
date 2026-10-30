"""``orient(section="sources")``: each logical source runs have landed Captures from.

A row per logical source, by name, from the landing records the activity feed
lists as ``capture_landed`` -- cited or not: how many landings it has, its
latest landing (when, the run and Line that landed it, the Capture's handle,
``Capture:`` ref, observation time and size and, as the Capture card says it,
whether the store can still produce it) and the CaptureContract versions they
were landed under, each with its count. Landings are operational state, read
live at the head whatever coordinate the read names. A landing whose record or
envelope is lost is counted in ``sources_unattributed``; a Capture naming no
logical source (a Procedure's emitted output) is in no row.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV1,
    CaptureRunCoordinateV1,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    render_capture_contract,
    render_capture_envelope,
)
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    EvidenceCommitment,
    ExternalSourceReference,
)
from cruxible_client.contracts.triggers import CaptureLandingSchedule
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.list_pages import ListCursorMismatch
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_line_triggers import SELECTOR, TRIGGER_CONTRACT, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)
KEV, PYPI = "cisa.kev.catalog", "pypi.release"
SECOND_CONTRACT = foreign_source_capture_contract(PYPI)
SECOND = capture_contract_digest(SECOND_CONTRACT).tagged
TRIGGER_DIGEST = capture_contract_digest(TRIGGER_CONTRACT).tagged


class SourcesWorld:
    """Landings nobody cites: three of KEV under one contract, PyPI under two.

    Also a Procedure's emitted output (no logical source) and one landing whose
    record payload is lost. ``before`` is the coordinate before PyPI's contract
    was accepted.
    """

    def __init__(self, root: Path) -> None:
        from tests.test_indexes.test_resolution_contracts import _accept_tree

        self.instance, _line, self.procedure, owner = line_world(
            root, CaptureLandingSchedule(event=SELECTOR), with_owner=True
        )
        self.before = self.instance.accepted_coordinate()
        tree = self.instance.tree_at(self.before.git_oid)
        tree[capture_contract_path(SECOND_CONTRACT.identity.name)] = render_capture_contract(
            SECOND_CONTRACT
        )
        _accept_tree(
            self.instance,
            owner,
            tree,
            timestamp="2026-08-24T15:05:00.000000Z",
            proposal_name="pypi-contract",
        )
        self.landed: dict[str, list[tuple[str, str]]] = {KEV: [], PYPI: []}
        for minute in range(3):
            self.land(KEV, TRIGGER_DIGEST, minute)
        self.land(PYPI, TRIGGER_DIGEST, 3)
        self.land(None, TRIGGER_DIGEST, 4)  # emitted output
        stored = self.land(PYPI, TRIGGER_DIGEST, 5)
        self.instance.body_store().erase(stored.record.payload_digest)  # lost record payload
        self.land(PYPI, SECOND, 6)

    def land(self, source: str | None, contract: str, minute: int) -> Any:
        """One run landing one Capture of ``source`` under ``contract``."""

        store = self.instance.body_store()
        at = READ_TIME + timedelta(minutes=minute)
        content = f"{source}-{minute}".encode()
        body = store.store(content)
        digest = "sha256:" + "a" * 64
        envelope = CaptureEnvelopeV1(
            capture_contract_digest=contract,
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
                digest_kind="exact_bytes",
                digest=body.digest,
                byte_length=len(content),
                materialization="cas",
            ),
            run_coordinate=CaptureRunCoordinateV1(
                run_kind="procedure",
                run_id=f"landing-{minute}",
                bound_generation=self.instance.accepted_coordinate().generation_root,
                executable_identity=self.procedure.procedure.identity,
                executable_digest=self.procedure.artifact_digest,
            ),
            run_receipt_digest=digest,
            producer=ArtifactIdentity(kind="Procedure", name="sources.fetch"),
            producer_binding_digest=digest,
            observed_at=at - timedelta(seconds=30),
        )
        captured = store.store(render_capture_envelope(envelope)).digest
        stored = capture(
            self.instance,
            self.procedure,
            partition=f"run:landing-{minute}",
            at=at,
            digest=contract,
            capture_digest=captured,
        )
        if source is not None:
            self.landed.setdefault(source, []).append((captured, str(stored.record.run_id)))
        return stored


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> SourcesWorld:
    return SourcesWorld(tmp_path_factory.mktemp("sources"))


def _handle(digest: str) -> str:
    return "CAP-" + digest.removeprefix("sha256:")[:12]


def _rows(instance: PlaybillInstance, **options: Any) -> dict[str, Any]:
    answer = service_playbill_orient(instance, section="sources", **options)
    assert answer.sources is not None
    return {row.logical_source: row for row in answer.sources}


def test_each_source_carries_its_landings_latest_landing_and_contracts(
    world: SourcesWorld,
) -> None:
    answer = service_playbill_orient(world.instance, section="sources", surface="mcp")

    assert answer.section == "sources" and answer.truncated is False
    assert answer.sources is not None
    assert [row.logical_source for row in answer.sources] == [KEV, PYPI]
    rows = {row.logical_source: row for row in answer.sources}
    trigger = TRIGGER_CONTRACT.identity.qualified
    kev = rows[KEV]
    assert kev.landings == 3
    assert [(c.contract, c.version, c.landings) for c in kev.contracts] == [(trigger, 1, 3)]
    pypi = rows[PYPI]
    # The landing whose payload is lost is attributed to no source.
    assert pypi.landings == 2
    assert sorted((c.contract, c.version, c.landings) for c in pypi.contracts) == sorted(
        [(trigger, 1, 1), (SECOND_CONTRACT.identity.qualified, 1, 1)]
    )
    assert answer.sources_unattributed == 1
    for source, row in rows.items():
        digest, run = world.landed[source][-1]
        latest = row.latest
        assert (latest.capture, latest.ref, latest.run) == (
            _handle(digest),
            f"Capture:{digest}",
            run,
        )
        assert latest.line is None  # a landing record with no admitting run names no Line
        assert latest.status == "available" and latest.erased is None
        assert latest.unavailable is False
        assert latest.observed_at == latest.landed_at - timedelta(seconds=30)
        assert latest.size == len(f"{source}-{6 if source == PYPI else 2}")
    assert rows[KEV].latest.landed_at == READ_TIME + timedelta(minutes=2)
    # Nobody cites any of it: the get card reads the Capture from its envelope.
    card = service_playbill_get(
        world.instance, request=GetRequest(ref=rows[KEV].latest.ref), access=_ACCESS
    ).card
    assert card is not None and card.citing_total == 0  # type: ignore[union-attr]
    assert card.source == KEV  # type: ignore[union-attr]
    # Landings are read live; the answer says so.
    assert answer.live is not None and answer.live.fields == ("sources",)
    assert answer.next[0] == f'cruxible_get(ref="{rows[KEV].latest.ref}")'


def test_sources_page_by_name_and_refuse_a_foreign_cursor(world: SourcesWorld) -> None:
    walked: list[str] = []
    cursor = None
    while True:
        page = service_playbill_orient(world.instance, section="sources", limit=1, cursor=cursor)
        assert page.sources is not None and len(page.sources) == 1
        assert page.sources_unattributed == 1
        walked.append(page.sources[0].logical_source)
        if page.next_cursor is None:
            assert page.truncated is False
            break
        assert page.truncated is True
        assert page.next[-1].startswith("cruxible orient --section sources --cursor ")
        cursor = page.next_cursor
    assert walked == [KEV, PYPI]

    first = service_playbill_orient(world.instance, section="sources", limit=1)
    with pytest.raises(ListCursorMismatch):
        service_playbill_orient(world.instance, section="activity", cursor=first.next_cursor)
    activity = service_playbill_orient(world.instance, section="activity", limit=1)
    with pytest.raises(ListCursorMismatch):
        service_playbill_orient(world.instance, section="sources", cursor=activity.next_cursor)


def test_the_section_reads_landings_live_whatever_coordinate_it_names(
    world: SourcesWorld,
) -> None:
    pinned = service_playbill_orient(world.instance, section="sources", at=world.before.git_oid)

    assert pinned.coordinate.git_oid == world.before.git_oid
    assert world.before.git_oid != world.instance.accepted_coordinate().git_oid
    assert pinned.live is not None and pinned.live.fields == ("sources",)
    # Landings and contract names are read at the head, not at the pinned coordinate.
    assert pinned.sources == service_playbill_orient(world.instance, section="sources").sources


def test_the_landings_are_the_ones_the_activity_feed_lists(world: SourcesWorld) -> None:
    activity = service_playbill_orient(world.instance, section="activity", limit=50).activity
    assert activity is not None
    landed = [row for row in activity if row.event == "capture_landed"]
    assert len(landed) == 7
    counted: dict[str, int] = {}
    for row in landed:
        if row.logical_source is not None:
            counted[row.logical_source] = counted.get(row.logical_source, 0) + 1
    assert {source: row.landings for source, row in _rows(world.instance).items()} == counted
    # The lost record is the feed's unavailable landing and the section's unattributed one.
    assert sum(1 for row in landed if row.unavailable) == 1


def test_a_lost_latest_payload_lists_the_landing_by_its_run(tmp_path: Path) -> None:
    lost = SourcesWorld(tmp_path)
    stored = lost.land(KEV, TRIGGER_DIGEST, 9)
    assert _rows(lost.instance)[KEV].latest.capture is not None  # projected while readable
    lost.instance.body_store().erase(stored.record.payload_digest)

    latest = _rows(lost.instance)[KEV].latest
    assert latest.unavailable is True and latest.capture is None
    assert latest.run == stored.record.run_id
    assert latest.landed_at == READ_TIME + timedelta(minutes=9)


def test_an_erased_latest_capture_says_so(tmp_path: Path) -> None:
    from tests.support.capture_erasure_world import erasure_world
    from tests.test_service.test_capture_erasure import BECAUSE, _erase

    erasures = erasure_world(tmp_path)
    (before,) = _rows(erasures.instance).values()
    # A real Source run's landing: its source is the one its Capture records.
    assert before.logical_source == "commerce.production.orders"
    assert before.latest.ref == f"Capture:{erasures.capture}"
    assert before.latest.status == "available" and before.latest.erased is None

    _erase(erasures, dry_run=False, at=_erase(erasures).coordinate.digest)

    (after,) = _rows(erasures.instance).values()
    assert (after.logical_source, after.landings) == (before.logical_source, 1)
    assert after.latest.status == "unavailable"
    assert after.latest.status_detail is not None and "erased" in after.latest.status_detail
    assert after.latest.erased is not None
    assert (after.latest.erased.by, after.latest.erased.because) == ("owner", BECAUSE)


def test_an_instance_with_no_runs_lists_no_sources(tmp_path: Path) -> None:
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    answer = service_playbill_orient(instance, section="sources")
    assert answer.sources == () and answer.sources_unattributed == 0


def test_a_landing_newer_than_the_reads_position_keeps_its_source(tmp_path: Path) -> None:
    """The projection keeps what it records, so it never reads at an older read's position.

    A read pins the body store's landing log when it starts; a run landing after
    that has an envelope the pinned read cannot see. Projecting that landing as
    the read sees the store would record it lost, for good.
    """

    from cruxible_core.storage.read_positions import read_positions, reading_at

    raced = SourcesWorld(tmp_path)
    assert _rows(raced.instance)  # everything so far is projected
    positions = read_positions(raced.instance)
    raced.land("new.source", TRIGGER_DIGEST, 20)

    with reading_at(positions):
        answer = service_playbill_orient(raced.instance, section="sources")
    assert answer.sources is not None and answer.sources_unattributed == 1
    (new,) = (row for row in answer.sources if row.logical_source == "new.source")
    assert new.landings == 1 and new.latest.unavailable is False
    assert new.latest.status == "available"

    after = service_playbill_orient(raced.instance, section="sources")
    assert after.sources_unattributed == 1
    assert _rows(raced.instance)["new.source"].landings == 1

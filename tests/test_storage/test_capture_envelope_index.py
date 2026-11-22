"""The body store indexes every Capture envelope by its contract as it stores it."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.captures import (
    CaptureRunCoordinateV1,
    build_cas_capture,
    capture_contract_digest,
)
from cruxible_core.storage.cas import ContentAddressedBodyStore, dry_run_bodies
from tests.core_support._pc_c_support import NOW, capture_contract, digest, provider


def _envelope(store: ContentAddressedBodyStore, body: bytes) -> str:
    contract = capture_contract()
    producer = provider(contract)
    return build_cas_capture(
        store=store,
        contract=contract,
        source_body=body,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id="index-run",
            bound_generation=digest("generation", "index"),
            executable_identity=contract.identity,
            executable_digest=capture_contract_digest(contract).tagged,
        ),
        run_receipt_digest=digest("receipt", body.decode()),
        producer=producer.identity,
        producer_binding_digest=digest("binding", "index"),
        observed_at=NOW,
    ).capture_digest


def test_envelopes_are_indexed_on_store_and_backfilled_once(tmp_path: Path) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    store = ContentAddressedBodyStore(root)
    contract = capture_contract_digest(capture_contract()).tagged
    first = _envelope(store, b'{"n":1}')
    store.store(b"not an envelope")
    assert store.capture_envelopes.captures((contract,), limit=8) == ((first,), True)

    # Objects stored before the index existed are added by one pass.
    (root / "capture-envelopes.sqlite3").unlink()
    second = _envelope(ContentAddressedBodyStore(root), b'{"n":2}')
    (root / "capture-envelopes.sqlite3").unlink()
    found, complete = ContentAddressedBodyStore(root).capture_envelopes.captures(
        (contract,), limit=8
    )
    assert complete and set(found) == {first, second}
    # The query is bounded and says when it is not the whole answer.
    assert store.capture_envelopes.captures((contract,), limit=1) == ((min(first, second),), False)


def test_a_dry_run_reads_the_index_without_writing_it(tmp_path: Path) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    held = _envelope(ContentAddressedBodyStore(root), b'{"n":3}')
    (root / "capture-envelopes.sqlite3").unlink()
    contract = capture_contract_digest(capture_contract()).tagged
    with dry_run_bodies():
        assert ContentAddressedBodyStore(root).capture_envelopes.captures((contract,), limit=8) == (
            (held,),
            True,
        )
    assert not (root / "capture-envelopes.sqlite3").exists()


def _reading_bytes(contract: str, *, url: str, observed: str, body: str) -> bytes:
    import json

    return json.dumps(
        {
            "capture_contract_digest": contract,
            "observed_at": observed,
            "commitment": body,
            "source": {
                "kind": "external",
                "source_identity": "web.fetch",
                "coordinate_type": "http-response-v1",
                "coordinate": {"url": url, "etag": body},
                "selector_type": "whole-response-v1",
                "selector": {},
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_newest_readings_follow_the_cited_source_by_index(tmp_path: Path) -> None:
    """A later reading of a cited source is found by seek; other sources are not read."""

    import sqlite3

    root = tmp_path / "cas"
    root.mkdir()
    store = ContentAddressedBodyStore(root)
    contract = digest("contract", "web")
    old = store.store(
        _reading_bytes(contract, url="https://a", observed="2026-11-01T00:00:00Z", body="1")
    ).digest
    new = store.store(
        _reading_bytes(contract, url="https://a", observed="2026-11-02T00:00:00Z", body="2")
    ).digest
    other = store.store(
        _reading_bytes(contract, url="https://b", observed="2026-11-03T00:00:00Z", body="3")
    ).digest
    index = store.capture_envelopes
    assert index.newest_readings((contract,), cited=(old,), subject="", limit=8) == (new, old)
    assert index.newest_readings((contract,), cited=(), subject="", limit=8) == ()
    assert index.newest_readings((contract,), cited=(other,), subject="", limit=8) == (other,)
    # Envelopes indexed before readings were kept are read once.
    with sqlite3.connect(root / "capture-envelopes.sqlite3") as connection:
        connection.execute("DELETE FROM readings")
        connection.execute("DELETE FROM readings_complete")
    fresh = ContentAddressedBodyStore(root).capture_envelopes
    assert fresh.newest_readings((contract,), cited=(old,), subject="", limit=1) == (new, old)
    # A dry run answers the same without writing, and adds what it holds.
    from cruxible_core.storage.cas import DryRunBodyStore

    with dry_run_bodies() as held:
        newest = (
            DryRunBodyStore(store, held)
            .store(
                _reading_bytes(contract, url="https://a", observed="2026-11-04T00:00:00Z", body="4")
            )
            .digest
        )
        assert index.newest_readings((contract,), cited=(old,), subject="", limit=8) == (
            newest,
            new,
            old,
        )

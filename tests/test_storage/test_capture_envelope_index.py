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

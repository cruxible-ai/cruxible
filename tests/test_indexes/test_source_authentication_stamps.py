"""A later process reuses a source authentication only for the exact piece it covered."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.indexes import sqlite as playbill_projection
from cruxible_core.indexes import typed_sqlite
from tests.core_support._knowledge_loop_support import seed_claims


def _count_authentications(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = typed_sqlite.authenticate_source_rows

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(typed_sqlite, "authenticate_source_rows", counted)
    return calls


def test_a_new_process_reuses_the_stamp_for_the_exact_piece(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as handle:
        stamps = handle.index_path.parent / playbill_projection.SOURCE_AUTHENTICATION_STAMPS
    assert stamps.is_file()
    calls = _count_authentications(monkeypatch)

    playbill_projection.reset_projection_verification_memo()  # a new process
    with instance.bind_accepted_projection(coordinate):
        pass
    assert calls == []

    # A stamp naming any other piece or coordinate is not this piece's.
    recorded = json.loads(stamps.read_bytes())
    for item in recorded:
        item["physical_digest"] = "sha256:" + "0" * 64
    stamps.write_bytes(json.dumps(recorded).encode())
    playbill_projection.reset_projection_verification_memo()
    with instance.bind_accepted_projection(coordinate):
        pass
    assert calls == [1]


def test_a_stamp_never_admits_a_changed_piece(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as handle:
        piece = handle.index_path
    content = bytearray(piece.read_bytes())
    content[-1] ^= 0xFF
    mode = piece.stat().st_mode
    piece.chmod(0o600)
    piece.write_bytes(bytes(content))
    piece.chmod(mode)
    playbill_projection.reset_projection_verification_memo()
    with pytest.raises(ProjectionIntegrityError):
        with instance.bind_accepted_projection(coordinate):
            pass


def test_a_record_that_appears_while_the_process_runs_is_not_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted records are starting trust only; a later one never admits a piece."""

    instance, _owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as handle:
        stamps = handle.index_path.parent / playbill_projection.SOURCE_AUTHENTICATION_STAMPS
    recorded = stamps.read_bytes()

    # A process that first looks while no record exists ...
    stamps.unlink()
    playbill_projection.reset_projection_verification_memo()
    assert playbill_projection._trusted_stamps(stamps.parent) == []
    # ... is later handed one, as a replacement written beside the piece would be.
    stamps.write_bytes(recorded)
    calls = _count_authentications(monkeypatch)
    playbill_projection._VERIFIED_PIECES.clear()
    with instance.bind_accepted_projection(coordinate):
        pass
    assert calls == [1]


def test_a_piece_replaced_after_startup_is_authenticated_before_it_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup recovery fixes the trusted records, so a later replacement gains nothing.

    The replacement is self-consistent: rows, logical sums, both manifests and a
    record naming the new piece all agree. Only source authentication tells it
    apart from what the ledger accepted.
    """

    import hashlib
    import sqlite3

    from cruxible_core.indexes.logical_digest import compute_table_sums, store_table_sums
    from cruxible_core.indexes.projection import render_projection_manifest
    from cruxible_core.indexes.serving import (
        SERVING_MANIFEST_FILE,
        load_serving_manifest,
        render_serving_manifest,
    )
    from cruxible_core.runtime.instance import PlaybillInstance

    original, _owner = seed_claims(tmp_path)
    PlaybillInstance.open(original.root, trust_root=original.trust_root)
    playbill_projection.reset_projection_verification_memo()  # a fresh daemon
    live = PlaybillInstance.open(original.root, trust_root=original.trust_root)
    manifest_path = Path(live._recovered.projection.manifest_path)
    directory = manifest_path.parent
    manifest = playbill_projection.load_projection_manifest(manifest_path)
    piece_path = directory / manifest.pieces[0].name

    piece_path.chmod(0o600)
    with sqlite3.connect(piece_path) as connection:
        connection.execute("UPDATE subjects SET revision = 100")
        store_table_sums(connection, compute_table_sums(connection))
        counts = typed_sqlite.row_counts(connection)
    piece_path.chmod(0o400)
    piece = manifest.pieces[0].model_copy(
        update={
            "physical_digest": playbill_projection.physical_file_digest(piece_path).tagged,
            "byte_length": piece_path.stat().st_size,
        }
    )
    replaced = manifest.model_copy(
        update={
            "pieces": (piece,),
            "row_counts": counts,
            "logical_digest": playbill_projection.projection_logical_digest(piece_path).tagged,
        }
    )
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(render_projection_manifest(replaced))
    serving = load_serving_manifest(directory).model_copy(
        update={
            "projection_manifest_digest": "sha256:"
            + hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "logical_digest": replaced.logical_digest,
        }
    )
    (directory / SERVING_MANIFEST_FILE).chmod(0o600)
    (directory / SERVING_MANIFEST_FILE).write_bytes(render_serving_manifest(serving))
    (directory / playbill_projection.SOURCE_AUTHENTICATION_STAMPS).write_text(
        json.dumps(
            [playbill_projection._authentication_stamp(live.accepted_coordinate(), replaced)]
        )
    )

    with pytest.raises(ProjectionIntegrityError, match="differ from accepted source"):
        with live.bind_accepted_projection(live.accepted_coordinate()):
            pass

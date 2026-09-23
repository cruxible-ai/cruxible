"""Warm event reads and interrupted index publication preserve retained authority."""

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import PlaybillJournalIntegrityError
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.exhaust.journal_index import JournalIndex
from tests.test_storage.test_journal_backends import _activate, _append, _backend, _stream


def test_index_rebuild_and_warm_exact_reads(tmp_path: Path, monkeypatch) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    expected = tuple(_append(backend, str(i)) for i in range(12))
    backend.index.path.unlink()
    assert backend.select_records(_stream(), run_id="run-a") == expected
    original = LocalJournalBackend._read_frames
    offsets = []

    def frames(*args, **kwargs):
        offsets.append(kwargs.get("offset", 0))
        yield from original(*args, **kwargs)

    monkeypatch.setattr(LocalJournalBackend, "_read_frames", staticmethod(frames))
    reopened = LocalJournalBackend(backend.root)
    selected = reopened.range_from_sequences(
        _stream(), "runs-2026-08", first_sequence=8, last_sequence=8
    )
    assert reopened.read_exact_range(selected) == (expected[7],)
    assert offsets and all(offset > 0 for offset in offsets)
    assert reopened.read_head(_stream(), "runs-2026-08").sequence == 12


def test_append_response_lost_before_index_update_is_recovered(tmp_path: Path, monkeypatch) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    first = _append(backend, "first")
    original_sync = JournalIndex.sync

    def crash_after_write(self, *args, recover_tail=False):
        # The writer's own pre-append tail recovery runs; the post-write update crashes.
        if recover_tail:
            return original_sync(self, *args, recover_tail=True)
        raise RuntimeError("crash")

    with monkeypatch.context() as patch:
        patch.setattr(JournalIndex, "sync", crash_after_write)
        with pytest.raises(RuntimeError, match="crash"):
            _append(backend, "second")
    reopened = LocalJournalBackend(backend.root)
    records = reopened.select_records(_stream(), run_id="run-a")
    assert len(records) == 2
    assert records[0] == first
    assert not tuple(reopened.index.dirty.iterdir())
    assert len(reopened.select_records(_stream(), run_id="run-a")) == 2


def test_selected_bytes_are_verified_not_served_from_sql(tmp_path: Path) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    _append(backend, "first")
    path = backend._record_log_path_for_testing(_stream(), "runs-2026-08")
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"run-a"', b'"run-b"'))
    with pytest.raises(PlaybillJournalIntegrityError):
        backend.select_records(_stream(), run_id="run-a")

"""The per-blob ceiling the exact-tree reader applies, at and over the bound."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_core.playbill.projection_artifacts import P2_C_ARTIFACT_KINDS
from cruxible_core.playbill.projection_tree import TreeReadLimits, read_registered_tree
from tests.test_playbill._projection_support import MemoryLedger

CAPTURE_PATH = "documents/large-capture.json"


def _ledger(path: Path, content: bytes) -> MemoryLedger:
    return MemoryLedger(path, {CAPTURE_PATH: content})


def _read(repository: MemoryLedger, *, limits: TreeReadLimits) -> tuple[str, ...]:
    return tuple(
        blob.path
        for blob in read_registered_tree(
            repository,
            repository.oid,
            limits=limits,
            artifact_kinds=P2_C_ARTIFACT_KINDS,
        )
    )


def test_a_five_mebibyte_capture_is_read_out_of_the_ledger(tmp_path: Path) -> None:
    """A captured source over the old 4 MiB ceiling is accepted and citable.

    Receive admits a single member of up to `max_file_bytes`, 8 MiB, while this
    reader's per-blob ceiling was 4 MiB: a 5 MiB capture passed admission and
    was then unreadable, so nothing could cite it. The ceiling is 64 MiB, and
    this reads the blob back rather than asserting the arithmetic that says it
    should -- five mebibytes of bytes cost microseconds to build and read.
    """

    body = b"c" * (5 * 1024 * 1024)
    repository = _ledger(tmp_path / "ledger", body)

    assert len(body) > 4 * 1024 * 1024
    assert len(body) <= TreeReadLimits().max_blob_bytes
    assert _read(repository, limits=TreeReadLimits()) == (CAPTURE_PATH,)


def test_a_blob_one_byte_over_the_ceiling_is_still_refused(tmp_path: Path) -> None:
    """The bound still fires, proved against an injected ceiling.

    The at-bound and one-over pair is what makes a ceiling a ceiling, and the
    ceiling is a parameter of the reader: proving it at 64 MiB would allocate
    64 MiB to learn what a small injected ceiling proves exactly as well, so the
    limit is injected and the real default is pinned in `test_guardrails`.
    """

    body = b"c" * 4_096
    at_bound = _ledger(tmp_path / "at-bound", body)
    over = _ledger(tmp_path / "over", body)

    assert _read(at_bound, limits=TreeReadLimits(max_blob_bytes=len(body))) == (CAPTURE_PATH,)

    with pytest.raises(ProjectionFormatError, match="per-file byte limit"):
        _read(over, limits=TreeReadLimits(max_blob_bytes=len(body) - 1))

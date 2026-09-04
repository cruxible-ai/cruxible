"""The one atomic whole-file write a workspace edit is allowed to make.

The publication road that used this compare-and-swap is gone -- nothing frames a
Claim into a page any more -- but the write itself is not the publication's: it
is what every block edit goes through, and losing a concurrent author's edit is
the failure it exists to refuse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.authoring.insertions import (
    PlaybillInsertionApplyError,
    replace_publication_file,
)


def test_publication_file_replace_refuses_a_concurrent_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "runbook.md"
    source.write_bytes(b"before\n")
    concurrent = b"concurrent author edit\n"
    original = Path.read_bytes

    def edit_before_compare(path: Path) -> bytes:
        if path == source:
            source.write_bytes(concurrent)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", edit_before_compare)
    with pytest.raises(PlaybillInsertionApplyError, match="compare-and-swap"):
        replace_publication_file(source, expected=b"before\n", replacement=b"after\n")

    monkeypatch.setattr(Path, "read_bytes", original)
    assert source.read_bytes() == concurrent

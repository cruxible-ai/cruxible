"""The one atomic whole-file write a workspace edit is allowed to make."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class PlaybillInsertionApplyError(ValueError):
    """A local source cannot be reconciled with its insertion expectation."""


def replace_publication_file(
    path: Path,
    *,
    expected: bytes,
    replacement: bytes,
) -> None:
    """Durably replace one exact preimage without overwriting a concurrent edit."""

    temporary: Path | None = None
    try:
        original_mode = path.stat().st_mode
        with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(replacement)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(original_mode)
        if path.read_bytes() != expected:
            raise PlaybillInsertionApplyError(
                "source bytes changed before the whole-file compare-and-swap"
            )
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise PlaybillInsertionApplyError(
            f"source could not be replaced atomically: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "PlaybillInsertionApplyError",
    "replace_publication_file",
]

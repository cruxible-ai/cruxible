"""The one atomic whole-file write a workspace edit is allowed to make."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile

from cruxible_client._safe_files import read_regular_file


class PageWriteError(ValueError):
    """A workspace page could not be replaced exactly as it was read."""


def replace_page_file(
    path: Path,
    *,
    expected: bytes | None,
    replacement: bytes,
) -> None:
    """Durably replace one exact preimage without overwriting a concurrent edit."""

    temporary: Path | None = None
    try:
        original_mode = 0o644 if expected is None else path.stat(follow_symlinks=False).st_mode
        if expected is not None and not stat.S_ISREG(original_mode):
            raise PageWriteError("source is no longer a regular file")
        with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(replacement)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(original_mode)
        if expected is None:
            # link refuses EEXIST: a concurrent creator is never overwritten.
            os.link(temporary, path)
            temporary.unlink()
            temporary = None
            return
        if read_regular_file(path) != expected:
            raise PageWriteError("source bytes changed before the whole-file compare-and-swap")
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise PageWriteError(f"source could not be replaced atomically: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "PageWriteError",
    "replace_page_file",
]

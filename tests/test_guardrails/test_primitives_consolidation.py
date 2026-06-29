"""Guardrail: every canonical-JSON / record-id pattern routes through primitives.py.

These checks are the only thing keeping ``canonical_json`` / ``new_id`` truly
canonical. If a contributor inlines either pattern again, dialect drift is back
on the table — that is exactly what these tests prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "cruxible_core"
PRIMITIVES_FILE = SRC_ROOT / "primitives.py"

# Match ``json.dumps(`` followed (within a few lines) by ``separators=(",", ":")``.
_CANONICAL_JSON_INLINE = re.compile(
    r"json\.dumps\([^)]*separators\s*=\s*\(\s*\"\s*,\s*\"\s*,\s*\"\s*:\s*\"\s*\)",
    re.DOTALL,
)

# Match inline UUID hex ID minting.
_RECORD_ID_INLINE = re.compile(r"uuid\.uuid4\(\)\.hex(?:\[:\d+\])?")


def _python_sources() -> list[Path]:
    return [path for path in SRC_ROOT.rglob("*.py") if path.is_file()]


def test_no_canonical_json_inline_outside_primitives() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        if path == PRIMITIVES_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        if _CANONICAL_JSON_INLINE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "Inline canonical-JSON serialization detected. "
        "Use cruxible_core.primitives.canonical_json instead. Offenders:\n  - "
        + "\n  - ".join(offenders)
    )


def test_no_record_id_inline_outside_primitives() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        if path == PRIMITIVES_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        if _RECORD_ID_INLINE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "Inline UUID-hex ID minting detected. "
        "Use cruxible_core.primitives.new_id(...) instead. Offenders:\n  - "
        + "\n  - ".join(offenders)
    )

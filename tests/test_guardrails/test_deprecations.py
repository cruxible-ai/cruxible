"""Guardrails for the structured deprecation registry and removal schedule."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_core import __version__
from cruxible_core.deprecation import (
    DEPRECATION_REGISTRY,
    emit_cli_deprecation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("notice", DEPRECATION_REGISTRY)
def test_every_registry_entry_emits_on_the_cli_and_has_a_schedule_row(
    notice: Any,
) -> None:
    expected = notice.as_dict()

    stream = io.StringIO()
    emit_cli_deprecation(notice, stream=stream)
    line = stream.getvalue()
    assert line.count("\n") == 1
    assert json.loads(line.removeprefix("Deprecation: ")) == expected

    row = f"| `{notice.surface}` |"
    matching_rows = [
        line for line in Path("DEPRECATIONS.md").read_text().splitlines() if line.startswith(row)
    ]
    assert len(matching_rows) == 1
    assert f"| {notice.removal_version} |" in matching_rows[0]


def test_registry_surfaces_are_unique() -> None:
    surfaces = [notice.surface for notice in DEPRECATION_REGISTRY]
    assert len(surfaces) == len(set(surfaces))


def test_removed_050_surfaces_are_absent_from_code_and_registry() -> None:
    # The registry is no longer empty (the Subject-address rows are live), so the
    # law is stated over the 0.5.0 removals themselves rather than over emptiness.
    removed_050_surfaces = {
        "legacy outcome record functions",
        "legacy outcome profile functions",
        "ProcedureTransitionResult.warnings string list",
        "playbill.claim.propose.legacy_wire_deprecated",
    }
    assert {notice.surface for notice in DEPRECATION_REGISTRY} & removed_050_surfaces == set()

    removed_source_markers = {
        "LEGACY_OUTCOME_RECORD",
        "LEGACY_OUTCOME_PROFILE",
        "PROCEDURE_STRING_WARNINGS",
        "ProcedureTransitionResult",
    }
    live_source = "\n".join(path.read_text() for path in sorted((REPO_ROOT / "src").rglob("*.py")))
    for marker in removed_source_markers:
        assert marker not in live_source


def _release_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")[:3])


@pytest.mark.parametrize("notice", DEPRECATION_REGISTRY)
def test_no_registered_surface_is_past_its_removal_window(notice: Any) -> None:
    """The removal window is a commitment; this is the gate that collects on it.

    The registry->table check above runs in one direction only and never
    compares ``removal_version`` against the version actually being shipped, so
    nothing failed when a release carried surfaces whose removal date had
    already passed. A removal version is a promise about the release it names:
    once ``__version__`` reaches it, the surface must be gone from the registry,
    not still warning.
    """
    assert _release_tuple(__version__) < _release_tuple(notice.removal_version), (
        f"'{notice.surface}' promised removal in {notice.removal_version} and this tree "
        f"is version {__version__}. Remove the surface and its registry entry (keeping "
        "the DEPRECATIONS.md and CHANGELOG rows as the historical record), or move the "
        "removal version deliberately -- a commitment change, not a version bump."
    )

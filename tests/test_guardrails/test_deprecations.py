"""Guardrails for the structured deprecation registry and removal schedule."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.deprecation import (
    DEPRECATION_REGISTRY,
    attach_mcp_deprecations,
    emit_cli_deprecation,
    emit_http_deprecations,
)


class _Headers:
    def __init__(self) -> None:
        self.values: list[tuple[str, str]] = []

    def append(self, name: str, value: str) -> None:
        self.values.append((name, value))


class _Response:
    def __init__(self) -> None:
        self.headers = _Headers()


@pytest.mark.parametrize("notice", DEPRECATION_REGISTRY)
def test_every_registry_entry_emits_on_each_transport_and_has_a_schedule_row(
    notice: Any,
) -> None:
    expected = notice.as_dict()

    stream = io.StringIO()
    emit_cli_deprecation(notice, stream=stream)
    line = stream.getvalue()
    assert line.count("\n") == 1
    assert json.loads(line.removeprefix("Deprecation: ")) == expected

    assert attach_mcp_deprecations({}, [notice]) == {"deprecation_warnings": [expected]}

    response = _Response()
    unchanged = {"ok": True}
    assert emit_http_deprecations(response, unchanged, [notice]) is unchanged
    assert response.headers.values == [
        (
            "Deprecation",
            json.dumps(expected, separators=(",", ":"), sort_keys=True),
        )
    ]

    row = f"| `{notice.surface}` |"
    matching_rows = [
        line for line in Path("DEPRECATIONS.md").read_text().splitlines() if line.startswith(row)
    ]
    assert len(matching_rows) == 1
    assert f"| {notice.removal_version} |" in matching_rows[0]


def test_emitters_use_existing_warning_envelopes_without_renaming_them() -> None:
    notice = DEPRECATION_REGISTRY[0]
    expected = notice.as_dict()

    assert attach_mcp_deprecations({"warnings": ["existing"]}, [notice]) == {
        "warnings": ["existing", expected]
    }

    response = _Response()
    assert emit_http_deprecations(response, {"warnings": ["existing"]}, [notice]) == {
        "warnings": ["existing", expected]
    }


def test_registry_surfaces_are_unique() -> None:
    surfaces = [notice.surface for notice in DEPRECATION_REGISTRY]
    assert len(surfaces) == len(set(surfaces))

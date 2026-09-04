"""Ratified v1 inventory guardrails for every public Playbill surface."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts import update_playbill_served_surface as served_surface

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPOSITORY_ROOT / "tests/goldens/playbill/served-surface-dp0b-v1.json"


@pytest.fixture(scope="module")
def live_surface() -> dict[str, object]:
    return served_surface.generate_served_surface()


def _ratified(surface: dict[str, object]) -> dict[str, object]:
    return {
        "format": served_surface.FORMAT,
        "succession": {
            "register_entry": "2026-09-02:p2-b5-v1-wire-freeze",
            "ratified_by": served_surface.RATIFIED_BY,
            "surface_digest": served_surface.surface_digest(surface),
        },
        "surface": surface,
    }


def _append_facade(surface: dict[str, object]) -> None:
    facade = surface["facade_verbs"]
    assert isinstance(facade, list)
    facade.append("playbill_hidden_addition")


def _delete_facade(surface: dict[str, object]) -> None:
    facade = surface["facade_verbs"]
    assert isinstance(facade, list)
    facade.pop()


def _rename_facade(surface: dict[str, object]) -> None:
    facade = surface["facade_verbs"]
    assert isinstance(facade, list)
    facade[0] = f"{facade[0]}_renamed"


def _mutate_http(surface: dict[str, object], field: str, value: object) -> None:
    routes = surface["http_routes"]
    assert isinstance(routes, list) and isinstance(routes[0], dict)
    routes[0][field] = value


def _same_count_mcp_swap(surface: dict[str, object]) -> None:
    tools = surface["mcp_tools"]
    assert isinstance(tools, list) and isinstance(tools[0], dict)
    tools[0]["name"] = "cruxible_playbill_same_count_substitution"


def _mutate_mcp(surface: dict[str, object], field: str, value: object) -> None:
    tools = surface["mcp_tools"]
    assert isinstance(tools, list) and isinstance(tools[0], dict)
    tools[0][field] = value


def _add_hidden_cli_leaf(surface: dict[str, object]) -> None:
    leaves = surface["cli_leaves"]
    assert isinstance(leaves, list)
    leaves.append(
        {
            "command": "playbill hidden",
            "delegate": "cruxible_core.cli.commands.playbill.hidden",
            "client_operations": [],
        }
    )


SELF_ATTACKS: tuple[tuple[str, Callable[[dict[str, object]], None]], ...] = (
    ("facade-add", _append_facade),
    ("facade-delete", _delete_facade),
    ("facade-rename", _rename_facade),
    ("http-method", lambda surface: _mutate_http(surface, "method", "PATCH")),
    ("http-route", lambda surface: _mutate_http(surface, "path", "/api/v1/hidden")),
    ("http-request-model", lambda surface: _mutate_http(surface, "request_model", "Hidden")),
    ("http-response-model", lambda surface: _mutate_http(surface, "response_model", "Hidden")),
    ("http-delegate", lambda surface: _mutate_http(surface, "delegate", "hidden.delegate")),
    ("mcp-same-count-swap", _same_count_mcp_swap),
    ("mcp-permission", lambda surface: _mutate_mcp(surface, "permission", "ADMIN")),
    (
        "mcp-schema",
        lambda surface: _mutate_mcp(surface, "input_schema_digest", "sha256:" + "0" * 64),
    ),
    ("mcp-delegate", lambda surface: _mutate_mcp(surface, "delegate", "hidden.delegate")),
    # The per-tool verb-to-tool join a cloud overlay reads: a tool quietly
    # reaching one more facade verb is a widening of what MCP publishes.
    (
        "mcp-facade-operations",
        lambda surface: _mutate_mcp(surface, "facade_operations", ["playbill_hidden"]),
    ),
    ("cli-hidden-leaf", _add_hidden_cli_leaf),
)


@pytest.mark.parametrize(("_name", "mutate"), SELF_ATTACKS, ids=[row[0] for row in SELF_ATTACKS])
def test_v1_surface_refuses_exact_inventory_drift(
    _name: str,
    mutate: Callable[[dict[str, object]], None],
    live_surface: dict[str, object],
) -> None:
    changed = copy.deepcopy(live_surface)
    mutate(changed)

    with pytest.raises(ValueError, match="differs from the ratified"):
        served_surface.verify_served_surface_snapshot(
            _ratified(changed),
            live_surface=live_surface,
        )


def test_checked_in_v1_surface_is_exact_and_ratified(live_surface: dict[str, object]) -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert set(snapshot["succession"]) == {
        "register_entry",
        "ratified_by",
        "surface_digest",
    }
    served_surface.verify_served_surface_snapshot(snapshot, live_surface=live_surface)


def test_updater_requires_succession_for_movement_and_is_byte_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {
        "facade_verbs": ["playbill_read"],
        "http_routes": [],
        "mcp_tools": [],
        "cli_leaves": [],
    }
    monkeypatch.setattr(served_surface, "generate_served_surface", lambda: live)
    snapshot_path = tmp_path / "served.json"
    original = b'{"format":"legacy"}\n'
    snapshot_path.write_bytes(original)
    unrelated = tmp_path / "unrelated-pin.json"
    unrelated.write_bytes(b'{"must":"not move"}\n')

    with pytest.raises(ValueError, match="requires --succession"):
        served_surface.update_snapshot(snapshot_path, succession=None)
    assert snapshot_path.read_bytes() == original

    assert served_surface.update_snapshot(
        snapshot_path,
        succession="2026-09-02:p2-b5-v1-wire-freeze",
    )
    first = snapshot_path.read_bytes()
    assert not served_surface.update_snapshot(snapshot_path, succession=None)
    assert snapshot_path.read_bytes() == first
    assert unrelated.read_bytes() == b'{"must":"not move"}\n'


def test_updater_refuses_unstable_succession_identifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(served_surface, "generate_served_surface", dict)
    with pytest.raises(ValueError, match="stable lower-case"):
        served_surface.update_snapshot(tmp_path / "served.json", succession="Free form approval")

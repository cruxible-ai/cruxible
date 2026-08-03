"""MCP result surfacing for advisory entity identity warnings."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from cruxible_core.mcp.server import create_server
from cruxible_core.runtime import api

IDENTITY_CONFIG = """\
version: '1.0'
name: mcp_declared_identity_keys
entity_types:
  Account:
    identity_hint: [name, family]
    properties:
      account_id: {type: string, primary_key: true}
      name: {type: string}
      family: {type: string}
relationships: []
"""


def _call_tool(server: FastMCP, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result: Any = asyncio.run(server.call_tool(name, args))
    if isinstance(result, tuple):
        return cast(dict[str, Any], result[1])
    return cast(dict[str, Any], json.loads(result[0].text))


def test_add_entity_tool_surfaces_identity_hint_warning(
    tmp_path: Path,
    governed_client: Any,
) -> None:
    del governed_client
    server = create_server()
    init = _call_tool(
        server,
        "cruxible_init",
        {
            "root_dir": str(tmp_path),
            "config_yaml": IDENTITY_CONFIG,
        },
    )
    instance_id = init["instance_id"]
    first = {
        "instance_id": instance_id,
        "entities": [
            {
                "entity_type": "Account",
                "entity_id": "product_bluest_account",
                "properties": {"name": "Bluest Account", "family": "Checking"},
            }
        ],
    }
    assert _call_tool(server, "cruxible_add_entity", first)["identity_warnings"] == []

    second = {
        "instance_id": instance_id,
        "entities": [
            {
                "entity_type": "Account",
                "entity_id": "checking_bluest_account",
                "properties": {"name": "BLUEST, ACCOUNT!", "family": "checking"},
            }
        ],
    }
    result = _call_tool(server, "cruxible_add_entity", second)

    assert result["entities_added"] == 1
    assert result["identity_warnings"] == [
        {
            "entity_type": "Account",
            "entity_id": "checking_bluest_account",
            "similar_existing_entity": {
                "entity_id": "product_bluest_account",
                "matched_properties": ["name", "family"],
            },
        }
    ]


def test_batch_direct_write_surfaces_intra_batch_identity_warning(
    tmp_path: Path,
    governed_client: Any,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        governed_client,
        "batch_direct_write",
        api.batch_direct_write,
        raising=False,
    )
    server = create_server()
    init = _call_tool(
        server,
        "cruxible_init",
        {
            "root_dir": str(tmp_path),
            "config_yaml": IDENTITY_CONFIG,
        },
    )

    result = _call_tool(
        server,
        "cruxible_batch_direct_write",
        {
            "instance_id": init["instance_id"],
            "payload": {
                "entities": [
                    {
                        "entity_type": "Account",
                        "entity_id": "product_bluest_account",
                        "properties": {"name": "Bluest Account", "family": "Checking"},
                    },
                    {
                        "entity_type": "Account",
                        "entity_id": "checking_bluest_account",
                        "properties": {"name": "BLUEST, ACCOUNT!", "family": "checking"},
                    },
                ]
            },
        },
    )

    assert result["entities_added"] == 2
    assert result["identity_warnings"] == [
        {
            "entity_type": "Account",
            "entity_id": "checking_bluest_account",
            "similar_existing_entity": {
                "entity_id": "product_bluest_account",
                "matched_properties": ["name", "family"],
            },
        }
    ]

"""Kit-parameterized MCP tool descriptions.

The contract under test: an agent can read the loaded config's vocabulary off
the tool surface, and nothing about the SCHEMAS moves when it does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from cruxible_core.config.loader import load_config_from_string
from cruxible_core.config.schema import ContractSchema, CoreConfig, PropertySchema
from cruxible_core.mcp.kit_surface import (
    KIT_SURFACE_CONFIG_ENV,
    MAX_CONTRACTS_WITH_FIELDS,
    MAX_NAMES_IN_DESCRIPTION,
    KitSurface,
    describe_contracts,
    describe_named_queries,
    describe_providers,
    resolve_kit_surface,
    summarize_kit_surface,
)
from cruxible_core.mcp.server import create_server
from cruxible_core.mcp.tool_prompts import (
    KIT_FACTS_BY_TOOL,
    TOOL_DESCRIPTIONS,
    tool_description,
)
from cruxible_core.mcp.tools import register_tools
from cruxible_core.server.config import ServerSettings

BANKING_CONFIG = """
name: banking_supervision
entity_types:
  Account:
    properties:
      account_id: {primary_key: true}
      risk_band: {}
  Counterparty:
    properties:
      counterparty_id: {primary_key: true}
relationships:
  - name: transacts_with
    from: Account
    to: Counterparty
named_queries:
  high_risk_accounts:
    mode: traversal
    entry_point: Account
    returns: Counterparty
    traversal:
      - relationship: transacts_with
        direction: outgoing
  counterparty_exposure:
    mode: traversal
    entry_point: Counterparty
    returns: Account
    traversal:
      - relationship: transacts_with
        direction: incoming
contracts:
  TransferReviewInput:
    fields:
      account_id: {type: string}
      amount: {type: float}
      memo: {type: string, optional: true}
  TransferReviewResults:
    fields:
      items: {type: json}
"""


LOCAL_MODE = ServerSettings()
REMOTE_MODE = ServerSettings(server_url="https://daemon.example/api")


def _banking_config(tmp_path: Path) -> Path:
    path = tmp_path / "banking.yaml"
    path.write_text(BANKING_CONFIG)
    return path


def _register_one_local_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand a single local instance registry record in front of the fallback.

    The fallback reaches for these two module-level factories at call time, so
    patching them is the seam that says "this host has exactly one instance"
    without standing up its storage.
    """
    from cruxible_core.runtime import instance_manager
    from cruxible_core.server import registry

    config = load_config_from_string(BANKING_CONFIG)
    record = SimpleNamespace(instance_id="inst_local_only")
    instance = SimpleNamespace(load_config=lambda: config)
    monkeypatch.setattr(
        registry,
        "get_registry",
        lambda: SimpleNamespace(list_instances=lambda: [record]),
    )
    monkeypatch.setattr(
        instance_manager,
        "get_manager",
        lambda: SimpleNamespace(get=lambda instance_id: instance),
    )


def _surface(**overrides: object) -> KitSurface:
    base: dict[str, object] = {
        "config_name": "banking_supervision",
        "named_queries": ("counterparty_exposure", "high_risk_accounts"),
        "providers": ("screen_transfer",),
        "contracts": (("TransferReviewInput", ("account_id", "amount", "memo"), 3),),
    }
    base.update(overrides)
    return KitSurface(**base)  # type: ignore[arg-type]


class TestSurfaceRendering:
    def test_named_queries_are_listed_under_the_config_name(self) -> None:
        rendered = describe_named_queries(_surface())
        assert rendered == (
            "Named queries in 'banking_supervision': counterparty_exposure, high_risk_accounts."
        )

    def test_providers_are_listed_under_the_config_name(self) -> None:
        assert describe_providers(_surface()) == (
            "Registered providers in 'banking_supervision': screen_transfer."
        )

    def test_contracts_carry_an_inline_field_preview(self) -> None:
        assert describe_contracts(_surface()) == (
            "Contracts in 'banking_supervision': TransferReviewInput(account_id, amount, memo)."
        )

    def test_a_wide_contract_reports_the_fields_it_did_not_show(self) -> None:
        surface = _surface(contracts=(("WideInput", ("a", "b", "c", "d"), 11),))
        assert "WideInput(a, b, c, d, +7 more)" in (describe_contracts(surface) or "")

    def test_facts_a_config_does_not_declare_are_omitted_entirely(self) -> None:
        """An empty list is not a fact worth spending description bytes on."""
        surface = _surface(named_queries=(), providers=(), contracts=())
        assert describe_named_queries(surface) is None
        assert describe_providers(surface) is None
        assert describe_contracts(surface) is None
        assert surface.is_empty

    def test_long_lists_truncate_with_a_total(self) -> None:
        names = tuple(f"query_{index:03d}" for index in range(MAX_NAMES_IN_DESCRIPTION + 12))
        rendered = describe_named_queries(_surface(named_queries=names)) or ""
        assert f"({len(names)} total; first {MAX_NAMES_IN_DESCRIPTION} shown)" in rendered
        assert f"query_{MAX_NAMES_IN_DESCRIPTION:03d}" not in rendered

    def test_field_previews_stop_once_the_contract_list_is_long(self) -> None:
        contracts = tuple(
            (f"Contract{index:02d}", ("field_a",), 1)
            for index in range(MAX_CONTRACTS_WITH_FIELDS + 3)
        )
        rendered = describe_contracts(_surface(contracts=contracts)) or ""
        assert f"Contract{MAX_CONTRACTS_WITH_FIELDS - 1:02d}(field_a)" in rendered
        assert f"Contract{MAX_CONTRACTS_WITH_FIELDS:02d}(field_a)" not in rendered
        assert f"Contract{MAX_CONTRACTS_WITH_FIELDS:02d}," in rendered


class TestSurfaceResolution:
    def test_an_explicit_config_path_is_summarized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KIT_SURFACE_CONFIG_ENV, str(_banking_config(tmp_path)))
        surface = resolve_kit_surface(settings=LOCAL_MODE)
        assert surface is not None
        assert surface.config_name == "banking_supervision"
        assert surface.named_queries == ("counterparty_exposure", "high_risk_accounts")
        assert [name for name, _fields, _total in surface.contracts] == [
            "TransferReviewInput",
            "TransferReviewResults",
        ]

    def test_an_unreadable_config_degrades_to_no_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A description is never worth failing server creation over."""
        monkeypatch.setenv(KIT_SURFACE_CONFIG_ENV, str(tmp_path / "absent.yaml"))
        assert resolve_kit_surface(settings=LOCAL_MODE) is None

    def test_an_invalid_config_degrades_to_no_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = tmp_path / "broken.yaml"
        broken.write_text("name: [unclosed\n")
        monkeypatch.setenv(KIT_SURFACE_CONFIG_ENV, str(broken))
        assert resolve_kit_surface(settings=LOCAL_MODE) is None

    def test_local_mode_describes_the_sole_local_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_one_local_instance(monkeypatch)
        surface = resolve_kit_surface(settings=LOCAL_MODE)
        assert surface is not None
        assert surface.config_name == "banking_supervision"

    def test_remote_mode_never_describes_an_unrelated_local_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A remote daemon's kit is not whatever kit happens to live on this host.

        The local registry describes instances THIS process would serve. Under a
        remote transport it serves none of them, so borrowing the sole local
        record would advertise named queries and contracts the daemon does not
        have — a wrong answer, where silence is a correct one.
        """
        _register_one_local_instance(monkeypatch)

        assert resolve_kit_surface(settings=REMOTE_MODE) is None

        for name, reviewed in TOOL_DESCRIPTIONS.items():
            described = tool_description(
                name, kit_surface=resolve_kit_surface(settings=REMOTE_MODE)
            )
            assert described == reviewed, name
            assert "banking_supervision" not in described, name

    def test_a_unix_socket_transport_is_remote_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _register_one_local_instance(monkeypatch)
        socket_mode = ServerSettings(server_socket="/run/cruxible/daemon.sock")
        assert resolve_kit_surface(settings=socket_mode) is None

    def test_an_explicit_config_still_wins_in_remote_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one way a remote topology describes its kit is by saying so."""
        _register_one_local_instance(monkeypatch)
        other = tmp_path / "other.yaml"
        other.write_text(BANKING_CONFIG.replace("name: banking_supervision", "name: served_kit"))
        monkeypatch.setenv(KIT_SURFACE_CONFIG_ENV, str(other))

        surface = resolve_kit_surface(settings=REMOTE_MODE)
        assert surface is not None
        assert surface.config_name == "served_kit"

    def test_summarize_reads_every_declared_fact(self) -> None:
        config = CoreConfig(
            name="banking_supervision",
            entity_types={"Account": {"properties": {"account_id": {"primary_key": True}}}},  # type: ignore[dict-item]
            contracts={
                "TransferReviewInput": ContractSchema(
                    fields={
                        "account_id": PropertySchema(type="string"),
                        "amount": PropertySchema(type="float"),
                    }
                )
            },
        )
        surface = summarize_kit_surface(config)
        assert surface.config_name == "banking_supervision"
        assert surface.contracts == (("TransferReviewInput", ("account_id", "amount"), 2),)


class TestDescriptionInjection:
    def test_without_a_surface_the_reviewed_text_is_returned_verbatim(self) -> None:
        for name, text in TOOL_DESCRIPTIONS.items():
            assert tool_description(name) == text

    def test_kit_facts_are_additive_and_keep_the_style_rule(self) -> None:
        surface = _surface()
        for name in TOOL_DESCRIPTIONS:
            described = tool_description(name, kit_surface=surface)
            assert described.startswith(TOOL_DESCRIPTIONS[name])
            assert described.startswith("Use when ")

    def test_only_the_tools_that_need_a_fact_carry_it(self) -> None:
        surface = _surface()
        assert "Named queries in" in tool_description("cruxible_query", kit_surface=surface)
        assert "Registered providers in" in tool_description(
            "cruxible_propose_procedure", kit_surface=surface
        )
        assert (
            tool_description("cruxible_attest", kit_surface=surface)
            == (TOOL_DESCRIPTIONS["cruxible_attest"])
        )

    def test_every_mapped_tool_is_a_real_tool(self) -> None:
        assert set(KIT_FACTS_BY_TOOL) <= set(TOOL_DESCRIPTIONS)


class TestServedSurfaceMatchesTheTransport:
    def test_a_remote_server_advertises_no_local_kit_facts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: the bug was visible on the wire, so assert it there."""
        _register_one_local_instance(monkeypatch)
        monkeypatch.setenv("CRUXIBLE_SERVER_URL", "https://daemon.example/api")

        tools = asyncio.run(create_server().list_tools())

        assert tools
        for tool in tools:
            assert "banking_supervision" not in (tool.description or ""), tool.name

    def test_a_local_server_still_advertises_the_sole_local_kit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _register_one_local_instance(monkeypatch)

        tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}

        assert "banking_supervision" in (tools["cruxible_query"].description or "")

    def test_an_unresolvable_transport_advertises_no_local_kit_facts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both transports set is remote intent, even though it resolves to neither."""
        _register_one_local_instance(monkeypatch)
        monkeypatch.setenv("CRUXIBLE_SERVER_URL", "https://daemon.example/api")
        monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", "/run/cruxible/daemon.sock")

        tools = asyncio.run(create_server().list_tools())

        assert tools
        for tool in tools:
            assert "banking_supervision" not in (tool.description or ""), tool.name


class TestSchemasAreKitInvariant:
    def test_a_kit_surface_changes_descriptions_and_nothing_else(self) -> None:
        """Schemas are contract surface. Only prose may vary with the kit."""
        plain = FastMCP(name="plain", instructions="")
        described = FastMCP(name="described", instructions="")
        register_tools(plain)
        register_tools(described, kit_surface=_surface())

        plain_tools = {tool.name: tool for tool in asyncio.run(plain.list_tools())}
        described_tools = {tool.name: tool for tool in asyncio.run(described.list_tools())}

        assert set(plain_tools) == set(described_tools)
        for name, tool in plain_tools.items():
            other = described_tools[name]
            assert tool.inputSchema == other.inputSchema, name
            assert tool.outputSchema == other.outputSchema, name
            assert tool.annotations == other.annotations, name

        changed = {
            name
            for name, tool in plain_tools.items()
            if tool.description != described_tools[name].description
        }
        assert changed == set(KIT_FACTS_BY_TOOL)

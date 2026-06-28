"""Unit tests for the refuse_direct_writes policy resolver.

Pins the precedence table from the plan (union semantics): the env kill-switch is
a HARD override that wins over everything; otherwise an explicit per-type
write_policy decides; otherwise the instance default applies. Env is read
per-call via ``monkeypatch.setenv`` (no process-global cache).
"""

from __future__ import annotations

import pytest

from cruxible_core.config.schema import CoreConfig
from cruxible_core.service.direct_write_policy import (
    _GOVERNED_SOURCES,
    effective_entity_write_policy,
    effective_relationship_write_policy,
    env_refuses_direct_writes,
    is_governed_source,
)

ENV_KEY = "CRUXIBLE_REFUSE_DIRECT_WRITES"


def _build_config(
    *,
    entity_write_policy: str | None,
    relationship_write_policy: str | None,
    default_write_policy: str,
) -> CoreConfig:
    entity_type: dict[str, object] = {
        "properties": {"asset_id": {"primary_key": True}},
    }
    if entity_write_policy is not None:
        entity_type["write_policy"] = entity_write_policy
    relationship: dict[str, object] = {
        "name": "asset_rel",
        "from": "Asset",
        "to": "Asset",
    }
    if relationship_write_policy is not None:
        relationship["write_policy"] = relationship_write_policy
    return CoreConfig.model_validate(
        {
            "name": "policy_test",
            "entity_types": {"Asset": entity_type},
            "relationships": [relationship],
            "runtime": {"default_write_policy": default_write_policy},
        }
    )


# ---------------------------------------------------------------------------
# Governed-source allowlist
# ---------------------------------------------------------------------------


def test_governed_sources_are_exactly_workflow_apply_and_group_resolve() -> None:
    assert _GOVERNED_SOURCES == frozenset({"workflow_apply", "group_resolve"})


@pytest.mark.parametrize("source", ["workflow_apply", "group_resolve"])
def test_governed_sources_recognized(source: str) -> None:
    assert is_governed_source(source) is True


@pytest.mark.parametrize(
    "source",
    ["add_entity", "add_relationship", "batch_direct_write", "cli_add", "mcp_add", ""],
)
def test_non_governed_sources_rejected(source: str) -> None:
    assert is_governed_source(source) is False


# ---------------------------------------------------------------------------
# env_refuses_direct_writes truthiness (mirrors server/config:_is_truthy)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_env_truthy_values(value: str) -> None:
    assert env_refuses_direct_writes({ENV_KEY: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "maybe"])
def test_env_falsey_values(value: str) -> None:
    assert env_refuses_direct_writes({ENV_KEY: value}) is False


def test_env_unset_is_false() -> None:
    assert env_refuses_direct_writes({}) is False


def test_env_read_per_call_via_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_KEY, raising=False)
    assert env_refuses_direct_writes() is False
    monkeypatch.setenv(ENV_KEY, "1")
    assert env_refuses_direct_writes() is True
    monkeypatch.delenv(ENV_KEY, raising=False)
    assert env_refuses_direct_writes() is False


# ---------------------------------------------------------------------------
# Precedence table (from the plan) — entity and relationship resolvers
# ---------------------------------------------------------------------------

# (env_set, type write_policy, runtime default, expected effective)
PRECEDENCE_ROWS = [
    (False, None, "direct", "direct"),
    (False, None, "proposal_only", "proposal_only"),
    (False, "direct", "proposal_only", "direct"),  # explicit opts out of default
    (False, "proposal_only", "direct", "proposal_only"),
    (True, "direct", "direct", "proposal_only"),  # env wins over explicit direct
    (True, None, "direct", "proposal_only"),  # env wins over default direct
    (True, "proposal_only", "proposal_only", "proposal_only"),
]


@pytest.mark.parametrize("env_set,type_policy,runtime_default,expected", PRECEDENCE_ROWS)
def test_entity_precedence(
    env_set: bool, type_policy: str | None, runtime_default: str, expected: str
) -> None:
    config = _build_config(
        entity_write_policy=type_policy,
        relationship_write_policy=None,
        default_write_policy=runtime_default,
    )
    environ = {ENV_KEY: "1"} if env_set else {}
    assert effective_entity_write_policy(config, "Asset", environ=environ) == expected


@pytest.mark.parametrize("env_set,type_policy,runtime_default,expected", PRECEDENCE_ROWS)
def test_relationship_precedence(
    env_set: bool, type_policy: str | None, runtime_default: str, expected: str
) -> None:
    config = _build_config(
        entity_write_policy=None,
        relationship_write_policy=type_policy,
        default_write_policy=runtime_default,
    )
    environ = {ENV_KEY: "1"} if env_set else {}
    assert effective_relationship_write_policy(config, "asset_rel", environ=environ) == expected


# ---------------------------------------------------------------------------
# Unknown types fall back to the instance default
# ---------------------------------------------------------------------------


def test_unknown_entity_type_uses_default() -> None:
    config = _build_config(
        entity_write_policy=None,
        relationship_write_policy=None,
        default_write_policy="proposal_only",
    )
    assert effective_entity_write_policy(config, "Nonexistent", environ={}) == "proposal_only"


def test_unknown_relationship_type_uses_default() -> None:
    config = _build_config(
        entity_write_policy=None,
        relationship_write_policy=None,
        default_write_policy="direct",
    )
    assert effective_relationship_write_policy(config, "nope", environ={}) == "direct"


# ---------------------------------------------------------------------------
# mint_only is ABSOLUTE — the env kill-switch must NOT downgrade it
# ---------------------------------------------------------------------------


def test_mint_only_resolves_to_mint_only_env_unset() -> None:
    config = _build_config(
        entity_write_policy="mint_only",
        relationship_write_policy=None,
        default_write_policy="direct",
    )
    assert effective_entity_write_policy(config, "Asset", environ={}) == "mint_only"


def test_mint_only_is_absolute_over_env_kill_switch() -> None:
    # The kill-switch downgrades to proposal_only, which is WEAKER than mint_only
    # (it would admit governed verbs). mint_only must win, not be downgraded.
    config = _build_config(
        entity_write_policy="mint_only",
        relationship_write_policy=None,
        default_write_policy="proposal_only",
    )
    assert effective_entity_write_policy(config, "Asset", environ={ENV_KEY: "1"}) == "mint_only"


def test_mint_only_is_absolute_over_monkeypatched_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_KEY, "1")
    config = _build_config(
        entity_write_policy="mint_only",
        relationship_write_policy=None,
        default_write_policy="direct",
    )
    # environ=None reads os.environ, which monkeypatch has set.
    assert effective_entity_write_policy(config, "Asset") == "mint_only"

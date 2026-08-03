"""Config validation for declared entity identity keys."""

from __future__ import annotations

import pytest

from cruxible_core.config.loader import load_config
from cruxible_core.errors import ConfigError


def _config(entity_fields: str) -> str:
    return f"""\
version: '1.0'
name: identity_config
entity_types:
  Account:
{entity_fields}
    properties:
      account_id: {{type: string, primary_key: true}}
      name: {{type: string}}
      family: {{type: string}}
      rank: {{type: int}}
relationships: []
"""


def test_declared_identity_fields_load() -> None:
    config = load_config(
        _config(
            "    identity_hint: [name, family]\n"
            "    unique_by: [name, family]\n"
            "    id_pattern: '^account_[a-z0-9_]+$'\n"
        )
    )

    account = config.entity_types["Account"]
    assert account.identity_hint == ["name", "family"]
    assert account.unique_by == ["name", "family"]
    assert account.id_pattern == "^account_[a-z0-9_]+$"


@pytest.mark.parametrize("field_name", ["identity_hint", "unique_by"])
def test_identity_key_rejects_unknown_property(field_name: str) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(_config(f"    {field_name}: [name, missing]\n"))

    assert f"{field_name} references unknown properties: missing" in str(exc_info.value)


@pytest.mark.parametrize("field_name", ["identity_hint", "unique_by"])
def test_identity_key_rejects_empty_property_list(field_name: str) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(_config(f"    {field_name}: []\n"))

    assert f"{field_name} must contain at least one property" in str(exc_info.value)


@pytest.mark.parametrize("field_name", ["identity_hint", "unique_by"])
def test_identity_key_rejects_non_string_property(field_name: str) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(_config(f"    {field_name}: [rank]\n"))

    assert f"{field_name} properties must have type 'string': rank" in str(exc_info.value)


def test_id_pattern_rejects_invalid_regex() -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_config(_config("    id_pattern: '[unterminated'\n"))

    assert "id_pattern must be a valid regular expression" in str(exc_info.value)

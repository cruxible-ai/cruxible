"""Tests for config YAML loading and saving."""

from pathlib import Path
from unittest.mock import patch

import pytest

from cruxible_core import __version__
from cruxible_core.config.loader import load_config, load_config_from_string, save_config
from cruxible_core.config.schema import ConstraintSchema
from cruxible_core.errors import ConfigError


class TestLoadConfig:
    def test_load_from_file(self, configs_dir: Path):
        config = load_config(configs_dir / "car_parts.yaml")
        assert config.name == "car_parts_compatibility"
        assert "Vehicle" in config.entity_types
        assert "Part" in config.entity_types
        assert len(config.relationships) == 2
        assert len(config.named_queries) == 3
        assert len(config.constraints) == 1

    def test_load_from_string(self):
        yaml_str = """
version: "1.0"
name: "test"
entity_types:
  Widget:
    properties:
      widget_id:
        type: string
        primary_key: true
      name:
        type: string
relationships: []
"""
        config = load_config(yaml_str)
        assert config.name == "test"
        assert "Widget" in config.entity_types

    def test_missing_file(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config(Path("/nonexistent/config.yaml"))

    def test_invalid_yaml(self):
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config("{{not: valid: yaml: [}")

    def test_non_dict_yaml(self):
        with pytest.raises(ConfigError, match="mapping"):
            load_config("- just\n- a\n- list\n")

    def test_missing_required_fields(self):
        with pytest.raises(ConfigError, match="validation failed"):
            load_config("version: '1.0'\n")

    def test_validation_errors_include_field_paths(self):
        """Pydantic errors include the field location, not just the message."""
        with pytest.raises(ConfigError) as exc_info:
            load_config(
                "version: '1.0'\nname: bad\nentity_types:\n  - name: foo\nrelationships: []\n"
            )
        error_str = str(exc_info.value)
        assert "entity_types" in error_str

    def test_car_parts_entity_properties(self, configs_dir: Path):
        config = load_config(configs_dir / "car_parts.yaml")

        vehicle = config.entity_types["Vehicle"]
        assert vehicle.get_primary_key() == "vehicle_id"
        assert vehicle.properties["year"].type == "int"
        assert vehicle.properties["trim"].optional is True

        part = config.entity_types["Part"]
        assert part.get_primary_key() == "part_number"
        assert part.properties["category"].enum is not None
        assert "brakes" in part.properties["category"].enum

    def test_car_parts_relationships(self, configs_dir: Path):
        config = load_config(configs_dir / "car_parts.yaml")

        fits = config.get_relationship("fits")
        assert fits is not None
        assert fits.from_entity == "Part"
        assert fits.to_entity == "Vehicle"
        assert fits.reverse_name == "fitted_parts"
        assert "verified" in fits.properties

        replaces = config.get_relationship("replaces")
        assert replaces is not None
        assert replaces.from_entity == "Part"
        assert replaces.to_entity == "Part"

    def test_car_parts_named_queries(self, configs_dir: Path):
        config = load_config(configs_dir / "car_parts.yaml")

        assert "parts_for_vehicle" in config.named_queries
        pfv = config.named_queries["parts_for_vehicle"]
        assert pfv.entry_point == "Vehicle"
        assert len(pfv.traversal) == 1
        assert pfv.traversal[0].relationship == "fits"
        assert pfv.traversal[0].direction == "incoming"


class TestSaveConfig:
    def test_save_config_round_trip(self, configs_dir: Path, tmp_path: Path):
        """Load car_parts.yaml, save to tmp, reload, assert equality."""
        original = load_config(configs_dir / "car_parts.yaml")
        out_path = tmp_path / "saved.yaml"
        save_config(original, out_path)

        reloaded = load_config(out_path)
        assert reloaded.cruxible_version == __version__
        # Compare everything except the auto-stamped version
        assert reloaded.model_dump(exclude={"cruxible_version"}) == original.model_dump(
            exclude={"cruxible_version"}
        )

    def test_save_config_stamps_version(self, configs_dir: Path, tmp_path: Path):
        """save_config auto-stamps cruxible_version without mutating the input."""
        original = load_config(configs_dir / "car_parts.yaml")
        assert original.cruxible_version is None

        out_path = tmp_path / "stamped.yaml"
        save_config(original, out_path)

        # Input not mutated
        assert original.cruxible_version is None
        # Output has version
        reloaded = load_config(out_path)
        assert reloaded.cruxible_version == __version__

    def test_load_config_without_cruxible_version(self):
        """Old configs without cruxible_version still load fine."""
        yaml_str = (
            "version: '1.0'\n"
            "name: no_version\n"
            "entity_types:\n"
            "  A:\n"
            "    properties:\n"
            "      id: {type: string, primary_key: true}\n"
            "relationships: []\n"
        )
        config = load_config(yaml_str)
        assert config.cruxible_version is None

    def test_save_config_with_constraints(self, configs_dir: Path, tmp_path: Path):
        """Config with constraints survives save/reload."""
        config = load_config(configs_dir / "car_parts.yaml")
        config.constraints.append(
            ConstraintSchema(
                name="test_constraint",
                rule="replaces.FROM.category == replaces.TO.category",
                severity="warning",
            )
        )
        out_path = tmp_path / "with_constraints.yaml"
        save_config(config, out_path)

        reloaded = load_config(out_path)
        names = [c.name for c in reloaded.constraints]
        assert "test_constraint" in names

    def test_save_config_with_entity_constraints(self, tmp_path: Path):
        yaml_str = (
            "version: '1.0'\n"
            "name: entity_constraints\n"
            "entity_types:\n"
            "  Thing:\n"
            "    constraints:\n"
            "      - thing.id != ''\n"
            "    properties:\n"
            "      id: {type: string, primary_key: true}\n"
            "relationships: []\n"
        )
        config = load_config(yaml_str)
        out_path = tmp_path / "entity_constraints.yaml"
        save_config(config, out_path)

        reloaded = load_config(out_path)
        assert reloaded.entity_types["Thing"].constraints == ["thing.id != ''"]

    def test_save_config_relationship_aliases(self, tmp_path: Path):
        """Output YAML uses 'from'/'to' not 'from_entity'/'to_entity'."""
        yaml_str = (
            "version: '1.0'\n"
            "name: alias_test\n"
            "entity_types:\n"
            "  A:\n"
            "    properties:\n"
            "      id: {type: string, primary_key: true}\n"
            "  B:\n"
            "    properties:\n"
            "      id: {type: string, primary_key: true}\n"
            "relationships:\n"
            "  - name: links\n"
            "    from: A\n"
            "    to: B\n"
        )
        config = load_config(yaml_str)
        out_path = tmp_path / "aliases.yaml"
        save_config(config, out_path)

        raw = out_path.read_text()
        assert "from: A" in raw or "from:" in raw
        # Should NOT contain 'from_entity'
        assert "from_entity" not in raw
        assert "to_entity" not in raw

    def test_save_config_atomic_write_failure(self, tmp_path: Path):
        """Monkeypatch Path.replace to raise, verify ConfigError and no temp file."""
        yaml_str = (
            "version: '1.0'\n"
            "name: fail_test\n"
            "entity_types:\n"
            "  A:\n"
            "    properties:\n"
            "      id: {type: string, primary_key: true}\n"
            "relationships: []\n"
        )
        config = load_config(yaml_str)
        out_path = tmp_path / "should_not_exist.yaml"

        with patch.object(Path, "replace", side_effect=OSError("disk full")):
            with pytest.raises(ConfigError, match="Failed to write config file"):
                save_config(config, out_path)

        # No temp files should be left behind
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0
        assert not out_path.exists()


class TestLoadConfigFromString:
    def test_string_produces_same_config(self, configs_dir: Path):
        """load_config_from_string(yaml) == load_config(path) for same content."""
        path = configs_dir / "car_parts.yaml"
        yaml_str = path.read_text()
        from_path = load_config(path)
        from_string = load_config_from_string(yaml_str)
        assert from_path == from_string

    def test_invalid_yaml_raises(self):
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config_from_string("{{not: valid: yaml: [}")

    def test_non_mapping_raises(self):
        with pytest.raises(ConfigError, match="mapping"):
            load_config_from_string("- just\n- a\n- list\n")

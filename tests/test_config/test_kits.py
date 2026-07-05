"""Tests for kit manifests and kit-local provider loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.config.schema import ProviderSchema
from cruxible_core.errors import ConfigError
from cruxible_core.kits import (
    KitManifest,
    compute_kit_provider_sha256,
    compute_kit_runtime_digest,
    config_yaml_has_kit_provider_refs,
    get_kit_catalog,
    load_kit_provider_module,
    materialize_kit,
    namespace_kit_provider_ref,
    resolve_kit_provider_ref,
    resolve_kit_ref,
    write_materialized_kit_metadata,
)
from cruxible_core.provider.registry import resolve_provider


def test_kit_manifest_validates_roles() -> None:
    standalone = KitManifest(
        kit_id="demo",
        version="0.2.0",
        role="standalone",
        entry_config="config.yaml",
    )
    assert standalone.target_state is None

    overlay = KitManifest(
        kit_id="demo-overlay",
        version="0.2.0",
        role="overlay",
        target_state="demo",
        entry_config="config.yaml",
    )
    assert overlay.target_state == "demo"

    with pytest.raises(ValidationError, match="requires target_state"):
        KitManifest(
            kit_id="bad-overlay",
            version="0.2.0",
            role="overlay",
            entry_config="config.yaml",
        )


def test_kit_provider_ref_loads_relative_imports(tmp_path: Path) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "common.py").write_text("VALUE = 42\n")
    (providers / "main.py").write_text(
        "from .common import VALUE\n\ndef run(_input, _context):\n    return {'value': VALUE}\n"
    )
    write_materialized_kit_metadata(tmp_path)

    path, attr, kit_root = resolve_kit_provider_ref(
        "kit://providers/main.py::run",
        tmp_path,
    )
    module = load_kit_provider_module(path, kit_root)

    assert attr == "run"
    assert module.run({}, None) == {"value": 42}


def test_materialize_rejects_overlay_kit_for_standalone_init(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="overlay", target_state="demo")

    with pytest.raises(ConfigError, match="Use `cruxible state create-overlay --kit`"):
        materialize_kit(
            kit=f"file://{source}",
            root=tmp_path / "target",
            expected_role="standalone",
        )


def test_shipped_catalog_is_overridden_by_local_kits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cruxible_core.kits._discover_local_kit_catalog", lambda: {})
    shipped = get_kit_catalog()
    assert shipped["kev-reference"] == "oci://ghcr.io/cruxible-ai/kits/kev-reference:0.2.0"

    monkeypatch.setattr(
        "cruxible_core.kits._discover_local_kit_catalog",
        lambda: {"kev-reference": "file:///tmp/local-kev-reference"},
    )
    assert get_kit_catalog()["kev-reference"] == "file:///tmp/local-kev-reference"


def test_shipped_catalog_resolves_every_featured_kit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every kit advertised in the public READMEs must resolve from the shipped
    # alias catalog so `cruxible init --kit <name>` works without a source
    # checkout (local discovery disabled). Guards against advertising a kit that
    # only exists in the dev tree.
    monkeypatch.setattr("cruxible_core.kits._discover_local_kit_catalog", lambda: {})
    catalog = get_kit_catalog()
    for kit in (
        "agent-operation",
        "kev-reference",
        "kev-triage",
        "supply-chain-blast-radius",
        "case-law-monitoring",
    ):
        assert kit in catalog, f"{kit} is featured in the README but not in the shipped catalog"


def test_alias_oci_resolution_uses_shipped_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="standalone")
    pulled: list[str] = []
    monkeypatch.setattr("cruxible_core.kits._discover_local_kit_catalog", lambda: {})
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "cache"))

    def fake_pull(ref: str) -> Path:
        pulled.append(ref)
        return source

    monkeypatch.setattr("cruxible_core.kits._pull_oci_kit", fake_pull)

    bundle = resolve_kit_ref("kev-reference")

    assert pulled == ["ghcr.io/cruxible-ai/kits/kev-reference:0.2.0"]
    assert bundle.manifest.kit_id == "demo"


def test_runtime_digest_ignores_unrelated_files_and_tracks_kit_files(tmp_path: Path) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    provider = providers / "main.py"
    provider.write_text("def run(_input, _context):\n    return {}\n")

    baseline = compute_kit_runtime_digest(tmp_path)
    (tmp_path / "notes.txt").write_text("not kit owned\n")
    assert compute_kit_runtime_digest(tmp_path) == baseline

    provider.write_text("def run(_input, _context):\n    return {'changed': True}\n")
    assert compute_kit_runtime_digest(tmp_path) != baseline


def test_dev_tree_resolution_requires_explicit_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "main.py").write_text("def run(_input, _context):\n    return {}\n")

    with pytest.raises(ConfigError, match="dev-tree kit root"):
        resolve_kit_provider_ref("kit://providers/main.py::run", tmp_path)

    monkeypatch.setenv("CRUXIBLE_KIT_DEV_RESOLVE", "1")
    path, _attr, _root = resolve_kit_provider_ref("kit://providers/main.py::run", tmp_path)
    assert path.name == "main.py"


def test_materialized_metadata_ignores_unrelated_files_but_detects_provider_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    _write_minimal_kit(source, role="standalone")
    providers = source / "providers"
    providers.mkdir()
    (providers / "main.py").write_text("def run(_input, _context):\n    return {}\n")

    materialize_kit(kit=f"file://{source}", root=target, expected_role="standalone")
    (target / "unrelated.txt").write_text("outside the kit runtime\n")
    resolve_kit_provider_ref("kit://providers/main.py::run", target)

    (target / "providers" / "main.py").write_text(
        "def run(_input, _context):\n    return {'changed': True}\n"
    )
    with pytest.raises(ConfigError, match="Materialized kit contents changed"):
        resolve_kit_provider_ref("kit://providers/main.py::run", target)


def test_provider_resolution_rejects_traversal_symlink_and_missing_callable(
    tmp_path: Path,
) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    target = providers / "target.py"
    target.write_text("VALUE = 1\n")
    write_materialized_kit_metadata(tmp_path)
    symlink = providers / "link.py"
    symlink.symlink_to(target)

    with pytest.raises(ConfigError, match="without '..'"):
        resolve_kit_provider_ref("kit://../target.py::run", tmp_path)
    with pytest.raises(ConfigError, match="symlinks"):
        resolve_kit_provider_ref("kit://providers/link.py::run", tmp_path)
    symlink.unlink()
    write_materialized_kit_metadata(tmp_path)

    provider = ProviderSchema(
        kind="function",
        contract_in="EmptyInput",
        contract_out="EmptyOutput",
        ref="kit://providers/target.py::missing",
        version="1.0.0",
    )
    with pytest.raises(ConfigError, match="does not resolve to an attribute"):
        resolve_provider("missing_callable", provider, config_base_path=tmp_path)


def test_provider_hash_changes_when_provider_tree_changes(tmp_path: Path) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    providers = tmp_path / "providers"
    providers.mkdir()
    provider = providers / "main.py"
    provider.write_text("def run(_input, _context):\n    return {}\n")
    write_materialized_kit_metadata(tmp_path)

    before = compute_kit_provider_sha256("kit://providers/main.py::run", tmp_path)
    provider.write_text("def run(_input, _context):\n    return {'changed': True}\n")
    write_materialized_kit_metadata(tmp_path)

    assert compute_kit_provider_sha256("kit://providers/main.py::run", tmp_path) != before


def test_config_yaml_kit_ref_detection_is_provider_ref_only() -> None:
    assert config_yaml_has_kit_provider_refs(
        "version: '1.0'\nproviders:\n  p:\n    ref: kit://providers/main.py::run\n"
    )
    assert not config_yaml_has_kit_provider_refs(
        "version: '1.0'\ndescription: 'example kit:// text only'\nproviders: {}\n"
    )


def test_materialized_metadata_records_bundle_and_runtime_digest(tmp_path: Path) -> None:
    _write_minimal_kit(tmp_path, role="standalone")
    write_materialized_kit_metadata(tmp_path, bundle_digest="sha256:bundle")

    payload = json.loads((tmp_path / ".cruxible" / "kit.json").read_text())
    assert payload["bundle_digest"] == "sha256:bundle"
    assert payload["runtime_digest"].startswith("sha256:")


def test_namespace_kit_provider_ref_is_idempotent() -> None:
    ref = "kit://providers/main.py::run"
    namespaced = namespace_kit_provider_ref(ref, "demo")
    assert namespaced == "kit://demo/providers/main.py::run"
    assert namespace_kit_provider_ref(namespaced, "demo") == namespaced


def test_namespaced_kit_provider_ref_resolves_under_instance_kits_dir(tmp_path: Path) -> None:
    instance_root = tmp_path / "instance"
    kit_root = instance_root / "kits" / "demo"
    kit_root.mkdir(parents=True)
    _write_minimal_kit(kit_root, role="standalone")
    providers = kit_root / "providers"
    providers.mkdir()
    (providers / "main.py").write_text("def run(_input, _context):\n    return {}\n")
    write_materialized_kit_metadata(kit_root)
    config_base = instance_root / ".cruxible" / "configs"
    config_base.mkdir(parents=True)

    path, attr, resolved_root = resolve_kit_provider_ref(
        "kit://demo/providers/main.py::run",
        config_base,
    )

    assert attr == "run"
    assert resolved_root == kit_root.resolve()
    assert path == kit_root.resolve() / "providers" / "main.py"


def test_namespaced_kit_provider_ref_rejects_kit_id_mismatch(tmp_path: Path) -> None:
    instance_root = tmp_path / "instance"
    kit_root = instance_root / "kits" / "other-id"
    kit_root.mkdir(parents=True)
    _write_minimal_kit(kit_root, role="standalone")  # manifest kit_id is 'demo'
    (kit_root / "providers").mkdir()
    (kit_root / "providers" / "main.py").write_text("def run(_i, _c):\n    return {}\n")
    write_materialized_kit_metadata(kit_root)

    with pytest.raises(ConfigError, match="declares kit_id 'demo', not 'other-id'"):
        resolve_kit_provider_ref("kit://other-id/providers/main.py::run", instance_root)


def test_kit_provider_ref_refuses_flat_and_namespaced_ambiguity(tmp_path: Path) -> None:
    flat_root = tmp_path / "flat"
    flat_root.mkdir()
    _write_minimal_kit(flat_root, role="standalone")
    (flat_root / "providers").mkdir()
    (flat_root / "providers" / "main.py").write_text("def run(_i, _c):\n    return {}\n")
    write_materialized_kit_metadata(flat_root)

    scoped_root = flat_root / "kits" / "demo"
    scoped_root.mkdir(parents=True)
    _write_minimal_kit(scoped_root, role="standalone")
    (scoped_root / "providers").mkdir()
    (scoped_root / "providers" / "main.py").write_text("def run(_i, _c):\n    return {}\n")
    write_materialized_kit_metadata(scoped_root)

    with pytest.raises(ConfigError, match="ambiguous"):
        resolve_kit_provider_ref("kit://demo/providers/main.py::run", flat_root)


def test_rewrite_extends_inserts_when_missing_and_replaces_when_present(tmp_path: Path) -> None:
    from cruxible_core.kits import _rewrite_extends

    config_path = tmp_path / "config.yaml"
    config_path.write_text("version: '1.0'\nname: demo\n")
    _rewrite_extends(config_path, ".cruxible/upstream/current/config.yaml")
    lines = config_path.read_text().splitlines()
    assert lines[0] == "extends: .cruxible/upstream/current/config.yaml"

    _rewrite_extends(config_path, "other/upstream.yaml")
    updated = config_path.read_text().splitlines()
    assert updated.count("extends: other/upstream.yaml") == 1
    assert not any(line.startswith("extends: .cruxible") for line in updated)


def _write_minimal_kit(
    root: Path,
    *,
    role: str,
    target_state: str | None = None,
) -> None:
    target_line = f"target_state: {target_state}\n" if target_state else ""
    root.joinpath("cruxible-kit.yaml").write_text(
        "schema_version: cruxible.kit.v1\n"
        "kit_id: demo\n"
        "version: 0.2.0\n"
        f"role: {role}\n"
        f"{target_line}"
        "entry_config: config.yaml\n"
        "provider_paths:\n"
        "  - providers\n"
        "copy_paths: []\n"
        "requires_extras: []\n"
    )
    root.joinpath("config.yaml").write_text(
        "version: '1.0'\nname: demo\nentity_types: {}\nrelationships: []\n"
    )
    root.joinpath("cruxible.lock.yaml").write_text(
        "version: '1'\nconfig_digest: test\nartifacts: {}\nproviders: {}\n"
    )

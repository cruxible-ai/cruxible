"""Tests for kit manifests and kit-local provider loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.config.schema import ProviderSchema
from cruxible_core.errors import ConfigError
from cruxible_core.kit_defaults import DEFAULT_BASE_KIT, get_default_base_kit
from cruxible_core.kits import (
    _SHIPPED_KIT_CATALOG,
    KitManifest,
    compute_kit_provider_sha256,
    compute_kit_runtime_digest,
    config_yaml_has_kit_provider_refs,
    enforce_min_core_version,
    get_kit_catalog,
    load_kit_manifest,
    load_kit_provider_module,
    materialize_kit,
    namespace_kit_provider_ref,
    resolve_kit_provider_ref,
    resolve_kit_ref,
    write_materialized_kit_metadata,
)
from cruxible_core.provider.registry import resolve_provider
from tests.support.workflow_helpers import write_placeholder_kit_lock


@pytest.fixture(autouse=True)
def _hermetic_kit_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the kit bundle cache: the shared user cache can hold legacy
    entries (e.g. pre-digest locks) that make these tests environment-dependent."""
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "kit-cache"))


def test_kit_manifest_validates_roles() -> None:
    base = KitManifest(
        kit_id="operation-base",
        version="0.2.0",
        role="base",
        entry_config="config.yaml",
    )
    assert base.target_state is None

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

    with pytest.raises(ValidationError, match="must not set requires_base"):
        KitManifest(
            kit_id="bad-base",
            version="0.2.0",
            role="base",
            requires_base="another-base",
        )


def test_default_base_kit_is_opt_out_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRUXIBLE_DEFAULT_BASE_KIT", raising=False)
    assert get_default_base_kit() == DEFAULT_BASE_KIT

    monkeypatch.setenv("CRUXIBLE_DEFAULT_BASE_KIT", "custom-base")
    assert get_default_base_kit() == "custom-base"

    monkeypatch.setenv("CRUXIBLE_DEFAULT_BASE_KIT", "off")
    assert get_default_base_kit() is None


def test_default_base_kit_respects_explicit_empty_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_DEFAULT_BASE_KIT", "process-only-base")

    assert get_default_base_kit({}) == DEFAULT_BASE_KIT


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


def test_kit_catalog_comes_from_local_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # There is no hard-coded shipped alias table any more: aliases come from the
    # source checkout (development) or, for installed distributions, from the
    # packaged kit distribution manifest.
    monkeypatch.setattr("cruxible_core.kits._discover_local_kit_catalog", lambda: {})
    assert get_kit_catalog() == {}

    monkeypatch.setattr(
        "cruxible_core.kits._discover_local_kit_catalog",
        lambda: {"kev-reference": "file:///tmp/local-kev-reference"},
    )
    assert get_kit_catalog()["kev-reference"] == "file:///tmp/local-kev-reference"


def test_unknown_kit_message_enumerates_available_kits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cruxible_core.kits._discover_local_kit_catalog",
        lambda: {"kev-reference": "file:///tmp/local-kev-reference"},
    )
    with pytest.raises(ConfigError) as excinfo:
        resolve_kit_ref("no-such-kit")

    message = str(excinfo.value)
    assert "Unknown kit 'no-such-kit'" in message
    assert "kev-reference" in message


def test_oci_alias_refs_are_gone_from_the_shipped_catalog() -> None:
    # The ghcr packages these named were never published; an alias must never
    # route to a dead oci ref again.
    assert _SHIPPED_KIT_CATALOG == {}
    assert all("oci://" not in ref for ref in get_kit_catalog().values())


def test_explicit_oci_ref_still_pulls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Aliases never route to oci any more, but an explicit third-party
    # `oci://` ref still resolves through the pull path.
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="standalone")
    pulled: list[str] = []
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "cache"))

    def fake_pull(ref: str) -> Path:
        pulled.append(ref)
        return source

    monkeypatch.setattr("cruxible_core.kits._pull_oci_kit", fake_pull)

    bundle = resolve_kit_ref("oci://example.com/kits/demo:1.0.0")

    assert pulled == ["example.com/kits/demo:1.0.0"]
    assert bundle.manifest.kit_id == "demo"


def test_manifest_without_min_core_version_still_loads(tmp_path: Path) -> None:
    # Backward compat: cruxible.kit.v1 manifests authored before the field.
    _write_minimal_kit(tmp_path, role="standalone")
    assert "min_core_version" not in tmp_path.joinpath("cruxible-kit.yaml").read_text()

    manifest = load_kit_manifest(tmp_path)

    assert manifest.min_core_version is None


def test_unknown_manifest_fields_are_ignored(tmp_path: Path) -> None:
    # The additive-at-v1 claim: an older core ignores fields it does not know,
    # which is why min_core_version does not need a schema_version bump.
    _write_minimal_kit(tmp_path, role="standalone")
    path = tmp_path / "cruxible-kit.yaml"
    path.write_text(path.read_text() + "some_future_field: 1\n")

    assert load_kit_manifest(tmp_path).kit_id == "demo"


def test_min_core_version_floor_refuses_older_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="standalone", min_core_version="9.9.0")
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("cruxible_core.__version__", "0.2.8")

    with pytest.raises(ConfigError) as excinfo:
        resolve_kit_ref(f"file://{source}")

    assert str(excinfo.value) == (
        "Kit 'demo' requires cruxible core >= 9.9.0, but this core is 0.2.8. "
        "Upgrade with: pip install --upgrade cruxible"
    )


def test_min_core_version_floor_refuses_via_local_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="standalone", min_core_version="9.9.0")
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "cruxible_core.kits._discover_local_kit_catalog",
        lambda: {"demo": f"file://{source}"},
    )

    with pytest.raises(ConfigError, match="Kit 'demo' requires cruxible core >= 9.9.0"):
        resolve_kit_ref("demo")


def test_min_core_version_floor_refuses_via_explicit_oci_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="standalone", min_core_version="9.9.0")
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("cruxible_core.kits._pull_oci_kit", lambda ref: source)

    with pytest.raises(ConfigError, match="Kit 'demo' requires cruxible core >= 9.9.0"):
        resolve_kit_ref("oci://example.com/kits/demo:1.0.0")


@pytest.mark.parametrize("core_version", ["1.4.0", "1.4.1", "2.0.0", "1.4.0.post1"])
def test_min_core_version_floor_allows_equal_or_newer_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_version: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="standalone", min_core_version="1.4.0")
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("cruxible_core.__version__", core_version)

    bundle = resolve_kit_ref(f"file://{source}")

    assert bundle.manifest.min_core_version == "1.4.0"


@pytest.mark.parametrize("core_version", ["1.4.0rc1", "1.4.0.dev1", "1.3.9"])
def test_prerelease_core_does_not_satisfy_a_final_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_version: str,
) -> None:
    # PEP 440 ordering: 1.4.0rc1 and 1.4.0.dev1 both sort below 1.4.0, so a
    # kit requiring 1.4.0 must refuse them.
    source = tmp_path / "source"
    source.mkdir()
    _write_minimal_kit(source, role="standalone", min_core_version="1.4.0")
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("cruxible_core.__version__", core_version)

    with pytest.raises(ConfigError, match="requires cruxible core >= 1.4.0"):
        resolve_kit_ref(f"file://{source}")


@pytest.mark.parametrize("bad_floor", ["garbage", "0..9", "²", "1.2.3.4.hello"])
def test_unparseable_min_core_version_is_a_manifest_error(
    tmp_path: Path,
    bad_floor: str,
) -> None:
    # Fail closed at load, naming the kit and the value -- never a crash later
    # at enforcement time. '²' is str.isdigit() but not int()-able.
    _write_minimal_kit(tmp_path, role="standalone", min_core_version=bad_floor)

    with pytest.raises(ConfigError) as excinfo:
        load_kit_manifest(tmp_path)

    message = str(excinfo.value)
    assert "Invalid kit manifest" in message
    assert "demo" in message
    assert bad_floor in message
    assert "PEP 440" in message


def test_pep440_normalized_floor_is_accepted(tmp_path: Path) -> None:
    # A leading 'v' is valid PEP 440 and normalizes to 9.9.0.
    _write_minimal_kit(tmp_path, role="standalone", min_core_version="v9.9.0")

    manifest = load_kit_manifest(tmp_path)

    assert manifest.min_core_version == "v9.9.0"
    with pytest.raises(ConfigError, match="requires cruxible core >= v9.9.0"):
        enforce_min_core_version(manifest)


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
    min_core_version: str | None = None,
) -> None:
    target_line = f"target_state: {target_state}\n" if target_state else ""
    floor_line = f"min_core_version: '{min_core_version}'\n" if min_core_version else ""
    root.joinpath("cruxible-kit.yaml").write_text(
        "schema_version: cruxible.kit.v1\n"
        "kit_id: demo\n"
        "version: 0.2.0\n"
        f"role: {role}\n"
        f"{target_line}"
        f"{floor_line}"
        "entry_config: config.yaml\n"
        "provider_paths:\n"
        "  - providers\n"
        "copy_paths: []\n"
        "requires_extras: []\n"
    )
    root.joinpath("config.yaml").write_text(
        "version: '1.0'\nname: demo\nentity_types: {}\nrelationships: []\n"
    )
    write_placeholder_kit_lock(root)

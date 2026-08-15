"""Regenerating a bundled kit lock is disclosed, never silent.

``docs/kit-authoring.md`` tells kit consumers they should not silently
experience a regenerated lock: the publisher's pinned artifact and provider
digests are what a bundled lock is FOR, and regenerating discards them. Both
mismatch kinds — the lock's self-digest and the config digest — previously
routed into the same silent ``build_lock`` fallback with nothing appended to
``InitResult.warnings``, so a consumer could not tell that the pin had been
dropped, let alone which digest failed.

The per-mismatch tests drive ``_merge_kit_locks`` directly. It is the decision
point (the caller only writes whatever it returns), and driving it directly lets
each mismatch kind be produced in isolation instead of hoping a full init
reaches it. The end-to-end tests at the bottom then prove the warning actually
survives the trip out to a caller, which is where it was being dropped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.config.loader import load_config
from cruxible_core.service.lifecycle import (
    _merge_kit_locks,
    service_init,
    service_init_governed_upload,
)
from cruxible_core.workflow.compiler import compute_lock_digest, load_lock

_KIT_ID = "warned-kit"

_CONFIG_YAML = """\
version: '1.0'
name: warned
entity_types:
  Widget:
    properties:
      widget_id: {type: string, primary_key: true}
relationships: []
"""


def _write_kit(root: Path, *, lock_body: str) -> tuple[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("cruxible-kit.yaml").write_text(
        "schema_version: cruxible.kit.v1\n"
        f"kit_id: {_KIT_ID}\n"
        "version: 0.2.0\n"
        "role: standalone\n"
        "entry_config: config.yaml\n"
        "provider_paths: []\n"
        "copy_paths: []\n"
        "requires_extras: []\n"
    )
    root.joinpath("config.yaml").write_text(_CONFIG_YAML)
    root.joinpath("cruxible.lock.yaml").write_text(lock_body)
    return _KIT_ID, root


def _valid_lock_body(kit_root: Path) -> str:
    """A lock whose config_digest and self-digest both verify against the kit."""
    from cruxible_core.workflow.compiler import build_lock, write_lock

    lock = build_lock(load_config(kit_root / "config.yaml"), kit_root)
    lock.lock_digest = compute_lock_digest(lock)
    write_lock(lock, kit_root / "cruxible.lock.yaml")
    return (kit_root / "cruxible.lock.yaml").read_text()


def _break_lock_digest(lock_path: Path) -> None:
    """Corrupt the lock's self-digest, leaving its config_digest intact."""
    stale = load_lock(lock_path).lock_digest
    assert stale is not None
    lock_path.write_text(lock_path.read_text().replace(stale, "sha256:0"))


@pytest.fixture
def kit_root(tmp_path: Path) -> Path:
    root = tmp_path / "kits" / _KIT_ID
    _write_kit(root, lock_body="version: '1'\nartifacts: {}\nproviders: {}\n")
    _valid_lock_body(root)
    return root


def test_a_verifying_lock_merges_with_no_warnings(kit_root: Path) -> None:
    """The control: nothing to disclose when the publisher's pin holds."""
    config = load_config(kit_root / "config.yaml")

    merged, warnings = _merge_kit_locks(config, [(_KIT_ID, kit_root)])

    assert merged is not None
    assert warnings == []


def test_lock_digest_mismatch_warns_and_names_the_kit_and_the_digest(kit_root: Path) -> None:
    lock_path = kit_root / "cruxible.lock.yaml"
    _break_lock_digest(lock_path)
    config = load_config(kit_root / "config.yaml")

    merged, warnings = _merge_kit_locks(config, [(_KIT_ID, kit_root)])

    assert merged is None, "a lock that does not verify must not be adopted"
    assert len(warnings) == 1
    assert _KIT_ID in warnings[0]
    assert "lock digest mismatch" in warnings[0]
    assert "REGENERATED" in warnings[0]


def test_config_digest_mismatch_warns_and_names_the_other_digest(kit_root: Path) -> None:
    """The config moved out from under a lock that still verifies against itself."""
    kit_root.joinpath("config.yaml").write_text(
        _CONFIG_YAML.replace("name: warned", "name: warned-but-edited")
    )
    config = load_config(kit_root / "config.yaml")

    merged, warnings = _merge_kit_locks(config, [(_KIT_ID, kit_root)])

    assert merged is None
    assert len(warnings) == 1
    assert _KIT_ID in warnings[0]
    assert "config digest mismatch" in warnings[0]


def test_the_two_mismatch_kinds_are_distinguishable(kit_root: Path, tmp_path: Path) -> None:
    """The whole point of naming the digest: a consumer can tell them apart.

    Both used to produce the same silence, so a stale lock and a config that had
    moved underneath it were indistinguishable from the consumer's side.
    """
    config = load_config(kit_root / "config.yaml")
    lock_path = kit_root / "cruxible.lock.yaml"

    kit_root.joinpath("config.yaml").write_text(
        _CONFIG_YAML.replace("name: warned", "name: edited")
    )
    _, config_warnings = _merge_kit_locks(
        load_config(kit_root / "config.yaml"), [(_KIT_ID, kit_root)]
    )

    kit_root.joinpath("config.yaml").write_text(_CONFIG_YAML)
    _break_lock_digest(lock_path)
    _, lock_warnings = _merge_kit_locks(config, [(_KIT_ID, kit_root)])

    assert config_warnings != lock_warnings


# ---------------------------------------------------------------------------
# End to end: the warning has to survive the trip out to a caller
# ---------------------------------------------------------------------------


def _stale_config_kit_source(tmp_path: Path, name: str) -> str:
    """A materializable standalone kit whose config moved out from under its lock.

    The CONFIG-digest mismatch is the reachable end-to-end route. A kit whose
    lock fails its own self-digest never gets this far: ``resolve_kit_ref``
    refuses it outright at materialization ("the lock was edited after it was
    generated"). The ``lock`` branch inside ``_merge_kit_locks`` is therefore
    defense in depth for callers that assemble kit dirs by other means, which is
    why it is covered by the unit tests above rather than here.
    """
    source = tmp_path / name
    _write_kit(source, lock_body="version: '1'\nartifacts: {}\nproviders: {}\n")
    _valid_lock_body(source)
    source.joinpath("config.yaml").write_text(
        _CONFIG_YAML.replace("name: warned", "name: warned-but-edited")
    )
    return f"file://{source}"


def test_service_init_surfaces_the_lock_warning_on_init_result(tmp_path: Path) -> None:
    """The direct service path: the warning reaches ``InitResult.warnings``."""
    result = service_init(
        tmp_path / "instance",
        kits=[_stale_config_kit_source(tmp_path, "kit-source")],
    )

    assert any("bundled lock" in warning for warning in result.warnings), result.warnings
    assert any(_KIT_ID in warning for warning in result.warnings)
    assert any("config digest mismatch" in warning for warning in result.warnings)


def test_governed_upload_surfaces_the_lock_warning(tmp_path: Path) -> None:
    """The governed path runs the OTHER producer and must disclose too.

    A governed upload copies a caller-owned kit workspace and installs its
    bundled lock through ``_install_instance_lock_from_materialized_kit`` — a
    separate function from the composed-kits merge, with its own silent
    regeneration fallback.
    """
    workspace = tmp_path / "workspace"
    _write_kit(workspace, lock_body="version: '1'\nartifacts: {}\nproviders: {}\n")
    _valid_lock_body(workspace)
    # The uploaded config differs from the one the bundled lock was built against.
    uploaded = _CONFIG_YAML.replace("name: warned", "name: warned-uploaded")

    result = service_init_governed_upload(
        tmp_path / "governed",
        workspace_root=workspace,
        config_yaml=uploaded,
    )

    assert any("bundled lock" in warning for warning in result.warnings), result.warnings
    assert any("config digest mismatch" in warning for warning in result.warnings)


def test_a_verifying_kit_lock_adds_no_warning_end_to_end(tmp_path: Path) -> None:
    """The control: a healthy kit must not emit a scary disclosure."""
    source = tmp_path / "kit-source-ok"
    _write_kit(source, lock_body="version: '1'\nartifacts: {}\nproviders: {}\n")
    _valid_lock_body(source)

    result = service_init(tmp_path / "instance-ok", kits=[f"file://{source}"])

    assert [w for w in result.warnings if "bundled lock" in w] == []

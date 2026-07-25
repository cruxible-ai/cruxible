"""Digest-verification hardening: recorded digests are compared, not just recorded.

Each test here covers one site where a digest used to be computed and stored
without ever being checked against the thing it pins. The shared expectation is
that a mismatch is a refusal naming what was expected, what was found, and how a
legitimate operator recovers -- never a warning, never silent acceptance.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.kits import OCI_PIN_FILE, OCI_REPIN_ENV, resolve_kit_ref
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_create_state_overlay,
    service_lock,
    service_publish_state,
    service_pull_state_preview,
    service_run,
)
from cruxible_core.transport.backends import RELEASE_MEMBER_DIGESTS_FILE
from tests.support.workflow_helpers import write_placeholder_kit_lock

STATE_MODEL_YAML = """\
version: "1.0"
name: digest_reference

entity_types:
  Case:
    properties:
      case_id:
        type: string
        primary_key: true
      title:
        type: string

relationships:
  - name: cites
    from: Case
    to: Case
"""


def _root_instance(tmp_path: Path) -> CruxibleInstance:
    root = tmp_path / "root"
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(STATE_MODEL_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    service_lock(instance)
    return instance


def _publish(tmp_path: Path, *, release_id: str = "r1") -> Path:
    instance = _root_instance(tmp_path)
    release_dir = tmp_path / "releases" / release_id
    service_publish_state(
        instance,
        transport_ref=f"file://{release_dir}",
        state_id="digest-state",
        release_id=release_id,
        compatibility="data_only",
    )
    return release_dir


# ---------------------------------------------------------------------------
# Site 1: a kit bundle's lock is verified, not merely found
# ---------------------------------------------------------------------------


def _write_overlay_kit(kit_dir: Path, *, target_state: str) -> None:
    kit_dir.mkdir(parents=True, exist_ok=True)
    (kit_dir / "config.yaml").write_text(
        "\n".join(
            [
                "version: '1.0'",
                "name: digest-overlay",
                "extends: placeholder.yaml",
                "entity_types: {}",
                "relationships: []",
            ]
        )
        + "\n"
    )
    (kit_dir / "cruxible-kit.yaml").write_text(
        "\n".join(
            [
                "schema_version: cruxible.kit.v1",
                "kit_id: digest-overlay",
                "version: 0.2.0",
                "role: overlay",
                f"target_state: {target_state}",
                "entry_config: config.yaml",
                "provider_paths: []",
                "copy_paths: []",
                "requires_extras: []",
            ]
        )
        + "\n"
    )
    write_placeholder_kit_lock(kit_dir)


def test_kit_materialization_refuses_a_tampered_bundled_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_dir = _publish(tmp_path)
    kit_dir = tmp_path / "kit"
    _write_overlay_kit(kit_dir, target_state="digest-state")

    # Edit the lock body without updating the lock_digest it records: exactly
    # what an attacker repointing a kit's pinned providers would produce.
    lock_path = kit_dir / "cruxible.lock.yaml"
    lock_path.write_text(lock_path.read_text().replace("config_digest: test", "config_digest: xxx"))

    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"digest-overlay": f"file://{kit_dir}"},
    )
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "kit-cache"))

    with pytest.raises(ConfigError) as exc_info:
        service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            kit="digest-overlay",
            root_dir=tmp_path / "overlay",
        )

    message = str(exc_info.value)
    assert "failed digest verification" in message
    assert "records lock_digest=" in message
    assert "its contents hash to" in message
    assert "cruxible lock --kit-dir" in message


def test_kit_materialization_refuses_a_lock_without_a_recorded_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_dir = _publish(tmp_path)
    kit_dir = tmp_path / "kit"
    _write_overlay_kit(kit_dir, target_state="digest-state")
    (kit_dir / "cruxible.lock.yaml").write_text(
        "version: '1'\nconfig_digest: test\nartifacts: {}\nproviders: {}\n"
    )

    monkeypatch.setattr(
        "cruxible_core.kits.get_kit_catalog",
        lambda: {"digest-overlay": f"file://{kit_dir}"},
    )
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(tmp_path / "kit-cache"))

    with pytest.raises(ConfigError, match="records no lock_digest"):
        service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            kit="digest-overlay",
            root_dir=tmp_path / "overlay",
        )


# ---------------------------------------------------------------------------
# Site 2: pulled release bundle members are verified before any apply
# ---------------------------------------------------------------------------


def test_published_bundle_pins_every_member_by_digest(tmp_path: Path) -> None:
    release_dir = _publish(tmp_path)
    sidecar = json.loads((release_dir / RELEASE_MEMBER_DIGESTS_FILE).read_text())

    assert sidecar["format_version"] == 1
    assert set(sidecar["digests"]) == {
        "manifest.json",
        "snapshot.json",
        "config.yaml",
        "graph.json",
        "cruxible.lock.yaml",
    }
    for member, digest in sidecar["digests"].items():
        assert digest.startswith("sha256:")
        assert (release_dir / member).exists(), member


@pytest.mark.parametrize(
    "member",
    ["graph.json", "config.yaml", "cruxible.lock.yaml", "manifest.json"],
)
def test_overlay_refuses_a_tampered_bundle_member(tmp_path: Path, member: str) -> None:
    release_dir = _publish(tmp_path)
    tampered = release_dir / member
    if member == "manifest.json":
        # Stay valid JSON that still parses into a manifest: the point is that
        # digest verification catches it, not that deserialization happens to.
        payload = json.loads(tampered.read_text())
        payload["owned_entity_types"] = [*payload["owned_entity_types"], "Smuggled"]
        tampered.write_text(json.dumps(payload, indent=2, sort_keys=True))
    else:
        tampered.write_text(tampered.read_text() + "\n# injected\n")

    with pytest.raises(ConfigError) as exc_info:
        service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            no_kit=True,
            root_dir=tmp_path / "overlay",
        )

    message = str(exc_info.value)
    assert "failed digest verification" in message
    assert member in message
    assert "expected sha256:" in message or "records sha256:" in message
    assert "Re-publish the release upstream" in message


def test_overlay_refuses_a_bundle_member_the_publisher_never_pinned(tmp_path: Path) -> None:
    release_dir = _publish(tmp_path)
    (release_dir / "extra.json").write_text("{}")

    with pytest.raises(ConfigError, match="does not pin: extra.json"):
        service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            no_kit=True,
            root_dir=tmp_path / "overlay",
        )


def test_pre_field_bundle_still_verifies_what_snapshot_json_records(tmp_path: Path) -> None:
    """Bundles published before members.json keep working -- but not blindly.

    snapshot.json has always recorded raw-byte digests for graph.json and the
    lock, so those two are still verified. Only config.yaml is unverifiable, and
    the pull says so rather than implying the bundle was checked.
    """
    release_dir = _publish(tmp_path)
    (release_dir / RELEASE_MEMBER_DIGESTS_FILE).unlink()

    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        no_kit=True,
        root_dir=tmp_path / "overlay",
    ).instance
    preview = service_pull_state_preview(overlay)

    assert any("predates per-member digests" in warning for warning in preview.warnings)
    assert any("config.yaml could not be verified" in warning for warning in preview.warnings)
    assert preview.conflicts == []


def test_pre_field_bundle_refuses_a_graph_that_snapshot_json_contradicts(tmp_path: Path) -> None:
    release_dir = _publish(tmp_path)
    (release_dir / RELEASE_MEMBER_DIGESTS_FILE).unlink()
    graph_path = release_dir / "graph.json"
    graph_path.write_text(graph_path.read_text().replace("{", "{ ", 1))

    with pytest.raises(ConfigError) as exc_info:
        service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            no_kit=True,
            root_dir=tmp_path / "overlay",
        )

    message = str(exc_info.value)
    assert "snapshot.json records" in message
    assert "graph.json" in message


def test_pull_refuses_a_locally_edited_materialized_upstream(tmp_path: Path) -> None:
    release_dir = _publish(tmp_path)
    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        no_kit=True,
        root_dir=tmp_path / "overlay",
    ).instance

    upstream_graph = overlay.get_root_path() / ".cruxible" / "upstream" / "current" / "graph.json"
    upstream_graph.write_text(upstream_graph.read_text().replace("{", "{ ", 1))

    with pytest.raises(ConfigError) as exc_info:
        service_pull_state_preview(overlay)

    message = str(exc_info.value)
    assert "no longer matches its recorded 'graph.json' digest" in message
    assert "Restore the file from the published release" in message


# ---------------------------------------------------------------------------
# Site 3: mutable refs are pinned to the content they first resolved to
# ---------------------------------------------------------------------------


def test_pull_refuses_a_release_id_rewritten_upstream(tmp_path: Path) -> None:
    release_dir = _publish(tmp_path)
    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        no_kit=True,
        root_dir=tmp_path / "overlay",
    ).instance

    # Republish different content under the SAME release_id, the file-transport
    # equivalent of repointing a mutable tag.
    shutil.rmtree(release_dir)
    rewritten = _publish(tmp_path / "second", release_id="r1")
    shutil.copytree(rewritten, release_dir)

    with pytest.raises(ConfigError) as exc_info:
        service_pull_state_preview(overlay)

    message = str(exc_info.value)
    assert "Published releases are immutable" in message
    assert "under a NEW release_id" in message


def test_oci_kit_ref_is_pinned_on_first_resolution_and_verified_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repointed oci:// tag is refused, and re-pinning is explicit.

    The registry is simulated locally: ``_pull_oci_kit`` is replaced with a
    directory copy so the pin logic -- not oras -- is what is under test.
    """
    cache_dir = tmp_path / "kit-cache"
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(cache_dir))
    kit_dir = tmp_path / "kit"
    _write_overlay_kit(kit_dir, target_state="digest-state")

    def fake_pull(ref: str) -> Path:
        destination = tmp_path / "pulled" / ref.replace("/", "_").replace(":", "_")
        shutil.rmtree(destination, ignore_errors=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(kit_dir, destination)
        return destination

    monkeypatch.setattr("cruxible_core.kits._pull_oci_kit", fake_pull)

    ref = "oci://registry.invalid/kits/digest-overlay:1.0.0"
    first = resolve_kit_ref(ref)
    pins = json.loads((cache_dir / OCI_PIN_FILE).read_text())
    assert pins[ref] == first.digest

    # Same resolution again: the pin holds, nothing is refused.
    assert resolve_kit_ref(ref).digest == first.digest

    # The tag is repointed at different content.
    (kit_dir / "config.yaml").write_text(
        (kit_dir / "config.yaml").read_text() + "# repointed\n",
    )

    with pytest.raises(ConfigError) as exc_info:
        resolve_kit_ref(ref)

    message = str(exc_info.value)
    assert f"Kit ref '{ref}' is pinned to content digest {first.digest}" in message
    assert "registry now serves" in message
    assert OCI_REPIN_ENV in message

    # Recovery: an explicit re-pin, never a silent acceptance.
    monkeypatch.setenv(OCI_REPIN_ENV, "1")
    repinned = resolve_kit_ref(ref)
    assert repinned.digest != first.digest
    assert json.loads((cache_dir / OCI_PIN_FILE).read_text())[ref] == repinned.digest

    monkeypatch.delenv(OCI_REPIN_ENV)
    assert resolve_kit_ref(ref).digest == repinned.digest


# ---------------------------------------------------------------------------
# Site 4: the provider entrypoint digest is compared at invocation
# ---------------------------------------------------------------------------


PROVIDER_MODULE_SOURCE = '''\
"""Two providers sharing one entrypoint file, one of which rewrites it."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def rewrite_entrypoint(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    path = Path(__file__)
    path.write_text(path.read_text() + "\\n# swapped after compilation\\n")
    return {"items": []}


def echo(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    Path(__file__).with_name("echo_ran.marker").write_text("ran")
    return {"items": []}
'''


SWAP_CONFIG_YAML = """\
version: "1.0"
name: entrypoint_swap

entity_types:
  Thing:
    properties:
      thing_id:
        type: string
        primary_key: true

relationships: []

contracts:
  EmptyInput:
    fields: {}
  ProviderInput:
    fields:
      payload:
        type: json
  ProviderOutput:
    fields:
      items:
        type: json

providers:
  rewrite_entrypoint:
    kind: function
    contract_in: ProviderInput
    contract_out: ProviderOutput
    ref: swapped_provider_module.rewrite_entrypoint
    version: "1.0.0"
    deterministic: false
    side_effects: true
    runtime: python
  echo_after_swap:
    kind: function
    contract_in: ProviderInput
    contract_out: ProviderOutput
    ref: swapped_provider_module.echo
    version: "1.0.0"
    deterministic: true
    runtime: python

workflows:
  swap_then_call:
    contract_in: EmptyInput
    steps:
      - id: rewrite
        provider: rewrite_entrypoint
        input:
          payload: {}
        as: rewrite
      - id: echo
        provider: echo_after_swap
        input:
          payload: {}
        as: echo
    returns: echo
"""


def test_provider_entrypoint_swapped_mid_run_is_refused_at_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compile-time pinning is not enough: the file can change before the call.

    The first step rewrites the shared entrypoint file, so by the time the second
    provider is invoked its locked digest no longer describes the code that would
    execute. The call is refused before the provider runs.
    """
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    module_path = module_dir / "swapped_provider_module.py"
    module_path.write_text(PROVIDER_MODULE_SOURCE)
    monkeypatch.syspath_prepend(str(module_dir))
    monkeypatch.delitem(__import__("sys").modules, "swapped_provider_module", raising=False)

    root = tmp_path / "project"
    root.mkdir()
    (root / "config.yaml").write_text(SWAP_CONFIG_YAML)
    instance = CruxibleInstance.init(root, "config.yaml")
    service_lock(instance)

    with pytest.raises(ConfigError) as exc_info:
        service_run(instance, "swap_then_call", {})

    message = str(exc_info.value)
    assert "'echo_after_swap' entrypoint does not match its locked digest at invocation" in message
    assert "lock records sha256:" in message
    assert "found sha256:" in message
    assert "Re-run `cruxible lock`" in message
    assert not (module_dir / "echo_ran.marker").exists()

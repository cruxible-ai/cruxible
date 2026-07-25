"""Digest-verification hardening: recorded digests are compared, not just recorded.

Each test here covers one site where a digest used to be computed and stored
without ever being checked against the thing it pins. The shared expectation is
that a mismatch is a refusal naming what was expected, what was found, and how a
legitimate operator recovers -- never a warning, never silent acceptance.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from cruxible_core.config.composition_ownership import resolve_composition_for_instance
from cruxible_core.errors import ConfigError
from cruxible_core.kits import (
    OCI_PIN_FILE,
    OCI_REPIN_ENV,
    compute_bundle_digest,
    resolve_kit_ref,
)
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import (
    service_create_state_overlay,
    service_lock,
    service_publish_state,
    service_pull_state_apply,
    service_pull_state_preview,
    service_reload_config,
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


def _downgrade_to_pre_field_bundle(release_dir: Path) -> None:
    """Turn a current bundle into one genuinely published before the sidecar existed.

    Removing ``members.json`` alone is not a pre-field bundle -- it is a current
    bundle with its sidecar stripped, which the manifest still declares and the
    pull therefore refuses. A real old bundle also has no
    ``bundle_format_version``/``members_digest``, so both must go.
    """
    (release_dir / RELEASE_MEMBER_DIGESTS_FILE).unlink()
    manifest_path = release_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload.pop("bundle_format_version", None)
    payload.pop("members_digest", None)
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True))


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


def test_published_manifest_declares_the_member_contract(tmp_path: Path) -> None:
    """The marker lives in the manifest, so stripping the sidecar is detectable.

    ``members_digest`` covers the sidecar body minus its ``manifest.json`` entry
    -- the manifest cannot pin its own final bytes -- so the two files vouch for
    each other without either hashing itself.
    """
    release_dir = _publish(tmp_path)
    manifest = json.loads((release_dir / "manifest.json").read_text())
    sidecar = json.loads((release_dir / RELEASE_MEMBER_DIGESTS_FILE).read_text())

    assert manifest["bundle_format_version"] == 1
    assert manifest["members_digest"].startswith("sha256:")

    without_manifest = {
        name: digest for name, digest in sidecar["digests"].items() if name != "manifest.json"
    }
    recomputed = hashlib.sha256(
        json.dumps(
            {"format_version": 1, "digests": without_manifest},
            indent=2,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert manifest["members_digest"] == f"sha256:{recomputed}"


def test_overlay_refuses_a_bundle_whose_declared_sidecar_was_stripped(tmp_path: Path) -> None:
    """The pre-field story is not a downgrade path.

    Deleting ``members.json`` from a current bundle used to buy the attacker the
    weaker pre-sidecar verification -- config.yaml unchecked, and only a warning
    to show for it. The manifest still declares the format, so the strip is now
    the refusal.
    """
    release_dir = _publish(tmp_path)
    (release_dir / RELEASE_MEMBER_DIGESTS_FILE).unlink()

    with pytest.raises(ConfigError) as exc_info:
        service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            no_kit=True,
            root_dir=tmp_path / "overlay",
        )

    message = str(exc_info.value)
    assert "declares bundle_format_version 1" in message
    assert "was stripped after publication" in message
    assert not (tmp_path / "overlay" / ".cruxible").exists()


def test_overlay_refuses_a_swapped_sidecar_the_manifest_does_not_vouch_for(
    tmp_path: Path,
) -> None:
    """A wholesale sidecar replacement is caught by the manifest's members_digest."""
    release_dir = _publish(tmp_path)
    # Re-pin every member at its CURRENT bytes after editing one of them: the
    # sidecar is now internally consistent, and only the manifest disagrees.
    config_path = release_dir / "config.yaml"
    config_path.write_text(config_path.read_text() + "\n# injected\n")
    sidecar_path = release_dir / RELEASE_MEMBER_DIGESTS_FILE
    sidecar = json.loads(sidecar_path.read_text())
    for member in sidecar["digests"]:
        digest = hashlib.sha256((release_dir / member).read_bytes()).hexdigest()
        sidecar["digests"][member] = f"sha256:{digest}"
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True))

    with pytest.raises(ConfigError) as exc_info:
        service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            no_kit=True,
            root_dir=tmp_path / "overlay",
        )

    message = str(exc_info.value)
    assert "manifest does not vouch for" in message
    assert "members_digest=" in message


def test_overlay_refuses_a_bundle_from_a_newer_cruxible(tmp_path: Path) -> None:
    release_dir = _publish(tmp_path)
    manifest_path = release_dir / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["bundle_format_version"] = 99
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    # Re-pin the manifest so the failure is the version gate, not the digest.
    sidecar_path = release_dir / RELEASE_MEMBER_DIGESTS_FILE
    sidecar = json.loads(sidecar_path.read_text())
    sidecar["digests"]["manifest.json"] = (
        f"sha256:{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}"
    )
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True))

    with pytest.raises(ConfigError, match="declares bundle_format_version 99"):
        service_create_state_overlay(
            transport_ref=f"file://{release_dir}",
            no_kit=True,
            root_dir=tmp_path / "overlay",
        )


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
    _downgrade_to_pre_field_bundle(release_dir)

    overlay_result = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        no_kit=True,
        root_dir=tmp_path / "overlay",
    )
    preview = service_pull_state_preview(overlay_result.instance)

    # The warning reaches the caller who created the overlay, not just the log.
    assert any("predates per-member digests" in warning for warning in overlay_result.warnings)
    assert any("predates per-member digests" in warning for warning in preview.warnings)
    assert any("config.yaml could not be verified" in warning for warning in preview.warnings)
    assert preview.conflicts == []


def test_pre_field_bundle_refuses_a_graph_that_snapshot_json_contradicts(tmp_path: Path) -> None:
    release_dir = _publish(tmp_path)
    _downgrade_to_pre_field_bundle(release_dir)
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


def test_config_reload_refuses_an_out_of_band_edit_to_the_upstream_config(
    tmp_path: Path,
) -> None:
    """The active config is composed BY EXTENDING the materialized upstream.

    An edit there is not a local override -- it rewrites the layer the overlay
    inherits, and a reload would materialize it into the instance's live schema
    as though it had been published. Reload verifies the file it is about to
    compose against, and the recovery is a re-pull, not an edit.
    """
    release_dir = _publish(tmp_path)
    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        no_kit=True,
        root_dir=tmp_path / "overlay",
    ).instance

    upstream_config = overlay.get_root_path() / ".cruxible" / "upstream" / "current" / "config.yaml"
    upstream_config.write_text(
        upstream_config.read_text().replace("Case:", "Case:\n    description: smuggled\n", 1)
    )

    with pytest.raises(ConfigError) as exc_info:
        service_reload_config(overlay)

    message = str(exc_info.value)
    assert "no longer matches its recorded 'config.yaml' digest" in message
    assert "re-pull it" in message

    # Ownership resolution reads the same file and refuses the same way, so a
    # tampered upstream cannot re-assign which types the overlay may write.
    with pytest.raises(ConfigError, match="no longer matches its recorded 'config.yaml' digest"):
        resolve_composition_for_instance(overlay)


def test_a_refused_pull_apply_leaves_the_live_graph_and_active_config_untouched(
    tmp_path: Path,
) -> None:
    """Verify-before-write, proven by what survives the refusal.

    The apply path snapshots, rewrites the composed active config, and merges
    the upstream graph into the live one. If verification ran anywhere after
    those writes, a refused apply would leave a half-pulled instance behind.
    """
    release_dir = _publish(tmp_path)
    overlay = service_create_state_overlay(
        transport_ref=f"file://{release_dir}",
        no_kit=True,
        root_dir=tmp_path / "overlay",
    ).instance
    preview = service_pull_state_preview(overlay)

    root = overlay.get_root_path()
    active_config_before = overlay.get_config_path().read_text()
    graph_before = overlay.load_graph().to_dict()
    upstream_before = json.loads(
        (root / ".cruxible" / "upstream" / "current" / "manifest.json").read_text()
    )

    # Tamper with the published bundle between preview and apply.
    tampered = release_dir / "graph.json"
    tampered.write_text(tampered.read_text() + "\n")

    with pytest.raises(ConfigError) as exc_info:
        service_pull_state_apply(overlay, expected_apply_digest=preview.apply_digest)
    assert "failed digest verification" in str(exc_info.value)

    assert overlay.get_config_path().read_text() == active_config_before
    assert overlay.load_graph().to_dict() == graph_before
    assert (
        json.loads((root / ".cruxible" / "upstream" / "current" / "manifest.json").read_text())
        == upstream_before
    )
    upstream = overlay.get_upstream_metadata()
    assert upstream is not None
    assert upstream.release_id == "r1"


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

    # A bare "yes" is not authorization: the operator names the digest they are
    # accepting, which is what makes the escape hatch self-expiring.
    monkeypatch.setenv(OCI_REPIN_ENV, "1")
    with pytest.raises(ConfigError, match="authorizes a different digest"):
        resolve_kit_ref(ref)

    # Recovery: an explicit re-pin naming the digest, never a silent acceptance.
    repointed_digest = compute_bundle_digest(kit_dir)
    monkeypatch.setenv(OCI_REPIN_ENV, repointed_digest)
    repinned = resolve_kit_ref(ref)
    assert repinned.digest == repointed_digest
    assert repinned.digest != first.digest
    assert json.loads((cache_dir / OCI_PIN_FILE).read_text())[ref] == repinned.digest

    monkeypatch.delenv(OCI_REPIN_ENV)
    assert resolve_kit_ref(ref).digest == repinned.digest


def test_a_lingering_repin_authorization_does_not_wave_through_the_next_repoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest-form escape hatch expires the moment the pin it named is taken.

    A ``CRUXIBLE_OCI_REPIN=1`` left in a shell profile or a CI job definition
    would authorize every future repoint of every ref. Naming the digest means
    the variable stops authorizing anything the moment the tag moves again.
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
    resolve_kit_ref(ref)

    config_path = kit_dir / "config.yaml"
    config_path.write_text(config_path.read_text() + "# repoint one\n")
    first_repoint = compute_bundle_digest(kit_dir)
    monkeypatch.setenv(OCI_REPIN_ENV, first_repoint)
    assert resolve_kit_ref(ref).digest == first_repoint

    # The authorization stays in the environment; the tag moves again.
    config_path.write_text(config_path.read_text() + "# repoint two\n")
    with pytest.raises(ConfigError) as exc_info:
        resolve_kit_ref(ref)
    assert "authorizes a different digest" in str(exc_info.value)
    assert json.loads((cache_dir / OCI_PIN_FILE).read_text())[ref] == first_repoint


def test_poisoned_kit_cache_entry_is_refused_rather_than_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest-keyed cache hit asserts its contents; the assertion is rechecked.

    The cache key is publicly derivable from the kit's contents, so anything
    that can write the cache directory can plant a directory under a known key.
    Reusing it on the strength of the key alone would hand a materialized kit a
    digest describing bytes it no longer holds.
    """
    cache_dir = tmp_path / "kit-cache"
    monkeypatch.setenv("CRUXIBLE_KIT_CACHE_DIR", str(cache_dir))
    kit_dir = tmp_path / "kit"
    _write_overlay_kit(kit_dir, target_state="digest-state")

    bundle = resolve_kit_ref(f"file://{kit_dir}")
    poisoned = bundle.root / "config.yaml"
    poisoned.write_text(poisoned.read_text() + "# planted in the cache\n")

    with pytest.raises(ConfigError) as exc_info:
        resolve_kit_ref(f"file://{kit_dir}")

    message = str(exc_info.value)
    assert "is poisoned" in message
    assert bundle.digest in message
    assert str(bundle.root) in message


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


COMMAND_SWAP_MODULE_SOURCE = '''\
"""A python provider that rewrites the workspace command invoked after it."""

from __future__ import annotations

import os
from typing import Any


def rewrite_command(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    script = os.environ["CRUXIBLE_TEST_COMMAND_PATH"]
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(
            "#!/usr/bin/env python3\\n"
            "import json, os, sys\\n"
            "sys.stdin.read()\\n"
            "open(os.environ['CRUXIBLE_TEST_MARKER_PATH'], 'w').write('ran')\\n"
            "print(json.dumps({'items': []}))\\n"
        )
    os.chmod(script, 0o755)
    return {"items": []}
'''


COMMAND_CONFIG_YAML = """\
version: "1.0"
name: command_swap

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
  rewrite_command:
    kind: function
    contract_in: ProviderInput
    contract_out: ProviderOutput
    ref: command_swap_module.rewrite_command
    version: "1.0.0"
    deterministic: false
    side_effects: true
    runtime: python
  workspace_command:
    kind: tool
    contract_in: ProviderInput
    contract_out: ProviderOutput
    ref: tools/emit.py
    version: "1.0.0"
    runtime: command
    config:
      args: []

workflows:
  call_command:
    contract_in: EmptyInput
    steps:
      - id: call
        provider: workspace_command
        input:
          payload: {}
        as: called
    returns: called
  swap_then_call:
    contract_in: EmptyInput
    steps:
      - id: rewrite
        provider: rewrite_command
        input:
          payload: {}
        as: rewrite
      - id: call
        provider: workspace_command
        input:
          payload: {}
        as: called
    returns: called
"""


def _write_workspace_command(root: Path) -> Path:
    tools = root / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    script = tools / "emit.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'items': []}))\n"
    )
    script.chmod(0o755)
    return script


def test_workspace_command_provider_swapped_mid_run_is_refused_at_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace-relative command is the instance's own code, so it is hashed.

    Nothing about "it is a subprocess rather than an import" makes a script
    shipped with the instance less worth pinning than a python entrypoint. The
    first step rewrites the script, so compile-time verification has already
    passed by the time it changes; the swapped command is refused at invocation
    and the subprocess never starts.
    """
    root = tmp_path / "project"
    root.mkdir(parents=True)
    script = _write_workspace_command(root)
    (root / "config.yaml").write_text(COMMAND_CONFIG_YAML)

    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    (module_dir / "command_swap_module.py").write_text(COMMAND_SWAP_MODULE_SOURCE)
    monkeypatch.syspath_prepend(str(module_dir))
    monkeypatch.delitem(__import__("sys").modules, "command_swap_module", raising=False)
    marker = tmp_path / "command_ran.marker"
    monkeypatch.setenv("CRUXIBLE_TEST_COMMAND_PATH", str(script))
    monkeypatch.setenv("CRUXIBLE_TEST_MARKER_PATH", str(marker))

    instance = CruxibleInstance.init(root, "config.yaml")
    service_lock(instance)

    # Workspace commands are hashed; their path identity is not separately
    # recorded, because the digest already covers the file at that path.
    digest = _locked_provider_digest(instance, "workspace_command")
    assert digest is not None and digest.startswith("sha256:")
    assert _locked_command_path(instance, "workspace_command") is None

    with pytest.raises(ConfigError) as exc_info:
        service_run(instance, "swap_then_call", {})

    message = str(exc_info.value)
    assert (
        "'workspace_command' entrypoint does not match its locked digest at invocation" in message
    )
    assert "Re-run `cruxible lock`" in message
    assert not marker.exists()


def _locked_provider_digest(instance: CruxibleInstance, name: str) -> str | None:
    from cruxible_core.workflow.compiler import load_lock, resolve_lock_path

    return load_lock(resolve_lock_path(instance)).providers[name].provider_entrypoint_digest


def _locked_command_path(instance: CruxibleInstance, name: str) -> str | None:
    from cruxible_core.workflow.compiler import load_lock, resolve_lock_path

    return load_lock(resolve_lock_path(instance)).providers[name].provider_command_path


def test_system_command_provider_records_path_identity_and_refuses_a_repoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """System executables are the OS trust boundary: path pinned, contents not.

    Hashing ``/usr/bin/...`` would invalidate every lock on every OS update, so
    only the resolved path is recorded. That recorded path is still *compared*:
    a ref that now resolves to a different executable -- a PATH entry inserted
    ahead of the locked one -- is refused.
    """
    root = tmp_path / "project"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    root.mkdir(parents=True)
    real = bin_dir / "emit-tool"
    real.write_text(
        "#!/bin/sh\ncat > /dev/null\necho '{\"items\": []}'\n",
    )
    real.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    (root / "config.yaml").write_text(COMMAND_CONFIG_YAML.replace("tools/emit.py", "emit-tool"))

    instance = CruxibleInstance.init(root, "config.yaml")
    service_lock(instance)

    assert _locked_provider_digest(instance, "workspace_command") is None
    assert _locked_command_path(instance, "workspace_command") == str(real)

    # PATH hijack: the same bare ref now resolves to a different file.
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    shadow = shadow_dir / "emit-tool"
    shadow.write_text("#!/bin/sh\ncat > /dev/null\necho '{\"items\": []}'\n")
    shadow.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shadow_dir}:{__import__('os').environ['PATH']}")

    with pytest.raises(ConfigError) as exc_info:
        service_run(instance, "call_command", {})

    message = str(exc_info.value)
    assert "'workspace_command' command ref resolves to" in message
    assert str(shadow) in message
    assert str(real) in message
    assert "Run `cruxible lock`" in message

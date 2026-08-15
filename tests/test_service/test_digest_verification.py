"""Digest verification retained for kit and Procedure provider donors."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.kits import (
    OCI_PIN_FILE,
    OCI_REPIN_ENV,
    compute_bundle_digest,
    resolve_kit_ref,
)
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service import service_lock, service_run
from tests.support.workflow_helpers import write_placeholder_kit_lock


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

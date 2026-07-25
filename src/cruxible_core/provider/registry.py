"""Provider runtime dispatch."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from cruxible_core.config.schema import ProviderSchema
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.kits import (
    is_kit_provider_ref,
    load_kit_provider_module,
    resolve_kit_provider_ref,
)
from cruxible_core.provider.types import ProviderCallable, ProviderContext


def _enforce_execution_policy() -> None:
    # Imported lazily: a module-level import of runtime.execution_policy
    # executes runtime/__init__ (which imports the full instance stack) and
    # closes an import cycle back into this module via workflow.compiler for
    # any entry point that reaches the provider package before runtime.
    from cruxible_core.runtime.execution_policy import enforce_customer_code_execution_supported

    enforce_customer_code_execution_supported()


def resolve_provider(
    provider_name: str,
    provider: ProviderSchema,
    *,
    config_base_path: Path | None = None,
    timeout_ceiling_s: float | None = None,
) -> ProviderCallable:
    """Resolve a provider into an executable callable for its declared runtime.

    ``timeout_ceiling_s`` is an invocation-level ceiling used by procedures.
    The default preserves the configured provider timeout semantics exactly.
    """
    _enforce_execution_policy()
    if provider.runtime == "python":
        return _resolve_python_provider(provider_name, provider, config_base_path=config_base_path)
    if provider.runtime == "http_json":
        return _build_http_json_provider(
            provider_name,
            provider,
            timeout_ceiling_s=timeout_ceiling_s,
        )
    if provider.runtime == "command":
        return _build_command_provider(
            provider_name,
            provider,
            timeout_ceiling_s=timeout_ceiling_s,
        )

    raise ConfigError(
        f"Provider '{provider_name}' uses unsupported runtime '{provider.runtime}'. "
        "Supported runtimes are 'python', 'http_json', and 'command'."
    )


def get_provider_entrypoint_path(
    provider_name: str,
    provider: ProviderSchema,
    *,
    config_base_path: Path | None = None,
) -> Path | None:
    """Locate a python provider's entrypoint file WITHOUT importing it.

    The digest of this file is what the lock pins and what invocation-time
    verification re-checks. Resolving it by importing the module would execute
    the module's top-level code before the digest that is supposed to gate that
    execution has been compared -- the tampered code would run first and the
    refusal would arrive second. ``importlib.util.find_spec`` asks the import
    system where the module *would* come from and stops there.

    ``find_spec`` still imports the ref's parent packages, because that is how
    package ``__path__`` is determined. A provider ref is a dotted path the
    operator chose; the packages containing it are as trusted as the config that
    names them. The leaf module -- the file the pin actually covers -- is never
    executed here.

    HASH SCOPE, stated rather than implied:

    * A plain function provider pins ONE file: the module the ref names. Helper
      modules it imports, and code re-exported into it from elsewhere, are
      outside the pin. A ref pointing at a re-export pins the re-exporting file,
      not the file where the function is defined.
    * A ``kit://`` provider pins the kit's whole declared provider tree (see
      ``compute_kit_provider_sha256``), so helpers inside the kit are covered.

    Kit providers are the ones to reach for when the pin must cover more than a
    single file.
    """
    _enforce_execution_policy()
    if provider.runtime == "command":
        target = resolve_command_provider_target(
            provider_name,
            provider,
            config_base_path=config_base_path,
        )
        return target.workspace_path
    if provider.runtime != "python":
        return None

    ref = provider.ref
    if is_kit_provider_ref(ref):
        if config_base_path is None:
            raise ConfigError(
                f"Provider '{provider_name}' uses kit:// ref '{ref}', but no config base path "
                "was provided for kit resolution"
            )
        module_path, _attr_name, _kit_root = resolve_kit_provider_ref(ref, config_base_path)
        return module_path

    module_name, sep, _attr_name = ref.rpartition(".")
    if not sep:
        raise ConfigError(
            f"Provider '{provider_name}' has invalid ref '{ref}'. Use module.attr import path."
        )
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError) as exc:
        raise ConfigError(
            f"Provider '{provider_name}' could not locate module '{module_name}': {exc}"
        ) from exc
    if spec is None or spec.origin is None or not Path(spec.origin).is_file():
        raise ConfigError(
            f"Provider '{provider_name}' ref '{ref}' does not resolve to a source file "
            f"(module '{module_name}' has no importable file origin). Provider "
            "entrypoints must be real files so their contents can be pinned."
        )
    source_path = Path(spec.origin)
    if source_path.is_symlink():
        raise ConfigError(
            f"Provider '{provider_name}' ref '{ref}' resolves to a symlink at "
            f"{source_path}. A symlinked entrypoint lets the pinned file be repointed "
            "at other code without changing anything the lock records, so it is "
            "refused. Point the provider at a real file."
        )
    return source_path


@dataclass(frozen=True)
class ResolvedCommandTarget:
    """Where a command provider's executable resolved, and what may be pinned about it.

    ``workspace_path`` is set only when the executable lives inside the config's
    own directory tree -- code the operator ships with the instance, which the
    lock hashes exactly like a python entrypoint. ``system_path`` is set when the
    ref resolves to an executable outside that tree.
    """

    ref: str
    declared_workspace_relative: bool
    workspace_path: Path | None
    system_path: Path | None


def resolve_command_provider_target(
    provider_name: str,
    provider: ProviderSchema,
    *,
    config_base_path: Path | None = None,
) -> ResolvedCommandTarget:
    """Resolve a command provider's executable and classify what can be pinned.

    COMMAND PROVIDER PIN POSTURE. A command ref names one of two very different
    things, and pretending otherwise would either under-verify or make locks
    unusable:

    * **Workspace-relative** (``./bin/extract``, ``tools/run.sh``): code shipped
      with the instance. It is hashed into the lock and re-compared immediately
      before every invocation, exactly like a python entrypoint. Swapping the
      script after the lock is refused.
    * **Absolute or PATH-resolved system executable** (``/usr/bin/jq``, ``curl``):
      the operating system's, not the instance's. Only its resolved path
      identity is recorded, and a ref that later resolves somewhere else is
      refused. Its CONTENTS are deliberately NOT hashed: every OS package
      update, security patch, and interpreter upgrade would otherwise invalidate
      every lock that mentions the binary, and operators would learn to
      regenerate locks reflexively -- which is exactly how a pin stops meaning
      anything. **System executables are the OS trust boundary.** Cruxible pins
      which file the ref resolves to; keeping that file trustworthy is the
      platform's job. A command whose contents must be pinned belongs inside the
      workspace or inside a kit.
    """
    ref = provider.ref.strip()
    if not ref:
        raise ConfigError(f"Provider '{provider_name}' command ref must not be empty")

    base = config_base_path.resolve() if config_base_path is not None else None
    candidate: Path | None
    if os.sep in ref or (os.altsep is not None and os.altsep in ref):
        candidate = Path(ref)
        if not candidate.is_absolute():
            if base is None:
                raise ConfigError(
                    f"Provider '{provider_name}' command ref '{ref}' is a relative path, "
                    "but no config base path was provided to resolve it against."
                )
            candidate = base / candidate
    else:
        found = shutil.which(ref)
        candidate = Path(found) if found is not None else None

    if candidate is None:
        return ResolvedCommandTarget(
            ref=ref,
            declared_workspace_relative=False,
            workspace_path=None,
            system_path=None,
        )

    declared_inside = base is not None and base in candidate.parents
    if not declared_inside:
        return ResolvedCommandTarget(
            ref=ref,
            declared_workspace_relative=False,
            workspace_path=None,
            system_path=candidate.resolve() if candidate.exists() else None,
        )

    if not candidate.is_file():
        return ResolvedCommandTarget(
            ref=ref,
            declared_workspace_relative=True,
            workspace_path=None,
            system_path=None,
        )
    if candidate.is_symlink():
        raise ConfigError(
            f"Provider '{provider_name}' command ref '{ref}' resolves to a symlink at "
            f"{candidate}. A symlinked command lets the pinned executable be repointed "
            "without changing anything the lock records, so it is refused. Point the "
            "provider at a real file."
        )
    resolved = candidate.resolve()
    if base not in resolved.parents:
        raise ConfigError(
            f"Provider '{provider_name}' command ref '{ref}' resolves outside the "
            f"instance directory tree, to {resolved}. A workspace-relative command must "
            "stay inside the workspace so its contents can be pinned; use an absolute "
            "system path if the OS-provided executable is what you mean."
        )
    return ResolvedCommandTarget(
        ref=ref,
        declared_workspace_relative=True,
        workspace_path=resolved,
        system_path=None,
    )


def _resolve_python_provider(
    provider_name: str,
    provider: ProviderSchema,
    *,
    config_base_path: Path | None,
) -> ProviderCallable:
    candidate = _resolve_python_candidate(
        provider_name,
        provider,
        config_base_path=config_base_path,
    )
    if not callable(candidate):
        raise ConfigError(f"Provider '{provider_name}' ref '{provider.ref}' is not callable")
    return cast(ProviderCallable, candidate)


def _resolve_python_candidate(
    provider_name: str,
    provider: ProviderSchema,
    *,
    config_base_path: Path | None,
) -> object:
    ref = provider.ref
    if is_kit_provider_ref(ref):
        if config_base_path is None:
            raise ConfigError(
                f"Provider '{provider_name}' uses kit:// ref '{ref}', but no config base path "
                "was provided for kit resolution"
            )
        module_path, attr_name, kit_root = resolve_kit_provider_ref(ref, config_base_path)
        module = load_kit_provider_module(module_path, kit_root)
        try:
            return getattr(module, attr_name)
        except AttributeError as exc:
            raise ConfigError(
                f"Provider '{provider_name}' ref '{ref}' does not resolve to an attribute"
            ) from exc

    module_name, sep, attr_name = ref.rpartition(".")
    if not sep:
        raise ConfigError(
            f"Provider '{provider_name}' has invalid ref '{ref}'. Use module.attr import path."
        )

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - exercised in tests
        raise ConfigError(
            f"Provider '{provider_name}' could not import module '{module_name}': {exc}"
        ) from exc

    try:
        candidate = getattr(module, attr_name)
    except AttributeError as exc:
        raise ConfigError(
            f"Provider '{provider_name}' ref '{ref}' does not resolve to an attribute"
        ) from exc
    return candidate


def _build_http_json_provider(
    provider_name: str,
    provider: ProviderSchema,
    *,
    timeout_ceiling_s: float | None,
) -> ProviderCallable:
    parsed = urlparse(provider.ref)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(
            f"Provider '{provider_name}' has invalid http_json ref '{provider.ref}'. "
            "Use a full http(s) URL."
        )

    headers = provider.config.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise ConfigError(f"Provider '{provider_name}' config.headers must be a string map")

    configured_timeout_s = _coerce_timeout(provider_name, provider.config.get("timeout_s", 30))
    timeout_s = _effective_timeout(
        provider_name,
        configured_timeout_s,
        timeout_ceiling_s,
    )

    def _execute(input_payload: dict[str, Any], _context: ProviderContext) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=timeout_s) as client:
                response = client.post(provider.ref, json=input_payload, headers=headers)
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise QueryExecutionError(
                f"Provider '{provider_name}' http_json request timed out after {timeout_s}s"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise QueryExecutionError(
                f"Provider '{provider_name}' http_json request failed with status "
                f"{exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise QueryExecutionError(
                f"Provider '{provider_name}' http_json request failed: {exc}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise QueryExecutionError(
                f"Provider '{provider_name}' http_json response was not valid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise QueryExecutionError(
                f"Provider '{provider_name}' http_json response must be a JSON object"
            )
        return cast(dict[str, Any], payload)

    return cast(ProviderCallable, _execute)


def _build_command_provider(
    provider_name: str,
    provider: ProviderSchema,
    *,
    timeout_ceiling_s: float | None,
) -> ProviderCallable:
    if not provider.ref.strip():
        raise ConfigError(f"Provider '{provider_name}' command ref must not be empty")

    args = provider.config.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ConfigError(f"Provider '{provider_name}' config.args must be a list of strings")

    extra_env = provider.config.get("env", {})
    if not isinstance(extra_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extra_env.items()
    ):
        raise ConfigError(f"Provider '{provider_name}' config.env must be a string map")

    configured_timeout_s = _coerce_timeout(provider_name, provider.config.get("timeout_s", 30))
    timeout_s = _effective_timeout(
        provider_name,
        configured_timeout_s,
        timeout_ceiling_s,
    )
    command = [provider.ref, *args]

    def _execute(input_payload: dict[str, Any], _context: ProviderContext) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(input_payload),
                text=True,
                capture_output=True,
                timeout=timeout_s,
                env={**os.environ, **extra_env},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise QueryExecutionError(
                f"Provider '{provider_name}' command timed out after {timeout_s}s"
            ) from exc
        except OSError as exc:
            raise QueryExecutionError(
                f"Provider '{provider_name}' command failed to start: {exc}"
            ) from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            detail = f": {stderr}" if stderr else ""
            raise QueryExecutionError(
                f"Provider '{provider_name}' command exited with status "
                f"{completed.returncode}{detail}"
            )

        try:
            payload = json.loads(completed.stdout)
        except ValueError as exc:
            raise QueryExecutionError(
                f"Provider '{provider_name}' command output was not valid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise QueryExecutionError(
                f"Provider '{provider_name}' command output must be a JSON object"
            )
        return cast(dict[str, Any], payload)

    return cast(ProviderCallable, _execute)


def _coerce_timeout(provider_name: str, value: object) -> float:
    if not isinstance(value, str | int | float):
        raise ConfigError(f"Provider '{provider_name}' timeout_s must be numeric")
    try:
        timeout_s = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Provider '{provider_name}' timeout_s must be numeric") from exc
    if timeout_s <= 0:
        raise ConfigError(f"Provider '{provider_name}' timeout_s must be greater than zero")
    return timeout_s


def _effective_timeout(
    provider_name: str,
    configured_timeout_s: float,
    timeout_ceiling_s: float | None,
) -> float:
    if timeout_ceiling_s is None:
        return configured_timeout_s
    ceiling = _coerce_timeout(provider_name, timeout_ceiling_s)
    return min(configured_timeout_s, ceiling)

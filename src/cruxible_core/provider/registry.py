"""Provider runtime dispatch."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, cast
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
) -> ProviderCallable:
    """Resolve a provider into an executable callable for its declared runtime."""
    _enforce_execution_policy()
    if provider.runtime == "python":
        return _resolve_python_provider(provider_name, provider, config_base_path=config_base_path)
    if provider.runtime == "http_json":
        return _build_http_json_provider(provider_name, provider)
    if provider.runtime == "command":
        return _build_command_provider(provider_name, provider)

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
    """Resolve the provider entrypoint file path when available."""
    _enforce_execution_policy()
    if provider.runtime != "python":
        return None

    candidate = _resolve_python_candidate(
        provider_name,
        provider,
        config_base_path=config_base_path,
    )
    if not callable(candidate):
        raise ConfigError(f"Provider '{provider_name}' ref '{provider.ref}' is not callable")
    callable_candidate = cast(Callable[..., Any], candidate)
    source_path = inspect.getsourcefile(callable_candidate) or inspect.getfile(callable_candidate)
    if source_path is None:
        raise ConfigError(
            f"Provider '{provider_name}' ref '{provider.ref}' does not resolve to a source file"
        )
    return Path(source_path)


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


def _build_http_json_provider(provider_name: str, provider: ProviderSchema) -> ProviderCallable:
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

    timeout_s = _coerce_timeout(provider_name, provider.config.get("timeout_s", 30))

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


def _build_command_provider(provider_name: str, provider: ProviderSchema) -> ProviderCallable:
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

    timeout_s = _coerce_timeout(provider_name, provider.config.get("timeout_s", 30))
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

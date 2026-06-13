"""Hosted execution policy gates for customer-supplied code."""

from __future__ import annotations

import os
from collections.abc import Mapping

from cruxible_core.errors import CustomerCodeExecutionUnsupportedError

SHARED_HOSTED_SERVER_PROFILE = "shared"
CUSTOMER_CODE_EXECUTION_UNSUPPORTED = "customer_code_execution_unsupported"
SUPPORTED_ISOLATED_EXECUTION_BACKENDS = frozenset({"docker"})


def is_shared_hosted_profile(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this process is running the shared hosted server profile."""
    env = environ or os.environ
    return env.get("CRUXIBLE_HOSTED_SERVER_PROFILE", "").strip().lower() == (
        SHARED_HOSTED_SERVER_PROFILE
    )


def isolated_execution_backend(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured isolated execution backend, normalized for comparison."""
    env = environ or os.environ
    backend = env.get("CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND", "").strip().lower()
    return backend or None


def customer_code_execution_supported(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether provider execution is allowed in the current hosted profile."""
    if not is_shared_hosted_profile(environ):
        return True
    backend = isolated_execution_backend(environ)
    return backend in SUPPORTED_ISOLATED_EXECUTION_BACKENDS


def enforce_customer_code_execution_supported(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Raise a public-safe error when shared hosted runtimes cannot execute providers."""
    if customer_code_execution_supported(environ):
        return
    raise CustomerCodeExecutionUnsupportedError()

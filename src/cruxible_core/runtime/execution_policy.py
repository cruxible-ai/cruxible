"""Hosted execution policy gates for customer-supplied code.

Two rulings shape this module (maintainer ruling 2026-09-03):

* The shared profile permits execution only when an isolated executor is
  actually REGISTERED in this build. Naming a backend in the environment is a
  claim, not a mechanism: no container executor exists in this tree, so a
  ``docker`` value used to re-enable spawning the Provider *directly on the
  host* -- the opposite of what its name promised. The registry below is empty,
  so the shared profile always refuses, typed, and it begins permitting
  execution on the day an executor is registered here.
* An unrecognised, non-empty ``CRUXIBLE_HOSTED_SERVER_PROFILE`` refuses typed
  instead of being read as "not shared". A profile this build cannot read is a
  profile whose execution policy it cannot establish, and failing open on a
  misspelling is the wrong direction for a multi-tenant guard.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from cruxible_client.contracts import IsolatedExecutorRegistrationV1
from cruxible_core.errors import (
    CustomerCodeExecutionUnsupportedError,
    HostedProfileUnknownError,
)

SHARED_HOSTED_SERVER_PROFILE = "shared"
CUSTOMER_CODE_EXECUTION_UNSUPPORTED = "customer_code_execution_unsupported"
HOSTED_PROFILE_UNKNOWN = "hosted_profile_unknown"

#: Hosted server profiles this build declares. Unset (or empty) is the ordinary
#: single-tenant runtime and is deliberately not a member: it is the ABSENCE of
#: a hosted profile, not a profile of its own.
KNOWN_HOSTED_SERVER_PROFILES = frozenset({SHARED_HOSTED_SERVER_PROFILE})

ISOLATION_BACKEND_NOT_IMPLEMENTED = "isolation backend not implemented"


@runtime_checkable
class IsolatedExecutor(Protocol):
    """An executor that can run Provider code away from the daemon's host."""

    def registration(self) -> IsolatedExecutorRegistrationV1:
        """Return the pinned record this executor is selected and audited by."""


#: Isolated executors registered in THIS process. Empty in core on purpose:
#: there is no container executor in this repository, so no environment value
#: can make execution under the shared profile isolated. An out-of-tree
#: executor registers itself here at import time, which is what turns
#: `CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND` from a claim into a selector.
_REGISTERED_ISOLATED_EXECUTORS: dict[str, IsolatedExecutorRegistrationV1] = {}


def register_isolated_executor(executor: IsolatedExecutor) -> IsolatedExecutorRegistrationV1:
    """Register one isolated executor under its own backend id.

    The record, not the environment, is the evidence: it names the backend id
    the environment selects and the exact implementation digest that is doing
    the isolating. Registering a second executor under one backend id is
    refused rather than silently replacing the first.
    """

    registration = executor.registration()
    existing = _REGISTERED_ISOLATED_EXECUTORS.get(registration.backend_id)
    if existing is not None and existing != registration:
        raise ValueError(
            f"isolated executor backend {registration.backend_id!r} is already registered "
            f"with implementation {existing.implementation_digest}"
        )
    _REGISTERED_ISOLATED_EXECUTORS[registration.backend_id] = registration
    return registration


def registered_isolated_executors() -> Mapping[str, IsolatedExecutorRegistrationV1]:
    """Return every isolated executor registered in this process, by backend id."""

    return dict(_REGISTERED_ISOLATED_EXECUTORS)


def hosted_server_profile(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured hosted server profile, normalized, or None."""
    env = environ or os.environ
    profile = env.get("CRUXIBLE_HOSTED_SERVER_PROFILE", "").strip().lower()
    return profile or None


def is_shared_hosted_profile(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this process is running the shared hosted server profile."""
    return hosted_server_profile(environ) == SHARED_HOSTED_SERVER_PROFILE


def isolated_execution_backend(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured isolated execution backend, normalized for comparison."""
    env = environ or os.environ
    backend = env.get("CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND", "").strip().lower()
    return backend or None


def isolated_execution_available(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether a REGISTERED isolated executor is selected and available."""
    backend = isolated_execution_backend(environ)
    return backend is not None and backend in registered_isolated_executors()


def _unsupported_detail(environ: Mapping[str, str] | None = None) -> str:
    backend = isolated_execution_backend(environ)
    if backend is None:
        return ISOLATION_BACKEND_NOT_IMPLEMENTED
    return f"{ISOLATION_BACKEND_NOT_IMPLEMENTED}: backend {backend!r} is not registered"


def customer_code_execution_supported(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether Provider execution is allowed in the current hosted profile.

    Fails CLOSED: an unknown profile answers False here and refuses typed in
    ``enforce_customer_code_execution_supported``.
    """
    profile = hosted_server_profile(environ)
    if profile is None:
        return True
    if profile not in KNOWN_HOSTED_SERVER_PROFILES:
        return False
    if profile == SHARED_HOSTED_SERVER_PROFILE:
        return isolated_execution_available(environ)
    return True


def enforce_customer_code_execution_supported(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Raise a public-safe error when this runtime may not execute Provider code."""
    profile = hosted_server_profile(environ)
    if profile is None:
        return
    if profile not in KNOWN_HOSTED_SERVER_PROFILES:
        raise HostedProfileUnknownError(profile)
    if profile == SHARED_HOSTED_SERVER_PROFILE and not isolated_execution_available(environ):
        raise CustomerCodeExecutionUnsupportedError(_unsupported_detail(environ))

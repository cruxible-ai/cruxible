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
from importlib.metadata import EntryPoint, entry_points
from typing import Protocol, runtime_checkable

from cruxible_client.contracts import IsolatedExecutorRegistrationV1
from cruxible_core.errors import (
    CustomerCodeExecutionUnsupportedError,
    HostedProfileUnknownError,
    IsolatedExecutorDiscoveryError,
)

SHARED_HOSTED_SERVER_PROFILE = "shared"
CUSTOMER_CODE_EXECUTION_UNSUPPORTED = "customer_code_execution_unsupported"
HOSTED_PROFILE_UNKNOWN = "hosted_profile_unknown"

#: Hosted server profiles this build declares. Unset (or empty) is the ordinary
#: single-tenant runtime and is deliberately not a member: it is the ABSENCE of
#: a hosted profile, not a profile of its own.
KNOWN_HOSTED_SERVER_PROFILES = frozenset({SHARED_HOSTED_SERVER_PROFILE})

ISOLATION_BACKEND_NOT_IMPLEMENTED = "isolation backend not implemented"

#: The entry-point group an out-of-tree executor distribution advertises itself
#: under. Pinned by the Cloud control plane, which ships its executor in the
#: tenant image: the daemon iterates this group once at start, and a
#: distribution that is installed but does not advertise here is not registered,
#: because a backend nothing declares is a backend nothing audited.
ISOLATED_EXECUTOR_ENTRY_POINT_GROUP = "cruxible.isolated_executors"


@runtime_checkable
class IsolatedExecutor(Protocol):
    """An executor that can run Provider code away from the daemon's host."""

    def registration(self) -> IsolatedExecutorRegistrationV1:
        """Return the pinned record this executor is selected and audited by."""


#: Isolated executors registered in THIS process. Empty in core on purpose:
#: there is no container executor in this repository, so no environment value
#: can make execution under the shared profile isolated. An out-of-tree executor
#: reaches this registry by advertising itself in the
#: `cruxible.isolated_executors` entry-point group, which the daemon iterates
#: once at start (`discover_isolated_executors`); an embedded host may also call
#: `register_isolated_executor` directly. Either way it is the registration, not
#: the environment, that turns `CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND` from
#: a claim into a selector -- and being installed on the daemon's `sys.path` is
#: the WHOLE trust boundary: there is no provenance check, allow-list or digest
#: pin on what a discovered distribution supplies.
_REGISTERED_ISOLATED_EXECUTORS: dict[str, IsolatedExecutorRegistrationV1] = {}


def _refuse_backend_collision(
    registration: IsolatedExecutorRegistrationV1,
    *,
    against: Mapping[str, IsolatedExecutorRegistrationV1],
) -> None:
    """One collision law, for the in-process seam and for discovery alike."""

    existing = against.get(registration.backend_id)
    if existing is not None and existing != registration:
        raise ValueError(
            f"isolated executor backend {registration.backend_id!r} is already registered "
            f"with implementation {existing.implementation_digest}"
        )


def register_isolated_executor(executor: IsolatedExecutor) -> IsolatedExecutorRegistrationV1:
    """Register one isolated executor under its own backend id.

    The record, not the environment, is the evidence: it names the backend id
    the environment selects and the exact implementation digest that is doing
    the isolating. Registering a second executor under one backend id is
    refused rather than silently replacing the first.
    """

    registration = executor.registration()
    _refuse_backend_collision(registration, against=_REGISTERED_ISOLATED_EXECUTORS)
    _REGISTERED_ISOLATED_EXECUTORS[registration.backend_id] = registration
    return registration


def registered_isolated_executors() -> Mapping[str, IsolatedExecutorRegistrationV1]:
    """Return every isolated executor registered in this process, by backend id."""

    return dict(_REGISTERED_ISOLATED_EXECUTORS)


def discover_isolated_executors(
    *,
    group: str = ISOLATED_EXECUTOR_ENTRY_POINT_GROUP,
) -> tuple[IsolatedExecutorRegistrationV1, ...]:
    """Register every isolated executor the installed distributions advertise.

    Called once at daemon start. Every entry point in the group is loaded and
    its registration read BEFORE any of them reaches the process registry, so
    the registry that decides the shared profile's execution policy is built
    from what is INSTALLED rather than from what an environment variable claims
    -- and is built whole or not at all. An object that is not an
    ``IsolatedExecutor``, one whose registration is malformed, and one that
    collides with a backend id already registered or staged are all the same
    failure: the daemon refuses to start, typed, naming the entry point, and
    NOTHING in this group is registered, including the executors whose entry
    points loaded before it. A partly-registered daemon -- one backend live
    because it sorted first, its sibling missing because the distribution is
    broken -- is exactly the state failing closed exists to prevent.
    """

    staged: dict[str, IsolatedExecutorRegistrationV1] = {}
    ordered: list[IsolatedExecutorRegistrationV1] = []
    for entry_point in sorted(
        entry_points(group=group),
        key=lambda item: item.name.encode("utf-8"),
    ):
        registration = _staged_registration(entry_point, group=group, staged=staged)
        staged[registration.backend_id] = registration
        ordered.append(registration)
    # Commit. Every refusal above happened before this line, so the registry
    # never holds a prefix of a discovery that failed.
    _REGISTERED_ISOLATED_EXECUTORS.update(staged)
    return tuple(ordered)


def _staged_registration(
    entry_point: EntryPoint,
    *,
    group: str,
    staged: Mapping[str, IsolatedExecutorRegistrationV1],
) -> IsolatedExecutorRegistrationV1:
    """Load one advertised executor and read its registration, or refuse typed."""

    def _refusal(detail: str) -> IsolatedExecutorDiscoveryError:
        distribution = entry_point.dist
        return IsolatedExecutorDiscoveryError(
            name=entry_point.name,
            entry_point=entry_point.value,
            distribution=None if distribution is None else distribution.name,
            group=group,
            detail=detail,
        )

    try:
        loaded = entry_point.load()
        # An entry point may advertise the executor itself or the class that
        # builds it; a class is constructed with no arguments, because every
        # input an executor needs is its own package's business, never the
        # daemon's.
        executor = loaded() if isinstance(loaded, type) else loaded
    except Exception as exc:  # noqa: BLE001 - any load failure is one refusal
        raise _refusal(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(executor, IsolatedExecutor):
        raise _refusal(
            f"{type(executor).__name__} does not implement the IsolatedExecutor protocol"
        )
    try:
        registration = executor.registration()
        _refuse_backend_collision(
            registration,
            against={**_REGISTERED_ISOLATED_EXECUTORS, **staged},
        )
    except Exception as exc:  # noqa: BLE001 - registration refusals are one refusal
        raise _refusal(f"{type(exc).__name__}: {exc}") from exc
    return registration


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


def provider_lane_applicable(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the Provider lane is part of this deployment's surface at all.

    Distinct from "the lane is healthy". A shared hosted profile with no
    registered isolated executor cannot run Provider code and will not until one
    is registered here -- so reporting that lane as available, and then refusing
    every run, is a health field saying the opposite of what it means.

    A profile this build cannot read stays applicable: that is a
    misconfiguration to be refused typed at the spawn, not an absence to be
    reported as normal.
    """

    profile = hosted_server_profile(environ)
    if profile is None or profile not in KNOWN_HOSTED_SERVER_PROFILES:
        return True
    return customer_code_execution_supported(environ)


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

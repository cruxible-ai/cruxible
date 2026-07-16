"""Deployment policy for the implicit base kit — no materialization machinery.

Lives outside ``cruxible_core.kits`` so surface layers (runtime API, CLI) can
resolve the deployment default without importing kit resolution or
materialization code.
"""

from __future__ import annotations

import os
from typing import Mapping

DEFAULT_BASE_KIT_ENV = "CRUXIBLE_DEFAULT_BASE_KIT"
DEFAULT_BASE_KIT = "agent-operation"


def get_default_base_kit(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the deployment's opt-out default base kit reference."""
    env = os.environ if environ is None else environ
    configured = env.get(DEFAULT_BASE_KIT_ENV)
    if configured is None:
        return DEFAULT_BASE_KIT
    normalized = configured.strip()
    if not normalized or normalized.lower() in {"none", "off", "false"}:
        return None
    return normalized

"""Provider execution types."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from cruxible_core.primitives import new_id

ProviderRuntime = Literal["python", "http_json", "command"]
"""How the provider callable is invoked.

- ``python``: imported Python callable.
- ``http_json``: HTTP endpoint with JSON request/response bodies.
- ``command``: subprocess invocation.
"""


class ResolvedArtifact(BaseModel):
    """Resolved artifact metadata passed to provider executions."""

    name: str
    kind: str
    uri: str
    local_path: str | None = None
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderContext(BaseModel):
    """Read-only execution metadata exposed to provider callables."""

    workflow_name: str
    step_id: str
    provider_name: str
    provider_version: str
    provider_config: dict[str, Any] = Field(default_factory=dict)
    deterministic: bool = True
    artifact: ResolvedArtifact | None = None


class ExecutionTrace(BaseModel):
    """Persisted trace proving that a provider execution ran.

    ``started_at`` and ``finished_at`` are required; callers must capture
    the real wall-clock start and end of execution rather than relying on
    construction-time defaults.
    """

    trace_id: str = Field(default_factory=lambda: new_id("TRC"))
    workflow_name: str
    step_id: str
    provider_name: str
    provider_version: str
    provider_ref: str
    provider_entrypoint_sha256: str | None = None
    runtime: ProviderRuntime
    deterministic: bool
    side_effects: bool
    artifact_name: str | None = None
    artifact_sha256: str | None = None
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output_payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "error"] = "success"
    error: str | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: float


class ProviderCallable(Protocol):
    """Callable provider implementation resolved from a provider ref."""

    def __call__(self, input_payload: dict[str, Any], context: ProviderContext) -> dict[str, Any]:
        """Execute a provider call and return a JSON-serializable payload."""

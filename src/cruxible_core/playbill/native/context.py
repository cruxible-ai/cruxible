"""The explicit context every render law holds over (§11.9.6).

`RenderContextV1` exists so that "deterministic" is a checkable statement rather
than an aspiration. A render is a pure function of accepted state and this
record, and this record carries every input that could otherwise have been
sampled from the environment:

* the **accepted generation** the render is a checkout of;
* the **evaluation time** governance facts are qualified by -- supplied, never
  read from a clock, so the same state and the same context always produce the
  same bytes;
* the **scope/query digest** the render is a subscription to (single-repo
  whole-scope in this slice; §11.9.7 defers subscription slicing);
* the **access profile** the projection was computed under, so a committed
  render can never be more permissive than the repository's readership;
* the **lens and renderer digest**, so a spelling change is visible in the
  manifest rather than silently reshaping a committed tree.

Nothing in this package calls `datetime.now`. That is the whole of the
"render never samples wall clock" law, and it is enforceable by reading the
imports of this package rather than by trusting the renderer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.native.grammar import NativeLensV1, default_native_lens
from cruxible_core.playbill.projection import AcceptedCoordinate

SCOPE_DIGEST_DOMAIN: Final = "playbill-native-render-scope-v1"

NATIVE_WHOLE_SCOPE: Final = "single_repo_whole_scope"
"""The dogfood-minimum scope. A committed render is defined by an accepted named
QueryDefinition once slicing lands; until then the subscription is "everything
this instance accepted," stated explicitly rather than left implicit."""


def native_scope_digest(
    *,
    instance_id: str,
    scope: str = NATIVE_WHOLE_SCOPE,
    query_name: str | None = None,
) -> str:
    """Digest the scope a render is a subscription to."""

    return typed_digest(
        Sha256Value,
        SCOPE_DIGEST_DOMAIN,
        {"instance_id": instance_id, "query_name": query_name, "scope": scope},
    ).tagged


class RenderContextV1(BaseModel):
    """Everything a render depends on that is not accepted state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-native-render-context-v1"] = "playbill-native-render-context-v1"
    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    at: AcceptedCoordinate
    evaluation_time: datetime
    scope: str = NATIVE_WHOLE_SCOPE
    scope_query_name: str | None = None
    scope_digest: str
    access_profile: CoverageAccessProfileV1
    lens: NativeLensV1

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("a render evaluation time must be an absolute instant")
        return value

    @model_validator(mode="after")
    def _context_law(self) -> "RenderContextV1":
        Sha256Value.from_tagged(self.scope_digest)
        expected = native_scope_digest(
            instance_id=self.instance_id,
            scope=self.scope,
            query_name=self.scope_query_name,
        )
        if expected != self.scope_digest:
            raise ValueError("a render scope digest must reproduce from the declared scope")
        return self

    @property
    def evaluation_time_text(self) -> str:
        """The one spelling of the read time every rendered file and the manifest use."""

        return self.evaluation_time.isoformat()


def whole_scope_context(
    *,
    instance_id: str,
    at: AcceptedCoordinate,
    evaluation_time: datetime,
    access_profile: CoverageAccessProfileV1,
    lens: NativeLensV1 | None = None,
) -> RenderContextV1:
    """Build the dogfood-minimum context: this instance, whole scope, one instant."""

    return RenderContextV1(
        instance_id=instance_id,
        at=at,
        evaluation_time=evaluation_time,
        scope_digest=native_scope_digest(instance_id=instance_id),
        access_profile=access_profile,
        lens=lens or default_native_lens(),
    )


__all__ = [
    "NATIVE_WHOLE_SCOPE",
    "SCOPE_DIGEST_DOMAIN",
    "RenderContextV1",
    "native_scope_digest",
    "whole_scope_context",
]

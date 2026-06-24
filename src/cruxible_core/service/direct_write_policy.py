"""Resolve the effective per-type direct-write policy.

``refuse_direct_writes`` adds the governance axis the cumulative ``CRUXIBLE_MODE``
tier ladder cannot express: "this domain is proposal-only." A type marked
``proposal_only`` refuses bare direct graph-write verbs (``add_entity`` /
``add_relationship`` / ``batch_direct_write`` / the typed lifecycle write) and
forces state in through the proposal/workflow path. It is a HARD constraint,
independent of permission tier — even ``CRUXIBLE_MODE=admin`` is refused.

This resolver lives in ``service/`` (not on ``CoreConfig``) deliberately: it
reads process env, and ``CoreConfig`` must stay env-agnostic and snapshot-stable.
Env is read per-call (no process-global cache) so ``monkeypatch.setenv`` works in
tests and a daemon picks up a flipped kill-switch without a restart.

Effective precedence (union — any path to ``proposal_only`` wins):

    proposal_only  if  env kill-switch set (HARD, wins over everything)
                   OR  the type's explicit ``write_policy == "proposal_only"``
                   OR  (type ``write_policy is None``
                        AND ``runtime.default_write_policy == "proposal_only"``)

An explicit per-type ``"direct"`` opts out of the instance default, but NOT the
env kill-switch.

The chokepoint (``graph/operations.py``) only enforces this for writes whose
``source`` is NOT a governed verb. Governed verbs funnel state in through the
audited proposal/workflow machinery and are always permitted; add new governed
verbs to ``_GOVERNED_SOURCES`` below.
"""

from __future__ import annotations

import os
from typing import Literal, Mapping

from cruxible_core.config.schema import CoreConfig

WritePolicy = Literal["direct", "proposal_only"]

# Sources that funnel state in through governed, audited machinery. A write
# carrying one of these is always permitted regardless of write_policy. Keep this
# an ALLOWLIST (not a denylist of direct verbs): every write funnels through the
# chokepoint, so an allowlist means a future direct verb cannot silently bypass
# governance — it is refused until it is deliberately added here.
#   - "workflow_apply": canonical workflow apply_entities / apply_relationships
#   - "group_resolve":  proposal group resolution (group propose -> resolve)
# Add any future governed verb here, with a comment naming it.
_GOVERNED_SOURCES: frozenset[str] = frozenset({"workflow_apply", "group_resolve"})

# Env kill-switch: daemon-wide override forcing proposal_only for the direct-write
# verbs at the chokepoint (overrides per-type opt-outs + the default). Scope: governs
# the write chokepoint only -- the feedback review/promotion channel is separate.
_ENV_REFUSE_DIRECT_WRITES = "CRUXIBLE_REFUSE_DIRECT_WRITES"


def _is_truthy(value: str | None) -> bool:
    # Mirror server/config.py:_is_truthy exactly so the kill-switch reads the
    # same truthy spellings as the rest of the daemon's env toggles.
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_governed_source(source: str) -> bool:
    """Return whether ``source`` is a governed (always-permitted) write verb."""
    return source in _GOVERNED_SOURCES


def env_refuses_direct_writes(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the daemon-wide kill-switch env var is set.

    Read per-call (no caching) so ``monkeypatch.setenv`` is observed in tests and
    a live daemon honors a flipped flag.
    """
    env = environ if environ is not None else os.environ
    return _is_truthy(env.get(_ENV_REFUSE_DIRECT_WRITES))


def _resolve(
    explicit: WritePolicy | None,
    default: WritePolicy,
    *,
    environ: Mapping[str, str] | None,
) -> WritePolicy:
    if env_refuses_direct_writes(environ):
        # HARD kill-switch wins over every per-type opt-out and the default.
        return "proposal_only"
    if explicit is not None:
        # An explicit per-type "direct" opts out of the instance default.
        return explicit
    return default


def effective_entity_write_policy(
    config: CoreConfig,
    entity_type: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> WritePolicy:
    """Resolve the effective write policy for an entity type."""
    schema = config.entity_types.get(entity_type)
    explicit = schema.write_policy if schema is not None else None
    return _resolve(
        explicit,
        config.runtime.default_write_policy,
        environ=environ,
    )


def effective_relationship_write_policy(
    config: CoreConfig,
    relationship_type: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> WritePolicy:
    """Resolve the effective write policy for a relationship type."""
    schema = config.get_relationship(relationship_type)
    explicit = schema.write_policy if schema is not None else None
    return _resolve(
        explicit,
        config.runtime.default_write_policy,
        environ=environ,
    )

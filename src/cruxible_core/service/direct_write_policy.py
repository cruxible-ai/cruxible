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

Effective precedence:

    mint_only      if  the type's explicit ``write_policy == "mint_only"``
                   (ABSOLUTE — wins over everything, including the env
                    kill-switch; an auth-managed type stays writable ONLY by the
                    ``token_mint`` source and must not be downgraded to the
                    weaker ``proposal_only`` by the kill-switch)
    proposal_only  elif env kill-switch set (HARD, wins over the per-type
                        opt-outs and the default below)
                   OR  the type's explicit ``write_policy == "proposal_only"``
                   OR  (type ``write_policy is None``
                        AND ``runtime.default_write_policy == "proposal_only"``)

An explicit per-type ``"direct"`` opts out of the instance default, but NOT the
env kill-switch.

The chokepoint (``graph/operations.py``) only enforces this for writes whose
``source`` is NOT a governed verb. Governed verbs funnel state in through the
audited proposal/workflow machinery and are always permitted for
``proposal_only`` types; add new governed verbs to ``_GOVERNED_SOURCES`` below.
A ``mint_only`` type is stricter still: it refuses EVERY source except
``TOKEN_MINT_SOURCE`` — including the governed verbs — so ``_GOVERNED_SOURCES``
does NOT apply to it.
"""

from __future__ import annotations

import os
from typing import Iterable, Literal, Mapping

from cruxible_core.config.schema import CoreConfig
from cruxible_core.runtime.permissions import PermissionMode

WritePolicy = Literal["direct", "proposal_only", "mint_only"]

# Sources that funnel state in through governed, audited machinery. A write
# carrying one of these is always permitted for a ``proposal_only`` type. Keep
# this an ALLOWLIST (not a denylist of direct verbs): every write funnels through
# the chokepoint, so an allowlist means a future direct verb cannot silently
# bypass governance — it is refused until it is deliberately added here.
#   - "workflow_apply": canonical workflow apply_entities / apply_relationships
#   - "group_resolve":  proposal group resolution (group propose -> resolve)
# Add any future governed verb here, with a comment naming it.
# NOTE: this set governs ``proposal_only`` ONLY. A ``mint_only`` type refuses
# every source except ``TOKEN_MINT_SOURCE`` — the governed verbs included — so
# ``TOKEN_MINT_SOURCE`` is deliberately NOT a member here.
_GOVERNED_SOURCES: frozenset[str] = frozenset({"workflow_apply", "group_resolve"})

# The sole source permitted to write a ``mint_only`` (auth-managed) entity type.
# Exclusive: a ``mint_only`` type refuses ALL other sources, including the
# governed verbs in ``_GOVERNED_SOURCES``.
TOKEN_MINT_SOURCE = "token_mint"

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
    if explicit == "mint_only":
        # ABSOLUTE: a mint_only (auth-managed) type stays mint_only regardless of
        # the env kill-switch. The kill-switch only downgrades to proposal_only,
        # which is WEAKER than mint_only (it would let governed verbs through), so
        # honoring it here would loosen, not tighten, the constraint.
        return "mint_only"
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


# ---------------------------------------------------------------------------
# Config-declared write tiers (the SECOND per-type write-governance knob)
# ---------------------------------------------------------------------------
#
# ``write_tier`` is the tier-ladder complement of ``write_policy``:
#
# * ``write_policy`` answers "may this type be direct-written AT ALL?"
#   (hard constraint, tier-independent, enforced at graph/operations.py).
# * ``write_tier`` answers "which permission tier may direct-write it?"
#   (per-type requirement for the direct-write verbs, enforced at the
#   runtime/api.py facades BEFORE the write reaches the chokepoint).
#
# A type may declare ``write_tier: governed_write`` to open its direct-write
# surface to sub-``graph_write`` actors (e.g. an operation kit's StateNote
# scratchpad surface). Undeclared types keep the classic ``graph_write``
# requirement, so declared tiers only ever LOWER the requirement — schema
# validation rejects tiers above ``graph_write`` (that is write_policy's job)
# and below ``governed_write`` (read_only is not a write tier).

# Schema-validated write_tier names -> permission modes.
_WRITE_TIER_MODES: dict[str, PermissionMode] = {
    "governed_write": PermissionMode.GOVERNED_WRITE,
    "graph_write": PermissionMode.GRAPH_WRITE,
}

# Requirement for any type without a declared write_tier (and for unknown type
# names: they fail schema validation later, but the permission gate must not be
# the layer that loosens for them).
DEFAULT_DIRECT_WRITE_TIER = PermissionMode.GRAPH_WRITE


def required_direct_write_tier(
    config: CoreConfig,
    *,
    entity_types: Iterable[str],
    relationship_types: Iterable[str],
) -> PermissionMode:
    """Return the permission tier a direct write of these types requires.

    The requirement is the MAX over every touched type: each type contributes
    its declared ``write_tier`` (or ``graph_write`` when undeclared/unknown),
    so a mixed payload is gated at the strictest member. An empty payload
    (a no-op or validation ping) keeps the classic ``graph_write`` requirement.

    Relationship writes contribute only the relationship type itself — the
    edge is the thing mutated; endpoint entities are referenced, not written.
    Creates and updates are one surface: the tier declares who may direct-write
    the type, not which verb flavor they use. ``write_policy`` refusals and
    mutation guards run after (and independent of) this check.
    """
    required = PermissionMode.READ_ONLY
    touched = False

    for entity_type in entity_types:
        touched = True
        schema = config.entity_types.get(entity_type)
        tier = schema.write_tier if schema is not None else None
        required = max(required, _WRITE_TIER_MODES.get(tier or "", DEFAULT_DIRECT_WRITE_TIER))

    for relationship_type in relationship_types:
        touched = True
        rel_schema = config.get_relationship(relationship_type)
        tier = rel_schema.write_tier if rel_schema is not None else None
        required = max(required, _WRITE_TIER_MODES.get(tier or "", DEFAULT_DIRECT_WRITE_TIER))

    if not touched:
        return DEFAULT_DIRECT_WRITE_TIER
    return required

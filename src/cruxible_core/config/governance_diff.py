"""Governance-diff classification over composed config pairs.

Pure functions over (old composed config, new composed config) that produce a
typed diff plus a ``tightened`` / ``neutral`` / ``weakened`` classification.
The ``config refresh`` gate is asymmetric — tightening/neutral refreshes need
``graph_write``, weakening refreshes need ``admin`` — so the classifier is the
security boundary and MUST fail closed: any change it cannot positively
classify as neutral or tightening is treated as weakening. Unknown config
surfaces default to weakening, never neutral.

Weakening rules (dd-config-by-reference-one-source):

- ``write_policy`` removed or downgraded on any type (order:
  ``mint_only`` > ``proposal_only`` > ``direct``); absent inherits
  ``runtime.default_write_policy``, so EFFECTIVE policies are compared.
- ``runtime.default_write_policy`` downgraded.
- A mutation guard removed, or its scope narrowed.
- Proposal policy loosened on any relationship: a signal removed, a signal
  role downgraded, ``always_review_on_unsure`` dropped, ``auto_resolve_when``
  broadened, the prior-trust requirement dropped.
- An error-severity quality check (or constraint) removed or demoted below
  error.
- Fail-closed conservatism for everything else that touches a governance
  surface, including changes to unrecognized config surfaces.

Schema additions, query changes, descriptions, and workflow additions are
neutral; tightening is the mirror image of the weakening list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from cruxible_core.config.schema import (
    CoreConfig,
    EntityTypeSchema,
    MutationGuardSchema,
    ProposalPolicySchema,
    RelationshipSchema,
    RuntimeConfigSchema,
)

Classification = Literal["tightened", "neutral", "weakened"]
ChangeDirection = Literal["tightening", "weakening"]

# Higher rank = stricter. A rank decrease on any ordered governance knob is a
# downgrade (weakening); an increase is the mirror tightening.
_WRITE_POLICY_RANK = {"direct": 0, "proposal_only": 1, "mint_only": 2}
_SIGNAL_ROLE_RANK = {"advisory": 0, "required": 1, "blocking": 2}
_AUTO_RESOLVE_RANK = {"no_contradict": 0, "all_support": 1}
_PRIOR_TRUST_RANK = {"trusted_or_watch": 0, "trusted_only": 1}

# Top-level CoreConfig surfaces with dedicated classification rules.
_GOVERNED_TOP_LEVEL_FIELDS = frozenset(
    {
        "entity_types",
        "relationships",
        "runtime",
        "mutation_guards",
        "quality_checks",
        "constraints",
        "decision_policies",
    }
)
# Top-level CoreConfig surfaces that carry no write-gating semantics: changes
# here never loosen a guarantee (queries and workflows are read/plan surfaces;
# provider/artifact drift is pinned by the workflow lock, not this classifier).
_NEUTRAL_TOP_LEVEL_FIELDS = frozenset(
    {
        "version",
        "name",
        "description",
        "cruxible_version",
        "extends",
        "named_queries",
        "enums",
        "feedback_profiles",
        "outcome_profiles",
        "contracts",
        "artifacts",
        "providers",
        "workflows",
        "tests",
    }
)
# RuntimeConfigSchema knobs with dedicated rules; any OTHER runtime field that
# changes is an unrecognized surface and fails closed to weakening.
_KNOWN_RUNTIME_FIELDS = frozenset({"trace_payloads", "mutation_payloads", "default_write_policy"})


@dataclass(frozen=True)
class GovernanceChange:
    """One classified governance-relevant difference between composed configs."""

    direction: ChangeDirection
    surface: str
    subject: str
    detail: str

    @property
    def summary(self) -> str:
        return f"[{self.direction}] {self.surface} '{self.subject}': {self.detail}"


@dataclass(frozen=True)
class GovernanceDiff:
    """Typed governance diff plus its aggregate classification.

    ``weakened`` dominates: a change set with any weakening finding is
    weakened even if it also tightens elsewhere, because the gate must be the
    strictest one any individual change requires.
    """

    changes: tuple[GovernanceChange, ...]

    @property
    def classification(self) -> Classification:
        if any(change.direction == "weakening" for change in self.changes):
            return "weakened"
        if self.changes:
            return "tightened"
        return "neutral"

    @property
    def summary_lines(self) -> list[str]:
        return [change.summary for change in self.changes]


def diff_governance(old: CoreConfig, new: CoreConfig) -> GovernanceDiff:
    """Diff two composed configs and classify the governance impact."""
    changes: list[GovernanceChange] = []
    changes.extend(_diff_default_write_policy(old.runtime, new.runtime))
    changes.extend(_diff_runtime_audit_knobs(old.runtime, new.runtime))
    changes.extend(_diff_entity_types(old, new))
    changes.extend(_diff_relationships(old, new))
    changes.extend(_diff_mutation_guards(old, new))
    changes.extend(
        _diff_severity_checks(
            old_items={check.name: check for check in old.quality_checks},
            new_items={check.name: check for check in new.quality_checks},
            surface="quality_check",
        )
    )
    changes.extend(
        _diff_severity_checks(
            old_items={constraint.name: constraint for constraint in old.constraints},
            new_items={constraint.name: constraint for constraint in new.constraints},
            surface="constraint",
        )
    )
    changes.extend(_diff_decision_policies(old, new))
    changes.extend(_diff_unrecognized_top_level(old, new))
    return GovernanceDiff(changes=tuple(changes))


def _dump(model: Any) -> Any:
    return model.model_dump(mode="python", by_alias=True, exclude_none=True)


def _dumps_equal(old: Any, new: Any, *, ignore: frozenset[str] = frozenset()) -> bool:
    old_data = {key: value for key, value in _dump(old).items() if key not in ignore}
    new_data = {key: value for key, value in _dump(new).items() if key not in ignore}
    return old_data == new_data


# ---------------------------------------------------------------------------
# Write policies (effective, per type)
# ---------------------------------------------------------------------------


def _diff_default_write_policy(
    old: RuntimeConfigSchema, new: RuntimeConfigSchema
) -> list[GovernanceChange]:
    old_rank = _WRITE_POLICY_RANK[old.default_write_policy]
    new_rank = _WRITE_POLICY_RANK[new.default_write_policy]
    if new_rank == old_rank:
        return []
    direction: ChangeDirection = "weakening" if new_rank < old_rank else "tightening"
    verb = "downgraded" if direction == "weakening" else "upgraded"
    return [
        GovernanceChange(
            direction=direction,
            surface="runtime.default_write_policy",
            subject="runtime",
            detail=(f"{verb} from '{old.default_write_policy}' to '{new.default_write_policy}'"),
        )
    ]


def _diff_runtime_audit_knobs(
    old: RuntimeConfigSchema, new: RuntimeConfigSchema
) -> list[GovernanceChange]:
    """Fail closed on audit-retention changes and unrecognized runtime fields.

    ``trace_payloads`` / ``mutation_payloads`` trade audit completeness against
    payload retention; neither direction is positively classifiable as safe, so
    any change is weakening.
    """
    changes: list[GovernanceChange] = []
    for field_name in ("trace_payloads", "mutation_payloads"):
        old_value = getattr(old, field_name)
        new_value = getattr(new, field_name)
        if old_value != new_value:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface=f"runtime.{field_name}",
                    subject="runtime",
                    detail=(
                        f"audit retention changed from '{old_value}' to '{new_value}' "
                        "(retention changes cannot be verified as safe)"
                    ),
                )
            )
    for field_name in RuntimeConfigSchema.model_fields:
        if field_name in _KNOWN_RUNTIME_FIELDS:
            continue
        if getattr(old, field_name) != getattr(new, field_name):
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface=f"runtime.{field_name}",
                    subject="runtime",
                    detail="unrecognized runtime surface changed (fail-closed)",
                )
            )
    return changes


def _effective_entity_policy(schema: EntityTypeSchema, runtime: RuntimeConfigSchema) -> str:
    return schema.write_policy or runtime.default_write_policy


def _effective_relationship_policy(schema: RelationshipSchema, runtime: RuntimeConfigSchema) -> str:
    return schema.write_policy or runtime.default_write_policy


def _diff_write_policy(
    *,
    surface: str,
    subject: str,
    old_effective: str,
    new_effective: str,
) -> list[GovernanceChange]:
    old_rank = _WRITE_POLICY_RANK[old_effective]
    new_rank = _WRITE_POLICY_RANK[new_effective]
    if new_rank == old_rank:
        return []
    direction: ChangeDirection = "weakening" if new_rank < old_rank else "tightening"
    verb = "downgraded" if direction == "weakening" else "upgraded"
    return [
        GovernanceChange(
            direction=direction,
            surface=surface,
            subject=subject,
            detail=(f"effective write_policy {verb} from '{old_effective}' to '{new_effective}'"),
        )
    ]


# ---------------------------------------------------------------------------
# Entity types
# ---------------------------------------------------------------------------


def _diff_entity_types(old: CoreConfig, new: CoreConfig) -> list[GovernanceChange]:
    changes: list[GovernanceChange] = []
    for name, old_schema in old.entity_types.items():
        new_schema = new.entity_types.get(name)
        if new_schema is None:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface="entity_type",
                    subject=name,
                    detail="entity type removed (its write policy and guards no longer apply)",
                )
            )
            continue
        changes.extend(
            _diff_write_policy(
                surface="entity_type.write_policy",
                subject=name,
                old_effective=_effective_entity_policy(old_schema, old.runtime),
                new_effective=_effective_entity_policy(new_schema, new.runtime),
            )
        )
        if old_schema.auth_managed != new_schema.auth_managed:
            direction: ChangeDirection = "weakening" if old_schema.auth_managed else "tightening"
            changes.append(
                GovernanceChange(
                    direction=direction,
                    surface="entity_type.auth_managed",
                    subject=name,
                    detail=(
                        "auth_managed dropped" if direction == "weakening" else "auth_managed added"
                    ),
                )
            )
        changes.extend(
            _diff_constraint_refs(
                subject=name,
                old_refs=old_schema.constraints,
                new_refs=new_schema.constraints,
            )
        )
        changes.extend(
            _diff_properties(
                surface="entity_type.property",
                subject=name,
                old_properties=old_schema.properties,
                new_properties=new_schema.properties,
            )
        )
    # Added entity types are schema additions: neutral.
    return changes


def _diff_constraint_refs(
    *, subject: str, old_refs: list[str], new_refs: list[str]
) -> list[GovernanceChange]:
    changes: list[GovernanceChange] = []
    for ref in old_refs:
        if ref not in new_refs:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface="entity_type.constraints",
                    subject=subject,
                    detail=f"constraint reference '{ref}' removed",
                )
            )
    for ref in new_refs:
        if ref not in old_refs:
            changes.append(
                GovernanceChange(
                    direction="tightening",
                    surface="entity_type.constraints",
                    subject=subject,
                    detail=f"constraint reference '{ref}' added",
                )
            )
    return changes


def _diff_properties(
    *,
    surface: str,
    subject: str,
    old_properties: dict[str, Any],
    new_properties: dict[str, Any],
) -> list[GovernanceChange]:
    """Property additions are neutral; removals and edits fail closed."""
    changes: list[GovernanceChange] = []
    for name, old_prop in old_properties.items():
        new_prop = new_properties.get(name)
        if new_prop is None:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface=surface,
                    subject=f"{subject}.{name}",
                    detail="property removed (fail-closed: existing data shape changed)",
                )
            )
            continue
        if not _dumps_equal(old_prop, new_prop, ignore=frozenset({"description"})):
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface=surface,
                    subject=f"{subject}.{name}",
                    detail=(
                        "property definition changed "
                        "(fail-closed: cannot be verified as tightening)"
                    ),
                )
            )
    return changes


# ---------------------------------------------------------------------------
# Relationships (write policy + proposal policy)
# ---------------------------------------------------------------------------

_RELATIONSHIP_GOVERNED_FIELDS = frozenset(
    {"write_policy", "proposal_policy", "properties", "description"}
)


def _diff_relationships(old: CoreConfig, new: CoreConfig) -> list[GovernanceChange]:
    changes: list[GovernanceChange] = []
    new_by_name = {rel.name: rel for rel in new.relationships}
    for old_rel in old.relationships:
        new_rel = new_by_name.get(old_rel.name)
        if new_rel is None:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface="relationship",
                    subject=old_rel.name,
                    detail=(
                        "relationship removed (its write and proposal policies no longer apply)"
                    ),
                )
            )
            continue
        changes.extend(
            _diff_write_policy(
                surface="relationship.write_policy",
                subject=old_rel.name,
                old_effective=_effective_relationship_policy(old_rel, old.runtime),
                new_effective=_effective_relationship_policy(new_rel, new.runtime),
            )
        )
        changes.extend(_diff_proposal_policy(old_rel, new_rel))
        changes.extend(
            _diff_properties(
                surface="relationship.property",
                subject=old_rel.name,
                old_properties=old_rel.properties,
                new_properties=new_rel.properties,
            )
        )
        # Structural edits (endpoints, cardinality, proposal identity, ...)
        # cannot be verified as tightening: fail closed.
        old_residual = {
            key: value
            for key, value in _dump(old_rel).items()
            if key not in _RELATIONSHIP_GOVERNED_FIELDS
        }
        new_residual = {
            key: value
            for key, value in _dump(new_rel).items()
            if key not in _RELATIONSHIP_GOVERNED_FIELDS
        }
        if old_residual != new_residual:
            changed_fields = sorted(
                key
                for key in {*old_residual, *new_residual}
                if old_residual.get(key) != new_residual.get(key)
            )
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface="relationship",
                    subject=old_rel.name,
                    detail=(
                        f"structural fields changed ({', '.join(changed_fields)}) "
                        "(fail-closed: cannot be verified as tightening)"
                    ),
                )
            )
    # Added relationships are schema additions: neutral.
    return changes


def _diff_proposal_policy(
    old_rel: RelationshipSchema, new_rel: RelationshipSchema
) -> list[GovernanceChange]:
    subject = old_rel.name
    old_policy = old_rel.proposal_policy
    new_policy = new_rel.proposal_policy
    if old_policy is None and new_policy is None:
        return []
    if old_policy is not None and new_policy is None:
        return [
            GovernanceChange(
                direction="weakening",
                surface="proposal_policy",
                subject=subject,
                detail="proposal policy removed",
            )
        ]
    if old_policy is None and new_policy is not None:
        return [
            GovernanceChange(
                direction="tightening",
                surface="proposal_policy",
                subject=subject,
                detail="proposal policy added",
            )
        ]
    assert old_policy is not None and new_policy is not None
    return _diff_proposal_policy_fields(subject, old_policy, new_policy)


def _diff_proposal_policy_fields(
    subject: str, old_policy: ProposalPolicySchema, new_policy: ProposalPolicySchema
) -> list[GovernanceChange]:
    changes: list[GovernanceChange] = []
    for signal_name, old_signal in old_policy.signals.items():
        new_signal = new_policy.signals.get(signal_name)
        if new_signal is None:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface="proposal_policy.signal",
                    subject=f"{subject}.{signal_name}",
                    detail="signal removed",
                )
            )
            continue
        old_role_rank = _SIGNAL_ROLE_RANK[old_signal.role]
        new_role_rank = _SIGNAL_ROLE_RANK[new_signal.role]
        if new_role_rank != old_role_rank:
            direction: ChangeDirection = (
                "weakening" if new_role_rank < old_role_rank else "tightening"
            )
            verb = "downgraded" if direction == "weakening" else "upgraded"
            changes.append(
                GovernanceChange(
                    direction=direction,
                    surface="proposal_policy.signal",
                    subject=f"{subject}.{signal_name}",
                    detail=f"role {verb} from '{old_signal.role}' to '{new_signal.role}'",
                )
            )
        for flag in ("always_review_on_unsure", "require_evidence_on_support"):
            old_flag = getattr(old_signal, flag)
            new_flag = getattr(new_signal, flag)
            if old_flag != new_flag:
                changes.append(
                    GovernanceChange(
                        direction="weakening" if old_flag else "tightening",
                        surface="proposal_policy.signal",
                        subject=f"{subject}.{signal_name}",
                        detail=(f"{flag} dropped" if old_flag else f"{flag} added"),
                    )
                )
    for signal_name in new_policy.signals:
        if signal_name not in old_policy.signals:
            changes.append(
                GovernanceChange(
                    direction="tightening",
                    surface="proposal_policy.signal",
                    subject=f"{subject}.{signal_name}",
                    detail="signal added",
                )
            )

    old_auto_rank = _AUTO_RESOLVE_RANK[old_policy.auto_resolve_when]
    new_auto_rank = _AUTO_RESOLVE_RANK[new_policy.auto_resolve_when]
    if new_auto_rank != old_auto_rank:
        direction = "weakening" if new_auto_rank < old_auto_rank else "tightening"
        verb = "broadened" if direction == "weakening" else "narrowed"
        changes.append(
            GovernanceChange(
                direction=direction,
                surface="proposal_policy.auto_resolve_when",
                subject=subject,
                detail=(
                    f"{verb} from '{old_policy.auto_resolve_when}' "
                    f"to '{new_policy.auto_resolve_when}'"
                ),
            )
        )

    old_trust_rank = _PRIOR_TRUST_RANK[old_policy.auto_resolve_requires_prior_trust]
    new_trust_rank = _PRIOR_TRUST_RANK[new_policy.auto_resolve_requires_prior_trust]
    if new_trust_rank != old_trust_rank:
        direction = "weakening" if new_trust_rank < old_trust_rank else "tightening"
        verb = "dropped" if direction == "weakening" else "raised"
        changes.append(
            GovernanceChange(
                direction=direction,
                surface="proposal_policy.auto_resolve_requires_prior_trust",
                subject=subject,
                detail=(
                    f"prior-trust requirement {verb}: "
                    f"'{old_policy.auto_resolve_requires_prior_trust}' -> "
                    f"'{new_policy.auto_resolve_requires_prior_trust}'"
                ),
            )
        )

    if new_policy.max_group_size != old_policy.max_group_size:
        direction = (
            "weakening" if new_policy.max_group_size > old_policy.max_group_size else "tightening"
        )
        verb = "raised" if direction == "weakening" else "lowered"
        changes.append(
            GovernanceChange(
                direction=direction,
                surface="proposal_policy.max_group_size",
                subject=subject,
                detail=(
                    f"max_group_size {verb} from {old_policy.max_group_size} "
                    f"to {new_policy.max_group_size}"
                ),
            )
        )
    return changes


# ---------------------------------------------------------------------------
# Mutation guards
# ---------------------------------------------------------------------------

_GUARD_SCOPE_FIELDS = ("where", "where_related", "where_not_related")


def _diff_mutation_guards(old: CoreConfig, new: CoreConfig) -> list[GovernanceChange]:
    changes: list[GovernanceChange] = []
    old_guards = {guard.name: guard for guard in old.mutation_guards}
    new_guards = {guard.name: guard for guard in new.mutation_guards}
    for name, old_guard in old_guards.items():
        new_guard = new_guards.get(name)
        if new_guard is None:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface="mutation_guard",
                    subject=name,
                    detail="mutation guard removed",
                )
            )
            continue
        changes.extend(_diff_mutation_guard(name, old_guard, new_guard))
    for name in new_guards:
        if name not in old_guards:
            changes.append(
                GovernanceChange(
                    direction="tightening",
                    surface="mutation_guard",
                    subject=name,
                    detail="mutation guard added",
                )
            )
    return changes


def _diff_mutation_guard(
    name: str, old_guard: MutationGuardSchema, new_guard: MutationGuardSchema
) -> list[GovernanceChange]:
    if _dumps_equal(old_guard, new_guard):
        return []
    # Message edits carry no enforcement semantics.
    if _dumps_equal(old_guard, new_guard, ignore=frozenset({"message"})):
        return []

    # The only positively classifiable content change is a pure scope change
    # with every enforcement field held equal.
    non_scope_ignore = frozenset({"message", *_GUARD_SCOPE_FIELDS})
    if _dumps_equal(old_guard, new_guard, ignore=non_scope_ignore):
        comparisons = [
            _compare_guard_scope_field(
                getattr(old_guard, field_name), getattr(new_guard, field_name)
            )
            for field_name in _GUARD_SCOPE_FIELDS
        ]
        if "unknown" not in comparisons:
            if "narrowed" in comparisons and "broadened" not in comparisons:
                return [
                    GovernanceChange(
                        direction="weakening",
                        surface="mutation_guard",
                        subject=name,
                        detail="guard scope narrowed (guard fires on fewer writes)",
                    )
                ]
            if "broadened" in comparisons and "narrowed" not in comparisons:
                return [
                    GovernanceChange(
                        direction="tightening",
                        surface="mutation_guard",
                        subject=name,
                        detail="guard scope broadened (guard fires on more writes)",
                    )
                ]
    return [
        GovernanceChange(
            direction="weakening",
            surface="mutation_guard",
            subject=name,
            detail=("guard definition changed (fail-closed: cannot be verified as tightening)"),
        )
    ]


def _compare_guard_scope_field(
    old_value: Any, new_value: Any
) -> Literal["equal", "narrowed", "broadened", "unknown"]:
    """Compare one guard scoping field.

    Scope predicates restrict when a guard fires: removing predicates makes
    the guard fire on MORE writes (broadened coverage = tightening), adding
    predicates makes it fire on FEWER writes (narrowed coverage = weakening).
    List fields carry AND semantics, so multiset containment orders them.
    """
    if isinstance(old_value, list) or isinstance(new_value, list):
        old_items = [_dump(item) for item in old_value or []]
        new_items = [_dump(item) for item in new_value or []]
        if _multiset_contains(old_items, new_items):
            return "equal" if _multiset_contains(new_items, old_items) else "broadened"
        if _multiset_contains(new_items, old_items):
            return "narrowed"
        return "unknown"

    old_data = _dump(old_value) if old_value is not None else None
    new_data = _dump(new_value) if new_value is not None else None
    if old_data == new_data:
        return "equal"
    if new_data is None:
        return "broadened"
    if old_data is None:
        return "narrowed"
    return "unknown"


def _multiset_contains(container: list[Any], items: list[Any]) -> bool:
    remaining = list(container)
    for item in items:
        try:
            remaining.remove(item)
        except ValueError:
            return False
    return True


# ---------------------------------------------------------------------------
# Quality checks and constraints (severity-bearing validations)
# ---------------------------------------------------------------------------


def _diff_severity_checks(
    *,
    old_items: dict[str, Any],
    new_items: dict[str, Any],
    surface: str,
) -> list[GovernanceChange]:
    """Classify severity-bearing validation surfaces by their error floor.

    Only error-severity entries gate anything: removing or demoting one is
    weakening; adding or promoting one is tightening. Warning-severity churn
    is neutral, but content edits to an error-severity entry fail closed.
    """
    changes: list[GovernanceChange] = []
    for name, old_check in old_items.items():
        new_check = new_items.get(name)
        if old_check.severity != "error":
            if new_check is not None and new_check.severity == "error":
                changes.append(
                    GovernanceChange(
                        direction="tightening",
                        surface=surface,
                        subject=name,
                        detail="severity promoted to error",
                    )
                )
            continue
        if new_check is None:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface=surface,
                    subject=name,
                    detail="error-severity check removed",
                )
            )
            continue
        if new_check.severity != "error":
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface=surface,
                    subject=name,
                    detail=f"severity demoted from 'error' to '{new_check.severity}'",
                )
            )
            continue
        if not _dumps_equal(old_check, new_check, ignore=frozenset({"description"})):
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface=surface,
                    subject=name,
                    detail=(
                        "error-severity check changed "
                        "(fail-closed: cannot be verified as tightening)"
                    ),
                )
            )
    for name, new_check in new_items.items():
        if name not in old_items and new_check.severity == "error":
            changes.append(
                GovernanceChange(
                    direction="tightening",
                    surface=surface,
                    subject=name,
                    detail="error-severity check added",
                )
            )
    return changes


# ---------------------------------------------------------------------------
# Decision policies
# ---------------------------------------------------------------------------

_DECISION_POLICY_NEUTRAL_FIELDS = frozenset({"description", "rationale"})


def _diff_decision_policies(old: CoreConfig, new: CoreConfig) -> list[GovernanceChange]:
    """Classify decision-policy churn by effect.

    ``require_review`` policies are review gates: removal is weakening,
    addition is tightening. ``suppress`` policies HIDE actions, so adding one
    is weakening (it can hide reviewable actions) and removing one restores
    visibility (tightening). Content edits fail closed.
    """
    changes: list[GovernanceChange] = []
    old_policies = {policy.name: policy for policy in old.decision_policies}
    new_policies = {policy.name: policy for policy in new.decision_policies}
    for name, old_policy in old_policies.items():
        new_policy = new_policies.get(name)
        if new_policy is None:
            direction: ChangeDirection = (
                "weakening" if old_policy.effect == "require_review" else "tightening"
            )
            detail = (
                "require_review policy removed"
                if old_policy.effect == "require_review"
                else "suppress policy removed (suppressed actions surface again)"
            )
            changes.append(
                GovernanceChange(
                    direction=direction,
                    surface="decision_policy",
                    subject=name,
                    detail=detail,
                )
            )
            continue
        if not _dumps_equal(old_policy, new_policy, ignore=_DECISION_POLICY_NEUTRAL_FIELDS):
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface="decision_policy",
                    subject=name,
                    detail=(
                        "decision policy changed (fail-closed: cannot be verified as tightening)"
                    ),
                )
            )
    for name, new_policy in new_policies.items():
        if name not in old_policies:
            direction = "tightening" if new_policy.effect == "require_review" else "weakening"
            detail = (
                "require_review policy added"
                if new_policy.effect == "require_review"
                else "suppress policy added (fail-closed: it can hide reviewable actions)"
            )
            changes.append(
                GovernanceChange(
                    direction=direction,
                    surface="decision_policy",
                    subject=name,
                    detail=detail,
                )
            )
    return changes


# ---------------------------------------------------------------------------
# Unrecognized top-level surfaces
# ---------------------------------------------------------------------------


def _diff_unrecognized_top_level(old: CoreConfig, new: CoreConfig) -> list[GovernanceChange]:
    """Fail closed on any CoreConfig field this classifier does not model."""
    changes: list[GovernanceChange] = []
    for field_name in CoreConfig.model_fields:
        if field_name in _GOVERNED_TOP_LEVEL_FIELDS or field_name in _NEUTRAL_TOP_LEVEL_FIELDS:
            continue
        old_value = getattr(old, field_name)
        new_value = getattr(new, field_name)
        old_data = _dump(old_value) if hasattr(old_value, "model_dump") else old_value
        new_data = _dump(new_value) if hasattr(new_value, "model_dump") else new_value
        if old_data != new_data:
            changes.append(
                GovernanceChange(
                    direction="weakening",
                    surface="config",
                    subject=field_name,
                    detail="unrecognized config surface changed (fail-closed)",
                )
            )
    return changes


__all__ = [
    "Classification",
    "ChangeDirection",
    "GovernanceChange",
    "GovernanceDiff",
    "diff_governance",
]

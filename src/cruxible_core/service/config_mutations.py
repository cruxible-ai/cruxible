"""Config mutation service functions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any, Literal

from cruxible_core.config.constraint_rules import parse_constraint_rule
from cruxible_core.config.schema import (
    ConstraintSchema,
    DecisionPolicyMatch,
    DecisionPolicySchema,
)
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.predicate import CONSTRAINT_RULE_SYNTAX
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import (
    AddConstraintServiceResult,
    AddDecisionPolicyServiceResult,
)
from cruxible_core.workflow.compiler import compute_lock_config_digest

ConstraintSeverity = Literal["warning", "error"]
DecisionPolicyAppliesTo = Literal["query", "workflow"]
DecisionPolicyEffect = Literal["suppress", "require_review"]


@contextmanager
def _config_write_is_undone_if_the_receipt_does_not_commit(
    instance: InstanceProtocol,
) -> Iterator[None]:
    """Restore the on-disk config if anything inside the receipt boundary fails.

    ``save_config`` replaces the YAML immediately, but the receipt is not durable
    until the mutation-receipt context manager commits its transaction on exit —
    and a failure there rolls back SQLite while leaving the ACTIVE RULES changed
    with no receipt naming who changed them. A constraint or decision policy
    adjudicates every later query and workflow, so an unreceipted one is exactly
    the state the receipt exists to make impossible.

    The prior bytes (not a re-serialized ``CoreConfig``) are what gets restored:
    a round trip through the model would silently normalize formatting and drop
    anything the loader does not model. The config provenance is captured and
    restored alongside them, since ``save_config`` re-records the materialized
    digest and leaving that pointing at bytes no longer on disk would fail
    ``verify_config_integrity``.
    """
    path = instance.get_config_path()
    prior_bytes = path.read_bytes() if path.exists() else None
    prior_provenance = instance.get_config_provenance()
    try:
        yield
    except BaseException:
        if prior_bytes is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(prior_bytes)
        with suppress(NotImplementedError):
            instance.set_config_provenance(prior_provenance)
        raise


def service_add_constraint(
    instance: InstanceProtocol,
    *,
    name: str,
    rule: str,
    severity: str = "warning",
    description: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> AddConstraintServiceResult:
    """Add a constraint rule to the active config and persist it.

    A constraint is ACTIVE CONFIG: once saved it adjudicates every subsequent
    query and workflow. The act therefore gets the same treatment as any other
    governed mutation -- an actor-attributed receipt naming the config digest
    before and after -- so a later reader can answer "who changed the rules, and
    which config did the change produce".
    """
    config = instance.load_config()

    for existing in config.constraints:
        if existing.name == name:
            raise ConfigError(f"Constraint '{name}' already exists in config")

    parsed = parse_constraint_rule(rule)
    if parsed is None:
        raise ConfigError(
            f"Rule syntax not supported: {rule!r}. Expected: {CONSTRAINT_RULE_SYNTAX}"
        )
    validated_severity = _constraint_severity(severity)
    config_digest_before = compute_lock_config_digest(config)

    config.constraints.append(
        ConstraintSchema(
            name=name,
            rule=rule,
            severity=validated_severity,
            description=description,
        )
    )
    warnings = validate_config(config)

    with (
        _config_write_is_undone_if_the_receipt_does_not_commit(instance),
        mutation_receipt(
            instance,
            "config_add_constraint",
            {
                "name": name,
                "rule": rule,
                "severity": validated_severity,
                "description": description,
            },
            actor_context=actor_context,
        ) as ctx,
    ):
        assert ctx.builder is not None
        ctx.builder.record_validation(
            passed=True,
            detail={
                "name": name,
                "config_digest_before": config_digest_before,
                "config_digest_after": compute_lock_config_digest(config),
                "config_warnings": warnings,
            },
        )
        # LAST act inside the boundary: everything that can still refuse this
        # mutation has run, so the window between replacing the active rules and
        # durably receipting them is as short as it can be made — and the
        # wrapper above closes it entirely if the commit still fails.
        instance.save_config(config)
        ctx.set_result(
            AddConstraintServiceResult(
                name=name,
                added=True,
                config_updated=True,
                warnings=warnings,
            )
        )

    result = ctx.result
    assert isinstance(result, AddConstraintServiceResult)
    return result


def service_add_decision_policy(
    instance: InstanceProtocol,
    *,
    name: str,
    applies_to: str,
    relationship_type: str,
    effect: str,
    match: dict[str, Any] | None = None,
    description: str | None = None,
    rationale: str = "",
    query_name: str | None = None,
    workflow_name: str | None = None,
    expires_at: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> AddDecisionPolicyServiceResult:
    """Add a decision policy to the active config and persist it.

    Receipted with pre/post config digests for the same reason as
    :func:`service_add_constraint`: a decision policy suppresses or escalates
    results for every later reader of the affected relationship type.
    """
    config = instance.load_config()

    for existing in config.decision_policies:
        if existing.name == name:
            raise ConfigError(f"Decision policy '{name}' already exists in config")

    validated_applies_to = _decision_policy_applies_to(applies_to)
    validated_effect = _decision_policy_effect(effect)
    config_digest_before = compute_lock_config_digest(config)

    config.decision_policies.append(
        DecisionPolicySchema(
            name=name,
            description=description,
            rationale=rationale,
            applies_to=validated_applies_to,
            query_name=query_name,
            workflow_name=workflow_name,
            relationship_type=relationship_type,
            effect=validated_effect,
            match=DecisionPolicyMatch.model_validate(match or {}),
            expires_at=expires_at,
        )
    )
    warnings = validate_config(config)

    with (
        _config_write_is_undone_if_the_receipt_does_not_commit(instance),
        mutation_receipt(
            instance,
            "config_add_decision_policy",
            {
                "name": name,
                "applies_to": validated_applies_to,
                "relationship_type": relationship_type,
                "effect": validated_effect,
                "query_name": query_name,
                "workflow_name": workflow_name,
                "expires_at": expires_at,
            },
            actor_context=actor_context,
        ) as ctx,
    ):
        assert ctx.builder is not None
        ctx.builder.record_validation(
            passed=True,
            detail={
                "name": name,
                "config_digest_before": config_digest_before,
                "config_digest_after": compute_lock_config_digest(config),
                "config_warnings": warnings,
            },
        )
        # LAST act inside the boundary; see service_add_constraint.
        instance.save_config(config)
        ctx.set_result(
            AddDecisionPolicyServiceResult(
                name=name,
                added=True,
                config_updated=True,
                warnings=warnings,
            )
        )

    result = ctx.result
    assert isinstance(result, AddDecisionPolicyServiceResult)
    return result


def _constraint_severity(value: str) -> ConstraintSeverity:
    if value == "warning":
        return "warning"
    if value == "error":
        return "error"
    raise ConfigError("Constraint severity must be 'warning' or 'error'")


def _decision_policy_applies_to(value: str) -> DecisionPolicyAppliesTo:
    if value == "query":
        return "query"
    if value == "workflow":
        return "workflow"
    raise ConfigError("Decision policy applies_to must be 'query' or 'workflow'")


def _decision_policy_effect(value: str) -> DecisionPolicyEffect:
    if value == "suppress":
        return "suppress"
    if value == "require_review":
        return "require_review"
    raise ConfigError("Decision policy effect must be 'suppress' or 'require_review'")

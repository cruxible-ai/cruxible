"""Governance, policy, quality, and outcome labels for canonical views."""

from __future__ import annotations

from typing import Any

from cruxible_core.canonical_views.labels import (
    _code_list,
    _humanize_label,
    _humanize_list,
)
from cruxible_core.config.schema import CoreConfig


def _governed_relationship_creation_paths(config: CoreConfig) -> dict[str, list[str]]:
    paths: dict[str, set[str]] = {}
    for workflow_name, workflow in sorted(config.workflows.items()):
        for step in workflow.steps:
            if step.propose_relationship_group is None:
                continue
            relationship_type = step.propose_relationship_group.relationship_type
            paths.setdefault(relationship_type, set()).add(workflow_name)
    return {
        relationship_type: sorted(workflow_names)
        for relationship_type, workflow_names in paths.items()
    }


def _creation_path_label(workflow_names: list[str]) -> str:
    if not workflow_names:
        return "Agent/manual group propose"
    return f"Workflow: {_humanize_list(sorted(workflow_names))}"


def _matching_policy_label(auto_resolve_when: str, prior_trust_policy: str) -> str:
    return (
        f"{_humanize_label(auto_resolve_when)}; prior trust: {_humanize_label(prior_trust_policy)}"
    )


def _decision_policy_label(policies: list[Any]) -> str:
    if not policies:
        return "Trust-gated auto-resolve"
    return "; ".join(
        f"{_humanize_label(policy.effect)}: {_humanize_label(policy.name)}"
        for policy in sorted(policies, key=lambda item: item.name)
    )


def _feedback_profile_label(profile: Any | None) -> str:
    if profile is None:
        return "-"
    count = len(profile.reason_codes)
    if count == 1:
        return "1 reason code"
    return f"{count} reason codes"


def _quality_check_target_label(check: Any) -> str:
    kind = getattr(check, "kind", "")
    if kind in {"property", "json_content"}:
        if check.target == "entity":
            return f"{_humanize_label(check.entity_type)}.{check.property}"
        return f"{_humanize_label(check.relationship_type)}.{check.property}"
    if kind == "uniqueness":
        return _humanize_label(check.entity_type)
    if kind == "bounds":
        if check.target == "entity_count":
            return f"{_humanize_label(check.entity_type)} count"
        return f"{_humanize_label(check.relationship_type)} count"
    if kind == "cardinality":
        direction = "out" if check.direction == "outgoing" else "in"
        return (
            f"{_humanize_label(check.entity_type)} -> "
            f"{_humanize_label(check.relationship_type)} ({direction})"
        )
    return "-"


def _quality_check_rule_label(check: Any) -> str:
    kind = getattr(check, "kind", "")
    if kind == "property":
        details = _quality_check_optional_details(
            (
                ("type", check.expected_type),
                ("pattern", check.pattern),
            )
        )
        return _join_label_parts((_humanize_label(check.rule), details))
    if kind == "json_content":
        details = _quality_check_optional_details(
            (
                ("keys", ", ".join(check.keys)),
                ("match", check.match),
            )
        )
        return _join_label_parts((_humanize_label(check.rule), details))
    if kind == "uniqueness":
        return f"Unique on {_code_list(check.properties)}"
    if kind in {"bounds", "cardinality"}:
        return _bounds_rule_label(check.min_count, check.max_count)
    return "-"


def _quality_check_optional_details(values: tuple[tuple[str, str | None], ...]) -> str:
    details = [f"{label}: `{value}`" for label, value in values if value]
    return "; ".join(details)


def _join_label_parts(values: tuple[str, ...]) -> str:
    return "; ".join(value for value in values if value)


def _bounds_rule_label(min_count: int | None, max_count: int | None) -> str:
    parts: list[str] = []
    if min_count is not None:
        parts.append(f"min `{min_count}`")
    if max_count is not None:
        parts.append(f"max `{max_count}`")
    return ", ".join(parts) if parts else "-"


def _profile_code_bullets(
    codes: Any,
) -> list[str]:
    lines = [f"  - `{code}` (`{hint}`): {description}" for code, hint, description in codes]
    if lines:
        return lines
    return ["  - None configured."]


def _scope_key_bullets(scope_keys: dict[str, str]) -> list[str]:
    if not scope_keys:
        return ["  - None configured."]
    return [f"  - `{key}`: `{path}`" for key, path in sorted(scope_keys.items())]


def _render_outcome_profile_group(
    title: str,
    config: CoreConfig,
    anchor_type: str,
) -> list[str]:
    profiles = [
        (name, profile)
        for name, profile in sorted(config.outcome_profiles.items())
        if profile.anchor_type == anchor_type
    ]
    lines = [f"#### {title}", ""]
    if not profiles:
        lines.append(f"No configured {title.lower()} outcome profiles.")
        return lines

    for index, (profile_name, profile) in enumerate(profiles):
        if index > 0:
            lines.append("")
        lines.extend(
            [
                f"##### `{profile_name}`",
                f"- Version: `{profile.version}`",
                f"- Target: {_outcome_profile_target(profile)}",
                "- Outcome codes:",
            ]
        )
        lines.extend(
            _profile_code_bullets(
                (
                    (code, outcome.remediation_hint, outcome.description)
                    for code, outcome in sorted(profile.outcome_codes.items())
                )
            )
        )
        lines.extend(["- Scope keys:", *_scope_key_bullets(profile.scope_keys)])
    return lines


def _outcome_profile_target(profile: Any) -> str:
    if profile.anchor_type == "resolution":
        return f"Relationship `{profile.relationship_type}`"
    return f"{_humanize_label(profile.surface_type)} `{profile.surface_name}`"

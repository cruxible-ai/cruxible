"""Version comparison helpers for KEV triage kit providers."""

from __future__ import annotations

import re
from typing import Any, Callable

from .common import _first_non_empty


def _assess_version_membership(
    installed_version: str,
    affected_versions: Any,
    fixed_version: Any,
) -> tuple[str, str]:
    if not installed_version:
        return "unsure", "Installed version is missing"

    ranges = affected_versions if isinstance(affected_versions, list) else []
    comparable_range_seen = False
    for range_spec in ranges:
        if not isinstance(range_spec, dict):
            continue
        membership = _version_in_range(installed_version, range_spec)
        if membership is None:
            continue
        comparable_range_seen = True
        if membership:
            return "support", _build_range_rationale(installed_version, range_spec)

    fixed = _first_non_empty(fixed_version)
    fixed_comparison = _compare_versions(installed_version, fixed) if fixed else None
    if comparable_range_seen:
        if fixed and fixed_comparison is not None and fixed_comparison >= 0:
            return (
                "contradict",
                f"Installed version {installed_version} is at or beyond fixed {fixed}",
            )
        return "contradict", f"Installed version {installed_version} is outside the affected range"

    if fixed and fixed_comparison is not None:
        if fixed_comparison < 0:
            return "support", f"Installed version {installed_version} is earlier than fixed {fixed}"
        return "contradict", f"Installed version {installed_version} is at or beyond fixed {fixed}"

    return (
        "unsure",
        f"Could not compare installed version {installed_version} to the reference data",
    )


def _build_range_rationale(installed_version: str, range_spec: dict[str, Any]) -> str:
    clauses: list[str] = [f"Installed version {installed_version} fits the affected range"]
    for field in (
        "version_start_including",
        "version_start_excluding",
        "version_end_including",
        "version_end_excluding",
        "version_exact",
        "fixed_version",
    ):
        value = _first_non_empty(range_spec.get(field))
        if value:
            clauses.append(f"{field}={value}")
    return "; ".join(clauses)


def _version_in_range(installed_version: str, range_spec: dict[str, Any]) -> bool | None:
    exact_version = _first_non_empty(range_spec.get("version_exact"))
    if exact_version:
        comparison = _compare_versions(installed_version, exact_version)
        return None if comparison is None else comparison == 0

    comparable = False
    predicates: tuple[tuple[str, Callable[[int], bool]], ...] = (
        ("version_start_including", lambda value: value >= 0),
        ("version_start_excluding", lambda value: value > 0),
        ("version_end_including", lambda value: value <= 0),
        ("version_end_excluding", lambda value: value < 0),
    )
    for field, predicate in predicates:
        bound = _first_non_empty(range_spec.get(field))
        if not bound:
            continue
        comparison = _compare_versions(installed_version, bound)
        if comparison is None:
            return None
        comparable = True
        if not predicate(comparison):
            return False

    if not comparable:
        return None
    return True


def _compare_versions(left: Any, right: Any) -> int | None:
    left_tokens = _tokenize_version(left)
    right_tokens = _tokenize_version(right)
    if not left_tokens or not right_tokens:
        return None

    max_length = max(len(left_tokens), len(right_tokens))
    for index in range(max_length):
        if index >= len(left_tokens):
            return -1
        if index >= len(right_tokens):
            return 1
        left_token = left_tokens[index]
        right_token = right_tokens[index]
        if left_token == right_token:
            continue
        if isinstance(left_token, int) and isinstance(right_token, int):
            return -1 if left_token < right_token else 1
        return -1 if str(left_token) < str(right_token) else 1

    return 0


def _tokenize_version(value: Any) -> list[int | str]:
    text = _first_non_empty(value)
    if text is None:
        return []
    parts = re.findall(r"[a-z]+|\d+", text.lower())
    tokens: list[int | str] = []
    for part in parts:
        if part.isdigit():
            tokens.append(int(part))
        else:
            tokens.append(part)
    return tokens

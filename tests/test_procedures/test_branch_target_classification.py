"""Completeness guardrail for the closed branch-target kind classification."""

from __future__ import annotations

from typing import get_args

import pytest

from cruxible_core.config.schema import StepKind
from cruxible_core.procedure.branch_targets import assert_branch_target_classification
from cruxible_core.procedure.types import _TOP_LEVEL_STEP_KINDS


def _shared_step_kinds() -> set[str]:
    return {str(kind) for kind in get_args(StepKind)}


def test_every_current_kind_is_classified() -> None:
    assert_branch_target_classification()


def test_adding_step_kind_fires_the_completeness_failure() -> None:
    expanded_step_kinds = _shared_step_kinds() | {"synthetic_new_step_kind"}

    with pytest.raises(AssertionError, match="unclassified.*synthetic_new_step_kind"):
        assert_branch_target_classification(step_kinds=expanded_step_kinds)


def test_adding_top_level_kind_fires_the_completeness_failure() -> None:
    expanded_top_level = set(_TOP_LEVEL_STEP_KINDS) | {"synthetic_top_level_kind"}

    with pytest.raises(AssertionError, match="synthetic_top_level_kind"):
        assert_branch_target_classification(
            step_kinds=_shared_step_kinds() | {"synthetic_top_level_kind"},
            top_level_step_kinds=expanded_top_level,
        )

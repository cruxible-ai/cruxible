"""Tests for the shared predicate core."""

from datetime import date, datetime, timedelta, timezone

import pytest

from cruxible_core.predicate import (
    PredicateCoercionError,
    comparison_symbol,
    evaluate_comparison,
    evaluate_typed_comparison,
    infer_predicate_value_type,
    normalize_comparison_op,
    validate_typed_predicate_operand,
)


class TestNormalizeComparisonOp:
    def test_symbolic_operators(self) -> None:
        assert normalize_comparison_op("==") == "eq"
        assert normalize_comparison_op("!=") == "ne"
        assert normalize_comparison_op(">") == "gt"
        assert normalize_comparison_op(">=") == "gte"
        assert normalize_comparison_op("<") == "lt"
        assert normalize_comparison_op("<=") == "lte"

    def test_semantic_operators(self) -> None:
        assert normalize_comparison_op("eq") == "eq"
        assert normalize_comparison_op("ne") == "ne"
        assert normalize_comparison_op("gt") == "gt"
        assert normalize_comparison_op("gte") == "gte"
        assert normalize_comparison_op("lt") == "lt"
        assert normalize_comparison_op("lte") == "lte"
        assert normalize_comparison_op("before") == "lt"
        assert normalize_comparison_op("on_or_before") == "lte"
        assert normalize_comparison_op("after") == "gt"
        assert normalize_comparison_op("on_or_after") == "gte"

    def test_unsupported_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported comparison operator"):
            normalize_comparison_op("contains")

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported comparison operator"):
            normalize_comparison_op("")


class TestComparisonSymbol:
    def test_roundtrip(self) -> None:
        assert comparison_symbol("eq") == "=="
        assert comparison_symbol("ne") == "!="
        assert comparison_symbol("gt") == ">"
        assert comparison_symbol("gte") == ">="
        assert comparison_symbol("lt") == "<"
        assert comparison_symbol("lte") == "<="
        assert comparison_symbol("before") == "<"
        assert comparison_symbol("on_or_after") == ">="

    def test_symbolic_input(self) -> None:
        assert comparison_symbol("==") == "=="
        assert comparison_symbol(">=") == ">="


class TestEvaluateComparison:
    # --- equality / inequality ---

    def test_eq_strings(self) -> None:
        assert evaluate_comparison("a", "eq", "a") is True
        assert evaluate_comparison("a", "eq", "b") is False

    def test_ne_strings(self) -> None:
        assert evaluate_comparison("a", "ne", "b") is True
        assert evaluate_comparison("a", "ne", "a") is False

    def test_eq_ints(self) -> None:
        assert evaluate_comparison(5, "eq", 5) is True
        assert evaluate_comparison(5, "eq", 6) is False

    def test_eq_symbolic(self) -> None:
        assert evaluate_comparison("x", "==", "x") is True
        assert evaluate_comparison("x", "!=", "x") is False

    # --- ordered comparisons ---

    def test_gt_ints(self) -> None:
        assert evaluate_comparison(5, "gt", 3) is True
        assert evaluate_comparison(3, "gt", 5) is False
        assert evaluate_comparison(5, "gt", 5) is False

    def test_gte_ints(self) -> None:
        assert evaluate_comparison(5, "gte", 5) is True
        assert evaluate_comparison(5, "gte", 6) is False

    def test_lt_ints(self) -> None:
        assert evaluate_comparison(3, "lt", 5) is True
        assert evaluate_comparison(5, "lt", 3) is False

    def test_lte_ints(self) -> None:
        assert evaluate_comparison(5, "lte", 5) is True
        assert evaluate_comparison(6, "lte", 5) is False

    def test_ordered_floats(self) -> None:
        assert evaluate_comparison(3.14, ">=", 3.14) is True
        assert evaluate_comparison(2.71, "<", 3.14) is True

    def test_ordered_strings(self) -> None:
        assert evaluate_comparison("apple", "<", "banana") is True
        assert evaluate_comparison("banana", ">", "apple") is True

    # --- incomparable types return False for ordered ops ---

    def test_incomparable_types_ordered(self) -> None:
        assert evaluate_comparison("text", "gt", 5) is False
        assert evaluate_comparison(5, "lt", "text") is False
        assert evaluate_comparison(None, "gte", 5) is False
        assert evaluate_comparison(5, "lte", None) is False

    # --- eq/ne with None ---

    def test_eq_with_none(self) -> None:
        assert evaluate_comparison(None, "eq", None) is True
        assert evaluate_comparison(None, "eq", "a") is False
        assert evaluate_comparison("a", "eq", None) is False

    def test_ne_with_none(self) -> None:
        assert evaluate_comparison(None, "ne", "a") is True
        assert evaluate_comparison("a", "ne", None) is True
        assert evaluate_comparison(None, "ne", None) is False

    # --- mixed numeric types ---

    def test_int_float_comparison(self) -> None:
        assert evaluate_comparison(5, "eq", 5.0) is True
        assert evaluate_comparison(5, "gte", 4.9) is True
        assert evaluate_comparison(5, "lt", 5.1) is True

    # --- unsupported operator ---

    def test_unsupported_op_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported comparison operator"):
            evaluate_comparison(1, "like", 1)


class TestEvaluateTypedComparison:
    def test_none_value_type_preserves_untyped_semantics(self) -> None:
        assert (
            evaluate_typed_comparison(
                "2026-05-17T12:00:00Z",
                "eq",
                "2026-05-17T12:00:00+00:00",
            )
            is False
        )
        assert evaluate_typed_comparison("apple", "<", "banana") is True

    def test_scalar_value_types_coerce_before_comparison(self) -> None:
        assert evaluate_typed_comparison("5", "eq", 5, value_type="int") is True
        assert evaluate_typed_comparison("5.5", "gt", 5, value_type="number") is True
        assert evaluate_typed_comparison("true", "eq", True, value_type="bool") is True
        assert evaluate_typed_comparison(123, "eq", "123", value_type="string") is True

    def test_datetime_comparisons_normalize_utc_inputs(self) -> None:
        assert (
            evaluate_typed_comparison(
                "2026-05-17T12:00:00Z",
                "eq",
                "2026-05-17T12:00:00+00:00",
                value_type="datetime",
            )
            is True
        )
        assert (
            evaluate_typed_comparison(
                "2026-05-17T08:00:00-04:00",
                "eq",
                datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
                value_type="datetime",
            )
            is True
        )
        assert (
            evaluate_typed_comparison(
                datetime(2026, 5, 17, 12, 0),
                "eq",
                datetime(2026, 5, 17, 8, 0, tzinfo=timezone(timedelta(hours=-4))),
                value_type="datetime",
            )
            is True
        )

    def test_date_comparisons(self) -> None:
        assert (
            evaluate_typed_comparison(
                "2026-05-17",
                "before",
                "2026-05-18",
                value_type="date",
            )
            is True
        )
        assert (
            evaluate_typed_comparison(
                date(2026, 5, 17),
                "on_or_before",
                "2026-05-17T23:59:59Z",
                value_type="date",
            )
            is True
        )

    def test_date_operand_validation_accepts_datetime_like_strings(self) -> None:
        validate_typed_predicate_operand("2026-05-17T23:59:59Z", "date")

    def test_temporal_operator_aliases(self) -> None:
        assert (
            evaluate_typed_comparison(
                "2026-05-18T00:00:00Z",
                "after",
                "2026-05-17T23:59:59Z",
                value_type="datetime",
            )
            is True
        )
        assert (
            evaluate_typed_comparison(
                "2026-05-18T00:00:00Z",
                "on_or_after",
                "2026-05-18T00:00:00+00:00",
                value_type="datetime",
            )
            is True
        )

    def test_invalid_typed_values_return_false(self) -> None:
        assert (
            evaluate_typed_comparison(
                "not-a-datetime",
                "before",
                "2026-05-17T12:00:00Z",
                value_type="datetime",
            )
            is False
        )
        assert (
            evaluate_typed_comparison(
                "not-a-date",
                "before",
                "2026-05-17",
                value_type="date",
            )
            is False
        )
        assert evaluate_typed_comparison("nope", "eq", True, value_type="bool") is False

    def test_invalid_typed_coercion_can_raise(self) -> None:
        with pytest.raises(PredicateCoercionError) as exc_info:
            evaluate_typed_comparison(
                "nope",
                "eq",
                True,
                value_type="bool",
                invalid="raise",
            )
        assert exc_info.value.value == "nope"
        assert exc_info.value.value_type == "bool"

    def test_invalid_operator_still_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported comparison operator"):
            evaluate_typed_comparison(
                "not-a-datetime",
                "during",
                "also-not-a-datetime",
                value_type="datetime",
            )


class TestInferPredicateValueType:
    def test_infers_temporal_type_from_left_runtime_value_only(self) -> None:
        assert infer_predicate_value_type(date(2026, 5, 17), "2026-05-18") == "date"
        assert (
            infer_predicate_value_type(
                datetime(2026, 5, 17, 12, tzinfo=timezone.utc),
                "2026-05-18T12:00:00Z",
            )
            == "datetime"
        )
        assert infer_predicate_value_type("2026-05-17", "2026-05-18") is None
        assert (
            infer_predicate_value_type(
                "2026-05-17T12:00:00Z",
                datetime(2026, 5, 18, 12, tzinfo=timezone.utc),
            )
            is None
        )

    def test_infers_scalar_values(self) -> None:
        assert infer_predicate_value_type("5", 5) == "number"
        assert infer_predicate_value_type("true", True) == "bool"

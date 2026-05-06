"""Tests for error class __str__ rendering."""

from __future__ import annotations

from cruxible_core.errors import (
    ConfigError,
    ConstraintViolationError,
    CoreError,
    DataValidationError,
    IngestionError,
    MutationError,
    ReceiptNotFoundError,
)


class TestConfigError:
    def test_str_with_errors(self):
        exc = ConfigError("Config has 2 error(s)", errors=["bad ref A", "bad ref B"])
        text = str(exc)
        assert "Config has 2 error(s)" in text
        assert "bad ref A" in text
        assert "bad ref B" in text

    def test_str_without_errors(self):
        exc = ConfigError("Config file not found")
        assert str(exc) == "Config file not found"

    def test_errors_list_preserved(self):
        exc = ConfigError("msg", errors=["a", "b"])
        assert exc.errors == ["a", "b"]
        assert exc.summary == "msg"


class TestDataValidationError:
    def test_str_with_errors(self):
        exc = DataValidationError("Validation failed", errors=["missing col X"])
        text = str(exc)
        assert "Validation failed" in text
        assert "missing col X" in text

    def test_str_without_errors(self):
        exc = DataValidationError("Generic failure")
        assert str(exc) == "Generic failure"


class TestConstraintViolationError:
    def test_str_with_violations(self):
        exc = ConstraintViolationError("2 violations", violations=["rule1", "rule2"])
        text = str(exc)
        assert "2 violations" in text
        assert "rule1" in text
        assert "rule2" in text

    def test_str_without_violations(self):
        exc = ConstraintViolationError("No details")
        assert str(exc) == "No details"


class TestErrorMessageCapping:
    """Verify __str__ caps output at 10 errors for large lists."""

    def test_data_validation_error_caps_display(self):
        errors = [f"error {i}" for i in range(50)]
        exc = DataValidationError("Validation failed", errors=errors)
        msg = str(exc)
        assert "error 0" in msg
        assert "error 9" in msg
        assert "error 10" not in msg
        assert "and 40 more error(s)" in msg

    def test_data_validation_error_preserves_full_list(self):
        errors = [f"error {i}" for i in range(50)]
        exc = DataValidationError("Validation failed", errors=errors)
        assert len(exc.errors) == 50

    def test_config_error_caps_display(self):
        errors = [f"config error {i}" for i in range(25)]
        exc = ConfigError("Config invalid", errors=errors)
        msg = str(exc)
        assert "config error 0" in msg
        assert "config error 9" in msg
        assert "config error 10" not in msg
        assert "and 15 more error(s)" in msg

    def test_constraint_violation_error_caps_display(self):
        violations = [f"violation {i}" for i in range(25)]
        exc = ConstraintViolationError("Constraints failed", violations=violations)
        msg = str(exc)
        assert "violation 0" in msg
        assert "violation 9" in msg
        assert "violation 10" not in msg
        assert "and 15 more error(s)" in msg

    def test_small_error_list_no_cap(self):
        errors = [f"error {i}" for i in range(5)]
        exc = DataValidationError("Validation failed", errors=errors)
        msg = str(exc)
        for i in range(5):
            assert f"error {i}" in msg
        assert "more error(s)" not in msg

    def test_exactly_10_errors_no_cap(self):
        errors = [f"e{i}" for i in range(10)]
        exc = ConfigError("Summary", errors=errors)
        msg = str(exc)
        for i in range(10):
            assert f"e{i}" in msg
        assert "more error(s)" not in msg


class TestMutationReceiptId:
    def test_core_error_with_receipt_id(self):
        exc = CoreError("msg", mutation_receipt_id="RCP-xxx")
        assert exc.mutation_receipt_id == "RCP-xxx"
        assert "(receipt: RCP-xxx)" in str(exc)

    def test_core_error_without_receipt_id(self):
        exc = CoreError("msg")
        assert exc.mutation_receipt_id is None
        assert "(receipt:" not in str(exc)

    def test_config_error_with_receipt_id(self):
        exc = ConfigError("msg", ["err1"], mutation_receipt_id="RCP-xxx")
        assert "(receipt: RCP-xxx)" in str(exc)

    def test_config_error_no_errors_with_receipt_id(self):
        exc = ConfigError("msg", mutation_receipt_id="RCP-xxx")
        assert str(exc) == "msg (receipt: RCP-xxx)"

    def test_data_validation_error_with_receipt_id(self):
        exc = DataValidationError("msg", ["err1"], mutation_receipt_id="RCP-xxx")
        assert "(receipt: RCP-xxx)" in str(exc)

    def test_constraint_violation_with_receipt_id(self):
        exc = ConstraintViolationError("msg", ["v1"], mutation_receipt_id="RCP-xxx")
        assert "(receipt: RCP-xxx)" in str(exc)

    def test_ingestion_error_inherits_receipt_id(self):
        exc = IngestionError("fail", mutation_receipt_id="RCP-yyy")
        assert exc.mutation_receipt_id == "RCP-yyy"
        assert "(receipt: RCP-yyy)" in str(exc)

    def test_mutation_error_inherits_receipt_id(self):
        exc = MutationError("fail", mutation_receipt_id="RCP-zzz")
        assert exc.mutation_receipt_id == "RCP-zzz"

    def test_receipt_not_found_no_collision(self):
        exc = ReceiptNotFoundError("RCP-old")
        assert exc.receipt_id == "RCP-old"
        assert exc.mutation_receipt_id is None

    def test_post_construction_mutation(self):
        exc = CoreError("msg")
        exc.mutation_receipt_id = "RCP-zzz"
        assert "(receipt: RCP-zzz)" in str(exc)

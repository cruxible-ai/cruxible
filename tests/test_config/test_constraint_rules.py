"""Tests for the shared constraint rule parser."""

from cruxible_core.config.constraint_rules import ParsedConstraintRule, parse_constraint_rule


class TestParseConstraintRule:
    def test_valid_rule(self):
        result = parse_constraint_rule("replaces.FROM.category == replaces.TO.category")
        assert result == ParsedConstraintRule("replaces", "category", "==", "category")

    def test_valid_rule_different_props(self):
        result = parse_constraint_rule(
            "classified_as.FROM.Category == classified_as.TO.CategoryName"
        )
        assert result == ParsedConstraintRule(
            "classified_as", "Category", "==", "CategoryName"
        )

    def test_valid_rule_with_spaces(self):
        result = parse_constraint_rule("fits.FROM.make  ==  fits.TO.make")
        assert result == ParsedConstraintRule("fits", "make", "==", "make")

    def test_valid_rule_not_equals(self):
        result = parse_constraint_rule("fits.FROM.make != fits.TO.make")
        assert result == ParsedConstraintRule("fits", "make", "!=", "make")

    def test_valid_rule_ordered_operator(self):
        result = parse_constraint_rule("fits.FROM.priority >= fits.TO.priority")
        assert result == ParsedConstraintRule("fits", "priority", ">=", "priority")

    def test_valid_rule_temporal_alias(self):
        result = parse_constraint_rule("fits.FROM.published_at before fits.TO.expires_at")
        assert result == ParsedConstraintRule(
            "fits",
            "published_at",
            "before",
            "expires_at",
        )

    def test_invalid_garbage(self):
        assert parse_constraint_rule("not a valid rule") is None

    def test_invalid_empty_string(self):
        assert parse_constraint_rule("") is None

    def test_invalid_partial_match(self):
        assert parse_constraint_rule("fits.FROM.make") is None

    def test_invalid_mismatched_relationship(self):
        # Backreference requires same relationship on both sides
        assert parse_constraint_rule("fits.FROM.make == replaces.TO.make") is None

    def test_hyphens_in_identifiers(self):
        result = parse_constraint_rule(
            "classified-as.FROM.sub-category == classified-as.TO.sub-category"
        )
        assert result == ParsedConstraintRule(
            "classified-as", "sub-category", "==", "sub-category"
        )

    def test_underscores_in_identifiers(self):
        result = parse_constraint_rule("has_part.FROM.part_type == has_part.TO.part_type")
        assert result == ParsedConstraintRule("has_part", "part_type", "==", "part_type")

    def test_digits_in_identifiers(self):
        result = parse_constraint_rule("rel1.FROM.prop2 == rel1.TO.prop3")
        assert result == ParsedConstraintRule("rel1", "prop2", "==", "prop3")

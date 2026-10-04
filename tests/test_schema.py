"""Schema validator tests: every supported keyword, accept and reject paths."""

from __future__ import annotations

import unittest

from harness.errors import ValidationError
from harness.schema import assert_valid, coerce, is_integer, is_number, normalize, summarize, validate

from .support import TempDirTestCase  # noqa: F401  (imported for discoverability symmetry)


class TestTypes(unittest.TestCase):
    def test_object_and_array_and_string(self) -> None:
        self.assertEqual(validate({"a": 1}, {"type": "object"}), [])
        self.assertEqual(validate([1], {"type": "array"}), [])
        self.assertEqual(validate("x", {"type": "string"}), [])
        self.assertTrue(validate(1, {"type": "object"}))

    def test_type_union_accepts_either(self) -> None:
        schema = {"type": ["string", "null"]}
        self.assertEqual(validate("a", schema), [])
        self.assertEqual(validate(None, schema), [])
        self.assertTrue(validate(3, schema))

    def test_unknown_declared_type_is_reported(self) -> None:
        errors = validate("a", {"type": "strng"})
        self.assertTrue(errors)
        self.assertIn("unknown type", errors[0])


class TestBoolIsNotANumber(unittest.TestCase):
    """`isinstance(True, int)` is True; JSON Schema says `true` is not a number."""

    def test_integer_rejects_bool(self) -> None:
        errors = validate(True, {"type": "integer"})
        self.assertTrue(errors, "True must not satisfy type=integer")
        self.assertIn("boolean", errors[0])

    def test_number_rejects_bool(self) -> None:
        self.assertTrue(validate(False, {"type": "number"}))

    def test_integer_accepts_int_and_rejects_float(self) -> None:
        self.assertEqual(validate(3, {"type": "integer"}), [])
        self.assertTrue(validate(3.5, {"type": "integer"}))

    def test_number_accepts_int_and_float(self) -> None:
        self.assertEqual(validate(3, {"type": "number"}), [])
        self.assertEqual(validate(3.5, {"type": "number"}), [])

    def test_boolean_type_still_works(self) -> None:
        self.assertEqual(validate(True, {"type": "boolean"}), [])
        self.assertTrue(validate(1, {"type": "boolean"}))

    def test_helpers_agree(self) -> None:
        self.assertFalse(is_integer(True))
        self.assertFalse(is_number(True))
        self.assertTrue(is_integer(2))
        self.assertTrue(is_number(2.5))

    def test_bool_does_not_trip_range_checks(self) -> None:
        # minimum/maximum are skipped for bools; otherwise True would fail minimum=5
        # with a confusing message instead of the type error.
        errors = validate(True, {"type": "integer", "minimum": 5})
        self.assertEqual(len(errors), 1)
        self.assertIn("expected integer", errors[0])


class TestObjects(unittest.TestCase):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
        "additionalProperties": False,
    }

    def test_valid(self) -> None:
        self.assertEqual(validate({"a": "x", "b": 2}, self.schema), [])

    def test_missing_required(self) -> None:
        errors = validate({"b": 2}, self.schema)
        self.assertTrue(any("required" in e for e in errors))

    def test_additional_property_rejected(self) -> None:
        errors = validate({"a": "x", "c": 1}, self.schema)
        self.assertTrue(any("additional properties" in e for e in errors))

    def test_nested_path_in_message(self) -> None:
        errors = validate({"a": 1}, self.schema)
        self.assertTrue(any(e.startswith("$.a") for e in errors), errors)

    def test_additional_properties_as_schema(self) -> None:
        schema = {"type": "object", "additionalProperties": {"type": "integer"}}
        self.assertEqual(validate({"anything": 1}, schema), [])
        self.assertTrue(validate({"anything": "no"}, schema))

    def test_array_item_errors_are_indexed(self) -> None:
        schema = {"type": "object", "properties": {"xs": {"type": "array", "items": {"type": "integer"}}}}
        errors = validate({"xs": [1, "two"]}, schema)
        self.assertTrue(any("$.xs[1]" in e for e in errors), errors)


class TestScalars(unittest.TestCase):
    def test_enum(self) -> None:
        schema = {"enum": ["a", "b"]}
        self.assertEqual(validate("a", schema), [])
        self.assertTrue(validate("c", schema))

    def test_enum_is_type_strict_for_bool_vs_int(self) -> None:
        self.assertTrue(validate(True, {"enum": [1, 2]}))
        self.assertTrue(validate(1, {"enum": [True]}))

    def test_const(self) -> None:
        self.assertEqual(validate(7, {"const": 7}), [])
        self.assertTrue(validate(8, {"const": 7}))

    def test_numeric_range(self) -> None:
        schema = {"type": "number", "minimum": 1, "maximum": 5}
        self.assertEqual(validate(3, schema), [])
        self.assertTrue(validate(0.5, schema))
        self.assertTrue(validate(5.5, schema))

    def test_exclusive_range(self) -> None:
        schema = {"type": "number", "exclusiveMinimum": 1, "exclusiveMaximum": 5}
        self.assertTrue(validate(1, schema))
        self.assertTrue(validate(5, schema))
        self.assertEqual(validate(3, schema), [])

    def test_string_length_and_pattern(self) -> None:
        self.assertTrue(validate("", {"type": "string", "minLength": 1}))
        self.assertTrue(validate("abcd", {"type": "string", "maxLength": 3}))
        self.assertEqual(validate("abc", {"type": "string", "pattern": "^a.c$"}), [])
        self.assertTrue(validate("abd", {"type": "string", "pattern": "^a.c$"}))

    def test_array_lengths(self) -> None:
        self.assertTrue(validate([], {"type": "array", "minItems": 1}))
        self.assertTrue(validate([1, 2], {"type": "array", "maxItems": 1}))
        self.assertEqual(validate([1], {"type": "array", "minItems": 1, "maxItems": 1}), [])


class TestAssertAndSummarize(unittest.TestCase):
    def test_assert_valid_raises_with_message(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            assert_valid({"a": 1}, {"type": "object", "properties": {"a": {"type": "string"}}})
        self.assertIn("invalid arguments", str(caught.exception))

    def test_assert_valid_passes(self) -> None:
        assert_valid({"a": "x"}, {"type": "object", "properties": {"a": {"type": "string"}}})

    def test_summarize_marks_optional(self) -> None:
        rendered = summarize(
            {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}, "required": ["a"]}
        )
        self.assertIn("a:string", rendered)
        self.assertIn("b?:integer", rendered)


class TestCoercion(unittest.TestCase):
    def test_json_string_to_array(self) -> None:
        self.assertEqual(coerce("[]", {"type": "array"}), [])

    def test_json_string_to_object(self) -> None:
        self.assertEqual(coerce('{"a": 1}', {"type": "object", "properties": {"a": {"type": "integer"}}}), {"a": 1})

    def test_string_to_integer(self) -> None:
        self.assertEqual(coerce("30", {"type": "integer"}), 30)

    def test_uncoercible_string_is_left_alone(self) -> None:
        self.assertEqual(coerce("thirty", {"type": "integer"}), "thirty")

    def test_normalize_then_validate(self) -> None:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        self.assertEqual(normalize({"n": "5"}, schema), {"n": 5})

    def test_normalize_raises_when_still_invalid(self) -> None:
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        with self.assertRaises(ValidationError):
            normalize({"n": "five"}, schema)

    def test_nested_coercion_in_arrays(self) -> None:
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertEqual(coerce(["1", "2"], schema), [1, 2])


if __name__ == "__main__":
    unittest.main()

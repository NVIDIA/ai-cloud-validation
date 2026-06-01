# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generic validations for step outputs.

These validations work with any step output and provide basic field checking,
schema validation, and common success/failure patterns.
"""

from typing import Any, ClassVar

from isvtest.core.validation import UNMAPPED, BaseValidation


class FieldExistsCheck(BaseValidation):
    """Check that required fields exist in step output.

    Config:
        step_output: The step output to check
        fields: List of field names that must exist
        field: Single field name (alternative to fields)
    """

    description: ClassVar[str] = "Check required fields exist in output"
    test_ids: ClassVar[tuple[str, ...]] = (UNMAPPED,)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        fields = self.config.get("fields", [])

        # Support single field
        single_field = self.config.get("field")
        if single_field and not fields:
            fields = [single_field]

        if not fields:
            self.set_failed("No 'fields' or 'field' specified")
            return

        missing = [f for f in fields if f not in step_output]

        if missing:
            self.set_failed(f"Missing fields: {', '.join(missing)}")
        else:
            self.set_passed(f"All required fields present: {', '.join(fields)}")


def _get_field_value(step_output: dict[str, Any], field: str) -> tuple[bool, Any]:
    """Return a top-level or dotted-path field from step output."""
    if field in step_output:
        return True, step_output[field]

    current: Any = step_output
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


class FieldValueCheck(BaseValidation):
    """Check that a field has an expected value.

    Config:
        step_output: The step output to check
        field: Field name to check
        expected: Expected value (exact match)
        contains: Value should contain this substring (for strings)
        min: Minimum value (for numbers)
        max: Maximum value (for numbers)
        operator: Comparison operator (eq, gt, gte, lt, lte)
    """

    description: ClassVar[str] = "Check field has expected value"
    test_ids: ClassVar[tuple[str, ...]] = (UNMAPPED,)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        field = self.config.get("field")

        if not field:
            self.set_failed("No 'field' specified")
            return

        found, actual = _get_field_value(step_output, field)
        if not found:
            self.set_failed(f"Field '{field}' not found in output")
            return

        # Check exact match
        expected = self.config.get("expected")
        if expected is not None:
            operator = self.config.get("operator", "eq")
            if self._compare(actual, expected, operator):
                self.set_passed(f"{field}={actual}")
            else:
                self.set_failed(f"{field}: expected {operator} {expected}, got {actual}")
            return

        # Check contains (for strings)
        contains = self.config.get("contains")
        if contains is not None:
            if isinstance(actual, str) and contains in actual:
                self.set_passed(f"{field} contains '{contains}'")
            else:
                self.set_failed(f"{field} does not contain '{contains}': {actual}")
            return

        # Check min/max (for numbers)
        min_val = self.config.get("min")
        max_val = self.config.get("max")

        if min_val is not None or max_val is not None:
            try:
                num_actual = float(actual)
                if min_val is not None and num_actual < min_val:
                    self.set_failed(f"{field}={num_actual} < min {min_val}")
                    return
                if max_val is not None and num_actual > max_val:
                    self.set_failed(f"{field}={num_actual} > max {max_val}")
                    return
                self.set_passed(f"{field}={num_actual} within range")
            except (ValueError, TypeError):
                self.set_failed(f"{field}={actual} is not a number")
            return

        # No check specified
        self.set_passed(f"{field}={actual}")

    def _compare(self, actual: object, expected: object, operator: str) -> bool:
        """Compare values using the specified operator."""
        if operator == "eq":
            return actual == expected
        try:
            actual_num = float(actual)  # type: ignore[arg-type]
            expected_num = float(expected)  # type: ignore[arg-type]
            if operator == "gt":
                return actual_num > expected_num
            if operator == "gte":
                return actual_num >= expected_num
            if operator == "lt":
                return actual_num < expected_num
            if operator == "lte":
                return actual_num <= expected_num
        except (ValueError, TypeError):
            pass
        return actual == expected


class SchemaValidation(BaseValidation):
    """Validate that step output matches expected schema.

    This validation is typically run automatically by StepExecutor,
    but can also be used explicitly for custom schema validation.

    Config:
        step_output: The step output to validate
        schema: Schema name to validate against
    """

    description: ClassVar[str] = "Validate output matches JSON schema"
    test_ids: ClassVar[tuple[str, ...]] = (UNMAPPED,)
    catalog_exclude: ClassVar[bool] = True

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        schema_name = self.config.get("schema")

        if not schema_name:
            self.set_failed("No 'schema' specified")
            return

        try:
            from isvctl.config.output_schemas import validate_output

            is_valid, errors = validate_output(step_output, schema_name)

            if is_valid:
                self.set_passed(f"Output matches '{schema_name}' schema")
            else:
                self.set_failed(f"Schema validation failed: {'; '.join(errors)}")

        except ImportError:
            self.set_failed("Could not import output_schemas module")
        except ValueError as e:
            self.set_failed(str(e))


class StepSuccessCheck(BaseValidation):
    """Validate that a step completed successfully.

    Checks the step output for success indicators:
    - 'success': true (boolean) - most common
    - 'status': "passed" or "skipped" - alternative

    Config:
        step_output: The step output to check (or use step)
    """

    description: ClassVar[str] = "Check step completed successfully"
    test_ids: ClassVar[tuple[str, ...]] = (UNMAPPED,)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        # Check success field first (most common)
        success = step_output.get("success")
        if success is True:
            message = step_output.get("message", "Step completed successfully")
            self.set_passed(message)
            return
        if success is False:
            error_type = step_output.get("error_type", "")
            error = step_output.get("error", step_output.get("message", "Unknown error"))
            if error_type:
                self.set_failed(f"Step failed [{error_type}]: {error}")
            else:
                self.set_failed(f"Step failed: {error}")
            return

        # Check status field as fallback
        status = step_output.get("status")
        if status == "passed":
            message = step_output.get("message", "Step completed successfully")
            self.set_passed(message)
        elif status == "skipped":
            self.set_passed("Step skipped")
        elif status:
            error = step_output.get("error", step_output.get("logs", ""))[:500]
            self.set_failed(f"Step failed: {error}" if error else "Step failed")
        else:
            self.set_failed("No 'success' or 'status' in step output")


def check_operations_passed(ops: dict[str, Any], expected: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Check which operations in an operations dict passed or failed.

    Args:
        ops: Dict of operation name -> {"passed": bool, ...}
        expected: List of operation names to check (defaults to all keys)

    Returns:
        Tuple of (passed_names, failed_descriptions)
    """
    if expected is None:
        expected = list(ops.keys())

    failed = []
    passed = []
    for op_name in expected:
        op = ops.get(op_name, {})
        if op.get("passed"):
            passed.append(op_name)
        else:
            error = op.get("error", "not passed")
            failed.append(f"{op_name}: {error}")

    return passed, failed


class CrudOperationsCheck(BaseValidation):
    """Validate that all CRUD operations in a step output passed.

    Checks the ``operations`` dict in step output and verifies each
    expected operation has ``passed: true``.

    Config:
        step_output: The step output containing an ``operations`` dict
        operations: List of operation names to check (e.g. ["get", "list", "create", "delete"])
    """

    description: ClassVar[str] = "Check all CRUD operations passed"
    test_ids: ClassVar[tuple[str, ...]] = (UNMAPPED,)

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        expected_ops = self.config.get("operations", [])

        ops = step_output.get("operations")
        if not isinstance(ops, dict):
            self.set_failed("No 'operations' dict in step output")
            return

        passed, failed = check_operations_passed(ops, expected_ops or None)

        if failed:
            self.set_failed(f"CRUD operations failed: {'; '.join(failed)}")
        else:
            self.set_passed(f"All CRUD operations passed: {', '.join(passed)}")

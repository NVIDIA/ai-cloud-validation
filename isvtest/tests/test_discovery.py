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

"""Tests for discovery module."""

from isvtest.core.discovery import _is_reframe_test


class TestIsReframeTest:
    """Tests for _is_reframe_test function."""

    def test_class_with_rfm_attribute_is_reframe_test(self) -> None:
        """Test that class with _rfm_regression_class_kind is detected."""

        class ReframeTest:
            _rfm_regression_class_kind = "simple"

        assert _is_reframe_test(ReframeTest) is True

    def test_class_without_rfm_attribute_is_not_reframe_test(self) -> None:
        """Test that class without attribute is not detected."""

        class RegularClass:
            pass

        assert _is_reframe_test(RegularClass) is False

    def test_class_with_none_rfm_attribute(self) -> None:
        """Test that class with None _rfm_regression_class_kind is still detected."""

        class ReframeTestNone:
            _rfm_regression_class_kind = None

        # hasattr returns True even if value is None
        assert _is_reframe_test(ReframeTestNone) is True

    def test_class_with_empty_string_rfm_attribute(self) -> None:
        """Test that class with empty string attribute is still detected."""

        class ReframeTestEmpty:
            _rfm_regression_class_kind = ""

        assert _is_reframe_test(ReframeTestEmpty) is True

    def test_subclass_inherits_detection(self) -> None:
        """Test that subclasses inherit the attribute detection."""

        class BaseReframeTest:
            _rfm_regression_class_kind = "simple"

        class DerivedTest(BaseReframeTest):
            pass

        assert _is_reframe_test(DerivedTest) is True

    def test_regular_validation_class(self) -> None:
        """Test that regular validation classes are not detected."""
        from isvtest.core.validation import BaseValidation

        class MyValidation(BaseValidation):
            def run(self) -> None:
                pass

        assert _is_reframe_test(MyValidation) is False

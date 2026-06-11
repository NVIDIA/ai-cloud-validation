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

"""Tests for configuration module."""

import os
from unittest.mock import patch

from isvreporter.config import get_endpoint, get_ssa_issuer


class TestGetEndpoint:
    """Tests for get_endpoint function."""

    def test_returns_endpoint_from_env(self) -> None:
        """Test that endpoint is returned from environment variable."""
        with patch.dict(os.environ, {"ISV_SERVICE_ENDPOINT": "https://example.com/api"}):
            assert get_endpoint() == "https://example.com/api"

    def test_returns_empty_string_when_not_set(self) -> None:
        """Test that empty string is returned when env var is not set."""
        with patch.dict(os.environ, {}, clear=True):
            assert get_endpoint() == ""


class TestGetSsaIssuer:
    """Tests for get_ssa_issuer function."""

    def test_returns_issuer_from_env(self) -> None:
        """Test that SSA issuer is returned from environment variable."""
        with patch.dict(os.environ, {"ISV_SSA_ISSUER": "https://example.com/ssa"}):
            assert get_ssa_issuer() == "https://example.com/ssa"

    def test_returns_empty_string_when_not_set(self) -> None:
        """Test that empty string is returned when env var is not set."""
        with patch.dict(os.environ, {}, clear=True):
            assert get_ssa_issuer() == ""

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

"""Unit tests for utility functions."""

import logging

from isvtest.config.settings import Settings
from isvtest.core.logger import setup_logger


def test_logger_setup() -> None:
    """Test logger configuration."""
    logger = setup_logger("test_logger")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "test_logger"


def test_logger_no_duplicate_handlers() -> None:
    """Test that calling setup_logger multiple times doesn't create duplicate handlers."""
    # Call setup_logger multiple times with the same name
    logger1 = setup_logger("test_duplicate_logger")
    initial_handler_count = len(logger1.handlers)

    logger2 = setup_logger("test_duplicate_logger")
    logger3 = setup_logger("test_duplicate_logger")

    # All should return the same logger instance
    assert logger1 is logger2
    assert logger2 is logger3

    # Handler count should remain the same (no duplicates)
    assert len(logger3.handlers) == initial_handler_count
    assert len(logger3.handlers) == 1


def test_settings_defaults() -> None:
    """Test default settings values."""
    settings = Settings()
    assert settings.validation_timeout == 300
    assert settings.log_level == "INFO"
    assert settings.reframe_path == "workloads"

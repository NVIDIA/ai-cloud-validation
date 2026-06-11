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

"""Tests for logger module."""

import logging
import sys

from isvtest.core.logger import setup_logger


class TestSetupLogger:
    """Tests for setup_logger function."""

    def test_returns_logger_instance(self) -> None:
        """Test that setup_logger returns a Logger instance."""
        logger = setup_logger("test_logger_1")
        assert isinstance(logger, logging.Logger)

    def test_uses_provided_name(self) -> None:
        """Test that logger uses the provided name."""
        logger = setup_logger("my_custom_name")
        assert logger.name == "my_custom_name"

    def test_default_name_is_isvtest(self) -> None:
        """Test that default logger name is 'isvtest'."""
        logger = setup_logger()
        assert logger.name == "isvtest"

    def test_default_level_is_info(self) -> None:
        """Test that default logging level is INFO."""
        logger = setup_logger("test_logger_2")
        assert logger.level == logging.INFO

    def test_custom_level(self) -> None:
        """Test that custom logging level is applied."""
        logger = setup_logger("test_logger_3", level=logging.DEBUG)
        assert logger.level == logging.DEBUG

        logger = setup_logger("test_logger_4", level=logging.WARNING)
        assert logger.level == logging.WARNING

    def test_propagate_is_false(self) -> None:
        """Test that logger propagate is set to False."""
        logger = setup_logger("test_logger_5")
        assert logger.propagate is False

    def test_handler_is_added(self) -> None:
        """Test that a handler is added to the logger."""
        logger = setup_logger("test_logger_6")
        assert len(logger.handlers) >= 1

    def test_handler_is_stream_handler(self) -> None:
        """Test that the handler is a StreamHandler."""
        logger = setup_logger("test_logger_7")
        # Find the handler added by setup_logger
        stream_handlers = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream_handlers) >= 1

    def test_handler_writes_to_stderr(self) -> None:
        """Diagnostics must go to stderr, leaving stdout for machine-readable output."""
        logger = setup_logger("test_logger_stderr")
        stream_handlers = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]
        assert stream_handlers
        assert stream_handlers[0].stream is sys.stderr

    def test_no_duplicate_handlers(self) -> None:
        """Test that calling setup_logger twice doesn't add duplicate handlers."""
        logger1 = setup_logger("test_logger_8")
        initial_count = len(logger1.handlers)

        logger2 = setup_logger("test_logger_8")
        final_count = len(logger2.handlers)

        assert logger1 is logger2
        assert initial_count == final_count

    def test_formatter_is_set(self) -> None:
        """Test that formatter is configured on the handler."""
        logger = setup_logger("test_logger_9")
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                formatter = handler.formatter
                assert formatter is not None
                # Check that format includes expected fields
                format_str = formatter._fmt
                assert "%(asctime)s" in format_str
                assert "%(name)s" in format_str
                assert "%(levelname)s" in format_str
                assert "%(message)s" in format_str
                break

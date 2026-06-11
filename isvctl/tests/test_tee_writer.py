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

"""Tests for TeeWriter class."""

from io import StringIO
from unittest.mock import MagicMock

from isvctl.cli.test import TeeWriter


class TestTeeWriter:
    """Tests for TeeWriter class."""

    def test_write_outputs_to_both_streams(self) -> None:
        """Test that write() outputs to both terminal and file."""
        terminal = StringIO()
        file = StringIO()
        tee = TeeWriter(terminal, file)

        result = tee.write("Hello, world!")

        assert terminal.getvalue() == "Hello, world!"
        assert file.getvalue() == "Hello, world!"
        assert result == len("Hello, world!")

    def test_write_returns_length(self) -> None:
        """Test that write() returns the number of characters written."""
        terminal = StringIO()
        file = StringIO()
        tee = TeeWriter(terminal, file)

        result = tee.write("test")

        assert result == 4

    def test_writelines_outputs_all_lines(self) -> None:
        """Test that writelines() outputs all lines to both streams."""
        terminal = StringIO()
        file = StringIO()
        tee = TeeWriter(terminal, file)

        tee.writelines(["line1\n", "line2\n", "line3\n"])

        expected = "line1\nline2\nline3\n"
        assert terminal.getvalue() == expected
        assert file.getvalue() == expected

    def test_flush_flushes_both_streams(self) -> None:
        """Test that flush() flushes both streams."""
        terminal = MagicMock()
        file = MagicMock()
        tee = TeeWriter(terminal, file)

        tee.flush()

        terminal.flush.assert_called_once()
        file.flush.assert_called_once()

    def test_isatty_returns_terminal_isatty(self) -> None:
        """Test that isatty() returns terminal's isatty value."""
        terminal = MagicMock()
        file = MagicMock()

        # Test when terminal is a TTY
        terminal.isatty.return_value = True
        tee = TeeWriter(terminal, file)
        assert tee.isatty() is True

        # Test when terminal is not a TTY
        terminal.isatty.return_value = False
        assert tee.isatty() is False

    def test_empty_write(self) -> None:
        """Test that empty write() works correctly."""
        terminal = StringIO()
        file = StringIO()
        tee = TeeWriter(terminal, file)

        result = tee.write("")

        assert result == 0
        assert terminal.getvalue() == ""
        assert file.getvalue() == ""

    def test_multiple_writes(self) -> None:
        """Test multiple sequential writes."""
        terminal = StringIO()
        file = StringIO()
        tee = TeeWriter(terminal, file)

        tee.write("first ")
        tee.write("second ")
        tee.write("third")

        expected = "first second third"
        assert terminal.getvalue() == expected
        assert file.getvalue() == expected

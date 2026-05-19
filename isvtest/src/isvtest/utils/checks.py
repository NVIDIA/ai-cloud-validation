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

"""Common utility functions for validation checks."""

import shutil
from pathlib import Path


def stub_exists(stub_path: str) -> bool:
    """Check if a stub script exists and is a file.

    Args:
        stub_path: Path to the stub script (relative or absolute).

    Returns:
        True if the stub exists and is a file, False otherwise.
    """
    path = Path(stub_path)
    return path.exists() and path.is_file()


def command_exists(command: str) -> bool:
    """Check if a command is available in PATH.

    Args:
        command: Name of the command to check (e.g., 'kubectl', 'sinfo').

    Returns:
        True if the command is available, False otherwise.
    """
    return shutil.which(command) is not None


def truncate(text: str, *, limit: int = 80) -> str:
    """Return ``text`` shortened to at most ``limit`` characters with an ellipsis marker."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

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

"""Shared constants and helpers for CLI subcommands."""

from pathlib import Path

OUTPUT_DIR_NAME = "_output"


def get_output_dir(root: Path | None = None) -> Path:
    """Return the output directory, creating it if needed.

    Args:
        root: Base directory. Defaults to cwd when None.

    Returns:
        Path to the output directory (already created on disk).
    """
    base = root or Path.cwd()
    output_dir = base / OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir

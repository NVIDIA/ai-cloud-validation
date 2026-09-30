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

"""Tests for removing stale source trees on the deploy target."""

from isvctl.cli.deploy import DEFAULT_ARCHIVE_PATHS, REMOTE_REPLACED_DIRS, _clear_replaced_dirs_script


def test_every_replaced_dir_is_restored_by_the_archive() -> None:
    """A removed tree the archive doesn't ship would leave the target without it."""
    for replaced in REMOTE_REPLACED_DIRS:
        assert any(replaced.startswith(path) for path in DEFAULT_ARCHIVE_PATHS if path.endswith("/")), replaced


def test_provider_dirs_are_never_removed() -> None:
    """Provider dirs can hold Terraform state created on the target."""
    assert not any(d.startswith("isvctl/configs/providers") or d == "isvctl" for d in REMOTE_REPLACED_DIRS)


def test_script_removes_each_source_tree() -> None:
    """Modules deleted locally (e.g. a renamed validation) must not be auto-imported remotely."""
    script = _clear_replaced_dirs_script()
    assert script.startswith("for dir in isvtest/src isvreporter/src isvctl/src isvctl/configs/suites; do")
    assert 'rm -rf "$dir"' in script


def test_script_stops_the_deploy_when_a_tree_cannot_be_removed() -> None:
    """A tree that survives cleanup would run stale code, so the deploy must stop."""
    script = _clear_replaced_dirs_script()
    assert 'rm -rf "$dir" || { echo "Failed to remove $dir" >&2; exit 1; }' in script

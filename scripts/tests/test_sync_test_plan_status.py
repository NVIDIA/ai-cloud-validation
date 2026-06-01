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

"""Tests for the pure text transform in sync_test_plan_status.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "sync_test_plan_status", Path(__file__).resolve().parent.parent / "sync_test_plan_status.py"
)
assert _spec and _spec.loader
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


def test_collect_issue_numbers() -> None:
    """Every #N reference is collected."""
    text = 'github_issues:\n  - "#37 (closed)"\n  - "#38 (open)"\n'
    assert sync.collect_issue_numbers(text) == {37, 38}


def test_apply_states_flips_stale_annotations() -> None:
    """Annotations are rewritten to match live state; changes are reported."""
    text = '  - "#37 (open)"\n  - "#38 (closed)"\n'
    new_text, changes = sync.apply_states(text, {37: "closed", 38: "closed"})

    assert "#37 (closed)" in new_text
    assert changes == [(37, "open", "closed")]


def test_apply_states_noop_when_in_sync() -> None:
    """No changes when annotations already match live state."""
    text = '  - "#37 (closed)"\n'
    new_text, changes = sync.apply_states(text, {37: "closed"})
    assert new_text == text
    assert changes == []


def test_apply_states_ignores_unknown_issue() -> None:
    """Issues missing from the fetched states are left untouched."""
    text = '  - "#999 (open)"\n'
    new_text, changes = sync.apply_states(text, {})
    assert new_text == text
    assert changes == []

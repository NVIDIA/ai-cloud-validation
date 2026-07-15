# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared result-verdict helpers for GCP provider scripts."""

from __future__ import annotations

from typing import Any


def preserve_success_after_cleanup(result: dict[str, Any]) -> bool:
    """Keep an established verdict successful only when cleanup also succeeded.

    Callers establish ``result["success"]`` inside their protected normal path.
    Exception handlers leave or reset it to false. Cleanup may only demote that
    verdict; it must never recompute subtests and promote a caught failure.
    """
    success = bool(result.get("success")) and not bool(result.get("cleanup_errors"))
    result["success"] = success
    return success

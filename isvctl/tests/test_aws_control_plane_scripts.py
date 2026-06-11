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

"""Tests for AWS control-plane reference scripts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
AWS_CONTROL_PLANE_SCRIPTS = ISVCTL_ROOT / "configs" / "providers" / "aws" / "scripts" / "control-plane"


def _load_control_plane_script(script_name: str) -> ModuleType:
    """Load an AWS control-plane script as a module for direct helper testing."""
    script_path = AWS_CONTROL_PLANE_SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(f"test_{script_path.stem}", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeS3LifecycleClient:
    """Fake S3 client covering successful object lifecycle calls."""

    def __init__(self) -> None:
        """Initialize fake object storage."""
        self.objects: dict[tuple[str, str], bytes] = {}

    def create_bucket(self, **kwargs: Any) -> None:
        """Accept bucket creation calls."""

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:
        """Store the uploaded object body."""
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        """Return the uploaded object body."""
        return {"Body": FakeStreamingBody(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket: str, Key: str) -> None:
        """Delete the uploaded object."""
        del self.objects[(Bucket, Key)]


class FakeStreamingBody:
    """Small fake for a boto3 streaming body."""

    def __init__(self, body: bytes) -> None:
        """Store the response body."""
        self.body = body

    def read(self) -> bytes:
        """Return the response body."""
        return self.body


def test_s3_object_lifecycle_cleanup_error_fails_step(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A leaked temp bucket makes the S3 lifecycle step fail."""
    module = _load_control_plane_script("s3_object_lifecycle.py")
    fake_s3 = FakeS3LifecycleClient()
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: fake_s3)
    monkeypatch.setattr(module, "_delete_bucket_best_effort", lambda *args, **kwargs: "DeleteBucket failed")
    monkeypatch.setattr(sys, "argv", ["s3_object_lifecycle.py", "--region", "us-west-2"])

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["success"] is False
    assert payload["cleanup_errors"] == ["DeleteBucket failed"]

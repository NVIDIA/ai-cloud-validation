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

"""Regression tests for the GKE adopt-node-pool current-size verification.

`verify_adopted_node_pool_shape` must verify the pool's CURRENT desired size (read
from the live managed-instance-group target sizes) against the contract input, not
the Container API's creation-time `initialNodeCount`. A pool created at one node and
later resized to two still reports `initialNodeCount=1`, so trusting it would let an
adopt requesting one node satisfy verification on stale creation-time data. These
tests exercise the create-at-one, resize-to-two, adopt-requesting-one regression
without any cloud call — `gcloud` is stubbed with canned describe output.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "isvctl" / "configs" / "providers" / "gcp" / "scripts" / "k8s" / "k8s_lib.py"
)
_spec = importlib.util.spec_from_file_location("gcp_k8s_lib", _SCRIPT_PATH)
assert _spec and _spec.loader
k8s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(k8s)


# A single-zone test pool created at one node: initialNodeCount stays 1 (creation
# time) even after a later resize, while the backing MIG targetSize reflects the
# CURRENT desired size.
_POOL_DESCRIBE: dict[str, Any] = {
    "config": {"machineType": "e2-standard-4"},
    "initialNodeCount": 1,
    "locations": ["us-central1-a"],
    "instanceGroupUrls": [
        "https://www.googleapis.com/compute/v1/projects/proj/zones/"
        "us-central1-a/instanceGroupManagers/gke-isv-pool-mig-abc123"
    ],
}


def _gcloud_stub(node_pool_json: dict[str, Any], mig_target_size: int):
    """Return a `gcloud` replacement dispatching node-pool vs MIG describe calls."""

    def _stub(args: list[str], *, timeout: int = 180, echo: bool = True) -> tuple[int, str]:
        if "node-pools" in args and "describe" in args:
            return 0, json.dumps(node_pool_json)
        if "instance-groups" in args and "describe" in args:
            return 0, f"{mig_target_size}\n"
        raise AssertionError(f"unexpected gcloud call: {args}")

    return _stub


def test_current_desired_size_reads_mig_target_not_initial_count() -> None:
    """The current desired size comes from the MIG targetSize, summed per zone."""
    with patch.object(k8s, "gcloud", _gcloud_stub(_POOL_DESCRIBE, 2)):
        assert k8s.node_pool_current_desired_size(_POOL_DESCRIBE, "proj") == 2


def test_adopt_verify_rejects_resized_pool_on_stale_initial_count() -> None:
    """create-at-one, resize-to-two, adopt-requesting-one must FAIL closed.

    initialNodeCount is still 1 after the resize, but the current MIG target size is
    2, so adoption requesting one node must raise a config_error instead of trivially
    passing on stale creation-time data.
    """
    with patch.object(k8s, "gcloud", _gcloud_stub(_POOL_DESCRIBE, 2)):
        with pytest.raises(k8s.LifecycleError) as excinfo:
            k8s.verify_adopted_node_pool_shape(
                "isv-gke",
                "test-pool",
                "us-central1-a",
                "proj",
                "e2-standard-4",
                {},
                [],
                expected_node_count=1,
            )
    assert excinfo.value.bucket == "config_error"
    assert "current desired size is 2" in excinfo.value.detail


def test_adopt_verify_accepts_matching_current_size() -> None:
    """A genuine same-run adopt (current MIG target size == requested) passes."""
    with patch.object(k8s, "gcloud", _gcloud_stub(_POOL_DESCRIBE, 1)):
        k8s.verify_adopted_node_pool_shape(
            "isv-gke",
            "test-pool",
            "us-central1-a",
            "proj",
            "e2-standard-4",
            {},
            [],
            expected_node_count=1,
        )  # no raise -> current size matches the contract input


def test_current_desired_size_raises_without_instance_groups() -> None:
    """A pool exposing no instanceGroupUrls cannot be verified against an assumed size."""
    pool_without_migs = {k: v for k, v in _POOL_DESCRIBE.items() if k != "instanceGroupUrls"}
    with patch.object(k8s, "gcloud", _gcloud_stub(pool_without_migs, 1)):
        with pytest.raises(k8s.LifecycleError) as excinfo:
            k8s.node_pool_current_desired_size(pool_without_migs, "proj")
    assert excinfo.value.bucket == "unknown_error"

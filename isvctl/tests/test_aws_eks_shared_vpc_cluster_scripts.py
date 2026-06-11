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

"""Contract tests for AWS EKS shared-VPC cluster helper scripts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

AWS_EKS_DIR = Path(__file__).resolve().parents[1] / "configs" / "providers" / "aws" / "scripts" / "eks"


@pytest.mark.parametrize("script_name", ["create_shared_vpc_cluster.sh", "destroy_shared_vpc_cluster.sh"])
@pytest.mark.parametrize("state_file", ["../terraform.tfstate", "/tmp/shared-vpc-cluster.tfstate"])
def test_shared_vpc_cluster_scripts_reject_state_file_paths(script_name: str, state_file: str) -> None:
    """Shared-cluster state overrides must stay local to terraform-shared-vpc-cluster/."""
    env = {**os.environ, "SHARED_VPC_CLUSTER_STATE_FILE": state_file}

    completed = subprocess.run(
        ["bash", str(AWS_EKS_DIR / script_name)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert completed.returncode == 1
    assert "SHARED_VPC_CLUSTER_STATE_FILE must be a local .tfstate filename" in completed.stderr

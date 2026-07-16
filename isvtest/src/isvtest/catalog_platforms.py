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

"""Canonical platform registry for the test catalog.

Leaf module with no dependencies so lightweight consumers (for example the
validate-suites pre-commit hook) can read the registry without triggering
test discovery and its heavy transitive imports via :mod:`isvtest.catalog`.
"""

# Configs that define the canonical test list per platform.
# Relative to the isvctl/configs/ directory.
PLATFORM_CONFIGS: dict[str, list[str]] = {
    "BARE_METAL": ["suites/bare_metal.yaml"],
    "CONTROL_PLANE": ["suites/control-plane.yaml"],
    "IAM": ["suites/iam.yaml"],
    "IMAGE_REGISTRY": ["suites/image-registry.yaml"],
    "KUBERNETES": ["suites/k8s.yaml"],
    "NETWORK": ["suites/network.yaml"],
    "OBSERVABILITY": ["suites/observability.yaml"],
    "SECURITY": ["suites/security.yaml"],
    "SLURM": ["suites/slurm.yaml"],
    "STORAGE": ["suites/storage.yaml"],
    "VM": ["suites/vm.yaml"],
}

# Maps wiring labels to platform strings so a check's platform can be inferred
# from its labels when it isn't otherwise tied to a platform. Derived from
# PLATFORM_CONFIGS: every platform's label is its lowercase name. Trait labels
# like "gpu", "ssh", "workload", and "slow" are intentionally not platforms.
LABEL_TO_PLATFORM: dict[str, str] = {platform.lower(): platform for platform in PLATFORM_CONFIGS}

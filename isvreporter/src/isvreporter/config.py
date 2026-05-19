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

"""Configuration for ISV Lab Service."""

import os


def get_endpoint() -> str:
    """Get ISV Lab Service endpoint from environment.

    Returns:
        The endpoint URL from ISV_SERVICE_ENDPOINT env var, or empty string if not set.
    """
    return os.environ.get("ISV_SERVICE_ENDPOINT", "")


def get_ssa_issuer() -> str:
    """Get SSA issuer URL from environment.

    Returns:
        The SSA issuer URL from ISV_SSA_ISSUER env var, or empty string if not set.
    """
    return os.environ.get("ISV_SSA_ISSUER", "")

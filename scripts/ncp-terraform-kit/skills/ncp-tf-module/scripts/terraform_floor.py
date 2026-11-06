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
"""The Terraform version that every Terraform file the kit writes requires."""

#: The maintainer's floor for every generated `versions.tf`. It is one value so
#: that a module and the tests that use the provider ask for the same Terraform.
#: The kit's generated code is tested on a newer release. The newest Terraform
#: feature that the generated code needs is `terraform test` (1.6), so this
#: floor is a decision, not a feature requirement.
REQUIRED_VERSION = ">= 1.9.5"

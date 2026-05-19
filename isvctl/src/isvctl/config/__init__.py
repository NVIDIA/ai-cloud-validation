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

"""Configuration management for isvctl."""

from isvctl.config.merger import merge_yaml_files
from isvctl.config.output_schemas import (
    get_schema,
    get_schema_for_step,
    list_schemas,
    list_step_mappings,
    register_schema,
    register_step_mapping,
    validate_output,
)
from isvctl.config.schema import (
    CommandConfig,
    CommandOutput,
    KubernetesOutput,
    LabConfig,
    PlatformCommands,
    RunConfig,
    SlurmOutput,
    StepConfig,
    ValidationConfig,
)

__all__ = [
    "CommandConfig",
    "CommandOutput",
    "KubernetesOutput",
    "LabConfig",
    "PlatformCommands",
    "RunConfig",
    "SlurmOutput",
    "StepConfig",
    "ValidationConfig",
    "get_schema",
    "get_schema_for_step",
    "list_schemas",
    "list_step_mappings",
    "merge_yaml_files",
    "register_schema",
    "register_step_mapping",
    "validate_output",
]

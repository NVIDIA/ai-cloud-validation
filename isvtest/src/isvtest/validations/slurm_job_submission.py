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

"""Slurm job submission validations."""

from typing import ClassVar

from isvtest.core.nvidia import count_gpus_from_list_output, has_gpu_output
from isvtest.core.validation import BaseValidation


class SlurmJobSubmission(BaseValidation):
    """Test basic Slurm job submission and completion."""

    description: ClassVar[str] = "Verify Slurm job submission works with GPU access"
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        # Submit a simple job that lists GPUs
        result = self.run_command("srun --partition=gpu --gres=gpu:1 nvidia-smi -L")

        if result.exit_code != 0:
            self.set_failed(f"srun job failed: {result.stderr}")
            return

        if not result.stdout.strip():
            self.set_failed("No output from nvidia-smi")
            return

        if not has_gpu_output(result.stdout):
            self.set_failed("GPU not found in nvidia-smi output")
            return

        # Count GPUs found using shared parser
        gpu_count = count_gpus_from_list_output(result.stdout)
        self.set_passed(f"Job submission successful, found {gpu_count} GPU(s)")

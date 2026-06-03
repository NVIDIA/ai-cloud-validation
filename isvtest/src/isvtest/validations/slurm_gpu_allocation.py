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

"""Slurm GPU allocation validations."""

from typing import ClassVar

from isvtest.core.nvidia import count_gpus_from_list_output
from isvtest.core.validation import BaseValidation


class SlurmGpuAllocation(BaseValidation):
    """Test Slurm GPU allocation using srun."""

    description: ClassVar[str] = "Verify Slurm can allocate GPUs to jobs"
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        # Get number of GPUs to request (default: 1)
        raw_num_gpus = self.config.get("num_gpus", 1)
        try:
            num_gpus = int(raw_num_gpus)
        except (TypeError, ValueError):
            self.set_failed(f"Invalid num_gpus value: {raw_num_gpus!r}")
            return

        # Run srun with GPU allocation
        result = self.run_command(f"srun --partition=gpu --gres=gpu:{num_gpus} nvidia-smi --list-gpus")

        if result.exit_code != 0:
            self.set_failed(f"srun failed: {result.stderr}")
            return

        # Count GPU lines in output using shared parser
        gpu_count = count_gpus_from_list_output(result.stdout)

        if gpu_count != num_gpus:
            self.set_failed(f"Expected {num_gpus} GPU(s), but got {gpu_count}")
            return

        self.set_passed(f"Successfully allocated {num_gpus} GPU(s) via Slurm")

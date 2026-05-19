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

"""Operation runner for sequential execution of clean-up operations."""

import logging
from typing import Any

from isvctl.cleaner.operations import OPERATIONS

logger = logging.getLogger(__name__)


class OperationRunner:
    """Executes clean-up operations sequentially with error handling."""

    def __init__(
        self,
        verbose: bool = False,
        dry_run: bool = False,
        continue_on_error: bool = False,
    ) -> None:
        """Initialize the operation runner.

        Args:
            verbose: Enable verbose output
            dry_run: Show operations without executing them
            continue_on_error: Continue execution even if an operation fails
        """
        self.verbose = verbose
        self.dry_run = dry_run
        self.continue_on_error = continue_on_error

    def run_operations(self, operation_names: list[str]) -> list[dict[str, Any]]:
        """Execute operations sequentially.

        Args:
            operation_names: List of operation names to execute

        Returns:
            List of result dictionaries, one per operation
        """
        results: list[dict[str, Any]] = []

        for idx, operation_name in enumerate(operation_names, 1):
            if operation_name not in OPERATIONS:
                logger.error(f"Unknown operation: {operation_name}")
                results.append(
                    {
                        "operation": operation_name,
                        "success": False,
                        "error": f"Unknown operation: {operation_name}",
                    }
                )
                if not self.continue_on_error:
                    break
                continue

            operation_func, operation_description = OPERATIONS[operation_name]

            logger.info(f"\n[{idx}/{len(operation_names)}] Running: {operation_name}")
            if self.verbose:
                logger.info(f"Description: {operation_description}")

            if self.dry_run:
                logger.info(f"[DRY RUN] Would execute: {operation_name}")
                results.append(
                    {
                        "operation": operation_name,
                        "success": True,
                        "message": "Dry run - not executed",
                    }
                )
                continue

            # Execute the operation
            try:
                result = operation_func()

                if self.verbose and result.get("message"):
                    logger.info(f"Result: {result['message']}")

                results.append(
                    {
                        "operation": operation_name,
                        "success": result.get("success", False),
                        "message": result.get("message", ""),
                    }
                )

                if not result.get("success", False):
                    logger.error(f"Operation {operation_name} failed")
                    if not self.continue_on_error:
                        logger.error("Stopping execution due to failure")
                        break
                else:
                    logger.info(f"{operation_name} completed successfully")

            except Exception as e:
                logger.error(f"Exception during {operation_name}: {e}")
                results.append(
                    {
                        "operation": operation_name,
                        "success": False,
                        "error": str(e),
                    }
                )
                if not self.continue_on_error:
                    logger.error("Stopping execution due to exception")
                    break

        return results

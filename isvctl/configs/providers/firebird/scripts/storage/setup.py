#!/usr/bin/env python3
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

"""Mint the storage run's ID and point the suite at the StorageProvider manifest.

Every filesystem the storage run creates is named ``isv-fs-<run_id>-<role>``
(the shim's too: the config hands ``run_id`` to ``StorageProviderApiCheck``), so
teardown deletes exactly this run's filesystems by that prefix. The suite reads
``storage.manifest_path`` from this step (``steps.setup.storage.manifest_path``)
to find the Firebird shim. Makes no API call.

Usage:
    python setup.py

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "setup",
    "run_id": "a1b2c3",
    "fs_prefix": "isv-fs-a1b2c3-",
    "storage": {"manifest_path": "/.../firebird/config/storage-provider-manifest.yaml"}
}
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.filesystems import new_run_id, run_prefix

MANIFEST = Path(__file__).resolve().parents[2] / "config" / "storage-provider-manifest.yaml"


def main() -> int:
    """Emit the run ID and the manifest path.

    Returns:
        0 when the manifest exists, 1 otherwise
    """
    run_id = new_run_id()
    result = {
        "success": MANIFEST.is_file(),
        "platform": "storage",
        "test_name": "setup",
        "run_id": run_id,
        "fs_prefix": run_prefix(run_id),
        "storage": {"manifest_path": str(MANIFEST)},
    }
    if not result["success"]:
        result["error"] = f"StorageProvider manifest not found at {MANIFEST}"
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

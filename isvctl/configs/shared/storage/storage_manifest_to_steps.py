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

"""Storage manifest -> step output adapter (provider-neutral).

Resolves a ``storage-provider-manifest.yaml`` to an absolute path and prints
the JSON the orchestrator stores under ``steps.<name>.storage.manifest_path``,
which ``StorageProviderApiCheck`` consumes to load the provider shims.

The manifest drives the StorageProvider API check ONLY. The CSI / NFS / POSIX
filesystem checks get their StorageClass names and protocol expectations from
their own config (suite/provider YAML literals or ``K8S_CSI_*`` env vars), not
from this manifest.

This only *resolves* the path - it does NOT load the provider shims (no backend
connections), so it is safe to run in any phase. The authoritative manifest
parsing happens when ``StorageProviderApiCheck`` loads the manifest in-process.

The manifest path is taken from ``STORAGE_PROVIDER_MANIFEST`` (or the first CLI
arg) and resolved to an absolute path so the emitted ``storage.manifest_path``
works regardless of the consumer's cwd. When unset, an empty path is emitted so
``StorageProviderApiCheck`` skips cleanly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _fail(message: str) -> int:
    """Emit a failure payload and return a non-zero exit code."""
    print(json.dumps({"success": False, "error": message}))
    return 1


def _resolve_manifest_path() -> Path | None:
    raw = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("STORAGE_PROVIDER_MANIFEST", "")
    raw = raw.strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    # Try as-given (relative to cwd), then relative to the repo root walking up.
    if candidate.is_file():
        return candidate.resolve()
    if not candidate.is_absolute():
        for base in (Path.cwd(), *Path.cwd().parents):
            probe = base / candidate
            if probe.is_file():
                return probe.resolve()
    return candidate  # non-existent; reported by the caller


def main() -> int:
    manifest_path = _resolve_manifest_path()
    if manifest_path is None:
        # No manifest configured: emit an empty path so StorageProviderApiCheck
        # skips, and the step still succeeds.
        print(
            json.dumps(
                {
                    "success": True,
                    "platform": "storage",
                    "test_name": "storage_manifest",
                    "storage": {"manifest_path": ""},
                }
            )
        )
        return 0

    if not manifest_path.is_file():
        return _fail(f"storage manifest not found: {manifest_path}")

    print(
        json.dumps(
            {
                "success": True,
                "platform": "storage",
                "test_name": "storage_manifest",
                "storage": {"manifest_path": str(manifest_path)},
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

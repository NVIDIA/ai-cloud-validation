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

"""Read Compute Engine labels and emit them as canonical-cased tags.

Compute Engine label keys must match ``[a-z]([-a-z0-9_]*)`` so the
launch step writes lowercase labels. The suite contract expects mixed-
case ``Name`` / ``CreatedBy`` keys — we project labels back to canonical
casing here so ``InstanceTagCheck.required_keys`` stays unchanged across
providers.

Validator-consumed fields must derive from a real signal: the values
come from the actual labels on the live instance, never hardcoded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import (
    get_instance,
    labels_to_canonical_tags,
    narrow_region_to_zone,
    resolve_project,
)
from common.errors import handle_gcp_errors


@handle_gcp_errors
def main() -> int:
    parser = argparse.ArgumentParser(description="Describe Compute Engine instance labels")
    parser.add_argument("--instance-id", required=True, help="Instance name")
    parser.add_argument("--region", required=True, help="GCP region or zone")
    parser.add_argument("--zone", default=None, help="GCP zone (overrides region)")
    parser.add_argument("--project", default=None, help="GCP project ID (ADC fallback)")
    args = parser.parse_args()

    project = resolve_project(args.project)
    zone = args.zone or narrow_region_to_zone(args.region)

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "tags": {},
        "tag_count": 0,
        "region": args.region,
        "zone": zone,
        "project": project,
    }

    try:
        inst = get_instance(project, zone, args.instance_id)
        labels = dict(getattr(inst, "labels", {}) or {})
        tags = labels_to_canonical_tags(labels)
        result["tags"] = tags
        result["tag_count"] = len(tags)
        result["success"] = True

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

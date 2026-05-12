#!/usr/bin/env python3
"""serial_console — Armada Bridge bare metal suite, test phase.

Checks serial console output and retention for a bare metal node via:
  GET /tenants/<tenant>/metal/<compute_node_id>/console-output

BLOCKED: Bridge does not expose a serial console API for bare metal nodes.
This step is best_effort: true in bare_metal.yaml.

Output satisfies both SerialConsoleCheck and SerialConsoleRetentionCheck:
  {success, platform, instance_id, console_available, serial_access_enabled,
   output_length, console_log_queryable, retention_days_configured,
   oldest_queryable_log_age_days, query_result_count, retention_evidence}
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.bridge_client import BridgeClient  # noqa: F401 — used in the live impl block
from common.errors import handle_bridge_errors

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


@handle_bridge_errors
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--compute-node-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "bare_metal"}

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "platform": "bare_metal",
                "instance_id": "demo-bm-node01",
                "console_available": False,
                "serial_access_enabled": True,
                "output_length": 0,
                "console_log_queryable": True,
                "retention_days_configured": 90,
                "oldest_queryable_log_age_days": 45,
                "query_result_count": 12,
                "retention_evidence": "Bridge audit-svc /audit-logs 90-day retention",
            }
        )
    else:
        raise NotImplementedError(
            "serial_console: Bridge has no serial console API for bare metal. "
            "This step is blocked (best_effort: true). "
            "Implement when Bridge exposes GET /tenants/<tenant>/metal/<id>/console-output."
        )

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

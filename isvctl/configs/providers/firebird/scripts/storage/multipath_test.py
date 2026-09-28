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

"""Client multipathing from the BM to the parallel filesystem servers (HSS18-01).

Reads the cluster's containers with the mount token (``weka cluster
container``) and probes the backends from the BM:

  multiple_paths         the BM's client container has two or more network
                         addresses to reach the servers (``path_count``)
  all_servers_reachable  every backend server (by host name) is UP and the BM
                         opens a TCP connection to at least one of its
                         addresses on its management port (``server_count``)
  failover_works         reported failed as not tested: it needs a second client
                         path and a path or server taken down, which a tenant
                         cannot do

Skips without a BM.

Usage:
    python multipath_test.py --instance-id bm.xxx --key-file /tmp/key --mount-point /mnt/isv-a1b2c3

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "multipath",
    "tests": {
        "multiple_paths":        {"passed": false, "path_count": 1, "error": "..."},
        "all_servers_reachable": {"passed": true, "server_count": 7},
        "failover_works":        {"passed": false, "supported": false, "error": "not tested: ..."}
    }
}
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.firebird_client import log
from common.wekafs import fill_missing, mounted_host, redact, run_id_of, run_python, token_file, weka_json

KEYS = ("multiple_paths", "all_servers_reachable", "failover_works")
FAILOVER_REASON = (
    "not tested: failover needs a second client path and a path or server taken down, which a tenant cannot do"
)
DEFAULT_PORT = 14000

# Runs on the BM: argv[1] = JSON {server: [[ip, port], ...]}; prints {server: reachable}.
REACH = """
import json, socket, sys
out = {}
for server, targets in json.loads(sys.argv[1]).items():
    out[server] = False
    for ip, port in targets:
        try:
            socket.create_connection((ip, port), timeout=3).close()
            out[server] = True
            break
        except OSError:
            pass
print(json.dumps(out))
"""


def main() -> int:
    """Count the client's paths and check every backend server is reachable.

    Returns:
        0 when the probes ran (or skipped without a BM), 1 otherwise
    """
    parser = argparse.ArgumentParser(description="Firebird client multipath test (HSS18-01)")
    parser.add_argument("--instance-id", default="", help="Provisioned BM (bm.ULID); empty skips the BM work")
    parser.add_argument("--key-file", default="", help="SSH private key of the BM")
    parser.add_argument("--ssh-user", default="ubuntu", help="SSH username")
    parser.add_argument("--mount-point", default="", help="Where setup_mount mounted the run's filesystem")
    parser.add_argument("--mount-error", default="", help="setup_mount's error, when it failed")
    args = parser.parse_args()

    tests: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {"success": False, "platform": "storage", "test_name": "multipath", "tests": tests}
    host, code = mounted_host(args, result, KEYS)
    if host is None:
        print(json.dumps(result, indent=2))
        return code or 0

    try:
        token_path = token_file(run_id_of(args.mount_point))
        containers = weka_json(host, "cluster container", token_path)
        clients = [c for c in containers if c.get("mode") == "client" and host.ip in (c.get("ips") or [])]
        if not clients:
            raise RuntimeError("weka cluster container lists no client container for this BM")
        paths = sorted({ip for c in clients for ip in c.get("ips") or []})
        tests["multiple_paths"] = {"passed": len(paths) >= 2, "path_count": len(paths)}
        if len(paths) < 2:
            tests["multiple_paths"]["error"] = f"the client reaches the servers over {len(paths)} network address"

        servers: dict[str, list[list[Any]]] = {}
        down: set[str] = set()
        for c in containers:
            if c.get("mode") != "backend":
                continue
            name = str(c.get("hostname") or c.get("host_id"))
            port = int(c.get("mgmt_port") or DEFAULT_PORT)
            targets = servers.setdefault(name, [])
            targets.extend([ip, port] for ip in c.get("ips") or [] if [ip, port] not in targets)
            if c.get("status") != "UP":
                down.add(name)
        if not servers:
            raise RuntimeError("weka cluster container lists no backend servers")
        attempts = sum(len(targets) for targets in servers.values())
        reach = run_python(host, REACH, [json.dumps(servers)], timeout=60 + 3 * attempts)
        unreachable = sorted(name for name, ok in reach.items() if not ok)
        ok = not unreachable and not down
        tests["all_servers_reachable"] = {"passed": ok, "server_count": len(servers)}
        if not ok:
            problems = [f"not UP: {', '.join(sorted(down))}"] if down else []
            problems += [f"unreachable from the BM: {', '.join(unreachable)}"] if unreachable else []
            tests["all_servers_reachable"]["error"] = "; ".join(problems)
        tests["failover_works"] = {"passed": False, "supported": False, "error": FAILOVER_REASON}
        result["success"] = True
    except Exception as e:
        result["error"] = redact(e)
        log(f"ERROR: {result['error']}")

    fill_missing(tests, KEYS, result.get("error", ""))
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

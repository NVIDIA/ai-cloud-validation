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

"""Validate home-directory quotas, accounting, and NFSv4 via FSx for OpenZFS."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import uuid
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import boto3
from common.ec2 import instance_network
from common.errors import handle_aws_errors, stamp_test_errors
from common.fsx import new_suffix, wait_filesystem_available
from common.openzfs import (
    cleanup_resources,
    create_filesystem,
    create_nfs_security_group,
    create_test_volume,
    set_volume_quota,
    wait_volume,
)
from common.ssh_utils import ssh_run, wait_for_ssh

_MOUNT_A = "/mnt/isv-dir-a"
_MOUNT_B = "/mnt/isv-dir-b"
_UID_A = 20001
_UID_B = 20002
_GID_A = 30001
_GID_B = 30002
_SIZE_A = 8 * 1024 * 1024
_SIZE_B = 4 * 1024 * 1024

_TEST_NAMES = (
    "filesystem_quota_configured",
    "filesystem_quota_updated",
    "filesystem_quota_enforced",
    "uid_usage_accounted",
    "gid_usage_accounted",
    "identity_usage_isolated",
    "nfsv4_mounted",
    "nfs_read_write",
    "nfs_shared_visibility",
)


def _run_remote(host: str, key_file: str, command: str, *, timeout: int = 300) -> tuple[int, str, str]:
    """Run a command on the Ubuntu client instance."""
    return ssh_run(host, "ubuntu", key_file, command, timeout=timeout)


def _mount_volume(host: str, key_file: str, dns_name: str, volume_path: str) -> None:
    """Install NFS tools and mount the export twice using NFSv4.1."""
    source = shlex.quote(f"{dns_name}:{volume_path}")
    command = (
        "set -eu; "
        "sudo env DEBIAN_FRONTEND=noninteractive apt-get update -qq; "
        "sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-common >/dev/null; "
        f"sudo mkdir -p {_MOUNT_A} {_MOUNT_B}; "
        f"sudo mount -t nfs4 -o vers=4.1 {source} {_MOUNT_A}; "
        f"sudo mount -t nfs4 -o vers=4.1 {source} {_MOUNT_B}; "
        f"sudo chmod 0777 {_MOUNT_A}"
    )
    rc, _, stderr = _run_remote(host, key_file, command, timeout=600)
    if rc != 0:
        raise RuntimeError(f"NFS mount setup failed: {stderr.strip()[:300]}")


def _probe_nfs(host: str, key_file: str) -> dict[str, dict[str, Any]]:
    """Verify NFSv4.1, read/write access, and visibility between mounts."""
    rc, mountinfo, stderr = _run_remote(host, key_file, f"findmnt -n -o FSTYPE,OPTIONS {_MOUNT_A}")
    mounted = rc == 0 and mountinfo.split(maxsplit=1)[0] == "nfs4" and "vers=4.1" in mountinfo
    mount_message = mountinfo.strip() if rc == 0 else stderr.strip()[:200]

    canary = f"isv-dir-{uuid.uuid4().hex}"
    rc, output, stderr = _run_remote(
        host,
        key_file,
        f"printf %s {shlex.quote(canary)} > {_MOUNT_A}/canary && cat {_MOUNT_B}/canary",
    )
    read_write = rc == 0
    shared = read_write and output == canary
    error = stderr.strip()[:200]
    return {
        "nfsv4_mounted": {"passed": mounted, "message": mount_message},
        "nfs_read_write": {"passed": read_write, "message": "Canary write/read succeeded" if read_write else error},
        "nfs_shared_visibility": {
            "passed": shared,
            "message": "Canary was visible through both mounts" if shared else f"Expected {canary!r}, got {output!r}",
        },
    }


def _probe_accounting(host: str, key_file: str) -> dict[str, dict[str, Any]]:
    """Write files with distinct ownership and verify UID/GID byte totals."""
    byte_sums = "; ".join(
        f"{var}=$(find {_MOUNT_B} -xdev -type f -{flag} {ident} -printf '%s\\n' | awk '{{s+=$1}} END {{print s+0}}')"
        for var, flag, ident in (
            ("uid_a", "uid", _UID_A),
            ("uid_b", "uid", _UID_B),
            ("gid_a", "gid", _GID_A),
            ("gid_b", "gid", _GID_B),
        )
    )
    command = (
        "set -eu; "
        f"sudo dd if=/dev/zero of={_MOUNT_A}/uid-a bs=1M count=8 status=none; "
        f"sudo chown {_UID_A}:{_GID_A} {_MOUNT_A}/uid-a; "
        f"sudo dd if=/dev/zero of={_MOUNT_A}/uid-b bs=1M count=4 status=none; "
        f"sudo chown {_UID_B}:{_GID_B} {_MOUNT_A}/uid-b; "
        f"{byte_sums}; "
        'printf "%s %s %s %s" "$uid_a" "$uid_b" "$gid_a" "$gid_b"'
    )
    rc, output, stderr = _run_remote(host, key_file, command)
    try:
        uid_a, uid_b, gid_a, gid_b = (int(value) for value in output.split())
    except (ValueError, TypeError):
        return {
            name: {"passed": False, "error": stderr.strip()[:200] or f"Invalid accounting output: {output!r}"}
            for name in ("uid_usage_accounted", "gid_usage_accounted", "identity_usage_isolated")
        }

    uid_ok = rc == 0 and (uid_a, uid_b) == (_SIZE_A, _SIZE_B)
    gid_ok = rc == 0 and (gid_a, gid_b) == (_SIZE_A, _SIZE_B)
    isolated = uid_ok and gid_ok and uid_a != uid_b and gid_a != gid_b
    return {
        "uid_usage_accounted": {"passed": uid_ok, "message": f"uid totals: {_UID_A}={uid_a}, {_UID_B}={uid_b}"},
        "gid_usage_accounted": {"passed": gid_ok, "message": f"gid totals: {_GID_A}={gid_a}, {_GID_B}={gid_b}"},
        "identity_usage_isolated": {"passed": isolated, "message": "UID/GID totals remained identity-scoped"},
    }


def _probe_quota_enforcement(host: str, key_file: str) -> dict[str, Any]:
    """Pass when a write beyond the 1 GiB volume quota is rejected."""
    command = f"sudo dd if=/dev/zero of={_MOUNT_A}/over-quota bs=1M count=1100 conv=fsync status=none"
    rc, _, stderr = _run_remote(host, key_file, command, timeout=300)
    message = stderr.strip()[:300]
    return {
        "passed": rc != 0 and any(token in message.lower() for token in ("quota", "no space", "exceeded")),
        "message": message or f"Over-quota write exited with status {rc}",
    }


def _quota_config_matches(volume: dict[str, Any], expected_gib: int) -> bool:
    """Return whether volume and identity quotas match the expected contract."""
    config = volume.get("OpenZFSConfiguration", {})
    quotas = config.get("UserAndGroupQuotas", [])
    expected = {
        ("USER", _UID_A, 1),
        ("GROUP", _GID_A, 1),
    }
    actual = {(item.get("Type"), item.get("Id"), item.get("StorageCapacityQuotaGiB")) for item in quotas}
    return config.get("StorageCapacityQuotaGiB") == expected_gib and expected <= actual


@handle_aws_errors
def main() -> int:
    """Provision FSx for OpenZFS, run all DIR01/DIR02 probes, and clean up."""
    parser = argparse.ArgumentParser(description="Validate home-directory storage via FSx for OpenZFS")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--key-file", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "storage",
        "test_name": "home_directory_storage",
        "tests": {name: {"passed": False} for name in _TEST_NAMES},
    }
    ec2 = boto3.client("ec2", region_name=args.region)
    fsx = boto3.client("fsx", region_name=args.region)
    suffix = new_suffix()
    fs_id: str | None = None
    host: str | None = None
    created: dict[str, str] = {}

    try:
        network = instance_network(ec2, args.instance_id)
        host = network["public_ip"]
        if not wait_for_ssh(host, "ubuntu", args.key_file):
            raise RuntimeError("SSH client instance did not become ready")

        sg_id = create_nfs_security_group(
            ec2,
            network["vpc_id"],
            network["security_group_id"],
            suffix,
            created,
        )
        fs_id = create_filesystem(fsx, network["subnet_id"], sg_id, suffix)
        filesystem = wait_filesystem_available(fsx, fs_id)
        root_volume_id = filesystem["OpenZFSConfiguration"]["RootVolumeId"]
        volume_id = create_test_volume(fsx, root_volume_id, network["private_ip"], suffix)
        volume = wait_volume(fsx, volume_id)

        configured = _quota_config_matches(volume, 2)
        result["tests"]["filesystem_quota_configured"] = {
            "passed": configured,
            "message": "2 GiB volume quota and per-UID/GID quotas configured",
        }

        dns_name = str(filesystem["DNSName"])
        volume_path = str(volume["OpenZFSConfiguration"]["VolumePath"])
        _mount_volume(host, args.key_file, dns_name, volume_path)
        result["tests"].update(_probe_nfs(host, args.key_file))
        result["tests"].update(_probe_accounting(host, args.key_file))

        set_volume_quota(fsx, volume_id, 1)
        updated_volume = wait_volume(fsx, volume_id, expected_quota_gib=1)
        updated = _quota_config_matches(updated_volume, 1)
        result["tests"]["filesystem_quota_updated"] = {
            "passed": updated,
            "message": "Volume quota updated from 2 GiB to 1 GiB",
        }
        if updated:
            result["tests"]["filesystem_quota_enforced"] = _probe_quota_enforcement(host, args.key_file)

        result["success"] = all(test.get("passed", False) for test in result["tests"].values())
    except Exception as error:
        result["error"] = str(error)
        stamp_test_errors(result, str(error))
    finally:
        cleanup_errors: list[str] = []
        if host:
            try:
                rc, _, stderr = _run_remote(
                    host,
                    args.key_file,
                    (
                        "rc=0; "
                        f"for mount in {_MOUNT_B} {_MOUNT_A}; do "
                        'if mountpoint -q "$mount"; then sudo umount -l "$mount" || rc=1; fi; '
                        "done; exit $rc"
                    ),
                )
                if rc != 0:
                    cleanup_errors.append(f"unmount cleanup failed: {stderr.strip()[:200] or f'exit code {rc}'}")
            except Exception as error:
                cleanup_errors.append(f"unmount cleanup failed: {error}")
        try:
            cleanup_errors.extend(cleanup_resources(ec2, fsx, fs_id, created.get("sg_id")))
        except Exception as error:
            cleanup_errors.append(f"AWS cleanup failed: {error}")
        result["cleanup"] = not cleanup_errors
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            result["success"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

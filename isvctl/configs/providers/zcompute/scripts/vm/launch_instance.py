#!/usr/bin/env python3
"""Launch a zcompute VM instance for ISV NCP validation.

Launches an instance (or reuses an existing one), allocates an EIP,
waits for SSH, and loads NVIDIA modules.

zcompute-specific notes:
  - No boto3 waiters — uses custom polling.
  - Root device is /dev/vda (not /dev/sda).
  - No auto public IP — EIP must be allocated and associated manually.
  - NVIDIA modules must be loaded via modprobe after SSH.
  - --owners self for describe-images returns empty; never filter by owner.

Environment:
    ZCOMPUTE_VM_INSTANCE_ID  - if set, reuse this instance instead of launching
    ZCOMPUTE_VM_KEY_FILE     - PEM file for the reused instance

Output JSON:
{
    "success": true, "platform": "vm",
    "instance_id": "i-xxx", "instance_type": "zh1.52xlarge",
    "public_ip": "172.28.x.x", "private_ip": "172.31.x.x",
    "state": "running", "ami_id": "ami-xxx",
    "key_name": "isv-test-key", "key_file": "/tmp/isv-test-key.pem",
    "vpc_id": "vpc-xxx", "subnet_id": "subnet-xxx",
    "security_group_id": "sg-xxx", "eip_allocation_id": "eipalloc-xxx"
}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.client import get_client  # noqa: E402
from common.ec2 import (  # noqa: E402
    allocate_and_associate_eip,
    create_key_pair,
    create_security_group,
    load_nvidia_modules,
    poll_instance_state,
    setup_gpu_dependencies,
    wait_for_public_ip,
)
from common.ssh_utils import wait_for_ssh  # noqa: E402

# Defaults — env vars take precedence over the hardcoded fallbacks.
DEFAULT_INSTANCE_TYPE = os.environ.get("ZCOMPUTE_TEST_INSTANCE_TYPE", "zh1.52xlarge")
DEFAULT_AMI_ID = os.environ.get(
    "ZCOMPUTE_TEST_AMI_ID",
    "ami-241c9a9e96d143dc90e1a5a50a3a8152",  # isv-ncp-gpu-baked-20260522
)
DEFAULT_VPC_ID = os.environ.get(
    "ZCOMPUTE_TEST_VPC_ID",
    "vpc-0b7f00012d3046f391a4e99399e456af",
)
DEFAULT_KEY_NAME = "isv-test-key"
DEFAULT_SSH_USER = "ubuntu"


def _get_default_subnet(ec2: Any, vpc_id: str) -> str:
    """Return the first subnet found in the given VPC."""
    resp = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )
    subnets = resp.get("Subnets", [])
    if not subnets:
        raise RuntimeError(f"No subnets found in VPC {vpc_id}")
    return subnets[0]["SubnetId"]


def _reuse_instance(ec2: Any, instance_id: str, key_file: str) -> dict[str, Any]:
    """Describe and optionally start an existing instance, then return its details."""
    print(
        f"[launch] reusing existing instance {instance_id} (ZCOMPUTE_VM_INSTANCE_ID)",
        file=sys.stderr,
    )
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    state = inst["State"]["Name"]

    if state == "stopped":
        print(f"[launch] instance is stopped; starting it ...", file=sys.stderr)
        ec2.start_instances(InstanceIds=[instance_id])
        state = poll_instance_state(
            ec2, instance_id, ["running"], timeout=600, interval=15
        )
    elif state != "running":
        state = poll_instance_state(
            ec2, instance_id, ["running"], timeout=600, interval=15
        )

    # Re-fetch after state change.
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]

    public_ip = inst.get("PublicIpAddress")
    if not public_ip or public_ip in ("", "None"):
        public_ip = None

    private_ip = inst.get("PrivateIpAddress")
    vpc_id = inst.get("VpcId", "")
    subnet_id = inst.get("SubnetId", "")
    instance_type = inst.get("InstanceType", "")
    ami_id = inst.get("ImageId", "")
    key_name = inst.get("KeyName", "")

    return {
        "success": True,
        "platform": "vm",
        "instance_id": instance_id,
        "instance_type": instance_type,
        "public_ip": public_ip,
        "private_ip": private_ip,
        "state": state,
        "ami_id": ami_id,
        "key_name": key_name,
        "key_file": key_file,
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "security_group_id": None,
        "eip_allocation_id": None,
        "reused": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch a zcompute VM instance")
    parser.add_argument("--name", default="isv-ncp-vm-test")
    parser.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "symphony"))
    parser.add_argument("--ami-id", default=DEFAULT_AMI_ID)
    parser.add_argument("--vpc-id", default=None)
    parser.add_argument("--subnet-id", default=None)
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME)
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    args = parser.parse_args()

    result: dict[str, Any] = {"success": False, "platform": "vm"}

    # Allow reuse of an existing instance via environment variables.
    existing_id = os.environ.get("ZCOMPUTE_VM_INSTANCE_ID", "").strip()
    existing_key = os.environ.get("ZCOMPUTE_VM_KEY_FILE", "").strip()

    ec2 = get_client("ec2", region=args.region)

    try:
        if existing_id and existing_key:
            result = _reuse_instance(ec2, existing_id, existing_key)
            public_ip = result.get("public_ip")
            if public_ip:
                ssh_ready = wait_for_ssh(
                    public_ip, args.ssh_user, existing_key, max_attempts=40, interval=15
                )
                result["ssh_ready"] = ssh_ready
                if ssh_ready:
                    nvidia_ok = load_nvidia_modules(public_ip, args.ssh_user, existing_key)
                    result["nvidia_modules_loaded"] = nvidia_ok
            return 0 if result.get("success") else 1

        # Determine VPC and subnet.
        vpc_id = args.vpc_id or DEFAULT_VPC_ID
        subnet_id = args.subnet_id
        if not subnet_id:
            subnet_id = _get_default_subnet(ec2, vpc_id)
        print(f"[launch] using VPC {vpc_id}, subnet {subnet_id}", file=sys.stderr)

        # Create (or reuse) key pair.
        key_file = create_key_pair(ec2, args.key_name)

        # zCompute returns RSA PKCS#1 keys; paramiko's CloudInitCheck requires
        # OpenSSH format. Convert in-place with ssh-keygen.
        try:
            import subprocess as _sp
            _sp.run(
                ['ssh-keygen', '-p', '-m', 'OpenSSH', '-f', key_file, '-N', ''],
                check=True, capture_output=True,
            )
            print(f"[launch] key converted to OpenSSH format", file=sys.stderr)
        except Exception as _e:
            print(f"[launch] WARNING: key format conversion failed (non-fatal): {_e}", file=sys.stderr)

        # Create (or reuse) security group.
        sg_name = f"{args.name}-sg"
        sg_id = create_security_group(ec2, vpc_id, sg_name)

        # Launch the instance.
        print(
            f"[launch] launching {args.instance_type} from {args.ami_id} ...",
            file=sys.stderr,
        )
        run_resp = ec2.run_instances(
            ImageId=args.ami_id,
            InstanceType=args.instance_type,
            MinCount=1,
            MaxCount=1,
            KeyName=args.key_name,
            SubnetId=subnet_id,
            SecurityGroupIds=[sg_id],
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/vda",
                    "Ebs": {
                        "VolumeSize": 100,
                        "VolumeType": "gp2",
                        "DeleteOnTermination": True,
                    },
                }
            ],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": args.name},
                        {"Key": "CreatedBy", "Value": "isvtest"},
                    ],
                }
            ],
        )
        instance_id = run_resp["Instances"][0]["InstanceId"]
        private_ip = run_resp["Instances"][0].get("PrivateIpAddress")
        print(f"[launch] instance {instance_id} launched", file=sys.stderr)

        # Poll until running — auto-activate if instance falls to shutoff.
        # zCompute occasionally transitions new instances to shutoff instead of
        # running (hypervisor scheduling issue). Check every ~60s and start it.
        import time as _launch_time
        _deadline = _launch_time.monotonic() + 900  # 15 min total budget
        state = "pending"
        while _launch_time.monotonic() < _deadline:
            try:
                _resp = ec2.describe_instances(InstanceIds=[instance_id])
                _instances = [
                    i for r in _resp.get("Reservations", [])
                    for i in r.get("Instances", [])
                    if i["InstanceId"] == instance_id
                ]
                state = _instances[0]["State"]["Name"] if _instances else state
            except Exception:
                pass

            if state == "running":
                print(f"[launch] instance {instance_id} is running", file=sys.stderr)
                break
            elif state in ("shutoff", "stopped"):
                print(
                    f"[launch] instance {instance_id} is {state} — sending start command",
                    file=sys.stderr,
                )
                try:
                    ec2.start_instances(InstanceIds=[instance_id])
                except Exception as _e:
                    print(f"[launch] WARNING: start_instances failed: {_e}", file=sys.stderr)
            else:
                print(
                    f"[launch] waiting for running (current: {state}) ...",
                    file=sys.stderr,
                )
            _launch_time.sleep(60)
        else:
            raise RuntimeError(
                f"Instance {instance_id} did not reach 'running' within 15 min "
                f"(last state: {state})"
            )

        # Allocate and associate EIP (zcompute does not auto-assign public IPs).
        allocation_id, public_ip = allocate_and_associate_eip(ec2, instance_id)

        # Confirm the IP is reflected in describe_instances.
        confirmed_ip = wait_for_public_ip(ec2, instance_id, timeout=120, interval=5)
        if confirmed_ip:
            public_ip = confirmed_ip

        # Wait for SSH.
        ssh_ready = wait_for_ssh(
            public_ip, args.ssh_user, key_file, max_attempts=40, interval=15
        )

        # Build result now so instance_id is always present even if GPU setup fails.
        result = {
            "success": True,
            "platform": "vm",
            "instance_id": instance_id,
            "instance_type": args.instance_type,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "state": state,
            "ami_id": args.ami_id,
            "key_name": args.key_name,
            "key_file": key_file,
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "security_group_id": sg_id,
            "eip_allocation_id": allocation_id,
            "ssh_ready": ssh_ready,
            "nvidia_modules_loaded": False,
            "gpu_deps": {},
        }

        # Load nvidia modules FIRST (installs kernel module from Ubuntu default repo),
        # THEN install GPU dependencies (Docker, CUDA, NCT).
        # Order is critical: setup_gpu_deps adds the CUDA apt repo which has a newer
        # nvidia-utils version than the kernel module package — installing utils after
        # the CUDA repo causes a driver/library version mismatch in nvidia-smi.
        if ssh_ready:
            try:
                result["nvidia_modules_loaded"] = load_nvidia_modules(public_ip, args.ssh_user, key_file)
            except Exception as e:
                print(f"[launch] WARNING: load_nvidia_modules failed (non-fatal): {e}", file=sys.stderr)

            try:
                print("[launch] installing GPU dependencies (Docker, NCT, CUDA) ...", file=sys.stderr)
                result["gpu_deps"] = setup_gpu_dependencies(public_ip, args.ssh_user, key_file)
            except Exception as e:
                print(f"[launch] WARNING: setup_gpu_dependencies failed (non-fatal): {e}", file=sys.stderr)

            # Post-setup nvidia diagnostic — written to /tmp/nvidia_diag.txt on toolbox.
            import subprocess as _diag_sp
            _diag = _diag_sp.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "BatchMode=yes", "-i", key_file, f"{args.ssh_user}@{public_ip}",
                 # 1. Try modprobe fresh and capture the exact error
                 "echo '=== MODPROBE ===' && sudo modprobe nvidia 2>&1 && echo 'modprobe: OK' || echo 'modprobe: FAILED' ; "
                 # 2. Kernel module version (populated only if modprobe succeeded)
                 "echo '=== KMOD ===' && (cat /sys/module/nvidia/version 2>/dev/null || echo N/A) ; "
                 # 3. What the libnvidia-ml.so.1 symlink actually resolves to
                 "echo '=== MLSO symlink ===' && ls -la /lib/x86_64-linux-gnu/libnvidia-ml.so* 2>/dev/null || echo none ; "
                 # 4. All nvidia/libnvidia packages installed
                 "echo '=== dpkg nvidia ===' && dpkg -l 'nvidia-*' 'libnvidia-*' 2>/dev/null | grep '^ii' || echo none ; "
                 # 5. nvidia-smi output
                 "echo '=== nvidia-smi ===' && nvidia-smi 2>&1 | head -5 ; "
                 # 6. lsmod nvidia
                 "echo '=== lsmod ===' && lsmod | grep nvidia || echo 'not in lsmod'"],
                capture_output=True, text=True, timeout=60,
            )
            diag_text = _diag.stdout.strip() or _diag.stderr.strip() or "no output"
            result["nvidia_diag"] = diag_text[:800]
            # Write to a file on the local machine (toolbox) so it survives isvctl log swallowing.
            try:
                with open("/tmp/nvidia_diag.txt", "w") as _f:
                    _f.write(diag_text)
            except Exception:
                pass

    except ClientError as e:
        result["error"] = str(e)
        result["error_code"] = e.response.get("Error", {}).get("Code", "")
    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""EC2 helper utilities for zcompute.

zcompute-specific notes:
  - No boto3 waiters — ec2.get_waiter() will fail. Use custom polling.
  - VPC starts in 'pending' state — poll until 'available'.
  - No auto public IP — must allocate_address + associate_address.
  - PublicIpAddress may be empty string, "None", or None at launch.
  - StartInstances goes: stopped -> pending -> stopped -> pending -> running.
  - Root device is /dev/vda (not /dev/sda).
  - NVIDIA modules not auto-loaded — must modprobe after SSH.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

from botocore.exceptions import ClientError


def poll_instance_state(
    ec2: Any,
    instance_id: str,
    target_states: list[str],
    timeout: int = 600,
    interval: int = 15,
) -> str:
    """Poll until instance state is in target_states.

    For StartInstances on zcompute the sequence is:
      stopped -> pending -> stopped -> pending -> running
    so we never give up early on 'stopped' when 'running' is the target.

    Args:
        ec2:           Boto3 EC2 client.
        instance_id:   EC2 instance ID.
        target_states: List of acceptable terminal states (e.g. ['running']).
        timeout:       Maximum seconds to wait.
        interval:      Polling interval in seconds.

    Returns:
        Final instance state string.

    Raises:
        TimeoutError: If the instance does not reach a target state in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]
        print(f"[poll] instance {instance_id} state: {state}", file=sys.stderr)
        if state in target_states:
            return state
        time.sleep(interval)

    raise TimeoutError(
        f"Instance {instance_id} did not reach {target_states} within {timeout}s"
    )


def wait_for_public_ip(
    ec2: Any,
    instance_id: str,
    timeout: int = 120,
    interval: int = 5,
) -> str | None:
    """Poll describe_instances until PublicIpAddress is non-empty/non-None.

    zcompute requires a manual EIP allocation/association — the public IP is
    not assigned automatically. Call allocate_and_associate_eip first, then
    this helper to confirm the IP is visible.

    Args:
        ec2:         Boto3 EC2 client.
        instance_id: EC2 instance ID.
        timeout:     Maximum seconds to wait.
        interval:    Polling interval in seconds.

    Returns:
        Public IP string, or None if not available within timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        inst = resp["Reservations"][0]["Instances"][0]
        ip = inst.get("PublicIpAddress")
        if ip and ip not in ("", "None"):
            return ip
        print(
            f"[poll] waiting for public IP on {instance_id} ...",
            file=sys.stderr,
        )
        time.sleep(interval)
    return None


def create_key_pair(
    ec2: Any,
    key_name: str,
    key_dir: str | None = None,
) -> str:
    """Create a key pair and save the PEM to disk.

    Idempotent:
      - If the key pair exists in EC2 AND the local PEM file exists, reuse both.
      - If the key pair exists in EC2 but no local PEM, delete and recreate.
      - If the key pair does not exist, create fresh.

    Args:
        ec2:      Boto3 EC2 client.
        key_name: Name of the key pair.
        key_dir:  Directory to save the PEM file. Defaults to /tmp.

    Returns:
        Absolute path to the saved PEM file.
    """
    if key_dir is None:
        key_dir = "/tmp"

    key_file = os.path.join(key_dir, f"{key_name}.pem")

    # Check whether the key pair already exists in EC2.
    exists_in_ec2 = False
    try:
        resp = ec2.describe_key_pairs(KeyNames=[key_name])
        if resp.get("KeyPairs"):
            exists_in_ec2 = True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "InvalidKeyPair.NotFound":
            exists_in_ec2 = False
        else:
            raise

    if exists_in_ec2:
        if os.path.exists(key_file):
            print(
                f"[ec2] reusing existing key pair '{key_name}' and PEM {key_file}",
                file=sys.stderr,
            )
            return key_file
        else:
            # PEM is gone — delete the key pair so we can recreate it with new material.
            print(
                f"[ec2] key pair '{key_name}' exists in EC2 but PEM not found locally; "
                "deleting and recreating.",
                file=sys.stderr,
            )
            ec2.delete_key_pair(KeyName=key_name)

    resp = ec2.create_key_pair(KeyName=key_name)
    pem_material = resp["KeyMaterial"]
    os.makedirs(key_dir, exist_ok=True)
    with open(key_file, "w") as fh:
        fh.write(pem_material)
    os.chmod(key_file, 0o600)
    print(f"[ec2] created key pair '{key_name}', saved to {key_file}", file=sys.stderr)
    return key_file


def create_security_group(
    ec2: Any,
    vpc_id: str,
    name: str,
    description: str = "ISV NCP validation",
) -> str:
    """Create a security group with SSH ingress, or reuse an existing one.

    Idempotent — if a SG with the same name already exists in the VPC, it
    is returned without modification.

    Args:
        ec2:         Boto3 EC2 client.
        vpc_id:      VPC ID in which to create the SG.
        name:        Security group name.
        description: Human-readable description.

    Returns:
        Security group ID (sg-xxx).
    """
    # Check whether a SG with this name already exists in the VPC.
    try:
        resp = ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [name]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]
        )
        if resp.get("SecurityGroups"):
            sg_id = resp["SecurityGroups"][0]["GroupId"]
            print(
                f"[ec2] reusing existing security group '{name}' ({sg_id})",
                file=sys.stderr,
            )
            return sg_id
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "InvalidGroup.NotFound":
            raise

    # Create the security group.
    resp = ec2.create_security_group(
        GroupName=name,
        Description=description,
        VpcId=vpc_id,
    )
    sg_id = resp["GroupId"]
    print(f"[ec2] created security group '{name}' ({sg_id})", file=sys.stderr)

    # Authorize SSH ingress from anywhere.
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}],
            }
        ],
    )
    print(f"[ec2] authorized SSH ingress on {sg_id}", file=sys.stderr)
    return sg_id


def allocate_and_associate_eip(
    ec2: Any,
    instance_id: str,
) -> tuple[str, str]:
    """Allocate an Elastic IP and associate it with an instance.

    zcompute does not assign public IPs automatically — this step is
    required to make the instance reachable.

    Args:
        ec2:         Boto3 EC2 client.
        instance_id: EC2 instance ID.

    Returns:
        Tuple of (allocation_id, public_ip).
    """
    alloc_resp = ec2.allocate_address(Domain="vpc")
    allocation_id = alloc_resp["AllocationId"]
    public_ip = alloc_resp["PublicIp"]
    print(
        f"[ec2] allocated EIP {public_ip} ({allocation_id})",
        file=sys.stderr,
    )

    ec2.associate_address(InstanceId=instance_id, AllocationId=allocation_id)
    print(
        f"[ec2] associated EIP {public_ip} with {instance_id}",
        file=sys.stderr,
    )
    return allocation_id, public_ip


def load_nvidia_modules(host: str, user: str, key_file: str) -> bool:
    """SSH into instance and load NVIDIA kernel modules.

    zcompute does not auto-load NVIDIA modules at boot; they must be
    loaded explicitly with modprobe.

    Args:
        host:     IP or hostname of the instance.
        user:     SSH username (e.g. 'ubuntu').
        key_file: Path to the private key PEM file.

    Returns:
        True if modprobe succeeded (including 'already loaded'), False otherwise.
    """
    def _ssh(command: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",  # prevent known_hosts conflicts between VMs
             "-o", "ConnectTimeout=10",
             "-o", "BatchMode=yes",
             "-i", key_file, f"{user}@{host}", command],
            capture_output=True, text=True, timeout=timeout,
        )

    print(f"[ec2] loading NVIDIA modules on {host} ...", file=sys.stderr)
    result = _ssh("sudo modprobe nvidia nvidia-uvm nvidia-modeset")
    loaded = result.returncode == 0 or "already" in result.stderr.lower()

    if not loaded and "not found" in result.stderr.lower():
        # Module not built for the current kernel (kernel upgraded since AMI was built).
        # Search for a pre-built linux-modules-nvidia-*-server-<kernel> package and install it.
        print("[ec2] module not found — searching for pre-built NVIDIA kernel modules ...", file=sys.stderr)
        install_cmd = (
            "KERNEL=$(uname -r) && "
            "PKG=$(apt-cache search \"linux-modules-nvidia.*${KERNEL}\" 2>/dev/null | head -1 | awk '{print $1}') && "
            "if [ -n \"$PKG\" ]; then "
            "  echo \"[ec2] installing $PKG\" && "
            "  sudo apt-get install -y --no-install-recommends $PKG 2>&1 | tail -3; "
            "else "
            "  echo \"[ec2] no pre-built module package found, trying dkms autoinstall\" && "
            "  sudo dkms autoinstall 2>&1 | tail -5; "
            "fi"
        )
        install_result = _ssh(install_cmd, timeout=600)
        print(f"[ec2] install result: {install_result.stdout.strip()}", file=sys.stderr)
        result = _ssh("sudo modprobe nvidia nvidia-uvm nvidia-modeset")
        loaded = result.returncode == 0 or "already" in result.stderr.lower()

    if loaded:
        print("[ec2] NVIDIA modules loaded successfully", file=sys.stderr)
    else:
        print(
            f"[ec2] modprobe failed (rc={result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False

    # Locate nvidia-smi and symlink it to /usr/local/bin which is in PATH
    # for all session types including paramiko non-interactive sessions.
    locate_and_link = (
        "NVSMI=$(find /usr /opt -name nvidia-smi -type f 2>/dev/null | head -1); "
        "echo \"[nvidia] found nvidia-smi at: $NVSMI\"; "
        "if [ -n \"$NVSMI\" ]; then "
        "  sudo ln -sf \"$NVSMI\" /usr/local/bin/nvidia-smi && "
        "  echo \"[nvidia] symlinked $NVSMI -> /usr/local/bin/nvidia-smi\"; "
        "else "
        "  DRVER=$(dpkg -l | awk '/nvidia-kernel-common-[0-9]/{match($2,/[0-9]+/,m);print m[0];exit}'); "
        "  DRVER=${DRVER:-535}; "
        "  echo \"[nvidia] nvidia-smi not found, installing nvidia-utils-${DRVER}-server\"; "
        "  sudo apt-get install -y --no-install-recommends nvidia-utils-${DRVER}-server 2>&1 | tail -3; "
        "  sudo ln -sf /usr/bin/nvidia-smi /usr/local/bin/nvidia-smi 2>/dev/null || true; "
        "fi"
    )
    r = _ssh(locate_and_link, timeout=120)
    print(f"[ec2] nvidia-smi setup: {r.stdout.strip()}", file=sys.stderr)

    # Wait until nvidia-smi actually communicates with the driver.
    # After modprobe the kernel module can take a few seconds to fully
    # initialize before nvidia-smi can query GPUs successfully.
    wait_cmd = (
        "for i in $(seq 1 15); do "
        "  GPU=$(/usr/local/bin/nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1); "
        "  if [ -n \"$GPU\" ]; then "
        "    echo \"[nvidia] driver ready after $((i-1)) retries: $GPU\"; exit 0; "
        "  fi; "
        "  echo \"[nvidia] driver not ready yet (attempt $i/15), waiting 3s...\"; "
        "  sleep 3; "
        "done; "
        "echo \"[nvidia] WARNING: driver did not become ready after 45s\"; exit 1"
    )
    r = _ssh(wait_cmd, timeout=90)
    print(f"[ec2] nvidia-smi wait: {r.stdout.strip()}", file=sys.stderr)
    if r.returncode != 0:
        print("[ec2] warning: nvidia-smi did not report GPUs — driver may not be loaded", file=sys.stderr)

    # Persist modules across reboots by adding to /etc/modules.
    # This ensures nvidia-smi is available immediately after every boot
    # without requiring a manual modprobe step.
    persist_cmd = (
        "sudo sh -c '"
        "grep -q nvidia /etc/modules || "
        "printf \"nvidia\\nnvidia-uvm\\nnvidia-modeset\\n\" >> /etc/modules'"
    )
    persist_result = _ssh(persist_cmd)
    if persist_result.returncode == 0:
        print("[ec2] NVIDIA modules persisted in /etc/modules", file=sys.stderr)
    else:
        print(
            f"[ec2] warning: could not persist modules to /etc/modules: "
            f"{persist_result.stderr.strip()}",
            file=sys.stderr,
        )

    # Restart Docker so the NVIDIA container runtime picks up the freshly
    # loaded kernel modules. Without this restart, `docker run --gpus all`
    # fails with "could not select device driver" after stop/start or reboot.
    r = _ssh("sudo systemctl restart docker 2>/dev/null || true", timeout=30)
    print("[ec2] Docker restarted to pick up NVIDIA modules", file=sys.stderr)

    return True


def setup_gpu_dependencies(host: str, user: str, key_file: str) -> dict[str, bool]:
    """Install Docker, NVIDIA Container Toolkit, and CUDA toolkit via SSH.

    Run once at VM launch time. Takes ~10-15 min on first launch.
    For production, bake these into the base image instead.

    Returns a dict of {component: success} for each install step.
    """

    def _ssh(command: str, timeout: int = 600) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
             "-o", "BatchMode=yes", "-i", key_file, f"{user}@{host}", command],
            capture_output=True, text=True, timeout=timeout,
        )

    results: dict[str, bool] = {
        "docker": False,
        "nvidia_container_toolkit": False,
        "cuda_toolkit": False,
        "nvidia_smi_accessible": False,
    }

    # ── 0. Diagnostics: show nvidia version state before we touch anything ───
    diag = _ssh(
        "echo '=== KMOD ===' && (cat /sys/module/nvidia/version 2>/dev/null || echo 'not loaded') && "
        "echo '=== DPKG nvidia-utils ===' && (dpkg -l 'nvidia-utils-*' 2>/dev/null | grep '^ii' || echo 'none') && "
        "echo '=== DPKG linux-modules-nvidia ===' && (dpkg -l 'linux-modules-nvidia-*' 2>/dev/null | grep '^ii' || echo 'none') && "
        "echo '=== nvidia-smi ===' && (nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -1 || echo 'failed')"
    )
    print(f"[setup] pre-install nvidia state:\n{diag.stdout}", file=sys.stderr)

    # ── 0.5. Hold nvidia-utils before CUDA repo ──────────────────────────────
    # The CUDA apt repo ships newer nvidia-utils packages that mismatch the
    # already-loaded kernel module. Hold them now so apt never auto-upgrades.
    print("[setup] holding nvidia-utils packages to prevent CUDA repo upgrade ...", file=sys.stderr)
    _ssh(
        "dpkg -l 'nvidia-utils-*' 2>/dev/null | awk '/^ii/{print $2}' "
        "| xargs -r sudo apt-mark hold 2>/dev/null || true"
    )

    # ── 1. Docker CE ─────────────────────────────────────────────────────────
    print("[setup] installing Docker ...", file=sys.stderr)
    docker_cmds = (
        "sudo apt-get update -qq && "
        "sudo apt-get install -y --no-install-recommends "
        "  docker.io curl wget gnupg2 ca-certificates && "
        "sudo systemctl enable --now docker && "
        "sudo usermod -aG docker ubuntu"
    )
    r = _ssh(docker_cmds, timeout=300)
    results["docker"] = r.returncode == 0
    if results["docker"]:
        print("[setup] Docker installed successfully", file=sys.stderr)
    else:
        print(f"[setup] Docker install failed: {r.stderr[-300:]}", file=sys.stderr)

    # ── 2. CUDA Toolkit (via NVIDIA official apt repo) ───────────────────────
    # Install only nvcc + cuda libraries (not the full cuda-toolkit-12-6 which
    # is ~3GB and times out on slow connections). This satisfies DriverCheck's
    # cuda_toolkit subtest which checks for nvcc availability.
    print("[setup] adding NVIDIA CUDA apt repo and installing nvcc + CUDA libs ...", file=sys.stderr)
    cuda_cmds = (
        # Add NVIDIA CUDA keyring + repo
        "wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/"
        "cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb && "
        "sudo dpkg -i /tmp/cuda-keyring.deb && "
        "sudo apt-get update -qq && "
        # Install nvcc compiler + essential CUDA libraries (much smaller than cuda-toolkit-12-6)
        "sudo apt-get install -y --no-install-recommends "
        "  cuda-nvcc-12-6 cuda-libraries-12-6 libcufft-dev-12-6 libcurand-dev-12-6 && "
        # Add CUDA bin to system-wide PATH for all session types
        "echo 'export PATH=/usr/local/cuda/bin:$PATH' | sudo tee /etc/profile.d/cuda.sh && "
        "sudo ln -sf /usr/local/cuda/bin/nvcc /usr/local/bin/nvcc 2>/dev/null || true"
    )
    r = _ssh(cuda_cmds, timeout=1800)
    check = _ssh(
        "nvcc --version 2>/dev/null || /usr/local/cuda/bin/nvcc --version 2>/dev/null || "
        "/usr/local/bin/nvcc --version 2>/dev/null",
        timeout=30,
    )
    results["cuda_toolkit"] = check.returncode == 0
    if results["cuda_toolkit"]:
        print(f"[setup] CUDA Toolkit installed: {check.stdout.strip()[:80]}", file=sys.stderr)
    else:
        print(f"[setup] CUDA Toolkit install failed: {r.stderr[-300:]}", file=sys.stderr)

    # ── 3. NVIDIA Container Toolkit ──────────────────────────────────────────
    # Install after CUDA so the apt-get update doesn't overwrite our CUDA repo.
    print("[setup] installing NVIDIA Container Toolkit ...", file=sys.stderr)
    nct_cmds = (
        "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey "
        "  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && "
        "curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list "
        "  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list && "
        "sudo apt-get update -qq && "
        "sudo apt-get install -y nvidia-container-toolkit && "
        "sudo nvidia-ctk runtime configure --runtime=docker && "
        "sudo systemctl restart docker"
    )
    r = _ssh(nct_cmds, timeout=300)
    results["nvidia_container_toolkit"] = r.returncode == 0
    if results["nvidia_container_toolkit"]:
        print("[setup] NVIDIA Container Toolkit installed successfully", file=sys.stderr)
    else:
        print(f"[setup] NVIDIA Container Toolkit install failed: {r.stderr[-300:]}", file=sys.stderr)

    # ── 4. Unhold and pin nvidia-utils to the loaded kernel module version ───
    # Unhold (we held in step 0.5 to prevent CUDA repo from upgrading).
    # Then force-install nvidia-utils matching the actual loaded kernel module.
    # We try both -server and non-server package name variants.
    print("[setup] pinning nvidia-utils to loaded kernel module version ...", file=sys.stderr)
    restore_cmds = (
        # Unhold first so apt can act on these packages again.
        "dpkg -l 'nvidia-utils-*' 2>/dev/null | awk '/^ii/{print $2}' "
        "| xargs -r sudo apt-mark unhold 2>/dev/null || true && "
        # Read the exact version from the already-loaded kernel module.
        "KMOD_VER=$(cat /sys/module/nvidia/version 2>/dev/null || echo '') && "
        "KMOD_MAJOR=$(echo \"$KMOD_VER\" | cut -d. -f1) && "
        "echo \"[setup] kernel module version: ${KMOD_VER:-unknown}, major: ${KMOD_MAJOR:-unknown}\" && "
        # If we know the kernel module version, force-install matching nvidia-utils.
        # Try -server first (Ubuntu HWE repos), fall back to plain name (CUDA repo).
        "if [ -n \"$KMOD_MAJOR\" ]; then "
        "  echo \"[setup] force-installing nvidia-utils-${KMOD_MAJOR} variants\" && "
        "  if sudo apt-get install -y --allow-downgrades --no-install-recommends "
        "       nvidia-utils-${KMOD_MAJOR}-server 2>&1; then "
        "    echo \"[setup] installed nvidia-utils-${KMOD_MAJOR}-server OK\"; "
        "  elif sudo apt-get install -y --allow-downgrades --no-install-recommends "
        "       nvidia-utils-${KMOD_MAJOR} 2>&1; then "
        "    echo \"[setup] installed nvidia-utils-${KMOD_MAJOR} OK\"; "
        "  else "
        "    echo \"[setup] WARNING: could not install nvidia-utils for major ${KMOD_MAJOR}\"; "
        "  fi; "
        "else "
        "  echo '[setup] kernel module not loaded, falling back to dpkg heuristic' && "
        "  DRVER=$(dpkg -l | awk '/nvidia-kernel-common-[0-9]/{match($2,/[0-9]+/,m);print m[0];exit}') && "
        "  DRVER=${DRVER:-535} && "
        "  sudo apt-get install -y --allow-downgrades --no-install-recommends "
        "    nvidia-utils-${DRVER}-server 2>&1 || "
        "  sudo apt-get install -y --allow-downgrades --no-install-recommends "
        "    nvidia-utils-${DRVER} 2>&1 || true; "
        "fi && "
        # Refresh ldconfig so the new library symlinks are picked up immediately.
        "sudo ldconfig && "
        # Symlink nvidia-smi into /usr/local/bin for all SSH session types.
        "NVSMI=$(find /usr /opt -name nvidia-smi -type f 2>/dev/null | head -1) && "
        "echo \"[setup] nvidia-smi at: $NVSMI\" && "
        "[ -n \"$NVSMI\" ] && sudo ln -sf \"$NVSMI\" /usr/local/bin/nvidia-smi || true && "
        # Restart docker so the NVIDIA container runtime picks up the correct library.
        "sudo systemctl restart docker 2>/dev/null || true && "
        "echo '[setup] docker restarted after nvidia-utils pin' && "
        # Final verification — output will appear in launch_instance JSON via nvidia_diag.
        "echo \"[setup] nvidia-smi final check: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -1)\""
    )
    r = _ssh(restore_cmds, timeout=300)
    results["nvidia_smi_accessible"] = r.returncode == 0
    print(f"[setup] nvidia-smi restore: {r.stdout.strip()}", file=sys.stderr)

    # ── 5. Post-install diagnostics: verify version consistency ─────────────
    diag2 = _ssh(
        "echo '=== KMOD ===' && (cat /sys/module/nvidia/version 2>/dev/null || echo 'not loaded') && "
        "echo '=== DPKG nvidia-utils ===' && (dpkg -l 'nvidia-utils-*' 2>/dev/null | grep '^ii' || echo 'none') && "
        "echo '=== DPKG linux-modules-nvidia ===' && (dpkg -l 'linux-modules-nvidia-*' 2>/dev/null | grep '^ii' | head -3 || echo 'none') && "
        "echo '=== nvidia-smi ===' && (nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -1 || echo 'failed')"
    )
    print(f"[setup] post-install nvidia state:\n{diag2.stdout}", file=sys.stderr)

    print(f"[setup] GPU dependencies complete: {results}", file=sys.stderr)
    return results

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

"""Shared Slurm utilities and helpers."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from isvtest.core.validation import BaseValidation

# =============================================================================
# Constants
# =============================================================================

# Terminal job states that indicate a job has finished (success or failure)
TERMINAL_STATES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "NODE_FAIL",
        "PREEMPTED",
        "OUT_OF_MEMORY",
    }
)

# Default timeout for job completion in seconds
DEFAULT_JOB_TIMEOUT = 600

# Default interval between job status checks in seconds
DEFAULT_POLL_INTERVAL = 10

# Default timeout per node for node-level tests in seconds
DEFAULT_NODE_TIMEOUT = 60

# Delay to allow NFS file sync before reading job output (seconds)
NFS_SYNC_DELAY = 1

# Path to Slurm manifest templates
MANIFESTS_DIR = Path(__file__).parent.parent / "workloads" / "manifests" / "slurm"

# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class PartitionInfo:
    """Structured information about a Slurm partition."""

    name: str
    avail: str
    timelimit: str
    node_count: int
    nodelist: str
    nodes: list[str]  # expanded node names


@dataclass
class JobInfo:
    """Structured information about a Slurm job from scontrol."""

    job_id: str
    state: str
    exit_code: int
    nodelist: str
    batch_host: str
    stdout_path: str
    stderr_path: str
    work_dir: str


@dataclass
class JobResult:
    """Result of a Slurm job submission and execution.

    Consolidates job metadata (from scontrol) with execution results.
    Used by both sbatch workloads and other job-based validations.
    """

    job_id: str
    success: bool
    state: str = ""
    exit_code: int = 0
    output: str = ""
    error: str = ""
    duration: float = 0.0
    nodelist: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_scontrol_job(output: str, job_id: str = "") -> JobInfo:
    """Parse scontrol show job output into structured data.

    Args:
        output: Raw output from 'scontrol show job <id>'.
        job_id: Optional job ID (used if not parsed from output).

    Returns:
        JobInfo with parsed fields. Fields default to empty/0 if not found.
    """

    def get_field(pattern: str, default: str = "") -> str:
        """Extract a field value from scontrol output."""
        match = re.search(pattern, output, re.MULTILINE)
        if match:
            val = match.group(1)
            return "" if val == "(null)" else val
        return default

    # Parse JobId if not provided (relaxed whitespace for Slurm version compatibility)
    # Use \S+ to handle array/step IDs like "12345_1" or "12345.batch"
    if not job_id:
        job_id = get_field(r"^\s*JobId=(\S+)", "")

    # Parse state (use \S+ to capture compound states like CANCELLED+)
    state = get_field(r"^\s*JobState=(\S+)", "UNKNOWN")

    # Parse exit code (format: ExitCode=0:0, on shared line)
    exit_match = re.search(r"\bExitCode=(\d+):\d+", output)
    exit_code = int(exit_match.group(1)) if exit_match else 0

    # Parse node info (NodeList is standalone, BatchHost for batch jobs)
    nodelist = get_field(r"^\s*NodeList=(\S+)")
    batch_host = get_field(r"^\s*BatchHost=(\S+)")

    # Parse output paths
    stdout_path = get_field(r"^\s*StdOut=(\S+)")
    stderr_path = get_field(r"^\s*StdErr=(\S+)")
    work_dir = get_field(r"^\s*WorkDir=(\S+)")

    # Substitute %j with job_id in paths
    if job_id:
        stdout_path = stdout_path.replace("%j", job_id)
        stderr_path = stderr_path.replace("%j", job_id)

    return JobInfo(
        job_id=job_id,
        state=state,
        exit_code=exit_code,
        nodelist=nodelist,
        batch_host=batch_host,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        work_dir=work_dir,
    )


def expand_nodelist(nodelist: str) -> list[str]:
    """Expand Slurm nodelist notation to individual node names.

    Examples:
        "node[1-3]" -> ["node1", "node2", "node3"]
        "gpu-n[1-2],cpu-n1" -> ["gpu-n1", "gpu-n2", "cpu-n1"]
        "node1,node2" -> ["node1", "node2"]
    """
    if not nodelist:
        return []

    nodes: list[str] = []
    # Split by comma, but handle brackets
    parts = re.split(r",(?![^\[]*\])", nodelist)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Check for range notation: prefix[start-end]suffix or prefix[a,b,c]suffix
        match = re.match(r"^(.+?)\[([^\]]+)\](.*)$", part)
        if match:
            prefix, range_spec, suffix = match.groups()
            # Handle comma-separated values in brackets
            for item in range_spec.split(","):
                if "-" in item:
                    # Range like "1-3"
                    range_match = re.match(r"(\d+)-(\d+)", item)
                    if range_match:
                        start, end = int(range_match.group(1)), int(range_match.group(2))
                        # Preserve leading zeros
                        width = len(range_match.group(1))
                        for i in range(start, end + 1):
                            nodes.append(f"{prefix}{str(i).zfill(width)}{suffix}")
                else:
                    # Single value like "1"
                    nodes.append(f"{prefix}{item}{suffix}")
        else:
            # No bracket notation, just a node name
            nodes.append(part)

    return nodes


def parse_sinfo_output(output: str) -> dict[str, PartitionInfo]:
    """Parse sinfo output into structured partition data.

    Expects output from: sinfo -o '%P %a %l %D %N'
    Format: PARTITION AVAIL TIMELIMIT NODES NODELIST

    Args:
        output: Raw sinfo command output.

    Returns:
        Dictionary mapping partition names to PartitionInfo objects.
    """
    partitions: dict[str, PartitionInfo] = {}
    lines = output.strip().split("\n")

    for line in lines[1:]:  # Skip header
        parts = line.split()
        if len(parts) >= 4:
            # Remove trailing * from default partition name
            name = parts[0].rstrip("*")
            nodelist = parts[4] if len(parts) > 4 else ""
            partitions[name] = PartitionInfo(
                name=name,
                avail=parts[1],
                timelimit=parts[2],
                node_count=int(parts[3]),
                nodelist=nodelist,
                nodes=expand_nodelist(nodelist),
            )

    return partitions


def get_partitions(validator: "BaseValidation") -> dict[str, PartitionInfo] | None:
    """Run sinfo and return structured partition data.

    Args:
        validator: BaseValidation instance for running commands.

    Returns:
        Dictionary mapping partition names to PartitionInfo objects,
        or None if sinfo command fails.
    """
    result = validator.run_command("sinfo -o '%P %a %l %D %N'")

    if result.exit_code != 0:
        return None

    return parse_sinfo_output(result.stdout)


def get_partition_names(output: str) -> list[str]:
    """Extract partition names from sinfo output.

    A lightweight parser when only names are needed.

    Args:
        output: Raw sinfo command output.

    Returns:
        List of partition names (without the default partition * suffix).
    """
    lines = [line for line in output.split("\n") if line.strip()]
    return [line.split()[0].rstrip("*") for line in lines[1:] if line.split()]


def get_partition_nodes(validator: "BaseValidation", partition_name: str) -> list[str] | None:
    """Get list of nodes in a partition using sinfo.

    Args:
        validator: BaseValidation instance for running commands and error handling.
        partition_name: Name of the Slurm partition.

    Returns:
        List of node names, empty list if partition has no nodes,
        or None if the sinfo command fails (error is set on validator).
    """
    result = validator.run_command(f"sinfo -p {partition_name} -h -o '%N'")
    if result.exit_code != 0:
        validator.set_failed(f"Failed to get nodes for partition '{partition_name}': {result.stderr}")
        return None
    raw = result.stdout.strip()
    if not raw:
        return []
    nodes: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        nodes.extend(expand_nodelist(line))
    return nodes


def read_remote_file(
    validator: "BaseValidation",
    file_path: str,
    node: str | None = None,
    cleanup: bool = False,
    user: str | None = None,
) -> str:
    """Read a file, trying local access first then SSH to a node.

    Args:
        validator: BaseValidation instance for running commands.
        file_path: Path to the file to read.
        node: Optional node to SSH to if local access fails.
        cleanup: Whether to delete the file after reading.
        user: SSH user (auto-detected from /home/<user>/... paths if not specified).

    Returns:
        File contents, or empty string if file couldn't be read.
    """
    # Try local access first (shared filesystem)
    result = validator.run_command(f"cat '{file_path}' 2>/dev/null", timeout=30)
    if result.exit_code == 0 and result.stdout:
        validator.log.info(f"Read file locally: {file_path}")
        if cleanup:
            validator.run_command(f"rm -f '{file_path}'", timeout=10)
        return result.stdout

    # Fall back to SSH if node provided
    if node:
        # Auto-detect user from /home/<user>/... paths if not specified
        ssh_user = user
        if not ssh_user:
            home_match = re.match(r"/home/([^/]+)/", file_path)
            if home_match:
                ssh_user = home_match.group(1)

        ssh_target = f"{ssh_user}@{node}" if ssh_user else node

        # Build SSH options - include identity file if we detected the user from path
        # This allows root to SSH as the detected user using their key
        ssh_opts = "-o StrictHostKeyChecking=no -o ConnectTimeout=10"
        if ssh_user:
            ssh_opts += f" -i /home/{ssh_user}/.ssh/id_rsa -i /home/{ssh_user}/.ssh/id_ed25519"

        validator.log.info(f"Trying SSH to {ssh_target} to read {file_path}...")
        result = validator.run_command(
            f"ssh {ssh_opts} {ssh_target} 'cat {file_path}'",
            timeout=30,
        )
        if result.exit_code == 0 and result.stdout:
            validator.log.info(f"Read file via SSH from {ssh_target}:{file_path}")
            if cleanup:
                validator.run_command(
                    f"ssh {ssh_opts} {ssh_target} 'rm -f {file_path}'",
                    timeout=10,
                )
            return result.stdout
        elif result.exit_code != 0:
            validator.log.warning(f"SSH to {ssh_target} failed (exit={result.exit_code}): {result.stderr}")

    return ""


def get_first_node(nodelist: str) -> str:
    """Extract the first node name from a Slurm nodelist.

    Handles formats like "node1", "node[1-3]", "node1,node2".

    Args:
        nodelist: Slurm nodelist string.

    Returns:
        First node name, or empty string if nodelist is empty.
    """
    if not nodelist:
        return ""
    nodes = expand_nodelist(nodelist)
    return nodes[0] if nodes else ""


def is_gpu_partition(validator: "BaseValidation", partition_name: str) -> bool:
    """Determine if partition is GPU-based by checking GRES (Generic Resources).

    Falls back to heuristic based on partition name if GRES query fails.
    """
    # Check GRES (Generic Resources)
    # -h: no header
    # -o %G: print GRES
    result = validator.run_command(f"sinfo -p {partition_name} -h -o %G")

    if result.exit_code == 0:
        # If "gpu" is present in GRES column, it's a GPU partition
        if "gpu" in result.stdout.lower():
            validator.log.debug(f"Partition '{partition_name}' detected as GPU-based (GRES: {result.stdout.strip()})")
            return True

        validator.log.debug(
            f"Partition '{partition_name}' detected as CPU-based (No 'gpu' in GRES: {result.stdout.strip()})"
        )
        return False

    validator.log.warning(
        f"Failed to query GRES for partition '{partition_name}': {result.stderr}. Falling back to name heuristic."
    )

    name_lower = partition_name.lower()
    # If name contains 'cpu' explicitly, it's not a GPU partition
    if "cpu" in name_lower:
        return False
    # If name contains 'gpu', it's a GPU partition
    if "gpu" in name_lower:
        return True
    # Default: assume GPU partition
    return True


def detect_container_runtime(validator: "BaseValidation") -> str:
    """Detect the best available container runtime for Slurm workloads.

    Probes the Slurm environment and falls back to local binary detection.
    Prioritizes MPI-capable runtimes that integrate with srun for true
    multi-node communication:

    1. pyxis/enroot - best Slurm integration (``--container-image`` via SPANK plugin)
    2. singularity/apptainer - native srun support
    3. docker - fallback (no native srun MPI integration)

    Args:
        validator: BaseValidation instance for running commands.

    Returns:
        Runtime name: ``"enroot"``, ``"singularity"``, or ``"docker"``.
        Defaults to ``"docker"`` if nothing else is detected.
    """
    # 1. Check for enroot via pyxis SPANK plugin (provides --container-image to srun)
    result = validator.run_command("srun --help 2>&1", timeout=10)
    if result.exit_code == 0:
        help_text = f"{result.stdout}\n{result.stderr}"
        if "--container-image" in help_text:
            validator.log.info("Detected enroot runtime (srun --container-image available via pyxis)")
            return "enroot"

    # 2. Check for singularity/apptainer
    for name in ("singularity", "apptainer"):
        result = validator.run_command(f"which {name} 2>/dev/null", timeout=5)
        if result.exit_code == 0:
            validator.log.info(f"Detected {name} container runtime")
            return "singularity"

    # 3. Check for docker
    result = validator.run_command("which docker 2>/dev/null", timeout=5)
    if result.exit_code == 0:
        validator.log.info("Detected docker container runtime")
        return "docker"

    # 4. Default to docker (may be available on compute nodes even if not on login node)
    validator.log.warning(
        "No container runtime detected locally, defaulting to docker (may be available on compute nodes)"
    )
    return "docker"


def get_partition_gpus_per_node(validator: "BaseValidation", partition_name: str) -> int | None:
    """Get the number of GPUs per node in a partition from GRES.

    Parses GRES output like "gpu:8", "gpu:a100:8", "gpu:nvidia_a100:8",
    or "gpu:8(S:0-7)" (with socket info suffix).

    Args:
        validator: BaseValidation instance for running commands.
        partition_name: Name of the Slurm partition.

    Returns:
        Number of GPUs per node, or None if cannot be determined.
    """
    result = validator.run_command(f"sinfo -p {partition_name} -h -o %G")

    if result.exit_code != 0:
        validator.log.warning(f"Failed to query GRES for partition '{partition_name}': {result.stderr}")
        return None

    raw_gres = result.stdout.strip()
    if not raw_gres:
        return None

    # Handle multi-line output: normalize to single line, split on whitespace/newlines
    # sinfo may return one line per node with different GRES
    gres_entries = raw_gres.replace("\n", " ").replace("\r", " ").split()

    for gres in gres_entries:
        gres = gres.strip()

        # Skip (null) entries
        if not gres or gres.lower() == "(null)" or "gpu" not in gres.lower():
            continue

        # Parse GRES format: gpu:N or gpu:type:N or gpu:N(S:0-7)
        # Examples: "gpu:8", "gpu:a100:8", "gpu:nvidia_a100:8", "gpu:8(S:0-7)"
        for part in gres.split(","):
            part = part.strip()
            if not part.lower().startswith("gpu"):
                continue

            # Strip any parenthesized suffix like "(S:0-7)" before parsing
            paren_idx = part.find("(")
            if paren_idx != -1:
                part = part[:paren_idx]

            # Split by colon and find the numeric part (usually last)
            segments = part.split(":")
            for seg in reversed(segments):
                if seg.isdigit():
                    gpu_count = int(seg)
                    validator.log.debug(
                        f"Partition '{partition_name}' has {gpu_count} GPUs per node (GRES: {raw_gres})"
                    )
                    return gpu_count

    validator.log.warning(f"Could not parse GPU count from GRES '{raw_gres}' for partition '{partition_name}'")
    return None


# =============================================================================
# Job Submission and Monitoring Utilities
# =============================================================================


def parse_sbatch_job_id(output: str) -> str | None:
    """Parse job ID from sbatch command output.

    Args:
        output: stdout from sbatch command (e.g., "Submitted batch job 12345").

    Returns:
        Job ID string, or None if parsing failed.
    """
    match = re.search(r"Submitted batch job (\d+)", output)
    return match.group(1) if match else None


def get_job_state(
    validator: "BaseValidation",
    job_id: str,
    use_sacct: bool = True,
) -> tuple[str, int, str, bool]:
    """Get job state, exit code, and nodelist using sacct or squeue/scontrol.

    Automatically falls back from sacct to squeue/scontrol if accounting
    is disabled.

    Args:
        validator: BaseValidation instance for running commands.
        job_id: The Slurm job ID.
        use_sacct: Whether to try sacct first (set False to skip).

    Returns:
        Tuple of (state, exit_code, nodelist, sacct_available).
        sacct_available indicates if sacct worked (for caching in loops).
    """
    if use_sacct:
        result = validator.run_command(
            f"sacct -j {job_id} --format=JobID,State,ExitCode,Elapsed,NodeList --noheader --parsable2",
            timeout=30,
        )

        if result.exit_code == 0 and "accounting storage is disabled" not in result.stderr.lower():
            # Parse sacct output
            for line in result.stdout.strip().splitlines():
                parts = line.split("|")
                if len(parts) >= 4 and parts[0] == job_id:
                    state = parts[1]
                    exit_code = 0
                    if ":" in parts[2]:
                        exit_code = int(parts[2].split(":")[0])
                    elif parts[2].isdigit():
                        exit_code = int(parts[2])
                    nodelist = parts[4] if len(parts) >= 5 and parts[4] not in ("", "(null)") else ""
                    return state, exit_code, nodelist, True

            # sacct worked but no matching job line yet (job may be pending)
            return "", 0, "", True

        validator.log.info("sacct unavailable, falling back to squeue/scontrol")

    # Fallback: squeue/scontrol
    state, exit_code, nodelist = _get_job_state_squeue(validator, job_id)
    return state, exit_code, nodelist, False


def _get_job_state_squeue(
    validator: "BaseValidation",
    job_id: str,
) -> tuple[str, int, str]:
    """Get job state using squeue/scontrol (fallback when sacct unavailable).

    Args:
        validator: BaseValidation instance for running commands.
        job_id: The Slurm job ID.

    Returns:
        Tuple of (state, exit_code, nodelist).
    """
    # Check if job is still in the queue
    result = validator.run_command(f"squeue -j {job_id} -h -o '%T'", timeout=30)

    if result.exit_code == 0 and result.stdout.strip():
        return result.stdout.strip(), 0, ""

    # Job not in queue - use scontrol to get final state
    result = validator.run_command(f"scontrol show job {job_id}", timeout=30)

    if result.exit_code != 0:
        # Job may have been purged from scontrol; check for output file as evidence it ran
        out_check = validator.run_command(f"test -f slurm-{job_id}.out", timeout=10)
        if out_check.exit_code == 0:
            validator.log.warning(f"Job {job_id} purged from scontrol, output file exists")
            # Return COMPLETED to signal terminal state, but exit_code=1 to indicate
            # we couldn't verify actual success. Callers check both state AND exit_code.
            return "COMPLETED", 1, ""
        return "UNKNOWN", 1, ""

    job_info = parse_scontrol_job(result.stdout, job_id)
    nodelist = job_info.nodelist or job_info.batch_host

    validator.log.debug(
        f"Job {job_id} scontrol: state={job_info.state}, exit_code={job_info.exit_code}, nodelist='{nodelist}'"
    )
    return job_info.state, job_info.exit_code, nodelist


def get_job_output(
    validator: "BaseValidation",
    job_id: str,
    nodelist: str = "",
    cleanup: bool = False,
) -> tuple[str, str]:
    """Retrieve job stdout/stderr output.

    Uses scontrol to find output file paths, falls back to default naming.
    Uses SSH to compute node if local access fails.

    Args:
        validator: BaseValidation instance for running commands.
        job_id: The Slurm job ID.
        nodelist: Slurm nodelist (for SSH fallback if no shared filesystem).
        cleanup: Whether to delete output files after reading.

    Returns:
        Tuple of (stdout_content, stderr_content).
    """
    import time

    time.sleep(NFS_SYNC_DELAY)  # Allow file sync on NFS

    stdout_path = f"slurm-{job_id}.out"
    stderr_path = f"slurm-{job_id}.err"
    node = ""

    # Try to get paths from scontrol
    result = validator.run_command(f"scontrol show job {job_id}", timeout=30)
    if result.exit_code == 0:
        job_info = parse_scontrol_job(result.stdout, job_id)
        if job_info.stdout_path:
            stdout_path = job_info.stdout_path
        if job_info.stderr_path:
            stderr_path = job_info.stderr_path
        if not nodelist:
            nodelist = job_info.nodelist or job_info.batch_host

    node = get_first_node(nodelist) if nodelist else None

    # Read files
    stdout_content = read_remote_file(validator, stdout_path, node=node, cleanup=cleanup)

    stderr_content = ""
    if stderr_path and stderr_path != stdout_path:
        stderr_content = read_remote_file(validator, stderr_path, node=node, cleanup=cleanup)

    if not stdout_content and nodelist:
        validator.log.warning(f"Could not retrieve job output from {stdout_path} (tried local and SSH to {nodelist})")

    return stdout_content, stderr_content


def load_manifest_template(name: str) -> str:
    """Load a script template from the manifests/slurm directory.

    Args:
        name: Template filename (e.g., "gpu_node_test.sh").

    Returns:
        Template content as string.

    Raises:
        FileNotFoundError: If template doesn't exist.
    """
    path = MANIFESTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Manifest template not found: {path}")
    return path.read_text(encoding="utf-8")

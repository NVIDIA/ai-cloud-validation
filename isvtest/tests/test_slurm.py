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

"""Tests for Slurm utility functions."""

from unittest.mock import MagicMock

from isvtest.core.runners import CommandResult
from isvtest.core.slurm import (
    detect_container_runtime,
    expand_nodelist,
    get_partition_names,
    parse_sbatch_job_id,
    parse_scontrol_job,
    parse_sinfo_output,
)


def _make_validator(commands: dict[str, CommandResult]) -> MagicMock:
    """Create a mock validator that returns predefined results for commands.

    Args:
        commands: Mapping of command substring to CommandResult.
            The first matching substring (checked in insertion order) wins.
    """
    validator = MagicMock()
    validator.log = MagicMock()

    def fake_run_command(cmd: str, **kwargs: object) -> CommandResult:
        for pattern, result in commands.items():
            if pattern in cmd:
                return result
        return CommandResult(exit_code=1, stdout="", stderr="command not found", duration=0.0)

    validator.run_command = MagicMock(side_effect=fake_run_command)
    return validator


class TestExpandNodelist:
    """Tests for expand_nodelist function."""

    def test_empty_nodelist(self) -> None:
        """Test that empty nodelist returns empty list."""
        assert expand_nodelist("") == []
        assert expand_nodelist(None) == []  # type: ignore[arg-type]

    def test_single_node(self) -> None:
        """Test single node without bracket notation."""
        assert expand_nodelist("node1") == ["node1"]
        assert expand_nodelist("gpu-worker-01") == ["gpu-worker-01"]

    def test_comma_separated_nodes(self) -> None:
        """Test comma-separated node list."""
        assert expand_nodelist("node1,node2,node3") == ["node1", "node2", "node3"]

    def test_range_notation(self) -> None:
        """Test bracket range notation."""
        assert expand_nodelist("node[1-3]") == ["node1", "node2", "node3"]
        assert expand_nodelist("gpu[01-03]") == ["gpu01", "gpu02", "gpu03"]

    def test_range_preserves_leading_zeros(self) -> None:
        """Test that leading zeros are preserved in ranges."""
        assert expand_nodelist("node[001-003]") == ["node001", "node002", "node003"]

    def test_bracket_with_comma_values(self) -> None:
        """Test bracket notation with comma-separated values."""
        assert expand_nodelist("node[1,3,5]") == ["node1", "node3", "node5"]

    def test_mixed_notation(self) -> None:
        """Test mixed bracket and comma notation."""
        result = expand_nodelist("gpu-n[1-2],cpu-n1")
        assert "gpu-n1" in result
        assert "gpu-n2" in result
        assert "cpu-n1" in result

    def test_prefix_and_suffix(self) -> None:
        """Test bracket notation with prefix and suffix."""
        assert expand_nodelist("rack1-node[1-2]-gpu") == [
            "rack1-node1-gpu",
            "rack1-node2-gpu",
        ]

    def test_whitespace_handling(self) -> None:
        """Test that whitespace in parts is handled."""
        result = expand_nodelist("node1, node2")
        assert "node1" in result
        assert "node2" in result


class TestParseScontrolJob:
    """Tests for parse_scontrol_job function."""

    def test_parse_basic_job_info(self) -> None:
        """Test parsing basic scontrol output."""
        output = """   JobId=12345 JobName=test_job
   UserId=user(1000) GroupId=group(1000)
   JobState=COMPLETED Reason=None
   ExitCode=0:0
   NodeList=node1
   BatchHost=node1
   StdOut=/home/user/job-12345.out
   StdErr=/home/user/job-12345.err
   WorkDir=/home/user"""

        result = parse_scontrol_job(output, "12345")

        assert result.job_id == "12345"
        assert result.state == "COMPLETED"
        assert result.exit_code == 0
        assert result.nodelist == "node1"
        assert result.batch_host == "node1"
        assert result.stdout_path == "/home/user/job-12345.out"
        assert result.stderr_path == "/home/user/job-12345.err"
        assert result.work_dir == "/home/user"

    def test_parse_job_id_from_output(self) -> None:
        """Test that job ID is parsed from output when not provided."""
        output = """   JobId=99999 JobName=my_job
   JobState=RUNNING"""

        result = parse_scontrol_job(output)
        assert result.job_id == "99999"

    def test_substitutes_job_id_in_paths(self) -> None:
        """Test that %j is substituted with job ID in paths."""
        output = """   JobId=12345 JobName=test
   JobState=COMPLETED
   ExitCode=0:0
   StdOut=/home/user/slurm-%j.out
   StdErr=/home/user/slurm-%j.err"""

        result = parse_scontrol_job(output, "12345")
        assert result.stdout_path == "/home/user/slurm-12345.out"
        assert result.stderr_path == "/home/user/slurm-12345.err"

    def test_handles_null_values(self) -> None:
        """Test that (null) values are converted to empty strings."""
        output = """   JobId=12345 JobName=test
   JobState=PENDING
   NodeList=(null)
   BatchHost=(null)"""

        result = parse_scontrol_job(output, "12345")
        assert result.nodelist == ""
        assert result.batch_host == ""

    def test_handles_cancelled_plus_state(self) -> None:
        """Test that compound states like CANCELLED+ are parsed."""
        output = """   JobId=12345 JobName=test
   JobState=CANCELLED+"""

        result = parse_scontrol_job(output, "12345")
        assert result.state == "CANCELLED+"

    def test_handles_missing_fields(self) -> None:
        """Test that missing fields default appropriately."""
        output = "   JobId=12345 JobName=test"

        result = parse_scontrol_job(output, "12345")
        assert result.state == "UNKNOWN"
        assert result.exit_code == 0
        assert result.nodelist == ""
        assert result.stdout_path == ""


class TestParseSinfoOutput:
    """Tests for parse_sinfo_output function."""

    def test_parse_basic_sinfo_output(self) -> None:
        """Test parsing basic sinfo output."""
        output = """PARTITION AVAIL TIMELIMIT NODES NODELIST
batch up 1-00:00:00 4 node[1-4]
gpu* up infinite 2 gpu[1-2]"""

        result = parse_sinfo_output(output)

        assert "batch" in result
        assert result["batch"].name == "batch"
        assert result["batch"].avail == "up"
        assert result["batch"].timelimit == "1-00:00:00"
        assert result["batch"].node_count == 4
        assert len(result["batch"].nodes) == 4

        assert "gpu" in result
        assert result["gpu"].node_count == 2

    def test_removes_default_partition_asterisk(self) -> None:
        """Test that asterisk is removed from default partition name."""
        output = """PARTITION AVAIL TIMELIMIT NODES NODELIST
default* up infinite 1 node1"""

        result = parse_sinfo_output(output)
        assert "default" in result
        assert "default*" not in result

    def test_handles_empty_output(self) -> None:
        """Test handling of empty/header-only output."""
        output = "PARTITION AVAIL TIMELIMIT NODES NODELIST"
        result = parse_sinfo_output(output)
        assert result == {}


class TestGetPartitionNames:
    """Tests for get_partition_names function."""

    def test_extracts_partition_names(self) -> None:
        """Test extraction of partition names from sinfo output."""
        output = """PARTITION AVAIL TIMELIMIT NODES NODELIST
batch up 1-00:00:00 4 node[1-4]
gpu up infinite 2 gpu[1-2]
debug* up 1:00:00 1 debug1"""

        result = get_partition_names(output)

        assert "batch" in result
        assert "gpu" in result
        assert "debug" in result
        assert "debug*" not in result

    def test_handles_empty_output(self) -> None:
        """Test handling of empty output."""
        assert get_partition_names("") == []

    def test_handles_header_only(self) -> None:
        """Test handling of header-only output."""
        output = "PARTITION AVAIL TIMELIMIT NODES NODELIST"
        assert get_partition_names(output) == []


class TestParseSbatchJobId:
    """Tests for parse_sbatch_job_id function."""

    def test_parse_standard_output(self) -> None:
        """Test parsing standard sbatch output."""
        output = "Submitted batch job 12345"
        assert parse_sbatch_job_id(output) == "12345"

    def test_parse_large_job_id(self) -> None:
        """Test parsing large job IDs."""
        output = "Submitted batch job 9876543210"
        assert parse_sbatch_job_id(output) == "9876543210"

    def test_parse_with_extra_text(self) -> None:
        """Test parsing with extra text around the job ID."""
        output = "Some warning\nSubmitted batch job 12345\nMore output"
        assert parse_sbatch_job_id(output) == "12345"

    def test_returns_none_for_no_match(self) -> None:
        """Test that None is returned when pattern doesn't match."""
        assert parse_sbatch_job_id("Error: submission failed") is None
        assert parse_sbatch_job_id("") is None

    def test_returns_none_for_invalid_format(self) -> None:
        """Test that None is returned for invalid format."""
        assert parse_sbatch_job_id("Submitted batch job abc") is None


class TestDetectContainerRuntime:
    """Tests for detect_container_runtime function."""

    def test_detects_enroot(self) -> None:
        """Test that enroot is detected when srun --help shows --container-image."""
        validator = _make_validator(
            {
                "srun --help": CommandResult(
                    exit_code=0,
                    stdout="  --container-image=IMAGE   container image to use\n",
                    stderr="",
                    duration=0.1,
                ),
            }
        )

        assert detect_container_runtime(validator) == "enroot"
        validator.log.info.assert_called()

    def test_detects_singularity(self) -> None:
        """Test that singularity is detected when srun has no enroot/pyxis but singularity is available."""
        validator = _make_validator(
            {
                "srun --help": CommandResult(exit_code=0, stdout="usage: srun [OPTIONS]...\n", stderr="", duration=0.1),
                "which singularity": CommandResult(
                    exit_code=0, stdout="/usr/bin/singularity\n", stderr="", duration=0.1
                ),
            }
        )

        assert detect_container_runtime(validator) == "singularity"

    def test_detects_apptainer_as_singularity(self) -> None:
        """Test that apptainer is reported as singularity."""
        validator = _make_validator(
            {
                "srun --help": CommandResult(exit_code=0, stdout="usage: srun [OPTIONS]...\n", stderr="", duration=0.1),
                "which apptainer": CommandResult(exit_code=0, stdout="/usr/bin/apptainer\n", stderr="", duration=0.1),
            }
        )

        assert detect_container_runtime(validator) == "singularity"

    def test_detects_docker(self) -> None:
        """Test that docker is detected as last resort."""
        validator = _make_validator(
            {
                "srun --help": CommandResult(exit_code=0, stdout="usage: srun [OPTIONS]...\n", stderr="", duration=0.1),
                "which docker": CommandResult(exit_code=0, stdout="/usr/bin/docker\n", stderr="", duration=0.1),
            }
        )

        assert detect_container_runtime(validator) == "docker"

    def test_defaults_to_docker_when_nothing_found(self) -> None:
        """Test that docker is the default when no runtime is detected."""
        validator = _make_validator(
            {
                "srun --help": CommandResult(exit_code=0, stdout="usage: srun [OPTIONS]...\n", stderr="", duration=0.1),
            }
        )

        assert detect_container_runtime(validator) == "docker"
        validator.log.warning.assert_called()

    def test_enroot_takes_priority_over_singularity(self) -> None:
        """Test that enroot is preferred when both enroot/pyxis and singularity are available."""
        validator = _make_validator(
            {
                "srun --help": CommandResult(
                    exit_code=0,
                    stdout="  --container-image=IMAGE   container image to use\n",
                    stderr="",
                    duration=0.1,
                ),
                "which singularity": CommandResult(
                    exit_code=0, stdout="/usr/bin/singularity\n", stderr="", duration=0.1
                ),
                "which docker": CommandResult(exit_code=0, stdout="/usr/bin/docker\n", stderr="", duration=0.1),
            }
        )

        assert detect_container_runtime(validator) == "enroot"

    def test_handles_srun_help_failure(self) -> None:
        """Test graceful handling when srun --help fails."""
        validator = _make_validator(
            {
                "srun --help": CommandResult(exit_code=1, stdout="", stderr="srun: error: ...", duration=0.1),
                "which docker": CommandResult(exit_code=0, stdout="/usr/bin/docker\n", stderr="", duration=0.1),
            }
        )

        assert detect_container_runtime(validator) == "docker"

    def test_container_image_in_stderr(self) -> None:
        """Test that enroot is detected even if --container-image appears in stderr."""
        validator = _make_validator(
            {
                "srun --help": CommandResult(
                    exit_code=0,
                    stdout="",
                    stderr="  --container-image=IMAGE   container image\n",
                    duration=0.1,
                ),
            }
        )

        assert detect_container_runtime(validator) == "enroot"

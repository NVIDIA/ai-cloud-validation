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

"""Deploy subcommand for isvctl.

Deploys ai-cloud-validation to a remote machine and runs validation tests.
"""

import logging
import os
import shlex
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from isvreporter.config import get_endpoint, get_ssa_issuer

from isvctl.cli import setup_logging
from isvctl.cli.common import get_output_dir, print_error, print_progress, print_step, print_warning
from isvctl.config.platform_resolution import effective_axes
from isvctl.orchestrator.loop import Phase
from isvctl.remote import SCPTransfer, SSHClient, TarArchive
from isvctl.remote.archive import DEFAULT_EXCLUDES as DEFAULT_ARCHIVE_EXCLUDES
from isvctl.remote.archive import ArchiveError
from isvctl.remote.transfer import SCPTransferError
from isvctl.reporting import check_upload_credentials, create_test_run, update_test_run

logger = logging.getLogger(__name__)


# Default paths to include in the deployment archive
DEFAULT_ARCHIVE_PATHS = [
    "isvtest/",
    "isvreporter/",
    "isvctl/",
    "pyproject.toml",
    "uv.lock",
]

app = typer.Typer(
    name="deploy",
    help="Deploy to remote machine and run validation tests",
    no_args_is_help=True,
)


def _resolve_config_paths(
    config_files: list[Path],
    working_dir: Path,
) -> list[str]:
    """Resolve configuration file paths.

    Args:
        config_files: Config files to use
        working_dir: Working directory for relative paths

    Returns:
        List of resolved config file paths (relative to working_dir)
    """
    configs: list[str] = []

    # Add configs
    for config in config_files:
        # Get path relative to working dir
        if config.is_absolute():
            try:
                rel_path = config.relative_to(working_dir)
                configs.append(str(rel_path))
            except ValueError:
                raise typer.BadParameter(f"Config file must be within workspace: {config}")
        else:
            configs.append(str(config))

    # Require at least one config
    if not configs:
        raise typer.BadParameter("At least one --config/-f config file is required")

    # Validate configs exist and are within allowed paths
    for config in configs:
        config_path = working_dir / config
        if not config_path.exists():
            raise typer.BadParameter(f"Config file not found: {config}")

        # Validate config is within archived paths
        valid_prefixes = ["isvctl/", "isvtest/", "isvreporter/"]
        if not any(config.startswith(prefix) for prefix in valid_prefixes):
            raise typer.BadParameter(f"Config '{config}' must be within isvctl/, isvtest/, or isvreporter/")

    return configs


def _print_configuration(
    remote_ip: str,
    port: int,
    user: str,
    remote_dir: str,
    jumphost: str | None,
    configs: list[str],
    phase: Phase,
    environment: str,
    upload_results: bool,
) -> None:
    """Print deployment configuration summary."""
    print_progress("=========================================")
    print_progress("Deployment Configuration")
    print_progress("=========================================")
    print_progress(f"Remote IP:        {remote_ip}")
    print_progress(f"SSH Port:         {port}")
    if jumphost:
        print_progress(f"Jumphost:         {jumphost}")
    print_progress(f"Remote User:      {user}")
    print_progress(f"Remote Directory: {remote_dir}")
    print_progress(f"Config Files:     {' '.join(configs)}")
    print_progress(f"Phase:            {phase.value}")
    print_progress(f"Environment:      {environment}")
    print_progress(f"Upload Results:   {upload_results}")
    print_progress("=========================================")
    print_progress("")


@app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    remote_ip: Annotated[
        str,
        typer.Argument(
            help="Remote IP address or hostname",
        ),
    ],
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="SSH port on target",
        ),
    ] = 22,
    user: Annotated[
        str,
        typer.Option(
            "--user",
            "-u",
            help="Remote username",
        ),
    ] = "nvidia",
    remote_dir: Annotated[
        str | None,
        typer.Option(
            "--remote-dir",
            "-d",
            help="Remote directory (default: /home/<user>/ai-cloud-validation)",
        ),
    ] = None,
    config: Annotated[
        list[Path] | None,
        typer.Option(
            "--config",
            "-f",
            help="Config file for isvctl (can be repeated, later files override)",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    lab_id: Annotated[
        int | None,
        typer.Option(
            "--lab-id",
            "-l",
            help="ISV Lab ID (required for result upload)",
        ),
    ] = None,
    jumphost: Annotated[
        str | None,
        typer.Option(
            "--jumphost",
            "-j",
            help="SSH jumphost (ProxyJump) for reaching target, format: host or host:port",
        ),
    ] = None,
    phase: Annotated[
        Phase,
        typer.Option(
            "--phase",
            help="Run specific phase: all, setup, test, teardown",
        ),
    ] = Phase.ALL,
    prod: Annotated[
        bool,
        typer.Option(
            "--prod",
            help="Use production environment (default: staging)",
        ),
    ] = False,
    no_upload: Annotated[
        bool,
        typer.Option(
            "--no-upload",
            help="Skip uploading results to isvreporter",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging",
        ),
    ] = False,
    cleanup: Annotated[
        bool,
        typer.Option(
            "--cleanup",
            help="Delete downloaded artifacts (pytest-output.log, junit-validation.xml) after completion",
        ),
    ] = False,
    isv_software_version: Annotated[
        str | None,
        typer.Option(
            "--isv-software-version",
            help="ISV software stack version (opaque string provided by ISV, e.g., 'nemo-2.1.0-rc3')",
        ),
    ] = None,
) -> None:
    """Deploy to remote machine and run validation tests.

    Creates a deployment archive, copies it to the remote machine,
    extracts and runs the validation tests, then downloads results.

    Examples:
        isvctl deploy run 192.168.1.100 -f isvctl/configs/suites/k8s.yaml

        isvctl deploy run 7.243.33.191 -j 202.56.94.106:2260 -u ubuntu -f isvctl/configs/suites/k8s.yaml

        isvctl deploy run 192.168.1.100 -f isvctl/configs/suites/slurm.yaml -- -v -s -k "test_name"
    """
    setup_logging(verbose)

    # Collect extra pytest args from context (after --)
    pytest_extra_args = shlex.join(ctx.args) if ctx.args else ""

    # Set working directory to workspace root
    working_dir = Path.cwd()

    # Set default remote directory
    effective_remote_dir = remote_dir or f"/home/{user}/ai-cloud-validation"

    # Resolve config paths
    config_files = config or []
    try:
        configs = _resolve_config_paths(config_files, working_dir)
    except typer.BadParameter as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    # Environment configuration
    environment = "production" if prod else "staging"

    # Check upload credentials
    upload_results = not no_upload
    if upload_results:
        if not lab_id:
            print_warning("--lab-id not specified, skipping result upload")
            upload_results = False
        else:
            can_upload, _, _ = check_upload_credentials()
            if not can_upload:
                print_warning("ISV_CLIENT_ID and/or ISV_CLIENT_SECRET not set")
                print_warning("Test results will not be uploaded to ISV Lab Service")
                upload_results = False
            else:
                endpoint = get_endpoint()
                ssa_issuer = get_ssa_issuer()
                if not endpoint or not ssa_issuer:
                    missing = []
                    if not endpoint:
                        missing.append("ISV_SERVICE_ENDPOINT")
                    if not ssa_issuer:
                        missing.append("ISV_SSA_ISSUER")
                    print_warning(f"{', '.join(missing)} not set, skipping result upload")
                    upload_results = False
                else:
                    os.environ["ISV_SERVICE_ENDPOINT"] = endpoint
                    os.environ["ISV_SSA_ISSUER"] = ssa_issuer

    # Print configuration
    _print_configuration(
        remote_ip=remote_ip,
        port=port,
        user=user,
        remote_dir=effective_remote_dir,
        jumphost=jumphost,
        configs=configs,
        phase=phase,
        environment=environment,
        upload_results=upload_results,
    )

    # Create SSH and SCP clients
    ssh = SSHClient(host=remote_ip, user=user, port=port, jumphost=jumphost, quiet=not verbose)
    scp = SCPTransfer(host=remote_ip, user=user, port=port, jumphost=jumphost)

    # Create temporary archive
    archive_name = "ai-cloud-validation.tar.gz"
    archive_path = Path(tempfile.gettempdir()) / archive_name

    try:
        # Step 1: Create archive
        print_step(f"Creating archive: {archive_name}")
        archiver = TarArchive(working_dir=working_dir)

        archive_paths = list(DEFAULT_ARCHIVE_PATHS)

        archiver.create(
            output=archive_path,
            paths=archive_paths,
            excludes=DEFAULT_ARCHIVE_EXCLUDES,
        )

        archive_size = archive_path.stat().st_size / (1024 * 1024)
        print_step(f"Archive created successfully (size: {archive_size:.1f}MB)")

        # Step 2: Test SSH connection
        if jumphost:
            print_step(f"Testing SSH connection to {remote_ip} via jumphost {jumphost}...")
        else:
            print_step(f"Testing SSH connection to {remote_ip}...")

        conn_result = ssh.test_connection()
        if not conn_result.success:
            if ssh.is_connection_error(conn_result):
                print_error("SSH connection failed")
                if jumphost:
                    print_progress(f"  Could not connect to {remote_ip} via jumphost {jumphost}")
                    print_progress("  Hint: If using certificate-based auth, you may need to refresh your credentials")
                    print_progress("        (e.g., re-run your organization's SSH credential/bootstrap command)")
                else:
                    print_progress(f"  Could not connect to {remote_ip}")
                if conn_result.stderr:
                    print_progress(f"  Details: {conn_result.stderr.strip()}")
            else:
                print_error(f"SSH connection test failed (exit code {conn_result.exit_code})")
            raise typer.Exit(code=1)

        print_step("SSH connection successful")

        # Step 3: Check remote directory and uv installation
        print_step("Ensuring remote directory exists and uv is installed...")

        dir_result = ssh.ensure_directory(effective_remote_dir)
        if not dir_result.success:
            print_error("Failed to create remote directory")
            if dir_result.stderr:
                print_progress(f"  Details: {dir_result.stderr.strip()}")
            raise typer.Exit(code=1)

        if not ssh.check_command_exists("uv"):
            print_error("'uv' is not installed on the remote machine (or not in PATH).")
            print_progress("Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh")
            print_progress("Then ensure ~/.local/bin is in your PATH")
            raise typer.Exit(code=1)

        # Step 3: Upload archive
        print_step("Copying archive to remote machine...")
        scp.upload(archive_path, f"{effective_remote_dir}/{archive_name}")
        print_step("Archive copied successfully")

        # Step 4: Create test run (if uploading results)
        test_run_id: str | None = None
        start_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        if upload_results and lab_id:
            print_step("Creating test run in isvreporter...")
            # Derive the (capability, module) axis pair from the first config
            # file (resolved through imports): platform suites target a
            # capability; module suites exercise a module and have no
            # capability of their own.
            capability: str | None = None
            module: str | None = None
            if config_files:
                try:
                    capability, module = effective_axes(Path(config_files[0]))
                except Exception as e:
                    logger.warning("Could not resolve axis keys from %s: %s", config_files[0], e)
            if capability is None and module is None:
                capability = "kubernetes"
            test_run_id = create_test_run(
                lab_id=lab_id,
                platform=capability,
                module=module,
                tags=["validation-test", "isvctl"],
                start_time=start_time,
                executed_by="isvctl deploy",
                ci_reference="local-deployment",
                isv_software_version=isv_software_version,
            )
            if not test_run_id:
                print_warning("Failed to create test run, continuing without upload")
                upload_results = False

        # Step 5: Run tests on remote
        print_step("Extracting archive and running tests on remote machine...")

        # Build config args for isvctl
        config_args = " ".join(f"-f {c}" for c in configs)

        # Handle NGC API key securely - use shlex.quote to prevent shell injection
        ngc_api_key = os.environ.get("NGC_API_KEY", "") or os.environ.get("NGC_NIM_API_KEY", "")
        env_vars = f"NGC_API_KEY={shlex.quote(ngc_api_key)}" if ngc_api_key else ""

        # Note: Variables like $PATH and $TEST_RESULT expand on the remote shell
        remote_script = f"""
# Ensure ~/.local/bin is in PATH (where uv is typically installed)
export PATH="$HOME/.local/bin:$PATH"

cd "{effective_remote_dir}"
echo "Extracting archive..."
tar -xzf "{archive_name}"

# Remove venv to avoid permission issues
if [ -d ".venv" ]; then
    echo "Removing existing venv..."
    sudo rm -rf .venv 2>/dev/null || rm -rf .venv
fi

echo "Running uv sync..."
uv sync --quiet

echo "Running validation tests with isvctl..."
echo "Command: isvctl test run {config_args} --phase {phase.value}"

set +e
set -o pipefail
sudo -E env PATH="$PATH" PYTHONUNBUFFERED=1 {env_vars} uv run isvctl test run {config_args} --phase {phase.value} --color=yes --junitxml=junit-validation.xml {pytest_extra_args} 2>&1 | tee pytest-output.log
TEST_RESULT=${{PIPESTATUS[0]}}
set +o pipefail

echo ""
echo "============================================="
if [ "$TEST_RESULT" -eq 0 ] 2>/dev/null; then
    echo "Tests completed successfully!"
else
    echo "Tests completed with failures (exit code: $TEST_RESULT)"
fi
echo "============================================="

exit ${{TEST_RESULT:-1}}
"""

        result = ssh.execute(remote_script, stream=True)
        test_exit_code = result.exit_code

        print_progress("")

        # Step 6: Download results (always download to working_dir)
        print_step("Copying test results from remote machine...")

        output_dir = get_output_dir(working_dir)

        local_log = output_dir / "pytest-output.log"
        if scp.download_optional(f"{effective_remote_dir}/pytest-output.log", local_log):
            print_step(f"Test log copied to {local_log}")
        else:
            print_warning("Failed to copy test log from remote")

        # Download JUnit XML
        local_junit: Path | None = output_dir / "junit-validation.xml"
        if scp.download_optional(f"{effective_remote_dir}/junit-validation.xml", local_junit):
            print_step(f"JUnit XML copied to {local_junit}")
        else:
            print_warning("Failed to copy JUnit XML from remote (may not exist)")
            local_junit = None

        # Step 7: Upload results to isvreporter (only if upload_results is enabled)
        if upload_results and test_run_id and lab_id:
            print_step("Uploading test results to isvreporter...")

            if update_test_run(
                lab_id=lab_id,
                test_run_id=test_run_id,
                success=test_exit_code == 0,
                start_time=start_time,
                log_file=local_log if local_log.exists() else None,
                junit_xml=local_junit if local_junit and local_junit.exists() else None,
                isv_software_version=isv_software_version,
            ):
                print_step("Test results uploaded successfully")
            else:
                print_warning("Failed to upload test results")

        # Clean up downloaded artifacts (only if --cleanup flag is used)
        if cleanup:
            if local_log.exists():
                local_log.unlink()
            if local_junit and local_junit.exists():
                local_junit.unlink()

        # Final status
        if test_exit_code != 0:
            print_progress("")
            print_error("Remote execution tests failed")
            raise typer.Exit(code=1)

        print_progress("")
        print_step("Deployment and testing completed successfully!")

    except ArchiveError as e:
        print_error(f"Failed to create archive: {e}")
        raise typer.Exit(code=1)
    except SCPTransferError as e:
        print_error(f"File transfer failed: {e}")
        raise typer.Exit(code=1)
    finally:
        # Clean up archive
        if archive_path.exists():
            archive_path.unlink()

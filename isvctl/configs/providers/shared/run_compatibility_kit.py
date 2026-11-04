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

"""Run the Run:AI compatibility kit container against a Kubernetes cluster.

The kit is a prebuilt Docker image published to NVIDIA NGC without guest
access (its build clones a private E2E suite, so this script never builds
it - point --image / RUNAI_COMPAT_KIT_IMAGE at the nvcr.io path or a locally
loaded tag). Pulling an nvcr.io image logs in with NGC_API_KEY first; a
locally present image needs no credentials. The kit runs as uid 65532 and cannot read a
host-owned mode-0600 kubeconfig from a bind mount, so the kubeconfig is staged
through a short-lived Docker volume with the runtime uid/mode - the same flow
as the kit's own Makefile.

The kit writes its artifacts (Allure report, CSV summary, log, results zip)
into --results-dir. This script parses the Allure summary and the CSV into the
provider-neutral JSON contract; pass/fail policy lives in the
RunAICompatibilityCheck validation, not here.

Modes:
    (default)    stage kubeconfig, run the kit, parse results
    --preflight  setup-phase probe: docker + kubeconfig present, image pullable
    --cleanup    teardown-phase sweep: remove leftover container/volume

Usage:
    python run_compatibility_kit.py --results-dir ./runai-compatibility-results
    python run_compatibility_kit.py --image runai/certification-kit:latest --timeout 3600
    python run_compatibility_kit.py --preflight
    python run_compatibility_kit.py --cleanup

Output JSON (default mode):
{
    "success": true,
    "platform": "k8s",
    "test_name": "runai_compatibility",
    "tests": {
        "compatibility": {
            "passed": false,
            "message": "89/107 tests passed (1 failed, 0 broken, 17 skipped)",
            "error": "Failed tests: Create distributed policy and verify its applied",
            "failed_tests": ["Create distributed policy and verify its applied"]
        }
    }
}

`success` means the kit executed and produced results; the compatibility
verdict is `tests.compatibility.passed`.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_IMAGE = "nvcr.io/nvidia/runai/runai-compatibility-kit:latest"
IMAGE_ENV_VAR = "RUNAI_COMPAT_KIT_IMAGE"
NGC_REGISTRY = "nvcr.io"
# The kit's own Makefile builds this local tag; point RUNAI_COMPAT_KIT_IMAGE at
# it to run a locally built kit without touching NGC.
LOCAL_MAKEFILE_TAG = "runai/certification-kit:latest"
CONTAINER_NAME = "isv-runai-compatibility-kit"
VOLUME_NAME = "isv-runai-compatibility-kit-kubeconfig"
KIT_RESULTS_MOUNT = "/app/e2e/results"
KIT_KUBE_DIR = "/home/runai/.kube"
KIT_UID_GID = "65532:65532"
SUMMARY_RELPATH = Path("allure-report") / "widgets" / "summary.json"
CSV_RELPATH = Path("test-results-summary.csv")
MAX_FAILED_NAMES = 10
LOG_TAIL_CHARS = 2000


def log(message: str) -> None:
    """Progress/log line on stderr - stdout carries only the final JSON."""
    print(message, file=sys.stderr)


def run_docker(args: list[str], timeout: int | None = None) -> tuple[int, str]:
    """Run a docker CLI command, returning (exit_code, combined output)."""
    proc = subprocess.run(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout or ""


def docker_available() -> bool:
    """Return whether a usable docker CLI + daemon is present."""
    if shutil.which("docker") is None:
        return False
    try:
        exit_code, _ = run_docker(["version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return exit_code == 0


def resolve_image(arg_value: str) -> str:
    """Image precedence: --image arg > RUNAI_COMPAT_KIT_IMAGE env > default."""
    return arg_value or os.environ.get(IMAGE_ENV_VAR, "") or DEFAULT_IMAGE


def ngc_login(image: str) -> str | None:
    """Log in to NGC when the image lives there. Returns an error, or None.

    The kit publishes to NGC without guest access, so pulling needs an
    authenticated docker login. The key goes over stdin, never argv.
    """
    if not image.startswith(f"{NGC_REGISTRY}/"):
        return None
    api_key = os.environ.get("NGC_API_KEY", "") or os.environ.get("NGC_NIM_API_KEY", "")
    if not api_key:
        return f"image {image} is on {NGC_REGISTRY} but NGC_API_KEY is not set"
    log(f"Logging in to {NGC_REGISTRY}...")
    proc = subprocess.run(
        ["docker", "login", NGC_REGISTRY, "-u", "$oauthtoken", "--password-stdin"],
        input=api_key,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return f"docker login {NGC_REGISTRY} failed: {(proc.stdout or '').strip()[-LOG_TAIL_CHARS:]}"
    return None


def ensure_image(image: str) -> str | None:
    """Make the kit image locally available. Returns an error, or None.

    A locally present image needs neither login nor pull, so a preloaded
    runner works without NGC credentials.
    """
    exit_code, _ = run_docker(["image", "inspect", image], timeout=60)
    if exit_code == 0:
        return None
    error = ngc_login(image)
    if error:
        return error
    log(f"Image {image} not present locally, pulling...")
    exit_code, output = run_docker(["pull", image], timeout=1800)
    if exit_code != 0:
        return f"image {image} is neither local nor pullable: {output.strip()[-LOG_TAIL_CHARS:]}"
    return None


def resolve_kubeconfig(arg_value: str) -> Path | None:
    """Kubeconfig precedence: --kubeconfig arg > KUBECONFIG env > ~/.kube/config.

    Returns the first candidate that exists, or None.
    """
    candidates = [arg_value, os.environ.get("KUBECONFIG", ""), "~/.kube/config"]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def parse_summary(summary_text: str) -> dict[str, int]:
    """Extract test counts from the Allure summary widget JSON.

    Raises ValueError when the document does not carry the expected
    ``statistic`` block.
    """
    try:
        document = json.loads(summary_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"summary is not valid JSON: {exc}") from exc

    statistic = document.get("statistic") if isinstance(document, dict) else None
    if not isinstance(statistic, dict):
        raise ValueError("summary has no 'statistic' block")

    counts = {}
    for key in ("passed", "failed", "broken", "skipped", "unknown", "total"):
        value = statistic.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"summary statistic '{key}' is not an integer: {value!r}")
        counts[key] = value
    return counts


def failed_test_names(csv_text: str) -> list[str]:
    """Names of failed/broken tests from the kit CSV, deduplicated in order."""
    names: list[str] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO(csv_text)):
        name = (row.get("Test Name") or "").strip()
        status = (row.get("Status") or "").strip().lower()
        if name and status in ("failed", "broken") and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def build_compatibility(counts: dict[str, int], failed_names: list[str]) -> dict[str, Any]:
    """Build the ``tests.compatibility`` contract block from parsed results."""
    passed = counts["failed"] == 0 and counts["broken"] == 0 and counts["unknown"] == 0 and counts["passed"] > 0
    message = (
        f"{counts['passed']}/{counts['total']} tests passed "
        f"({counts['failed']} failed, {counts['broken']} broken, {counts['skipped']} skipped)"
    )
    compatibility: dict[str, Any] = {"passed": passed, "message": message}
    if not passed:
        if failed_names:
            shown = failed_names[:MAX_FAILED_NAMES]
            error = "Failed tests: " + "; ".join(shown)
            if len(failed_names) > len(shown):
                error += f" ... and {len(failed_names) - len(shown)} more"
            compatibility["failed_tests"] = shown
        elif counts["passed"] == 0:
            error = "no tests passed"
        else:
            error = message
        compatibility["error"] = error
    return compatibility


def compatibility_from_results(results_dir: Path) -> dict[str, Any]:
    """Parse the kit's results directory into the compatibility block.

    Raises ValueError when the Allure summary is missing or malformed. A
    missing CSV only costs the failed-test names, not the verdict.
    """
    summary_path = results_dir / SUMMARY_RELPATH
    if not summary_path.is_file():
        raise ValueError(f"kit produced no Allure summary at {summary_path}")
    counts = parse_summary(summary_path.read_text())

    csv_path = results_dir / CSV_RELPATH
    names: list[str] = []
    if csv_path.is_file():
        try:
            names = failed_test_names(csv_path.read_text())
        except csv.Error as exc:
            log(f"Could not parse {csv_path.name}: {exc}")
    return build_compatibility(counts, names)


def remove_leftovers() -> list[str]:
    """Best-effort removal of the kit container and kubeconfig volume."""
    removed = []
    exit_code, _ = run_docker(["rm", "-f", CONTAINER_NAME], timeout=60)
    if exit_code == 0:
        removed.append(f"container {CONTAINER_NAME}")
    exit_code, _ = run_docker(["volume", "rm", VOLUME_NAME], timeout=60)
    if exit_code == 0:
        removed.append(f"volume {VOLUME_NAME}")
    return removed


def stage_kubeconfig(image: str, kubeconfig: Path) -> None:
    """Copy the kubeconfig into a volume owned by the kit's runtime uid.

    Raises RuntimeError with the docker output on failure.
    """
    exit_code, output = run_docker(["volume", "create", VOLUME_NAME], timeout=60)
    if exit_code != 0:
        raise RuntimeError(f"docker volume create failed: {output.strip()}")

    exit_code, output = run_docker(
        [
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/bash",
            "-v",
            f"{kubeconfig}:/source/config:ro",
            "--mount",
            f"type=volume,source={VOLUME_NAME},target={KIT_KUBE_DIR}",
            image,
            "-ec",
            f"cp /source/config {KIT_KUBE_DIR}/config"
            f" && chown {KIT_UID_GID} {KIT_KUBE_DIR}/config"
            f" && chmod 0600 {KIT_KUBE_DIR}/config",
        ],
        timeout=300,
    )
    if exit_code != 0:
        raise RuntimeError(f"kubeconfig staging failed: {output.strip()[-LOG_TAIL_CHARS:]}")


def run_kit(image: str, results_dir: Path, timeout: int) -> tuple[int, str]:
    """Run the kit container. On timeout, force-remove it and re-raise."""
    try:
        return run_docker(
            [
                "run",
                "--rm",
                "--name",
                CONTAINER_NAME,
                "-v",
                f"{results_dir}:{KIT_RESULTS_MOUNT}",
                "--mount",
                f"type=volume,source={VOLUME_NAME},target={KIT_KUBE_DIR}",
                image,
            ],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        run_docker(["rm", "-f", CONTAINER_NAME], timeout=60)
        raise


def skipped_result(result: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark the contract as skipped (validations then skip, not fail)."""
    log(f"Skipping: {reason}")
    result["success"] = True
    result["skipped"] = True
    result["skip_reason"] = reason
    return result


def run_preflight(args: argparse.Namespace) -> dict[str, Any]:
    """Setup-phase probe: docker usable, kubeconfig present, image available."""
    image = resolve_image(args.image)
    result: dict[str, Any] = {
        "success": False,
        "platform": "k8s",
        "test_name": "runai_compatibility_preflight",
        "image": image,
    }

    if not docker_available():
        return skipped_result(result, "docker is not available on this host")
    if resolve_kubeconfig(args.kubeconfig) is None:
        return skipped_result(result, "no kubeconfig found (set --kubeconfig or KUBECONFIG)")

    error = ensure_image(image)
    if error:
        result["error"] = error
        return result

    result["success"] = True
    return result


def run_cleanup() -> dict[str, Any]:
    """Teardown-phase sweep for leftovers from an aborted run."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "k8s",
        "test_name": "runai_compatibility_cleanup",
    }
    if not docker_available():
        return skipped_result(result, "docker is not available on this host")
    removed = remove_leftovers()
    log("Removed: " + (", ".join(removed) if removed else "nothing to clean up"))
    result["success"] = True
    return result


def run_compatibility(args: argparse.Namespace) -> dict[str, Any]:
    """Default mode: stage kubeconfig, run the kit, parse its results."""
    image = resolve_image(args.image)
    result: dict[str, Any] = {
        "success": False,
        "platform": "k8s",
        "test_name": "runai_compatibility",
        "tests": {},
    }

    if not docker_available():
        return skipped_result(result, "docker is not available on this host")
    kubeconfig = resolve_kubeconfig(args.kubeconfig)
    if kubeconfig is None:
        return skipped_result(result, "no kubeconfig found (set --kubeconfig or KUBECONFIG)")

    results_dir = Path(args.results_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    remove_leftovers()  # a previous aborted run must not collide on fixed names
    try:
        # Preflight normally did this already; standalone runs get the same
        # NGC-login-then-pull path instead of an unauthenticated docker run pull.
        error = ensure_image(image)
        if error:
            raise RuntimeError(error)

        log(f"Staging kubeconfig {kubeconfig} for the kit runtime user...")
        stage_kubeconfig(image, kubeconfig)

        log(f"Running {image} (timeout: {args.timeout}s, results: {results_dir})...")
        _exit_code, output = run_kit(image, results_dir, args.timeout)
        tail = output.strip()[-LOG_TAIL_CHARS:]
        if tail:
            log(f"Kit output (tail):\n{tail}")

        # Playwright exits non-zero on test failures, so the container exit
        # code cannot distinguish "kit broke" from "tests failed". The parsed
        # summary is the signal: present means the kit ran to reporting.
        result["tests"]["compatibility"] = compatibility_from_results(results_dir)
        result["success"] = True
        log(f"Compatibility artifacts (Allure report, results package) in {results_dir}")
    except subprocess.TimeoutExpired:
        result["error"] = f"compatibility kit did not finish within {args.timeout}s"
        result["error_type"] = "timeout"
    except (RuntimeError, ValueError, OSError) as exc:
        result["error"] = str(exc)
    finally:
        run_docker(["volume", "rm", VOLUME_NAME], timeout=60)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Run:AI compatibility kit container")
    parser.add_argument("--image", default="", help=f"Kit image (default: ${IMAGE_ENV_VAR} or {DEFAULT_IMAGE})")
    parser.add_argument("--kubeconfig", default="", help="Kubeconfig path (default: $KUBECONFIG or ~/.kube/config)")
    parser.add_argument(
        "--results-dir",
        default="runai-compatibility-results",
        help="Host directory the kit writes its artifacts into",
    )
    parser.add_argument("--timeout", type=int, default=3600, help="Seconds to allow the kit container to run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="Only check docker/kubeconfig/image availability")
    mode.add_argument("--cleanup", action="store_true", help="Only remove leftover kit container/volume")
    args = parser.parse_args()

    if args.preflight:
        result = run_preflight(args)
    elif args.cleanup:
        result = run_cleanup()
    else:
        result = run_compatibility(args)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

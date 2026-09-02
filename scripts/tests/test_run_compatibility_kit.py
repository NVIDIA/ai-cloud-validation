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

"""Tests for providers/shared/run_compatibility_kit.py.

Cover the results parsing (Allure summary + CSV -> contract JSON) and the
mode flows with the docker CLI mocked out - no docker daemon needed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "isvctl" / "configs" / "providers" / "shared" / "run_compatibility_kit.py"
)
_spec = importlib.util.spec_from_file_location("run_compatibility_kit", _SCRIPT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)


def _summary_text(passed: int = 89, failed: int = 1, broken: int = 0, skipped: int = 17, unknown: int = 0) -> str:
    total = passed + failed + broken + skipped + unknown
    return json.dumps(
        {
            "reportName": "Allure Report",
            "statistic": {
                "failed": failed,
                "broken": broken,
                "skipped": skipped,
                "passed": passed,
                "unknown": unknown,
                "total": total,
            },
        }
    )


CSV_TEXT = (
    "Test Name,Status,Duration (ms),Tags,Error Details\n"
    '"should create access key","passed","2007",""\n'
    '"Create distributed policy and verify its applied","failed","1915",""\n'
    '"Create distributed policy and verify its applied","failed","1911",""\n'
    '"creates a workload with nfs","skipped","0",""\n'
    '"flaky infra test","broken","10",""\n'
)


# ─── parse_summary ───────────────────────────────────────────────────


def test_parse_summary_extracts_counts() -> None:
    counts = kit.parse_summary(_summary_text())
    assert counts == {"passed": 89, "failed": 1, "broken": 0, "skipped": 17, "unknown": 0, "total": 107}


def test_parse_summary_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        kit.parse_summary("<html>not json</html>")


def test_parse_summary_rejects_missing_statistic() -> None:
    with pytest.raises(ValueError, match="no 'statistic'"):
        kit.parse_summary(json.dumps({"reportName": "Allure Report"}))


def test_parse_summary_rejects_non_integer_counts() -> None:
    with pytest.raises(ValueError, match="'failed' is not an integer"):
        kit.parse_summary(json.dumps({"statistic": {"failed": "one"}}))
    with pytest.raises(ValueError, match="'failed' is not an integer"):
        kit.parse_summary(json.dumps({"statistic": {"failed": True}}))


# ─── failed_test_names ───────────────────────────────────────────────


def test_failed_test_names_collects_failed_and_broken_deduplicated() -> None:
    assert kit.failed_test_names(CSV_TEXT) == [
        "Create distributed policy and verify its applied",
        "flaky infra test",
    ]


def test_failed_test_names_empty_for_clean_csv() -> None:
    clean = 'Test Name,Status,Duration (ms),Tags,Error Details\n"ok test","passed","1",""\n'
    assert kit.failed_test_names(clean) == []
    assert kit.failed_test_names("") == []


# ─── build_compatibility ─────────────────────────────────────────────


def test_build_compatibility_passes_when_nothing_failed() -> None:
    counts = {"passed": 89, "failed": 0, "broken": 0, "skipped": 17, "unknown": 0, "total": 106}
    compatibility = kit.build_compatibility(counts, [])
    assert compatibility["passed"] is True
    assert compatibility["message"] == "89/106 tests passed (0 failed, 0 broken, 17 skipped)"
    assert "error" not in compatibility


def test_build_compatibility_fails_with_failed_test_names() -> None:
    counts = {"passed": 89, "failed": 1, "broken": 0, "skipped": 17, "unknown": 0, "total": 107}
    compatibility = kit.build_compatibility(counts, ["Create distributed policy and verify its applied"])
    assert compatibility["passed"] is False
    assert compatibility["error"] == "Failed tests: Create distributed policy and verify its applied"
    assert compatibility["failed_tests"] == ["Create distributed policy and verify its applied"]


def test_build_compatibility_caps_failed_test_names() -> None:
    counts = {"passed": 0, "failed": 15, "broken": 0, "skipped": 0, "unknown": 0, "total": 15}
    compatibility = kit.build_compatibility(counts, [f"test {i}" for i in range(15)])
    assert len(compatibility["failed_tests"]) == kit.MAX_FAILED_NAMES
    assert "and 5 more" in compatibility["error"]


def test_build_compatibility_fails_when_nothing_passed() -> None:
    counts = {"passed": 0, "failed": 0, "broken": 0, "skipped": 5, "unknown": 0, "total": 5}
    compatibility = kit.build_compatibility(counts, [])
    assert compatibility["passed"] is False
    assert compatibility["error"] == "no tests passed"


def test_build_compatibility_fails_on_broken_or_unknown() -> None:
    broken = {"passed": 10, "failed": 0, "broken": 2, "skipped": 0, "unknown": 0, "total": 12}
    assert kit.build_compatibility(broken, [])["passed"] is False
    unknown = {"passed": 10, "failed": 0, "broken": 0, "skipped": 0, "unknown": 1, "total": 11}
    assert kit.build_compatibility(unknown, [])["passed"] is False


# ─── compatibility_from_results ──────────────────────────────────────


def _write_results(results_dir: Path, summary: str | None = None, csv_text: str | None = None) -> None:
    if summary is not None:
        summary_path = results_dir / kit.SUMMARY_RELPATH
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
    if csv_text is not None:
        (results_dir / kit.CSV_RELPATH).write_text(csv_text)


def test_compatibility_from_results_parses_full_package(tmp_path: Path) -> None:
    _write_results(tmp_path, summary=_summary_text(), csv_text=CSV_TEXT)
    compatibility = kit.compatibility_from_results(tmp_path)
    assert compatibility["passed"] is False
    assert "Create distributed policy" in compatibility["error"]


def test_compatibility_from_results_requires_summary(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Allure summary"):
        kit.compatibility_from_results(tmp_path)


def test_compatibility_from_results_tolerates_missing_csv(tmp_path: Path) -> None:
    _write_results(tmp_path, summary=_summary_text(passed=10, failed=0, skipped=0))
    compatibility = kit.compatibility_from_results(tmp_path)
    assert compatibility["passed"] is True


# ─── resolution precedence ───────────────────────────────────────────


def test_resolve_image_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(kit.IMAGE_ENV_VAR, raising=False)
    assert kit.resolve_image("") == kit.DEFAULT_IMAGE
    monkeypatch.setenv(kit.IMAGE_ENV_VAR, "registry/env-image:1")
    assert kit.resolve_image("") == "registry/env-image:1"
    assert kit.resolve_image("registry/arg-image:2") == "registry/arg-image:2"


def test_resolve_kubeconfig_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arg_config = tmp_path / "arg-config"
    env_config = tmp_path / "env-config"
    arg_config.write_text("apiVersion: v1")
    env_config.write_text("apiVersion: v1")

    monkeypatch.setenv("KUBECONFIG", str(env_config))
    assert kit.resolve_kubeconfig(str(arg_config)) == arg_config
    assert kit.resolve_kubeconfig("") == env_config
    # A configured path that does not exist falls through to the next source.
    assert kit.resolve_kubeconfig(str(tmp_path / "missing")) == env_config


def test_resolve_kubeconfig_none_when_nothing_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing"))
    monkeypatch.setenv("HOME", str(tmp_path))  # ~/.kube/config resolves under tmp
    assert kit.resolve_kubeconfig("") is None


# ─── mode flows (docker mocked) ──────────────────────────────────────


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "image": "",
        "kubeconfig": "",
        "results_dir": "results",
        "timeout": 60,
        "preflight": False,
        "cleanup": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_run_compatibility_skips_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kit, "docker_available", lambda: False)
    result = kit.run_compatibility(_args())
    assert result["success"] is True
    assert result["skipped"] is True
    assert "docker" in result["skip_reason"]


def test_run_compatibility_skips_without_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "resolve_kubeconfig", lambda _arg: None)
    result = kit.run_compatibility(_args())
    assert result["success"] is True
    assert result["skipped"] is True
    assert "kubeconfig" in result["skip_reason"]


def test_run_compatibility_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker calls all succeed and the kit leaves a parsable results package."""
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1")
    results_dir = tmp_path / "results"
    docker_calls: list[list[str]] = []

    def fake_run_docker(args: list[str], timeout: int | None = None) -> tuple[int, str]:
        docker_calls.append(args)
        if args[:1] == ["run"] and "--name" in args:
            # The kit run: emit the results package the parser reads back.
            _write_results(results_dir, summary=_summary_text(passed=10, failed=0, skipped=0), csv_text="")
        return 0, ""

    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "run_docker", fake_run_docker)

    result = kit.run_compatibility(_args(kubeconfig=str(kubeconfig), results_dir=str(results_dir)))

    assert result["success"] is True
    assert result["tests"]["compatibility"]["passed"] is True
    # The kubeconfig staging volume must be removed at the end of the run.
    assert ["volume", "rm", kit.VOLUME_NAME] in docker_calls


def test_run_compatibility_fails_when_summary_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kit ran but left no Allure summary - the step must fail, not pass."""
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1")

    def fake_run_docker(args: list[str], timeout: int | None = None) -> tuple[int, str]:
        if args[:1] == ["run"] and "--name" in args:
            return 1, "kit exploded"  # kit run fails and writes no results
        return 0, ""

    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "run_docker", fake_run_docker)

    result = kit.run_compatibility(_args(kubeconfig=str(kubeconfig), results_dir=str(tmp_path / "results")))

    assert result["success"] is False
    assert "no Allure summary" in result["error"]


def test_run_compatibility_fails_when_staging_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1")

    def fake_run_docker(args: list[str], timeout: int | None = None) -> tuple[int, str]:
        if args[:2] == ["volume", "create"]:
            return 1, "daemon down"
        return 0, ""

    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "run_docker", fake_run_docker)

    result = kit.run_compatibility(_args(kubeconfig=str(kubeconfig), results_dir=str(tmp_path / "results")))

    assert result["success"] is False
    assert "docker volume create failed" in result["error"]


def test_run_compatibility_reports_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1")

    def fake_run_docker(args: list[str], timeout: int | None = None) -> tuple[int, str]:
        if args[:1] == ["run"] and "--name" in args:
            raise subprocess.TimeoutExpired(cmd=["docker", *args], timeout=timeout or 0)
        return 0, ""

    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "run_docker", fake_run_docker)

    result = kit.run_compatibility(_args(kubeconfig=str(kubeconfig), results_dir=str(tmp_path / "results")))

    assert result["success"] is False
    assert result["error_type"] == "timeout"


def test_run_preflight_pulls_missing_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1")
    docker_calls: list[list[str]] = []

    def fake_run_docker(args: list[str], timeout: int | None = None) -> tuple[int, str]:
        docker_calls.append(args)
        if args[:2] == ["image", "inspect"]:
            return 1, "no such image"
        return 0, ""

    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "run_docker", fake_run_docker)

    result = kit.run_preflight(_args(image="registry/kit:1", kubeconfig=str(kubeconfig), preflight=True))

    assert result["success"] is True
    assert ["pull", "registry/kit:1"] in docker_calls


def test_ngc_login_skipped_for_non_ngc_image() -> None:
    assert kit.ngc_login("registry.example.com/kit:1") is None
    assert kit.ngc_login(kit.LOCAL_MAKEFILE_TAG) is None


def test_ngc_login_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    monkeypatch.delenv("NGC_NIM_API_KEY", raising=False)
    error = kit.ngc_login(f"{kit.NGC_REGISTRY}/org/kit:1")
    assert error is not None
    assert "NGC_API_KEY" in error


def test_ngc_login_sends_key_over_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NGC_API_KEY", "nvapi-secret")
    calls: list[dict[str, Any]] = []

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append({"cmd": cmd, "input": kwargs.get("input")})

        class _Proc:
            returncode = 0
            stdout = "Login Succeeded"

        return _Proc()

    monkeypatch.setattr(kit.subprocess, "run", fake_subprocess_run)

    assert kit.ngc_login(f"{kit.NGC_REGISTRY}/org/kit:1") is None
    assert calls[0]["cmd"][:3] == ["docker", "login", kit.NGC_REGISTRY]
    assert "--password-stdin" in calls[0]["cmd"]
    assert calls[0]["input"] == "nvapi-secret"
    # The key must never appear in argv (visible in the process table).
    assert "nvapi-secret" not in calls[0]["cmd"]


def test_ensure_image_short_circuits_when_local(monkeypatch: pytest.MonkeyPatch) -> None:
    docker_calls: list[list[str]] = []

    def fake_run_docker(args: list[str], timeout: int | None = None) -> tuple[int, str]:
        docker_calls.append(args)
        return 0, ""

    monkeypatch.setattr(kit, "run_docker", fake_run_docker)

    assert kit.ensure_image(f"{kit.NGC_REGISTRY}/org/kit:1") is None
    assert docker_calls == [["image", "inspect", f"{kit.NGC_REGISTRY}/org/kit:1"]]


def test_ensure_image_fails_for_ngc_image_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NGC_API_KEY", raising=False)
    monkeypatch.delenv("NGC_NIM_API_KEY", raising=False)
    monkeypatch.setattr(kit, "run_docker", lambda args, timeout=None: (1, "no such image"))

    error = kit.ensure_image(f"{kit.NGC_REGISTRY}/org/kit:1")
    assert error is not None
    assert "NGC_API_KEY" in error


def test_run_preflight_fails_when_image_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1")

    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "run_docker", lambda args, timeout=None: (1, "denied"))

    # Non-NGC image: exercises the pull-failure branch without a login attempt.
    result = kit.run_preflight(_args(image="registry.example.com/kit:1", kubeconfig=str(kubeconfig), preflight=True))

    assert result["success"] is False
    assert "neither local nor pullable" in result["error"]


def test_run_preflight_default_image_needs_ngc_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default image lives on NGC, so pulling it without a key must fail clearly."""
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1")

    monkeypatch.delenv("NGC_API_KEY", raising=False)
    monkeypatch.delenv("NGC_NIM_API_KEY", raising=False)
    monkeypatch.delenv(kit.IMAGE_ENV_VAR, raising=False)
    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "run_docker", lambda args, timeout=None: (1, "no such image"))

    result = kit.run_preflight(_args(kubeconfig=str(kubeconfig), preflight=True))

    assert result["success"] is False
    assert "NGC_API_KEY" in result["error"]


def test_run_cleanup_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kit, "docker_available", lambda: True)
    monkeypatch.setattr(kit, "run_docker", lambda args, timeout=None: (0, ""))
    result = kit.run_cleanup()
    assert result["success"] is True


def test_run_cleanup_skips_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kit, "docker_available", lambda: False)
    result = kit.run_cleanup()
    assert result["success"] is True
    assert result["skipped"] is True

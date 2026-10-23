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

"""Tests for the nvstorage-scale provider (storage-scale-test driven from isvctl).

Config tests run against the MERGED config of every variant -- the base ``storage.yaml`` and
each overlay loaded on top of it -- because that is all the orchestrator ever sees.

The provider defines no checks of its own; the storage suite's ``HssQosThroughputCheck``
(HSS02-01) judges the ``qos_throughput`` step, which also gates the result on every earlier
step's invariants. The gate tests below are what stand behind that: they show each invariant
a former per-step check asserted still fails the run.

``test_steps.sh`` is the step scripts' offline suite. It is opt-in
(``NVSTORAGE_SCALE_STEP_SUITE=1``, ~5 min) and needs a storage-scale-test checkout and
bash >= 4; everything else here needs none of that and runs in seconds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig

ISVCTL_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = ISVCTL_ROOT.parent
PROVIDER = ISVCTL_ROOT / "configs" / "providers" / "nvstorage-scale"
CONFIG = PROVIDER / "config"
SCRIPTS = PROVIDER / "scripts" / "storage"
SUITE = ISVCTL_ROOT / "configs" / "suites" / "storage.yaml"
HERE = Path(__file__).resolve().parent
SST_CHECKOUT = Path(os.environ.get("SST_CHECKOUT", REPO_ROOT.parent / "storage-scale-test"))

# variant -> overlay (None = the base config alone)
VARIANTS: dict[str, str | None] = {
    "io": None,
    "multiclaim": "multiclaim.yaml",
    "mdtest": "mdtest.yaml",
    "slurm": "slurm.yaml",
}
KUBECTL_VARIANTS = ("io", "multiclaim", "mdtest")
IO_KUBECTL_VARIANTS = ("io", "multiclaim")
OPT_IN_STEPS = ("cancel", "resume")


def _merged(variant: str) -> dict[str, Any]:
    files = [CONFIG / "storage.yaml"]
    if VARIANTS[variant]:
        files.append(CONFIG / "overlays" / VARIANTS[variant])
    return merge_yaml_files(files)


def _steps(variant: str) -> list[dict[str, Any]]:
    return _merged(variant)["commands"]["storage"]["steps"]


def _by_name(variant: str) -> dict[str, dict[str, Any]]:
    return {step["name"]: step for step in _steps(variant)}


def _names(variant: str) -> list[str]:
    return [step["name"] for step in _steps(variant)]


def _arg_after(step: dict[str, Any], flag: str) -> str | None:
    args = step.get("args", [])
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None


def _provider_files() -> list[Path]:
    return [CONFIG / "storage.yaml", *sorted((CONFIG / "overlays").glob("*.yaml"))]


# --------------------------------------------------------------------------- structure


def test_storage_config_imports_the_storage_suite() -> None:
    """Like every provider, the config supplies commands for the canonical storage suite."""
    raw = yaml.safe_load((CONFIG / "storage.yaml").read_text())
    assert raw["import"] == ["../../../suites/storage.yaml"]


@pytest.mark.parametrize("variant", VARIANTS)
def test_merged_config_has_one_storage_command_group(variant: str) -> None:
    """Platform detection uses the sole command key; it must be the suite's domain."""
    merged = _merged(variant)
    assert list(merged["commands"]) == ["storage"]
    assert "platform" not in merged["tests"]


@pytest.mark.parametrize("variant", VARIANTS)
def test_merged_config_parses_under_the_run_schema(variant: str) -> None:
    """The merged config is a valid RunConfig, steps and validations included."""
    RunConfig(**_merged(variant))


@pytest.mark.parametrize("path", _provider_files(), ids=lambda p: p.name)
def test_provider_files_define_no_checks_or_labels(path: Path) -> None:
    """Checks and labels belong to the suite; a provider only supplies commands and settings."""
    raw = yaml.safe_load(path.read_text())
    assert not (raw.get("tests") or {}).get("validations"), f"{path.name} defines validations"
    assert "labels:" not in path.read_text(), f"{path.name} declares labels"


def test_overlays_do_not_import() -> None:
    """Overlays load as a second -f on top of storage.yaml; importing would double-load the suite."""
    for path in sorted((CONFIG / "overlays").glob("*.yaml")):
        assert "import" not in yaml.safe_load(path.read_text()), path.name


CAPABILITY = {"io": "kubernetes", "multiclaim": "kubernetes", "mdtest": "kubernetes", "slurm": "slurm"}


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_step_requires_the_variant_capability(variant: str) -> None:
    """Without --capability the run context is core, where HSS02-01 is filtered out.

    Steps without `requires` would still run there -- a full sweep and up to an hour of
    polling -- with no verdict at the end. Declaring the capability makes them skip with it,
    as the Kubernetes steps of the other storage providers do.
    """
    want = [CAPABILITY[variant]]
    for step in _steps(variant):
        assert step.get("requires") == want, f"{step['name']}: requires {step.get('requires')}, want {want}"


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_step_phase_is_declared(variant: str) -> None:
    """A step in an undeclared phase aborts the run."""
    group = _merged(variant)["commands"]["storage"]
    assert {step["phase"] for step in group["steps"]} <= set(group["phases"])


@pytest.mark.parametrize("variant", VARIANTS)
def test_step_scripts_resolve_from_the_config_directory(variant: str) -> None:
    """Steps run with cwd = the first config file's directory, as for every provider."""
    for step in _steps(variant):
        script = step["command"].split()[-1]
        assert script.startswith("../scripts/"), f"{step['name']}: {script} is not config-relative"
        assert (CONFIG / script).resolve().is_file(), f"{step['name']}: {script} does not exist"


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_settings_reference_is_defined(variant: str) -> None:
    """A `{{ name }}` arg with no matching setting renders empty and silently drops a threshold."""
    merged = _merged(variant)
    settings = set(merged["tests"]["settings"])
    for step in _steps(variant):
        for arg in step.get("args", []):
            for ref in re.findall(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}", str(arg)):
                assert ref in settings, f"{step['name']} uses {{{{ {ref} }}}} with no such setting"


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_sst_key_is_one_the_generator_accepts(variant: str) -> None:
    """The generator rejects unknown SST_* keys, so a typo would fail the first step at runtime."""
    source = (SCRIPTS / "gen_env_sh.py").read_text()
    spec = set(re.findall(r'"(SST_[A-Z_]+)":', source)) | {"SST_SUBSTRATE"}
    used = {key for step in _steps(variant) for key in (step.get("env") or {}) if key.startswith("SST_")}
    assert used <= spec, f"unknown keys: {sorted(used - spec)}"


@pytest.mark.parametrize("path", _provider_files(), ids=lambda p: p.name)
def test_no_cluster_specific_values_are_hardcoded(path: Path) -> None:
    """Namespace, node selector and claims are per cluster and come from the caller's SST_* env."""
    for step in yaml.safe_load(path.read_text()).get("commands", {}).get("storage", {}).get("steps", []):
        env = step.get("env") or {}
        for key in ("SST_KUBECTL_NAMESPACE", "SST_KUBECTL_NODE_SELECTOR", "SST_TEST_DIRS"):
            assert key not in env, f"{path.name}:{step['name']} hardcodes {key}"


# --------------------------------------------------------------------------- lifecycle


@pytest.mark.parametrize("variant", VARIANTS)
def test_poll_blocks_longer_than_nothing_and_shorter_than_its_timeout(variant: str) -> None:
    """poll_sweep must wait for the sweep, and its step timeout must outlast the wait."""
    poll = _by_name(variant)["poll_sweep"]
    wait = int(_arg_after(poll, "--wait") or 0)
    assert wait > 0
    assert poll["timeout"] > wait
    assert "--allow-incomplete" not in poll["args"]


@pytest.mark.parametrize("variant", KUBECTL_VARIANTS)
def test_kubectl_steps_shell_the_driver(variant: str) -> None:
    """On kubectl the authoritative state and the results live on the cluster, behind the driver."""
    steps = _by_name(variant)
    for name in ("poll_sweep", "collect", "metrics"):
        assert "--scale-test-base" in steps[name]["args"], name


@pytest.mark.parametrize("variant", IO_KUBECTL_VARIANTS)
def test_io_kubectl_submit_runs_to_completion(variant: str) -> None:
    """files-per-node + worker-directories refuses a duration-based run upstream."""
    assert "--run-to-completion" in _by_name(variant)["submit_sweep"]["args"]


@pytest.mark.parametrize("variant", KUBECTL_VARIANTS)
def test_preflight_runs_in_setup_before_submit(variant: str) -> None:
    """Validate the environment before anything is dispatched."""
    names = _names(variant)
    assert names.index("preflight") < names.index("submit_sweep")
    assert _by_name(variant)["preflight"]["phase"] == "setup"


@pytest.mark.parametrize("variant", VARIANTS)
def test_lifecycle_order(variant: str) -> None:
    """cancel before collect (it is what releases the Lease); resume after (collection-gated)."""
    names = _names(variant)
    if "cancel" in names:
        assert names.index("cancel") < names.index("collect")
    assert names.index("collect") < names.index("resume")
    for name in ("verify", "targets"):
        if name in names:  # the .out files are on the PVC until collect publishes them
            assert names.index("collect") < names.index(name)


@pytest.mark.parametrize("variant", VARIANTS)
def test_opt_in_steps_are_skipped_by_default(variant: str) -> None:
    """Cancelling or retrying a benchmark is an operator decision."""
    steps = _by_name(variant)
    for name in OPT_IN_STEPS:
        if name in steps:
            assert steps[name].get("skip") is True, name


@pytest.mark.parametrize("variant", VARIANTS)
def test_nothing_before_collect_can_skip_cleanup(variant: str) -> None:
    """A failing step without continue_on_failure breaks its phase, and collect releases the Lease."""
    steps = _steps(variant)
    collect = next(i for i, s in enumerate(steps) if s["name"] == "collect")
    phase = steps[collect]["phase"]
    offenders = [
        s["name"]
        for s in steps[:collect]
        if s["phase"] == phase and not s.get("continue_on_failure") and not s.get("skip")
    ]
    assert not offenders


def test_orchestrator_still_breaks_the_phase_on_a_failing_step() -> None:
    """The cleanup guarantee above (and the gate below) exist because of this line."""
    source = (ISVCTL_ROOT / "src" / "isvctl" / "orchestrator" / "step_executor.py").read_text()
    assert "if not step_result.success and not step.continue_on_failure:" in source


def test_mdtest_overlay_is_wired_end_to_end() -> None:
    """Every driver-facing step routes to mdtest, with mdtest thresholds and no IO-only steps."""
    steps = _by_name("mdtest")
    for name in ("submit_sweep", "poll_sweep", "cancel", "collect", "resume", "metrics", "qos_throughput"):
        assert _arg_after(steps[name], "--workload") == "mdtest", name
    assert "--tasks" in steps["submit_sweep"]["args"]
    assert "verify" not in steps and "targets" not in steps
    # Compare flag names only: thresholds are passed as `--flag={{ setting }}`.
    flags = {str(a).split("=", 1)[0] for a in steps["metrics"]["args"] if str(a).startswith("--")}
    assert not [f for f in flags if re.fullmatch(r"--min-(node-)?(write|read)-(gb-s|iops)", f)]
    assert "--min-create-ops-s" in flags


def test_multiclaim_overlay_only_raises_min_claims() -> None:
    """Multi-claim reuses the base steps unchanged and demands >= 2 claims."""
    raw = yaml.safe_load((CONFIG / "overlays" / "multiclaim.yaml").read_text())
    assert "commands" not in raw
    assert _merged("multiclaim")["tests"]["settings"]["min_claims"] == "2"
    assert _merged("io")["tests"]["settings"]["min_claims"] == "1"


@pytest.mark.parametrize("variant", VARIANTS)
def test_nothing_before_the_gate_can_stop_it_running(variant: str) -> None:
    """A test-phase step that fails without continue_on_failure stops the phase.

    If that happens before qos_throughput, the gate never runs and HSS02-01 reports SKIPPED
    ("step did not produce output") instead of FAILED -- exactly backwards when the failure
    was a missed throughput floor, which makes `metrics` exit non-zero.
    """
    steps = _steps(variant)
    gate = next(i for i, s in enumerate(steps) if s["name"] == "qos_throughput")
    offenders = [
        s["name"]
        for s in steps[:gate]
        if s["phase"] == "test" and not s.get("continue_on_failure") and not s.get("skip")
    ]
    assert not offenders


@pytest.mark.parametrize("variant", VARIANTS)
def test_settings_driven_values_ride_with_their_flag(variant: str) -> None:
    """An arg that is only `{{ setting }}` is DROPPED when the setting is blank.

    The orchestrator removes empty renders from argv (step_executor._render_args), so the
    flag before it is left with no value and argparse fails before the step prints JSON.
    `--flag={{ setting }}` renders to `--flag=`, which the script reads as "unset".
    """
    for step in _steps(variant):
        for arg in step.get("args", []):
            assert not re.fullmatch(r"\{\{\s*[A-Za-z_]\w*\s*\}\}", str(arg)), (
                f"{step['name']}: bare settings arg {arg!r}; write it as --flag={arg}"
            )


def test_a_blank_setting_reaches_the_script_as_unset() -> None:
    """End to end through the real renderer: Slurm's blank IOPS minimums survive as `--flag=`."""
    from isvctl.orchestrator.context import Context
    from isvctl.orchestrator.step_executor import StepExecutor

    merged = _merged("slurm")
    ctx = Context(RunConfig(**merged))
    ctx.set_step_output("submit_sweep", {"sweep_dir": "/tmp/x"})
    ctx.set_step_output("gen_env", {"substrate": "slurm"})
    argv = StepExecutor()._render_args(_by_name("slurm")["metrics"]["args"], ctx)
    assert "--min-write-iops=" in argv and "--min-read-iops=" in argv


def _script_default(script: str, flag: str) -> int:
    match = re.search(rf'"{re.escape(flag)}",[^)]*?default=(\d+)', (SCRIPTS / script).read_text())
    assert match, f"{script} has no numeric default for {flag}"
    return int(match.group(1))


@pytest.mark.parametrize("variant", VARIANTS)
def test_step_timeouts_outlast_the_scripts_own(variant: str) -> None:
    """A step killed by the orchestrator prints no JSON; the script must time out first."""
    steps = _by_name(variant)
    assert steps["submit_sweep"]["timeout"] > _script_default("submit_sweep.py", "--submit-timeout")
    poll = steps["poll_sweep"]
    worst = (
        int(_arg_after(poll, "--wait"))
        + int(_arg_after(poll, "--interval") or 0)
        + _script_default("poll_sweep.py", "--native-timeout")
    )
    assert poll["timeout"] > worst, f"poll can run {worst}s, step timeout is {poll['timeout']}s"


def test_gen_env_accepts_the_cleanup_steps_inputs() -> None:
    """Every step inherits the shell environment, including gen_env.

    The Slurm overlay's opt-in cleanup step reads SST_TEST_DATA_PATH and SST_TEST_ROOT; if
    gen_env rejected them as unknown keys, enabling cleanup would fail the run at step one.
    """
    env = {
        "PATH": os.environ["PATH"],
        "SST_SUBSTRATE": "slurm",
        "SST_TEST_DIRS": "/lustre/a:1",
        "SST_TEST_ROOT": "/lustre",
        "SST_TEST_DATA_PATH": "/lustre/a",
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gen_env_sh.py"), "--out", os.devnull],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_an_unset_results_dir_is_named_not_misreported(tmp_path: Path) -> None:
    """Unset SST_RESULTS_DIR renders the config's paths empty; both steps must say so."""
    gen = subprocess.run(
        [sys.executable, str(SCRIPTS / "gen_env_sh.py"), "--out", "/env.acv.sh"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "SST_SUBSTRATE": "kubectl", "SST_TEST_DIRS": "ns/c:p:1"},
        timeout=60,
    )
    if gen.returncode != 0:  # read-only / on this host; a root container would write it
        assert "SST_RESULTS_DIR" in json.loads(gen.stdout)["error"]
    # A minimal stub checkout, so submit gets past its checkout checks to the results dir.
    (tmp_path / "storage-tests" / "fs" / "kubectl").mkdir(parents=True)
    (tmp_path / "storage-tests" / "fs" / "nv-elbencho-sweep.sh").write_text("exit 0\n")
    (tmp_path / "storage-tests" / "fs" / "kubectl" / "_nv-elbencho-kubectl-functions.sh").write_text("")
    sub = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "submit_sweep.py"),
            "--env-sh",
            str(tmp_path / "x.sh"),
            "--config-hash",
            "h",
            "--substrate",
            "kubectl",
            "--results-dir",
            "",
            "--scale-test-base",
            str(tmp_path),
            "--nodes",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = json.loads(sub.stdout)
    assert out["success"] is False and "SST_RESULTS_DIR" in out["error"]


def test_gen_env_reports_bad_input_as_json() -> None:
    """Like every other step: a failure is {success: false, error}, not an empty stdout."""
    env = {"PATH": os.environ["PATH"], "SST_SUBSTRATE": "kubectl", "SST_NOT_A_KEY": "1"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "gen_env_sh.py"), "--out", os.devnull],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    out = json.loads(proc.stdout)
    assert proc.returncode == 1 and out["success"] is False
    assert out["platform"] == "storage" and "SST_NOT_A_KEY" in out["error"]


# --------------------------------------------------------------------------- HSS02-01 + gate


def test_suite_lets_hss02_run_on_kubernetes_and_slurm_clients() -> None:
    """HSS02-01 binds qos_throughput and is eligible for the substrates this provider runs on."""
    suite = yaml.safe_load(SUITE.read_text())
    check = suite["tests"]["validations"]["sds_controller"]["checks"]["HssQosThroughputCheck"]
    assert check["step"] == "qos_throughput"
    assert {"kubernetes", "slurm"} <= set(check["requires"])


@pytest.mark.parametrize("variant", VARIANTS)
def test_qos_throughput_is_the_last_test_step_and_fails_the_run(variant: str) -> None:
    """The verdict comes last, and its own failure must not be swallowed."""
    test_steps = [s for s in _steps(variant) if s["phase"] == "test"]
    assert test_steps[-1]["name"] == "qos_throughput"
    assert not test_steps[-1].get("continue_on_failure")


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_step_that_cannot_fail_the_run_feeds_the_gate(variant: str) -> None:
    """A continue_on_failure step never fails the run itself, so the gate must see its output.

    This is what lets the provider define no checks: drop a step from qos_throughput's inputs
    and its failure would pass silently.
    """
    ordered = _steps(variant)
    gate = next(i for i, s in enumerate(ordered) if s["name"] == "qos_throughput")
    qos = ordered[gate]
    # Only what runs before the verdict, in its phase. Teardown (e.g. Slurm's data cleanup)
    # comes after the verdict and has no bearing on it.
    feeds = [s["name"] for s in ordered[:gate] if s["phase"] == "test" and s.get("continue_on_failure")]
    feeds += [n for n in ("preflight",) if n in _names(variant)]
    for name in feeds:
        flag = "--" + name.replace("_", "-")
        template = _arg_after(qos, flag)
        assert template, f"qos_throughput never receives {name}"
        assert f"steps.{name}" in template and "tojson" in template, template
        # A step that times out or crashes stores no output, and `tojson` on that raises:
        # the orchestrator then passes the raw template through and the gate dies on "not
        # JSON" instead of reporting which step did not report.
        assert "default({})" in template, f"{name}: a missing output must render as {{}}, not crash rendering"


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_live_suite_probe_is_excluded(variant: str) -> None:
    """SAFETY: a step-less suite check runs straight against the cluster.

    Those probes provision PVCs, install CSI drivers and expand volumes. This provider only
    reads claims that already exist, and on a cluster whose shared filesystem class is a
    parallel filesystem a probe would create full-size Retain filesystems. Fails when the suite gains a live probe
    the exclude list does not cover -- add it to config/storage.yaml after deciding.
    """
    suite = yaml.safe_load(SUITE.read_text())
    live = {
        name
        for group in suite["tests"]["validations"].values()
        if isinstance(group, dict)
        for name, check in (group.get("checks") or {}).items()
        if not ((check or {}).get("step") or group.get("step"))
    }
    assert live, "suite parse found no live probes; the guard would be vacuous"
    excluded = set(_merged(variant)["tests"]["exclude"]["tests"])
    assert live <= excluded, f"live suite probes not excluded: {sorted(live - excluded)}"


@pytest.mark.parametrize("variant", VARIANTS)
def test_only_hss02_binds_a_step_this_provider_defines(variant: str) -> None:
    """Every other suite check is bound to a step this provider never runs, so it skips."""
    suite = yaml.safe_load(SUITE.read_text())
    ours = set(_names(variant))
    bound = {
        name
        for group in suite["tests"]["validations"].values()
        if isinstance(group, dict)
        for name, check in (group.get("checks") or {}).items()
        if ((check or {}).get("step") or group.get("step")) in ours
    }
    assert bound == {"HssQosThroughputCheck"}


# The gate script, exercised directly. Each case is an invariant a former per-step check held.

HEALTHY = {
    "preflight": {"success": True, "preflight_passed": True, "checks_run": 14},
    "poll_sweep": {
        "success": True,
        "lock_contended": False,
        "progress_source": "PVC",
        "counts_authoritative": True,
        "kubectl_executions_pending": 0,
        "kubectl_executions_running": 0,
    },
    "collect": {
        "success": True,
        "complete": True,
        "failed": 0,
        "results_csv_count": 1,
        "succeeded": 1,
        "failure_kind": "none",
        "is_storage_finding": False,
        "evidence_source_count": 2,
        "nodes_available_count": 2,
        "node_coverage": 0.5,
        "nodes_used_count": 1,
        "release_state_known": True,
        "resources_released": True,
    },
    "verify": {
        "success": True,
        "verified": True,
        "complete": True,
        "outputs_expected": 1,
        "directories_summarized": 1,
    },
    "targets": {"success": True, "weights_match": True, "claims_configured": 1, "claims_exercised": 1},
    "metrics": {
        "success": True,
        "samples": 2,
        "asserted": True,
        "checks": [
            {"name": "write_agg_gb_s", "passed": True, "observed": 0.195, "floor": 0.1, "detail": "w"},
            {"name": "read_agg_gb_s", "passed": True, "observed": 0.244, "floor": 0.12, "detail": "r"},
            {"name": "write_iops", "passed": True, "observed": 186.0, "floor": 90.0, "detail": "wi"},
            {"name": "read_iops", "passed": True, "observed": 233.0, "floor": 115.0, "detail": "ri"},
        ],
    },
    "cancel": {},
    "resume": {},
}


def _gate(overrides: dict[str, Any] | None = None, *extra: str, substrate: str = "kubectl") -> tuple[int, dict]:
    outputs = {**HEALTHY, **(overrides or {})}
    argv = [sys.executable, str(SCRIPTS / "qos_throughput.py"), "--substrate", substrate, *extra]
    for name, out in outputs.items():
        if out is not None:
            argv += ["--" + name.replace("_", "-"), json.dumps(out)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout)


def _patched(step: str, **fields: Any) -> dict[str, Any]:
    return {step: {**HEALTHY[step], **fields}}


def test_gate_passes_a_healthy_sweep_in_the_hss02_shape() -> None:
    """The output is exactly what HssQosThroughputCheck reads."""
    code, out = _gate()
    assert code == 0 and out["success"] is True
    assert out["platform"] == "storage" and out["test_name"] == "qos_throughput"
    bw, iops = out["tests"]["bandwidth_meets_min"], out["tests"]["iops_meets_min"]
    assert bw["passed"] and iops["passed"]
    # The headline pair is one check's measurement and its own floor, never mixed directions.
    assert (bw["binding_check"], bw["measured_mbps"], bw["min_mbps"]) == ("write_agg_gb_s", 195.0, 100.0)
    assert (iops["binding_check"], iops["measured_iops"], iops["min_iops"]) == ("read_iops", 233.0, 115.0)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (_patched("preflight", checks_run=0), "preflight: checks_run >= 1"),
        (_patched("poll_sweep", success=False), "poll_sweep: step succeeded"),
        (_patched("poll_sweep", lock_contended=True), "poll_sweep: lock_contended is false"),
        (_patched("poll_sweep", progress_source="LOCAL"), "poll_sweep: progress_source is PVC"),
        (_patched("collect", complete=False), "collect: every execution reached SUCCESS"),
        (_patched("collect", failed=1), "collect: failed == 0"),
        (_patched("collect", evidence_source_count=0), "collect: evidence_source_count >= 1"),
        (_patched("collect", nodes_used_count=0), "collect: nodes_used_count >= 1"),
        (_patched("verify", complete=False), "verify: full configured matrix ran"),
        (_patched("targets", weights_match=False), "targets: weights_match is true"),
        (_patched("metrics", asserted=False), "metrics: thresholds were applied"),
        ({"collect": {}}, "collect: step did not report"),
        (
            {
                "cancel": {
                    "success": True,
                    "finalised": False,
                    "release_state_known": True,
                    "resources_released": False,
                    "state": "CANCEL_REQUESTED",
                    "cancel_attempts": 1,
                }
            },
            "cancel: cancellation finalised",
        ),
        (
            {"resume": {"success": True, "new_attempt": False, "previous_attempt": "a", "attempt": "a", "resumed": 0}},
            "resume: a new attempt was dispatched",
        ),
    ],
)
def test_gate_refuses_a_verdict_on_an_invalid_sweep(overrides: dict[str, Any], reason: str) -> None:
    """Each invariant fails both HSS02-01 tests, naming why -- not as a throughput miss."""
    code, out = _gate(overrides)
    assert code == 1 and out["success"] is False
    for test in ("bandwidth_meets_min", "iops_meets_min"):
        assert out["tests"][test]["passed"] is False
        assert reason in out["tests"][test]["error"]
    assert out["error"].count("sweep not valid") == 1  # said once, not once per test


def test_gate_fails_the_run_but_not_hss02_when_cleanup_is_left_held() -> None:
    """A held Lease blocks the next run but does not make the measurement wrong.

    So HSS02-01's subtests still pass on their own merits, while the step fails -- turning
    the run red -- and says what is still held.
    """
    leak = "this attempt still owns cluster-side state: RESULT_COLLECTION='CLEANUP_PENDING'"
    code, out = _gate(_patched("collect", resources_released=False, leak_note=leak))
    assert code == 1 and out["success"] is False
    assert out["tests"]["bandwidth_meets_min"]["passed"] and out["tests"]["iops_meets_min"]["passed"]
    assert out["cleanup_errors"] == [leak] and leak in out["error"]


def test_gate_honours_a_warn_only_preflight() -> None:
    """--warn-only is an explicit operator override; the verdict must not refuse it."""
    pre = {"success": True, "preflight_passed": False, "warn_only_override": True, "checks_run": 14}
    code, out = _gate({"preflight": pre})
    assert code == 0 and out["success"] is True, out.get("error")


def test_gate_demands_the_configured_claim_count() -> None:
    """With --min-claims 2, a single-claim sweep is not a multi-claim result."""
    code, out = _gate(None, "--min-claims", "2")
    assert code == 1 and "claims_exercised >= 2" in out["error"]


def test_gate_fails_closed_without_an_iops_minimum() -> None:
    """An unconfigured minimum is not a met one."""
    metrics = {**HEALTHY["metrics"], "checks": [c for c in HEALTHY["metrics"]["checks"] if "iops" not in c["name"]]}
    code, out = _gate({"metrics": metrics})
    assert code == 1
    assert out["tests"]["bandwidth_meets_min"]["passed"] is True
    assert "no iops minimum configured" in out["tests"]["iops_meets_min"]["error"]


def test_gate_reports_a_missed_floor_as_a_throughput_failure() -> None:
    """A valid sweep below its floor fails on bandwidth, with the measurement in the message."""
    checks = [dict(c) for c in HEALTHY["metrics"]["checks"]]
    checks[0].update(passed=False, floor=99.0, detail="0.195 GB/s vs floor 99.0 GB/s")
    code, out = _gate({"metrics": {**HEALTHY["metrics"], "checks": checks}})
    assert code == 1
    assert out["tests"]["iops_meets_min"]["passed"] is True
    assert "0.195 GB/s vs floor 99.0 GB/s" in out["tests"]["bandwidth_meets_min"]["error"]


def test_gate_skips_bandwidth_on_mdtest() -> None:
    """mdtest measures operations, not bandwidth: HSS02-01 is skipped, not passed, when healthy."""
    metrics = {
        "success": True,
        "samples": 3,
        "asserted": True,
        "all_phases_measured": True,
        "phases_measured": ["create", "stat", "delete"],
        "checks": [{"name": "create_ops_s", "passed": True, "observed": 1700.0, "floor": 800.0, "detail": "c"}],
    }
    collect = {**HEALTHY["collect"], "workload": "mdtest"}
    code, out = _gate({"metrics": metrics, "collect": collect, "verify": None, "targets": None}, "--workload", "mdtest")
    assert code == 0
    assert out["tests"]["bandwidth_meets_min"]["skipped"] is True
    assert out["tests"]["iops_meets_min"]["passed"] is True


def test_gate_requires_metrics() -> None:
    """Without measurements there is nothing to judge."""
    code, out = _gate({"metrics": None})
    assert code == 1 and "--metrics is required" in out["error"]


# --------------------------------------------------------------------------- step-script suite


def _bash() -> str | None:
    for candidate in (shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        if candidate and Path(candidate).is_file():
            probe = subprocess.run([candidate, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True)
            if probe.stdout.strip().isdigit() and int(probe.stdout) >= 4:
                return candidate
    return None


def test_step_scripts_offline_suite() -> None:
    """Run test_steps.sh: hundreds of offline assertions over every step script.

    Opt-in: it takes ~5 minutes (hundreds of real subprocesses against stub drivers), which
    would multiply `make test`. Run it with NVSTORAGE_SCALE_STEP_SUITE=1 whenever a step
    script changes. Needs a storage-scale-test checkout (SST_CHECKOUT, default
    ../storage-scale-test next to this repo) for the real env.sh.template and drift checks.
    """
    if os.environ.get("NVSTORAGE_SCALE_STEP_SUITE") != "1":
        pytest.skip("slow (~5 min); set NVSTORAGE_SCALE_STEP_SUITE=1 to run the step-script suite")
    bash = _bash()
    if bash is None:
        pytest.skip("test_steps.sh needs bash >= 4")
    if not (SST_CHECKOUT / "env.sh.template").is_file():
        pytest.skip(f"no storage-scale-test checkout at {SST_CHECKOUT} (set SST_CHECKOUT)")
    proc = subprocess.run(
        [bash, str(HERE / "test_steps.sh")],
        capture_output=True,
        text=True,
        timeout=1800,
        env={**os.environ, "SST_CHECKOUT": str(SST_CHECKOUT), "PATH": f"{Path(bash).parent}:{os.environ['PATH']}"},
    )
    summary = re.search(r"^(\d+) passed, (\d+) failed$", proc.stdout, re.M)
    failures = "\n".join(line for line in proc.stdout.splitlines() if line.lstrip().startswith("FAIL"))
    assert summary, f"no summary line (the suite exited early):\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    assert summary.group(2) == "0" and proc.returncode == 0, failures

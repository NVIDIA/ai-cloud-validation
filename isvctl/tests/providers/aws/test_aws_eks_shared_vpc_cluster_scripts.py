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

"""Contract tests for AWS EKS shared-VPC cluster helper scripts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from isvtest.validations.k8s_multi_cluster import K8sMultiClusterSameVpcCheck

from isvctl.config.output_schemas import validate_output

AWS_EKS_DIR = Path(__file__).resolve().parents[3] / "configs" / "providers" / "aws" / "scripts" / "eks"


@pytest.mark.parametrize("script_name", ["create_shared_vpc_cluster.sh", "destroy_shared_vpc_cluster.sh"])
@pytest.mark.parametrize("state_file", ["../terraform.tfstate", "/tmp/shared-vpc-cluster.tfstate"])
def test_shared_vpc_cluster_scripts_reject_state_file_paths(script_name: str, state_file: str) -> None:
    """Shared-cluster state overrides must stay local to terraform-shared-vpc-cluster/."""
    env = {**os.environ, "SHARED_VPC_CLUSTER_STATE_FILE": state_file}

    completed = subprocess.run(
        ["bash", str(AWS_EKS_DIR / script_name)],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert completed.returncode == 1
    assert "SHARED_VPC_CLUSTER_STATE_FILE must be a local .tfstate filename" in completed.stderr


def _run_creation(tmp_path: Path, fault: str = "") -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    """Run the actual shell workflow with stubbed cloud/cluster CLIs and real jq."""
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is required to exercise the provider shell script")
    scripts = tmp_path / "eks"
    (scripts / "terraform").mkdir(parents=True)
    (scripts / "terraform" / "terraform.tfstate").write_text("{}")
    (scripts / "terraform-shared-vpc-cluster").mkdir()
    script = scripts / "create_shared_vpc_cluster.sh"
    shutil.copyfile(AWS_EKS_DIR / script.name, script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = (
        f"#!{sys.executable}\n"
        + r"""
import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
fault = os.environ["TEST_FAULT"]
entry = {"tool": tool, "args": args}
if tool == "kubectl":
    entry["cluster"] = Path(os.environ["KUBECONFIG"]).read_text()
with open(os.environ["TEST_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")
if tool == "terraform":
    if "output" in args:
        print("us-west-2" if args[-1] == "region" else
              "primary" if any(a.startswith("-chdir=") for a in args) else "secondary")
elif tool == "aws":
    if args[:2] == ["sts", "get-caller-identity"]:
        print("123456789012")
    elif args[:2] == ["eks", "describe-cluster"]:
        print(json.dumps({"cluster": {"status": "ACTIVE", "resourcesVpcConfig": {"vpcId": "vpc-123"}}}))
    elif args[:2] == ["eks", "update-kubeconfig"]:
        name = args[args.index("--name") + 1]
        if fault == name + "-config":
            sys.exit(1)
        config = args[args.index("--kubeconfig") + 1]
        Path(config).write_text(name)
    else:
        sys.exit(99)
elif tool == "kubectl":
    name = entry["cluster"]
    assert "--request-timeout=10s" in args
    if "--raw" in args:
        if fault == name + "-api":
            sys.exit(1)
        print("ok")
    elif "namespace" in args:
        print("" if fault == name + "-uid" else
              "same-uid" if fault == "duplicate-uid" else name + "-uid")
    elif "nodes" in args:
        if fault == name + "-nodes-error":
            sys.exit(1)
        conditions = [{"type": "Ready", "status": "False" if fault == name + "-nodes" else "True"}]
        print(json.dumps({"items": [{"status": {"conditions": conditions}}]}))
    else:
        sys.exit(99)
"""
    )
    for tool in ("terraform", "aws", "kubectl"):
        executable = bin_dir / tool
        executable.write_text(stub)
        executable.chmod(0o755)
    (bin_dir / "jq").symlink_to(jq)
    log = tmp_path / "calls.jsonl"
    caller_config = tmp_path / "caller-config"
    caller_config.write_text("caller context unchanged")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "KUBECONFIG": str(caller_config),
        "TEST_LOG": str(log),
        "TEST_FAULT": fault,
        "TF_AUTO_APPROVE": "true",
        "SHARED_VPC_CLUSTER_STATE_FILE": "terraform.tfstate",
        "SECONDARY_CLUSTER_READY_TIMEOUT": "0",
        "SECONDARY_CLUSTER_POLL_INTERVAL": "1",
    }
    completed = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=30)
    assert caller_config.read_text() == "caller context unchanged"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    configs = [c["args"][c["args"].index("--kubeconfig") + 1] for c in calls if "--kubeconfig" in c["args"]]
    assert len(configs) == len(set(configs))
    assert all(not Path(path).exists() for path in configs), "temporary kubeconfigs must be cleaned up"
    return completed, calls


def test_creation_contacts_both_clusters_and_emits_usable_evidence(tmp_path: Path) -> None:
    """The provisioned clusters must both supply API health, identity and Ready-node evidence."""
    completed, calls = _run_creation(tmp_path)
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    valid, errors = validate_output(output, "multi_cluster")
    assert valid, errors
    check = K8sMultiClusterSameVpcCheck(config={"step_output": output})
    check.run()
    assert check.passed, check.message
    for name in ("primary", "secondary"):
        probes = [call["args"] for call in calls if call.get("cluster") == name]
        assert any("/readyz" in args for args in probes)
        assert any("namespace" in args for args in probes)
        assert any("nodes" in args for args in probes)


@pytest.mark.parametrize("cluster", ["primary", "secondary"])
@pytest.mark.parametrize("failure", ["api", "nodes", "nodes-error", "uid", "config"])
def test_creation_fails_if_either_cluster_cannot_prove_health(tmp_path: Path, cluster: str, failure: str) -> None:
    """An API/configuration failure, missing identity or zero Ready nodes cannot emit success."""
    completed, calls = _run_creation(tmp_path, f"{cluster}-{failure}")
    assert any(
        call["tool"] == "aws" and "update-kubeconfig" in call["args"] and cluster in call["args"] for call in calls
    )
    assert completed.returncode != 0
    assert not completed.stdout.strip()


def test_creation_evidence_rejects_two_contexts_pointing_at_one_cluster(tmp_path: Path) -> None:
    """The real emitted API identities let the validator detect a misdirected kubeconfig."""
    completed, _ = _run_creation(tmp_path, "duplicate-uid")
    assert completed.returncode == 0, completed.stderr
    check = K8sMultiClusterSameVpcCheck(config={"step_output": json.loads(completed.stdout)})
    check.run()
    assert not check.passed
    assert "Duplicate cluster_uid" in check.message

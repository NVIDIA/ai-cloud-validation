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

"""Exercise the AWS workload-IAM workflow with fake AWS and Kubernetes boundaries."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from botocore.exceptions import ClientError, NoCredentialsError
from isvtest.validations.k8s_service_account_iam import K8sServiceAccountIamCheck

from isvctl.config.output_schemas import validate_output

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "configs/providers/aws/scripts/eks/service_account_iam.py"
NAMESPACE = "isv-ksa-" + "1" * 12
ROLE_ARN = f"arn:aws:iam::123456789012:role/{NAMESPACE}"
DENIAL = "An error occurred (AccessDenied) when calling the GetObject operation: Access Denied"


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Supply real workflow code with controlled cloud APIs and kubectl results."""
    spec = importlib.util.spec_from_file_location("service_account_iam_probe", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    eks, iam, s3 = MagicMock(), MagicMock(), MagicMock()
    eks.describe_cluster.return_value = {
        "cluster": {
            "arn": "arn:aws:eks:us-west-2:123456789012:cluster/test",
            "identity": {"oidc": {"issuer": "https://oidc.eks.us-west-2.amazonaws.com/id/ISSUER"}},
        }
    }
    iam.create_role.return_value = {"Role": {"Arn": ROLE_ARN, "RoleId": "expected-role-id"}}
    session = MagicMock()
    session.client.side_effect = lambda name, **kwargs: {"eks": eks, "iam": iam, "s3": s3}[name]
    monkeypatch.setattr(module.boto3, "Session", lambda **kwargs: session)
    monkeypatch.setattr(module.shutil, "which", lambda executable: f"/bin/{executable}")
    monkeypatch.setattr(
        module.uuid, "uuid4", MagicMock(side_effect=[SimpleNamespace(hex="1" * 32), SimpleNamespace(hex="2" * 32)])
    )
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.delenv("KUBECTL", raising=False)
    state = SimpleNamespace(module=module, eks=eks, iam=iam, s3=s3, calls=[], manifests=[], fault="", retries=0)

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Model only external command outcomes, not the workflow's decisions."""
        state.calls.append((args, kwargs))
        stdout, stderr, code = "", "", 0
        if args[:3] == ["aws", "eks", "update-kubeconfig"]:
            Path(kwargs["env"]["KUBECONFIG"]).write_text("temporary target cluster config")
        elif "--raw" in args:
            stdout = "not ready" if state.fault == "api" else "ok\n"
        elif "create" in args:
            if kwargs.get("input"):
                state.manifests.append(json.loads(kwargs["input"]))
                if state.fault == "pod-create" and state.manifests[-1]["kind"] == "Pod":
                    code, stderr = 1, "admission denied"
            elif state.fault == "namespace-create":
                code, stderr = 1, "namespace already exists"
        elif "wait" in args:
            if state.fault == "timeout":
                raise subprocess.TimeoutExpired(args, 30)
        elif "get" in args and "pod" in args:
            stdout = json.dumps(
                {
                    "metadata": {"namespace": NAMESPACE},
                    "spec": {"serviceAccountName": "wrong" if state.fault == "service-account" else "workload"},
                }
            )
        elif "exec" in args:
            if "/bin/sh" in args:
                if state.fault == "token":
                    code, stderr = 1, "web identity token not injected"
            elif "get-caller-identity" in args:
                stdout = json.dumps(
                    {"UserId": "node-role:session" if state.fault == "identity" else "expected-role-id:session"}
                )
                if state.fault == "identity-json":
                    stdout = "invalid JSON"
                if state.fault == "sts":
                    code, stderr = 1, "Could not connect to STS endpoint"
                if state.fault == "propagation" and state.retries == 0:
                    state.retries += 1
                    code, stderr = 1, "An error occurred (AccessDenied) when calling AssumeRoleWithWebIdentity"
            elif "get-object" in args:
                if args[args.index("--key") + 1] == "allowed":
                    if state.fault == "allowed":
                        code, stderr = 1, DENIAL
                elif state.fault != "overprivileged":
                    code, stderr = (
                        1,
                        {
                            "network": "Could not connect to endpoint",
                            "missing-object": "An error occurred (NoSuchKey) when calling the GetObject operation",
                            "wrong-denial": "An error occurred (AccessDenied) when calling the AssumeRoleWithWebIdentity operation",
                        }.get(state.fault, DENIAL),
                    )
            elif "cat" in args:
                stdout = "wrong content" if state.fault == "content" else "2" * 32
            else:
                raise AssertionError(args)
        elif "delete" in args:
            if state.fault == "namespace-cleanup":
                code, stderr = 1, "namespace deletion failed"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    monkeypatch.setattr(module.subprocess, "run", run)
    return state


def execute(probe: SimpleNamespace, region: str = "us-west-2") -> dict[str, Any]:
    """Execute and schema-check the real provider workflow's output."""
    output = probe.module.run_probe("test", region, probe.module.DEFAULT_IMAGE, 180)
    valid, errors = validate_output(output, "service_account_iam")
    assert valid, errors
    return output


def test_workload_identity_and_scopes_pass_and_cleanup(
    probe: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Observe identity and both S3 outcomes inside the pod, preserving caller configuration."""
    original = tmp_path / "caller-config"
    original.write_text("unchanged")
    monkeypatch.setenv("KUBECONFIG", str(original))
    output = execute(probe)
    check = K8sServiceAccountIamCheck(config={"step_output": output})
    check.run()
    assert check.passed, check.message
    assert original.read_text() == "unchanged"
    configs = {kwargs["env"]["KUBECONFIG"] for _, kwargs in probe.calls}
    assert len(configs) == 1 and str(original) not in configs
    assert all(not Path(path).exists() for path in configs)
    assert sum("get-object" in args for args, _ in probe.calls) == 2
    assert sum("get-caller-identity" in args for args, _ in probe.calls) == 1
    probe.iam.delete_role.assert_called_once_with(RoleName=NAMESPACE)
    probe.iam.delete_role_policy.assert_called_once()
    assert probe.s3.delete_object.call_count == 2
    probe.s3.delete_bucket.assert_called_once()
    assert any("delete" in args and "namespace" in args for args, _ in probe.calls)


def test_binding_and_permission_policy_are_scoped(probe: SimpleNamespace) -> None:
    """The trust policy pins namespace, SA and audience; object access excludes the denied key."""
    execute(probe)
    trust = json.loads(probe.iam.create_role.call_args.kwargs["AssumeRolePolicyDocument"])
    conditions = trust["Statement"][0]["Condition"]["StringEquals"]
    assert set(conditions.values()) == {"sts.amazonaws.com", f"system:serviceaccount:{NAMESPACE}:workload"}
    policy = json.loads(probe.iam.put_role_policy.call_args.kwargs["PolicyDocument"])
    assert policy["Statement"] == [
        {"Effect": "Allow", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{NAMESPACE}-123456789012/allowed"}
    ]
    service_account, pod = probe.manifests
    assert service_account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"] == ROLE_ARN
    assert pod["spec"]["serviceAccountName"] == "workload"
    assert "volumes" not in pod["spec"]  # Token must be injected by the platform.
    environment = pod["spec"]["containers"][0]["env"]
    assert {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"} in environment
    assert not {item["name"] for item in environment} & {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }


@pytest.mark.parametrize(
    "fault",
    [
        "token",
        "identity",
        "identity-json",
        "service-account",
        "sts",
        "allowed",
        "content",
        "overprivileged",
        "network",
        "missing-object",
        "wrong-denial",
        "timeout",
        "pod-create",
    ],
)
def test_failed_execution_is_not_a_skip_and_always_cleans_up(probe: SimpleNamespace, fault: str) -> None:
    """Network/STS errors and bad bindings must not count as scope denials or missing prerequisites."""
    probe.fault = fault
    output = execute(probe)
    assert output["success"] is False and not output.get("skipped") and output["error"]
    probe.iam.delete_role.assert_called_once()
    probe.s3.delete_bucket.assert_called_once()
    assert any("delete" in args for args, _ in probe.calls)
    assert all(not Path(kwargs["env"]["KUBECONFIG"]).exists() for _, kwargs in probe.calls)


def test_failed_namespace_creation_does_not_delete_existing_namespace(probe: SimpleNamespace) -> None:
    """Only a namespace successfully created by this run is removed."""
    probe.fault = "namespace-create"
    assert execute(probe)["success"] is False
    assert not any("delete" in args for args, _ in probe.calls)
    probe.iam.delete_role.assert_called_once()
    probe.s3.delete_bucket.assert_called_once()


def test_iam_propagation_denial_is_retried(probe: SimpleNamespace) -> None:
    """A transient initial STS denial may recover without weakening final identity assertions."""
    probe.fault = "propagation"
    assert execute(probe)["success"] is True
    assert sum("get-caller-identity" in args for args, _ in probe.calls) == 2


def test_cleanup_errors_fail_and_do_not_block_other_cleanup(probe: SimpleNamespace) -> None:
    """A namespace deletion failure must still attempt every AWS deletion."""
    probe.fault = "namespace-cleanup"
    probe.s3.delete_bucket.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteBucket")
    output = execute(probe)
    assert output["success"] is False and len(output["cleanup_errors"]) == 2
    probe.iam.delete_role.assert_called_once()
    probe.iam.delete_role_policy.assert_called_once()


@pytest.mark.parametrize("fault", ["missing-cli", "missing-credentials", "missing-oidc"])
def test_missing_prerequisites_skip_without_creating_resources(
    probe: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Do not create fixtures when the provider probe cannot start."""
    if fault == "missing-cli":
        monkeypatch.setattr(probe.module.shutil, "which", lambda name: None)
    elif fault == "missing-credentials":
        probe.eks.describe_cluster.side_effect = NoCredentialsError()
    else:
        probe.iam.get_open_id_connect_provider.side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity"}}, "GetOpenIDConnectProvider"
        )
    output = execute(probe)
    assert output["skipped"] is True and output["success"] is False
    probe.iam.create_role.assert_not_called()
    probe.s3.create_bucket.assert_not_called()


def test_api_failure_is_a_failure_before_mutations(probe: SimpleNamespace) -> None:
    """An existing cluster reporting unhealthy is not a missing prerequisite."""
    probe.fault = "api"
    output = execute(probe)
    assert not output["success"] and not output.get("skipped")
    probe.iam.create_role.assert_not_called()


def test_credentials_disappear_after_start_is_failure(probe: SimpleNamespace) -> None:
    """An execution-time credential error must not turn a started test into a skip."""
    probe.iam.put_role_policy.side_effect = NoCredentialsError()
    output = execute(probe)
    assert not output["success"] and not output.get("skipped")
    probe.iam.delete_role.assert_called_once()


def test_failed_object_setup_cleans_up_only_created_fixtures(probe: SimpleNamespace) -> None:
    """Partial setup still removes the role and bucket without deleting unrelated objects."""
    probe.s3.put_object.side_effect = [None, ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")]
    assert execute(probe)["success"] is False
    probe.s3.delete_object.assert_called_once_with(Bucket=f"{NAMESPACE}-123456789012", Key="allowed")
    probe.s3.delete_bucket.assert_called_once()
    probe.iam.delete_role.assert_called_once()


def test_us_east_1_bucket_creation_omits_location_constraint(probe: SimpleNamespace) -> None:
    """S3's default region requires a different create-bucket request."""
    assert execute(probe, "us-east-1")["success"] is True
    assert "CreateBucketConfiguration" not in probe.s3.create_bucket.call_args.kwargs


def test_schema_rejects_success_without_runtime_evidence() -> None:
    """A bare provider success flag does not satisfy the output contract."""
    valid, _ = validate_output(
        {"success": True, "platform": "kubernetes", "test_name": "service_account_iam"}, "service_account_iam"
    )
    assert not valid


def test_suite_and_aws_provider_wire_same_test() -> None:
    """K8S16-01 is discoverable and executes the AWS probe in the test phase."""
    suite = yaml.safe_load((ROOT / "configs/suites/k8s.yaml").read_text())
    provider = yaml.safe_load((ROOT / "configs/providers/aws/config/eks.yaml").read_text())
    check = suite["tests"]["validations"]["k8s_identity"]["checks"]["K8sServiceAccountIamCheck"]
    assert check["test_id"] == "K8S16-01"
    step = next(s for s in provider["commands"]["kubernetes"]["steps"] if s["name"] == check["step"])
    assert step["phase"] == "test" and step["output_schema"] == "service_account_iam"

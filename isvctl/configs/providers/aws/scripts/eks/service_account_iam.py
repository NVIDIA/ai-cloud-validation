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

"""Exercise EKS IRSA from a pod, including allowed and out-of-scope S3 reads.

Requires an existing EKS cluster with its IAM OIDC provider configured. Creates
only uniquely named test resources and removes them before emitting evidence.
The controller's credentials are never passed to the pod. The pod must receive
its role/token from the EKS ServiceAccount webhook; EC2 metadata fallback is off.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from common.errors import classify_aws_error, delete_with_retry, stamp_test_errors

DEFAULT_IMAGE = "public.ecr.aws/aws-cli/aws-cli:2.34.0"
POD_READY_TIMEOUT = 180
PROBES = ("identity", "allowed_access", "out_of_scope_denied")


def command(
    args: list[str], env: dict[str, str], *, payload: dict | None = None, timeout: int = 45, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a bounded subprocess without a shell and never print credentials."""
    result = subprocess.run(
        args, env=env, input=json.dumps(payload) if payload else None, capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result


def cleanup(errors: list[str], label: str, delete: Callable[..., Any], **kwargs: Any) -> None:
    """Delete one AWS fixture with retries, recording a failure without masking others."""
    if not delete_with_retry(delete, resource_desc=label, **kwargs):
        errors.append(f"Could not {label}")


def run_probe(cluster_name: str, region: str, image: str) -> dict[str, Any]:
    """Create a scoped role and use its ServiceAccount binding from a real pod."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "kubernetes",
        "test_name": "service_account_iam",
        "tests": {probe: {"passed": False} for probe in PROBES},
    }
    tests = result["tests"]
    kubectl = shlex.split(os.environ.get("KUBECTL") or "kubectl")
    if not cluster_name or not kubectl or not shutil.which(kubectl[0]) or not shutil.which("aws"):
        return dict(result, skipped=True, skip_reason="EKS cluster name, kubectl, and AWS CLI are required")
    errors: list[str] = []
    try:
        config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2})
        session = boto3.Session(region_name=region)
        eks, iam, s3 = (session.client(name, config=config) for name in ("eks", "iam", "s3"))
        try:
            cluster = eks.describe_cluster(name=cluster_name)["cluster"]
        except NoCredentialsError:
            return dict(result, skipped=True, skip_reason="AWS credentials are not configured")
        arn = cluster["arn"].split(":")
        partition, account = arn[1], arn[4]
        issuer = cluster.get("identity", {}).get("oidc", {}).get("issuer", "")
        if not issuer.startswith("https://"):
            return dict(result, skipped=True, skip_reason="EKS cluster has no OIDC issuer for IRSA")
        provider = issuer.removeprefix("https://")
        provider_arn = f"arn:{partition}:iam::{account}:oidc-provider/{provider}"
        try:
            iam.get_open_id_connect_provider(OpenIDConnectProviderArn=provider_arn)
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchEntity":
                return dict(result, skipped=True, skip_reason="The cluster IAM OIDC provider is not configured")
            raise

        name = "isv-ksa-" + uuid.uuid4().hex[:12]
        bucket = f"{name}-{account}"
        nonce = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="isv-ksa-iam-") as directory, ExitStack() as stack:
            env = dict(os.environ, KUBECONFIG=os.path.join(directory, "config"))
            command(
                [
                    "aws",
                    "eks",
                    "update-kubeconfig",
                    "--name",
                    cluster_name,
                    "--region",
                    region,
                    "--kubeconfig",
                    env["KUBECONFIG"],
                ],
                env,
            )

            def kube(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                """Use the target cluster without changing the caller's kubeconfig."""
                # The subprocess timeout also bounds waits and namespace cleanup,
                # without a shorter HTTP timeout interrupting a healthy watch.
                return command([*kubectl, *args], env, **kwargs)

            if kube(["get", "--raw", "/readyz"]).stdout.strip() != "ok":
                raise RuntimeError("The selected cluster API is not ready")
            trust = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Federated": provider_arn},
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Condition": {
                            "StringEquals": {
                                f"{provider}:aud": "sts.amazonaws.com",
                                f"{provider}:sub": f"system:serviceaccount:{name}:workload",
                            }
                        },
                    }
                ],
            }
            role = iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Tags=[{"Key": "CreatedBy", "Value": "isvtest"}],
            )["Role"]
            stack.callback(cleanup, errors, f"delete role {name}", iam.delete_role, RoleName=name)
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "s3:GetObject",
                        "Resource": f"arn:{partition}:s3:::{bucket}/allowed",
                    }
                ],
            }
            iam.put_role_policy(RoleName=name, PolicyName="read-allowed-object", PolicyDocument=json.dumps(policy))
            stack.callback(
                cleanup,
                errors,
                "delete role policy",
                iam.delete_role_policy,
                RoleName=name,
                PolicyName="read-allowed-object",
            )

            bucket_args: dict[str, Any] = {"Bucket": bucket}
            if region != "us-east-1":
                bucket_args["CreateBucketConfiguration"] = {"LocationConstraint": region}
            s3.create_bucket(**bucket_args)
            stack.callback(cleanup, errors, f"delete bucket {bucket}", s3.delete_bucket, Bucket=bucket)
            s3.put_public_access_block(
                Bucket=bucket,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
            for key in ("allowed", "denied"):
                s3.put_object(Bucket=bucket, Key=key, Body=nonce.encode())
                stack.callback(cleanup, errors, f"delete object {key}", s3.delete_object, Bucket=bucket, Key=key)

            kube(["create", "namespace", name])

            @stack.callback
            def delete_namespace() -> None:
                """Remove the namespace and everything the probe created in it."""
                try:
                    kube(["delete", "namespace", name, "--ignore-not-found", "--timeout=120s"], timeout=135)
                except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                    errors.append(f"delete namespace {name}: {error}")

            kube(
                ["create", "-f", "-"],
                payload={
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {
                        "name": "workload",
                        "namespace": name,
                        "annotations": {
                            "eks.amazonaws.com/role-arn": role["Arn"],
                            "eks.amazonaws.com/sts-regional-endpoints": "true",
                        },
                    },
                },
            )
            kube(
                ["create", "-f", "-"],
                payload={
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": "probe", "namespace": name},
                    "spec": {
                        "serviceAccountName": "workload",
                        "restartPolicy": "Never",
                        # The sleeping probe ignores SIGTERM; don't make namespace cleanup wait it out.
                        "terminationGracePeriodSeconds": 0,
                        "containers": [
                            {
                                "name": "probe",
                                "image": image,
                                "command": ["/bin/sh", "-c", "sleep 1200"],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "1", "memory": "512Mi"},
                                },
                                "env": [
                                    {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"},
                                    {"name": "AWS_DEFAULT_REGION", "value": region},
                                    {"name": "AWS_MAX_ATTEMPTS", "value": "2"},
                                ],
                            }
                        ],
                    },
                },
            )
            kube(
                ["wait", "-n", name, "pod/probe", "--for=condition=Ready", f"--timeout={POD_READY_TIMEOUT}s"],
                timeout=POD_READY_TIMEOUT + 15,
            )
            execute = ["exec", "-n", name, "probe", "-c", "probe", "--"]
            # Inspect the webhook's binding without printing the token or accepting node credentials.
            kube(
                [
                    *execute,
                    "/bin/sh",
                    "-c",
                    'test "$AWS_ROLE_ARN" = "$1" && test -n "$AWS_WEB_IDENTITY_TOKEN_FILE" '
                    '&& test -r "$AWS_WEB_IDENTITY_TOKEN_FILE" && test -z "$AWS_ACCESS_KEY_ID"',
                    "probe",
                    role["Arn"],
                ]
            )

            def aws_in_pod(args: list[str], retry_denied: bool = False) -> subprocess.CompletedProcess[str]:
                """Retry only explicit IAM propagation denials; leave other failures visible."""
                args = [*execute, "aws", "--cli-connect-timeout", "10", "--cli-read-timeout", "20", *args]
                for _ in range(5):
                    response = kube(args, check=False)
                    if response.returncode == 0 or not retry_denied or "(AccessDenied)" not in response.stderr:
                        return response
                    time.sleep(5)
                return kube(args, check=False)

            identity = aws_in_pod(["sts", "get-caller-identity", "--output", "json"], retry_denied=True)
            if identity.returncode:
                raise RuntimeError(f"Pod could not assume its role: {identity.stderr.strip()}")
            user_id = json.loads(identity.stdout).get("UserId")
            # Compare the immutable role ID: a node or other role cannot match it.
            if not isinstance(user_id, str) or user_id.split(":", 1)[0] != role["RoleId"]:
                raise RuntimeError("Pod assumed an unexpected IAM identity")
            tests["identity"] = {
                "passed": True,
                "message": "Pod assumed its ServiceAccount's role via a federated token",
            }

            allowed = aws_in_pod(
                ["s3api", "get-object", "--bucket", bucket, "--key", "allowed", "/tmp/allowed"], retry_denied=True
            )
            if allowed.returncode:
                raise RuntimeError(f"Allowed object read failed: {allowed.stderr.strip()}")
            if kube([*execute, "cat", "/tmp/allowed"]).stdout != nonce:
                raise RuntimeError("Allowed object read returned unexpected content")
            tests["allowed_access"] = {"passed": True, "message": "Pod read the object its policy allows"}
            denied = aws_in_pod(["s3api", "get-object", "--bucket", bucket, "--key", "denied", "/tmp/denied"])
            if denied.returncode == 0 or (
                "An error occurred (AccessDenied) when calling the GetObject operation" not in denied.stderr
            ):
                raise RuntimeError("Out-of-scope object read was not denied by authorization")
            tests["out_of_scope_denied"] = {"passed": True, "message": "Pod was denied an object outside its policy"}
            result["success"] = True
    except (
        BotoCoreError,
        ClientError,
        OSError,
        subprocess.SubprocessError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        result["error_type"], result["error"] = classify_aws_error(error)
        stamp_test_errors(result, result["error"])
    if errors:
        result.update(success=False, cleanup_errors=errors)
    return result


def main() -> int:
    """Emit provider-neutral evidence for K8S16-01."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", default=os.environ.get("EKS_CLUSTER_NAME", ""))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    output = run_probe(args.cluster_name, args.region, args.image)
    print(json.dumps(output, indent=2))
    return 0 if output["success"] or output.get("skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())

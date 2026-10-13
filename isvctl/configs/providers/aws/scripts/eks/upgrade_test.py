#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in EKS upgrade experiments with independent HTTP and Kubernetes observers.

Use a disposable cluster. This script changes cluster/node versions;
it removes its own canary, but infrastructure teardown belongs to the caller.
"""

import argparse
import ipaddress
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from kubernetes import client, config

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared/k8s"))

from upgrade_canary import UpgradeCanary
from upgrade_observer import ApplicationObserver


def timestamp(value: str) -> float:
    """Convert an actual Kubernetes audit timestamp to epoch seconds."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def node_evidence(node: dict[str, Any]) -> dict[str, Any]:
    """Read node identity, AMI revision and readiness from Kubernetes."""
    return {
        "uid": node["metadata"]["uid"],
        "name": node["metadata"]["name"],
        "revision": node["metadata"].get("labels", {}).get("eks.amazonaws.com/nodegroup-image", ""),
        "ready": any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", [])),
        "schedulable": not node.get("spec", {}).get("unschedulable", False),
    }


class PoolObserver:
    """Persist pod placement, node state and PDB status independently of AWS polling."""

    def __init__(self, api: client.ApiClient, canary: UpgradeCanary, pool: str, artifact: Path) -> None:
        """Observe only the selected managed pool and uniquely owned canary."""
        self.api, self.canary, self.pool, self.artifact = api, canary, pool, artifact
        self.samples: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.done = threading.Event()
        self.first = threading.Event()
        self.thread = threading.Thread(target=self.observe, daemon=True)

    def get(self, path: str, query: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        """Issue bounded authenticated GETs without spawning kubectl per sample."""
        return self.api.call_api(
            path,
            "GET",
            query_params=query or [],
            response_type="object",
            auth_settings=["BearerToken"],
            _return_http_data_only=True,
            _request_timeout=(2, 2),
        )

    def sample(self) -> dict[str, Any]:
        """Keep raw status values; unavailable or stale observations are not successes."""
        ns = self.canary.namespace
        at = time.time()
        pdb = self.get(f"/apis/policy/v1/namespaces/{ns}/poddisruptionbudgets/canary")
        status, metadata = pdb.get("status", {}), pdb["metadata"]
        budget = {
            "at": at,
            "uid": metadata["uid"],
            "generation": metadata["generation"],
            "observed_generation": status.get("observedGeneration", 0),
            "expected_pods": status.get("expectedPods", 0),
            "desired_healthy": status.get("desiredHealthy", 0),
            "current_healthy": status.get("currentHealthy", 0),
            "disruptions_allowed": status.get("disruptionsAllowed", 0),
        }
        nodes = self.get("/api/v1/nodes", [("labelSelector", f"eks.amazonaws.com/nodegroup={self.pool}")])["items"]
        pods = self.get(f"/api/v1/namespaces/{ns}/pods")["items"]
        return {
            "at": at,
            "pdb": budget,
            "nodes": [node_evidence(n) for n in nodes],
            "pods": [
                {
                    "uid": p["metadata"]["uid"],
                    "name": p["metadata"]["name"],
                    "node": p.get("spec", {}).get("nodeName", ""),
                    "terminating": bool(p["metadata"].get("deletionTimestamp")),
                }
                for p in pods
            ],
        }

    def observe(self) -> None:
        """Flush observations and surface API/persistence failures to the main thread."""
        try:
            with self.artifact.open("x") as stream:
                while not self.done.is_set():
                    began = time.monotonic()
                    sample = self.sample()
                    stream.write(json.dumps(sample) + "\n")
                    stream.flush()
                    self.samples.append(sample)
                    self.first.set()
                    self.done.wait(max(0, 1 - (time.monotonic() - began)))
        except Exception as error:
            self.error = error
            self.first.set()

    def start(self) -> None:
        """Require the baseline before any upgrade request."""
        self.thread.start()
        if not self.first.wait(15) or self.error:
            raise RuntimeError("Could not start pool observer") from self.error

    def stop(self) -> None:
        """Bound shutdown and fail on lost evidence."""
        self.done.set()
        if self.thread.ident is not None:
            self.thread.join(15)
        if self.thread.is_alive() or self.error:
            raise RuntimeError("Pool observation failed or did not stop") from self.error


def audit_events(logs: Any, cluster: str, namespace: str, start: float, end: float) -> list[dict[str, Any]]:
    """Read every page of the updater's namespace audit records, including deletes."""
    events = {}
    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=f"/aws/eks/{cluster}/cluster",
        logStreamNamePrefix="kube-apiserver-audit",
        startTime=int(start * 1000),
        endTime=int(end * 1000) + 1,
        filterPattern='{ ($.objectRef.namespace = "' + namespace + '") && ($.user.username = "eks:node-manager") }',
    )
    for page in pages:
        for event in page.get("events", []):
            record = json.loads(event["message"])
            if record.get("stage") == "ResponseComplete":
                events[record["auditID"]] = record
    return sorted(events.values(), key=lambda e: e["requestReceivedTimestamp"])


def drain_evidence(
    records: list[dict[str, Any]], samples: list[dict[str, Any]], before: list[dict[str, Any]]
) -> dict[str, Any]:
    """Correlate the EKS updater's real audit events with observed pod/node identities."""
    nodes = {n["name"]: n["uid"] for n in before}
    pods = {p["name"]: p for s in samples for p in s["pods"] if p["node"] in nodes}
    pdb_uid = samples[0]["pdb"]["uid"]
    evictions, blocked = [], []
    for record in records:
        # Only EKS's managed-node updater can substantiate this experiment.
        if record.get("user", {}).get("username") != "eks:node-manager":
            continue
        ref = record.get("objectRef", {})
        if ref.get("resource") != "pods":
            continue
        method = (
            "eviction"
            if ref.get("subresource") == "eviction" and record.get("verb") == "create"
            else record.get("verb")
        )
        if method not in ("eviction", "delete", "deletecollection"):
            continue
        if ref.get("name") not in pods:
            raise RuntimeError("Updater mutated a canary pod without an observed node/UID mapping")
        pod = pods[ref["name"]]
        at = timestamp(record["requestReceivedTimestamp"])
        code = record.get("responseStatus", {}).get("code", 0)
        event = {
            "at": at,
            "node_uid": nodes[pod["node"]],
            "pod_uid": pod["uid"],
            "pdb_uid": pdb_uid,
            "status_code": code,
            "method": method,
        }
        if method == "eviction" and code == 429:
            message = record.get("responseStatus", {}).get("message", "").lower()
            if "disruption budget" in message:
                preceding = [s["pdb"] for s in samples if s["pdb"]["at"] <= at]
                if preceding:
                    blocked.append(
                        {
                            **event,
                            "reason": "DisruptionBudget",
                            "disruptions_allowed": preceding[-1]["disruptions_allowed"],
                        }
                    )
        elif method != "eviction" or 200 <= code < 300:
            evictions.append(event)
    return {
        "uid": pdb_uid,
        "samples": [s["pdb"] for s in samples],
        "blocked_eviction": blocked[0] if blocked else None,
        "evictions": evictions,
    }


def rollouts(samples: list[dict[str, Any]], before: list[dict[str, Any]], pdb: dict[str, Any]) -> list[dict[str, Any]]:
    """Report observed old-node drain intervals without inventing surge-node pairings."""
    result = []
    for node in before:
        attempts = [e["at"] for e in [pdb["blocked_eviction"], *pdb["evictions"]] if e and e["node_uid"] == node["uid"]]
        if not attempts:
            raise RuntimeError("Missing updater drain evidence for an original node")
        began = min(attempts)
        completed = next(
            (
                s["at"]
                for s in samples
                if s["at"] > max(attempts) and not any(p["node"] == node["name"] for p in s["pods"])
            ),
            None,
        )
        if completed is None:
            raise RuntimeError("Protected pods did not leave the drained node")
        result.append({"old_uid": node["uid"], "drain_started_at": began, "drained_at": completed})
    return result


class Experiment:
    """Own one explicit EKS upgrade target and all temporary evidence resources."""

    def __init__(self, check: str) -> None:
        """Validate opt-in and create a new artifact directory before any mutation."""
        self.check = check
        self.cluster = os.environ["EKS_UPGRADE_CLUSTER"]
        self.pool = os.environ["EKS_UPGRADE_NODE_POOL"]
        self.target = os.environ["EKS_UPGRADE_TARGET_VERSION"]
        self.region = os.environ["AWS_REGION"]
        if os.environ.get("EKS_UPGRADE_ALLOW") != self.cluster:
            raise ValueError("EKS_UPGRADE_ALLOW must equal the exact disposable cluster name")
        self.cidrs = [str(ipaddress.ip_network(c.strip())) for c in os.environ["EKS_UPGRADE_SOURCE_CIDRS"].split(",")]
        self.directory = Path(os.environ["EKS_UPGRADE_ARTIFACT_DIR"]) / check
        self.directory.mkdir(parents=True, exist_ok=False)
        session = boto3.Session(region_name=self.region)
        cfg = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2})
        self.eks, self.logs, self.ssm = (session.client(name, config=cfg) for name in ("eks", "logs", "ssm"))
        self.canary: UpgradeCanary | None = None
        self.observer: ApplicationObserver | None = None
        self.pool_observer: PoolObserver | None = None
        self.result: dict[str, Any] = {"success": False, "platform": "kubernetes", "test_name": check}

    def prepare(self) -> client.ApiClient:
        """Connect through an isolated kubeconfig and verify active provider state."""
        cluster = self.eks.describe_cluster(name=self.cluster)["cluster"]
        if cluster["status"] != "ACTIVE":
            raise RuntimeError("Cluster must be ACTIVE before the experiment")
        kubeconfig = self.directory / "kubeconfig"
        subprocess.run(
            [
                "aws",
                "eks",
                "update-kubeconfig",
                "--name",
                self.cluster,
                "--region",
                self.region,
                "--kubeconfig",
                str(kubeconfig),
            ],
            check=True,
            capture_output=True,
            timeout=45,
        )
        kubeconfig.chmod(0o600)
        configuration = client.Configuration()
        config.load_kube_config(config_file=str(kubeconfig), client_configuration=configuration)
        configuration.retries = 0
        api = client.ApiClient(configuration)
        self.canary = UpgradeCanary(
            ["kubectl", "--kubeconfig", str(kubeconfig)],
            node_selector={"eks.amazonaws.com/nodegroup": self.pool},
            source_cidrs=self.cidrs,
        )
        return api

    def version(self) -> str:
        """Read the API server's actual version independently of AWS status."""
        return json.loads(self.canary.command(["get", "--raw=/version"]))["gitVersion"]

    def catalogue(self, minor: str) -> dict[str, Any]:
        """Read a supported release from the regional EKS catalogue."""
        entries = self.eks.describe_cluster_versions(clusterVersions=[minor])["clusterVersions"]
        if len(entries) != 1 or entries[0]["versionStatus"] != "STANDARD_SUPPORT":
            raise RuntimeError("This experiment requires a release in standard support")
        return entries[0]

    def policy(self) -> None:
        """Corroborate documented EKS ownership with this cluster and regional catalogue."""
        cluster = self.eks.describe_cluster(name=self.cluster)["cluster"]
        current = self.catalogue(cluster["version"])
        target = self.catalogue(self.target)
        self.result.update(
            observed_version=self.version(),
            policy={
                "owner": "provider",
                "maintenance_types": ["routine", "security"],
                "managed_control_plane": cluster["status"] == "ACTIVE" and ":eks:" in cluster["arn"],
                "supported": current["versionStatus"] == "STANDARD_SUPPORT",
                "reference": "https://docs.aws.amazon.com/eks/latest/userguide/update-cluster.html",
                "status_reference": "https://docs.aws.amazon.com/eks/latest/APIReference/API_DescribeUpdate.html",
                "configuration_reference": cluster["arn"],
                "supported_transitions": [{"from": self.version(), "to": target["kubernetesPatchVersion"]}],
            },
        )

    def wait_update(self, operation: dict[str, Any], *, node: bool = False) -> None:
        """Poll bounded EKS status while independent observers continue collecting."""
        (self.directory / "operation.json").write_text(json.dumps(operation, indent=2))
        deadline = time.monotonic() + 3600
        released = False
        while time.monotonic() < deadline:
            if self.pool_observer and self.pool_observer.error:
                raise RuntimeError("Pool evidence collection failed") from self.pool_observer.error
            if self.observer and self.observer.error:
                raise RuntimeError("Application evidence persistence failed") from self.observer.error
            args = {"name": self.cluster, "updateId": operation["id"]}
            if node:
                args["nodegroupName"] = self.pool
                if not released:
                    records = audit_events(
                        self.logs, self.cluster, self.canary.namespace, operation["started_at"], time.time()
                    )
                    evidence = drain_evidence(records, list(self.pool_observer.samples), self.result["nodes_before"])
                    if evidence["blocked_eviction"]:
                        self.canary.budget(self.canary.replicas - 1)
                        released = True
            update = self.eks.describe_update(**args)["update"]
            operation["status"] = update["status"].lower()
            (self.directory / "operation.json").write_text(json.dumps(operation, indent=2))
            if update["status"] == "Successful":
                if node and not released:
                    raise RuntimeError("Updater completed without an observed budget-blocked drain")
                operation["status"] = "succeeded"
                return
            if update["status"] in ("Failed", "Cancelled"):
                raise RuntimeError(f"Upgrade {operation['id']} {update['status']}: {update.get('errors', [])}")
            time.sleep(10)
        raise TimeoutError("Upgrade did not complete within one hour")

    def application(self) -> None:
        """Create the canary and establish healthy HTTP baseline before submission."""
        (self.directory / "canary.json").write_text(
            json.dumps(
                {
                    "cluster": self.cluster,
                    "namespace": self.canary.namespace,
                    "owner": self.canary.owner,
                },
                indent=2,
            )
        )
        self.canary.create()
        endpoint = self.canary.wait_ready()
        self.observer = ApplicationObserver(endpoint, self.canary.owner, self.directory / "application.jsonl")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            probe = self.observer.request()
            if probe["status_code"] == 200 and probe["response"] == self.canary.owner:
                self.observer.start()
                return
            time.sleep(2)
        raise TimeoutError("Load-balanced application did not become reachable")

    def control_plane(self) -> None:
        """Perform a real next-minor upgrade with uninterrupted application observation."""
        before = self.version()
        target = self.catalogue(self.target)["kubernetesPatchVersion"]
        before_parts, target_parts = before.lstrip("v").split("."), self.target.split(".")
        if before_parts[0] != target_parts[0] or int(target_parts[1]) != int(before_parts[1]) + 1:
            raise ValueError("Requested control-plane target must be the next minor release")
        self.application()
        start = time.time()
        update = self.eks.update_cluster_version(name=self.cluster, version=self.target)["update"]
        operation = {"id": update["id"], "started_at": start, "status": "running", "executed_by": "provider"}
        self.result.update(
            operation=operation,
            observed_version_before=before,
            target_version=target,
            maintenance_window_required=False,
        )
        self.wait_update(operation)
        self.result["observed_version_after"] = self.version()
        operation["completed_at"] = time.time()
        time.sleep(3)

    def node_pool(self, api: client.ApiClient) -> None:
        """Block the real EKS drain with a PDB, release it, and observe pool convergence."""
        cluster = self.eks.describe_cluster(name=self.cluster)["cluster"]
        group = self.eks.describe_nodegroup(clusterName=self.cluster, nodegroupName=self.pool)["nodegroup"]
        if cluster["version"] != self.target or group["status"] != "ACTIVE":
            raise RuntimeError("Control plane must reach the target and pool must be ACTIVE")
        if group.get("amiType") != "AL2023_x86_64_STANDARD" or group.get("launchTemplate"):
            raise ValueError("Reference experiment supports AL2023 x86 CPU pools without custom launch templates")
        if group.get("updateConfig", {}).get("maxUnavailable") != 1:
            raise ValueError("Pool must be configured for maxUnavailable=1")
        if not any("audit" in item["types"] and item["enabled"] for item in cluster["logging"]["clusterLogging"]):
            raise ValueError("EKS audit logging must be enabled before the experiment")
        recommendation = json.loads(
            self.ssm.get_parameter(
                Name=f"/aws/service/eks/optimized-ami/{self.target}/amazon-linux-2023/x86_64/standard/recommended"
            )["Parameter"]["Value"]
        )
        self.result["target_revision"] = recommendation["image_id"]
        self.application()
        self.pool_observer = PoolObserver(api, self.canary, self.pool, self.directory / "pool.jsonl")
        initial = self.pool_observer.sample()
        before = initial["nodes"]
        if len(before) < 2 or any(
            not n["ready"] or not n["schedulable"] or n["revision"] == recommendation["image_id"] for n in before
        ):
            raise ValueError("Pool must have at least two healthy nodes requiring the target revision")
        if {p["node"] for p in initial["pods"]} != {n["name"] for n in before}:
            raise ValueError("Canary must exercise every selected pool node")
        self.result["nodes_before"] = before
        self.canary.budget(self.canary.replicas)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            budget = self.pool_observer.sample()["pdb"]
            if (
                budget["observed_generation"] == budget["generation"]
                and budget["desired_healthy"] == self.canary.replicas
                and budget["disruptions_allowed"] == 0
            ):
                break
            time.sleep(1)
        else:
            raise TimeoutError("Restrictive disruption budget did not reconcile")
        self.pool_observer.start()
        start = time.time()
        update = self.eks.update_nodegroup_version(
            clusterName=self.cluster,
            nodegroupName=self.pool,
            version=self.target,
            releaseVersion=recommendation["release_version"],
            force=False,
        )["update"]
        operation = {"id": update["id"], "started_at": start, "status": "running", "initiated_by": "tenant"}
        self.result["operation"] = operation
        self.wait_update(operation, node=True)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            latest = self.pool_observer.samples[-1]
            if (
                len(latest["nodes"]) == len(before)
                and all(
                    n["ready"] and n["schedulable"] and n["revision"] == recommendation["image_id"]
                    for n in latest["nodes"]
                )
                and latest["pdb"]["current_healthy"] == self.canary.replicas
            ):
                break
            time.sleep(2)
        else:
            raise TimeoutError("Node pool/canary did not recover after the update")
        self.result["nodes_after"] = latest["nodes"]
        operation["completed_at"] = time.time()
        # Allow CloudWatch delivery to settle; retain every audit page for review.
        time.sleep(60)
        records = audit_events(self.logs, self.cluster, self.canary.namespace, start, operation["completed_at"])
        (self.directory / "audit.json").write_text(json.dumps(records, indent=2))
        samples = list(self.pool_observer.samples)
        pdb = drain_evidence(records, samples, before)
        protected = {p["uid"] for s in samples for p in s["pods"] if p["node"] in {n["name"] for n in before}}
        accepted = {
            e["pod_uid"] for e in pdb["evictions"] if e["method"] == "eviction" and 200 <= e["status_code"] < 300
        }
        pdb.update(
            replicas=self.canary.replicas,
            min_available=self.canary.replicas - 1,
            audit_complete=bool(protected) and protected == accepted,
        )
        self.result.update(pdb=pdb, rollouts=rollouts(samples, before, pdb))

    def run(self) -> dict[str, Any]:
        """Collect real evidence and always attempt cleanup of the owned namespace."""
        api = None
        try:
            api = self.prepare()
            if self.check == "managed_upgrade_policy":
                self.policy()
            elif self.check == "control_plane_upgrade":
                self.control_plane()
            else:
                self.node_pool(api)
            self.result["success"] = True
        except Exception as error:
            self.result.update(success=False, error=str(error), error_type=type(error).__name__)
        finally:
            for observer in (self.pool_observer, self.observer):
                if observer is not None:
                    try:
                        evidence = observer.stop()
                        if observer is self.observer:
                            self.result["application"] = evidence
                    except Exception as error:
                        self.result.update(success=False, error=str(error))
            if self.canary is not None:
                try:
                    self.canary.cleanup()
                except Exception as error:
                    self.result.setdefault("cleanup_errors", []).append(str(error))
                    self.result["success"] = False
            if api is not None:
                api.close()
            (self.directory / "result.json").write_text(json.dumps(self.result, indent=2))
        return self.result


def main() -> int:
    """Emit the provider-neutral evidence envelope, including real failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", required=True, choices=["managed_upgrade_policy", "control_plane_upgrade", "node_pool_upgrade"]
    )
    args = parser.parse_args()

    def interrupted(signum: int, frame: Any) -> None:
        """Route termination through resource cleanup instead of abandoning the canary."""
        raise RuntimeError(f"Upgrade experiment interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        output = Experiment(args.check).run()
    except Exception as error:
        output = {"success": False, "platform": "kubernetes", "test_name": args.check, "error": str(error)}
    print(json.dumps(output))
    return 0 if output["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

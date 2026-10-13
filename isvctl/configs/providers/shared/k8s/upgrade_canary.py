# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provision and clean up a uniquely owned application for upgrade experiments."""

import json
import subprocess
import time
import uuid
from typing import Any

SERVER = """import http.server, os
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = os.environ['CANARY_RESPONSE'].encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""


class UpgradeCanary:
    """Keep resource mutation scoped to one run's namespace and workload."""

    def __init__(
        self,
        kubectl: list[str],
        *,
        node_selector: dict[str, str],
        source_cidrs: list[str],
        replicas: int = 3,
        image: str = "python:3.12-alpine",
    ) -> None:
        """Select canary placement and restrict external access to the controller."""
        if replicas < 2 or not node_selector or not source_cidrs:
            raise ValueError("Canary requires replicas >= 2, a node selector and controller source CIDRs")
        self.kubectl = kubectl
        self.owner = uuid.uuid4().hex
        self.namespace = "isv-upgrade-" + self.owner[:12]
        self.node_selector = node_selector
        self.source_cidrs = source_cidrs
        self.replicas = replicas
        self.image = image
        self.created = False

    def command(self, args: list[str], payload: Any = None, *, timeout: int = 45) -> str:
        """Run bounded kubectl argv without changing the caller's current context."""
        result = subprocess.run(
            [*self.kubectl, *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "kubectl failed")
        return result.stdout

    def get(self, kind: str, name: str = "", *, namespaced: bool = True) -> dict[str, Any]:
        """Return actual Kubernetes observations for the current fixture."""
        args = ["get", kind]
        if name:
            args.append(name)
        if namespaced:
            args += ["--namespace", self.namespace]
        return json.loads(self.command([*args, "-o", "json"]))

    def create(self) -> None:
        """Create the replicated canary, budget and externally reachable Service."""
        # Register cleanup before a request that could succeed but lose its response.
        self.created = True
        self.command(
            ["create", "-f", "-"],
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": self.namespace, "labels": {"isv-upgrade-owner": self.owner}},
            },
        )
        labels = {"app": "upgrade-canary", "isv-upgrade-owner": self.owner}
        metadata = {"name": "canary", "namespace": self.namespace}
        container = {
            "name": "canary",
            "image": self.image,
            "command": ["python", "-c", SERVER],
            "env": [{"name": "CANARY_RESPONSE", "value": self.owner}],
            "ports": [{"containerPort": 8080}],
            "readinessProbe": {"httpGet": {"path": "/", "port": 8080}, "periodSeconds": 1, "failureThreshold": 1},
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}},
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
        }
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata,
            "spec": {
                "replicas": self.replicas,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "nodeSelector": self.node_selector,
                        "topologySpreadConstraints": [
                            {
                                "maxSkew": 1,
                                "topologyKey": "kubernetes.io/hostname",
                                "whenUnsatisfiable": "DoNotSchedule",
                                "labelSelector": {"matchLabels": labels},
                            }
                        ],
                        "containers": [container],
                    },
                },
            },
        }
        pdb = {
            "apiVersion": "policy/v1",
            "kind": "PodDisruptionBudget",
            "metadata": metadata,
            "spec": {"minAvailable": self.replicas - 1, "selector": {"matchLabels": labels}},
        }
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata,
            "spec": {
                "type": "LoadBalancer",
                "selector": labels,
                "loadBalancerSourceRanges": self.source_cidrs,
                "ports": [{"port": 80, "targetPort": 8080, "protocol": "TCP"}],
            },
        }
        for resource in (deployment, pdb, service):
            self.command(["create", "-f", "-"], resource)

    def wait_ready(self, timeout: int = 900) -> str:
        """Wait for canary readiness and the assigned Service endpoint."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            deployment = self.get("deployment", "canary")
            service = self.get("service", "canary")
            status = deployment.get("status", {})
            ingress = service.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            if (
                status.get("observedGeneration") == deployment["metadata"]["generation"]
                and status.get("readyReplicas") == self.replicas
                and ingress
            ):
                address = ingress[0].get("hostname") or ingress[0].get("ip")
                if address:
                    return "http://" + (f"[{address}]" if ":" in address else address) + "/"
            time.sleep(2)
        raise TimeoutError("Canary replicas or Service endpoint did not become ready")

    def budget(self, minimum: int) -> None:
        """Change only this run's disruption budget, for the blocked-drain experiment."""
        if not 1 <= minimum <= self.replicas:
            raise ValueError("Disruption budget must preserve at least one canary replica")
        self.command(
            [
                "patch",
                "pdb",
                "canary",
                "-n",
                self.namespace,
                "--type=merge",
                "-p",
                json.dumps({"spec": {"minAvailable": minimum}}),
            ]
        )

    def pdb_sample(self) -> dict[str, Any]:
        """Preserve real controller status, including stale generations or violations."""
        at = time.time()
        pdb = self.get("pdb", "canary")
        status, metadata = pdb.get("status", {}), pdb["metadata"]
        return {
            "at": at,
            "uid": metadata["uid"],
            "generation": metadata["generation"],
            "observed_generation": status.get("observedGeneration", 0),
            "expected_pods": status.get("expectedPods", 0),
            "desired_healthy": status.get("desiredHealthy", 0),
            "current_healthy": status.get("currentHealthy", 0),
            "disruptions_allowed": status.get("disruptionsAllowed", 0),
        }

    def cleanup(self) -> None:
        """Remove only the namespace bearing this run's ownership token."""
        if not self.created:
            return
        raw = self.command(["get", "namespace", self.namespace, "--ignore-not-found", "-o", "json"])
        if not raw.strip():
            self.created = False
            return
        namespace = json.loads(raw)
        if namespace["metadata"].get("labels", {}).get("isv-upgrade-owner") != self.owner:
            raise RuntimeError("Canary namespace ownership changed; refusing cleanup")
        self.command(["delete", "namespace", self.namespace, "--wait=true", "--timeout=600s"], timeout=615)
        self.created = False

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

"""Install or remove an explicitly configured, disposable CSI driver.

CSI_INSTALL_CONFIG points to JSON with method (helm/kustomize), namespace,
source (pinned local chart/kustomization), drivers (CSIDriver names), workloads
(kind/name strings), and optional values (local Helm values file), release and
timeout_s. CSI_INSTALL_STATE is a new local state file retained until teardown.
KUBECTL and HELM accept shell-quoted command prefixes; commands never use a shell.

Only fresh resources are accepted. Existing drivers, namespaces and chart objects
are never adopted. Teardown checks UIDs and refuses to remove CRDs with objects
outside the owned namespace, or a CSI driver with remaining PVs. Review the supplied chart/manifests before running.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import yaml


class CsiInstaller:
    """Own one fresh installation and persist the exact resources needed for teardown."""

    def __init__(self, state_path: Path) -> None:
        """Initialize CLI prefixes without changing the cluster."""
        self.state_path = state_path
        self.kubectl = shlex.split(os.environ.get("KUBECTL", "kubectl"))
        self.helm = shlex.split(os.environ.get("HELM", "helm"))
        self.timeout = 300
        self.state: dict[str, Any] = {}

    def command(self, args: list[str], data: str | None = None) -> str:
        """Run a bounded command, keeping rendered manifests out of diagnostics."""
        result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=self.timeout + 30)
        if result.returncode:
            raise RuntimeError(f"{Path(args[0]).name} command failed: {result.stderr.strip()[:300]}")
        return result.stdout

    def remember(self, obj: dict[str, Any]) -> None:
        """Record only identity and UID, never a resource's potentially secret contents."""
        resource = self.identity(obj)
        resource["metadata"]["uid"] = obj["metadata"]["uid"]
        if resource not in self.state["resources"]:
            self.state["resources"].append(resource)
        self.save()

    def save(self) -> None:
        """Persist recovery state privately before advancing the lifecycle."""
        with open(self.state_path, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write(json.dumps(self.state, indent=2) + "\n")

    def get(self, resource: dict[str, Any]) -> dict[str, Any] | None:
        """Look up an exact resource; absence is distinct from an API failure."""
        text = self.command(
            self.kubectl + ["get", "-f", "-", "--ignore-not-found=true", "-o", "json"], yaml.safe_dump(resource)
        )
        if not text.strip():
            return None
        payload = json.loads(text)
        if payload.get("kind") == "List":
            items = payload.get("items", [])
            if len(items) > 1:
                raise ValueError("Expected one installation resource")
            return items[0] if items else None
        return payload

    @staticmethod
    def identity(doc: dict[str, Any], namespace: str = "") -> dict[str, Any]:
        """Retain only the API identity; no credentials or workload specifications."""
        meta = doc["metadata"]
        result = {"apiVersion": doc["apiVersion"], "kind": doc["kind"], "metadata": {"name": meta["name"]}}
        if meta.get("namespace") or namespace:
            result["metadata"]["namespace"] = meta.get("namespace", namespace)
        return result

    def install(self, config: dict[str, Any]) -> dict[str, Any]:
        """Render, preflight, install and verify registration plus workload readiness."""
        method = config.get("method")
        if method not in {"helm", "kustomize"}:
            raise ValueError("method must be helm or kustomize")
        for key in ("namespace", "source", "drivers", "workloads"):
            if not config.get(key):
                raise ValueError(f"Missing CSI installation setting: {key}")
        if not isinstance(config["drivers"], list) or not isinstance(config["workloads"], list):
            raise ValueError("drivers and workloads must be nonempty lists")
        self.timeout = int(config.get("timeout_s", 300))
        namespace = config["namespace"]
        release = config.get("release", "isvtest-csi")
        ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}
        if self.get(ns):
            raise ValueError("Installation namespace already exists; use a fresh namespace")
        values = ["-f", config["values"]] if config.get("values") else []
        if method == "helm":
            rendered = self.command(
                self.helm + ["template", release, config["source"], "-n", namespace, "--include-crds", *values]
            )
        else:
            rendered = self.command(self.kubectl + ["kustomize", config["source"]])
        docs = [doc for doc in yaml.safe_load_all(rendered) if doc]
        if not docs:
            raise ValueError("Installation rendered no resources")
        # Resolve scope using discovery, plus CRDs introduced by this installation.
        discovery = self.command(self.kubectl + ["api-resources", "--no-headers"])
        scopes = {}
        for line in discovery.splitlines():
            fields = line.split()
            for index, field in enumerate(fields):
                if field in {"true", "false"} and index + 1 < len(fields):
                    scopes[fields[index + 1]] = field == "true"
        new_kinds = {d["spec"]["names"]["kind"] for d in docs if d["kind"] == "CustomResourceDefinition"}
        for doc in docs:
            if doc["kind"] == "CustomResourceDefinition":
                scopes[doc["spec"]["names"]["kind"]] = doc["spec"]["scope"] == "Namespaced"
        resources = []
        for doc in docs:
            annotations = doc.get("metadata", {}).get("annotations", {})
            if "helm.sh/hook" in annotations:
                raise ValueError("Use a CSI chart without Helm hooks for a disposable installation")
            kind = doc["kind"]
            if kind == "StorageClass" and any(
                str(annotations.get(key, "")).lower() == "true"
                for key in (
                    "storageclass.kubernetes.io/is-default-class",
                    "storageclass.beta.kubernetes.io/is-default-class",
                )
            ):
                raise ValueError("Disposable CSI installation must not create a default StorageClass")
            if kind not in scopes:
                raise ValueError(f"Unknown API kind in installation: {kind}")
            if kind == "Namespace":
                if doc["metadata"]["name"] != namespace:
                    raise ValueError("Manifests must use only the fresh installation namespace")
                ns = doc
                continue
            resource = self.identity(doc, namespace if scopes[kind] else "")
            if scopes[kind] and resource["metadata"]["namespace"] != namespace:
                raise ValueError("Manifests must use only the fresh installation namespace")
            if kind not in new_kinds and self.get(resource):
                raise ValueError(f"Refusing to adopt existing {kind}/{resource['metadata']['name']}")
            resources.append(resource)
        rendered_drivers = {r["metadata"]["name"] for r in resources if r["kind"] == "CSIDriver"}
        if not set(config["drivers"]).issubset(rendered_drivers):
            raise ValueError("Every expected CSIDriver must be part of the fresh installation")
        # Exclusive state creation prevents an accidental second installation from losing ownership.
        with open(self.state_path, "x", opener=lambda path, flags: os.open(path, flags, 0o600)):
            pass
        self.state = {
            "method": method,
            "namespace": namespace,
            "release": release,
            "drivers": config["drivers"],
            "resources": [],
            "pending": [ns, *resources],
            "owner": uuid.uuid4().hex,
            "kubectl": self.kubectl,
            "helm": self.helm,
            "timeout_s": self.timeout,
        }
        self.save()
        ns["metadata"].setdefault("labels", {})["isvtest-csi-install"] = self.state["owner"]
        if method == "helm":
            ns["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "Helm"
            ns["metadata"].setdefault("annotations", {}).update(
                {
                    "meta.helm.sh/release-name": release,
                    "meta.helm.sh/release-namespace": namespace,
                }
            )
        try:
            self.command(self.kubectl + ["create", "-f", "-"], yaml.safe_dump(ns))
            self.remember(self.get(ns))
            if method == "helm":
                # Helm does not own files from a chart's crds/ directory. Create those
                # prerequisites explicitly so teardown can verify their exact UIDs.
                raw_crds = self.command(self.helm + ["show", "crds", config["source"]])
                for crd in yaml.safe_load_all(raw_crds):
                    if not crd:
                        continue
                    crd["metadata"].setdefault("labels", {})["isvtest-csi-install"] = self.state["owner"]
                    self.command(self.kubectl + ["create", "-f", "-"], yaml.safe_dump(crd))
                    self.remember(self.get(self.identity(crd)))
                    self.command(
                        self.kubectl
                        + [
                            "wait",
                            f"crd/{crd['metadata']['name']}",
                            "--for=condition=Established",
                            f"--timeout={self.timeout}s",
                        ]
                    )
                self.state["helm_started"] = True
                self.save()
                self.command(
                    self.helm
                    + [
                        "install",
                        release,
                        config["source"],
                        "-n",
                        namespace,
                        "--wait",
                        "--skip-crds",
                        f"--timeout={self.timeout}s",
                        *values,
                    ]
                )
            else:
                # create fails on conflicts instead of modifying existing resources.
                for doc in docs:
                    if doc["kind"] == "Namespace":
                        continue
                    if scopes[doc["kind"]]:
                        doc["metadata"]["namespace"] = namespace
                    doc["metadata"].setdefault("labels", {})["isvtest-csi-install"] = self.state["owner"]
                    self.command(self.kubectl + ["create", "-f", "-"], yaml.safe_dump(doc))
                    resource = self.get(self.identity(doc))
                    self.remember(resource)
                    if doc["kind"] == "CustomResourceDefinition":
                        self.command(
                            self.kubectl
                            + [
                                "wait",
                                f"crd/{doc['metadata']['name']}",
                                "--for=condition=Established",
                                f"--timeout={self.timeout}s",
                            ]
                        )
            for workload in config["workloads"]:
                kind, _name = workload.split("/", 1)
                if kind not in {"deployment", "daemonset", "statefulset"}:
                    raise ValueError("workloads must be deployment/name, daemonset/name or statefulset/name")
                self.command(
                    self.kubectl + ["rollout", "status", workload, "-n", namespace, f"--timeout={self.timeout}s"]
                )
                obj = json.loads(self.command(self.kubectl + ["get", workload, "-n", namespace, "-o", "json"]))
                status = obj.get("status", {})
                desired = (
                    status.get("desiredNumberScheduled", 0) if kind == "daemonset" else obj["spec"].get("replicas", 1)
                )
                ready = status.get("numberReady", 0) if kind == "daemonset" else status.get("readyReplicas", 0)
                if desired < 1 or ready < desired:
                    raise RuntimeError("CSI workload has no ready replicas")
            for driver in config["drivers"]:
                self.command(self.kubectl + ["get", "csidriver", driver, "-o", "json"])
            return {
                "success": True,
                "platform": "kubernetes",
                "method": method,
                "tests": {
                    "installed": {"passed": True},
                    "drivers_registered": {"passed": True},
                    "workloads_ready": {"passed": True},
                },
            }
        finally:
            self.recover()

    def recover(self) -> None:
        """Recover identities from a partially completed install using its ownership markers."""
        known = {json.dumps(self.identity(obj), sort_keys=True) for obj in self.state["resources"]}
        for resource in self.state["pending"]:
            if json.dumps(self.identity(resource), sort_keys=True) in known:
                continue
            try:
                obj = self.get(resource)
                if not obj:
                    continue
                meta = obj["metadata"]
                annotations = meta.get("annotations", {})
                helm_owned = (
                    annotations.get("meta.helm.sh/release-name") == self.state["release"]
                    and annotations.get("meta.helm.sh/release-namespace") == self.state["namespace"]
                )
                if (self.state["method"] == "helm" and helm_owned) or meta.get("labels", {}).get(
                    "isvtest-csi-install"
                ) == self.state["owner"]:
                    self.remember(obj)
            except RuntimeError:
                # Keep pending identities when an API is unavailable or its CRD was never created.
                continue
        self.save()

    def remove(self) -> None:
        """Remove owned objects, refusing changed identities or active storage consumers."""
        if not self.state_path.exists():
            return  # Setup failed its preflight; nothing was installed.
        self.state = json.loads(self.state_path.read_text())
        if self.kubectl != self.state["kubectl"] or self.helm != self.state["helm"]:
            raise ValueError("Teardown must use the original KUBECTL and HELM command prefixes")
        self.timeout = self.state["timeout_s"]
        self.recover()
        deadline = time.monotonic() + self.timeout
        while True:
            pvs = json.loads(self.command(self.kubectl + ["get", "pv", "-o", "json"]))
            if not any(pv.get("spec", {}).get("csi", {}).get("driver") in self.state["drivers"] for pv in pvs["items"]):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("CSI installation still has PVs; remove its probe PVCs and wait for deletion first")
            time.sleep(min(2, remaining))
        owned_uids = {obj["metadata"]["uid"] for obj in self.state["resources"]}
        for obj in self.state["resources"]:
            current = self.get(self.identity(obj))
            if current and current["metadata"]["uid"] != obj["metadata"]["uid"]:
                raise RuntimeError("An installation resource was replaced; refusing teardown")
            if current and obj["kind"] == "CustomResourceDefinition":
                instances = json.loads(
                    self.command(self.kubectl + ["get", obj["metadata"]["name"], "-A", "-o", "json"])
                )
                if any(
                    item.get("metadata", {}).get("namespace") != self.state["namespace"]
                    and item.get("metadata", {}).get("uid") not in owned_uids
                    for item in instances["items"]
                ):
                    raise RuntimeError(
                        "Installation CRD still contains objects outside its namespace; refusing teardown"
                    )
        if self.state.get("helm_started"):
            releases = json.loads(
                self.command(self.helm + ["list", "-n", self.state["namespace"], "--all", "-o", "json"])
            )
            if any(r["name"] == self.state["release"] for r in releases):
                self.command(
                    self.helm
                    + [
                        "uninstall",
                        self.state["release"],
                        "-n",
                        self.state["namespace"],
                        "--wait",
                        f"--timeout={self.timeout}s",
                    ]
                )
        # Remove the owned namespace before its CRDs so namespaced controller inventory
        # is cleaned normally. Cluster-scoped or foreign CR instances were rejected above.
        resources = sorted(
            reversed(self.state["resources"]),
            key=lambda obj: 2 if obj["kind"] == "CustomResourceDefinition" else 1 if obj["kind"] == "Namespace" else 0,
        )
        for obj in resources:
            self.command(
                self.kubectl
                + ["delete", "-f", "-", "--ignore-not-found=true", "--wait=true", f"--timeout={self.timeout}s"],
                yaml.safe_dump(self.identity(obj)),
            )
            self.state["resources"].remove(obj)
            self.state["pending"] = [
                item for item in self.state["pending"] if self.identity(item) != self.identity(obj)
            ]
            self.save()
        self.state_path.unlink()


def main() -> int:
    """Emit provider-neutral evidence and leave recovery state after any failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "remove"])
    args = parser.parse_args()
    result: dict[str, Any] = {"success": False, "platform": "kubernetes"}
    try:
        installer = CsiInstaller(Path(os.environ["CSI_INSTALL_STATE"]))
        if args.action == "install":
            config = json.loads(Path(os.environ["CSI_INSTALL_CONFIG"]).read_text())
            result = installer.install(config)
        else:
            installer.remove()
            result = {"success": True, "platform": "kubernetes"}
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
    print(json.dumps(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

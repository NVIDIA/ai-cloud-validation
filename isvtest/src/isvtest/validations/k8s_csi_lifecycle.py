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

from __future__ import annotations

import json
import os
import shlex
import uuid
from typing import Any, ClassVar

import pytest
import yaml

from isvtest.core.k8s import get_kubectl_base_shell, get_kubectl_command
from isvtest.core.validation import BaseValidation
from isvtest.validations.k8s_storage import _apply_manifest, _apply_mount_pod_manifest


class K8sCsiSnapshotRestoreCheck(BaseValidation):
    """Prove that a CSI snapshot restores point-in-time data into a distinct volume.

    Requires storage_class (K8S_CSI_BLOCK_SC) and snapshot_class
    (K8S_CSI_SNAPSHOT_CLASS). Both classes must use Delete policies. The snapshot
    API and controller must already be installed. Missing class configuration skips the test.
    pvc_size defaults to 1Gi, access_mode to ReadWriteOnce, and wait_timeout_s to 180.
    Only the probe's new namespace and its dynamically provisioned objects are removed.
    """

    description: ClassVar[str] = "Verify CSI snapshot readiness and point-in-time data restoration."
    timeout: ClassVar[int] = 240

    def _command(self, *args: str) -> str:
        """Run kubectl, rejecting API failures instead of treating empty output as evidence."""
        result = self.run_command(f"{self._base} {shlex.join(args)}")
        if result.exit_code:
            raise RuntimeError(f"kubectl {args[0]} failed: {result.stderr.strip()[:200]}")
        return result.stdout

    def _get(self, resource: str, name: str, *, namespaced: bool = False) -> dict[str, Any]:
        """Read one API object, requiring a JSON object response."""
        ns_args = ["-n", self._namespace] if namespaced else []
        payload = json.loads(self._command("get", resource, name, *ns_args, "-o", "json"))
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid {resource} response")
        return payload

    def _create(self, kind: str, name: str, spec: dict[str, Any]) -> None:
        """Create an object within the newly owned probe namespace."""
        doc = {
            "apiVersion": "snapshot.storage.k8s.io/v1" if kind == "VolumeSnapshot" else "v1",
            "kind": kind,
            "metadata": {"name": name, "namespace": self._namespace},
            "spec": spec,
        }
        rc, err = _apply_manifest(self._parts, yaml.safe_dump(doc), self.timeout)
        if rc:
            raise RuntimeError(f"Could not create {kind}: {err.strip()[:200]}")

    def _mount(self, name: str, wait: str) -> str:
        """Mount a PVC, wait for readiness and verify a bound CSI volume."""
        rc, err = _apply_mount_pod_manifest(self._parts, self._namespace, name, name, self.timeout)
        if rc:
            raise RuntimeError(f"Could not create consumer: {err.strip()[:200]}")
        self._command("wait", "-n", self._namespace, f"pod/{name}", "--for=condition=Ready", wait)
        pvc = self._get("pvc", name, namespaced=True)
        pv_name = pvc.get("spec", {}).get("volumeName")
        if pvc.get("status", {}).get("phase") != "Bound" or not pv_name:
            raise RuntimeError("Consumer has no bound volume")
        pv = self._get("pv", pv_name)
        csi = pv.get("spec", {}).get("csi", {})
        if csi.get("driver") != self._driver or not csi.get("volumeHandle"):
            raise RuntimeError("Bound volume is not provided by the configured CSI driver")
        return csi["volumeHandle"]

    def _exec(self, pod: str, *args: str) -> str:
        """Run the data probe inside a mounted consumer."""
        return self._command("exec", "-n", self._namespace, pod, "--", *args).strip()

    def run(self) -> None:
        """Snapshot seeded data, change the source, restore and compare both volumes."""
        sc = str(self.config.get("storage_class") or os.environ.get("K8S_CSI_BLOCK_SC", ""))
        snap_class = str(self.config.get("snapshot_class") or os.environ.get("K8S_CSI_SNAPSHOT_CLASS", ""))
        if not sc or not snap_class:
            pytest.skip("CSI snapshot validation requires storage_class and snapshot_class")
        wait_s = self._parse_positive_int("wait_timeout_s", default=180)
        if wait_s is None:
            return
        wait = f"--timeout={wait_s}s"
        self._base = get_kubectl_base_shell()
        self._parts = get_kubectl_command()
        self._namespace = f"isvtest-csi-snapshot-{uuid.uuid4().hex[:8]}"
        created = False
        snapshot_created = False
        content_name = ""
        try:
            storage = self._get("storageclass", sc)
            snapshot = self._get("volumesnapshotclass", snap_class)
            self._driver = storage.get("provisioner", "")
            if not self._driver or snapshot.get("driver") != self._driver:
                raise RuntimeError("StorageClass and VolumeSnapshotClass must use the same CSI driver")
            if storage.get("reclaimPolicy", "Delete") != "Delete" or snapshot.get("deletionPolicy") != "Delete":
                raise RuntimeError("Snapshot probe requires Delete reclaim and snapshot deletion policies")
            self._command("create", "namespace", self._namespace)
            created = True
            pvc_spec = {
                "storageClassName": sc,
                "accessModes": [self.config.get("access_mode", "ReadWriteOnce")],
                "resources": {"requests": {"storage": self.config.get("pvc_size", "1Gi")}},
            }
            self._create("PersistentVolumeClaim", "source", pvc_spec)
            source_handle = self._mount("source", wait)
            canary = uuid.uuid4().hex
            self._exec("source", "sh", "-c", f"echo {canary} > /data/snapshot-canary && sync")
            self._create(
                "VolumeSnapshot",
                "snapshot",
                {
                    "volumeSnapshotClassName": snap_class,
                    "source": {"persistentVolumeClaimName": "source"},
                },
            )
            snapshot_created = True
            self._command(
                "wait",
                "-n",
                self._namespace,
                "volumesnapshot/snapshot",
                "--for=jsonpath={.status.readyToUse}=true",
                wait,
            )
            snapshot = self._get("volumesnapshot", "snapshot", namespaced=True)
            status = snapshot.get("status", {})
            content_name = status.get("boundVolumeSnapshotContentName", "")
            if status.get("readyToUse") is not True or not content_name or status.get("error"):
                raise RuntimeError("Snapshot has no ready bound content")
            content = self._get("volumesnapshotcontent", content_name)
            content_spec = content.get("spec", {})
            content_status = content.get("status", {})
            if (
                content_spec.get("driver") != self._driver
                or content_spec.get("volumeSnapshotRef", {}).get("uid") != snapshot["metadata"]["uid"]
                or content_status.get("readyToUse") is not True
                or not content_status.get("snapshotHandle")
            ):
                raise RuntimeError("Snapshot content does not prove a ready CSI snapshot of this claim")
            self.report_subtest("snapshot-ready", passed=True, message="CSI snapshot and bound content are ready")
            self._exec("source", "sh", "-c", "echo changed-after-snapshot > /data/snapshot-canary && sync")
            self._create(
                "PersistentVolumeClaim",
                "restored",
                {
                    **pvc_spec,
                    "dataSource": {"apiGroup": "snapshot.storage.k8s.io", "kind": "VolumeSnapshot", "name": "snapshot"},
                },
            )
            restored_handle = self._mount("restored", wait)
            if restored_handle == source_handle:
                raise RuntimeError("Restore reused the source CSI volume")
            if self._exec("restored", "cat", "/data/snapshot-canary") != canary:
                raise RuntimeError("Restored data does not match the snapshot canary")
            if self._exec("source", "cat", "/data/snapshot-canary") != "changed-after-snapshot":
                raise RuntimeError("Source mutation was not preserved independently of the restore")
            self.report_subtest("snapshot-data-restored", passed=True, message="Distinct volume restored original data")
            self.set_passed("CSI snapshot restored point-in-time data; source remains independently modified")
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            self.set_failed(str(exc))
        finally:
            if created:
                cleanup_wait = f"--timeout={max(wait_s, 120)}s"
                try:
                    # A pending restore PVC protects its snapshot from deletion. Remove that
                    # consumer first, then let snapshot-controller release the backend snapshot.
                    for resource in ("pod", "pvc"):
                        self._command(
                            "delete",
                            resource,
                            "restored",
                            "-n",
                            self._namespace,
                            "--ignore-not-found=true",
                            "--wait=true",
                            cleanup_wait,
                        )
                    if snapshot_created:
                        self._command(
                            "delete",
                            "volumesnapshot",
                            "snapshot",
                            "-n",
                            self._namespace,
                            "--ignore-not-found=true",
                            "--wait=true",
                            cleanup_wait,
                        )
                    if content_name:
                        self._command("wait", f"volumesnapshotcontent/{content_name}", "--for=delete", cleanup_wait)
                except (RuntimeError, ValueError) as exc:
                    self.set_failed(f"{self._error} Snapshot cleanup failed: {exc}".strip())
                finally:
                    try:
                        self._command(
                            "delete",
                            "namespace",
                            self._namespace,
                            "--ignore-not-found=true",
                            "--wait=true",
                            cleanup_wait,
                        )
                    except RuntimeError as exc:
                        self.set_failed(f"{self._error} Namespace cleanup failed: {exc}".strip())


class K8sCsiHelmInstallCheck(BaseValidation):
    """Require successful installation, registration and readiness from the install step.

    K8S23 allows either Helm or Kustomize. Only the unused alternative skips,
    after successful evidence for the selected method has been verified.
    """

    description: ClassVar[str] = "Verify CSI installation through Helm."
    method: ClassVar[str] = "helm"

    def run(self) -> None:
        """Reject absent or incomplete evidence, including a tool-version-only result."""
        evidence = self.config.get("installation", self.config.get("step_output", {}))
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except ValueError:
                self.set_failed("CSI installation evidence must be a JSON object")
                return
        if not isinstance(evidence, dict) or evidence.get("success") is not True:
            self.set_failed("CSI installation evidence is missing or failed; run the CSI installation setup step")
            return
        tests = evidence.get("tests", {})
        if not isinstance(tests, dict) or any(
            not isinstance(tests.get(key), dict) or tests[key].get("passed") is not True
            for key in ("installed", "drivers_registered", "workloads_ready")
        ):
            self.set_failed("CSI installation requires install, driver registration and workload readiness evidence")
            return
        method = evidence.get("method")
        if method not in {"helm", "kustomize"}:
            self.set_failed("CSI installation must use Helm or Kustomize")
        elif method != self.method:
            pytest.skip(f"CSI installed successfully with {method}; K8S23 permits either installation method")
        else:
            self.set_passed(f"CSI installed with {method}; driver registration and workload readiness verified")


class K8sCsiKustomizeInstallCheck(K8sCsiHelmInstallCheck):
    """Verify the Kustomize alternative of the K8S23 installation requirement."""

    description: ClassVar[str] = "Verify CSI installation through Kustomize."
    method: ClassVar[str] = "kustomize"

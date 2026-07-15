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

"""Verify customer-visible TLS certificates rotate within policy (SEC09-01).

The AWS reference scopes this to managed Kubernetes control-plane certificates,
which are provider-managed and not exposed through any customer inventory, so it
always exits via a structured skip. On GCP the GKE control-plane certificate is
likewise Google-managed and not queryable, but GCP DOES expose customer-managed
TLS certificates through Certificate Manager
(``google.cloud.certificate_manager_v1.CertificateManagerClient.list_certificates``),
whose managed certificates carry an ``expire_time`` and are auto-renewed before
expiry.

This script is read-only (it creates nothing, so there is no teardown). It walks
Certificate Manager for customer-visible certificates and evaluates the validity
window. When the only certificates are the provider-hidden GKE control-plane CA,
or none exist at all, it emits a structured skip mirroring the AWS
provider-hidden path. Google Certificate Authority Service (private CA) is
intentionally out of scope here -- this check covers Certificate Manager only.

Usage:
    python3 cert_rotation_test.py --region us-central1 --project my-project

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "cert_rotation_test",
    "rotation_window_days": 60,
    "certs_inspected": 2,
    "auto_rotated": 1,
    "short_validity": 1,
    "out_of_policy": 0,
    "tests": {
        "cert_inventory_non_empty": {"passed": true},
        "no_certs_out_of_policy": {"passed": true},
        "rotation_evidence_present": {"passed": true}
    }
}
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # providers/gcp/scripts/

from common.compute import resolve_project
from common.errors import handle_gcp_errors
from cryptography import x509
from google.api_core import exceptions as gax
from google.cloud import certificate_manager_v1

# Policy threshold: a managed certificate must rotate (auto-renew) within this
# many days. Certificate Manager renews managed certificates well before
# expiry, so a live managed certificate is evidence of in-policy rotation.
ROTATION_WINDOW_DAYS = 60

# Service-disabled wire markers. Certificate Manager may be a disabled API on a
# project that never enabled it; GCP surfaces that as a 403 whose message
# carries one of these markers. That disabled-API case is provider-hidden parity
# with the AWS managed-control-plane path, not a hard failure. A *generic*
# access-denied (the caller simply lacks certificatemanager.certificates.list)
# is deliberately NOT in this set: it is an inspection failure that must surface,
# never a clean skip.
_SERVICE_DISABLED_MARKERS = ("service_disabled", "has not been used", "is disabled")


def _is_service_unavailable(e: Exception) -> bool:
    """Return True iff ``e`` signals the Certificate Manager API is DISABLED on this project.

    Only a SERVICE_DISABLED condition (the API was never enabled) is
    provider-hidden parity with the AWS managed-control-plane skip path. GCP
    reports it as a 403 ``PermissionDenied`` whose message carries one of the
    service-disabled markers, so classification is by message, not exception
    type. A generic ``PermissionDenied`` / Forbidden / list failure (the caller
    lacks the list permission) is an inspection error that MUST hard-fail with
    evidence -- reporting it as a clean skip would hide that the
    customer-visible certificate inventory was never evaluated.
    """
    msg = str(e).lower()
    return any(marker in msg for marker in _SERVICE_DISABLED_MARKERS)


@dataclass(frozen=True)
class CertificateEvidence:
    """One certificate's rotation category and evidence verdict."""

    category: str
    compliant: bool
    reason: str


def _aware(value: datetime) -> datetime:
    """Normalize a provider datetime to UTC-aware form."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _certificate_type(cert: Any) -> str | None:
    """Return the Certificate Manager proto oneof selection."""
    try:
        return certificate_manager_v1.Certificate.pb(cert).WhichOneof("type")
    except (AttributeError, TypeError, ValueError):
        return None


def _evaluate_certificate(cert: Any, now: datetime) -> CertificateEvidence:
    """Evaluate provider-managed renewal or self-managed short-validity evidence."""
    cert_type = _certificate_type(cert)
    if cert_type == "managed":
        active = cert.managed.state == certificate_manager_v1.Certificate.ManagedCertificate.State.ACTIVE
        expire_time = getattr(cert, "expire_time", None)
        if not active:
            return CertificateEvidence("out_of_policy", False, "managed certificate is not ACTIVE")
        if not expire_time:
            return CertificateEvidence("out_of_policy", False, "ACTIVE managed certificate has no expiry")
        if _aware(expire_time) <= now:
            return CertificateEvidence("out_of_policy", False, "managed certificate is expired")
        return CertificateEvidence("auto_rotated", True, "ACTIVE provider-managed certificate")

    if cert_type == "self_managed":
        pem = str(getattr(cert, "pem_certificate", "") or "").strip()
        if not pem:
            return CertificateEvidence("out_of_policy", False, "self-managed certificate has no PEM readback")
        try:
            chain = x509.load_pem_x509_certificates(pem.encode())
        except ValueError:
            return CertificateEvidence("out_of_policy", False, "self-managed certificate PEM is malformed")
        if not chain:
            return CertificateEvidence("out_of_policy", False, "self-managed certificate PEM is empty")
        leaf = chain[0]
        valid_from = leaf.not_valid_before_utc
        valid_until = leaf.not_valid_after_utc
        if not valid_from <= now < valid_until:
            return CertificateEvidence("out_of_policy", False, "self-managed certificate is not currently valid")
        if valid_until - valid_from > timedelta(days=ROTATION_WINDOW_DAYS):
            return CertificateEvidence(
                "out_of_policy",
                False,
                f"self-managed certificate validity exceeds {ROTATION_WINDOW_DAYS} days",
            )
        return CertificateEvidence("short_validity", True, "self-managed certificate validity is within policy")

    return CertificateEvidence("out_of_policy", False, "certificate type is missing or indeterminate")


def _list_certificates(client: Any, parent: str) -> list[Any]:
    """Return a complete certificate inventory or propagate a partial read.

    A location-level NotFound before the pager yields anything is a genuine
    empty inventory. Once any certificate has been observed, a later NotFound
    is an incomplete inventory and must fail rather than erase that evidence.
    """
    certificates: list[Any] = []
    try:
        for certificate in client.list_certificates(parent=parent):
            certificates.append(certificate)
    except gax.NotFound:
        if certificates:
            raise
    return certificates


@handle_gcp_errors
def main() -> int:
    """Evaluate Certificate Manager certificate rotation and emit JSON result."""
    parser = argparse.ArgumentParser(description="Certificate rotation cycle test (SEC09-01)")
    parser.add_argument("--region", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "cert_rotation_test",
        "rotation_window_days": ROTATION_WINDOW_DAYS,
        "certs_inspected": 0,
        "auto_rotated": 0,
        "short_validity": 0,
        "out_of_policy": 0,
        "tests": {
            "cert_inventory_non_empty": {"passed": False},
            "no_certs_out_of_policy": {"passed": False},
            "rotation_evidence_present": {"passed": False},
        },
    }

    try:
        project = resolve_project(args.project)
        client = certificate_manager_v1.CertificateManagerClient()
        # Certificate Manager certificates live under the global location; the
        # managed-certificate inventory is not regional.
        parent = f"projects/{project}/locations/global"

        certs_inspected = 0
        counts = {"auto_rotated": 0, "short_validity": 0, "out_of_policy": 0}
        failures: list[str] = []
        now = datetime.now(UTC)
        for cert in _list_certificates(client, parent):
            certs_inspected += 1
            # List responses may omit the output-only PEM. Fetch the full
            # self-managed resource before declaring its evidence absent. A
            # listed certificate that disappears during readback is retained
            # as out-of-policy evidence, never collapsed into empty inventory.
            if _certificate_type(cert) == "self_managed" and not getattr(cert, "pem_certificate", None):
                listed_name = getattr(cert, "name", "<unnamed>")
                try:
                    cert = client.get_certificate(name=cert.name)
                except gax.NotFound as exc:
                    counts["out_of_policy"] += 1
                    failures.append(f"{listed_name}: listed certificate could not be read: {exc}")
                    continue
            evidence = _evaluate_certificate(cert, now)
            counts[evidence.category] += 1
            if not evidence.compliant:
                failures.append(f"{getattr(cert, 'name', '<unnamed>')}: {evidence.reason}")

        if certs_inspected == 0:
            # Only the provider-hidden GKE control-plane CA (or no customer
            # certificates) exists. Mirror the AWS provider-hidden path with a
            # structured skip the validator honors.
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "No customer-visible Certificate Manager certificates found on this platform"
            result["tests"] = {
                "cert_inventory_non_empty": {"passed": True, "skipped": True, "message": result["skip_reason"]},
                "no_certs_out_of_policy": {
                    "passed": True,
                    "skipped": True,
                    "message": "No managed certificate inventory to evaluate",
                },
                "rotation_evidence_present": {
                    "passed": True,
                    "skipped": True,
                    "message": "GKE control-plane certificate rotation is provider-managed",
                },
            }
            print(json.dumps(result, indent=2))
            return 0

        result["certs_inspected"] = certs_inspected
        result.update(counts)
        result["tests"]["cert_inventory_non_empty"] = {
            "passed": True,
            "message": f"Inspected {certs_inspected} Certificate Manager certificate(s)",
        }
        result["tests"]["no_certs_out_of_policy"] = {
            "passed": counts["out_of_policy"] == 0,
            "message" if counts["out_of_policy"] == 0 else "error": (
                f"All {certs_inspected} certificate(s) have explicit rotation evidence"
                if counts["out_of_policy"] == 0
                else "; ".join(failures)
            ),
        }
        evidence_count = counts["auto_rotated"] + counts["short_validity"]
        result["tests"]["rotation_evidence_present"] = {
            "passed": evidence_count == certs_inspected,
            "message" if evidence_count == certs_inspected else "error": (
                f"Rotation evidence: {counts['auto_rotated']} managed auto-renewed, "
                f"{counts['short_validity']} self-managed short-validity"
                if evidence_count == certs_inspected
                else f"Only {evidence_count}/{certs_inspected} certificate(s) have rotation evidence"
            ),
        }
        result["success"] = all(t["passed"] for t in result["tests"].values())
    except Exception as e:
        if _is_service_unavailable(e):
            # Certificate Manager API is DISABLED on this project -- the
            # customer-visible certificate surface does not exist here, which is
            # provider-hidden parity, not a hard failure. A generic
            # PermissionDenied / list failure is NOT matched here and falls
            # through to a hard inspection error below.
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = "Certificate Manager API is not enabled on this project"
            result["tests"] = {
                "cert_inventory_non_empty": {"passed": True, "skipped": True, "message": result["skip_reason"]},
                "no_certs_out_of_policy": {"passed": True, "skipped": True, "message": result["skip_reason"]},
                "rotation_evidence_present": {"passed": True, "skipped": True, "message": result["skip_reason"]},
            }
            print(json.dumps(result, indent=2))
            return 0
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())

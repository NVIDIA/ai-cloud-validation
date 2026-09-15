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

"""Network validations for step outputs.

Validations for VPCs, subnets, security groups, connectivity, traffic flow,
and DDI (DNS/DHCP/IP management).
"""

from __future__ import annotations

import ipaddress
import math
import re
import shlex
import time
import uuid
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

if TYPE_CHECKING:
    import paramiko

import pytest

from isvtest.core.k8s import (
    command_detail,
    get_kubectl_base_shell,
    kubectl_items_or_empty,
    kubectl_items_or_fail,
    kubectl_payload_or_none,
    names_from_items,
)
from isvtest.core.ssh import (
    get_failed_subtests,
    get_ssh_client,
    get_ssh_config,
    run_ssh_command,
)
from isvtest.core.validation import BaseValidation, check_required_tests


class NetworkProvisionedCheck(BaseValidation):
    """Validate network/VPC was provisioned.

    Config:
        step_output: The step output to check

    Step output:
        network_id: Network/VPC identifier
        cidr: Network CIDR block
        subnets: Optional list of subnets
    """

    description: ClassVar[str] = "Check network was provisioned"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        network_id = step_output.get("network_id")
        if not network_id:
            self.set_failed("No 'network_id' in step output")
            return

        cidr = step_output.get("cidr", "N/A")
        subnets = step_output.get("subnets", [])
        subnet_count = len(subnets) if isinstance(subnets, list) else 0

        self.set_passed(f"Network {network_id} provisioned: CIDR={cidr}, subnets={subnet_count}")


class VpcCrudCheck(BaseValidation):
    """Validate VPC CRUD operations completed successfully.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_vpc, read_vpc, update_tags, update_dns, delete_vpc
        Each test has 'passed' boolean
    """

    description: ClassVar[str] = "Check VPC CRUD operations"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        # Operations to assert; defaults to the full CRUD set. Callers can pass
        # a subset (e.g. ["create_vpc"]) to scope one wiring to a single plan id.
        required_tests = self.config.get("operations") or [
            "create_vpc",
            "read_vpc",
            "update_tags",
            "update_dns",
            "delete_vpc",
        ]
        passed_tests = []
        failed_tests = []

        for test_name in required_tests:
            test_result = tests.get(test_name, {})
            if test_result.get("passed"):
                passed_tests.append(test_name)
            else:
                error = test_result.get("error", "unknown error")
                failed_tests.append(f"{test_name}: {error}")

        if not failed_tests:
            self.set_passed(f"All {len(passed_tests)} CRUD tests passed")
        else:
            self.set_failed(f"Failed tests: {', '.join(failed_tests)}")


class SubnetConfigCheck(BaseValidation):
    """Validate subnet configuration across availability zones.

    Config:
        step_output: The step output to check
        min_subnets: Minimum number of subnets required (default: 2)
        require_multi_az: Require subnets across multiple AZs (default: True)

    Step output:
        tests: dict with create_subnets, az_distribution, subnets_available
        subnets: list of subnet info
    """

    description: ClassVar[str] = "Check subnet configuration"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})
        subnets = step_output.get("subnets", [])

        min_subnets = self.config.get("min_subnets", 2)
        require_multi_az = self.config.get("require_multi_az", True)

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        # Check subnet count
        if len(subnets) < min_subnets:
            self.set_failed(f"Only {len(subnets)} subnets, minimum {min_subnets} required")
            return

        # Check AZ distribution
        if require_multi_az:
            az_result = tests.get("az_distribution", {})
            az_count = az_result.get("az_count", 0)
            if az_count < 2:
                self.set_failed(f"Subnets in only {az_count} AZ(s), multi-AZ required")
                return

        # Check all tests passed
        failed_tests = []
        for test_name, test_result in tests.items():
            if not test_result.get("passed"):
                failed_tests.append(test_name)

        if failed_tests:
            self.set_failed(f"Failed tests: {', '.join(failed_tests)}")
        else:
            azs = tests.get("az_distribution", {}).get("azs", [])
            self.set_passed(f"{len(subnets)} subnets across {len(azs)} AZs")


class VpcIsolationCheck(BaseValidation):
    """Validate VPC isolation - no connectivity between separate VPCs.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with no_peering, no_cross_routes_a, no_cross_routes_b, sg_isolation_*
        vpc_a, vpc_b: VPC info
    """

    description: ClassVar[str] = "Check VPC isolation"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required_tests = ["no_peering", "no_cross_routes_a", "no_cross_routes_b"]
        failed_tests = []

        for test_name in required_tests:
            test_result = tests.get(test_name, {})
            if not test_result.get("passed"):
                error = test_result.get("error", "test not found")
                failed_tests.append(f"{test_name}: {error}")

        # Also check SG isolation tests
        for key, value in tests.items():
            if key.startswith("sg_isolation") and not value.get("passed"):
                failed_tests.append(f"{key}: {value.get('error', 'failed')}")

        if failed_tests:
            self.set_failed(f"Isolation violations: {'; '.join(failed_tests)}")
        else:
            vpc_a = step_output.get("vpc_a", {}).get("id", "?")
            vpc_b = step_output.get("vpc_b", {}).get("id", "?")
            self.set_passed(f"VPCs {vpc_a} and {vpc_b} are properly isolated")


class SgCrudCheck(BaseValidation):
    """Validate Security Group CRUD lifecycle operations.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_vpc, create_sg, read_sg, update_sg_add_rule,
               update_sg_modify_rule, update_sg_remove_rule,
               delete_sg, verify_deleted
    """

    description: ClassVar[str] = "Check security group CRUD operations"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        # Operations to assert; defaults to the full SG CRUD set. Callers can
        # pass a subset to scope one wiring to a single plan id.
        required_tests = self.config.get("operations") or [
            "create_vpc",
            "create_sg",
            "read_sg",
            "update_sg_add_rule",
            "update_sg_modify_rule",
            "update_sg_remove_rule",
            "delete_sg",
            "verify_deleted",
        ]
        passed_tests = []
        failed_tests = []

        for test_name in required_tests:
            test_result = tests.get(test_name, {})
            if test_result.get("passed"):
                passed_tests.append(test_name)
            else:
                error = test_result.get("error", "unknown error")
                failed_tests.append(f"{test_name}: {error}")

        if not failed_tests:
            self.set_passed(f"All {len(passed_tests)} SG CRUD tests passed")
        else:
            self.set_failed(f"Failed tests: {', '.join(failed_tests)}")


class SecurityBlockingCheck(BaseValidation):
    """Validate security group and NACL blocking rules work correctly.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with sg_default_deny_inbound, sg_allows_specific_ssh,
               sg_denies_vpc_icmp, nacl_explicit_deny, sg_restricted_egress
    """

    description: ClassVar[str] = "Check security blocking rules"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        security_tests = [
            "sg_default_deny_inbound",
            "sg_allows_specific_ssh",
            "sg_denies_vpc_icmp",
            "nacl_explicit_deny",
            "sg_restricted_egress",
        ]

        passed = 0
        failed_tests = []

        for test_name in security_tests:
            test_result = tests.get(test_name, {})
            if test_result.get("passed"):
                passed += 1
            else:
                failed_tests.append(test_name)

        if failed_tests:
            self.set_failed(f"Security tests failed: {', '.join(failed_tests)}")
        else:
            self.set_passed(f"All {passed} security blocking tests passed")


class NetworkConnectivityCheck(BaseValidation):
    """Validate network connectivity for instances.

    Config:
        step_output: The step output to check

    Step output:
        instances: list of instance info with public_ip, private_ip
        tests: optional dict with connectivity test results
    """

    description: ClassVar[str] = "Check network connectivity"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        instances = step_output.get("instances", [])
        tests = step_output.get("tests", {})

        if not instances:
            self.set_failed("No 'instances' in step output")
            return

        # Check instances have IPs
        instances_with_ip = 0
        for inst in instances:
            if isinstance(inst, dict):
                if inst.get("private_ip") or inst.get("public_ip"):
                    instances_with_ip += 1

        if instances_with_ip == 0:
            self.set_failed("No instances have IP addresses assigned")
            return

        # Check connectivity tests if present
        if tests:
            failed = [k for k, v in tests.items() if not v.get("passed")]
            if failed:
                self.set_failed(f"Connectivity tests failed: {', '.join(failed)}")
                return

        self.set_passed(f"{instances_with_ip} instances with network connectivity")


class TrafficFlowCheck(BaseValidation):
    """Validate real network traffic flow (ping allowed/blocked).

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with traffic_allowed, traffic_blocked, internet_icmp, internet_http
    """

    description: ClassVar[str] = "Check traffic flow"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        traffic_tests = ["traffic_allowed", "traffic_blocked", "internet_icmp", "internet_http"]
        passed = []
        failed = []

        for test_name in traffic_tests:
            test_result = tests.get(test_name, {})
            if test_result.get("passed"):
                passed.append(test_name)
            else:
                error = test_result.get("error", "not found")
                failed.append(f"{test_name}: {error}")

        if failed:
            self.set_failed(f"Traffic tests failed: {'; '.join(failed)}")
        else:
            latency = tests.get("traffic_allowed", {}).get("latency_ms", "N/A")
            self.set_passed(f"All {len(passed)} traffic tests passed (latency: {latency}ms)")


class DhcpIpManagementCheck(BaseValidation):
    """Validate DHCP/IP management on an instance via SSH.

    SSHes into an instance and verifies that:
    1. A DHCP lease is active (dhclient or systemd-networkd)
    2. The instance IP matches what the platform reports
    3. DHCP-provided options (DNS, domain) are correctly configured

    Config:
        step_output: Must include public_ip or host, key_file, ssh_user
        inventory: Optional inventory for SSH config resolution

    Step output:
        public_ip: SSH target address
        private_ip: Expected private IP (for comparison)
        key_file: Path to SSH private key
        ssh_user: SSH username
    """

    description: ClassVar[str] = "Check DHCP/IP management via SSH"
    timeout: ClassVar[int] = 60

    def run(self) -> None:
        try:
            import paramiko  # noqa: F401
        except ImportError:
            self.set_failed("paramiko not installed")
            return

        ssh_cfg = get_ssh_config(self.config, self.config.get("inventory", {}))
        host = ssh_cfg["ssh_host"]
        user = ssh_cfg["ssh_user"]
        key_path = ssh_cfg["ssh_key_path"]

        if not host:
            self.set_failed("No SSH host configured")
            return
        if not key_path:
            self.set_failed("No SSH key configured")
            return

        try:
            ssh = get_ssh_client(host, user, key_path)
        except Exception as e:
            self.set_failed(f"SSH connection failed: {e}")
            return

        try:
            self._check_dhcp_lease(ssh)
            self._check_ip_matches_platform(ssh)
            self._check_dhcp_options(ssh)

            failed = get_failed_subtests(self._subtest_results)
            if failed:
                self.set_failed(f"DHCP subtests failed: {', '.join(failed)}")
            else:
                self.set_passed(f"DHCP/IP management verified on {host}")
        finally:
            ssh.close()

    def _check_dhcp_lease(self, ssh: paramiko.SSHClient) -> None:
        """Check that a DHCP client is active and a valid lease exists."""
        cmd = (
            "echo '---DHCP_PROC---' && "
            "(pgrep -a 'dhclient|dhcpcd|systemd-network' 2>/dev/null || echo 'NO_DHCP_PROCESS') && "
            "echo '---DHCP_LEASE---' && "
            "(cat /var/lib/dhcp/dhclient*.leases "
            "/run/systemd/netif/leases/* "
            "/var/lib/NetworkManager/internal-*.lease "
            "/var/lib/NetworkManager/dhclient-*.lease "
            "2>/dev/null || echo 'NO_LEASE_FILES')"
        )
        _exit_code, stdout, _ = run_ssh_command(ssh, cmd)

        proc_section = ""
        lease_section = ""
        if "---DHCP_PROC---" in stdout and "---DHCP_LEASE---" in stdout:
            parts = stdout.split("---DHCP_LEASE---")
            proc_section = parts[0].split("---DHCP_PROC---")[-1].strip()
            lease_section = parts[1].strip()

        has_process = "NO_DHCP_PROCESS" not in proc_section and proc_section != ""
        has_lease = "NO_LEASE_FILES" not in lease_section and lease_section != ""

        if has_process or has_lease:
            details = []
            if has_process:
                details.append("DHCP process running")
            if has_lease:
                details.append("lease file found")
            self.report_subtest("dhcp_lease_active", True, "; ".join(details))
        else:
            self.report_subtest("dhcp_lease_active", False, "No DHCP process or lease files found")

    def _check_ip_matches_platform(self, ssh: paramiko.SSHClient) -> None:
        """Compare instance IP against platform-reported private_ip."""
        expected_ip = self.config.get("step_output", {}).get("private_ip")
        if not expected_ip:
            self.report_subtest(
                "ip_matches_platform",
                True,
                "Skipped: no private_ip in step_output",
                skipped=True,
            )
            return

        cmd = "ip -4 addr show scope global | awk '/inet / {split($2, a, \"/\"); print a[1]}'"
        _exit_code, stdout, _ = run_ssh_command(ssh, cmd)
        actual_ips = [ip.strip() for ip in stdout.strip().splitlines() if ip.strip()]

        if expected_ip in actual_ips:
            self.report_subtest(
                "ip_matches_platform",
                True,
                f"Platform IP {expected_ip} found on instance",
            )
        else:
            self.report_subtest(
                "ip_matches_platform",
                False,
                f"Expected {expected_ip}, found {actual_ips}",
            )

    def _check_dhcp_options(self, ssh: paramiko.SSHClient) -> None:
        """Verify DHCP-provided DNS and domain options are configured."""
        cmd = (
            "echo '---RESOLV---' && "
            "(cat /etc/resolv.conf 2>/dev/null || echo 'NO_RESOLV_CONF') && "
            "echo '---DHCP_OPTS---' && "
            "(grep -rh 'domain-name-servers\\|domain-name\\|ntp-servers' /var/lib/dhcp/ 2>/dev/null; "
            "grep -rh 'DNS=\\|DOMAINNAME=\\|NTP=' /run/systemd/netif/leases/ 2>/dev/null; "
            "echo 'DONE')"
        )
        _exit_code, stdout, _ = run_ssh_command(ssh, cmd)

        resolv_section = ""
        if "---RESOLV---" in stdout and "---DHCP_OPTS---" in stdout:
            parts = stdout.split("---DHCP_OPTS---")
            resolv_section = parts[0].split("---RESOLV---")[-1].strip()

        nameservers = re.findall(r"nameserver\s+([\d.]+)", resolv_section)

        if nameservers:
            self.report_subtest(
                "dhcp_options_correct",
                True,
                f"DNS servers: {', '.join(nameservers)}",
            )
        else:
            self.report_subtest(
                "dhcp_options_correct",
                False,
                "No nameserver entries found in /etc/resolv.conf",
            )


class VpcIpConfigCheck(BaseValidation):
    """Validate VPC-level IP configuration is sensible.

    Checks that:
    1. DHCP options set is configured with DNS servers
    2. Subnet CIDRs are valid, non-overlapping, and within VPC range
    3. Public IP assignment is configured per the provider's model

    Config:
        step_output: VPC creation output with dhcp_options, subnets, cidr
        min_ips_per_subnet: Minimum IPs per subnet (default: 16)
        auto_assign_ip_mode: How the NCP exposes external IPs (default: "subnet"):
            - "subnet": at least one subnet must have ``auto_assign_public_ip``
              truthy (AWS model - MapPublicIpOnLaunch)
            - "instance": external IPs are attached per instance at launch time
              (GCP model - accessConfig), so subnet-level flags don't apply;
              the subtest reports PASS with informational status
            - "disabled": no public IPs expected for this deployment; the
              subtest reports PASS with informational status

    Step output:
        cidr: VPC CIDR block (e.g. "10.0.0.0/16")
        subnets: list of subnet dicts with cidr, auto_assign_public_ip, available_ips
        dhcp_options: dict with domain_name_servers, domain_name, etc.
    """

    description: ClassVar[str] = "Check VPC IP configuration"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})

        if not step_output:
            self.set_failed("No step_output provided")
            return

        self._check_dhcp_options_configured(step_output)
        self._check_subnet_cidr_valid(step_output)
        self._check_auto_assign_ip(step_output)

        failed = get_failed_subtests(self._subtest_results)
        if failed:
            self.set_failed(f"VPC IP config subtests failed: {', '.join(failed)}")
        else:
            self.set_passed("VPC IP configuration is valid")

    def _check_dhcp_options_configured(self, step_output: dict) -> None:
        """Verify DHCP options set is configured with DNS."""
        dhcp_options = step_output.get("dhcp_options")
        if not dhcp_options:
            self.report_subtest(
                "dhcp_options_configured",
                False,
                "No 'dhcp_options' in step output",
            )
            return

        dns_servers = dhcp_options.get("domain_name_servers", [])
        if not dns_servers:
            self.report_subtest(
                "dhcp_options_configured",
                False,
                "No domain_name_servers in DHCP options",
            )
            return

        domain = dhcp_options.get("domain_name", "N/A")
        self.report_subtest(
            "dhcp_options_configured",
            True,
            f"DNS: {dns_servers}, domain: {domain}",
        )

    def _check_subnet_cidr_valid(self, step_output: dict) -> None:
        """Validate subnet CIDRs are within VPC range and non-overlapping."""
        vpc_cidr_str = step_output.get("cidr")
        subnets = step_output.get("subnets", [])

        if not vpc_cidr_str:
            self.report_subtest(
                "subnet_cidr_valid",
                False,
                "No 'cidr' in step output",
            )
            return

        if not subnets:
            self.report_subtest(
                "subnet_cidr_valid",
                False,
                "No 'subnets' in step output",
            )
            return

        try:
            vpc_net = ipaddress.ip_network(vpc_cidr_str, strict=False)
        except ValueError as e:
            self.report_subtest(
                "subnet_cidr_valid",
                False,
                f"Invalid VPC CIDR: {e}",
            )
            return

        min_ips = self.config.get("min_ips_per_subnet", 16)
        subnet_nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        errors: list[str] = []

        for sub in subnets:
            cidr_str = sub.get("cidr", "")
            try:
                subnet_net = ipaddress.ip_network(cidr_str, strict=False)
            except ValueError:
                errors.append(f"Invalid subnet CIDR: {cidr_str}")
                continue

            # Check within VPC range (subnet_of requires matching IPv4/IPv6)
            if isinstance(subnet_net, ipaddress.IPv4Network) and isinstance(vpc_net, ipaddress.IPv4Network):
                if not subnet_net.subnet_of(vpc_net):
                    errors.append(f"{cidr_str} not within VPC {vpc_cidr_str}")
            elif isinstance(subnet_net, ipaddress.IPv6Network) and isinstance(vpc_net, ipaddress.IPv6Network):
                if not subnet_net.subnet_of(vpc_net):
                    errors.append(f"{cidr_str} not within VPC {vpc_cidr_str}")
            else:
                errors.append(
                    f"{cidr_str} address family does not match VPC {vpc_cidr_str}",
                )

            # Check overlap with previously seen subnets (same address family only)
            for existing in subnet_nets:
                if isinstance(subnet_net, ipaddress.IPv4Network) and isinstance(existing, ipaddress.IPv4Network):
                    if subnet_net.overlaps(existing):
                        errors.append(f"{cidr_str} overlaps {existing}")
                elif isinstance(subnet_net, ipaddress.IPv6Network) and isinstance(existing, ipaddress.IPv6Network):
                    if subnet_net.overlaps(existing):
                        errors.append(f"{cidr_str} overlaps {existing}")

            # Check IP capacity
            available = sub.get("available_ips", subnet_net.num_addresses - 5)
            if available < min_ips:
                errors.append(f"{cidr_str} has only {available} IPs (min: {min_ips})")

            subnet_nets.append(subnet_net)

        if errors:
            self.report_subtest(
                "subnet_cidr_valid",
                False,
                "; ".join(errors),
            )
        else:
            self.report_subtest(
                "subnet_cidr_valid",
                True,
                f"{len(subnet_nets)} subnets valid within {vpc_cidr_str}",
            )

    def _check_auto_assign_ip(self, step_output: dict) -> None:
        """Check public IP assignment per the NCP's configured model."""
        mode = self.config.get("auto_assign_ip_mode", "subnet")
        valid_modes = ("subnet", "instance", "disabled")
        if mode not in valid_modes:
            self.report_subtest(
                "auto_assign_ip_enabled",
                False,
                f"Invalid auto_assign_ip_mode={mode!r} (expected one of {valid_modes})",
            )
            return

        if mode == "instance":
            self.report_subtest(
                "auto_assign_ip_enabled",
                True,
                "NCP assigns external IPs per-instance (no subnet-level flag)",
            )
            return

        if mode == "disabled":
            self.report_subtest(
                "auto_assign_ip_enabled",
                True,
                "Public IP assignment disabled for this deployment",
            )
            return

        # mode == "subnet": AWS-style, at least one subnet must opt in.
        subnets = step_output.get("subnets", [])

        if not subnets:
            self.report_subtest(
                "auto_assign_ip_enabled",
                False,
                "No subnets in step output",
            )
            return

        auto_assign_subnets = [
            s.get("subnet_id", s.get("cidr", "unknown")) for s in subnets if s.get("auto_assign_public_ip")
        ]

        if auto_assign_subnets:
            self.report_subtest(
                "auto_assign_ip_enabled",
                True,
                f"{len(auto_assign_subnets)} subnet(s) with auto-assign IP",
            )
        else:
            self.report_subtest(
                "auto_assign_ip_enabled",
                False,
                "No subnets have auto_assign_public_ip enabled",
            )


def _run_sg_scoping_check(
    validation: BaseValidation,
    required_keys: list[str],
    default_scope: str,
    label: str,
) -> None:
    """Shared logic for SG scoping validations (workload/node/subnet/service)."""
    if not check_required_tests(validation, required_keys, f"{label} scoping tests failed"):
        return
    scope = validation.config.get("step_output", {}).get("scope", default_scope)
    validation.set_passed(f"SG rules correctly scoped at {scope} level")


def _is_evidence_present(step_output: dict[str, object], key: str) -> bool:
    """Return True when a required SDN logging evidence field is populated."""
    value = step_output.get(key)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _run_sdn_logging_check(
    validation: BaseValidation,
    required_keys: list[str],
    evidence_keys: list[str],
    label: str,
) -> None:
    """Shared logic for SDN logging validations."""
    if not check_required_tests(validation, required_keys, f"{label} logging tests failed"):
        return

    step_output = validation.config.get("step_output", {})
    missing_evidence = [key for key in evidence_keys if not _is_evidence_present(step_output, key)]
    if missing_evidence:
        validation.set_failed(f"Missing SDN logging evidence: {', '.join(missing_evidence)}")
        return

    summary = ", ".join(f"{key}={step_output.get(key)}" for key in evidence_keys)
    validation.set_passed(f"{label} logging validated ({summary})")


class SdnHardwareFaultLoggingCheck(BaseValidation):
    """Validate logging is available for network hardware faults.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with logging_endpoint_reachable,
               fault_event_source_queryable, log_destination_configured,
               event_schema_valid
        log_destination: Customer-visible log destination identifier
        recent_event_count: Number of recent provider hardware-fault events
    """

    description: ClassVar[str] = "Check SDN hardware fault logging"

    def run(self) -> None:
        """Check hardware-fault logging from step output."""
        _run_sdn_logging_check(
            self,
            [
                "logging_endpoint_reachable",
                "fault_event_source_queryable",
                "log_destination_configured",
                "event_schema_valid",
            ],
            ["log_destination", "recent_event_count"],
            "SDN hardware fault",
        )


class SdnLatencyPerfLoggingCheck(BaseValidation):
    """Validate logging captures latency/performance fluctuations.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with metrics_endpoint_reachable, performance_metric_present,
               packet_metric_present, samples_recent
        telemetry_namespace: Telemetry namespace used for the metric/log samples
        sample_window_seconds: Recent sample lookback window
        probe_resource_id: Resource whose telemetry was sampled
    """

    description: ClassVar[str] = "Check SDN latency/performance logging"

    def run(self) -> None:
        """Check latency/performance logging from step output."""
        _run_sdn_logging_check(
            self,
            [
                "metrics_endpoint_reachable",
                "performance_metric_present",
                "packet_metric_present",
                "samples_recent",
            ],
            ["telemetry_namespace", "sample_window_seconds", "probe_resource_id"],
            "SDN latency/performance",
        )


class SdnFilterAuditTrailCheck(BaseValidation):
    """Validate audit trails for network filtering rule changes.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with audit_endpoint_reachable, create_rule_logged,
               modify_rule_logged, delete_rule_logged,
               audit_event_has_required_fields, cleanup
        trail_id: Audit trail identifier or source
        actor_field: Actor field used by the audit event
        target_rule_id: Rule/security-group identifier modified by the probe
    """

    description: ClassVar[str] = "Check SDN filtering rule audit trail"

    def run(self) -> None:
        """Check filtering-rule audit logging from step output."""
        _run_sdn_logging_check(
            self,
            [
                "audit_endpoint_reachable",
                "create_rule_logged",
                "modify_rule_logged",
                "delete_rule_logged",
                "audit_event_has_required_fields",
                "cleanup",
            ],
            ["trail_id", "actor_field", "target_rule_id"],
            "SDN filtering audit trail",
        )


def _coerce_nonnegative_float(value: object, field_name: str) -> tuple[float | None, str | None]:
    """Return a non-negative float value or an error string."""
    if isinstance(value, bool):
        return None, f"`{field_name}` must be a number, got bool: {value!r}"
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, f"`{field_name}` must be a number, got {type(value).__name__}: {value!r}"
    if not math.isfinite(numeric):
        return None, f"`{field_name}` must be finite, got {numeric!r}"
    if numeric < 0:
        return None, f"`{field_name}` must be >= 0, got {numeric}"
    return numeric, None


class SgPolicyPropagationTimingCheck(BaseValidation):
    """Validate security policy rule propagation timing.

    Config:
        step_output: The step output to check
        max_propagation_seconds: Optional timing threshold override

    Step output:
        tests: dict with create_probe_rule, rule_observed,
               revoke_probe_rule, removal_observed, cleanup
        target_rule_id: Rule/security-group identifier modified by the probe
        add_observed_seconds: Time until the added policy was observable
        remove_observed_seconds: Time until the removed policy disappeared
        max_propagation_seconds: Provider threshold used by the probe
    """

    description: ClassVar[str] = "Check security policy propagation timing"

    def run(self) -> None:
        """Check policy propagation timing evidence from step output."""
        required_tests = [
            "create_probe_rule",
            "rule_observed",
            "revoke_probe_rule",
            "removal_observed",
            "cleanup",
        ]
        if not check_required_tests(self, required_tests, "Security policy propagation tests failed"):
            return

        step_output = self.config.get("step_output", {})
        missing_evidence = [
            key
            for key in ("target_rule_id", "add_observed_seconds", "remove_observed_seconds")
            if step_output.get(key) in (None, "")
        ]
        if missing_evidence:
            self.set_failed(f"Missing SDN policy propagation evidence: {', '.join(missing_evidence)}")
            return

        threshold_source = self.config.get(
            "max_propagation_seconds",
            step_output.get("max_propagation_seconds", 10),
        )
        max_seconds, threshold_error = _coerce_nonnegative_float(threshold_source, "max_propagation_seconds")
        if threshold_error:
            self.set_failed(threshold_error)
            return

        add_seconds, add_error = _coerce_nonnegative_float(
            step_output.get("add_observed_seconds"),
            "add_observed_seconds",
        )
        remove_seconds, remove_error = _coerce_nonnegative_float(
            step_output.get("remove_observed_seconds"),
            "remove_observed_seconds",
        )
        errors = [error for error in (add_error, remove_error) if error]
        if errors:
            self.set_failed("; ".join(errors))
            return

        assert add_seconds is not None
        assert remove_seconds is not None
        assert max_seconds is not None

        slow = []
        if add_seconds > max_seconds:
            slow.append(f"add {add_seconds:.2f}s exceeds {max_seconds:.2f}s")
        if remove_seconds > max_seconds:
            slow.append(f"remove {remove_seconds:.2f}s exceeds {max_seconds:.2f}s")
        if slow:
            self.set_failed(f"Security policy propagation timing exceeded: {', '.join(slow)}")
            return

        self.set_passed(
            "Security policy propagation within threshold "
            f"(target={step_output['target_rule_id']}, add={add_seconds:.2f}s, "
            f"remove={remove_seconds:.2f}s, max={max_seconds:.2f}s)"
        )


class SgWorkloadScopingCheck(BaseValidation):
    """Validate security group rules can be scoped at workload level.

    Verifies the platform supports applying SG rules that target individual
    workloads (pods, containers, tasks) rather than broad node/subnet ranges.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_sg, apply_workload_rule, workload_allowed,
               other_workload_blocked, cleanup
    """

    description: ClassVar[str] = "Check SG rules scoped at workload level"

    def run(self) -> None:
        """Check workload-level SG scoping from step output."""
        _run_sg_scoping_check(
            self,
            ["create_sg", "apply_workload_rule", "workload_allowed", "other_workload_blocked", "cleanup"],
            "workload",
            "Workload",
        )


class SgNodeScopingCheck(BaseValidation):
    """Validate security group rules can be scoped at node level.

    Verifies the platform supports applying SG rules that target individual
    nodes, ensuring traffic policy is enforced per-host.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_sg, apply_node_rule, target_node_allowed,
               other_node_blocked, cleanup
    """

    description: ClassVar[str] = "Check SG rules scoped at node level"

    def run(self) -> None:
        """Check node-level SG scoping from step output."""
        _run_sg_scoping_check(
            self,
            ["create_sg", "apply_node_rule", "target_node_allowed", "other_node_blocked", "cleanup"],
            "node",
            "Node",
        )


class SgSubnetScopingCheck(BaseValidation):
    """Validate security group rules can be scoped at subnet/tenant level.

    Verifies the platform supports applying SG rules at the subnet or
    tenant boundary, ensuring cross-tenant or cross-subnet traffic is
    controlled.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_sg, apply_subnet_rule, subnet_allowed,
               other_subnet_blocked, cleanup
    """

    description: ClassVar[str] = "Check SG rules scoped at subnet/tenant level"

    def run(self) -> None:
        """Check subnet-level SG scoping from step output."""
        _run_sg_scoping_check(
            self,
            ["create_sg", "apply_subnet_rule", "subnet_allowed", "other_subnet_blocked", "cleanup"],
            "subnet",
            "Subnet",
        )


class SgServiceScopingCheck(BaseValidation):
    """Validate security group rules can be scoped at service level.

    Verifies the platform supports applying SG rules that target a specific
    service endpoint (e.g. the K8s API server), so the rule does not leak
    onto unrelated workloads/nodes/subnets.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_sg, apply_service_rule, service_endpoint_allowed,
               other_endpoint_blocked, cleanup
    """

    description: ClassVar[str] = "Check SG rules scoped at service level"

    def run(self) -> None:
        """Check service-level SG scoping from step output."""
        _run_sg_scoping_check(
            self,
            ["create_sg", "apply_service_rule", "service_endpoint_allowed", "other_endpoint_blocked", "cleanup"],
            "service",
            "Service",
        )


class SgPortSecurityPolicyCheck(BaseValidation):
    """Validate custom port security policies on virtual interfaces.

    Verifies the platform can apply a custom ingress port policy to a
    target virtual interface without permitting adjacent/unlisted ports
    or leaking the policy onto an unrelated virtual interface.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_virtual_interface, apply_port_policy,
               allowed_port_permitted, unlisted_port_blocked,
               other_interface_unaffected, cleanup
    """

    description: ClassVar[str] = "Check custom port security policies on virtual interfaces"

    def run(self) -> None:
        """Check virtual-interface port policy behavior from step output."""
        required = [
            "create_virtual_interface",
            "apply_port_policy",
            "allowed_port_permitted",
            "unlisted_port_blocked",
            "other_interface_unaffected",
            "cleanup",
        ]
        if not check_required_tests(self, required, "Port security policy tests failed"):
            return
        self.set_passed("Custom port security policy scoped to virtual interface")


def _is_non_empty_string(value: object) -> bool:
    """Return True when value is a non-empty string after trimming."""
    return isinstance(value, str) and bool(value.strip())


def _validate_node_reports(value: object, kind: str) -> str | None:
    """Return an error message if value is not a list of per-node reports.

    Every per-node contract in this module carries the same shape - a list of
    objects each identified by a non-empty ``node_id`` - so the guard lives here
    rather than being restated by each check.
    """
    if not isinstance(value, list):
        return f"`nodes` must be a list of per-node {kind} reports"
    for index, node in enumerate(value):
        if not isinstance(node, dict):
            return f"`nodes[{index}]` must be an object"
        if not _is_non_empty_string(node.get("node_id")):
            return f"`nodes[{index}].node_id` must be a non-empty string"
    return None


def _validate_string_list(value: object, field_name: str) -> str | None:
    """Return an error message if value is not a non-empty list of strings."""
    if not isinstance(value, list):
        return f"`fabric.{field_name}` must be a non-empty list of strings"
    if not value:
        return f"`fabric.{field_name}` must not be empty"
    invalid_items = [item for item in value if not _is_non_empty_string(item)]
    if invalid_items:
        return f"`fabric.{field_name}` contains non-string or empty values"
    return None


class BackendSwitchFabricCheck(BaseValidation):
    """Validate backend switch fabric IDs for a compute node.

    Config:
        step_output: The step output to check

    Step output:
        node_id: Compute node identifier
        fabric: dict with leaf_switch_ids, spine_switch_ids, core_switch_ids
        tests: dict with node_resolved, leaf_switch_ids_present,
               spine_switch_ids_present, core_switch_ids_present
    """

    description: ClassVar[str] = "Check backend switch fabric IDs"

    def run(self) -> None:
        """Check backend switch fabric metadata from step output."""
        step_output = self.config.get("step_output", {})

        required = [
            "node_resolved",
            "leaf_switch_ids_present",
            "spine_switch_ids_present",
            "core_switch_ids_present",
        ]
        if not check_required_tests(self, required, "Backend switch fabric tests failed"):
            return

        node_id = step_output.get("node_id")
        if not _is_non_empty_string(node_id):
            self.set_failed("`node_id` must be a non-empty string")
            return

        fabric = step_output.get("fabric")
        if not isinstance(fabric, dict):
            self.set_failed("`fabric` must be an object with leaf, spine, and core switch IDs")
            return

        errors = [
            error
            for error in (
                _validate_string_list(fabric.get("leaf_switch_ids"), "leaf_switch_ids"),
                _validate_string_list(fabric.get("spine_switch_ids"), "spine_switch_ids"),
                _validate_string_list(fabric.get("core_switch_ids"), "core_switch_ids"),
            )
            if error is not None
        ]
        if errors:
            self.set_failed("; ".join(errors))
            return

        leaf_count = len(fabric["leaf_switch_ids"])
        spine_count = len(fabric["spine_switch_ids"])
        core_count = len(fabric["core_switch_ids"])
        self.set_passed(
            f"Backend fabric for {node_id}: {leaf_count} leaf, {spine_count} spine, {core_count} core switch ID(s)"
        )


class NvlinkDomainCheck(BaseValidation):
    """Validate NVLink domain metadata for a compute node.

    Non-NVLink nodes are skipped explicitly so reports distinguish unsupported
    hardware from validated NVLink domain metadata.

    Config:
        step_output: The step output to check

    Step output:
        node_id: Compute node identifier
        nvlink_supported: True when the node supports NVLink
        nvlink_domain_id: NVLink domain ID when NVLink is supported
        tests: dict with node_resolved, nvlink_support_detected,
               nvlink_domain_id_present
    """

    description: ClassVar[str] = "Check NVLink domain ID"

    def run(self) -> None:
        """Check NVLink domain metadata from step output."""
        step_output = self.config.get("step_output", {})

        node_id = step_output.get("node_id")
        if not _is_non_empty_string(node_id):
            self.set_failed("`node_id` must be a non-empty string")
            return

        detection_required = ["node_resolved", "nvlink_support_detected"]
        if not check_required_tests(self, detection_required, "NVLink support detection tests failed"):
            return

        nvlink_supported = step_output.get("nvlink_supported")
        if nvlink_supported is False:
            import pytest

            pytest.skip(f"NVLink not supported on node {node_id}; skipping NVLink domain validation")

        if nvlink_supported is not True:
            self.set_failed("`nvlink_supported` must be a boolean")
            return

        if not check_required_tests(self, ["nvlink_domain_id_present"], "NVLink domain tests failed"):
            return

        nvlink_domain_id = step_output.get("nvlink_domain_id")
        if not _is_non_empty_string(nvlink_domain_id):
            self.set_failed("`nvlink_domain_id` must be a non-empty string when NVLink is supported")
            return

        self.set_passed(f"NVLink domain for {node_id}: {nvlink_domain_id}")


class ImexDomainConnectivityCheck(BaseValidation):
    """Validate an IMEX domain is operational and every member pair is mutually connected.

    Membership is taken from the nodes that declare ``domain_member``, and
    pairwise connectivity is computed here from each node's own
    ``peers_reachable`` list. The provider-reported ``domain.fully_connected``
    boolean is deliberately ignored, so a one-way fault (node A observes B, but
    B does not observe A) cannot pass just because the vendor claims the domain
    is healthy.

    The IMEX service state of each node is carried for diagnostics but not
    asserted on - service lifecycle is out of scope for this check.

    Config:
        step_output: The step output to check

    Step output:
        domain: object with domain_id, state (operational when "up"),
                expected_members (min 2), and the ignored fully_connected flag
        nodes: list of {node_id, service_state, domain_member, peers_reachable}
        nodes_checked / nodes_validated: counts carried for reporting
    """

    description: ClassVar[str] = "Check IMEX domain is operational and fully, mutually connected"

    def run(self) -> None:
        """Check IMEX domain state, membership, and pairwise connectivity."""
        step_output = self.config.get("step_output", {})

        domain = step_output.get("domain")
        if not isinstance(domain, dict):
            self.set_failed("`domain` must be an object with domain_id, state, and expected_members")
            return

        domain_id = domain.get("domain_id")
        if not _is_non_empty_string(domain_id):
            self.set_failed("`domain.domain_id` must be a non-empty string")
            return

        state = domain.get("state")
        if not _is_non_empty_string(state) or state.strip().lower() != "up":
            self.set_failed(f"IMEX domain {domain_id} state is {state!r}, expected an operational state ('up')")
            return

        expected_error = _validate_string_list(domain.get("expected_members"), "expected_members")
        if expected_error is not None:
            self.set_failed(expected_error.replace("`fabric.", "`domain."))
            return
        expected_members = domain["expected_members"]
        if len(set(expected_members)) != len(expected_members):
            self.set_failed(f"IMEX domain {domain_id} `domain.expected_members` contains duplicate node IDs")
            return
        if len(expected_members) < 2:
            # A single-node connectivity matrix is trivially complete, so this
            # would otherwise pass vacuously - fail naming the environment.
            self.set_failed(
                f"IMEX domain {domain_id} has only {len(expected_members)} expected member(s): this environment "
                "is too small to validate a multi-node NVLink domain, which requires at least two members"
            )
            return

        nodes = step_output.get("nodes")
        if error := _validate_node_reports(nodes, "domain"):
            self.set_failed(error)
            return

        reported = [node["node_id"] for node in nodes if node.get("domain_member") is True]
        if len(set(reported)) != len(reported):
            self.set_failed(f"IMEX domain {domain_id} reports duplicate node IDs among its members")
            return

        expected_set = set(expected_members)
        reported_set = set(reported)
        if expected_set != reported_set:
            missing = sorted(expected_set - reported_set)
            unexpected = sorted(reported_set - expected_set)
            details = []
            if missing:
                details.append(f"missing: {missing}")
            if unexpected:
                # A node we were not allocated appearing in the domain is a
                # tenancy finding, not just a bookkeeping mismatch.
                details.append(f"unexpected (possible tenancy issue): {unexpected}")
            self.set_failed(f"IMEX domain {domain_id} membership mismatch ({'; '.join(details)})")
            return

        peers_by_node: dict[str, list[str]] = {}
        for node in nodes:
            if node.get("domain_member") is not True:
                continue
            peers = node.get("peers_reachable")
            if not isinstance(peers, list):
                self.set_failed(f"`peers_reachable` for {node['node_id']} must be a list of peer node IDs")
                return
            peers_by_node[node["node_id"]] = peers

        # Compute mutual connectivity per pair rather than trusting
        # domain.fully_connected, so a one-way fault cannot pass.
        members = sorted(reported_set)
        faults: list[str] = []
        for i, node_a in enumerate(members):
            for node_b in members[i + 1 :]:
                a_sees_b = node_b in peers_by_node[node_a]
                b_sees_a = node_a in peers_by_node[node_b]
                if a_sees_b and b_sees_a:
                    continue
                if a_sees_b or b_sees_a:
                    one_way = f"{node_a}->{node_b}" if a_sees_b else f"{node_b}->{node_a}"
                    faults.append(f"{node_a}<->{node_b}: one-way only ({one_way})")
                else:
                    faults.append(f"{node_a}<->{node_b}: no connectivity observed in either direction")

        if faults:
            self.set_failed(f"IMEX domain {domain_id} is not fully connected: {'; '.join(faults)}")
            return

        pair_count = len(members) * (len(members) - 1) // 2
        self.set_passed(
            f"IMEX domain {domain_id} is operational with {len(members)} member(s); "
            f"all {pair_count} pair(s) mutually connected"
        )


class ImexServicePresenceCheck(BaseValidation):
    """Validate IMEX ships in the delivered node image (host model).

    Asserts, per node, that the IMEX daemon and its control tooling are present
    and that the service is *registered with the node's service manager* - the
    manager reporting a loaded definition, not a unit file sitting on disk.

    Scope is set by the allocation, not by the node: nodes the provider marks
    with ``in_nvlink_allocation`` are asserted against. A node must not be able
    to self-report its way out of scope, so the provider is responsible for
    setting that flag from the allocation rather than from the node's own view
    of its NVLink support.

    Zero nodes asserted against is a failure, not a pass - an eight-node run
    that asserts against none of them is a vacuous pass otherwise.

    Config:
        step_output: The step output to check

    Step output:
        nodes: list of {node_id, in_nvlink_allocation, service_present,
               control_tooling_present, service_registration, boot_disposition}
        nodes_checked / nodes_validated: counts carried for reporting
    """

    description: ClassVar[str] = "Check IMEX service and tooling ship in the node image"

    #: Only a loaded definition passes. ``masked`` is a deployment-model
    #: mismatch rather than a missing package, so it is called out separately.
    _REGISTRATION_STATES: ClassVar[frozenset[str]] = frozenset({"loaded", "masked", "not_found", "error"})

    def run(self) -> None:
        """Check IMEX service/tooling presence and service-manager registration."""
        step_output = self.config.get("step_output", {})

        nodes = step_output.get("nodes")
        if error := _validate_node_reports(nodes, "IMEX service"):
            self.set_failed(error)
            return

        in_scope = [node for node in nodes if node.get("in_nvlink_allocation") is True]
        if not in_scope:
            # Counting examined nodes instead of asserted ones is exactly how a
            # run that skipped everything reports a pass, so fail loudly here.
            self.set_failed(
                f"No nodes were asserted against: {len(nodes)} node(s) reported, none marked as part of a "
                "multi-node NVLink allocation. Scope is set by the allocation, so zero asserted nodes is a "
                "failure rather than a pass"
            )
            return

        failures: list[str] = []
        for node in in_scope:
            node_id = node["node_id"]

            if node.get("service_present") is not True:
                failures.append(f"{node_id}: IMEX service not present in the node image")
            if node.get("control_tooling_present") is not True:
                failures.append(f"{node_id}: IMEX control tooling not present or not invocable")

            registration = node.get("service_registration")
            if not _is_non_empty_string(registration) or registration not in self._REGISTRATION_STATES:
                failures.append(
                    f"{node_id}: `service_registration` must be one of "
                    f"{sorted(self._REGISTRATION_STATES)}, got {registration!r}"
                )
            elif registration == "masked":
                failures.append(
                    f"{node_id}: IMEX service is masked with the node's service manager - a deployment-model "
                    "mismatch rather than a missing package"
                )
            elif registration != "loaded":
                failures.append(f"{node_id}: service manager reports registration {registration!r}, expected 'loaded'")

        if failures:
            self.set_failed(f"IMEX host-model checks failed on {len(failures)} count(s): {'; '.join(failures)}")
            return

        # Boot disposition is evidence only - whether IMEX starts at boot is
        # explicitly out of scope, so it is reported and never asserted on.
        dispositions = sorted({str(node.get("boot_disposition")) for node in in_scope if node.get("boot_disposition")})
        evidence = f" (boot disposition: {', '.join(dispositions)})" if dispositions else ""
        self.set_passed(
            f"IMEX service and control tooling present and registered on all {len(in_scope)} "
            f"in-scope node(s) of {len(nodes)} reported{evidence}"
        )


# The driver's own CRD. Its presence is what makes a cluster's multi-node NVLink
# capability "advertised" for scoping purposes.
COMPUTE_DOMAIN_CRD = "computedomains.resource.nvidia.com"
VENDOR_API_GROUP = "resource.nvidia.com"

# Device classes and per-node drivers are matched by role: a compute-domain name
# under the vendor's suffix. The exact names move with the driver version, the
# role they play does not.
COMPUTE_DOMAIN_ROLE = re.compile(r"^compute-domain[a-z0-9.-]*\.nvidia\.com$")

# Set by GPU feature discovery on nodes that belong to an NVLink clique; its
# value names the clique.
CLIQUE_LABEL = "nvidia.com/gpu.clique"
# GPU accounting. A DRA-only cluster need not expose the extended resource at
# all, so the feature-discovery label counts as well.
GPU_RESOURCE = "nvidia.com/gpu"
GPU_PRESENT_LABEL = "nvidia.com/gpu.present"

DEVICE_CLASS_RESOURCE = "deviceclasses.resource.k8s.io"
RESOURCE_SLICE_RESOURCE = "resourceslices.resource.k8s.io"


def _node_labels(node: dict[str, Any]) -> dict[str, Any]:
    """Return a node's labels, or an empty mapping when it carries none."""
    labels = node.get("metadata", {}).get("labels")
    return labels if isinstance(labels, dict) else {}


def _node_name(node: dict[str, Any]) -> str:
    """Return the node's name, or an empty string when it carries none."""
    name = node.get("metadata", {}).get("name")
    return name if isinstance(name, str) else ""


def _is_gpu_node(node: dict[str, Any]) -> bool:
    """Return whether the cluster accounts for GPUs on this node."""
    if _node_labels(node).get(GPU_PRESENT_LABEL) == "true":
        return True
    allocatable = node.get("status", {}).get("allocatable") or {}
    if not isinstance(allocatable, dict):
        return False
    try:
        return int(allocatable.get(GPU_RESOURCE, 0)) > 0
    except (TypeError, ValueError):
        return False


class ImexComputeDomainCapabilityCheck(BaseValidation):
    """Validate compute-domain capability is present on a cluster (DRA model).

    Asserts two cluster-side facts: the resource-allocation driver's
    compute-domain device classes are registered cluster-wide, and every GPU
    node in the NVLink clique publishes compute-domain resources through the
    driver's per-node plugin. The published resource is the evidence, not a
    running pod - the resource exists only once the plugin registered with the
    node agent and published successfully, which a pod in a Running state does
    not establish.

    Everything asserted is a cluster object, so the check reads the cluster API
    directly rather than through a provider step. Nothing here is provider
    specific and nothing is a provider's to report, which is also why no node
    list is accepted: scope must not be a provider's choice.

    Scope is every GPU node the cluster accounts for, never a node's
    self-report, so a GPU node arriving without the NVLink clique label fails
    rather than dropping out of the set - otherwise a node could report its way
    out of being tested. An empty clique-labelled set fails for the same
    reason: it is how the check would otherwise succeed vacuously on a cluster
    with no NVLink nodes at all.

    Nothing is asserted about the state of any host IMEX daemon. The driver
    supports two ownership modes, and an active host daemon is a defect under
    one and a requirement under the other, so the observed mode is reported as
    evidence only.

    A cluster that advertises no multi-node NVLink capability through the
    driver (the ComputeDomain CRD is not registered) is out of scope for
    SDN17-02 rather than failing it, and skips. That gate is deliberately a
    different object from the device classes the check asserts on, so the
    assertion cannot certify itself.
    """

    description: ClassVar[str] = "Check compute-domain capability is present cluster-side without tenant installation"

    def run(self) -> None:
        """Read compute-domain capability from the cluster and assert on it."""
        if not _multi_node_nvlink_advertised(self):
            return

        device_classes = self._compute_domain_device_classes()
        if device_classes is None:
            return

        published = self._publishing_nodes()
        if published is None:
            return

        nodes = self._gpu_nodes()
        if nodes is None:
            return

        reports = [_node_report(node, published) for node in sorted(nodes, key=_node_name) if _node_name(node)]

        if not any(report["clique_labelled"] for report in reports):
            # Reporting the examined count instead of the asserted one is how a
            # cluster with no NVLink nodes passes this check, so fail loudly.
            self.set_failed(
                f"No nodes were asserted against: {len(reports)} GPU node(s) reported, none carrying the NVLink "
                "clique label. Scope is the cluster's own GPU accounting, so zero asserted nodes is a failure "
                "rather than a pass"
            )
            return

        failures: list[str] = []
        if not device_classes:
            failures.append("the driver's compute-domain device classes are not registered cluster-wide")

        for report in reports:
            if not report["clique_labelled"]:
                failures.append(
                    f"{report['node_id']}: GPU node in scope carries no NVLink clique label - a node in a "
                    "multi-node NVLink cluster cannot report its way out of scope"
                )
            elif not report["published"]:
                failures.append(
                    f"{report['node_id']}: the driver's per-node plugin publishes no compute-domain resources"
                )

        if failures:
            self.set_failed(
                f"Compute-domain capability checks failed on {len(failures)} count(s): {'; '.join(failures)}"
            )
            return

        # Every node is clique-labelled by this point: an unlabelled one is a
        # failure above rather than a node that left the asserted set.
        self.set_passed(
            f"Compute-domain device classes are registered and all {len(reports)} clique-labelled GPU "
            f"node(s) publish compute-domain resources "
            f"(IMEX daemon ownership: {_ownership_mode(device_classes)})"
        )

    def _compute_domain_device_classes(self) -> list[str] | None:
        """Return the registered compute-domain device class names."""
        items = _listing(self, DEVICE_CLASS_RESOURCE)
        if items is None:
            return None
        return [name for name in names_from_items(items) if COMPUTE_DOMAIN_ROLE.fullmatch(name)]

    def _publishing_nodes(self) -> set[str] | None:
        """Return the nodes the compute-domain per-node plugin has published for."""
        items = _listing(self, RESOURCE_SLICE_RESOURCE)
        if items is None:
            return None
        published: set[str] = set()
        for slice_ in items:
            spec = slice_.get("spec")
            if not isinstance(spec, dict):
                continue
            driver = spec.get("driver")
            node_name = spec.get("nodeName")
            if isinstance(driver, str) and COMPUTE_DOMAIN_ROLE.fullmatch(driver) and isinstance(node_name, str):
                published.add(node_name)
        return published

    def _gpu_nodes(self) -> list[dict[str, Any]] | None:
        """Return every GPU node the cluster accounts for.

        Scope comes from the cluster's own GPU accounting rather than from a
        node's view of its NVLink support, so a clique-less GPU node is still
        examined.
        """
        items = _listing(self, "nodes")
        if items is None:
            return None
        return [node for node in items if _is_gpu_node(node)]


def _node_report(node: dict[str, Any], published: set[str]) -> dict[str, Any]:
    """Return one node's compute-domain facts as the check asserts on them."""
    clique = _node_labels(node).get(CLIQUE_LABEL)
    return {
        "node_id": _node_name(node),
        "clique_labelled": bool(isinstance(clique, str) and clique.strip()),
        "published": _node_name(node) in published,
    }


def _ownership_mode(device_classes: list[str]) -> str:
    """Return the observed IMEX daemon ownership mode.

    The driver runs the daemons itself only where it also offers a device class
    for them; a cluster whose daemons are managed on the host registers the
    channel class alone. Evidence only - no assertion rests on this.
    """
    return "driver" if any("daemon" in name for name in device_classes) else "host"


def _multi_node_nvlink_advertised(validation: BaseValidation) -> bool:
    """Return whether the cluster advertises multi-node NVLink through the driver.

    Skips outright when it does not. An unserved API group is an empty
    successful listing, so a failed read is a cluster the check could not
    reach - which must never be mistaken for a cluster that offers no
    multi-node NVLink, and fails instead.
    """
    result = validation.run_command(
        get_kubectl_base_shell("api-resources", f"--api-group={VENDOR_API_GROUP}", "-o", "name")
    )
    if result.exit_code != 0:
        validation.set_failed(f"Failed to read the {VENDOR_API_GROUP} API group: {command_detail(result)}")
        return False
    if COMPUTE_DOMAIN_CRD not in result.stdout.split():
        pytest.skip(
            "Cluster advertises no multi-node NVLink capability through the driver "
            f"({COMPUTE_DOMAIN_CRD} is not registered)"
        )
    return True


def _listing(validation: BaseValidation, resource: str, *args: str) -> list[dict[str, Any]] | None:
    """Return one ``kubectl get`` listing, or None after failing the validation."""
    result = validation.run_command(get_kubectl_base_shell("get", resource, *args, "-o", "json"))
    return kubectl_items_or_fail(validation, result, resource)


# The compute-domain half of the driver, as cluster objects.
COMPUTE_DOMAIN_RESOURCE = "computedomains.resource.nvidia.com"
COMPUTE_DOMAIN_API_VERSION = "resource.nvidia.com/v1beta1"

# The controller stamps every object it creates for a domain with that domain's
# UID, which is what ties a daemon pod back to one ComputeDomain.
COMPUTE_DOMAIN_LABEL = "resource.nvidia.com/computeDomain"

# The chart plumbs the configured ownership mode onto the controller as an
# environment variable. Reading it there is the driver declaring which daemon
# lifecycle it implements - a different thing from looking at what is running.
COMPUTE_DOMAIN_CONTROLLER_COMMAND = "compute-domain-controller"
IMEX_MODE_ENV = "IMEX_MODE"
DRIVER_MANAGED_MODE = "driverManaged"
HOST_MANAGED_MODE = "hostManaged"

READY = "Ready"

# Namespaces are DNS-1123 labels, and an image reference carries no whitespace
# or quoting. Both are rejected before they reach a manifest rather than being
# quoted into one.
_DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_IMAGE_REFERENCE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$")

_COMPUTE_DOMAIN_MANIFEST = """\
apiVersion: {api_version}
kind: ComputeDomain
metadata:
  name: {name}
  namespace: {namespace}
spec:
  numNodes: 0
  channel:
    resourceClaimTemplate:
      name: {name}-channel
"""

# One claiming pod per clique node. The driver places no daemon until a channel
# claim is prepared, so this is what gives the check a subject at all - see
# _claim_channels. The container only has to hold its claim open, so it sleeps;
# nothing is asserted about it.
_CHANNEL_CLAIM_MANIFEST = """\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: {name}-claim
  namespace: {namespace}
spec:
  selector:
    matchLabels:
      app: {name}-claim
  template:
    metadata:
      labels:
        app: {name}-claim
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: {clique_label}
                operator: Exists
      tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
      containers:
      - name: claim
        image: {image}
        command: ["sh", "-c", "sleep {hold_seconds}"]
        resources:
          claims:
          - name: imex-channel
      resourceClaims:
      - name: imex-channel
        resourceClaimTemplateName: {name}-channel
"""


class _DomainState(NamedTuple):
    """One observation of a compute domain and the daemons serving it.

    ``members`` maps a node the domain accounts for to the readiness the domain
    reports for it; ``daemons`` maps a node to the domain's daemon pod there.
    The two are read separately on purpose - a node the domain calls ready with
    no daemon behind it is a defect, not a rounding error.
    """

    uid: str
    members: dict[str, str]
    daemons: dict[str, dict[str, Any]]


def _pod_node(pod: dict[str, Any]) -> str:
    """Return the node a pod is bound to, or an empty string when unbound."""
    node = (pod.get("spec") or {}).get("nodeName")
    return node if isinstance(node, str) else ""


def _pod_is_ready(pod: dict[str, Any]) -> bool:
    """Return whether a pod reports a Ready condition."""
    conditions = (pod.get("status") or {}).get("conditions") or []
    return any(
        isinstance(condition, dict) and condition.get("type") == READY and condition.get("status") == "True"
        for condition in conditions
    )


def _live_daemons_by_node(pods: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the domain's daemon pod per node, skipping pods being torn down.

    A terminating pod must never stand in for the replacement a recovery poll
    is waiting on, which is the whole reason the deletion timestamp is honoured
    here rather than the pod phase.
    """
    by_node: dict[str, dict[str, Any]] = {}
    for pod in pods:
        metadata = pod.get("metadata") or {}
        if not isinstance(metadata, dict) or metadata.get("deletionTimestamp"):
            continue
        node = _pod_node(pod)
        if node:
            by_node[node] = pod
    return by_node


def _container_env(container: dict[str, Any], name: str) -> str | None:
    """Return a container's literal value for ``name``, or None when unset."""
    for entry in container.get("env") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            value = entry.get("value")
            return value if isinstance(value, str) else None
    return None


class ImexDaemonRecoveryCheck(BaseValidation):
    """Validate the driver-managed IMEX daemon runs unaided and recovers (DRA model).

    Asserts two things about an allocated compute domain: every node the domain
    accounts for is served by a running daemon *and* reported as a ready
    member, with nothing the test started; and that killing one of those
    daemons is repaired by the driver, back to ready membership, unaided and
    within a bounded time.

    Allocating the domain and running a workload in it are the tenant's
    documented actions, and are setup here. Starting an IMEX daemon is not: the
    daemons exist because the driver created them in response to that, so the
    check never starts, restarts, or repairs one. A domain that does not come
    up on its own fails rather than being nursed into shape.

    The allocation is a compute domain *and* a claiming pod per clique node,
    because a domain on its own has no daemons to assert against: the driver's
    per-domain DaemonSet selects on a node label the kubelet plugin applies
    while preparing a channel claim, so an unclaimed domain keeps a DaemonSet
    of size zero indefinitely.

    Membership is read from what the domain reports about a node, not from the
    daemon pod's phase. A replacement pod that reaches Running but never
    rejoins the domain is the exact defect this exists to catch, so the two are
    observed separately and reported separately: never rescheduled indicts the
    controller, rescheduled but never a ready member indicts domain formation.

    Scope is the domain's own accounting of its nodes. Zero members is a
    failure rather than a vacuous pass, and a member that recovery could not be
    asserted against is not quietly dropped from the count.

    Termination is a deletion of the daemon in place. The node is never
    cordoned, drained, or removed from the domain - those are deliberate
    departures, which a correct controller declines to repair, so a
    departure-based reading of this test would fail on a healthy cluster.

    Population is clusters where the driver owns the daemon lifecycle, read
    from the mode the driver declares. A cluster configured to defer to an
    operator-run host daemon creates no per-domain daemon and so has no subject
    here, and skips. Nothing is asserted about any host daemon, including where
    one legitimately exists.

    This check is destructive: it allocates a compute domain, deletes a daemon
    inside it, and releases the domain afterwards. Run it on an idle cluster.

    Config:
        namespace: Namespace to allocate the compute domain in (default "default")
        image: Image for the claiming pods (default "busybox:1.36"); it only
            has to hold a channel claim open
        formation_timeout_seconds: Bound on the domain coming up (default 300)
        recovery_timeout_seconds: Bound on recovery; derived from the observed
            formation time when unset
    """

    description: ClassVar[str] = "Check the driver-managed IMEX daemon runs unaided and recovers after termination"

    #: Recovering one node cannot reasonably be slower than forming the whole
    #: domain, so the recovery budget is derived from the formation this run
    #: observed rather than picked round. The floor covers a domain that formed
    #: almost instantly, where a small multiple of it would be no budget at all.
    _RECOVERY_BUDGET_MULTIPLIER: ClassVar[float] = 2.0
    _RECOVERY_FLOOR_SECONDS: ClassVar[int] = 60
    _POLL_INTERVAL_SECONDS: ClassVar[int] = 5
    _READ_TIMEOUT_SECONDS: ClassVar[int] = 30
    #: Deletions here wait for completion, so they cannot share the read
    #: timeout: a pod's default termination grace period is 30s by itself, and
    #: the domain is removed behind a finalizer. Bounded, but well clear of
    #: both, so a slow-but-healthy teardown is not reported as a failure.
    _DELETE_TIMEOUT_SECONDS: ClassVar[int] = 120

    def run(self) -> None:
        """Allocate a compute domain, kill one daemon, and assert it is restored."""
        if not _multi_node_nvlink_advertised(self):
            return

        mode = self._declared_ownership_mode()
        if mode is None:
            return
        if mode == HOST_MANAGED_MODE:
            pytest.skip(
                "Driver defers the IMEX daemon to an operator-run host service "
                f"({IMEX_MODE_ENV}={HOST_MANAGED_MODE}), so it owns no daemon to assert on"
            )

        namespace = self.config.get("namespace", "default")
        if not (isinstance(namespace, str) and _DNS_LABEL.fullmatch(namespace)):
            self.set_failed(f"`namespace` must be a DNS-1123 label, got {namespace!r}")
            return

        image = self.config.get("image", "busybox:1.36")
        if not (isinstance(image, str) and _IMAGE_REFERENCE.fullmatch(image)):
            self.set_failed(f"`image` must be an image reference, got {image!r}")
            return

        formation_timeout = self._parse_positive_int("formation_timeout_seconds", default=300)
        if formation_timeout is None:
            return

        name = f"isv-sdn18-02-{uuid.uuid4().hex[:8]}"
        if not self._apply(
            _COMPUTE_DOMAIN_MANIFEST.format(api_version=COMPUTE_DOMAIN_API_VERSION, name=name, namespace=namespace),
            f"compute domain {name}",
        ):
            return
        try:
            if self._claim_channels(namespace, name, image, formation_timeout):
                self._assert_unaided_recovery(namespace, name, formation_timeout)
        finally:
            self._release(namespace, name)

    def _declared_ownership_mode(self) -> str | None:
        """Return the daemon ownership mode the driver declares, or None on failure.

        The mode is read from the controller's own configuration. The absence
        of a daemon is deliberately not consulted: a broken driver-owned
        deployment looks exactly like a cluster that never had one, and
        inferring from it would report the first as the second.
        """
        deployments = _listing(self, "deployments", "--all-namespaces")
        if deployments is None:
            return None

        for deployment in deployments:
            containers = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
            for container in containers.get("containers") or []:
                if not isinstance(container, dict):
                    continue
                if COMPUTE_DOMAIN_CONTROLLER_COMMAND not in (container.get("command") or []):
                    continue
                # A driver predating the mode being configurable declares none,
                # and driver-managed is the only lifecycle it implements.
                declared = _container_env(container, IMEX_MODE_ENV) or DRIVER_MANAGED_MODE
                if declared not in (DRIVER_MANAGED_MODE, HOST_MANAGED_MODE):
                    self.set_failed(
                        f"Driver declares an unrecognised IMEX daemon ownership mode {declared!r}, "
                        f"expected one of {DRIVER_MANAGED_MODE!r} or {HOST_MANAGED_MODE!r}"
                    )
                    return None
                return declared

        self.set_failed(
            "Could not establish the IMEX daemon ownership mode: no compute-domain controller was found to "
            "declare one. The mode has to come from the driver's own configuration, since the absence of a "
            "daemon is not evidence that the driver defers to a host-managed one"
        )
        return None

    def _apply(self, manifest: str, what: str) -> bool:
        """Create one object from a manifest, or fail the check naming it."""
        apply = get_kubectl_base_shell("apply", "-f", "-")
        result = self.run_command(f"printf '%s' {shlex.quote(manifest)} | {apply}", timeout=self._READ_TIMEOUT_SECONDS)
        if result.exit_code != 0:
            self.set_failed(f"Failed to create {what}: {command_detail(result)}")
            return False
        return True

    def _claim_channels(self, namespace: str, name: str, image: str, formation_timeout: int) -> bool:
        """Run one claiming pod per clique node so the driver places daemons.

        Without a prepared channel claim the driver's per-domain DaemonSet
        selects on a node label nobody has set and stays at size zero however
        long the check waits, leaving the domain with no daemons to assert
        against. These pods only hold their claims open - nothing is asserted
        about them, and they are expected to sit in ContainerCreating until the
        daemon on their node reports ready.
        """
        return self._apply(
            _CHANNEL_CLAIM_MANIFEST.format(
                name=name,
                namespace=namespace,
                image=image,
                clique_label=CLIQUE_LABEL,
                # Outlast the worst case the check itself allows - formation
                # plus a recovery budget of twice it - so a claim never lapses
                # mid-run and takes the daemons down with it, while still
                # expiring on its own if this process is killed outright.
                hold_seconds=formation_timeout * 4,
            ),
            f"channel claims for compute domain {name}",
        )

    def _release(self, namespace: str, name: str) -> None:
        """Delete the claims and the domain, failing a passing check on leftovers.

        Teardown is mandatory rather than best-effort: the check created these,
        and leaving them behind changes what the next run observes. The claims
        go first so no daemon is still holding a prepared channel when the
        domain is removed. An already-failing check keeps its own message,
        which is the more useful one.
        """
        leftovers: list[str] = []
        for resource, obj in (("daemonset", f"{name}-claim"), (COMPUTE_DOMAIN_RESOURCE, name)):
            result = self.run_command(
                get_kubectl_base_shell("delete", resource, obj, "-n", namespace, "--ignore-not-found=true"),
                timeout=self._DELETE_TIMEOUT_SECONDS,
            )
            if result.exit_code != 0:
                leftovers.append(f"{resource}/{obj}: {command_detail(result)}")

        if leftovers and self.passed:
            self.set_failed(f"Could not release what the check created: {'; '.join(leftovers)}")

    def _assert_unaided_recovery(self, namespace: str, name: str, formation_timeout: int) -> None:
        """Assert unaided presence, then terminate one daemon and assert recovery."""
        formed = self._await_formation(namespace, name, formation_timeout)
        if formed is None:
            return
        state, formation_seconds = formed

        self.report_subtest(
            "unaided_presence",
            True,
            f"All {len(state.members)} domain member(s) served by a running daemon and reported ready "
            f"{formation_seconds:.0f}s after allocation, none started by the test",
            duration=formation_seconds,
        )

        target = sorted(state.members)[0]
        target_uid = ((state.daemons[target].get("metadata") or {}).get("uid")) or ""
        if not self._terminate(namespace, state.daemons[target]):
            return

        budget = self._recovery_budget(formation_seconds)
        if budget is None:
            return
        self._await_recovery(namespace, name, target, target_uid, budget, len(state.members))

    def _recovery_budget(self, formation_seconds: float) -> int | None:
        """Return the recovery timeout, derived from formation unless configured."""
        if self.config.get("recovery_timeout_seconds") is not None:
            return self._parse_positive_int("recovery_timeout_seconds", default=0)
        derived = int(formation_seconds * self._RECOVERY_BUDGET_MULTIPLIER)
        return max(self._RECOVERY_FLOOR_SECONDS, derived)

    def _terminate(self, namespace: str, pod: dict[str, Any]) -> bool:
        """Delete one daemon in place, confirming the termination took effect.

        The node itself is left alone. Cordoning, draining, or shrinking the
        domain would be a deliberate departure, which a correct controller does
        not repair.
        """
        metadata = pod.get("metadata") or {}
        pod_name = metadata.get("name") or ""
        pod_namespace = metadata.get("namespace") or namespace
        result = self.run_command(
            get_kubectl_base_shell("delete", "pod", pod_name, "-n", pod_namespace, "--wait=true"),
            timeout=self._DELETE_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            self.set_failed(f"Failed to terminate daemon {pod_name}: {command_detail(result)}")
            return False
        self.report_subtest("terminate", True, f"Deleted daemon {pod_name} in place")
        return True

    def _await_formation(self, namespace: str, name: str, timeout: int) -> tuple[_DomainState, float] | None:
        """Wait for every domain member to be served by a ready daemon.

        Returns the settled state and how long it took, which is the observed
        reconciliation baseline the recovery budget is derived from. A domain
        that never gets there fails: a daemon not running on arrival indicts
        the driver deployment, and is not a setup step for the check to repair.
        """
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        detail = "the domain reported no members"
        while True:
            state, error = self._observe(namespace, name)
            if state is not None:
                if not state.members:
                    detail = "the domain reported no members, so there was nothing to assert against"
                else:
                    unserved = sorted(node for node in state.members if node not in state.daemons)
                    unready = sorted(
                        node
                        for node, status in state.members.items()
                        if node in state.daemons and (status != READY or not _pod_is_ready(state.daemons[node]))
                    )
                    if not unserved and not unready:
                        return state, time.monotonic() - started
                    detail = _formation_detail(unserved, unready)
            else:
                detail = error

            if time.monotonic() >= deadline:
                self.set_failed(
                    f"Compute domain {name} did not come up unaided within {timeout}s: {detail}. The driver "
                    "is responsible for starting these daemons, so this indicts the driver deployment"
                )
                return None
            time.sleep(self._POLL_INTERVAL_SECONDS)

    def _await_recovery(
        self,
        namespace: str,
        name: str,
        target: str,
        terminated_uid: str,
        timeout: int,
        members: int,
    ) -> None:
        """Wait for the terminated daemon to be replaced and rejoin the domain."""
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        rescheduled_at: float | None = None
        while True:
            state, error = self._observe(namespace, name)
            elapsed = time.monotonic() - started
            if state is not None:
                replacement = state.daemons.get(target, {})
                replacement_uid = ((replacement.get("metadata") or {}).get("uid")) or ""
                rescheduled = bool(replacement_uid) and replacement_uid != terminated_uid
                if rescheduled and rescheduled_at is None:
                    rescheduled_at = elapsed
                if rescheduled and _pod_is_ready(replacement) and state.members.get(target) == READY:
                    self._report_recovered(target, elapsed, rescheduled_at or elapsed, members, timeout)
                    return

            if time.monotonic() >= deadline:
                self._report_not_recovered(target, rescheduled_at, timeout, error if state is None else "")
                return
            time.sleep(self._POLL_INTERVAL_SECONDS)

    def _report_recovered(self, target: str, elapsed: float, rescheduled_at: float, members: int, budget: int) -> None:
        """Record a successful recovery, with the elapsed time the ticket asks for."""
        self.report_subtest("daemon_rescheduled", True, f"{target}: replacement daemon ready", duration=rescheduled_at)
        self.report_subtest("domain_member", True, f"{target}: ready member again", duration=elapsed)
        self.set_passed(
            f"All {members} domain member(s) ran a driver-started daemon on arrival, and {target} was "
            f"restored to ready domain membership {elapsed:.0f}s after its daemon was terminated "
            f"(budget {budget}s), with no intervention"
        )

    def _report_not_recovered(self, target: str, rescheduled_at: float | None, budget: int, read_error: str) -> None:
        """Fail a recovery that ran out of budget, naming what did not happen.

        The two failures indict different components, so they never share a
        message: a daemon that was never put back is the controller's, and one
        that came back but never rejoined is domain formation's.
        """
        if rescheduled_at is None:
            self.report_subtest("daemon_rescheduled", False, f"{target}: no replacement daemon within {budget}s")
            reason = (
                f"the driver never rescheduled a daemon onto {target} within {budget}s, which indicts the controller"
            )
        else:
            self.report_subtest(
                "daemon_rescheduled", True, f"{target}: replacement daemon appeared", duration=rescheduled_at
            )
            self.report_subtest("domain_member", False, f"{target}: never became a ready member again")
            reason = (
                f"a replacement daemon appeared on {target} after {rescheduled_at:.0f}s but the node never "
                f"became a ready domain member within {budget}s, which indicts domain formation rather than "
                "the controller"
            )
        detail = f" (last read: {read_error})" if read_error else ""
        self.set_failed(f"IMEX daemon termination on {target} was not repaired unaided: {reason}{detail}")

    def _observe(self, namespace: str, name: str) -> tuple[_DomainState | None, str]:
        """Read the domain and its daemons once.

        Returns ``(state, "")`` on success, or ``(None, error)`` when the
        cluster could not be read. A read failure mid-poll is not fatal on its
        own - the surrounding deadline decides that - but it is carried so a
        timeout can name it instead of reporting a bare elapsed time.
        """
        result = self.run_command(
            get_kubectl_base_shell("get", COMPUTE_DOMAIN_RESOURCE, name, "-n", namespace, "-o", "json"),
            timeout=self._READ_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            return None, f"could not read compute domain {name}: {command_detail(result)}"
        domain = kubectl_payload_or_none(result)
        if domain is None:
            return None, f"could not parse compute domain {name}"

        uid = ((domain.get("metadata") or {}).get("uid")) or ""
        if not uid:
            return None, f"compute domain {name} carries no UID to match its daemons by"

        members: dict[str, str] = {}
        for node in (domain.get("status") or {}).get("nodes") or []:
            if isinstance(node, dict) and _is_non_empty_string(node.get("name")):
                members[str(node["name"])] = str(node.get("status") or "")

        pods = self.run_command(
            get_kubectl_base_shell(
                "get", "pods", "--all-namespaces", "-l", f"{COMPUTE_DOMAIN_LABEL}={uid}", "-o", "json"
            ),
            timeout=self._READ_TIMEOUT_SECONDS,
        )
        if pods.exit_code != 0:
            return None, f"could not read the daemons of compute domain {name}: {command_detail(pods)}"
        items = kubectl_items_or_empty(pods)
        return _DomainState(uid=uid, members=members, daemons=_live_daemons_by_node(items)), ""


def _formation_detail(unserved: list[str], unready: list[str]) -> str:
    """Describe which members are missing a daemon and which are not yet ready."""
    parts = []
    if unserved:
        parts.append(f"no daemon on {', '.join(unserved)}")
    if unready:
        parts.append(f"not ready on {', '.join(unready)}")
    return "; ".join(parts)


class ByoipCheck(BaseValidation):
    """Validate Bring-Your-Own-IP (BYOIP) with non-conflicting custom CIDRs.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with custom_cidr_create, custom_cidr_verify,
               standard_cidr_create, no_conflict, custom_cidr_subnet
    """

    description: ClassVar[str] = "Check BYOIP support"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required = [
            "custom_cidr_create",
            "custom_cidr_verify",
            "standard_cidr_create",
            "no_conflict",
            "custom_cidr_subnet",
        ]
        failed = []

        for test_name in required:
            test_result = tests.get(test_name, {})
            if not test_result.get("passed"):
                error = test_result.get("error", "test not found")
                failed.append(f"{test_name}: {error}")

        if failed:
            self.set_failed(f"BYOIP tests failed: {'; '.join(failed)}")
        else:
            cidr = tests.get("custom_cidr_create", {}).get("cidr", "N/A")
            self.set_passed(f"BYOIP validated with custom CIDR {cidr}")


class StablePrivateIpCheck(BaseValidation):
    """Validate private IP stability across instance stop/start.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_instance, record_ip, stop_instance,
               start_instance, ip_unchanged
    """

    description: ClassVar[str] = "Check private IP stability"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required = [
            "create_instance",
            "record_ip",
            "stop_instance",
            "start_instance",
            "ip_unchanged",
        ]
        failed = []

        for test_name in required:
            test_result = tests.get(test_name, {})
            if not test_result.get("passed"):
                error = test_result.get("error", "test not found")
                failed.append(f"{test_name}: {error}")

        if failed:
            self.set_failed(f"Stable IP tests failed: {'; '.join(failed)}")
        else:
            ip_result = tests.get("ip_unchanged", {})
            ip = ip_result.get("ip_before", "N/A")
            self.set_passed(f"Private IP {ip} stable across stop/start")


class StorageL3RoutingCheck(BaseValidation):
    """Validate all-to-all L3 routing between storage hosts (SDN08-01).

    Storage hosts spread across multiple subnets of one software-defined private
    network must reach every other host over L3 (full mesh), with traffic routed
    on the VPC's local route rather than through a gateway. This is the inverse of
    the SDN04 isolation checks: it asserts reachability, not blocking.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with distinct_subnets, all_to_all_reachable,
               cross_subnet_routing, no_gateway_hop
    """

    description: ClassVar[str] = "Check all-to-all L3 routing between storage hosts"

    def run(self) -> None:
        """Validate required L3 routing subtests and report full-mesh reachability."""
        required = [
            "distinct_subnets",
            "all_to_all_reachable",
            "cross_subnet_routing",
            "no_gateway_hop",
        ]
        if not check_required_tests(self, required, "Storage L3 routing tests failed"):
            return

        tests = self.config.get("step_output", {}).get("tests", {})
        mesh = tests.get("all_to_all_reachable", {})
        subnet_count = tests.get("distinct_subnets", {}).get("subnet_count", "N/A")
        pairs = mesh.get("pairs_reachable", "N/A")
        total = mesh.get("pairs_tested", "N/A")
        self.set_passed(
            f"Full-mesh L3 routing across {subnet_count} subnets ({pairs}/{total} host pairs reachable, no gateway hop)"
        )


class StableEgressIpCheck(BaseValidation):
    """Validate egress IP stability across repeated probes (DMS05-01).

    NVIDIA cloud services use IP allowlists, so workloads that call out to
    them must present a stable egress IP. A provider script launches a
    test instance, probes its egress IP N times against an external
    IP-discovery endpoint (e.g., https://api.ipify.org), and reports
    whether every probe returned the same address.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_instance, probe_egress_ip, egress_ip_stable
    """

    description: ClassVar[str] = "Check egress IP stability across probes"

    def run(self) -> None:
        """Validate stable egress IP subtest results and record the outcome."""
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required = [
            "create_instance",
            "probe_egress_ip",
            "egress_ip_stable",
        ]
        failed = []

        for test_name in required:
            test_result = tests.get(test_name, {})
            if not test_result.get("passed"):
                error = test_result.get("error", "test not found")
                failed.append(f"{test_name}: {error}")

        if failed:
            self.set_failed(f"Stable egress IP tests failed: {'; '.join(failed)}")
        else:
            probe_result = tests.get("probe_egress_ip", {})
            probes = probe_result.get("probes")
            if probes is None:
                self.set_failed("Malformed stable egress IP step output: missing probe_egress_ip.probes")
            else:
                self.set_passed(f"Egress IP stable across {probes} probes")


class FloatingIpCheck(BaseValidation):
    """Validate floating IP can be atomically switched between instances.

    Config:
        step_output: The step output to check
        max_switch_seconds: Maximum allowed switch time (default: 10)

    Step output:
        tests: dict with allocate_eip, associate_to_a, verify_on_a,
               reassociate_to_b, verify_on_b, verify_not_on_a
    """

    description: ClassVar[str] = "Check floating IP switch"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})
        max_seconds = self.config.get("max_switch_seconds", 10)

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required = [
            "allocate_eip",
            "associate_to_a",
            "verify_on_a",
            "reassociate_to_b",
            "verify_on_b",
            "verify_not_on_a",
        ]
        failed = []

        for test_name in required:
            test_result = tests.get(test_name, {})
            if not test_result.get("passed"):
                error = test_result.get("error", "test not found")
                failed.append(f"{test_name}: {error}")

        # Extra check: switch time
        switch_time = tests.get("reassociate_to_b", {}).get("switch_seconds")
        if switch_time is not None and switch_time > max_seconds:
            failed.append(f"reassociate_to_b: switch took {switch_time}s, limit is {max_seconds}s")

        if failed:
            self.set_failed(f"Floating IP tests failed: {'; '.join(failed)}")
        else:
            eip = tests.get("allocate_eip", {}).get("public_ip", "N/A")
            self.set_passed(f"Floating IP {eip} switched in {switch_time}s (limit: {max_seconds}s)")


class LocalizedDnsCheck(BaseValidation):
    """Validate localized DNS with custom internal domain resolution.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_vpc_with_dns, create_hosted_zone,
               create_dns_record, verify_dns_settings, resolve_record
    """

    description: ClassVar[str] = "Check localized DNS"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required = [
            "create_vpc_with_dns",
            "create_hosted_zone",
            "create_dns_record",
            "verify_dns_settings",
            "resolve_record",
        ]
        failed = []

        for test_name in required:
            test_result = tests.get(test_name, {})
            if not test_result.get("passed"):
                error = test_result.get("error", "test not found")
                failed.append(f"{test_name}: {error}")

        if failed:
            self.set_failed(f"DNS tests failed: {'; '.join(failed)}")
        else:
            fqdn = tests.get("create_dns_record", {}).get("fqdn", "N/A")
            resolved = tests.get("resolve_record", {}).get("resolved_ip", "N/A")
            self.set_passed(f"DNS resolution: {fqdn} -> {resolved}")


class VpcPeeringCheck(BaseValidation):
    """Validate VPC peering - create peering, add routes, verify connectivity.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with create_vpc_a, create_vpc_b, create_peering,
               accept_peering, add_routes, peering_active
        vpc_a, vpc_b: VPC info
    """

    description: ClassVar[str] = "Check VPC peering"

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required = [
            "create_vpc_a",
            "create_vpc_b",
            "create_peering",
            "accept_peering",
            "add_routes",
            "peering_active",
        ]
        failed = []

        for test_name in required:
            test_result = tests.get(test_name, {})
            if not test_result.get("passed"):
                error = test_result.get("error", "test not found")
                failed.append(f"{test_name}: {error}")

        if failed:
            self.set_failed(f"Peering tests failed: {'; '.join(failed)}")
        else:
            vpc_a = step_output.get("vpc_a", {}).get("id", "?")
            vpc_b = step_output.get("vpc_b", {}).get("id", "?")
            self.set_passed(f"VPC peering active: {vpc_a} <-> {vpc_b}")

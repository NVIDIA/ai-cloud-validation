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
import re
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    import paramiko

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required_tests = ["create_vpc", "read_vpc", "update_tags", "update_dns", "delete_vpc"]
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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network", "security"]

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
    markers: ClassVar[list[str]] = ["network", "security"]

    def run(self) -> None:
        step_output = self.config.get("step_output", {})
        tests = step_output.get("tests", {})

        if not tests:
            self.set_failed("No 'tests' in step output")
            return

        required_tests = [
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
    markers: ClassVar[list[str]] = ["network", "security"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network", "ssh"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network", "security"]

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
    markers: ClassVar[list[str]] = ["network", "security"]

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
    markers: ClassVar[list[str]] = ["network", "security"]

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
    markers: ClassVar[list[str]] = ["network", "security"]

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
    markers: ClassVar[list[str]] = ["network", "security"]

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
    markers: ClassVar[list[str]] = ["network", "security"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

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


class ByoipCheck(BaseValidation):
    """Validate Bring-Your-Own-IP (BYOIP) with non-conflicting custom CIDRs.

    Config:
        step_output: The step output to check

    Step output:
        tests: dict with custom_cidr_create, custom_cidr_verify,
               standard_cidr_create, no_conflict, custom_cidr_subnet
    """

    description: ClassVar[str] = "Check BYOIP support"
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

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
    markers: ClassVar[list[str]] = ["network"]

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

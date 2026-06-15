# Network Validation Guide (AWS)

This guide provides a complete walkthrough for validating AWS VPC networking capabilities using the AI Cloud Validation framework. These tests verify software-defined networking overlays and fabric behavior for compute-on-demand requirements.

## Overview

The network validation suite includes 14 comprehensive test suites:

| Test Suite | Duration | Description |
|------------|----------|-------------|
| **VPC CRUD** | ~30s | Create, read, update, delete VPC lifecycle |
| **Subnet Config** | ~30s | Multi-AZ subnet distribution and route tables |
| **VPC Isolation** | ~30s | Security boundaries between separate VPCs |
| **SG CRUD** | ~30s | Security group create, read, update rules, delete lifecycle |
| **Security Blocking** | ~30s | SG/NACL blocking rules (negative tests) |
| **Connectivity** | ~3 min | Instance network assignment via SSM |
| **Traffic Validation** | ~5-7 min | Real ping tests - allowed/blocked traffic |
| **VPC IP Config** | ~10s | DHCP options, subnet CIDRs, auto-assign IP (DDI) |
| **DHCP IP Management** | ~3 min | DHCP lease, IP match, DNS options via SSH (DDI) |
| **BYOIP** | ~30s | Bring-Your-Own-IP with custom CIDRs |
| **Stable Private IP** | ~5 min | IP persistence across stop/start |
| **Floating IP** | ~5 min | Atomic IP switch between instances (<10s) |
| **Localized DNS** | ~60s | Custom internal domain resolution |
| **VPC Peering** | ~30s | Cross-VPC connectivity with full bandwidth |

**Total runtime**: ~25-30 minutes for all tests

**Key Features:**

- All tests are **SELF-CONTAINED** - they create their own VPCs and clean up after
- **No pre-existing infrastructure required** - just AWS credentials
- **Platform-agnostic validations** - scripts output JSON, validations check results
- Includes **negative test cases** to verify security controls work correctly
- **Real traffic tests** using AWS SSM to run actual ping commands

## Architecture

### Step-Based Execution Model

```text
┌─────────────────────────────────────────────────────────────────┐
│                    AWS Network Validation Suite                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Scripts (boto3)                Validations (JSON check)        │
│  ┌────────────────────────┐    ┌──────────────────────────┐     │
│  │ vpc_crud_test.py       │───▶│ VpcCrudCheck             │     │
│  │ subnet_test.py         │───▶│ SubnetConfigCheck        │     │
│  │ isolation_test.py      │───▶│ VpcIsolationCheck        │     │
│  │ sg_crud_test.py        │───▶│ SgCrudCheck              │     │
│  │ security_test.py       │───▶│ SecurityBlockingCheck    │     │
│  │ test_connectivity.py   │───▶│ NetworkConnectivityCheck │     │
│  │ traffic_test.py        │───▶│ TrafficFlowCheck         │     │
│  │ vpc_ip_config_test.py  │───▶│ VpcIpConfigCheck         │     │
│  │ dhcp_ip_test.py        │───▶│ DhcpIpManagementCheck    │     │
│  │ byoip_test.py          │───▶│ ByoipCheck               │     │
│  │ stable_ip_test.py      │───▶│ StablePrivateIpCheck     │     │
│  │ floating_ip_test.py    │───▶│ FloatingIpCheck          │     │
│  │ dns_test.py            │───▶│ LocalizedDnsCheck        │     │
│  │ peering_test.py        │───▶│ VpcPeeringCheck          │     │
│  └────────────────────────┘    └──────────────────────────┘     │
│                                                                 │
│  Platform-specific            Platform-agnostic                 │
│  (AWS boto3 SDK)              (checks JSON output)              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Traffic Validation Test Architecture

The most comprehensive test actually sends real network traffic:

```text
┌─────────────────────────────────────────────────────────────────┐
│                          Test VPC                               │
│                                                                 │
│  ┌─────────────┐   ping (ok)     ┌────────────────────────────┐ │
│  │   Source    │ ───────────────▶│ Target (SG allows ICMP)    │ │
│  │ (SSM agent) │                 └────────────────────────────┘ │
│  │             │  ping (blocked) ┌────────────────────────────┐ │
│  │             │ ───────────────▶│ Target (SG blocks ICMP)    │ │
│  │             │                 └────────────────────────────┘ │
│  │             │                                                │
│  │             │ ──── ping 8.8.8.8 ────▶ Internet (ICMP) (ok)   │
│  │             │ ── curl amazonaws ────▶ Internet (HTTPS) (ok)  │
│  └─────────────┘                                                │
│         │                                                       │
│         ▼                                                       │
│       [IGW]                                                     │
└─────────┼───────────────────────────────────────────────────────┘
          │
          ▼
      Internet
```

## Scripts

All scripts are located in `isvctl/configs/providers/aws/scripts/network/`:

| Script | Purpose | Output Schema |
|--------|---------|---------------|
| `create_vpc.py` | Create shared VPC for tests | `network` |
| `vpc_crud_test.py` | VPC create/read/update/delete | `vpc_crud` |
| `subnet_test.py` | Multi-AZ subnet configuration | `subnet_config` |
| `isolation_test.py` | VPC isolation verification | `vpc_isolation` |
| `sg_crud_test.py` | Security group CRUD lifecycle | `sg_crud` |
| `security_test.py` | SG/NACL blocking rules | `security_blocking` |
| `test_connectivity.py` | Instance connectivity via SSM | `connectivity_result` |
| `traffic_test.py` | Real ping traffic tests | `traffic_flow` |
| `vpc_ip_config_test.py` | DHCP options, subnet CIDRs, auto-assign IP | `vpc_ip_config` |
| `dhcp_ip_test.py` | DHCP lease, IP match, DNS options via SSH | `dhcp_ip` |
| `byoip_test.py` | Bring-Your-Own-IP with custom CIDRs | `byoip` |
| `stable_ip_test.py` | IP persistence across stop/start | `stable_ip` |
| `floating_ip_test.py` | Atomic IP switch between instances | `floating_ip` |
| `dns_test.py` | Custom internal domain resolution | `localized_dns` |
| `peering_test.py` | Cross-VPC connectivity | `vpc_peering` |
| `teardown.py` | Clean up VPC resources | `teardown` |

## Quick Start

```bash
# Run all network tests
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml

# Run specific test suites
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml -- -k "vpc_crud"
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml -- -k "isolation"
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml -- -k "security"
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml -- -k "traffic"

# Run with verbose output
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml -v
```

## Test Cases

### 1. VPC CRUD Check

**Script**: `vpc_crud_test.py`
**Validation**: `VpcCrudCheck`

Tests the complete VPC lifecycle:

- Create VPC with specified CIDR
- Wait for VPC to become "available"
- Update VPC tags
- Enable DNS hostnames and support
- Delete VPC and verify removal

**Output example**:

```json
{
  "success": true,
  "platform": "network",
  "tests": {
    "create_vpc": {"passed": true, "vpc_id": "vpc-xxx"},
    "read_vpc": {"passed": true, "state": "available"},
    "update_tags": {"passed": true},
    "update_dns": {"passed": true},
    "delete_vpc": {"passed": true}
  }
}
```

### 2. Subnet Configuration Check

**Script**: `subnet_test.py`
**Validation**: `SubnetConfigCheck`

Tests multi-AZ subnet distribution:

- Create VPC with specified CIDR
- Create multiple subnets across availability zones
- Verify AZ distribution
- Check subnet availability state
- Verify route table exists

**Configuration options**:

```yaml
validations:
  - SubnetConfigCheck:
      min_subnets: 4
      require_multi_az: true
```

### 3. VPC Isolation Check

**Script**: `isolation_test.py`
**Validation**: `VpcIsolationCheck`

Verifies security boundaries between VPCs:

- Create two VPCs with non-overlapping CIDRs
- Verify no VPC peering connections exist
- Check route tables don't reference other VPC
- Validate default security groups block cross-VPC traffic

### 4. Security Group CRUD Check

**Script**: `sg_crud_test.py`
**Validation**: `SgCrudCheck`

Tests the complete security group lifecycle:

- Create security group with description and tags
- Read security group attributes
- Update security group rules (add/remove ingress and egress rules)
- Delete security group and verify removal

### 5. Security Blocking Check (Negative Tests)

**Script**: `security_test.py`
**Validation**: `SecurityBlockingCheck`

Tests that security rules correctly **block** traffic:

- **Empty SG default deny**: Verifies no inbound rules = default deny
- **SG specific allow**: Creates SG allowing only SSH from specific CIDR
- **SG denies ICMP**: Verifies ICMP is blocked when not explicitly allowed
- **NACL explicit deny**: Creates NACL with explicit deny rule
- **Restricted egress**: Creates SG with HTTPS-only outbound

### 6. Connectivity Check

**Script**: `test_connectivity.py`
**Validation**: `NetworkConnectivityCheck`

Tests instance network connectivity:

- Creates IAM role for SSM access
- Launches EC2 instances in VPC subnets
- Waits for SSM agent to be online
- Tests ping between instances
- Tests internet connectivity

### 7. Traffic Validation Check

**Script**: `traffic_test.py`
**Validation**: `TrafficFlowCheck`

The most comprehensive test - sends real network traffic:

- Creates VPC with Internet Gateway
- Creates IAM role for SSM
- Creates two security groups (allow ICMP / deny ICMP)
- Launches 3 instances (source + 2 targets)
- Tests ping to allowed target (should succeed)
- Tests ping to blocked target (should fail)
- Tests internet ICMP (ping 8.8.8.8)
- Tests internet HTTPS (curl checkip.amazonaws.com)

### 8. VPC IP Configuration Check (DDI)

**Script**: `vpc_ip_config_test.py`
**Validation**: `VpcIpConfigCheck`

Tests IP address management configuration on the shared VPC:

- Verify VPC CIDR block
- Check subnet CIDRs and available IP counts
- Verify auto-assign public IP settings
- Check DHCP options set (domain name servers, domain name)

### 9. DHCP IP Management Check (DDI)

**Script**: `dhcp_ip_test.py`
**Validation**: `DhcpIpManagementCheck`

Tests DHCP-assigned IP management via SSH on a live instance:

- Launch instance in the shared VPC
- Verify DHCP lease is active
- Check assigned IP matches AWS metadata
- Verify DNS options are configured correctly

### 10. BYOIP Check

**Script**: `byoip_test.py`
**Validation**: `ByoipCheck`

Tests Bring-Your-Own-IP with non-standard CIDR ranges:

- Create VPC with custom CIDR (e.g., `100.64.0.0/16`)
- Create VPC with standard CIDR (e.g., `10.90.0.0/16`)
- Verify both VPCs are functional
- Verify subnets can be created in custom CIDR ranges
- Clean up both VPCs

### 11. Stable Private IP Check

**Script**: `stable_ip_test.py`
**Validation**: `StablePrivateIpCheck`

Tests that private IPs persist across instance stop/start cycles:

- Create VPC and launch instance
- Record private IP address
- Stop instance, then start it
- Verify private IP is unchanged after restart

### 12. Floating IP Check

**Script**: `floating_ip_test.py`
**Validation**: `FloatingIpCheck`

Tests atomic IP reassignment between instances:

- Create VPC and launch two instances
- Allocate Elastic IP and associate with instance A
- Reassociate Elastic IP to instance B
- Verify switch completes within the `max_switch_seconds` threshold (default 10s)
- Clean up all resources

### 13. Localized DNS Check

**Script**: `dns_test.py`
**Validation**: `LocalizedDnsCheck`

Tests custom internal domain resolution via Route 53 private hosted zones:

- Create VPC and private hosted zone (e.g., `internal.isv.test`)
- Create DNS records pointing to instance IPs
- Verify forward resolution works from within the VPC
- Clean up hosted zone and VPC

### 14. VPC Peering Check

**Script**: `peering_test.py`
**Validation**: `VpcPeeringCheck`

Tests cross-VPC connectivity via VPC peering:

- Create two VPCs with non-overlapping CIDRs
- Create and accept VPC peering connection
- Update route tables in both VPCs
- Verify connectivity between peered VPCs
- Clean up peering connection and VPCs

## Prerequisites

### AWS Credentials

```bash
# Option 1: AWS CLI configuration
aws configure

# Option 2: Environment variables
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-2

# Option 3: STS temporary credentials
export AWS_ACCESS_KEY_ID=ASIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=...
export AWS_REGION=us-west-2
```

### Required IAM Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:DescribeVpcs", "ec2:ModifyVpcAttribute",
        "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:DescribeSubnets",
        "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup", "ec2:DescribeSecurityGroups",
        "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupEgress",
        "ec2:CreateNetworkAcl", "ec2:DeleteNetworkAcl", "ec2:DescribeNetworkAcls",
        "ec2:CreateNetworkAclEntry",
        "ec2:CreateInternetGateway", "ec2:DeleteInternetGateway", "ec2:AttachInternetGateway",
        "ec2:DetachInternetGateway", "ec2:DescribeInternetGateways",
        "ec2:CreateRouteTable", "ec2:DeleteRouteTable", "ec2:AssociateRouteTable",
        "ec2:DisassociateRouteTable", "ec2:CreateRoute", "ec2:DescribeRouteTables",
        "ec2:RunInstances", "ec2:TerminateInstances", "ec2:DescribeInstances",
        "ec2:ModifySubnetAttribute",
        "ec2:CreateTags", "ec2:DescribeTags",
        "ec2:DescribeImages", "ec2:DescribeAvailabilityZones",
        "ec2:DescribeVpcPeeringConnections", "ec2:DescribeVpcAttribute"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:DeleteRole", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
        "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile",
        "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
        "iam:PassRole"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:DescribeInstanceInformation"
      ],
      "Resource": "*"
    }
  ]
}
```

## Configuration

### network.yaml Structure

The AWS provider config imports the canonical network test suite and overrides commands with boto3 scripts:

```yaml
import:
  - ../../../../suites/network.yaml

version: "1.0"

commands:
  network:
    phases: ["setup", "test", "teardown"]
    steps:
      - name: create_network      # Setup: shared VPC
        phase: setup
        command: "python3 ../create_vpc.py"
        args: ["--name", "isv-shared-vpc", "--region", "{{region}}", "--cidr", "10.0.0.0/16"]
        timeout: 300

      - name: vpc_crud             # Test 1: VPC CRUD (~30s)
      - name: subnet_config        # Test 2: Subnet Config (~30s)
      - name: vpc_isolation         # Test 3: VPC Isolation (~30s)
      - name: sg_crud               # Test 4a: SG CRUD (~30s)
      - name: security_blocking     # Test 4b: Security Blocking (~30s)
      - name: connectivity_test     # Test 5: Connectivity (~3 min)
      - name: traffic_validation    # Test 6: Traffic Validation (~5-7 min)
      - name: vpc_ip_config         # Test 7: VPC IP Config - DDI (~10s)
      - name: dhcp_ip_test          # Test 8: DHCP IP Management - DDI (~3 min)
      - name: byoip_test            # Test 9: BYOIP (~30s)
      - name: stable_ip_test        # Test 10: Stable Private IP (~5 min)
      - name: floating_ip_test      # Test 11: Floating IP (~5 min)
      - name: dns_test              # Test 12: Localized DNS (~60s)
      - name: peering_test          # Test 13: VPC Peering (~30s)

      - name: teardown             # Teardown: shared VPC cleanup
        phase: teardown
        command: "python3 ../teardown.py"
        # ...

tests:
  cluster_name: "aws-network-validation"
  settings:
    region: "us-west-2"
```

See [`providers/aws/config/network.yaml`](../../../config/network.yaml) for the full config with all arguments and timeouts.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region for tests | `us-west-2` |
| `AWS_ACCESS_KEY_ID` | AWS access key | From AWS config |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | From AWS config |
| `AWS_SESSION_TOKEN` | STS session token | None |
| `AWS_NETWORK_SKIP_TEARDOWN` | Skip VPC teardown | `false` |

## Example Output

```shell
============================================================
ORCHESTRATION RESULTS
============================================================
[PASS] SETUP   : create_network: passed
  [create_network] NetworkProvisionedCheck: PASSED - Network vpc-xxx provisioned: CIDR=10.0.0.0/16, subnets=2
[PASS] TEST    : vpc_crud: passed; subnet_config: passed; vpc_isolation: passed; sg_crud: passed;
                 security_blocking: passed; connectivity_test: passed; traffic_validation: passed;
                 vpc_ip_config: passed; dhcp_ip_test: passed; byoip_test: passed;
                 stable_ip_test: passed; floating_ip_test: passed; dns_test: passed; peering_test: passed
  [vpc_crud] VpcCrudCheck: PASSED - All 5 CRUD tests passed
  [subnet_config] SubnetConfigCheck: PASSED - 4 subnets across 2 AZs
  [vpc_isolation] VpcIsolationCheck: PASSED - VPCs vpc-a and vpc-b are properly isolated
  [sg_crud] SgCrudCheck: PASSED - All 4 SG CRUD operations passed
  [security_blocking] SecurityBlockingCheck: PASSED - All 5 security blocking tests passed
  [connectivity_test] NetworkConnectivityCheck: PASSED - 2 instances with network connectivity
  [traffic_validation] TrafficFlowCheck: PASSED - All 4 traffic tests passed (latency: 0.5ms)
  [vpc_ip_config] VpcIpConfigCheck: PASSED - CIDR, subnets, DHCP options verified
  [dhcp_ip_test] DhcpIpManagementCheck: PASSED - DHCP lease active, IP matches metadata
  [byoip_test] ByoipCheck: PASSED - Custom CIDR 100.64.0.0/16 and standard CIDR functional
  [stable_ip_test] StablePrivateIpCheck: PASSED - Private IP unchanged after stop/start
  [floating_ip_test] FloatingIpCheck: PASSED - IP switched in 1.2s (threshold: 10s)
  [dns_test] LocalizedDnsCheck: PASSED - internal.isv.test resolves correctly
  [peering_test] VpcPeeringCheck: PASSED - Cross-VPC connectivity verified
[PASS] TEARDOWN: teardown: passed
  [teardown] StepSuccessCheck: PASSED - Teardown successful
------------------------------------------------------------
[PASS] All phases completed successfully
```

## Troubleshooting

### "VPC limit exceeded"

AWS accounts have default VPC limits (5 per region). Delete unused VPCs or request a limit increase.

```bash
aws ec2 describe-vpcs --query 'length(Vpcs)'
```

### "UnauthorizedOperation"

Missing IAM permissions. See [Required IAM Permissions](#required-iam-permissions).

### "SSM not ready" or "InvalidInstanceId"

The SSM agent takes time to register. The tests include retry logic, but if it still fails:

- Ensure instances have public IP (for SSM endpoint connectivity)
- Verify IAM role has `AmazonSSMManagedInstanceCore` policy
- Check VPC has internet gateway and route to 0.0.0.0/0

### Tests timing out

Increase timeout in config:

```yaml
- name: traffic_validation
  timeout: 1200  # 20 minutes
```

## Cost & Cleanup

> **Warning**: These tests create AWS resources (VPCs, subnets, security groups,
> internet gateways, EC2 instances) that incur costs. Resources are
> automatically cleaned up during the teardown phase, but if teardown fails
> or is skipped, you must manually delete them to avoid ongoing charges.

```bash
# Find VPCs tagged by isvtest
aws ec2 describe-vpcs --filters "Name=tag:CreatedBy,Values=isvtest" \
  --query 'Vpcs[*].[VpcId,Tags[?Key==`Name`].Value|[0],State]' --output table

# Delete orphaned VPCs (must remove dependent resources first - see below)
VPC_ID="vpc-xxx"
```

`delete-vpc` fails with `DependencyViolation` if any resources still reference
the VPC. Remove dependencies in this order before deleting:

1. Terminate EC2 instances, load balancers, RDS/EFS mount targets (`terminate-instances`)
2. Delete NAT Gateways and VPC Endpoints (`delete-nat-gateway`, `delete-vpc-endpoints`)
3. Detach & delete remaining ENIs and release Elastic IPs (`delete-network-interface`)
4. Detach & delete Internet/VPN Gateways (`detach-internet-gateway`, `delete-internet-gateway`)
5. Delete peering/VPN connections (`delete-vpc-peering-connection`)
6. Disassociate & delete custom route tables (`disassociate-route-table`, `delete-route-table`)
7. Delete subnets (`delete-subnet`)
8. Delete non-default security groups and NACLs (`delete-security-group`, `delete-network-acl`)

> **Key check:** there must be **zero ENIs** remaining before `delete-vpc` succeeds.
> Verify with: `aws ec2 describe-network-interfaces --filters Name=vpc-id,Values=$VPC_ID`

```bash
# Quick manual cleanup example
VPC_ID="vpc-xxx"
aws ec2 terminate-instances --instance-ids $(aws ec2 describe-instances \
  --filters "Name=vpc-id,Values=$VPC_ID" --query 'Reservations[*].Instances[*].InstanceId' --output text)
aws ec2 detach-internet-gateway --internet-gateway-id igw-xxx --vpc-id $VPC_ID
aws ec2 delete-internet-gateway --internet-gateway-id igw-xxx
aws ec2 delete-subnet --subnet-id subnet-xxx
aws ec2 delete-security-group --group-id sg-xxx
aws ec2 delete-vpc --vpc-id $VPC_ID
```

For automated cleanup, use the suite's teardown phase which handles the full
dependency sequence via [`teardown.py`](../teardown.py):

```bash
uv run isvctl test run -f isvctl/configs/providers/aws/config/network.yaml --phase teardown
```

## Related Documentation

- [Configuration Guide](../../../../../../../docs/guides/configuration.md)
- [isvctl Documentation](../../../../../../../docs/packages/isvctl.md)
- [AWS EKS Validation Guide](../../eks/docs/aws-eks.md)

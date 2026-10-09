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

"""Validation classes for isvtest.

Validations are organized by category:
- generic: Field checks, schema validation, teardown/workload success
- cluster: Kubernetes cluster validations
- instance: VM/EC2 instance validations
- network: VPC, subnet, security group validations
- iam: Access key, tenant, and service account validations
- security: BMC isolation, BMC protocol posture, API endpoint isolation, infrastructure hardening

All validations are also available via step_assertions for backward compatibility.
"""

from isvtest.validations.attestation import (
    BmFirmwareAttestationCheck,
    BmNonceAttestationCheck,
)
from isvtest.validations.cluster import (
    ClusterHealthCheck,
    GpuOperatorInstalledCheck,
    NodeCountCheck,
    PerformanceCheck,
)
from isvtest.validations.generic import (
    FieldExistsCheck,
    FieldValueCheck,
    SchemaValidation,
    StepSuccessCheck,
)
from isvtest.validations.governance import (
    GovernanceMetricsCheck,
)
from isvtest.validations.health import (
    HealthAggregationCheck,
    HostHealthCheck,
)
from isvtest.validations.host import (
    CloudInitCheck,
    ContainerRuntimeCheck,
    CpuInfoCheck,
    DriverCheck,
)
from isvtest.validations.iam import (
    AccessKeyAuthenticatedCheck,
    AccessKeyCreatedCheck,
    AccessKeyDisabledCheck,
    AccessKeyRejectedCheck,
    ServiceAccountCredentialCheck,
    TenantCreatedCheck,
    TenantInfoCheck,
    TenantListedCheck,
)
from isvtest.validations.infiniband import (
    IbKeysConfiguredCheck,
    IbTenantIsolationCheck,
)
from isvtest.validations.instance import (
    InstanceListCheck,
    InstancePowerCycleCheck,
    InstanceRebootCheck,
    InstanceStartCheck,
    InstanceStateCheck,
    InstanceStopCheck,
    InstanceTagCheck,
    StableIdentifierCheck,
    VmInstanceIdReportedCheck,
    VmLaunchedWithSpecifiedKeyCheck,
)
from isvtest.validations.k8s_conformance import (
    K8sCncfConformanceCheck,
)
from isvtest.validations.network import (
    BackendSwitchFabricCheck,
    ByoipCheck,
    DhcpIpManagementCheck,
    FiveTupleFilteringCheck,
    FloatingIpCheck,
    LocalizedDnsCheck,
    NetworkConnectivityCheck,
    NetworkProvisionedCheck,
    NvlinkDomainCheck,
    SdnFilterAuditTrailCheck,
    SdnHardwareFaultLoggingCheck,
    SdnLatencyPerfLoggingCheck,
    SecurityBlockingCheck,
    SgCrudCheck,
    SgNodeScopingCheck,
    SgPolicyPropagationTimingCheck,
    SgPortSecurityPolicyCheck,
    SgServiceScopingCheck,
    SgSubnetScopingCheck,
    SgWorkloadScopingCheck,
    StablePrivateIpCheck,
    StorageL3RoutingCheck,
    SubnetConfigCheck,
    TrafficFlowCheck,
    VpcCrudCheck,
    VpcIpConfigCheck,
    VpcIsolationCheck,
    VpcPeeringCheck,
)
from isvtest.validations.nim import (
    NimHealthCheck,
    NimInferenceCheck,
    NimModelCheck,
)
from isvtest.validations.sanitization import (
    BmDiskSanitizationCheck,
    BmFirmwareResetCheck,
    BmGpuMemorySanitizationCheck,
    BmMemorySanitizationCheck,
    SkipSanitizationBreakfixCheck,
)
from isvtest.validations.security import (
    ApiEndpointIsolationCheck,
    AuditLogEntryCheck,
    AuditLogRetentionCheck,
    BmcBastionAccessCheck,
    BmcManagementNetworkCheck,
    BmcProtocolSecurityCheck,
    BmcTenantIsolationCheck,
    CentralizedKmsCheck,
    CertRotationCycleCheck,
    CustomerManagedKeyCheck,
    InsecureProtocolsCheck,
    KmsEncryptionOptionCheck,
    LeastPrivilegePolicyCheck,
    MfaEnforcedCheck,
    MinimalRoleEnforcementCheck,
    MutualTlsCheck,
    OidcUserAuthCheck,
    ShortLivedCredentialsCheck,
    TenantIsolationCheck,
    VirtualDeviceHardeningCheck,
    VmConsoleRbacCheck,
)
from isvtest.validations.storage_infra import (
    OobFailureDetectionCheck,
    StableStorageNodeIpCheck,
)

__all__ = [
    "AccessKeyAuthenticatedCheck",
    "AccessKeyCreatedCheck",
    "AccessKeyDisabledCheck",
    "AccessKeyRejectedCheck",
    "ApiEndpointIsolationCheck",
    "AuditLogEntryCheck",
    "AuditLogRetentionCheck",
    "BackendSwitchFabricCheck",
    "BmDiskSanitizationCheck",
    "BmFirmwareAttestationCheck",
    "BmFirmwareResetCheck",
    "BmGpuMemorySanitizationCheck",
    "BmMemorySanitizationCheck",
    "BmNonceAttestationCheck",
    "BmcBastionAccessCheck",
    "BmcManagementNetworkCheck",
    "BmcProtocolSecurityCheck",
    "BmcTenantIsolationCheck",
    "ByoipCheck",
    "CentralizedKmsCheck",
    "CertRotationCycleCheck",
    "CloudInitCheck",
    "ClusterHealthCheck",
    "ContainerRuntimeCheck",
    "CpuInfoCheck",
    "CustomerManagedKeyCheck",
    "DhcpIpManagementCheck",
    "DriverCheck",
    "FieldExistsCheck",
    "FieldValueCheck",
    "FiveTupleFilteringCheck",
    "FloatingIpCheck",
    "GovernanceMetricsCheck",
    "GpuOperatorInstalledCheck",
    "HealthAggregationCheck",
    "HostHealthCheck",
    "IbKeysConfiguredCheck",
    "IbTenantIsolationCheck",
    "InsecureProtocolsCheck",
    "InstanceListCheck",
    "InstancePowerCycleCheck",
    "InstanceRebootCheck",
    "InstanceStartCheck",
    "InstanceStateCheck",
    "InstanceStopCheck",
    "InstanceTagCheck",
    "K8sCncfConformanceCheck",
    "KmsEncryptionOptionCheck",
    "LeastPrivilegePolicyCheck",
    "LocalizedDnsCheck",
    "MfaEnforcedCheck",
    "MinimalRoleEnforcementCheck",
    "MutualTlsCheck",
    "NetworkConnectivityCheck",
    "NetworkProvisionedCheck",
    "NimHealthCheck",
    "NimInferenceCheck",
    "NimModelCheck",
    "NodeCountCheck",
    "NvlinkDomainCheck",
    "OidcUserAuthCheck",
    "OobFailureDetectionCheck",
    "PerformanceCheck",
    "SchemaValidation",
    "SdnFilterAuditTrailCheck",
    "SdnHardwareFaultLoggingCheck",
    "SdnLatencyPerfLoggingCheck",
    "SecurityBlockingCheck",
    "ServiceAccountCredentialCheck",
    "SgCrudCheck",
    "SgNodeScopingCheck",
    "SgPolicyPropagationTimingCheck",
    "SgPortSecurityPolicyCheck",
    "SgServiceScopingCheck",
    "SgSubnetScopingCheck",
    "SgWorkloadScopingCheck",
    "ShortLivedCredentialsCheck",
    "SkipSanitizationBreakfixCheck",
    "StableIdentifierCheck",
    "StablePrivateIpCheck",
    "StableStorageNodeIpCheck",
    "StepSuccessCheck",
    "StorageL3RoutingCheck",
    "SubnetConfigCheck",
    "TenantCreatedCheck",
    "TenantInfoCheck",
    "TenantIsolationCheck",
    "TenantListedCheck",
    "TrafficFlowCheck",
    "VirtualDeviceHardeningCheck",
    "VmConsoleRbacCheck",
    "VmInstanceIdReportedCheck",
    "VmLaunchedWithSpecifiedKeyCheck",
    "VpcCrudCheck",
    "VpcIpConfigCheck",
    "VpcIsolationCheck",
    "VpcPeeringCheck",
]

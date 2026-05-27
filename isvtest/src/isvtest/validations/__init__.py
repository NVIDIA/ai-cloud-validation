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
from isvtest.validations.instance import (
    InstanceCreatedCheck,
    InstanceListCheck,
    InstancePowerCycleCheck,
    InstanceRebootCheck,
    InstanceSpecifiedKeyCheck,
    InstanceStartCheck,
    InstanceStateCheck,
    InstanceStopCheck,
    InstanceTagCheck,
    StableIdentifierCheck,
)
from isvtest.validations.k8s_conformance import (
    K8sCncfConformanceCheck,
)
from isvtest.validations.network import (
    BackendSwitchFabricCheck,
    ByoipCheck,
    DhcpIpManagementCheck,
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
    ConsoleRbacCheck,
    CustomerManagedKeyCheck,
    InsecureProtocolsCheck,
    KmsEncryptionOptionCheck,
    LeastPrivilegePolicyCheck,
    MfaEnforcedCheck,
    MinimalRoleEnforcementCheck,
    OidcUserAuthCheck,
    ShortLivedCredentialsCheck,
    TenantIsolationCheck,
    VirtualDeviceHardeningCheck,
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
    "BmcBastionAccessCheck",
    "BmcManagementNetworkCheck",
    "BmcProtocolSecurityCheck",
    "BmcTenantIsolationCheck",
    "ByoipCheck",
    "CentralizedKmsCheck",
    "CertRotationCycleCheck",
    "CloudInitCheck",
    "ClusterHealthCheck",
    "ConsoleRbacCheck",
    "ContainerRuntimeCheck",
    "CpuInfoCheck",
    "CustomerManagedKeyCheck",
    "DhcpIpManagementCheck",
    "DriverCheck",
    "FieldExistsCheck",
    "FieldValueCheck",
    "FloatingIpCheck",
    "GpuOperatorInstalledCheck",
    "InsecureProtocolsCheck",
    "InstanceCreatedCheck",
    "InstanceListCheck",
    "InstancePowerCycleCheck",
    "InstanceRebootCheck",
    "InstanceSpecifiedKeyCheck",
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
    "NetworkConnectivityCheck",
    "NetworkProvisionedCheck",
    "NimHealthCheck",
    "NimInferenceCheck",
    "NimModelCheck",
    "NodeCountCheck",
    "NvlinkDomainCheck",
    "OidcUserAuthCheck",
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
    "StableIdentifierCheck",
    "StablePrivateIpCheck",
    "StepSuccessCheck",
    "SubnetConfigCheck",
    "TenantCreatedCheck",
    "TenantInfoCheck",
    "TenantIsolationCheck",
    "TenantListedCheck",
    "TrafficFlowCheck",
    "VirtualDeviceHardeningCheck",
    "VpcCrudCheck",
    "VpcIpConfigCheck",
    "VpcIsolationCheck",
    "VpcPeeringCheck",
]

<!-- GENERATED FILE - DO NOT EDIT BY HAND. Source: offtake-requirements.yaml. Run `make plan`. -->

# NVIDIA Requirements for AI Clouds

> Structured source of record: `offtake-requirements.yaml` (version 2.4).
> Curated, in-repo copy of the publicly-published *NVIDIA Requirements for
> AI Clouds*. Edit the YAML, not this file.

## Exemplar Performance

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| BM01 | Benchmarking for Exemplar Performance | Run per Scalable Unit, for example, a 512 GPU cluster. Achieve performance within 5% of an NVIDIA-provided target. This target should be met on every Scalable Unit (SU) handed off. | Benchmarking for exemplar cloud | active |

## Compute and Network Provisioning

### General, Compute, and Lifecycle Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| CNP01 | API/CLI Access | DGXC must have API access to the NCP provisioning system for: (1) node lifecycle management (create, update, delete, and list) and power-state management (reboot and power cycle); (2) network configuration; (3) resource and topology discovery; (4) access configuration (users, service accounts, groups, and roles); and (5) maintenance and operations, as described in the Fleet Health and Maintenance section. | BM: #49, #52, #53, #57, #106, #108, #105, VM: #42, #45, #46, #47, #50, #110, #111, TBD-topo | active |
| CNP02 | Multi-step Workflow | For workflows requiring multiple operations, the NCP shall provide a documented and automatable method that identifies required sequencing, dependencies, completion states, and handling of partial failures. The method may use supported APIs or an infrastructure-as-code solution. | INFO | active |
| CNP03 | NVLink-Aware Allocation | For NVL72, the API must support NVLink domain-aware allocation. | TBD-topo | active |
| CNP04 | Resource States | Must support clear resource states, for example, instance and network states, where applicable. Example states include provisioning, running, degraded, maintenance required, stopping, stopped, terminating, and terminated. | BM: #51 VM: #45 | active |
| CNP05 | Tagging | Support for user-defined tags and labels, and `cloud-init` metadata on instances. | VM: #112 TBD-tag | active |
| CNP06 | Console Access | For each compute resource assigned to NVIDIA, the NCP must provide access to the resource’s console: the guest-instance console for virtualized capacity, or the host OS console for bare-metal capacity. Read-only access is sufficient, and interactive access is preferred. Console output must be retained for at least one day, and 30 days is preferred. | TBD-console | active |
| CNP07 | If VMaaS Offered: <br>VMs per Node | GPU nodes: NVIDIA deploys one VM per node, and support for one VM per GPU is a nice-to-have.<br> General-purpose CPU nodes: Support multiple VMs per node. Actual deployments are selected through a memory and core-count shape. | INFO | active |
| CNP08 | Stable Identifiers | All resources, for example, nodes and switches, must have a stable and persistent ID that does not change during the lifespan of the resource, even when the resource goes offline for a service event. VMs must also have a stable identifier. | add | active |
| CNP09 | Firmware | All firmware must be brought to a known good state between tenants. The NCP should implement a robust firmware update process. All firmware must be cryptographically signed and attested during boot. | TBD | active |
| CNP10 | Remote Management | The out-of-band management interface for each physical compute node must support Redfish over TLS. IPMI must be disabled. | add | active |

### Boot Process and Disks

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| BOOT01 | Image Deployment and Updates | API-driven workflow allowing DGXC to deploy, update, and manage vendor-provided or tenant-supplied OS images for bare metal or virtual machine instances. | #38, #39, #4 , #43, #44, #58, add-k8s | active |
| BOOT02 | Guest Metadata | The control plane must make tenant-defined instance metadata and bootstrap configuration available to the guest OS through a documented metadata interface. The interface must be compatible with `cloud-init`. | BM: #107 VM: #113 | active |
| BOOT03 | Custom Disk Images | Support tenant-created custom OS images in documented formats such as `raw` and `qcow2`, and provide API-based image lifecycle management. Tenant administrators shall be able to share or replicate images across tenant projects or environments. | #104, #40 | active |
| BOOT04 | Node Local Storage | GPU and CPU nodes support access to node local storage, either NVMe or SSD, for use as scratch storage or for caching services. | TBD | active |

### SDN and Virtual Networking

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SDN01 | Virtual Networking | Full API lifecycle management (create, read, update, delete, and list) for software-defined private networks. Must support non-conflicting BYOIP, including `7.0.0.0/8`, and stable tenant IP allocations. Applicable to all types of tenant-managed resource nodes, such as CPU, GPU, and storage nodes. | #11, #12, #13, #14: #115 #116 | active |
| SDN02 | Security Groups | Support for VPC-style security groups, or an equivalent, including IP and CIDR-based allow and deny rules. Must define scope and application at the workload, node, service, and subnet or tenant levels. An example service is the Kubernetes API service. | TBD (assign GH issue) | active |
| SDN03 | Security Operations | Full API capabilities to manage security groups, including defined audit processes. | add | active |
| SDN04 | Tenant Isolation | Logical or physical network isolation between tenants across all networks. One tenant shall not be accessible to or observable by another tenant. Out-of-band infrastructure management interfaces, including BMCs, shall be isolated from all tenant-accessible networks. | #16, #17, #18 | active |
| SDN05 | Floating/Movable IP | Ability to switch a floating private IP between nodes, automatically or through an API, in less than 10 seconds and without requiring an instance reboot. | #117 | active |
| SDN06 | Localized DNS | Support for tenant-defined localized DNS configuration to enable internal domain resolution to private endpoints, such as storage endpoints. | #118 | active |
| SDN07 | Virtual Network Peering | Support for cross-virtual-network connectivity with full bandwidth and no hairpin routing. | #119 | active |
| SDN08 | Storage Mesh Connectivity | The virtual network from SDN01 must provide unrestricted L3 routing between all storage hosts, enabling full-mesh, all-to-all communication across different subnets without going through a gateway. | INFO | active |
| SDN09 | Observability | The NCP shall provide tenant-visible telemetry sufficient to identify and troubleshoot tenant-impacting network faults and performance degradation, including degraded states, errors, drops, utilization, and latency across applicable network domains. | INFO | active |
| SDN10 | DNS Private Domain | Must allow each node’s DNS resolver to forward tenant-defined private domains, for example, `*.nvidia.com`, to a tenant-specified DNS server. |  | active |

## Kubernetes as a Service (KaaS) Requirements

### Kubernetes Conformance, Versioning, and Compliance

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S01 | Certified Versions | Certified upstream versions: official CNCF-certified versions only, with no proprietary forks, that pass the standard Kubernetes conformance tests. | TBD-cncf tests | active |
| K8S02 | Version Updates | Support the three most recent minor releases within the maintenance window. New minor versions must be available within 4 to 6 weeks of the upstream release. Control-plane security patching must be automated. | INFO | active |
| K8S03 | EOL Policy | Defined notification periods for version deprecation. | INFO | active |
| K8S04 | Kubernetes Security Response | Must participate in the Kubernetes Security Response Committee (SRC) process.  Must be attempting to join if not part of the security committee. Must be able to:<ul><li>Responsibly disclose any discovered vulnerabilities to the Kubernetes SRC</li><li>Receive and respond to embargo notifications from the SRC</li><li>Patch disclosed vulnerabilities in the managed service during embargo prior to public disclosure and in compliance with direction provided from the Kubernetes SRC ensuring that the patching process does not violate embargo or SRC guidance.</li></ul> | INFO | active |

### Kubernetes Operational Excellence

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S05 | Lifecycle Management - Control Plane | API for CRUD provisioning; less than 30 min control plane bring-up. | #2, #5, #6 | active |
| K8S06 | Lifecycle Management - Node Pool | <ul><li>API/CLI/Terraform for CRUD provisioning ( e.g., create node pool, update node pool, delete node pool, scale a node pool to a target count).<ul><li>Must be able to specify node type (specific CPU or GPU instance type) including CPU-only node pools with high-performance networking for data movement and ingest workloads</li></ul></li><li>Ability to specify default node labels and node taints within a node pool when a node joins the cluster.</li><li>When down-scaling a node pool, ability to down-scale bad/specific nodes.</li></ul> | add | active |
| K8S07 | API Server Metrics | Share Prometheus-compatible API server metrics sufficient to measure availability, request rate, latency, queueing, throttling, rejected requests, and control-plane saturation. |  | active |
| K8S08 | Versioning | Provider-managed control plane upgrade processes. | INFO | active |
| K8S09 | Zero-Downtime Upgrades | Minor version control plane updates without application downtime or maintenance windows. | INFO | active |
| K8S10 | Node Upgrades | User-initiated rolling updates that respect pod disruption budgets. | TBD | active |
| K8S11 | HA Control Plane | Redundant architecture with `etcd` separation. | INFO | active |
| K8S12 | Backup and Disaster Recovery | Supported recovery within a defined RPO and RTO. Recovery must be auditable and testable. | INFO | active |

### Robust Kubernetes Security

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S14 | Control Plane Isolation | Per-tenant Kubernetes control plane nodes must be separate from worker nodes and outside of the tenant cluster or VPC. | TBD | active |
| K8S15 | Access Controls | Cluster endpoint must provide network access controls. | TBD | active |
| K8S16 | IAM Integration | Kubernetes Service Accounts shall integrate with the platform IAM system to enable workloads to assume platform-managed identities and roles with appropriate scopes. | TBD | active |
| K8S17 | Service Accounts | Kubernetes shall support standard Service Accounts and projected tokens as the workload identity mechanism, including a cluster-specific OIDC issuer to enable workload identity federation. The cluster shall expose OIDC discovery and JWKS endpoints that are reachable by configured external identity consumers, such as AWS IAM and GCP workload identity. | TBD | active |
| K8S19 | Encryption | At-rest encryption for `etcd` and secrets. | INFO | active |
| K8S20 | Logging | Ability to view or export Kubernetes control plane logs, including `kube-apiserver` and `kube-controller-manager` logs. | add | active |

### Kubernetes Component and Extension Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S21 | API Extensions | Mandatory support for CRDs and Validating/Mutating Admission Controllers. | add | removed |
| K8S22 | CNI | Standard compliance, with support for network policies. IPv4 and IPv6 dual-stack support is desired. | add | active |
| K8S23 | CSI | The NCP provides CSI drivers with dynamic provisioning and volume expansion for block, home directory, and high-speed storage offerings. NVIDIA has the option to install the drivers through Helm or Kustomize for testing or customization as needed. <ul><li>CSI credentials are scoped to the tenant cluster, with the ability to isolate storage access by cluster.</li><li>APIs are provided to query storage usage at the tenant and cluster level, with per-PVC, per-volume, per-directory, and per-user quotas, and usage reporting to manage utilization across and within PVCs.</li><li>Vendor-provided storage kernel modules and tools are provided in one of three ways: installed by the CSI driver, preinstalled in an NCP-provided machine image, or supplied as installable packages.</li></ul> | add | active |
| K8S24 | DRA | Dynamic Resource Allocation (DRA) must be enabled regardless of upstream feature status, whether beta or GA. Some DRA features require enabling feature gates for the control plane so that customers can run AI workloads with new DRA features. | TBD | active |
| K8S25 | Operator Support | Support standard operator-based management of hardware accelerators and associated drivers. Provider-default accelerator operators and drivers shall be replaceable or overridable to allow installation of tenant-required operator and driver versions, for example, GPU Operator and Network Operator. Provide golden configurations for GPU Operator and Network Operator. | add | active |

### Kubernetes Functionality

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S26 | Clusters | Support multiple clusters in the same tenancy, and support multiple clusters in the same VPC. | add | active |
| K8S27 | Kubernetes Control Plane Size Pinning | Pin control plane instances to handle a particular load limit. | add | active |
| K8S28 | Performance | Meet the standard Kubernetes performance test certified up to 5,000 nodes, or to the maximum size of the cluster, whichever is smaller, and size the control plane as necessary. The managed Kubernetes control plane SLO and performance must meet or exceed the standard Kubernetes results. | add | active |
| K8S29 | Kubernetes LoadBalancer Service Support | The platform shall support Kubernetes Service resources of type LoadBalancer, including:<ul><li>External load balancers with publicly routable IPs</li><li>Internal load balancers with private IPs reachable via private network access</li><li>Static IP assignment.</li></ul> |  | active |
| K8S30 | DNS Configuration | The platform shall support configuring Kubernetes internal DNS, such as CoreDNS, with conditional forwarding rules for specified DNS zones to designated enterprise or internal DNS resolvers. |  | active |
| K8S31 | Configurable Kubernetes CIDR Ranges | Ability to configure the Kubernetes service IP range, node IP range, and pod IP range. |  | active |
| K8S32 | API Priority and Fairness | The managed Kubernetes control plane shall use Kubernetes API Priority and Fairness (APF) to assign requests to multiple priority levels, protect critical system and administrative operations from lower-priority traffic, and prevent any single client or workload from monopolizing API-server capacity during overload. The NCP shall publish its request processing latency SLOs per priority. |  | active |

## Security and Identity Management

### Identity and Access Management (IAM)

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC01 | Authentication | **Users**: Support federated single sign-on with a customer’s enterprise identity provider using OIDC or SAML 2.0. OIDC shall be supported for CLI and API authentication, including human access to managed Kubernetes API servers. OIDC-issued tokens shall be validated for signature, issuer, audience, expiration, and required identity and group claims. | add | active |
| SEC03 | Authentication | **External Services**: Support authentication of out-of-cluster service accounts for service-to-service access. Must support credential-based access, including long-lived credentials where required. If long-lived credentials, such as API keys, are issued, the platform shall support configurable expiration and rotation. Ownership attribution is required for all service accounts. The platform shall provide account information, such as detection of unused accounts. | add | active |
| SEC04 | Authorization (RBAC) | The platform shall enforce least-privilege RBAC for all managed services and infrastructure, featuring granular API actions such as CRUD, scopes such as development, staging, and production, and functions such as image builder, provisioner, and auditor. Roles and permissions shall be assignable to groups, with users inheriting access through group membership (GBAC). Group membership may be sourced from OIDC claims, SCIM-provisioned groups, or both. | add INFO | active |
| SEC05 | Identity and Directory Services | The platform shall integrate with the NVIDIA LDAP (RFC2307bis) directory service such that users’ identities and group membership can be resolved by dependent services for authentication and authorization decisions. | INFO | active |
| SEC06 | Workload/Service Identity | Support standard workload, service, and node security identities using short-lived credentials, including OIDC-based workload identity federation and Kubernetes ServiceAccounts where applicable. | INFO | active |
| SEC07 | Admin Interfaces | All administrative interfaces, whether UI, CLI, or API, must be protected by multifactor authentication (MFA). One example is a management API. | INFO | active |
| SEC08 | Audit Logs | Audit logs must be generated and retained for all security-relevant events, including management and control plane API calls, authentication events, and authorization decisions. Audit logs shall be retained for a minimum of 30 days and accessible to authorized platform operators. Must provide a log export mechanism, such as publishing to an S3 bucket. Exported logs should include sufficient metadata to identify the tenant, project or account, region, service, resource identifier, actor, event timestamp, source IP where applicable, action, and authorization result. | add | active |
| SEC23 | Provisioning | The platform shall support SCIM 2.0 for automated user and group lifecycle management from enterprise identity providers. SCIM endpoints shall require authenticated and authorized access, support core User and Group resource operations, and synchronize group membership changes with the platform authorization engine. Synchronized groups shall be first-class RBAC objects targetable by role bindings and IAM policies, and membership changes shall propagate promptly across all managed services. |  | active |
| SEC24 | Authentication | Must support domain-based IdP routing, mapping multiple email domains to a designated identity provider. For example, map `nvidia.com` and `nvw.nvidia.com` to the NVIDIA enterprise IdP. |  | active |
| SEC25 | Organization-Level Policies | The platform shall support organization-level security guardrail policies that cascade across all subordinate tenant resources, such as networks, clusters, storage, and compute, and that cannot be weakened or bypassed by lower-level configuration. Policy violations shall be denied at resource creation or update time and recorded in audit logs. | add | active |
| SEC26 | SSO Enforcement | The platform shall allow authorized administrators to enforce federated SSO for a tenant, restricting local username and password login and other non-federated login for regular users. Enforcement shall apply consistently across UI, CLI, API, and administrative interfaces. | add | active |
| SEC27 | Account Management | The platform shall expose a programmatic mechanism to create and manage:<ul><li>Isolation Units (e.g., projects, sub-project)</li><li>IAM Users</li><li>Service Accounts</li></ul> | add | active |

### Cryptography and Key Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC09 | Key and Certificate Lifecycle | The platform shall support secure issuance, distribution, storage, rotation, and revocation of cryptographic keys and certificates used across platform services. It shall support automated rotation of provider-managed and customer-managed keys and certificates, with configurable rotation intervals. Keys and certificates must be auditable and must have an expiration date. | Add INFO | active |
| SEC10 | Key Usage | The platform shall support use of managed keys and certificates across platform services for encryption, authentication, and signing. | add | active |

### Network Isolation and Encryption

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC11 | Tenancy Model | Hard physical or logical isolation for network, data, and compute. Separation of control planes and tenants is mandatory. This includes separation of storage resources. Provide hierarchical tenancy with at least an organization level and a project level. | add | active |
| SEC12 | BMC Security | Out-of-band management (BMC) must be on a dedicated, restricted network that is either physically separate or VLAN-isolated or VRF-isolated. Direct access from the public internet or general corporate networks must be blocked, and access must be permitted only through a hardened bastion, or jumphost, server. | add | active |
| SEC13 | Network Traffic Encryption | Encryption and mutual authentication for all east-west and north-south network traffic. | add | active |

### Edge Network Security

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC14 | Private Access | No public internet access by default. All API endpoints, such as the Kubernetes API server, must be restricted through a firewall or private link. | INFO | active |
| SEC15 | Edge Network Security Policy | All traffic must be filtered through security groups, user-customizable ACLs, or both, using 5-tuple rules. | INFO | active |
| SEC16 | Enforcement | The NCP must specify the edge network enforcement technology, such as hardware firewalls, SDN, or DPUs and SmartNICs, and its specific placement in the packet path. | INFO | active |
| SEC17 | Threat Intelligence and Scale | Ability to subscribe to GeoIP threat and embargo feeds and import them into security groups. The NCP should share the maximum supported number of records and rules. | INFO | active |
| SEC18 | MACsec Protection Links | Protect links between the NCP data center and the NVIDIA POP. | INFO | active |

### Hardware Security and Compliance

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC19 | SOC 2 | SOC 2 Type 1 or better is required, covering Security, Availability, and Confidentiality across all services and data center infrastructure. | INFO | active |
| SEC20 | At-Rest Data Protection | Mandatory encryption of all data at rest, such as data on local NVMe or SSD storage and on network-attached storage. | INFO | active |
| SEC21 | Data Sanitization | Data sanitization must be performed between tenants or on a hardware replacement. Sanitization includes cryptographic erase of all data drives between tenants, sanitization or wipe of any persistent or volatile memory including SRAM and GPU memory, and resetting of the TPM and BIOS. | add | active |
| SEC22 | Root of Trust and <br>Secure Boot | Mandatory support across all platforms for hardware root of trust mechanisms (TPM 2.0). The platform must enable UEFI OS Secure Boot with TPM 2.0. | INFO | active |

## Break-Fix Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| BFX01 | Break-Fix Lifecycle | **Compute**: Power-cycle individual nodes or reset a VM instance.<br>**GPU**: Reset GPUs on an individual node (as needed - K8s).<br>**Maintenance**: Return/Report an individual node and a rack to the Provider for maintenance.<br>**Cordon**: Mark a node as unschedulable for new workloads (but finish existing).<br>**Replace**: Request a host-replacement when health thresholds are breached. | add | active |
| BFX02 | Break-Fix Events | <ul><li>Query for any upcoming/current maintenance events for a node or rack.</li><li>Query for any retirement notices for a node/rack.</li><li>Query for historical/status information for equipment repair.</li><li>Event information should include:<ul><li>ticket open date</li><li>ticket update date</li><li>ticket close date</li></ul></li><li>Hardware Stable Identifier (e.g., node ID)</li><li>Hardware category/type impacted (e.g., GPU, fan, interconnect)</li><li>Maintenance/Error/fault description (some short description of the issue)</li><li>Action: Categorization of action (e.g. repairs done on faulty GPUs to resolve the fault)</li><li>Provider Account ID</li><li>Ticket ID</li><li>Node Handover Date (Date when the node was deployed in Production)</li></ul> |  | active |
| BFX03 | Diagnostics | <ul><li>Identify serial numbers of installed hardware (chassis, baseboard, network adapters, CPU, GPU, etc.). Obfuscated but stable identifiers are also OK.</li><li>Inspect firmware versions of compute nodes and NV switch trays.</li></ul> |  | active |

## Storage Requirements

### File System Storage for Home Directory and High-Speed File System

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| DIR01 | Quota Support | The service must support configurable filesystem-wide limits with per-UID, per-GID, and per-directory quota settings and overrides. Usage accounting must be available for all quota types when enabled. | INFO | active |
| DIR02 | Filesystem Semantics | The NFS interface must be NFS 4.1 or later. File storage must provide POSIX filesystem semantics, including file locking using `flock()`. | INFO | active |
| DIR03 | Snapshots | The file system must provide snapshot and restore functionality. |  | active |
| DIR04 | LDAP | The file service must support integration with an NVIDIA-managed LDAP directory service, as described in SEC05.<br><br>NFS storage must support storage system LDAP group enumeration through RFC2307bis to resolve full group membership for users belonging to more than 16 groups. |  | active |

### High-Speed Storage Service Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| HSS01 | Provisioning APIs | Storage provisioning may be performed through a vendor portal or API, or through an NCP portal or API. | add | active |
| HSS02 | Performance | Must provision the requested throughput for minimum bandwidth and IOPS. | INFO | active |
| HSS03 | Integration | Kubernetes CSI support is required. A break-fix API is required to report storage issues. | INFO | active |

### High-Speed Filesystem Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| HSS04 | Quota Support | Configurable filesystem-wide limit, default user/gid quota settings, and per uid/gid overrides available. Usage accounting for uid/gids when the feature is enabled. Configurable directory quota settings … it must be possible to apply a quota for a given directory. Usage accounting for directory quotas when enabled. | INFO | removed |
| HSS05 | Upgrade, maintenance | Provider / NCP initiates desired maintenance. NVIDIA can schedule actual maintenance and can defer maintenance up to 2 weeks. Upgrades should be non-disruptive. | INFO | removed |
| HSS06 | RDMA Memory Protection | Storage systems using RDMA must enforce memory protection via authorization keys for both local and remote access | INFO | removed |
| HSS07 | Parallel High-Speed Filesystem | Parallel or multi-path high-speed filesystem that supports scaling to thousands of simultaneous clients while sustaining requested performance. Must be accessible to all nodes in the tenancy, whether GPU or CPU nodes. | INFO | active |
| HSS08 | Single File System Size | It must be possible to allocate a file system of at least 1 PiB even if the initial request is smaller, and to grow it beyond 10 PiB as cluster size increases. This hard requirement may be higher for a specific site, and any higher value is communicated through the Ancillary Services Document. | INFO | active |
| HSS09 | Multiple Filesystems (Fungible Total Capacity) | More than one filesystem is supported within the total capacity. The minimum file system size is 50 TiB or less. | INFO | active |
| HSS10 | Filesystem Expansion | Live file system expansion is supported, in terms of capacity, inodes, I/O performance, and metadata operations performance. Expanded services must meet contracted performance targets. | INFO | active |
| HSS11 | Client | <ul><li>Ability to describe your client: In-Kernel, userspace, or bare-metal client installation requirements.</li><li>Support integration with client kernels / OS used by NVIDIA, as needed.</li><li>DKMS-enabled packages available for Ubuntu 20.04, 22.04, and 24.04-based operating systems.</li><li>ARM64 versions compatible with GB200-ready kernels are mandatory, e.g. Linux 6.8.x.<br><br>Managed Storage Service Provider will provide client configuration best practices and configuration guidelines for filesystem options and kernel module configuration to reliably achieve optimal performance on ARM and x86_64-based clients.</li></ul> | INFO | active |
| HSS12 | Quota (User, project & group) | Must support soft and hard quotas - uid / gid / project(directory)-id quotas with enforcement. | INFO | removed |
| HSS13 | Root-squash | NVIDIA needs to be able to enable, disable, and manage root-squash at any time. | INFO | active |
| HSS14 | flock | It must be possible to mount the file system with flock. | INFO | removed |
| HSS15 | Ability to Audit Changes | Enable NVIDIA to access changelog data for filesystem auditing and detailed user operations tracking. <br>Tracking must cover UID and GID, file creation, directory creation, file renaming, directory renaming, file deletion, and directory deletion. | INFO | active |
| HSS16 | High Availability | All services are required to tolerate any critical component failure in the backend and provide continued client access to all storage services in such cases. | INFO | active |
| HSS17 | Multi-Node Coherency | One second or less for client attribute and dentry cache updates and invalidations. | INFO | active |
| HSS18 | Client Multipathing | Clients must support multipathing to multiple endpoints for all storage servers. Multiple client NICs are not required. | INFO | active |
| HSS19 | LDAP (for NFS) | NFS-based high-speed filesystem services must support integration with an NVIDIA-managed LDAP server (including unix uid group membership for users with > 16 group memberships) as per SEC05 |  | removed |

### Data Movement Systems Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| DMS01 | Dedicated Kubernetes Cluster | A provider-managed Kubernetes cluster, or the ability for NVIDIA to stand up its own cluster, for the data mover stack. The cluster must be available ahead of the GPU cluster bring-up so that data can be pre-staged. | add | active |
| DMS02 | Data Mover Nodes (CPU) | Dedicated CPU nodes for running the data mover, with high-performance networking. The exact quantity is communicated through the Ancillary Services Document. | INFO | active |
| DMS03 | Access to Same GPU Storage | The same filesystem that is mounted on GPU nodes must be mounted on the data mover nodes, or it must be possible to mount the same filesystem through CSI. | INFO | active |
| DMS04 | Access to the NVIDIA Corporate Network | A dedicated link, as described in the network transport requirements, to the NVIDIA corporate network. A VPN is preferred, and a stable IP for allowlisting is otherwise required. | INFO | active |
| DMS05 | Stable Egress IP | A stable IP for allowlisted access to NVIDIA services, similar to a NAT gateway. | add | active |

### DGXC-Managed Storage System Deployment

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| STG01 | Operating System Support | The NCP must support a workflow that allows DGXC storage operators to integrate vendor-provided or storage-specific operating system images through bare-metal or VM provisioning for storage servers. The workflow must allow DGXC to deploy custom OS images, for example, vendor-enhanced kernels for Lustre, Rocky Linux, and Ubuntu 20.04, 22.04, and 24.04. | INFO | active |
| STG02 | Drive Sanitization Policy | Cryptographically erase data drive contents between storage system tenants, with full attestation of host firmware. Must support an optional flag to skip drive sanitization during break-fix flows, such as a power supply replacement, where tenancy does not change. Critical hardware component replacements may require sanitization without an override, and this includes GPU and CPU node local storage. | add | active |
| STG03 | Stable IP Assignment | Storage nodes must support static IP addressing that remains stable during host lifecycle operations and does not reset between maintenance events. | INFO | active |
| STG04 | Out-of-Band Failure Detection | The NCP must provide the ability to detect system failures out-of-band, including device, network, memory, and drive failures, enabling DGXC to proactively respond to hardware issues. | INFO | active |
| STG05 | Topology Observability | The NCP must provide visibility into failure domains to enable DGXC to provision storage nodes with physical diversity. Storage systems must be able to provision nodes that purposefully span failure domains for resilience. | INFO | active |
| STG06 | BlueField/DPU Support | For storage systems that use BlueField-based architectures, the host provisioning system must support lifecycle management and specific configuration requirements for BlueField JBOF systems that export NVMe-oF to hosts. | INFO | active |

## Network Transport and Fabric Visibility

### Backend Switch Fabric API

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| NET01 | Backend Switch Fabric API | For each compute node, the API must provide visibility into the backend network switches connecting the node to the core.<ul><li>**Identification:** Each switch must be identified by a unique, stable identifier. A "switch" may represent a physical switch or a logical connectivity domain.</li><li>**Structure:** API may be gRPC or REST. Response structure may include multiple nodes (pagination expected).</li><li>**Topology:** Switch info can be returned as an ordered array of IDs (e.g., leaf, spine, core) or separate fields for each tier.</li></ul> | INFO | active |
| NET02 | NVLink Domain API | **Requirement:** For compute nodes supporting NVLink (e.g., GB200, GB300, Vera Rubin), the API shall return the unique identifier of the NVLink domain associated with each node.<br><br>**Implementation:** Can be a separate API method or part of the Backend Switch Fabric API. | INFO | active |

## Transport and Networking Requirements

### Non-Conflicting IP Space Allocation for the DGXC Cluster

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| NET03 | Non-Conflicting IP Space Allocation for the DGXC Cluster | **Bring Your Own IP (BYOIP)**: NCP shall support the ability for NVIDIA to bring and allocate its own IP private address space for DGXC GPU clusters.<br><br>**Stable IP**: NCP shall provide a possibility to create static IP allocations that persist across instance restarts and re-creations. That includes floating IP allocations.<br><br>**DoD space:** NCP shall support allocation and use of the 7.0.0.0/8 IPv4 address space for DGXC GPU cluster deployments. This IP space shall be considered equivalent to RFC1918 addresses.<br><br>**Routing Support:** NCP must support advertising and routing of BYOIP prefixes within the NCP environment and across interconnects (Private Cloud Interconnect, IPSec, etc.) | add | active |

### Connection to NVIDIA CorpIT Network

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| NET04 | Connection to NVIDIA CorpIT Network | **Bandwidth**: Low bandwidth, up to 10 Gbps. <br>**Transport**: Private Cloud Interconnect with VIF and BGP, which is preferred for better performance and security. DGXC establishes connectivity to the NCP through a mutually agreed Point of Presence (POP) using Private Cloud Interconnect, functionally equivalent to AWS Direct Connect, GCP Dedicated Interconnect, Azure ExpressRoute, and OCI FastConnect. Connectivity is provisioned with a Virtual Interface (VIF), and routing is established through BGP. The interconnect is used to exchange private IP space, including RFC1918 and `7.0.0.0/8`, between DGXC and the NCP. | INFO | active |

### Connection to DGXC Storage

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| NET05 | Connection to DGXC Storage | **Transport**: Private Cloud Interconnect with VIF and BGP, which is preferred for better performance and security. DGXC establishes connectivity to the NCP through a mutually agreed Point of Presence (POP) using Private Cloud Interconnect, functionally equivalent to AWS Direct Connect, GCP Dedicated Interconnect, Azure ExpressRoute, and OCI FastConnect. Connectivity is provisioned with a Virtual Interface (VIF), and routing is established through BGP. The interconnect is used to exchange private IP space, including RFC1918 and `7.0.0.0/8`, between DGXC and the NCP. | INFO | active |

### Cluster Local Internet Access

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| NET06 | Cluster Local Internet Access | **Cluster internet access**: Egress NAT IPs should be a static pool dedicated only to the NVIDIA cluster, tenancy, and VPC. These persistent IP addresses must be used exclusively for DGXC traffic and shall not be shared with or carry traffic from other NCP tenants.<br> **Availability**: Must support redundant upstream paths to ensure connectivity under failure. | INFO | active |

## Capacity and Fleet Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| CAP01 | Governance Metrics | **Required Governance Metrics**<br>The core metrics needed to track fleet health are:<ul><li>**Delivered**: Nodes/GPUs provisioned and available to NVIDIA, allocated to a specific account/project/tenant.</li><li>**Healthy**: Nodes/GPUs functioning and meeting SLA requirements, allocated to a specific account/project/tenant.</li><li>**Reserved**: Resources allocated to a specific account/project/tenant.</li><li>**Total Active/In-Use**: Nodes/GPUs currently in use within a specific account/project/tenant.</li></ul> | INFO | active |
| CAP02 | Resource Governance API Metrics | The Resource Governance API must return the following information for each node:<ul><li>**Node ID** (Unique identifier for a GPU node)</li><li>**Health State** (Healthy/Unhealthy classification)</li><li>**Instance ID** (Identifier for virtual workload)</li><li>**Creation Timestamp** (Time workload/node was created)</li><li>**Hardware Type** (Descriptor for the hardware model)</li><li>**GPU Count** (Number of GPUs per node)</li><li>**Top-level Account/ID** (Identifier for the top-level organization/account)</li><li>**Sub-Level Project/ID** (Identifier for the nested project/sub-account)</li><li>**In Use** (True/False status indicating if the GPU Node is turned on and in use)</li><li>**Region** (Region of the data center where nodes are deployed)</li></ul> | add | active |
| CAP03 | Resource Discovery APIs | It is not acceptable to have capacity be “handed” to DGXC through a phone, slack or email message.  For example, when  cluster first comes online, nodes/racks are likely being handed off weekly (or more frequently).  Instead, provide the following mechanism (and we can poll):<br><br>**Programmatic Capacity Discovery**: All newly delivered capacity must be discoverable via a centralized API. This "Resource Index" must provide a stable resource identifier and some information on why it’s being provided (e.g. capacity fulfillment on GB300 project, break-fix / RMA return to cluster, etc) | add | active |
| CAP04 | Logical Compartmentalization and Resource Isolation | To ensure performance consistency and security, the NCP must support strict logical and physical isolation of NVIDIA’s reserved capacity.<br><br>**Capacity Reservations**: A mechanism to logically group and "pin" a set of resources (compute, network, storage) to accounts (or equivalent constructs) in an NVIDIA tenancy<br><br>**Atomic Allocation**: Support for reserving a "topology block" as a single unit, ensuring all resources in that block share identical performance characteristics and security boundaries. | INFO | active |
| CAP05 | Unified Health and Lifecycle APIs | NVIDIA requires a "single source of truth" for the health of both physical hosts and logical compute primitives.<br><br>**Per-Host Health:** Real-time API access to the health bits of physical hardware (GPU state, thermal status, memory health).<br><br>**Primitive-Level Status:** Health aggregation at the cluster, nodegroup, or reservation level to identify broad infrastructure failures (e.g., a spine switch failure affecting a whole block). | INFO | active |

## Operational Requirements

### Support and Ticketing

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OST01 | 24x7 Coverage | The NCP must provide continuously available, 24x7 support, including emergency access recovery. Support requests must be tracked through a ticketing system of record. A real-time communication channel, such as Slack or phone, must also be available, along with a support SLA. |  | active |
| OST02 | Ticketing System | The customer-facing trouble-ticketing system shall be the system of record against which all customer-raised issues are logged, each with a unique identifier and a current state. The system must be searchable by attributes such as error codes and keywords, so that an already reported issue can be found. |  | active |
| OST03 | Ticket Severity and Targets | Every ticket shall carry the severity classification defined in OIE02 and follow the corresponding response, resolution, and escalation targets in the Service Levels section. |  | active |
| OST04 | Customer-Confirmed Closure | Customer-opened tickets shall not be closed until the customer agrees the issue is resolved. |  | active |

### Incident, Outage, and Escalation Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OIE01 | Incident Handling | The NCP shall maintain a defined incident-management practice to detect, classify, respond to, escalate, and resolve service-impacting events within the targets in the Service Levels section. |  | active |
| OIE02 | Severity Model | The NCP shall maintain a documented severity model for incidents and tickets, with a defined escalation path for each severity. The Service Levels section shall map the NCP severity levels to the applicable NVIDIA service targets. |  | active |
| OIE03 | Incident Command | For each major incident, the NCP shall assign an incident commander, or an equivalent role, responsible for coordinating technical response, decisions, handoffs, and customer communications under a maintained major-incident playbook. |  | active |
| OIE04 | Customer Notification | Customers shall be notified programmatically of qualifying, service-impacting incidents within the timeframe in the Service Levels section. |  | active |
| OIE05 | Automated Fault Remediation | The NCP shall automatically detect faults. After detection, non-disruptive faults should be automatically remediated. Tenant-disruptive faults should be remediated in a directed manner that follows the defined maintenance processes. When a fault cannot be remediated programmatically, it should fall back to an NCP-managed manual flow. |  | active |
| OIE06 | Root Cause and Corrective Action (RCCA) | For qualifying incidents, the NCP shall deliver a formal written RCCA report within a defined, published timeframe of at least monthly, stating specific corrective actions. |  | active |
| OIE07 | Corrective-Action Tracking | Corrective actions shall be tracked to closure and target dates. Overdue actions shall be escalated and reviewed on a defined cadence. |  | active |

### Security Incident and Breach Notification

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OSB01 | Security Incident Handling | The NCP shall detect, contain, investigate, and remediate security incidents affecting customer tenancy, data, or infrastructure, under a defined security incident response capability. |  | active |
| OSB02 | Breach Notification | The NCP shall notify tenants of any confirmed or reasonably suspected security breach, unauthorized access, or compromise of NVIDIA data or tenancy without undue delay, and with all available information. Notification shall occur no later than 24 hours for a high-severity or confirmed compromise, and no later than 72 hours for all other incidents. |  | active |
| OSB03 | Evidence Preservation and Joint Investigation | The NCP shall preserve relevant logs and forensic evidence for the period required by applicable law or regulation, and for no less than 12 months. The NCP shall support joint investigation, including NVIDIA participation where agreed. |  | active |
| OSB04 | Coordinated Disclosure | Public disclosure and external communications relating to an incident affecting the tenant shall be coordinated with the tenant. |  | active |
| OSB05 | Security Post-Incident Report | Security incidents shall follow the RCCA process defined in OIE06, with any additional information about the scope of the compromise. |  | active |

### Service Health and Status Communications

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OSH01 | Status Dashboard | An external health and status dashboard shall be published and available. The dashboard shall show health by region and by service, using clear multi-state indicators, for example, green, yellow, and red. |  | active |
| OSH02 | Incident and Maintenance Posting | Active incidents and scheduled maintenance shall be posted to the dashboard in a timely manner. |  | active |
| OSH03 | Historical Status | Historical uptime and incident history shall be retained and viewable. |  | active |
| OSH04 | Programmatic Access | Status and health should be consumable programmatically for ingestion by the tenant. |  | active |

### Change and Release Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OCR01 | Change Governance | The NCP shall employ a change control practice that ensures every change to production infrastructure is documented and approved before implementation and carries a rollback or back-out plan. Disruptive changes shall be communicated in advance, as described in the Fleet Health and Maintenance section. |  | active |
| OCR02 | Emergency Change | The NCP may employ an emergency change path, distinct from standard change, with retrospective review aligned to the RCCA process. |  | active |
| OCR03 | Progressive Delivery | Changes to NCP-managed platform or software services shall roll out with staged validation, defined stop criteria, blast-radius controls, and a tested rollback and recovery plan appropriate to the change. |  | active |
| OCR04 | Change Observation | Following a change, a defined observation period shall elapse before the change is declared successful and closed. If an issue surfaces during this window, the changes shall be remediated and treated as a failed change rather than a new incident. |  | active |
| OCR05 | Auditable Changes | Changes to production infrastructure supporting delivered services shall be versioned, reviewed, and auditable. Unauthorized or unreviewed deviations from the approved configuration shall be detected and remediated. |  | active |

### Fleet Health and Maintenance

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OFM01 | Declared Maintenance | Formal maintenance for instances in a maintenance-required or degraded mode shall be declared to the tenant through a defined process, with a hand-back window and a maintenance window provided. A hand-back window is expected to allow deferral for at least 2 weeks. |  | active |
| OFM02 | Tenant Scheduling and Deferral | Tenants shall be able to schedule declared maintenance services through an API or console tools within the hand-back window. The NCP should not force eviction unless the window has expired. The tenant accepts the risk of continued use during the hand-back window. Maintenance should be non-disruptive where possible. Tenants should have the option to request a deferral, meaning an extension of the hand-back window. The NCP is not obligated to accept a deferral but may evaluate the request based on the criticality of the maintenance. |  | active |
| OFM03 | Maintenance Tracking | Maintenance window open and close times shall be tracked, recorded, and exposed in scheduling APIs. An incident shall be declared when a window exceeds its planned close time. |  | active |
| OFM04 | Emergency Maintenance | A defined path shall exist for declaring and executing emergency maintenance windows. This path allows the hand-back window to be very small, on the order of minutes. |  | active |
| OFM05 | Validated Return | Following a declared or emergency maintenance event, such as a firmware upgrade or hardware replacement, the NCP shall complete all maintenance and validation across the affected compute, GPU, fabric, and storage resources before returning them to service. The validation is included in the maintenance window, whether the resources return to the tenant or to the allocable pool. |  | active |
| OFM06 | Progressive Rollout | Firmware and OS changes shall be rolled out progressively, through an incremental deployment method, rather than fleet-wide at once. A failed partial deployment halts the rollout and is remediated. The tenant should be given the option to test and validate new firmware in a development cluster before it is rolled out to the tenant fleet. |  | active |
| OFM07 | Sparing | The NCP shall define a spare parts and hot sparing strategy to support the availability SLA. |  | active |

### Operational Access and Activity Logging

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OAL01 | Operational Access Policy | Operator access to production infrastructure shall require multifactor authentication (MFA) and shall be restricted, through role-based and policy-based authorization, to the minimum permissions necessary to perform the operator’s assigned duties, as described in SEC07. |  | active |
| OAL02 | Recertification and Offboarding | Access shall be recertified periodically and revoked on offboarding. |  | active |
| OAL03 | Operational Activity Logging | All manual and administrative actions by NCP personnel affecting production infrastructure shall be included in the audit logs required by SEC08 and attributed to the authenticated operator and any assumed role. |  | active |
| OAL04 | Log Monitoring | Operational activity logs shall be reviewed or monitored for anomalous activity. |  | active |
| OAL05 | Two-Person Rule | High-risk operator actions on production infrastructure shall require two distinct authorized people, and no single operator can perform them alone. This shall be technically enforced and logged with both identities. A time-bounded break-glass exception may be used for emergency recovery, and its use must be logged and reported. |  | active |

### Vulnerability and Patch Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OVP01 | Vulnerability Management | The NCP shall identify, prioritize, remediate, and transparently disclose vulnerabilities and Common Vulnerabilities and Exposures (CVEs) affecting the infrastructure. |  | active |
| OVP02 | Scanning | Infrastructure shall be regularly scanned for known vulnerabilities and CVEs. |  | active |
| OVP03 | Severity Prioritization | CVEs shall be prioritized by severity, for example, through a CVSS-based rating, with Known Exploited Vulnerabilities (KEV) receiving the highest prioritization. |  | active |
| OVP04 | Remediation Targets | Remediation targets shall be defined by severity in the applicable service agreement, and critical vulnerabilities shall be remediated within those targets. |  | active |
| OVP05 | Tracking to Closure | Remediation of vulnerabilities that affect delivered services or customer tenancy shall be tracked to closure with an accountable owner and target date. |  | active |
| OVP06 | Exceptions | Exceptions and risk acceptances shall be formally documented and approved by an NVIDIA risk signatory. |  | active |

### Monitoring, Alerting, and On-Call

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OMA01 | Monitoring | The NCP shall continuously monitor infrastructure and service health independent of tenant workloads, without requiring agents inside tenant workloads and without depending on tenant scheduler or application data. |  | active |
| OMA02 | Alerting | Alerting shall use defined thresholds and route notifications to designated responders, with escalation for unacknowledged alerts. |  | active |
| OMA03 | On-Call Coverage | On-call coverage shall respond to alert notifications, staffed under a 24x7x365 coverage model. |  | active |
| OMA04 | Alert Tracking | Alert notification acknowledgement and response times shall be tracked. Reporting is needed only where SLAs or operational requirements require it. |  | active |
| OMA05 | Retention for Investigation | Telemetry required to support investigations shall be retained for a period aligned to the retention defined in the Telemetry Requirements section. |  | active |

### Capacity and Availability Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OCA01 | Capacity Planning | The NCP shall maintain a capacity planning process for GPU, compute, and supporting resources, with utilization monitored and forecast against available capacity. |  | active |
| OCA02 | Availability Targets | Availability targets for all services shall be defined and published in the Service Levels section. Where a target is not specified by NVIDIA, the NCP proposes the applicable target and measurement method. |  | active |
| OCA03 | Availability Measurement | Availability shall be measured and reported to the tenant against the published targets. |  | active |
| OCA04 | SLA Remedies | An SLA credit or remedy process shall exist for missed targets, financially backed where specified. |  | active |
| OCA05 | Metering Accuracy | Consumption metering used as the basis for capacity or billing shall meet the accuracy target published in the Service Levels section. |  | active |

### Service-Level Measurement and Definitions

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OSL01 | Availability Exclusions | Scheduled maintenance does not count against availability for the declared affected resources and during the declared maintenance window. Required notice and tenant scheduling commitments must be met, the impact must not exceed the declared scope, and all maintenance, validation, and return-to-service work must complete by the declared close time. |  | active |
| OSL02 | Measurement Source | Service-level measurement methodologies must be defined so that both the NCP and the tenant can compute the same result. |  | active |
| OSL03 | Credit Mechanics | The mechanism for how SLA credits are calculated, claimed, and applied for missed targets shall be formally defined. |  | active |

### Disaster Recovery and Business Continuity

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| ODR01 | DR and BC Capability | The NCP shall maintain disaster recovery and business continuity plans covering the services it delivers. |  | active |
| ODR02 | Backups and Verification | Backups shall be performed and their restore integrity verified. |  | active |
| ODR03 | Recovery Objectives | Recovery objectives, meaning the RTO and RPO, shall be defined and auditable across services, such as managed Kubernetes and storage services. |  | active |
| ODR04 | DR Testing | DR and failover shall be tested on a defined cadence, with recorded results. |  | active |
| ODR05 | DR Roles | Roles and responsibilities for DR execution shall be defined. |  | active |

### Provisioning and Onboarding

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| OPO01 | Onboarding and Provisioning | The NCP shall run a defined onboarding and provisioning process. |  | active |
| OPO02 | Quota Management | Resource quotas and provisioning shall be managed and tracked. |  | active |
| OPO03 | Verifiable Data Deletion | Customer data deletion on offboarding shall be defined and verifiable, including cryptographic erase between tenants, as described in SEC21. |  | active |

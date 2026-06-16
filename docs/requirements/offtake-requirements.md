<!-- GENERATED FILE - DO NOT EDIT BY HAND. Source: offtake-requirements.yaml. Run `make plan`. -->

# NVIDIA Requirements Guide for AI Clouds

> Structured source of record: `offtake-requirements.yaml` (version 2.3.1).
> Curated, in-repo copy of the publicly-published *NVIDIA Requirements Guide
> for AI Clouds*. Edit the YAML, not this file.

## Exemplar Cloud Workload Performance

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| BM01 | Run per Scalable Unit (e.g. 512 GPU cluster) | Achieve within 5% of an NVIDIA provided target performance number; this should be run on every Scalable Unit (SU) handed off | Benchmarking for exemplar cloud | active |

## Compute and Network Provisioning

### General, Compute & Lifecycle Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| CNP01 | API/CLI Access | DGXC must have API or CLI access to the NCP provisioning system for: (1) Node lifecycle management (create, update, delete, list, or manage power states (reboot, on/off power cycle); (2) Network configuration; (3) Inventory and topology discovery; (4) security configuration (users, service accounts, groups, roles); (5) Maintenance and operations (see later section) (storage discussed in storage section) | BM: #49, #52, #53, #57, #106, #108, #105, VM: #42, #45, #46, #47, #50, #110, #111, TBD-topo | active |
| CNP02 | Declarative Resource Interfaces | For resources requiring multiple steps and a workflow, please provide the appropriate mechanism. A terraform provider is preferred. E.g. automating filesystem provisioning | INFO | active |
| CNP03 | NVLink-Aware Allocation | For NVL72 the API must support NVLink domain-aware allocation | TBD-topo | active |
| CNP04 | Resource States | Must support clear resource (e.g. instance, network) states where applicable. For example, provisioning, running, degraded, maintenance required, stopping, stopped, terminating, terminated. | BM: #51 VM: #45 | active |
| CNP05 | Tagging | Support for user-defined tags/labels and cloud-init metadata on instances. | VM: #112 TBD-tag | active |
| CNP06 | Console Access | Serial console access is required (read-only sufficient, interactive preferred).Serial console output shall be logged and be available for historic queries (at least 1 month retention). | TBD-console | active |
| CNP07 | If VMaaS present: # VMs/Node | GPU Nodes: no more than one VM per Node General Purpose CPU Nodes: More than one per node, with ability to select via memory/core count shape. | INFO | active |
| CNP08 | Stable Identifiers | All resources (e.g. nodes, switches) must have a stable and persistent ID that does not change during the lifespan, even when it goes offline for a service event. VMs must also have a stable identifier. | add | active |
| CNP09 | Firmware | Between tenants, all firmware must be brought to a known good state, all firmware must be cryptographically signed and attested during boot. | TBD | active |
| CNP10 | Remote Management | Platform management solutions (e.g., BMC) must support Redfish over TLS (Disable IPMI). | add | active |

### Boot Process & Disks

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| BOOT01 | Image Deployment & Updates | API-driven workflow allowing DGXC to deploy, update, and manage vendor-provided or custom disk images via bare-metal, VM, or k8s node pool provisioning. | #38, #39, #4 , #43, #44, #58, add-k8s | active |
| BOOT02 | Access to Instance Metadata from Guest OS | Support for cloud-init and instance metadata discovery via link-local addresses or virtual devices. | BM: #107 VM: #113 | active |
| BOOT03 | Custom Disk Images | Support for tenant created custom OS images (either of: raw, qcow2, etc). API calls: get, list, create, delete. Images should be accessible across all tenant projects/clusters/environments. | #104, #40 | active |
| BOOT04 | Node Local Storage | GPU and CPU nodes support access to node local storage (NVMe / SSD) for use as scratch storage or for caching services | TBD | active |

### SDN & Virtual Networking

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SDN01 | Virtual Networking | Full API/CLI lifecycle management (Create, Read, Update, Delete, List) for software-defined private networks. Must support non-conflicting BYOIP (including 7.0.0.0/8) and stable private IP allocations. Applicable to all types of resource nodes (CPU, GPU, Storage, etc). | #11, #12, #13, #14: #115 #116 | active |
| SDN02 | Security Groups | Support for VPC-style security groups (or equivalent), including IP/CIDR-based allow and deny rules. Must define scope/application at workload, node, service (e.g. K8s API Service) and subnet/tenant levels. | TBD (assign GH issue) | active |
| SDN03 | Security Operations | Full API/CLI capabilities to Create, Read, Update, and Delete security groups, including defined audit processes | add | active |
| SDN04 | Tenant Isolation | Hard logical or physical network segmentation for out-of-band management (BMC), user traffic, and storage-specific operations. | #16, #17, #18 | active |
| SDN05 | Floating/Movable IP | Ability to automatically or API-driven switch a floating private IP between nodes via API within <10 seconds without requiring an instance reboot. | #117 | active |
| SDN06 | Localized DNS | Support for tenant-defined localized DNS configuration to enable internal domain resolution to private endpoints (e.g. storage endpoints) | #118 | active |
| SDN07 | VPC Peering | Support for cross-virtual-network connectivity with full bandwidth and no "hairpin" routing. | #119 | active |
| SDN08 | Storage Mesh Connectivity | The virtual network (from SDN01) must provide unrestricted L3 routing between all storage hosts, enabling full-mesh, all-to-all communication across different subnet (w/o going thru a gateway) | INFO | active |
| SDN09 | Observability | The platform shall provide comprehensive logging for network infrastructure, including hardware faults, latency/performance fluctuations, and a detailed audit trail of all configuration changes to network filtering rules.. | INFO | active |
| SDN10 | DNS private domain | must allow each nodes DNS resolver to forward a tenant-defined private domains (e.g. *.nvidia.com) to a tenant specified DNS server |  | active |

## Kubernetes As a Service (KaaS) Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S01 | Certified Versions | Certified Upstream Versions: Official CNCF-certified versions only; no proprietary forks, and passes the standard Kubernetes conformance tests. | TBD-cncf tests | active |
| K8S02 | Version Updates | Support the three most recent minor releases (in the maintenance window); new minor versions must be available within 4-6 weeks of the upstream release; automated control plane security patching. | INFO | active |
| K8S03 | EOL Policy | Defined notification periods for version deprecation. | INFO | active |
| K8S04 | Kubernetes Security Response | Must participate in the Kubernetes Security Response Committee (SRC) process. Must be attempting to join if not part of the security committee. Must be able to: Responsibly disclose any discovered vulnerabilities to the Kubernetes SRC Receive and respond to embargo notifications from the SRC Patch disclosed vulnerabilities in the managed service during embargo prior to public disclosure and in compliance with direction provided from the Kubernetes SRC ensuring that the patching process does not violate embargo or SRC guidance. | INFO | active |

### Kubernetes Operational Excellence

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S05 | Lifecycle Management - control plane | API/CLI/Terraform Provider for CRUD provisioning; <30 min control plane bring-up. | #2, #5, #6 | active |
| K8S06 | Lifecycle Management - node pool | API/CLI/Terraform for CRUD provisioning ( e.g., create node pool, update node pool, delete node pool, scale a node pool to a target count). Must be able to specify node type (specific CPU or GPU instance type) including CPU-only node pools with high-performance networking for data movement and ingest workloads Ability to specify default node labels and node taints within a node pool when a node joins the cluster. When down-scaling a node pool, ability to down-scale bad/specific nodes. | add | active |
| K8S07 | API Server Metrics | Share API Server metrics in a Prometheus scrapable format to allow NVIDIA to measure API Server SLO |  | active |
| K8S08 | Versioning | Provider-managed control plane upgrade processes. | INFO | active |
| K8S09 | Zero-Downtime Upgrades | Minor version control plane updates without app downtime or maintenance windows. | INFO | active |
| K8S10 | Node Upgrades | user-initiated rolling updates respecting pod disruption budgets. | TBD | active |
| K8S11 | HA Control Plane | Redundant architecture with etcd separation. | INFO | active |
| K8S12 | Backup & Disaster Recovery | Supported recovery within defined RPO/RTO; needs to be auditable & testable | INFO | active |

### Robust K8s Security

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S14 | Control Plane Isolation | Per tenant k8s control plane nodes must be separate from worker nodes and outside of the tenant cluster/VPC. | TBD | active |
| K8S15 | Access Controls | Cluster endpoint must provide network access controls. | TBD | active |
| K8S16 | IAM Integration | Kubernetes Service Accounts shall integrate with the platform IAM system to enable workloads to assume platform-managed identities and roles with appropriate scopes. | TBD | active |
| K8S17 | Service Accounts | Kubernetes shall support standard Service Accounts and projected tokens as the workload identity mechanism, including a cluster-specific OIDC issuer to enable workload identity federation. The cluster shall expose OIDC discovery and JWKS endpoints that are reachable by configured external identity consumers (e.g. AWS IAM, GCP workload identity) | TBD | active |
| K8S19 | Encryption | at-rest encryption for etcd and secrets | INFO | active |
| K8S20 | Logging | Ability to view or export Kubernetes control plane logs (apiserver, kcm). | add | active |

### Kubernetes Component and Extension Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S21 | API Extensions | Mandatory support for CRDs and Validating/Mutating Admission Controllers. | add | active |
| K8S22 | CNI | Standard compliance; supports Network Policies; IPv4/IPv6 dual-stack desired. | add | active |
| K8S23 | CSI | NCP provides CSI Driver installable by NVIDIA (helm or kustomize) for Block, shared FS, and NFS Support for static and dynamic provisioning, snapshots, and resizing via PVs and PVCs. CSI credentials are tenant cluster scoped (no cross cluster) APIs to query storage usage against overall cluster quota with per PVC/Volume usage to manage utilization across PVCs and manage quotas using provided credentials Vendor provided storage kernel modules and tools provided via (1) installed by CSI driver, (2) pre-installed in NCP provided machine image or (3) installable packages provided | add | active |
| K8S24 | DRA | Enabled Dynamic Resource Allocation (DRA) regardless of upstream feature status (Beta/GA). Some DRA features require enabling feature gates for the control plane, in case our customers want to run AI workload with new DRA features | TBD | active |
| K8S25 | Operator Support | Support standard operator-based management of hardware accelerators and associated drivers. Provider-default accelerator operators and drivers shall be replaceable or overridable to allow installation of tenant-required operator and driver versions (e.g., GPU Operator, Network Operator). Provide golden configurations for GPU Operator and Network Operator. | add | active |

### Kubernetes Functionality

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| K8S26 | Clusters | Support multiple clusters in the same tenancy; support multiple clusters in the same VPC. | add | active |
| K8S27 | Kubernetes Control Plane size pinning | Pin Control Plane instances to handle a particular load-limit | add | active |
| K8S28 | Performance | Meet the standard Kubernetes performance test certified up to 5000 nodes (or to the maximum size of the cluster, whichever is smaller) - size CP as necessary. Managed Kubernetes Control Plane SLO and Performance meets or better than the Kubernetes standards results. | add | active |
| K8S29 | Kubernetes LoadBalancer Service Support | The platform shall support Kubernetes Service resources of type LoadBalancer, including: External load balancers with publicly routable IPs Internal load balancers with private IPs reachable via private network access Static IP assignment |  | active |
| K8S30 | DNS Configuration | The platform shall support configuring Kubernetes internal DNS (e.g. CoreDNS) with conditional forwarding rules for specified DNS zones to designated enterprise or internal DNS resolvers. |  | active |
| K8S31 | Configurable Kubernetes CIDR ranges | Ability to configure Kubernetes service IP range, Node IP range, and Pod IP range. |  | active |

## Security & Identity Management

### Identity & Access Management (IAM)

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC01 | Authentication | Users: Support standards-based user authentication via OIDC and SAML 2.0, including federation with external identity providers (e.g. NVIDIA’s enterprise IdP) for single sign-on (SSO) across platform and tenant-facing services. Validate OIDC-issued tokens including signature, issuer, audience, expiration, and required claims for identity and authorization decisions. | add | active |
| SEC03 | Authentication | External Services: Support authentication of out of cluster service accounts for service-to-service access. Must support credential-based access, including long-lived credentials where required. If long-lived credentials (e.g. API keys) are issued, the platform will support configurable expiration and rotation. Need ownership attribution for all service accounts. The platform shall provide account information (such as detection of unused accounts). | add | active |
| SEC04 | Authorization (RBAC) | The platform shall enforce least-privilege RBAC for all managed services and infrastructure, featuring granular API actions (e.g. CRUD), scopes (e.g. dev vs staging vs prod), and function (e.g. image builder, provisioner, auditor). Roles and permissions shall be assignable to groups, with users inheriting access through group membership (GBAC); group membership may be sourced from OIDC claims and/or SCIM-provisioned groups. | add INFO | active |
| SEC05 | Identity / Directory Services | The platform shall integrate with the NVIDIA LDAP (RFC2307bis) directory service such that users identities and group membership can be resolved by dependent services for authentication and authorization decisions (e.g. storage - POSIX-based access control ) | INFO | active |
| SEC06 | Workload/Service Identity | Support standard workload, service, and node security identities using short-lived credentials, including OIDC-based workload identity federation and Kubernetes Service Accounts where applicable. | INFO | active |
| SEC07 | Admin Interfaces | All administrative interfaces—whether UI, CLI, or API—must be protected by Multi-Factor Authentication (e.g. mgmt API) | INFO | active |
| SEC08 | Audit Logs | Audit logs must be generated and retained for all security-relevant events, including management and control plane API calls, authentication events, and authorization decisions. Audit logs shall be retained for a minimum of 30 days and accessible to authorized platform operators. Must provide a log export mechanism (such as publishing to an S3 bucket). Exported logs should include sufficient metadata to identify tenant, project/account, region, service, resource identifier, actor, event timestamp, source IP where applicable, action, and authorization result. | add | active |
| SEC23 | Provisioning | The platform shall support SCIM 2.0 for automated user and group lifecycle management from enterprise identity providers. SCIM endpoints shall require authenticated and authorized access, support core User and Group resource operations, and synchronize group membership changes with the platform authorization engine. Synchronized groups shall be first-class RBAC objects targetable by role bindings and IAM policies; membership changes shall propagate promptly across all managed services. |  | active |
| SEC24 | Authentication | Must support domain-based IdP routing, mapping multiple email domains to a designated identity provider (e.g., nvidia.com and nvw.nvidia.com to the NVIDIA enterprise IdP) |  | active |
| SEC25 | Organization-Level Guardrail Policies | The platform shall support organization-level security guardrail policies that cascade across all subordinate tenant resources (networks, clusters, storage, compute) and cannot be weakened or bypassed by lower-level configuration. Policy violations shall be denied at resource creation/update time and recorded in audit logs. | add | active |
| SEC26 | SSO Enforcement | The platform shall allow authorized administrators to enforce federated SSO for a tenant, restricting local username/password and other non-federated login for regular users. Enforcement shall apply consistently across UI, CLI, API, and administrative interfaces. | add | active |
| SEC27 |  | The platform shall expose a programmatic mechanism to create and manage: - Isolation Units (e.g., projects, sub-project) - IAM Users - Service Accounts - Logs | add | active |

### Cryptography and Key Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC09 | Key & Certificate Lifecycle | The platform shall support secure issuance, distribution, storage, rotation, and revocation of cryptographic keys and certificates used across platform services. It shall support automated rotation of provider-managed and customer-managed keys and certificates, with configurable rotation intervals. Must be auditable. Must have an expiration date. | Add INFO | active |
| SEC10 | Key Usage | The platform shall support use of managed keys and certificates across platform services for encryption, authentication, and signing. | add | active |

### Network Isolation & Encryption

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC11 | Tenancy Model | Hard physical or logical isolation for network, data, and compute. Separation of control planes and tenants is mandatory. This includes separation of storage resources. Provide hierarchical tenancy (at least organization → project). | add | active |
| SEC12 | BMC Security | Out-of-band management (BMC) must be on a dedicated, restricted network (physically separate or VLAN/VRF-isolated). Direct access from the public internet or general corporate networks must be blocked, and only accessed via a hardened bastion (jumphost) server. | add | active |
| SEC13 | Network Traffic Encryption | Encryption and mutual authentication (mTLS or equivalent) for all east-west and north-south network traffic | add | active |

### Edge Network Security

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC14 | Private Access | No public internet access by default; all API endpoints (e.g. K8s API Server) must be restricted via firewall/private link. | INFO | active |
| SEC15 | Edge Network Security Policy | All traffic must be filtered via Security Groups and/or user customizable ACLs using 5-tuple rules. | INFO | active |
| SEC16 | Enforcement | NCP must specify the enforcement technology (e.g., Hardware firewalls, SDN, DPUs/SmartNICs) and its specific placement in the packet path. | INFO | active |
| SEC17 | Threat Intelligence & Scale | Ability to subscribe to GeoIP threat & Embargo feeds and import them into security groups. NCP should share the max supported records/rules. | INFO | active |
| SEC18 | MACSec protection links: | Protect links between NCP Data Center and NVIDIA POP | INFO | active |

### Hardware Security & Compliance

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| SEC19 | SOC 2 | SOC2 type 1 or better is required covering Security, Availability, and Confidentiality across all services and DC infrastructure | INFO | active |
| SEC20 | At-Rest Data Protection | Mandatory encryption of all data at rest (e.g. local NVMe/SSD, network-attached storage) via Self-Encrypted Drives (SED). | INFO | active |
| SEC21 | Data Sanitization | Data sanitization must be performed between tenants or on a hardware replacement, including cryptographic erase of all data drives between tenants; sanitization/wipe of any persistent or volatile memory including SRAM/GPU memory; resetting of TPM and BIOS | add | active |
| SEC22 | Root of Trust + Secure Boot. | Mandatory support across all platforms for Hardware Root of Trust mechanisms (TPM 2.0). The platform must enable UEFI OS Secure Boot w/ TPM 2.0. | INFO | active |

## Breakfix Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| BFX01 | Breakfix lifecycle | Compute: Power-cycle individual nodes or reset a VM instance. GPU: Reset GPUs on an individual node (as needed - k8s) Maintenance: Return/Report an individual node and a rack to the Provider for maintenance Cordon: Mark a node as unschedulable for new workloads (but finish existing) Replace: Request a host-replacement when health thresholds are breached | add | active |
| BFX02 | Breakfix Events | Query for any upcoming/current maintenance events for a node or rack Query for any retirement notices for a node/rack. Query for historical / status information for equipment repair. Event information should include: ticket open date ticket update date ticket close date Hardware Stable Identifier (e.g., node ID) Hardware category/type impacted (e.g., GPU, fan, interconnect) Maintenance/Error/fault description (some short description of the issue) Action: Categorization of action (e.g. repairs done on faulty GPUs to resolve the fault) Provider Account ID ticket ID Node Handover Date (Date when the node was deployed in Production) |  | active |
| BFX03 | Diagnostics | Identify serial numbers of installed hardware (chassis, baseboard, network adapters, CPU, GPU, etc). Obfuscated but stable identifiers are also OK. Inspect firmware versions of compute nodes and NV switch trays. |  | active |

## Storage Requirements

### Home Directory Storage

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| DIR01 | File Service uid/gid Quota feature | Configurable filesystem-wide limit, default user/gid quota settings, and per uid/gid overrides available. Usage accounting for uid/gids when the feature is enabled. | INFO | active |
| DIR02 | Must be NFS storage | NVIDIA requires NFSv4 protocol shared storage to work Access control based on DLs requires POSIX | INFO | active |
| DIR03 | Snapshots | The file system must support the ability to provide snapshot / restore functionality. |  | active |
| DIR04 | LDAP | File Service must support integration with an NVIDIA-managed LDAP (see SEC05) |  | active |

### High-Speed Storage Service Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| HSS01 | Provisioning APIs | Storage provisioning may be via vendor portal/API or NCP portal/API. | add | active |
| HSS02 | Performance | Must provision needed throughput requested for minimum bandwidth and IOPS. | INFO | active |
| HSS03 | Integration | K8s: CSI support Breakfix API required to report storage issues | INFO | active |
| HSS04 | Quota Support | Configurable filesystem-wide limit, default user/gid quota settings, and per uid/gid overrides available. Usage accounting for uid/gids when the feature is enabled. Configurable directory quota settings … it must be possible to apply a quota for a given directory. Usage accounting for directory quotas when enabled. | INFO | active |
| HSS05 | Upgrade, maintenance | Provider / NCP initiates desired maintenance. NVIDIA can schedule actual maintenance and can defer maintenance up to 2 weeks. Upgrades should be non-disruptive. | INFO | active |
| HSS06 | RDMA Memory Protection | Storage systems using RDMA must enforce memory protection via authorization keys for both local and remote access | INFO | active |

## These are capabilities required of the high-speed filesystem.

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| HSS07 | Parallel high speed filesystem | Parallel or multi-path high-speed filesystem that supports scaling to thousands of simultaneous clients while sustaining requested performance. | INFO | active |
| HSS08 | Single file system size | It must be possible to allocate a file system of at least 1 PiB even if the initial request is less. Growing to > 10PiB as cluster size increases. This hard requirement may be higher for a specific site and if so will be communicated via the ancillary services document. | INFO | active |
| HSS09 | Multiple Filesystems (fungible total capacity) | Can have >1 filesystem within our total capacity. Minimal file system size <= 50 TiB. | INFO | active |
| HSS10 | Filesystem expansion | Live file system expansion is supported, in terms of capacity, inodes, IO performance, and metadata operations performance. Performance should scale linearly with capacity. | INFO | active |
| HSS11 | Client | Ability to describe your client: In-Kernel, userspace, or bare-metal client installation requirements. Support integration with client kernels / OS used by NVIDIA, as needed. DKMS-enabled packages available for Ubuntu 20.04, 22.04, and 24.04-based operating systems. ARM64 versions compatible with GB200-ready kernels are mandatory, e.g. Linux 6.8.x. Managed Storage Service Provider will provide client configuration best practices and configuration guidelines for filesystem options and kernel module configuration to reliably achieve optimal performance on ARM and x86_64-based clients. | INFO | active |
| HSS12 | Quota (User, project & group) | Must support soft and hard quotas - uid / gid / project(directory)-id quotas with enforcement. | INFO | active |
| HSS13 | Root-squash | Nvidia needs to be able to enable or disable and manage root-squash at any time. | INFO | active |
| HSS14 | flock | It must be possible to mount the file system with flock. | INFO | active |
| HSS15 | Ability to Audit Changes | Enable Nvidia to have access to changelog data for filesystem auditing and detailed user operations tracking. Tracking by uid/gid, create files, create dirs, rename files, rename dirs, delete files, delete dirs | INFO | active |
| HSS16 | HA | All services are required to tolerate any critical component failure in the backend and provide continued client access to all storage services in such cases. | INFO | active |
| HSS17 | Multi-Node Coherency | One second or less for client attribute and dentry cache updates/invalidates | INFO | active |
| HSS18 | Client Multipathing | Clients must have multipathing to all storage servers | INFO | active |
| HSS19 | LDAP (for NFS) | NFS-based high-speed filesystem services must support integration with an NVIDIA-managed LDAP server (including unix uid group membership for users with > 16 group memberships) as per SEC05 |  | active |

### Data Movement Systems Requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| DMS01 | Dedicated K8s Cluster | Provider-managed k8s cluster (or ability to stand up our own) for Data Mover stack available ahead of the GPU cluster bringup to pre-stage data | add | active |
| DMS02 | Data Mover Nodes (CPU) | Dedicated CPU nodes for running data mover - needs high performance networking (exact quantity will be communicated via ancillary services doc) | INFO | active |
| DMS03 | Access to same GPU storage | Same filesystem as mounted on GPU nodes mounted on the Data Mover nodes (or ability to mount the same filesystem via CSI) | INFO | active |
| DMS04 | Access to nvidia corp net | Dedicate link (as described in network transport) to NVIDIA corp net, preferably with vpn, but otherwise with stable IP for allowlisting | INFO | active |
| DMS05 | Stable egress IP | Stable IP to IP allowlist access to Nvidia services. (e.g. similar to NAT Gateway) | add | active |

## Host Provisioning & Lifecycle

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| STG01 | Operating System Support | NCP must support a workflow that allows DGXC storage operators to integrate vendor-provided or storage-specific operating system images via bare-metal or VM provisioning for storage servers. The workflow must: (a) Allow DGXC to deploy custom OS images (e.g., vendor-enhanced kernels for Lustre, Rocky Linux, Ubuntu 20.04/22.04/24.04). | INFO | active |
| STG02 | Drive Sanitization Policy | Cryptographically erase data drive contents between storage system tenants with full attestation of host firmware. Must support an optional flag to skip drive sanitization during break/fix flows (e.g., power supply replacement) where tenancy does not change. Critical hardware component replacements may require sanitization without override, this is inclusive of GPU / CPU node local storage | add | active |
| STG03 | Stable IP Assignment | Storage nodes must support static IP addressing that remains stable during host lifecycle operations and does not reset between maintenance events. | INFO | active |
| STG04 | Out-of-Band Failure Detection | NCP must provide the ability to detect system failures out-of-band, including device, network, memory, and drive failures, enabling DGXC to proactively respond to hardware issues. | INFO | active |
| STG05 | Topology Observability | NCP must provide visibility into failure domains to enable DGXC to provision storage nodes with physical diversity. Storage systems must be able to provision nodes that purposefully span failure domains for resilience. | INFO | active |
| STG06 | BlueField/DPU Support | For storage systems utilizing BlueField-based architectures, the host provisioning system must support lifecycle management and specific configuration requirements for BlueField "JBOF" systems that export NVMe-oF to hosts. | INFO | active |

## Network Transport & Fabric Visibility

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| NET01 | Backend Switch Fabric API | For each compute node, the API must provide visibility into the backend network switches connecting the node to the core. Identification: Each switch must be identified by a unique, stable identifier. A "switch" may represent a physical switch or a logical connectivity domain. Structure: API may be gRPC or REST. Response structure may include multiple nodes (pagination expected). Topology: Switch info can be returned as an ordered array of IDs (e.g., leaf, spine, core) or separate fields for each tier | INFO | active |
| NET02 | NVLink Domain API | Requirement: For compute nodes supporting NVLink (e.g., GB200, GB300, Vera Rubin), the API shall return the unique identifier of the NVLink domain associated with each node. Implementation: Can be a separate API method or part of the Backend Switch Fabric API. | INFO | active |

## Transport and Networking requirements

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| NET03 | Non-Conflicting IP space allocation for the DGXC cluster | Bring Your Own IP (BYOIP): NCP shall support the ability for NVIDIA to bring and allocate its own IP private address space for DGXC GPU clusters. Stable IP: NCP shall provide a possibility to create static IP allocations that persist across instance restarts and re-creations. That includes floating IP allocations. DoD space: NCP shall support allocation and use of the 7.0.0.0/8 IPv4 address space for DGXC GPU cluster deployments. This IP space shall be considered equivalent to RFC1918 addresses Routing Support: NCP must support advertising and routing of BYOIP prefixes within the NCP environment and across interconnects (Private Cloud Interconnect, IPSec, etc.) | add | active |
| NET04 | Connection to NVIDIA CorpIT Network | Bandwidth: Low bandwidth (Up to 10Gbps). Transport: Private Cloud interconnect + VIF + BGP (preferred for better performance/security). DGXC will establish connectivity to NCP through a mutually agreed Point of Presence (POP) using Private Cloud Interconnect, functionally equivalent to AWS Direct Connect, GCP Dedicated Interconnect, Azure ExpressRoute, and OCI FastConnect. Connectivity will be provisioned with a Virtual Interface (VIF) and routing established via BGP. The interconnect will be used to exchange private IP space (RFC1918, as well as 7.0.0.0/8) between DGXC and NCP. | INFO | active |

## Connection to DGXC Storage

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| NET05 | Connection to DGXC Storage | Transport: Private Cloud interconnect + VIF + BGP (preferred for better performance/security). DGXC will establish connectivity to NCP through a mutually agreed Point of Presence (POP) using Private Cloud Interconnect, functionally equivalent to AWS Direct Connect, GCP Dedicated Interconnect, Azure ExpressRoute, and OCI FastConnect. Connectivity will be provisioned with a Virtual Interface (VIF) and routing established via BGP. The interconnect will be used to exchange private IP space (RFC1918, as well as 7.0.0.0/8) between DGXC and NCP. | INFO | active |
| NET06 | Cluster Local Internet Access | Cluster Internet access: Egress NAT IPs should be a static pool dedicated to only Nvidia Cluster/Tenancy/VPC. These persistent IP addresses must be used exclusively for DGXC traffic and shall not be shared with or carry traffic from other NCP tenants. Availability: Must support redundant upstream paths to ensure connectivity under failure. | INFO | active |

## Capacity & Fleet Management

| Req ID | Requirement Area | Description | Test details | Status |
| :----- | :--------------- | :---------- | :----------- | :----- |
| CAP01 | Governance metrics | Required Governance Metrics The core metrics needed to track fleet health are: Delivered: Nodes/GPUs provisioned and available to NVIDIA, allocated to a specific account/project/tenant. Healthy: Nodes/GPUs functioning and meeting SLA requirements, allocated to a specific account/project/tenant. Reserved: Resources allocated to a specific account/project/tenant. Total Active/In-Use: Nodes/GPUs currently in use within a specific account/project/tenant. | INFO | active |
| CAP02 | Resource Governance API Metrics | The Resource Governance API must return the following information for each node: Node ID (Unique identifier for a GPU node) Health State (Healthy/Unhealthy classification) Instance ID (Identifier for virtual workload) Creation Timestamp (Time workload/node was created) Hardware Type (Descriptor for the hardware model) GPU Count (Number of GPUs per node) Top-levelAccount/ID (Identifier for the top-level organization/account) Sub-LevelProject/ID (Identifier for the nested project/sub-account) In Use (True/False status indicating if the GPU Node is turned on and in use) Region (Region of the data center where nodes are deployed) | add | active |
| CAP03 | Resource Discovery APIs | It is not acceptable to have capacity be “handed” to DGXC through a phone, slack or email message. For example, when cluster first comes online, nodes/racks are likely being handed off weekly (or more frequently). Instead, please provide the following mechanism (and we can poll): Programmatic Capacity Discovery: All newly delivered capacity must be discoverable via a centralized API. This "Resource Index" must provide a stable resource identifier and some information on why it’s being provided (e.g. capacity fulfillment on gb300 project, break-fix / RMA return to cluster, etc) | add | active |
| CAP04 | Logical Compartmentalization & Resource Isolation | To ensure performance consistency and security, the NCP must support strict logical and physical isolation of NVIDIA’s reserved capacity. Capacity Reservations: A mechanism to logically group and "pin" a set of resources (compute, network, storage) to accounts (or equivalent constructs) in an NVIDIA tenancy Atomic Allocation: Support for reserving a "topology block" as a single unit, ensuring all resources in that block share identical performance characteristics and security boundaries. | INFO | active |
| CAP05 | Unified Health & Lifecycle APIs | NVIDIA requires a "single source of truth" for the health of both physical hosts and logical compute primitives. Per-Host Health: Real-time API access to the health bits of physical hardware (GPU state, thermal status, memory health). Primitive-Level Status: Health aggregation at the cluster, nodegroup, or reservation level to identify broad infrastructure failures (e.g., a spine switch failure affecting a whole block). | INFO | active |

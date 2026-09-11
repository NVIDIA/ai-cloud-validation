# NVIDIA Requirements for AI Clouds

| Version | Date | Description of Change |
| :---- | :---- | :---- |
| 2.1 | February 26, 2026 | Initial version |
| 2.2 | April 10, 2026 | Update to v2.2 |
| 2.3 | June 25, 2026 | Update to v2.3 |
| 2.4 | September 1, 2026 | Update to v2.4 with operational requirements added |

## Introduction

### Purpose and Intent

This document serves three main purposes:

1. **Setting requirements for NVIDIA Cloud Partners (NCP) delivering GPU capacity to NVIDIA.**
   This is the primary requirements document from NVIDIA to any NCP providing NVIDIA GPU and AI compute and software services.
   These requirements cover the full stack of AI cloud infrastructure services and operations needed to run NVIDIA DGX Cloud, expanding on the NVIDIA hardware reference design.
2. **Providing a reference set of requirements for the industry.**
   NVIDIA is publishing this document openly so that NCPs, GPU data center operators, and AI practitioners can use it as a reference for the capabilities a large GPU consumer requires.
3. **Defining the NVIDIA service delivery expectations.**
   NVIDIA expects services to be delivered as Generally Available (GA) to all customers, not as bespoke implementations built for NVIDIA alone.
   NVIDIA expects operational excellence, transparent communication, and proactive engagement from all partners.

NVIDIA will consider additional services that an NCP offers or plans to offer beyond what is described here.

> [!NOTE]
> Refer to the [Definitions](#definitions) section for defined terms used throughout this guide.

### Interpretation and Definitions

#### Interpretation

The interpretations and defined terms in this section apply throughout this guide.
A more specific definition or measurement rule stated for a particular requirement or service applies within that narrower scope.

Provider-specific product names and resource constructs may differ from the terminology used here.
Compliance is determined by whether the provider’s implementation satisfies the meaning and required outcome stated in this guide.

##### Normative Language

This guide uses the following normative terms to indicate requirement levels.

* Shall and must indicate a mandatory requirement.
* Should indicates a recommended but non-mandatory capability or practice.
  If a capability is intended to be required for compliance, the requirement must use shall or must.
* May indicates a permitted option.
* Preferred and nice-to-have indicate non-mandatory evaluation preferences and do not establish minimum compliance requirements.

##### Relationship to Deployment-Specific Requirements

An Ancillary Services Document or other deployment-specific agreement may establish site-specific quantities, topology, performance targets, schedules, and additional requirements.
It does not waive or weaken a baseline requirement in this guide unless it expressly identifies the affected requirement and the approved exception.

#### Definitions

The following table defines the terms used throughout this guide.

| Term | Definition |
| :---- | :---- |
| Ancillary Services Document | An engagement- or site-specific supplement defining quantities, topology, performance targets, schedules, and approved exceptions. It supplements this guide and does not waive a baseline requirement unless it expressly identifies the exception. |
| Auditable | Producing retained and queryable records sufficient to identify the actor, assumed role where applicable, action, target, time, and outcome and to reconstruct the relevant event or change. |
| Availability | The percentage of eligible measurement time during which a service satisfies its defined Available Condition. Eligible time excludes only expressly permitted exclusions. |
| Break-Fix | Corrective operations used to diagnose, isolate, repair, or replace failed infrastructure and return affected capacity or services to operation. |
| Break-Glass Access | Exceptional, time-limited privileged access used for emergency recovery when the normal access path cannot be used. Break-glass access uses a unique authenticated identity and is logged, automatically expired, and reviewed promptly after use. |
| Bring Your Own IP (BYOIP) | An IP prefix that the tenant is authorized to use and requests the NCP to accept, advertise, or route within the NCP environment. BYOIP is distinct from the internal reuse of address space that the tenant does not own or control publicly. |
| Common Vulnerabilities and Exposures (CVE) Identifier | An identifier assigned through the CVE program to a publicly disclosed cybersecurity vulnerability. A CVE identifier does not, by itself, indicate severity, exploitability, or active exploitation. |
| Compute Instance | A tenant-consumable execution environment provided as either a bare-metal instance or a virtual machine. |
| Critical Vulnerability | A vulnerability rated Critical under the scoring system and version identified in the applicable service agreement, or elevated to critical treatment under agreed criteria such as known exploitation, exposure, asset criticality, or tenant impact. |
| Cryptographic Erase | Data sanitization performed by irreversibly destroying all cryptographic keys required to decrypt the affected data, including applicable key copies, such that the data is computationally infeasible to recover. |
| Data Durability | The measure that data successfully acknowledged as stored remains retrievable without loss or silent corruption over the stated measurement period, excluding authorized deletion. |
| Data Sanitization | A verifiable process that renders tenant data infeasible to recover from the affected media or system under the applicable sanitization standard. |
| Declared Maintenance | A planned maintenance event formally communicated through the required mechanism with its affected scope, expected impact, Hand-Back Window, Maintenance Window, and tenant scheduling or deferral options. |
| Degraded | A condition in which a resource or service remains partially usable but no longer satisfies one or more defined health, performance, redundancy, or availability criteria. |
| Delivered Capacity | Capacity that has been handed over to the tenant, is programmatically discoverable, and is within the agreed service scope. |
| Delivered Service | An NCP-operated capability, resource class, or endpoint expressly included within the agreed service scope. Each delivered service has an identified service boundary and Available Condition. |
| DGXC-Managed Storage System | A storage system for which NVIDIA DGX Cloud operates the storage software or service layer while the NCP supplies and operates the underlying infrastructure according to the documented Shared Responsibility Model. |
| Emergency Maintenance | Urgent maintenance required to address an imminent material security, safety, data-integrity, or service-availability risk through an expedited process and potentially shortened Hand-Back Window. |
| Failure Domain | A set of resources capable of failing together because they share a dependency such as a host, rack, power source, network switch, storage controller, facility, or availability zone. |
| Fault | An abnormal component or service condition that may or may not yet affect a tenant. A fault becomes an Incident when it causes or meets the defined criteria for service, data, or security impact. |
| Generally Available (GA) | A production-supported service or capability offered through the NCP’s standard product, security, lifecycle, support, and service-level processes to eligible customers generally. |
| Hand-Back Window | The period beginning when maintenance is declared and ending at the deadline by which the tenant must initiate the maintenance or release the affected resource for maintenance. |
| High-Speed Filesystem | A tenant-visible filesystem intended for concurrent workload I/O and subject to the contracted throughput, IOPS, metadata-performance, capacity, and scale requirements. |
| High-Speed Storage Service | The NCP-managed provisioning, management, availability, maintenance, and recovery capability through which High-Speed Filesystems are delivered. |
| Home Directory Storage | Persistent file storage used primarily for per-user home directories and user-level state. |
| Idempotency | The property that retrying the same identified request does not create a duplicate operation or an unintended additional state change. Idempotency does not, by itself, provide durability. |
| Incident | An unplanned event that interrupts, degrades, or compromises a Delivered Service, tenant data, or tenant security. |
| Initial Response | The elapsed time from incident or ticket receipt until a qualified responder acknowledges the issue, assumes ownership, and begins active investigation. An automated receipt alone does not qualify. |
| Kubernetes API Server | The API endpoint of an individual Kubernetes cluster through which clients read and change cluster state. It is distinct from the Managed Kubernetes Service API used to create and manage clusters. |
| Kubernetes Cluster | A Kubernetes control plane and its registered nodes operating together as one cluster. |
| Kubernetes Cluster Control Plane | The components responsible for managing Kubernetes cluster state and behavior, including the Kubernetes API server, scheduling and controller components, and persistent cluster-state storage. |
| Kubernetes Cluster Data Plane | The Kubernetes nodes and supporting runtime components that execute scheduled tenant workloads. |
| Kubernetes Node | A Compute Instance registered as a worker in a Kubernetes cluster. A Kubernetes node may be backed by bare-metal or virtualized capacity. |
| Kubernetes ServiceAccount | A Kubernetes namespace-scoped identity used by workloads to authenticate to the Kubernetes API and, where configured, participate in workload identity federation. |
| Least Privilege | Granting a principal only the permissions, resource scope, and duration required to perform its assigned function. |
| Long-Lived Credential | A credential that remains usable beyond a short session until it expires or is revoked, such as a static API key or client secret. |
| Maintenance Window | The declared interval during which the NCP performs maintenance, completes required validation, and returns the affected resource or service to operation. |
| Major Incident | An Incident that activates the NCP’s formal major-incident process based on its mapped severity, breadth, duration, security or data impact, or coordination requirements. |
| Managed Kubernetes Cluster | A Kubernetes cluster provisioned for a tenant through the Managed Kubernetes Service. Also referred to as a Tenant Kubernetes Cluster. |
| Managed Kubernetes Service | An NCP-operated service that provisions and manages tenant Kubernetes clusters. The division of responsibility for cluster components is defined by the service’s Shared Responsibility Model. |
| Managed Kubernetes Service API | The NCP service API used to create, inspect, update, upgrade, scale, and delete Managed Kubernetes Clusters and to obtain their lifecycle and status information. |
| Measurement Interval | A fixed interval classified as available or unavailable, or compliant or noncompliant, according to the applicable service-level measurement method. |
| Measurement Period | The span of time over which service-level compliance is calculated. |
| Metering Accuracy | The percentage difference between reported consumption and the mutually agreed reference measurement over the applicable Measurement Period. |
| Mitigation | An action or compensating control that reduces a vulnerability’s exploitability or impact without eliminating the underlying vulnerability. |
| Mutating Request | A request intended to create, modify, or delete persistent service or cluster state. |
| NCP-Managed Service | A service for which the NCP is responsible for the provider-controlled lifecycle, operation, maintenance, availability, security, and recovery obligations assigned in the Shared Responsibility Model. |
| NVIDIA Cloud Partner (NCP) | The AI cloud provider responsible for delivering and operating the applicable compute, network, storage, managed-service, and supporting operational capabilities described in this guide. Sometimes referred to as infrastructure provider or cloud operator. |
| Non-Disruptive | Causing no tenant-visible loss of access, workload restart or eviction, connection or mount loss, data loss, or required tenant intervention. Maintaining full contracted performance is included only where the applicable requirement expressly states it. |
| NVLink Domain | A uniquely identified set of GPUs or systems participating in a common NVLink® or NVSwitch connectivity and management domain. |
| Outage | A period during which a Delivered Service does not satisfy its defined Available Condition. |
| Physical Host | An individual bare-metal server that provides the underlying compute resources for one or more Compute Instances. |
| Platform Service Account | A non-human principal in platform identity and access management used by software, services, or automation. It is distinct from a Kubernetes ServiceAccount. |
| Production Infrastructure | Compute, network, storage, control-plane, management, and supporting systems under NCP control whose operation or configuration can affect a Delivered Service or the confidentiality, integrity, or availability of tenant data or tenancy. |
| Project | A subordinate administrative and resource-management scope within a Tenancy that contains resources and identities and inherits applicable tenant-wide policies. |
| Recovery Point Objective (RPO) | The maximum amount of service data or state that may be lost following a disruption, expressed as time measured backward from the point of recovery. |
| Recovery Time Objective (RTO) | The maximum elapsed time after a disruption by which the specified service must be restored to its agreed operating level. |
| Remediation | An action that eliminates the exploitable condition associated with a vulnerability. A temporary Mitigation does not constitute remediation unless formally approved through the exception process. |
| Request Durability | The percentage of successfully acknowledged Mutating Requests whose intended state transition remains durably recorded and recoverable through a service failure. |
| Resolution | Permanent correction or formally agreed disposition of the underlying cause of an Incident or ticket. Resolution is distinct from Service Restoration and ticket closure. |
| Risk Acceptance | A documented and time-bounded decision by an authorized risk owner to accept identified residual risk, including its scope, rationale, compensating controls, expiration, and review requirements. |
| Security Incident | A credible suspected or confirmed event that jeopardizes the confidentiality, integrity, or availability of tenant data, credentials, tenancy, or NCP-managed infrastructure. |
| Service-Level Agreement (SLA) | An externally committed service level incorporating one or more measurable targets and defining the applicable scope, measurement method, Measurement Period, exclusions, reporting, and any applicable remedy. |
| Service-Level Objective (SLO) | A measurable service-performance target consisting of a metric, threshold, scope, and Measurement Period. An SLO is not, by itself, an SLA unless incorporated into the applicable agreement. |
| Service Restoration | Return of an affected service to its defined Available Condition, either through a permanent correction or an accepted Workaround. |
| Shared Responsibility Model | The documented assignment of operational, security, maintenance, availability, and recovery responsibilities between the NCP and tenant for each service layer. |
| Stable Identifier | A unique identifier that remains unchanged throughout the defined lifetime of a resource, including display-name changes, power-state changes, and maintenance events. |
| Stable IP Allocation | An IP-address reservation that remains associated with the same logical tenant resource through the documented lifecycle operations covered by the applicable requirement. Persistence following deletion and recreation requires an explicit reservation and reassociation mechanism. |
| System for Cross-Domain Identity Management (SCIM) 2.0 | A standards-based protocol for provisioning, updating, and deprovisioning users, groups, and group memberships. SCIM performs identity lifecycle management; it does not authenticate users. |
| Telemetry | Machine-readable metrics, logs, events, and alerts expressly required by this guide and made available through an agreed tenant-ingestion interface. Traces are included only where expressly required. |
| Tenant | The customer organization consuming NCP services. A tenant has multiple users. |
| Tenancy | The top-level customer administrative, policy, resource-ownership, and isolation boundary within the NCP environment. A Tenancy contains its subordinate projects, identities, policies, compute, network, storage, data, and managed-service resources. |
| Tenant Data | Data provided by or on behalf of a tenant and data generated specifically through the tenant’s use of a service, including applicable copies, replicas, backups, and metadata, except for categories expressly excluded by the applicable agreement. |
| Topology Block | A group of compute, network, storage, or supporting resources reserved or allocated as one unit because they share defined topology, performance, security, and failure-domain characteristics. |

#### Capacity Attributes

The following capacity terms are overlapping attributes, not mutually exclusive lifecycle states.
A resource can be Delivered, Reserved, Allocated, Healthy, and In Use at the same time.

| Term | Definition |
| :---- | :---- |
| Reserved Capacity | Capacity committed exclusively to a tenant and unavailable for allocation to another tenant, but not necessarily handed over or ready for use. |
| Delivered Capacity | Reserved capacity that has been handed over to the tenant and is programmatically discoverable within the agreed service scope. |
| Allocated Capacity | Delivered capacity associated with a specific tenant, project, cluster, reservation, or other defined resource scope. |
| Healthy Capacity | Delivered capacity that passes the agreed health and validation criteria and is capable of satisfying its contracted functionality and performance requirements. |
| Available Capacity | Healthy, Delivered Capacity that is ready for tenant provisioning or use and is not undergoing maintenance or otherwise unavailable. |
| In-Use Capacity | Allocated Capacity currently assigned to a running Compute Instance, cluster, or tenant workload. |

### Testing Compliance

NVIDIA aims to test conformance to all of the requirements described in this document.
Not all requirements are easily testable.
For example, many operational requirements and SLAs require measurement over long periods of time or during failure scenarios.
For requirements without a clear test mechanism, compliance is checked through a self-grading process and review.

#### AI Cloud Ready

Where possible, NVIDIA provides testing capabilities.
NVIDIA has created the [AI Cloud Ready test suite](https://github.com/NVIDIA/ISV-NCP-Validation-Suite).
This suite is continually evolving, but one of its main goals is to test as many of the requirements found in this document as possible.
Refer to the AI Cloud Ready documentation for details on how to run the tests.
Tests are added regularly, so review the [Requirements Test Matrix](https://github.com/NVIDIA/ai-cloud-validation/blob/main/docs/requirements/test-requirements-matrix.adoc) to find which tests validate which requirements.

#### Exemplar Performance

While some performance testing happens in AI Cloud Ready, the primary mechanism to validate real-world cluster performance is the NVIDIA Exemplar program.
This program seeks to improve performance per total cost of ownership (TCO) with hardware and software recipes, references, tools, and capabilities.
Run the latest publicly available benchmark test suite from the [NVIDIA Exemplar Performance repository](https://github.com/NVIDIA/exemplar-performance) and always pick the latest release version from that repository.
The release must be completed on one uniform hardware cluster type.
Run all the workloads for a given release and share the results in the following template.

| Req ID | Feature | Min Size | Description |
| :---- | :---- | :---- | :---- |
| **BM01** | Benchmarking for Exemplar Performance | Run per Scalable Unit, for example, a 512 GPU cluster | Achieve performance within 5% of an NVIDIA-provided target. This target should be met on every Scalable Unit (SU) handed off. |

## Service Levels

NCPs should be able to demonstrate the ability to meet the service-level agreements (SLAs) to be considered for NVIDIA consumption, which is sometimes called NVIDIA offtake or offtake.

Unless otherwise stated, published availability, response, performance, metering, and telemetry targets in this guide are minimum service levels to be incorporated into the applicable SLA.
Mandatory capabilities that are not measured service levels are stated as requirements.

### Support and Incident Response

The NCP may use its own published incident and ticket severity scale, and the NVIDIA expectation is at least three levels.
For NVIDIA services, SLA targets are measured against the following NVIDIA severity model: Sev-1 (critical or outage), Sev-2 (major or degraded), Sev-3 (minor), and Sev-4 (request or informational).

The NCP shall maintain a documented mapping from its severity scale to the NVIDIA model.
Where the NCP severity scale has fewer than four levels, or an NCP severity level spans more than one NVIDIA severity level, the more severe NVIDIA level and its associated targets shall apply.

The following response targets apply to the NVIDIA severity model.

- **Sev-1 (critical or outage) initial response**: 15 minutes.
  The NCP shall work continuously, 24x7, until service is restored or a workaround is available.
- **Sev-2 through Sev-4**: Response and resolution targets shall be defined and published by the NCP and measured against the corresponding NVIDIA severity level.

### Storage

The following service levels apply to NCP-provided storage.

- **Performance**: The NCP shall provision and sustain the contracted minimum performance.
  Performance below 85% of contracted targets sustained for 5 minutes or more constitutes an incident.
- **Availability**: 99.9%, measured per PB per month, defined as the fraction of time in which client mounts are healthy and no I/O request stalls beyond 60 seconds.
- **Durability**: 99.9999% per year against data loss or silent corruption, excluding tenant-initiated deletion.
  Data acknowledged as written must survive any single component failure.

### Managed Kubernetes Service

The definitions for the Managed Kubernetes Service elements are provided in the [Definitions](#definitions) section of this document.

The management service API must provide the following service levels.

- **Availability**: 99.95%, measured by the endpoint’s health status and ability to accept, durably store, and respond to the submitted requests.
- **Durability**: At least 99.999% of successfully acknowledged mutating service-management requests shall remain durably recorded and recoverable.
  Ensure that retrying such requests does not cause duplicate operations or unintended effects.
  Non-mutating requests need not be durably retained during service impairment.

The managed Kubernetes cluster control plane must provide the following service levels.

- **Availability**: 99.95% per managed cluster, measured by the API server endpoint’s health status and ability to accept, durably store, and respond to the submitted requests.
- **Durability**: At least 99.9% of successfully acknowledged mutating API requests.
  Idempotency must be guaranteed.

#### Managed Inference Endpoints (If Present)

Where offered, this SLA applies to each NCP-managed, tenant-facing production endpoint used to submit inference requests and receive model responses.
Management and control-plane APIs are covered separately.

- **Availability**: 99.9% per endpoint per calendar month, measured by the endpoint’s ability to accept and successfully complete conforming inference requests.

### Metering for Consumption Measurement

The following accuracy target applies to consumption metering.

- **Accuracy**: Within ±0.5% per month for shared or serverless capacity, and within ±0.1% per month for dedicated capacity.

### Telemetry

The following SLA applies to all required telemetry, measured from observation at the source until the record is available through the agreed ingestion interface.

- **Delivered Latency**: The NCP shall make at least 99.95% of required telemetry records (including metrics and logs) available within 120 seconds of observation at the source.

## Functional Requirements

This section describes the functional requirements across a wide range of domains.

### Compute and Network Provisioning

This section outlines the requirements for provisioning compute and network resources.
Compute instances can be provided as either bare-metal instances through Bare Metal-as-a-Service (BMaaS) or virtual machines through Virtual Machine-as-a-Service (VMaaS) to support the NVIDIA DGX Cloud engagement.
All operations must be controlled through a fully documented and secure API.
All systems are expected to scale and perform at scale.

#### General, Compute, and Lifecycle Management

The following table lists the general compute and lifecycle management requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **CNP01** | API/CLI Access | DGXC must have API access to the NCP provisioning system for: (1) node lifecycle management (create, update, delete, and list) and power-state management (reboot and power cycle); (2) network configuration; (3) resource and topology discovery; (4) access configuration (users, service accounts, groups, and roles); and (5) maintenance and operations, as described in the Fleet Health and Maintenance section. |
| **CNP02** | Multi-step Workflow | For workflows requiring multiple operations, the NCP shall provide a documented and automatable method that identifies required sequencing, dependencies, completion states, and handling of partial failures. The method may use supported APIs or an infrastructure-as-code solution. |
| **CNP03** | NVLink-Aware Allocation | For NVL72, the API must support NVLink domain-aware allocation. |
| **CNP04** | Resource States | Must support clear resource states, for example, instance and network states, where applicable. Example states include provisioning, running, degraded, maintenance required, stopping, stopped, terminating, and terminated. |
| **CNP05** | Tagging | Support for user-defined tags and labels, and `cloud-init` metadata on instances. |
| **CNP06** | Console Access | For each compute resource assigned to NVIDIA, the NCP must provide access to the resource’s console: the guest-instance console for virtualized capacity, or the host OS console for bare-metal capacity. Read-only access is sufficient, and interactive access is preferred. Console output must be retained for at least one day, and 30 days is preferred. |
| **CNP07** | If VMaaS Offered: <br>VMs per Node | GPU nodes: NVIDIA deploys one VM per node, and support for one VM per GPU is a nice-to-have.<br> General-purpose CPU nodes: Support multiple VMs per node. Actual deployments are selected through a memory and core-count shape. |
| **CNP08** | Stable Identifiers | All resources, for example, nodes and switches, must have a stable and persistent ID that does not change during the lifespan of the resource, even when the resource goes offline for a service event. VMs must also have a stable identifier. |
| **CNP09** | Firmware | All firmware must be brought to a known good state between tenants. The NCP should implement a robust firmware update process. All firmware must be cryptographically signed and attested during boot. |
| **CNP10** | Remote Management | The out-of-band management interface for each physical compute node must support Redfish over TLS. IPMI must be disabled. |

#### Boot Process and Disks

The following table lists the boot process and disk requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **BOOT01** | Image Deployment and Updates | API-driven workflow allowing DGXC to deploy, update, and manage vendor-provided or tenant-supplied OS images for bare metal or virtual machine instances. |
| **BOOT02** | Guest Metadata | The control plane must make tenant-defined instance metadata and bootstrap configuration available to the guest OS through a documented metadata interface. The interface must be compatible with `cloud-init`. |
| **BOOT03** | Custom Disk Images | Support tenant-created custom OS images in documented formats such as `raw` and `qcow2`, and provide API-based image lifecycle management. Tenant administrators shall be able to share or replicate images across tenant projects or environments. |
| **BOOT04** | Node Local Storage | GPU and CPU nodes support access to node local storage, either NVMe or SSD, for use as scratch storage or for caching services. |

#### SDN and Virtual Networking

This section covers the virtual networking requirements.
Physical transport and network requirements are described in the Transport and Networking Requirements section.

The following table lists the software-defined networking and virtual networking requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **SDN01** | Virtual Networking | Full API lifecycle management (create, read, update, delete, and list) for software-defined private networks. Must support non-conflicting BYOIP, including `7.0.0.0/8`, and stable tenant IP allocations. Applicable to all types of tenant-managed resource nodes, such as CPU, GPU, and storage nodes. |
| **SDN02** | Security Groups | Support for VPC-style security groups, or an equivalent, including IP and CIDR-based allow and deny rules. Must define scope and application at the workload, node, service, and subnet or tenant levels. An example service is the Kubernetes API service. |
| **SDN03** | Security Operations | Full API capabilities to manage security groups, including defined audit processes. |
| **SDN04** | Tenant Isolation | Logical or physical network isolation between tenants across all networks. One tenant shall not be accessible to or observable by another tenant. Out-of-band infrastructure management interfaces, including BMCs, shall be isolated from all tenant-accessible networks. |
| **SDN05** | Floating/Movable IP | Ability to switch a floating private IP between nodes, automatically or through an API, in less than 10 seconds and without requiring an instance reboot. |
| **SDN06** | Localized DNS | Support for tenant-defined localized DNS configuration to enable internal domain resolution to private endpoints, such as storage endpoints. |
| **SDN07** | Virtual Network Peering | Support for cross-virtual-network connectivity with full bandwidth and no hairpin routing. |
| **SDN08** | Storage Mesh Connectivity | The virtual network from SDN01 must provide unrestricted L3 routing between all storage hosts, enabling full-mesh, all-to-all communication across different subnets without going through a gateway. |
| **SDN09** | Observability | The NCP shall provide tenant-visible telemetry sufficient to identify and troubleshoot tenant-impacting network faults and performance degradation, including degraded states, errors, drops, utilization, and latency across applicable network domains. |
| **SDN10** | DNS Private Domain | Must allow each node’s DNS resolver to forward tenant-defined private domains, for example, `*.nvidia.com`, to a tenant-specified DNS server. |

### Kubernetes as a Service (KaaS) Requirements

#### Kubernetes Conformance, Versioning, and Compliance

The following table lists the Kubernetes conformance, versioning, and compliance requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **K8S01** | Certified Versions | Certified upstream versions: official CNCF-certified versions only, with no proprietary forks, that pass the standard Kubernetes conformance tests. |
| **K8S02** | Version Updates | Support the three most recent minor releases within the maintenance window. New minor versions must be available within 4 to 6 weeks of the upstream release. Control-plane security patching must be automated. |
| **K8S03** | EOL Policy | Defined notification periods for version deprecation. |
| **K8S04** | Kubernetes Security Response | Must participate in the Kubernetes Security Response Committee (SRC) process.  Must be attempting to join if not part of the security committee. Must be able to:<ul><li>Responsibly disclose any discovered vulnerabilities to the Kubernetes SRC</li><li>Receive and respond to embargo notifications from the SRC</li><li>Patch disclosed vulnerabilities in the managed service during embargo prior to public disclosure and in compliance with direction provided from the Kubernetes SRC ensuring that the patching process does not violate embargo or SRC guidance.</li></ul> |

#### Kubernetes Operational Excellence

The following table lists the Kubernetes operational excellence requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **K8S05** | Lifecycle Management - Control Plane | API for CRUD provisioning; less than 30 min control plane bring-up. |
| **K8S06** | Lifecycle Management - Node Pool | <ul><li>API/CLI/Terraform for CRUD provisioning ( e.g., create node pool, update node pool, delete node pool, scale a node pool to a target count).<ul><li>Must be able to specify node type (specific CPU or GPU instance type) including CPU-only node pools with high-performance networking for data movement and ingest workloads</li></ul></li><li>Ability to specify default node labels and node taints within a node pool when a node joins the cluster.</li><li>When down-scaling a node pool, ability to down-scale bad/specific nodes.</li></ul> |
| **K8S07** | API Server Metrics | Share Prometheus-compatible API server metrics sufficient to measure availability, request rate, latency, queueing, throttling, rejected requests, and control-plane saturation. |
| **K8S08** | Versioning | Provider-managed control plane upgrade processes. |
| **K8S09** | Zero-Downtime Upgrades | Minor version control plane updates without application downtime or maintenance windows. |
| **K8S10** | Node Upgrades | User-initiated rolling updates that respect pod disruption budgets. |
| **K8S11** | HA Control Plane | Redundant architecture with `etcd` separation. |
| **K8S12** | Backup and Disaster Recovery | Supported recovery within a defined RPO and RTO. Recovery must be auditable and testable. |

#### Robust Kubernetes Security

The following table lists the Kubernetes security requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **K8S14** | Control Plane Isolation | Per-tenant Kubernetes control plane nodes must be separate from worker nodes and outside of the tenant cluster or VPC. |
| **K8S15** | Access Controls | Cluster endpoint must provide network access controls. |
| **K8S16** | IAM Integration | Kubernetes Service Accounts shall integrate with the platform IAM system to enable workloads to assume platform-managed identities and roles with appropriate scopes. |
| **K8S17** | Service Accounts | Kubernetes shall support standard Service Accounts and projected tokens as the workload identity mechanism, including a cluster-specific OIDC issuer to enable workload identity federation. The cluster shall expose OIDC discovery and JWKS endpoints that are reachable by configured external identity consumers, such as AWS IAM and GCP workload identity. |
| **K8S19** | Encryption | At-rest encryption for `etcd` and secrets. |
| **K8S20** | Logging | Ability to view or export Kubernetes control plane logs, including `kube-apiserver` and `kube-controller-manager` logs. |

#### Kubernetes Component and Extension Requirements

The following table lists the Kubernetes component and extension requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **K8S22** | CNI | Standard compliance, with support for network policies. IPv4 and IPv6 dual-stack support is desired. |
| **K8S23** | CSI | The NCP provides CSI drivers with dynamic provisioning and volume expansion for block, home directory, and high-speed storage offerings. NVIDIA has the option to install the drivers through Helm or Kustomize for testing or customization as needed. <ul><li>CSI credentials are scoped to the tenant cluster, with the ability to isolate storage access by cluster.</li><li>APIs are provided to query storage usage at the tenant and cluster level, with per-PVC, per-volume, per-directory, and per-user quotas, and usage reporting to manage utilization across and within PVCs.</li><li>Vendor-provided storage kernel modules and tools are provided in one of three ways: installed by the CSI driver, preinstalled in an NCP-provided machine image, or supplied as installable packages.</li></ul> |
| **K8S24** | DRA | Dynamic Resource Allocation (DRA) must be enabled regardless of upstream feature status, whether beta or GA. Some DRA features require enabling feature gates for the control plane so that customers can run AI workloads with new DRA features. |
| **K8S25** | Operator Support | Support standard operator-based management of hardware accelerators and associated drivers. Provider-default accelerator operators and drivers shall be replaceable or overridable to allow installation of tenant-required operator and driver versions, for example, GPU Operator and Network Operator. Provide golden configurations for GPU Operator and Network Operator. |

#### Kubernetes Functionality

The following table lists the Kubernetes functionality requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **K8S26** | Clusters | Support multiple clusters in the same tenancy, and support multiple clusters in the same VPC. |
| **K8S27** | Kubernetes Control Plane Size Pinning | Pin control plane instances to handle a particular load limit. |
| **K8S28** | Performance | Meet the standard Kubernetes performance test certified up to 5,000 nodes, or to the maximum size of the cluster, whichever is smaller, and size the control plane as necessary. The managed Kubernetes control plane SLO and performance must meet or exceed the standard Kubernetes results. |
| **K8S29** | Kubernetes LoadBalancer Service Support | The platform shall support Kubernetes Service resources of type LoadBalancer, including:<ul><li>External load balancers with publicly routable IPs</li><li>Internal load balancers with private IPs reachable via private network access</li><li>Static IP assignment.</li></ul> |
| **K8S30** | DNS Configuration | The platform shall support configuring Kubernetes internal DNS, such as CoreDNS, with conditional forwarding rules for specified DNS zones to designated enterprise or internal DNS resolvers. |
| **K8S31** | Configurable Kubernetes CIDR Ranges | Ability to configure the Kubernetes service IP range, node IP range, and pod IP range. |
| **K8S32** | API Priority and Fairness | The managed Kubernetes control plane shall use Kubernetes API Priority and Fairness (APF) to assign requests to multiple priority levels, protect critical system and administrative operations from lower-priority traffic, and prevent any single client or workload from monopolizing API-server capacity during overload. The NCP shall publish its request processing latency SLOs per priority. |

### Security and Identity Management

#### Identity and Access Management (IAM)

The platform must provide a centralized system for authentication, identity federation, authorization, and lifecycle provisioning across all platform services.
It shall integrate with a trusted external or platform-hosted identity provider and consume OIDC-based identity tokens for user authentication.
Upstream identity sources and protocols, such as enterprise directories, may be used through federation with the identity provider.

The following table lists the identity and access management requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **SEC01** | Authentication | **Users**: Support federated single sign-on with a customer’s enterprise identity provider using OIDC or SAML 2.0. OIDC shall be supported for CLI and API authentication, including human access to managed Kubernetes API servers. OIDC-issued tokens shall be validated for signature, issuer, audience, expiration, and required identity and group claims. |
| **SEC03** | Authentication | **External Services**: Support authentication of out-of-cluster service accounts for service-to-service access. Must support credential-based access, including long-lived credentials where required. If long-lived credentials, such as API keys, are issued, the platform shall support configurable expiration and rotation. Ownership attribution is required for all service accounts. The platform shall provide account information, such as detection of unused accounts. |
| **SEC04** | Authorization (RBAC) | The platform shall enforce least-privilege RBAC for all managed services and infrastructure, featuring granular API actions such as CRUD, scopes such as development, staging, and production, and functions such as image builder, provisioner, and auditor. Roles and permissions shall be assignable to groups, with users inheriting access through group membership (GBAC). Group membership may be sourced from OIDC claims, SCIM-provisioned groups, or both. |
| **SEC05** | Identity and Directory Services | The platform shall integrate with the NVIDIA LDAP (RFC2307bis) directory service such that users’ identities and group membership can be resolved by dependent services for authentication and authorization decisions. |
| **SEC06** | Workload/Service Identity | Support standard workload, service, and node security identities using short-lived credentials, including OIDC-based workload identity federation and Kubernetes ServiceAccounts where applicable. |
| **SEC07** | Admin Interfaces | All administrative interfaces, whether UI, CLI, or API, must be protected by multifactor authentication (MFA). One example is a management API. |
| **SEC08** | Audit Logs | Audit logs must be generated and retained for all security-relevant events, including management and control plane API calls, authentication events, and authorization decisions. Audit logs shall be retained for a minimum of 30 days and accessible to authorized platform operators. Must provide a log export mechanism, such as publishing to an S3 bucket. Exported logs should include sufficient metadata to identify the tenant, project or account, region, service, resource identifier, actor, event timestamp, source IP where applicable, action, and authorization result. |
| **SEC23** | Provisioning | The platform shall support SCIM 2.0 for automated user and group lifecycle management from enterprise identity providers. SCIM endpoints shall require authenticated and authorized access, support core User and Group resource operations, and synchronize group membership changes with the platform authorization engine. Synchronized groups shall be first-class RBAC objects targetable by role bindings and IAM policies, and membership changes shall propagate promptly across all managed services. |
| **SEC24** | Authentication | Must support domain-based IdP routing, mapping multiple email domains to a designated identity provider. For example, map `nvidia.com` and `nvw.nvidia.com` to the NVIDIA enterprise IdP. |
| **SEC25** | Organization-Level Policies | The platform shall support organization-level security guardrail policies that cascade across all subordinate tenant resources, such as networks, clusters, storage, and compute, and that cannot be weakened or bypassed by lower-level configuration. Policy violations shall be denied at resource creation or update time and recorded in audit logs. |
| **SEC26** | SSO Enforcement | The platform shall allow authorized administrators to enforce federated SSO for a tenant, restricting local username and password login and other non-federated login for regular users. Enforcement shall apply consistently across UI, CLI, API, and administrative interfaces. |
| **SEC27** | Account Management | The platform shall expose a programmatic mechanism to create and manage:<ul><li>Isolation Units (e.g., projects, sub-project)</li><li>IAM Users</li><li>Service Accounts</li></ul> |

#### Cryptography and Key Management

The following table lists the cryptography and key management requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **SEC09** | Key and Certificate Lifecycle | The platform shall support secure issuance, distribution, storage, rotation, and revocation of cryptographic keys and certificates used across platform services. It shall support automated rotation of provider-managed and customer-managed keys and certificates, with configurable rotation intervals. Keys and certificates must be auditable and must have an expiration date. |
| **SEC10** | Key Usage | The platform shall support use of managed keys and certificates across platform services for encryption, authentication, and signing. |

#### Network Isolation and Encryption

The following table lists the network isolation and encryption requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **SEC11** | Tenancy Model | Hard physical or logical isolation for network, data, and compute. Separation of control planes and tenants is mandatory. This includes separation of storage resources. Provide hierarchical tenancy with at least an organization level and a project level. |
| **SEC12** | BMC Security | Out-of-band management (BMC) must be on a dedicated, restricted network that is either physically separate or VLAN-isolated or VRF-isolated. Direct access from the public internet or general corporate networks must be blocked, and access must be permitted only through a hardened bastion, or jumphost, server. |
| **SEC13** | Network Traffic Encryption | Encryption and mutual authentication for all east-west and north-south network traffic. |

#### Edge Network Security

The following table lists the edge network security requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **SEC14** | Private Access | No public internet access by default. All API endpoints, such as the Kubernetes API server, must be restricted through a firewall or private link. |
| **SEC15** | Edge Network Security Policy | All traffic must be filtered through security groups, user-customizable ACLs, or both, using 5-tuple rules. |
| **SEC16** | Enforcement | The NCP must specify the edge network enforcement technology, such as hardware firewalls, SDN, or DPUs and SmartNICs, and its specific placement in the packet path. |
| **SEC17** | Threat Intelligence and Scale | Ability to subscribe to GeoIP threat and embargo feeds and import them into security groups. The NCP should share the maximum supported number of records and rules. |
| **SEC18** | MACsec Protection Links | Protect links between the NCP data center and the NVIDIA POP. |

#### Hardware Security and Compliance

The following table lists the hardware security and compliance requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **SEC19** | SOC 2 | SOC 2 Type 1 or better is required, covering Security, Availability, and Confidentiality across all services and data center infrastructure. |
| **SEC20** | At-Rest Data Protection | Mandatory encryption of all data at rest, such as data on local NVMe or SSD storage and on network-attached storage. |
| **SEC21** | Data Sanitization | Data sanitization must be performed between tenants or on a hardware replacement. Sanitization includes cryptographic erase of all data drives between tenants, sanitization or wipe of any persistent or volatile memory including SRAM and GPU memory, and resetting of the TPM and BIOS. |
| **SEC22** | Root of Trust and <br>Secure Boot | Mandatory support across all platforms for hardware root of trust mechanisms (TPM 2.0). The platform must enable UEFI OS Secure Boot with TPM 2.0. |

### Break-Fix Requirements

The NCP must provide a specific break-fix API to support fleet reliability.
Any node-level remediation must not affect other parts of the tenancy.
Specifically, NVLink must be reconfigured properly to take a node out of the tenancy.

The API must enable the actions in the following table.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **BFX01** | Break-Fix Lifecycle | **Compute**: Power-cycle individual nodes or reset a VM instance.<br>**GPU**: Reset GPUs on an individual node (as needed - K8s).<br>**Maintenance**: Return/Report an individual node and a rack to the Provider for maintenance.<br>**Cordon**: Mark a node as unschedulable for new workloads (but finish existing).<br>**Replace**: Request a host-replacement when health thresholds are breached. |
| **BFX02** | Break-Fix Events | <ul><li>Query for any upcoming/current maintenance events for a node or rack.</li><li>Query for any retirement notices for a node/rack.</li><li>Query for historical/status information for equipment repair.</li><li>Event information should include:<ul><li>ticket open date</li><li>ticket update date</li><li>ticket close date</li></ul></li><li>Hardware Stable Identifier (e.g., node ID)</li><li>Hardware category/type impacted (e.g., GPU, fan, interconnect)</li><li>Maintenance/Error/fault description (some short description of the issue)</li><li>Action: Categorization of action (e.g. repairs done on faulty GPUs to resolve the fault)</li><li>Provider Account ID</li><li>Ticket ID</li><li>Node Handover Date (Date when the node was deployed in Production)</li></ul> |
| **BFX03** | Diagnostics | <ul><li>Identify serial numbers of installed hardware (chassis, baseboard, network adapters, CPU, GPU, etc.). Obfuscated but stable identifiers are also OK.</li><li>Inspect firmware versions of compute nodes and NV switch trays.</li></ul> |

### Telemetry Requirements

The telemetry requirements consist of two core components that require alignment between DGX Cloud and the NCP:

1. **Delivery method.** This component defines how the NCP delivers telemetry to DGX Cloud for ingestion.
2. **Telemetry scope.** This component defines what telemetry the NCP delivers to DGX Cloud.

All telemetry should be delivered within the latency target defined in the Telemetry service-level section.

#### Delivery Method

The NCP must deliver all required telemetry, including metrics and logs, in a manner that allows for ingestion into DGX Cloud systems with a latency of no longer than 120 seconds.
Native OpenTelemetry Protocol delivery is preferred.

#### Telemetry Scope

NVIDIA will provide to the NCP, at the appropriate time in the engagement, a list of the telemetry artifacts, including the required metrics and logs.
After receipt, the NCP must provide a formal written response that details the following:

* Confirmation of its ability to deliver the specified metrics and logs.
* Projected timelines for delivery.
* Specific technical details, including metric names, label names, and label values.

#### Network Telemetry

The NCP must provide network telemetry across the following domains:

* North-south, or front-end, network, including client-facing and external interconnects.
* East-west, or back-end, network, including GPU-to-GPU interconnects.
* Management network, including control plane and orchestration traffic.
* NVSwitch fabric, including intra-node GPU switching, applicable only to GB200 and later clusters.
* Host network, including NIC-level and server connectivity.

#### Storage Telemetry

The NCP storage services shall provide telemetry using the common delivery method and delivery target defined in the Telemetry service levels.

The required key telemetry includes the following:

* End-to-end visibility covering both the client and storage service, including provisioned and maximum throughput, IOPS, metadata operations, and latency.
  Granularity by filesystem, volume, client, Kubernetes pod, and Slurm job ID, where applicable.
* Capacity, including used, available, and total capacity, and file, object, and inode counts.
* Client health and I/O state, including connection status, queue depth, outstanding and blocked I/O, errors, retries, timeouts, throttling, failovers, and degraded states.
* Alerting on critical events, including inode count, capacity, performance saturation, and component failure.

#### Logs

DGX Cloud requires the NCP to provide logs from various network technologies, including but not limited to the following:

1. Fabric Manager logs for the NVLink domain, where applicable.
2. Subnet Manager logs for the NVLink domain, where applicable.
3. VPC flow logs for all ingress and egress traffic.
4. UFM event logs.
5. General switch logs.
6. Switch syslogs.
7. Switch kernel logs.
8. BMC SEL logs.
9. Syslogs.
10. Management logs.

### Storage Requirements

The NCP must provide shared storage solutions, where applicable, that are manageable through standard APIs and UIs, including auditing rights for NVIDIA access.

#### File System Storage for Home Directory and High-Speed File System

The following table lists the requirements that apply to both home directory storage and high-speed file system storage.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **DIR01** | Quota Support | The service must support configurable filesystem-wide limits with per-UID, per-GID, and per-directory quota settings and overrides. Usage accounting must be available for all quota types when enabled. |
| **DIR02** | Filesystem Semantics | The NFS interface must be NFS 4.1 or later. File storage must provide POSIX filesystem semantics, including file locking using `flock()`. |
| **DIR03** | Snapshots | The file system must provide snapshot and restore functionality. |
| **DIR04** | LDAP | The file service must support integration with an NVIDIA-managed LDAP directory service, as described in SEC05.<br><br>NFS storage must support storage system LDAP group enumeration through RFC2307bis to resolve full group membership for users belonging to more than 16 groups. |

#### High-Speed Storage Service Requirements

The following table lists the requirements for provisioning and interacting with the provider’s service offering.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **HSS01** | Provisioning APIs | Storage provisioning may be performed through a vendor portal or API, or through an NCP portal or API. |
| **HSS02** | Performance | Must provision the requested throughput for minimum bandwidth and IOPS. |
| **HSS03** | Integration | Kubernetes CSI support is required. A break-fix API is required to report storage issues. |

#### High-Speed Filesystem Requirements

The following table lists the capabilities required for the high-speed filesystem.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **HSS07** | Parallel High-Speed Filesystem | Parallel or multi-path high-speed filesystem that supports scaling to thousands of simultaneous clients while sustaining requested performance. Must be accessible to all nodes in the tenancy, whether GPU or CPU nodes. |
| **HSS08** | Single File System Size | It must be possible to allocate a file system of at least 1 PiB even if the initial request is smaller, and to grow it beyond 10 PiB as cluster size increases. This hard requirement may be higher for a specific site, and any higher value is communicated through the Ancillary Services Document. |
| **HSS09** | Multiple Filesystems (Fungible Total Capacity) | More than one filesystem is supported within the total capacity. The minimum file system size is 50 TiB or less. |
| **HSS10** | Filesystem Expansion | Live file system expansion is supported, in terms of capacity, inodes, I/O performance, and metadata operations performance. Expanded services must meet contracted performance targets. |
| **HSS11** | Client | <ul><li>Ability to describe your client: In-Kernel, userspace, or bare-metal client installation requirements.</li><li>Support integration with client kernels / OS used by NVIDIA, as needed.</li><li>DKMS-enabled packages available for Ubuntu 20.04, 22.04, and 24.04-based operating systems.</li><li>ARM64 versions compatible with GB200-ready kernels are mandatory, e.g. Linux 6.8.x.<br><br>Managed Storage Service Provider will provide client configuration best practices and configuration guidelines for filesystem options and kernel module configuration to reliably achieve optimal performance on ARM and x86_64-based clients.</li></ul> |
| **HSS13** | Root-squash | NVIDIA needs to be able to enable, disable, and manage root-squash at any time. |
| **HSS15** | Ability to Audit Changes | Enable NVIDIA to access changelog data for filesystem auditing and detailed user operations tracking. <br>Tracking must cover UID and GID, file creation, directory creation, file renaming, directory renaming, file deletion, and directory deletion. |
| **HSS16** | High Availability | All services are required to tolerate any critical component failure in the backend and provide continued client access to all storage services in such cases. |
| **HSS17** | Multi-Node Coherency | One second or less for client attribute and dentry cache updates and invalidations. |
| **HSS18** | Client Multipathing | Clients must support multipathing to multiple endpoints for all storage servers. Multiple client NICs are not required. |

#### Data Movement Systems Requirements

The data movement system is used to copy data from an external data source, such as NVIDIA or another cloud, to the NCP data center.

The following table lists the data movement system requirements.

| Req ID | Requirement | Description |
| :---- | :---- | :---- |
| **DMS01** | Dedicated Kubernetes Cluster | A provider-managed Kubernetes cluster, or the ability for NVIDIA to stand up its own cluster, for the data mover stack. The cluster must be available ahead of the GPU cluster bring-up so that data can be pre-staged. |
| **DMS02** | Data Mover Nodes (CPU) | Dedicated CPU nodes for running the data mover, with high-performance networking. The exact quantity is communicated through the Ancillary Services Document. |
| **DMS03** | Access to Same GPU Storage | The same filesystem that is mounted on GPU nodes must be mounted on the data mover nodes, or it must be possible to mount the same filesystem through CSI. |
| **DMS04** | Access to the NVIDIA Corporate Network | A dedicated link, as described in the network transport requirements, to the NVIDIA corporate network. A VPN is preferred, and a stable IP for allowlisting is otherwise required. |
| **DMS05** | Stable Egress IP | A stable IP for allowlisted access to NVIDIA services, similar to a NAT gateway. |

#### DGXC-Managed Storage System Deployment

For scenarios where DGXC, rather than the NCP, deploys and manages the storage-system software, the following requirements apply.
These requirements enable DGXC to operate storage systems, such as high-speed parallel filesystems, capacity object storage, or block storage, using NCP-provided infrastructure while maintaining operational control.
For storage systems provided by the NCP, disregard this section.

##### Host Provisioning and Lifecycle

The following table lists the host provisioning and lifecycle requirements for DGXC-managed storage systems.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **STG01** | Operating System Support | The NCP must support a workflow that allows DGXC storage operators to integrate vendor-provided or storage-specific operating system images through bare-metal or VM provisioning for storage servers. The workflow must allow DGXC to deploy custom OS images, for example, vendor-enhanced kernels for Lustre, Rocky Linux, and Ubuntu 20.04, 22.04, and 24.04. |
| **STG02** | Drive Sanitization Policy | Cryptographically erase data drive contents between storage system tenants, with full attestation of host firmware. Must support an optional flag to skip drive sanitization during break-fix flows, such as a power supply replacement, where tenancy does not change. Critical hardware component replacements may require sanitization without an override, and this includes GPU and CPU node local storage. |
| **STG03** | Stable IP Assignment | Storage nodes must support static IP addressing that remains stable during host lifecycle operations and does not reset between maintenance events. |
| **STG04** | Out-of-Band Failure Detection | The NCP must provide the ability to detect system failures out-of-band, including device, network, memory, and drive failures, enabling DGXC to proactively respond to hardware issues. |
| **STG05** | Topology Observability | The NCP must provide visibility into failure domains to enable DGXC to provision storage nodes with physical diversity. Storage systems must be able to provision nodes that purposefully span failure domains for resilience. |
| **STG06** | BlueField/DPU Support | For storage systems that use BlueField-based architectures, the host provisioning system must support lifecycle management and specific configuration requirements for BlueField JBOF systems that export NVMe-oF to hosts. |

### Network Transport and Fabric Visibility

#### Backend Switch Fabric API

The purpose of this API is to expose sufficient information about the cluster network topology to enable efficient scheduling, placement, and optimization of multi-node GPU workloads.
Understanding the network hierarchy between compute instances and switches, as well as intra-node and inter-node NVLink domains, is essential for minimizing communication latency and maximizing throughput.
This applies to north-south, east-west, and NVLink networks, but not to the management network.
Refer to the Appendix for a DGXC-recommended reference implementation.

The following table lists the backend switch fabric API requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **NET01** | Backend Switch Fabric API | For each compute node, the API must provide visibility into the backend network switches connecting the node to the core.<ul><li>**Identification:** Each switch must be identified by a unique, stable identifier. A "switch" may represent a physical switch or a logical connectivity domain.</li><li>**Structure:** API may be gRPC or REST. Response structure may include multiple nodes (pagination expected).</li><li>**Topology:** Switch info can be returned as an ordered array of IDs (e.g., leaf, spine, core) or separate fields for each tier.</li></ul> |
| **NET02** | NVLink Domain API | **Requirement:** For compute nodes supporting NVLink (e.g., GB200, GB300, Vera Rubin), the API shall return the unique identifier of the NVLink domain associated with each node.<br><br>**Implementation:** Can be a separate API method or part of the Backend Switch Fabric API. |

### Transport and Networking Requirements

#### Non-Conflicting IP Space Allocation for the DGXC Cluster

The purpose of this requirement is to ensure that DGXC GPU clusters deployed in the NCP environment can access the NVIDIA DGXC and CorpIT network directly through routing exchange.
DGXC cluster IP addresses must not conflict with existing NVIDIA private IP space.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **NET03** | Non-Conflicting IP Space Allocation for the DGXC Cluster | **Bring Your Own IP (BYOIP)**: NCP shall support the ability for NVIDIA to bring and allocate its own IP private address space for DGXC GPU clusters.<br><br>**Stable IP**: NCP shall provide a possibility to create static IP allocations that persist across instance restarts and re-creations. That includes floating IP allocations.<br><br>**DoD space:** NCP shall support allocation and use of the 7.0.0.0/8 IPv4 address space for DGXC GPU cluster deployments. This IP space shall be considered equivalent to RFC1918 addresses.<br><br>**Routing Support:** NCP must support advertising and routing of BYOIP prefixes within the NCP environment and across interconnects (Private Cloud Interconnect, IPSec, etc.) |

#### Connection to NVIDIA CorpIT Network

The purpose of this requirement is to provide a connection from DGXC GPU clusters within the NCP environment to NVIDIA CorpIT for internal command, control, and administrative access.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **NET04** | Connection to NVIDIA CorpIT Network | **Bandwidth**: Low bandwidth, up to 10 Gbps. <br>**Transport**: Private Cloud Interconnect with VIF and BGP, which is preferred for better performance and security. DGXC establishes connectivity to the NCP through a mutually agreed Point of Presence (POP) using Private Cloud Interconnect, functionally equivalent to AWS Direct Connect, GCP Dedicated Interconnect, Azure ExpressRoute, and OCI FastConnect. Connectivity is provisioned with a Virtual Interface (VIF), and routing is established through BGP. The interconnect is used to exchange private IP space, including RFC1918 and `7.0.0.0/8`, between DGXC and the NCP. |

The following diagram shows Private Cloud Interconnect with VIF and BGP for CorpIT access.

![Corporate network connectivity diagram](docs/ncp/nvidia-requirements-for-ai-clouds/assets/images/nrac-corpnet-connectivity.png)

#### Connection to DGXC Storage

The purpose of this requirement is to enable high-bandwidth, end-to-end MACsec-encrypted, fail-closed access between the DGXC GPU clusters in the NCP environment and NVIDIA DGXC on-premises object storage for large-scale data movement.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **NET05** | Connection to DGXC Storage | **Transport**: Private Cloud Interconnect with VIF and BGP, which is preferred for better performance and security. DGXC establishes connectivity to the NCP through a mutually agreed Point of Presence (POP) using Private Cloud Interconnect, functionally equivalent to AWS Direct Connect, GCP Dedicated Interconnect, Azure ExpressRoute, and OCI FastConnect. Connectivity is provisioned with a Virtual Interface (VIF), and routing is established through BGP. The interconnect is used to exchange private IP space, including RFC1918 and `7.0.0.0/8`, between DGXC and the NCP. |

The following diagram shows the storage connectivity path between the DGXC GPU clusters and NVIDIA DGXC object storage.

![Storage connectivity diagram](docs/ncp/nvidia-requirements-for-ai-clouds/assets/images/nrac-storage-connectivity.png)

#### Cluster Local Internet Access

The purpose of this requirement is to provide DGXC GPU clusters within the NCP environment with general internet access, including access to NVIDIA DGXC services hosted on third-party public cloud services.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **NET06** | Cluster Local Internet Access | **Cluster internet access**: Egress NAT IPs should be a static pool dedicated only to the NVIDIA cluster, tenancy, and VPC. These persistent IP addresses must be used exclusively for DGXC traffic and shall not be shared with or carry traffic from other NCP tenants.<br> **Availability**: Must support redundant upstream paths to ensure connectivity under failure. |

The following diagram shows public internet access for DGXC-hosted services.

![Internet access diagram](docs/ncp/nvidia-requirements-for-ai-clouds/assets/images/nrac-internet-access.png)

### Capacity and Fleet Management

This section defines the essential metrics required for standardized monitoring and reporting of fleet health in partner engagements, supporting operations and contractual SLAs.

The following table lists the capacity and fleet management requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **CAP01** | Governance Metrics | **Required Governance Metrics**<br>The core metrics needed to track fleet health are:<ul><li>**Delivered**: Nodes/GPUs provisioned and available to NVIDIA, allocated to a specific account/project/tenant.</li><li>**Healthy**: Nodes/GPUs functioning and meeting SLA requirements, allocated to a specific account/project/tenant.</li><li>**Reserved**: Resources allocated to a specific account/project/tenant.</li><li>**Total Active/In-Use**: Nodes/GPUs currently in use within a specific account/project/tenant.</li></ul> |
| **CAP02** | Resource Governance API Metrics | The Resource Governance API must return the following information for each node:<ul><li>**Node ID** (Unique identifier for a GPU node)</li><li>**Health State** (Healthy/Unhealthy classification)</li><li>**Instance ID** (Identifier for virtual workload)</li><li>**Creation Timestamp** (Time workload/node was created)</li><li>**Hardware Type** (Descriptor for the hardware model)</li><li>**GPU Count** (Number of GPUs per node)</li><li>**Top-level Account/ID** (Identifier for the top-level organization/account)</li><li>**Sub-Level Project/ID** (Identifier for the nested project/sub-account)</li><li>**In Use** (True/False status indicating if the GPU Node is turned on and in use)</li><li>**Region** (Region of the data center where nodes are deployed)</li></ul> |
| **CAP03** | Resource Discovery APIs | It is not acceptable to have capacity be “handed” to DGXC through a phone, slack or email message.  For example, when  cluster first comes online, nodes/racks are likely being handed off weekly (or more frequently).  Instead, provide the following mechanism (and we can poll):<br><br>**Programmatic Capacity Discovery**: All newly delivered capacity must be discoverable via a centralized API. This "Resource Index" must provide a stable resource identifier and some information on why it’s being provided (e.g. capacity fulfillment on GB300 project, break-fix / RMA return to cluster, etc) |
| **CAP04** | Logical Compartmentalization and Resource Isolation | To ensure performance consistency and security, the NCP must support strict logical and physical isolation of NVIDIA’s reserved capacity.<br><br>**Capacity Reservations**: A mechanism to logically group and "pin" a set of resources (compute, network, storage) to accounts (or equivalent constructs) in an NVIDIA tenancy<br><br>**Atomic Allocation**: Support for reserving a "topology block" as a single unit, ensuring all resources in that block share identical performance characteristics and security boundaries. |
| **CAP05** | Unified Health and Lifecycle APIs | NVIDIA requires a "single source of truth" for the health of both physical hosts and logical compute primitives.<br><br>**Per-Host Health:** Real-time API access to the health bits of physical hardware (GPU state, thermal status, memory health).<br><br>**Primitive-Level Status:** Health aggregation at the cluster, nodegroup, or reservation level to identify broad infrastructure failures (e.g., a spine switch failure affecting a whole block). |

## Operational Requirements

This section defines how the NCP operates the service.
Where an earlier section defines a capability, such as incident management or telemetry delivery, this section defines the operational practice around that capability and references it rather than repeating it.
Numeric service-level targets are stated once, in the Service Levels section, and operational models and processes are defined here.
These operational expectations describe generally available operational maturity, and they are not bespoke to NVIDIA.
Requirement levels are carried in the description text through the words must and shall, consistent with the rest of this document.

### Support and Ticketing

The NCP must provide staffed, responsive support with a system of record for every issue NVIDIA raises, classified under the common severity model and driven to the targets in the Service Levels section.

The following table lists the support and ticketing requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OST01** | 24x7 Coverage | The NCP must provide continuously available, 24x7 support, including emergency access recovery. Support requests must be tracked through a ticketing system of record. A real-time communication channel, such as Slack or phone, must also be available, along with a support SLA. |
| **OST02** | Ticketing System | The customer-facing trouble-ticketing system shall be the system of record against which all customer-raised issues are logged, each with a unique identifier and a current state. The system must be searchable by attributes such as error codes and keywords, so that an already reported issue can be found. |
| **OST03** | Ticket Severity and Targets | Every ticket shall carry the severity classification defined in OIE02 and follow the corresponding response, resolution, and escalation targets in the Service Levels section. |
| **OST04** | Customer-Confirmed Closure | Customer-opened tickets shall not be closed until the customer agrees the issue is resolved. |

### Incident, Outage, and Escalation Management

The NCP must detect, classify, communicate, and resolve service-impacting events, and drive each to durable resolution through root-cause analysis.
The NCP must ultimately take permanent steps to ensure there is no recurrence.
Fault signals are defined in the Telemetry Requirements section and remediation primitives in the Break-Fix Requirements section, and this section defines how they are operationalized.
The severity model here is the single model used across this section, including ticketing.

The following table lists the incident, outage, and escalation management requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OIE01** | Incident Handling | The NCP shall maintain a defined incident-management practice to detect, classify, respond to, escalate, and resolve service-impacting events within the targets in the Service Levels section. |
| **OIE02** | Severity Model | The NCP shall maintain a documented severity model for incidents and tickets, with a defined escalation path for each severity. The Service Levels section shall map the NCP severity levels to the applicable NVIDIA service targets. |
| **OIE03** | Incident Command | For each major incident, the NCP shall assign an incident commander, or an equivalent role, responsible for coordinating technical response, decisions, handoffs, and customer communications under a maintained major-incident playbook. |
| **OIE04** | Customer Notification | Customers shall be notified programmatically of qualifying, service-impacting incidents within the timeframe in the Service Levels section. |
| **OIE05** | Automated Fault Remediation | The NCP shall automatically detect faults. After detection, non-disruptive faults should be automatically remediated. Tenant-disruptive faults should be remediated in a directed manner that follows the defined maintenance processes. When a fault cannot be remediated programmatically, it should fall back to an NCP-managed manual flow. |
| **OIE06** | Root Cause and Corrective Action (RCCA) | For qualifying incidents, the NCP shall deliver a formal written RCCA report within a defined, published timeframe of at least monthly, stating specific corrective actions. |
| **OIE07** | Corrective-Action Tracking | Corrective actions shall be tracked to closure and target dates. Overdue actions shall be escalated and reviewed on a defined cadence. |

### Security Incident and Breach Notification

This section governs security incidents and confirmed or suspected compromise of NVIDIA tenancy, data, or infrastructure.
It complements the Security and Identity Management section, which defines the underlying controls.

The following table lists the security incident and breach notification requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OSB01** | Security Incident Handling | The NCP shall detect, contain, investigate, and remediate security incidents affecting customer tenancy, data, or infrastructure, under a defined security incident response capability. |
| **OSB02** | Breach Notification | The NCP shall notify tenants of any confirmed or reasonably suspected security breach, unauthorized access, or compromise of NVIDIA data or tenancy without undue delay, and with all available information. Notification shall occur no later than 24 hours for a high-severity or confirmed compromise, and no later than 72 hours for all other incidents. |
| **OSB03** | Evidence Preservation and Joint Investigation | The NCP shall preserve relevant logs and forensic evidence for the period required by applicable law or regulation, and for no less than 12 months. The NCP shall support joint investigation, including NVIDIA participation where agreed. |
| **OSB04** | Coordinated Disclosure | Public disclosure and external communications relating to an incident affecting the tenant shall be coordinated with the tenant. |
| **OSB05** | Security Post-Incident Report | Security incidents shall follow the RCCA process defined in OIE06, with any additional information about the scope of the compromise. |

### Service Health and Status Communications

The NCP must give NVIDIA an authoritative, always-available view of service health, active incidents, and planned maintenance.

The following table lists the service health and status communication requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OSH01** | Status Dashboard | An external health and status dashboard shall be published and available. The dashboard shall show health by region and by service, using clear multi-state indicators, for example, green, yellow, and red. |
| **OSH02** | Incident and Maintenance Posting | Active incidents and scheduled maintenance shall be posted to the dashboard in a timely manner. |
| **OSH03** | Historical Status | Historical uptime and incident history shall be retained and viewable. |
| **OSH04** | Programmatic Access | Status and health should be consumable programmatically for ingestion by the tenant. |

### Change and Release Management

Changes to production infrastructure must be controlled so that each change is approved, reversible, and communicated.
High-risk changes progress through a staged rollout.

The following table lists the change and release management requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OCR01** | Change Governance | The NCP shall employ a change control practice that ensures every change to production infrastructure is documented and approved before implementation and carries a rollback or back-out plan. Disruptive changes shall be communicated in advance, as described in the Fleet Health and Maintenance section. |
| **OCR02** | Emergency Change | The NCP may employ an emergency change path, distinct from standard change, with retrospective review aligned to the RCCA process. |
| **OCR03** | Progressive Delivery | Changes to NCP-managed platform or software services shall roll out with staged validation, defined stop criteria, blast-radius controls, and a tested rollback and recovery plan appropriate to the change. |
| **OCR04** | Change Observation | Following a change, a defined observation period shall elapse before the change is declared successful and closed. If an issue surfaces during this window, the changes shall be remediated and treated as a failed change rather than a new incident. |
| **OCR05** | Auditable Changes | Changes to production infrastructure supporting delivered services shall be versioned, reviewed, and auditable. Unauthorized or unreviewed deviations from the approved configuration shall be detected and remediated. |

### Fleet Health and Maintenance

Planned and emergency maintenance must be declared, scheduled with NVIDIA, and tracked.
A hand-back window is how long the tenant has to return the instance, and the maintenance window is how long the NCP has to perform the service.

The following table lists the fleet health and maintenance requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OFM01** | Declared Maintenance | Formal maintenance for instances in a maintenance-required or degraded mode shall be declared to the tenant through a defined process, with a hand-back window and a maintenance window provided. A hand-back window is expected to allow deferral for at least 2 weeks. |
| **OFM02** | Tenant Scheduling and Deferral | Tenants shall be able to schedule declared maintenance services through an API or console tools within the hand-back window. The NCP should not force eviction unless the window has expired. The tenant accepts the risk of continued use during the hand-back window. Maintenance should be non-disruptive where possible. Tenants should have the option to request a deferral, meaning an extension of the hand-back window. The NCP is not obligated to accept a deferral but may evaluate the request based on the criticality of the maintenance. |
| **OFM03** | Maintenance Tracking | Maintenance window open and close times shall be tracked, recorded, and exposed in scheduling APIs. An incident shall be declared when a window exceeds its planned close time. |
| **OFM04** | Emergency Maintenance | A defined path shall exist for declaring and executing emergency maintenance windows. This path allows the hand-back window to be very small, on the order of minutes. |
| **OFM05** | Validated Return | Following a declared or emergency maintenance event, such as a firmware upgrade or hardware replacement, the NCP shall complete all maintenance and validation across the affected compute, GPU, fabric, and storage resources before returning them to service. The validation is included in the maintenance window, whether the resources return to the tenant or to the allocable pool. |
| **OFM06** | Progressive Rollout | Firmware and OS changes shall be rolled out progressively, through an incremental deployment method, rather than fleet-wide at once. A failed partial deployment halts the rollout and is remediated. The tenant should be given the option to test and validate new firmware in a development cluster before it is rolled out to the tenant fleet. |
| **OFM07** | Sparing | The NCP shall define a spare parts and hot sparing strategy to support the availability SLA. |

### Operational Access and Activity Logging

The NCP operational access to production infrastructure must follow the security best practice of least privilege, be strongly authenticated, and be fully logged.
This section governs operator and provider access.
Tenant-facing identity and access management is defined in the Security and Identity Management section, and these requirements reference rather than restate it.

The following table lists the operational access and activity logging requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OAL01** | Operational Access Policy | Operator access to production infrastructure shall require multifactor authentication (MFA) and shall be restricted, through role-based and policy-based authorization, to the minimum permissions necessary to perform the operator’s assigned duties, as described in SEC07. |
| **OAL02** | Recertification and Offboarding | Access shall be recertified periodically and revoked on offboarding. |
| **OAL03** | Operational Activity Logging | All manual and administrative actions by NCP personnel affecting production infrastructure shall be included in the audit logs required by SEC08 and attributed to the authenticated operator and any assumed role. |
| **OAL04** | Log Monitoring | Operational activity logs shall be reviewed or monitored for anomalous activity. |
| **OAL05** | Two-Person Rule | High-risk operator actions on production infrastructure shall require two distinct authorized people, and no single operator can perform them alone. This shall be technically enforced and logged with both identities. A time-bounded break-glass exception may be used for emergency recovery, and its use must be logged and reported. |

### Vulnerability and Patch Management

The NCP must identify, prioritize, remediate, and, where relevant, disclose vulnerabilities across the infrastructure, and track them to closure.

The following table lists the vulnerability and patch management requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OVP01** | Vulnerability Management | The NCP shall identify, prioritize, remediate, and transparently disclose vulnerabilities and Common Vulnerabilities and Exposures (CVEs) affecting the infrastructure. |
| **OVP02** | Scanning | Infrastructure shall be regularly scanned for known vulnerabilities and CVEs. |
| **OVP03** | Severity Prioritization | CVEs shall be prioritized by severity, for example, through a CVSS-based rating, with Known Exploited Vulnerabilities (KEV) receiving the highest prioritization. |
| **OVP04** | Remediation Targets | Remediation targets shall be defined by severity in the applicable service agreement, and critical vulnerabilities shall be remediated within those targets. |
| **OVP05** | Tracking to Closure | Remediation of vulnerabilities that affect delivered services or customer tenancy shall be tracked to closure with an accountable owner and target date. |
| **OVP06** | Exceptions | Exceptions and risk acceptances shall be formally documented and approved by an NVIDIA risk signatory. |

### Monitoring, Alerting, and On-Call

The NCP must monitor infrastructure and services, alert on defined conditions, and staff on-call to respond to alert notifications 24 hours a day, 365 days a year, independent of regional holidays and observances and of local and geographical events.
Telemetry content and delivery are defined in the Telemetry Requirements section, and this section defines the operational response layer.

The following table lists the monitoring, alerting, and on-call requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OMA01** | Monitoring | The NCP shall continuously monitor infrastructure and service health independent of tenant workloads, without requiring agents inside tenant workloads and without depending on tenant scheduler or application data. |
| **OMA02** | Alerting | Alerting shall use defined thresholds and route notifications to designated responders, with escalation for unacknowledged alerts. |
| **OMA03** | On-Call Coverage | On-call coverage shall respond to alert notifications, staffed under a 24x7x365 coverage model. |
| **OMA04** | Alert Tracking | Alert notification acknowledgement and response times shall be tracked. Reporting is needed only where SLAs or operational requirements require it. |
| **OMA05** | Retention for Investigation | Telemetry required to support investigations shall be retained for a period aligned to the retention defined in the Telemetry Requirements section. |

### Capacity and Availability Management

The NCP must plan capacity, measure availability against the published targets, and remedy misses.
Fleet inventory and health metrics are defined in the Capacity and Fleet Management section, the numeric targets in the Service Levels section, and the measurement mechanics in the next section.

The following table lists the capacity and availability management requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OCA01** | Capacity Planning | The NCP shall maintain a capacity planning process for GPU, compute, and supporting resources, with utilization monitored and forecast against available capacity. |
| **OCA02** | Availability Targets | Availability targets for all services shall be defined and published in the Service Levels section. Where a target is not specified by NVIDIA, the NCP proposes the applicable target and measurement method. |
| **OCA03** | Availability Measurement | Availability shall be measured and reported to the tenant against the published targets. |
| **OCA04** | SLA Remedies | An SLA credit or remedy process shall exist for missed targets, financially backed where specified. |
| **OCA05** | Metering Accuracy | Consumption metering used as the basis for capacity or billing shall meet the accuracy target published in the Service Levels section. |

### Service-Level Measurement and Definitions

Availability and other service-level targets are enforceable only if measurement is defined.
This section states how targets are measured, what is excluded, and how misses are reconciled, complementing the numeric targets in the Service Levels section.

The following table lists the service-level measurement requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OSL01** | Availability Exclusions | Scheduled maintenance does not count against availability for the declared affected resources and during the declared maintenance window. Required notice and tenant scheduling commitments must be met, the impact must not exceed the declared scope, and all maintenance, validation, and return-to-service work must complete by the declared close time. |
| **OSL02** | Measurement Source | Service-level measurement methodologies must be defined so that both the NCP and the tenant can compute the same result. |
| **OSL03** | Credit Mechanics | The mechanism for how SLA credits are calculated, claimed, and applied for missed targets shall be formally defined. |

### Disaster Recovery and Business Continuity

This section defines disaster recovery (DR) and business continuity planning, backup verification, recovery objectives, and recovery testing for NCP-delivered services.

The following table lists the disaster recovery and business continuity requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **ODR01** | DR and BC Capability | The NCP shall maintain disaster recovery and business continuity plans covering the services it delivers. |
| **ODR02** | Backups and Verification | Backups shall be performed and their restore integrity verified. |
| **ODR03** | Recovery Objectives | Recovery objectives, meaning the RTO and RPO, shall be defined and auditable across services, such as managed Kubernetes and storage services. |
| **ODR04** | DR Testing | DR and failover shall be tested on a defined cadence, with recorded results. |
| **ODR05** | DR Roles | Roles and responsibilities for DR execution shall be defined. |

### Provisioning and Onboarding

The NCP must run a defined lifecycle for standing up, scaling, and tearing down NVIDIA tenancy, including verifiable data deletion on exit.
New-capacity delivery timelines are defined in the Service Levels section, and capacity discovery is defined in the Capacity and Fleet Management section.

The following table lists the provisioning and onboarding requirements.

| Req ID | Requirement Area | Description |
| :---- | :---- | :---- |
| **OPO01** | Onboarding and Provisioning | The NCP shall run a defined onboarding and provisioning process. |
| **OPO02** | Quota Management | Resource quotas and provisioning shall be managed and tracked. |
| **OPO03** | Verifiable Data Deletion | Customer data deletion on offboarding shall be defined and verifiable, including cryptographic erase between tenants, as described in SEC21. |

## Appendix

This section contains links to reference documents and implementation guidance that provide additional details for NCPs.
Refer to [requirement level key words](https://datatracker.ietf.org/doc/rfc2119/) for the well-known definition of certain terms used in the document.

### Implementation Guidance

The following reference documents provide additional information on implementing some of the requirements in this guide.

1. Network topology discovery: [NVIDIA Topograph](https://github.com/NVIDIA/topograph).
   Aligning with this YAML format may be useful.
   Topograph currently provides in-cluster topology.
2. Exemplar Cloud: [NVIDIA Exemplar Cloud](https://www.nvidia.com/en-us/data-center/ai-cloud-performance/) and the [DGXC benchmarking repository](https://github.com/NVIDIA/dgxc-benchmarking/tree/v26.02).
3. Kubernetes security guidance: [Kubernetes Security Response Committee](https://github.com/kubernetes/committee-security-response).

### Other Feature Considerations (Not Minimum Requirements)

The following capabilities are not minimum requirements, but NVIDIA considers them valuable additions.

1. **Disk cloning.** Disk-cloning capability for network-attached block devices.
   It should be possible to clone a disk even on a running instance.
2. **Managed control plane autoscaling.** Strong preference for the control plane to automatically add capacity when load increases.
3. **Threat detection.** Control planes, management planes, and hosts under the service provider control should deploy threat-detection and anomaly-detection solutions that can identify active threats, for example, a host-based intrusion detection system (HIDS) and a network-based intrusion detection system (NIDS).
4. **Break-glass administrative access.** The platform should support a limited break-glass access mechanism for designated NVIDIA administrative users when federated SSO is unavailable, misconfigured, compromised, or otherwise prevents authorized access to the tenant.
   Break-glass accounts should use local platform credentials independent of the external identity provider, be explicitly excluded from SSO enforcement, and be protected by strong authentication controls including MFA.
   Their use should be auditable, generate security-relevant logs and alerts, and support periodic review, rotation, disablement, and testing.

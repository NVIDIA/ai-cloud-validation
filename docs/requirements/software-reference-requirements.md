<!-- GENERATED FILE - DO NOT EDIT BY HAND. Source: software-reference-requirements.yaml. Run `make plan`. -->

# Software Reference Requirements

This document lists requirements derived from the *NCP Software Reference Guide* (NSRG).

## Foundational Services

### Image Registry

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| IMG01 | Image Registry | Verify that an OS image (iso file) with full NVIDIA support is readily available | NSRG: Compute Service (PXE/image deploy) | `IMG01-01` |  |
| IMG02 | Image Registry | Verify that an OS install configuration (e.g. iPXE config like Carbide) with full NVIDIA support is readily available | NSRG: Compute Service (PXE Boot Server) | `IMG02-01` |  |
| IMG03 | Image Registry | Verify that an VM image with full NVIDIA support is readily available | NSRG: Compute Service (VM Control Plane) | `IMG03-01` |  |

### Key Secret Mgmt

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| AUTH01 | Key Secret Mgmt | CRUD an SSH/Teleport compatible public key for system login | NSRG: Cloud Control Plane (IAM) | `AUTH01-01` |  |
| AUTH02 | Key Secret Mgmt | Spin up an instance which uses a specified key | NSRG: Compute Service (instance lifecycle) | `AUTH02-01` |  |
| AUTH03 | Key Secret Mgmt | Access other components via a specified key as possible (SOL, Network devices) | NSRG: Compute Service (serial console / SMN) | `AUTH03-01` |  |

### Identity and Asset Mgmt (IAM)

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| IAM01 | Identity and Asset Mgmt (IAM) | Create user and log in as that user | NSRG: Cloud Control Plane (IAM) | `IAM01-01` |  |
| IAM02 | Identity and Asset Mgmt (IAM) | Delete user | NSRG: Cloud Control Plane (IAM) | `IAM02-01` |  |
| IAM03 | Identity and Asset Mgmt (IAM) | Validate that a user created can access the API and authorized resources | NSRG: Cloud Control Plane (IAM authz) | `IAM03-01` |  |

### DDI

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| IPAM01 | DDI | Check that IP addresses are managed by the platform, and that DHCP is able to provide IP addresses | NSRG: SDN Layer (Network Manager IPAM/DHCP) | `IPAM01-01` |  |
| IPAM02 | DDI | Check that IP addresses are managed sensibly as VPCs are configured (Move to SDN section?) | NSRG: SDN Layer / Creating VPCs | `IPAM02-01` |  |

### Network Underlay & Mgmt

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| NETMGMT01 | Network Underlay & Mgmt | Verify that all switches in the are reachable and healthy (SSH, API, etc) | NSRG: SDN Layer / Networking (switch mgmt) | `NETMGMT01-01` |  |

### Resource Database

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| RESDB01 | Resource Database | Verify that all resources assigned to the cluster are accounted for | NSRG: Compute Service (Instance Database / inventory) | `RESDB01-01` |  |

## Infrastructure and Data Services

### Control Plane accessible

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| CP03 | Control Plane accessible | Make sure we can ping the control plane, or get a heart beat/status code | NSRG: Cloud Control Plane | `CP03-01` |  |
| CP04 | Control Plane accessible | Control Plane can be upgraded (measure impact) | NSRG: Cloud Control Plane | `CP04-01` |  |
| CP05 | Control Plane accessible | Access keys can be created, received, used to log in | NSRG: Cloud Control Plane (IAM) | `CP05-01` |  |
| CP06 | Control Plane accessible | Access keys can expire or be disabled | NSRG: Cloud Control Plane (IAM) | `CP06-01` |  |
| CP07 | Control Plane accessible | Create tenants | NSRG: Cloud Control Plane (per-tenant quotas) | `CP07-01` |  |
| CP08 | Control Plane accessible | Retrieve list of tenants | NSRG: Cloud Control Plane | `CP08-01` |  |
| CP09 | Control Plane accessible | Retrieve info about individual tenant | NSRG: Cloud Control Plane | `CP09-01` |  |
| CP10 | Control Plane accessible | Delete Tenant | NSRG: Cloud Control Plane | `CP10-01` |  |
| CP11 | Control Plane accessible | Add user to tenant | NSRG: Cloud Control Plane (IAM) | `CP11-01` |  |

### Hardware ingestion

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| HWING01 | Hardware ingestion | Makes sure that all hardware under test has been ingested, and matches the provided hardware | NSRG: Compute Service (Machine Lifecycle Manager - discovery/ingestion) | `HWING01-01` |  |
| HWING02 | Hardware ingestion | Make sure that a device can be removed from the system | NSRG: Compute Service (Machine Lifecycle Manager) | `HWING02-01` |  |

### Attestation

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| ATTEST01 | Attestation | Check that OS is approved | NSRG: Boot and Attestation / Remote Attestation | `ATTEST01-01` |  |
| ATTEST02 | Attestation | Check that an updated BIOS is installed on all hardware | NSRG: Boot and Attestation / Measured Boot | `ATTEST02-01` |  |

### Compute Services: BMaaS

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| BMAAS01 | Compute Services: BMaaS | Node attached storage | NSRG: Virtual Storage / SDS Layer | `BMAAS01-01` |  |
| BMAAS02 | Compute Services: BMaaS | Storage: Config/default/access to local NVME drives | NSRG: Virtual Storage (Ephemeral Storage) | `BMAAS02-01` |  |
| BMAAS03 | Compute Services: BMaaS | A running node can be accessed via SSH/Teleport for further configuration (Bare Metal) | NSRG: Compute Service | `BMAAS03-01` |  |
| BMAAS04 | Compute Services: BMaaS | DEPRECATED: A node can be reinstalled from its configured stock operating system | NSRG: Compute Service (Machine Lifecycle Manager) | `BMAAS04-01` |  |
| BMAAS05 | Compute Services: BMaaS | Check the health and status of the DPU on instantiation | NSRG: Break-Fix (Initial Node Validation) | `BMAAS05-01` |  |
| BMAAS06 | Compute Services: BMaaS | Nodes can communicate across ethernet, infiniband, and NVLink (Bare Metal) | NSRG: Networking (TAN/CIN/NVLink) / Performance Requirements | `BMAAS06-01` |  |
| BMAAS07 | Compute Services: BMaaS | Check for any per-host status log over time. | NSRG: Telemetry & Observability (Compute) | `BMAAS07-01` |  |
| BMAAS08 | Compute Services: BMaaS | Verify NVIDIA hardware | NSRG: Break-Fix (Initial Node Validation) | `BMAAS08-01` |  |
| BMAAS09 | Compute Services: BMaaS | GPU Stress workload (Bare Metal) | NSRG: Break-Fix / Node Level Health Checks | `BMAAS09-01` |  |
| BMAAS10 | Compute Services: BMaaS | Nim Inference jobs (Bare Metal) | NSRG: AI Platform-as-a-Service | `BMAAS10-01` |  |
| BMAAS11 | Compute Services: BMaaS | NCCL tests | NSRG: Performance Requirements / Networking (CIN) | `BMAAS11-01` |  |
| BMAAS12 | Compute Services: BMaaS | Training workload test | NSRG: AI Platform-as-a-Service / Performance Requirements | `BMAAS12-01` |  |

### Compute Service: VMaaS

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| VMAAS01 | Compute Service: VMaaS | A running node can be accessed via SSH/Teleport for further configuration (VM) | NSRG: Compute Service (VM Control Plane) | `VMAAS01-01` |  |
| VMAAS02 | Compute Service: VMaaS | DEPRECATED: A node can be reinstalled from its configured stock operating system | NSRG: Compute Service (VM Control Plane) | `VMAAS02-01` |  |
| VMAAS03 | Compute Service: VMaaS | Noisy Neighbor CPU/GPU tests | NSRG: Workload Isolation | `VMAAS03-01` |  |
| VMAAS04 | Compute Service: VMaaS | Tenant cannot allocate more VMs than their quota allows | NSRG: Cloud Control Plane (per-tenant quotas) | `VMAAS04-01` |  |
| VMAAS05 | Compute Service: VMaaS | Check that the host OS is a known acceptable image (like Ubuntu/DGXOS) | NSRG: Compute Service / Boot and Attestation | `VMAAS05-01` |  |
| VMAAS06 | Compute Service: VMaaS | Check that the GPU is visible and accessible from the VM | NSRG: Virtualizing a GPU | `VMAAS06-01` |  |
| VMAAS07 | Compute Service: VMaaS | Check that the correct Linux Kernel, libvirt, sbios, and NVIDIA drivers are installed | NSRG: Virtualizing a GPU / Performance Requirements | `VMAAS07-01` |  |
| VMAAS08 | Compute Service: VMaaS | Check that vEGM is configured correctly | NSRG: Virtualizing a GPU / Performance Requirements | `VMAAS08-01` |  |
| VMAAS09 | Compute Service: VMaaS | Check that vCPU pinning is set correctly, PCI bus is configured correctly | NSRG: Performance Requirements (VM Networking) / Virtualizing a GPU | `VMAAS09-01` |  |
| VMAAS10 | Compute Service: VMaaS | TODO: More things from the "NVIDIA Grace I/O Virtualization Guide" | NSRG: Performance Requirements / Virtualizing a GPU | `VMAAS10-01` |  |
| VMAAS11 | Compute Service: VMaaS | GPU Stress workload (VM) | NSRG: Break-Fix / Node Level Health Checks | `VMAAS11-01` |  |
| VMAAS12 | Compute Service: VMaaS | Nim Inference jobs (VM) | NSRG: AI Platform-as-a-Service | `VMAAS12-01` |  |
| VMAAS13 | Compute Service: VMaaS | Run NCCL tests | NSRG: Performance Requirements (VM Networking) | `VMAAS13-01` |  |
| VMAAS14 | Compute Service: VMaaS | Nodes can communicate across ethernet, infiniband, and NVLink (VM) | NSRG: Performance Requirements (VM Networking) / Networking | `VMAAS14-01` |  |

### SDN Controller

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| SDN11 | SDN Controller | Support Stable Private IP allocations, where if a VM crashes and restarts the same IP address remains pinned until the node is deleted | NSRG: SDN Layer / Creating VPCs | `SDN11-01` |  |
| SDN12 | SDN Controller | NVLink: Create a new partition; verify success response with partition key. | NSRG: Partitioning the NVLink Network (Scale-Up) | `SDN12-01` |  |
| SDN13 | SDN Controller | NVLink: List partitions; retrieve a specific partition by partition ID | NSRG: Partitioning the NVLink Network (Scale-Up) | `SDN13-01` |  |
| SDN14 | SDN Controller | NVLink: Delete an empty partition (no instances); retrieve by ID and verify not found. | NSRG: Partitioning the NVLink Network (Scale-Up) | `SDN14-01` |  |
| SDN15 | SDN Controller | NVLink: Create a partition, create instance in that partition, make sure the instance becomes ready, delete instance, verify GPUs removed from partition. | NSRG: Partitioning the NVLink Network (Scale-Up) | `SDN15-01` |  |
| SDN16 | SDN Controller | NVLink: Create partition, create 2 instances, verify both share the same nvlink_partition_id and both GPUs are reported for that partition | NSRG: Partitioning the NVLink Network (Scale-Up) | `SDN16-01` |  |
| SDN17 | SDN Controller | IMEX: Assert nvidia-imex and nvidia-imex-ctl packages are installed and nvidia-imex.service unit is registered with systemd. | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN17-01` |  |
| SDN18 | SDN Controller | IMEX: Start nvidia-imex via systemctl start nvidia-imex and assert the service reaches active/ready state. | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN18-01` |  |
| SDN19 | SDN Controller | IMEX: Stop nvidia-imex via systemctl stop nvidia-imex and assert clean shutdown (other nodes show disabled node status). | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN19-01` |  |
| SDN20 | SDN Controller | IMEX: Enable IMEX at boot with systemctl enable nvidia-imex, reboot, and confirm IMEX starts automatically. | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN20-01` |  |
| SDN21 | SDN Controller | IMEX: After all nodes start IMEX, query domain state via nvidia-imex-ctl -N and assert state is UP, fully connected matrix | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN21-01` |  |
| SDN22 | SDN Controller | IMEX: With IMEX_ENABLE_AUTH_ENCRYPTION=0, form a multi-node domain and assert connectivity without any auth/encryption. | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN22-01` |  |
| SDN23 | SDN Controller | IMEX: Enable IMEX_ENABLE_AUTH_ENCRYPTION=1 with SSL_TLS mode, provide valid server/client certs, and assert mutual authentication and encrypted communication. | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN23-01` |  |
| SDN24 | SDN Controller | IMEX: Enable GSSAPI/Kerberos mode (GSS_AUTH_ENCRYPT), configure KDC/keytabs, and assert mutual auth + encryption between nodes. | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN24-01` |  |
| SDN25 | SDN Controller | IMEX: IMEX/K8s: Submit a workload requesting a ComputeDomain; assert ComputeDomain pods are created on each allocated node. | NSRG: Kubernetes Architecture for ML/AI (DRA/IMEX) | `SDN25-01` |  |
| SDN26 | SDN Controller | IMEX: From workload pods in a ComputeDomain run nvbandwidth-test-job | NSRG: Kubernetes Architecture for ML/AI (IMEX) / Performance Requirements | `SDN26-01` |  |
| SDN27 | SDN Controller | IMEX: After workload completes, assert ComputeDomain and associated IMEX resources are cleaned up. | NSRG: Kubernetes Architecture for ML/AI (IMEX) | `SDN27-01` |  |
| SDN28 | SDN Controller | Redundant Gateways | NSRG: SDN Layer | `SDN28-01` |  |

### Metadata Service

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| META01 | Metadata Service | TODO | NSRG: Creating VPCs (Metadata Network) | `META01-01` |  |

### Cloud Control Plane

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| CTRL01 | Cloud Control Plane | Needs to have an API we can access from our tests to do actions | NSRG: Cloud Control Plane | `CTRL01-01` |  |

### Load Balancing

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| LB01 | Load Balancing | CRUD a load balancer | NSRG: SDN Layer | `LB01-01` |  |
| LB02 | Load Balancing | Verify that traffic is distributed across multiple nodes | NSRG: SDN Layer | `LB02-01` |  |
| LB03 | Load Balancing | Verify that nodes can be marked as down or up (for rolling deployments/etc) | NSRG: SDN Layer | `LB03-01` |  |
| LB04 | Load Balancing | Verify that no traffic is dropped during outages | NSRG: SDN Layer | `LB04-01` |  |

### Break-Fix

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| BFX04 | Break-Fix | Check that GPUd or Sentinel is running | NSRG: Node Level Health Checks (DCGM) | `BFX04-01` |  |
| BFX05 | Break-Fix | Verify that Tenants can be notified, by some communication system, of planned future node maintenance | NSRG: Break-Fix Architecture | `BFX05-01` |  |
| BFX06 | Break-Fix | Verify that Tenants can be notified, by some communication system, of immediate node failure | NSRG: Break-Fix Architecture | `BFX06-01` |  |

## Data Services

### SDS Controller

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| STOR01 | SDS Controller | Storage: Config/default/access to network provided storage | NSRG: Software Defined Storage (SDS) Layer | `STOR01-01` |  |

### Object Storage Service

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| DATASVC01 | Object Storage Service | Verify S3-compatible API access with authenticated endpoints | NSRG: SDS Layer (object storage) / Storage | `DATASVC01-01` |  |

### Block Storage Services

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| DATASVC02 | Block Storage Services | Verify volume snapshots | NSRG: SDS Layer | `DATASVC02-01` |  |
| DATASVC03 | Block Storage Services | Verify volume resizing | NSRG: SDS Layer | `DATASVC03-01` |  |
| DATASVC04 | Block Storage Services | Verify persistent block volumes survive instance restarts | NSRG: SDS Layer | `DATASVC04-01` |  |

### Cache Services

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| DATASVC05 | Cache Services | Create a managed cache instance, write a key-value pair, read it back, verify data integrity | NSRG: Other Workloads/Native Applications | `DATASVC05-01` |  |

### Vector Store

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| DATASVC06 | Vector Store | Create a vector store, insert an embedding, perform similarity search, verify result | NSRG: Other Workloads/Native Applications | `DATASVC06-01` |  |

### Relational Database

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| DATASVC07 | Relational Database | Create a managed SQL database, create a table, insert/query data, verify results | NSRG: Other Workloads/Native Applications | `DATASVC07-01` |  |

## Workload Orchestration

### Backup and Recovery

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| DATASVC08 | Backup and Recovery | Create a backup of a block volume, delete original, restore from backup, verify data is the same as the original | NSRG: SDS Layer | `DATASVC08-01` |  |

### Model Registry

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| DATASVC09 | Model Registry | Push a model artifact with a version tag, retrieve by version, verify it matches (prefer a smaller reference model) | NSRG: K8s-Native ML/AI Frameworks / CaaS (OCI registries) | `DATASVC09-01` |  |

### Slurm Control Plane

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| SLURM01 | Slurm Control Plane | Create a SLURM instance using the software platform | NSRG: AI Platform-as-a-Service (Slurm) | `SLURM01-01` |  |
| SLURM02 | Slurm Control Plane | SlurmInfoAvailable | NSRG: AI Platform-as-a-Service (Slurm) | `SLURM02-01` |  |
| SLURM03 | Slurm Control Plane | SlurmPartition cpu and gpu | NSRG: AI Platform-as-a-Service (Slurm) | `SLURM03-01` |  |
| SLURM04 | Slurm Control Plane | SlurmJobSubmission | NSRG: AI Platform-as-a-Service (Slurm) | `SLURM04-01` |  |
| SLURM05 | Slurm Control Plane | SlurmGpuAllocation 1gpu and 2gpu | NSRG: AI Platform-as-a-Service (Slurm) | `SLURM05-01` |  |
| SLURM06 | Slurm Control Plane | SlurmNodeJobExecution cpu and gpu | NSRG: AI Platform-as-a-Service (Slurm) | `SLURM06-01` |  |
| SLURM07 | Slurm Control Plane | SlurmGpuStressWorkload | NSRG: AI Platform-as-a-Service (Slurm) / Node Level Health Checks | `SLURM07-01` |  |
| SLURM08 | Slurm Control Plane | SlurmNcclMultiNodeWorkload | NSRG: AI Platform-as-a-Service (Slurm) / Performance Requirements | `SLURM08-01` |  |
| SLURM09 | Slurm Control Plane | SlurmSbatchWorkload gpu and cpu | NSRG: AI Platform-as-a-Service (Slurm) | `SLURM09-01` |  |
| SLURM10 | Slurm Control Plane | SlurmSbatchWorkload-inline | NSRG: AI Platform-as-a-Service (Slurm) | `SLURM10-01` |  |

### Managed Kubernetes Control Plane

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| K8S38 | Managed Kubernetes Control Plane | Run through all the steps of the k8s lifecycle management, including user-initated CP update | NSRG: Container-as-a-Service: Kubernetes | `K8S38-01` |  |
| K8S39 | Managed Kubernetes Control Plane | Be able to run k8s workloads | NSRG: Container-as-a-Service: Kubernetes | `K8S39-01` |  |
| K8S40 | Managed Kubernetes Control Plane | K8s Nim Inference | NSRG: K8s-Native ML/AI Frameworks (Dynamo/NIM) | `K8S40-01` |  |
| K8S32 | Managed Kubernetes Control Plane | K8s Nim Helm | NSRG: K8s-Native ML/AI Frameworks | `K8S32-01` |  |
| K8S33 | Managed Kubernetes Control Plane | K8s Nccl | NSRG: Kubernetes Architecture for ML/AI / Performance Requirements | `K8S33-01` |  |
| K8S34 | Managed Kubernetes Control Plane | validate adherence to upstream proxy requirements (service to pod load balancing, acces to internal services) | NSRG: Container-as-a-Service: Kubernetes | `K8S34-01` |  |
| K8S35 | Managed Kubernetes Control Plane | Verify that pod-to-pod L3 traffic flow logs are available and queryable | NSRG: Container-as-a-Service: Kubernetes / Telemetry | `K8S35-01` |  |
| K8S36 | Managed Kubernetes Control Plane | Verify Cluster Autoscaler integration (upstream) | NSRG: Container-as-a-Service: Kubernetes (autoscaling/Karpenter) | `K8S36-01` |  |
| K8S37 | Managed Kubernetes Control Plane | Verify the managed K8s service meets standard Kubernetes performance tests to max cluster size | NSRG: Container-as-a-Service: Kubernetes | `K8S37-01` |  |
| K8S41 | Managed Kubernetes Control Plane | The control plane should automatically add more capacity when load increases (control-plane autoscaling) | NSRG: Container-as-a-Service: Kubernetes (autoscaling) | `K8S41-01` |  |

### Power Policy Management

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| POWER01 | Power Policy Management | TODO | NSRG: Operator View / Compute Service (power states) | `POWER01-01` |  |

## AI Platform & User Access

### Host API Gateway

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| APIGW01 | Host API Gateway | TODO | NSRG: AI Platform-as-a-Service / Host API Gateway | `APIGW01-01` |  |

### Al Platform 1..N Control Planes

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| AICP01 | Al Platform 1..N Control Planes | TODO | NSRG: AI Platform-as-a-Service (Lepton/Run:ai/NVCF) | `AICP01-01` |  |

### Web Management Dashboard

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| WEBUI01 | Web Management Dashboard | TODO | NSRG: Cloud Control Plane (UI) | `WEBUI01-01` |  |

## Observability

### Observability Collectors

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| OBS01 | Observability Collectors | Validate that an OTel endpoint is provided and some data can be pulled from it | NSRG: Telemetry & Observability (Reference Architecture, OTel) | `OBS01-01` |  |
| OBS02 | Observability Collectors | Logging information about the control plane is available | NSRG: Telemetry & Observability (Logging Baseline) | `OBS02-01` |  |
| OBS03 | Observability Collectors | Logging information from instances | NSRG: Telemetry & Observability (Host-Conditional Logs) | `OBS03-01` |  |
| OBS04 | Observability Collectors | Alerts can be issued by the system in case of problems | NSRG: Telemetry & Observability (Hot Path / operational) | `OBS04-01` |  |
| OBS05 | Observability Collectors | Verify telemetry latency is no longer than 120 seconds | NSRG: Telemetry & Observability (Delivery Method) | `OBS05-01` |  |
| OBS06 | Observability Collectors | Verify telemetry is available for North-South (front-end) network | NSRG: Telemetry & Observability (Network) | `OBS06-01` |  |
| OBS07 | Observability Collectors | Verify telemetry is available for East-West (back-end / GPU interconnect) network | NSRG: Telemetry & Observability (Network) | `OBS07-01` |  |
| OBS08 | Observability Collectors | Verify telemetry is available for Management network | NSRG: Telemetry & Observability (Network) | `OBS08-01` |  |
| OBS09 | Observability Collectors | Verify telemetry is available for NVSwitch Fabric (GB200+) | NSRG: Telemetry & Observability (Network) | `OBS09-01` |  |
| OBS10 | Observability Collectors | Verify telemetry is available for Host network (NIC-level) | NSRG: Telemetry & Observability (Network) | `OBS10-01` |  |
| OBS11 | Observability Collectors | Verify Fabric Manager logs are available (where applicable) | NSRG: Telemetry & Observability (Logs) | `OBS11-01` |  |
| OBS12 | Observability Collectors | Verify Subnet Manager logs are available (where applicable) | NSRG: Telemetry & Observability (Logs) | `OBS12-01` |  |
| OBS13 | Observability Collectors | Verify UFM Event logs are available | NSRG: Telemetry & Observability (Logs) | `OBS13-01` |  |
| OBS14 | Observability Collectors | Verify general switch logs are available | NSRG: Telemetry & Observability (Logs) | `OBS14-01` |  |
| OBS15 | Observability Collectors | Verify switch syslogs are available | NSRG: Telemetry & Observability (Logs) | `OBS15-01` |  |
| OBS16 | Observability Collectors | Verify switch kernel logs are available | NSRG: Telemetry & Observability (Logs) | `OBS16-01` |  |
| OBS17 | Observability Collectors | Verify BMC SEL logs are available | NSRG: Telemetry & Observability (Universal / Out-of-Band Logs) | `OBS17-01` |  |
| OBS18 | Observability Collectors | Verify host syslogs are available | NSRG: Telemetry & Observability (Host-Conditional Logs) | `OBS18-01` |  |
| OBS19 | Observability Collectors | Verify VPC Flow logs (all ingress/egress traffic) are available | NSRG: Telemetry & Observability (Network / Logs) | `OBS19-01` |  |

### Telemetry / Data Lakes

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| TELEM01 | Telemetry / Data Lakes | TODO | NSRG: Telemetry & Observability (Cold Path / Data Lake) | `TELEM01-01` |  |
| TELEM02 | Telemetry / Data Lakes | Read only access to a BMaaS system's serial console | NSRG: Compute Service (serial console) | `TELEM02-01` |  |
| TELEM03 | Telemetry / Data Lakes | Read only access to a VM serial console | NSRG: Compute Service (serial console) | `TELEM03-01` |  |
| TELEM04 | Telemetry / Data Lakes | Verify BMC/Redfish telemetry is accessible via API for GPU metrics not available from the host OS | NSRG: Telemetry & Observability (Hardware Management, Redfish) | `TELEM04-01` |  |
| TELEM05 | Telemetry / Data Lakes | Verify storage resource capacity metrics are available (used/free/total) | NSRG: Telemetry & Observability (Storage) | `TELEM05-01` |  |
| TELEM06 | Telemetry / Data Lakes | Verify storage performance metrics are available (bandwidth, IOPS, latency) | NSRG: Telemetry & Observability (Storage) | `TELEM06-01` |  |
| TELEM07 | Telemetry / Data Lakes | Verify NVLink metrics are available from the GPU perspective (per-link counters, bandwidth utilization) | NSRG: Telemetry & Observability (GPU / Network) | `TELEM07-01` |  |
| TELEM08 | Telemetry / Data Lakes | Verify NVLink metrics are available from the switch perspective (port-level counters, error rates) | NSRG: Telemetry & Observability (Network / Fabric) | `TELEM08-01` |  |

## Benchmarking

### Exemplar

| Req ID | Requirement Area | Description | Reference Mapping | Covers test | Status |
| :----- | :--------------- | :---------- | :---------------- | :---------- | :----- |
| BENCH01 | Exemplar | NVMesh checks? | NSRG: Performance Requirements / Exemplar | `BENCH01-01` |  |
| BENCH02 | Exemplar | IPv4 and IPv6 checks? | NSRG: Networking | `BENCH02-01` |  |

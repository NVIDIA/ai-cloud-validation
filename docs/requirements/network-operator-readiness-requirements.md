<!-- GENERATED FILE - DO NOT EDIT BY HAND. Source: network-operator-readiness-requirements.yaml. Run `make plan`. -->

# Enterprise RA Network Operator Self-Validation Integration PRD

> Structured source of record: `network-operator-readiness-requirements.yaml` (version prd-snapshot-2026-08-04).
> Owner: NVIDIA Network Operator team.
> Edit the YAML, not this file.

## Ownership

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-000 | Integration ownership | The Network Operator team owns and maintains the integration solution, including compatibility updates for the underlying tests. | active |

## Framework Integration

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-001 | Standard validation workflow | Integrate Network Operator self-validation tests into AI Cloud Validation so Enterprise and AI Cloud Ready users can run them through the standard workflow. | active |
| ENT-REQ-002 | Launch Kit reuse | Reuse applicable Kubernetes Launch Kit validation components, including topology discovery, manifest readiness, RDMA connectivity, and RDMA bandwidth. | active |
| ENT-REQ-003 | Selection and program profiles | Support individual and grouped Network Operator validation, with Enterprise and AI Cloud Ready profiles able to mark checks required or optional. | active |
| ENT-REQ-004 | Runtime parameters | Expose applicable runtime parameters such as namespace, node selector, network and driver modes, rail and network names, resource and IP pool names, GPU count, and timeout. | active |

## Network Validation

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-005 | Ethernet and RoCE | Validate SR-IOV Network RDMA and RDMA Shared scenarios, including secondary network attachment, RDMA device availability, pod-to-pod RDMA or RoCE connectivity, and basic bandwidth. | active |
| ENT-REQ-006 | InfiniBand | Validate InfiniBand SR-IOV and RDMA Shared with IPoIB scenarios, including IB device availability, pod network attachment, and pod-to-pod InfiniBand connectivity. | active |
| ENT-REQ-007 | Host-device networking | Validate host-device networking for Kubernetes workers running in virtual machines, covering both Ethernet or RoCE and InfiniBand. | active |
| ENT-REQ-008 | GPUDirect RDMA | Validate GPUDirect RDMA peer-to-peer connectivity between GPU-enabled pods across supported worker nodes. | active |
| ENT-REQ-009 | Deployment health | Validate Network Operator deployment health and required resources for the selected mode, including policies, secondary networks, IP pools, Multus and CNI components, and drivers. | active |

## Lifecycle Safety

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-010 | State restoration | For tests that modify Network Operator or cluster state, capture the pre-test configuration and restore the original Network Operator state after success or failure. | active |

## Catalog and Documentation

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-011 | Catalog metadata | Add catalog entries for all tests with owner, labels, dependencies, descriptions, and required YAML updates while following repository contribution standards. | active |
| ENT-REQ-012 | Prerequisites | Document the required cluster prerequisites for each validation area. | active |

## Reporting

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-013 | Results and evidence | Integrate pass or fail status, logs, Launch Kit reports, generated manifests, Kubernetes state, connectivity results, and bandwidth results into AI Cloud Validation reporting, catalog, and AI Cloud Labs artifacts. | active |

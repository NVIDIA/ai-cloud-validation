<!-- GENERATED FILE - DO NOT EDIT BY HAND. Source: gpu-operator-readiness-requirements.yaml. Run `make plan`. -->

# GPU Operator Self-Certify Integration Solution for AI Cloud Validation Suite PRD

> Structured source of record: `gpu-operator-readiness-requirements.yaml` (version prd-snapshot-2026-11-06).
> Owner: NVIDIA GPU Operator team.
> Edit the YAML, not this file.

## Ownership

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-100 | Integration ownership | The GPU Operator team owns and maintains the integration solution, including updates and changes to the underlying tests. | active |

## Framework Integration

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-101 | Test exposure | Expose the GPU Operator self-certification tests GPUOP-00 to GPUOP-08 to the framework. | active |
| ENT-REQ-102 | Selection and program profiles | Allow Enterprise to run all GPU Operator tests as one suite or as individual GPUOP tests, and allow NCP to exclude tests not required for the NeoCloud MVP, such as MIG and vGPU paths. | active |
| ENT-REQ-103 | Runtime parameters | Expose parameters for namespace, timeout, GPU count, GPU driver version and update policy, MIG strategy, RDMA resource names, DMA-BUF mode, GDRCopy enablement, and other GPU Operator test inputs. | active |

## Evidence

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-104 | Logs and artifacts | Preserve the logs and artifacts needed for partner validation evidence. | active |

## Lifecycle Safety

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-105 | State restoration | For tests that modify GPU Operator or cluster state, including the GPU driver version and update policy, MIG strategy, and RDMA, capture the pre-test configuration and restore the original GPU Operator state after the test completes or fails. | active |

## Catalog and Documentation

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-106 | Catalog metadata | Add AI Cloud Validation catalog entries for all GPUOP tests with owner, labels, dependencies, and descriptions, including the required YAML updates, while following repository contribution standards. | active |
| ENT-REQ-107 | Prerequisites | Document the required cluster prerequisites for each GPUOP test. | active |

## Reporting

| Req ID | Requirement Area | Description | Status |
| :----- | :--------------- | :---------- | :----- |
| ENT-REQ-108 | ISV-NCP report | Integrate results into the ISV-NCP report and catalog. | active |

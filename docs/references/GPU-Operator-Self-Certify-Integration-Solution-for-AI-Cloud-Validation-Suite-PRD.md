# GPU Operator Self-Certify Integration Solution for AI Cloud Validation Suite

## Overview

The Enterprise Reference Architecture (ERA) Infrsastructure ISV Readiness
Program extends the NVIDIA AI Cloud Ready Program’s AI Cloud Validation suite to
validate enterprise-grade infrastructure software on scaled CRA deployments.
While the AI Cloud Ready program validates software in cloud/neo-cloud
environments, enterprise customers are increasingly deploying AI workloads on
on-premise infrastructure built using OEM backed CRA designs which introduce
specific considerations such as alignment with specific cluster/node
configurations (2-8-5-200,2-8-9-800,etc.), networking topology and operational
requirements not fully covered by the existing AI Cloud Ready validation tests.

The GPU Operator Self-Certification kit is a utility used to validate the proper
operation of the GPU Operator on a Kubernetes cluster. It was originally created
for ISV partners who have developed their own driver container and need to
verify that it interoperates correctly with the rest of the GPU Operator stack.

The requested work is to create a lightweight solution that allows one or more
GPU Operator Self-Certify tests to be invoked from the AI Cloud Validation suite
using test entries, configurable parameters, and catalog metadata. This does not
require rewriting the GPU Operator tests. It packages the existing tests so they
can be selected, executed, reported, and reused consistently across Enterprise
and NCP AI Cloud Ready programs. Today the tests in the AI Cloud Validation
suite utilise PyTest classes. There are multiple options for integrating or
calling from the existing Go codebase.

## Users and Impact

| User | Need | Impact |
| :---- | :---- | :---- |
| Enterprise Reference Architecture team | Validate GPU Operator readiness and behavior on **Enterprise scale** AI Kubernetes clusters and ISV platforms | Reduces manual validation and creates repeatable partner evidence.Gives us confidence in our partner's software platform. |
| AI Cloud Validation team | Validate GPU Operator readiness and behavior on **NCP scale** AI Kubernetes clusters and ISV platforms | Provides clear readiness signal inside existing AI Cloud Validation suite. Gives us confidence in our partner's software platform. |
| ISV partners | Run a single validation framework with clear pass/fail output for GPU Operator along with other K8s tests | Lowers onboarding friction and improves confidence in NVIDIA software stack. |
| GPU Operator team | Increase cluster-scale adoption and reduce ad hoc support asks for specific ISVs | More consistent field validation data and earlier issue discovery for top Infrastructure ISVs |

## Solution Requirements

Create a testing solution that integrates into the AI Cloud Validation suite
(framework). It would invoke the GPU Operator Self-Certify tests seamlessly. The
solution should expose each GPUOP test as an individually selectable test and
optionally expose a grouped "GPU Operator Self-Certify" suite.

That should support:

| Capability | Description |
| :---- | :---- |
| Individual test execution | Example: run only GPUOP-00 or GPUIP-06 |
| Grouped execution | Example: run all MVP GPU Operator tests or a subset  in one command. |
| Parameterized execution | Allow configuration for namespace, GPU count, MIG strategy, RDMA enablement, drivers DMA-BUF, GDRCopy, timeout, and image settings as and when needed. These will be exposed as variables within the wrapper for ISVs to adjust |
| AI Cloud Labs reporting | Emit pass/fail and logs into AI Cloud Labs result artifacts. |
| Test catalog entries | Add each GPUOP test to the AI Cloud Validation suite and AI Cloud Labs catalog with capability, label, and ownership metadata. |

## GPU Operator Test Matrix

| Test | Description | Enterprise Need | AI Cloud Ready Need |
| :---- | :---- | :---- | :---- |
| GPUOP-00 \- Default Settings | Validate default GPU Operator deployment and basic GPU pod functionality. | Required | Required |
| GPUOP-01 \- GPU Sharing via Time-Slicing | Validate GPU time-slicing configuration and pod access. | Required | Optional |
| GPUOP-02 \- DCGM Standalone | Validate DCGM metrics/health monitoring path. | Required | Required |
| GPUOP-03 \- Driver Update | Validate GPU driver lifecycle/update behavior. | Required | Required |
| GPUOP-04 \- MIG Mode, Single Strategy | Validate MIG single strategy behavior. | Required | Optional |
| GPUOP-05 \- MIG Mode, Mixed Strategy | Validate MIG mixed strategy behavior. | Required | Optional |
| GPUOP-06 \- GPUDirect RDMA | Validate GPUDirect RDMA with Network Operator/RDMA stack. | Required | Required G |
| GPUOP-07 \- GPUDirect RDMA with DMA-BUF | Validate GPUDirect RDMA using DMA-BUF path. | Required | Required |
| GPUOP-08 \- GDRCopy | Validate GDRCopy installation and functionality. | Required | Required |

## Requirements

| Requirement ID | Requirement |
| :---- | :---- |
| ENT-REQ-100 | For all requirements specified below, **the GPU Operator team** will own and maintain the integration solution including updates and changes to the underlying tests |
| ENT-REQ-101 | Expose the relevant test (GPUOP-00 - GPUOP-08) to the framework |
| ENT-REQ-102 | Allow Enterprise to run all GPU Operator tests as one suite or individual GPUOP tests. This could be done through parameters or flags. Allow NCP to exclude tests not required for NeoCloud MVP, such as MIG and vGPU-related paths |
| ENT-REQ-103 | Expose parameters for namespace, timeout, GPU count, GPU driver version/update policy, MIG strategy, RDMA resource names, DMA-BUF mode, GDRCopy enablement, and other GPU Operator test inputs. |
| ENT-REQ-104 | Preserve logs and artifacts needed for partner validation evidence |
| ENT-REQ-105 | For tests that modify GPU Operator or cluster state (including GPU driver version/update policy,  MIG strategy, RDMA etc.) the wrapper must capture the pre-test configuration and restore the cluster to its original GPU Operator state after the test completes or fails |
| ENT-REQ-106 | Add AI Cloud Validation catalog entries for all GPUOP tests with owner, labels, dependencies, and descriptions (including updating necessary YAML files). Any committed code needs to adhere to the repository standards as documented in the contributions.md file in AI Cloud Ready repository |
| ENT-REQ-107 | Document required cluster prerequisites for each GPUOP test |
| ENT-REQ-108 | Integrate results into the ISV-NCP report and catalog |

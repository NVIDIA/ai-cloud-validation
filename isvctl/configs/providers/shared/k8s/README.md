<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Kubernetes networking probes

`networking_probe.py` supplies provider-neutral runtime evidence for `K8S29-01`,
`K8S30-01`, and `K8S31-01`. The validators grade that evidence independently.
The probes use the current kubeconfig, or the command prefix selected by `KUBECTL`.
They do not change the caller's kubeconfig or provision a cluster.

Copy [networking-example.json](networking-example.json), replace the provider
settings and ranges with those used for your test cluster, and set an absolute path:

```bash
export K8S_NETWORKING_CONFIG=/absolute/path/networking.json
uv run isvctl test run -f isvctl/configs/providers/shared/k8s/networking.yaml --phase test -- \
  -k 'K8sLoadBalancerCheck or K8sDnsForwardingCheck or K8sCidrRangesCheck'
```

To collect one probe's JSON directly:

```bash
uv run python isvctl/configs/providers/shared/k8s/networking_probe.py \
  --check=dns_forwarding --config=/absolute/path/networking.json
```

A missing section skips that check. An empty `dns_forwarding` object opts in using
the default CoreDNS settings. An unreadable/malformed file, failed API call, timeout,
or failed cleanup is a failure. By default no probe runs without explicit configuration.
A direct probe emits observations; run its validator (as the suite does) to grade
mismatched answers, IP ranges and HTTP bodies. Exit code zero alone is not proof of
conformance.

## LoadBalancer services: K8S29-01

The probe creates a backend with a unique response, a private-network client pod,
and three Services: public, private, and static-public-IP. It resolves every
reported ingress hostname/IP and contacts each address. Public/static HTTP requests
originate from the controller, so run the controller on an external network to
exercise the public access path. Private HTTP requests originate from the client pod.
Each response must equal the unique backend value. Public IPs must be globally
routable; private IPs must be RFC1918 or IPv6 ULA. The static Service's observed
addresses must exactly match `static_ips`; ignored annotations cannot pass.

The example uses an **already installed AWS Load Balancer Controller**, suitable
subnets, and pre-reserved EIPs. Replace annotations/`loadBalancerClass` for your
provider. `loadBalancerIP` is also accepted for controllers that still support it.
Supply one EIP allocation and matching expected address per selected static subnet.
The probe does not allocate/release your reserved IPs or install a cloud controller.
See the controller's [Service annotation reference](https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/service/annotations/).

## Conditional forwarding: K8S30-01

This check **temporarily edits the selected cluster CoreDNS ConfigMap**. Use a test
cluster and an identity allowed to read/patch that ConfigMap. The existing Corefile
must have the `reload` plugin enabled. The probe appends a unique `.invalid` zone
forwarding to a disposable upstream resolver, then resolves its unique record from
a pod using the cluster's normal DNS. It checks the answer, the upstream's query
log, and a normal `kubernetes.default.svc` lookup.

Cleanup removes only the exact block added by this run. Compare-and-swap patches
preserve unrelated concurrent edits and refuse to overwrite a recreated ConfigMap.
Failed restoration fails the result even if lookups passed. Avoid simultaneous
CoreDNS rollouts while running the check. The probe tests configurable forwarding
with a controlled resolver; it does not certify reachability of every enterprise
DNS server. See CoreDNS [forward](https://coredns.io/plugins/forward/) and
[reload](https://coredns.io/plugins/reload/).

## Configured ranges: K8S31-01

Supply the service, node and pod CIDRs used when provisioning the cluster. The probe
reads the live ServiceCIDR API and node `podCIDRs`, then creates a new pod and
ClusterIP Service. The validator compares configured service ranges exactly,
checks node pod-CIDR containment, and verifies the node, new pod and new Service
addresses are within the supplied ranges. IPv4 and IPv6 are supported.

This is **partial coverage** of configurable ranges: it verifies a preconfigured
cluster and fresh allocations, but does not reprovision alternative configurations
or read provider-specific node subnet APIs. To test another range choice, provision
a disposable cluster with those ranges and run again. A CNI such as default AWS VPC
CNI that does not publish node `podCIDRs` needs a provider-specific probe; this check
skips rather than inferring a configured range from one observed address.
The ServiceCIDR API must be enabled; see [Service IP ranges](https://kubernetes.io/docs/tasks/network/extend-service-ip-ranges/).

## Fixtures and cleanup

The controller needs `kubectl`, Python 3.12, Kubernetes permissions to create/delete
namespaces, pods, ConfigMaps and Services, pod exec/log access, and read access to
nodes/ServiceCIDRs for the range check. Pods use `python:3.12-alpine`; the temporary
DNS resolver uses `registry.k8s.io/coredns/coredns:v1.12.3`. Set `python_image` or
`coredns_image` within each check's settings for approved mirrors.

Resources use a unique `isv-net-*` namespace. Cleanup waits for namespace deletion
(including Service finalizers) and reports failures. CoreDNS restoration runs before
namespace deletion, even after lookup failures. Hard process termination or lost
cluster access can interrupt cleanup: inspect the reported namespace and CoreDNS
zone before retrying. Normal cleanup never deletes a preexisting namespace after a
creation conflict.

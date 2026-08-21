# Validation Contracts

Provider-agnostic validation contracts. Each YAML defines *what* to
validate (checks, expected fields, thresholds) but not *how* to run it.
[Provider configs](../providers/) import these files and supply the
commands (steps + scripts) that produce JSON for the validations to check.

- **Adding your own platform?** Start at the [my-isv scaffold](../providers/my-isv/scripts/README.md).
- **New to the framework?** See the [External Validation Guide](../../../docs/guides/external-validation-guide.md).
- **Try it without cloud credentials:** `make demo-test`.

## What runs, and when

A **platform suite** (`tests.capability: <capability>` — `vm`, `bare_metal`,
`kubernetes`, `slurm`) is the obligation attached to declaring that capability. Its checks
declare no `requires:`; they all run.

A **plain suite** (everything else — `storage`, `network`, ...) mixes checks
that need no infrastructure with checks that do. Each declares what it
presupposes:

```yaml
requires: []                # core - runs in every context
requires: [kubernetes]      # runs only under --capability kubernetes
requires: [vm, bare_metal]  # any-match: either context satisfies it
```

One rule decides what runs, and it does not depend on how you named the
config — `--suite`, `-f`, and `--label` discovery all behave identically:

> **A plain suite with no `--capability` runs its core checks.** Name a
> capability to add the checks gated on it.

```bash
isvctl test run --provider aws --suite storage                        # core only
isvctl test run --provider aws --suite storage --capability vm        # core + vm checks
```

There is no "run everything" context: no ISV runs on `vm` and `kubernetes`
at once, so a run always carries exactly one context. Steps follow the same
rule — give a step `requires:` when it builds or tears down a fixture only
some contexts need, so a core run neither provisions nor leaks it.

## Running against a system that is already up

A canonical suite can be run directly with `--suite` (or `-f`), against a
cluster you already have and with no provider selected. Whether any lifecycle
runs depends on the file: a suite that declares no `commands:` runs its test
phase only, and one that declares them supplies a scaffold fixture that reports
on the current system rather than provisioning anything. For example, run
selected Kubernetes storage probes against the cluster `KUBECTL` selects:

```bash
ISVTEST_INCLUDE_UNRELEASED=1 uv run isvctl test run --suite storage \
  --capability kubernetes -- \
  -k "K8sNfsMountOptionsCheck or K8sCsiStorageTypesCheck"
```

`storage` gets its StorageClass names from its `setup_cluster` fixture, which
reports the classes already installed on the cluster. Set `K8S_CSI_BLOCK_SC`,
`K8S_CSI_SHARED_FS_SC`, or `K8S_CSI_NFS_SC` to name them yourself, and add
`--phase test` to skip the fixture entirely. Checks bound to provider-produced
step output still skip as `step_not_configured` when their provider is absent.
The equivalent explicit-file form is
`-f isvctl/configs/suites/storage.yaml`.

Suites:
[`iam`](iam.yaml),
[`network`](network.yaml),
[`vm`](vm.yaml),
[`bare_metal`](bare_metal.yaml),
[`storage`](storage.yaml),
[`observability`](observability.yaml),
[`k8s`](k8s.yaml),
[`slurm`](slurm.yaml),
[`control-plane`](control-plane.yaml),
[`image-registry`](image-registry.yaml),
[`security`](security.yaml).
For the domain / script-count / AWS-reference overview see the
[my-isv scaffold README](../providers/my-isv/scripts/README.md#domains).

## Naming a check

A wiring name is the test's identity everywhere downstream — the catalog, the
report, the service — so it has to be globally unique and say what it proves.
Wiring a generic check under its class name spends that identity on the
implementation instead, and repeating it produces several rows all called
`StepSuccessCheck`. Name the property under test and list the generic checks
that establish it:

```yaml
    setup_checks:
      step: create_user
      checks:
        IamUserCreatedCheck:
          test_id: "IAM01-01"
          labels: ["iam"]
          requires: []
          description: "Check the IAM user is created with usable credentials"
          compose:
            - StepSuccessCheck
            - FieldExistsCheck:
                fields: ["username", "access_key_id"]
```

That is one catalog entry carrying one `test_id`, and one test. Every member
runs against the group's step output as a subtest, so a failure still names the
part that broke, and every member runs even after an earlier one fails. A member
that needs parameters takes them inline (`- CheckName: {...}`); one that does
not stays a single line.

Because a composite has no validation class to borrow from, it declares its own
`description` (the catalog uses it) and its name must not shadow a class name. A
check that wires one purpose-built class — `SerialConsoleCheck`,
`IamCredentialAccessCheck` — already has a name that says what it proves and
keeps it.

The generic checks — `StepSuccessCheck`, `FieldExistsCheck`, `FieldValueCheck`,
`CrudOperationsCheck` — are marked `compose_only` in their class definition and a
suite may only reach them from inside a `compose` list. Suffixing the class name
(`StepSuccessCheck-teardown`) does not count: it still leaves the mechanism, not
the property under test, as the test's public identity.

### Prefixes

A `Bm`/`Vm` prefix means the check asserts a property of one bare-metal host or
one VM, so the same property can be proven for both without the two names
colliding: `BmGpusPresentCheck` and `VmGpusPresentCheck`, `BmCloudInitCheck` and
`VmCloudInitCheck`. The prefix follows the subject, not the suite and not the
`test_id`'s requirement family — `BmHardwareSerialCheck` proves BFX03-01 and
`BmCloudInitCheck` proves BOOT02-01, and both are still properties of a host.

A check that asserts something about the platform, the fabric, or a service
stays unprefixed even when it is wired into `bare_metal.yaml`, because there is
no per-host reading of it to distinguish: `GovernanceMetricsCheck` (fleet-wide
counts), `HealthAggregationCheck` (cluster/nodegroup/reservation level),
`IbTenantIsolationCheck` (fabric), `StableStorageNodeIpCheck` (storage service).
Those live here because that is where the provider supplying the step imports
from; several already carry a `network` or `sds_controller` label for the same
reason. Prefixing them would claim a scope their plan items do not have.

Three more stay unprefixed for narrower reasons.
`VirtualDeviceHardeningCheck` (CNP01-17) already names its subject and has no
bare-metal reading. `HostHealthCheck` (CAP05-01) is the per-host half of a pair
whose other half is fleet-level, and prefixing one half would hide that they are
one requirement at two scopes. `SerialConsoleRetentionCheck` (CNP06-02) is a
property of the console archive rather than of the host, and unlike CNP06-01/03
its plan item is not platform-scoped.

## Test Suite Details

### IAM (`iam.yaml`)

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `create_user` | setup | `providers/my-isv/scripts/iam/create_user.py` | `username`, `user_id`, `access_key_id`, `secret_access_key` |
| `test_credentials` | test | `providers/my-isv/scripts/iam/test_credentials.py` | `account_id`, `tests.identity.passed`, `tests.access.passed` (`IamCredentialAccessCheck` / IAM03-01) |
| `teardown` | teardown | `providers/my-isv/scripts/iam/delete_user.py` | `resources_deleted`, `message` |

### Network (`network.yaml`)

| Step | Phase | Script | What It Tests |
|------|-------|--------|---------------|
| `create_network` | setup | `providers/my-isv/scripts/network/create_vpc.py` | Shared VPC creation |
| `list_vpcs` | test | `providers/nico/scripts/network/list_vpcs.py` | Pre-provisioned VPC inventory: `vpcs`, `count`, `found_target` |
| `get_vpc` | test | `providers/nico/scripts/network/get_vpc.py` | Single VPC identity: `vpc_id`, `vpc_name` |
| `subnet_assignment` | test | `providers/nico/scripts/network/check_subnet_assignment.py` | `tests.subnet_assigned.passed` for the VPC under test |
| `vpc_crud` | test | `providers/my-isv/scripts/network/vpc_crud_test.py` | Create/Read/Update/Delete lifecycle |
| `subnet_config` | test | `providers/my-isv/scripts/network/subnet_test.py` | Multi-AZ subnet distribution |
| `vpc_isolation` | test | `providers/my-isv/scripts/network/isolation_test.py` | Security boundaries between VPCs |
| `sg_crud` | test | `providers/my-isv/scripts/network/sg_crud_test.py` | Security group create/read/update/delete lifecycle |
| `security_blocking` | test | `providers/my-isv/scripts/network/security_test.py` | Firewall/ACL blocking rules |
| `connectivity_test` | test | `providers/my-isv/scripts/network/test_connectivity.py` | Instance network assignment |
| `traffic_validation` | test | `providers/my-isv/scripts/network/traffic_test.py` | Ping allowed/blocked, internet |
| `vpc_ip_config` | test | `providers/my-isv/scripts/network/vpc_ip_config_test.py` | DHCP options, subnet CIDRs, auto-assign IP |
| `dhcp_ip_test` | test | `providers/my-isv/scripts/network/dhcp_ip_test.py` | DHCP lease, IP match, DNS options via SSH |
| `byoip_test` | test | `providers/my-isv/scripts/network/byoip_test.py` | Bring-Your-Own-IP with custom CIDRs |
| `stable_ip_test` | test | `providers/my-isv/scripts/network/stable_ip_test.py` | IP persistence across stop/start |
| `floating_ip_test` | test | `providers/my-isv/scripts/network/floating_ip_test.py` | Atomic IP switch between instances |
| `dns_test` | test | `providers/my-isv/scripts/network/dns_test.py` | Custom internal domain resolution |
| `sg_workload_scoping` | test | `providers/my-isv/scripts/network/sg_scoping_test.py` | SG rules scoped at workload level |
| `sg_node_scoping` | test | `providers/my-isv/scripts/network/sg_scoping_test.py` | SG rules scoped at node level |
| `sg_subnet_scoping` | test | `providers/my-isv/scripts/network/sg_scoping_test.py` | SG rules scoped at subnet/tenant level |
| `sg_service_scoping` | test | `providers/my-isv/scripts/network/sg_scoping_test.py` | SG rules scoped at service level (e.g. K8s API) |
| `sdn_hardware_fault_logging` | test | `providers/my-isv/scripts/network/sdn_logging_test.py` | SDN hardware fault log visibility |
| `sdn_latency_perf_logging` | test | `providers/my-isv/scripts/network/sdn_logging_test.py` | SDN latency/performance telemetry samples |
| `sdn_filter_audit_trail` | test | `providers/my-isv/scripts/network/sdn_logging_test.py` | Audit trail for filtering rule changes |
| `peering_test` | test | `providers/my-isv/scripts/network/peering_test.py` | Cross-VPC connectivity |
| `backend_switch_fabric` | test | `providers/my-isv/scripts/network/backend_switch_fabric_test.py` | Backend leaf, spine, and core switch IDs |
| `nvlink_domain` | test | `providers/my-isv/scripts/network/nvlink_domain_test.py` | NVLink domain ID when the node supports NVLink |
| `teardown` | teardown | `providers/my-isv/scripts/network/teardown.py` | VPC cleanup |

### Observability (`observability.yaml`)

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `vpc_flow_logs` | test | `providers/my-isv/scripts/observability/log_availability_test.py` | `tests.*.probes.network_id`, `log_destination`, `traffic_type` |
| `host_syslogs` | test | `providers/my-isv/scripts/observability/log_availability_test.py` | `tests.*.probes.hosts_checked`, `log_source`, `entry_count`, `latest_timestamp` |
| `bmc_sel_logs` | test | `providers/my-isv/scripts/observability/log_availability_test.py` | `tests.*.probes.bmc_endpoints_checked`, `log_source`, `entry_count` |
| `bmc_gpu_telemetry` | test | `providers/my-isv/scripts/observability/log_availability_test.py` | `tests.*.probes.bmc_endpoints_checked`, `telemetry_endpoint`, `metric_names`, `host_os_unavailable_metrics`, `sample_count` |
| `storage_capacity_telemetry` | test | `providers/my-isv/scripts/observability/storage_telemetry_test.py` | `tests.*.probes.volumes_checked`, `telemetry_source`, `metric_names`, `capacity_kinds`, `sample_count`, `latest_timestamp` |
| `storage_performance_telemetry` | test | `providers/my-isv/scripts/observability/storage_telemetry_test.py` | `tests.*.probes.volumes_checked`, `telemetry_source`, `metric_names`, `performance_kinds`, `sample_count`, `latest_timestamp` |
| `gpu_nvlink_telemetry` | test | `providers/my-isv/scripts/observability/nvlink_telemetry_test.py` | `tests.*.probes.links_checked`, `telemetry_source`, `metric_names`, `sample_count`, `latest_timestamp` |
| `switch_nvlink_telemetry` | test | `providers/my-isv/scripts/observability/nvlink_telemetry_test.py` | `tests.*.probes.ports_checked`, `telemetry_source`, `metric_names`, `sample_count`, `latest_timestamp` |
| `ufm_event_logs` | test | `providers/my-isv/scripts/observability/log_availability_test.py` | `tests.*.probes.log_endpoints_checked`, `log_source`, `entry_count`, `latest_timestamp` |
| `general_switch_logs` | test | `providers/my-isv/scripts/observability/log_availability_test.py` | `tests.*.probes.switches_checked`, `log_source`, `entry_count`, `latest_timestamp` |
| `switch_syslogs` | test | `providers/my-isv/scripts/observability/log_availability_test.py` | `tests.*.probes.switches_checked`, `log_source`, `entry_count`, `latest_timestamp` |
| `switch_kernel_logs` | test | `providers/my-isv/scripts/observability/log_availability_test.py` | `tests.*.probes.switches_checked`, `log_source`, `entry_count`, `latest_timestamp` |

### VM (`vm.yaml`)

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `launch_instance` | setup | `providers/my-isv/scripts/vm/launch_instance.py` | `instance_id`, `public_ip`, `key_file`, `vpc_id`, `requested_key_name`, `key_name` |
| `list_instances` | test | `providers/my-isv/scripts/vm/list_instances.py` | `instances`, `total_count` |
| `verify_tags` | test | `providers/my-isv/scripts/vm/describe_tags.py` | `instance_id`, `tags`, `tag_count` |
| `serial_console` | test | `providers/my-isv/scripts/vm/serial_console.py` | `console_available`, `serial_access_enabled` |
| `component_key_access` | test | `providers/my-isv/scripts/vm/component_key_access.py` | `key_name`; for non-skipped results also `tests.sol_access.passed`, `tests.network_device_access.passed` (`VmComponentKeyAccessCheck` / AUTH03-01; AWS may emit top-level `skipped` when serial console access is disabled) |
| `stop_instance` | test | `providers/my-isv/scripts/vm/stop_instance.py` | `instance_id`, `state`, `stop_initiated` |
| `start_instance` | test | `providers/my-isv/scripts/vm/start_instance.py` | `instance_id`, `state`, `public_ip`, `ssh_ready` |
| `reboot_instance` | test | `providers/my-isv/scripts/vm/reboot_instance.py` | `reboot_initiated`, `ssh_ready`, `uptime_seconds` |
| `describe_instance` | test | `providers/my-isv/scripts/vm/describe_instance.py` | `instance_id`, `state`, `public_ip`, `key_file` |
| `deploy_nim` | test | `providers/shared/deploy_nim.py` | `container_id`, `health_endpoint` |
| `teardown_nim` | teardown | `providers/shared/teardown_nim.py` | `message` |
| `teardown` | teardown | `providers/my-isv/scripts/vm/teardown.py` | `resources_deleted`, `message` |

### Bare Metal (`bare_metal.yaml`)

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `launch_instance` | setup | `providers/my-isv/scripts/bare_metal/launch_instance.py` | `instance_id`, `public_ip`, `key_file`, `vpc_id` |
| `list_instances` | test | `providers/my-isv/scripts/vm/list_instances.py` | Reuses VM script |
| `verify_tags` | test | `providers/my-isv/scripts/bare_metal/describe_tags.py` | `instance_id`, `tags`, `tag_count` |
| `topology_placement` | test | `providers/my-isv/scripts/bare_metal/topology_placement.py` | `placement_supported`, `operations` |
| `serial_console` | test | `providers/my-isv/scripts/bare_metal/serial_console.py` | `console_available`, `serial_access_enabled`, `console_log_queryable`, `retention_days_required`, `retention_days_configured`, `oldest_queryable_log_age_days`, `query_result_count`, `retention_evidence` |
| `verify_image` | test | `providers/aws/scripts/image-registry/verify_image_installed.py` | `instance_id`, `image_id`, `image_name`, `instance_state` - BOOT01-03 for providers whose image-registry run cannot install a metal host |
| `stop_instance` | test | `providers/my-isv/scripts/bare_metal/stop_instance.py` | `instance_id`, `state`, `stop_initiated` |
| `start_instance` | test | `providers/my-isv/scripts/bare_metal/start_instance.py` | `instance_id`, `state`, `public_ip`, `ssh_ready` |
| `reboot_instance` | test | `providers/my-isv/scripts/bare_metal/reboot_instance.py` | `reboot_initiated`, `ssh_ready`, `uptime_seconds` |
| `power_cycle_instance` | test | `providers/my-isv/scripts/bare_metal/power_cycle_instance.py` | `instance_id`, `state`, `public_ip`, `ssh_ready` |
| `describe_instance` | test | `providers/my-isv/scripts/bare_metal/describe_instance.py` | `state`, `public_ip`, `key_file` |
| `reinstall_instance` | test | `providers/my-isv/scripts/bare_metal/reinstall_instance.py` | `instance_state` (skipped by default) |
| `deploy_nim` | test | `providers/shared/deploy_nim.py` | Shared NIM deployment |
| `teardown_nim` | teardown | `providers/shared/teardown_nim.py` | Shared NIM cleanup |
| `teardown` | teardown | `providers/my-isv/scripts/bare_metal/teardown.py` | `resources_deleted`, `message` |
| `verify_teardown` | teardown | `providers/my-isv/scripts/bare_metal/verify_terminated.py` | `checks.instance_terminated`, `checks.sg_deleted` |
| `verify_ingestion` | test | `providers/nico/scripts/hardware_ingestion/verify_ingestion.py` | `expected_count`, `ingested_count`, `matched_count`, `missing`, `extra`, `machines[].status`, `machines[].health` |
| `check_dpu_health` | test | `providers/nico/scripts/dpu/check_dpu_health.py` | `machines_checked`, `machines[].dpu_count`, `machines[].dpu_agent_heartbeat`, `machines[].health_summary`, `machines[].health_alerts` |
| `check_dpu_network` | test | _no implementation yet_ | `interfaces[].{name,status,type}`, `bgp_enabled`, optional `dpu_extension_deployments[].{name,status,version}` |
| `query_governance_metrics` | test | `providers/nico/scripts/governance/query_metrics.py` | `machine_count`, `metrics.delivered.{nodes,gpus}`, `metrics.healthy.{nodes,gpus}`, `metrics.reserved.{nodes,gpus}`, `metrics.active.{nodes,gpus}` |
| `query_fleet_inventory` | test | `providers/nico/scripts/governance/query_fleet_inventory.py` | `nodes_checked`, `nodes[].{node_id,health_state,instance_id,created_at,hardware_type,gpu_count,account_id,project_id,in_use,region}` |
| `query_resource_discovery` | test | `providers/nico/scripts/governance/query_resource_discovery.py` | `polls`, `poll_interval_seconds`, `unstable_identifiers`, `resources_checked`, `resources[].{resource_id,delivery_reason,discovered}` |
| `query_host_health` | test | `providers/nico/scripts/health/query_host_health.py` | `hosts_checked`, `hosts[].health_present`, `hosts[].healthy`, `hosts[].observed_age_seconds`, `hosts[].probe_ids`, `hosts[].alerts[].{id,target,message,classifications}`, `hosts[].components.{gpu,thermal,memory,cooling}` |
| `query_health_aggregation` | test | `providers/nico/scripts/health/query_health_aggregation.py` | `aggregation_level`, `groups[].{total,healthy,unhealthy,status,unhealthy_hosts}` |
| `query_ib_tenant_isolation` | test | `providers/nico/scripts/infiniband/query_ib_tenant_isolation.py` | `partitions_checked`, `partitions[].{name,partition_key,tenant_id,status}` |
| `query_ib_keys` | test | `providers/nico/scripts/infiniband/query_ib_keys.py` | `partitions_with_pkey`, `keys.<name>.{configured,source,detail}` |
| `query_sanitization` | test | `providers/nico/scripts/sanitization/query_sanitization.py` | `machines_checked`, `machines[].{available,in_use,has_gpu,served_tenant,sanitized,breakfix_skip_observed,tenancy_preserved,stale_tenant_binding,vendor,product_name,bios_version,transitions}` |
| `query_stable_ips` | test | `providers/nico/scripts/storage/query_stable_ips.py` | `hosts_checked`, `hosts[].{host_id,hw_sku_device_type,primary_ip_addresses}` |
| `query_oob_health` | test | `providers/nico/scripts/health/query_oob_health.py` | `hosts_checked`, `hosts[].{host_id,oob_health_present,bmc_probe_ids,failure_categories.<device\|network\|memory\|drive>.{observable,probe_ids}}` |
| `query_attestation` | test | `providers/nico/scripts/attestation/query_attestation.py` | `machines_checked`, `machines[].{attestation_supported,nonce_verified,attestation_signature_valid,secure_boot_enabled,boot_measurements_attested,measured_boot_state}` |
| `query_topology` | test | `providers/nico/scripts/topology/query_topology.py` | `hosts_checked`, `hosts[].{host_id,failure_domain}` |
| `query_serial_numbers` | test | `providers/nico/scripts/hardware_inventory/query_serial_numbers.py` | `machines_checked`, `machines[].components.{chassis,baseboard,cpu,gpu,nic}.{present,identifiers}` (BFX03-01) |
| `query_maintenance_events` | test | `providers/nico/scripts/breakfix/query_maintenance_events.py` | `events_queryable`, `events[].{machine_id,status,message}` (BFX02-01) |
| `query_retirement_notices` | test | `providers/my-isv/scripts/breakfix/query_retirement_notices.py` | `notices_queryable`, `notices[].{target_type,target_id,notice_type,origin_kind,origin,not_before}` (BFX02-02) |
| `query_repair_history` | test | `providers/nico/scripts/breakfix/query_repair_history.py` | `history_queryable`, `records[].{machine_id,entries}` -- a record needs non-empty `entries` to count (BFX02-03) |
| `query_switch_firmware` | test | `providers/my-isv/scripts/breakfix/query_switch_firmware.py` | `trays[].{tray_id,firmware_version}` (BFX03-02) |
| `query_bmc_kernel_logs` | test | `providers/nico/scripts/breakfix/query_bmc_kernel_logs.py` | `hosts[].{host_id,kernel_log_available}` (BFX03-03) |
| `return_node_maintenance` | test | `providers/my-isv/scripts/breakfix/return_node_maintenance.py` | `operation.{requested,accepted,machine_id,maintenance_mode}` (BFX01-02) |
| `return_rack_maintenance` | test | `providers/my-isv/scripts/breakfix/return_rack_maintenance.py` | `operation.{requested,accepted,rack_id}` (BFX01-03) |
| `request_host_replacement` | test | `providers/my-isv/scripts/breakfix/request_host_replacement.py` | `operation.{requested,node_removed_from_pool,machine_id}` (BFX01-05) |
| `query_node_health_agents` | test | `providers/my-isv/scripts/breakfix/query_node_health_agents.py` | `agents_observable`, `agents[].{node_id,agent_name,running}` (BFX04-01) |
| `query_planned_notifications` | test | `providers/my-isv/scripts/breakfix/query_planned_notifications.py` | `notification_channel_observable`, `notifications[].{machine_id,type,message,notified_at}` (BFX05-01) |
| `query_failure_notifications` | test | `providers/my-isv/scripts/breakfix/query_failure_notifications.py` | `notification_channel_observable`, `notifications[].{machine_id,type,message,notified_at}` (BFX06-01) |

### Storage (`storage.yaml`)

Umbrella suite for the storage capability area. Today it covers persistent block
storage (DATASVC-XX-02/03/04); future object/file storage checks land here too rather
than spawning new suites. A shared fixture (`launch_instance` + `create_volume`)
provisions one instance with a single attached, formatted, mounted, and seeded block
volume. The three test-phase steps all reuse that fixture.

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `launch_instance` | setup | `providers/my-isv/scripts/vm/launch_instance.py` | `instance_id`, `state`, `public_ip`, `key_file` (reuses VM script; only under `vm`/`bare_metal`) |
| `create_volume` | setup | `providers/my-isv/scripts/storage/create_volume.py` | `volume_id`, `mount_point`, `sentinel_content`, `operations.{create,attach,format,mount,write_sentinel}` |
| `snapshot_lifecycle` | test | `providers/my-isv/scripts/storage/snapshot_lifecycle.py` | `volume_id`, `snapshot_id`, `operations.{create_snapshot,restore_volume,verify_data}` (verify_data includes `content_matches`) |
| `volume_resize` | test | `providers/my-isv/scripts/storage/volume_resize.py` | `volume_id`, `operations.{modify_volume,grow_partition,resize_filesystem,verify_size}` |
| `volume_persistence` | test | `providers/my-isv/scripts/storage/volume_persistence.py` | `volume_id`, `operations.{stop,start,verify_attached,verify_data}` (verify_data includes `content_matches`) |
| `teardown_volume` | teardown | `providers/my-isv/scripts/storage/teardown_volume.py` | `resources_deleted`, `message` |
| `teardown` | teardown | `providers/my-isv/scripts/vm/teardown.py` | `resources_deleted`, `message` (reuses VM script) |

### Kubernetes (`k8s.yaml`)

| Step | Phase | Script |
|------|-------|--------|
| `setup` | setup | `providers/my-isv/scripts/k8s/setup.sh` |
| `teardown` | teardown | `providers/my-isv/scripts/k8s/teardown.sh` |
| `reset_gpus` | test | `providers/my-isv/scripts/breakfix/reset_gpus.py` (BFX01-01) |
| `cordon_node` | test | `providers/my-isv/scripts/breakfix/cordon_node.py` (BFX01-04) |

Validations use `kubectl` directly (or a custom CLI via the `KUBECTL` env var): node counts, GPU operator, pod health, NCCL/NIM workloads. Break-fix cordon and GPU reset are optional provider steps.

### Slurm (`slurm.yaml`)

| Step | Phase | Script |
|------|-------|--------|
| `setup` | setup | `providers/my-isv/scripts/slurm/setup.sh` |
| `teardown` | teardown | `providers/my-isv/scripts/slurm/teardown.sh` |

Validations use `sinfo`/`srun` directly: partitions, GPU allocation, job scheduling.

### Control Plane (`control-plane.yaml`)

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `check_api` | setup | `providers/my-isv/scripts/control-plane/check_api.py` | `account_id`, `tests` |
| `create_access_key` | setup | `providers/my-isv/scripts/control-plane/create_access_key.py` | `username`, `access_key_id` |
| `create_tenant` | setup | `providers/my-isv/scripts/control-plane/create_tenant.py` | `tenant_name`, `tenant_id` |
| `test_access_key` | test | `providers/my-isv/scripts/control-plane/test_access_key.py` | `authenticated`, `account_id` |
| `disable_access_key` | test | `providers/my-isv/scripts/control-plane/disable_access_key.py` | `status` |
| `verify_key_rejected` | test | `providers/my-isv/scripts/control-plane/verify_key_rejected.py` | `rejected`, `error_code` |
| `list_tenants` | test | `providers/my-isv/scripts/control-plane/list_tenants.py` | `found_target`, `target_tenant`, `count` |
| `get_tenant` | test | `providers/my-isv/scripts/control-plane/get_tenant.py` | `tenant_name`, `description` |
| `s3_object_lifecycle` | test | `providers/my-isv/scripts/control-plane/s3_object_lifecycle.py` | `bucket_name`, `object_key`, `operations.{put,get,delete}` (get includes `content_matches`) |
| `delete_access_key` | teardown | `providers/my-isv/scripts/control-plane/delete_access_key.py` | `resources_deleted` |
| `delete_tenant` | teardown | `providers/my-isv/scripts/control-plane/delete_tenant.py` | `resources_deleted` |

### Image Registry (`image-registry.yaml`)

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `upload_image` | setup | `providers/my-isv/scripts/image-registry/upload_image.py` | `image_id`, `storage_bucket`, `disk_ids` |
| `crud_image` | test | `providers/my-isv/scripts/image-registry/crud_image.py` | `image_id`, `operations` |
| `launch_instance` | test | `providers/my-isv/scripts/image-registry/launch_instance.py` | `instance_id`, `public_ip`, `key_path` |
| `crud_install_config` | test | `providers/my-isv/scripts/image-registry/crud_install_config.py` | `config_id`, `config_name`, `operations` |
| `install_image_bm` | test | `providers/my-isv/scripts/image-registry/install_image_bm.py` | `instance_id`, `image_id`, `instance_state` |
| `install_config_bm` | test | `providers/my-isv/scripts/image-registry/install_config_bm.py` | `instance_id`, `config_id`, `instance_state`, `state` |
| `teardown_instance` | teardown | `providers/my-isv/scripts/image-registry/teardown.py` | `resources_deleted`, `message` (instance, key pair, security group, instance profile — only under `vm`) |
| `teardown_image` | teardown | `providers/my-isv/scripts/image-registry/teardown.py` | `resources_deleted`, `message` (image, disks, bucket — always) |

### Security (`security.yaml`)

| Step | Phase | Script | What It Tests |
|------|-------|--------|---------------|
| `bmc_management_network` | test | `providers/my-isv/scripts/security/bmc_management_network_test.py` | BMC management network is dedicated and restricted |
| `bmc_tenant_isolation` | test | `providers/my-isv/scripts/security/bmc_isolation_test.py` | BMC/IPMI/Redfish unreachable from tenant network |
| `bmc_protocol_security` | test | `providers/my-isv/scripts/security/bmc_protocol_security_test.py` | CNP10-01: IPMI disabled; Redfish over TLS with AAA |
| `bmc_bastion_access` | test | `providers/my-isv/scripts/security/bmc_bastion_access_test.py` | SEC12-03: BMC reachable only through a hardened bastion |
| `api_endpoint_isolation` | test | `providers/my-isv/scripts/security/api_endpoint_test.py` | API endpoints not publicly accessible |
| `mutual_tls_test` | test | `providers/shared/mutual_tls_test.py` | SEC13-01: mTLS (or equivalent) for north-south and east-west traffic |
| `insecure_protocols_test` | test | `providers/shared/insecure_protocols_test.py` | SEC13-02: insecure protocols (HTTP, SSLv3, TLSv1) disabled |
| `mfa_enforcement` | test | `providers/my-isv/scripts/security/mfa_enforcement_test.py` | Administrative UI, CLI, and API access require MFA |
| `cert_rotation_test` | test | `providers/my-isv/scripts/security/cert_rotation_test.py` | SEC09-01: TLS certificate rotation cycle or auto-renewal |
| `kms_encryption_options_test` | test | `providers/my-isv/scripts/security/kms_encryption_options_test.py` | SEC09-02: Provider-managed and customer-managed KMS options |
| `centralized_kms_test` | test | `providers/my-isv/scripts/security/centralized_kms_test.py` | SEC09-03: Encrypted resources use centralized KMS |
| `customer_managed_key_test` | test | `providers/my-isv/scripts/security/customer_managed_key_test.py` | SEC09-04: Customer-managed key / BYOK encryption |
| `least_privilege_test` | test | `providers/my-isv/scripts/security/least_privilege_test.py` | SEC04-01/02: Least-privilege policy dimensions and minimal-role denial |
| `audit_logging_test` | test | `providers/my-isv/scripts/security/audit_logging_test.py` | SEC08-01/02: Audit-log entry metadata and retention >= 30 days |
| `sa_credential_test` | test | `providers/my-isv/scripts/security/sa_credential_test.py` | Service account long-lived credential auth |
| `oidc_user_auth_test` | test | `providers/my-isv/scripts/security/oidc_user_auth_test.py` | OIDC issuer metadata and protected endpoint token acceptance/rejection |
| `short_lived_credentials_test` | test | `providers/my-isv/scripts/security/short_lived_credentials_test.py` | SEC02-01: workloads and nodes receive credentials with finite, bounded TTL |
| `tenant_isolation_test` | test | `providers/my-isv/scripts/security/tenant_isolation_test.py` | SEC11-01: hard tenant isolation across network/data/compute/storage |
| `capacity_reservation_grouping` | test | `providers/my-isv/scripts/capacity/reservation_grouping.py` | CAP04-01: capacity is logically grouped and pinned to one account/tenant |
| `topology_block_atomic_allocation` | test | `providers/my-isv/scripts/capacity/topology_block_atomic_allocation.py` | CAP04-02: topology block allocation is atomic, homogeneous, and isolated |
| `teardown` | teardown | `providers/my-isv/scripts/security/teardown.py` | Cleanup test resources |

`capacity_reservation_grouping` verifies CAP04-01. Provider scripts must emit this minimal JSON contract:

```json
{
  "success": true,
  "platform": "provider",
  "reservation_id": "reservation-or-allocation-id",
  "account_id": "account-id",
  "resources": [
    {
      "resource_id": "resource-id",
      "resource_type": "compute|network|storage|ip_block|instance_type",
      "account_id": "account-id",
      "pinned": true
    }
  ],
  "pinned": true,
  "isolation_enforced": true
}
```

`topology_block_atomic_allocation` verifies CAP04-02. Provider scripts must emit this minimal JSON contract:

```json
{
  "success": true,
  "platform": "provider",
  "topology_block": {
    "block_id": "block-id",
    "reservation_id": "reservation-or-allocation-id",
    "tenant_id": "tenant-id",
    "allocated_as_unit": true,
    "partial_allocation": false,
    "homogeneous": true,
    "isolation_enforced": true,
    "requested": {"compute": 2, "network": 1, "storage": 0},
    "allocated": {"compute": 2, "network": 1, "storage": 0},
    "resources": [
      {
        "resource_id": "resource-id",
        "resource_type": "compute|network|storage",
        "tenant_id": "tenant-id",
        "topology_block_id": "block-id",
        "performance_domain": "performance-domain-id",
        "isolation_boundary": "tenant-id"
      }
    ]
  }
}
```

## Related Documentation

- [my-isv Scaffold](../providers/my-isv/scripts/README.md) - Copy-and-fill-in scripts for your own platform
- [External Validation Guide](../../../docs/guides/external-validation-guide.md) - Writing scripts, config format, running validations
- [Configuration Guide](../../../docs/guides/configuration.md) - Full config reference (steps, schemas, templates)
- [AWS Reference Implementation](../../../docs/references/aws.md) - Working AWS examples for all test suites

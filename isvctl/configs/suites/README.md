# Validation Contracts

Provider-agnostic validation contracts. Each YAML defines *what* to
validate (checks, expected fields, thresholds) but not *how* to run it.
[Provider configs](../providers/) import these files and supply the
commands (steps + scripts) that produce JSON for the validations to check.

- **Adding your own platform?** Start at the [my-isv scaffold](../providers/my-isv/scripts/README.md).
- **New to the framework?** See the [External Validation Guide](../../../docs/guides/external-validation-guide.md).
- **Try it without cloud credentials:** `make demo-test`.

Suites:
[`iam`](iam.yaml),
[`network`](network.yaml),
[`vm`](vm.yaml),
[`bare_metal`](bare_metal.yaml),
[`observability`](observability.yaml),
[`k8s`](k8s.yaml),
[`slurm`](slurm.yaml),
[`control-plane`](control-plane.yaml),
[`image-registry`](image-registry.yaml),
[`security`](security.yaml).
For the domain / script-count / AWS-reference overview see the
[my-isv scaffold README](../providers/my-isv/scripts/README.md#domains).

## Test Suite Details

### IAM (`iam.yaml`)

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `create_user` | setup | `providers/my-isv/scripts/iam/create_user.py` | `username`, `user_id`, `access_key_id`, `secret_access_key` |
| `test_credentials` | test | `providers/my-isv/scripts/iam/test_credentials.py` | `account_id`, `tests.identity.passed`, `tests.access.passed` |
| `teardown` | teardown | `providers/my-isv/scripts/iam/delete_user.py` | `resources_deleted`, `message` |

### Network (`network.yaml`)

| Step | Phase | Script | What It Tests |
|------|-------|--------|---------------|
| `create_network` | setup | `providers/my-isv/scripts/network/create_vpc.py` | Shared VPC creation |
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

### VM (`vm.yaml`)

| Step | Phase | Script | Key JSON Fields |
|------|-------|--------|-----------------|
| `launch_instance` | setup | `providers/my-isv/scripts/vm/launch_instance.py` | `instance_id`, `public_ip`, `key_file`, `vpc_id` |
| `list_instances` | test | `providers/my-isv/scripts/vm/list_instances.py` | `instances`, `total_count` |
| `verify_tags` | test | `providers/my-isv/scripts/vm/describe_tags.py` | `instance_id`, `tags`, `tag_count` |
| `serial_console` | test | `providers/my-isv/scripts/vm/serial_console.py` | `console_available`, `serial_access_enabled` |
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
| `query_governance_metrics` | test | `providers/nico/scripts/governance/query_metrics.py` | `machine_count`, `metrics.delivered.{nodes,gpus}`, `metrics.healthy.{nodes,gpus}`, `metrics.reserved.{nodes,gpus}`, `metrics.active.{nodes,gpus}` |
| `query_host_health` | test | `providers/nico/scripts/health/query_host_health.py` | `hosts_checked`, `hosts[].health_present`, `hosts[].healthy`, `hosts[].observed_age_seconds`, `hosts[].probe_ids`, `hosts[].alerts[].{id,target,message,classifications}`, `hosts[].components.{gpu,thermal,memory,cooling}` |
| `query_health_aggregation` | test | `providers/nico/scripts/health/query_health_aggregation.py` | `aggregation_level`, `groups[].{total,healthy,unhealthy,status,unhealthy_hosts}` |
| `query_ib_tenant_isolation` | test | `providers/nico/scripts/infiniband/query_ib_tenant_isolation.py` | `partitions_checked`, `partitions[].{name,partition_key,tenant_id,status}` |
| `query_ib_keys` | test | `providers/nico/scripts/infiniband/query_ib_keys.py` | `partitions_with_pkey`, `keys.<name>.{configured,source,detail}` |
| `query_sanitization` | test | `providers/nico/scripts/sanitization/query_sanitization.py` | `machines_checked`, `machines[].{available,in_use,has_gpu,served_tenant,sanitized,stale_tenant_binding,vendor,product_name,bios_version,transitions}` |

### Kubernetes (`k8s.yaml`)

| Step | Phase | Script |
|------|-------|--------|
| `setup` | setup | `providers/my-isv/scripts/k8s/setup.sh` |
| `teardown` | teardown | `providers/my-isv/scripts/k8s/teardown.sh` |

Validations use `kubectl` directly (or a custom CLI via the `KUBECTL` env var): node counts, GPU operator, pod health, NCCL/NIM workloads.

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
| `teardown` | teardown | `providers/my-isv/scripts/image-registry/teardown.py` | `resources_deleted`, `message` |

### Security (`security.yaml`)

| Step | Phase | Script | What It Tests |
|------|-------|--------|---------------|
| `bmc_management_network` | test | `providers/my-isv/scripts/security/bmc_management_network_test.py` | BMC management network is dedicated and restricted |
| `bmc_tenant_isolation` | test | `providers/my-isv/scripts/security/bmc_isolation_test.py` | BMC/IPMI/Redfish unreachable from tenant network |
| `bmc_protocol_security` | test | `providers/my-isv/scripts/security/bmc_protocol_security_test.py` | CNP10-01: IPMI disabled; Redfish over TLS with AAA |
| `bmc_bastion_access` | test | `providers/my-isv/scripts/security/bmc_bastion_access_test.py` | SEC12-03: BMC reachable only through a hardened bastion |
| `api_endpoint_isolation` | test | `providers/my-isv/scripts/security/api_endpoint_test.py` | API endpoints not publicly accessible |
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
| `teardown` | teardown | `providers/my-isv/scripts/security/teardown.py` | Cleanup test resources |

## Related Documentation

- [my-isv Scaffold](../providers/my-isv/scripts/README.md) - Copy-and-fill-in scripts for your own platform
- [External Validation Guide](../../../docs/guides/external-validation-guide.md) - Writing scripts, config format, running validations
- [Configuration Guide](../../../docs/guides/configuration.md) - Full config reference (steps, schemas, templates)
- [AWS Reference Implementation](../../../docs/references/aws.md) - Working AWS examples for all test suites

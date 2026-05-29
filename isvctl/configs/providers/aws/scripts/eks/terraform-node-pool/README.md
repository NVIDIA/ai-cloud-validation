# Node Pool Terraform module

This module attaches a managed node group to the cluster provisioned by
`../terraform/`, exercising the create, update (scale), and delete legs of
the node-pool CRUD contract. It is driven by `../create_node_pool.sh` (setup -
used for both initial create and subsequent updates, since `terraform apply`
is idempotent) and `../destroy_node_pool.sh` (teardown); end users should
not `terraform apply` it by hand.

## State

Local backend: by default `terraform.tfstate` in this directory. The main
cluster state in `../terraform/terraform.tfstate` is read via
`terraform_remote_state` but never written.

The module manages exactly one node group per state file. To run **several
pools side by side** in the same cluster (e.g. a CPU pool and a GPU pool with
independent node counts, per K8S06), give each pool its own state file via the
`NODE_POOL_STATE_FILE` env var on `create_node_pool.sh`/`destroy_node_pool.sh`
(e.g. `terraform.tfstate` for the CPU pool, `terraform-gpu.tfstate` for the
GPU pool). The scripts pass it through with `terraform apply -state=<file>` /
`terraform destroy -state=<file>`, so the pools never clobber one another's
state. See the `create_test_node_pool` / `create_test_gpu_node_pool` steps in
`../../../config/eks.yaml` for the wiring. A pool's destroy step must use the
same `NODE_POOL_STATE_FILE` as its create step.

## Inputs

| Variable          | Default                  | Notes                                                        |
|-------------------|--------------------------|--------------------------------------------------------------|
| `region`          | `us-west-2`              | Must match the cluster's region.                             |
| `environment`     | `dev`                    | Default tag.                                                 |
| `node_pool_name`  | `isv-test-pool`          | EKS `nodegroup` name; visible as `eks.amazonaws.com/nodegroup=<name>`. |
| `instance_types`  | `["m6i.large"]`          | CPU default; set to `["c5n.18xlarge"]` etc. for high-perf-net. |
| `ami_type`        | `AL2023_x86_64_STANDARD` | Use `AL2_x86_64_GPU` for legacy GPU pools.                   |
| `capacity_type`   | `ON_DEMAND`              | `ON_DEMAND` or `SPOT`.                                       |
| `desired_size`    | `1`                      | `min`/`max`/`desired` are pinned to this value.              |
| `labels`          | `{}`                     | Merged on top of stable `isv.ncp.validation/pool` markers.   |
| `taints`          | `[]`                     | Kubernetes effect spelling (`NoSchedule`); module translates to EKS enum. |

## Outputs

Used by `create_node_pool.sh` to shape the `node_pool` JSON payload:

- `node_pool_name`
- `label_selector` (`eks.amazonaws.com/nodegroup=<name>`)
- `desired_size`
- `expected_labels`
- `expected_taints`
- `expected_instance_types`

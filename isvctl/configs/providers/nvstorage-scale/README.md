# nvstorage-scale Provider Configs

Runs [storage-scale-test](https://github.com/NVIDIA/storage-scale-test) (elbencho / mdtest sweeps)
against storage that already exists, and reports the result through the storage suite's
**HSS02-01** check, `HssQosThroughputCheck`: provisioned throughput meets the minimum bandwidth and
IOPS. storage-scale-test measures; this provider turns the measurement into the suite's verdict.

Like every provider, it supplies commands and settings for a canonical suite
([`suites/storage.yaml`](../../suites/storage.yaml)) and defines no checks of its own.

It wraps a **separate checkout**: point `SCALE_TEST_BASE` at one, v2.0.0 or later (v2.0.0
removed `KUBECTL_PVC`/`KUBECTL_PV` and moved the claim into the `TEST_DIRS` key).

## Configs

| Config | What it runs | Capability |
|--------|--------------|------------|
| [`config/storage.yaml`](config/storage.yaml) | elbencho IO sweep on Kubernetes, one or more claims | `kubernetes` |
| [`config/overlays/multiclaim.yaml`](config/overlays/multiclaim.yaml) | the same sweep, required to spread across >= 2 weighted claims | `kubernetes` |
| [`config/overlays/mdtest.yaml`](config/overlays/mdtest.yaml) | metadata sweep (create / stat / delete) instead of IO | `kubernetes` |
| [`config/overlays/slurm.yaml`](config/overlays/slurm.yaml) | the IO sweep on a Slurm cluster (not yet run; see below) | `slurm` |

Overlays load as a second `-f` after `storage.yaml` (see Quick Start), as AWS's do.

## Quick Start

```bash
export SCALE_TEST_BASE=~/path/to/storage-scale-test
export SST_CLUSTER_NAME=my-cluster
export SST_TEST_DIRS="my-namespace/my-claim:scale-test:1"   # [namespace/]claim:path:weight
export SST_KUBECTL_NODE_SELECTOR="node-role=storage-test"   # optional; empty = any node
export SST_ORDER_NODES=1
export SST_RESULTS_DIR=~/results/sweep-$(date -u +%Y%m%dZ%H%M%S)
mkdir -p "$SST_RESULTS_DIR"

uv run isvctl test run --provider nvstorage-scale --suite storage --capability kubernetes

# a variant: name the base config and its overlay (--suite cannot be combined with -f)
uv run isvctl test run --capability kubernetes \
    -f isvctl/configs/providers/nvstorage-scale/config/storage.yaml \
    -f isvctl/configs/providers/nvstorage-scale/config/overlays/multiclaim.yaml
```

`--capability` is required: HSS02-01 runs on the `kubernetes`, `slurm`, `vm` and `bare_metal`
capabilities, so in the default core context it is skipped, and with it the step that reports it.

Inputs are `SST_*` environment variables, translated into storage-scale-test's own names by
[`gen_env_sh.py`](scripts/storage/gen_env_sh.py). It rejects unknown `SST_*` keys, so a typo fails
the first step instead of silently running defaults. Multiple claims are comma separated and
weighted: `"ns/a:scale-test:2,ns/b:scale-test:1"` loads the first claim twice as hard.
`SST_KUBECTL_NAMESPACE` sets the namespace for keys that do not name one. Every claim must already
exist and be Bound (RWX, Filesystem mode); storage-scale-test never provisions storage.

Minimum bandwidth and IOPS are `tests.settings` (`min_write_gb_s`, `min_read_gb_s`, `min_write_iops`,
`min_read_iops`; ops/s for mdtest). The defaults were baselined on one dev cluster and are tied to
the config's matrix shape — re-baseline on yours.

## How it works

`gen_env` → `preflight` → `submit_sweep` → `poll_sweep` → *`cancel`* → `collect` → *`resume`* →
`verify` → `targets` → `metrics` → `qos_throughput`

No step waits for the whole sweep: `submit_sweep` returns once the sweep is dispatched, and later
steps re-attach to it through its results directory. `collect` publishes the results and releases
the PVC Lease, Job and DaemonSet, so the steps before it continue on failure to guarantee it runs.
`verify` checks every configured cell produced output; `targets` checks every configured claim was
written to at its weight; `metrics` applies the minimums.

`qos_throughput` reports HSS02-01. It is also the gate: because the earlier steps continue on
failure, the orchestrator would not fail the run for them, so this step receives each one's output
and refuses a verdict on a sweep that lost the cluster, ran part of its matrix, missed a claim, or
left a cancel unfinished. On mdtest, `bandwidth_meets_min` is reported skipped — mdtest measures
operations, not bandwidth.

The storage suite's live Kubernetes probes (CSI install, PVC provisioning and expansion) are
excluded in `storage.yaml`: they would provision storage, and this provider only reads claims that
already exist.

## Known limitations

- **Cancel then resume is blocked upstream.** Cancelling a running sweep works, but `--collect` then
  fails to publish it (local execution sentinels stay `PENDING` though the PVC holds complete
  results), and resume refuses to run until collection is complete. The error blames lost cluster
  access, which is misleading; the driver's own `--collect` fails the same way. `cancel` and
  `resume` ship `skip: true`.
- **A sweep that is still running when the run stops keeps its cluster resources.** `collect`
  releases the PVC Lease, Job and DaemonSet, and it runs in the test phase, because `verify` and
  `metrics` need the results it publishes. If setup fails after the sweep was dispatched, or
  `poll_sweep` gives up while the sweep is still running, nothing releases them, and the next run
  against that claim waits on the Lease. Releasing a running sweep needs cancel then collect, which
  is the upstream issue above. Recover by hand once the sweep finishes:
  `storage-tests/fs/nv-elbencho-sweep.sh --status <sweep_dir>`, then `--collect <sweep_dir>`.
- **No teardown on Kubernetes.** Upstream `--delete-only` supports only SSH and Slurm, so benchmark
  data accumulates under the claim's path.
- **The Slurm overlay has not been run.** Its bandwidth minimums were baselined on a Slurm dev
  cluster, but its IOPS minimums are empty, and HSS02-01 fails with "no iops minimum configured"
  until you set them.

## Tests

[`isvctl/tests/providers/nvstorage_scale/`](../../../tests/providers/nvstorage_scale/) — run by
`make test`. The step scripts' offline suite (`test_steps.sh`, ~5 min) is opt-in:
`NVSTORAGE_SCALE_STEP_SUITE=1`. `check_upstream_drift.py` there catches `gen_env_sh.py` emitting a
variable upstream renamed or removed; run it whenever your storage-scale-test checkout moves.

## See Also

- [Validation Test Suites](../../suites/README.md) — the provider-agnostic contract
- [storage-scale-test](https://github.com/NVIDIA/storage-scale-test)

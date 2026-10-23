# Round-trip fixtures

## `elbencho-20260908Z230802/` — the real recording ✅

Recorded from a `--nodes 2` Slurm sweep whose executions
both reported SUCCESS. `env_used.{sh,yaml}` are written by `write_elbencho_env_used`
(`storage-scale-test/lib/env_functions.sh:1275`).

This is the authoritative fixture. Round-trip: **16/16 shared keys match**.

## `synthetic/env_used.yaml` — kept deliberately

Hand-written to the same shape. Retained because it is *editable*: the negative tests perturb
it to prove `compare_env.py` actually fails on drift rather than passing vacuously. Do not
delete it in favour of the real one — they serve different purposes.

## What the comparison can and cannot prove

`env_used.yaml` is a **sweep-relevant subset**. It records `TEST_DIRS`, `FS_MAX_*` and the
`ELBENCHO_*` family, and omits `RESULTS_DIR`, `LOGS_DIR`, `partition`, `run_time`, `account`,
`client_type`, `client_arch` and every `SLURM_*`. It also carries values derived at run time
from CLI flags — `dio_or_bio`, `rand_option`, `single_option`, `sweep_*`, `nodes_spec`,
`treefile_cache` — which the generator must never emit.

So the round-trip covers the elbencho/threshold/`TEST_DIRS` half only. The Slurm/path half is
covered by the golden file (`testdata/slurm-minimal.env` → `testdata/golden/slurm-minimal.env.sh`),
which has no fixture dependency.

## Re-fetching

    CTX=<your-kube-context>
    LOGIN=$(kubectl --context $CTX -n slurm get pods | awk '/slurm-login/{print $1; exit}')
    D=/scratch/scale-test/results/elbencho-20260908Z230802
    for f in env_used.yaml env_used.sh; do
      kubectl --context $CTX -n slurm exec "$LOGIN" -- cat "$D/$f" \
        > "elbencho-20260908Z230802/$f"
    done

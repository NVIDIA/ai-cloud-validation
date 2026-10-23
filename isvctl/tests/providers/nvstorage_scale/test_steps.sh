#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Offline tests for the step scripts: no cluster, no Slurm, no Kubernetes.
# Everything is exercised against a stub driver and synthetic sweep directories, because the
# behaviour worth testing here is the *state machine* over durable files, not elbencho.
#
#   ./isvctl/tests/providers/nvstorage_scale/test_steps.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This suite lives in isvctl/tests/providers/nvstorage_scale/; the provider it tests is
# isvctl/configs/providers/nvstorage-scale/.
PROV="$(cd "$HERE/../../../configs/providers/nvstorage-scale" && pwd)"
S="$PROV/scripts/storage"
export S   # the inline python below imports submit_sweep from it
PY="${PYTHON:-python3}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
ok()   { printf '  ok   %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
check(){ if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (want '$3', got '$2')"; fi; }
jqf()  { "$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get(sys.argv[2]))" "$1" "$2"; }

# ---------------------------------------------------------------- fake sweep dirs
# mk_sweep <dir> <status>...   e.g. mk_sweep d SUCCESS SUCCESS FAILED
mk_sweep() {
    local dir="$1"; shift
    mkdir -p "$dir/executions"
    local i=1
    for st in "$@"; do
        printf '%s\n' "$st" > "$(printf '%s/executions/%04d.status' "$dir" "$i")"
        printf '%s\n' "$([[ $st == SUCCESS ]] && echo 0 || echo 1)" \
            > "$(printf '%s/executions/%04d.exitcode' "$dir" "$i")"
        i=$((i+1))
    done
}

echo "== compose_env_sh: overlay splices into the template ahead of env_base.sh =="
BASE="$WORK/sst"
mkdir -p "$BASE/storage-tests/fs" "$BASE/lib"
# The one real file this suite reads. Override with SST_CHECKOUT if your checkout is elsewhere.
SST_CHECKOUT="${SST_CHECKOUT:-$PROV/../../../../../storage-scale-test}"
# Keep the value but never hand it to the step scripts: gen_env_sh.py rejects unknown SST_*
# keys by design, so an exported SST_CHECKOUT -- exactly what the hint below tells you to
# set -- failed every test that composes an env.sh with "unknown input key: SST_CHECKOUT".
export -n SST_CHECKOUT
TEMPLATE="$SST_CHECKOUT/env.sh.template"
if [[ ! -f "$TEMPLATE" ]]; then
    printf 'cannot find env.sh.template at %s\n  set SST_CHECKOUT=<storage-scale-test checkout>\n' \
        "$TEMPLATE" >&2
    exit 2
fi
cp "$TEMPLATE" "$BASE/env.sh.template"
# Stand in for the real lib so sourcing terminates; record that the chain was reached.
echo 'ACV_CHAIN_REACHED=1' > "$BASE/lib/env_base.sh"
OVERLAY="$HERE/testdata/golden/slurm-minimal.env.sh"

"$PY" - "$BASE" "$OVERLAY" <<'PYEOF' > "$WORK/composed.sh"
import sys, os
sys.path.insert(0, os.environ["S"])
sys.path.insert(0, os.path.dirname(os.environ["S"]))
from submit_sweep import compose_env_sh
sys.stdout.write(compose_env_sh(os.path.join(sys.argv[1], "env.sh.template"), sys.argv[2]))
PYEOF
if [[ -s "$WORK/composed.sh" ]]; then ok "composed a non-empty env.sh"; else bad "compose produced nothing"; fi
grep -q 'ACV overlay' "$WORK/composed.sh" && ok "overlay marker present" || bad "overlay marker missing"
# The chain line must come after the overlay, or env_base.sh derives from stale values.
ov=$(grep -n 'ACV overlay (generated' "$WORK/composed.sh" | head -1 | cut -d: -f1)
ch=$(grep -n 'lib/env_base.sh' "$WORK/composed.sh" | tail -1 | cut -d: -f1)
if [[ -n "$ov" && -n "$ch" && "$ov" -lt "$ch" ]]; then ok "overlay precedes the env_base.sh chain"
else bad "overlay/chain ordering wrong (overlay@$ov chain@$ch)"; fi

echo
echo "== compose_env_sh: the result actually sources (bash >= 4.4) =="
# macOS bash 3.2 cannot 'declare -A', so this assertion is only meaningful under a modern
# bash. Prefer docker; fall back to the local bash if it is new enough; else skip loudly.
RUNNER=""
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then RUNNER=docker
elif [[ "${BASH_VERSINFO[0]}" -ge 5 ]]; then RUNNER=local
fi
cp "$WORK/composed.sh" "$BASE/env.sh"
probe='source ./env.sh && echo "SUBSTRATE=$EXECUTION_SUBSTRATE" && echo "PART=$partition" \
  && echo "CHAIN=$ACV_CHAIN_REACHED" && echo "TD=${!TEST_DIRS[*]}" && echo "THR=${ELBENCHO_SCALE_THREAD_LIST[*]}"'
case "$RUNNER" in
  docker) out=$(docker run --rm -v "$BASE":/w -w /w debian:12 bash -c "$probe" 2>&1) ;;
  local)  out=$(cd "$BASE" && bash -c "$probe" 2>&1) ;;
  *)      out="" ;;
esac
if [[ -z "$RUNNER" ]]; then
    printf '  SKIP sourcing check: no docker and local bash is %s (needs >= 4.4 for declare -A)\n' "$BASH_VERSION"
else
    grep -q 'SUBSTRATE=slurm' <<<"$out" && ok "EXECUTION_SUBSTRATE survives the splice" || bad "substrate: $out"
    grep -q 'PART=all'        <<<"$out" && ok "overlay overrides the template's value"  || bad "partition: $out"
    grep -q 'CHAIN=1'         <<<"$out" && ok "lib/env_base.sh still gets sourced"      || bad "chain: $out"
    grep -q 'TD=/scratch'     <<<"$out" && ok "TEST_DIRS assoc array is usable"         || bad "TEST_DIRS: $out"
    grep -q 'THR=1 4'         <<<"$out" && ok "sweep arrays survive"                    || bad "threads: $out"
fi

echo
echo "== submit_sweep: detached launch, dir discovery, jobid handoff =="
RES="$WORK/results"; mkdir -p "$RES"
cat > "$BASE/storage-tests/fs/nv-elbencho-sweep.sh" <<'STUB'
#!/usr/bin/env bash
# Stub driver: mimics the real one's *durable side effects* and nothing else.
# It reads RESULTS_DIR out of env.sh rather than sourcing it, so this test stays runnable
# under macOS bash 3.2. Whether the composed env.sh actually sources is asserted separately,
# under docker, above.
set -euo pipefail
ENV_SH="$(dirname "${BASH_SOURCE[0]}")/../../env.sh"
RESULTS_DIR=$(sed -n 's/^export RESULTS_DIR="\(.*\)"$/\1/p' "$ENV_SH" | tail -1)
[[ -n "$RESULTS_DIR" ]] || { echo "stub: no RESULTS_DIR in $ENV_SH" >&2; exit 1; }
DS=$(date -u +"%Y%m%dZ%H%M%S")
OUT="${RESULTS_DIR}/elbencho-${DS}"
mkdir -p "$OUT/executions"
printf 'PENDING\n' > "$OUT/executions/0001.status"
sleep "${STUB_JOBID_DELAY:-0}"
printf '%s\n' "${STUB_JOBID:-4242}" > "$OUT/executions/.coordinator.jobid"
sleep 30
STUB
chmod +x "$BASE/storage-tests/fs/nv-elbencho-sweep.sh"

# A test double for a modern bash. submit_sweep now refuses to launch under bash 3.x (macOS),
# which is correct but would make every assertion below unrunnable on a laptop. This shim
# answers the version probe and otherwise delegates; the refusal itself is asserted separately.
FAKEBASH="$WORK/bin/bash5"; mkdir -p "$WORK/bin"
cat > "$FAKEBASH" <<'FB'
#!/bin/bash
if [[ "${2:-}" == 'echo $BASH_VERSINFO' ]]; then echo 5; exit 0; fi
exec /bin/bash "$@"
FB
chmod +x "$FAKEBASH"
# The stub needs RESULTS_DIR from the env.sh it sources, so regenerate the overlay with it.
printf 'SST_SUBSTRATE=slurm\nSST_RESULTS_DIR=%s\nSST_PARTITION=all\nSST_TEST_DIRS=%s:1\n' \
    "$RES" "$WORK/data" > "$WORK/in.env"
"$PY" "$S/gen_env_sh.py" --config "$WORK/in.env" --out "$WORK/overlay.sh" > "$WORK/gen.json" 2>"$WORK/gen.err" \
    && ok "gen_env_sh produced an overlay for the stub" || bad "gen_env_sh: $(cat "$WORK/gen.err")"

"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash deadbeef \
    --substrate slurm --results-dir "$RES" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    --dir-timeout 30 --jobid-timeout 30 > "$WORK/submit.json" 2>"$WORK/submit.err"
rc=$?
check "submit exits 0" "$rc" "0"
check "submit reports success" "$(jqf "$WORK/submit.json" success)" "True"
check "submit found the coordinator jobid" "$(jqf "$WORK/submit.json" coordinator_jobid)" "4242"
SWEEP=$(jqf "$WORK/submit.json" sweep_dir)
[[ -d "$SWEEP" ]] && ok "sweep_dir points at a real directory" || bad "sweep_dir=$SWEEP"
[[ -L "$BASE/env.sh" ]] && ok "env.sh was installed as a symlink (run artifact stays immutable)" \
    || bad "env.sh is not a symlink"
# stdout must be pure JSON or ACV silently drops the output; the stub logs to stderr only.
"$PY" -c "import json,sys;json.load(open(sys.argv[1]))" "$WORK/submit.json" \
    && ok "submit stdout is parseable JSON and nothing else" || bad "submit stdout not pure JSON"

echo
echo "== submit_sweep: a driver that dies while sourcing env.sh is reported, not hung =="
cat > "$BASE/storage-tests/fs/nv-elbencho-sweep.sh" <<'STUB'
#!/usr/bin/env bash
echo "boom" >&2; exit 1
STUB
chmod +x "$BASE/storage-tests/fs/nv-elbencho-sweep.sh"
RES2="$WORK/results2"
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash cafe \
    --substrate slurm --results-dir "$RES2" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    --dir-timeout 3 --jobid-timeout 3 > "$WORK/submit2.json" 2>/dev/null
check "failed launch exits 1" "$?" "1"
check "failed launch reports success=False" "$(jqf "$WORK/submit2.json" success)" "False"

echo
echo "== submit_sweep: an old bash is refused up front, not deep in the driver log =="
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash old \
    --substrate slurm --results-dir "$WORK/results_old" --scale-test-base "$BASE" --nodes 1 \
    --bash /bin/bash > "$WORK/oldbash.json" 2>/dev/null
if [[ "${BASH_VERSINFO[0]}" -lt 4 ]]; then
    check "bash 3.x is rejected" "$(jqf "$WORK/oldbash.json" success)" "False"
    "$PY" -c "import json,sys;assert 'declare -A' in json.load(open(sys.argv[1]))['error']" \
        "$WORK/oldbash.json" && ok "the refusal names declare -A, not a driver error" \
        || bad "refusal text is unhelpful"
else
    printf '  SKIP old-bash refusal: /bin/bash here is already >= 4\n'
fi
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash nosuch \
    --substrate slurm --results-dir "$WORK/results_nb" --scale-test-base "$BASE" --nodes 1 \
    --bash "$WORK/no-such-bash" > "$WORK/nobash.json" 2>/dev/null
check "a missing bash is reported, not raised" "$(jqf "$WORK/nobash.json" success)" "False"

echo "== poll_sweep: sentinel tallies and the abandoned-sweep case =="
mk_sweep "$WORK/s_done" SUCCESS SUCCESS SUCCESS
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_done" --substrate slurm > "$WORK/p1.json" 2>/dev/null
check "all SUCCESS -> complete"      "$(jqf "$WORK/p1.json" state)"   "complete"
check "all SUCCESS -> success"       "$(jqf "$WORK/p1.json" success)" "True"

mk_sweep "$WORK/s_mixed" SUCCESS FAILED
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_mixed" --substrate slurm > "$WORK/p2.json" 2>/dev/null
check "a FAILED cell -> complete_with_failures" "$(jqf "$WORK/p2.json" state)"   "complete_with_failures"
check "a FAILED cell -> success=False"          "$(jqf "$WORK/p2.json" success)" "False"

mk_sweep "$WORK/s_run" SUCCESS RUNNING PENDING
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_run" --substrate slurm > "$WORK/p3.json" 2>/dev/null
check "outstanding work -> running"         "$(jqf "$WORK/p3.json" state)"   "running"
check "outstanding work fails by default"   "$(jqf "$WORK/p3.json" success)" "False"
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_run" --substrate slurm --allow-incomplete \
    > "$WORK/p4.json" 2>/dev/null
check "--allow-incomplete lets a progress report pass" "$(jqf "$WORK/p4.json" success)" "True"

# The case only Slurm can answer: nothing left running, but work never finished.
STUBBIN="$WORK/bin"; mkdir -p "$STUBBIN"
printf '#!/usr/bin/env bash\nexit 1\n' > "$STUBBIN/squeue"
printf '#!/usr/bin/env bash\necho "COMPLETED"\n' > "$STUBBIN/sacct"
chmod +x "$STUBBIN/squeue" "$STUBBIN/sacct"
mk_sweep "$WORK/s_aband" SUCCESS PENDING
printf '4242\n' > "$WORK/s_aband/executions/.coordinator.jobid"
PATH="$STUBBIN:$PATH" "$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_aband" --substrate slurm \
    > "$WORK/p5.json" 2>/dev/null
check "coordinator gone + work pending -> abandoned" "$(jqf "$WORK/p5.json" state)"   "abandoned"
check "abandoned -> success=False"                   "$(jqf "$WORK/p5.json" success)" "False"

# ...and the case that must NOT be read as abandoned: Slurm unreachable.
printf '#!/usr/bin/env bash\nexit 1\n' > "$STUBBIN/sacct"; chmod +x "$STUBBIN/sacct"
PATH="$STUBBIN:$PATH" "$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_aband" --substrate slurm \
    > "$WORK/p6.json" 2>/dev/null
check "unreachable Slurm is NOT reported as abandoned" "$(jqf "$WORK/p6.json" state)" "running"

# Jinja renders a JSON null as "None" and ACV only drops *empty* rendered args, so a sweep
# whose jobid was never found arrives here as the literal string. It must fall back to disk.
mk_sweep "$WORK/s_nonestr" SUCCESS
printf '9999\n' > "$WORK/s_nonestr/executions/.coordinator.jobid"
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_nonestr" --substrate slurm \
    --coordinator-jobid None > "$WORK/p8.json" 2>/dev/null
check 'literal "None" jobid falls back to the on-disk handle' \
    "$(jqf "$WORK/p8.json" coordinator_jobid)" "9999"

# ...and with no Slurm client at all (a laptop), which must be a report, not a traceback.
PATH="/usr/bin:/bin" "$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_nonestr" --substrate slurm \
    > "$WORK/p9.json" 2>/dev/null
check "absent squeue/sacct is reported, not raised" "$(jqf "$WORK/p9.json" state)" "complete"

mkdir -p "$WORK/s_empty/executions"
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/s_empty" --substrate slurm > "$WORK/p7.json" 2>/dev/null
check "no sentinels -> unreified" "$(jqf "$WORK/p7.json" state)" "unreified"

echo
echo "== collect_sweep: execution roll-up and CSV selection =="
mk_sweep "$WORK/c_ok" SUCCESS SUCCESS
printf 'a,b\n1,2\n3,4\n' > "$WORK/c_ok/elbencho-1M-c_001-s_004-d_000_20260918Z120000.csv"
printf 'a,b\n9,9\n'      > "$WORK/c_ok/elbencho-1M-c_001-s_004-d_000_20260918Z120000-live.csv"
printf 'x\n' > "$WORK/c_ok/env_used.yaml"
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/c_ok" --substrate slurm > "$WORK/c1.json" 2>/dev/null
check "clean sweep -> success"      "$(jqf "$WORK/c1.json" success)"   "True"
check "counts the executions"       "$(jqf "$WORK/c1.json" succeeded)" "2"
check "reports complete"            "$(jqf "$WORK/c1.json" complete)"  "True"
n=$("$PY" -c "import json,sys;print(len(json.load(open(sys.argv[1]))['results_csv']))" "$WORK/c1.json")
check "live CSV excluded from results" "$n" "1"
rows=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['results_csv'][0]['rows'])" "$WORK/c1.json")
check "CSV row count read" "$rows" "2"

mk_sweep "$WORK/c_bad" SUCCESS FAILED
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/c_bad" --substrate slurm > "$WORK/c2.json" 2>/dev/null
check "a FAILED cell fails the step" "$(jqf "$WORK/c2.json" success)" "False"
"$PY" -c "import json,sys;assert 'resume' in json.load(open(sys.argv[1]))['error']" "$WORK/c2.json" \
    && ok "error names the --resume recovery path" || bad "error text lacks --resume"
# ...but it still publishes what did finish, which is the whole point of continue_on_failure.
check "partial results still published" "$(jqf "$WORK/c2.json" succeeded)" "1"

mk_sweep "$WORK/c_part" SUCCESS PENDING
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/c_part" --substrate slurm > "$WORK/c3.json" 2>/dev/null
check "incomplete passes without --require-complete" "$(jqf "$WORK/c3.json" success)" "True"
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/c_part" --substrate slurm --require-complete \
    > "$WORK/c4.json" 2>/dev/null
check "--require-complete rejects it" "$(jqf "$WORK/c4.json" success)" "False"

echo
echo "== SPEC does not drift from upstream storage-scale-test =="
# The one check that looks at upstream at all. The golden file compares the generator to
# itself and the round-trip fixture is frozen, so neither can see a rename upstream -- which
# would make the generator emit a variable nothing reads and the sweep run on defaults.
DRIFT="$HERE/check_upstream_drift.py"
if [[ -f "$SST_CHECKOUT/env.sh.template" ]]; then
    "$PY" "$DRIFT" --scale-test-base "$SST_CHECKOUT" > "$WORK/drift.txt" 2>&1 \
        && ok "every SPEC target still exists upstream" \
        || bad "SPEC has drifted from upstream: $(grep -A4 DRIFT "$WORK/drift.txt" | head -5)"

    # A checker that cannot fail is not a checker: rename a variable in a scratch copy of
    # upstream and require the drift to be reported.
    D="$WORK/driftcopy"; mkdir -p "$D/lib"
    cp "$SST_CHECKOUT/env.sh.template" "$D/" 2>/dev/null
    cp "$SST_CHECKOUT/lib/env_base.sh" "$D/lib/" 2>/dev/null
    "$PY" - "$D" <<'PYREN'
import re, sys, os
for rel in ("env.sh.template", "lib/env_base.sh"):
    p = os.path.join(sys.argv[1], rel)
    if os.path.exists(p):
        t = open(p).read().replace("ELBENCHO_SCALE_THREAD_LIST", "ELBENCHO_THREADS")
        open(p, "w").write(t)
PYREN
    if "$PY" "$DRIFT" --scale-test-base "$D" >/dev/null 2>&1; then
        bad "the drift checker did NOT catch a renamed variable"
    else
        ok "a renamed upstream variable is reported as drift"
    fi

    # The rename case above was always caught. The REMOVAL case was not: storage-scale-test
    # v2.0.0 deleted KUBECTL_PVC and KUBECTL_PV, and because both SPEC entries are gated to
    # (KUBECTL,) the checker filed them under "pending upstream merge -- expected absent from
    # main" and exited 0, while the generator kept emitting them. A live cluster run caught it
    # instead. The exemption is now conditional on upstream NOT yet defining any KUBECTL_*
    # name; this proves it stays conditional.
    G="$WORK/gen_removed.py"
    "$PY" - "$PROV/scripts/storage/gen_env_sh.py" "$G" <<'PYRM'
import sys
src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
anchor = '    "SST_KUBECTL_NAMESPACE"'
assert anchor in s, "SPEC anchor missing -- the SPEC literal's shape changed"
s = s.replace(anchor,
              '    "SST_KUBECTL_PVC": ("KUBECTL_PVC", "str", (KUBECTL,)),\n' + anchor, 1)
open(dst, "w").write(s)
PYRM
    if "$PY" "$DRIFT" --scale-test-base "$SST_CHECKOUT" --generator "$G" >"$WORK/rm.txt" 2>&1; then
        bad "a kubectl-only entry REMOVED upstream was excused as 'pending upstream merge'"
    else
        ok "an entry removed upstream is reported as drift, not pending merge"
        grep -q "KUBECTL_PVC" "$WORK/rm.txt" \
            && ok "...and the removed variable is named" \
            || bad "drift reported but KUBECTL_PVC was not named"
    fi
else
    printf '  SKIP upstream drift check: no checkout at %s\n' "$SST_CHECKOUT"
fi

echo "== run-metadata keys are accepted, unknown keys still rejected =="
printf 'SST_SUBSTRATE=slurm\nSST_CLUSTER_NAME=some-cluster\nSST_RESULTS_DIR=%s\nSST_TEST_DIRS=%s:1\n' \
    "$WORK/r" "$WORK/d" > "$WORK/meta.env"
"$PY" "$S/gen_env_sh.py" --config "$WORK/meta.env" --out "$WORK/meta.sh" >/dev/null 2>&1 \
    && ok "SST_CLUSTER_NAME is accepted as metadata" || bad "SST_CLUSTER_NAME was rejected"
grep -q "CLUSTER_NAME" "$WORK/meta.sh" && bad "metadata leaked into env.sh" \
    || ok "...and is not emitted into env.sh"
printf 'SST_SUBSTRATE=slurm\nSST_NONSENSE=1\n' > "$WORK/bad.env"
"$PY" "$S/gen_env_sh.py" --config "$WORK/bad.env" --out /dev/null >/dev/null 2>&1 \
    && bad "an unknown key was accepted" || ok "a genuinely unknown key is still fatal"

echo "== every step emits the 'success' field ACV's schema requires =="
# ACV validates step stdout against an auto-detected schema that requires `success`. A step can
# do all its work and still fail on the way out.
"$PY" "$S/gen_env_sh.py" --config "$HERE/testdata/slurm-minimal.env" --out /dev/null 2>/dev/null \
  | "$PY" -c 'import json,sys; d=json.load(sys.stdin); need={"success","platform","cluster_name"}; sys.exit(0 if need<=set(d) else 1)' \
  && ok "gen_env_sh emits success/platform/cluster_name" || bad "gen_env_sh output misses a schema-required key"
# ...and every sweeplib-based step gets them from emit().
"$PY" -c '
import sys, os, json, io, contextlib
sys.path.insert(0, os.environ["S"])
sys.path.insert(0, os.path.dirname(os.environ["S"]))
import common.sweeplib as sweeplib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    sweeplib.emit({"success": True})
d = json.loads(buf.getvalue())
need = {"success", "platform", "cluster_name"}
sys.exit(0 if need <= set(d) else 1)' \
  && ok "sweeplib.emit injects the schema-required fields" || bad "sweeplib.emit output misses a required key"

echo "== metrics: headline numbers and threshold checks =="
# A stand-in for what extract-elbencho.py --to-csv writes. Header is the ElbenchoMetrics
# dataclass (extract-elbencho.py:157); only the columns metrics.py reads are populated.
MCSV="$WORK/elbencho-metrics.csv"
{
  printf 'nodes,io_size,threads,io_depth,operation,iops,throughput_mib_s,throughput_gbps\n'
  # 2 nodes x 4 threads WRITE: 8192 MiB/s aggregate -> 8.59 GB/s agg, 4.295 GB/s per node
  printf '2,1M,4,1,WRITE,8000,8192,68.7\n'
  printf '1,1M,4,1,WRITE,4000,4096,34.3\n'
  printf '2,1M,4,1,READ,16000,16384,137.4\n'
} > "$MCSV"
mk_sweep "$WORK/m_ok" SUCCESS SUCCESS

"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_ok" --from-csv "$MCSV" > "$WORK/m1.json" 2>/dev/null
check "parses the extractor CSV" "$(jqf "$WORK/m1.json" samples)" "3"
w=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['headline']['write']['best_agg_gb_s'])" "$WORK/m1.json")
check "best WRITE aggregate is GB/s from MiB/s (8192 MiB/s = 8.59 GB/s)" "$w" "8.59"
pn=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['headline']['write']['best_per_node_gb_s'])" "$WORK/m1.json")
check "per-node divides by the node count" "$pn" "4.295"
nn=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['headline']['write']['best_agg_at']['nodes'])" "$WORK/m1.json")
check "reports the shape that won" "$nn" "2"
# Units trap: throughput_gbps in the CSV is Gb/s (bits). If we had trusted it, this would be 68.7.
"$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));assert d['headline']['write']['best_agg_gb_s'] < 10" \
    "$WORK/m1.json" && ok "ignores the CSV's Gb/s column (bits) in favour of MiB/s" \
    || bad "used the bits column"

# No thresholds configured must be visible, not a silent pass.
check "measuring without asserting is flagged" "$(jqf "$WORK/m1.json" asserted)" "False"
check "...and still exits success" "$(jqf "$WORK/m1.json" success)" "True"

"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_ok" --from-csv "$MCSV" --min-write-gb-s 5 \
    > "$WORK/m2.json" 2>/dev/null
check "a met floor passes"     "$(jqf "$WORK/m2.json" success)"  "True"
check "...and records asserted" "$(jqf "$WORK/m2.json" asserted)" "True"
"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_ok" --from-csv "$MCSV" --min-write-gb-s 50 \
    > "$WORK/m3.json" 2>/dev/null
check "an unmet floor fails" "$(jqf "$WORK/m3.json" success)" "False"
"$PY" -c "import json,sys;assert 'write_agg_gb_s' in json.load(open(sys.argv[1]))['error']" "$WORK/m3.json" \
    && ok "the error names which check failed" || bad "error does not name the check"

# A floor for an operation the sweep never ran must fail, not pass vacuously.
printf 'nodes,io_size,threads,io_depth,operation,iops,throughput_mib_s,throughput_gbps\n2,1M,4,1,WRITE,8000,8192,68.7\n' \
    > "$WORK/wonly.csv"
"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_ok" --from-csv "$WORK/wonly.csv" --min-read-gb-s 1 \
    > "$WORK/m4.json" 2>/dev/null
check "a floor with no matching measurement fails" "$(jqf "$WORK/m4.json" success)" "False"

echo "== metrics: the extractor is actually invoked (not just --from-csv) =="
# Every check above passes --from-csv, which bypasses extract() entirely. That is why the
# suite stayed green when upstream 0af71d8 deleted utils/extract-elbencho.sh out from under
# us. These drive the real resolution and invocation path.

# 1. A fake checkout whose extractor is an executable .py, as upstream now ships it.
FAKE="$WORK/fakesst"; mkdir -p "$FAKE/utils"
cat > "$FAKE/utils/extract-elbencho.py" <<'EXTR'
#!/usr/bin/env python3
import sys, os
# Record how we were invoked so the test can prove argv[0] was not "bash".
d = sys.argv[-1]
open(os.path.join(d, "invoked-as.txt"), "w").write(sys.argv[0] + "\n")
open(os.path.join(d, "elbencho-metrics.csv"), "w").write(
    "nodes,io_size,threads,io_depth,operation,iops,throughput_mib_s,throughput_gbps\n"
    "2,1M,4,1,WRITE,8000,8192,68.7\n")
EXTR
chmod +x "$FAKE/utils/extract-elbencho.py"

mk_sweep "$WORK/m_ex" SUCCESS SUCCESS
"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_ex" --scale-test-base "$FAKE" \
    > "$WORK/mex.json" 2>"$WORK/mex.err"
check "extract() runs the upstream extractor and reads its CSV" "$(jqf "$WORK/mex.json" samples)" "1"
if [[ -f "$WORK/m_ex/invoked-as.txt" ]]; then
    ok "the extractor really ran"
    grep -q '\.py$' "$WORK/m_ex/invoked-as.txt" \
        && ok "...invoked as the .py itself, letting its shebang bootstrap the venv" \
        || bad "extractor invoked as $(cat "$WORK/m_ex/invoked-as.txt"), not the .py"
else
    bad "extract() never invoked the extractor: $(head -3 "$WORK/mex.err")"
fi

# 2. A checkout with only the OLD .sh wrapper must now fail, not silently succeed.
OLDSST="$WORK/oldsst"; mkdir -p "$OLDSST/utils"
printf '#!/bin/bash\nexit 0\n' > "$OLDSST/utils/extract-elbencho.sh"
chmod +x "$OLDSST/utils/extract-elbencho.sh"
mk_sweep "$WORK/m_old" SUCCESS SUCCESS
"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_old" --scale-test-base "$OLDSST" \
    > "$WORK/mold.json" 2>/dev/null
check "a checkout with only the removed .sh wrapper fails" "$(jqf "$WORK/mold.json" success)" "False"

# 3. Drift: the extractor metrics.py names must exist in the real checkout.
if [[ -d "$SST_CHECKOUT/utils" ]]; then
    EXP=$("$PY" -c "import sys;sys.path.insert(0,'$S');import metrics;print(metrics.EXTRACTOR)")
    if [[ -f "$SST_CHECKOUT/$EXP" ]]; then
        ok "metrics.py EXTRACTOR ($EXP) exists upstream"
        [[ -x "$SST_CHECKOUT/$EXP" ]] \
            && ok "...and is executable, so invoking it directly works" \
            || bad "$EXP is not executable; direct invocation will fail"
    else
        bad "metrics.py EXTRACTOR ($EXP) does not exist upstream -- extraction will fail at runtime"
    fi
else
    printf '  SKIP extractor drift check: no checkout at %s\n' "$SST_CHECKOUT"
fi

printf 'nodes,io_size,threads,io_depth,operation,iops,throughput_mib_s,throughput_gbps\n' > "$WORK/empty.csv"
"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_ok" --from-csv "$WORK/empty.csv" > "$WORK/m5.json" 2>/dev/null
check "an empty extractor CSV fails loudly" "$(jqf "$WORK/m5.json" success)" "False"

# The derived roll-up lands in the sweep dir; collect must not count it as a result.
cp "$MCSV" "$WORK/c_ok/elbencho-metrics.csv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/c_ok" --substrate slurm > "$WORK/c5.json" 2>/dev/null
n2=$("$PY" -c "import json,sys;print(len(json.load(open(sys.argv[1]))['results_csv']))" "$WORK/c5.json")
check "collect ignores the derived elbencho-metrics.csv" "$n2" "1"

echo "== provenance: a run is never labelled with the wrong substrate =="
# emit() used to default `platform` to "slurm". ACV's `env:` is PER-STEP, so the metrics
# step inherits no SST_SUBSTRATE and every kubectl run was published as Slurm. A wrong
# default is worse than an absent one: it is silently believable.
printf 'nodes,io_size,threads,io_depth,operation,iops,throughput_mib_s,throughput_gbps\n2,1M,4,1,WRITE,8000,8192,68.7\n' \
    > "$WORK/prov.csv"
mk_sweep "$WORK/m_prov" SUCCESS SUCCESS
( unset SST_SUBSTRATE
  "$PY" "$S/metrics.py" --sweep-dir "$WORK/m_prov" --from-csv "$WORK/prov.csv" \
      --min-write-gb-s 0.01 > "$WORK/prov_none.json" 2>/dev/null )
pl=$(jqf "$WORK/prov_none.json" substrate)
check "no substrate given reports 'unknown', not a guess" "$pl" "unknown"
check "platform is the validation domain, like every storage provider" "$(jqf "$WORK/prov_none.json" platform)" "storage"
[[ "$pl" == "slurm" ]] && bad "regressed: a kubectl sweep would publish as Slurm" || true

"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_prov" --from-csv "$WORK/prov.csv" \
    --substrate kubectl --min-write-gb-s 0.01 > "$WORK/prov_k.json" 2>/dev/null
check "--substrate kubectl is reported as kubectl" "$(jqf "$WORK/prov_k.json" substrate)" "kubectl"
"$PY" "$S/metrics.py" --sweep-dir "$WORK/m_prov" --from-csv "$WORK/prov.csv" \
    --substrate slurm --min-write-gb-s 0.01 > "$WORK/prov_s.json" 2>/dev/null
check "--substrate slurm is reported as slurm" "$(jqf "$WORK/prov_s.json" substrate)" "slurm"

echo "== resume_sweep: only when there is work, and refusal is not failure =="
# A resume stub: appends the refusal line, or writes a NEW coordinator id, per STUB_RESUME_MODE.
cat > "$BASE/storage-tests/fs/nv-elbencho-sweep.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "--resume" ]] || { echo "stub: expected --resume, got ${1:-}" >&2; exit 2; }
DIR="$2"
case "${STUB_RESUME_MODE:-dispatch}" in
  refuse) echo "Error: coordinator job 4242 is still active; refusing to reset or redispatch its RUNNING execution(s)" >&2; exit 1 ;;
  silent) exit 1 ;;
  *) printf '%s\n' "${STUB_NEW_JOBID:-5151}" > "$DIR/executions/.coordinator.jobid" ;;
esac
STUB
chmod +x "$BASE/storage-tests/fs/nv-elbencho-sweep.sh"

mk_sweep "$WORK/r_done" SUCCESS SUCCESS
printf 'x\n' > "$WORK/r_done/env_used.sh"
"$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/r_done" --substrate slurm \
    --scale-test-base "$BASE" --bash "$FAKEBASH" > "$WORK/r1.json" 2>/dev/null
check "nothing to resume is refused by default" "$(jqf "$WORK/r1.json" success)" "False"
check "...and reported as a noop"               "$(jqf "$WORK/r1.json" outcome)" "noop"
"$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/r_done" --substrate slurm --allow-noop \
    --scale-test-base "$BASE" --bash "$FAKEBASH" > "$WORK/r2.json" 2>/dev/null
check "--allow-noop makes it a pass" "$(jqf "$WORK/r2.json" success)" "True"

mk_sweep "$WORK/r_part" SUCCESS PENDING
printf 'x\n' > "$WORK/r_part/env_used.sh"
printf '4242\n' > "$WORK/r_part/executions/.coordinator.jobid"
STUB_RESUME_MODE=dispatch "$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/r_part" --substrate slurm \
    --scale-test-base "$BASE" --bash "$FAKEBASH" --jobid-timeout 20 > "$WORK/r3.json" 2>/dev/null
check "a real resume dispatches"            "$(jqf "$WORK/r3.json" outcome)" "dispatched"
check "...and waits for a NEW jobid (not 4242)" "$(jqf "$WORK/r3.json" coordinator_jobid)" "5151"
check "...counting only the outstanding work"   "$(jqf "$WORK/r3.json" resumed)" "1"

# The trap this exists for: exit 1 means two opposite things.
mk_sweep "$WORK/r_live" SUCCESS PENDING
printf 'x\n' > "$WORK/r_live/env_used.sh"
printf '4242\n' > "$WORK/r_live/executions/.coordinator.jobid"
STUB_RESUME_MODE=refuse "$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/r_live" --substrate slurm \
    --scale-test-base "$BASE" --bash "$FAKEBASH" --jobid-timeout 20 > "$WORK/r4.json" 2>/dev/null
check "a live coordinator reads as refused, not failed" "$(jqf "$WORK/r4.json" outcome)" "refused"
"$PY" -c "import json,sys;assert 'still running' in json.load(open(sys.argv[1]))['error']" "$WORK/r4.json" \
    && ok "the refusal says the sweep is running, not broken" || bad "refusal wording is misleading"

STUB_RESUME_MODE=silent "$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/r_live" --substrate slurm \
    --scale-test-base "$BASE" --bash "$FAKEBASH" --jobid-timeout 6 > "$WORK/r5.json" 2>/dev/null
check "a silent exit-1 is a timeout, distinct from a refusal" "$(jqf "$WORK/r5.json" outcome)" "timeout"

mk_sweep "$WORK/r_noenv" SUCCESS PENDING
"$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/r_noenv" --substrate slurm \
    --scale-test-base "$BASE" --bash "$FAKEBASH" > "$WORK/r6.json" 2>/dev/null
check "a missing env_used.sh is caught before dispatch" "$(jqf "$WORK/r6.json" success)" "False"

echo
echo "== delete_data: path guards refuse before anything reaches a compute node =="
ROOT="$WORK/testroot"; mkdir -p "$ROOT/sweepdata"
dr() { "$PY" "$S/delete_data.py" --path "$1" --test-root "$ROOT" --substrate slurm \
        --scale-test-base "$BASE" --dry-run > "$WORK/d.json" 2>/dev/null; jqf "$WORK/d.json" success; }
check "a strict subdir is allowed"        "$(dr "$ROOT/sweepdata")" "True"
check "the test root itself is refused"   "$(dr "$ROOT")"           "False"
check "a trailing slash on the root too"  "$(dr "$ROOT/")"          "False"
check "a path outside the root is refused" "$(dr "$WORK/elsewhere")" "False"
check "/ is refused"                       "$(dr "/")"               "False"
check "a relative path is refused"         "$(dr "sweepdata")"       "False"
check "'..' is refused"                    "$(dr "$ROOT/../etc")"    "False"
# Re-run the allowed case so this asserts on a dry run that PASSED -- the refusals above
# exit via fail(), which has no 'deleted' key at all.
dr "$ROOT/sweepdata" > /dev/null
"$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));assert d['dry_run'] is True and d['deleted'] is False" \
    "$WORK/d.json" && ok "an allowed dry run reports dry_run=true, deleted=false" \
    || bad "dry run claimed a deletion"

# A symlink inside the root must not smuggle an outside target past the check.
ln -s /etc "$ROOT/escape" 2>/dev/null || true
check "a symlink under the root is judged by its path, not its target" "$(dr "$ROOT/escape")" "True"

echo "== submit_sweep: idempotent re-attach on config_hash =="
# Re-running submit after a dropped connection must NOT start a second coordinator against
# the same TEST_DIRS. The first submit (above) left sidecars in $SWEEP.
cat > "$BASE/storage-tests/fs/nv-elbencho-sweep.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
ENV_SH="$(dirname "${BASH_SOURCE[0]}")/../../env.sh"
RESULTS_DIR=$(sed -n 's/^export RESULTS_DIR="\(.*\)"$/\1/p' "$ENV_SH" | tail -1)
DS=$(date -u +"%Y%m%dZ%H%M%S")
OUT="${RESULTS_DIR}/elbencho-${DS}"
mkdir -p "$OUT/executions"
printf 'PENDING\n' > "$OUT/executions/0001.status"
printf '%s\n' "${STUB_NEW_JOBID:-7777}" > "$OUT/executions/.coordinator.jobid"
sleep 30
STUB
chmod +x "$BASE/storage-tests/fs/nv-elbencho-sweep.sh"
[[ -f "$SWEEP/executions/.acv-config-hash" ]] && ok "submit recorded a config_hash sidecar" \
    || bad "no .acv-config-hash sidecar"
[[ -f "$SWEEP/executions/.acv-launcher.pid" ]] && ok "submit recorded a launcher-pid sidecar" \
    || bad "no .acv-launcher.pid sidecar"

"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash deadbeef \
    --substrate slurm --results-dir "$RES" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    --dir-timeout 20 --jobid-timeout 20 > "$WORK/s_again.json" 2>/dev/null
check "same config_hash with work outstanding re-attaches" "$(jqf "$WORK/s_again.json" reattached)" "True"
check "...to the SAME sweep dir"                           "$(jqf "$WORK/s_again.json" sweep_dir)" "$SWEEP"
check "...without minting a new coordinator"               "$(jqf "$WORK/s_again.json" coordinator_jobid)" "4242"

"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash deadbeef --no-reattach \
    --substrate slurm --results-dir "$RES" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    --dir-timeout 20 --jobid-timeout 20 > "$WORK/s_force.json" 2>/dev/null
check "--no-reattach forces a new sweep" "$(jqf "$WORK/s_force.json" reattached)" "False"

# A finished sweep is not re-attachable: asking again means asking for a NEW run.
printf 'SUCCESS\n' > "$SWEEP/executions/0001.status"
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash cabbage \
    --substrate slurm --results-dir "$RES" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    --dir-timeout 20 --jobid-timeout 20 > "$WORK/s_new.json" 2>/dev/null
check "a different config_hash never re-attaches" "$(jqf "$WORK/s_new.json" reattached)" "False"

echo
echo "== poll_sweep: SSH liveness, the substrate with no scheduler to ask =="
mk_sweep "$WORK/ssh_live" SUCCESS PENDING
sleep 120 & LIVE_PID=$!
printf '%s\n' "$LIVE_PID" > "$WORK/ssh_live/executions/.acv-launcher.pid"
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/ssh_live" --substrate ssh > "$WORK/x1.json" 2>/dev/null
check "a live dispatcher reads as running" "$(jqf "$WORK/x1.json" state)" "running"
check "...and says so explicitly"          "$(jqf "$WORK/x1.json" dispatcher_alive)" "True"
kill "$LIVE_PID" 2>/dev/null; wait "$LIVE_PID" 2>/dev/null

mk_sweep "$WORK/ssh_dead" SUCCESS PENDING
printf '%s\n' "$LIVE_PID" > "$WORK/ssh_dead/executions/.acv-launcher.pid"
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/ssh_dead" --substrate ssh > "$WORK/x2.json" 2>/dev/null
check "a dead dispatcher is detected as abandoned" "$(jqf "$WORK/x2.json" state)" "abandoned"
"$PY" -c "import json,sys;assert 'SSH dispatcher' in json.load(open(sys.argv[1]))['error']" "$WORK/x2.json" \
    && ok "the error names the SSH dispatcher, not a Slurm job" || bad "error text is Slurm-shaped"

# No sidecar at all (a sweep from before this existed, or polled from another host).
mk_sweep "$WORK/ssh_unknown" SUCCESS PENDING
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/ssh_unknown" --substrate ssh > "$WORK/x3.json" 2>/dev/null
check "an unknown PID is NOT called abandoned" "$(jqf "$WORK/x3.json" state)" "running"
check "...and liveness is reported as unknown" "$(jqf "$WORK/x3.json" dispatcher_alive)" "None"

echo "== kubectl paths refuse to pretend on a checkout without the implementation =="
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash beef --substrate kubectl \
    --results-dir "$WORK/results3" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    > "$WORK/k1.json" 2>/dev/null
check "kubectl submit fails loudly on a stale checkout" "$(jqf "$WORK/k1.json" success)" "False"
"$PY" -c "import json,sys;assert 'stale checkout' in json.load(open(sys.argv[1]))['error']" "$WORK/k1.json" \
    && ok "error tells the user to pull current main" || bad "error text lacks the stale-checkout hint"

# The regression this guards: upstream carries EXECUTION_SUBSTRATE in the substrate
# SELECTOR (env_base.sh) independently of any kubectl execution code. Detecting the branch by
# that string meant a checkout with the selector but no kubectl implementation looked ready, and
# the Slurm-shaped path would run against it. Detect the implementation directory instead.
printf 'EXECUTION_SUBSTRATE is referenced here but there is no kubectl implementation\n' \
    >> "$BASE/storage-tests/fs/nv-elbencho-sweep.sh"
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash sel --substrate kubectl \
    --results-dir "$WORK/results_sel" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    > "$WORK/k2.json" 2>/dev/null
check "a checkout with the selector but no kubectl code is still refused" \
    "$(jqf "$WORK/k2.json" success)" "False"
"$PY" -c "import json,sys;assert '_nv-elbencho-kubectl-functions.sh' in json.load(open(sys.argv[1]))['error']" \
    "$WORK/k2.json" && ok "...and the error names the missing implementation file" \
    || bad "error does not name the kubectl implementation file"

# ...and it accepts a checkout that does have the implementation.
mkdir -p "$BASE/storage-tests/fs/kubectl"
printf '# stub\n' > "$BASE/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash impl --substrate kubectl \
    --results-dir "$WORK/results_impl" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    > "$WORK/k3.json" 2>/dev/null
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1])).get('error') or ''; sys.exit(0 if 'stale checkout' not in e else 1)" \
    "$WORK/k3.json" && ok "a checkout WITH the implementation gets past the guard" \
    || bad "guard still refuses a checkout that has the kubectl implementation"
rm -rf "$BASE/storage-tests/fs/kubectl"

echo "== poll_sweep: a finished kubectl sweep is not waited on =="
# On kubectl the per-cell <NNNN>.status sentinels stay PENDING locally until --collect
# publishes them, so tally() -- and therefore assess()'s `done` -- cannot see that the
# cluster already finished. kubectl_status() used to attach the driver's output without
# reading it, so `--wait` blocked on a sweep that was already SUCCESS
# (STATE=SUCCESS / NEXT_ACTION=COLLECT) until its full deadline.
mk_kstub() {  # $1=dir  $2=STATE  $3=NEXT_ACTION
    mkdir -p "$1/storage-tests/fs"
    cat > "$1/storage-tests/fs/nv-elbencho-sweep.sh" <<KSTUB
#!/bin/bash
printf 'ATTEMPT=deadbeef\nSTATE=$2\nEXECUTION_SCOPE=CURRENT_ATTEMPT\n'
printf 'PROGRESS_SOURCE=PVC\nEXECUTIONS_TOTAL=1\nEXECUTIONS_PENDING=0\n'
printf 'EXECUTIONS_RUNNING=0\nEXECUTIONS_SUCCEEDED=1\nEXECUTIONS_FAILED=0\n'
printf 'RESULT_COLLECTION=PENDING\nNEXT_ACTION=$3\n'
KSTUB
    chmod +x "$1/storage-tests/fs/nv-elbencho-sweep.sh"
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
}

# The sweep dir still reads PENDING locally -- exactly the live shape.
mk_sweep "$WORK/kp_done" PENDING
KB="$WORK/kbase_ok"; mk_kstub "$KB" SUCCESS COLLECT
start=$(date +%s)
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/kp_done" --substrate kubectl \
    --scale-test-base "$KB" --wait 60 --interval 15 > "$WORK/kp1.json" 2>/dev/null
elapsed=$(( $(date +%s) - start ))
check "a SUCCESS sweep is reported done despite PENDING sentinels" "$(jqf "$WORK/kp1.json" done)" "True"
check "...and as a success" "$(jqf "$WORK/kp1.json" success)" "True"
check "...with state success" "$(jqf "$WORK/kp1.json" state)" "success"
[[ "$elapsed" -lt 15 ]] && ok "...and returns immediately (${elapsed}s), not after --wait" \
    || bad "poll blocked ${elapsed}s on a finished sweep"
# This assertion used to read `counts_authoritative == False`, because authority was
# derived from `NEXT_ACTION != COLLECT`. That conflated two different things: "the results
# have not been collected yet" and "the counts cannot be trusted". The driver read these
# counts from the PVC and says so (PROGRESS_SOURCE=PVC); NEXT_ACTION=COLLECT describes what
# to do next, not where the numbers came from. Asserting the old conflation kept the
# published truth out of the payload on every terminal-but-uncollected sweep.
check "an uncollected sweep's cluster counts are still trustworthy" \
    "$(jqf "$WORK/kp1.json" counts_authoritative)" "True"
check "...and are the ones reported" "$(jqf "$WORK/kp1.json" succeeded)" "1"
check "...not the local sentinels, which still read PENDING" \
    "$(jqf "$WORK/kp1.json" local_succeeded)" "0"

# A FAILED driver state must fail the step, not pass because tally saw no failures.
mk_sweep "$WORK/kp_fail" PENDING
KBF="$WORK/kbase_fail"; mk_kstub "$KBF" FAILED NONE
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/kp_fail" --substrate kubectl \
    --scale-test-base "$KBF" --wait 5 --interval 5 > "$WORK/kp2.json" 2>/dev/null
check "a FAILED sweep is reported failed" "$(jqf "$WORK/kp2.json" success)" "False"
check "...and done" "$(jqf "$WORK/kp2.json" done)" "True"

# A NON-terminal driver state must NOT override assess(); the sweep really is running.
mk_sweep "$WORK/kp_run" PENDING
KBR="$WORK/kbase_run"; mk_kstub "$KBR" RUNNING WAIT
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/kp_run" --substrate kubectl \
    --scale-test-base "$KBR" --wait 1 --interval 1 --allow-incomplete \
    > "$WORK/kp3.json" 2>/dev/null
check "a RUNNING sweep is still reported running" "$(jqf "$WORK/kp3.json" state)" "running"
check "...and not done" "$(jqf "$WORK/kp3.json" done)" "False"

echo "== poll_sweep refuses to degrade silently on kubectl =="
# The config omitted --scale-test-base for poll_sweep (copied from the slurm block, where
# sacct answers instead). The kubectl branch was then skipped, `done` fell back to tally(),
# and --wait blocked for its full 3600s on a sweep that had already succeeded. The help
# text claimed the flag was required; nothing enforced it.
mk_sweep "$WORK/kp_noebase" PENDING
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/kp_noebase" --substrate kubectl \
    --wait 5 --interval 5 > "$WORK/kpnb.json" 2>/dev/null
check "kubectl without --scale-test-base is refused" "$(jqf "$WORK/kpnb.json" success)" "False"
"$PY" -c "import json,sys;assert 'scale-test-base' in json.load(open(sys.argv[1]))['error']" \
    "$WORK/kpnb.json" && ok "...and the error names the missing flag" \
    || bad "refusal does not name --scale-test-base"
# It must refuse FAST, not after --wait.
start=$(date +%s)
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/kp_noebase" --substrate kubectl \
    --wait 30 --interval 5 > /dev/null 2>&1
[[ $(( $(date +%s) - start )) -lt 10 ]] && ok "...immediately, not after the full --wait" \
    || bad "refusal took the whole --wait"
# slurm must NOT be affected: it has its own liveness source.
mk_sweep "$WORK/kp_slurm" PENDING
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/kp_slurm" --substrate slurm \
    --wait 1 --interval 1 --allow-incomplete > "$WORK/kps.json" 2>/dev/null
check "slurm without --scale-test-base still works" "$(jqf "$WORK/kps.json" success)" "True"

echo "== losing cluster access is reported as blindness, never as progress =="
# The detach-and-reattach workflow's whole point is surviving a lost credential. Both
# steps used to answer a blind query with a confident, wrong story.
mk_blindstub() {  # $1=dir -- a driver that cannot reach the cluster
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    cat > "$1/storage-tests/fs/nv-elbencho-sweep.sh" <<'BSTUB'
#!/bin/bash
printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=API_UNAVAILABLE
'
printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_REMOTE_STATE=unknown
'
exit 1
BSTUB
    chmod +x "$1/storage-tests/fs/nv-elbencho-sweep.sh"
}
BB="$WORK/blindbase"; mk_blindstub "$BB"

# poll: must say "unknown", not "running", and must not advise --allow-incomplete.
mk_sweep "$WORK/blind_poll" PENDING
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/blind_poll" --substrate kubectl \
    --scale-test-base "$BB" --wait 1 --interval 1 > "$WORK/bp.json" 2>/dev/null
check "a blind poll reports state unknown" "$(jqf "$WORK/bp.json" state)" "unknown"
check "...not success" "$(jqf "$WORK/bp.json" success)" "False"
check "...and not done" "$(jqf "$WORK/bp.json" done)" "False"
check "...counts are flagged non-authoritative" "$(jqf "$WORK/bp.json" counts_authoritative)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'access' in e" "$WORK/bp.json" \
    && ok "...and the error names lost access" || bad "blind poll does not mention access"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'allow-incomplete' not in e" \
    "$WORK/bp.json" && ok "...without advising --allow-incomplete, which would mask it" \
    || bad "blind poll still suggests --allow-incomplete"
# The sweep is detached: the error must say the sweep is unaffected, not imply it died.
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'keeps running' in e" \
    "$WORK/bp.json" && ok "...and says the sweep itself is unaffected" \
    || bad "blind poll does not reassure that the sweep survives"

# A blind poll must NOT be rescued by --allow-incomplete either.
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/blind_poll" --substrate kubectl \
    --scale-test-base "$BB" --wait 1 --interval 1 --allow-incomplete \
    > "$WORK/bpa.json" 2>/dev/null
check "--allow-incomplete does not turn blindness into a pass" "$(jqf "$WORK/bpa.json" success)" "False"

# collect: publishing nothing is not success.
mk_sweep "$WORK/blind_coll" PENDING
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/blind_coll" --substrate kubectl \
    --scale-test-base "$BB" > "$WORK/bc.json" 2>/dev/null
check "a blind collect fails instead of passing vacuously" "$(jqf "$WORK/bc.json" success)" "False"
check "...with no results counted" "$(jqf "$WORK/bc.json" results_csv_count)" "0"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'access' in e" "$WORK/bc.json" \
    && ok "...and the error names lost access" || bad "blind collect does not mention access"

# But a sweep with FAILED cells that DID publish must still collect leniently.
mk_sweep "$WORK/part_coll" SUCCESS FAILED
printf 'a,b\n1,2\n' > "$WORK/part_coll/elbencho-1M-c_001-s_001-d_001_20260101Z000000.csv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/part_coll" --substrate ssh \
    > "$WORK/pc.json" 2>/dev/null
check "a partially-failed sweep still publishes its results" \
    "$(jqf "$WORK/pc.json" results_csv_count)" "1"

echo "== cancel: a kubectl lifecycle verb, and it does NOT release the cluster =="
# A stub driver whose --status answers from a file, so a test can drive the attempt
# through SUBMITTED -> CANCEL_REQUESTED -> TERMINAL the way upstream does.
mk_cancelstub() {  # $1=dir  $2=state-file
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    cat > "$1/storage-tests/fs/nv-elbencho-sweep.sh" <<CSTUB
#!/bin/bash
SF="$2"
if [[ "\$1" == "--cancel" ]]; then
    # Upstream: repeating --cancel resumes finalisation (T-08). Advance one step.
    cur=\$(head -1 "\$SF")
    case "\$cur" in
        RUNNING)          printf 'CANCEL_REQUESTED\\n' > "\$SF" ;;
        CANCEL_REQUESTED) printf 'CANCELLED\\n' > "\$SF" ;;
    esac
    exit 0
fi
cur=\$(head -1 "\$SF")
case "\$cur" in
    CANCEL_REQUESTED)
        printf 'STATE=RUNNING\\nSTORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE=CANCEL_REQUESTED\\n'
        printf 'EXECUTIONS_TOTAL=1\\nRESULT_COLLECTION=PENDING\\nNEXT_ACTION=CANCEL\\n' ;;
    CANCELLED)
        printf 'STATE=CANCELLED\\nEXECUTIONS_TOTAL=1\\n'
        printf 'RESULT_COLLECTION=PENDING\\nNEXT_ACTION=COLLECT\\n' ;;
    SUCCESS)
        printf 'STATE=SUCCESS\\nEXECUTIONS_TOTAL=1\\n'
        printf 'RESULT_COLLECTION=COMPLETE\\nNEXT_ACTION=NONE\\n' ;;
    *)  printf 'STATE=RUNNING\\nEXECUTIONS_TOTAL=1\\n'
        printf 'RESULT_COLLECTION=PENDING\\nNEXT_ACTION=WAIT\\n' ;;
esac
CSTUB
    chmod +x "$1/storage-tests/fs/nv-elbencho-sweep.sh"
}

mk_sweep "$WORK/cx" PENDING
# slurm/ssh: --status/--cancel/--collect all route to kubectl_lifecycle_operation
# (nv-elbencho-sweep.sh:466), so cancel is kubectl-only. Refuse with the real reason.
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate slurm > "$WORK/cs.json" 2>/dev/null
check "cancel on slurm is refused" "$(jqf "$WORK/cs.json" success)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'scancel' in e" "$WORK/cs.json" \
    && ok "...and names the substrate-correct alternative" || bad "slurm refusal gives no alternative"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate kubectl > "$WORK/cb.json" 2>/dev/null
check "cancel on kubectl without --scale-test-base is refused" "$(jqf "$WORK/cb.json" success)" "False"

# A running sweep: one --cancel leaves CANCEL_REQUESTED, which must NOT read as running.
CSB="$WORK/cbase"; SFILE="$WORK/cstate.txt"; printf 'RUNNING\n' > "$SFILE"; mk_cancelstub "$CSB" "$SFILE"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate kubectl \
    --scale-test-base "$CSB" --retries 0 --retry-interval 0 > "$WORK/c1.json" 2>/dev/null
check "a half-finished cancel is not reported as success" "$(jqf "$WORK/c1.json" success)" "False"
check "...and reports the CANCEL_REQUESTED attempt state" \
    "$(jqf "$WORK/c1.json" local_attempt_state)" "CANCEL_REQUESTED"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'still in flight' in e" \
    "$WORK/c1.json" && ok "...naming it as resumable, not failed" || bad "CANCEL_REQUESTED not explained"
"$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));assert d['state']!='running'" "$WORK/c1.json" \
    && ok "...and never as ordinary running state (upstream T-09)" || bad "reported ordinary running"

# Retrying finalises it, exactly as upstream says repeated --cancel does (T-08).
printf 'RUNNING\n' > "$SFILE"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate kubectl \
    --scale-test-base "$CSB" --retries 3 --retry-interval 0 --expect-cancelled \
    > "$WORK/c2.json" 2>/dev/null
check "retrying --cancel finalises the cancellation" "$(jqf "$WORK/c2.json" state)" "CANCELLED"
check "...and it took more than one attempt" "$(jqf "$WORK/c2.json" cancel_attempts)" "2"
check "...and --expect-cancelled makes it a pass" "$(jqf "$WORK/c2.json" success)" "True"
# THE POINT: cancel stops the Job but retains collection data; only --collect releases.
check "cancel does NOT report resources as released" \
    "$(jqf "$WORK/c2.json" resources_released)" "False"
"$PY" -c "import json,sys;n=json.load(open(sys.argv[1]))['leak_note'];assert 'collect' in n and 'Lease' in n" \
    "$WORK/c2.json" && ok "...and says the Lease is released by collect, not cancel" \
    || bad "cancel does not explain the retained Lease"

# Nothing to cancel.
printf 'SUCCESS\n' > "$SFILE"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate kubectl \
    --scale-test-base "$CSB" > "$WORK/c3.json" 2>/dev/null
check "cancelling a finished sweep is a noop" "$(jqf "$WORK/c3.json" noop)" "True"
check "...and fails without --allow-noop" "$(jqf "$WORK/c3.json" success)" "False"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate kubectl \
    --scale-test-base "$CSB" --allow-noop > "$WORK/c4.json" 2>/dev/null
check "...and passes with --allow-noop" "$(jqf "$WORK/c4.json" success)" "True"

echo "== cancel emits one shape, and the validations can assert on it =="
# A validation cannot be written against fields that appear on some paths only. The noop
# path used to omit cancel_attempts/cancel_exit_code/local_attempt_state, so a
# FieldExistsCheck failed on exactly the runs where nothing had gone wrong.
CANCEL_FIELDS="sweep_dir substrate state_before state local_attempt_state cancelled noop finalised cancel_attempts cancel_exit_code resources_released release_state_known success"
printf 'SUCCESS\n' > "$SFILE"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate kubectl \
    --scale-test-base "$CSB" --allow-noop > "$WORK/sh_noop.json" 2>/dev/null
printf 'RUNNING\n' > "$SFILE"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate kubectl \
    --scale-test-base "$CSB" --retries 3 --retry-interval 0 --expect-cancelled \
    > "$WORK/sh_canc.json" 2>/dev/null
for f in $CANCEL_FIELDS; do
    miss=""
    for j in sh_noop sh_canc; do
        "$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if sys.argv[2] in d else 1)" \
            "$WORK/$j.json" "$f" || miss="$miss $j"
    done
    [[ -z "$miss" ]] || bad "field '$f' missing from:$miss"
done
ok "every cancel outcome emits the same field set"

# finalised is the assertion the validation rests on: it must be FALSE while a
# cancellation is still in flight, and TRUE once resolved.
printf 'RUNNING\n' > "$SFILE"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/cx" --substrate kubectl \
    --scale-test-base "$CSB" --retries 0 --retry-interval 0 > "$WORK/fin0.json" 2>/dev/null
check "finalised is False while CANCEL_REQUESTED" "$(jqf "$WORK/fin0.json" finalised)" "False"
check "...and True once the cancellation resolves" "$(jqf "$WORK/sh_canc.json" finalised)" "True"
check "...and True when there was nothing to cancel" "$(jqf "$WORK/sh_noop.json" finalised)" "True"

# release_state_known must distinguish "nothing is held" from "I could not tell".
check "release state is known when the driver answered" \
    "$(jqf "$WORK/sh_canc.json" release_state_known)" "True"
"$PY" "$S/cancel_sweep.py" --sweep-dir "$WORK/blind_poll" --substrate kubectl \
    --scale-test-base "$BB" > "$WORK/relblind.json" 2>/dev/null
check "...and the step fails outright when the driver cannot be read" \
    "$(jqf "$WORK/relblind.json" success)" "False"

echo "== collect makes an unreleased cluster visible =="
# collect is continue_on_failure in the config, so a collect that did not release can pass
# the phase and silently leave the PVC Lease held, failing the next run on lease contention.
printf 'CANCELLED\n' > "$SFILE"   # RESULT_COLLECTION=PENDING in the stub
mk_sweep "$WORK/cleak" SUCCESS
printf 'a,b\n1,2\n' > "$WORK/cleak/elbencho-1M-c_001-s_001-d_001_20260101Z000000.csv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/cleak" --substrate kubectl \
    --scale-test-base "$CSB" > "$WORK/cl1.json" 2>/dev/null
check "an unreleased collect is flagged" "$(jqf "$WORK/cl1.json" resources_released)" "False"
"$PY" -c "import json,sys;n=json.load(open(sys.argv[1]))['leak_note'];assert 'contend' in n" \
    "$WORK/cl1.json" && ok "...and warns the next sweep will contend" || bad "leak note is not actionable"
printf 'SUCCESS\n' > "$SFILE"     # RESULT_COLLECTION=COMPLETE
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/cleak" --substrate kubectl \
    --scale-test-base "$CSB" > "$WORK/cl2.json" 2>/dev/null
check "a released collect reports released" "$(jqf "$WORK/cl2.json" resources_released)" "True"
"$PY" -c "import json,sys;assert 'leak_note' not in json.load(open(sys.argv[1]))" "$WORK/cl2.json" \
    && ok "...with no leak note" || bad "clean collect still warns"

echo "== collect follows NEXT_ACTION=COLLECT until cleanup completes, and stops trying =="
# One --collect can publish the results but leave its own cleanup
# pending (RESULT_COLLECTION=CLEANUP_PENDING, NEXT_ACTION=COLLECT) with the PVC Lease held.
# A second --collect completed it. collect must do that itself, a bounded number of times.
mk_retrystub() {  # $1=dir  $2=count-file  $3=collects needed before cleanup completes
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    cat > "$1/storage-tests/fs/nv-elbencho-sweep.sh" <<RSTUB
CF="$2"; NEED="$3"
if [[ "\$1" == "--collect" ]]; then echo collect >> "\$CF"; printf 'STATE=SUCCESS\\n'; exit 0; fi
n=\$(grep -c collect "\$CF" 2>/dev/null || true)
if [[ "\${n:-0}" -ge "\$NEED" ]]; then
    printf 'STATE=SUCCESS\\nEXECUTIONS_TOTAL=1\\nRESULT_COLLECTION=COMPLETE\\nNEXT_ACTION=NONE\\n'
else
    printf 'STATE=SUCCESS\\nEXECUTIONS_TOTAL=1\\nRESULT_COLLECTION=CLEANUP_PENDING\\nNEXT_ACTION=COLLECT\\n'
fi
RSTUB
    chmod +x "$1/storage-tests/fs/nv-elbencho-sweep.sh"
}
mk_sweep "$WORK/rsw" SUCCESS
printf 'a,b\n1,2\n' > "$WORK/rsw/elbencho-1M-c_001-s_001-d_001_20260101Z000000.csv"
: > "$WORK/rcount2"; mk_retrystub "$WORK/rbase2" "$WORK/rcount2" 2
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/rsw" --substrate kubectl \
    --scale-test-base "$WORK/rbase2" > "$WORK/rc1.json" 2>/dev/null
check "a pending cleanup is collected again until it is released" \
    "$(jqf "$WORK/rc1.json" resources_released)" "True"
check "...which took a second collect" "$(jqf "$WORK/rc1.json" collect_attempts)" "2"
: > "$WORK/rcount9"; mk_retrystub "$WORK/rbase9" "$WORK/rcount9" 9
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/rsw" --substrate kubectl \
    --scale-test-base "$WORK/rbase9" --collect-retries 2 > "$WORK/rc2.json" 2>/dev/null
check "the retries are bounded" "$(jqf "$WORK/rc2.json" collect_attempts)" "3"
check "...and a cleanup that never completes is still reported as a leak" \
    "$(jqf "$WORK/rc2.json" resources_released)" "False"

echo "== CANCELLED is terminal, and is not described as an unfinished run =="
# BATCH_STATE_TERMINAL omitted CANCELLED, so poll waited out its full deadline on a sweep
# that had already stopped; and assess()'s tally error survived, advising
# --allow-incomplete, which cannot rescue a cancelled sweep.
printf 'CANCELLED\n' > "$SFILE"
mk_sweep "$WORK/pcan" PENDING
start=$(date +%s)
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/pcan" --substrate kubectl \
    --scale-test-base "$CSB" --wait 45 --interval 15 > "$WORK/pc1.json" 2>/dev/null
check "a CANCELLED sweep is terminal" "$(jqf "$WORK/pc1.json" done)" "True"
check "...reported as cancelled" "$(jqf "$WORK/pc1.json" state)" "cancelled"
check "...and not a success" "$(jqf "$WORK/pc1.json" success)" "False"
[[ $(( $(date +%s) - start )) -lt 15 ]] && ok "...returning at once, not after --wait" \
    || bad "poll waited on a cancelled sweep"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'allow-incomplete' not in e" \
    "$WORK/pc1.json" && ok "...without advising --allow-incomplete, which cannot help" \
    || bad "cancelled sweep still suggests --allow-incomplete"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'CANCELLED' in e" \
    "$WORK/pc1.json" && ok "...and the error names the cancellation" || bad "error does not name CANCELLED"

echo "== the REAL upstream STATE vocabulary is handled, not an invented one =="
# kubectl_emit_status_view prints a *display* value, not the raw outcome
# (_nv-elbencho-kubectl-functions.sh:706-719):
#   PREPARED|SUBMITTED        -> PREPARING
#   RUNNING, running==0,p>0   -> BETWEEN_EXECUTIONS
#   RUNNING, running==0,p==0  -> AWAITING_COMPLETION
#   SUCCESS|FAILED|CANCELLED  -> unchanged
# Testing against invented names proves nothing, so drive every value it can emit. The
# three RUNNING-ish names must read as running -- NOT as terminal (which would collect an
# unfinished sweep) and NOT as unknown (which would cry lost access on a healthy run).
mk_statestub() {  # $1=dir  $2=STATE  $3=NEXT_ACTION
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    cat > "$1/storage-tests/fs/nv-elbencho-sweep.sh" <<VSTUB
#!/bin/bash
printf 'ATTEMPT=deadbeef\\nSTATE=$2\\nEXECUTION_SCOPE=CURRENT_ATTEMPT\\n'
printf 'PROGRESS_SOURCE=PVC\\nEXECUTIONS_TOTAL=1\\nEXECUTIONS_PENDING=1\\n'
printf 'EXECUTIONS_RUNNING=0\\nEXECUTIONS_SUCCEEDED=0\\nEXECUTIONS_FAILED=0\\n'
printf 'RESULT_COLLECTION=PENDING\\nNEXT_ACTION=$3\\n'
VSTUB
    chmod +x "$1/storage-tests/fs/nv-elbencho-sweep.sh"
}
mk_sweep "$WORK/vocab" PENDING
for st in PREPARING BETWEEN_EXECUTIONS AWAITING_COMPLETION; do
    VB="$WORK/vb_$st"; mk_statestub "$VB" "$st" WAIT
    "$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/vocab" --substrate kubectl \
        --scale-test-base "$VB" --wait 1 --interval 1 --allow-incomplete \
        > "$WORK/v_$st.json" 2>/dev/null
    check "$st is not terminal" "$(jqf "$WORK/v_$st.json" done)" "False"
    s_got=$(jqf "$WORK/v_$st.json" state)
    [[ "$s_got" != "unknown" ]] && ok "...and is not mistaken for lost access" \
        || bad "$st read as unknown: a healthy run would report lost access"
done
for st in SUCCESS FAILED CANCELLED; do
    VB="$WORK/vb_$st"; mk_statestub "$VB" "$st" COLLECT
    "$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/vocab" --substrate kubectl \
        --scale-test-base "$VB" --wait 1 --interval 1 > "$WORK/v_$st.json" 2>/dev/null
    check "$st is terminal" "$(jqf "$WORK/v_$st.json" done)" "True"
done
check "only SUCCESS is a passing outcome" \
    "$(jqf "$WORK/v_SUCCESS.json" success)/$(jqf "$WORK/v_FAILED.json" success)/$(jqf "$WORK/v_CANCELLED.json" success)" \
    "True/False/False"

# NEXT_ACTION=RESUME (collection COMPLETE but cells outstanding) must not read as "more
# to wait for": the sweep is terminal and needs an operator decision.
VB="$WORK/vb_resume"; mk_statestub "$VB" FAILED RESUME
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/vocab" --substrate kubectl \
    --scale-test-base "$VB" --wait 1 --interval 1 > "$WORK/v_res.json" 2>/dev/null
check "NEXT_ACTION=RESUME on a terminal state is still done" "$(jqf "$WORK/v_res.json" done)" "True"
# ...and counts are authoritative then, because collection already ran.
check "...with counts treated as authoritative" \
    "$(jqf "$WORK/v_res.json" counts_authoritative)" "True"

echo "== a sealed kubectl batch is judged by ONE authority, not two =="
# A sealed kubectl batch's --status carries both namespaces at once. BATCH_STATE
# normalises to a bare STATE and the delegated kubectl view also prints a bare STATE=,
# so the surviving value depends on upstream print order. If the kubectl branch also ran
# it would re-judge the batch off whichever key won the collision.
BSTUB="$WORK/bothbase"; mkdir -p "$BSTUB/storage-tests/fs/kubectl"
touch "$BSTUB/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
cat > "$BSTUB/storage-tests/fs/nv-elbencho-sweep.sh" <<'BOTH'
#!/bin/bash
# Batch namespace says SEALED+SUCCESS; the delegated kubectl view prints a bare STATE
# LAST, so a naive parse sees STATE=PREPARING and would call a finished batch unfinished.
printf 'BATCH=SEALED
'
printf 'STORAGE_SCALE_TEST_BATCH_STATE=SUCCESS
'
printf 'STORAGE_SCALE_TEST_BATCH_NEXT_ACTION=NONE
'
printf 'STORAGE_SCALE_TEST_KUBECTL_STATE=SUCCESS
'
printf 'ATTEMPT=deadbeef
STATE=PREPARING
NEXT_ACTION=WAIT
'
BOTH
chmod +x "$BSTUB/storage-tests/fs/nv-elbencho-sweep.sh"

mk_sweep "$WORK/sealedb" PENDING
printf 'id	flags
0001	--nodes 1
' > "$WORK/sealedb/batch-manifest.tsv"
printf 'deadbeef
' > "$WORK/sealedb/batch-sealed.sha256"
start=$(date +%s)
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/sealedb" --substrate kubectl \
    --scale-test-base "$BSTUB" --wait 30 --interval 10 > "$WORK/sb.json" 2>/dev/null
check "the batch verdict wins over the colliding bare STATE" "$(jqf "$WORK/sb.json" done)" "True"
check "...and is reported as success" "$(jqf "$WORK/sb.json" state)" "success"
[[ $(( $(date +%s) - start )) -lt 12 ]] && ok "...returning at once, not after --wait" \
    || bad "poll waited on a finished sealed batch"
"$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));assert d['state']!='preparing'" \
    "$WORK/sb.json" && ok "...never adopting the kubectl view's display state" \
    || bad "the kubectl branch overrode the batch verdict"

echo "== prepared batches (upstream PR #7) =="

# --batch-group without --batch is a usage error, not a silently ignored flag.
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash bg --substrate slurm \
    --results-dir "$WORK/rb0" --scale-test-base "$BASE" --nodes 1 --bash "$FAKEBASH" \
    --batch-group "--nodes 2" > "$WORK/bg.json" 2>/dev/null
check "--batch-group without --batch is refused" "$(jqf "$WORK/bg.json" success)" "False"

# A driver that prepares a batch but prints no BATCH_RESULTS line must be a hard error:
# that line is the only machine-readable handle to the batch root.
BATCHBASE="$WORK/batchbase"; mkdir -p "$BATCHBASE/storage-tests/fs"
cat > "$BATCHBASE/storage-tests/fs/nv-elbencho-sweep.sh" <<'DRV'
#!/bin/bash
echo "prepared something, telling you nothing"
exit 0
DRV
chmod +x "$BATCHBASE/storage-tests/fs/nv-elbencho-sweep.sh"
cp "$BASE/env.sh.template" "$BATCHBASE/env.sh.template" 2>/dev/null || :
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash nb --substrate slurm \
    --results-dir "$WORK/rb1" --scale-test-base "$BATCHBASE" --nodes 1 --bash "$FAKEBASH" \
    --batch > "$WORK/nb.json" 2>/dev/null
check "a --batch that prints no BATCH_RESULTS fails loudly" "$(jqf "$WORK/nb.json" success)" "False"
"$PY" -c "import json,sys;assert 'STORAGE_SCALE_TEST_BATCH_RESULTS' in json.load(open(sys.argv[1]))['error']" \
    "$WORK/nb.json" && ok "...and the error names the missing handle" \
    || bad "error does not name STORAGE_SCALE_TEST_BATCH_RESULTS"

# A --start that returns 0 without writing batch-sealed.sha256 must not be reported as
# started: the seal is what freezes the execution set, so no seal means no batch ran.
SEALBASE="$WORK/sealbase"; mkdir -p "$SEALBASE/storage-tests/fs"
cat > "$SEALBASE/storage-tests/fs/nv-elbencho-sweep.sh" <<DRV
#!/bin/bash
if [[ "\$1" == "--batch" ]]; then
    mkdir -p "$WORK/thebatch/executions"
    : > "$WORK/thebatch/batch-manifest.tsv"
    echo "STORAGE_SCALE_TEST_BATCH_RESULTS=$WORK/thebatch"
    exit 0
fi
exit 0   # --start lies: exits clean, seals nothing
DRV
chmod +x "$SEALBASE/storage-tests/fs/nv-elbencho-sweep.sh"
cp "$BASE/env.sh.template" "$SEALBASE/env.sh.template" 2>/dev/null || :
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash sl --substrate kubectl \
    --results-dir "$WORK/rb2" --scale-test-base "$SEALBASE" --nodes 1 --bash "$FAKEBASH" \
    --batch > "$WORK/seal.json" 2>/dev/null
check "a --start that seals nothing is not reported as started" \
    "$(jqf "$WORK/seal.json" success)" "False"
"$PY" -c "import json,sys;assert 'batch-sealed.sha256' in json.load(open(sys.argv[1]))['error']" \
    "$WORK/seal.json" && ok "...and the error names the missing seal" \
    || bad "error does not name the missing seal"

# --no-start leaves the batch DRAFT and says so, rather than pretending it dispatched.
"$PY" "$S/submit_sweep.py" --env-sh "$WORK/overlay.sh" --config-hash ns --substrate kubectl \
    --results-dir "$WORK/rb3" --scale-test-base "$SEALBASE" --nodes 1 --bash "$FAKEBASH" \
    --batch --no-start > "$WORK/ns.json" 2>/dev/null
check "--no-start reports success" "$(jqf "$WORK/ns.json" success)" "True"
check "--no-start leaves the batch unsealed" "$(jqf "$WORK/ns.json" sealed)" "False"

# The two status dialects co-occur on a sealed kubectl batch; collapsing them loses one.
SWEEPLIB_DIR="$S" "$PY" - <<'PYEOF' && ok "parse_batch_status keeps BATCH_ and KUBECTL_ namespaces apart" \
    || bad "parse_batch_status collapsed the two status namespaces"
import os, sys; sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from common.sweeplib import parse_batch_status, batch_is_terminal
d = parse_batch_status(
    "STORAGE_SCALE_TEST_BATCH_STATE=SEALED\n"
    "STORAGE_SCALE_TEST_KUBECTL_STATE=RUNNING\n"
    "NEXT_ACTION=WAIT\n")
assert d["BATCH_STATE"] == "SEALED", d
assert d["KUBECTL_STATE"] == "RUNNING", d
assert not batch_is_terminal(d), d
# All three namespaces must survive together, each readable on its own terms.
both = parse_batch_status(
    "STORAGE_SCALE_TEST_BATCH_STATE=SUCCESS\n"
    "STORAGE_SCALE_TEST_BATCH_NEXT_ACTION=NONE\n"
    "STORAGE_SCALE_TEST_KUBECTL_STATE=SUCCESS\n"
    "STATE=PREPARING\nNEXT_ACTION=WAIT\n")
assert both["BATCH_STATE"] == "SUCCESS", both
assert both["STATE"] == "PREPARING", both
assert both["KUBECTL_STATE"] == "SUCCESS", both
assert batch_is_terminal(both), both
assert batch_is_terminal(parse_batch_status("STATE=SUCCESS\nNEXT_ACTION=NONE\n"))
assert batch_is_terminal(parse_batch_status("STATE=FAILED\nNEXT_ACTION=RESUME\n"))
PYEOF

# Upstream PR #8 made completion mode mandatory when ELBENCHO_FILES_PER_NODE is set with
# the worker-directories layout. Without a way to pass --run-to-completion the provider
# could not express that config at all, so --batch failed before preparing anything.
SWEEPLIB_DIR="$S" "$PY" - <<'PYEOF' && ok "--run-to-completion reaches the driver argv" \
    || bad "--run-to-completion is not forwarded to the driver"
import os, sys, types
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from submit_sweep import build_driver_args
base = dict(mode="default", read_from=None, bio=False, rand=False, single=False, nodes="1")
off = build_driver_args(types.SimpleNamespace(run_to_completion=False, **base))
on = build_driver_args(types.SimpleNamespace(run_to_completion=True, **base))
assert "--run-to-completion" not in off, off
assert "--run-to-completion" in on, on
PYEOF


# =======================================================================================
# WORKLOADS: nv-mdtest-elbencho.sh alongside nv-elbencho-sweep.sh
#
# The two filesystem front doors are the same program carrying different workloads, which
# is exactly what makes mis-routing dangerous: every lifecycle verb exists on BOTH, so
# pointing the wrong driver at a results directory does not fail loudly -- it fails
# obscurely, inside a detached process. These tests assert the routing, not the benchmark.
# =======================================================================================
echo
echo "== workload registry: output directory names cannot cross over =="
SWEEPLIB_DIR="$S" "$PY" - <<'PYEOF' && ok "io/mdtest directory patterns are mutually exclusive" \
    || bad "a workload regex matches the other workload's directories"
import os, sys
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from common.sweeplib import workload, WORKLOAD_IO, WORKLOAD_MDTEST
io, md = workload(WORKLOAD_IO), workload(WORKLOAD_MDTEST)
# The trap: "elbencho-<DS>" is a SUBSTRING of "mdtest-elbencho-<DS>". Without the ^ anchor
# an io poll picks up mdtest sweeps from the same RESULTS_DIR.
assert io.dir_re.match("elbencho-20260908Z230802")
assert not io.dir_re.match("mdtest-elbencho-20260908Z230802"), "io regex is not anchored"
assert md.dir_re.match("mdtest-elbencho-20260908Z230802")
assert not md.dir_re.match("elbencho-20260908Z230802")
for name in ("elbencho-2026", "elbencho-20260908Z2308021", "elbencho-foo"):
    assert not io.dir_re.match(name), name
PYEOF

mkdir -p "$WORK/mixed_results/elbencho-20260101Z000000" \
         "$WORK/mixed_results/mdtest-elbencho-20260101Z000001" \
         "$WORK/mixed_results/not-a-sweep"
SWEEPLIB_DIR="$S" RD="$WORK/mixed_results" "$PY" - <<'PYEOF' \
    && ok "find_sweep_dirs returns only the requested workload from a shared RESULTS_DIR" \
    || bad "find_sweep_dirs mixes workloads"
import os, sys
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from common.sweeplib import find_sweep_dirs, WORKLOAD_IO, WORKLOAD_MDTEST
rd = os.environ["RD"]
io = [os.path.basename(p) for p in find_sweep_dirs(rd, WORKLOAD_IO)]
md = [os.path.basename(p) for p in find_sweep_dirs(rd, WORKLOAD_MDTEST)]
assert io == ["elbencho-20260101Z000000"], io
assert md == ["mdtest-elbencho-20260101Z000001"], md
# The default must stay io, or every existing caller silently changes meaning.
assert find_sweep_dirs(rd) == find_sweep_dirs(rd, WORKLOAD_IO)
PYEOF

SWEEPLIB_DIR="$S" "$PY" - <<'PYEOF' && ok "an unknown workload name is rejected, not defaulted" \
    || bad "workload() silently accepts an unknown name"
import os, sys
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from common.sweeplib import workload
try:
    workload("warp")
except ValueError as exc:
    assert "warp" in str(exc) and "mdtest" in str(exc), exc
else:
    raise AssertionError("workload('warp') did not raise")
PYEOF

echo
echo "== workload registry: every registered path exists upstream (drift guard) =="
# The same class of break as the deleted extract-elbencho.sh wrapper: upstream moves a file
# and the integration keeps pointing at the old path until a live run fails.
SWEEPLIB_DIR="$S" SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' \
    && ok "every workload driver and extractor exists in the checkout" \
    || bad "a registered driver/extractor path is missing upstream"
import os, sys
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from common.sweeplib import WORKLOADS
base = os.environ["SSTC"]
missing = [p for wl in WORKLOADS.values()
           for p in (wl.driver_path(base), wl.extractor_path(base))
           if not os.path.isfile(p)]
assert not missing, missing
PYEOF

# The two front doors upstream REFUSES to run on kubectl. If either ever gains kubectl
# support this test fails, which is the signal to wire it -- the integration deliberately
# ships no code for a substrate the tool rejects, because nothing here could exercise it.
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' \
    && ok "warp/netbench still refuse kubectl upstream (nothing to wire yet)" \
    || bad "warp or netbench now supports kubectl -- the integration should be extended"
import os, sys
base = os.environ["SSTC"]
for rel in ("storage-tests/object/nv-warp-sweep.sh", "storage-tests/network/nv-netbench.sh"):
    assert "kubectl execution is not supported" in open(os.path.join(base, rel)).read(), rel
PYEOF

echo
echo "== submit_sweep: mdtest argv is built from the mdtest flags only =="
SWEEPLIB_DIR="$S" "$PY" - <<'PYEOF' && ok "mdtest argv carries --nodes/--tasks and no io-only flags" \
    || bad "mdtest argv is wrong"
import os, sys, types
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from submit_sweep import build_driver_args
ns = types.SimpleNamespace(workload="mdtest", nodes="1,2", tasks="4",
                           single_dir_file_target=None, env_override=None,
                           mode="default", read_from=None, bio=False, rand=False,
                           single=False, run_to_completion=False)
argv = build_driver_args(ns)
assert argv == ["--nodes", "1,2", "--tasks", "4"], argv
# Upstream rejects anything outside its flag set with "unexpected argument"
# (nv-mdtest-elbencho.sh:89), so none of these may ever appear.
for flag in ("--write-only", "--write-no-read", "--read-from", "--bio", "--rand",
             "--single", "--run-to-completion"):
    assert flag not in argv, flag
ns.single_dir_file_target = 1000
assert build_driver_args(ns)[-2:] == ["--single-dir-file-target", "1000"]
PYEOF

SWEEPLIB_DIR="$S" "$PY" - <<'PYEOF' && ok "--env-override is forwarded on both workloads" \
    || bad "--env-override is not forwarded"
import os, sys, types
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from submit_sweep import build_driver_args
shared = dict(env_override="/tmp/over.sh", nodes="1")
md = build_driver_args(types.SimpleNamespace(
    workload="mdtest", tasks="1", single_dir_file_target=None, mode="default",
    read_from=None, bio=False, rand=False, single=False, run_to_completion=False, **shared))
io = build_driver_args(types.SimpleNamespace(
    workload="io", mode="default", read_from=None, bio=False, rand=False, single=False,
    run_to_completion=False, **shared))
for argv in (md, io):
    assert argv[argv.index("--env-override") + 1] == "/tmp/over.sh", argv
assert "--env-override" not in build_driver_args(types.SimpleNamespace(
    workload="io", mode="default", read_from=None, bio=False, rand=False, single=False,
    run_to_completion=False, env_override=None, nodes="1"))
PYEOF

echo
echo "== submit_sweep: mismatched workload flags are refused before anything starts =="
# These were all silent before: the driver launches DETACHED, so its "unexpected argument"
# lands in a launch log nobody reads and the step reports only "no sweep dir appeared
# within N seconds" -- a timeout that looks like a cluster problem.
MDBASE="$WORK/mdbase"
mkdir -p "$MDBASE/storage-tests/fs" "$MDBASE/lib"
cp "$SST_CHECKOUT/env.sh.template" "$MDBASE/env.sh.template"
echo 'ACV_CHAIN_REACHED=1' > "$MDBASE/lib/env_base.sh"
for drv in nv-elbencho-sweep.sh nv-mdtest-elbencho.sh; do
    printf '#!/bin/bash\nexit 0\n' > "$MDBASE/storage-tests/fs/$drv"
    chmod +x "$MDBASE/storage-tests/fs/$drv"
done

submit_err() {  # prints the step's error field for the given extra args
    "$PY" "$S/submit_sweep.py" --env-sh "$OVERLAY" \
        --config-hash deadbeef --substrate kubectl --results-dir "$WORK/mdres" \
        --scale-test-base "$MDBASE" "$@" > "$WORK/se.json" 2>/dev/null
    "$PY" -c "import json,sys;print(json.load(open(sys.argv[1])).get('error',''))" "$WORK/se.json"
}

e=$(submit_err --workload mdtest --nodes 1)
grep -q -- '--tasks' <<<"$e" && ok "mdtest without --tasks is refused by name" \
    || bad "mdtest without --tasks: $e"

e=$(submit_err --workload io --nodes 1 --tasks 4)
grep -q 'mdtest-only' <<<"$e" && ok "--tasks on an io sweep is refused" || bad "io+tasks: $e"

e=$(submit_err --workload mdtest --nodes 1 --tasks 1 --mode write-only)
grep -q -- '--mode' <<<"$e" && ok "--mode on mdtest is refused by name" || bad "mdtest+mode: $e"

e=$(submit_err --workload mdtest --nodes 1 --tasks 1 --run-to-completion)
grep -q -- '--run-to-completion' <<<"$e" \
    && ok "--run-to-completion on mdtest is refused by name" || bad "mdtest+rtc: $e"

e=$(submit_err --workload mdtest --nodes 1 --tasks 1 --bio --rand)
if grep -q -- '--bio' <<<"$e" && grep -q -- '--rand' <<<"$e"; then
    ok "...and ALL offending flags are named, not just the first"
else
    bad "mdtest+bio/rand named only some: $e"
fi

e=$(submit_err --workload io --nodes 1 --env-override "$WORK/does-not-exist.sh")
grep -q 'no such --env-override' <<<"$e" \
    && ok "a missing --env-override file is refused up front" || bad "env-override: $e"

echo
echo "== submit_sweep: provenance -- which workload, which upstream checkout =="
# This is the ONE test that must point at the real checkout: it captures sst_version by
# running the actual driver's --version. But submit_sweep's install_env_sh
# (submit_sweep.py:100-111) rewrites that checkout's env.sh into a symlink under .acv/ as
# a side effect -- so an ordinary suite run silently reconfigured somebody's working tree,
# and the next hand-run sweep used whatever this test last wrote. Snapshot it, restore it
# below, and assert the restoration.
_ENVSH="$SST_CHECKOUT/env.sh"
if   [[ -L "$_ENVSH" ]]; then _ENVSH_SAVE="link:$(readlink "$_ENVSH")"
elif [[ -f "$_ENVSH" ]]; then _ENVSH_SAVE="file:"; cp "$_ENVSH" "$WORK/envsh.snapshot"
else                          _ENVSH_SAVE="absent:"
fi
"$PY" "$S/submit_sweep.py" --env-sh "$OVERLAY" \
    --config-hash deadbeef --substrate kubectl --results-dir "$WORK/mdres2" \
    --scale-test-base "$SST_CHECKOUT" --workload mdtest --nodes 1 --tasks 1 \
    > "$WORK/prov.json" 2>/dev/null
check "the payload records the workload it ran" "$(jqf "$WORK/prov.json" workload)" "mdtest"
"$PY" - "$WORK/prov.json" <<'PYEOF' && ok "the payload records the storage-scale-test version" \
    || bad "no usable sst_version in the submit payload"
import json, sys
d = json.load(open(sys.argv[1]))
v = d.get("sst_version")
assert v, d
# project_version_option prints one bare line; multi-line means we captured noise.
assert "\n" not in v, repr(v)
PYEOF

# Put the checkout back exactly as we found it, including the artefacts install_env_sh
# leaves behind (the .acv/ copy keyed by our fake hash, and the backup it takes when the
# env.sh it displaces was a regular file).
rm -f "$_ENVSH" "$SST_CHECKOUT"/env.sh.acv-backup-* "$SST_CHECKOUT/.acv/env-deadbeef.sh"
case "${_ENVSH_SAVE%%:*}" in
    link) ln -s "${_ENVSH_SAVE#link:}" "$_ENVSH" ;;
    file) cp "$WORK/envsh.snapshot" "$_ENVSH" ;;
esac
_ENVSH_NOW="absent:"
if   [[ -L "$_ENVSH" ]]; then _ENVSH_NOW="link:$(readlink "$_ENVSH")"
elif [[ -f "$_ENVSH" ]]; then _ENVSH_NOW="file:"
fi
check "the checkout's env.sh is left as the suite found it" "$_ENVSH_NOW" "$_ENVSH_SAVE"
if [[ "$_ENVSH_SAVE" == file:* ]]; then
    cmp -s "$WORK/envsh.snapshot" "$_ENVSH" \
        && ok "the restored env.sh is byte-identical" \
        || bad "env.sh was restored but its contents differ"
fi

echo
echo "== collect_sweep: the derived roll-up is never counted as a result =="
# elbencho's roll-up has a FIXED name; mdtest's INTERPOLATES datestamps. A name comparison
# catches the first and misses the second, so collect would report the analysis output as
# one extra "result" on every mdtest re-run.
CD="$WORK/csvdir"; mkdir -p "$CD/executions"
: > "$CD/elbencho-1M-c_001-s_001-d_001_20260101Z000000.csv"
: > "$CD/mdtest-elbencho-c_001_20260101Z000000.csv"
: > "$CD/elbencho-metrics.csv"
: > "$CD/mdtest-elbencho-metrics-20260101Z000000.csv"
: > "$CD/elbencho-1M-c_001-live.csv"
SWEEPLIB_DIR="$S" CD="$CD" "$PY" - <<'PYEOF' && ok "both roll-ups and live samples are excluded" \
    || bad "find_csvs counts a derived roll-up or a live sample"
import os, sys
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from collect_sweep import find_csvs
names = sorted(os.path.basename(p) for p in find_csvs(os.environ["CD"]))
assert names == ["elbencho-1M-c_001-s_001-d_001_20260101Z000000.csv",
                 "mdtest-elbencho-c_001_20260101Z000000.csv"], names
PYEOF

echo
echo "== metrics: mdtest measures rates and latencies, not bandwidth =="
mkdir -p "$WORK/mdsweep"
MDCSV="$WORK/mdsweep/mdtest-elbencho-metrics-20260101Z000000.csv"
cat > "$MDCSV" <<'CSV'
node_count,thread_count,iteration_count,create_rate_avg,stat_rate_avg,delete_rate_avg,create_lat_p99,stat_lat_p99,delete_lat_p99
1,1,1,100.0,900.0,80.0,500.0,90.0,600.0
1,4,1,400.0,3000.0,350.0,2500.0,120.0,2800.0
2,4,1,250.0,2000.0,200.0,900.0,100.0,950.0
CSV
"$PY" "$S/metrics.py" --workload mdtest --sweep-dir "$WORK/mdsweep" --from-csv "$MDCSV" \
    --min-create-ops-s 1 > "$WORK/md.json" 2>/dev/null
check "mdtest metrics succeed over a roll-up CSV" "$(jqf "$WORK/md.json" success)" "True"
check "...and record the workload" "$(jqf "$WORK/md.json" workload)" "mdtest"
"$PY" - "$WORK/md.json" <<'PYEOF' && ok "headline reports each phase's peak with its own shape" \
    || bad "mdtest headline is wrong"
import json, sys
h = json.load(open(sys.argv[1]))["headline"]
assert set(h) == {"create", "stat", "delete"}, h
assert h["create"]["best_ops_s"] == 400.0, h["create"]
assert h["create"]["best_at"] == {"nodes": 1, "threads": 4}, h["create"]
# The latency reported must come FROM THE BEST-RATE ROW (2500), not be the best latency
# seen anywhere (500). Pairing a peak rate with another row's tail describes a run that
# never happened.
assert h["create"]["lat_p99_us_at_best"] == 2500.0, h["create"]
assert h["stat"]["best_ops_s"] == 3000.0, h["stat"]
assert h["delete"]["best_ops_s"] == 350.0, h["delete"]
PYEOF

"$PY" "$S/metrics.py" --workload mdtest --sweep-dir "$WORK/mdsweep" --from-csv "$MDCSV" \
    --max-create-lat-p99-us 100 > "$WORK/mdlat.json" 2>/dev/null
check "a latency CEILING can fail (bigger is worse)" "$(jqf "$WORK/mdlat.json" success)" "False"
"$PY" "$S/metrics.py" --workload mdtest --sweep-dir "$WORK/mdsweep" --from-csv "$MDCSV" \
    --max-create-lat-p99-us 9999 > "$WORK/mdlat2.json" 2>/dev/null
check "...and passes under the cap" "$(jqf "$WORK/mdlat2.json" success)" "True"
"$PY" "$S/metrics.py" --workload mdtest --sweep-dir "$WORK/mdsweep" --from-csv "$MDCSV" \
    --min-create-ops-s 99999 > "$WORK/mdfloor.json" 2>/dev/null
check "an ops/s FLOOR can fail" "$(jqf "$WORK/mdfloor.json" success)" "False"

echo
echo "== metrics: a threshold the workload cannot measure is an error, not a no-op =="
# The vacuous-pass guard. An ignored --min-read-gb-s on an mdtest run would leave
# asserted:true with every check passing while the number the operator wrote was never
# compared to anything.
"$PY" "$S/metrics.py" --workload mdtest --sweep-dir "$WORK/mdsweep" --from-csv "$MDCSV" \
    --min-read-gb-s 5 > "$WORK/mdx.json" 2>/dev/null
check "io thresholds on an mdtest run fail the step" "$(jqf "$WORK/mdx.json" success)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert '--min-read-gb-s' in e" \
    "$WORK/mdx.json" && ok "...naming the flag that cannot be measured" \
    || bad "the error does not name the offending flag"

mkdir -p "$WORK/iosweep"
IOCSV="$WORK/iosweep/elbencho-metrics.csv"
cat > "$IOCSV" <<'CSV'
operation,nodes,threads,io_size,io_depth,iops,throughput_mib_s
WRITE,1,1,1M,1,10,1000
READ,1,1,1M,1,20,2000
CSV
"$PY" "$S/metrics.py" --workload io --sweep-dir "$WORK/iosweep" --from-csv "$IOCSV" \
    --min-create-ops-s 5 > "$WORK/iox.json" 2>/dev/null
check "mdtest thresholds on an io run fail the step" "$(jqf "$WORK/iox.json" success)" "False"
"$PY" "$S/metrics.py" --workload io --sweep-dir "$WORK/iosweep" --from-csv "$IOCSV" \
    --min-write-gb-s 0.001 > "$WORK/ioy.json" 2>/dev/null
check "the io path still works unchanged" "$(jqf "$WORK/ioy.json" success)" "True"
check "...and defaults to the io workload" "$(jqf "$WORK/ioy.json" workload)" "io"

echo
echo "== metrics: the mdtest roll-up is found by glob, never by a fixed name =="
GB="$WORK/globbase"; mkdir -p "$GB/utils" "$WORK/globsweep"
cat > "$GB/utils/extract-mdtest-elbencho.py" <<'XSTUB'
#!/bin/bash
# Writes the INTERPOLATED name upstream actually produces
# (extract-mdtest-elbencho.py:2410-2415). A fixed-name lookup finds nothing here, and the
# step then reports "the extractor produced no CSV" on a perfectly good run.
d="${2:-.}"
cat > "$d/mdtest-elbencho-metrics-20260101Z000000+20260101Z000100.csv" <<'C'
node_count,thread_count,iteration_count,create_rate_avg,stat_rate_avg,delete_rate_avg,create_lat_p99,stat_lat_p99,delete_lat_p99
1,1,1,42.0,43.0,44.0,10.0,11.0,12.0
C
exit 0
XSTUB
chmod +x "$GB/utils/extract-mdtest-elbencho.py"
"$PY" "$S/metrics.py" --workload mdtest --sweep-dir "$WORK/globsweep" \
    --scale-test-base "$GB" --min-create-ops-s 1 > "$WORK/glob.json" 2>/dev/null
check "the interpolated roll-up name is found" "$(jqf "$WORK/glob.json" success)" "True"
"$PY" - "$WORK/glob.json" <<'PYEOF' && ok "...and it is the file the extractor actually wrote" \
    || bad "metrics read the wrong CSV"
import json, os, sys
d = json.load(open(sys.argv[1]))
assert os.path.basename(d["metrics_csv"]).startswith("mdtest-elbencho-metrics-"), d["metrics_csv"]
assert d["headline"]["create"]["best_ops_s"] == 42.0, d["headline"]
PYEOF

echo
echo "== lifecycle verbs address the mdtest driver, not the io one =="
# Every verb exists on BOTH drivers, so a mis-routed step does not fail loudly -- it asks
# the wrong program about a results directory it never created.
WB="$WORK/whichbase"; mkdir -p "$WB/storage-tests/fs/kubectl"
touch "$WB/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
for drv in nv-elbencho-sweep.sh nv-mdtest-elbencho.sh; do
    cat > "$WB/storage-tests/fs/$drv" <<WSTUB
#!/bin/bash
echo "\$(basename "\$0") \$1" >> "$WORK/which.log"
printf 'STATE=SUCCESS\nNEXT_ACTION=NONE\nRESULT_COLLECTION=COMPLETE\n'
printf 'EXECUTIONS_TOTAL=1\nEXECUTIONS_SUCCEEDED=1\nEXECUTIONS_FAILED=0\n'
exit 0
WSTUB
    chmod +x "$WB/storage-tests/fs/$drv"
done
: > "$WORK/which.log"
mk_sweep "$WORK/mdlife" SUCCESS
"$PY" "$S/poll_sweep.py" --workload mdtest --sweep-dir "$WORK/mdlife" --substrate kubectl \
    --scale-test-base "$WB" --wait 1 --interval 1 > /dev/null 2>&1
"$PY" "$S/collect_sweep.py" --workload mdtest --sweep-dir "$WORK/mdlife" --substrate kubectl \
    --scale-test-base "$WB" > /dev/null 2>&1
"$PY" "$S/cancel_sweep.py" --workload mdtest --sweep-dir "$WORK/mdlife" --substrate kubectl \
    --scale-test-base "$WB" --allow-noop > "$WORK/mdnoop.json" 2>/dev/null
if grep -q 'nv-mdtest-elbencho.sh' "$WORK/which.log" \
   && ! grep -q 'nv-elbencho-sweep.sh' "$WORK/which.log"; then
    ok "poll/collect/cancel all shell nv-mdtest-elbencho.sh under --workload mdtest"
else
    bad "a lifecycle step shelled the wrong driver: $(sort -u "$WORK/which.log" | tr '\n' ' ')"
fi
for verb in --status --collect; do
    grep -q -- "$verb" "$WORK/which.log" && ok "...$verb reached the mdtest driver" \
        || bad "$verb never reached the mdtest driver"
done
# The stub above reports SUCCESS, so cancel correctly SHORT-CIRCUITS without ever issuing
# --cancel: there is nothing to stop. Asserting --cancel in that log would have demanded a
# pointless call on a finished sweep. Prove the verb reaches the mdtest driver with a stub
# that is actually RUNNING, which is the only state where cancelling means anything.
CB="$WORK/cancelbase"; mkdir -p "$CB/storage-tests/fs/kubectl"
touch "$CB/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
cat > "$CB/storage-tests/fs/nv-mdtest-elbencho.sh" <<CSTUB
#!/bin/bash
# RUNNING until a --cancel lands, CANCELLED afterwards -- the real two-phase shape.
MARK="$WORK/cancelled.mark"
echo "\$(basename "\$0") \$1" >> "$WORK/which2.log"
if [[ "\$1" == --cancel ]]; then touch "\$MARK"; exit 0; fi
if [[ -f "\$MARK" ]]; then
    printf 'STATE=CANCELLED\nNEXT_ACTION=COLLECT\nRESULT_COLLECTION=PENDING\n'
else
    printf 'STATE=RUNNING\nNEXT_ACTION=WAIT\nRESULT_COLLECTION=PENDING\n'
fi
exit 0
CSTUB
chmod +x "$CB/storage-tests/fs/nv-mdtest-elbencho.sh"
printf '#!/bin/bash\necho "WRONG-DRIVER \$1" >> "%s"\nexit 0\n' "$WORK/which2.log" \
    > "$CB/storage-tests/fs/nv-elbencho-sweep.sh"
chmod +x "$CB/storage-tests/fs/nv-elbencho-sweep.sh"
: > "$WORK/which2.log"
mk_sweep "$WORK/mdcancel" RUNNING
"$PY" "$S/cancel_sweep.py" --workload mdtest --sweep-dir "$WORK/mdcancel" \
    --substrate kubectl --scale-test-base "$CB" --expect-cancelled --retries 0 \
    > "$WORK/mdcancel.json" 2>/dev/null
grep -q -- 'nv-mdtest-elbencho.sh --cancel' "$WORK/which2.log" \
    && ok "...--cancel reached the mdtest driver on a RUNNING sweep" \
    || bad "--cancel never reached the mdtest driver: $(tr '\n' ' ' < "$WORK/which2.log")"
grep -q 'WRONG-DRIVER' "$WORK/which2.log" \
    && bad "cancel also shelled the io driver" || ok "...and never the io driver"
check "a RUNNING mdtest sweep cancels to CANCELLED" "$(jqf "$WORK/mdcancel.json" state)" "CANCELLED"
check "...reported as finalised" "$(jqf "$WORK/mdcancel.json" finalised)" "True"
check "...with the leak state known" "$(jqf "$WORK/mdcancel.json" release_state_known)" "True"
check "...and resources NOT yet released (collect does that)" \
    "$(jqf "$WORK/mdcancel.json" resources_released)" "False"
check "...recording the workload" "$(jqf "$WORK/mdcancel.json" workload)" "mdtest"
# The noop path must carry the same field set, or a FieldExistsCheck fails on exactly the
# runs where nothing went wrong.
SWEEPLIB_DIR="$S" "$PY" - "$WORK/mdnoop.json" "$WORK/mdcancel.json" <<'PYEOF' \
    && ok "the noop and real-cancel payloads have the same shape" \
    || bad "cancel payload shape differs between the noop and cancel paths"
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
drop = {"note", "error", "leak_note", "status_error", "platform", "cluster_name"}
assert set(a) - drop == set(b) - drop, (sorted(set(a) ^ set(b)))
PYEOF
# And the default must still be io, so every existing config keeps its meaning.
: > "$WORK/which.log"
"$PY" "$S/collect_sweep.py" --sweep-dir "$WORK/mdlife" --substrate kubectl \
    --scale-test-base "$WB" > /dev/null 2>&1
grep -q 'nv-elbencho-sweep.sh' "$WORK/which.log" \
    && ok "omitting --workload still means io (no silent behaviour change)" \
    || bad "the default workload changed"

echo
echo "== metrics: phase coverage is a flat field, not a path into headline =="
"$PY" "$S/metrics.py" --workload mdtest --sweep-dir "$WORK/mdsweep" --from-csv "$MDCSV" \
    --min-create-ops-s 1 > "$WORK/mdph.json" 2>/dev/null
check "all three phases measured" "$(jqf "$WORK/mdph.json" all_phases_measured)" "True"
"$PY" - "$WORK/mdph.json" <<'PYEOF' && ok "phases_measured names them in run order" \
    || bad "phases_measured is wrong"
import json, sys
assert json.load(open(sys.argv[1]))["phases_measured"] == ["create", "stat", "delete"]
PYEOF
# A partial sweep -- create only -- must NOT satisfy the completeness check. A floor on
# create alone would happily pass this run.
PARTIAL="$WORK/mdsweep/partial.csv"
cat > "$PARTIAL" <<'CSV'
node_count,thread_count,iteration_count,create_rate_avg,stat_rate_avg,delete_rate_avg,create_lat_p99,stat_lat_p99,delete_lat_p99
1,1,1,100.0,0,0,500.0,0,0
CSV
"$PY" "$S/metrics.py" --workload mdtest --sweep-dir "$WORK/mdsweep" --from-csv "$PARTIAL" \
    --min-create-ops-s 1 > "$WORK/mdpart.json" 2>/dev/null
check "a create-only sweep is not 'all phases measured'" \
    "$(jqf "$WORK/mdpart.json" all_phases_measured)" "False"
check "...while the create floor itself still passes" "$(jqf "$WORK/mdpart.json" success)" "True"
# The io workload must not grow these keys: absent is correct, false would read as
# "measured nothing".
"$PY" - "$WORK/ioy.json" <<'PYEOF' && ok "the io payload carries no mdtest phase fields" \
    || bad "io metrics emit mdtest phase fields"
import json, sys
d = json.load(open(sys.argv[1]))
assert "phases_measured" not in d and "all_phases_measured" not in d, sorted(d)
PYEOF


# =======================================================================================
# BATCHES, PRE-FLIGHT, MIXED WORKLOADS
# =======================================================================================
echo
echo "== is_batch_dir matches upstream's marker set, not just the manifest =="
# elbencho_batch_is_batch (lib/_batch_functions.sh:23-29) tests FOUR markers, and says why:
# "a missing manifest must never downgrade it to an ordinary results tree." Testing only
# the manifest sent a damaged batch down the plain-sweep path -- poll would tally local
# sentinels instead of asking --status, collect would skip the batch branch.
SWEEPLIB_DIR="$S" W="$WORK" "$PY" - <<'PYEOF' && ok "any batch marker identifies a batch" \
    || bad "is_batch_dir does not match upstream's marker set"
import os, sys
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from common.sweeplib import is_batch_dir, BATCH_MARKERS
root = os.path.join(os.environ["W"], "markers")
assert set(BATCH_MARKERS) == {
    "batch-manifest.tsv", "batch-profile.tsv",
    "batch-sealed.sha256", "batch-sealed-environment.sha256"}, BATCH_MARKERS
for i, marker in enumerate(BATCH_MARKERS):
    d = os.path.join(root, str(i))
    os.makedirs(d, exist_ok=True)
    assert not is_batch_dir(d), f"empty dir looked like a batch: {d}"
    open(os.path.join(d, marker), "w").close()
    assert is_batch_dir(d), f"{marker} alone did not identify a batch"
# A DANGLING symlink is upstream's hardest case: `[[ ! -e x && ! -L x ]]` counts it as
# present, while os.path.exists follows the link and answers False. A dangling marker is a
# damaged batch, which is exactly when the batch path matters most.
d = os.path.join(root, "dangling")
os.makedirs(d, exist_ok=True)
os.symlink("/nonexistent-batch-target", os.path.join(d, "batch-sealed.sha256"))
assert is_batch_dir(d), "a dangling marker symlink was not treated as a batch"
PYEOF

echo
echo "== mixed batches: a group can name its own workload =="
# The manifest's group rows carry a kind column that is literally io or mdtest
# (lib/_batch_functions.sh:81 rejects anything else), and upstream's usage says either
# launcher can --start the mixed batch. Mixing is how bandwidth and metadata get measured
# against one filesystem under one sealed execution set.
SWEEPLIB_DIR="$S" "$PY" - <<'PYEOF' && ok "group specs route to the right launcher" \
    || bad "mixed batch group parsing is wrong"
import os, sys
sys.path.insert(0, os.environ["SWEEPLIB_DIR"])
sys.path.insert(0, os.path.dirname(os.environ["SWEEPLIB_DIR"]))
from submit_sweep import parse_group_spec
assert parse_group_spec("--nodes 2 --write-no-read", "io") == (
    "io", ["--nodes", "2", "--write-no-read"])
assert parse_group_spec("mdtest: --nodes 1 --tasks 4", "io") == (
    "mdtest", ["--nodes", "1", "--tasks", "4"])
assert parse_group_spec("io: --nodes 2", "mdtest") == ("io", ["--nodes", "2"])
# A bare spec inherits the run's workload, so every pre-existing config keeps its meaning.
assert parse_group_spec("--nodes 2", "mdtest") == ("mdtest", ["--nodes", "2"])
# A colon inside a VALUE must never be read as a prefix.
assert parse_group_spec("--read-from host:/path", "io") == (
    "io", ["--read-from", "host:/path"])
assert parse_group_spec("--read-from /a:/b", "io")[0] == "io"
# An unknown prefix is not a workload: treat the whole thing as flags and let the driver
# reject it, rather than silently routing to the default.
assert parse_group_spec("warp: --nodes 1", "io")[0] == "io"
PYEOF

# And the appends must actually reach two different launchers.
MIXB="$WORK/mixbase"; mkdir -p "$MIXB/storage-tests/fs" "$MIXB/lib"
cp "$SST_CHECKOUT/env.sh.template" "$MIXB/env.sh.template"
echo 'ACV_CHAIN_REACHED=1' > "$MIXB/lib/env_base.sh"
for drv in nv-elbencho-sweep.sh nv-mdtest-elbencho.sh; do
    cat > "$MIXB/storage-tests/fs/$drv" <<MSTUB
#!/bin/bash
echo "\$(basename "\$0") \$1" >> "$WORK/mix.log"
[[ "\$1" == --batch ]] && echo "STORAGE_SCALE_TEST_BATCH_RESULTS=$WORK/mixroot"
exit 0
MSTUB
    chmod +x "$MIXB/storage-tests/fs/$drv"
done
mkdir -p "$WORK/mixroot"
: > "$WORK/mix.log"
"$PY" "$S/submit_sweep.py" --env-sh "$OVERLAY" --config-hash mix01 --substrate kubectl \
    --results-dir "$WORK/mixres" --scale-test-base "$MIXB" --nodes 1 \
    --batch --no-start --batch-group 'mdtest: --nodes 1 --tasks 4' \
    --batch-group '--nodes 2' > "$WORK/mix.json" 2>/dev/null
check "a mixed batch prepares 3 groups" "$(jqf "$WORK/mix.json" groups_prepared)" "3"
check "...and is flagged mixed" "$(jqf "$WORK/mix.json" mixed_workloads)" "True"
"$PY" - "$WORK/mix.json" <<'PYEOF' && ok "...recording each group's workload in order" \
    || bad "group_workloads is wrong"
import json, sys
assert json.load(open(sys.argv[1]))["group_workloads"] == ["io", "mdtest", "io"]
PYEOF
if grep -q 'nv-mdtest-elbencho.sh --append' "$WORK/mix.log" \
   && grep -q 'nv-elbencho-sweep.sh --append' "$WORK/mix.log" \
   && grep -q 'nv-elbencho-sweep.sh --batch' "$WORK/mix.log"; then
    ok "both launchers appended into the one batch root"
else
    bad "mixed batch did not use both launchers: $(tr '\n' ' ' < "$WORK/mix.log")"
fi
# A single-workload batch must NOT claim to be mixed, or a metrics step would switch to
# --kind all for no reason.
: > "$WORK/mix.log"
"$PY" "$S/submit_sweep.py" --env-sh "$OVERLAY" --config-hash mix02 --substrate kubectl \
    --results-dir "$WORK/mixres2" --scale-test-base "$MIXB" --nodes 1 \
    --batch --no-start --batch-group '--nodes 2' > "$WORK/mix2.json" 2>/dev/null
check "a single-workload batch is not flagged mixed" \
    "$(jqf "$WORK/mix2.json" mixed_workloads)" "False"


echo
echo "== a STARTED batch keeps the shape facts the prepare discovered =="
# start_batch() describes the DISPATCH, not the batch, and its dict replaces the prepare's.
# So a started mixed batch reported group_workloads/mixed_workloads as null, and a metrics
# step keying off mixed_workloads would report half of it. Every earlier test used
# --no-start, which returns the prepare dict directly -- that is exactly why they missed it.
: > "$WORK/mix.log"
"$PY" "$S/submit_sweep.py" --env-sh "$OVERLAY" --config-hash mix03 --substrate kubectl \
    --results-dir "$WORK/mixres3" --scale-test-base "$MIXB" --nodes 1 \
    --batch --batch-group 'mdtest: --nodes 1 --tasks 4' > "$WORK/mixstart.json" 2>/dev/null
check "a started mixed batch still reports mixed" \
    "$(jqf "$WORK/mixstart.json" mixed_workloads)" "True"
check "...and still reports its group count" \
    "$(jqf "$WORK/mixstart.json" groups_prepared)" "2"
"$PY" - "$WORK/mixstart.json" <<'PYEOF' && ok "...and still names each group's workload" \
    || bad "group_workloads was lost on the started path"
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("group_workloads") == ["io", "mdtest"], d.get("group_workloads")
PYEOF
# The --no-start path must be unchanged.
"$PY" "$S/submit_sweep.py" --env-sh "$OVERLAY" --config-hash mix04 --substrate kubectl \
    --results-dir "$WORK/mixres4" --scale-test-base "$MIXB" --nodes 1 --batch --no-start \
    --batch-group 'mdtest: --nodes 1 --tasks 4' > "$WORK/mixdraft.json" 2>/dev/null
check "a DRAFT mixed batch reports mixed too" \
    "$(jqf "$WORK/mixdraft.json" mixed_workloads)" "True"

echo
echo "== poll: a finished batch passes, it does not fail on a stale tally =="
# On kubectl the per-cell sentinels stay PENDING until --collect publishes them, so assess()
# sees every cell outstanding. The batch branch overrode `done` and `state` but NOT
# `success`, so a batch the driver had already called SUCCESS reported
# state=success, done=true, success=FALSE -- failing the poll step of a run that worked.
# The plain-sweep kubectl branch had this fixed; the batch branch did not.
mk_batch_stub() {  # $1=base  $2=STATE  $3=NEXT_ACTION
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    cat > "$1/storage-tests/fs/nv-elbencho-sweep.sh" <<BSTB
#!/bin/bash
printf 'STORAGE_SCALE_TEST_BATCH=SEALED\n'
printf 'STORAGE_SCALE_TEST_BATCH_STATE=$2\n'
printf 'STORAGE_SCALE_TEST_BATCH_NEXT_ACTION=$3\n'
printf 'STORAGE_SCALE_TEST_BATCH_EXECUTIONS_TOTAL=2\n'
exit 0
BSTB
    chmod +x "$1/storage-tests/fs/nv-elbencho-sweep.sh"
}
# A sealed batch whose cells are all still PENDING locally -- the real kubectl shape.
mk_sweep "$WORK/bpoll" PENDING PENDING
: > "$WORK/bpoll/batch-manifest.tsv"
BOK="$WORK/bok"; mk_batch_stub "$BOK" SUCCESS COLLECT
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/bpoll" --substrate kubectl \
    --scale-test-base "$BOK" --wait 1 --interval 1 > "$WORK/bpoll.json" 2>/dev/null
check "a SUCCESS batch passes the poll step" "$(jqf "$WORK/bpoll.json" success)" "True"
check "...and is done" "$(jqf "$WORK/bpoll.json" done)" "True"
check "...with its counts flagged non-authoritative" \
    "$(jqf "$WORK/bpoll.json" counts_authoritative)" "False"
"$PY" - "$WORK/bpoll.json" <<'PYEOF' && ok "...and carries no leftover 'still outstanding' error" \
    || bad "a successful batch poll still reports an error"
import json, sys
d = json.load(open(sys.argv[1]))
assert "error" not in d, d.get("error")
PYEOF
# FAILED and CANCELLED are terminal too -- done, but NOT a pass, and each says what to do.
BFAIL="$WORK/bfail"; mk_batch_stub "$BFAIL" FAILED COLLECT
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/bpoll" --substrate kubectl \
    --scale-test-base "$BFAIL" --wait 1 --interval 1 > "$WORK/bfail.json" 2>/dev/null
check "a FAILED batch does not pass" "$(jqf "$WORK/bfail.json" success)" "False"
check "...but is still terminal" "$(jqf "$WORK/bfail.json" done)" "True"
BCAN="$WORK/bcan"; mk_batch_stub "$BCAN" CANCELLED COLLECT
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/bpoll" --substrate kubectl \
    --scale-test-base "$BCAN" --wait 1 --interval 1 > "$WORK/bcan.json" 2>/dev/null
check "a CANCELLED batch does not pass" "$(jqf "$WORK/bcan.json" success)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'CANCELLED' in e and 'release' in e" \
    "$WORK/bcan.json" && ok "...and names cancellation as terminal, not a stall" \
    || bad "the cancelled-batch error is not actionable"
# A batch that is genuinely still running must NOT be turned into a pass.
BRUN="$WORK/brun"; mk_batch_stub "$BRUN" RUNNING WAIT
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/bpoll" --substrate kubectl \
    --scale-test-base "$BRUN" --wait 1 --interval 1 > "$WORK/brun.json" 2>/dev/null
check "a RUNNING batch still does not pass" "$(jqf "$WORK/brun.json" success)" "False"
check "...and is not done" "$(jqf "$WORK/brun.json" done)" "False"

echo
echo "== metrics: a prepared batch is reported by extract-filesystem.py =="
# The per-workload engines take RESULT directories. A batch root holds
# groups/NNNN/<kind>-<DS>, and committed membership is defined by the manifest, never by
# walking the tree -- so an engine pointed at a batch root reports nothing and this step
# would blame the extractor for a batch full of good results.
BB2="$WORK/batchbase"; mkdir -p "$BB2/utils"
BROOT="$WORK/batchroot"; mkdir -p "$BROOT/reports/groups/0001" "$BROOT/reports/groups/0002"
: > "$BROOT/batch-manifest.tsv"
cat > "$BB2/utils/extract-filesystem.py" <<'BSTUB'
#!/bin/bash
# Mimics report_batch: one roll-up per group, each named by ITS OWN engine's convention.
root=""
for a in "$@"; do case "$a" in -*) ;; *) [ -z "$root" ] && root="$a" ;; esac; done
echo "ARGS: $*" >> "$root/../extract.args"
cat > "$root/reports/groups/0001/elbencho-metrics.csv" <<'C'
operation,nodes,threads,io_size,io_depth,iops,throughput_mib_s
WRITE,1,1,1M,1,10,1000
READ,1,1,1M,1,20,2000
C
cat > "$root/reports/groups/0002/mdtest-elbencho-metrics-20260101Z000000.csv" <<'C'
node_count,thread_count,iteration_count,create_rate_avg,stat_rate_avg,delete_rate_avg,create_lat_p99,stat_lat_p99,delete_lat_p99
1,1,1,500.0,600.0,700.0,10.0,11.0,12.0
C
exit 0
BSTUB
chmod +x "$BB2/utils/extract-filesystem.py"
"$PY" "$S/metrics.py" --sweep-dir "$BROOT" --scale-test-base "$BB2" \
    --min-write-gb-s 0.001 --min-create-ops-s 1 > "$WORK/batch.json" 2>/dev/null
check "a batch reports successfully" "$(jqf "$WORK/batch.json" success)" "True"
check "...and is flagged as a batch" "$(jqf "$WORK/batch.json" batch)" "True"
check "...and as mixed" "$(jqf "$WORK/batch.json" mixed_workloads)" "True"
"$PY" - "$WORK/batch.json" <<'PYEOF' && ok "both workloads are measured and kept separate" \
    || bad "batch metrics merged or dropped a workload"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["workloads_measured"] == ["io", "mdtest"], d["workloads_measured"]
bw = d["by_workload"]
assert bw["io"]["headline"]["write"]["best_agg_gb_s"] > 0, bw["io"]
assert bw["mdtest"]["headline"]["create"]["best_ops_s"] == 500.0, bw["mdtest"]
# io rows and mdtest rows share no columns; merging them into one headline would produce a
# table where every row is mostly empty.
assert d["headline"] == {}, d["headline"]
assert d["metrics_csv"] is None, d["metrics_csv"]
# Both threshold sets are legitimate on a mixed batch, and both must have been applied.
names = {c["name"] for c in d["checks"]}
assert "write_agg_gb_s" in names and "create_ops_s" in names, names
PYEOF
grep -q -- '--kind all' "$WORK/extract.args" \
    && ok "the batch reporter is asked for every kind, not just --workload" \
    || bad "batch reporting narrowed to one kind: $(cat "$WORK/extract.args" 2>/dev/null)"
# --groups must reach the reporter so an operator can report a subset.
rm -f "$WORK/extract.args"
"$PY" "$S/metrics.py" --sweep-dir "$BROOT" --scale-test-base "$BB2" --groups 0001 \
    --min-write-gb-s 0.001 --min-create-ops-s 1 > /dev/null 2>&1
grep -q -- '--groups 0001' "$WORK/extract.args" && ok "--groups is forwarded to the reporter" \
    || bad "--groups never reached the reporter"

echo
echo "== metrics: a threshold is judged by what was MEASURED, not by --workload =="
# On a mixed batch both sets are legitimate. On a single-kind run the other set must still
# be refused rather than ignored -- an ignored threshold leaves asserted:true with the
# operator's number never compared to anything.
SINGLE="$WORK/singlebatch"; mkdir -p "$SINGLE/reports/groups/0001"
: > "$SINGLE/batch-profile.tsv"
SB="$WORK/singlebase"; mkdir -p "$SB/utils"
cat > "$SB/utils/extract-filesystem.py" <<'SSTUB'
#!/bin/bash
root=""
for a in "$@"; do case "$a" in -*) ;; *) [ -z "$root" ] && root="$a" ;; esac; done
cat > "$root/reports/groups/0001/elbencho-metrics.csv" <<'C'
operation,nodes,threads,io_size,io_depth,iops,throughput_mib_s
WRITE,1,1,1M,1,10,1000
C
exit 0
SSTUB
chmod +x "$SB/utils/extract-filesystem.py"
"$PY" "$S/metrics.py" --sweep-dir "$SINGLE" --scale-test-base "$SB" \
    --min-create-ops-s 1 > "$WORK/sb.json" 2>/dev/null
check "an mdtest threshold on an io-only batch fails" "$(jqf "$WORK/sb.json" success)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert '--min-create-ops-s' in e and 'io' in e" \
    "$WORK/sb.json" && ok "...naming the flag and what was actually measured" \
    || bad "the error does not name the flag or the measured kinds"
"$PY" "$S/metrics.py" --sweep-dir "$SINGLE" --scale-test-base "$SB" \
    --min-write-gb-s 0.001 > "$WORK/sb2.json" 2>/dev/null
check "...while the applicable threshold passes" "$(jqf "$WORK/sb2.json" success)" "True"
check "...and a single-kind batch is not flagged mixed" \
    "$(jqf "$WORK/sb2.json" mixed_workloads)" "False"
# Single-workload output must keep the EXACT top-level shape existing validations assert.
"$PY" - "$WORK/sb2.json" <<'PYEOF' && ok "single-workload payloads keep the old top-level shape" \
    || bad "the batch refactor changed the single-workload payload shape"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["metrics_csv"], d["metrics_csv"]
assert d["samples"] >= 1
assert d["headline"]["write"]["best_agg_gb_s"] > 0, d["headline"]
PYEOF

# --from-csv cannot stand for a batch: upstream refuses it (filesystem_batch.py:435-438)
# because one CSV cannot represent several groups.
"$PY" "$S/metrics.py" --sweep-dir "$SINGLE" --from-csv "$IOCSV" > "$WORK/sb3.json" 2>/dev/null
check "--from-csv on a batch is refused" "$(jqf "$WORK/sb3.json" success)" "False"

echo
echo "== preflight: upstream's validate_env.sh runs before anything is dispatched =="
PB="$WORK/preflightbase"; mkdir -p "$PB/storage-tests/fs" "$PB/lib"
cp "$SST_CHECKOUT/env.sh.template" "$PB/env.sh.template"
echo 'ACV_CHAIN_REACHED=1' > "$PB/lib/env_base.sh"
printf '#!/bin/bash\nexit 0\n' > "$PB/storage-tests/fs/nv-elbencho-sweep.sh"
chmod +x "$PB/storage-tests/fs/nv-elbencho-sweep.sh"
cat > "$PB/validate_env.sh" <<'VOK'
#!/bin/bash
echo "  Kubernetes context: some-context"
echo "  Kubernetes worker selector: node-role=storage-test (2 Ready arm64 node(s))"
echo "All validation checks passed successfully"
exit 0
VOK
chmod +x "$PB/validate_env.sh"
"$PY" "$S/preflight.py" --scale-test-base "$PB" --env-sh "$OVERLAY" \
    --config-hash pf01 > "$WORK/pf.json" 2>/dev/null
check "a passing validator passes the step" "$(jqf "$WORK/pf.json" success)" "True"
check "...and records it as preflight_passed" "$(jqf "$WORK/pf.json" preflight_passed)" "True"
check "...having parsed the individual checks" "$(jqf "$WORK/pf.json" checks_run)" "2"

cat > "$PB/validate_env.sh" <<'VBAD'
#!/bin/bash
echo "  Kubernetes context: some-context"
echo "Validation failed with the following errors:"
echo "  TEST_DIRS path is not a writable directory: /mnt/storage-scale-test/nope"
exit 1
VBAD
chmod +x "$PB/validate_env.sh"
"$PY" "$S/preflight.py" --scale-test-base "$PB" --env-sh "$OVERLAY" \
    --config-hash pf01 > "$WORK/pfbad.json" 2>/dev/null
check "a failing validator fails the step" "$(jqf "$WORK/pfbad.json" success)" "False"
check "...and says so in preflight_passed" "$(jqf "$WORK/pfbad.json" preflight_passed)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'writable' in e and 'Nothing was' in e" \
    "$WORK/pfbad.json" && ok "...surfacing the real reason and that nothing was created" \
    || bad "the preflight error is not actionable"
"$PY" "$S/preflight.py" --scale-test-base "$PB" --env-sh "$OVERLAY" \
    --config-hash pf01 --warn-only > "$WORK/pfwarn.json" 2>/dev/null
check "--warn-only passes the step" "$(jqf "$WORK/pfwarn.json" success)" "True"
check "...but never claims the preflight passed" \
    "$(jqf "$WORK/pfwarn.json" preflight_passed)" "False"

# A validator whose WORDING changed must not start passing: the exit code is the authority.
cat > "$PB/validate_env.sh" <<'VQUIET'
#!/bin/bash
echo "something upstream reworded"
exit 1
VQUIET
chmod +x "$PB/validate_env.sh"
"$PY" "$S/preflight.py" --scale-test-base "$PB" --env-sh "$OVERLAY" \
    --config-hash pf01 > "$WORK/pfq.json" 2>/dev/null
check "an unparsable failure still fails" "$(jqf "$WORK/pfq.json" success)" "False"

# The env.sh it validates must be THIS run's. Without installing first it would validate
# whatever was left behind by the previous run and pass on settings never used.
"$PY" - "$WORK/pf.json" "$PB" <<'PYEOF' && ok "preflight installs this run's env.sh before validating" \
    || bad "preflight did not install the composed env.sh"
import json, os, sys
d = json.load(open(sys.argv[1]))
composed = d["composed_env_sh"]
assert os.path.isfile(composed), composed
link = os.path.join(sys.argv[2], "env.sh")
assert os.path.realpath(link) == os.path.realpath(composed), (link, composed)
assert "ACV overlay" in open(composed).read()
PYEOF


echo
echo "== preflight: the diagnostics block is structured, not inlined into the error =="
# ACV repeats a failing step's error verbatim inside EVERY failing validation's text, so a
# long error is printed twice. Upstream's diagnostics block is ~18 KEY=VALUE lines, most of
# them empty; inlining it produced a ~2000-character error that buried the one line saying
# what to do. AGENTS.md asks for minimal step output.
cat > "$PB/validate_env.sh" <<'VDIAG'
#!/bin/bash
echo "  Kubernetes context: some-context"
echo "Validation failed with the following errors:"
echo "  Kubernetes API or storage validation failed: relogin required"
echo "    Unable to connect to the server: getting credentials"
echo "    STORAGE_SCALE_TEST_DIAGNOSTIC_OPERATION=kubectl-observe"
echo "    STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE=api-observation"
echo "    STORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE=unknown"
echo "    STORAGE_SCALE_TEST_DIAGNOSTIC_JOB_EVIDENCE=''"
echo "    STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=AUTH"
echo "    STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_KIND=''"
echo "    STORAGE_SCALE_TEST_DIAGNOSTIC_SAFE_NEXT_ACTION=authenticate\\ kubectl\\ and\\ retry"
exit 1
VDIAG
chmod +x "$PB/validate_env.sh"
"$PY" "$S/preflight.py" --scale-test-base "$PB" --env-sh "$OVERLAY" \
    --config-hash pf02 > "$WORK/pfdiag.json" 2>/dev/null
check "a diagnosed failure still fails" "$(jqf "$WORK/pfdiag.json" success)" "False"
"$PY" - "$WORK/pfdiag.json" <<'PYEOF' && ok "the error is short and names the reason and next action" \
    || bad "the preflight error is bloated or lost its hint"
import json, sys
d = json.load(open(sys.argv[1]))
err = d["error"]
# The real inlined version was 2104 chars. A cap well under that, with the useful parts kept.
assert len(err) < 500, f"error is {len(err)} chars: {err[:200]}"
assert "AUTH" in err, err
assert "authenticate kubectl" in err, err
assert "Nothing was created" in err, err
# Empty and 'unknown' diagnostics carry no information and must not be kept.
diag = d["diagnostics"]
assert diag["reason"] == "AUTH", diag
assert diag["operation"] == "kubectl-observe", diag
assert "local_state" not in diag, diag      # was 'unknown'
assert "resource_kind" not in diag, diag    # was ''
assert "job_evidence" not in diag, diag     # was ''
# The escaped spaces upstream prints must be readable.
assert diag["safe_next_action"] == "authenticate kubectl and retry", diag
# No diagnostics line may leak into the human error list.
assert not [e for e in d["errors"] if e.startswith("STORAGE_SCALE_TEST_DIAGNOSTIC_")], d["errors"]
PYEOF

echo
echo "== kubectl resume: the collection gate is checked HERE, not discovered in the driver =="
# kubectl_resume_collected_sweep refuses unless the attempt is COLLECTED
# (_nv-elbencho-kubectl-functions.sh:6176-6186) and answers with
# INVALID_LIFECYCLE_OPERATION plus a page of diagnostics naming an internal state machine.
# Checking the gate here turns that into a sentence naming the STEP to run.
mk_resume_stub() {  # $1=base  $2=RESULT_COLLECTION  $3=attempt-before  $4=attempt-after
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    for drv in nv-elbencho-sweep.sh nv-mdtest-elbencho.sh; do
        cat > "$1/storage-tests/fs/$drv" <<RSTUB
#!/bin/bash
MARK="$1/.resumed"
echo "\$(basename "\$0") \$1" >> "$1/resume.log"
case "\$1" in
  --resume) touch "\$MARK"; exit \${RESUME_RC:-0} ;;
  --status)
     if [[ -f "\$MARK" ]]; then printf 'ATTEMPT=$4\n'; else printf 'ATTEMPT=$3\n'; fi
     printf 'STATE=SUCCESS\nNEXT_ACTION=RESUME\nRESULT_COLLECTION=$2\n'
     printf 'EXECUTIONS_TOTAL=2\nEXECUTIONS_SUCCEEDED=1\nEXECUTIONS_FAILED=1\n'
     exit 0 ;;
esac
exit 0
RSTUB
        chmod +x "$1/storage-tests/fs/$drv"
    done
}

# A sweep with one cell still not SUCCESS -- something to resume. env_used.sh is part of
# the fixture because --resume REHYDRATES the prior configuration from it (upstream never
# rereads env.sh for a lifecycle operation), and resume_sweep.py refuses without it. A
# fixture missing it tests the wrong guard.
mk_sweep "$WORK/rsweep" SUCCESS FAILED
echo 'export RESULTS_DIR=/tmp' > "$WORK/rsweep/env_used.sh"

RGATE="$WORK/rgate"; mk_resume_stub "$RGATE" PENDING att-001 att-002
"$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/rsweep" --substrate kubectl \
    --scale-test-base "$RGATE" > "$WORK/rgate.json" 2>/dev/null
check "an uncollected attempt is refused" "$(jqf "$WORK/rgate.json" success)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'collect step' in e and 'COMPLETE' in e" \
    "$WORK/rgate.json" && ok "...naming the collect step and the state it needs" \
    || bad "the gate error does not name the collect step"
grep -q -- '--resume' "$RGATE/resume.log" 2>/dev/null \
    && bad "the driver was shelled anyway, before the gate was satisfied" \
    || ok "...without shelling --resume at all"

echo
echo "== kubectl resume: a new attempt id is the proof, not the exit code =="
ROK="$WORK/rok"; mk_resume_stub "$ROK" COMPLETE att-001 att-002
"$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/rsweep" --substrate kubectl \
    --scale-test-base "$ROK" > "$WORK/rok.json" 2>/dev/null
check "a collected attempt resumes" "$(jqf "$WORK/rok.json" success)" "True"
check "...reporting a new attempt" "$(jqf "$WORK/rok.json" new_attempt)" "True"
check "...naming the attempt it replaced" "$(jqf "$WORK/rok.json" previous_attempt)" "att-001"
check "...and the one it created" "$(jqf "$WORK/rok.json" attempt)" "att-002"
check "...with the outstanding cell counted" "$(jqf "$WORK/rok.json" resumed)" "1"
grep -q -- '--resume' "$ROK/resume.log" && ok "...having actually shelled --resume" \
    || bad "--resume never reached the driver"

# THE failure this step exists to catch: exit 0 with an unchanged attempt id. Upstream
# mints a new id on resume, so an unchanged one means nothing was dispatched.
RNOOP="$WORK/rnoop"; mk_resume_stub "$RNOOP" COMPLETE att-001 att-001
"$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/rsweep" --substrate kubectl \
    --scale-test-base "$RNOOP" > "$WORK/rnoop.json" 2>/dev/null
check "exit 0 with an unchanged attempt id is NOT a pass" \
    "$(jqf "$WORK/rnoop.json" success)" "False"
check "...and is not reported as a new attempt" \
    "$(jqf "$WORK/rnoop.json" new_attempt)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'did not change' in e and 'nothing was' in e.lower()" \
    "$WORK/rnoop.json" && ok "...saying plainly that nothing was dispatched" \
    || bad "the no-op resume error is not explicit"

# A driver that fails must not be reported as resumed.
RFAIL="$WORK/rfail"; mk_resume_stub "$RFAIL" COMPLETE att-001 att-002
RESUME_RC=1 "$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/rsweep" --substrate kubectl \
    --scale-test-base "$RFAIL" > "$WORK/rfail.json" 2>/dev/null
check "a refused --resume fails the step" "$(jqf "$WORK/rfail.json" success)" "False"
check "...with outcome refused" "$(jqf "$WORK/rfail.json" outcome)" "refused"

# Nothing outstanding -> nothing to resume, and that is normal.
mk_sweep "$WORK/rdone" SUCCESS SUCCESS
echo 'export RESULTS_DIR=/tmp' > "$WORK/rdone/env_used.sh"
"$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/rdone" --substrate kubectl \
    --scale-test-base "$ROK" --allow-noop > "$WORK/rdone.json" 2>/dev/null
check "an already-complete sweep is a noop, not an error" \
    "$(jqf "$WORK/rdone.json" success)" "True"
check "...reported as such" "$(jqf "$WORK/rdone.json" outcome)" "noop"

# Lost access before resuming must read as blindness, not as a gate failure.
BR="$WORK/rblind"; mkdir -p "$BR/storage-tests/fs/kubectl"
touch "$BR/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
printf '#!/bin/bash\nexit 1\n' > "$BR/storage-tests/fs/nv-elbencho-sweep.sh"
chmod +x "$BR/storage-tests/fs/nv-elbencho-sweep.sh"
"$PY" "$S/resume_sweep.py" --sweep-dir "$WORK/rsweep" --substrate kubectl \
    --scale-test-base "$BR" > "$WORK/rblind.json" 2>/dev/null
check "a blind resume fails" "$(jqf "$WORK/rblind.json" success)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'access' in e and 'unaffected' in e" \
    "$WORK/rblind.json" && ok "...naming lost access, not a collection gate" \
    || bad "a blind resume is misreported"

# mdtest must route to its own driver here too.
: > "$ROK/resume.log"
"$PY" "$S/resume_sweep.py" --workload mdtest --sweep-dir "$WORK/rsweep" --substrate kubectl \
    --scale-test-base "$ROK" > /dev/null 2>&1
grep -q 'nv-mdtest-elbencho.sh --resume' "$ROK/resume.log" \
    && ok "mdtest resume shells the mdtest driver" \
    || bad "mdtest resume used the wrong driver: $(tr '\n' ' ' < "$ROK/resume.log")"

echo
echo "== kubectl teardown is refused for the RIGHT reason =="
# --delete-only dispatches through sbatch or ssh and has no kubectl branch: "Error:
# --delete-only supports EXECUTION_SUBSTRATE=ssh or slurm" (nv-elbencho-sweep.sh:721, v2.0.0;
# v1.8.0 said "requires SLURM_ENABLED or SSH_ENABLED" at :631).
# The old message said the path was merely "not exercised", which reads as "probably works".
TD="$WORK/tdbase"; mkdir -p "$TD/storage-tests/fs"
printf '#!/bin/bash\nexit 0\n' > "$TD/storage-tests/fs/nv-elbencho-sweep.sh"
chmod +x "$TD/storage-tests/fs/nv-elbencho-sweep.sh"
mkdir -p "$WORK/tdroot/data"
"$PY" "$S/delete_data.py" --path "$WORK/tdroot/data" --test-root "$WORK/tdroot" \
    --substrate kubectl --scale-test-base "$TD" > "$WORK/td.json" 2>/dev/null
check "kubectl teardown is refused" "$(jqf "$WORK/td.json" success)" "False"
"$PY" - "$WORK/td.json" <<'PYEOF' && ok "...stating the upstream constraint and the alternative" \
    || bad "the teardown refusal is vague or misleading"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "SLURM_ENABLED or SSH_ENABLED" in e, e
assert "cluster-side job" in e, e
# Must NOT imply it merely needs testing -- it cannot work at all.
assert "not been exercised" not in e, e
# And must not be confused with the Lease: collect already handles Kubernetes objects.
assert "releases the Lease" in e, e
PYEOF
# Matches the REFUSAL, not one wording of it. v2.0.0 reworded "--delete-only requires
# SLURM_ENABLED or SSH_ENABLED" to "--delete-only supports EXECUTION_SUBSTRATE=ssh or
# slurm" (nv-elbencho-sweep.sh:721,778) without changing behaviour. The exact-string
# version of this check then failed with "now supports kubectl", which was false, and that
# false alarm was taken at face value for several sessions. A missing refusal means only
# that it is worth looking, so the failure says that and no more.
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' && ok "--delete-only still refuses kubectl upstream" \
    || bad "--delete-only's ssh/slurm-only refusal is gone upstream -- check whether kubectl teardown is now possible"
import os, re, sys
text = open(os.path.join(os.environ["SSTC"],
                         "storage-tests/fs/nv-elbencho-sweep.sh")).read()
refusals = [ln for ln in text.splitlines() if "--delete-only" in ln and re.search(
    r"SLURM_ENABLED or SSH_ENABLED|EXECUTION_SUBSTRATE=ssh or slurm", ln)]
assert refusals, "no --delete-only substrate refusal found"
assert not any("kubectl" in ln for ln in refusals), refusals
PYEOF

echo
echo "== verify: a sweep that produced every configured cell is complete =="
VB="$WORK/vbase"; mkdir -p "$VB"
# The summarizer is the only thing read out of the checkout; symlink the real tree in.
ln -sfn "$SST_CHECKOUT/utils" "$VB/utils"
ln -sfn "$SST_CHECKOUT/lib" "$VB/lib"
mkdir -p "$VB/storage-tests/fs"
printf '#!/bin/bash\necho stub\n' > "$VB/storage-tests/fs/nv-elbencho-sweep.sh"
chmod +x "$VB/storage-tests/fs/nv-elbencho-sweep.sh"

mk_io_results() {  # $1=dir  $2=nodes_spec  then node numbers that DID produce output
    local dir="$1" spec="$2"; shift 2
    mkdir -p "$dir"
    cat > "$dir/env_used.yaml" <<EOF
nodes_spec: "$spec"
dio_or_bio: dio
ELBENCHO_SCALE_IO_SIZES:
  - 1m
ELBENCHO_SCALE_THREAD_LIST:
  - 4
ELBENCHO_IODEPTH_LIST:
  - 1
ELBENCHO_SCALE_READ_WRITE_DURATION: 30
ELBENCHO_FILE_SIZE_MULTIPLIER: 1024
EOF
    local ds="${dir##*elbencho-}"
    for n in "$@"; do
        touch "$dir/elbencho-1m-c_$(printf '%03d' "$n")-s_004-d_001_${ds}.out"
    done
}

VFULL="$WORK/vres/elbencho-20261011Z120000"
mk_io_results "$VFULL" "1,2" 1 2
"$PY" "$S/verify_sweep.py" --sweep-dir "$VFULL" --scale-test-base "$VB" \
    > "$WORK/vfull.json" 2>/dev/null
check "a complete sweep passes" "$(jqf "$WORK/vfull.json" success)" "True"
check "...and says so" "$(jqf "$WORK/vfull.json" complete)" "True"
check "...having actually been verified" "$(jqf "$WORK/vfull.json" verified)" "True"
# The assertion that makes `complete` mean something. Upstream prints the expected total
# ONLY inside the INCOMPLETE banner, so reading it straight gives 0 here -- and a
# "matrix was non-empty" guard would then fail on exactly the healthy case.
check "...with a non-zero expected matrix even though nothing is missing" \
    "$(jqf "$WORK/vfull.json" outputs_expected)" "2"
check "...and nothing missing" "$(jqf "$WORK/vfull.json" outputs_missing)" "0"

echo
echo "== verify: a sweep missing configured cells FAILS, and counts them =="
# This is the failure no other step can see: every execution that exists is fine, there
# are simply fewer of them than were asked for. collect counts what was produced; only
# this reads env_used.yaml back.
VPART="$WORK/vres/elbencho-20261011Z130000"
mk_io_results "$VPART" "1,2,4" 1 2       # node count 4 never ran
"$PY" "$S/verify_sweep.py" --sweep-dir "$VPART" --scale-test-base "$VB" \
    > "$WORK/vpart.json" 2>/dev/null
check "an incomplete sweep fails" "$(jqf "$WORK/vpart.json" success)" "False"
check "...reporting how many cells are missing" "$(jqf "$WORK/vpart.json" outputs_missing)" "1"
check "...out of how many were configured" "$(jqf "$WORK/vpart.json" outputs_expected)" "3"
check "...while still reporting it was verified" "$(jqf "$WORK/vpart.json" verified)" "True"
# The banner is PREPENDED to line 1, so the basename is not the first token. Parsing it
# positionally yields "missing", which would then look like an unsummarized directory.
check "...naming the result directory, not the banner's first word" \
    "$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['results'][0]['name'])" "$WORK/vpart.json")" \
    "elbencho-20261011Z130000"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'full configured matrix' in e and 'env_used.yaml' in e" \
    "$WORK/vpart.json" && ok "...and saying which configuration it was compared against" \
    || bad "the incompleteness error does not name env_used.yaml"
# --allow-incomplete is for a deliberately truncated sweep (cancel/resume), and must keep
# the finding visible rather than deleting it.
"$PY" "$S/verify_sweep.py" --sweep-dir "$VPART" --scale-test-base "$VB" \
    --allow-incomplete > "$WORK/vallow.json" 2>/dev/null
check "--allow-incomplete passes the step" "$(jqf "$WORK/vallow.json" success)" "True"
check "...without claiming the sweep was complete" "$(jqf "$WORK/vallow.json" complete)" "False"
check "...and flags the override" "$(jqf "$WORK/vallow.json" allow_incomplete_override)" "True"

echo
echo "== verify: exit 0 is NOT evidence -- a skipped directory must fail =="
# summarize-elbencho.py ALWAYS returns 0 (main(), last line). Not a directory, no
# env_used.yaml, an mdtest snapshot, an unparsable nodes_spec: each is a stderr warning
# and a `continue`. A wrapper keyed on the exit code reports green on a sweep it never
# opened -- which is every sweep, since nothing makes it non-zero.
VNOENV="$WORK/vres/elbencho-20261011Z140000"
mkdir -p "$VNOENV"                       # no env_used.yaml at all
"$PY" "$S/verify_sweep.py" --sweep-dir "$VNOENV" --scale-test-base "$VB" \
    > "$WORK/vskip.json" 2>/dev/null
check "a directory the summarizer skipped is not a pass" "$(jqf "$WORK/vskip.json" success)" "False"
check "...and is explicitly NOT verified" "$(jqf "$WORK/vskip.json" verified)" "False"
check "...even though the tool exited 0" "$(jqf "$WORK/vskip.json" exit_code)" "0"
check "...with no summary block produced" "$(jqf "$WORK/vskip.json" directories_summarized)" "0"
"$PY" - "$WORK/vskip.json" <<'PYEOF' && ok "...carrying the tool's own skip reason" \
    || bad "the skip reason was not surfaced"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["warnings"], "stderr warnings were dropped"
assert "env_used.yaml" in d["warnings"][0], d["warnings"]
assert "nothing was verified" in d["error"], d["error"]
PYEOF
# The repair tool is NAMED but never CALLED. reconstruct_elbencho_env_used.py derives a
# configuration from the very *.out files the completeness check diffs against it, so
# running it automatically would close a loop on itself and report complete for any sweep
# at all -- including one that ran a single cell.
"$PY" - "$WORK/vskip.json" <<'PYCIRC' && ok "...pointing at the repair tool while warning it is circular" \
    || bad "the env_used.yaml hint is missing or does not warn about circularity"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "reconstruct_elbencho_env_used.py" in e, e
assert "circular" in e, e
PYCIRC
if grep -q 'reconstruct_elbencho_env_used' "$S/verify_sweep.py" \
   && ! grep -qE 'run\(\[[^]]*reconstruct' "$S/verify_sweep.py"; then
    ok "...and never shells it"
else
    bad "verify_sweep.py invokes the reconstructor -- that makes the check circular"
fi

echo
echo "== verify: non-SUCCESS executions are the OTHER incompleteness signal =="
# _completeness_note checks execution statuses FIRST and returns before the file diff, so
# a sweep can be incomplete both ways and report only this one. Both banner shapes are
# parsed; this proves the status shape is not silently dropped.
VEXEC="$WORK/vres/elbencho-20261011Z150000"
mk_io_results "$VEXEC" "1" 1
mkdir -p "$VEXEC/executions"
printf 'true\n' > "$VEXEC/executions/0001.sh"
printf 'SUCCESS\n' > "$VEXEC/executions/0001.status"
printf 'true\n' > "$VEXEC/executions/0002.sh"
printf 'FAILED\n' > "$VEXEC/executions/0002.status"
"$PY" "$S/verify_sweep.py" --sweep-dir "$VEXEC" --scale-test-base "$VB" \
    > "$WORK/vexec.json" 2>/dev/null
check "a sweep with a non-SUCCESS execution fails" "$(jqf "$WORK/vexec.json" success)" "False"
"$PY" - "$WORK/vexec.json" <<'PYEOF' && ok "...parsed as a count, not left as prose" \
    || bad "the executions-not-SUCCESS banner was not parsed"
import json, sys
r = json.load(open(sys.argv[1]))["results"][0]
assert r["executions_not_success"] == 1, r
assert r["executions_total"] == 2, r
PYEOF
# And the configured total must come from THAT banner, not from present+missing.
# `_completeness_note` returns the execution-status note first and never runs the file
# diff, so `outputs_missing` is 0 even though cells are missing -- deriving the matrix as
# present+missing understates it. A sweep cancelled after 5 of 8 cells reports 6 .out files
# present and 0 missing; the matrix is 8.
check "...with the configured total taken from the execution count" \
    "$(jqf "$WORK/vexec.json" outputs_expected)" "2"
check "...not from the one file on disk" "$(jqf "$WORK/vexec.json" outputs_present)" "1"

echo
echo "== verify is IO-only, and says why rather than verifying nothing =="
# _validate_io_sweep_env rejects an mdtest env_used.yaml and SKIPS the directory, so
# pointing this step at mdtest would produce zero blocks and a confusing "nothing was
# verified". Refusing up front names the real constraint.
"$PY" "$S/verify_sweep.py" --workload mdtest --sweep-dir "$VFULL" --scale-test-base "$VB" \
    > "$WORK/vmd.json" 2>/dev/null
check "mdtest is refused" "$(jqf "$WORK/vmd.json" success)" "False"
"$PY" - "$WORK/vmd.json" <<'PYEOF' && ok "...naming the upstream constraint and what does cover mdtest" \
    || bad "the mdtest refusal is vague"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "IO-only" in e, e
assert "nv-elbencho IO sweep" in e, e
assert "execution sentinels" in e, e   # what DOES cover mdtest
PYEOF
# And upstream must still reject mdtest -- if it ever learns to, wire the step up.
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' && ok "summarize-elbencho.py still rejects mdtest snapshots" \
    || bad "summarize-elbencho.py now handles mdtest -- extend verify to it"
import os
text = open(os.path.join(os.environ["SSTC"], "utils/summarize-elbencho.py")).read()
assert "not nv-elbencho IO sweep" in text
PYEOF
# The mdtest config must NOT carry a verify step it cannot run.
# Matches a STEP, not a mention: the mdtest config names verify_sweep.py in a comment
# explaining why it is absent, and that comment is the documentation this check protects.
echo "== verify: a batch root is expanded to its io group directories =="
# A batch root holds no env_used.yaml of its own -- the per-group result dirs do -- so
# handing the summarizer the root would summarize nothing and exit 0.
VBATCH="$WORK/vbatch"
mkdir -p "$VBATCH/groups/0001" "$VBATCH/groups/0002"
: > "$VBATCH/batch-manifest.tsv"
mk_io_results "$VBATCH/groups/0001/elbencho-20261011Z160000" "1" 1
mk_io_results "$VBATCH/groups/0002/elbencho-20261011Z170000" "1,2" 1    # node 2 missing
"$PY" "$S/verify_sweep.py" --sweep-dir "$VBATCH" --scale-test-base "$VB" \
    > "$WORK/vbatch.json" 2>/dev/null
check "a batch root is recognised" "$(jqf "$WORK/vbatch.json" batch)" "True"
check "...and both io groups are summarized" "$(jqf "$WORK/vbatch.json" directories_summarized)" "2"
check "...with one group incomplete" "$(jqf "$WORK/vbatch.json" incomplete_count)" "1"
check "...failing the step" "$(jqf "$WORK/vbatch.json" success)" "False"
check "...and totalling the matrix across groups" "$(jqf "$WORK/vbatch.json" outputs_expected)" "3"
# An mdtest-only batch has no io groups; that must be a named refusal, not an empty pass.
VMDB="$WORK/vmdbatch"; mkdir -p "$VMDB/groups/0001/mdtest-elbencho-20261011Z180000"
: > "$VMDB/batch-manifest.tsv"
"$PY" "$S/verify_sweep.py" --sweep-dir "$VMDB" --scale-test-base "$VB" \
    > "$WORK/vmdb.json" 2>/dev/null
check "an mdtest-only batch is refused, not silently passed" "$(jqf "$WORK/vmdb.json" success)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'no io group directories' in e" \
    "$WORK/vmdb.json" && ok "...saying there was nothing it could verify" \
    || bad "the mdtest-only batch refusal is unclear"

echo
echo "== placement: the eligible pool and the measured set are different sets =="
# They diverge whenever the pool is larger than --nodes, and the choice is RANDOM unless
# ORDER_NODES is set (`shuf` in _nv-elbencho-kubectl-coordinator.sh:449). Workers can stand
# up on BOTH eligible nodes while every execution runs on one -- node_coverage 0.5 -- and
# every throughput assertion still passes.
mk_placement() {  # $1=sweep dir  $2=attempt id  $3=pool CSV  $4=used-per-exec CSV
    local dir="$1" att="$2" adir
    adir="$dir/kubernetes/attempts/$att"
    mkdir -p "$adir" "$dir/executions"
    printf '%s' "$att" > "$dir/kubernetes/current-attempt"
    local n i=1
    for n in ${3//,/ }; do
        printf '%s\tuid-%s\tarm64\n' "$n" "$n" >> "$adir/nodes.tsv"
        printf '%s\tuid-%s\tpod-%s\tpuid-%s\t10.0.0.%d\tarm64\timg\n' \
            "$n" "$n" "$n" "$n" "$i" >> "$adir/worker-endpoints.tsv"
        i=$((i+1))
    done
    i=1
    for n in ${4//,/ }; do
        printf '%s\tpod-%s\tpuid-%s\t10.0.0.%d\n' "$n" "$n" "$n" "$i" \
            > "$(printf '%s/executions/%04d.workers.tsv' "$dir" "$i")"
        i=$((i+1))
    done
}
PLBASE="$WORK/plbase"; mkdir -p "$PLBASE/storage-tests/fs/kubectl"
touch "$PLBASE/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
printf '#!/bin/bash\nexit 0\n' > "$PLBASE/storage-tests/fs/nv-elbencho-sweep.sh"
chmod +x "$PLBASE/storage-tests/fs/nv-elbencho-sweep.sh"

PSUB="$WORK/psub"
mk_sweep "$PSUB" SUCCESS SUCCESS
mk_placement "$PSUB" att01 "n1,n2,n3" "n1,n1"
: > "$PSUB/elbencho-1M-c_001-s_001-d_001_20261012Z010101.csv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$PSUB" --substrate kubectl \
    --scale-test-base "$PLBASE" > "$WORK/pl.json" 2>/dev/null
check "the eligible pool is reported" "$(jqf "$WORK/pl.json" nodes_available_count)" "3"
check "...separately from what was measured" "$(jqf "$WORK/pl.json" nodes_used_count)" "1"
check "...with the ratio made explicit" "$(jqf "$WORK/pl.json" node_coverage)" "0.3333"
"$PY" - "$WORK/pl.json" <<'PYEOF' && ok "...and the nodes themselves are named, not just counted" \
    || bad "placement does not name the nodes"
import json, sys
p = json.load(open(sys.argv[1]))["placement"]
assert p["nodes_used"] == ["n1"], p
assert p["nodes_available"] == ["n1", "n2", "n3"], p
assert p["pods_used"] == ["pod-n1"], p
assert p["nodes_with_workers_count"] == 3, p   # workers stood up everywhere, one measured
PYEOF

echo
echo "== placement: the selection MODE is what makes coverage interpretable =="
# Coverage of 0.5 under random selection may be a different half next run; under ordered
# selection it is the same half every time. It is read from the run's OWN env_used.sh, so
# a sweep collected days later from a different shell reports what it ran under.
printf "export ORDER_NODES_ENABLED=1\n" > "$PSUB/env_used.sh"
"$PY" "$S/collect_sweep.py" --sweep-dir "$PSUB" --substrate kubectl \
    --scale-test-base "$PLBASE" > "$WORK/pord.json" 2>"$WORK/pord.err"
check "an ordered run is reported as deterministic" \
    "$(jqf "$WORK/pord.json" worker_selection)" "deterministic"
grep -q 'raise --nodes' "$WORK/pord.err" \
    && ok "...and the advice is to widen the sweep" \
    || bad "the partial-pool advice is wrong for an ordered run"
# Telling an operator to set a knob they already set reads as a bug in the report.
grep -q 'SST_ORDER_NODES=1' "$WORK/pord.err" \
    && bad "an ordered run is still told to set SST_ORDER_NODES" \
    || ok "...not to set a knob that is already set"
printf "export ORDER_NODES_ENABLED=''\n" > "$PSUB/env_used.sh"
"$PY" "$S/collect_sweep.py" --sweep-dir "$PSUB" --substrate kubectl \
    --scale-test-base "$PLBASE" > "$WORK/prand.json" 2>"$WORK/prand.err"
check "an unordered run is reported as random" \
    "$(jqf "$WORK/prand.json" worker_selection)" "random"
grep -q 'SST_ORDER_NODES=1' "$WORK/prand.err" \
    && ok "...and IS told how to make it repeatable" \
    || bad "a random run is not told about SST_ORDER_NODES"
rm -f "$PSUB/env_used.sh"
"$PY" "$S/collect_sweep.py" --sweep-dir "$PSUB" --substrate kubectl \
    --scale-test-base "$PLBASE" > "$WORK/punk.json" 2>/dev/null
check "no snapshot -> unknown, not guessed from the current shell" \
    "$(jqf "$WORK/punk.json" worker_selection)" "unknown"

echo
echo "== placement: a partial pool is reported, not failed =="
# Measuring a subset is a legitimate configuration -- `--nodes 1` against a larger pool is
# the normal case. What is not legitimate is not knowing, so this must stay a warning.
check "a half-measured pool still collects successfully" "$(jqf "$WORK/pl.json" success)" "True"

echo
echo "== placement: full coverage reads as 1.0 =="
PFULL="$WORK/pfull"
mk_sweep "$PFULL" SUCCESS SUCCESS
mk_placement "$PFULL" att02 "n1,n2" "n1,n2"
: > "$PFULL/elbencho-1M-c_001-s_001-d_001_20261012Z020202.csv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$PFULL" --substrate kubectl \
    --scale-test-base "$PLBASE" > "$WORK/pfull.json" 2>/dev/null
check "every eligible node measured -> coverage 1.0" "$(jqf "$WORK/pfull.json" node_coverage)" "1.0"
check "...and both are counted" "$(jqf "$WORK/pfull.json" nodes_used_count)" "2"

echo
echo "== placement: the CURRENT attempt is read, not whichever sorts first =="
# A resumed sweep holds several attempts. Only the one that produced these results is
# worth reporting, and `kubernetes/current-attempt` names it -- globbing the attempts dir
# would report the placement of an attempt that has been superseded.
PRES="$WORK/pres"
mk_sweep "$PRES" SUCCESS SUCCESS
# The superseded attempt is named so it sorts FIRST. Attempt ids are random hex upstream,
# so sort order says nothing about recency -- a reader that globs the attempts directory
# picks an arbitrary one and is right only by luck. These names make that luck impossible.
mk_placement "$PRES" aaa-superseded "n1,n2,n3" "n1"
mkdir -p "$PRES/kubernetes/attempts/zzz-current"
printf 'n9\tuid-n9\tarm64\n' > "$PRES/kubernetes/attempts/zzz-current/nodes.tsv"
printf 'zzz-current' > "$PRES/kubernetes/current-attempt"
: > "$PRES/elbencho-1M-c_001-s_001-d_001_20261012Z030303.csv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$PRES" --substrate kubectl \
    --scale-test-base "$PLBASE" > "$WORK/pres.json" 2>/dev/null
check "the current attempt's pool is the one reported" \
    "$(jqf "$WORK/pres.json" nodes_available_count)" "1"
"$PY" - "$WORK/pres.json" <<'PYEOF' && ok "...and the attempt directory is named for traceability" \
    || bad "the reported attempt is not the current one"
import json, sys
p = json.load(open(sys.argv[1]))["placement"]
assert p["attempt_dir"].endswith("zzz-current"), p["attempt_dir"]
assert p["nodes_available"] == ["n9"], p
PYEOF

echo
echo "== placement: an unknown pool is None, not a flattering 1.0 =="
# Dividing by an empty pool would be a crash or, worse, a ratio of 1.0 that reads as
# "everything was covered" when nothing is known at all.
PNONE="$WORK/pnone"
mk_sweep "$PNONE" SUCCESS
mkdir -p "$PNONE/executions"
printf 'n1\tpod-n1\tpuid\t10.0.0.1\n' > "$PNONE/executions/0001.workers.tsv"
: > "$PNONE/elbencho-1M-c_001-s_001-d_001_20261012Z040404.csv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$PNONE" --substrate kubectl \
    --scale-test-base "$PLBASE" > "$WORK/pnone.json" 2>/dev/null
check "no attempt metadata -> coverage is unknown" "$(jqf "$WORK/pnone.json" node_coverage)" "None"
check "...but what DID run is still reported" "$(jqf "$WORK/pnone.json" nodes_used_count)" "1"

echo
echo "== ORDER_NODES is reachable from ACV, for all substrates =="
# Without it, placement is random whenever the pool exceeds --nodes, so two runs of the
# identical config can differ for that reason alone -- a confounder for any threshold.
SST_ORDER_NODES=1 SST_SUBSTRATE=kubectl "$PY" "$S/gen_env_sh.py" --out "$WORK/ord.sh" \
    > /dev/null 2>&1
grep -qE '^export ORDER_NODES=1$' "$WORK/ord.sh" \
    && ok "SST_ORDER_NODES=1 reaches upstream as ORDER_NODES=1" \
    || bad "SST_ORDER_NODES does not map to ORDER_NODES: $(grep -i order "$WORK/ord.sh")"
# Absent means absent: the overlay must not assert a default upstream already owns.
SST_SUBSTRATE=kubectl "$PY" "$S/gen_env_sh.py" --out "$WORK/noord.sh" > /dev/null 2>&1
grep -q 'ORDER_NODES' "$WORK/noord.sh" \
    && bad "the overlay sets ORDER_NODES when the caller did not" \
    || ok "...and is omitted entirely when unset, leaving upstream's default"
# The kubectl paths must still honour it, or the knob is decorative.
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' && ok "both kubectl paths still read ORDER_NODES_ENABLED" \
    || bad "kubectl no longer honours ORDER_NODES -- the knob is decorative"
import os
root = os.environ["SSTC"]
for rel in ("storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh",
            "storage-tests/fs/kubectl/_nv-elbencho-kubectl-coordinator.sh"):
    assert "ORDER_NODES_ENABLED" in open(os.path.join(root, rel)).read(), rel
PYEOF

echo
echo "== poll: a RUNNING kubectl sweep reports the DRIVER's counts, not the local tally =="
# The local <NNNN>.status sentinels stay PENDING until --collect publishes them, so
# tally() reports 0 succeeded for the whole life of a running sweep: one that is 1/8, then
# 6/8, then 8/8 reads succeeded=0 pending=8 at every probe while --status returns the true
# counts from the PVC.
mk_status_stub() {  # $1=base $2=STATE $3=SOURCE $4=pending $5=running $6=success $7=next
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    local total=$(( $4 + $5 + $6 ))
    for drv in nv-elbencho-sweep.sh nv-mdtest-elbencho.sh; do
        cat > "$1/storage-tests/fs/$drv" <<SSTUB
#!/bin/bash
printf 'ATTEMPT=aa11\n'
printf 'STATE=$2\n'
printf 'EXECUTION_SCOPE=CURRENT_ATTEMPT\n'
printf 'PROGRESS_SOURCE=$3\n'
printf 'EXECUTIONS_TOTAL=$total\n'
printf 'EXECUTIONS_PENDING=$4\n'
printf 'EXECUTIONS_RUNNING=$5\n'
printf 'EXECUTIONS_SUCCEEDED=$6\n'
printf 'EXECUTIONS_FAILED=0\n'
printf 'RESULT_COLLECTION=PENDING\n'
printf 'NEXT_ACTION=$7\n'
exit 0
SSTUB
        chmod +x "$1/storage-tests/fs/$drv"
    done
}
# Eight cells, all PENDING locally -- the real kubectl shape mid-run.
PRUN="$WORK/prun"
mk_sweep "$PRUN" PENDING PENDING PENDING PENDING PENDING PENDING PENDING PENDING
GRUN="$WORK/grun"; mk_status_stub "$GRUN" RUNNING PVC 1 1 6 WAIT
"$PY" "$S/poll_sweep.py" --sweep-dir "$PRUN" --substrate kubectl \
    --scale-test-base "$GRUN" --wait 1 --interval 1 --allow-incomplete \
    > "$WORK/prun.json" 2>/dev/null
check "a running sweep reports the cluster's succeeded count" \
    "$(jqf "$WORK/prun.json" succeeded)" "6"
check "...and its pending count" "$(jqf "$WORK/prun.json" pending)" "1"
check "...and its running count" "$(jqf "$WORK/prun.json" running)" "1"
check "...naming the source" "$(jqf "$WORK/prun.json" progress_source)" "PVC"
check "...and saying the counts can be trusted" \
    "$(jqf "$WORK/prun.json" counts_authoritative)" "True"
# The local tally is not discarded -- a gap between cluster and local is itself diagnostic.
check "...while preserving what the local tree said" \
    "$(jqf "$WORK/prun.json" local_succeeded)" "0"
check "...which is the stale number this replaces" \
    "$(jqf "$WORK/prun.json" local_pending)" "8"

echo
echo "== poll: counts are INTEGERS, or a FieldValueCheck silently never matches =="
# --status emits text. A string survives into the payload looking like a number while
# failing every min/max comparison without erroring -- the check just never matches.
"$PY" - "$WORK/prun.json" <<'PYEOF' && ok "driver counts are parsed as integers" \
    || bad "driver counts reached the payload as strings"
import json, sys
d = json.load(open(sys.argv[1]))
for k in ("kubectl_executions_total", "kubectl_executions_pending",
          "kubectl_executions_running", "kubectl_executions_succeeded",
          "kubectl_executions_failed"):
    assert isinstance(d.get(k), int), (k, type(d.get(k)), d.get(k))
PYEOF

echo
echo "== poll: LOCAL counts are NOT adopted, and say so =="
# PROGRESS_SOURCE=LOCAL means the driver read the published manifest, not the cluster.
# Before a collect that tree is empty, so adopting those numbers would be the same bug
# with an extra step.
GLOC="$WORK/gloc"; mk_status_stub "$GLOC" RUNNING LOCAL 1 1 6 WAIT
"$PY" "$S/poll_sweep.py" --sweep-dir "$PRUN" --substrate kubectl \
    --scale-test-base "$GLOC" --wait 1 --interval 1 --allow-incomplete \
    > "$WORK/ploc.json" 2>/dev/null
check "a LOCAL progress source is not adopted" "$(jqf "$WORK/ploc.json" succeeded)" "0"
check "...and is flagged non-authoritative" \
    "$(jqf "$WORK/ploc.json" counts_authoritative)" "False"
"$PY" -c "import json,sys;n=json.load(open(sys.argv[1]))['counts_note'];assert 'PROGRESS_SOURCE=PVC' in n and 'collect' in n" \
    "$WORK/ploc.json" && ok "...explaining which fact was missing" \
    || bad "the non-authoritative note does not name PROGRESS_SOURCE"

echo
echo "== poll: adoption is all-or-nothing =="
# A partial adoption would mix cluster and local numbers into a tally that adds up to
# nothing coherent -- worse than either source alone, because it looks self-consistent.
GPART="$WORK/gpart"; mkdir -p "$GPART/storage-tests/fs/kubectl"
touch "$GPART/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
cat > "$GPART/storage-tests/fs/nv-elbencho-sweep.sh" <<'PSTUB'
#!/bin/bash
printf 'STATE=RUNNING\nPROGRESS_SOURCE=PVC\n'
printf 'EXECUTIONS_TOTAL=8\nEXECUTIONS_SUCCEEDED=6\n'
printf 'NEXT_ACTION=WAIT\n'
exit 0
PSTUB
chmod +x "$GPART/storage-tests/fs/nv-elbencho-sweep.sh"
"$PY" "$S/poll_sweep.py" --sweep-dir "$PRUN" --substrate kubectl \
    --scale-test-base "$GPART" --wait 1 --interval 1 --allow-incomplete \
    > "$WORK/ppart.json" 2>/dev/null
check "an incomplete count set is not partially adopted" \
    "$(jqf "$WORK/ppart.json" succeeded)" "0"
check "...and is not called authoritative" \
    "$(jqf "$WORK/ppart.json" counts_authoritative)" "False"

echo
echo "== poll: an unparsable count stays visible rather than vanishing =="
GBAD="$WORK/gbad"; mkdir -p "$GBAD/storage-tests/fs/kubectl"
touch "$GBAD/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
cat > "$GBAD/storage-tests/fs/nv-elbencho-sweep.sh" <<'BSTUB'
#!/bin/bash
printf 'STATE=RUNNING\nPROGRESS_SOURCE=PVC\n'
printf 'EXECUTIONS_TOTAL=eight\nEXECUTIONS_PENDING=1\nEXECUTIONS_RUNNING=1\n'
printf 'EXECUTIONS_SUCCEEDED=6\nEXECUTIONS_FAILED=0\nNEXT_ACTION=WAIT\n'
exit 0
BSTUB
chmod +x "$GBAD/storage-tests/fs/nv-elbencho-sweep.sh"
"$PY" "$S/poll_sweep.py" --sweep-dir "$PRUN" --substrate kubectl \
    --scale-test-base "$GBAD" --wait 1 --interval 1 --allow-incomplete \
    > "$WORK/pbad.json" 2>/dev/null
check "an unparsable total blocks adoption" "$(jqf "$WORK/pbad.json" succeeded)" "0"
"$PY" - "$WORK/pbad.json" <<'PYEOF' && ok "...and the raw value is kept for diagnosis" \
    || bad "an unparsable count was silently dropped"
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("kubectl_executions_total_raw") == "eight", d.get("kubectl_executions_total_raw")
assert "kubectl_executions_total" not in d, "a bad value was reported as a count"
PYEOF

echo
echo "== poll: a terminal sweep still reports the cluster's final counts =="
# This is the case the old NEXT_ACTION heuristic got right by accident and the running
# case it got wrong by omission. Both now come from the same published fact.
GDONE="$WORK/gdone"; mk_status_stub "$GDONE" SUCCESS PVC 0 0 8 COLLECT
"$PY" "$S/poll_sweep.py" --sweep-dir "$PRUN" --substrate kubectl \
    --scale-test-base "$GDONE" --wait 1 --interval 1 > "$WORK/pdone.json" 2>/dev/null
check "a finished sweep passes" "$(jqf "$WORK/pdone.json" success)" "True"
check "...reporting all cells succeeded" "$(jqf "$WORK/pdone.json" succeeded)" "8"
check "...from the cluster" "$(jqf "$WORK/pdone.json" counts_authoritative)" "True"

echo
echo "== PROGRESS_SOURCE is still a published contract upstream =="
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' && ok "kubectl_emit_status_view still emits PROGRESS_SOURCE" \
    || bad "PROGRESS_SOURCE is gone upstream -- poll's authority signal is broken"
import os
text = open(os.path.join(os.environ["SSTC"],
      "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh")).read()
assert "PROGRESS_SOURCE=%s" in text
# The two values the view validates; adopting on anything else would be a guess.
assert '"$source" =~ ^(LOCAL|PVC)$' in text
# And the live path must still be the PVC one.
assert "PVC PENDING" in text
PYEOF

echo
echo "== a lost coordinator is NOT a storage result =="
# An evicted or preempted coordinator pod leaves FAILED cells behind exactly like a
# storage fault does. Upstream distinguishes them; ACV used to report both as
# "N execution(s) FAILED ... --resume", which is a false finding on the first.
FBASE="$WORK/fbase"; mkdir -p "$FBASE/storage-tests/fs/kubectl"
touch "$FBASE/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
printf '#!/bin/bash\nexit 0\n' > "$FBASE/storage-tests/fs/nv-elbencho-sweep.sh"
chmod +x "$FBASE/storage-tests/fs/nv-elbencho-sweep.sh"

mk_failed_sweep() {  # $1=dir; remaining args are "filename=content" pairs
    local dir="$1"; shift
    mk_sweep "$dir" SUCCESS FAILED
    : > "$dir/elbencho-1M-c_001-s_001-d_001_20261012Z010101.csv"
    local pair
    for pair in "$@"; do printf '%b' "${pair#*=}" > "$dir/${pair%%=*}"; done
}

FLOSS="$WORK/floss"
mk_failed_sweep "$FLOSS" \
    'run-summary.tsv=schema\t1\nattempt_id\tab12\nrun_status\tFAILED\npending\t0\nrunning\t0\nsuccess\t1\nfailed\t1\n' \
    'coordinator-loss.tsv=schema\t1\nattempt_id\tab12\nexecution\t0002\nobserved_status\tRUNNING\nexecution_observed_status\tRUNNING\nrecovered_status\tFAILED\nexit_code\t143\n'
"$PY" "$S/collect_sweep.py" --sweep-dir "$FLOSS" --substrate kubectl \
    --scale-test-base "$FBASE" > "$WORK/floss.json" 2>/dev/null
check "a coordinator loss is classified as such" \
    "$(jqf "$WORK/floss.json" failure_kind)" "coordinator_loss"
check "...and is explicitly NOT a storage finding" \
    "$(jqf "$WORK/floss.json" is_storage_finding)" "False"
check "...carrying the exit code" "$(jqf "$WORK/floss.json" failure_exit_code)" "143"
check "...and the step still fails, because something did go wrong" \
    "$(jqf "$WORK/floss.json" success)" "False"
"$PY" - "$WORK/floss.json" <<'PYEOF' && ok "...with an error that says what it was and was not" \
    || bad "the coordinator-loss error reads like a storage failure"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "NOT a storage result" in e, e
assert "SIGTERM" in e, e                      # 143 is named, not left as a number
assert "measurement is void, not negative" in e, e
# Resuming into the same preemption reproduces the eviction, not the measurement.
assert "--resume" not in e, e
PYEOF

echo
echo "== a FAILED benchmark cell IS a storage result, and keeps the --resume advice =="
FBENCH="$WORK/fbench"
mk_failed_sweep "$FBENCH" \
    'run-summary.tsv=schema\t1\nattempt_id\tcd34\nrun_status\tFAILED\npending\t0\nrunning\t0\nsuccess\t1\nfailed\t1\nfailed_execution\t0002\nexit_code\t1\n'
"$PY" "$S/collect_sweep.py" --sweep-dir "$FBENCH" --substrate kubectl \
    --scale-test-base "$FBASE" > "$WORK/fbench.json" 2>/dev/null
check "a failed cell is classified as a benchmark failure" \
    "$(jqf "$WORK/fbench.json" failure_kind)" "benchmark"
check "...and IS a storage finding" "$(jqf "$WORK/fbench.json" is_storage_finding)" "True"
"$PY" - "$WORK/fbench.json" <<'PYEOF' && ok "...naming the cell and keeping the recovery command" \
    || bad "the benchmark-failure error lost its detail or its advice"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "execution 0002" in e, e
assert "IS a result about the storage" in e, e
assert "--resume" in e, e
PYEOF

echo
echo "== a SIGTERMed run is infrastructure, even with NO coordinator-loss.tsv =="
# Killing a real coordinator pod writes NO coordinator-loss.tsv -- that file appears only
# on upstream's recovery path (coordinator:1259), when a coordinator detects it lost a
# prior incarnation. Kill it outright and the cell it was running is simply marked FAILED
# with exit code 143, which by cell count alone is indistinguishable from a storage fault.
# The first version of this code reported it as `benchmark` / is_storage_finding=True.
#
# The fixture below is the ledger such a kill produces.
FTERM="$WORK/fterm"
mk_failed_sweep "$FTERM" \
    'run-summary.tsv=schema\t1\nattempt_id\t2a0a1983\nupdated_utc\t2026-10-12T07:21:14Z\nrun_status\tFAILED\npending\t4\nrunning\t0\nsuccess\t3\nfailed\t1\nfailed_execution\t0004\nexit_code\t143\n'
"$PY" "$S/collect_sweep.py" --sweep-dir "$FTERM" --substrate kubectl \
    --scale-test-base "$FBASE" > "$WORK/fterm.json" 2>/dev/null
check "a SIGTERMed run is classified as terminated" \
    "$(jqf "$WORK/fterm.json" failure_kind)" "terminated"
check "...and is NOT a storage finding" \
    "$(jqf "$WORK/fterm.json" is_storage_finding)" "False"
check "...with no coordinator-loss.tsv needed to reach that conclusion" \
    "$(jqf "$WORK/fterm.json" evidence_source_count)" "1"
"$PY" - "$WORK/fterm.json" <<'PYEOF' && ok "...explaining that 143 means killed, not failed" \
    || bad "the terminated-run error does not explain SIGTERM"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "NOT a storage result" in e, e
assert "SIGTERM" in e and "143" in e, e
assert "preemption" in e, e
assert "execution 0004" in e, e
assert "--resume" not in e, e
PYEOF
# A deliberate cancel ALSO sends SIGTERM, but upstream records CANCELLED, not FAILED.
# Without that distinction every cancelled sweep would be reported as an eviction.
FCAN="$WORK/fcan"
mk_sweep "$FCAN" SUCCESS SUCCESS
: > "$FCAN/elbencho-1M-c_001-s_001-d_001_20261012Z030303.csv"
printf 'schema\t1\nrun_status\tCANCELLED\npending\t0\nrunning\t0\nsuccess\t2\nfailed\t0\nexit_code\t143\n' \
    > "$FCAN/run-summary.tsv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$FCAN" --substrate kubectl \
    --scale-test-base "$FBASE" > "$WORK/fcan.json" 2>/dev/null
check "a CANCELLED run at exit 143 is not called a termination" \
    "$(jqf "$WORK/fcan.json" failure_kind)" "none"

echo
echo "== classification order: a loss that ALSO failed cells is still a loss =="
# This is the whole point. A lost coordinator leaves FAILED cells behind, so testing
# cells first would label every eviction a benchmark failure -- the exact misreading
# this exists to prevent. $FLOSS above has failed=1 AND coordinator-loss.tsv.
check "cells are not consulted before the loss evidence" \
    "$(jqf "$WORK/floss.json" failure_kind)" "coordinator_loss"

echo
echo "== a startup failure measured nothing, so it is not a storage result either =="
FSTART="$WORK/fstart"
mk_failed_sweep "$FSTART" \
    'run-summary.tsv=schema\t1\nattempt_id\tef56\nrun_status\tFAILED\npending\t0\nrunning\t0\nsuccess\t1\nfailed\t1\n' \
    'startup-error.txt=coordinator interrupted outside a failed cell (execution=none, rc=1)\n'
"$PY" "$S/collect_sweep.py" --sweep-dir "$FSTART" --substrate kubectl \
    --scale-test-base "$FBASE" > "$WORK/fstart.json" 2>/dev/null
check "a startup failure is classified" "$(jqf "$WORK/fstart.json" failure_kind)" "startup"
check "...and is not a storage finding" \
    "$(jqf "$WORK/fstart.json" is_storage_finding)" "False"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'outside any benchmark cell' in e and 'Nothing was measured' in e" \
    "$WORK/fstart.json" && ok "...quoting upstream's own message" \
    || bad "the startup-failure error does not carry upstream's message"

echo
echo "== a failure with no evidence says so, rather than inventing a cause =="
FBLIND="$WORK/fblind"
mk_failed_sweep "$FBLIND" 'run-summary.tsv=schema\t1\nrun_status\tFAILED\nfailed\t0\n'
"$PY" "$S/collect_sweep.py" --sweep-dir "$FBLIND" --substrate kubectl \
    --scale-test-base "$FBASE" > "$WORK/fblind.json" 2>/dev/null
# run_status FAILED with failed=0 and no loss/startup file: upstream recorded a failure
# and left no cause. Guessing one would be worse than admitting it.
check "an undiagnosable failure is classified benchmark only when a cell failed" \
    "$(jqf "$WORK/fblind.json" failure_kind)" "benchmark"

echo
echo "== a clean sweep records no failure and reads the ledger anyway =="
FOK="$WORK/fok"
mk_sweep "$FOK" SUCCESS SUCCESS
: > "$FOK/elbencho-1M-c_001-s_001-d_001_20261012Z020202.csv"
printf 'schema\t1\nattempt_id\t99aa\nrun_status\tSUCCESS\npending\t0\nrunning\t0\nsuccess\t2\nfailed\t0\n' \
    > "$FOK/run-summary.tsv"
"$PY" "$S/collect_sweep.py" --sweep-dir "$FOK" --substrate kubectl \
    --scale-test-base "$FBASE" > "$WORK/fok.json" 2>/dev/null
check "a clean sweep passes" "$(jqf "$WORK/fok.json" success)" "True"
check "...with no failure recorded" "$(jqf "$WORK/fok.json" failure_kind)" "none"
check "...and is not called a storage finding" \
    "$(jqf "$WORK/fok.json" is_storage_finding)" "False"
# The assertion the config makes: the ledger was actually found and read. A collect that
# published nothing would otherwise satisfy a kind of `none` by having looked at nothing.
check "...having read upstream's ledger" "$(jqf "$WORK/fok.json" evidence_source_count)" "1"

echo
echo "== the evidence files are still upstream's published contract =="
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' && ok "collect still publishes the three failure artefacts" \
    || bad "the failure-evidence artefacts changed upstream"
import os
root = os.environ["SSTC"]
fn = open(os.path.join(root,
    "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh")).read()
# The ledger allow-list is what guarantees these reach the local tree at --collect.
for name in ("run-summary\\.tsv", "startup-error\\.txt", "coordinator-loss\\.tsv"):
    assert name in fn, name
co = open(os.path.join(root,
    "storage-tests/fs/kubectl/_nv-elbencho-kubectl-coordinator.sh")).read()
# The keys read_failure_evidence depends on.
for key in ("observed_status", "execution_observed_status", "recovered_status",
            "failed_execution", "exit_code"):
    assert key in co, key
PYEOF

echo
echo "== a contended lock is NOT lost cluster access =="
# Every kubectl lifecycle verb takes a NON-BLOCKING flock on kubernetes/lifecycle.lock
# (_nv-elbencho-kubectl-functions.sh:285-306). A contended operation exits 1 immediately
# with a message on stderr -- which, by exit code alone, is what ACV read as "cluster
# access was lost (expired credential...)", telling the operator to re-authenticate over a
# local flock.
LOCKMSG='Error: another Kubernetes lifecycle operation is active for %s\n'
mk_lock_stub() {  # $1=base -- every verb refuses with upstream's contention message
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    for drv in nv-elbencho-sweep.sh nv-mdtest-elbencho.sh; do
        cat > "$1/storage-tests/fs/$drv" <<'LSTUB'
#!/bin/bash
printf 'Error: another Kubernetes lifecycle operation is active for %s/kubernetes\n' "$2" >&2
printf 'Wait for that operation to finish, then retry this command.\n' >&2
exit 1
LSTUB
        chmod +x "$1/storage-tests/fs/$drv"
    done
}
LB="$WORK/lockbase"; mk_lock_stub "$LB"
LSW="$WORK/locksweep"; mk_sweep "$LSW" PENDING PENDING
mkdir -p "$LSW/kubernetes"

"$PY" "$S/poll_sweep.py" --sweep-dir "$LSW" --substrate kubectl \
    --scale-test-base "$LB" --wait 1 --interval 1 > "$WORK/lpoll.json" 2>/dev/null
check "poll reports contention as its own state" "$(jqf "$WORK/lpoll.json" state)" "lock-contended"
check "...flagged explicitly" "$(jqf "$WORK/lpoll.json" lock_contended)" "True"
check "...and does not pass" "$(jqf "$WORK/lpoll.json" success)" "False"
"$PY" - "$WORK/lpoll.json" <<'PYEOF' && ok "...telling the operator to wait, not to re-authenticate" \
    || bad "the contention error still blames credentials"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "lifecycle operation is already running" in e, e
assert "NOT a credential or connectivity problem" in e, e
assert "re-authenticating will not help" in e, e
assert "Wait for the other operation" in e, e
# The old misdiagnosis must be gone, not merely supplemented.
assert "expired credential" not in e, e
assert "Restore access" not in e, e
PYEOF

echo
echo "== collect under contention does not report lost access either =="
# Contention produces exactly the shape collect's access-loss branch looks for: non-zero
# exit, no CSVs, everything outstanding. Order matters -- the contention test must come
# first or that branch claims it.
"$PY" "$S/collect_sweep.py" --sweep-dir "$LSW" --substrate kubectl \
    --scale-test-base "$LB" > "$WORK/lcoll.json" 2>/dev/null
check "collect fails on contention" "$(jqf "$WORK/lcoll.json" success)" "False"
check "...flagged as contention" "$(jqf "$WORK/lcoll.json" lock_contended)" "True"
"$PY" - "$WORK/lcoll.json" <<'PYEOF' && ok "...and names the lock, not the credential" \
    || bad "collect still misreports contention as lost access"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "lifecycle.lock" in e, e
assert "expired credential" not in e, e
PYEOF

echo
echo "== cancel under contention cancels nothing, and says so =="
"$PY" "$S/cancel_sweep.py" --sweep-dir "$LSW" --substrate kubectl \
    --scale-test-base "$LB" --expect-cancelled > "$WORK/lcan.json" 2>/dev/null
check "cancel fails on contention" "$(jqf "$WORK/lcan.json" success)" "False"
check "...reporting nothing was cancelled" "$(jqf "$WORK/lcan.json" cancelled)" "False"
check "...and flagging the lock" "$(jqf "$WORK/lcan.json" lock_contended)" "True"

echo
echo "== resume under contention is CONTENDED, not REFUSED =="
# A refusal means the sweep was in a state that declined a resume. Contention means the
# driver never got past the lock -- different problem, different remedy.
printf 'export ORDER_NODES_ENABLED=1\n' > "$LSW/env_used.sh"
mk_sweep "$LSW" SUCCESS FAILED
"$PY" "$S/resume_sweep.py" --sweep-dir "$LSW" --substrate kubectl \
    --scale-test-base "$LB" > "$WORK/lres.json" 2>/dev/null
check "resume fails on contention" "$(jqf "$WORK/lres.json" success)" "False"
check "...with outcome contended, not refused" "$(jqf "$WORK/lres.json" outcome)" "contended"
check "...and nothing resumed" "$(jqf "$WORK/lres.json" resumed)" "0"

echo
echo "== a genuine driver failure is still reported as one =="
# The contention detector keys on upstream's message, so an ordinary non-zero exit must
# still reach the existing diagnosis -- otherwise every failure becomes "try again later".
FB="$WORK/failbase"; mkdir -p "$FB/storage-tests/fs/kubectl"
touch "$FB/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
printf '#!/bin/bash\necho "Error: something else entirely" >&2\nexit 1\n' \
    > "$FB/storage-tests/fs/nv-elbencho-sweep.sh"
chmod +x "$FB/storage-tests/fs/nv-elbencho-sweep.sh"
LSW2="$WORK/locksweep2"; mk_sweep "$LSW2" PENDING PENDING; mkdir -p "$LSW2/kubernetes"
"$PY" "$S/poll_sweep.py" --sweep-dir "$LSW2" --substrate kubectl \
    --scale-test-base "$FB" --wait 1 --interval 1 > "$WORK/lother.json" 2>/dev/null
check "an unrelated driver failure is not called contention" \
    "$(jqf "$WORK/lother.json" lock_contended)" "False"
check "...and still reads as unknown state" "$(jqf "$WORK/lother.json" state)" "unknown"
"$PY" -c "import json,sys;e=json.load(open(sys.argv[1]))['error'];assert 'cluster access' in e" \
    "$WORK/lother.json" && ok "...keeping the access-loss diagnosis where it belongs" \
    || bad "the access-loss diagnosis was lost for real failures"

echo
echo "== a successful status is explicitly not contended =="
# Absent must not read as false-y by accident: a validation asserting lock_contended ==
# false needs the key to exist on the healthy path.
GOK="$WORK/lockok"; mk_status_stub "$GOK" SUCCESS PVC 0 0 2 COLLECT
"$PY" "$S/poll_sweep.py" --sweep-dir "$LSW2" --substrate kubectl \
    --scale-test-base "$GOK" --wait 1 --interval 1 > "$WORK/lok.json" 2>/dev/null
check "a clean poll records lock_contended=false" "$(jqf "$WORK/lok.json" lock_contended)" "False"
check "...and passes" "$(jqf "$WORK/lok.json" success)" "True"

echo
echo "== the contention message is still upstream's published contract =="
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' && ok "upstream still emits the contention message this keys on" \
    || bad "the lock contention message changed upstream -- detection is broken"
import os
root = os.environ["SSTC"]
fn = open(os.path.join(root,
    "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh")).read()
assert "another Kubernetes lifecycle operation is active" in fn
# Non-blocking is the whole reason contention surfaces as an error rather than a wait.
assert 'flock -n "$acquired_fd"' in fn
# And upstream treats the message as a contract its own tests assert on.
contract = open(os.path.join(root,
    "tests/test_elbencho_kubectl_lifecycle_contract.py")).read()
assert "another Kubernetes lifecycle operation is active" in contract
PYEOF

# =======================================================================================
# THE BATCH PATH IS THE SAME PATH. Every defect fixed on the plain kubectl sweep in rounds
# 8 and 10 was still live on the batch path a round later, because the two branches read
# --status independently. They now share `status_view`, and these tests pin the shared
# behaviour on the branch that kept being forgotten.
# =======================================================================================
echo
echo "== the batch path reads the SAME status view as a plain sweep =="
# A REALISTIC sealed-kubectl-batch stub. Upstream emits BARE keys from both emitters
# (_nv-elbencho-kubectl-functions.sh:722-729, lib/_batch_functions.sh:526-530) and only
# ever ONE progress view, named by EXECUTION_SCOPE. The older fixture above uses
# STORAGE_SCALE_TEST_BATCH_* prefixes, which upstream emits only on --batch/--append
# prepare output -- never on --status.
mk_real_batch_stub() {   # $1=base  $2=STATE  $3=NEXT_ACTION  $4=PROGRESS_SOURCE  $5=SUCCEEDED
    mkdir -p "$1/storage-tests/fs/kubectl"
    touch "$1/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    # Computed HERE, not in the stub: `$((...))` inside a single-quoted printf is literal
    # text, and the resulting non-integer is correctly refused by adopt_driver_counts'
    # all-or-nothing guard -- which is how this fixture bug first showed up.
    local _pending=$(( 4 - $5 ))
    cat > "$1/storage-tests/fs/nv-elbencho-sweep.sh" <<RBSTUB
#!/bin/bash
if [[ -n "\$CONTEND" ]]; then
  echo "error: another Kubernetes lifecycle operation is active" >&2
  exit 1
fi
printf 'BATCH=SEALED\nATTEMPT=att-001\nSTATE=$2\n'
printf 'EXECUTION_SCOPE=CURRENT_ATTEMPT\nPROGRESS_SOURCE=$4\n'
printf 'EXECUTIONS_TOTAL=4\nEXECUTIONS_PENDING=$_pending\nEXECUTIONS_RUNNING=0\n'
printf 'EXECUTIONS_SUCCEEDED=$5\nEXECUTIONS_FAILED=0\n'
printf 'RESULT_COLLECTION=PENDING\nNEXT_ACTION=$3\n'
exit 0
RBSTUB
    chmod +x "$1/storage-tests/fs/nv-elbencho-sweep.sh"
}

# The real kubectl shape: cells PENDING locally until --collect, driver reading the PVC.
mk_sweep "$WORK/rbatch" PENDING PENDING PENDING PENDING
for m in batch-manifest.tsv batch-profile.tsv batch-sealed.sha256 batch-sealed-environment.sha256; do
    : > "$WORK/rbatch/$m"
done

RB1="$WORK/rb1"; mk_real_batch_stub "$RB1" RUNNING WAIT PVC 3
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/rbatch" --substrate kubectl \
    --scale-test-base "$RB1" --allow-incomplete > "$WORK/rb1.json" 2>/dev/null
# THE bug: a RUNNING batch has NEXT_ACTION=WAIT, so the old `next_action != COLLECT`
# heuristic called the stale local tally authoritative for the whole life of the run.
check "a running batch adopts the driver's PVC counts" \
    "$(jqf "$WORK/rb1.json" succeeded)" "3"
check "...rather than the local sentinels" "$(jqf "$WORK/rb1.json" local_succeeded)" "0"
check "...and keeps the local tally visible" "$(jqf "$WORK/rb1.json" local_pending)" "4"
check "...flagged authoritative because PROGRESS_SOURCE said PVC" \
    "$(jqf "$WORK/rb1.json" counts_authoritative)" "True"
check "...recording where the counts came from" \
    "$(jqf "$WORK/rb1.json" progress_source)" "PVC"

# PROGRESS_SOURCE=LOCAL is upstream saying "these came from the published manifest".
# That must NOT be adopted while cells are outstanding, and must not be called authoritative.
RB2="$WORK/rb2"; mk_real_batch_stub "$RB2" RUNNING WAIT LOCAL 3
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/rbatch" --substrate kubectl \
    --scale-test-base "$RB2" --allow-incomplete > "$WORK/rb2.json" 2>/dev/null
check "a LOCAL progress source is not adopted" "$(jqf "$WORK/rb2.json" succeeded)" "0"
check "...and is not called authoritative" \
    "$(jqf "$WORK/rb2.json" counts_authoritative)" "False"
"$PY" -c "import json,sys;n=json.load(open(sys.argv[1]))['counts_note'];assert 'PROGRESS_SOURCE=PVC' in n" \
    "$WORK/rb2.json" && ok "...saying which fact was missing" \
    || bad "the batch counts_note does not name PROGRESS_SOURCE"

echo
echo "== a contended batch poll is contention, not a stalled sweep =="
# Before the fix this fell through to assess()'s tally verdict: it waited out the FULL
# --wait deadline and then advised --allow-incomplete, over a local file lock. And
# `lock_contended` was absent entirely, so the gate's `lock_contended is False` check -- which
# reads poll_sweep for batch and plain runs alike -- reported a missing field instead of the
# diagnosis, hiding it behind a bookkeeping complaint.
CONTEND=1 "$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/rbatch" --substrate kubectl \
    --scale-test-base "$RB1" > "$WORK/rbc.json" 2>/dev/null
check "a contended batch reports lock_contended" "$(jqf "$WORK/rbc.json" lock_contended)" "True"
check "...as its own state" "$(jqf "$WORK/rbc.json" state)" "lock-contended"
check "...which is not a pass" "$(jqf "$WORK/rbc.json" success)" "False"
check "...and not done" "$(jqf "$WORK/rbc.json" done)" "False"
check "...with counts explicitly not authoritative" \
    "$(jqf "$WORK/rbc.json" counts_authoritative)" "False"
"$PY" - "$WORK/rbc.json" <<'PYEOF' && ok "...naming the lock, not credentials or --allow-incomplete" \
    || bad "the contended-batch error gives the wrong advice"
import json, sys
e = json.load(open(sys.argv[1]))["error"]
assert "lifecycle.lock" in e, e
assert "re-authenticating will not help" in e, e
assert "allow-incomplete" not in e, e
PYEOF

# A clean batch poll must say so POSITIVELY -- absent is not False to a FieldExistsCheck.
check "a clean batch poll records lock_contended=false" \
    "$(jqf "$WORK/rb1.json" lock_contended)" "False"

echo
echo "== the batch and plain paths agree on an identical status view =="
# The regression that produced both bugs above was divergence, so assert equivalence
# directly: same driver output, batch dir vs plain dir, same view fields.
mk_sweep "$WORK/pbatch" PENDING PENDING PENDING PENDING
"$PY" "$S/poll_sweep.py" --sweep-dir "$WORK/pbatch" --substrate kubectl \
    --scale-test-base "$RB1" --allow-incomplete > "$WORK/pb.json" 2>/dev/null
"$PY" - "$WORK/rb1.json" "$WORK/pb.json" <<'PYEOF' && ok "batch and plain publish the same progress view" \
    || bad "the batch and plain status views have drifted apart again"
import json, sys
b = json.load(open(sys.argv[1])); p = json.load(open(sys.argv[2]))
keys = ["progress_source", "execution_scope", "counts_authoritative", "lock_contended",
        "succeeded", "pending", "total", "kubectl_executions_succeeded"]
for k in keys:
    assert b.get(k) == p.get(k), f"{k}: batch={b.get(k)!r} plain={p.get(k)!r}"
PYEOF

echo
echo "== upstream really does emit bare, single-view status keys =="
# The reason the two paths can share one parser. If upstream ever introduces a second
# simultaneous view or a key namespace, this fails and the sharing must be revisited.
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' && ok "both emitters print bare EXECUTIONS_* keys" \
    || bad "upstream's status key shape changed -- revisit status_view()"
import os
root = os.environ["SSTC"]
kube = open(os.path.join(root, "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh")).read()
batch = open(os.path.join(root, "lib/_batch_functions.sh")).read()
assert "printf 'EXECUTIONS_SUCCEEDED=%s\\nEXECUTIONS_FAILED=%s\\n'" in kube
assert "printf 'EXECUTIONS_SUCCEEDED=%s\\nEXECUTIONS_FAILED=%s\\n'" in batch
# EXECUTION_SCOPE is the discriminator both views publish.
assert "EXECUTION_SCOPE=CURRENT_ATTEMPT" in kube
assert "EXECUTION_SCOPE=BATCH" in batch
# And no --status emitter uses the STORAGE_SCALE_TEST_ namespace.
assert "STORAGE_SCALE_TEST_BATCH_STATE" not in batch
PYEOF
SSTC="$SST_CHECKOUT" "$PY" - <<'PYEOF' && ok "upstream still documents a single progress view" \
    || bad "the single-view contract changed -- the shared parser assumes it"
import os
doc = open(os.path.join(os.environ["SSTC"],
                        "docs/KUBERNETES_ELBENCHO_LIFECYCLE.md")).read()
assert "Only one progress view is emitted" in doc
PYEOF

# =======================================================================================
# PHASE WIRING: the step before `collect` must never break the phase.
# =======================================================================================
echo
echo "== verify pairs summary blocks to directories by name =="
# find_group_dirs returns group order (0001, 0002, ...); summarize-elbencho.py RE-SORTS
# its argv by basename (_sort_key, :471). When datestamps do not ascend with group number
# a positional zip pairs a block from one group with a directory from another, and
# expected_cells' no-banner fallback then counts the wrong directory's .out files.
# A FRESH base, deliberately not $VB: the earlier verify section symlinks $VB/utils at
# the REAL upstream checkout so it can run the genuine summarizer, and writing a stub into
# $VB/utils therefore writes THROUGH the symlink and overwrites upstream's own
# summarize-elbencho.py. (It did, once, while this test was being written.) Anything that
# installs a stub must own a plain directory of its own.
VPB="$WORK/vpairbase"; mkdir -p "$VPB/utils"
VR="$WORK/vpairroot"
mkdir -p "$VR/groups/0001/elbencho-20260101Z000000" "$VR/groups/0002/elbencho-20250101Z000000"
: > "$VR/batch-manifest.tsv"
# Group 0001 is COMPLETE and holds 3 outputs; group 0002 is INCOMPLETE with a banner.
# Their datestamps are deliberately INVERTED, so basename order is the reverse of group order.
for i in 1 2 3; do
    : > "$VR/groups/0001/elbencho-20260101Z000000/elbencho-1M-c_1-s_1-d_1_20260101Z00000$i.out"
done
: > "$VR/groups/0002/elbencho-20250101Z000000/elbencho-1M-c_1-s_1-d_1_20250101Z000001.out"
# A summarizer stub that emits blocks in ITS sort order (basename), like the real one.
# A PYTHON stub -- verify_sweep invokes the summarizer as `python3 <script>`, which is
# also how upstream ships it.
cat > "$VPB/utils/summarize-elbencho.py" <<'VSTUB'
import sys
# Emitted in the summarizer's OWN order: sorted by basename (_sort_key, :471), which here
# is the reverse of the group order verify_sweep passes in.
print("*** INCOMPLETE RUN *** missing 7/8 glob elbencho-20250101Z000000 dur: 1s nodes: 1")
print("    detail line")
print("elbencho-20260101Z000000 dur: 1s nodes: 1")
print("    detail line")
sys.exit(0)
VSTUB
"$PY" "$S/verify_sweep.py" --sweep-dir "$VR" --scale-test-base "$VPB" \
    --allow-incomplete > "$WORK/vp.json" 2>/dev/null
check "blocks are matched to directories by name" "$(jqf "$WORK/vp.json" pairing)" "by-name"
# 0002 carries the banner -> its configured total is 8. 0001 has no banner -> its 3 files
# ARE its matrix. Correct total is 11. A positional zip pairs the banner with 0001 and
# counts 0002's single file for the complete block: 8 + 1 = 9.
check "...so the configured matrix is summed from the right directories" \
    "$(jqf "$WORK/vp.json" outputs_expected)" "11"
check "...and the files actually present are counted across both" \
    "$(jqf "$WORK/vp.json" outputs_present)" "4"

# =======================================================================================
# TARGETS: which claims a sweep actually wrote to, and at what weight
# =======================================================================================
echo
echo "== targets: observed claim weights are compared against the configured ones =="
# Fixture shape is copied from a real v2.0.0 two-claim run: env_used.yaml
# records TEST_DIRS and the resolved mounts, and elbencho's COMMAND LINE carries EVERY
# target of the cell on one line -- so dropping a claim means editing the line, not
# deleting it.
mk_targets() {  # $1=dir  $2=COMMAND LINE target list (space separated)
    mkdir -p "$1"
    cat > "$1/env_used.yaml" <<'TEOF'
EXECUTION_SUBSTRATE: "kubectl"
KUBECTL_MAPPED_TEST_DIRS:
  "/mnt/storage-scale-test/claim-b/scale-test": 1
  "/mnt/storage-scale-test/claim-a/scale-test": 2

TEST_DIRS:
  "claim-a:scale-test": 2
  "claim-b:scale-test": 1

ELBENCHO_SCALE_IO_SIZES: ["1M"]
TEOF
    local args="" t
    for t in $2; do args+=" \"/mnt/storage-scale-test/$t-20261020Z180808-e0001\""; done
    printf 'COMMAND LINE: "/usr/bin/elbencho" "--write" "--block=1M"%s\n' "$args" > "$1/c.out"
    # The same slots again for the READ phase: a repeat must not inflate the weight.
    printf 'COMMAND LINE: "/usr/bin/elbencho" "--read" "--block=1M"%s\n' "$args" >> "$1/c.out"
}
G1=claim-a/scale-test/elbencho-sweep-target-1
G2=claim-a/scale-test/elbencho-sweep-target-2
M1=claim-b/scale-test/elbencho-sweep-target-1

mk_targets "$WORK/tg_ok" "$G1 $G2 $M1"
"$PY" "$S/targets_sweep.py" --sweep-dir "$WORK/tg_ok" --substrate kubectl > "$WORK/tg_ok.json" 2>/dev/null
check "a 2:1 two-claim sweep matches its configured weights" "$(jqf "$WORK/tg_ok.json" weights_match)" "True"
check "...and counts both claims as exercised" "$(jqf "$WORK/tg_ok.json" claims_exercised)" "2"
"$PY" - "$WORK/tg_ok.json" <<'PYT' && ok "...counting a slot repeated across phases once" \
    || bad "a target slot repeated across phases inflated the observed weight"
import json, sys
t = json.load(open(sys.argv[1]))["targets"]
assert t["claim-a:scale-test"]["weight_observed"] == 2, t
assert t["claim-b:scale-test"]["weight_observed"] == 1, t
PYT

# The failure this step exists for: every cell healthy, one claim never touched.
mk_targets "$WORK/tg_skip" "$G1 $G2"
"$PY" "$S/targets_sweep.py" --sweep-dir "$WORK/tg_skip" --substrate kubectl > "$WORK/tg_skip.json" 2>/dev/null
check "a configured claim that was never written to breaks weights_match" \
    "$(jqf "$WORK/tg_skip.json" weights_match)" "False"
check "...and is named" "$(jqf "$WORK/tg_skip.json" unexercised_claims)" "['claim-b:scale-test']"
check "...while the step itself still succeeds (it reports; the config judges)" \
    "$(jqf "$WORK/tg_skip.json" success)" "True"

mk_targets "$WORK/tg_under" "$G1 $M1"
"$PY" "$S/targets_sweep.py" --sweep-dir "$WORK/tg_under" --substrate kubectl > "$WORK/tg_under.json" 2>/dev/null
check "a claim run below its configured weight breaks weights_match" \
    "$(jqf "$WORK/tg_under.json" weights_match)" "False"

mk_targets "$WORK/tg_stray" "$G1 $G2 $M1 other-pvc/scale-test/elbencho-sweep-target-1"
"$PY" "$S/targets_sweep.py" --sweep-dir "$WORK/tg_stray" --substrate kubectl > "$WORK/tg_stray.json" 2>/dev/null
check "a target no configured claim accounts for breaks weights_match" \
    "$(jqf "$WORK/tg_stray.json" weights_match)" "False"

mkdir -p "$WORK/tg_noout"; cp "$WORK/tg_ok/env_used.yaml" "$WORK/tg_noout/"
"$PY" "$S/targets_sweep.py" --sweep-dir "$WORK/tg_noout" --substrate kubectl > "$WORK/tg_noout.json" 2>/dev/null \
    && bad "targets passed with no .out files to observe" \
    || check "no .out files fails the step instead of reporting zero claims" "$(jqf "$WORK/tg_noout.json" success)" "False"

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]

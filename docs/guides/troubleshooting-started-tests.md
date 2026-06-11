# Troubleshooting: Test Runs Stuck in STARTED

This guide helps ISV Lab partners diagnose and fix test runs that never reach a terminal state (SUCCESS or FAILED) and remain **STARTED** in the [ISV Validation Labs portal](https://public.ncp-isv-validation-labs.nvidia.com/).

## Root cause (single RCA)

**A test run moves to SUCCESS or FAILED only when the ISV Lab Service API receives an "update" request for that run.** If that update is never sent, the run stays STARTED.

The update is sent when:

1. **Single-command flow (recommended)**
   You run:

   ```bash
   isvctl test run -f configs/suites/k8s.yaml --lab-id ${ISV_LAB_ID}
   ```

   The same process that creates the test run (STARTED) later calls the API again to set SUCCESS or FAILED after all phases (setup -> test -> teardown) finish. If this process exits normally, the run is updated.

2. **Split flow (create + script + update)**
   You use a CI/CD or script that:
   - Creates the run (e.g. `isvreporter create` or a step that calls the create API), then
   - Runs tests in a **separate** step/container/job, then
   - Is supposed to run `isvreporter update` (or equivalent) in an `after_script` / cleanup step.

If the step that is supposed to call **update** never runs, the run stays STARTED.

## Why the update might never run

| Scenario | What happens | Result |
|----------|-----------------------|--------|
| Process killed | Job/container timeout, OOM, or manual kill before the code that calls `update` runs. | STARTED |
| Process hangs | A validation or step never returns (e.g. waiting on a resource, deadlock). The process never reaches the line that calls `update`. | STARTED |
| Split workflow | "Create" runs in one job/container; tests run in another. The second job never calls `update` (or doesn't have the test run ID). | STARTED |
| Wrong working directory | Test run ID is saved to `_output/testrun_id.txt` in the CWD of the process that created the run. If the process that should call `update` runs in a different directory or container and doesn't pass `--test-run-id`, it can't find the ID and may skip update. | STARTED |
| after_script not run | In CI, if the main script is cancelled, times out, or fails in a way that skips `after_script`, the update step never runs. | STARTED |

Runs that show **Executed by: isvctl** and reach SUCCESS/FAILED are using the single-command flow where the same process both creates and updates the run. Runs that show a user email (e.g. **Executed by: <user@partner.com>**) and stay STARTED are often using a split or custom flow where the update step is missing or never executed.

## Recommended fix: single-command flow

Use one command so that create and update happen in the same process:

```bash
# Required env (see ISV Lab Validation Guide)
export ISV_LAB_ID="35"  # Your assigned lab ID
export ISV_SERVICE_ENDPOINT="..."  # From NVIDIA
export ISV_SSA_ISSUER="..."
export ISV_CLIENT_ID="..."
export ISV_CLIENT_SECRET="..."
export NGC_API_KEY="..."  # optional, for NIM tests

# Kubernetes
isvctl test run -f configs/suites/k8s.yaml --lab-id ${ISV_LAB_ID}

# Slurm
isvctl test run -f configs/suites/slurm.yaml --lab-id ${ISV_LAB_ID}
```

Do **not** use `--no-upload` if you want results reported. The same process will:

1. Create the test run (portal shows STARTED).
2. Run setup -> test -> teardown.
3. Call the API again with SUCCESS or FAILED (and optional JUnit/log).

If the process is killed or hangs before step 3, the run will still stay STARTED; in that case fix timeouts/hangs (see below).

## If you must use a split flow (create -> script -> update)

Ensure the step that runs **after** the tests **always** calls update, even when the test step fails or is cancelled:

1. **Pass the test run ID explicitly** if the update runs in a different working directory or container:

   ```bash
   isvctl report update --lab-id ${ISV_LAB_ID} --test-run-id <ID> --status FAILED
   ```

   Get `<ID>` from the create step output or from `_output/testrun_id.txt` in the create step and pass it (e.g. artifact or variable) to the update step.

2. **Use a trap so update runs on exit** (e.g. in a shell wrapper):

   ```bash
   TRAP_TEST_RUN_ID=""
   trap 'if [ -n "$TRAP_TEST_RUN_ID" ]; then isvctl report update --lab-id $ISV_LAB_ID --test-run-id "$TRAP_TEST_RUN_ID" --status FAILED; fi' EXIT
   # create run, set TRAP_TEST_RUN_ID, run tests, then update with real status in the normal path
   ```

   Then in the normal path (when tests finish) call update with SUCCESS or FAILED; on EXIT (cancel, timeout, crash) the trap can at least send FAILED so the run doesn't stay STARTED.

3. **CI: Prefer a single job** that runs `isvctl test run ... --lab-id ...` so create and update are in one process. If you use separate jobs, the second job must receive the test run ID and call update in a block that runs even on failure (e.g. `after_script` that always runs, or a dedicated "report status" job that gets the ID from the first job).

## If the process hangs or is killed

- **Timeouts**
  Increase job/container timeout so that the full suite (including setup/teardown) can finish. Long-running workload tests (e.g. NIM) can take 15-30+ minutes.

- **Hangs**
  Check which phase hangs (setup, test, or teardown) from logs. Common causes: waiting on a cluster resource, Slurm job that never completes, or a validation that blocks. Fix the stub or validation, or exclude slow tests during initial runs. For one-off negative selection, use the underlying pytest marker expression:

  ```bash
  isvctl test run -f configs/suites/k8s.yaml --lab-id ${ISV_LAB_ID} -- -m "not workload"
  ```

- **OOM / kill**
  Ensure the runner has enough memory and that no step leaks memory. Once the process is stable and completes, the same single-command flow will send the update.

## Cleaning up already-STARTED runs (optional)

For runs that are already stuck in STARTED, you can set a terminal status manually so the portal no longer shows them as in progress:

```bash
# Set to FAILED (use if the run did not complete successfully or is abandoned)
isvctl report update --lab-id <LAB_ID> --test-run-id <TEST_RUN_ID> --status FAILED

# Set to SUCCESS only if you independently know the run completed successfully
isvctl report update --lab-id <LAB_ID> --test-run-id <TEST_RUN_ID> --status SUCCESS
```

Get `<TEST_RUN_ID>` from the portal (e.g. 66, 67, 68, 71, 72, 73). `<LAB_ID>` is your assigned lab ID.

## Summary

| Goal | Action |
|------|--------|
| Avoid new STARTED runs | Use **one** `isvctl test run -f configs/... --lab-id $ISV_LAB_ID` (no split create/update). |
| Split flow | Ensure the step that runs after tests **always** calls `report update` (trap, after_script, or explicit job) and has the test run ID (e.g. `--test-run-id`). |
| Process exits early | Fix timeouts, OOM, or hanging validations so the same process can reach the update call. |
| Existing STARTED runs | Use `isvctl report update --lab-id ... --test-run-id ... --status FAILED` (or SUCCESS) to close them. |

For more detail on installation and running tests, see the [ISV Lab Validation Guide](https://apps.nvidia.com/PID/ContentLibraries/Detail?id=1146950) and the in-repo [Getting Started](../getting-started.md) and [Configuration](configuration.md) guides.

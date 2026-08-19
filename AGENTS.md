# AGENTS.md

General agent behavior lives in `.cursor/rules/karpathy-guidelines.mdc` (always applied).
Python conventions live in `.cursor/rules/python-standards.mdc` (auto-applied to `*.py`).
This file documents project-specific context an agent can't grep for.

## Project Overview

NVIDIA AI Cloud Validation Suite - validation and management tools for NVIDIA ISV Lab GPU
cluster environments. Monorepo with three Python packages managed as a uv workspace:

- **isvctl** - CLI controller for cluster lifecycle (setup → test → teardown)
- **isvtest** - Validation framework engine (pytest-based with custom discovery)
- **isvreporter** - Test results reporter for ISV Lab Service API

## Common Commands

```bash
uv sync                # install workspace
make build             # build all packages
make test              # run tests
make demo-test         # run all my-isv configs end-to-end (ISVCTL_DEMO_MODE=1, ~10s, no cloud)
make lint              # ruff
make format            # ruff format
make plan              # render docs/test-plan.yaml to AsciiDoc + interactive HTML
uv run isvctl test run -f isvctl/configs/suites/k8s.yaml          # canonical invocation
uv run isvctl test run -f config.yaml -- -v -s -k "test_name"     # forward pytest args
```

## Step-Based Execution Model

The framework separates *doing* from *checking*:

```text
Config (YAML) → Script (any language) → JSON output → Validations (assertions)
```

1. Scripts (Python, Bash, ...) perform cloud operations and print structured JSON to stdout.
2. Validations are simple assertions over that JSON - no cloud SDK code in validations.
3. Validations reference step output via Jinja2: `"{{steps.create_network.vpc_id}}"`,
   `"{{region}}"`. The orchestrator warns when a template references a missing step or
   field (catches `ChainableUndefined` silent fallbacks).

### JSON contract discipline

- Test stdout JSON is the provider-neutral contract between scripts and
  validations. Use ISV-agnostic names and avoid AWS-specific resource concepts
  unless a validation or later step consumes them.
- Keep output minimal: `success`, `platform`, `test_name`, and
  `tests.<check>.passed/message/probes` are usually enough. Omit IDs, regions,
  endpoint inventories, and other fields that do not affect behavior.
- Failure/skip diagnostics are allowed, but keep them concise and generic:
  top-level `error`/`error_type`, `tests.<check>.error`, `skip_reason`, or
  `cleanup_errors`. Avoid raw provider responses and resource dumps.

### Lifecycle invariants (non-obvious)

- Phases run in order: `setup → test → teardown`.
- **Teardown runs after setup/test failures by default** so cloud resources get
  cleaned up - but it is skipped when `teardown_on_failure` is disabled, or when
  setup was requested in the same invocation but no setup steps actually ran.
- **Teardown is best-effort** - one failing teardown step does not block the others.
- **Standalone teardown** (`isvctl test run -f config.yaml --phase teardown`) runs
  unconditionally - useful after a previous run with `AWS_SKIP_TEARDOWN`.
- **A config with no `commands:` runs validations only** against a system that is
  already up. Only the test phase runs. Live probes run directly; validations
  wired to provider step output still skip as `step_not_configured`. Declaring
  commands is per file - some canonical suites carry a scaffold fixture, others
  none - so read the file rather than inferring from the kind of suite.
- Multiple `-f` configs merge; later files override earlier ones.

## Architecture

### isvctl - orchestration

Entry point: `isvctl/src/isvctl/main.py` (Typer).

- `cli/` - subcommands (`test`, `deploy`, `clean`, `docs`, `report`)
- `orchestrator/` - `loop.py` (phase loop), `step_executor.py` (step + validation
  execution, supports `best_effort` mode), `commands.py` (legacy command model),
  `process.py` (shared subprocess and process-group timeout handling), `context.py`
  (Jinja2 with missing-reference warnings)
- `config/` - `schema.py` (Pydantic), `output_schemas.py` (per-step JSON schemas),
  `merger.py` (multi-file merge)
- `remote/` - `ssh.py` (with jumphost), `archive.py`, `transfer.py` (SCP via jumphost proxy)
- `cleaner/` - resource cleanup

### isvtest - validation framework

Entry point: `isvtest/src/isvtest/main.py`.

`run_validations_via_pytest()` is the bridge isvctl calls. It transforms validation
configs to pytest format, runs native pytest, and returns rich in-memory results
(category, message) alongside the exit code.

- `core/validation.py` - `BaseValidation` abstract class
- `core/discovery.py` - finds `BaseValidation` subclasses and ReFrame tests
- `core/runners.py` - `LocalRunner`, `SlurmRunner`, ...
- `core/{k8s,slurm,nvidia,ngc,workload}.py` - domain helpers

Validation classes live in `isvtest/src/isvtest/validations/` grouped by domain
(`generic.py`, `cluster.py`, `instance.py`, `network.py`, `iam.py`, `security.py`,
`host.py`, `k8s_*.py`, `slurm_*.py`, `bm_*.py`). Each subclass is auto-discovered.
Filtering labels live on the YAML wiring (`labels: [...]` per check), not on the
class; the catalog, pytest marks, `isvctl docs`, and the orchestrator's
include/exclude-label filtering all read them from there. Declare labels ONLY in
`isvctl/configs/suites/*.yaml` - never add `labels:` to per-check wiring under
`isvctl/configs/providers/**`; provider configs inherit labels from the suites
they import (top-level `exclude.labels:` filtering blocks are fine). Sole
exception: the single-node local providers
`isvctl/configs/providers/{k3s,microk8s,minikube}.yaml`, which wire host-level
checks that exist in no suite. Those checks are local-dev tools no ISV runs, so
they are deliberately absent from the catalog (built from `suites/` only) and
therefore from `released_tests.json` - run those three configs with
`ISVTEST_INCLUDE_UNRELEASED=1` or they skip as `unreleased`.

Workloads (`isvtest/src/isvtest/workloads/`) are long-running tests (NIM, NCCL,
stress) labelled `("workload", "slow", ...)` with manifests and helper scripts
colocated.

Test config loaded from YAML/JSON via `config/loader.py`. Global fixtures in
`tests/conftest.py`. `tests/test_validations.py` dynamically generates pytest tests
from `BaseValidation` classes.

### isvreporter - results upload

Entry point: `isvreporter/src/isvreporter/main.py` (Typer).

- `client.py` - ISV Lab Service API client
- `auth.py` - OAuth2
- `junit_parser.py` - pytest JUnit XML parsing
- `platform.py` - platform detection

### Remote deploy flow

`isvctl deploy run` → tarball repo (`remote/archive.py`) → SCP through optional
jumphost (`remote/transfer.py`) → `install.sh` on target → `isvctl test run` with
forwarded env vars → optional isvreporter upload.

## Files agents must not edit

- `isvtest/src/isvtest/released_tests.json` - release-gating manifest owned
  by the release process (bumped via `chore: update package versions`). New
  checks ship unreleased and land here in a separate release commit, not in
  feature PRs. To exercise an unreleased check end-to-end against a config,
  run with `ISVTEST_INCLUDE_UNRELEASED=1` (the orchestrator otherwise logs
  `Skipping unreleased validation '<Name>'` and the new check is a no-op).
  On a `releases/X.Y.x` maintenance branch this manifest is regenerated from
  that branch's own catalog, so it legitimately differs from `main`'s - do not
  "reconcile" it by copying `main`'s version.

## Directory Layout

- Workspace root `pyproject.toml` defines members; each package has its own
  `pyproject.toml`; all source under `src/`.
- `isvctl/configs/suites/` - provider-agnostic test contracts. Discovery is
  recursive, so related domain suites may be grouped in a subdirectory; YAML
  filename stems must remain globally unique.
- `isvctl/configs/providers/<name>/` - one folder per provider (`aws/`, `my-isv/`, ...):
  - `config/` - YAML wiring (imports a suite, supplies commands)
  - `scripts/` - executable scripts (Python/Bash) that do the work, organized by
    domain (`network/`, `vm/`, `iam/`, `k8s/`, ...)
  - `scripts/common/` - provider-local Python helpers, imported via a single
    `sys.path.insert(0, Path(__file__).resolve().parents[1])` per script
- `isvctl/configs/providers/shared/` - cross-provider scripts (`deploy_nim.py`,
  `teardown_nim.py`).
- `isvctl/schemas/` - JSON Schema files.

### Provider notes

- **`my-isv/`** - scaffold for ISVs to copy. Each script has a TODO block and a
  `DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"` gate: real run returns
  `"Not implemented - ..."`; demo mode returns dummy success. `make demo-test` sets
  `ISVCTL_DEMO_MODE=1`. See `providers/my-isv/scripts/README.md`.
- **`aws/`** - fully implemented reference using boto3/Terraform.
  `aws/scripts/common/` provides `ec2`, `errors` (with `delete_with_retry`),
  `ssh_utils.wait_for_ssh`, `serial_console`, `vpc`.

### Network Operator / Kubernetes Launch Kit

- All provider-owned Launch Kit files live under
  `isvctl/configs/providers/k8s-launch-kit/`: generic and Network Operator YAML
  in `config/`, executable transport in `scripts/`, and provider documentation
  in `README.md`.
- `isvctl/configs/providers/k8s-launch-kit/config/provider.yaml` is the generic provider. Its
  public API mirrors the CLI: `prepare`, `verify`, Kubernetes preflight,
  `discover`, `generate`, `deploy`, `validate`, and `clean`. Workflow settings are raw
  argument arrays; do not model or duplicate Launch Kit flags/defaults here.
  The single file-level input, `user_config`, points to a complete Launch Kit
  configuration. Discovery copies it to `<working_dir>/user-config.yaml` and
  writes the resolved result to `<working_dir>/cluster-config.yaml`, preserving
  the source file and the default paths used by subsequent commands.
  Its `validate` step uses `timeout: null` so l8k owns the automatically
  calculated or user-supplied matrix deadline; all other workflow steps retain
  finite outer isvctl watchdogs. Any provider may use a null `StepConfig`
  timeout when its invoked command owns a bounded deadline.
- Launch Kit-specific transport code belongs under
  `isvctl/configs/providers/k8s-launch-kit/`, not `providers/shared/` (which is
  reserved for scripts reused by unrelated providers). Production code lives
  in `scripts/`. Executable mocks and pinned fixtures are test-only and live in
  `isvctl/tests/providers/k8s_launch_kit/fixtures/`; product configuration must
  never reference them.
- `prepare` supports `verify` and explicit `install` modes. Install mode
  downloads and records the official Launch Kit installer, then delegates
  archive selection, checksum verification, and installation to it. Both modes
  verify `l8k version --output json` and `l8k schema`. The configured string
  environment is shared by install, verification, preflight, and workflows.
- The Kubernetes preflight is mandatory before each normal test use case. It
  derives a single explicit kubeconfig from the raw l8k arguments (and rejects
  conflicts), verifies API access, requires a non-empty node inventory, and
  requires at least one Ready node. A failure stops the remaining steps in that
  workflow/use case. An explicit teardown-only recovery intentionally invokes
  `l8k clean` directly because cleanup must not depend on a passing test
  prerequisite.
- `isvctl/configs/suites/k8s-launch-kit/network-operator.yaml` owns only catalog wiring and
  interpretation for the globally selectable PRD checks. Each check binds to
  the real step that produced its evidence.
- `isvctl/configs/suites/k8s-launch-kit/network-operator-use-cases.yaml` composes those shared
  check classes into six concrete end-to-end tests: RoCE and InfiniBand across
  SR-IOV, RDMA Shared, and host-device modes. Include only checks applicable to
  a use case; do not run all checks and hide mismatches as interleaved skips.
- `isvctl/configs/providers/k8s-launch-kit/config/network-operator.yaml` is the
  production six-use-case configuration. It defaults to real `l8k` and
  `kubectl`, executes each supported fabric/deployment combination as a named
  custom phase, and gives every use case isolated working and evidence
  directories. Its fabric/deployment arguments define test identity; Launch
  Kit continues to own runtime defaults and users extend raw argv in overlays.
  A global `user_config` is staged independently for each selected use case;
  when it is set, raw discovery arguments cannot also select user/save config paths.
  Keep default config/deployment path flags out of all grouped phase arguments;
  overlays may add them only when intentionally overriding Launch Kit's paths.
- Mock-backed coverage loads that same production YAML and injects test-owned
  executables only in `isvctl/tests/providers/k8s_launch_kit/test_provider.py`.
  Result-check tests live under `isvtest/tests/k8s_launch_kit/`.
- Independent use-case phases are listed in `continue_after_failure` so a failed
  case does not suppress later evidence. The failed phase still fails the final
  run after its linked teardown succeeds. Never use that option for shared
  setup or dependent phases; a failed finalizer always blocks later phases.
- `StepConfig.finalizer_for` links cleanup to a mutating target. Provider
  cleanup belongs in `phase: teardown`; the orchestrator runs it immediately
  after the target phase validations and reports `<target-phase>-teardown`,
  including for `--phase test`. It only activates when the target process
  started, while `--phase teardown` runs it unconditionally as recovery. Use
  this instead of unconditional cleanup when a preflight failure must not
  delete pre-existing state. Schema validation requires matching capability
  and validation-selection gates.
- Mutating steps associated with a selectable test declare
  `requires_selected_validations`. The gate applies release, capability, label,
  and suite exclusions before command execution. It is also the reporting
  ownership edge: a failed selected step makes each named validation a
  `step_failed` error in structured results and JUnit rather than allowing a
  later missing-output skip. Keep
  `requires_available_validations` for release-only gating; pytest `-k`/`-m`
  selection remains too late to prune lifecycle commands.
- `CompositeCheck` predates the Launch Kit work and is framework machinery for
  `compose:` entries. It now forwards member probes as `MemberName/probe-name`.
  A member-level `pytest.skip` is reported as a skipped member while the
  composite continues; skipped members neither pass nor fail the parent.
  Successful validations with subtests are compacted by the shared isvctl
  renderer; do not add suite-specific output flags.
- Launch Kit areas are separate validation classes in
  `isvtest/validations/k8s_launch_kit/checks.py`; detailed probes use `report_subtest()` so
  all manifest and connectivity rows reach JUnit output before the parent fails.
- Do not invent a `selfValidation` field in l8k output. Current `discover`,
  `generate`, and `clean` emit one `ui.JSONResult`, successful standalone
  `deploy` emits no stdout, and `validate` emits a JSON stream (static state,
  connectivity matrix, then report path). The provider wraps these unmodified documents in a transport
  envelope and keeps semantic assertions in pytest. The envelope records the
  absolute command working directory so validations can resolve Launch Kit's
  relative evidence paths without rewriting its output.
- Current l8k base check selection remains ICMP, `rping`, and `ib_write_bw`.
  When `validation.gpuDirect.enabled` is true, GPUDirect DMA-BUF follows
  `ib_write_bw` and is emitted as the distinct `gpudirect_dmabuf` result family.
  Consume that family without adding an AI Cloud Validation default or a fourth
  `--validation-checks` value.
- `l8k clean` is the only supported deletion path. Each use case declares it in
  the teardown phase, linked to its deploy step; do not reproduce its
  CR/finalizer/Helm logic with kubectl. It removes the test deployment but does
  not snapshot or restore pre-existing state, so do not claim full ENT-REQ-010
  coverage until Launch Kit owns a transactional restore and verification API.
- Use `--label ethernet` or `--label infiniband` to prune the grouped run to one
  fabric's three workflows. Use `--label sriov`, `--label rdma_shared`, or
  `--label host_device` for one deployment-mode pair; labels compose to select
  one concrete use case. `-k`/marker selection still happens after lifecycle
  commands and does not prune Launch Kit workflows.
- Use `--label gpudirect` to select the six GPU-capable use-case definitions.
  The semantic member skips when Launch Kit emits no `gpudirect_dmabuf` rows;
  emitted failed rows must fail the parent with endpoint GPU evidence.
- Current reporting uploads JUnit/log/catalog only. Files under
  `_output/k8s-launch-kit` are local evidence until the reporter gains an
  explicit, redacted attachment contract.
- Design, prerequisites, unit-test boundaries, PRD mapping, and production gaps live in
  `docs/guides/k8s-launch-kit/network-operator.md`.
- The structured PRD source is
  `docs/requirements/network-operator-readiness-requirements.yaml`; keep its
  `ENT-REQ-*` edges in `docs/requirements/test-requirements-matrix.yaml` and
  regenerate committed views with `make plan`.

## Environment Variables

| Variable | Description | Used by |
| -------- | ----------- | ------- |
| `ISV_SERVICE_ENDPOINT` | ISV Lab Service API endpoint | isvreporter |
| `ISV_SSA_ISSUER` | ISV Lab Service SSA issuer | isvreporter |
| `ISV_CLIENT_ID` / `ISV_CLIENT_SECRET` | ISV Lab Service credentials | isvreporter |
| `NGC_API_KEY` | NGC key for NIM workloads / container registry | isvtest, isvctl |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` | AWS auth | AWS scripts |
| `KUBECTL` | Optional kubectl-compatible CLI prefix (POSIX `shlex` in Python, word-split in shell; overrides `K8S_PROVIDER` detection) | isvtest `get_kubectl_command`, isvctl k8s scripts |
| `ISVCTL_DEMO_MODE` | `"1"` makes `my-isv` scripts return dummy success | scripts |
| `AWS_SKIP_TEARDOWN` | Skip teardown phase (run later with `--phase teardown`) | AWS configs |
| `ISVCTL_CONFIG` / `ISVCTL_SECRETS` | Override the `config.yml` / `secrets.yml` paths (default `${XDG_CONFIG_HOME:-~/.config}/isvctl/`) | isvctl `configure`, `test`, `doctor` |

### Persisted user config

`isvctl configure` persists env vars so users don't re-`export` them per shell.
On disk they are grouped into provider-namespaced sections (`nico.api_base` ⇆
`NICO_API_BASE`); non-secret values go in `config.yml` (`0644`), secrets in
`secrets.yml` (`0600`). The variable catalog and the section/prefix mapping live
in `isvctl/config/env_catalog.py` (shared with `doctor`); the section⇆env-name
translation is a serialization detail in `config/user.py` (the public API stays
keyed by env var name). Both files carry a top-level `version:` (the on-disk
schema version, `SCHEMA_VERSION` in `config/user.py`); a missing version reads as
the initial `1`, and a version newer than the build understands is rejected with
a clear "upgrade isvctl" error rather than mis-parsed. Secret-vs-non-secret
routing reuses `redaction.is_secret_env_var`. The "Flags" group is non-persistable.
`test run`, `test validate`, and `doctor` apply both files (unless
`--no-user-config`) via `cli/common.apply_user_config`, and an already-exported
var always wins (process env > files > defaults).

## Cursor Cloud specific instructions

- **uv** is installed via `pip install uv` (the `~/.local/bin` path must be on `PATH`).
- `uv sync` from the workspace root is the only install step; it creates `.venv/` with all three packages in editable mode.
- The `uv-build` version warning during `uv sync`/`make build` is cosmetic and does not affect functionality.
- `make demo-test` is the best quick E2E smoke test — it runs all `my-isv` provider configs in demo mode (~8 s, no cloud credentials needed).
- For unit tests, `make test` runs all three packages plus `scripts/tests/`.
- For linting, `make lint` uses `uvx` to run a pinned ruff version (no global install required).
- **DCO sign-off required:** All commits must include a `Signed-off-by` line (enforced by the DCO Probot check on PRs). Use `git commit --signoff` or `git commit -s` for every commit.
- **Pre-commit checks:** Run `uvx pre-commit run -a` before committing to catch formatting, linting, SPDX header, and link issues early.
- No external services (databases, containers, clusters) are needed for local development or testing. All cloud-dependent tests require explicit credentials (`AWS_*`, `NGC_API_KEY`, etc.) and are skipped in demo mode.

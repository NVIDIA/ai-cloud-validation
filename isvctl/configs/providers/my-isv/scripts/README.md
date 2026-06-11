# my-isv scaffold

Copy-and-fill-in scripts for adding your own platform to the validation suite.

Each script ships with a TODO block and two behaviors:

- **Default run** - exits with `"Not implemented - ..."`, making it obvious where to fill in your platform's API calls.
- **Demo mode** (`ISVCTL_DEMO_MODE=1`) - returns dummy-success JSON so the whole pipeline runs end-to-end without any cloud. Used by `make demo-test`.

## The three pieces that make this work

```text
suites/*.yaml                    <- contract   (what to validate; platform-agnostic)
                  │
                  ▼ imported by
providers/my-isv/config/*.yaml   <- wiring     (which scripts implement each step)
                  │
                  ▼ invokes
providers/my-isv/scripts/<domain>/*.py  <- scaffold   (generated for you; fill in TODO blocks)
```

The `suites/` layer is the validation contract - you never modify it, you
`import:` it from your provider config. Generate a provider scaffold from this
template, then fill in the TODOs.

## Domains

| Domain | Scripts | Contract | Provider YAML | AWS reference |
|--------|---------|----------|---------------|---------------|
| `iam/` | 3 | [`suites/iam.yaml`](../../../suites/iam.yaml) | [`config/iam.yaml`](../config/iam.yaml) | [`providers/aws/scripts/iam/`](../../aws/scripts/iam/) |
| `control-plane/` | 10 | [`suites/control-plane.yaml`](../../../suites/control-plane.yaml) | [`config/control-plane.yaml`](../config/control-plane.yaml) | [`providers/aws/scripts/control-plane/`](../../aws/scripts/control-plane/) |
| `vm/` | 9 | [`suites/vm.yaml`](../../../suites/vm.yaml) | [`config/vm.yaml`](../config/vm.yaml) | [`providers/aws/scripts/vm/`](../../aws/scripts/vm/) |
| `bare_metal/` | 12 | [`suites/bare_metal.yaml`](../../../suites/bare_metal.yaml) | [`config/bare_metal.yaml`](../config/bare_metal.yaml) | [`providers/aws/scripts/bare_metal/`](../../aws/scripts/bare_metal/) |
| `network/` | 18 | [`suites/network.yaml`](../../../suites/network.yaml) | [`config/network.yaml`](../config/network.yaml) | [`providers/aws/scripts/network/`](../../aws/scripts/network/) |
| `observability/` | 1 | [`suites/observability.yaml`](../../../suites/observability.yaml) | [`config/observability.yaml`](../config/observability.yaml) | [`providers/aws/scripts/observability/`](../../aws/scripts/observability/) |
| `image-registry/` | 7 | [`suites/image-registry.yaml`](../../../suites/image-registry.yaml) | [`config/image-registry.yaml`](../config/image-registry.yaml) | [`providers/aws/scripts/image-registry/`](../../aws/scripts/image-registry/) |
| `security/` | 19 | [`suites/security.yaml`](../../../suites/security.yaml) | [`config/security.yaml`](../config/security.yaml) | [`providers/aws/scripts/security/`](../../aws/scripts/security/), [`providers/aws/scripts/capacity/`](../../aws/scripts/capacity/) |
| `k8s/` | 9 shell | [`suites/k8s.yaml`](../../../suites/k8s.yaml) | - | [`providers/aws/scripts/eks/`](../../aws/scripts/eks/) |
| `slurm/` | 2 shell | [`suites/slurm.yaml`](../../../suites/slurm.yaml) | - | - |

See [`suites/README.md`](../../../suites/README.md) for the per-step / per-field breakdown.

## Usage

**1. Preview the pipeline with no cloud (~10s):**

```bash
make demo-test
```

**2. Generate the scaffold and wiring under your provider name:**

```bash
uv run isvctl provider scaffold acme
```

Use `--dry-run` to preview the destination and next commands, or `--output-dir`
to generate outside `isvctl/configs/providers/`.

**3. Implement each script** - each has a `TODO:` block with pseudocode and a link to the AWS reference implementation.

**4. Run for real (no demo flag):**

```bash
uv run isvctl test run -f isvctl/configs/providers/acme/config/vm.yaml
```

## Private provider repositories

You do not need to contribute your provider scripts back to this repository,
Today `isvctl` runs provider configs by path: this repository supplies the
CLI, validation suites, and validation code; your private repository supplies
the provider `config/` and `scripts/` files.

Generate the scaffold directly into the private provider repository path you
intend to keep:

```bash
git clone <ISV-NCP-Validation-Suite>
cd ISV-NCP-Validation-Suite
uv sync

uv run isvctl provider scaffold acme --output-dir ../isvctl-provider-acme
```

Then initialize or connect that generated directory to your private Git repo
and implement the scripts there:

```bash
cd ../isvctl-provider-acme
git init
git remote add origin git@github.com:acme/isvctl-provider-acme.git
# implement scripts/*
```

Run the private provider from the validation suite checkout by passing its
config path:

```bash
cd ../ISV-NCP-Validation-Suite
uv run isvctl test run -f ../isvctl-provider-acme/config/vm.yaml
```

Current limitation: out-of-tree provider YAML assumes you run `isvctl` from
the validation suite checkout root. Suite imports and shared scripts use paths
relative to that checkout; provider-owned scripts stay relative to the generated
provider `config/` directory.

## Anatomy of a script

Every Python script in this tree follows the same shape - this is what you're
copying:

```python
DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

def main() -> int:
    args = parser.parse_args()
    result = {"success": False, "platform": "<domain>", ...}

    # ╔═══════════════════════════════════════════════════════╗
    # ║  TODO: Replace with your platform's API calls         ║
    # ║  Example (pseudocode):                                ║
    # ║    client = MyCloudClient(region=args.region)         ║
    # ║    ...                                                ║
    # ╚═══════════════════════════════════════════════════════╝

    if DEMO_MODE:
        # dummy-success values so make demo-test passes
        result["success"] = True
        result[...] = ...
    else:
        result["error"] = "Not implemented - replace with your platform's ... logic"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1
```

Keep the output field names in the documented contract - the validations
read specific keys (`instance_id`, `state`, `public_ip`, etc.). The AWS
reference implementation is the source of truth for what "correct" output
looks like.

For bare-metal serial console validation, the scaffold must also prove that
historical console logs are queryable for the required retention window. Emit
`console_log_queryable`, `retention_days_configured`,
`oldest_queryable_log_age_days`, `query_result_count`, and `retention_evidence`
from a real serial console log archive or retention policy query.

## See also

- [`config/`](../config/) - the YAML wiring that invokes these scripts
- [`suites/README.md`](../../../suites/README.md) - per-step breakdown and JSON field reference
- [AWS reference](../../../../../docs/references/aws.md) - working implementation of every script in this tree
- [External Validation Guide](../../../../../docs/guides/external-validation-guide.md) - writing scripts, JSON output format

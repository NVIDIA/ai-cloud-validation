# my-isv provider configs

YAML wiring that connects the provider-agnostic [validation suites](../../suites/) to
the [my-isv scaffold scripts](scripts/). Each config `import:`s a
suite and overrides the commands with paths into `scripts/`.

The scripts ship with a demo-mode fallback, so these configs run end-to-end
out of the box under `ISVCTL_DEMO_MODE=1` - that's what `make demo-test`
exercises.

## Configs

| Config | Domain | Scripts |
|--------|--------|---------|
| [`config/iam.yaml`](config/iam.yaml) | User lifecycle (create -> verify -> delete) | [`scripts/iam/`](scripts/iam/) |
| [`config/control-plane.yaml`](config/control-plane.yaml) | API health, access keys, tenant lifecycle | [`scripts/control-plane/`](scripts/control-plane/) |
| [`config/vm.yaml`](config/vm.yaml) | GPU VM lifecycle (launch -> stop/start -> reboot -> teardown) | [`scripts/vm/`](scripts/vm/) |
| [`config/bare_metal.yaml`](config/bare_metal.yaml) | BMaaS lifecycle (launch -> topology -> serial -> power-cycle -> teardown) | [`scripts/bare_metal/`](scripts/bare_metal/) |
| [`config/network.yaml`](config/network.yaml) | VPC CRUD, subnets, isolation, SG, connectivity, traffic, DDI, fabric metadata | [`scripts/network/`](scripts/network/) |
| [`config/image-registry.yaml`](config/image-registry.yaml) | Image upload, CRUD, VM launch, install config, BMaaS install | [`scripts/image-registry/`](scripts/image-registry/) |
| [`config/security.yaml`](config/security.yaml) | BMC isolation, BMC protocol security, API endpoint isolation, cert rotation, KMS options, centralized KMS, BYOK, SA credential auth | [`scripts/security/`](scripts/security/) |

## Coverage note

These configs exclude validations that require SSH into a real host
(`exclude.labels: [ssh]`) and skip steps that need real cloud APIs
(e.g. `deploy_nim`), because dummy scripts can't spin up real hosts.
Each YAML's header comment documents exactly which checks are excluded
and why - remove those exclusions as your real scripts come online.

## See also

- [`scripts/`](scripts/) - the scaffold scripts these configs invoke (start here)
- [`../../suites/README.md`](../../suites/README.md) - per-step JSON-field breakdown
- [AWS provider configs](../aws/) - a working reference implementation using the same pattern

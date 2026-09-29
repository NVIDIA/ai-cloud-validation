# Provider Registry

Providers maintained by partners in their own repositories. Each entry pins
the exact commit that was validated; the provider's `config/` and `scripts/`
never live in this repository.

- One file per provider: `<name>.yaml`, where `name` matches the filename.
- Entries are validated against
  [`provider-registry.schema.json`](../../schemas/provider-registry.schema.json).
- Every `suites` entry must name a file in [`suites/`](../suites/).

```yaml
schema_version: 1
name: acme
vendor: Acme Cloud Inc.
description: Acme Cloud GPU instances and networking.
repo_url: https://github.com/acme/isvctl-provider-acme
commit: 0123456789abcdef0123456789abcdef01234567
ref: v1.2.0
tested_with: "0.13.0"
suites: [vm, network]
maintainers:
  - github: acme-handle
    email: oss@acme.example
documentation_url: https://github.com/acme/isvctl-provider-acme#reproducing
status: qualified
```

`status` is one of:

- `qualified`: pinned to `commit` and validated against `tested_with`.
- `experimental`: may give only a `ref`, so fetched content is not reproducible.
- `deprecated`: kept for history, hidden from the default listing.
- `demo`: the scripts only return dummy results, like `example.yaml`.
  `isvctl test run --provider <name>` runs it with `ISVCTL_DEMO_MODE=1` and
  never uploads the results.

# Provider Registry

Providers maintained by partners in their own repositories. Each entry pins
the exact commit that was validated; the provider's `config/` and `scripts/`
never live in this repository.

- One file per provider: `<name>.yaml`, where `name` matches the filename.
- Entries are validated against
  [`provider-registry.schema.json`](../../schemas/provider-registry.schema.json).
- Every `suites` entry must name a file in [`suites/`](../suites/).
- A repository can hold several providers, one directory each, with one entry
  per provider: set `path` to the provider's directory (omit it when `config/`
  and `scripts/` are at the repository root).

```yaml
schema_version: 1
name: acme
vendor: Acme Cloud Inc.
description: Acme Cloud GPU instances and networking.
repo_url: https://github.com/acme/isvctl-provider-acme
path: acme-gpu  # optional: the provider's directory in the repository
commit: 0123456789abcdef0123456789abcdef01234567
tested_with: "0.13.0"
suites: [vm, network]
maintainers:
  - github: acme-handle
    email: oss@acme.example
documentation_url: https://github.com/acme/isvctl-provider-acme#reproducing
status: supported
```

For what each `status` means, see
[Lifecycle](../../../docs/guides/provider-registry.md#lifecycle).

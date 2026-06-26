# isvctl

Unified controller for ISV Lab cluster lifecycle orchestration.

## Quick Start

```bash
# From workspace root
uv sync
uv run isvctl configure                 # persist env vars once (interactive)
uv run isvctl doctor
uv run isvctl test run -f isvctl/configs/suites/k8s.yaml

# Check a specific config before running it
uv run isvctl doctor -f isvctl/configs/suites/k8s.yaml

# View documentation
uv run isvctl docs
uv run isvctl docs -t getting-started        # view a specific topic

# List all validation tests by category
uv run isvctl docs tests
uv run isvctl docs tests -l kubernetes                   # filter by label
uv run isvctl docs tests -f isvctl/configs/suites/k8s.yaml      # show config test instances
uv run isvctl docs tests -i StepSuccessCheck             # detailed info for a test
```

## Configuration

`isvctl configure` persists the env vars an `isvctl test run` needs so you don't
re-`export` them in every shell (handy for providers like NICo that need several).

```bash
uv run isvctl configure                 # walk every variable
uv run isvctl configure --provider nico # only NICo's variables
uv run isvctl configure show            # show what's saved (secrets masked)
uv run isvctl configure set NICO_API_BASE https://nico.example.com
uv run isvctl configure set nico.organization=ncx nico.oidc_scope=example
uv run isvctl configure unset nico.api_base
uv run isvctl configure unset nico     # remove one section after confirmation
uv run isvctl configure unset --all     # remove all saved config after confirmation
uv run isvctl configure path            # print the file paths
```

Values are split across two files under `${XDG_CONFIG_HOME:-~/.config}/isvctl/`,
organized into provider-namespaced sections:

- `config.yml` (`0644`) — non-secret values.
- `secrets.yml` (`0600`) — secret values (API keys, client secrets, tokens).

```yaml
# config.yml
nico:
  api_base: https://nico.example.com
  organization: example-org
  site_id: 00000000-0000-0000-0000-000000000000
```

```yaml
# secrets.yml (0600)
nico:
  client_secret: ...
```

Each key maps to an env var by section prefix — `nico.api_base` ⇆ `NICO_API_BASE`,
`aws.region` ⇆ `AWS_REGION`, `ngc.api_key` ⇆ `NGC_API_KEY` — so what gets exported
is unambiguous. `ISVCTL_CONFIG` and `ISVCTL_SECRETS` override the individual paths.
Precedence is **process env > files > defaults**: a variable already exported in
your shell is never overridden, so CI and one-off `FOO=bar isvctl ...` overrides
keep working. Use `isvctl configure set <name> [value]`,
`isvctl configure set <name>=<value> [<name>=<value> ...]`, and
`isvctl configure unset <name>` for precise edits; both env var names
(`NICO_API_BASE`) and section keys (`nico.api_base`) are accepted. Multiple
values must use `key=value` assignment form. Use `isvctl configure unset <section>`
(`nico`, `aws`, `ngc`, `isv_lab_service`) to remove all saved values for a section
from both files after confirmation. Omitting the value prompts interactively, with hidden input for secrets. Pass
`--no-user-config` to `test run`/`test validate`/`doctor` to ignore the files.
Per-run flags (`KUBECTL`, `ISVCTL_DEMO_MODE`, …) are deliberately not persisted —
pass them on the command line or export them each time.

Keep `~/.config/isvctl/` out of any repository — `secrets.yml` holds plaintext
credentials. Run `isvctl doctor` (optionally `--provider <name>`) to verify.

## Documentation

See [docs/packages/isvctl.md](../docs/packages/isvctl.md) for full documentation.

## Related

- [my-isv Scaffold](configs/providers/my-isv/scripts/README.md) - Adding your own platform? Start here
- [Validation Suites](configs/suites/README.md) - Provider-agnostic validation contracts
- [Configuration Guide](../docs/guides/configuration.md)
- [Remote Deployment](../docs/guides/remote-deployment.md)

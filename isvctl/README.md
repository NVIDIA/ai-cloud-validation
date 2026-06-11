# isvctl

Unified controller for ISV Lab cluster lifecycle orchestration.

## Quick Start

```bash
# From workspace root
uv sync
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

## Documentation

See [docs/packages/isvctl.md](../docs/packages/isvctl.md) for full documentation.

## Related

- [my-isv Scaffold](configs/providers/my-isv/scripts/README.md) - Adding your own platform? Start here
- [Validation Suites](configs/suites/README.md) - Provider-agnostic validation contracts
- [Configuration Guide](../docs/guides/configuration.md)
- [Remote Deployment](../docs/guides/remote-deployment.md)

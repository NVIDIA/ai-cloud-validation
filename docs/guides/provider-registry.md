# Provider Registry

Partners maintain their providers - the `config/` and `scripts/` that connect
a platform to the validation suites - in their own repositories. This
repository keeps a registry of them: one small YAML file per provider that pins
the exact commit that was validated, the release it was validated against, and
who maintains it.

```text
partner repo (config/, scripts/)  <-- pinned by --  isvctl/configs/providers-registry/<name>.yaml
                                                          |
                    isvctl provider list / isvctl provider fetch <name>
                                                          |
                                     isvctl/configs/providers-external/<name>/
                                                          |
                           isvctl test run --provider <name> --suite <suite>
```

Registry entries hold no provider code. The provider's code and its support
stay with the partner.

The registry includes a living example,
[isvctl-provider-example](https://github.com/abegnoche/isvctl-provider-example),
registered as `example` with `status: demo`: an unimplemented scaffold that
passes in demo mode and runs weekly against the latest release. Use it as the model for your own
repository and entry.

## Using a registered provider

```bash
uv run isvctl provider list                          # registered providers and whether each is fetched
uv run isvctl provider fetch acme                    # check out the pinned commit
uv run isvctl test run --provider acme --suite vm    # run it like an in-tree provider
uv run isvctl provider remove acme                   # delete the fetched copy; --all removes every one
```

`fetch` checks the provider out at its pinned commit into
`isvctl/configs/providers-external/acme/`, which git ignores, verifies the
checked-out commit matches the registry, and lists the suites it can run:

```text
Fetched acme at 0123456789ab into isvctl/configs/providers-external/acme
Suites: network, vm
Setup and prerequisites (credentials, environment): https://github.com/acme/isvctl-provider-acme#reproducing
Then run one with:
  uv run isvctl test run --provider acme --suite network
```

Most providers need credentials or environment variables before a run; the
link is the entry's `documentation_url`, which is why it must explain them.

- A fetched provider works everywhere an in-tree one does:
  `test run --provider acme --suite vm`, `--provider acme --label network`,
  and `doctor --provider acme`.
- `test run --provider acme` refuses to run a registered provider that is not
  fetched, or whose checkout is not at the commit the registry pins - for
  example after a `git pull` updated the entry. Run `provider fetch acme` again;
  it replaces the previous checkout, including any local edits.
- `provider list` shows each entry's checkout as `yes`, `no`,
  `stale (<commit>)`, or `local` (a directory `fetch` did not create, such as
  your own scaffold).
- `deprecated` entries can still be fetched, with a warning.
- `demo` entries, like `example`, only return dummy results:
  `test run --provider example` turns on `ISVCTL_DEMO_MODE=1` by itself.
- Whenever `ISVCTL_DEMO_MODE=1` is set, however it was set, results are never
  uploaded to the ISV Lab Service.
- Registry names never clash with in-tree providers: the registry rejects an
  entry named after a directory in `isvctl/configs/providers/`.

## Registering a provider

1. **Scaffold it** with `uv run isvctl provider scaffold acme` and keep it in
   your own repository - see
   [Private provider repositories](../../isvctl/configs/providers/my-isv/scripts/README.md).
   It is created in `isvctl/configs/providers-external/acme/` and runs with
   `--provider acme`. The scaffold also writes a prefilled entry,
   `isvctl/configs/providers-registry/acme.yaml`, whose `<...>` placeholders fail
   validation on purpose: `isvctl` skips it with a warning, and the pre-commit
   check rejects it, until you fill it in.
2. **Validate it against a release tag** of this repository, not `main`.
3. **Prepare your repository**:
   - A disclaimer at the top of its README, for example:

     > This provider is maintained by Acme Cloud Inc., not by NVIDIA. NVIDIA does
     > not endorse or support it. Report issues to oss@acme.example.

   - Instructions that let someone with appropriate access reproduce your
     results - this is the entry's `documentation_url`.
   - A license.
4. **Open a pull request here** that adds:
   - `isvctl/configs/providers-registry/<name>.yaml` - the entry the scaffold
     wrote, with its `<...>` placeholders replaced. The format is described in
     the [registry README](../../isvctl/configs/providers-registry/README.md) and
     enforced by [its schema](../../isvctl/schemas/provider-registry.schema.json).
   - A [CODEOWNERS](../../.github/CODEOWNERS) line so changes to your entry are
     routed to you. Keep the maintainers team on it: the last matching
     CODEOWNERS rule wins, so a line naming only you would drop them.

     ```text
     isvctl/configs/providers-registry/acme.yaml @acme-handle @NVIDIA/ncp-isv-lab-maintainer
     ```

   Like every pull request here, it must be signed off (DCO), confirming you
   have the right to submit it.

### Submission checklist

- [ ] The file starts with the repository's SPDX license header (the scaffold's
      entry already has it), and no `<...>` placeholder is left.
- [ ] `python scripts/validate_provider_registry.py --check` passes (the
      pre-commit hook runs it too).
- [ ] `name` matches the filename, and `commit` is the full 40-character SHA.
- [ ] `tested_with` is the release you validated against, without a leading `v`.
- [ ] Every `suites` entry has a matching `config/<suite>.yaml` in your repository.
- [ ] `uv run isvctl provider fetch <name>` succeeds from a clean checkout.
- [ ] Your README carries the disclaimer, and `documentation_url` explains how to reproduce the results.
- [ ] CODEOWNERS names your GitHub handle and the maintainers team.

## Lifecycle

`status` is one of:

- `supported`: maintained by its maintainers (not NVIDIA), and the commit passed
  the declared suites against `tested_with`.
- `deprecated`: no longer maintained or validated; hidden from `provider list`
  unless `--all`.
- `demo`: dummy results only; runs in demo mode and is never uploaded.

**Proposed, not yet in force:** an entry that has not been re-validated
against a release in the last six months moves to `deprecated`. It stays
fetchable, so users can still try it against older releases. Re-validating
means updating `commit` and `tested_with` in a new pull request.

## Design notes

- **External repositories, not a `contrib/` directory.** Partner code ages
  differently from this repository's: in-tree, a stale integration becomes this
  repository's problem to explain and to keep passing CI.
- **The registry follows [krew-index](https://github.com/kubernetes-sigs/krew-index)**,
  the plugin index for `kubectl`: one manifest per entry, named after the entry,
  with an explicit schema version.
- **Fetching follows [pre-commit](https://github.com/pre-commit/pre-commit)**:
  `git init`, a shallow fetch of the pinned commit, a detached checkout, with
  inherited `GIT_*` variables and git template hooks kept out, staged in a
  temporary directory and moved into place only once the commit is verified.
- **Pinning to a full commit SHA** is what makes results reproducible - the same
  rule GitHub recommends for third-party Actions.
- **Submission metadata follows [cncf/k8s-conformance](https://github.com/cncf/k8s-conformance)**:
  vendor, maintainer contact, reproduction instructions, and a
  re-certify-or-lapse lifecycle.

## Open questions

- **Evidence of a validation run.** A registry entry says a commit was
  validated, but nothing in this repository shows the results. Results can be
  uploaded to the ISV Lab Service, which requires service credentials; whether
  a public, sanitized form of the evidence should accompany entries is
  undecided.
- **Recording the provider on reported runs.** Uploaded runs record the
  validation suite's version and build, but not which provider, or which
  provider commit, produced them.
- **Provider contract versioning.** Nothing yet states which suite versions a
  provider built against one release remains compatible with.
- **Registry freshness.** The registry ships inside the checkout, so a user on a
  release tag does not see providers registered afterwards, even ones validated
  against that release. Krew avoids this by keeping its index in a separate
  repository that `krew update` fetches at runtime; reading the registry from
  upstream at runtime, or a separate registry repository, are the alternatives.

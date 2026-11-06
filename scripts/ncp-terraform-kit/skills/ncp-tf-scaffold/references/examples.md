# Examples

What operators ask, and what this skill does.

**"Generate the tests for the compute layer only."** → `instantiate --contract
resource --layer compute`. The other layers appear in the result as skipped,
reason `not_selected`.

**"The account cannot create clusters."** → instantiate everything, and run with
the cluster resources left out. Report them as skipped because the account has
no permission, and give that reason in your report: no script writes it. They
are not reported passed, and they are not removed from the case set.

**"Which fixture should this resource use?"** → read the specification's required
fields and enumerations, choose values that satisfy them, and record why in the
case. Do not read the provider.

**"Can this run without an account?"** → `offline` always, and `replay` when
the exchanges were recorded earlier. Only `live` needs credentials now.

**"I have no account at all and want to start building."** → write the cases,
then render them `--mode offline`. It plans and stops, and it proves the
configuration and the contract hold. Say plainly that it proves no NCP would
accept the request.

**"We have an account now. Do I regenerate the cases?"** → No. Render the same
filled cases `--mode live`. The case always held the steps a live run needs, and
regenerating would discard the fixtures somebody reviewed.

**"Can we use replay?"** → Not until something is recorded. A recording takes an
account once and is made against the API directly, never through the provider.
It reads the provider's schema export, which marks each field that holds a
secret. `ncp-tf-cassette` records it.

# A header that the documents state for many operations

Some headers come from no machine-readable document. A prose document in the
specification directory states them once, for many operations. An example is
an account header that a request to a resource outside every account path must
carry. The derivation reads the machine-readable documents only, so the case
does not list such a header until you record it.

The provider sends such a header only when a provider setting is on. A
generated test configuration has no `provider` block, so a live test turns the
setting on through its environment variable alone. When no case lists the
header, nothing asks for that variable. The provider then sends no header, and
the API's refusal reads as a missing permission.

## What to do

1. Read every document in the specification directory, in any format. Do not
   stop at the documents that a script parses.
2. Look for a header or a precondition that applies to many operations.
3. For each operation of each case, decide whether the statement applies.
   Read the path parameters, not only the path text: a parameter value can
   hold the account, as in a path of the form `v2/{+sinkName}`.
4. Put the proposal in your reply to the operator. List each operation that
   needs the header. Quote the document's sentence, and name the document.
   Mark each operation `stated` when the document names its API, and
   `inferred` when only the document's rule fits it. End with a question that
   asks the operator to confirm the list. A reply that only says that a
   confirmation is pending gives the operator nothing to confirm.
5. Wait for the operator's answer. Do not write the header into a case before
   the answer.
6. Record each confirmed header in that operation's `expected_requests` entry:

   ```yaml
   headers:
   - name: X-Account-Header
     source: provider
     setting: ACCOUNT_HEADER_ON
     required: true
   ```

   `setting` is the environment variable that turns the provider's switch on.
   The provider's settings say which variable each one falls back to.
7. Add `expected_requests.<verb>.headers` to the case's `judged` list, so a
   kit update keeps the answer.

## What the kit does with it

- The offline request check turns the setting on, and asserts the header on
  the create request. Replay turns it on, and asserts the header on each
  request of the case.
- `render --mode live` refuses a header with `source: provider` and no
  `setting`. With the provider's schema export, it also refuses a `setting`
  that no provider setting falls back to.
- A live test whose operations, or whose dependency resources' creates, need
  the header stops before its first step when the setting is not true. The
  runner summary counts it under `refused to start`.

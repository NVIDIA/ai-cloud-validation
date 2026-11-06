---
name: ncp-tf-cassette
description: Use to record replay cassettes — "record the exchanges", "record cassettes", "make replay work", "replay is not available yet". Records one real request and response for each operation of a resource, against the NCP's API directly, with the operator's word before each create, update and delete.
version: 0.1.0
metadata:
  author: NVIDIA Corporation
  tags: [ncp, terraform, replay, cassette, recording]
---

# NCP Terraform Cassette Recording

## Purpose

The offline request check asserts the create request only. A defect in an
update, in a read after a write, or in how the provider parses a response shows
only in a live run. A cassette is the recording of one operation's exchange with
the API. With the cassettes of a resource, the scaffold renders a replay test
that drives the whole lifecycle against them, with no account and no call to the
NCP API.

## The rules

**1. Record against the API, never through the provider.** A cassette captured
from the provider's own traffic records the provider, so it cannot be evidence
that the provider is right. You build each request from the case, and the
script sends it.

**2. Ask the operator before each create, update and delete.** Each one creates
or changes a real resource in the account. Show the operator the method, the
URL and the body, and ask. Stop until the answer comes. Do not send the request
before the answer, even when the operator asked you to record the cassettes:
that request asks for the recording, not for the spend. A read needs no
separate approval.

**3. Name what you create by the run id, and end with the sweeper.** Export a
new `TFKIT_RUN_ID` before the first request. Name each resource as the rendered
test names it, because replay matches the request that the provider sends from
the test. When the case's fixture holds `{run_id}` in any value, the test sends
the fixture as it is, with its name. Only when it holds none does the test put
`naming.name_pattern` in the field that `naming.name_field` names. A delete
that the API refuses, or a session that stops, leaves the resource. End each
recording session with the sweeper's listing for that run id
(`ncp-tf-sweeper`), and show it to the operator.

**4. A cassette is a committed file.** The script keeps only the headers that
the case asserts. It replaces the run id, each value that the run supplies to
the case and each `--redact` variable with a placeholder. It writes nothing
when the API answers with a failure, other than the not-found of the read after
the delete, or when the exchange holds a secret: a field that the provider's
schema export marks sensitive, or a private key. Its message then says what to
do. Run `check` before you report a cassette as done.

**5. A wrong kit script is the operator's to know.** When `check` or `record` refuses what you think is correct, report it to the operator as a kit defect: name the script, the cassette and the message, and stop. Do not check or record with a script of yours that loads the kit's code and changes what it does, and do not edit the installed kit: an install does not update your script, and the defect stays hidden. If the operator refuses a kit change, say that the step waits for the kit fix.

## Prerequisites

- The provider's reviewed resource cases. Each case names the cassette of each
  operation in `expected_requests`: `<terraform_type>-<verb>`, or
  `<terraform_type>_<case name>-<verb>` for a named case. Below, `<type>` is
  the part before the verb.
- The provider's schema export. It marks each field that holds a secret.
- The account, and the operator's word for each change to it.
- The directory for the cassettes. Ask the operator where it goes.

## Steps

### 1. Record one resource, in this order

| Step | Cassette | `--after` |
|---|---|---|
| create, then each poll of the operation it returns, until it is done | `<type>-create` | |
| read | `<type>-read` | `create` |
| update, then each poll, when the case has an update | `<type>-update` | |
| read | `<type>-read` | `update` |
| delete, then each poll | `<type>-delete` | |
| read, after the delete is done | `<type>-read` | `delete` |

Replay answers a read with what the API returned after the last change, so give
each read its `--after`. When the read after the delete is a not-found, replay
serves it once before the first found read: a provider that believes a first
not-found fails. An API that keeps a deleted resource answers that read with
the resource, and replay serves it only after the delete.

When the create and the update call one operation, such as a label setter,
`--after` cannot tell the two calls apart, and replay gives a read during the
create the update's result. Then give each read also `--after-request
<cassette>:<index>`: the change call that it follows, with the zero-based
place of that call in its cassette. Name the call, not a poll of it.

### 2. Send each request with the script

```bash
uv run scripts/cassette.py record \
  --cases <the provider's resource cases directory> \
  --out <the cassettes directory> \
  --provider-export <the provider's schema export> \
  --name <the cassette name from the case> \
  --method <the method> --url <the full URL> \
  --body <a file with the request body, for a create or an update> \
  --auth-header <the credential header> --auth-env <the variable that holds it> \
  --header <a header the case asserts>=<the variable that holds its value> \
  --redact <a variable whose value is an account value, such as the project> \
  --after <create, update or delete, for a read> \
  --after-request <cassette>:<index>, for a read, when one operation is called twice \
  --token <the query parameter of a de-duplication token, when the provider sends one>
```

Build a create's body from the case's reviewed `fixture.create`, in the API's
spelling, and from nothing else. A recorded body that differs from the fixture
makes the provider's correct request fail to match.

An update's body is the one that the provider sends for the case's update, and
it can hold more than `fixture.update`: a `PUT` sends the whole resource as the
read returned it, and a `PATCH` can send each field that the provider manages.
Take it from the provider's update code, or from a replay test that reports
`no recording matches` on the update: its message shows the body. Then record
the resource's operations again, in order, with that body built from the read
that you recorded and the run's real values.

When the provider sends a de-duplication token in a query parameter, such as a
request ID that it makes for each create, send the request with a new token in
that parameter, and add `--token <the parameter>`. The provider makes a new
token for each request, so no recorded value can match it: the script writes
the parameter as `{dedup_token}`, and replay requires the parameter and accepts
any value in it. Do not write `{dedup_token}` into an existing cassette that
was recorded without the token: the API never received that request, and a
token it refuses would pass replay. Remove that cassette, and record its
operation again.

A response can name an account another way than the request does, such as by
number where the request used an ID. Both name one account, so write it with
the request's placeholder: `--redact <the number's variable>=<the request's
variable>`. Replay gives each placeholder its own stand-in, so a placeholder of
the number's own makes the provider read an account that no recording holds.

The script refuses a name that no case expects, and sends nothing then.
`record` appends to the cassette. To record an operation again, remove its
cassette first.

### 3. Check the cassettes

```bash
uv run scripts/cassette.py check \
  --cases <the provider's resource cases directory> \
  --cassettes <the cassettes directory> \
  --auth-env <the variable that holds the credential> \
  --redact <each --redact value you recorded with>
```

`check` refuses a cassette with no exchange, a credential header, a header that
the case does not assert, a bearer token, the credential, a private key, the
raw value of a placeholder, or a name that no case expects. Run it with the recording's variables still set, so
it can find a raw value.

### 4. End the session with the sweeper's listing

List what the run id left, and show the operator. Delete only what the operator
approves, through the sweeper.

### 5. Render the replay tests

Render the cases with the scaffold's `--mode replay --cassettes <directory>`
into the provider's generated test package. A resource gets a replay test when
its create, read and delete are recorded. The provider's offline entry point
runs the replay tests after the request checks.

Do not edit the provider's README. The scaffold's render writes its run
section, which already says how to run one replay test alone and how a
resource gets its recordings, so the replay tests need no new sentence there.

## Limitations

- A cassette shows what the API returned when it was recorded, not what it
  returns now. A live run stays the proof of the current API.
- A response can hold an account value that no variable names, such as a
  project number. Read each cassette before the commit, and redact each one
  you find. A value that names the request's account takes the request's
  placeholder (step 2).

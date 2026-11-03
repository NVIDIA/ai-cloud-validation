# Reading the documents

Detail for `ncp-tf-provider` steps 1 and 2: how the script reads a
specification, which you repeat by hand when it refuses one.

## The two languages

The two languages state the same things differently, and both are read:

| | OpenAPI | Discovery |
|---|---|---|
| required | the schema's own `required` list, and required parameters | required parameters, and a property's `annotations.required` when it names the create |
| output-only | the `readOnly` flag | a marker in the description text |
| long-running | a declared `202 Accepted` response | an operation resource as the response |
| request body | usually written inline | always behind a reference |
| how a plan cites an operation | `METHOD /path`, where the document defines no id worth citing | `op:<id>`, the method's own id |

An inline body is given a name derived from its operation, so everything after
this point sees one kind of schema whatever the document did. This holds for a
response as well as a request: a response left unnamed supplies no attributes,
which reads exactly like an API that assigns nothing.

A nested object written in place, with no schema name — both languages do it,
for small objects such as one entry of a rule list — is expanded like a named
one, down to its own `required` fields. It has no name, so no cycle can pass
through it, and the depth bound is its only limit. Left unexpanded, its fields
exist in the API and cannot be set through the provider.

Both reference forms resolve, and so does a bare identifier. A plan may not cite
`ext:` for a resource operation: a generated resource cannot call an endpoint
the provider does not own, and the derivation refuses it rather than reporting
the operation as missing.

## What the schema reads

**Read the create response, not only the create request.** The request says what
may be sent; the response says what comes back. A field only the response
carries is one the API assigns, and it is where a resource's identifier is
stated — without it the provider has nothing to refresh or destroy with. A field
the request can send, at any depth, stays the practitioner's to set: a wrapper
body nests the resource one level down, so the same name can be settable inside
and returned at the top.

**A long-running create is the exception.** Its response describes the operation
rather than the resource, so nothing is read from it and the artifact says so.
Those resources take their computed attributes from the request's own
output-only markers, from the read's response, and from the completed
operation at run time.

**Then read the read's response.** Terraform refreshes state from the read. A
field that the read states, and that no request sends and no create answer
states, is computed too: a create that answers with a confirmation alone, such
as the ids it made, states none of the resource. A read that takes no path
parameter of its own is a list, and adds nothing.

**Where a required parameter and a returned field share a name**, the parameter
is kept and the collision is recorded. One document can name the parent in the
path and the created resource in the body with the same word, and choosing
silently would lose either a required input or the resource's identity.

Whether the API satisfies a requirement was already judged by the coverage
report and recorded as a status there. **A requirement the API cannot meet is a
`partial` in the coverage report, not a field missing from here.** Filtering the schema by
requirement would make a requirement edit force a schema change, and would bury
a judgement where no gate can see it.

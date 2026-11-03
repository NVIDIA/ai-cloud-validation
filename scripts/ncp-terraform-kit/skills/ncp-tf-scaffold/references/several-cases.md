# Several cases of one resource type

How a resource type gets a second case, and how it runs and is recorded.
`SKILL.md` step 3 points here.

## When a type needs a further case

One case holds one configuration of its resource. A requirement can need a
second one: a node type the first case does not create, for example, or a
configuration that costs much more than a normal run. Add a further case only
when the operator asks for it. Each case is a full case with its own judged
fields, so each one is review time the operator spends.

The type's first case stays the unnamed case. Keep its file, its test names and
its record entry, so its earlier results stay with it. Its content can change.

When a requirement names alternative configurations, for example a CPU node
type and a GPU node type, each alternative needs a case that sets the value
that tells it apart. The unnamed case is one of the alternatives. If it does
not set that value and the operator did not give one, end your answer with a
question that asks for it, for example "Which machine type should the first
case use?". Write the answer in the unnamed case. **Do not report the work as
done while the unnamed case lacks the value.** A statement that the value is
pending is not a question, and the operator does not answer it. A case that
does not set the value tests the API's default, and the requirement stays half
proved.

## Write it

```bash
uv run scripts/case_builder.py instantiate --contract resource \
  --resource <type> --case-name <name> \
  --coverage <path>/coverage.yaml --out <provider>/tests/cases
```

The name is lowercase letters and digits. The command writes
`<type>_<name>.yaml` beside the type's unnamed case, with `resource.case_name`
and every judged field empty. Fill it as step 3 says. Every value that makes
the configuration differ, such as a machine type, is a judged value that the
operator gives. Do not choose one.

A lookup of a case by its type takes the unnamed case: a dependency on the type
creates its dependency resource from the unnamed case, and `check
--resource-cases` compares a module entry with the unnamed case. The further
case declares its own `dependencies`. To give it the same dependency resource
as the unnamed case, copy that entry.

Each case renders its own names. The unnamed case keeps
`TestAcc<Type>_lifecycle`. The further case gets `TestAcc<Type>_<name>_lifecycle`,
`TestRequest<Type>_<name>_create`, its own files and its own workspace,
`<type>_<name>/`. The two cases create resources under the same name pattern,
and `go test` runs one test after the other, so one case destroys its resources
before the next one creates them.

`check` and `render` refuse two cases that render as one case id: two unnamed
cases of one type, two cases of one type with one name, or a case `<name>` of
`<type>` beside a case of the type `<type>_<name>`. They also refuse a
`case_name` that is not lowercase letters and digits.

A live run runs a further case when its `-run` selects
`TestAcc<Type>_<name>_lifecycle`, so tell the operator what the case creates
before such a run.

## How it is recorded

`record --cases` reads the cases. A further case is the entry
`<type>[<name>]`, beside the type's entry. A requirement is `live` only when
every case of its type passed live.

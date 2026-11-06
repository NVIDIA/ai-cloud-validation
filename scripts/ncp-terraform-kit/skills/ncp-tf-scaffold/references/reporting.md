# What to report when the step finishes

**Anything you changed in an artifact this step did not generate comes first**,
before what the step produced — which file, which field, and on whose authority.
A summary that opens with what was built reads as success, and an edit to
another step's output is the thing most worth a second pair of eyes.

Then, in this order:

1. Where the cases went, and how many.
2. **What is owed, by field and by count, each against the number of cases** —
   the script prints the tally; relay it rather than a single total. "67 fields"
   says nothing a reviewer can act on; "24 of 24 create fixtures, 19 of 24
   update fixtures, 24 of 24 readiness conditions" says where the work is. **A
   count below the number of cases needs its one sentence**, or it reads as
   cases somebody failed to finish: the API offers no such operation there, so
   the step and its decision are both gone, and each such case names the drop
   under `narrowed`.
3. **Where to find the question, and where the answer goes** — inside the case,
   under `review`. Each entry carries the question, what a wrong answer costs,
   and the shape to write. For a fixture the entry also names the `schema` that
   describes the body. **The case copies no part of that schema**: what a
   document states about a body is too much to carry and too easy to carry in
   part, and a part read as the whole is worse than the trip to read it. **The answer goes at the field the
   entry names** — `fixture.create` is the `create:` key under `fixture:` — and
   not under `review`, which is generated and read back by nothing. Say that
   nothing else in a case is theirs to edit, so they can skip the rest.
4. What the run did not cover: `selection.yaml` for a resource run, the run's
   output for a blueprint run.
5. What the check refuses, and why — not its exit code. Until the decisions
   are filled it refuses every case and names each unfilled field, so it is the
   worklist. "It exits 1 as designed" tells the operator nothing to act on.

**Do not hand the operator a command to run.** They ask you for the next step,
in their own words — to check the filled cases, or to render them — and you run
it. A command in a summary makes the operator the runner and leaves you holding
nothing they can correct.

# Modeling state

Cruxible holds typed state: Subjects, ClaimTypes and Claims, plus the
definitions that act on them. Not everything belongs there. This page is about
deciding what does, and shaping it well when it does.

## State or source

Make something **typed state** when the system has to do something with it:

- ask across items: filter, count, sort, join (`query`);
- enforce vocabulary: enumerated values, references that must resolve to a
  real Subject;
- check consistency mechanically across links, such as a task marked done
  whose blocker is still open;
- coordinate concurrent writers: atomic change sets, compare-and-set on a
  read value;
- keep per-field history and attribution;
- drive behavior: `next` rows, Lines, Triggers, staleness of blocks and
  citations.

Keep it **prose in a source file** (Markdown, notes) when it is read whole and
revised whole: reasoning and rationale, trade-offs and rejected options,
narrative, guides, checkpoints, friction logs, and anything that would need an
invented schema only to store text.

The usual right shape is a **hybrid**: prose is the source, and typed state is
extracted from it with citations. A decision's full text lives in a Markdown
section; the decision Subject keeps its standing, its date, what it governs,
what it amends, a one-line summary, and a citation of the passage. Editing the
prose then makes `next` report the citation as drifted, and the summary gets
reviewed. Reports built from state go back into pages as
[projection blocks](declared-blocks.md), rendered or authored and stamped
either way.

Warning signs that something is in the wrong place:

- long text stored as a Claim value, then cut short on read or edited in place
  and amended only in prose;
- obligations or follow-ups that exist only inside a paragraph, so nothing
  lists them;
- several hand-synced copies of one list that drift apart.

## Citation or Document

When a Claim rests on what a file says, **cite** it: catalogue the file in
`.cruxible/sources.yaml` (a `name` and a `locator` are enough) and pass
`--evidence-file PATH#ANCHOR`. The quoted passage is retained, and `next`
reports `citation_drifted` when it changes; repair by binding the Claim to the
new text or retiring it.

Make the file a **Document** only when its exact wording is itself the
governed thing, such as a policy or a specification: the catalog entry gains
its Document fields, and `sources compile` and `sources propose` turn each
revision into a reviewed, versioned artifact. Most usage is citations.

## Subjects

Subjects are the things your state is about, named `kind/id`. Choose kinds for
the things people and agents start from, fan out from, or review on their own;
a concept that may need to be queried or linked deserves a kind rather than a
text field.

Choose IDs that are stable for the real thing: a durable source identifier, a
slug you will not need to change, never a mutable title. Before minting a new
Subject, look for an existing one:

1. `get` the exact `kind/id`, and grep the floor for it and its likely names;
2. `orient --kind K` to see the kind, and `query K --contains TEXT` for near
   values;
3. reuse what exists, or mint the new Subject.

## ClaimTypes

A ClaimType is one field of a kind. Decide, for each:

- **What the value is**: a literal checked by a JSON schema (use an `enum` for
  a closed set of values, and `member_descriptions` to say what each means), a
  reference to another Subject (a typed relationship, with
  `allowed_object_subject_kinds`), or exact content.
- **How many**: `one` (a value that is replaced) or `many` (a set that values
  are added to).
- **Which roles**: `observation` (what was seen), `normative` (what should
  be), `environment_binding`, `derivation`. Give a `default_role` so writers
  need not name one.
- **Which evidence supports a value**: the evidence admission rules. Admit the
  writer's own words for values a person or agent simply states; admit the
  catalogued sources and CaptureContracts that should back values that must be
  checkable. A value whose evidence is not admitted is recorded with the
  verdict `uncovered`, which reads expose.
- **How competing values resolve**: the resolution policy.
- **A description**, so `orient` and agents know what the field means.

Do not encode current truth in the type. Claims carry values; attestations,
evidence and history carry how well they hold up. When the meaning of a field
must change, succeed the ClaimType with `cruxible claim-type migrate` and decide
what happens to each Claim that depends on it, rather than redefining it in
place.

## Evidence that holds up

- Prefer a citation or a Capture to own words when the value comes from
  somewhere; own words are right for decisions and judgments.
- Record contradiction as evidence against a Claim (`claim attest
  --contradict`, or a Claim of what you observed) instead of minting an
  inverse field.
- A projection block is never evidence: cite the Claim it reflects, or text
  outside every block.

## Procedures

Make a Procedure when a way of acting should be reviewed once and then run the
same way every time: reading state, fetching or reading sources, calling
providers, and proposing what should change. A Procedure's contract (its
input, output, budgets, and the providers it pins) should let an agent decide
whether to run it without reading its graph.

- Pin every provider exactly. When the consumer of your definitions should
  choose a provider, ship a Blueprint and let them instantiate it.
- Read through accepted state and named queries, and acquire outside data
  through Source nodes under an acquisition policy, never through ambient
  access.
- Let a Line run it when it should happen on a schedule or on an event, and
  give a Line that proposes or settles changes a mandate that says exactly
  what it may do.
- Declare measurements when you want to know whether it worked, and read them
  with `procedure readings`.

## Defaults stay visible

Authoring is progressive: a ClaimType or Procedure that states only what
differs from the defaults is complete, and the defaults remain visible in what
was accepted (`get … --detail proof`). Nothing is filled in at run time that
was not in the accepted definition.

## Indexes are projections

Queries run over accepted state through the daemon's indexes. Any faster
index, search engine or graph database you add is a projection of accepted
state and source references, and can be rebuilt from them.

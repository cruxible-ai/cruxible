# Projection blocks

A projection block is a passage of a workspace page that reflects accepted
state and carries a stamp recording exactly which state it reflects. The page
stays an ordinary file you own; the stamp is what lets Cruxible tell you when
the passage no longer matches.

## Rendered or authored

Governed does not mean generated. A block is one of two kinds, and both are
governed the same way:

- **Rendered**: the deterministic output of a named query, written for you
  by `cruxible block repin --render`: a Markdown table of the result Subjects
  and their fields, or a bulleted list when the query projects no fields.
  Use it for lists, tables and indexes.
- **Authored**: prose you or an agent wrote as-is (a synthesis, a decision
  summary, an explanation), declared as a block whose stamp records the
  Claims, artifacts or query it summarizes. Use it for anything a query
  cannot write.

When the backing state moves, both are reported stale the same way. Only a
rendered block can regenerate itself; an authored block needs only the signal,
and its author rewrites or reaffirms it. Do not model a document's content as
new state just so it can be generated: the requirement is traceability and
staleness, not derivation.

## The workflow

1. **Write the markers** and, for an authored block, the prose between them:

   ~~~markdown
   <!-- cruxible:block:status -->
   The release is blocked on one review; everything else is done.
   <!-- /cruxible:block:status -->
   ~~~

   The page must be catalogued in `.cruxible/sources.yaml`; its entry name is
   the block's source ID.

2. **Repin to stamp it.** Name what the block reflects:

   ~~~bash
   cruxible block repin board status --claim CLM-… --claim CLM-…
   cruxible block repin board tasks --query QueryDefinition:project.task_status --render
   ~~~

   Repin reads the backings at the accepted head, writes the stamped opening
   marker, and registers the block with the instance. It mints no Claim and
   never edits authored prose; `--render` writes the body of a block backed by
   one query. `--dry-run` computes and checks the stamp and writes nothing.

3. **Let `next` or `block sync` report it.** When a backing changes,
   `cruxible next` reports `projection_backing_stale`, and when someone edits
   the block's text, `projection_dirty`. `cruxible block sync --all` checks
   every catalogued page and edits nothing.

4. **Re-check and repin.** Read what changed, revise the prose if it no longer
   holds (or rerun `--render`), and repin. A repin is your declaration that
   the passage reflects its backings again.

5. **When the backings are gone**, take the block down in two steps.
   `cruxible block depublish SOURCE_ID BLOCK_ID` releases the instance's
   registration, so `next` stops asking for the block back; it edits no page.
   Then `cruxible block detach PATH` strips the markers and keeps the prose
   (it refuses a block that is still live), or remove them by hand.

## Blocks are not evidence

A page can hold two kinds of governed passage, and they never overlap:

- a passage you wrote that a Claim **cites** as evidence. It has no markers:
  catalogue the page and cite the span (`--evidence-file PATH#ANCHOR`);
- a **projection block**, which reflects Claims that already exist.

A citation whose span lies inside a projection block is refused
(`cruxible.projection.evidence_from_projection`), because a passage that both
reflected state and backed it would let a page attest to itself. Cite the
underlying Claim instead, or text outside every block.

## Currency policy

`--currency-policy warn`, the default, makes a stale block advisory: `next`
reports it and `block sync` exits zero. `require_current` makes it a blocking
`next` finding and fails `block sync`. Neither ever blocks accepting the
underlying state.

The marker grammar, stamp storage, and every option are in the
[CLI reference](cli-reference.md#block).

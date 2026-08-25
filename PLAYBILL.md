# Playbill declared blocks

Governed knowledge lives in the accepted ledger. Ordinary Markdown may declare
that an agent-written passage reflects one or more accepted Claims or named
QueryDefinitions; the passage is never itself canonical governed state.

Create both delimiters and every prose byte yourself:

```markdown
<!-- playbill:block:status -->
Reflects generation 12: the incident is ready for review.
<!-- /playbill:block:status -->
```

The unstamped pair is not a declaration until an explicit first repin:

```bash
cruxible playbill block repin corpus.runbook status --claim CLM-example
```

Repin replaces only the opening marker with its machine-derived backing,
coordinate, generation, and body commitments. The caption is optional authored
content; the machine never inserts, updates, or removes prose. Future repins
without backing options retain the existing identities and parameters. Supplying
explicit Claim or QueryDefinition references replaces the backing set.

`playbill next` reports `projection_dirty` when the prose no longer matches its
declared body and `projection_backing_stale` when a visible backing's actual
statement or query-result meaning changes. Review and edit prose yourself, then
repin. Unrelated accepted generations never stale a semantically current block.

SDK independent-evidence authoring and CLI Flow-A bind refuse anchors inside a
declared block: cite its underlying Claim or deliberately author an explicit
copy citation instead. This guard is client-side only. Raw-wire clients can
bypass it; that residual is intentionally a review norm, not daemon enforcement,
and declarations introduce no governed region family or standing views tree.

# Compact projection manifests: remaining design slice

A compact marker should reference immutable canonical bytes of the complete
backing declaration. `ProjectionBlockStampV1` already contains the authored
backing selection, declared coordinate and generation, and body digest; those
bytes can form the manifest rather than inventing another truth model.

The manifest is authored information. It cannot be reconstructed from accepted
Claims alone: the agent selected these backings. To promise ledger recovery,
retain the manifest through an accepted, ledger-referenced artifact and retain
its content-addressed body. A disposable projection directory or the current
operational declaration journal alone does not satisfy that promise. Exact
recovery of the authored paragraph additionally requires retaining its body.

An offline package must include the Markdown and every referenced manifest,
for example under `.playbill/manifests/<full-sha256-hex>.json`. The marker carries
the full digest and source/block identity. Filenames and shortened UI labels are
not integrity evidence. A shared parser receives an explicit bounded
manifest map/resolver and verifies canonical bytes, digest, identity binding,
limits, and existing declaration constraints before returning a resolved stamp.
Missing or invalid manifests are unresolved/refused, never ordinary prose or
unstamped bootstrap declarations. Existing inline markers remain readable.

The slice must update all consumers together:

- Shared marker parsing, source discovery, framing and frame assertions.
- SDK stamping, repinning, syncing, source selectors and workspace observations.
- Server coverage parsing, whose current input is source bytes alone; either
  supply verified manifest bytes or resolve them through retained artifacts.
- Evidence exclusion of projection regions, including unresolved markers.
- Procedure publication framing and its exact byte commitments.
- Portable package export/import, retention references and recovery checks.

Document writes must retain the manifest before referencing it. New wire and
marker versions must preserve old bytes and commitments. The feature should
ship with offline, missing-manifest, corruption, identity substitution, bounded
resolution, and rebuild tests. It is deliberately deferred from the initial
state-loop performance pass rather than implemented as a local-only marker
rewrite that other authoritative consumers cannot interpret.

# Modeling semantic state in Playbill

This branch no longer uses one YAML config as the authority for a mutable entity
graph. Model accepted knowledge around semantic subjects and external sources.

## Start with source ownership

For every source, decide:

- what system is authoritative for the underlying record;
- how a stable record or query coordinate is expressed;
- whether exact bytes can be pinned;
- what change cadence and freshness matter;
- which observations are exhaust versus candidates for governance.

Playbill should not copy an external database merely to point at it. A Claim may
refer to a source coordinate and evidence digest while the database remains the
record authority.

## Choose the governance unit

Use a Document when exact prose or a whole artifact is genuinely the review
unit. Use a Claim when the useful unit is a proposition. Use a Procedure when
the useful unit is a bounded, reusable way of acting.

One Document can yield many candidate Claims. Extraction is deterministic, but
selection is explicit: proposing one candidate does not govern every statement
in the body.

## Stable subjects

Subjects are discovery anchors. Prefer canonical identities tied to domain
referents rather than whichever phrase an author happened to use.

Before minting a new subject:

1. search exact identity and aliases;
2. search type and namespace;
3. search optional recall-only tags;
4. inspect near candidates;
5. choose reuse, alias, or an explicit distinct-from disposition.

Aliases affect resolution and therefore require stronger authority than
recall-only tags.

## Claim design

A ClaimType should state:

- proposition shape and required fields;
- subject roles;
- canonicalization and identity rules;
- acceptance policy;
- allowed attestation/evidence forms;
- projection and explanation expectations.

Do not encode current truth into the type itself. Claims carry propositions;
attestations and acceptance history carry epistemic development.

## Procedure design

A Procedure contract should let an agent decide whether to invoke it without
loading implementation details:

- contract in;
- contract out;
- preconditions;
- exported capabilities;
- budgets and terminal caps;
- pinned dependencies;
- governance metadata and track record.

## Defaults and policy

Authoring must be progressive. Common ClaimTypes and Procedures should inherit
safe defaults for acceptance, evidence, and explanation. Explicit policy is
required only where the domain differs.

Defaults must remain visible after compilation. They are deterministic authored
semantics, not hidden runtime guesses.

## Query model

Queries operate over accepted semantic projections and may join subjects whose
evidence originates in different data silos. A cloud graph/search database can
accelerate traversal, but its contents are projections of accepted state and
source references, not a new ground truth.

Start with grep-friendly files and local SQLite. Add indexes, piecewise
projections, or graph databases only when measured demand requires them.

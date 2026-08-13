# Playbill Family 1: governed Documents

Playbill Family 1 is an opt-in, greenfield authority for governed Documents. A
Document has a small accepted envelope in the Git ledger and exact body bytes in
content-addressed storage. Changes move through distinct store, propose, review,
approve, and activate stages. Accepted reads name the complete coordinate:
Git OID, semantic root, generation root, and compiler digest.

This family does **not** migrate, replace, or shadow-write existing StateNote or
WorkItem graph entities. Their current storage and write paths remain their only
authority. Similar prose in a legacy entity and a Playbill Document is two
independent records until a later, separately reviewed dependency-closed migration
defines a converter and flips the applicable authority fence. PB-E makes no
retirement claim for either legacy type.

## Public lifecycle

1. `playbill body store` puts exact bytes into inert CAS. Presence in CAS carries
   no authority.
2. `playbill document propose` or `playbill sources propose` admits authenticated
   intent and deterministically evaluates an immutable candidate.
3. Review exposes the candidate digest, semantic parent, settlement base, complete
   member enumeration, required approvals, and permission-filtered readable diff.
4. `playbill proposal approve` fetches that exact challenge, signs the frozen
   Ed25519 attestation preimage with a client-held key, and sends only the public
   attestation. The daemon never accepts a private key or client key path.
5. `playbill proposal activate` independently verifies approvals, prebuilds the
   projection, and advances accepted state by parent-bound compare-and-set.
6. Document reads, history, and `playbill explain` bind to accepted coordinates;
   proposal/candidate coordinates remain visibly provisional.

Principal registration, rotation, revocation, and recovery use a distinct
principal-lifecycle law. Recovery is deliberately narrow: a recovery principal can
repair key state but cannot approve an ordinary Document candidate.

## Local source catalogs

Source catalogs are client-side authoring inputs, not state authority. A portable
catalog contains repository-relative locators; an ignored local overlay can name
explicit absolute/root-aliased sources. Compilation reads only declared regular
files under configured roots, rejects symlinks and ambiguous targets, and emits a
canonical path-free bundle containing exact bytes and digests. `sources check` is
read-only. `sources propose` submits the frozen bundle, so a later filesystem edit
cannot change what is proposed. The daemon, HTTP API, and MCP tools never
dereference a client-supplied local path.

There are no filesystem watchers, scheduled compilation, automatic proposal,
automatic approval, or automatic activation in Family 1.

## Explanation boundary

`playbill-explain-v1` returns structured governance, provenance, attestation
coverage, history, source mapping, proof references, and permission-aware
redactions for one semantic subject at one accepted coordinate. `summary` and
`evidence` are supported. `proof` is reserved and returns a typed unsupported-detail
result in this batch. The contract is suitable for later editor/LSP clients, but
PB-E ships no language server, editor extension, or mutation affordance through
explanation.

The opt-in dogfood test can be run with:

```bash
CRUXIBLE_RUN_PLAYBILL_DOGFOOD=1 uv run pytest -q \
  tests/test_playbill/test_family1_dogfood.py
```

It reads the ratified design and implementation-program files without modifying
them, governs copies of their exact bytes, supersedes one body, and verifies replay
and projection rebuild from accepted HEAD.

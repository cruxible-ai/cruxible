# Playbill Family 1: governed Documents

Documents are the first implemented Playbill semantic family. A Document has a
small accepted envelope in the Git ledger and exact body bytes in
content-addressed storage. Changes move through distinct store, propose, review,
approve, and activate stages.

This family established the authority model now used for destructive
convergence. The old StateNote/WorkItem public products and shadow-write paths
have since been removed from the served branch. Claims and Procedures will be
implemented directly under Playbill semantics rather than migrated through a
compatibility layer.

## Public lifecycle

1. body store puts exact bytes into inert CAS. Presence carries no authority.
2. document propose or sources propose records authenticated intent and
   deterministically evaluates an immutable candidate.
3. Review exposes candidate digest, semantic parent, settlement base, complete
   member enumeration, required approvals, and permission-filtered diff.
4. proposal approve fetches the exact challenge and signs it with a client-held
   Ed25519 key. The daemon receives only the public attestation.
5. proposal activate independently verifies approvals, prebuilds the
   projection, and advances accepted state by parent-bound compare-and-set.
6. Reads, history, and explain bind to Git OID, semantic root, generation root,
   and compiler digest.

Principal registration, rotation, revocation, and recovery use a separate
principal-lifecycle law. Recovery can repair key state but cannot approve an
ordinary Document candidate.

## Local source catalogs

Source catalogs are client-side authoring inputs, not authority. A portable
catalog contains relative locators; an ignored local overlay can name explicit
absolute or root-aliased sources.

Compilation reads only declared regular files under configured roots, rejects
symlinks and ambiguous targets, and emits a canonical path-free bundle
containing exact bytes and digests. sources check is read-only. sources propose
submits the frozen bundle, so later filesystem edits cannot change the
candidate. The daemon never dereferences a client-supplied local path.

There are no filesystem watchers, scheduled compilation, automatic proposal,
automatic approval, or automatic activation.

## Explanation

playbill-explain-v1 returns structured governance, provenance, attestation
coverage, history, source mapping, proof references, and permission-aware
redactions for one semantic subject at one accepted coordinate.

summary and evidence are supported. proof is reserved and returns a typed
unsupported-detail result. The contract is suitable for editor/LSP and
agent-facing explanation clients, but explanation is read-only and ships no
mutation authority.

## What Family 1 intentionally does not solve

A governed whole Document does not make each sentence a first-class proposition.
The Claims program adds semantic subjects, ClaimTypes, selected extraction,
attestations, vocabulary reuse, and distinctness dispositions. Procedures become
the parallel first-class executable family.

The Document envelope and source-bundle mechanics remain useful: they provide
exact bytes, stable provenance, review material, and deterministic compiler
inputs for those granular subjects.

## Dogfood test

~~~bash
CRUXIBLE_RUN_PLAYBILL_DOGFOOD=1 uv run pytest -q \
  tests/test_playbill/test_family1_dogfood.py
~~~

The test governs copies of the ratified design/program bytes, supersedes one
body, and verifies replay and projection rebuild from accepted HEAD.

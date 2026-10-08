# Governance Flow

Use this reference whenever a change stops at a proposal, when an instance's
governance needs setting up (who may approve, which agents act), and for the
final hand-off of `create-state`, `adopt-kit` and `automate-with-procedures`.

## How a change becomes accepted state

Every change is a change set proposed against the accepted state it was read
at:

1. **Propose.** A write (`set`, `add`, `retire`, `write`), an authoring submit,
   a ClaimType proposal, a kit add or remove, or a provider install creates a
   proposal: a frozen candidate evaluated against its base. A refused proposal
   is kept with its diagnostics. A proposal is not accepted state.
2. **Review.** `cruxible get PROPOSAL_ID` reads its status, changes and, for a
   refusal, the code, message and repair. `cruxible proposal review
   PROPOSAL_ID` prints how to diff the candidate against accepted state with
   plain Git (`git diff cruxible-ledger/accepted...cruxible-ledger/proposals/…`)
   and where the daemon's evaluation and approval records are.
3. **Approve**, when the policy requires it. `cruxible proposal approve
   PROPOSAL_ID` signs the exact candidate with the approver's private key on
   the client and sends only the signature. An approval does not activate
   anything. On MCP, `cruxible_proposal_approve` signs with a key from the
   server's `CRUXIBLE_MCP_KEY_DIR`; pass the reviewed `candidate_digest`.
4. **Activate.** `cruxible proposal activate PROPOSAL_ID` checks approvals and
   the policy and advances accepted state by compare-and-set.

Writes, kit adds and removes, and provider installs do all of this in one call
when the approval policy requires no approval and the caller's tier may
activate; otherwise they stop at the proposal and name who may approve it.

## Stale and unwanted proposals

A proposal made before another change was activated is stale: it can never
activate as it is. `cruxible next` lists it (`proposal_stale`) for its author.

- `cruxible proposal readmit PROPOSAL_ID` checks the same change again at the
  current head and returns a fresh proposal, which needs its own review.
- `cruxible proposal withdraw PROPOSAL_ID --reason "..."` retires a proposal
  that will never be wanted.

To avoid staleness while setting up, propose and activate one change at a time,
or put related changes in one change set (`write` for values, a `change_set`
authoring input or `cx.changes(rationale=...)` for definitions).

## Set up governance

Decide with the user, early:

- **The approval policy** (`cruxible get ApprovalPolicy:instance`).
  `self_approval_allowed`, the default, lets an author approve and activate
  their own changes. `independent_approval_required` needs an approval from
  someone other than the author for every governed change (`cruxible init
  --require-independent-approval` sets it at creation; afterwards it changes
  through an `approval_policy` authoring input, `cruxible authoring example
  approval-policy`).
- **Agent principals.** Each agent gets its own principal, so its acts are
  attributed to it: `cruxible principal add NAME --key-dir DIR` registers it
  and writes its settings to `DIR/cruxible.env` for the agent to load. On a
  daemon with auth, the agent's credential tier decides what it may do:
  `governed_write` proposes and authors but cannot approve or activate,
  `graph_write` can. With auth off, the principal ID is a claim, not a
  boundary.
- **Mandates** for Lines that propose or settle (see
  `automate-with-procedures`): the narrowest grant, scope and condition that
  does the job.

## Final checks and hand-off

Before handing off:

```bash
cruxible next                        # nothing unexpected; every row explained
cruxible proposal list --status open # nothing left half-done
cruxible block sync --all            # pages match their backings
cruxible sources check               # catalogued Documents match their files
cruxible kit status                  # when kits are installed
```

Then tell the user, briefly:

- the vocabulary (kinds and fields) and where each field's evidence comes from;
- the named queries and the pages that render them;
- the approval policy, the principals and what each may do;
- any enabled Lines, their schedules and mandates;
- which `next` rows to expect over time (drifted citations when files change,
  stale blocks when state moves, coverage gaps after a restart) and their
  repairs;
- anything left open, and why.

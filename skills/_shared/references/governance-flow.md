# Governance Flow

Use this reference whenever a change stops at a proposal, when an instance's
governance needs setting up (who may approve, which agents act), and for the
final hand-off of `create-state`, `adopt-kit` and `automate-with-procedures`.
[Review and governance](https://docs.cruxible.ai/review-and-governance/) is the
source; this is the checklist.

## When a change stops at a proposal

Every change is checked first, and lands in the same call only when the
approval policy needs nobody else's approval and the caller's tier may
activate. Otherwise the result says why it stopped and what to run next.

```bash
cruxible proposal list --status open
cruxible proposal review PROPOSAL_ID     # what it changes, why, and the git diff command
cruxible proposal approve PROPOSAL_ID    # signs; the approval that completes it activates it
```

- `--hold` signs without activating; someone runs `cruxible proposal activate`
  later.
- A stale proposal (`proposal_stale` in `next`) is recompiled at the current
  head when approved; approvals of the old one don't carry over. If what it
  changes has moved, `cruxible proposal readmit` rebases it for review.
- `cruxible proposal withdraw ID --reason "…"` drops your own proposal (an
  identical change submitted again afterwards needs a different `because`);
  a reviewer turns one down with `cruxible proposal reject ID --reason "…"`.
- On MCP, `cruxible_proposal_approve` signs with a key from the server's
  `CRUXIBLE_MCP_KEY_DIR`; pass the reviewed `candidate_digest`.

To avoid staleness while setting up, land one change at a time, or put
related changes in one change set.

## Set up governance

Decide with the user, early:

- **The approval policy.** `self_approval_allowed` (the default) or
  `independent_approval_required` (`cruxible init
  --require-independent-approval`), under which nobody approves their own
  change. Independence is what lets a team trust each other's findings.
- **Agent principals.** Each agent gets its own principal (`cruxible principal
  add NAME --key-dir DIR`), so its acts are attributed to it. On a daemon with
  auth, a `governed_write` credential proposes but can't approve; with auth
  off, a principal ID is a claim, not a boundary.
- **Mandates** for Lines that propose or settle: the narrowest grant that does
  the job, settling only facts copied verbatim from a source.

## Final checks and hand-off

```bash
cruxible next                          # nothing unexpected; every row explained
cruxible proposal list --status open   # nothing left half-done
cruxible kit status                    # when kits are installed
```

Then tell the user, briefly:

- the vocabulary (kinds and fields) and what evidence backs each field;
- the saved queries and the pages that render them;
- the approval policy, the principals and what each may do;
- any enabled Lines, their schedules, and their mandates and expiry;
- which `next` rows to expect over time (drifted citations when files change,
  stale pages when state moves, gaps after a restart, inbox items) and their
  repairs;
- anything left open, and why.

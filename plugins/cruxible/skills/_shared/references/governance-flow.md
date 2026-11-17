# Governance and review

Never approve your own proposal, including when daemon auth is off or the local
policy allows self-approval. A second agent (normally a subagent) acting as its
own principal reviews the exact candidate and approves: `reviewer`, set up by
`cruxible init --reviewer-key-dir DIR --require-independent-approval` or by
`cruxible principal add`. With no second principal, set one up rather than
self-approve. The person approves only if they asked to. A reviewer subagent
uses the CLI with the reviewer's settings (`cruxible context use --principal
reviewer`, or `CRUXIBLE_PRINCIPAL_ID=reviewer`), because subagents share the
parent's MCP servers, which act as the author. This is an instruction to agents,
not an auth-off isolation boundary. User authorization still defines what work
may be proposed or executed.

A proposal is not accepted state. Read its value changes, what each cites,
its definitions, diagnostics and `candidate_digest` with `proposal review`; an
approver signs that reviewed digest. The approval that completes policy may
also activate; `--hold` signs without activation. A candidate awaiting
activation needs activation, not another approval.

An author who wants a second agent's review under a policy that needs no
approval writes with `--no-accept` (`accept="never"`): the proposal waits as
`ready_to_activate` and `next` lists it. The reviewing agent runs `proposal
review`, then `proposal approve` (it signs, and activates when nothing else is
required) or `proposal activate`; `proposal reject` turns it down. Verify the acceptance receipt's coordinate before reporting
a change as accepted.

A changed or stale candidate needs fresh review. Only its author readmits a
stale proposal; old approvals do not transfer. Report a pending proposal or
refusal with its served repair rather than retrying toward acceptance blindly.

For one-instance local use, the plugin defaults to admin and the adapter
finds or auto-starts a daemon. Tier permits an endpoint; principals, evidence,
Mandates and approval policy determine what is lawful. With auth off,
principal IDs are claimed identities among processes of the same user.

For teams, use authenticated credentials and separate principals: author
governed_write, reviewer graph_write, operator admin. The reviewer owns its
signing key. Configure the adapter's credential/tier/key directory outside the
plugin; never ship keys or tokens. `cruxible_proposal_approve` signs with
`CRUXIBLE_MCP_KEY_DIR` and the reviewed digest. Host initialization, credential
provisioning and changing approval policy are separately authorized operator
work, not consequences of installing this plugin.

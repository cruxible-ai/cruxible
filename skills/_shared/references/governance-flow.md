# Governance and review

A second agent reviews the exact candidate; never self-approve, including
when daemon auth is off or the local policy allows self-approval. This is an
instruction to agents, not an auth-off isolation boundary. User authorization
still defines what work may be proposed or executed.

A proposal is not accepted state. Read its diff, diagnostics and
`candidate_digest` with `proposal review`; an approver signs that reviewed
digest. The approval that completes policy may also activate; `--hold` signs
without activation. A candidate awaiting activation needs activation, not
another approval. Verify the acceptance receipt's coordinate before reporting
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


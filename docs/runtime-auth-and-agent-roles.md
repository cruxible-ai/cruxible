# Authentication, credentials, and Playbill principals

Playbill uses two independent authorization layers.

## Runtime bearer credentials

A bearer credential authenticates HTTP/MCP/CLI transport to one daemon-managed
instance. It carries a permission tier:

| Tier | Meaning |
|---|---|
| read_only | Accepted reads, review rendering, explanation, checks |
| governed_write | Read plus body storage and proposal creation |
| graph_write | Governed write plus approval submission and activation |
| admin | Instance allocation, initialization, credentials, principal changes |

The daemon has an immutable capability ceiling. A credential cannot exceed it.

server start generates a one-time bootstrap secret. Use
credential claim-bootstrap to exchange it for the first persistent admin token,
or use the secret for the initial local bootstrap session. Tokens are secrets:
keep them out of repositories, prompts, logs, and source bundles.

Credential rotation mints a replacement and revokes the old credential.
recover-admin is a local-filesystem ownership recovery path for daemon
administration; it is not a Playbill content-approval bypass.

## Playbill principals

A Playbill principal is an accepted public-key record with one or more roles:

- owner;
- reviewer;
- recovery.

Principal authority is evaluated against the candidate, governance scope, and
accepted key history.

The CLI generates unencrypted OpenSSH Ed25519 key material:

~~~text
<principal-id>.ed25519       private, mode 0600
<principal-id>.ed25519.pub   public, mode 0644
~~~

Ed25519 is a modern public-key signature algorithm. The private key creates a
signature over an exact approval challenge. Anyone with the public key can
verify that signature, but cannot create another one.

Client key directories must be outside the workspace and outside daemon-managed
roots. The daemon receives only public principal records and signed
attestations. It never receives a principal private key or client key path.

## Approval flow

~~~text
daemon: prepare exact candidate challenge
client: verify rendered candidate
client: sign challenge with principal private key
daemon: receive public attestation
daemon: verify key history, role, scope, digest, and coordinate
daemon: activation re-verifies before compare-and-set
~~~

A stale or mismatched challenge fails. A valid signature does not by itself
activate state.

## Daemon key

Each Playbill instance has a separate daemon-held Ed25519 key for ledger commit
mechanics and bootstrap verification. It is not an owner/reviewer principal and
cannot satisfy human/agent approval roles.

## Rotation and revocation

Principal changes are governed proposals. Add a second principal with
`cruxible playbill principal add ID --role reviewer --key-dir DIR --name NAME`;
the key is generated in client custody, and the affected principal must approve
its own lifecycle proposal with its current key (the key-possession proof), and
the proposal must then be activated before the change enters accepted state.
Other principals may record additional voluntary approvals. Rotation introduces a new public key
while retaining history needed to verify old approvals. Revocation prevents new
authority from the revoked principal without making historical signatures
unverifiable.

## Recovery

Recovery principals may repair owner/reviewer key state after custody loss.
They cannot approve ordinary Document candidates. This narrow authority avoids
turning recovery into a universal governance bypass.

Keep recovery private keys offline when practical and in a custody directory
separate from normal owner/reviewer keys.

## Agents

Give each agent or harness its own runtime credential and principal identity
when it needs to propose or review. Attribution should not collapse several
agents into one shared token/key.

An agent may prepare or submit an approval only within its assigned role. Human
review tooling can use the same challenge/signature protocol without sharing
private keys with the daemon or with another agent.

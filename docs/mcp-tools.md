# MCP Tools Reference

This reference is generated from the Playbill-only MCP registration surface.
Removed legacy tools are intentionally absent.

## cruxible_playbill_activate

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when an approved Playbill candidate is ready to settle.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `proposal_id` | yes | Tool input. |

## cruxible_playbill_check_source_bundle

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to compare compiled source bytes with accepted state.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `bundle` | yes | Tool input. |

## cruxible_playbill_dereference

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need verified accepted body bytes and have body-read permission.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `identity` | yes | Tool input. |

## cruxible_playbill_explain

**Permission:** `READ_ONLY`

**Purpose:** Use when you need coordinate-bound governance, provenance, and attestation coverage.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `subject` | yes | Tool input. |
| `at` | yes | Tool input. |
| `detail` | no | Tool input. |
| `include_body` | no | Tool input. |

## cruxible_playbill_get_document

**Permission:** `READ_ONLY`

**Purpose:** Use when you need one accepted Document envelope and structured facts.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `identity` | yes | Tool input. |

## cruxible_playbill_history

**Permission:** `READ_ONLY`

**Purpose:** Use when you need one Document's replay-verified accepted history.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `identity` | yes | Tool input. |

## cruxible_playbill_host_create

**Permission:** `ADMIN`

**Purpose:** Use when you need an empty daemon-owned host before Playbill bootstrap; this adopts no config or semantic state.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | no | Tool input. |

## cruxible_playbill_init

**Permission:** `ADMIN`

**Purpose:** Use when you need to bootstrap Playbill from client-generated public keys.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `principals` | yes | Tool input. |
| `operating_profile` | no | Tool input. |

## cruxible_playbill_inspect_proposal

**Permission:** `READ_ONLY`

**Purpose:** Use when you need immutable proposal evaluation and candidate evidence.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `proposal_id` | yes | Tool input. |

## cruxible_playbill_inspect_refusal

**Permission:** `READ_ONLY`

**Purpose:** Use when you need typed admission or acceptance-law diagnostics for a proposal.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `proposal_id` | yes | Tool input. |

## cruxible_playbill_list_documents

**Permission:** `READ_ONLY`

**Purpose:** Use when you need accepted Documents and their exact coordinate.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |

## cruxible_playbill_list_principals

**Permission:** `READ_ONLY`

**Purpose:** Use when you need accepted public principal records and their coordinate.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |

## cruxible_playbill_prepare_approval

**Permission:** `READ_ONLY`

**Purpose:** Use when a client-held signer needs the exact immutable approval statement.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `proposal_id` | yes | Tool input. |
| `signer_id` | yes | Tool input. |
| `include_body` | no | Tool input. |

## cruxible_playbill_propose_document

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to propose a governed Document create or supersession.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `shell` | yes | Tool input. |
| `proposal_name` | yes | Tool input. |
| `source_compilation_digest` | no | Tool input. |

## cruxible_playbill_propose_principal_change

**Permission:** `ADMIN`

**Purpose:** Use when you need a governed principal registration, rotation, revocation, or recovery.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `principal` | yes | Tool input. |
| `proposal_name` | yes | Tool input. |

## cruxible_playbill_propose_source_bundle

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to propose frozen source bytes without sending a local path.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `bundle` | yes | Tool input. |
| `source_name` | yes | Tool input. |
| `proposal_name` | yes | Tool input. |

## cruxible_playbill_review

**Permission:** `READ_ONLY`

**Purpose:** Use when you need a structured candidate review and permission-filtered diff.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `proposal_id` | yes | Tool input. |
| `include_body` | no | Tool input. |

## cruxible_playbill_source_context

**Permission:** `READ_ONLY`

**Purpose:** Use when a local client needs path-free accepted inputs before compiling sources.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |

## cruxible_playbill_store_body

**Permission:** `GOVERNED_WRITE`

**Purpose:** Use when you need to store exact Document bytes inertly before proposing them.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `content_base64` | yes | Tool input. |

## cruxible_playbill_submit_approval

**Permission:** `GRAPH_WRITE`

**Purpose:** Use when you have a public approval attestation produced outside the daemon.

**Inputs:**

| Name | Required | Description |
|---|---:|---|
| `instance_id` | yes | Tool input. |
| `proposal_id` | yes | Tool input. |
| `attestation` | yes | Tool input. |

## cruxible_server_info

**Permission:** `READ_ONLY`

**Purpose:** Use when you need live daemon version, state-directory, authentication, or instance-count information.

**Inputs:** None.

## cruxible_version

**Permission:** `READ_ONLY`

**Purpose:** Use when you need to confirm which cruxible build is running.

**Inputs:** None.

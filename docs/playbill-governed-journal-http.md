# Governed journal HTTP peer protocol

This document defines the open HTTP profile implemented by
`HttpGovernedJournalClient`. A governed journal home is not a
`JournalBackendProtocol` storage adapter. The caller describes an operational
event, while the home assigns its stream, partition, chain coordinates, and
authenticated actor attribution.

The protocol reuses the journal models and normalized interchange in
`cruxible_core.playbill.exhaust`. It defines no parallel record, digest, chain,
or export format.

## Peer configuration

The client is configured with five opaque values:

- `endpoint_root`: a URL that already contains every deployment-specific scope;
- `home_stream_id`: the home's locator for the logical stream;
- `identity`: the expected `JournalStreamIdentityV1`;
- `authorization_header`: the complete value sent as `Authorization`;
- `format_version`: the peer profile identifier sent in request bodies and
  required on successful responses.

A caller may also configure extra write headers. They are forwarded unchanged
only on writer acquisition, append, and fence requests. This extension lets a
deployment require a second write proof without teaching Core how that proof is
issued or managed. Extra headers cannot replace `Authorization` or the fencing
header.

The client appends only the relative path
`journal/streams/{home_stream_id}` and the operation paths below. Path segments
are percent encoded. It never inserts deployment scope into `endpoint_root`.

## Common encoding and headers

Requests and responses use JSON. Every payload-bearing request contains:

```json
{"format_version":"<configured peer profile>"}
```

Every successful response is a JSON object containing the same byte-exact
`format_version`. A missing or different value is a verification failure.

Every request carries:

```text
Authorization: <opaque configured value>
```

Append and fence additionally carry:

```text
X-Cruxible-Journal-Fencing-Token: <opaque active fence>
```

Authorization material and fencing tokens never appear in response bodies or
diagnostics. Implementations must not log them.

Model documents named below are the `model_dump(mode="json")` form of the
corresponding Core v1 model. Implementations must use Core's existing digest and
verification functions; reproducing the JSON shape without reproducing its
digest is not conformance.

## Read operations

### Read one partition head

```text
GET journal/streams/{home_stream_id}/partitions/{partition_id}/head
```

Response:

```json
{
  "format_version": "<peer profile>",
  "head": {"tag": "playbill-journal-partition-head-v1", "...": "..."}
}
```

The head must be a valid `JournalPartitionHeadV1` naming the configured logical
stream and requested partition.

`read_head_vector` is a client-side composition of this operation. The client
sorts verified heads using `journal_head_key` and constructs
`JournalHeadVectorV1`; there is no separate vector route.

### Read an exact range

```text
GET journal/streams/{home_stream_id}/partitions/{partition_id}/records
    ?first_sequence={first}&last_sequence={last}
```

Response:

```json
{
  "format_version": "<peer profile>",
  "range": {"tag": "playbill-journal-range-v1", "...": "..."},
  "records": [
    {"tag": "playbill-stored-procedure-journal-record-v1", "...": "..."}
  ]
}
```

`range` must equal the complete requested `JournalRangeV1`, not merely its
sequence bounds. `records` must pass `verify_journal_range`. Missing, shortened,
reordered, discontinuous, or digest-invalid records are verification failures;
they are never returned as an empty result.

### Read coverage

```text
GET journal/streams/{home_stream_id}/coverage
```

Response fields used by Core:

```json
{
  "format_version": "<peer profile>",
  "coverage": {
    "coverage": "exact|truncated|expired|unavailable|unauthorized",
    "partitions": [
      {
        "partition_id": "runs-2026-08",
        "coverage": "exact|truncated|expired|unavailable|unauthorized",
        "head_sequence": 7,
        "head_record_digest": "sha256:...",
        "reason": null
      }
    ],
    "reason": null
  }
}
```

The response may contain peer-specific operational fields; Core ignores them.
It does not reinterpret them as journal semantics.

For every requested partition, the client separately reads the authoritative
head and compares it with `head_sequence` and `head_record_digest`. An omitted
partition or a disagreement produces `UNAVAILABLE`, never `EXACT` with an empty
head set. HTTP 401 or 403 produces `UNAUTHORIZED` coverage.

## Writer operations

### Acquire a writer

```text
POST journal/streams/{home_stream_id}/partitions/{partition_id}/lease
```

Request:

```json
{
  "format_version": "<peer profile>",
  "expected_head_sequence": 7,
  "expected_head_record_digest": "sha256:..."
}
```

Response:

```json
{
  "format_version": "<peer profile>",
  "writer_lease": {
    "journal_stream_id": "<home_stream_id>",
    "partition_id": "runs-2026-08",
    "lease_generation": 3,
    "status": "active",
    "expected_head_sequence": 7,
    "expected_head_record_digest": "sha256:..."
  },
  "fencing_token": "<opaque one-time token>"
}
```

The response must reproduce the home stream, partition, and exact expected head.
The generation is a positive integer. The fencing token is returned once and is
kept out of object representations by the Core result type.

### Append governed content

```text
POST journal/streams/{home_stream_id}/partitions/{partition_id}/records
```

Request:

```json
{
  "format_version": "<peer profile>",
  "content": {
    "event_kind": "node_fired",
    "accepted_coordinate": {"...": "..."},
    "definition_digest": "sha256:...",
    "payload_digest": "sha256:...",
    "recorded_at": "2026-08-23T12:00:00Z"
  },
  "idempotency_key": "sha256:...",
  "expected_head_sequence": 7,
  "expected_head_record_digest": "sha256:..."
}
```

`content` is the canonical subset of `ProcedureJournalRecordDraftV1` supplied
by the caller. It must not contain `tag`, `stream`, `partition_id`, `sequence`,
`previous_record_digest`, or `actor_context`. The home refuses those fields
rather than ignoring them. It adds the addressed stream and partition, assigns
the next chain coordinate, and derives actor attribution at its authenticated
boundary.

Core derives `idempotency_key`; callers cannot override it. The derivation is:

```text
typed_digest(
  ArtifactDigest,
  "playbill-governed-journal-append-idempotency-v1",
  {
    "stream": <JournalStreamIdentityV1 JSON>,
    "partition_id": <requested partition>,
    "expected_head": <JournalPartitionHeadV1 JSON>,
    "content": <normalized canonical content>
  }
).tagged
```

The fencing token and actor attribution are deliberately absent. Retrying the
same append after transport uncertainty derives the same key; changing its
logical coordinate or content derives a different key.

Response:

```json
{
  "format_version": "<peer profile>",
  "record": {"tag": "playbill-stored-procedure-journal-record-v1", "...": "..."},
  "head": {"tag": "playbill-journal-partition-head-v1", "...": "..."},
  "replayed": false,
  "operation_id": "<home operation>"
}
```

The returned record must name the configured logical stream and requested
partition. The returned head's sequence and digest must commit that exact
record. For `replayed: false`, the record must be at expected sequence plus one
and commit the expected head digest as its predecessor. For `replayed: true`,
that extension check does not apply; every other scope, digest, content, and head
check still applies. The returned actor context is accepted as home-assigned,
while the remaining record fields must reproduce caller `content` through
`ProcedureJournalRecordDraftV1` and `ProcedureJournalRecordV1.bind`.

### Fence a writer

```text
POST journal/streams/{home_stream_id}/partitions/{partition_id}/lease/fence
```

Request:

```json
{
  "format_version": "<peer profile>",
  "expected_generation": 3
}
```

Response:

```json
{
  "format_version": "<peer profile>",
  "writer_lease": {
    "journal_stream_id": "<home_stream_id>",
    "partition_id": "runs-2026-08",
    "lease_generation": 3,
    "status": "fenced"
  }
}
```

The response must reproduce every supplied coordinate and report `fenced`.

## Portable transfer and handoff

All transfer bytes are the exact result of
`render_journal_export(JournalExportBundleV1)`. The client parses them with
`parse_journal_export`, verifies the signed `JournalHeadManifestV1`, binds the
manifest to the bundle, checks the logical stream, and reproduces the advertised
counts.

The common export object is:

```json
{
  "export": {
    "payload_base64": "<strict base64 of normalized export bytes>",
    "byte_length": 1234,
    "segment_count": 2,
    "record_count": 70
  },
  "head_manifest": {"tag": "playbill-journal-head-manifest-v1", "...": "..."},
  "expected_head_public_key": "<32-byte lowercase Ed25519 public key hex>",
  "operation_id": "<home operation>"
}
```

### Export

```text
POST journal/streams/{home_stream_id}/export
```

Request:

```json
{
  "format_version": "<peer profile>",
  "partition_ids": ["runs-2026-08"]
}
```

The response is the common export object plus `format_version`. Its signed head
vector must name exactly the requested partition set.

### Import a transfer

```text
POST journal/streams/{home_stream_id}/import
```

Request:

```json
{
  "format_version": "<peer profile>",
  "payload_base64": "<strict base64 of normalized export bytes>",
  "expected_head_public_key": "<source head key>"
}
```

The client verifies the transfer before sending it. The home verifies it again,
imports only exact continuations, and refuses a missing prefix or fork.

Response:

```json
{
  "format_version": "<peer profile>",
  "imported_heads": [
    {"tag": "playbill-journal-partition-head-v1", "...": "..."}
  ]
}
```

`imported_heads` must exactly equal the signed head vector in the transfer.

### Begin handoff

```text
POST journal/streams/{home_stream_id}/handoff/begin
```

The request is the export request. The response is the common export object,
`format_version`, and:

```json
{"moving_partitions":["runs-2026-08"]}
```

`moving_partitions` must equal the populated partitions in the verified head
vector. The client returns a `JournalHeadProof`; its signature proves custody of
the named prefixes, not semantic acceptance.

### Complete handoff

```text
POST journal/streams/{home_stream_id}/handoff/complete
```

Request:

```json
{
  "format_version": "<peer profile>",
  "target_head_manifest": {"tag": "playbill-journal-head-manifest-v1", "...": "..."},
  "target_head_public_key": "<target head key>",
  "source_fencing_tokens": {"runs-2026-08": "<source fence>"},
  "partition_ids": ["runs-2026-08"]
}
```

Core verifies the target proof and its exact stream and partition set before
sending. The home fences its source writers only after independently verifying
that proof.

Response:

```json
{
  "format_version": "<peer profile>",
  "released_partitions": ["runs-2026-08"],
  "fenced_leases": [
    {
      "journal_stream_id": "<home_stream_id>",
      "partition_id": "runs-2026-08",
      "status": "fenced"
    }
  ],
  "export_remains_available": true
}
```

The released and fenced partition sets must exactly reproduce the request.

## Refusals

A non-success response is a JSON object with an optional string `error_code`:

```json
{
  "error_code": "journal_law_refused",
  "message": "human-oriented detail"
}
```

Core retains `error_code` as an opaque `refusal_id`. It does not interpret the
human message or turn a peer's operational policy into Core types.

The following peer identifiers classify a writer or prefix conflict and produce
`RemoteJournalConflict` on conflict-capable operations:

- `journal_law_refused`;
- `journal_writer_lease_conflict`;
- `journal_writer_lease_invalid`;
- `journal_idempotency_conflict`.

Every other non-success response produces `RemoteJournalRefusal`. A response
without usable JSON or an identifier remains a refusal with its HTTP status and
an unspecified identifier. A failed HTTP exchange produces
`RemoteJournalTransportError`.

A 2xx response that is malformed, substituted, unverifiable, discontinuous, or
digest-invalid produces `RemoteJournalVerificationError`. A conforming caller
must never convert one of these conditions into an empty record set, an empty
transfer, or exact coverage.

## Authority boundary

This protocol retains operational records and proves their custody. Transport
authorization, a writer grant, a successful append, and a signed head do not
accept a Claim, promote exhaust, grant a semantic role, or mutate accepted
Playbill state.

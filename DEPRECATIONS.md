# Deprecations

Cruxible follows deprecate-then-remove for shipped surfaces. Deprecated inputs
remain accepted for the stated window, delegate to or teach the replacement,
and emit the same structured warning shape on every supported transport:
`{surface, replacement, removal_version}`. Removal versions are commitments;
changing one requires updating the registry, this table, tests, and changelog
together.

| Surface | Replacement | Deprecated in | Removal version |
| --- | --- | --- | --- |
| `feedback action 'flag'` | `attest --stance contradict` | 0.3.0 | 0.4.0 |
| `feedback action 'approve'` | `feedback action 'accept'` | 0.3.0 | 0.4.0 |
| `legacy outcome record functions` | `resolution contracts and attestations` | 0.3.0 | 0.5.0 |
| `legacy outcome profile functions` | `resolution contract declarations` | 0.3.0 | 0.5.0 |
| `feedback group_override write path` | none (retired; see below) | 0.3.0 | 0.4.0 |
| `FeedbackRecord.source input` | `actor_context` | 0.3.0 | 0.4.0 |
| `OutcomeRecord.source input` | `actor_context` | 0.3.0 | 0.4.0 |
| `GroupResolution.resolved_by input` | `resolved_actor_context` | 0.3.0 | 0.4.0 |
| `CandidateGroup.proposed_by input` | `proposed_actor_context` | 0.3.0 | 0.4.0 |
| `DecisionRecord.opened_by input` | `opened_actor_context` | 0.3.0 | 0.4.0 |
| `GroupStatus 'auto_resolved' read-only member` | `resolved with resolution_source='auto_resolved'` | 0.3.0 | 0.4.0 |
| `OperationType 'group_clear' read-only member` | `group_withdraw` | 0.3.0 | 0.4.0 |
| `StateHealthGroupsSection.auto_resolved_count` | `withdrawn_count` | 0.3.0 | 0.4.0 |
| `ProcedureTransitionResult.warnings string list` | `ProcedureTransitionResult.typed_warnings` | 0.4.0 | 0.5.0 |

The rows stay after a surface is removed: this table is the historical schedule,
not a list of what is still accepted. Removed in 0.4.0 (the registry entry, the
acceptance path, and every surface that carried them are gone; the DEPRECATIONS
and CHANGELOG rows are the record): `feedback action 'flag'`, `feedback action
'approve'`, `feedback group_override write path`, `FeedbackRecord.source input`,
`OutcomeRecord.source input`, `GroupResolution.resolved_by input`,
`CandidateGroup.proposed_by input`, and `DecisionRecord.opened_by input`.

Sending any of them now FAILS rather than warning, and fails by NAME at every
public boundary rather than being dropped as an unknown key: `422` on HTTP, a
typed tool error on MCP (the tool does not run), a `ValidationError` on the
client input contracts, and a `BadParameter` naming the offending item on
`cruxible feedback batch`. Each refusal names the retired key and what to send
instead. Only the retired names are refused — an unrelated unknown field is
still tolerated, because banning extras outright is a wider contract change
than this schedule promised.

**Retired with no replacement in 0.4.0: `feedback group_override write path`.**
This row once named `force_review` as the replacement. It is not one, and the
schedule now says so. What is GONE is every way to SET
`assertion.group_override`: the `--group-override` CLI flags, the HTTP and MCP
inputs, the client kwargs, and the service write path behind them — with no
successor input on any transport. What REMAINS is the stored flag on edges
0.2.x/0.3 instances already stamped: `group/governance.py` still reads it, so
such an edge still raises a proposal's review priority and still blocks
auto-resolution, and reads still report it faithfully. `force_review` is a
per-call boolean argument to `service_propose_group` (also raised by a
workflow's `require_review` policy) that forces ONE proposal to be reviewed; it
is Python-level, lives inside the service layer, appears on no HTTP route, MCP
tool, CLI command or client method, and persists nothing on an edge — so it is
neither an equivalent nor a migration target. Exposing a per-proposal review
forcer on the public transports is a separate, demand-gated work item.

**Rescheduled in 0.4.0.** `legacy outcome record functions` and `legacy outcome
profile functions` were stamped 0.4.0 and were NOT removed; the maintainer moved
both to 0.5.0. The stated replacement does not exist yet — resolution contracts
carry no equivalent of an outcome profile's coded vocabulary, its
`required_scope_keys`, or the profile-drift analysis `analyze outcomes` reports,
and porting that machinery is post-Playbill-branch work. Removing the only
writer first would leave the `outcome_profiles` config four shipped kits
declare, the `outcomes` table, `list outcomes`, and `analyze outcomes` alive
with nothing able to feed them. Both entries now carry `removal_version:
"0.5.0"` explicitly in the registry rather than inheriting the default, so the
new commitment survives a change to that default.

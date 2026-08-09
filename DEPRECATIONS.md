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
| `legacy outcome record functions` | `resolution contracts and attestations` | 0.3.0 | 0.4.0 |
| `legacy outcome profile functions` | `resolution contract declarations` | 0.3.0 | 0.4.0 |
| `feedback group_override write path` | `force_review` | 0.3.0 | 0.4.0 |
| `FeedbackRecord.source input` | `actor_context` | 0.3.0 | 0.4.0 |
| `OutcomeRecord.source input` | `actor_context` | 0.3.0 | 0.4.0 |
| `GroupResolution.resolved_by input` | `resolved_actor_context` | 0.3.0 | 0.4.0 |
| `CandidateGroup.proposed_by input` | `proposed_actor_context` | 0.3.0 | 0.4.0 |
| `DecisionRecord.opened_by input` | `opened_actor_context` | 0.3.0 | 0.4.0 |
| `GroupStatus 'auto_resolved' read-only member` | `resolved with resolution_source='auto_resolved'` | 0.3.0 | 0.4.0 |
| `OperationType 'group_clear' read-only member` | `group_withdraw` | 0.3.0 | 0.4.0 |
| `StateHealthGroupsSection.auto_resolved_count` | `withdrawn_count` | 0.3.0 | 0.4.0 |
| `ProcedureTransitionResult.warnings string list` | `ProcedureTransitionResult.typed_warnings` | 0.4.0 | 0.5.0 |

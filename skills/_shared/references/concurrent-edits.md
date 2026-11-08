# Editing shared state

Carry what was read into the write: use `expect` / `--expect` for a value or
section, `--expect-absent` for an absent value, or `--at` for the coordinate
a preview/operation guards. Use the guard that the served operation supports;
these are not interchangeable with an approval's candidate digest.

A compare-and-set or stale-coordinate refusal means someone may have changed
the input. Read the current state and reconcile the intended edit. Do not
remove the guard or automatically retry against a newer head.

For proposals, review the exact candidate and pass its `candidate_digest`
when approving. The readback coordinate in the acceptance receipt identifies
what landed; a preview or submitted intent alone does not establish acceptance.

For file/Document section replacement, the current section observation is
`expect`, not the entire document's assumed old contents. Pair Subject ends
are immutable; editing the relationship's facts cannot retarget its ends.

# Control Review: CTRL-3 Reverse Proxy Allowlist

## Summary

`CTRL-3` (`Reverse Proxy Allowlist`) is attached to `ASSET-8` in deterministic
seed data. The API Platform and Security teams reviewed the control after the
partner API incident and concluded it materially reduces exposure to WebLogic
administrative endpoints when source CIDRs are tightly constrained and changes
are time-bounded.

## Candidate graph facts

- Proposed `control_mitigates_class`:
  `CTRL-3 -> admin_interface_rce`
  `validation_basis=Replay of blocked admin-console requests from non-allowlisted partner hosts`

## Caveat

The review notes that `CTRL-3` should remain partial rather than full support,
because it does not protect already-allowlisted sources and depends on strict
change-control around temporary partner access expansions.

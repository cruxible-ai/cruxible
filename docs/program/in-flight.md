# In flight

Batches that have not landed, as of generation one. Every row is backed by a
`dev.batch.state` claim and, where one exists, a `dev.batch.blocked_by` claim.

### Governed: in-flight batches
<!-- playbill:block:pub-43145c6f401c6724a0b476392f519720:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTM4N2I2OTIxYmIyM2ZmZDBkNmE2NDQzMGY5OWIzMDhjIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NWExNmUzYTQwOWJhYzA2OWVlNGZlOTExMzhjOWFiMjk2YTA0YzJlZDIzOGE5NTZhM2ExNmUxMzdhMGQ0MTU5MCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItNDMxNDVjNmY0MDFjNjcyNGEwYjQ3NjM5MmY1MTk3MjAiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo3NDMyZjQ4ZjA4ZTdlODBiM2M0Zjk4NWQ0N2UyOGFmMGZhNjg0Y2VhMmJjZThhZjEyNWIzN2Q1MWFiNDBmNDk5IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1pbi1mbGlnaHQiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
| batch | track | state | tip | waiting on |
|---|---|---|---|---|
| `ops-hotfix-1` | `ops-hotfix` | reviewed | `7def996fe77aefa3fad6e94e852e41c2f2d431e9` | its own fix round, then a rebase onto the P2-B6-bearing head |
| `cloud-cut-a` | `cloud` | reviewed | `060f9cf07b6fba0e9bf60e698a5b5b32d23042ac` | the core pin, held until ops hotfix 1 lands |
| `p2-b6-1` | `p2-b` | planned | — | its worktree exists; unblocked |
| `compiler-succession` | `succession` | planned | — | unblocked; no dispatch contract written yet |
| `ledger-mirror-batch` | `mirror` | planned | — | the succession lane |
| `cloud-cut-b` | `cloud` | planned | — | cut A |
| `ds-0` | `ds` | planned | — | a daemon repoint |
| `ds-1` … `ds-6` | `ds` | planned | — | the phase before each |

`p2-b3` is a fourth kind of not-landed: a dispatch contract and a worktree
exist, the worktree is parked at the P2-C landing commit, and the register
records neither a dispatch nor a return. It is carried as planned with that
ambiguity attached rather than dropped.
<!-- /playbill:block:pub-43145c6f401c6724a0b476392f519720 -->

### Governed: open verdicts
<!-- playbill:block:pub-14f5869ba40bb3f9a9018fd2a680a4df:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWM4MDA5Y2MwYTk2ZGZmMGI3OWEwM2ZjZmI0MTljYjBjIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6ZmQ3YTQwNmVmZGFmZTlmZWM0N2JhNDQ2NmI5MTdmODJiYWE5ZWNhZGI5ODMzMzMwMTY5ZGY3YTc2ODZkZjYxMiIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItMTRmNTg2OWJhNDBiYjNmOWE5MDE4ZmQyYTY4MGE0ZGYiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo1NmM5YmM2NjU5MWM2NDZiMGQ3MTMwY2ZiYTIxZDMwMzZiYTY3ZmQ1ZTEwYzg1ZDg5MjgyZGUzMDlhNzEyMjcwIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1pbi1mbGlnaHQiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
Every in-flight batch has been reviewed at least once, and every one of those
reviews accepted with fixes rather than rejecting. One carries a finding that
gates landing on its own.

| batch | pass | verdict | gating finding |
|---|---|---|---|
| `ops-hotfix-1` | review A (runtime, security) | accept with fixes | none; the security item holds where it matters |
| `ops-hotfix-1` | review B (surfaces, freeze, deprecation) | accept with fixes | the batch's one irreversible write is absent from the surface guardrail and announces no write target |
| `cloud-cut-a` | delta re-review | accept | none |
<!-- /playbill:block:pub-14f5869ba40bb3f9a9018fd2a680a4df -->

### Governed: sequencing
<!-- playbill:block:pub-493199a4b67a6d6addadf50e5233d37f:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTVkNzU2MWVjMmRhOTczNjhmNzExNjgyNWRmZjc0NTMxIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NDBlYzI3NjljZDJiOGVjYTlhNmRkODAyNzQxMTY0OWI0OTllOWZhNGY5YTc2NzY3M2NlNTM3YTZmMzc1NjEwZiIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItNDkzMTk5YTRiNjdhNmQ2YWRkYWRmNTBlNTIzM2QzN2YiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjoxYzc4Mjg5ZDNlNmUzOGIwODYyODQyYzZhYTFhY2RjMjQ4YTY4NTMwNmZkZDc0ZDcwNmE3YjQ0MjJlMmQxZjVmIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1pbi1mbGlnaHQiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
The core order of record is: P2-B6, then ops hotfix 1, then P2-B6.1, then the
compiler-succession lane, then the ledger mirror. The first of those has landed;
everything below it is unblocked in principle and sequenced in practice. The
dogfood order is DS-0 through DS-6, each phase waiting on the one before it. The
cloud lane runs in its own repository against a pinned core commit and lands
nothing on this branch.

Two couplings are worth stating because they are not visible from the order
alone. Ops hotfix 1 and P2-B6 edit sixteen of the same files, and a dry-run
rebase hit its first content conflict immediately, so the ops batch owes a
rebase onto the now-landed head before it can land. And the cloud pin is held
rather than taken because both core batches re-pin the served surface under the
same succession date and move the handshake digest; one of the two has now
moved, so the pin can be taken once the other does.

This page was written across a period in which one of its own rows changed:
P2-B6 moved from reviewed to landed and the branch head advanced sixteen
commits. That is the ordinary condition of the page, not an exception, and it
is why the rows are claims rather than prose.
<!-- /playbill:block:pub-493199a4b67a6d6addadf50e5233d37f -->

# Method laws

The standing rules of the work: how a batch is dispatched, verified, reviewed
and landed. Each row is backed by a `dev.law.text` claim carrying the law as
exact content, and where the law came from a named failure, a `dev.law.origin`
claim pointing at the batch or card that produced it.

Almost every law here was bought with an incident. That is the honest reading
of the table: the rules are not a methodology chosen in advance but a record of
what went wrong once and was not allowed to go wrong twice.

### Governed: dispatch
<!-- playbill:block:pub-6e3c7f9c31bbf6372bd95c9c543dd416:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTUyOWQ4ZDY5NzBjMWJjNzYyYjdiZTBhOGIwZTRmZjRlIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6MDMyM2RmZmJjNjIxMjI2NzViN2Y0Mzg2NjEwZTRkZWVhOTdjOTcyODA2NzNjMDQ1YmQ0NDQyNDQ0YTMwNGI2MCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItNmUzYzdmOWMzMWJiZjYzNzJiZDk1YzljNTQzZGQ0MTYiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo5ODNjNmQyN2RmYWIwMDQ4N2ZlZTYzZjAyNGJmNWVmMmFkN2QxNTFiNDA5YWY5YjAxM2EzOWJjMWI3ZDczN2NjIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYXdzIiwidGFnIjoicGxheWJpbGwtcHJvamVjdGlvbi1zdGFtcC12MSJ9 -->
| law | statement |
|---|---|
| done sentinel | a report ends with a unique batch sentinel, written to the file and not only to the session; blocking variants are separate sentinels meaning wait |
| named verification scope | the dispatch names the exact test command and file scope; the agent never chooses scope, and widening happens only by ruling |
| stop on contradiction | an agent that hits a new fork, a contradiction between rulings, or an unauthorised surface movement stops and reports rather than improvising |
| survey then stop | a batch that must survey before it builds runs a read-only phase, writes a stop sentinel, and waits for a written go carrying the rulings |
| no names, no machine paths | no personal paths and no personal names in code, tests or docs; rulings are attributed to the maintainer |
| trailer-free commits | commits carry no trailers of any kind |
| no implementer push | implementers never push; the landing rebase, merge and push are the manager's |
| worktree isolation | work happens in a fresh linked worktree; the shared checkout, any running daemon and any state directory are never touched |
<!-- /playbill:block:pub-6e3c7f9c31bbf6372bd95c9c543dd416 -->

### Governed: verification
<!-- playbill:block:pub-109cd9a1b1728327bc0a5c7b092337b7:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTI1MTA2ODRkN2QzNDczNjUwODI1ZWIwNzA0OTlmMzNmIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6ZTMwOGM5ZjQwZTEwMWJkOTI3NzgwNWFkMmE3OGU5NzQzM2E5MThiMzkzYjg2MTE3MWE2Yzk3YzQxZjViNzMyNyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItMTA5Y2Q5YTFiMTcyODMyN2JjMGE1YzdiMDkyMzM3YjciLCJib2R5X2RpZ2VzdCI6InNoYTI1NjplZWI4ZDFmNmNiMGUzNTA2ZDk2ZmQzMmNkMTA3NDRlZTE4ZmFlNjYyNjQyYzUzYTk0ZDBhYzJkNDU0YWFkZTdmIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYXdzIiwidGFnIjoicGxheWJpbGwtcHJvamVjdGlvbi1zdGFtcC12MSJ9 -->
| law | statement | origin |
|---|---|---|
| no parity, no full suite | no parity script, no full-suite run and no golden suite anywhere during implementation; verification is the batch's named scope | — |
| no goldens unless pinned shapes move | the golden suite is never run by an implementation batch; in-scope pinned fixtures are regenerated through their updaters with the diff reviewed, and hand-editing one is forbidden | — |
| surface-batch guardrails | any batch changing a user-facing surface runs the guardrail suite and the target-visibility test regardless of its subject-matter scope | `pc-df1` |
| the whole CLI directory | any batch changing CLI options, help text or docstrings runs the whole CLI test directory, not a hand-picked file | `p2-b6` |
| the whole client directory | a batch widening a shared contract vocabulary runs the whole client test directory in its landing matrix | `p2-b2` |
| the typecheck command, verbatim | every landing matrix runs the literal CI typecheck over both source trees; typechecking changed files is not verification | `pc-df3` |
| isolated state root | parity and every landing matrix run with an isolated state root, and the summary line is recorded rather than a bare claim of green | `p2-c-serial` |
| paste real output | reports paste the actual final output of every command; a claim of clean without the pasted line is a report defect, and red is reported, never described as green | `pc-c1` |
| named self-attack probes | every runtime unit's exit condition names its own self-attack probes, and the report lists them | — |
| the clock table | a batch touching a time-bearing field delivers a table classifying every one, and states a mismatches line even when empty | `p2-b5` |
| forbidden-path grep | committed files carry no home-directory literals, no path insertions and no in-repo scratch; the manager greps every returned tip and a guardrail enforces it | `p2-b2` |

Four of those exist because a batch was verified in a way that looked thorough
and was not. The typecheck law came from a broken class that lived outside the
changed set, so a changed-files check reported success while CI was red. The
whole-CLI law came from a review finding two red tests at head that both
verification scopes had excluded. The isolated-state-root law came from a
recorded green run that could not be reproduced and whose output was gone. The
paste-real-output law came from three separate reports claiming clean results
the reviewer then disproved by rerunning the commands.
<!-- /playbill:block:pub-109cd9a1b1728327bc0a5c7b092337b7 -->

### Governed: review
<!-- playbill:block:pub-37339468370cd4c241215199a1f9645f:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWVkZmNlY2UzZmQ2NGMyMjgzNDQzN2MxZWRmZTI5MjY0In0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NTM1ODZkMzA1ZTM3N2RkN2VmOWNjN2E2ODY2NDZjMzZhMWQyZGExODAyMzY0ODcxMGNlNzdiNTU2MWE3ZDFkOSIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItMzczMzk0NjgzNzBjZDRjMjQxMjE1MTk5YTFmOTY0NWYiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjpiNjFkMGViNjdhMDYwNTkyYjJjYmJiNDI4MTI2NGEzMTcwYmFkMWJhODZkODA5YzZiZGIzYzlkY2YxNjgyMThjIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYXdzIiwidGFnIjoicGxheWJpbGwtcHJvamVjdGlvbi1zdGFtcC12MSJ9 -->
| law | statement | origin |
|---|---|---|
| never solo review | every substantive tip gets an independent adversarial pass, and large batches get two on different lenses | — |
| the branch is frozen under review | a branch under review does not move for implementers until the verdict lands | — |
| no fix round off an interim verdict | a fix round waits for the reviewer's final marker; dispatching off an interim file breaks the review freeze | `pc-hr-batch` |
| the fix-design gate | for every blocking or major finding the implementer posts a short fix design before writing code, and the manager approves or redirects | `p2-b2` |
| the manager pre-check | before any reviewer is launched the manager reruns the prior confirmed probes and the finding probes, reads each blocking fix against its ruling, and reruns the report's commands | `p2-b2` |
| rerun the confirmed list | a re-review reruns the previous round's confirmed properties, not only its open findings, because a fix can flip one | — |
| hold-class declarations | a renamed, inverted, weakened or deleted test is a succession: declare it with its successor and that successor's exact assertion. A rename is never a fix | — |
| code-truth citation | a claim that something is unimplemented or dead enters the record only with a code citation; a surface probe may assert only that it is unreachable from that surface | — |

The code-truth law has the most instructive origin. Probes were surface-fenced
by design, so they could not distinguish not-built from built-but-broken. Their
findings were promoted into implementation-status claims without a code pass,
and a whole cluster of conclusions was later overturned. The lesson recorded at
the time was that the missing failure channel and the audit defect were the
same defect: an observer with no way to see the difference becomes a guesser.
<!-- /playbill:block:pub-37339468370cd4c241215199a1f9645f -->

### Governed: landing
<!-- playbill:block:pub-28973e04459d20485126e98764193df8:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTJkMjA2ZTQ3MWZmNzBhYTc3ZDZlYmNlYWY1M2IxNDAxIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6OWZlNjc2YTU4ODI2N2JmNDQ3NzU2OTJiZjY0OTZhYzdiYzNlZTA5NDliYWIwMTc0NzBhN2E3MDY2Y2Q2YjQ5OSIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItMjg5NzNlMDQ0NTlkMjA0ODUxMjZlOTg3NjQxOTNkZjgiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo4ZDY4MDVjZTkyNDUyMTQxYzFjZDg4ZjJjMWFlNzBmNDA3Y2RiNjExMTQzMTRmZDY5ODNlMTNjZDk0NDkyZGRjIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYXdzIiwidGFnIjoicGxheWJpbGwtcHJvamVjdGlvbi1zdGFtcC12MSJ9 -->
| law | statement | origin |
|---|---|---|
| one atomic freeze commit, last | every frozen-surface re-pin lands in exactly one commit, made last, generated only by the updater scripts; a landing rebase re-makes it last | — |
| updater-generated counterfactuals | snapshot counterfactuals are generated through the actual updater in a scratch tree, never by constructing models in memory | `pc-p2a-fix` |
| invariant pin sweeps | frozen-pin audits are driven from the pins — every pinned constant equals a live recomputation at the tip — never from the diff, and gate-equivalents are forbidden in certifications | `p2-b0` |
| the exact forty-character pin | reports state the exact full tip and any review request pins the exact reviewed full commit; a short form never matches | — |
| the detached gate | full runs are forbidden in implementer and reviewer sessions; the one full run per landing is the manager's detached gate, and anything it surfaces returns as a fix round | — |
| tree identity | when a merge's tree is byte-identical to a frozen tip that already passed the literal gate, the tree check plus the recorded run satisfies the gate | `p2-b0` |
| gate on the summary line | landing commands are never chained after a verification invocation without gating on its summary line; a failure gets a revert, never a force push | `hotfix-3` |
<!-- /playbill:block:pub-28973e04459d20485126e98764193df8 -->

### Governed: product laws that became method
<!-- playbill:block:pub-3a11d73c8c05794ca5e6d1634b980221:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTI2OGY4OGJmMjIyZDRiMmRhOGU3NGQyYTAxZGQyZTk5In0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6M2EwNjQ2NzQzNTllMWJhMTJjNTAyZjI0NTE4Y2Y4NTZjY2RjMDYyNzBiMWY2ZWNlMmE4OTMwOTBlMTFhMzI0NSIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItM2ExMWQ3M2M4YzA1Nzk0Y2E1ZTZkMTYzNGI5ODAyMjEiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo3ZGNlMzYwMDg5NjE4ZGM4NjUyOTk4MmFjYjM3MTlkYTljZjhiZmRlNTBiZGQ3MjljNTRjMzE0OTkzYWZhODdiIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYXdzIiwidGFnIjoicGxheWJpbGwtcHJvamVjdGlvbi1zdGFtcC12MSJ9 -->
Two rules are about the code rather than the process, and are here because
every later batch is checked against them.

| law | statement | origin |
|---|---|---|
| no moving aliases | no acceptance gate, law selector or exemption may compare against a moving current alias; selection is by exact retained coordinate or by installed-lineage membership, enforced by a syntax-tree guardrail over both source trees | card 34 |
| deny on real on-disk names | containment checks decide on the real on-disk path, walked component by component with no-follow descriptors and matched against actual entry names, and the deny list is case-insensitive everywhere; over-denial is acceptable, under-denial never is | `p2-b4-u2` |
| replay predicates | any change to a replay validity predicate enumerates the operations that can produce the newly invalid shape, and a matrix guard asserts replay survives every accepted public write | — |

The alias law came from three incidents of one shape in a week, the last of
which would have made accepted records unreplayable from a fresh clone at the
next law revision. The deny-list law came from a review that drove uppercase
spellings of configuration, key material and managed state to a provider child
end to end on a case-insensitive volume. Both are now mechanical: neither
depends on anyone remembering them.
<!-- /playbill:block:pub-3a11d73c8c05794ca5e6d1634b980221 -->

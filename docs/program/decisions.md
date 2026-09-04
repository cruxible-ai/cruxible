# Decisions

Rulings that constrain the work. Each row is backed by a `dev.decision.ruling`
claim carrying the ruling text as exact content, and a `dev.decision.standing`
claim. Where a ruling was reached inside a batch, a `dev.decision.decided_in`
claim names it.

A decision is not a law. A decision closes a question about the product; a law
governs how the work is done. Rulings that create laws are both, joined by the
law's origin claim; `laws.md` holds that side.

Statuses: **ratified** (closed and binding), **proposed** (put and not
answered), **deferred** (deliberately parked, which is a state and not an
absence), **superseded** (replaced by a later ruling), **retracted** (never
validly ruled). A retraction is a status revision. Nothing is deleted.

### Governed: shape of the authoring surface
<!-- playbill:block:pub-3e437456ce2498f1fd841f3d097db4e7:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWYxYjUzYTZjZWFmZTlkM2Y3ZDEwZTY0Mzg1YjhkY2MzIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6N2UwNDgxYTI4YTA0Mjk0ZGQ2Y2I2MGZiODI3MzMyZjg5YTJiMmIxNGQ3ZTVhMjZjMDU5Yjc0NmFiYzgyOTIwMiIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItM2U0Mzc0NTZjZTI0OThmMWZkODQxZjNkMDk3ZGI0ZTciLCJib2R5X2RpZ2VzdCI6InNoYTI1NjpjNWM1M2JkMjJmYTUxZDczOTkxYWE3ZmFkY2M0M2RiMjUxNjJkMzA4M2VhMTZmMTc4ZjY2YzEzMWEwZDRiZGIwIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1kZWNpc2lvbnMiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
| decision | status | ruling |
|---|---|---|
| no bespoke propose verbs | ratified | every governed proposal converges on the authoring coordinator; no new verb, ever |
| one-to-one authoring changesets | ratified | one intent, one lowered tree, one proposal, one generation, carrying any mix of members and admitting or refusing whole |
| multi-claim before the program instance | ratified | multi-claim generations move ahead of this instance in the order |
| two-speed revision model | ratified | revise when the fact changed; succeed when the shape or existence changes |
| citation detachment is illegal | ratified | no actor may switch out another actor's evidence; citations only accrete |
| capture restructure | ratified | captures become claim-independent records and citations carry the edges, so dependence is structural |
| corroboration build promotes | ratified | evidence means external captures, corroboration means internal cross-claim gating, and the names must diverge |
| no rendering in v1 | ratified | the same facts have many valid representations; the model writes the prose, and drift is handled by policy on existing machinery |
| declared, not rendered | ratified | a projection block is agent-written text optionally declared as reflecting claims; the system never writes prose or splices bytes |
| the native surface is de-scoped | ratified | generated projections are dropped; sanctioned projection blocks are the native surface |
| the served surface freezes after B5 | proposed | no new verbs in v1; effort goes to refusal quality — put, never answered, and two later batches added verbs |
<!-- /playbill:block:pub-3e437456ce2498f1fd841f3d097db4e7 -->

### Governed: authority and approval
<!-- playbill:block:pub-3b286c4571f601b9bf86ba0ebbc53543:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTBmZDM1ZjgxNmU4MGZhYTNhYmJkNThiNmE3ODFiMTgzIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NDAzODU3ODQ0NTQ1MmVlNzlkNjhjOGEwZTIzODk3YTBiOTdkOGJkOTdhZDM0MmQ3ZTE0OGI0OGEzZWY4NjFmZCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItM2IyODZjNDU3MWY2MDFiOWJmODZiYTBlYmJjNTM1NDMiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo0YTIzYTRhNzIxYTdlOTMzOTYyNDY5YWNhZmU0MTIxMjE2MzE5NTZmYWEwNGIxNjk1ZTk4ZDEyMWFiYjRjM2QzIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1kZWNpc2lvbnMiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
| decision | status | ruling |
|---|---|---|
| roles: full removal | ratified | per-artifact role lists reserve the wrong shape; delete them from the wire and keep credential tiers |
| roles off the hot path | superseded | keep the machinery dormant for a future ownership design — amended by the removal above |
| approval-friction removal | ratified | solo init is legal, governed approval is opt-in, and the approval policy is genesis state with a one-way ratchet |
| honest custody | ratified | local key directories are attribution and repository hygiene, not a security boundary |
| the attestation annex | ratified | outstanding evidence follows the lineage and never blocks succession; only an admitted account resolves; mechanical rederivation never adjudicates |
| the line-mandate association | ratified | a line's mandate is the accepted mandate whose pinned procedure equals the line's, with no new field on the line |
<!-- /playbill:block:pub-3b286c4571f601b9bf86ba0ebbc53543 -->

### Governed: compute and acquisition
<!-- playbill:block:pub-2e9d9b3761380a0901156f5706485863:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWJmYmM4YTBmNjQwMDQ2YWNlOTdhODlkYzA3MTY1NTYwIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NGZlZDY4NGQ3MzNlYjZjOWQ5YWRhMzJkOTdlNTk0ZTU3ZTA2YzJkNTI5MmZhNmYyNjk1OWZhNGIwYjlhNjVjOCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItMmU5ZDliMzc2MTM4MGEwOTAxMTU2ZjU3MDY0ODU4NjMiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjoxNGZkNzQzNjQyZDdlY2IwNjZhODg3YWM2NjZlMjUwNzJiYmJhNTEzYWQyMjJiZmI4Mzc1MDIzMWFkMjA0NTM2IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1kZWNpc2lvbnMiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
| decision | status | ruling |
|---|---|---|
| do all node kinds now | ratified | procedures cannot exist without providers; twelve of thirteen kinds ship, in three dependency batches |
| seam counting | ratified | budgets charge where the dataflow iterates, never by inspecting value shapes |
| collections enter the vocabulary | ratified | the schema gains a list type with nested item fields; bounds are boundary checks, meters are cumulative |
| the typed halt terminal | ratified | a guard's false branch may end the run with a typed no-result outcome |
| source and provider converge | ratified | one invocation protocol, one registry, one receipt field set; a source is a provider invocation landing as a capture |
| seed by proposal | ratified | built-ins enter every instance as ordinary governed proposals of compiler-shipped bytes |
| local materialization | ratified | a local world may pin a locally built artifact; registry distribution pins are required only for the sanctioned hosted roster |
| no hardcoded limits | ratified | the provider output cap lives in a seeded policy, resolved at admission and recorded in the receipt — never in code |
| the floor re-baseline goes first | ratified | it lands before anything pins a materialization digest |
| erasure, third option | ratified | the daemon reports material unavailable by policy and never claims erasure; claims are never erased |
| the served line-run trigger | ratified | a line run is a served request carrying line identity, occurrence and evaluation time |
| succession no-op detection | ratified | activation keys on the layout-normalized definition digest, so a pure reorder refuses instead of minting a generation |
<!-- /playbill:block:pub-2e9d9b3761380a0901156f5706485863 -->

### Governed: settlement and calibration
<!-- playbill:block:pub-fb8d7b308569a2366cd018b186ff1fb3:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWE4MWM1NmE3NWZlYmEwMGI0NGI4ZDkxODFjODFmYzlhIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NjQ5MjJhNThhNThhZjU3ZmQ4ZmMwZDAyODc1YWNmYWIyYTg2MmNkYmRiZDBlZTA1Y2Q1N2FmMTMxYmNmYmFiZCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItZmI4ZDdiMzA4NTY5YTIzNjZjZDAxOGIxODZmZjFmYjMiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo3MDVhYWYxZGQ1YjM4MmFlN2RhYWM5MjAxYTI2YWExODdiZjljMTJlOTQ1M2EyZmQ1YjE2ZWUxNzBjZjdkYmU0IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1kZWNpc2lvbnMiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
| decision | status | ruling |
|---|---|---|
| the three-family law | ratified | policies constrain and change by proposal; readings inform and change by compute; mandates grant and change by ratification |
| calibration cycle v1 | ratified | only settled predictions calibrate; readings are pinned artifacts; cold start uses no synthetic priors; human attestation never enters |
| settled is independent of verdict | ratified | settled filters rows, the outcome scores, the verdict is ignored — selecting on verdict would condition the sample on evidential standing |
| the producer receipt | ratified | the receipt a capture binds is the topological producer commitment, never caller-supplied |
| calibration is implementation-local | ratified | the cohort key is the exact procedure digest plus the provider implementation digests; successors start cold |
<!-- /playbill:block:pub-fb8d7b308569a2366cd018b186ff1fb3 -->

### Governed: repository, ledger and lanes
<!-- playbill:block:pub-762da96c5eca07d641b99a52ee196a3e:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTU0Y2I2NDM3NDIwYThjM2RjMDBmODNmYzk1ODYxNDQ5In0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NTY2OGM2MWZlNjYxOTI5ZWUyMTcwMTMxNDI3ZjljNzg4ZDQ4YzM3NDkxZjYzZmI2MDg4OGE4MjUyNjUzNWRlNiIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItNzYyZGE5NmM1ZWNhMDdkNjQxYjk5YTUyZWUxOTZhM2UiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo1NDE2ZmUyZTcxYTI0NDc0NzFkNmZlOWYxOTdkODhiNmVhNWQ3Y2UyYWZmY2JjMjk5NDRiMDNmYmRlZWYzMGM1IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1kZWNpc2lvbnMiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
| decision | status | ruling |
|---|---|---|
| SHA-1 ledgers | ratified | git viewers do not recognise the other object format; inherit the workspace's, else SHA-1, with an explicit option on init |
| the mirror column | ratified | private per-instance mirrors, accepted ref only, a repository-consistency gate, a checked-in trust file, unreachable treated as neutral |
| the nine mirror rulings | deferred | relayed with recommendations and parked until the acquisition track was done |
| private ledger remotes | ratified | the dogfood repository gets no remote; each instance's ledger gets a private one, pushed after every activation |
| the integration branch does not merge | ratified | it stays the integration branch until the work is ready to publish; certification does not trigger a merge |
| the providers repository is public | ratified | the deploy-key question closes and CI checks it out at a pinned commit |
| the providers repository is private | superseded | kept private for now, with the flip a later call |
| succession is tested retroactively | ratified | a fresh instance first; the old instance is kept as the succession fixture and never retired |
| procedure output is rung 2 | ratified | derived claims arrive as proposals, reviewed by an agent and activated by the world's own principal |
| the epistemic eval | ratified | convergence, distribution and adoption readings on the real loop |
| the dogfood domain is security | ratified | our own dependency surface as asset inventory, with feed, advisory, lockfile and git sources |
| self-host the development program | ratified | register the program as governed state: rulings, verdicts, landings — the decision this instance exists to satisfy |
<!-- /playbill:block:pub-762da96c5eca07d641b99a52ee196a3e -->

### Governed: retracted and reversed
<!-- playbill:block:pub-885417d86d44ef506d865305fd03edba:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWIxNzlkYzFjYmJlZmZiNjllNzVmNmY0ZjRjOGE0ODgxIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NTEwNTY1NjRmODVlN2EzNWYzMWE2NjcyNTNmM2E4MTM5YzlkMzM2Zjk5NGQ3MzIxMjRlZDQzMGVmNzNjODcxYiIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItODg1NDE3ZDg2ZDQ0ZWY1MDZkODY1MzA1ZmQwM2VkYmEiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjpkZDg1ZTIyY2U0ZTZjZDVjMDc4ZjE0ZTlkZDc4OWRmYWJlYjllMjJjMTJmNGE3Njk3MDhjODViNTBlZWUwZjBhIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1kZWNpc2lvbnMiLCJ0YWciOiJwbGF5YmlsbC1wcm9qZWN0aW9uLXN0YW1wLXYxIn0 -->
Three entries above are not live, and they are the most useful rows on the
page, because they are what the record is for.

`l2b-ruled-b` was registered as a ruling from a maintainer's leaning rather than
a ruling, caught, and retracted before any code was written; the actual ruling
was a deferral. Its status claim reads retracted and its text is unchanged.

`roles-off-hot-path` was ratified and then amended eight days later by a
stronger ruling that deleted the machinery instead of parking it. Its status
reads superseded, not retracted: it was validly ruled and then replaced.

`providers-repo-private` was a real decision that a later decision reversed. It
is superseded for the same reason.

The distinction is load-bearing. Retracted means the ruling never had standing.
Superseded means it did, and stopped. Deleting either would make the ledger
agree with the present at the cost of being unable to explain it.
<!-- /playbill:block:pub-885417d86d44ef506d865305fd03edba -->

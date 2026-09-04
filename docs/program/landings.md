# Landings

Every batch that reached the integration branch, oldest first within each
track. Each row is backed by a `dev.batch.state` claim and a
`dev.batch.landed_at` claim pinning the full forty-character commit.

The commit named is the landing commit, which for most large batches is not the
commit a reviewer signed off: a landing rebase re-makes the freeze commit last
and rewrites the chain. Where the reviewed tip differs it is recorded as a
qualified tip claim on the same batch, so a reader can tell which bytes a
verdict actually covered.

### Governed: the outward-face track
<!-- playbill:block:pub-9e9c6c9b3242ecb548c282e5e10495c8:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTU1MTk1N2IyYTI0MzEwYmIxNGI3OTc0ZWU2YTc0M2FiIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NTZhYTkwMjI1Y2UzZGQ5OTJiMzlkOTMzNzk5M2RlYzllNWY1MjYwNWEzNjhlMzJiMWJkNzU3Yjg0YTk1OTg2MSIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItOWU5YzZjOWIzMjQyZWNiNTQ4YzI4MmU1ZTEwNDk1YzgiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo4NDlkMmQyZjc0ZjVkMjA5Y2MwNTU0NDVkZDY2ZDI5MDQ3YTg2MWU1YmY5ODE3ZmMyNGNkZGM3YzVkZTlhNjkwIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYW5kaW5ncyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| batch | landed at | what it carried |
|---|---|---|
| `pc-g-s1a` | `c30e0499e3d0f3df4a4a0a96bcc3a3fcf3435ecc` | knowledge-loop services for the surface slice |
| `pc-g12b` | `2ec518d51f85cb13d81a9bdc26afba1355d16ed0` | source-local scan proofs, needle route, anchor liveness |
| `pc-g12c` | `430a601a4416cc4dbec5a1438ed1a548f96a47aa` | claim retirement with attributed closure |
| `pc-g12d` | `7888329173b8c0bd6588252d306b81fb918867ef` | publication v2 under the block-frame ruling |
| `pc-g12g` | `31020a98ed6311020e58f05fd0326eaee5c41496` | curation precision, calibration table, replay guards |
| `pc-g12h` | `17439f070833e76885f0363ad66258872ea66b22` | the probe-found defect batch and the prepare collision warning |

`pc-g12f` and `pc-g-s9-nofire` are on this track and did not land. The first
self-declares complete but its tip is not in the branch history; the second is
an investigation that stopped because the correct repair was not legal under
the rules it was dispatched with.
<!-- /playbill:block:pub-9e9c6c9b3242ecb548c282e5e10495c8 -->

### Governed: the capability pass
<!-- playbill:block:pub-5bef7d6712fcb8aa7efa2e23269ea714:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWUzZWI0NGI2ZTVmYzI1YjNlMmMwODEyMGI4NDNhMmVkIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6MDY1ODE3ZTM1MzUxMTUxMjdhMTBlZWQ1MmQ5ZDRmOTRkYmEyMTAwODg4ZjY5YzY3ZTMyYjU1OWNkMzg2NWU5NSIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItNWJlZjdkNjcxMmZjYjhhYTdlZmEyZTIzMjY5ZWE3MTQiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjpiNTIzM2M4ZDZiNzNjNjNjNDNkZDM5N2FkNzJiMWNmNjcyMzNmOWJkNDBlZmEyOWU1ODgzYTBhYTFkYzY4NzI5IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYW5kaW5ncyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| batch | landed at | what it carried |
|---|---|---|
| `pc-del1` | `1b22dab72337cf86d81f71f03b50a473529eaad9` | the old-cruxible deletion cut |
| `pc-del2` | `831a4ff74b92d07160f7a9a90c05671ec7a34897` | facade and dormant-governance deletions |
| `pc-fix-lane` | `ae0aa201dfd67d0f5315900334fb8765a6f324a4` | claim-side fixes and the ratified ergonomics set |
| `pc-del3` | `03ea26b80160e24acf895c841bdaf9a8d1c24ec6` | legacy wire and zero-use surface retirement |
| `pc-c1` | `d2c57754545ec25cc7897c285cdd9603e3dd4af4` | corroboration admission, role-free wire, coordinate guard |
| `pc-c2` | `8fb36ceb44023da1965061f9514ae1f08642fd5b` | cite-existing captures, retirement advisories, citation-relation projection |
| `pc-att` | `37be2257adecafbb1f6d5a38b54ed7d6c2de39ac` | the attestation door |
| `pc-c3` | `e03ba36fc31b5b7d71ae1ce4933d7ee76c452e2f` | the claims closing batch |
| `pc-c4` | `26b9b8c403fab8bd93698d3c6677020bef6a847e` | wave-2 fixes |
| `pc-d5` | `15ad8ce9415bd29466bf84aa490be57a94240086` | coordinator convergence, policy inventory, operator closures |
| `pc-d5b` | `a30eb94244883d015b074630b94d83a72a5c6e1b` | acceptance-probe closures |
<!-- /playbill:block:pub-5bef7d6712fcb8aa7efa2e23269ea714 -->

### Governed: the compute interior
<!-- playbill:block:pub-73298bab100e67471148b60854b5c6ce:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWU1NDBjNTAyZGVlN2I2NWYxYjA1MDM3YWU4MDc0M2RjIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NjJhZTdkMmM5ODBkMWM1OGE4ODE2M2IwOTgyMjkyNWNhNzE1MGRiODI5NmIxYmU2NjNjNDAwMmVhYTgxMTVjOSIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItNzMyOThiYWIxMDBlNjc0NzExNDhiNjA4NTRiNWM2Y2UiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo2YmJmMTYwZDU0Y2FkOGJjZDlkMTVhOTdjNzFkNmFmODZkZGM4YTc4MmI3NTA0MDc4ZGZmYjNjM2RlODhhMTI5IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYW5kaW5ncyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| batch | landed at | what it carried |
|---|---|---|
| `pc-p2a` | `d65e22f9e88f64b4ae9f87a8415caec97b6000d9` | five served node kinds, real budgets, replayable receipts, typed failures |
| `pc-p2a-fix` | `c6734998b94bdbb2be6e4f7e2b427e6014c50fa3` | one collection budget law, the typed halt terminal, receipt accounting |
| `pc-p2a-fix-cb1` | `1fcdb9dbf91e96a854208543ef116f9fd0a5e044` | guard-arm halts as graph terminals |
<!-- /playbill:block:pub-73298bab100e67471148b60854b5c6ce -->

### Governed: the human-readable stack and the dogfood lane
<!-- playbill:block:pub-a1487719e764d768fb3bd35c2b33309a:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWQ1OTk2ZjM3ODZlMjM3OWI2Njk5OWVkZWVhNGVmZjA2In0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6YTJiZDE5YWM1ZDZlMzBmMGZmMmE2N2IyZjRmNjg1MjM2NTIzNzU3ODhjOTE5ZTQ5Yjc2YzRiNmU5MGVhOWY4MCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItYTE0ODc3MTllNzY0ZDc2OGZiM2JkMzVjMmIzMzMwOWEiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjphOGUwNDBhZTI5ZGE2NTU5YjQ0N2Q4M2Q3Y2E3NmY1NTVhYWYyZjI0ZmRjOGZkNWNhYWUwNjE3MGFlYTNlOTJmIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYW5kaW5ncyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| batch | landed at | what it carried |
|---|---|---|
| `pc-hr-batch` | `32f976ad1316e043b588e3e1672eab8fd5871972` | pretty canonical artifact bytes, the codec re-key, delta rendering, ledger-as-remote, the layout canon |
| `pc-df1` | `43dfcf37969c2e2608d2d6a09912690ae50da100` | the first adoption-defect set |
| `pc-df2` | `34150f2ca7ddf9cfe4e2b2ebc6205f23c8645123` | the write-brick fix, shape-changing migration, the readmit refusal |
| `pc-df3` | `511796e168f6f13952a6b309eafe8fe229a493a5` | workspace-scoped context, block sync, the surface updaters |
| `pc-df4` | `a410250817a79c8155934a5c847d6b37c1cb083e` | eleven operator and authoring units |
<!-- /playbill:block:pub-a1487719e764d768fb3bd35c2b33309a -->

### Governed: acquisition
<!-- playbill:block:pub-48102b1313ad6507d5091e4d41a63944:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTNkZTEwM2IzNDEwNjBmMGEyY2YyYmE2MDE2ZTM0NDdkIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NjE4YWU5Y2ZlMDEwNjZmZDJmZjRhODg4NDZkODcyYTE5MDEwM2VlMzM1Yzc0MTM4M2MzNWNmYjVlOTU4MzU5ZSIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItNDgxMDJiMTMxM2FkNjUwN2Q1MDkxZTRkNDFhNjM5NDQiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjoyOWNkM2U3YTRjYTUxYjFhNGFiZDQ3ZDlhOGNhYzM2MTI3MGFkZjA0NGE4NDIxODc0NGU0NzM5MzM4NjFkNzY2IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYW5kaW5ncyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| batch | landed at | revision | what it carried |
|---|---|---|---|
| `p2-b0` | `0475460d36ec01603ea1be364fb4a36998f17271` | — | the three-plane replay foundation |
| `p2-b1` | `8c071bd651a12e3c94d16d27fe65dfeec480ece0` | — | the provider interface and graph v4 |
| `p2-b2` | `ea99f07c4f7707216735dfba705ccaf9fc2e4c17` | 16 | the provider runtime, leases, kill law and recovery |
| `p2-b4-u1` | `6cbd786dc7192277c4dec2fc2e32fa33588c4c24` | 17 | Capture v2, the landing journal, the producer-receipt resolver |
| `p2-b4-u2` | `0b8ef0337ce4086d1acf9698404c55d2b6137ada` | 18 | the workspace-file provider, read receipts, seed by proposal |
| `p2-b5` | `2f244715d1fb87168f190014157b22ddf2dc0438` | 19 | the served line-run trigger, prediction and settlement, the clock taxonomy, the v1 wire freeze |
| `p2-b6` | `84da639a17fd7bc8ca6d7350a74349f2a889dfb3` | — | one authoring intent is one changeset: claims, claim types and retirements admitted as members, plural insertions, the changeset builder |
<!-- /playbill:block:pub-48102b1313ad6507d5091e4d41a63944 -->

### Governed: effectful terminals
<!-- playbill:block:pub-ff5166513118628d12453184f2063192:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWIzMWI5Nzg5Mjg1YzI4OTlkNTVhM2ExNTA0ZGZkMjYyIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NmE0MTQ3ODgxZTdiN2FlODBjM2QyMjg4NzMyMjViMzZiZWFhZDM0MWQ3NzZlNzYwMmRiNGZkYjUyYjM4MjBlNCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItZmY1MTY2NTEzMTE4NjI4ZDEyNDUzMTg0ZjIwNjMxOTIiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjoxZGFmY2UyMmZiMDQ2Mzc1YWRiMjUxMjMwN2QyZDM4NWJhZjFjYjk2Yzk1NzYwMzcwNzVjNDE4NmFlZjcyZWU4IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYW5kaW5ncyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| batch | landed at | what it carried |
|---|---|---|
| `p2-c-lane2` | `2fee556d79da5276b87ef502e26537a44bd27a67` | resolution-contract successors, the settled-outcomes fold, calibration readings |
| `p2-c-serial` | `d1a07d6340674e30b6636151d2a886437ec652ce` | effectful terminals, mandates, the rung ladder |
<!-- /playbill:block:pub-ff5166513118628d12453184f2063192 -->

### Governed: hotfixes
<!-- playbill:block:pub-315426608b4f33d44389b329d22ea13f:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTBjMzEzZDdkOThiZmU0MmE5NzI3YWU2ZjNhZWNkNDE5In0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6ZDNhMjFkNzIyNGI1MzJmYTRmNTJkMmY0M2Y4NDFhYWZkNjJmZWE2MzMxZDYzN2VkNGFlZWZlZTZiODgzYjQ0NiIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItMzE1NDI2NjA4YjRmMzNkNDQzODliMzI5ZDIyZWExM2YiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo4MzU4OTZmOTQzMWM3MjM5YzUzZDEwYzY0NzM0OGI1NTU3Zjc3ZTY3OGE3ZWY3ODMxMjdiNzI0MTlmZWJjNGM3IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYW5kaW5ncyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| batch | landed at | what it fixed |
|---|---|---|
| `hotfix-1` | `0b2505265cde3bcf436906e9efdd5d90278649ef` | three stale test selectors left behind by the codec move |
| `hotfix-2` | `3cbeff5737024182ac0bd77ec0fdf64e7a71fb8d` | the legacy subject shorthand the codec change silently broke |
| `hotfix-3` | `ead316f266ee5373d58c44fd29efbce73bea535a` | a five-hundred on daemon-scope operations, and a credential message that names its repair |
| `hotfix-4` | `975ef4f09ac0b9d1d0ed7385ac0a936bcbc5f94a` | the handshake that had broken every SDK connection, plus two inherited pins |
| `hotfix-5` | `620bc0e9300734995d3f905699b52e7f1d4b8391` | stale pins, test isolation, one shared custody resolver, the inherited-git scrub |
| `hotfix-6` | `81ceb40234792f651341d52a48a1724231724c5e` | all fifty-five parity failures at the freeze tip, eight of them real product defects |
<!-- /playbill:block:pub-315426608b4f33d44389b329d22ea13f -->

### Governed: certification
<!-- playbill:block:pub-8b85642e335dcc4c8c834a1e1f829bac:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLThiZWM0YjJmMGU5ZDFmYTI2OWRjMjhlMDEzYWQ1ZjRhIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6Yjc0NzYyYTA5NTY3Y2JkODJjZTQ0OGQ1ZDZmZDc4Njg5NzZhY2MzOWVmNjc5MjEwMzcxMzFhNzRjMDlmNjcyZCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItOGI4NTY0MmUzMzVkY2M0YzhjODM0YTFlMWY4MjliYWMiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjpiNzBkYjNlOTNiMTUxOTdlNTQ5MjE4ZDQ3MzExN2QwNjg1YmRmNTk5NGNiMGU4NDFkOGMxNmE5YTI1YWRjZGQxIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1sYW5kaW5ncyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
Three batches reached certified rather than merely landed, meaning an
independent run of the full literal gate was recorded against their exact
bytes: `p2-b0`, `pc-d5` and `pc-d5b`, and the acquisition track as a whole at
`81ceb40234792f651341d52a48a1724231724c5e`, where the second full run came back
green with every earlier failure closed and attributed.
<!-- /playbill:block:pub-8b85642e335dcc4c8c834a1e1f829bac -->

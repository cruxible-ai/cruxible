# Friction cards

Eighty-three cards, written in the moment while using the product as an
adopter would. Each row is backed by a `dev.card.disposition` claim, a
`dev.card.lane` claim, and where one exists a `dev.card.closed_by` claim naming
the batch that closed it.

A card is not a bug report. Positives are cards too, and they carry weight:
they are the only record of what worked without help, which is what a later
regression is measured against.

### Governed: the first standup, cards 1 to 22
<!-- playbill:block:pub-ddf9b4b771263034da0e887066731e4f:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLWM4NmQ3ZWJiZDU2ZjBjNjQ5NDNlMDgwZWY5NWVkMTZkIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6MjNmNGQ4YWU3Y2YzM2E1YzBjYzdiNzY4ZTE0MTVhMGZhOTZlOTk1OThkZjJmN2U1OTMxNGE3MTcwOWZiMjQwMSIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItZGRmOWI0Yjc3MTI2MzAzNGRhMGU4ODcwNjY3MzFlNGYiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjpmZDY5YTMzMjE4MDRkOTBkZmMxYWMyOTg3YWM4OTMyZjljNTBlM2ZlMzIwMmQ4NWI4MTI1ZDgyNjFmMGM0NDBiIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1jYXJkcyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| # | kind | card | status | closed by |
|---|---|---|---|---|
| 1 | positive | the solo init path is documented and worked first try | closed | — |
| 2 | friction | the quickstart never mints a labelled credential, and that decides your identity later | open | — |
| 3 | friction | the shipped vertical kit does not exist at this head | open | — |
| 4 | friction | the claim-type example option was removed | open | — |
| 5 | friction | canonical subject ids reject the identifier format of the whole domain | open | — |
| 6 | mixed | anchor ambiguity on lockfiles: excellent refusal, no way to act on it | closed | `pc-df1` |
| 7 | positive | a real two-source severity disagreement surfaced as blocking, unprompted | closed | — |
| 8 | friction | the CLI accepts a source catalog the SDK refuses | closed | `pc-df1` |
| 9 | friction | you cannot publish a governed block from the CLI alone | closed | `pc-df1` |
| 10 | probe | coverage resolves lockfile spans exactly; the publication only reaches candidate | closed | `pc-df1` |
| 11 | positive | the hook annotates a real search with real provenance | closed | — |
| 12 | friction | three silent empty-result dead ends before the hook produced anything | closed | `pc-df1` |
| 13 | friction | a stale exported token silently shadows the gate's credential | open | — |
| 14 | positive | the CI gate does exactly what a security team needs, first run | closed | — |
| 15 | positive | the uncovered-claim row carries a hint that named exactly what was wrong | closed | — |
| 16 | positive | the full adjudicate-and-clear loop worked end to end | closed | — |
| 17 | positive | freshness-sensitive retirement produced the right advisory on the right artifact | closed | — |
| 18 | wishlist | the three things that would most shorten a real adoption | open | — |
| 19 | positive | closing note on what needed no help at all | closed | — |
| 20 | friction | the ledger and the repository use different object formats and git will not bridge them | closed | `ops-hotfix-1` |
| 21 | mixed | one-line canonical artifacts make the diff unreadable until you add a text converter | closed | `pc-hr-batch` |
| 22 | positive | the gate protects the schema book, but via markers rather than coverage | closed | — |
<!-- /playbill:block:pub-ddf9b4b771263034da0e887066731e4f -->

### Governed: cards 23 to 46
<!-- playbill:block:pub-1ec54e118a0731254c51d9b8565ac74d:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTJhODEzNzRjYmVkOGFjZGUzNTRjNTUzYzUyZDE5YjQxIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6MWMxMWYwNTE0NDFhZGM2OGM5M2YxY2RiYzdhYzI0YzY1MTkxYWQxZTQxZmMzMmJjODI2YzhmZGUxNDJkNDExNCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItMWVjNTRlMTE4YTA3MzEyNTRjNTFkOWI4NTY1YWM3NGQiLCJib2R5X2RpZ2VzdCI6InNoYTI1NjoxNzE0MTdlNjY2YTQ2YjdhYjhkZDA3MWZkYWRiN2Y5OWJmZGE2ZDVlYTU1ZmY1MjM1NWIxYzRjYjQ4Y2E2MjNmIiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1jYXJkcyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
These were written in a working session rather than to the log, and were
backfilled afterwards from a transcript. The backfill recovers a paragraph for
some and explicitly records that no paragraph survives for others. Cards 27, 31
and 37 have no recoverable text at all; they are carried by number, open, with
that ambiguity attached rather than invented.

| # | card | status | closed by |
|---|---|---|---|
| 23 | a legacy registry in a real home directory obstructs unrelated test and daemon state | closed | `pc-df4` |
| 24 | init must not consume a remembered context | closed | `pc-df2` |
| 25 | either ship the key-reuse procedure or refuse it | closed | `pc-df2` |
| 26 | init validates its preconditions before minting | closed | `pc-df2` |
| 27 | *no text recovered* | open | — |
| 28 | the re-seed case | closed | `pc-hr-batch` |
| 29 | host allocation, first of a pair | closed | `pc-df2` |
| 30 | host allocation, second of a pair | closed | `pc-df2` |
| 31 | *no text recovered* | open | — |
| 32 | a zero-occurrence anchor refusal reports ambiguity instead of absence | closed | `pc-df2` |
| 33 | the CLI and SDK refuse version skew with a typed message rather than misbehaving | closed | `pc-df2` |
| 34 | the write-path gate bricks every instance on every upgrade | closed | `pc-df2` |
| 35 | shape-changing claim-type migration is impossible | closed | `pc-df2` |
| 36 | readmit creates a new proposal at the same stale base | closed | `pc-df2` |
| 37 | *no text recovered* | open | — |
| 38 | host create dedupes on the recorded root and reports a false created | closed | `pc-df2` |
| 39 | plain init, a socket daemon, attach before init and an open-only mirror give the whole review story | closed | — |
| 40 | rendered cards in candidate trees | closed | `p2-b5` |
| 41 | the review-refs mirror script and the fetch refspec | open | — |
| 42 | warn when a claim or procedure has no projection in the repository | closed | `pc-df3` |
| 43 | one verb to realign stale blocks to the accepted coordinate | closed | `pc-df3` |
| 44 | remembered-context scoping: one global slot | closed | `pc-df3` |
| 45 | show an instance's workspace binding, transport and attached state | closed | `pc-df4` |
| 46 | per-workspace remembered context | closed | `pc-df3` |

Cards 34 and 35 are the two launch-class defects of the program. The first
bricked every instance on every upgrade because a write gate compared a
compiler coordinate for equality instead of lineage membership. The second made
a shape-changing migration impossible, so a claim type could never change what
kind of thing its object was. Both were closed by the same batch, and the
review of that batch found the fix selecting on a moving alias — the same bug
shape one axis over — which is where the standing alias law comes from.
<!-- /playbill:block:pub-1ec54e118a0731254c51d9b8565ac74d -->

### Governed: cards 47 to 83
<!-- playbill:block:pub-a58b03c44388ef00edb63044f44059bb:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTkxMWEzNmE4ODhlYTAxOGRiMjU4MDIxNWUyMzc1ZDFlIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6Mjk2OTcxNzAzMWJjYTdmMjFhNGUzZjA4MjgzZWNjMWJiM2Q3NWE1NzhkMDgwZGZjZmY1Zjk3OTVjYWExNmViNCIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItYTU4YjAzYzQ0Mzg4ZWYwMGVkYjYzMDQ0ZjQ0MDU5YmIiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo4MzQwMTBmNzdmODVlMDRmNTI2NTAwNzA4Njk4N2VlNTA0NmFjN2VmOWM1ZmI5MGNlNjYxZTNjY2JkMmRjNDU2IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1jYXJkcyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
| # | kind | card | status | closed by |
|---|---|---|---|---|
| 47 | friction | no packaged service unit, so staying up across reboots is the operator's problem | closed | `pc-df4` |
| 48 | friction | server status says nothing about whether instances survive the upgrade | closed | `pc-df4` |
| 49 | friction | an unauthorised status refusal names no repair | closed | `hotfix-3` |
| 50 | defect | an instance-scoped token calling server status crashes the error mapper | closed | `hotfix-3` |
| 51 | positive | the readmit law fired live with the exact repair | closed | — |
| 52 | friction | readmit by reference returns a bare not-found and the listing hides the id column | closed | `pc-df4` |
| 53 | friction | migrate submit prints a placeholder instead of the proposal id | closed | `pc-df4` |
| 54 | friction | the proposal listing shows the coordinate's timestamp, not the proposal's | closed | `pc-df4` |
| 55 | friction | claim get returns the admission envelope with no statement-first view | closed | `pc-df4` |
| 56 | friction | checking the ledger out in an editor collides with an existing branch name | open | — |
| 57 | friction | floor export refuses a non-empty cache without naming the flag | closed | `pc-df3` |
| 58 | gap | a block describing a claim type cannot be pinned to it, so a migration leaves it stale with the gate green | closed | `p2-b5` |
| 59 | friction | the missing-floor row never clears because export writes a path the observation does not know | closed | `pc-df3` |
| 60 | friction | approve demands a signer id although the bearer already identifies the principal | open | — |
| 61 | gap | attachment never writes the workspace's own config | closed | `pc-df3` |
| 62 | friction | the invalid-floor row carries no reason and a repair that cannot clear it | closed | `pc-df3` |
| 63 | friction | migration preflight with no dispositions refuses instead of inventorying | open | — |
| 64 | gap | no ergonomic path authors a subject-valued claim, so a migration cannot be completed | closed | `pc-df4` |
| 65 | friction | curation sees zero blocks in a workspace with four and says nothing about why | closed | `pc-df3` |
| 66 | observation | the patrol works, but weakness is uniform and the rows are opaque | open | — |
| 67 | gap | a prediction cannot be authored or settled from any served surface | closed | `p2-b5` |
| 68 | defect | the SDK cannot connect to any daemon newer than its own frozen wire version | closed | `hotfix-4` |
| 69 | gap | a revision carries no publication, so a revised block never reaches the page | closed | `pc-df3` |
| 70 | defect | the gate passes with governed blocks bound to superseded claims | closed | `pc-df3` |
| 71 | gap | an instance cannot be decommissioned through any sanctioned verb | open | `ops-hotfix-1` |
| 72 | defect | nothing stops a daemon, and nothing stops a second one over the same state root | open | `ops-hotfix-1` |
| 73 | gap | a body-only amend is invisible to the workspace check | closed | `pc-df4` |
| 74 | gap | no verb attaches an existing registered instance to its workspace | closed | `pc-df4` |
| 75 | gap | a subject's profile does not show incoming relationships | open | `ops-hotfix-1` |
| 76 | friction | one read takes two arguments where every other surface takes an address | open | `ops-hotfix-1` |
| 77 | gap | the tool-protocol init cannot opt out of seeding | open | `ops-hotfix-1` |
| 78 | defect | the governed read receipt records the requested spelling, not the real on-disk name | open | `ops-hotfix-1` |
| 79 | gap | an instance never leaves its genesis compiler revision | open | — |
| 80 | gap | resubmitting a proposal reference orphans the previous commits | open | `ops-hotfix-1` |
| 81 | defect | the shared-profile customer-code gate is never enforced | open | `ops-hotfix-1` |
| 82 | defect | a startup test fails after the server suite | open | — |
| 83 | gap | wire changes outside the contracts module and inside request bodies move no pin | open | — |

Eight cards name a closing batch and are still open: their batch is implemented
and reviewed but has not landed, and a card closes when the fix lands, not when
it is written. Card 81 is the security one, fixed without a deprecation window
because an enforcement function existed with no callers at all.
<!-- /playbill:block:pub-a58b03c44388ef00edb63044f44059bb -->

### Governed: what the cards say as a set
<!-- playbill:block:pub-e6014c6a3f5996ec56c08fdd0035553f:eyJiYWNraW5nIjpbeyJpZGVudGl0eSI6eyJraW5kIjoiQ2xhaW0iLCJuYW1lIjoiQ0xNLTIzNzA2MzNjNmRlMzMwYmQyNmFiOTU0N2JhOTcxNzUwIn0sInN0YXRlbWVudF9kaWdlc3QiOiJzaGEyNTY6NzM3YWRkZjMzZDM2ZWMwYjJmZmIwZTg1NzI3MTlmNmRmY2U2ZmFmODJmYzg0OGUxYmQ0MDUzMGM2Y2M1YjRjNyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tY2xhaW0tYmFja2luZy12MSJ9XSwiYmxvY2tfaWQiOiJwdWItZTYwMTRjNmEzZjU5OTZlYzU2YzA4ZmRkMDAzNTU1M2YiLCJib2R5X2RpZ2VzdCI6InNoYTI1Njo5OGM5MDg3MGUxYTNlNjBjMjQxMDdkOTc3ZGU2NWJmYzY0ZmZiMjkwZDgwOWRkZmRkZjI5NDNhMWFiNzQwZDE5IiwiZGVjbGFyZWRfY29vcmRpbmF0ZSI6eyJjb21waWxlcl9kaWdlc3QiOiJzaGEyNTY6OTdkYzE0NzYwMzQ0NGE2ZjkxMGU5ZWRkZTkzZWQ1NmYyMGUxOTZjY2VlZmEwMjI4MGYyNjU3MjU1M2U1M2NhYiIsImdlbmVyYXRpb25fcm9vdCI6InNoYTI1NjowM2U1MTNkNTU4MzlmOTJjMDkyMDJmNjc4YTc3NDMyNzk5OGJlYTM5YjliNDdiYjM2OWIyZWQ2ZjljNmZlNThlIiwiZ2l0X29pZCI6IjQxMzlkYzk1MTMxNWNkZDNiZGMzOGQ2ZmI2ZjJlNmEyOGE2NzM2NzMiLCJzZW1hbnRpY19yb290Ijoic2hhMjU2OmM4ZDcxOWE2ODRhNjU3MjY0NDc2ODBmYjExNTI4NDNmZGRjZmI0NDdkNjAyODg4ZGU0ZmQ0MmM2YTM3MjQ3MjYiLCJ0YWciOiJwbGF5YmlsbC1hY2NlcHRlZC1jb29yZGluYXRlLXYxIn0sImRlY2xhcmVkX2dlbmVyYXRpb24iOjExLCJncmFtbWFyX3ZlcnNpb24iOiJwbGF5YmlsbC1wcm9qZWN0aW9uLW1hcmtlci1ncmFtbWFyLXYxIiwic291cmNlX2lkIjoicHJvZ3JhbS1jYXJkcyIsInRhZyI6InBsYXliaWxsLXByb2plY3Rpb24tc3RhbXAtdjEifQ -->
Twenty-one of eighty-three are positives or probe results that passed. That
ratio is the point of keeping them in the same numbering: a log that records
only friction cannot tell a regression from a thing that never worked.

The recurring shapes are worth naming, because each produced a law or a
decision rather than only a fix: a surface that refuses correctly but offers no
way to act on the refusal; a read that exists on one surface and not its
sibling; an operator concern with no verb at all; and a gate that passes while
the thing it guards is stale.
<!-- /playbill:block:pub-e6014c6a3f5996ec56c08fdd0035553f -->

# Integration checkpoint dogfooding — 2026-09-08

Recorded local integration 9184d986, the SDK/daemon version gap, and the latest
maintainer ruling that write redesign is required before OSS release. Three
Claims read back exactly and supported at `0f4d7005e41a3be328320b3c3ae303e48f1540aa`.
This was the existing live daemon, not a deployment of the merged source.

| Stage | Seconds |
|---|---:|
| connect | 2.325 |
| world | 0.016 |
| prefetch | 0.972 |
| authoring_capture | 0.013 |
| prepare | 1.255 |
| submit | 4.378 |
| review | 0.483 |
| accept_connect | 2.279 |
| preaccept_review_check | 0.789 |
| accept | 5.899 |
| readback | 1.189 |

Total active SDK time: **19.858s**, or
**15.254s excluding connections**.
This includes reads/review checks and excludes agent composition, offline review
and tool approval waiting. No separate approval attestation was required; there
were no SDK errors or retries. No full orientation refresh was added after accept.
One live observation, not a controlled before/after benchmark or percentile claim.

The existing checkpoint/review helpers were reused, but still required
request-specific source and Claim text. Splitting for offline review added a
second connection. This remains agent-workflow friction; the merge alone does
not resolve it. The earlier 27.607s checkpoint observation was carried into this
state update. Carry today's completed observation into the next meaningful update,
not an additional measurement-only write.

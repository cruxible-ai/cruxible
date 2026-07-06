# Case-law corpus seed

Pinned JSON bundle for the case-law-monitoring kit. Public-law rows use real
opinion metadata and public-domain Supreme Court opinion text from
CourtListener; firm-world clients, matters, filings, deadlines, and outcomes
are synthetic.

Data-file digest (README excluded), reproducible with
`find . \( -name '*.json' -o -name '*.txt' \) -type f | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256`
in this directory:

`sha256:cfd38ea7df0dcb26fe7253d3c9abeb43200dfdc9e3e1a8b6f93911f1200b7714`

The digest of record is the workflow lock's full directory artifact digest,
which also covers this README; the command above is for quick human
verification of the data files without a self-referential hash.

## Files

- `act1_seed.json`: pre-2024 corpus plus synthetic firm state.
- `holdings.json`: curated holdings for the corpus opinions — a hand-made
  miniature of a **citator's editorial layer** (what KeyCite/Shepard's sell:
  holding statements, issue mappings, treatment orientation, pin cites), with
  dereferenceable quote+offset evidence those services don't expose. Firms do
  not have this data natively; in real operation it is either **built** — the
  accumulated, approved exhaust of the agent/attorney loop proposing holdings
  through the HoldingCandidates contract as opinions land — or **bought** as
  a governed reference-state slice. Running the kit on another corpus means
  shipping your own holdings rows or none: uncurated opinions land as
  `unsure` and stop at review.
- `act2_update.json`: Loper Bright update fixture for offline refresh.
- `statute_match_hints.json`: optional per-statute keyword hints for
  matter-to-statute scoping — demo-tuned bundle data (the tokens include the
  synthetic firm's matter names), not provider logic. Replace or delete for
  your own corpus; the provider falls back to plain token overlap, and every
  hint match stays `unsure` and review-gated.
- `docket_feed.json`: synthetic docket filings and deadlines.
- `case_outcomes.json`: synthetic resolved matter outcomes.
- `opinions/`: CourtListener opinion text files plus `manifest.json`. Each
  curated evidence object records a verbatim quote, character offsets, and the
  referenced text file hash.

## Acquiring fresh opinions

Acquisition happens outside the workflow boundary, at the artifact seam:

1. `python scripts/fetch_courtlistener.py --cluster-id <id> -o update.json`
   (auth via `COURTLISTENER_API_TOKEN` or `~/.cruxible/courtlistener-api-key`;
   the output matches the `sync_corpus_update` contract).
2. Review the JSON; register each opinion text for evidence dereferencing:
   `cruxible source register <text-file> --id opinion_text_<opinion_id>`.
3. Apply: `cruxible run --workflow sync_corpus_update --input-file update.json`
   (preview, then `cruxible apply`), then run the treatment/impact proposal
   chain.

Workflows and providers never fetch: a live call inside a deterministic
pipeline would vary per run while the lock pins only the code. The fetch
result crosses into Cruxible as digest-pinned artifacts and reviewed rows.

## Act 1 Counts

- Entities: 10 Opinions, 5 Courts, 7 Judges, 12 Statutes/doctrines, 6 LegalIssues, 5 Clients, 5 Matters, 5 Arguments.
- Deterministic corpus edges: 10 opinion_from_court, 10 opinion_decided_by_judge, 11 opinion_cites_opinion.
- Deterministic firm edges: 5 matter_for_client, 5 matter_in_jurisdiction, 5 argument_in_matter, 8 argument_raises_issue, 8 argument_cites_opinion, 12 statute_governs_issue.

Act 1 public opinions: Chevron, Rust, Sweet Home, Smiley, Mead, Barnhart, Brand X,
Entergy, Mayo, and City of Arlington. The synthetic matters intentionally rely
on Chevron-era deference authorities so the act-two update has visible effects.

## Act 2 Counts

- Entities: 1 Opinion, 1 Court, 1 Judge.
- Edges: 1 opinion_from_court, 1 opinion_decided_by_judge, 3 opinion_cites_opinion.

Act 2 adds Loper Bright Enterprises v. Raimondo, 603 U.S. 369 (2024), filed
2024-06-28. Its citation contexts mark Chevron as overruled, Brand X as
abrogated to the extent it depended on Chevron, and Mead as limited. Those
stated treatment verbs emit supported citator proposals; citation contexts
without a recognized treatment verb default to `follows` with an `unsure`
verdict and remain review-gated.

The act-one Mead to Chevron citation is deliberately classified as `follows`
with an `unsure` verdict even though Mead is standardly treated as a limiting
case; using `limits` would fire the bad-law alarm before the act-two update.
Curated holding-to-statute and holding-to-issue rows emit supported proposals.
Keyword-only holding/statute/issue matches and matter/statute token-overlap
scope rows emit `unsure` proposals, so they surface for review instead of
self-certifying. Loper to Brand X uses the hedged citator label `abrogates`
because Loper expressly declined to disturb prior holdings, 603 U.S. at 412.

## Feed Counts

- Docket feed: 4 Filings, 5 Deadlines, 4 filing_in_matter edges, 5 matter_has_deadline edges.
- Outcome feed: 2 CaseOutcomes, 2 outcome_of_matter edges, 2 outcome_resolved_argument edges.

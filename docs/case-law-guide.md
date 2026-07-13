# Case Law: A Governed Citator On Hard State

The case-law-monitoring kit builds a citator you can audit, composed over
the agent-operation base. The **corpus** layer is real public law —
opinions, courts, judges, statutes, and the citation graph,
CourtListener-sourced and digest-pinned (the Chevron-deference cluster).
The **firm** layer is the practice — clients, matters, arguments,
filings, deadlines — plus the governed edges an attorney actually
reviews: holdings, interpretations, citation treatment, argument support
and risk, matter impact.

The demo runs in two acts: act one builds the pre-2024 world and the
matter is quiet; act two lands *Loper Bright*, and the payoff query —
`supporting_authority_now_bad_law` — surfaces the decision that overruled
this matter's supporting authority, with a receipt for the traversal.
Every judgment-bearing edge on that path was proposed by an extractor and
admitted by a reviewer: a rule declared in config is an invariant; an
instruction followed by a model is a probability, drawn fresh every call.
The opinions are real public law; the firm layer — clients, matters,
filings — is synthetic demo data. Run everything from a clone of the repo
(seed paths are repo-relative), with `jq` installed.

## 1. Start an identity-on instance

Run the daemon with auth on so writes are credential-backed identities
rather than one shared operator. In production the extractor and the
reviewer hold separately minted credentials; this walkthrough runs under
the admin credential for brevity.

```bash
CRUXIBLE_SERVER_AUTH=true \
CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=change-me-once \
CRUXIBLE_SERVER_STATE_DIR="$HOME/.cruxible/case-law" cruxible server start   # shell 1

# shell 2 — init with the bootstrap secret as bearer, then claim the admin credential
CRUXIBLE_SERVER_BEARER_TOKEN=change-me-once \
  cruxible --server-url http://127.0.0.1:8100 init \
  --kit agent-operation --kit case-law-monitoring --bootstrap

CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=change-me-once cruxible credential claim-bootstrap
export CRUXIBLE_SERVER_BEARER_TOKEN=<printed-admin-token>
```

## 2. Register the opinion texts

Every holding and treatment judgment then cites a verbatim,
hash-verified passage of the real opinion:

```bash
jq -r '.opinions[] | [.opinion_id, .source_url] | @tsv' \
  kits/case-law-monitoring/data/seed/opinions/manifest.json |
while IFS=$'\t' read -r opinion_id source_url; do
  cruxible source register \
    --path "kits/case-law-monitoring/data/seed/opinions/${opinion_id}.txt" \
    --id "opinion_text_${opinion_id}" --kind markdown --retention manifest_only \
    --original-uri "$source_url" --label "${opinion_id} opinion text" --json
done
```

## 3. Act one — build the world, then judge the holdings

Canonical ingest previews first, then commits; one extractor payload then
seeds the inert Holding entities and the governed `opinion_has_holding`
proposal:

```bash
cruxible run --workflow build_corpus
cruxible apply --workflow build_corpus --from-last-preview

cruxible run --workflow analyze_opinions_for_holdings --json \
  | jq '{items: .output.items}' > holding-candidates.json

cruxible run --workflow apply_candidate_holdings \
  --input-file holding-candidates.json --save-preview holdings-preview.json
cruxible apply --preview-file holdings-preview.json

cruxible propose --workflow propose_holdings_from_opinion \
  --input-file holding-candidates.json
```

The proposal lands as a pending group — review and resolve, the pattern
for every `propose` below:

```bash
cruxible group list --status pending_review
cruxible group get --group <group-id>
cruxible group resolve --group <group-id> --action approve \
  --rationale "Verified holdings against the registered opinion text." \
  --expected-pending-version <pending-version>
```

## 4. Wire holdings to statutes, issues, and arguments

Each layer reads the one before it, so resolve each group before running
the next; the last sets the baseline citator state (act one is all
`follows`):

```bash
cruxible propose --workflow propose_statute_interpretations   # then resolve
cruxible propose --workflow propose_holding_issue_links       # then resolve
cruxible propose --workflow propose_argument_support          # then resolve
cruxible propose --workflow propose_opinion_treatment         # then resolve

cruxible query run negative_treatment_for_cited_authorities \
  --param matter_id=matter_greengrid_epa
```

Zero results is the point — the doctrine is standing.

## 5. Act two — Loper Bright arrives

The treatment proposal after the corpus update contains exactly three new
members — `overrules` Chevron, `abrogates` Brand X, `limits` Mead — each
with quote-and-offset evidence. Negative treatment always requires review:

```bash
cruxible run --workflow refresh_corpus --json | jq '.output' > corpus-update.json
cruxible run --workflow sync_corpus_update \
  --input-file corpus-update.json --save-preview sync-preview.json
cruxible apply --preview-file sync-preview.json

cruxible propose --workflow propose_opinion_treatment         # review, then resolve
```

## 6. The payoff

```bash
cruxible query run supporting_authority_now_bad_law \
  --param matter_id=matter_greengrid_epa --json

cruxible explain --receipt <receipt-id> --format markdown
```

Three results: one path through each supporting authority *Loper Bright*
took down, newest treatment first, with a `receipt_id` for the whole
derivation — `explain` renders it.

## Staying current

Fresh opinions arrive outside the workflow boundary:
`kits/case-law-monitoring/scripts/fetch_courtlistener.py` pulls from
CourtListener (set `COURTLISTENER_API_TOKEN`) and the reviewed rows feed
`sync_corpus_update` exactly like act two; `analyze_review_work` +
`apply_review_work_items` then route the obligations as base WorkItems
that close only through the review gate. The bundled extractors are the
zero-LLM floor, not the ceiling: an agent supplies smarter judgment as
the same contract rows, through the same gates — see the kit
[README](https://github.com/cruxible-ai/cruxible/tree/main/kits/case-law-monitoring/).

"""Reviewed intent descriptions for the Cruxible MCP surface."""

from __future__ import annotations

from cruxible_core.errors import ConfigError

TOOL_DESCRIPTIONS: dict[str, str] = {
    "cruxible_provider_list": (
        "Use when you need to know which Providers are installed here (each with its package, "
        "version and the interface IDs it implements) and which packages the configured "
        "provider repository offers."
    ),
    "cruxible_provider_install": (
        "Use when you want to install a provider package by name (from the configured "
        "repository or the provider index) and register its definitions. The registration "
        "lands at once when the approval policy requires no approval, otherwise it stops at "
        "proposed (awaiting_approval). Requires admin permission; installation grants no "
        "execution permissions."
    ),
    "cruxible_provider_retire": (
        "Retire an exact provider build when no live definition pins it. Preview by default; "
        "retain its environment and deployments. Requires admin permission."
    ),
    "cruxible_provider_move": (
        "Move live Procedures to a provider build (newest means most recently registered). "
        "Preview each re-pin, carried disabled Line and propose-only Mandate; commit the governed "
        "change with dry_run false and at set to the preview coordinate."
    ),
    "cruxible_kit_build": (
        "Use when you want to export the definitions under owned identity prefixes as a "
        "kit release another instance can install. A release is a lineage-free snapshot; "
        "its manifest records where it was built. providers names provider packages "
        "(wheel, lock and path-dependency wheels, staged with body_store first): every "
        "Provider a carried Procedure pins must be named. delivery index makes one the "
        "kit's default provider, installed by name only where nothing implements it."
    ),
    "cruxible_kit_status": (
        "Use when you need the installed kits, the kit paths edited since install, what was "
        "kept on purpose, and (for registry kits) the latest available version."
    ),
    "cruxible_kit_add": (
        "Use when you want to install or upgrade a kit, by registry reference or bundle. It "
        "always proposes: a definition held differently takes the release's version unless "
        "kept (keep, keep_local_edits); the plan says what each change does and counts "
        "dependents. It lands at once when the approval policy requires no approval. A kit "
        "bundling provider packages installs them first (admin; a commit stages their files), "
        "and a default provider by name unless something here implements it (satisfied), "
        "then proposes the definitions; an install awaiting approval stops at "
        "awaiting_providers, and its activation proposes them on their own. Every Procedure "
        "on a shipped provider moves to that exact build (kit and user definitions); "
        "keep_build opts out by Procedure identity. The preview lists re-pins, disabled "
        "Line successors and propose-only Mandates; Triggers stay unchanged. "
        "Old builds stay installed."
    ),
    "cruxible_claim_type_upgrade": (
        "Use to move ClaimTypes before v7 to v7, which states revision_evidence "
        "(default replace: a statement-changing revision keeps only the evidence it cites) "
        "and evidence_requirement (kept at self). It only proposes; dry_run proposes nothing."
    ),
    "cruxible_kit_remove": (
        "Use when you want to retire what a kit installed. It lands at once when the approval "
        "policy requires no approval; live Claims that depend on those definitions block it."
    ),
    "cruxible_server_info": (
        "Use when you need adapter and daemon versions with state, auth, and host metadata."
    ),
    "cruxible_body_store": (
        "Use when you need to store exact Document bytes inertly before proposing them."
    ),
    "cruxible_document_propose": (
        "Use when you need to propose a governed Document create or supersession."
    ),
    "cruxible_proposal_review": (
        "Use when you need a structured candidate review and permission-filtered diff."
    ),
    "cruxible_proposal_approve_prepare": (
        "Use when a client-held signer needs the exact immutable approval statement."
    ),
    "cruxible_proposal_approve_submit": (
        "Use when you have a public approval attestation produced outside the daemon; "
        "like approve, it activates a candidate it completes unless hold is set."
    ),
    "cruxible_proposal_approve": (
        "Use after review to approve a proposal with a local key from the server's "
        "CRUXIBLE_MCP_KEY_DIR; it challenges, signs and submits in one call. The approval "
        "that completes the candidate also activates it when your tier may; hold=true "
        "signs only. Pass the reviewed candidate_digest to bind it to what you read."
    ),
    "cruxible_proposal_activate": (
        "Use when an admitted candidate's approvals are in but nothing activated it: a "
        "held approval, or one that needs no approval."
    ),
    "cruxible_orient": (
        "Use first, to see what an instance holds: each Subject kind with its count and "
        "predicates (type, enum members, accepted evidence), artifact counts, named queries, "
        "Claims by status, whether you can author, what needs attention, and runnable next "
        "calls. Pass kind for one kind in full with sample Subject IDs, or section to page "
        "documents, procedures, claim_types, queries, interfaces, principals, policies, or an "
        "operational family (runs, lines, captures, ...)."
    ),
    "cruxible_whoami": (
        "Use when you need which instance this server acts on, who you are there, and "
        "whether you can author (and the repair when not)."
    ),
    "cruxible_proposal_list": (
        "Use when you need to find open proposals or inspect terminal proposal outcomes. "
        "Returns one page (default limit 50); when truncated, pass next_cursor back as cursor."
    ),
    "cruxible_proposal_readmit": (
        "Use when a stale proposal should be re-admitted against the current coordinate."
    ),
    "cruxible_proposal_withdraw": (
        "Use when an open proposal can never be activated and should leave the open inventory."
    ),
    "cruxible_proposal_reject": (
        "Use when you review another actor's open proposal and it should not be accepted; "
        "the reason is recorded and the proposal leaves the open inventory."
    ),
    "cruxible_capture_read": (
        "Use when you need verified retained Capture evidence for inspection or Claim authoring. "
        "Requires body-read permission; max_bytes bounds returned material. "
        "Never refetches sources."
    ),
    "cruxible_capture_erase": (
        "Use when a Capture's retained bytes must not stay (a secret a tool printed). "
        "Only a contract that authorizes erasure allows it; it cannot be undone: preview, "
        "then commit with dry_run=false and at=<the preview's coordinate digest>."
    ),
    "cruxible_files_check": (
        "Use when you need to check the workspace's catalogued files (or a compiled "
        "bundle) against accepted state: aligned, modified, pending, behind and the rest."
    ),
    "cruxible_files_propose": (
        "Use when a catalogued workspace file should become its Document's next revision: "
        "name the catalog file; this adapter reads and compiles the file and proposes it, "
        "landing it when policy and your tier allow (accept=never proposes only)."
    ),
    "cruxible_compiler_upgrade": (
        "Use to propose a compiler upgrade (default: the daemon's compiler, at the head). "
        "Admin only; one-way once active; settles on one signed cruxible_proposal_approve."
    ),
    "cruxible_principal_propose": (
        "Use when you need a governed principal registration, rotation, revocation, or recovery."
    ),
    "cruxible_claim_type_propose": (
        "Use when you need a governed ClaimType before any Claim can state that predicate; "
        "pass a ClaimTypeInputRecord: a new one states its subject kinds, object and "
        "cardinality, and every other field "
        "has a default (`cruxible claim-type propose --template --expanded`); an edit states "
        "only what changes. Lands when policy and your tier allow; accept=never proposes only."
    ),
    "cruxible_claim_type_migrate": (
        "Use when a ClaimType and all of its dependent Claim dispositions must change atomically."
    ),
    "cruxible_claim_attest": (
        "Use when you examined a Claim and want to sign support, contradict, or unsure on it, "
        "optionally citing new Captures you examined."
    ),
    "cruxible_authoring_example": (
        "Use when you need a model-constructed Claim, Procedure, Line, acquisition policy, "
        "mandate, Subject, QueryDefinition, or ApprovalPolicy authoring input template."
    ),
    "cruxible_authoring_get": (
        "Use when you need the current durable content and state of one authoring intent."
    ),
    "cruxible_authoring_list": (
        "Use when you need to find your incomplete authoring work without remembering handles."
    ),
    "cruxible_authoring_compile": (
        "Use when you want to author or revise a Claim or Procedure and learn every "
        "refusal at once."
    ),
    "cruxible_authoring_bind": (
        "Use when one exact anchor in a configured workspace file is the evidence for a "
        "Claim you are authoring."
    ),
    "cruxible_authoring_preflight": (
        "Use when you need a complete binding check of an existing authoring intent."
    ),
    "cruxible_next": (
        "Use when you need what to work on next: ranked repair work, conflicts, and stale "
        "evidence, each with its exact next operation."
    ),
    "cruxible_authoring_submit": (
        "Use when an authoring intent has passed preflight and should become one candidate."
    ),
    "cruxible_authoring_status": (
        "Use when you need exactly what still separates an authored candidate from acceptance."
    ),
    "cruxible_block_repin": (
        "Use to stamp a new projection block or refresh one: name the catalogued file "
        "and the block; this adapter reads the backings, rewrites the opening marker and "
        "registers the block. The marker grammar is in docs/cli-reference.md, Projection "
        "block markers."
    ),
    "cruxible_block_sync": (
        "Use to check whether page blocks still match their backings; each stale block names "
        "its repin. It edits no page."
    ),
    "cruxible_block_detach": (
        "Use to take retired blocks' markers off pages, keeping the prose: preview with "
        "dry_run, then commit with at set to the preview's coordinate digest."
    ),
    "cruxible_block_depublish": (
        "Use when a published page block is being taken down for good, so the registration "
        "that demands its frame is released instead of asking for the block back."
    ),
    "cruxible_set": (
        "Use to change one value: set a field of a Subject (kind/id) to a value. It replaces "
        "the live value (no Claim ID needed), adds a missing Subject of a known kind, and "
        "accepts in the same call when policy lets you; otherwise it answers awaiting_approval "
        "with the approve call. Check each change's verdict and the warnings."
    ),
    "cruxible_retire": (
        "Use to end one live Claim (by Claim ID, or Subject and field) with its dependents, or "
        "a Subject (kind/id): its key Claims go too, its others only with with_claims."
    ),
    "cruxible_write": (
        "Write Claims and Markdown sections together. docs: append under or replace a section "
        "(heading lists; [] preamble); cite it as kind=section evidence or by doc id. Docs "
        "are written after admission."
    ),
    "cruxible_get": (
        "Use when you have a reference to one thing -- a Claim id or prefix, kind/id, a "
        "predicate, ClaimType:/Document:/Procedure:/query:/Trigger:/Principal:/"
        "Provider:NAME[/BUILD]/ProviderInterface:/SourceAcquisitionPolicy:<name>, "
        "ApprovalPolicy:instance, "
        "ProcedureRuntimePolicy:instance, or a proposal id -- and want its values. detail: summary "
        "(default card), evidence, why, history, proof (full envelope), body (Document bytes "
        "by range). A wrong name refuses with the nearest names."
    ),
    "cruxible_query": (
        "Use to answer any question over accepted state in one call. Compact: kind "
        '(e.g. "project.work_item") with where filters such as '
        '{"field": "status", "eq": "ready"} (also ne, lt, lte, gt, gte, in, '
        "exists, contains), select, follow and order_by; contains alone searches every "
        "Claim value; a definition kind (ClaimType, Procedure, Trigger, Line, SubjectKind, "
        "CaptureContract, ProcedureMandate, QueryDefinition) lists those definitions with "
        "typed columns. Or pass a query name "
        "with params (budgets, receipt=full for its replay receipt). Rows lead with values "
        "and carry flags (stale, contested, contradicted, uncovered, unsure_hold); status "
        "adds overturned, refused or retired Claims (retired also lists retired Subjects) "
        "and claims=true names each cell's Claims. "
        "When truncated, pass next_cursor back as cursor. A wrong name refuses with the "
        "nearest valid names."
    ),
    "cruxible_query_spec": (
        "Use when compact filters cannot say it: run one full QueryDefinitionSpec inline "
        "(traversals, disjunctions, projections) without accepting a QueryDefinition. "
        "Same rows, flags and paging as cruxible_query."
    ),
    "cruxible_procedure_run": (
        "Use when you need to execute an accepted Procedure with durable outcomes."
    ),
    "cruxible_procedure_measure": (
        "Use when a Procedure's declared measurements are due: evaluate them from real evidence "
        "at an explicit observation instant, persist the resolution, and credit one finalized "
        "run's exact grain. Retrying replays the standing answer; pending and expired windows "
        "write nothing."
    ),
    "cruxible_procedure_dispose": (
        "Record your upheld or overturned judgment and reason on the latest measurement "
        "resolution, attributed to your authenticated principal. Find its resolution_id "
        "in procedure readings or get Procedure:NAME. Exact retries reuse the judgment; "
        "overturn clears the standing answer without extending its measurement window."
    ),
    "cruxible_procedure_readings": (
        "Use when you need each measurement's standing (pending, expired, resolved) and the "
        "retained exact-grain readings that credit real runs. Read-only and paginated."
    ),
    "cruxible_line_enable": (
        "Enable a Line: its Triggers do nothing until then. The daemon admits what they match "
        "from now on, under your credential and the Line and Trigger versions current now. "
        "Needs governed write even for observe-only Lines. Never catches up. Read it with "
        "cruxible_get(ref='Line:NAME')."
    ),
    "cruxible_line_disable": (
        "Stop a Line admitting work on its own. Runs already admitted keep going. "
        "A Line already stopped returns already_disabled."
    ),
    "cruxible_line_evaluate": (
        "Turn a missed range (since, until) into pending work for dispatch; no runs start. "
        "dry_run only reports what the range makes eligible. Page incomplete results."
    ),
    "cruxible_line_dispatch": (
        "Run pending occurrences under your current authority, up to limit (default 100). "
        "An enabled Line admits only what it matched itself; the rest waits for this call."
    ),
    "cruxible_inbox_list": (
        "Use when next names unread inbox items, or to review what Lines sent for judgment. "
        "Items are untyped data a Line could not decide; filter by line and status."
    ),
    "cruxible_inbox_get": (
        "Use to read one inbox item whole before judging it: payload, how often it was seen, "
        "the runs and source Captures of its sightings, and any prior disposition."
    ),
    "cruxible_inbox_ack": (
        "Use when an inbox item needs nothing further from you and no reason is worth recording."
    ),
    "cruxible_inbox_dismiss": (
        "Use when an inbox item needs nothing; the reason is recorded and stays visible if the "
        "same item recurs."
    ),
    "cruxible_inbox_link": (
        "Use after you acted on an inbox item: link it to the proposal you wrote or to a "
        "Subject (such as a note) that records your judgment."
    ),
    "cruxible_line_run": (
        "Run a Line once now as a manual occurrence under its inputs, budgets, authority and "
        "mandate; it never consumes a Trigger. Pass event when its Procedure takes one."
    ),
    "cruxible_prediction_list": (
        "Find accepted resolution contracts testing a Claim, by Claim ID. Returns their "
        "definitions and version references, including retired contracts."
    ),
    "cruxible_prediction_propose": (
        "Propose a governed test of an accepted Claim: its hypothesis is a Claim ID, plus an "
        "observation selector, mechanical rule, and fixed or retained-event observation window."
    ),
    "cruxible_prediction_settle": (
        "Use when a predicted Claim and its matching later observation are accepted: pass the "
        "prediction id and the observation's Claim ID."
    ),
    "cruxible_curation_list": (
        "Beta: detection runs only when a curation-detect Trigger is authored. "
        "List the curation patterns detection recorded; a pure read. Each item carries the "
        "latest_event_digest the rulings "
        "need; detection says when it last ran, inactive_detectors which detectors cannot run. "
        "Returns one page (default limit 25); when truncated, pass next_cursor back as cursor."
    ),
    "cruxible_curation_duplicates": (
        "Use to find what may name one thing twice in one kind: Subjects whose ids or aliases, "
        "and values of one predicate, that meet under a broad comparison (casefold, no "
        "punctuation, the alias graph) but differ under the declared rules, and Subjects of a "
        "hint kind holding one key. Resolve a Subject or key group with semantic.alias plus "
        "retire, or semantic.distinct_from; a value group with "
        "a canonical_form succession. A pure, bounded read; pass next_cursor while truncated."
    ),
    "cruxible_audit": (
        "Rank visible Claim verification work by exact stake, weakness, and recency factors. "
        "This read records completed coverage but never recommends or executes a repair."
    ),
    "cruxible_curation_overrule": (
        "Beta curation (detection only with an authored curation-detect Trigger). "
        "Use when a detected pattern does not apply here: closes the item permanently, and the "
        "pattern is never raised again."
    ),
    "cruxible_curation_accept_fixed": (
        "Beta curation (detection only with an authored curation-detect Trigger). "
        "Use after an accepted change fixed an item: name it by accepted_proposal_id or "
        "accepted_generation; it must touch the item's subject or evidence."
    ),
    "cruxible_curation_suppress": (
        "Beta curation (detection only with an authored curation-detect Trigger). "
        "Hide an item (scope item) or its whole lineage (scope lineage) without resolving it."
    ),
    "cruxible_curation_unsuppress": (
        "Beta curation (detection only with an authored curation-detect Trigger). "
        "Lift a suppression on an item, so what it hid is listed again."
    ),
    "cruxible_since": (
        "Use when you need the exact accepted ChangeSet members after a known generation."
    ),
    "cruxible_floor_export": (
        "Use when you need the accepted floor as greppable files: return the bytes, write "
        "them to the workspace, or check that copy's status."
    ),
    "cruxible_coverage_resolve": (
        "Use when you have read or changed working files and need what they have to do with "
        "accepted state; the file catalog binds files to their sources."
    ),
    "cruxible_files_compile": (
        "Use to compile the workspace's catalogued files without constructing file "
        "digests or compilation wire."
    ),
}


def tool_description(tool_name: str) -> str:
    try:
        return TOOL_DESCRIPTIONS[tool_name]
    except KeyError as exc:
        raise ConfigError(f"MCP tool '{tool_name}' is missing a prompt description") from exc


__all__ = [
    "TOOL_DESCRIPTIONS",
    "tool_description",
]

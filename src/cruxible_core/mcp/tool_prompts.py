"""Reviewed intent descriptions for the Cruxible MCP surface."""

from __future__ import annotations

from cruxible_core.errors import ConfigError

TOOL_DESCRIPTIONS: dict[str, str] = {
    "cruxible_provider_list": (
        "Use when you need to discover available provider packages: each package's name, "
        "version and the provider interface IDs it implements."
    ),
    "cruxible_provider_install": (
        "Use when you want to install a provider package by name (from the configured "
        "repository or the provider index) and register its definitions. The registration "
        "lands at once when the approval policy requires no approval, otherwise it stops at "
        "proposed (awaiting_approval). Requires admin permission; installation grants no "
        "execution permissions."
    ),
    "cruxible_kit_build": (
        "Use when you want to export the definitions under owned identity prefixes as a "
        "kit release another instance can install; pass the previous release to continue "
        "its lineage."
    ),
    "cruxible_kit_status": (
        "Use when you need the installed kits and the kit paths edited since install."
    ),
    "cruxible_kit_add": (
        "Use when you want to install or upgrade a kit. It only proposes one change set; "
        "approval and activation remain the ordinary steps."
    ),
    "cruxible_claim_type_upgrade": (
        "Use to move ClaimTypes before v7 to v7, which states revision_evidence "
        "(default replace: a statement-changing revision keeps only the evidence it cites) "
        "and evidence_requirement (kept at self). It only proposes; dry_run proposes nothing."
    ),
    "cruxible_kit_remove": (
        "Use when you want to retire what a kit installed. It only proposes; live Claims "
        "that depend on those definitions block it."
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
        "Use when you have a public approval attestation produced outside the daemon."
    ),
    "cruxible_proposal_approve": (
        "Use after review to approve a proposal with a local key from the server's "
        "CRUXIBLE_MCP_KEY_DIR; it challenges, signs and submits in one call and never "
        "activates. Pass the reviewed candidate_digest to bind it to what you read."
    ),
    "cruxible_proposal_activate": (
        "Use when an admitted Cruxible candidate has satisfied any committed requirements and "
        "is ready to settle."
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
    "cruxible_capture_read": (
        "Use when you need verified retained Capture evidence for inspection or Claim authoring. "
        "Requires body-read permission; max_bytes bounds returned material. "
        "Never refetches sources."
    ),
    "cruxible_source_context": (
        "Use when a local client needs path-free accepted inputs before compiling sources."
    ),
    "cruxible_source_check": (
        "Use when you need to check sources against accepted state: a compiled bundle, "
        "or catalog-declared workspace files."
    ),
    "cruxible_propose_source_bundle": (
        "Use when you need to propose frozen source bytes without sending a local path."
    ),
    "cruxible_compiler_upgrade": (
        "Use to propose an explicit compiler upgrade bound to the exact accepted base. "
        "Requires admin permission; approve and activate through the normal proposal workflow."
    ),
    "cruxible_principal_propose": (
        "Use when you need a governed principal registration, rotation, revocation, or recovery."
    ),
    "cruxible_claim_type_propose": (
        "Use when you need a governed ClaimType before any Claim can state that predicate; "
        "pass a complete ClaimTypeInputRecord whose evidence rules match its capture contracts. "
        "Generate a lawful starting payload with "
        "`cruxible claim-type propose --template`."
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
        "Flow-A Claim."
    ),
    "cruxible_authoring_preflight": (
        "Use when you need a complete binding check of an existing authoring intent."
    ),
    "cruxible_authoring_rebase": (
        "Use when an authoring intent went stale because accepted state moved and must be "
        "rebased onto the current coordinate before preflight or submit."
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
        "Use to stamp a new projection block or refresh one: name the page (file or source) "
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
        "Use to end one live Claim: by Claim ID, or by Subject and field when it holds one "
        "value. Its dependent Claims retire with it, in one change set."
    ),
    "cruxible_write": (
        "Use to make several changes as one change set: set, add (one more value in a "
        "many-valued field, e.g. two links) and retire, all accepted or refused together."
    ),
    "cruxible_get": (
        "Use when you have a reference to one thing -- a Claim id or prefix, kind/id, a "
        "predicate, ClaimType:/Document:/Procedure:/query:/Trigger:/Principal:/"
        "ProviderInterface:/SourceAcquisitionPolicy:<name>, ApprovalPolicy:instance, "
        "ProcedureRuntimePolicy:instance, or a proposal id -- and want its values. detail: summary "
        "(default card), evidence, why, history, proof (full envelope), body (Document bytes "
        "by range). A wrong name refuses with the nearest names."
    ),
    "cruxible_query": (
        "Use to answer any question over accepted state in one call. Compact: kind "
        '(e.g. "dev.roadmap_item") with where filters such as '
        '{"field": "adoption_state", "eq": "adopted"} (also ne, lt, lte, gt, gte, in, '
        "exists, contains), select, follow and order_by; contains alone searches every "
        "Claim value; kind ClaimType or Procedure lists definitions, kind Trigger or Line "
        "lists Triggers (name, schedule, target) and Lines (enabled, triggers). Or pass a "
        "query name "
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
    "cruxible_procedure_readiness": (
        "Use when you need to know whether an accepted Procedure can run or which slots must "
        "be bound first."
    ),
    "cruxible_procedure_bind": (
        "Use when an accepted Procedure's open slots should be bound to exact accepted "
        "artifacts through governance."
    ),
    "cruxible_procedure_run": (
        "Use when you need to execute an accepted Procedure with durable outcomes."
    ),
    "cruxible_procedure_run_status": (
        "Use when you need one Procedure run's typed outcomes and exact next operation."
    ),
    "cruxible_procedure_measure": (
        "Use when a Procedure's declared measurements are due: evaluate them from real evidence "
        "at an explicit observation instant, persist the resolution, and credit one finalized "
        "run's exact grain. Retrying replays the standing answer; pending and expired windows "
        "write nothing."
    ),
    "cruxible_procedure_readings": (
        "Use when you need each measurement's standing (pending, expired, resolved) and the "
        "retained exact-grain readings that credit real runs. Read-only and paginated."
    ),
    "cruxible_line_check": (
        "Check a named Line without enqueuing or running it. Incomplete coverage is not absence; "
        "retain the returned checked_until when paging."
    ),
    "cruxible_line_arm": (
        "Arm a Line so the daemon admits what it matches from now on, under your credential "
        "and the Line version current now. Never catches up: earlier pending work and daemon "
        "downtime need evaluate and dispatch. Repeating it unchanged returns already_armed."
    ),
    "cruxible_line_disarm": (
        "Stop a Line admitting work on its own. Runs already admitted keep going. "
        "A Line already stopped returns already_disarmed."
    ),
    "cruxible_line_status": (
        "Read whether a Line is armed, its pending work, and why an arm stopped "
        "(credential revoked, Line changed, disarmed). Rearm to resume."
    ),
    "cruxible_line_evaluate": (
        "Evaluate an explicit missed range into pending work. "
        "Repeat or page incomplete results; no runs start."
    ),
    "cruxible_line_dispatch": (
        "Execute pending occurrences under your current authority. An armed Line admits only "
        "what it matched itself; everything else waits for this call."
    ),
    "cruxible_line_run": (
        "Trigger one due accepted Line occurrence. Reuse a returned occurrence id only as an "
        "idempotency assertion; the daemon derives occurrence identity."
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
        "List mechanically detected curation patterns. Supply an explicit workspace_observation "
        "only when the client has scanned declared blocks; the daemon never reads workspace files. "
        "Returns one page (default limit 25); when truncated, pass next_cursor back as cursor."
    ),
    "cruxible_audit": (
        "Rank visible Claim verification work by exact stake, weakness, and recency factors. "
        "This read records completed coverage but never recommends or executes a repair."
    ),
    "cruxible_curation_overrule": (
        "Use when the exact mechanical detector pattern is inapplicable and should be closed."
    ),
    "cruxible_curation_accept_fixed": (
        "Use only after an accepted ChangeSet mechanically intersects the curation evidence."
    ),
    "cruxible_curation_suppress": (
        "Hide open curation work by item, pattern, or instance without resolving it."
    ),
    "cruxible_since": (
        "Use when you need the exact accepted ChangeSet members after a known generation."
    ),
    "cruxible_floor_export": (
        "Use when you need the accepted floor as greppable files: return the bytes, write "
        "them to the workspace, or check that copy's status."
    ),
    "cruxible_coverage": (
        "Use when you have read or changed working files and need what they have to do with "
        "accepted state."
    ),
    "cruxible_workspace_source_compile": (
        "Use to compile catalog-declared files under this MCP client's workspace without "
        "constructing source digests or compilation wire."
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

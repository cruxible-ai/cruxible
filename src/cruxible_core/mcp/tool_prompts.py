"""Reviewed intent descriptions for the Playbill MCP surface."""

from __future__ import annotations

from cruxible_core.errors import ConfigError

TOOL_DESCRIPTIONS: dict[str, str] = {
    "cruxible_playbill_provider_catalog": (
        "Use when you need to discover available provider packages and their node types."
    ),
    "cruxible_playbill_provider_install": (
        "Use when you want to install a provider package and register its definitions. "
        "Requires admin permission; installation grants no execution permissions."
    ),
    "cruxible_playbill_kit_build": (
        "Use when you want to export the definitions under owned identity prefixes as a "
        "kit release another instance can install; pass the previous release to continue "
        "its lineage."
    ),
    "cruxible_playbill_kit_status": (
        "Use when you need the installed kits and the kit paths edited since install."
    ),
    "cruxible_playbill_kit_add": (
        "Use when you want to install or upgrade a kit. It only proposes one change set; "
        "approval and activation remain the ordinary steps."
    ),
    "cruxible_playbill_kit_remove": (
        "Use when you want to retire what a kit installed. It only proposes; live Claims "
        "that depend on those definitions block it."
    ),
    "cruxible_server_info": (
        "Use when you need adapter and daemon versions with state, auth, and host metadata; "
        "an instance-scoped credential gets its own instance's host and identity."
    ),
    "cruxible_playbill_host_create": (
        "Use when you need an empty daemon-owned host before Playbill bootstrap; "
        "this adopts no config or semantic state."
    ),
    "cruxible_playbill_init": (
        "Use when you need to bootstrap Playbill from client-generated public keys."
    ),
    "cruxible_playbill_instance_decommission": (
        "Use when an instance must stop accepting governed writes for good; reads keep "
        "serving, nothing is deleted, and the state cannot be reversed."
    ),
    "cruxible_playbill_store_body": (
        "Use when you need to store exact Document bytes inertly before proposing them."
    ),
    "cruxible_playbill_propose_document": (
        "Use when you need to propose a governed Document create or supersession."
    ),
    "cruxible_playbill_inspect_proposal": (
        "Use when you need immutable proposal evaluation and candidate evidence."
    ),
    "cruxible_playbill_inspect_refusal": (
        "Use when you need typed admission or acceptance-law diagnostics for a proposal."
    ),
    "cruxible_playbill_review": (
        "Use when you need a structured candidate review and permission-filtered diff."
    ),
    "cruxible_playbill_prepare_approval": (
        "Use when a client-held signer needs the exact immutable approval statement."
    ),
    "cruxible_playbill_submit_approval": (
        "Use when you have a public approval attestation produced outside the daemon."
    ),
    "cruxible_playbill_activate": (
        "Use when an admitted Playbill candidate has satisfied any committed requirements and "
        "is ready to settle."
    ),
    "cruxible_playbill_whoami": (
        "Use when you need which instance this server acts on, who you are there, and "
        "the adapter and daemon versions."
    ),
    "cruxible_playbill_proposal_list": (
        "Use when you need to find open proposals or inspect terminal proposal outcomes. "
        "Returns one page (default limit 50); when truncated, pass next_cursor back as cursor."
    ),
    "cruxible_playbill_proposal_readmit": (
        "Use when a stale proposal should be re-admitted against the current coordinate."
    ),
    "cruxible_playbill_proposal_withdraw": (
        "Use when an open proposal can never be activated and should leave the open inventory."
    ),
    "cruxible_playbill_list_documents": (
        "Use when you need accepted Documents and their exact coordinate."
    ),
    "cruxible_playbill_get_document": (
        "Use when you need one accepted Document envelope and structured facts."
    ),
    "cruxible_playbill_dereference": (
        "Use when you need verified accepted body bytes and have body-read permission."
    ),
    "cruxible_playbill_read_capture": (
        "Use when you need verified retained Capture evidence for inspection or Claim authoring. "
        "Requires body-read permission; max_bytes bounds returned material. "
        "Never refetches sources."
    ),
    "cruxible_playbill_history": (
        "Use when you need one Document's replay-verified accepted history."
    ),
    "cruxible_playbill_explain": (
        "Use when you need coordinate-bound governance, provenance, and attestation coverage."
    ),
    "cruxible_playbill_source_context": (
        "Use when a local client needs path-free accepted inputs before compiling sources."
    ),
    "cruxible_playbill_source_check": (
        "Use when you need to check sources against accepted state: a compiled bundle, "
        "or catalog-declared workspace files."
    ),
    "cruxible_playbill_propose_source_bundle": (
        "Use when you need to propose frozen source bytes without sending a local path."
    ),
    "cruxible_playbill_list_principals": (
        "Use when you need accepted public principal records and their coordinate."
    ),
    "cruxible_playbill_compiler_upgrade": (
        "Use to propose an explicit compiler upgrade bound to the exact accepted base. "
        "Requires admin permission; approve and activate through the normal proposal workflow."
    ),
    "cruxible_playbill_propose_principal_change": (
        "Use when you need a governed principal registration, rotation, revocation, or recovery."
    ),
    "cruxible_playbill_list_subjects": (
        "Use when you need accepted Subjects and their exact coordinate, optionally of one "
        "subject_kind."
    ),
    "cruxible_playbill_get_subject": (
        "Use when you need one accepted Subject envelope and its structured facts."
    ),
    "cruxible_playbill_subject_history": (
        "Use when you need one Subject's accepted lineage across generations."
    ),
    "cruxible_playbill_propose_claim_type": (
        "Use when you need a governed ClaimType before any Claim can state that predicate; "
        "pass a complete ClaimTypeInputV1 whose evidence rules match its capture contracts. "
        "Generate a lawful starting payload with "
        "`cruxible playbill claim-type propose --template`."
    ),
    "cruxible_playbill_claim_type_migrate": (
        "Use when a ClaimType and all of its dependent Claim dispositions must change atomically."
    ),
    "cruxible_playbill_list_claim_types": (
        "Use when you need the accepted predicate vocabulary an instance admits."
    ),
    "cruxible_playbill_get_claim_type": (
        "Use when you need one predicate's accepted structure, cardinality, and policy."
    ),
    "cruxible_playbill_claim_retire": (
        "Use when one Claim and its transitive Claim dependents must retire with explicit "
        "attribution in one governed ChangeSet."
    ),
    "cruxible_playbill_claim_attest": (
        "Use when you examined a Claim and want to sign support, contradict, or unsure on it, "
        "optionally citing new Captures you examined."
    ),
    "cruxible_playbill_authoring_create": (
        "Use when you need a durable machine-owned intent before iterating on a governed write."
    ),
    "cruxible_playbill_authoring_example": (
        "Use when you need a model-constructed Claim, Procedure, Subject, QueryDefinition, "
        "or ApprovalPolicy authoring input template."
    ),
    "cruxible_playbill_authoring_get": (
        "Use when you need the current durable content and state of one authoring intent."
    ),
    "cruxible_playbill_authoring_resume": (
        "Use when you need to continue an authoring flow after losing conversational context."
    ),
    "cruxible_playbill_authoring_list_pending": (
        "Use when you need to find your incomplete authoring work without remembering handles."
    ),
    "cruxible_playbill_authoring_compile": (
        "Use when you want to author or revise a Claim or Procedure and learn every "
        "refusal at once."
    ),
    "cruxible_playbill_authoring_bind": (
        "Use when one exact anchor in a configured workspace file is the evidence for a "
        "Flow-A Claim."
    ),
    "cruxible_playbill_authoring_preflight": (
        "Use when you need a complete binding check of an existing authoring intent."
    ),
    "cruxible_playbill_authoring_rebase": (
        "Use when an authoring intent went stale because accepted state moved and must be "
        "rebased onto the current coordinate before preflight or submit."
    ),
    "cruxible_playbill_next": (
        "Use when you need what to work on next: ranked repair work, conflicts, and stale "
        "evidence, each with its exact next operation."
    ),
    "cruxible_playbill_authoring_submit": (
        "Use when an authoring intent has passed preflight and should become one candidate."
    ),
    "cruxible_playbill_authoring_status": (
        "Use when you need exactly what still separates an authored candidate from acceptance."
    ),
    "cruxible_playbill_block_declare": (
        "Use after stamping a projection block so the instance registers the marker; "
        "`cruxible playbill block repin` does this for you."
    ),
    "cruxible_playbill_authoring_abandon_insertion": (
        "Use to release a publication expectation an instance already holds; nothing mints "
        "a new one."
    ),
    "cruxible_playbill_host_workspace_detach": (
        "Use when a Git worktree is moving from one governed host to another, so the host "
        "it is registered against releases it first."
    ),
    "cruxible_playbill_block_depublish": (
        "Use when a published page block is being taken down for good, so the registration "
        "that demands its frame is released instead of asking for the block back."
    ),
    "cruxible_playbill_list_claims": (
        "Use when you need accepted Claims, optionally narrowed to a Subject, subject_kind "
        "or predicate."
    ),
    "cruxible_playbill_claim_values": (
        "Use when you need a status table: the value and verdict of each live Claim for "
        "every Subject of one kind (or named subject_ids) and the given predicates, "
        "without full Claim views."
    ),
    "cruxible_playbill_get_claim": (
        "Use when you need one accepted Claim envelope and its structured facts."
    ),
    "cruxible_playbill_claim_history": (
        "Use when you need one Claim's accepted lineage across generations."
    ),
    "cruxible_playbill_explain_claim": (
        "Use when you need why one Claim holds: its verdict, law evidence, and sources."
    ),
    "cruxible_playbill_list_query_definitions": (
        "Use when you need the accepted named entrypoints an instance publishes."
    ),
    "cruxible_playbill_policies_in_force": (
        "Use when you need the live governed policy inventory at the accepted coordinate. "
        "Returns one page (default limit 25); when truncated, pass next_cursor back as cursor."
    ),
    "cruxible_playbill_get_query_definition": (
        "Use when you need one entrypoint's parameters, budgets, and result contract."
    ),
    "cruxible_playbill_run_query": (
        "Use when you need accepted state answered by a named entrypoint with a replay receipt."
    ),
    "cruxible_playbill_procedure_readiness": (
        "Use when you need to know whether an accepted Procedure can run or which slots must "
        "be bound first."
    ),
    "cruxible_playbill_procedure_bind": (
        "Use when an accepted Procedure's open slots should be bound to exact accepted "
        "artifacts through governance."
    ),
    "cruxible_playbill_procedure_run": (
        "Use when you need to execute an accepted Procedure with durable outcomes."
    ),
    "cruxible_playbill_procedure_run_status": (
        "Use when you need one Procedure run's typed outcomes and exact next operation."
    ),
    "cruxible_playbill_procedure_measure": (
        "Use when a Procedure's declared measurements are due: evaluate them from real evidence "
        "at an explicit observation instant, persist the resolution, and credit one finalized "
        "run's exact grain. Retrying replays the standing answer; pending and expired windows "
        "write nothing."
    ),
    "cruxible_playbill_procedure_readings": (
        "Use when you need each measurement's standing (pending, expired, resolved) and the "
        "retained exact-grain readings that credit real runs. Read-only and paginated."
    ),
    "cruxible_playbill_line_check": (
        "Check a named Line without enqueuing or running it. Incomplete coverage is not absence; "
        "retain the returned checked_until when paging."
    ),
    "cruxible_playbill_line_arm": (
        "Arm a Line so the daemon admits what it matches from now on, under your credential "
        "and the Line version current now. Never catches up: earlier pending work and daemon "
        "downtime need evaluate and dispatch."
    ),
    "cruxible_playbill_line_disarm": (
        "Stop a Line admitting work on its own. Runs already admitted keep going."
    ),
    "cruxible_playbill_line_arm_status": (
        "Read whether a Line is armed, its pending work, and why an arm stopped "
        "(credential revoked, Line changed, disarmed). Rearm to resume."
    ),
    "cruxible_playbill_line_evaluate": (
        "Evaluate an explicit missed range into pending work. "
        "Repeat or page incomplete results; no runs start."
    ),
    "cruxible_playbill_line_dispatch": (
        "Execute pending occurrences under your current authority. An armed Line admits only "
        "what it matched itself; everything else waits for this call."
    ),
    "cruxible_playbill_line_run": (
        "Trigger one due accepted Line occurrence. Reuse a returned occurrence id only as an "
        "idempotency assertion; the daemon derives occurrence identity."
    ),
    "cruxible_playbill_resolution_contracts": (
        "Find accepted resolution contracts for an exact Claim version. Returns their "
        "definitions and version references, including retired contracts."
    ),
    "cruxible_playbill_predict": (
        "Propose a governed test of an already accepted exact Claim version. Supply its "
        "observation selector, mechanical rule, and fixed or retained-event observation window."
    ),
    "cruxible_playbill_settle": (
        "Use when a predicted Claim and its matching later observation are accepted, optionally "
        "binding the exact retained mandate-settlement terminal record."
    ),
    "cruxible_playbill_discover": (
        "Use when you do not yet know which interface or Subject names the state you want. "
        "truncated means a budget clipped the hits; raise budget or narrow the query."
    ),
    "cruxible_playbill_search": (
        "Use search mode to find accepted Claims, Procedures, or installed demands; "
        "list mode for deterministic pagination; orient mode for counts and exact follow-ups."
    ),
    "cruxible_playbill_curation_list": (
        "List mechanically detected curation patterns. Supply an explicit workspace_observation "
        "only when the client has scanned declared blocks; the daemon never reads workspace files. "
        "Returns one page (default limit 25); when truncated, pass next_cursor back as cursor."
    ),
    "cruxible_playbill_audit": (
        "Rank visible Claim verification work by exact stake, weakness, and recency factors. "
        "This read records completed coverage but never recommends or executes a repair."
    ),
    "cruxible_playbill_curation_overrule": (
        "Use when the exact mechanical detector pattern is inapplicable and should be closed."
    ),
    "cruxible_playbill_curation_accept_fixed": (
        "Use only after an accepted ChangeSet mechanically intersects the curation evidence."
    ),
    "cruxible_playbill_curation_suppress": (
        "Hide open curation work by item, pattern, or instance without resolving it."
    ),
    "cruxible_playbill_since": (
        "Use when you need the exact accepted ChangeSet members after a known generation."
    ),
    "cruxible_playbill_expand": (
        "Use when you need one address's bounded governance, provenance, and relation context."
    ),
    "cruxible_playbill_floor_export": (
        "Use when you need the accepted floor as greppable files: return the bytes, write "
        "them to the workspace, or check that copy's status."
    ),
    "cruxible_playbill_coverage": (
        "Use when you have read or changed working files and need what they have to do with "
        "accepted state."
    ),
    "cruxible_playbill_workspace_source_compile": (
        "Use to compile catalog-declared files under this MCP client's workspace without "
        "constructing source digests or compilation wire."
    ),
    "cruxible_playbill_seed_plan": (
        "Use to inspect the deterministic proposal sequence for a workspace seed bundle; this "
        "does not contact or mutate an instance."
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

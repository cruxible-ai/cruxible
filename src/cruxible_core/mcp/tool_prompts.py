"""Reviewed intent descriptions for the Playbill MCP surface."""

from __future__ import annotations

from cruxible_core.errors import ConfigError

TOOL_PROMPT_STYLE_RULE = (
    'Tool descriptions must start with "Use when", name the user intent first, '
    "and avoid implementation details that do not help a client choose a tool."
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "cruxible_version": "Use when you need to confirm which cruxible build is running.",
    "cruxible_server_info": (
        "Use when you need live daemon version, state-directory, authentication, "
        "or instance-count information."
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
        "Use when you need the credential-derived writer identity, permission mode, and "
        "accepted principal status."
    ),
    "cruxible_playbill_proposal_list": (
        "Use when you need to find open proposals or inspect terminal proposal outcomes."
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
    "cruxible_playbill_history": (
        "Use when you need one Document's replay-verified accepted history."
    ),
    "cruxible_playbill_explain": (
        "Use when you need coordinate-bound governance, provenance, and attestation coverage."
    ),
    "cruxible_playbill_source_context": (
        "Use when a local client needs path-free accepted inputs before compiling sources."
    ),
    "cruxible_playbill_check_source_bundle": (
        "Use when you need to compare compiled source bytes with accepted state."
    ),
    "cruxible_playbill_propose_source_bundle": (
        "Use when you need to propose frozen source bytes without sending a local path."
    ),
    "cruxible_playbill_list_principals": (
        "Use when you need accepted public principal records and their coordinate."
    ),
    "cruxible_playbill_propose_principal_change": (
        "Use when you need a governed principal registration, rotation, revocation, or recovery."
    ),
    "cruxible_playbill_propose_subject": (
        "Use when you need a governed identity-only Subject to hang Claims on."
    ),
    "cruxible_playbill_list_subjects": (
        "Use when you need accepted Subjects and their exact coordinate."
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
        "Sign and append an exact-Claim observation. The local signature binds the caller's "
        "ordinary principal to having examined the named Claim. Choose an explicit stance."
    ),
    "cruxible_playbill_claim_attest_new_capture": (
        "Sign and append a structured new-Capture observation using a prepared digest-free "
        "client request."
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
    "cruxible_playbill_authoring_submit": (
        "Use when an authoring intent has passed preflight and should become one candidate."
    ),
    "cruxible_playbill_authoring_status": (
        "Use when you need exactly what still separates an authored candidate from acceptance."
    ),
    "cruxible_playbill_authoring_confirm_insertion": (
        "Use after applying a pending insertion or prepared publication to bind its exact "
        "observed postimage."
    ),
    "cruxible_playbill_authoring_prepare_publication": (
        "Use after the Flow-B Claim is accepted to prepare one exact stamped publication "
        "against fresh whole-source bytes."
    ),
    "cruxible_playbill_authoring_abandon_insertion": (
        "Use when a pending publication copy should be retired while its accepted self-source "
        "Claim remains governed."
    ),
    "cruxible_playbill_list_claims": (
        "Use when you need accepted Claims, optionally narrowed to a Subject or predicate."
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
    "cruxible_playbill_propose_query_definition": (
        "Use when you need a governed named entrypoint others can execute and replay."
    ),
    "cruxible_playbill_list_query_definitions": (
        "Use when you need the accepted named entrypoints an instance publishes."
    ),
    "cruxible_playbill_policies_in_force": (
        "Use when you need the live governed policy inventory at the accepted coordinate."
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
        "Use when you need to execute an accepted query-only Procedure with durable outcomes."
    ),
    "cruxible_playbill_procedure_run_status": (
        "Use when you need one Procedure run's typed outcomes and exact next operation."
    ),
    "cruxible_playbill_line_run": (
        "Trigger one due accepted Line occurrence. Reuse a returned occurrence id only as an "
        "idempotency assertion; the daemon derives occurrence identity."
    ),
    "cruxible_playbill_predict": (
        "Use when an uncertain Claim can be tested later. Supply the exact accepted Procedure "
        "measurement, observation selector, mechanical rule, and validity-window deadline."
    ),
    "cruxible_playbill_settle": (
        "Use when a predicted Claim and its matching later observation are accepted, optionally "
        "binding the exact retained mandate-settlement terminal record."
    ),
    "cruxible_playbill_discover": (
        "Use when you do not yet know which interface or Subject names the state you want."
    ),
    "cruxible_playbill_search": (
        "Use search mode to find accepted Claims, Procedures, or installed demands; "
        "list mode for deterministic pagination; orient mode for counts and exact follow-ups."
    ),
    "cruxible_playbill_curation_list": (
        "List mechanically detected curation patterns. Supply an explicit workspace_observation "
        "only when the client has scanned declared blocks; the daemon never reads workspace files."
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
    "cruxible_playbill_export_floor": (
        "Use when you need the whole accepted floor as greppable files rather than one read."
    ),
    "cruxible_playbill_workspace_floor_export": (
        "Use when you need the accepted floor verified and written under this MCP client's "
        "configured workspace."
    ),
    "cruxible_playbill_workspace_floor_status": (
        "Use when you need to know whether this MCP client's configured floor is current, "
        "stale, missing, or invalid."
    ),
    "cruxible_playbill_resolve_coverage": (
        "Use when you have read or changed working files and need what they have to do with "
        "accepted state."
    ),
    "cruxible_playbill_workspace_source_compile": (
        "Use to compile catalog-declared files under this MCP client's workspace without "
        "constructing source digests or compilation wire."
    ),
    "cruxible_playbill_workspace_source_check": (
        "Use to compile catalog-declared workspace files and compare them with accepted state."
    ),
    "cruxible_playbill_workspace_coverage_resolve": (
        "Use after reading or changing selected workspace files; supply logical bindings and "
        "the selections while the adapter derives byte observations."
    ),
    "cruxible_playbill_workspace_coverage_status": (
        "Use for one coverage answer over every file in the declared workspace binding set."
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
    "TOOL_PROMPT_STYLE_RULE",
    "tool_description",
]

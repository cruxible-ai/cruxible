"""HTTP client for Cruxible daemon mode."""

from __future__ import annotations

import builtins
import json
from typing import Any, Mapping, TypeVar

import httpx
from pydantic import BaseModel

from cruxible_client import contracts
from cruxible_client.errors import ConfigError, CoreError, ErrorResponse, response_to_error

ModelT = TypeVar("ModelT", bound=BaseModel)


class CruxibleClient:
    """Thin sync client for local UDS or remote HTTP transports."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        socket_path: str | None = None,
        token: str | None = None,
    ) -> None:
        if bool(base_url) == bool(socket_path):
            raise ConfigError("Configure exactly one of base_url or socket_path for CruxibleClient")

        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        if socket_path is not None:
            self._client = httpx.Client(
                base_url="http://cruxible",
                headers=headers,
                transport=httpx.HTTPTransport(uds=socket_path),
            )
        else:
            assert base_url is not None
            self._client = httpx.Client(base_url=base_url, headers=headers)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CruxibleClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _check_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            body = ErrorResponse.model_validate(response.json())
        except Exception as exc:
            detail = response.text[:500]
            raise CoreError(
                f"Server request failed with status {response.status_code}: {detail}"
            ) from exc
        raise response_to_error(response.status_code, body)

    def _parse_model(self, response: httpx.Response, model_cls: type[ModelT]) -> ModelT:
        self._check_error(response)
        return model_cls.model_validate(response.json())

    def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        self._check_error(response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise CoreError("Expected JSON object response from Cruxible server")
        return payload

    @staticmethod
    def _omit_none_params(params: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in params.items() if value is not None}

    @staticmethod
    def _actor_context_payload(
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if actor_context is None:
            return None
        if isinstance(actor_context, contracts.GovernedActorContext):
            return actor_context.model_dump(mode="json", exclude_none=True)
        return dict(actor_context)

    @classmethod
    def _with_actor_context(
        cls,
        payload: dict[str, Any],
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None,
    ) -> dict[str, Any]:
        actor_payload = cls._actor_context_payload(actor_context)
        if actor_payload is not None:
            payload["actor_context"] = actor_payload
        return payload

    def claim_runtime_bootstrap(
        self,
        instance_id: str,
        bootstrap_secret: str,
    ) -> contracts.RuntimeCredentialBootstrapResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/bootstrap/claim",
            json={"bootstrap_secret": bootstrap_secret},
        )
        return self._parse_model(response, contracts.RuntimeCredentialBootstrapResult)

    def init_hosted_instance(
        self,
        *,
        source_type: contracts.HostedInstanceSourceType,
        instance_id: str | None = None,
        kit_ref: str | None = None,
        transport_ref: str | None = None,
        state_ref: str | None = None,
        overlay_kit_ref: str | None = None,
        no_overlay_kit: bool = False,
    ) -> contracts.HostedInstanceInitResult:
        response = self._client.post(
            "/api/v1/runtime/instances",
            json={
                "instance_id": instance_id,
                "source_type": source_type,
                "kit_ref": kit_ref,
                "transport_ref": transport_ref,
                "state_ref": state_ref,
                "overlay_kit_ref": overlay_kit_ref,
                "no_overlay_kit": no_overlay_kit,
            },
        )
        return self._parse_model(response, contracts.HostedInstanceInitResult)

    def create_runtime_credential(
        self,
        instance_id: str,
        *,
        label: str,
        permission_mode: contracts.RuntimeCredentialPermissionMode = "admin",
    ) -> contracts.RuntimeCredentialResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/credentials",
            json={"label": label, "permission_mode": permission_mode},
        )
        return self._parse_model(response, contracts.RuntimeCredentialResult)

    def list_runtime_credentials(
        self,
        instance_id: str,
    ) -> contracts.RuntimeCredentialListResult:
        response = self._client.get(f"/api/v1/{instance_id}/runtime/credentials")
        return self._parse_model(response, contracts.RuntimeCredentialListResult)

    def revoke_runtime_credential(
        self,
        instance_id: str,
        credential_id: str,
    ) -> contracts.RuntimeCredentialResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{credential_id}/revoke",
        )
        return self._parse_model(response, contracts.RuntimeCredentialResult)

    def rotate_runtime_credential(
        self,
        instance_id: str,
        credential_id: str,
    ) -> contracts.RuntimeCredentialResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate",
        )
        return self._parse_model(response, contracts.RuntimeCredentialResult)

    def init(
        self,
        root_dir: str,
        config_path: str | None = None,
        config_yaml: str | None = None,
        data_dir: str | None = None,
        kit: str | None = None,
    ) -> contracts.InitResult:
        response = self._client.post(
            "/api/v1/instances",
            json={
                "root_dir": root_dir,
                "config_path": config_path,
                "config_yaml": config_yaml,
                "data_dir": data_dir,
                "kit": kit,
            },
        )
        return self._parse_model(response, contracts.InitResult)

    def validate(
        self,
        config_path: str | None = None,
        config_yaml: str | None = None,
    ) -> contracts.ValidateResult:
        response = self._client.post(
            "/api/v1/validate",
            json={"config_path": config_path, "config_yaml": config_yaml},
        )
        return self._parse_model(response, contracts.ValidateResult)

    def server_info(self) -> contracts.ServerInfoResult:
        response = self._client.get("/api/v1/server/info")
        return self._parse_model(response, contracts.ServerInfoResult)

    def server_restart(self) -> contracts.ServerRestartResult:
        response = self._client.post("/api/v1/server/restart")
        return self._parse_model(response, contracts.ServerRestartResult)

    def version(self) -> str:
        """Return the daemon's reported version via the unauthenticated probe.

        Used to confirm a daemon is up (and on which version) after a restart,
        guarding the dev loop against silent client/server skew.
        """
        payload = self._parse_json(self._client.get("/version"))
        version = payload.get("version")
        if not isinstance(version, str):
            raise CoreError("Server /version response missing version string")
        return version

    def create_state_overlay(
        self,
        *,
        root_dir: str,
        transport_ref: str | None = None,
        state_ref: str | None = None,
        kit: str | None = None,
        no_kit: bool = False,
    ) -> contracts.StateOverlayResult:
        response = self._client.post(
            "/api/v1/states/overlays",
            json={
                "transport_ref": transport_ref,
                "state_ref": state_ref,
                "kit": kit,
                "no_kit": no_kit,
                "root_dir": root_dir,
            },
        )
        return self._parse_model(response, contracts.StateOverlayResult)

    def query(
        self,
        instance_id: str,
        query_name: str,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
        relationship_state: contracts.QueryRelationshipState | None = None,
        decision_record_id: str | None = None,
    ) -> contracts.QueryToolResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/queries/run",
            json={
                "query_name": query_name,
                "params": params,
                "limit": limit,
                "offset": offset,
                "relationship_state": relationship_state,
                "decision_record_id": decision_record_id,
            },
        )
        return self._parse_model(response, contracts.QueryToolResult)

    def view(
        self,
        instance_id: str,
        query_name: str,
        params: dict[str, str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        relationship_state: contracts.QueryRelationshipState | None = None,
    ) -> contracts.QueryToolResult:
        """Run a named query through the GET /views read-model shim.

        View query parameters are string-valued; ``limit``, ``offset``, and
        ``relationship_state`` are reserved view keys and must not appear in
        ``params``.
        """
        reserved = {"limit", "offset", "relationship_state"} & set(params or {})
        if reserved:
            raise ValueError(f"params may not use reserved view keys: {sorted(reserved)}")
        query_params: dict[str, Any] = dict(params or {})
        if limit is not None:
            query_params["limit"] = limit
        if offset:
            query_params["offset"] = offset
        if relationship_state is not None:
            query_params["relationship_state"] = relationship_state
        response = self._client.get(
            f"/api/v1/{instance_id}/views/{query_name}",
            params=query_params,
        )
        return self._parse_model(response, contracts.QueryToolResult)

    def query_inline(
        self,
        instance_id: str,
        definition: contracts.InlineQueryDefinition,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        relationship_state: contracts.QueryRelationshipState | None = None,
        decision_record_id: str | None = None,
    ) -> contracts.QueryToolResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/queries/run-inline",
            json={
                "definition": definition.model_dump(mode="json", exclude_none=True),
                "params": params,
                "limit": limit,
                "relationship_state": relationship_state,
                "decision_record_id": decision_record_id,
            },
        )
        return self._parse_model(response, contracts.QueryToolResult)

    def create_decision_record(
        self,
        instance_id: str,
        *,
        question: str,
        subject_type: str | None = None,
        subject_id: str | None = None,
        opened_by: str = "human",
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.DecisionRecordResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/decision-records",
            json=self._with_actor_context(
                {
                    "question": question,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "opened_by": opened_by,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.DecisionRecordResult)

    def get_decision_record(
        self,
        instance_id: str,
        decision_record_id: str,
        *,
        include_events: bool = True,
    ) -> contracts.DecisionRecordResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/decision-records/{decision_record_id}",
            params={"include_events": include_events},
        )
        return self._parse_model(response, contracts.DecisionRecordResult)

    def list_decision_records(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        decision_class: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.DecisionRecordListResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/decision-records",
            params=self._omit_none_params(
                {
                    "status": status,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "decision_class": decision_class,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )
        return self._parse_model(response, contracts.DecisionRecordListResult)

    def list_decision_events(
        self,
        instance_id: str,
        *,
        decision_record_id: str | None = None,
        receipt_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.DecisionEventListResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/decision-records/events",
            params=self._omit_none_params(
                {
                    "decision_record_id": decision_record_id,
                    "receipt_id": receipt_id,
                    "trace_id": trace_id,
                    "status": status,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )
        return self._parse_model(response, contracts.DecisionEventListResult)

    def finalize_decision_record(
        self,
        instance_id: str,
        decision_record_id: str,
        *,
        final_decision: str,
        decision_class: contracts.DecisionClass,
        rationale: str = "",
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.DecisionRecordResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/decision-records/{decision_record_id}/finalize",
            json=self._with_actor_context(
                {
                    "final_decision": final_decision,
                    "decision_class": decision_class,
                    "rationale": rationale,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.DecisionRecordResult)

    def abandon_decision_record(
        self,
        instance_id: str,
        decision_record_id: str,
        *,
        reason: str = "",
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.DecisionRecordResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/decision-records/{decision_record_id}/abandon",
            json=self._with_actor_context({"reason": reason}, actor_context),
        )
        return self._parse_model(response, contracts.DecisionRecordResult)

    def render_wiki(
        self,
        instance_id: str,
        *,
        focus: list[str] | None = None,
        include_types: list[str] | None = None,
        scope: str | None = None,
        max_per_type: int = 50,
        all_subjects: bool = False,
    ) -> contracts.WikiRenderResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/wiki/render",
            json={
                "focus": focus or [],
                "include_types": include_types or [],
                "scope": scope,
                "max_per_type": max_per_type,
                "all_subjects": all_subjects,
            },
        )
        return self._parse_model(response, contracts.WikiRenderResult)

    def receipt(self, instance_id: str, receipt_id: str) -> dict[str, Any]:
        response = self._client.get(f"/api/v1/{instance_id}/receipts/{receipt_id}")
        return self._parse_json(response)

    def explain_receipt(
        self,
        instance_id: str,
        receipt_id: str,
        *,
        format: contracts.ReceiptExplanationFormat = "markdown",
    ) -> contracts.ReceiptExplanationResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/receipts/{receipt_id}/explain",
            params={"format": format},
        )
        return self._parse_model(response, contracts.ReceiptExplanationResult)

    def get_trace(self, instance_id: str, trace_id: str) -> dict[str, Any]:
        response = self._client.get(f"/api/v1/{instance_id}/traces/{trace_id}")
        return self._parse_json(response)

    def list_traces(
        self,
        instance_id: str,
        *,
        workflow_name: str | None = None,
        provider_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> contracts.TraceListResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/traces",
            params=self._omit_none_params(
                {
                    "workflow_name": workflow_name,
                    "provider_name": provider_name,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )
        return self._parse_model(response, contracts.TraceListResult)

    def feedback(
        self,
        instance_id: str,
        *,
        action: contracts.FeedbackAction,
        source: contracts.FeedbackSource,
        from_type: str,
        from_id: str,
        relationship_type: str,
        to_type: str,
        to_id: str,
        edge_key: int | None = None,
        reason: str = "",
        reason_code: str | None = None,
        scope_hints: dict[str, Any] | None = None,
        corrections: dict[str, Any] | None = None,
        group_override: bool = False,
        receipt_id: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.FeedbackResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/feedback",
            json=self._with_actor_context(
                {
                    "receipt_id": receipt_id,
                    "action": action,
                    "source": source,
                    "from_type": from_type,
                    "from_id": from_id,
                    "relationship_type": relationship_type,
                    "to_type": to_type,
                    "to_id": to_id,
                    "edge_key": edge_key,
                    "reason": reason,
                    "reason_code": reason_code,
                    "scope_hints": scope_hints,
                    "corrections": corrections,
                    "group_override": group_override,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.FeedbackResult)

    def feedback_batch(
        self,
        instance_id: str,
        *,
        items: list[contracts.FeedbackBatchItemInput],
        source: contracts.FeedbackSource,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.FeedbackBatchResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/feedback/batch",
            json=self._with_actor_context(
                {
                    "source": source,
                    "items": [item.model_dump(mode="json") for item in items],
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.FeedbackBatchResult)

    def feedback_from_query(
        self,
        instance_id: str,
        *,
        receipt_id: str,
        result_index: int,
        action: contracts.FeedbackAction,
        source: contracts.FeedbackSource = "human",
        reason: str = "",
        reason_code: str | None = None,
        scope_hints: dict[str, Any] | None = None,
        corrections: dict[str, Any] | None = None,
        group_override: bool = False,
        path_index: int | None = None,
        path_alias: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.FeedbackResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/feedback/from-query",
            json=self._with_actor_context(
                {
                    "receipt_id": receipt_id,
                    "result_index": result_index,
                    "action": action,
                    "source": source,
                    "reason": reason,
                    "reason_code": reason_code,
                    "scope_hints": scope_hints,
                    "corrections": corrections,
                    "group_override": group_override,
                    "path_index": path_index,
                    "path_alias": path_alias,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.FeedbackResult)

    def outcome(
        self,
        instance_id: str,
        *,
        receipt_id: str | None = None,
        outcome: contracts.OutcomeValue,
        anchor_type: contracts.OutcomeAnchorType = "receipt",
        anchor_id: str | None = None,
        source: contracts.FeedbackSource = "human",
        outcome_code: str | None = None,
        scope_hints: dict[str, Any] | None = None,
        outcome_profile_key: str | None = None,
        detail: dict[str, Any] | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.OutcomeResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/outcome",
            json=self._with_actor_context(
                {
                    "receipt_id": receipt_id,
                    "anchor_type": anchor_type,
                    "anchor_id": anchor_id,
                    "outcome": outcome,
                    "source": source,
                    "outcome_code": outcome_code,
                    "scope_hints": scope_hints,
                    "outcome_profile_key": outcome_profile_key,
                    "detail": detail,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.OutcomeResult)

    def list(
        self,
        instance_id: str,
        *,
        resource_type: contracts.ResourceType,
        entity_type: str | None = None,
        relationship_type: str | None = None,
        query_name: str | None = None,
        receipt_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        property_filter: dict[str, Any] | None = None,
        where: dict[str, dict[str, Any]] | None = None,
        operation_type: str | None = None,
        fields: builtins.list[str] | None = None,
    ) -> contracts.ListResult:
        params: dict[str, Any] = {
            "entity_type": entity_type,
            "relationship_type": relationship_type,
            "query_name": query_name,
            "receipt_id": receipt_id,
            "limit": limit,
            "offset": offset,
            "operation_type": operation_type,
            "fields": fields,
        }
        if property_filter is not None:
            params["property_filter"] = json.dumps(property_filter)
        if where is not None:
            params["where"] = json.dumps(where)
        response = self._client.get(
            f"/api/v1/{instance_id}/list/{resource_type}",
            params=self._omit_none_params(params),
        )
        return self._parse_model(response, contracts.ListResult)

    def evaluate(
        self,
        instance_id: str,
        *,
        max_findings: int = 100,
        exclude_orphan_types: builtins.list[str] | None = None,
        severity_filter: builtins.list[contracts.FindingSeverity] | None = None,
        category_filter: builtins.list[contracts.FindingCategory] | None = None,
    ) -> contracts.EvaluateResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/evaluate",
            json={
                "max_findings": max_findings,
                "exclude_orphan_types": exclude_orphan_types,
                "severity_filter": severity_filter,
                "category_filter": category_filter,
            },
        )
        return self._parse_model(response, contracts.EvaluateResult)

    def lint(
        self,
        instance_id: str,
        *,
        max_findings: int = 100,
        analysis_limit: int = 200,
        min_support: int = 5,
        exclude_orphan_types: builtins.list[str] | None = None,
    ) -> contracts.LintResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/lint",
            json={
                "max_findings": max_findings,
                "analysis_limit": analysis_limit,
                "min_support": min_support,
                "exclude_orphan_types": exclude_orphan_types,
            },
        )
        return self._parse_model(response, contracts.LintResult)

    def get_feedback_profile(
        self,
        instance_id: str,
        relationship_type: str,
    ) -> contracts.FeedbackProfileResult:
        response = self._client.get(f"/api/v1/{instance_id}/feedback/profiles/{relationship_type}")
        return self._parse_model(response, contracts.FeedbackProfileResult)

    def get_outcome_profile(
        self,
        instance_id: str,
        *,
        anchor_type: contracts.OutcomeAnchorType,
        relationship_type: str | None = None,
        workflow_name: str | None = None,
        surface_type: str | None = None,
        surface_name: str | None = None,
    ) -> contracts.OutcomeProfileResult:
        params = {
            "anchor_type": anchor_type,
            "relationship_type": relationship_type,
            "workflow_name": workflow_name,
            "surface_type": surface_type,
            "surface_name": surface_name,
        }
        response = self._client.get(
            f"/api/v1/{instance_id}/outcome/profile",
            params=self._omit_none_params(params),
        )
        return self._parse_model(response, contracts.OutcomeProfileResult)

    def analyze_feedback(
        self,
        instance_id: str,
        *,
        relationship_type: str,
        limit: int = 200,
        min_support: int = 5,
        decision_surface_type: str | None = None,
        decision_surface_name: str | None = None,
        property_pairs: builtins.list[contracts.PropertyPairInput] | None = None,
    ) -> contracts.AnalyzeFeedbackResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/feedback/analyze",
            json={
                "relationship_type": relationship_type,
                "limit": limit,
                "min_support": min_support,
                "decision_surface_type": decision_surface_type,
                "decision_surface_name": decision_surface_name,
                "property_pairs": (
                    [pair.model_dump(mode="json") for pair in property_pairs]
                    if property_pairs
                    else None
                ),
            },
        )
        return self._parse_model(response, contracts.AnalyzeFeedbackResult)

    def analyze_outcomes(
        self,
        instance_id: str,
        *,
        anchor_type: contracts.OutcomeAnchorType,
        relationship_type: str | None = None,
        workflow_name: str | None = None,
        query_name: str | None = None,
        surface_type: str | None = None,
        surface_name: str | None = None,
        limit: int = 200,
        min_support: int = 5,
    ) -> contracts.AnalyzeOutcomesResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/outcomes/analyze",
            json={
                "anchor_type": anchor_type,
                "relationship_type": relationship_type,
                "workflow_name": workflow_name,
                "query_name": query_name,
                "surface_type": surface_type,
                "surface_name": surface_name,
                "limit": limit,
                "min_support": min_support,
            },
        )
        return self._parse_model(response, contracts.AnalyzeOutcomesResult)

    def schema(self, instance_id: str) -> dict[str, Any]:
        response = self._client.get(f"/api/v1/{instance_id}/schema")
        return self._parse_json(response)

    def list_queries(
        self,
        instance_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> contracts.QueryListResult:
        params: dict[str, int] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        response = self._client.get(
            f"/api/v1/{instance_id}/queries",
            params=params,
        )
        return self._parse_model(response, contracts.QueryListResult)

    def describe_query(
        self,
        instance_id: str,
        query_name: str,
    ) -> contracts.NamedQueryInfoResult:
        response = self._client.get(f"/api/v1/{instance_id}/queries/{query_name}")
        return self._parse_model(response, contracts.NamedQueryInfoResult)

    def stats(self, instance_id: str) -> contracts.StatsResult:
        response = self._client.get(f"/api/v1/{instance_id}/stats")
        return self._parse_model(response, contracts.StatsResult)

    def inspect_entity(
        self,
        instance_id: str,
        entity_type: str,
        entity_id: str,
        *,
        direction: str = "both",
        relationship_type: str | None = None,
        limit: int | None = None,
    ) -> contracts.InspectEntityResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/inspect/entity/{entity_type}/{entity_id}",
            params=self._omit_none_params(
                {
                    "direction": direction,
                    "relationship_type": relationship_type,
                    "limit": limit,
                }
            ),
        )
        return self._parse_model(response, contracts.InspectEntityResult)

    def inspect_entity_history(
        self,
        instance_id: str,
        entity_type: str,
        *,
        entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> contracts.EntityChangeHistoryResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/inspect/entity-history/{entity_type}",
            params=self._omit_none_params(
                {
                    "entity_id": entity_id,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )
        return self._parse_model(response, contracts.EntityChangeHistoryResult)

    def inspect_view(
        self,
        instance_id: str,
        view: str,
        *,
        limit: int = 200,
    ) -> contracts.CanonicalViewResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/inspect/{view}",
            params={"limit": limit},
        )
        return self._parse_model(response, contracts.CanonicalViewResult)

    def reload_config(
        self,
        instance_id: str,
        *,
        config_path: str | None = None,
        config_yaml: str | None = None,
    ) -> contracts.ReloadConfigResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/config/reload",
            json={"config_path": config_path, "config_yaml": config_yaml},
        )
        return self._parse_model(response, contracts.ReloadConfigResult)

    def sample(
        self,
        instance_id: str,
        entity_type: str,
        limit: int = 5,
        *,
        fields: builtins.list[str] | None = None,
    ) -> contracts.SampleResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/sample/{entity_type}",
            params=self._omit_none_params({"limit": limit, "fields": fields}),
        )
        return self._parse_model(response, contracts.SampleResult)

    def add_relationships(
        self,
        instance_id: str,
        relationships: builtins.list[contracts.RelationshipInput],
        *,
        dry_run: bool = False,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.AddRelationshipResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/relationships",
            json=self._with_actor_context(
                {
                    "relationships": [item.model_dump(mode="json") for item in relationships],
                    "dry_run": dry_run,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.AddRelationshipResult)

    def add_entities(
        self,
        instance_id: str,
        entities: builtins.list[contracts.EntityInput],
        *,
        dry_run: bool = False,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.AddEntityResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/entities",
            json=self._with_actor_context(
                {
                    "entities": [item.model_dump(mode="json") for item in entities],
                    "dry_run": dry_run,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.AddEntityResult)

    def batch_direct_write(
        self,
        instance_id: str,
        payload: contracts.BatchDirectWritePayload,
        *,
        dry_run: bool = False,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.BatchDirectWriteResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/direct-writes/batch",
            json=self._with_actor_context(
                {"payload": payload.model_dump(mode="json"), "dry_run": dry_run},
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.BatchDirectWriteResult)

    def workflow_lock(
        self,
        instance_id: str,
        *,
        force: bool = False,
    ) -> contracts.WorkflowLockResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/workflows/lock",
            json={"force": force},
        )
        return self._parse_model(response, contracts.WorkflowLockResult)

    def workflow_plan(
        self,
        instance_id: str,
        *,
        workflow_name: str,
        input_payload: dict[str, Any] | None = None,
    ) -> contracts.WorkflowPlanResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/workflows/plan",
            json={"workflow_name": workflow_name, "input": input_payload or {}},
        )
        return self._parse_model(response, contracts.WorkflowPlanResult)

    def workflow_run(
        self,
        instance_id: str,
        *,
        workflow_name: str,
        input_payload: dict[str, Any] | None = None,
        decision_record_id: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.WorkflowRunResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/workflows/run",
            json=self._with_actor_context(
                {
                    "workflow_name": workflow_name,
                    "input": input_payload or {},
                    "decision_record_id": decision_record_id,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.WorkflowRunResult)

    def workflow_apply(
        self,
        instance_id: str,
        *,
        workflow_name: str,
        expected_apply_digest: str,
        expected_head_snapshot_id: str | None = None,
        input_payload: dict[str, Any] | None = None,
        decision_record_id: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.WorkflowApplyResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/workflows/apply",
            json=self._with_actor_context(
                {
                    "workflow_name": workflow_name,
                    "input": input_payload or {},
                    "expected_apply_digest": expected_apply_digest,
                    "expected_head_snapshot_id": expected_head_snapshot_id,
                    "decision_record_id": decision_record_id,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.WorkflowApplyResult)

    def workflow_test(
        self,
        instance_id: str,
        *,
        name: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.WorkflowTestResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/workflows/test",
            json=self._with_actor_context({"name": name}, actor_context),
        )
        return self._parse_model(response, contracts.WorkflowTestResult)

    def propose_workflow(
        self,
        instance_id: str,
        *,
        workflow_name: str,
        input_payload: dict[str, Any] | None = None,
        decision_record_id: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.WorkflowProposeResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/workflows/propose",
            json=self._with_actor_context(
                {
                    "workflow_name": workflow_name,
                    "input": input_payload or {},
                    "decision_record_id": decision_record_id,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.WorkflowProposeResult)

    def create_snapshot(
        self,
        instance_id: str,
        *,
        label: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.SnapshotCreateResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/snapshots",
            json=self._with_actor_context({"label": label}, actor_context),
        )
        return self._parse_model(response, contracts.SnapshotCreateResult)

    def snapshot_instance(
        self,
        instance_id: str,
        *,
        artifact_path: str,
        label: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.InstanceSnapshotResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/instance/snapshot",
            json=self._with_actor_context(
                {"artifact_path": artifact_path, "label": label},
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.InstanceSnapshotResult)

    def restore_instance(
        self,
        *,
        artifact_path: str,
        root_dir: str | None = None,
    ) -> contracts.InstanceRestoreResult:
        response = self._client.post(
            "/api/v1/instances/restore",
            json={"artifact_path": artifact_path, "root_dir": root_dir},
        )
        return self._parse_model(response, contracts.InstanceRestoreResult)

    def relocate_instance(
        self,
        instance_id: str,
        *,
        to_dir: str,
        remove_source: bool = False,
    ) -> contracts.InstanceRelocateResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/instance/relocate",
            json={"to_dir": to_dir, "remove_source": remove_source},
        )
        return self._parse_model(response, contracts.InstanceRelocateResult)

    def list_snapshots(
        self,
        instance_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> contracts.SnapshotListResult:
        params: dict[str, int] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        response = self._client.get(
            f"/api/v1/{instance_id}/snapshots",
            params=params,
        )
        return self._parse_model(response, contracts.SnapshotListResult)

    def register_source_artifact(
        self,
        instance_id: str,
        *,
        source_path: str,
        source_kind: contracts.SourceKind = "markdown",
        source_retention: contracts.SourceRetention = "manifest_only",
        original_uri: str | None = None,
        label: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.RegisterSourceArtifactResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/source-artifacts/register",
            json=self._with_actor_context(
                {
                    "source_path": source_path,
                    "source_kind": source_kind,
                    "source_retention": source_retention,
                    "original_uri": original_uri,
                    "label": label,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.RegisterSourceArtifactResult)

    def dereference_source_evidence(
        self,
        instance_id: str,
        *,
        source_artifact_id: str,
        chunk_id: str | None = None,
        heading_path: builtins.list[str] | None = None,
        block_selector: str | None = None,
        expected_content_hash: str | None = None,
    ) -> contracts.DereferenceSourceEvidenceResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/source-evidence/dereference",
            json={
                "source_artifact_id": source_artifact_id,
                "chunk_id": chunk_id,
                "heading_path": heading_path,
                "block_selector": block_selector,
                "expected_content_hash": expected_content_hash,
            },
        )
        return self._parse_model(response, contracts.DereferenceSourceEvidenceResult)

    def clone_snapshot(
        self,
        instance_id: str,
        *,
        snapshot_id: str,
        root_dir: str,
    ) -> contracts.CloneSnapshotResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/snapshots/clone",
            json={"snapshot_id": snapshot_id, "root_dir": root_dir},
        )
        return self._parse_model(response, contracts.CloneSnapshotResult)

    def state_publish(
        self,
        instance_id: str,
        *,
        transport_ref: str,
        state_id: str,
        release_id: str,
        compatibility: contracts.StateCompatibility,
    ) -> contracts.StatePublishResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/state/publish",
            json={
                "transport_ref": transport_ref,
                "state_id": state_id,
                "release_id": release_id,
                "compatibility": compatibility,
            },
        )
        return self._parse_model(response, contracts.StatePublishResult)

    def state_status(self, instance_id: str) -> contracts.StateStatusResult:
        response = self._client.get(f"/api/v1/{instance_id}/state/status")
        return self._parse_model(response, contracts.StateStatusResult)

    def state_pull_preview(self, instance_id: str) -> contracts.StatePullPreviewResult:
        response = self._client.post(f"/api/v1/{instance_id}/state/pull/preview")
        return self._parse_model(response, contracts.StatePullPreviewResult)

    def state_pull_apply(
        self,
        instance_id: str,
        *,
        expected_apply_digest: str,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.StatePullApplyResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/state/pull/apply",
            json=self._with_actor_context(
                {"expected_apply_digest": expected_apply_digest},
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.StatePullApplyResult)

    def add_constraint(
        self,
        instance_id: str,
        *,
        name: str,
        rule: str,
        severity: contracts.ConstraintSeverity = "warning",
        description: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.AddConstraintResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/constraints",
            json=self._with_actor_context(
                {
                    "name": name,
                    "rule": rule,
                    "severity": severity,
                    "description": description,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.AddConstraintResult)

    def add_decision_policy(
        self,
        instance_id: str,
        *,
        name: str,
        applies_to: contracts.DecisionPolicyAppliesTo,
        relationship_type: str,
        effect: contracts.DecisionPolicyEffect,
        match: contracts.DecisionPolicyMatchInput | None = None,
        description: str | None = None,
        rationale: str = "",
        query_name: str | None = None,
        workflow_name: str | None = None,
        expires_at: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.AddDecisionPolicyResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/decision-policies",
            json=self._with_actor_context(
                {
                    "name": name,
                    "applies_to": applies_to,
                    "relationship_type": relationship_type,
                    "effect": effect,
                    "match": match.model_dump(mode="json", by_alias=True) if match else None,
                    "description": description,
                    "rationale": rationale,
                    "query_name": query_name,
                    "workflow_name": workflow_name,
                    "expires_at": expires_at,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.AddDecisionPolicyResult)

    def get_entity(
        self,
        instance_id: str,
        entity_type: str,
        entity_id: str,
    ) -> contracts.GetEntityResult:
        response = self._client.get(f"/api/v1/{instance_id}/entities/{entity_type}/{entity_id}")
        return self._parse_model(response, contracts.GetEntityResult)

    def get_relationship(
        self,
        instance_id: str,
        *,
        from_type: str,
        from_id: str,
        relationship_type: str,
        to_type: str,
        to_id: str,
        edge_key: int | None = None,
    ) -> contracts.GetRelationshipResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/relationships/lookup",
            params=self._omit_none_params(
                {
                    "from_type": from_type,
                    "from_id": from_id,
                    "relationship_type": relationship_type,
                    "to_type": to_type,
                    "to_id": to_id,
                    "edge_key": edge_key,
                }
            ),
        )
        return self._parse_model(response, contracts.GetRelationshipResult)

    def get_relationship_lineage(
        self,
        instance_id: str,
        *,
        from_type: str,
        from_id: str,
        relationship_type: str,
        to_type: str,
        to_id: str,
        edge_key: int | None = None,
    ) -> contracts.RelationshipLineageResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/relationships/lineage",
            params=self._omit_none_params(
                {
                    "from_type": from_type,
                    "from_id": from_id,
                    "relationship_type": relationship_type,
                    "to_type": to_type,
                    "to_id": to_id,
                    "edge_key": edge_key,
                }
            ),
        )
        return self._parse_model(response, contracts.RelationshipLineageResult)

    def propose_group(
        self,
        instance_id: str,
        *,
        relationship_type: str,
        members: builtins.list[contracts.MemberInput],
        thesis_text: str = "",
        thesis_facts: dict[str, Any] | None = None,
        analysis_state: dict[str, Any] | None = None,
        signal_sources_used: builtins.list[str] | None = None,
        proposed_by: contracts.GroupProposedBy = "agent",
        suggested_priority: str | None = None,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.ProposeGroupToolResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/groups/propose",
            json=self._with_actor_context(
                {
                    "relationship_type": relationship_type,
                    "members": [item.model_dump(mode="json") for item in members],
                    "thesis_text": thesis_text,
                    "thesis_facts": thesis_facts,
                    "analysis_state": analysis_state,
                    "signal_sources_used": signal_sources_used,
                    "proposed_by": proposed_by,
                    "suggested_priority": suggested_priority,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.ProposeGroupToolResult)

    def resolve_group(
        self,
        instance_id: str,
        group_id: str,
        *,
        action: contracts.GroupAction,
        rationale: str = "",
        resolved_by: contracts.GroupResolvedBy = "human",
        expected_pending_version: int,
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.ResolveGroupToolResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/groups/{group_id}/resolve",
            json=self._with_actor_context(
                {
                    "action": action,
                    "rationale": rationale,
                    "resolved_by": resolved_by,
                    "expected_pending_version": expected_pending_version,
                },
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.ResolveGroupToolResult)

    def update_trust_status(
        self,
        instance_id: str,
        resolution_id: str,
        *,
        trust_status: contracts.GroupTrustStatus,
        reason: str = "",
        actor_context: contracts.GovernedActorContext | dict[str, Any] | None = None,
    ) -> contracts.UpdateTrustStatusToolResult:
        response = self._client.patch(
            f"/api/v1/{instance_id}/resolutions/{resolution_id}/trust",
            json=self._with_actor_context(
                {"trust_status": trust_status, "reason": reason},
                actor_context,
            ),
        )
        return self._parse_model(response, contracts.UpdateTrustStatusToolResult)

    def get_group(self, instance_id: str, group_id: str) -> contracts.GetGroupToolResult:
        response = self._client.get(f"/api/v1/{instance_id}/groups/{group_id}")
        return self._parse_model(response, contracts.GetGroupToolResult)

    def get_group_status(
        self,
        instance_id: str,
        *,
        group_id: str | None = None,
        signature: str | None = None,
    ) -> contracts.GroupBucketStatusToolResult:
        if group_id is None and signature is None:
            raise ValueError("Provide group_id or signature")
        if group_id is not None:
            response = self._client.get(f"/api/v1/{instance_id}/groups/{group_id}/status")
        else:
            assert signature is not None
            response = self._client.get(f"/api/v1/{instance_id}/group-status/{signature}")
        return self._parse_model(response, contracts.GroupBucketStatusToolResult)

    def list_groups(
        self,
        instance_id: str,
        *,
        relationship_type: str | None = None,
        status: contracts.GroupStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> contracts.ListGroupsToolResult:
        params: dict[str, str | int | float | bool | None] = {
            "limit": limit,
            "offset": offset,
        }
        if relationship_type is not None:
            params["relationship_type"] = relationship_type
        if status is not None:
            params["status"] = status
        response = self._client.get(
            f"/api/v1/{instance_id}/groups",
            params=params,
        )
        return self._parse_model(response, contracts.ListGroupsToolResult)

    def list_resolutions(
        self,
        instance_id: str,
        *,
        relationship_type: str | None = None,
        action: contracts.GroupAction | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> contracts.ListResolutionsToolResult:
        params: dict[str, str | int | float | bool | None] = {
            "limit": limit,
            "offset": offset,
        }
        if relationship_type is not None:
            params["relationship_type"] = relationship_type
        if action is not None:
            params["action"] = action
        response = self._client.get(
            f"/api/v1/{instance_id}/resolutions",
            params=params,
        )
        return self._parse_model(response, contracts.ListResolutionsToolResult)

"""Thin HTTP client for the Playbill-only daemon surface."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel

from cruxible_client import contracts
from cruxible_client.errors import (
    ConfigError,
    CoreError,
    ErrorResponse,
    ServerUnreachableError,
    response_to_error,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def _default_timeout() -> httpx.Timeout:
    budget = float(os.environ.get("CRUXIBLE_CLIENT_TIMEOUT_S", "180"))
    return httpx.Timeout(connect=5.0, read=budget, write=budget, pool=5.0)


class _TransportGuard:
    def __init__(self, client: httpx.Client, target: str) -> None:
        self._client = client
        self._target = target

    def _guard(self, method: str, *args: Any, **kwargs: Any) -> httpx.Response:
        try:
            response: httpx.Response = getattr(self._client, method)(*args, **kwargs)
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            budget = os.environ.get("CRUXIBLE_CLIENT_TIMEOUT_S", "180")
            raise ServerUnreachableError(
                self._target,
                (
                    f"no response after {budget}s — the request reached the server and "
                    "may still be running or may already have completed. Do not assume "
                    "failure: verify state before retrying, and raise "
                    "CRUXIBLE_CLIENT_TIMEOUT_S for long operations"
                ),
            ) from exc
        except httpx.TransportError as exc:
            raise ServerUnreachableError(self._target, str(exc) or exc.__class__.__name__) from exc
        return response

    def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return self._guard("get", *args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return self._guard("post", *args, **kwargs)

    def close(self) -> None:
        self._client.close()


class CruxibleClient:
    """Synchronous client for daemon host, credential, and Playbill operations."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        socket_path: str | None = None,
        token: str | None = None,
    ) -> None:
        if bool(base_url) == bool(socket_path):
            raise ConfigError("Configure exactly one of base_url or socket_path for CruxibleClient")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if socket_path is not None:
            target = f"unix:{socket_path}"
            raw_client = httpx.Client(
                base_url="http://cruxible",
                headers=headers,
                transport=httpx.HTTPTransport(uds=socket_path),
                timeout=_default_timeout(),
            )
        else:
            assert base_url is not None
            target = base_url
            raw_client = httpx.Client(
                base_url=base_url,
                headers=headers,
                timeout=_default_timeout(),
            )
        self._client = _TransportGuard(raw_client, target)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CruxibleClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _check_error(response: httpx.Response) -> None:
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

    def version(self) -> str:
        response = self._client.get("/version")
        payload = self._parse_json(response)
        version = payload.get("version")
        if not isinstance(version, str):
            raise CoreError("Server /version response missing version string")
        return version

    def server_info(self) -> contracts.ServerInfoResult:
        response = self._client.get("/api/v1/server/info")
        return self._parse_model(response, contracts.ServerInfoResult)

    def server_restart(self) -> contracts.ServerRestartResult:
        response = self._client.post("/api/v1/server/restart")
        return self._parse_model(response, contracts.ServerRestartResult)

    def create_playbill_host(
        self, *, instance_id: str | None = None
    ) -> contracts.PlaybillHostResult:
        response = self._client.post(
            "/api/v1/runtime/instances",
            json={"instance_id": instance_id},
        )
        return self._parse_model(response, contracts.PlaybillHostResult)

    def claim_runtime_bootstrap(
        self, instance_id: str, bootstrap_secret: str
    ) -> contracts.RuntimeCredentialBootstrapResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/bootstrap/claim",
            json={"bootstrap_secret": bootstrap_secret},
        )
        return self._parse_model(response, contracts.RuntimeCredentialBootstrapResult)

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

    def list_runtime_credentials(self, instance_id: str) -> contracts.RuntimeCredentialListResult:
        response = self._client.get(f"/api/v1/{instance_id}/runtime/credentials")
        return self._parse_model(response, contracts.RuntimeCredentialListResult)

    def revoke_runtime_credential(
        self, instance_id: str, credential_id: str
    ) -> contracts.RuntimeCredentialResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{credential_id}/revoke"
        )
        return self._parse_model(response, contracts.RuntimeCredentialResult)

    def rotate_runtime_credential(
        self, instance_id: str, credential_id: str
    ) -> contracts.RuntimeCredentialResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate"
        )
        return self._parse_model(response, contracts.RuntimeCredentialResult)

    @staticmethod
    def _playbill_coordinate_params(
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None,
    ) -> dict[str, str]:
        if at is None:
            return {}
        value = at.model_dump(mode="json") if isinstance(at, BaseModel) else dict(at)
        return {
            name: str(value[name])
            for name in ("git_oid", "semantic_root", "generation_root", "compiler_digest")
        }

    def init_playbill(
        self,
        instance_id: str,
        *,
        principals: Sequence[Mapping[str, Any]],
        operating_profile: Literal["local", "cloud"] = "local",
    ) -> contracts.PlaybillInitResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/init",
            json={
                "principals": [dict(item) for item in principals],
                "operating_profile": operating_profile,
            },
        )
        return self._parse_model(response, contracts.PlaybillInitResult)

    def store_playbill_body(
        self, instance_id: str, content: bytes
    ) -> contracts.PlaybillCasObjectResult:
        import base64

        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/bodies",
            json={"content_base64": base64.b64encode(content).decode("ascii")},
        )
        return self._parse_model(response, contracts.PlaybillCasObjectResult)

    def propose_playbill_document(
        self,
        instance_id: str,
        *,
        shell: Mapping[str, Any],
        proposal_name: str,
        source_compilation_digest: str | None = None,
        base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillProposalInspection:
        payload: dict[str, Any] = {
            "shell": dict(shell),
            "proposal_name": proposal_name,
            "source_compilation_digest": source_compilation_digest,
        }
        if base is not None:
            payload["base"] = (
                base.model_dump(mode="json") if isinstance(base, BaseModel) else dict(base)
            )
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/documents/proposals", json=payload
        )
        return self._parse_model(response, contracts.PlaybillProposalInspection)

    def propose_playbill_principal_change(
        self,
        instance_id: str,
        *,
        principal: Mapping[str, Any],
        proposal_name: str,
        base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillProposalInspection:
        payload: dict[str, Any] = {
            "principal": dict(principal),
            "proposal_name": proposal_name,
        }
        if base is not None:
            payload["base"] = (
                base.model_dump(mode="json") if isinstance(base, BaseModel) else dict(base)
            )
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/principals/proposals", json=payload
        )
        return self._parse_model(response, contracts.PlaybillProposalInspection)

    def list_playbill_principals(self, instance_id: str) -> contracts.PlaybillPrincipalList:
        response = self._client.get(f"/api/v1/{instance_id}/playbill/principals")
        return self._parse_model(response, contracts.PlaybillPrincipalList)

    def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
        response = self._client.get(f"/api/v1/{instance_id}/playbill/whoami")
        return self._parse_model(response, contracts.PlaybillWhoAmI)

    def list_playbill_proposals(
        self,
        instance_id: str,
        *,
        status: Literal["open", "settled"] | None = None,
    ) -> contracts.PlaybillProposalList:
        params = {} if status is None else {"status": status}
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/proposals",
            params=params,
        )
        return self._parse_model(response, contracts.PlaybillProposalList)

    def readmit_playbill_proposal(
        self,
        instance_id: str,
        proposal_id: str,
    ) -> contracts.PlaybillProposalReadmitResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/readmit",
            json={"tag": "playbill-proposal-readmit-request-v1"},
        )
        return self._parse_model(response, contracts.PlaybillProposalReadmitResult)

    def inspect_playbill_proposal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillProposalInspection:
        response = self._client.get(f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}")
        return self._parse_model(response, contracts.PlaybillProposalInspection)

    def inspect_playbill_refusal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillRefusalInspection:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/refusal"
        )
        return self._parse_model(response, contracts.PlaybillRefusalInspection)

    def review_playbill_proposal(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        include_body: bool = False,
    ) -> contracts.PlaybillProposalReview:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/review",
            json={"include_body": include_body},
        )
        return self._parse_model(response, contracts.PlaybillProposalReview)

    def prepare_playbill_approval(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        signer_id: str,
        include_body: bool = False,
    ) -> contracts.PlaybillApprovalChallenge:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
            json={"signer_id": signer_id, "include_body": include_body},
        )
        return self._parse_model(response, contracts.PlaybillApprovalChallenge)

    def submit_playbill_approval(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        attestation: Mapping[str, Any],
    ) -> contracts.PlaybillApprovalReceipt:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals",
            json={"attestation": dict(attestation)},
        )
        return self._parse_model(response, contracts.PlaybillApprovalReceipt)

    def approve_playbill_proposal(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        signer_id: str,
        signer: Callable[[dict[str, Any]], Mapping[str, Any]],
        include_body: bool = False,
    ) -> contracts.PlaybillApprovalReceipt:
        challenge = self.prepare_playbill_approval(
            instance_id,
            proposal_id,
            signer_id=signer_id,
            include_body=include_body,
        )
        return self.submit_playbill_approval(
            instance_id,
            proposal_id,
            attestation=signer(dict(challenge.statement)),
        )

    def activate_playbill_proposal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillActivationReceipt:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate"
        )
        return self._parse_model(response, contracts.PlaybillActivationReceipt)

    def list_playbill_documents(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillDocumentList:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/documents",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillDocumentList)

    def get_playbill_document(
        self,
        instance_id: str,
        identity: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillDocumentView:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/documents/{identity}",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillDocumentView)

    def dereference_playbill_document(
        self,
        instance_id: str,
        identity: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillBodyRead:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/documents/{identity}/body",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillBodyRead)

    def playbill_document_history(
        self, instance_id: str, identity: str
    ) -> contracts.PlaybillDocumentHistory:
        response = self._client.get(f"/api/v1/{instance_id}/playbill/documents/{identity}/history")
        return self._parse_model(response, contracts.PlaybillDocumentHistory)

    def explain_playbill_subject(
        self,
        instance_id: str,
        *,
        subject: Mapping[str, Any],
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any],
        detail: Literal["summary", "evidence", "proof"] = "summary",
        include_body: bool = False,
    ) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
        coordinate = at.model_dump(mode="json") if isinstance(at, BaseModel) else dict(at)
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/explain",
            json={
                "subject": dict(subject),
                "at": coordinate,
                "detail": detail,
                "include_body": include_body,
            },
        )
        payload = self._parse_json(response)
        if payload.get("tag") == "playbill-explain-v1":
            return contracts.PlaybillExplainResult.model_validate(payload)
        return contracts.PlaybillExplainUnsupportedDetail.model_validate(payload)

    def playbill_source_context(self, instance_id: str) -> contracts.PlaybillSourceContext:
        response = self._client.get(f"/api/v1/{instance_id}/playbill/sources/context")
        return self._parse_model(response, contracts.PlaybillSourceContext)

    def check_playbill_source_bundle(
        self, instance_id: str, *, bundle: Mapping[str, Any]
    ) -> contracts.PlaybillSourceCheckResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/sources/check",
            json={"bundle": dict(bundle)},
        )
        return self._parse_model(response, contracts.PlaybillSourceCheckResult)

    def propose_playbill_source_bundle(
        self,
        instance_id: str,
        *,
        bundle: Mapping[str, Any],
        source_name: str,
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/sources/proposals",
            json={
                "bundle": dict(bundle),
                "source_name": source_name,
                "proposal_name": proposal_name,
            },
        )
        return self._parse_model(response, contracts.PlaybillProposalInspection)

    @staticmethod
    def _playbill_coordinate_body(
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if at is None:
            return None
        return at.model_dump(mode="json") if isinstance(at, BaseModel) else dict(at)

    def _playbill_proposal_payload(
        self,
        *,
        proposal_name: str,
        base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None,
        **fields: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"proposal_name": proposal_name, **fields}
        base_payload = self._playbill_coordinate_body(base)
        if base_payload is not None:
            payload["base"] = base_payload
        return payload

    def propose_playbill_subject(
        self,
        instance_id: str,
        *,
        shell: Mapping[str, Any],
        proposal_name: str,
        base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillProposalInspection:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/subjects/proposals",
            json=self._playbill_proposal_payload(
                proposal_name=proposal_name, base=base, shell=dict(shell)
            ),
        )
        return self._parse_model(response, contracts.PlaybillProposalInspection)

    def list_playbill_subjects(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillSubjectList:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/subjects",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillSubjectList)

    def get_playbill_subject(
        self,
        instance_id: str,
        subject_kind: str,
        subject_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillSubjectView:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillSubjectView)

    def playbill_subject_history(
        self, instance_id: str, subject_kind: str, subject_id: str
    ) -> contracts.PlaybillSubjectHistory:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}/history"
        )
        return self._parse_model(response, contracts.PlaybillSubjectHistory)

    def propose_playbill_claim_type(
        self,
        instance_id: str,
        *,
        claim_type: Mapping[str, Any],
        proposal_name: str,
        base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillProposalInspection:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/claim-types/proposals",
            json=self._playbill_proposal_payload(
                proposal_name=proposal_name, base=base, claim_type=dict(claim_type)
            ),
        )
        return self._parse_model(response, contracts.PlaybillProposalInspection)

    def list_playbill_claim_types(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillClaimTypeList:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/claim-types",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillClaimTypeList)

    def get_playbill_claim_type(
        self,
        instance_id: str,
        predicate: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillClaimTypeView:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/claim-types/{predicate}",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillClaimTypeView)

    def propose_playbill_claim(
        self,
        instance_id: str,
        *,
        authoring: Mapping[str, Any],
        proposal_name: str,
        base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillClaimProposal:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/claims/proposals",
            json=self._playbill_proposal_payload(
                proposal_name=proposal_name, base=base, authoring=dict(authoring)
            ),
        )
        return self._parse_model(response, contracts.PlaybillClaimProposal)

    def propose_playbill_claims(
        self,
        instance_id: str,
        *,
        authorings: Sequence[Mapping[str, Any]],
        proposal_name: str,
        base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillClaimBatchProposal:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/claims/proposals/batch",
            json=self._playbill_proposal_payload(
                proposal_name=proposal_name,
                base=base,
                authorings=[dict(item) for item in authorings],
            ),
        )
        return self._parse_model(response, contracts.PlaybillClaimBatchProposal)

    def create_playbill_authoring_intent(
        self,
        instance_id: str,
        *,
        payload: Mapping[str, Any],
    ) -> contracts.PlaybillAuthoringIntentView:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/authoring/intents",
            json={
                "tag": "playbill-authoring-intent-create-request-v1",
                "payload": dict(payload),
            },
        )
        return self._parse_model(response, contracts.PlaybillAuthoringIntentView)

    def get_playbill_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringIntentView:
        response = self._client.get(f"/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}")
        return self._parse_model(response, contracts.PlaybillAuthoringIntentView)

    def resume_playbill_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringIntentView:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/resume"
        )
        return self._parse_model(response, contracts.PlaybillAuthoringIntentView)

    def list_pending_playbill_authoring_intents(
        self,
        instance_id: str,
    ) -> contracts.PlaybillAuthoringIntentList:
        response = self._client.get(f"/api/v1/{instance_id}/playbill/authoring/intents")
        return self._parse_model(response, contracts.PlaybillAuthoringIntentList)

    def compile_playbill_authoring(
        self,
        instance_id: str,
        *,
        payload: Mapping[str, Any],
        intent_id: str | None = None,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/authoring/compile",
            json={
                "tag": "playbill-authoring-intent-compile-request-v1",
                "payload": dict(payload),
                "intent_id": intent_id,
            },
        )
        return self._parse_model(response, contracts.PlaybillAuthoringPreflightResult)

    def preflight_playbill_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/preflight",
            json={"tag": "playbill-authoring-intent-preflight-request-v1"},
        )
        return self._parse_model(response, contracts.PlaybillAuthoringPreflightResult)

    def submit_playbill_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillAuthoringSubmitResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/submit",
            json={"tag": "playbill-authoring-intent-submit-request-v1"},
        )
        return self._parse_model(response, contracts.PlaybillAuthoringSubmitResult)

    def playbill_authoring_intent_status(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillCandidateStatus:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/status"
        )
        return self._parse_model(response, contracts.PlaybillCandidateStatus)

    def confirm_playbill_authoring_insertion(
        self,
        instance_id: str,
        intent_id: str,
        *,
        observation: Mapping[str, Any],
    ) -> contracts.PlaybillInsertionConfirmResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/confirm",
            json={
                "tag": "playbill-insertion-confirm-request-v1",
                "observation": dict(observation),
            },
        )
        return self._parse_model(response, contracts.PlaybillInsertionConfirmResult)

    def abandon_playbill_authoring_insertion(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.PlaybillInsertionAbandonResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/abandon",
            json={"tag": "playbill-insertion-abandon-request-v1"},
        )
        return self._parse_model(response, contracts.PlaybillInsertionAbandonResult)

    def list_playbill_claims(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        subject_path: str | None = None,
        predicate: str | None = None,
        include_retired: bool = False,
    ) -> contracts.PlaybillClaimList:
        params: dict[str, Any] = {
            **self._playbill_coordinate_params(at),
            "include_retired": include_retired,
        }
        if subject_path is not None:
            params["subject_path"] = subject_path
        if predicate is not None:
            params["predicate"] = predicate
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/claims",
            params=params,
        )
        return self._parse_model(response, contracts.PlaybillClaimList)

    def get_playbill_claim(
        self,
        instance_id: str,
        identity: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        evaluation_time: str | None = None,
    ) -> contracts.PlaybillClaimViewV2:
        params = self._playbill_coordinate_params(at)
        if evaluation_time is not None:
            params["evaluation_time"] = evaluation_time
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/claims/{identity}",
            params=params,
        )
        return self._parse_model(response, contracts.PlaybillClaimViewV2)

    def playbill_claim_history(
        self, instance_id: str, identity: str
    ) -> contracts.PlaybillClaimHistory:
        response = self._client.get(f"/api/v1/{instance_id}/playbill/claims/{identity}/history")
        return self._parse_model(response, contracts.PlaybillClaimHistory)

    def explain_playbill_claim(
        self,
        instance_id: str,
        identity: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        evaluation_time: str | None = None,
    ) -> contracts.PlaybillClaimExplanationV2:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/claims/{identity}/explanation",
            json={
                "at": self._playbill_coordinate_body(at),
                "evaluation_time": evaluation_time,
            },
        )
        return self._parse_model(response, contracts.PlaybillClaimExplanationV2)

    def propose_playbill_query_definition(
        self,
        instance_id: str,
        *,
        query: Mapping[str, Any],
        proposal_name: str,
        base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillProposalInspection:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/queries/proposals",
            json=self._playbill_proposal_payload(
                proposal_name=proposal_name, base=base, query=dict(query)
            ),
        )
        return self._parse_model(response, contracts.PlaybillProposalInspection)

    def list_playbill_query_definitions(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillQueryDefinitionList:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/queries",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillQueryDefinitionList)

    def get_playbill_query_definition(
        self,
        instance_id: str,
        name: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillQueryDefinitionView:
        response = self._client.get(
            f"/api/v1/{instance_id}/playbill/queries/{name}",
            params=self._playbill_coordinate_params(at),
        )
        return self._parse_model(response, contracts.PlaybillQueryDefinitionView)

    def run_playbill_query(
        self,
        instance_id: str,
        name: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        evaluation_time: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        budgets: Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillQueryRun:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/queries/{name}/run",
            json={
                "at": self._playbill_coordinate_body(at),
                "evaluation_time": evaluation_time,
                "parameters": None if parameters is None else dict(parameters),
                "budgets": None if budgets is None else dict(budgets),
            },
        )
        return self._parse_model(response, contracts.PlaybillQueryRun)

    def discover_playbill(
        self,
        instance_id: str,
        *,
        query: str | None = None,
        entrypoint: str | None = None,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        evaluation_time: str | None = None,
        profile: Literal["interfaces", "subjects", "all"] = "interfaces",
        budget: Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillDiscoveryResult | contracts.PlaybillInterfaceInventory:
        payload: dict[str, Any] = {
            "query": query,
            "entrypoint": entrypoint,
            "at": self._playbill_coordinate_body(at),
            "evaluation_time": evaluation_time,
            "profile": profile,
        }
        if budget is not None:
            payload["budget"] = dict(budget)
        response = self._client.post(f"/api/v1/{instance_id}/playbill/discover", json=payload)
        parsed = self._parse_json(response)
        if parsed.get("tag") == "playbill-interface-inventory-v1":
            return contracts.PlaybillInterfaceInventory.model_validate(parsed)
        return contracts.PlaybillDiscoveryResult.model_validate(parsed)

    def search_playbill(
        self,
        instance_id: str,
        *,
        mode: Literal["search", "list", "orient"],
        query: str | None = None,
        kinds: Sequence[str] = ("brief", "claim", "demand", "procedure"),
        subject: Mapping[str, Any] | None = None,
        statuses: Sequence[str] = (),
        cursor: Mapping[str, Any] | None = None,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        evaluation_time: str | None = None,
        budgets: Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillSearchResult:
        payload: dict[str, Any] = {
            "mode": mode,
            "query": query,
            "kinds": sorted(set(kinds)),
            "subject": None if subject is None else dict(subject),
            "statuses": sorted(set(statuses)),
            "cursor": None if cursor is None else dict(cursor),
            "at": self._playbill_coordinate_body(at),
            "evaluation_time": evaluation_time,
        }
        resolved_budgets = budgets
        if resolved_budgets is None and cursor is not None:
            cursor_budgets = cursor.get("budgets")
            if isinstance(cursor_budgets, Mapping):
                resolved_budgets = cursor_budgets
        if resolved_budgets is not None:
            payload["budgets"] = dict(resolved_budgets)
        response = self._client.post(f"/api/v1/{instance_id}/playbill/search", json=payload)
        return self._parse_model(response, contracts.PlaybillSearchResult)

    def expand_playbill(
        self,
        instance_id: str,
        *,
        address: Mapping[str, Any],
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        evaluation_time: str | None = None,
        facets: Sequence[str] = (),
        budget: Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillContextCapsule:
        payload: dict[str, Any] = {
            "address": dict(address),
            "at": self._playbill_coordinate_body(at),
            "evaluation_time": evaluation_time,
            "facets": list(facets),
        }
        if budget is not None:
            payload["budget"] = dict(budget)
        response = self._client.post(f"/api/v1/{instance_id}/playbill/expand", json=payload)
        return self._parse_model(response, contracts.PlaybillContextCapsule)

    def resolve_playbill_coverage(
        self,
        instance_id: str,
        *,
        observations: Sequence[Mapping[str, Any]],
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        budget: Mapping[str, Any] | None = None,
        scan_budget: Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillCoverageResult:
        """Resolve coverage for a batch of already-observed working sources.

        The caller observes its own working set -- binding each path to a
        declared logical source and hashing the bytes it actually read -- and
        this call carries those observations. Coverage is delivered against
        them; the daemon reads no client filesystem.
        """

        payload: dict[str, Any] = {
            "at": self._playbill_coordinate_body(at),
            "observations": [dict(item) for item in observations],
        }
        if budget is not None:
            payload["budget"] = dict(budget)
        if scan_budget is not None:
            payload["scan_budget"] = dict(scan_budget)
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/coverage/resolve",
            json=payload,
        )
        return self._parse_model(response, contracts.PlaybillCoverageResult)

    def export_playbill_floor(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillFloorExport:
        response = self._client.post(
            f"/api/v1/{instance_id}/playbill/floor/export",
            json={"at": self._playbill_coordinate_body(at)},
        )
        return self._parse_model(response, contracts.PlaybillFloorExport)

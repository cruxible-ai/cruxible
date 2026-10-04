"""Thin HTTP client for the Playbill-only daemon surface."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import (
    AuthoringIntentCompileRequest,
    AuthoringIntentCompileRequestV1,
    AuthoringIntentCompileRequestV2,
    AuthoringIntentCreateRequest,
    AuthoringIntentCreateRequestV1,
    AuthoringIntentCreateRequestV2,
)
from cruxible_client.contracts.capture_reads import CaptureRead, CaptureReadRequest
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequest,
    ClaimAttestationAppendResult,
)
from cruxible_client.contracts.claim_reads import (
    ClaimBackingsRequest,
    ClaimBackingsResult,
    ClaimReadBatchRequest,
    ClaimReadBatchResult,
)
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.errors import (
    SinceRequestInvalid,
)
from cruxible_client.contracts.evidence_rule_upgrade import (
    EvidenceRuleUpgradeRequest,
    EvidenceRuleUpgradeResult,
)
from cruxible_client.contracts.floor import FloorDelta
from cruxible_client.contracts.get_reads import (
    GetBatchRequest,
    GetBatchResult,
    GetRequest,
    GetResult,
)
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitBuildRequest,
    KitBuildResult,
    KitChangeResult,
    KitRemoveRequest,
    KitStatus,
)
from cruxible_client.contracts.principals import (
    PRINCIPAL_ID_ENV,
    PRINCIPAL_ID_HEADER,
    is_canonical_principal_id,
)
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreview,
    ProcedureSourcePreviewRequest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_installation import (
    ProviderCatalog,
    ProviderInstallRequest,
    ProviderInstallResult,
)
from cruxible_client.contracts.runtime_credentials import RuntimeCredentialPrincipalProof
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_client.contracts.write import (
    RetireRequest,
    SetRequest,
    WriteOutcome,
    WriteRequest,
)
from cruxible_client.errors import (
    ConfigError,
    CoreError,
    ErrorResponse,
    ServerUnreachableError,
    response_to_error,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
_CLAIM_TYPE_MIGRATION_RESPONSE: TypeAdapter[contracts.ClaimTypeMigrationResponse] = TypeAdapter(
    contracts.ClaimTypeMigrationResponse
)


def validate_principal_id(principal_id: str) -> str:
    """Refuse a principal ID no registry could hold, before it reaches the wire."""

    if not is_canonical_principal_id(principal_id):
        raise ConfigError(
            f"principal ID {principal_id!r} is not a canonical lowercase identifier "
            "(a letter, then up to 127 of a-z 0-9 . _ -); repair: set "
            f"{PRINCIPAL_ID_ENV} or --principal-id to a registered principal ID"
        )
    return principal_id


def configured_principal_id(environ: Mapping[str, str] | None = None) -> str | None:
    """The principal ID this process is configured to act as, if any."""

    env = os.environ if environ is None else environ
    raw = (env.get(PRINCIPAL_ID_ENV) or "").strip()
    return validate_principal_id(raw) if raw else None


# The per-request budget an ordinary call is given.
CLIENT_TIMEOUT_ENV = "CRUXIBLE_CLIENT_TIMEOUT_S"
DEFAULT_CLIENT_TIMEOUT_S = 180.0


def _budget(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number of seconds") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive number of seconds")
    return value


def _default_timeout() -> httpx.Timeout:
    budget = _budget(CLIENT_TIMEOUT_ENV, DEFAULT_CLIENT_TIMEOUT_S)
    return httpx.Timeout(connect=5.0, read=budget, write=budget, pool=5.0)


class _TransportGuard:
    def __init__(self, client: httpx.Client, target: str) -> None:
        self._client = client
        self._target = target

    def _guard(self, method: str, *args: Any, **kwargs: Any) -> httpx.Response:
        try:
            response: httpx.Response = getattr(self._client, method)(*args, **kwargs)
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            budget = os.environ.get(CLIENT_TIMEOUT_ENV, str(int(DEFAULT_CLIENT_TIMEOUT_S)))
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


def _change_control(dry_run: bool | None, at: str | None) -> dict[str, Any]:
    """The R12 change-control fields a change request carries, when set."""

    body: dict[str, Any] = {}
    if dry_run is not None:
        body["dry_run"] = dry_run
    if at is not None:
        body["at"] = at
    return body


class CruxibleClient:
    """Synchronous client for daemon host, credential, and Cruxible operations."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        socket_path: str | None = None,
        token: str | None = None,
        principal_id: str | None = None,
    ) -> None:
        if bool(base_url) == bool(socket_path):
            raise ConfigError("Configure exactly one of base_url or socket_path for CruxibleClient")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if principal_id is not None:
            headers[PRINCIPAL_ID_HEADER] = validate_principal_id(principal_id)
        self.principal_id = principal_id
        self.socket_path = socket_path
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
        version, _snapshot_digest = self._version_info()
        return version

    def daemon_identity(self) -> tuple[str, str | None]:
        """Return the daemon's version and the boot id of its process image."""

        response = self._client.get("/version")
        payload = self._parse_json(response)
        version = payload.get("version")
        if not isinstance(version, str):
            raise CoreError("Server /version response missing version string")
        boot_id = payload.get("boot_id")
        return version, boot_id if isinstance(boot_id, str) else None

    def _version_info(self) -> tuple[str, str | None]:
        """Return package and served authoring-contract versions from the public probe."""

        response = self._client.get("/version")
        payload = self._parse_json(response)
        version = payload.get("version")
        if not isinstance(version, str):
            raise CoreError("Server /version response missing version string")
        snapshot_digest = payload.get("sdk_contract_snapshot_digest")
        return version, snapshot_digest if isinstance(snapshot_digest, str) else None

    def check_projection_blocks(
        self,
        instance_id: str,
        *,
        request: contracts.ProjectionCheckRequest,
    ) -> contracts.ProjectionCheckResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/projections/check",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.ProjectionCheckResult)

    def read_block_sync_backing(
        self,
        instance_id: str,
        *,
        request: contracts.BlockSyncReadRequest,
    ) -> contracts.BlockSyncReadResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/projections/sync-backing",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.BlockSyncReadResult)

    def server_info(self) -> contracts.ServerInfoResult:
        response = self._client.get("/api/v1/server/info")
        return self._parse_model(response, contracts.ServerInfoResult)

    def server_restart(self) -> contracts.ServerRestartResult:
        response = self._client.post("/api/v1/server/restart")
        return self._parse_model(response, contracts.ServerRestartResult)

    def server_stop(self) -> contracts.ServerStopResult:
        response = self._client.post("/api/v1/server/stop")
        return self._parse_model(response, contracts.ServerStopResult)

    def create_host(
        self,
        *,
        instance_id: str | None = None,
        workspace_root: str | None = None,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.HostResult:
        payload: dict[str, Any] = {"instance_id": instance_id, **_change_control(dry_run, at)}
        if workspace_root is not None:
            payload["workspace_root"] = workspace_root
        response = self._client.post(
            "/api/v1/runtime/instances",
            json=payload,
        )
        return self._parse_model(response, contracts.HostResult)

    def declare_block(
        self, instance_id: str, stamp: Mapping[str, Any]
    ) -> contracts.BlockDeclareResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/blocks/declare",
            json={"stamp": dict(stamp)},
        )
        return self._parse_model(response, contracts.BlockDeclareResult)

    def depublish_block(
        self,
        instance_id: str,
        source_id: str,
        block_id: str,
        *,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.BlockDepublishResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/blocks/depublish",
            json={"source_id": source_id, "block_id": block_id, **_change_control(dry_run, at)},
        )
        return self._parse_model(response, contracts.BlockDepublishResult)

    def host_workspace_detach(
        self, instance_id: str, *, dry_run: bool | None = None, at: str | None = None
    ) -> contracts.WorkspaceDetachResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/workspace-detach",
            json=_change_control(dry_run, at),
        )
        return self._parse_model(response, contracts.WorkspaceDetachResult)

    def host_workspace_attach(
        self,
        instance_id: str,
        *,
        workspace_root: str,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.HostWorkspaceAttachResult:
        """Attach the host to a Git worktree, initialized or not (local socket only)."""

        response = self._client.post(
            f"/api/v1/{instance_id}/workspace-attach",
            json={"workspace_root": workspace_root, **_change_control(dry_run, at)},
        )
        return self._parse_model(response, contracts.HostWorkspaceAttachResult)

    def host_workspace_registration(self, instance_id: str) -> contracts.HostWorkspaceRegistration:
        response = self._client.get(f"/api/v1/{instance_id}/workspace-registration")
        return self._parse_model(response, contracts.HostWorkspaceRegistration)

    def set_floor_delivery(
        self,
        instance_id: str,
        *,
        enabled: bool,
    ) -> contracts.HostWorkspaceRegistration:
        response = self._client.post(
            f"/api/v1/{instance_id}/workspace/floor-delivery", json={"enabled": enabled}
        )
        return self._parse_model(response, contracts.HostWorkspaceRegistration)

    def deliver_floor_now(
        self,
        instance_id: str,
        *,
        include: tuple[contracts.FloorExportPart, ...] = (),
        at: contracts.AcceptedCoordinate | None = None,
    ) -> contracts.FloorDeliveryResult:
        request = contracts.FloorDeliverNowRequest(include=include, at=at)
        response = self._client.post(
            f"/api/v1/{instance_id}/floor/deliver-now",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.FloorDeliveryResult)

    def show_host(self, instance_id: str) -> contracts.HostInspection:
        response = self._client.get(f"/api/v1/{instance_id}/host")
        return self._parse_model(response, contracts.HostInspection)

    def claim_runtime_bootstrap(
        self,
        instance_id: str,
        bootstrap_secret: str,
        *,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.RuntimeCredentialBootstrapResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/bootstrap/claim",
            json={"bootstrap_secret": bootstrap_secret, **_change_control(dry_run, at)},
        )
        return self._parse_model(response, contracts.RuntimeCredentialBootstrapResult)

    def create_runtime_credential(
        self,
        instance_id: str,
        *,
        principal_id: str,
        permission_mode: contracts.RuntimeCredentialPermissionMode,
        label: str | None = None,
        principal_proof: RuntimeCredentialPrincipalProof | None = None,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.RuntimeCredentialResult:
        """Mint a credential that acts as ``principal_id``.

        The daemon refuses unless the principal is active and this request
        carries its authority: the request already acts as that principal, or
        ``principal_proof`` is the principal's signed consent. ``label`` is a
        description only.
        """

        body: dict[str, object] = {
            "principal_id": principal_id,
            "permission_mode": permission_mode,
        }
        if label is not None:
            body["label"] = label
        if principal_proof is not None:
            body["principal_proof"] = principal_proof.model_dump(mode="json")
        body.update(_change_control(dry_run, at))
        response = self._client.post(f"/api/v1/{instance_id}/runtime/credentials", json=body)
        return self._parse_model(response, contracts.RuntimeCredentialResult)

    def list_runtime_credentials(self, instance_id: str) -> contracts.RuntimeCredentialListResult:
        response = self._client.get(f"/api/v1/{instance_id}/runtime/credentials")
        return self._parse_model(response, contracts.RuntimeCredentialListResult)

    def revoke_runtime_credential(
        self,
        instance_id: str,
        credential_id: str,
        *,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.RuntimeCredentialResult:
        """Revoke a credential. It cannot be undone: it previews unless ``dry_run`` is
        false, and then commits only with ``at`` (the preview's coordinate)."""

        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{credential_id}/revoke",
            json=_change_control(dry_run, at),
        )
        return self._parse_model(response, contracts.RuntimeCredentialResult)

    def rotate_runtime_credential(
        self,
        instance_id: str,
        credential_id: str,
        *,
        principal_proof: RuntimeCredentialPrincipalProof | None = None,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.RuntimeCredentialResult:
        """Replace a credential's token; a bound one needs its principal's authority.

        The old token is revoked, which cannot be undone: it previews unless
        ``dry_run`` is false, and then commits only with ``at``.
        """

        body: dict[str, Any] = _change_control(dry_run, at)
        if principal_proof is not None:
            body["principal_proof"] = principal_proof.model_dump(mode="json")
        response = self._client.post(
            f"/api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate", json=body
        )
        return self._parse_model(response, contracts.RuntimeCredentialResult)

    @staticmethod
    def _coordinate_params(
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None,
    ) -> dict[str, str]:
        if at is None:
            return {}
        value = at.model_dump(mode="json") if isinstance(at, BaseModel) else dict(at)
        return {
            name: str(value[name])
            for name in ("git_oid", "semantic_root", "generation_root", "compiler_digest")
        }

    def init(
        self,
        instance_id: str,
        *,
        principals: Sequence[Mapping[str, Any]],
        operating_profile: Literal["local", "cloud"] = "local",
        require_independent_approval: bool = False,
        workspace_root: str | None = None,
        git_object_format: Literal["sha1", "sha256"] | None = None,
        mirror_url: str | None = None,
    ) -> contracts.InitResult:
        payload: dict[str, Any] = {
            "principals": [dict(item) for item in principals],
            "operating_profile": operating_profile,
            "require_independent_approval": require_independent_approval,
        }
        if workspace_root is not None:
            payload["workspace_root"] = workspace_root
        if git_object_format is not None:
            payload["git_object_format"] = git_object_format
        if mirror_url is not None:
            payload["mirror_url"] = mirror_url
        response = self._client.post(
            f"/api/v1/{instance_id}/init",
            json=payload,
        )
        return self._parse_model(response, contracts.InitResult)

    def store_body(self, instance_id: str, content: bytes) -> contracts.CasObjectResult:
        import base64

        response = self._client.post(
            f"/api/v1/{instance_id}/bodies",
            json={"content_base64": base64.b64encode(content).decode("ascii")},
        )
        return self._parse_model(response, contracts.CasObjectResult)

    def decommission_instance(
        self,
        instance_id: str,
        *,
        reason: str,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.InstanceDecommissionResult:
        """Decommission the instance. It cannot be undone: it previews unless
        ``dry_run`` is false, and then commits only with ``at``."""

        response = self._client.post(
            f"/api/v1/{instance_id}/instance/decommission",
            json={"reason": reason, **_change_control(dry_run, at)},
        )
        return self._parse_model(response, contracts.InstanceDecommissionResult)

    def set_ledger_mirror(
        self, instance_id: str, *, url: str, dry_run: bool | None = None, at: str | None = None
    ) -> contracts.LedgerMirror:
        """Bind a mirror and publish to it. A disclosure cannot be called back: it
        previews unless ``dry_run`` is false, and then commits only with ``at``."""

        response = self._client.post(
            f"/api/v1/{instance_id}/ledger/mirror",
            json={"url": url, **_change_control(dry_run, at)},
        )
        return self._parse_model(response, contracts.LedgerMirror)

    def publish_ledger(
        self,
        instance_id: str,
        *,
        timeout: float = 60.0,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.LedgerMirror:
        if isinstance(timeout, bool) or not 0 <= timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        response = self._client.post(
            f"/api/v1/{instance_id}/ledger/publish",
            json={"timeout": timeout, **_change_control(dry_run, at)},
        )
        return self._parse_model(response, contracts.LedgerMirror)

    def get_ledger_mirror(self, instance_id: str) -> contracts.LedgerMirror:
        response = self._client.get(f"/api/v1/{instance_id}/ledger/mirror")
        return self._parse_model(response, contracts.LedgerMirror)

    def list_provider_packages(self, instance_id: str) -> ProviderCatalog:
        response = self._client.get(f"/api/v1/{instance_id}/providers")
        return self._parse_model(response, ProviderCatalog)

    def install_provider(
        self,
        instance_id: str,
        request: ProviderInstallRequest,
    ) -> ProviderInstallResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/providers/install",
            json=request.model_dump(mode="json"),
            timeout=600,
        )
        return self._parse_model(response, ProviderInstallResult)

    def build_kit(self, instance_id: str, request: KitBuildRequest) -> KitBuildResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/kits/build", json=request.model_dump(mode="json")
        )
        return self._parse_model(response, KitBuildResult)

    def kit_status(self, instance_id: str) -> KitStatus:
        response = self._client.get(f"/api/v1/{instance_id}/kits")
        return self._parse_model(response, KitStatus)

    def add_kit(self, instance_id: str, request: KitAddRequest) -> KitChangeResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/kits", json=request.model_dump(mode="json")
        )
        return self._parse_model(response, KitChangeResult)

    def upgrade_claim_types(
        self, instance_id: str, request: ClaimTypeUpgradeRequest
    ) -> ClaimTypeUpgradeResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/claim-types/upgrade",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, ClaimTypeUpgradeResult)

    def upgrade_evidence_rules(
        self, instance_id: str, request: EvidenceRuleUpgradeRequest
    ) -> EvidenceRuleUpgradeResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/claim-types/evidence-rules/upgrade",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, EvidenceRuleUpgradeResult)

    def remove_kit(self, instance_id: str, request: KitRemoveRequest) -> KitChangeResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/kits/remove", json=request.model_dump(mode="json")
        )
        return self._parse_model(response, KitChangeResult)

    def propose_document(
        self,
        instance_id: str,
        *,
        shell: Mapping[str, Any],
        proposal_name: str,
        source_compilation_digest: str | None = None,
        base: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.ProposalInspection:
        payload: dict[str, Any] = {
            "shell": dict(shell),
            "proposal_name": proposal_name,
            "source_compilation_digest": source_compilation_digest,
            **_change_control(dry_run, at),
        }
        if base is not None:
            payload["base"] = (
                base.model_dump(mode="json") if isinstance(base, BaseModel) else dict(base)
            )
        response = self._client.post(f"/api/v1/{instance_id}/documents/proposals", json=payload)
        return self._parse_model(response, contracts.ProposalInspection)

    def propose_compiler_upgrade(
        self,
        instance_id: str,
        *,
        target: CompilerCoordinate,
        base: AcceptedCoordinate | contracts.AcceptedCoordinate,
        proposal_name: str,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.ProposalInspection:
        response = self._client.post(
            f"/api/v1/{instance_id}/compiler/proposals",
            json={
                "target": target.model_dump(mode="json"),
                "base": base.model_dump(mode="json"),
                "proposal_name": proposal_name,
                **_change_control(dry_run, at),
            },
        )
        return self._parse_model(response, contracts.ProposalInspection)

    def propose_principal_change(
        self,
        instance_id: str,
        *,
        principal: Mapping[str, Any],
        proposal_name: str,
        base: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.ProposalInspection:
        payload: dict[str, Any] = {
            "principal": dict(principal),
            "proposal_name": proposal_name,
            **_change_control(dry_run, at),
        }
        if base is not None:
            payload["base"] = (
                base.model_dump(mode="json") if isinstance(base, BaseModel) else dict(base)
            )
        response = self._client.post(f"/api/v1/{instance_id}/principals/proposals", json=payload)
        return self._parse_model(response, contracts.ProposalInspection)

    def whoami(self, instance_id: str) -> contracts.WhoAmI:
        response = self._client.get(f"/api/v1/{instance_id}/whoami")
        return self._parse_model(response, contracts.WhoAmI)

    def head(
        self,
        instance_id: str,
        *,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | str | None = None,
    ) -> contracts.Head:
        """The accepted head (or ``at``) as a coordinate and its generation; nothing else."""

        params: dict[str, Any] = (
            {"at": at} if isinstance(at, str) else dict(self._coordinate_params(at))
        )
        response = self._client.get(f"/api/v1/{instance_id}/head", params=params)
        return self._parse_model(response, contracts.Head)

    def orient(
        self,
        instance_id: str,
        *,
        kind: str | None = None,
        section: contracts.OrientSection | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | str | None = None,
        evaluation_time: str | None = None,
        surface: contracts.OrientSurface = "sdk",
        caller_tools: Sequence[str] | None = None,
    ) -> contracts.OrientResult:
        """The orient map: kinds, artifacts, you, attention and next calls for ``surface``.

        ``at`` is an accepted coordinate or one accepted generation's Git OID, or a
        unique prefix of it of at least 12 hex characters.
        ``kind`` reads one kind in full; ``section`` pages one artifact family.
        """
        params: dict[str, Any] = (
            {"at": at} if isinstance(at, str) else dict(self._coordinate_params(at))
        )
        params["surface"] = surface
        if caller_tools is not None:
            # An empty value preserves an explicitly empty profile on a GET.
            params["caller_tools"] = list(caller_tools) or [""]
        for name, value in (
            ("kind", kind),
            ("section", section),
            ("limit", limit),
            ("cursor", cursor),
            ("evaluation_time", evaluation_time),
        ):
            if value is not None:
                params[name] = value
        response = self._client.get(f"/api/v1/{instance_id}/orient", params=params)
        return self._parse_model(response, contracts.OrientResult)

    def list_proposals(
        self,
        instance_id: str,
        *,
        status: Literal["open", "settled", "incomplete"] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> contracts.ProposalList:
        """One page of proposals; follow ``next_cursor`` while ``truncated``."""
        params: dict[str, Any] = {} if status is None else {"status": status}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        response = self._client.get(
            f"/api/v1/{instance_id}/proposals",
            params=params,
        )
        return self._parse_model(response, contracts.ProposalList)

    def resolve_proposal_selector(
        self,
        instance_id: str,
        selector: str,
    ) -> contracts.ProposalSelectorResult:
        response = self._client.get(
            f"/api/v1/{instance_id}/proposal-selector",
            params={"selector": selector},
        )
        return self._parse_model(response, contracts.ProposalSelectorResult)

    def readmit_proposal(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.ProposalReadmitResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/proposals/{proposal_id}/readmit",
            json={"tag": "playbill-proposal-readmit-request-v1", **_change_control(dry_run, at)},
        )
        return self._parse_model(response, contracts.ProposalReadmitResult)

    def withdraw_proposal(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        reason: str,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.ProposalWithdrawResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/proposals/{proposal_id}/withdraw",
            json={
                "tag": "playbill-proposal-withdraw-request-v1",
                "reason": reason,
                **_change_control(dry_run, at),
            },
        )
        return self._parse_model(response, contracts.ProposalWithdrawResult)

    def inspect_proposal(self, instance_id: str, proposal_id: str) -> contracts.ProposalInspection:
        response = self._client.get(f"/api/v1/{instance_id}/proposals/{proposal_id}")
        return self._parse_model(response, contracts.ProposalInspection)

    def proposal_status(self, instance_id: str, proposal_id: str) -> contracts.ProposalListEntry:
        """One proposal's list entry at the current accepted coordinate, read by ID."""
        response = self._client.get(f"/api/v1/{instance_id}/proposals/{proposal_id}/status")
        return self._parse_model(response, contracts.ProposalListEntry)

    def inspect_refusal(self, instance_id: str, proposal_id: str) -> contracts.RefusalInspection:
        response = self._client.get(f"/api/v1/{instance_id}/proposals/{proposal_id}/refusal")
        return self._parse_model(response, contracts.RefusalInspection)

    def review_proposal(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        include_body: bool = False,
        workspace_observation: Mapping[str, Any] | None = None,
    ) -> contracts.ProposalReview:
        response = self._client.post(
            f"/api/v1/{instance_id}/proposals/{proposal_id}/review",
            json={
                "include_body": include_body,
                "workspace_observation": (
                    None if workspace_observation is None else dict(workspace_observation)
                ),
            },
        )
        return self._parse_model(response, contracts.ProposalReview)

    def prepare_approval(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        signer_id: str,
        include_body: bool = False,
    ) -> contracts.ApprovalChallenge:
        response = self._client.post(
            f"/api/v1/{instance_id}/proposals/{proposal_id}/approval-challenge",
            json={"signer_id": signer_id, "include_body": include_body},
        )
        return self._parse_model(response, contracts.ApprovalChallenge)

    def submit_approval(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        attestation: Mapping[str, Any],
    ) -> contracts.ApprovalReceipt:
        response = self._client.post(
            f"/api/v1/{instance_id}/proposals/{proposal_id}/approvals",
            json={"attestation": dict(attestation)},
        )
        return self._parse_model(response, contracts.ApprovalReceipt)

    def approve_proposal(
        self,
        instance_id: str,
        proposal_id: str,
        *,
        signer_id: str,
        signer: Callable[[dict[str, Any]], Mapping[str, Any]],
        include_body: bool = False,
    ) -> contracts.ApprovalReceipt:
        challenge = self.prepare_approval(
            instance_id,
            proposal_id,
            signer_id=signer_id,
            include_body=include_body,
        )
        return self.submit_approval(
            instance_id,
            proposal_id,
            attestation=signer(dict(challenge.statement)),
        )

    def activate_proposal(self, instance_id: str, proposal_id: str) -> contracts.ActivationReceipt:
        response = self._client.post(f"/api/v1/{instance_id}/proposals/{proposal_id}/activate")
        return self._parse_model(response, contracts.ActivationReceipt)

    def read_capture(self, instance_id: str, request: CaptureReadRequest) -> CaptureRead:
        response = self._client.post(
            f"/api/v1/{instance_id}/captures/read",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, CaptureRead)

    def source_context(self, instance_id: str) -> contracts.SourceContext:
        response = self._client.get(f"/api/v1/{instance_id}/sources/context")
        return self._parse_model(response, contracts.SourceContext)

    def check_source_bundle(
        self, instance_id: str, *, bundle: Mapping[str, Any]
    ) -> contracts.SourceCheckResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/sources/check",
            json={"bundle": dict(bundle)},
        )
        return self._parse_model(response, contracts.SourceCheckResult)

    def propose_source_bundle(
        self,
        instance_id: str,
        *,
        bundle: Mapping[str, Any],
        source_name: str,
        proposal_name: str,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.ProposalInspection:
        response = self._client.post(
            f"/api/v1/{instance_id}/sources/proposals",
            json={
                "bundle": dict(bundle),
                "source_name": source_name,
                "proposal_name": proposal_name,
                **_change_control(dry_run, at),
            },
        )
        return self._parse_model(response, contracts.ProposalInspection)

    @staticmethod
    def _coordinate_body(
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if at is None:
            return None
        return at.model_dump(mode="json") if isinstance(at, BaseModel) else dict(at)

    def _proposal_payload(
        self,
        *,
        proposal_name: str,
        base: contracts.AcceptedCoordinate | Mapping[str, Any] | None,
        **fields: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"proposal_name": proposal_name, **fields}
        base_payload = self._coordinate_body(base)
        if base_payload is not None:
            payload["base"] = base_payload
        return payload

    def propose_claim_type(
        self,
        instance_id: str,
        *,
        claim_type: Mapping[str, Any],
        proposal_name: str,
        base: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.ProposalInspection:
        response = self._client.post(
            f"/api/v1/{instance_id}/claim-types/proposals",
            json={
                **self._proposal_payload(
                    proposal_name=proposal_name, base=base, claim_type=dict(claim_type)
                ),
                **_change_control(dry_run, at),
            },
        )
        return self._parse_model(response, contracts.ProposalInspection)

    def propose_claim_type_input(
        self,
        instance_id: str,
        *,
        input: Mapping[str, Any],
        proposal_name: str,
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.ClaimTypeInputProposalResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/claim-types/proposals",
            json={
                "tag": "playbill-claim-type-input-propose-request-v1",
                "input": dict(input),
                "proposal_name": proposal_name,
                **_change_control(dry_run, at),
            },
        )
        return self._parse_model(response, contracts.ClaimTypeInputProposalResult)

    def migrate_claim_type(
        self,
        instance_id: str,
        *,
        request: Mapping[str, Any],
    ) -> contracts.ClaimTypeMigrationResponse:
        response = self._client.post(
            f"/api/v1/{instance_id}/claim-types/migrations",
            json=dict(request),
        )
        self._check_error(response)
        return _CLAIM_TYPE_MIGRATION_RESPONSE.validate_python(response.json())

    def append_claim_attestation(
        self,
        instance_id: str,
        *,
        request: ClaimAttestationAppendRequest,
    ) -> ClaimAttestationAppendResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/claim-attestations",
            json=request.model_dump(mode="json"),
        )
        self._check_error(response)
        return ClaimAttestationAppendResult.model_validate(response.json())

    def recover_claim_attestations(self, instance_id: str) -> None:
        response = self._client.post(
            f"/api/v1/{instance_id}/claim-attestations/recover",
        )
        self._check_error(response)

    def resolution_contracts(
        self, instance_id: str, *, request: contracts.ResolutionContractsRequest
    ) -> contracts.ResolutionContractsResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/resolution-contracts/query",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.ResolutionContractsResult)

    def predict(
        self,
        instance_id: str,
        *,
        request: contracts.PredictRequest,
    ) -> contracts.PredictResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/predictions",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.PredictResult)

    def settle_prediction(
        self,
        instance_id: str,
        prediction_id: str,
        *,
        request: contracts.SettleRequest,
    ) -> contracts.SettleResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/predictions/{prediction_id}/settlements",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.SettleResult)

    def create_authoring_intent(
        self,
        instance_id: str,
        *,
        payload: Mapping[str, Any],
        reference_expectations: Sequence[Mapping[str, Any]] | None = None,
        program_stamp: Mapping[str, Any] | None = None,
    ) -> contracts.AuthoringIntentViewRecord:
        request: (
            AuthoringIntentCreateRequestV1
            | AuthoringIntentCreateRequestV2
            | AuthoringIntentCreateRequest
        )
        if reference_expectations is None:
            if program_stamp is not None:
                raise ValueError("program_stamp requires reference_expectations")
            request = AuthoringIntentCreateRequestV1.model_validate({"payload": dict(payload)})
        elif program_stamp is None:
            request = AuthoringIntentCreateRequestV2.model_validate(
                {
                    "payload": dict(payload),
                    "reference_expectations": [dict(item) for item in reference_expectations],
                }
            )
        else:
            request = AuthoringIntentCreateRequest.model_validate(
                {
                    "payload": dict(payload),
                    "reference_expectations": [dict(item) for item in reference_expectations],
                    "program_stamp": dict(program_stamp),
                }
            )
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/intents",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.AuthoringIntentViewRecord)

    def create_authoring_input(
        self,
        instance_id: str,
        *,
        input: Mapping[str, Any],
    ) -> contracts.AuthoringIntentViewRecord:
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/intents",
            json={
                "tag": "playbill-authoring-input-create-request-v1",
                "input": dict(input),
            },
        )
        return self._parse_model(response, contracts.AuthoringIntentViewRecord)

    def get_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.AuthoringIntentViewRecord:
        response = self._client.get(f"/api/v1/{instance_id}/authoring/intents/{intent_id}")
        return self._parse_model(response, contracts.AuthoringIntentViewRecord)

    def resume_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.AuthoringIntentViewRecord:
        response = self._client.get(f"/api/v1/{instance_id}/authoring/intents/{intent_id}/resume")
        return self._parse_model(response, contracts.AuthoringIntentViewRecord)

    def list_pending_authoring_intents(
        self,
        instance_id: str,
    ) -> contracts.AuthoringIntentListRecord:
        response = self._client.get(f"/api/v1/{instance_id}/authoring/intents")
        return self._parse_model(response, contracts.AuthoringIntentListRecord)

    def compile_authoring(
        self,
        instance_id: str,
        *,
        payload: Mapping[str, Any],
        intent_id: str | None = None,
        reference_expectations: Sequence[Mapping[str, Any]] | None = None,
        program_stamp: Mapping[str, Any] | None = None,
    ) -> contracts.AuthoringPreflightResult:
        request: (
            AuthoringIntentCompileRequestV1
            | AuthoringIntentCompileRequestV2
            | AuthoringIntentCompileRequest
        )
        if reference_expectations is None:
            if program_stamp is not None:
                raise ValueError("program_stamp requires reference_expectations")
            request = AuthoringIntentCompileRequestV1.model_validate(
                {"payload": dict(payload), "intent_id": intent_id}
            )
        elif program_stamp is None:
            request = AuthoringIntentCompileRequestV2.model_validate(
                {
                    "payload": dict(payload),
                    "reference_expectations": [dict(item) for item in reference_expectations],
                    "intent_id": intent_id,
                }
            )
        else:
            request = AuthoringIntentCompileRequest.model_validate(
                {
                    "payload": dict(payload),
                    "reference_expectations": [dict(item) for item in reference_expectations],
                    "program_stamp": dict(program_stamp),
                    "intent_id": intent_id,
                }
            )
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/compile",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.AuthoringPreflightResult)

    def submit_authoring(
        self,
        instance_id: str,
        *,
        payload: Mapping[str, Any],
        reference_expectations: Sequence[Mapping[str, Any]],
        program_stamp: Mapping[str, Any],
        intent_id: str | None = None,
    ) -> contracts.AuthoringSubmitResultRecord:
        """Compile and submit in one request; the daemon preflights once, on submit."""
        request = AuthoringIntentCompileRequest.model_validate(
            {
                "payload": dict(payload),
                "reference_expectations": [dict(item) for item in reference_expectations],
                "program_stamp": dict(program_stamp),
                "intent_id": intent_id,
            }
        )
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/submit",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.AuthoringSubmitResultRecord)

    def compile_authoring_input(
        self,
        instance_id: str,
        *,
        input: Mapping[str, Any],
        intent_id: str | None = None,
    ) -> contracts.AuthoringPreflightResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/compile",
            json={
                "tag": "playbill-authoring-input-compile-request-v1",
                "input": dict(input),
                "intent_id": intent_id,
            },
        )
        return self._parse_model(response, contracts.AuthoringPreflightResult)

    def preflight_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.AuthoringPreflightResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/intents/{intent_id}/preflight",
            json={"tag": "playbill-authoring-intent-preflight-request-v1"},
        )
        return self._parse_model(response, contracts.AuthoringPreflightResult)

    def rebase_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.AuthoringIntentViewRecord:
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/intents/{intent_id}/rebase",
            json={"tag": "playbill-authoring-intent-rebase-request-v1"},
        )
        return self._parse_model(response, contracts.AuthoringIntentViewRecord)

    def submit_authoring_intent(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.AuthoringSubmitResultRecord:
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/intents/{intent_id}/submit",
            json={"tag": "playbill-authoring-intent-submit-request-v1"},
        )
        return self._parse_model(response, contracts.AuthoringSubmitResultRecord)

    def authoring_intent_status(
        self,
        instance_id: str,
        intent_id: str,
    ) -> contracts.CandidateStatusRecord:
        response = self._client.get(f"/api/v1/{instance_id}/authoring/intents/{intent_id}/status")
        return self._parse_model(response, contracts.CandidateStatusRecord)

    def abandon_authoring_insertion(
        self,
        instance_id: str,
        intent_id: str,
        *,
        expectation_id: str | None = None,
    ) -> contracts.InsertionAbandonResultRecord:
        response = self._client.post(
            f"/api/v1/{instance_id}/authoring/intents/{intent_id}/insertion/abandon",
            json={
                "tag": "playbill-insertion-abandon-request-v1",
                "expectation_id": expectation_id,
            },
        )
        return self._parse_model(response, contracts.InsertionAbandonResultRecord)

    def read_claim_batch(
        self,
        instance_id: str,
        *,
        request: ClaimReadBatchRequest,
    ) -> ClaimReadBatchResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/claims/read-batch",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, ClaimReadBatchResult)

    def get(
        self,
        instance_id: str,
        *,
        request: GetRequest,
    ) -> GetResult:
        """One governed thing by any reference form, values first; see ``detail``."""
        response = self._client.post(
            f"/api/v1/{instance_id}/get",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return self._parse_model(response, GetResult)

    def get_batch(
        self,
        instance_id: str,
        *,
        request: GetBatchRequest,
    ) -> GetBatchResult:
        """SDK-internal: several references at one coordinate and one detail."""
        response = self._client.post(
            f"/api/v1/{instance_id}/get-batch",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return self._parse_model(response, GetBatchResult)

    def set(self, instance_id: str, *, request: SetRequest) -> WriteOutcome:
        """Put one value in one field of one Subject; a refused write is an outcome."""
        response = self._client.post(
            f"/api/v1/{instance_id}/set",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return self._parse_model(response, WriteOutcome)

    def retire(self, instance_id: str, *, request: RetireRequest) -> WriteOutcome:
        """End one live Claim, named by ID or by its Subject and field."""
        response = self._client.post(
            f"/api/v1/{instance_id}/retire",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return self._parse_model(response, WriteOutcome)

    def write(self, instance_id: str, *, request: WriteRequest) -> WriteOutcome:
        """Apply set, add and retire changes as one change set."""
        response = self._client.post(
            f"/api/v1/{instance_id}/write",
            json=request.model_dump(mode="json", exclude_none=True),
        )
        return self._parse_model(response, WriteOutcome)

    def get_claim_backings(
        self,
        instance_id: str,
        *,
        claim_ids: Sequence[str],
        at: contracts.AcceptedCoordinate | Mapping[str, Any],
    ) -> ClaimBackingsResult:
        request = ClaimBackingsRequest.model_validate(
            {
                "at": self._coordinate_body(at),
                "claim_ids": tuple(claim_ids),
            }
        )
        response = self._client.post(
            f"/api/v1/{instance_id}/claims/backings",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, ClaimBackingsResult)

    def query(
        self,
        instance_id: str,
        *,
        request: contracts.QueryRequest,
    ) -> contracts.QueryResultRecord:
        """One page of a query answer; pass ``next_cursor`` back while ``truncated``."""
        response = self._client.post(
            f"/api/v1/{instance_id}/query",
            # Whole request: a spec's union members carry defaulted discriminators
            # (``kind``) that exclude_defaults would strip.
            json=request.model_dump(mode="json", by_alias=True),
        )
        return self._parse_model(response, contracts.QueryResultRecord)

    def preview_procedure_source(
        self, instance_id: str, *, request: ProcedureSourcePreviewRequest
    ) -> ProcedureSourcePreview:
        from cruxible_client.contracts.procedures.source_requests import ProcedureSourcePreview

        response = self._client.post(
            f"/api/v1/{instance_id}/procedures/source/preview",
            json=request.model_dump(mode="json", by_alias=True),
        )
        return self._parse_model(response, ProcedureSourcePreview)

    def procedure_readiness(
        self,
        instance_id: str,
        name: str,
        *,
        evaluation_time: str,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.ProcedureReadiness:
        params = self._coordinate_params(at)
        params["evaluation_time"] = evaluation_time
        response = self._client.get(
            f"/api/v1/{instance_id}/procedures/{name}/readiness",
            params=params,
        )
        return self._parse_model(response, contracts.ProcedureReadiness)

    def bind_procedure(
        self,
        instance_id: str,
        name: str,
        *,
        bindings: Sequence[Mapping[str, Any]],
    ) -> contracts.ProcedureBindResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/procedures/{name}/bind",
            json={
                "tag": "playbill-procedure-bind-request-v1",
                "bindings": [dict(item) for item in bindings],
            },
        )
        return self._parse_model(response, contracts.ProcedureBindResult)

    def run_procedure(
        self,
        instance_id: str,
        name: str,
        *,
        evaluation_time: str | None,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        input: Any,
        resolution_contract: contracts.ResolutionContractReference | None = None,
        trigger_event: contracts.TriggerEventReference | None = None,
    ) -> contracts.ProcedureRunState:
        response = self._client.post(
            f"/api/v1/{instance_id}/procedures/{name}/runs",
            json={
                **(
                    {"resolution_contract": resolution_contract.model_dump(mode="json")}
                    if resolution_contract is not None
                    else {}
                ),
                **(
                    {"trigger_event": trigger_event.model_dump(mode="json")}
                    if trigger_event is not None
                    else {}
                ),
                "tag": "playbill-procedure-run-request-v2",
                "at": self._coordinate_body(at),
                "evaluation_time": evaluation_time,
                "input": input,
            },
        )
        return self._parse_model(response, contracts.ProcedureRunState)

    def get_procedure_run(
        self,
        instance_id: str,
        run_id: str,
    ) -> contracts.ProcedureRunState:
        response = self._client.get(f"/api/v1/{instance_id}/procedure-runs/{run_id}")
        return self._parse_model(response, contracts.ProcedureRunState)

    def measure_procedure(
        self,
        instance_id: str,
        name: str,
        *,
        request: contracts.ProcedureMeasureRequest,
    ) -> contracts.ProcedureMeasureResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/procedures/{name}/measurements",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.ProcedureMeasureResult)

    def list_procedure_readings(
        self,
        instance_id: str,
        name: str,
        *,
        request: contracts.ProcedureReadingsRequest,
    ) -> contracts.ProcedureReadingsResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/procedures/{name}/readings",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.ProcedureReadingsResult)

    def check_line(
        self, instance_id: str, line: str, *, request: contracts.LineTriggerCheckRequest
    ) -> contracts.LineTriggerCheckResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/lines/{line}/check",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.LineTriggerCheckResult)

    def arm_line(
        self, instance_id: str, line: str, *, dry_run: bool | None = None, at: str | None = None
    ) -> contracts.LineArm:
        response = self._client.post(
            f"/api/v1/{instance_id}/lines/{line}/arm",
            json=_change_control(dry_run, at),
        )
        return self._parse_model(response, contracts.LineArm)

    def disarm_line(
        self, instance_id: str, line: str, *, dry_run: bool | None = None, at: str | None = None
    ) -> contracts.LineArm:
        response = self._client.post(
            f"/api/v1/{instance_id}/lines/{line}/disarm",
            json=_change_control(dry_run, at),
        )
        return self._parse_model(response, contracts.LineArm)

    def line_status(self, instance_id: str, line: str) -> contracts.LineArm:
        response = self._client.get(f"/api/v1/{instance_id}/lines/{line}/arm")
        return self._parse_model(response, contracts.LineArm)

    def evaluate_line(
        self, instance_id: str, line: str, *, request: contracts.LineEvaluateRequest
    ) -> contracts.LineTriggerCheckResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/lines/{line}/evaluate",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.LineTriggerCheckResult)

    def dispatch_line(
        self, instance_id: str, line: str, *, request: contracts.LineDispatchRequest
    ) -> contracts.LineDispatchResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/lines/{line}/dispatch",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.LineDispatchResult)

    def run_line(
        self,
        instance_id: str,
        line: str,
        *,
        occurrence_id: str | None,
        evaluation_time: str | None = None,
        resolution_contract: contracts.ResolutionContractReference | None = None,
        trigger_event: contracts.TriggerEventReference | None = None,
        trigger: str | None = None,
    ) -> contracts.ProcedureRunState:
        response = self._client.post(
            f"/api/v1/{instance_id}/lines/{line}/runs",
            json={
                **({"trigger": trigger} if trigger is not None else {}),
                **(
                    {"resolution_contract": resolution_contract.model_dump(mode="json")}
                    if resolution_contract is not None
                    else {}
                ),
                **(
                    {"trigger_event": trigger_event.model_dump(mode="json")}
                    if trigger_event is not None
                    else {}
                ),
                "tag": "playbill-line-run-request-v1",
                "line": line,
                "occurrence_id": occurrence_id,
                "evaluation_time": evaluation_time,
            },
        )
        return self._parse_model(response, contracts.ProcedureRunState)

    def next(
        self,
        instance_id: str,
        *,
        evaluation_time: str,
        access_profile: Mapping[str, Any],
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        expiring_within: Mapping[str, Any] | None = None,
        workspace_observation: Mapping[str, Any] | None = None,
        since_result_digest: str | None = None,
        at_attestation_head_digest: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        caller_surface: Literal["cli", "mcp", "sdk"] | None = None,
        caller_tools: Sequence[str] | None = None,
    ) -> contracts.NextResult:
        payload: dict[str, Any] = {
            "tag": "playbill-next-request-v2",
            "at": self._coordinate_body(at),
            "evaluation_time": evaluation_time,
            "access_profile": dict(access_profile),
            "workspace_observation": (
                None if workspace_observation is None else dict(workspace_observation)
            ),
        }
        if expiring_within is not None:
            payload["expiring_within"] = dict(expiring_within)
        if since_result_digest is not None:
            payload["since_result_digest"] = since_result_digest
        if at_attestation_head_digest is not None:
            payload["at_attestation_head_digest"] = at_attestation_head_digest
        if limit is not None:
            payload["limit"] = limit
        if cursor is not None:
            payload["cursor"] = cursor
        if caller_surface is not None:
            payload["caller_surface"] = caller_surface
        if caller_tools is not None:
            payload["caller_tools"] = list(caller_tools)
        response = self._client.post(f"/api/v1/{instance_id}/next", json=payload)
        return self._parse_model(response, contracts.NextResult)

    def since(
        self,
        instance_id: str,
        *,
        generation: int,
        access_profile: Mapping[str, Any],
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        max_rows: int = 100,
        max_bytes: int = 65_536,
        cursor: contracts.SinceCursor | Mapping[str, Any] | None = None,
    ) -> contracts.SinceResult:
        try:
            request = contracts.SinceRequest.model_validate(
                {
                    "generation": generation,
                    "at": None
                    if at is None
                    else (
                        at
                        if isinstance(at, Mapping)
                        and not isinstance(at, contracts.AcceptedCoordinate)
                        else self._coordinate_body(at)
                    ),
                    "access_profile": access_profile,
                    "max_rows": max_rows,
                    "max_bytes": max_bytes,
                    "cursor": cursor,
                }
            )
        except ValidationError as exc:
            raise SinceRequestInvalid.from_validation_errors(exc.errors(include_url=False)) from exc
        response = self._client.post(
            f"/api/v1/{instance_id}/since",
            json=request.model_dump(mode="json"),
        )
        return self._parse_model(response, contracts.SinceResult)

    def list_curation(
        self,
        instance_id: str,
        *,
        evaluation_time: str,
        access_profile: Mapping[str, Any],
        workspace_observation: Mapping[str, Any] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> contracts.CurationListResult:
        """One page of the curation queue; follow ``next_cursor`` while ``truncated``."""
        body: dict[str, Any] = {
            "tag": "playbill-curation-list-request-v1",
            "evaluation_time": evaluation_time,
            "access_profile": dict(access_profile),
            "workspace_observation": (
                None if workspace_observation is None else dict(workspace_observation)
            ),
        }
        if limit is not None:
            body["limit"] = limit
        if cursor is not None:
            body["cursor"] = cursor
        response = self._client.post(
            f"/api/v1/{instance_id}/curation/list",
            json=body,
        )
        return self._parse_model(response, contracts.CurationListResult)

    def audit(
        self,
        instance_id: str,
        *,
        evaluation_time: str,
        access_profile: Mapping[str, Any],
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        claim_type_identities: tuple[str, ...] = (),
        subject_kinds: tuple[str, ...] = (),
        max_rows: int = 100,
        max_bytes: int = 65_536,
        cursor: contracts.AuditCursor | Mapping[str, Any] | None = None,
    ) -> contracts.AuditResult:
        payload = {
            "tag": "playbill-audit-request-v1",
            "at": (
                None
                if at is None
                else at.model_dump(mode="json")
                if isinstance(at, contracts.AcceptedCoordinate)
                else dict(at)
            ),
            "evaluation_time": evaluation_time,
            "access_profile": dict(access_profile),
            "scope": {
                "tag": "playbill-audit-scope-v1",
                "claim_type_identities": list(claim_type_identities),
                "subject_kinds": list(subject_kinds),
            },
            "budget": {
                "tag": "playbill-audit-budget-v1",
                "max_rows": max_rows,
                "max_bytes": max_bytes,
            },
            "cursor": (
                None
                if cursor is None
                else cursor.model_dump(mode="json")
                if isinstance(cursor, contracts.AuditCursor)
                else dict(cursor)
            ),
        }
        response = self._client.post(
            f"/api/v1/{instance_id}/audit",
            json={key: value for key, value in payload.items() if value is not None},
        )
        return self._parse_model(response, contracts.AuditResult)

    def overrule_curation(
        self,
        instance_id: str,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        attribution_refs: tuple[str, ...] = (),
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.CurationActionResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/curation/overrule",
            json={
                "tag": "playbill-curation-overrule-request-v1",
                "item_id": item_id,
                "expected_latest_event_digest": expected_latest_event_digest,
                "reason": reason,
                "attribution_refs": list(attribution_refs),
                **_change_control(dry_run, at),
            },
        )
        return self._parse_model(response, contracts.CurationActionResult)

    def accept_fixed_curation(
        self,
        instance_id: str,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        accepted_proposal_id: str,
        accepted_changeset_digest: str,
        attribution_refs: tuple[str, ...] = (),
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.CurationActionResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/curation/accept-fixed",
            json={
                "tag": "playbill-curation-accept-fixed-request-v1",
                "item_id": item_id,
                "expected_latest_event_digest": expected_latest_event_digest,
                "reason": reason,
                "accepted_proposal_id": accepted_proposal_id,
                "accepted_changeset_digest": accepted_changeset_digest,
                "attribution_refs": list(attribution_refs),
                **_change_control(dry_run, at),
            },
        )
        return self._parse_model(response, contracts.CurationActionResult)

    def suppress_curation(
        self,
        instance_id: str,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        scope: Literal["item", "pattern", "instance"],
        until_generation: int | None = None,
        attribution_refs: tuple[str, ...] = (),
        dry_run: bool | None = None,
        at: str | None = None,
    ) -> contracts.CurationActionResult:
        response = self._client.post(
            f"/api/v1/{instance_id}/curation/suppress",
            json={
                "tag": "playbill-curation-suppress-request-v1",
                "item_id": item_id,
                "expected_latest_event_digest": expected_latest_event_digest,
                "reason": reason,
                "scope": scope,
                "until_generation": until_generation,
                "attribution_refs": list(attribution_refs),
                **_change_control(dry_run, at),
            },
        )
        return self._parse_model(response, contracts.CurationActionResult)

    def resolve_coverage(
        self,
        instance_id: str,
        *,
        observations: Sequence[Mapping[str, Any]],
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        budget: Mapping[str, Any] | None = None,
        scan_budget: Mapping[str, Any] | None = None,
    ) -> contracts.CoverageResult:
        """Resolve coverage for a batch of already-observed working sources.

        The caller observes its own working set -- binding each path to a
        declared logical source and hashing the bytes it actually read -- and
        this call carries those observations. Coverage is delivered against
        them; the daemon reads no client filesystem.
        """

        payload: dict[str, Any] = {
            "at": self._coordinate_body(at),
            "observations": [dict(item) for item in observations],
        }
        if budget is not None:
            payload["budget"] = dict(budget)
        if scan_budget is not None:
            payload["scan_budget"] = dict(scan_budget)
        response = self._client.post(
            f"/api/v1/{instance_id}/coverage/resolve",
            json=payload,
        )
        return self._parse_model(response, contracts.CoverageResult)

    def floor_delta(
        self,
        instance_id: str,
        *,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        base_generation: int | None = None,
        base_renderer: str | None = None,
    ) -> FloorDelta:
        """What brings a floor at ``base_generation`` to ``at`` (default: head).

        Pass the generation and renderer of the floor you hold (from its
        ``manifest.json``); apply the answer with ``apply_floor_delta``.
        """

        response = self._client.post(
            f"/api/v1/{instance_id}/floor/delta",
            json={
                "at": self._coordinate_body(at),
                "base_generation": base_generation,
                "base_renderer": base_renderer,
            },
        )
        return self._parse_model(response, FloorDelta)

    def export_floor(
        self,
        instance_id: str,
        *,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        format_version: Literal[2, 5] = 5,
        include: Sequence[contracts.FloorExportPart] = (),
        review_notes_oid: str | None = None,
    ) -> contracts.FloorExport:
        response = self._client.post(
            f"/api/v1/{instance_id}/floor/export",
            json={
                "at": self._coordinate_body(at),
                "format_version": format_version,
                **({"include": sorted(set(include))} if include else {}),
                "review_notes_oid": review_notes_oid,
            },
        )
        return self._parse_model(response, contracts.FloorExport)

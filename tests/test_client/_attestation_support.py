"""Service-backed client seam for real local Claim-attestation signing tests."""

from __future__ import annotations

from pathlib import Path

from cruxible_client import contracts
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequest,
    ClaimAttestationAppendResult,
)
from cruxible_client.contracts.get_reads import PlaybillGetRequest, PlaybillGetResult
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.evidence.claim_attestations import (
    service_append_claim_attestation,
)
from cruxible_core.service.proposals.proposals import service_playbill_whoami
from cruxible_core.storage.cas import BodyAccessContext


class ServiceAttestationClient:
    """Exercise client composition against the real service layer, without a daemon."""

    def __init__(self, instance: PlaybillInstance, *, actor_id: str, state_dir: Path) -> None:
        self.instance = instance
        self.actor_id = actor_id
        self.state_dir = state_dir

    def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
        assert instance_id == self.instance.descriptor.instance_id
        value = service_playbill_whoami(
            self.instance,
            actor_id=self.actor_id,
            credential_label=self.actor_id,
            actor_id_source="runtime_credential",
            authenticated=True,
            permission_mode=PermissionMode.GOVERNED_WRITE,
        )
        return contracts.PlaybillWhoAmI.model_validate(value.model_dump(mode="json"))

    def orient_playbill(
        self,
        instance_id: str,
        *,
        section: contracts.PlaybillOrientSection,
        limit: int,
        cursor: str | None = None,
    ) -> contracts.PlaybillOrientResult:
        assert instance_id == self.instance.descriptor.instance_id
        return service_playbill_orient(
            self.instance, section=section, limit=limit, cursor=cursor, surface="sdk"
        )

    def playbill_get(self, instance_id: str, *, request: PlaybillGetRequest) -> PlaybillGetResult:
        assert instance_id == self.instance.descriptor.instance_id
        return service_playbill_get(
            self.instance,
            request=request,
            access=BodyAccessContext(principal_id=self.actor_id, can_read_body=False),
        )

    def append_playbill_claim_attestation(
        self,
        instance_id: str,
        *,
        request: ClaimAttestationAppendRequest,
    ) -> ClaimAttestationAppendResult:
        assert instance_id == self.instance.descriptor.instance_id
        return service_append_claim_attestation(
            self.instance,
            request=request,
            actor_id=self.actor_id,
        )

    def server_info(self) -> contracts.ServerInfoResult:
        return contracts.ServerInfoResult(
            server_required=False,
            state_root=str(self.state_dir),
            version="0.5.0",
            instance_count=1,
            auth_enabled=False,
            auth_required=False,
            provider_lane=contracts.ProviderLaneStatus(state="available", code=None, detail=None),
        )


__all__ = ["ServiceAttestationClient"]

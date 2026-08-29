"""Service-backed client seam for real local Claim-attestation signing tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cruxible_client import contracts
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationAppendResultV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import service_list_playbill_principals
from cruxible_core.playbill.service.subjects import service_get_playbill_subject
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.service.playbill_claim_attestations import (
    service_append_claim_attestation,
)
from cruxible_core.service.playbill_claims import service_get_playbill_claim
from cruxible_core.service.playbill_proposals import service_playbill_whoami


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
            actor_id_source="runtime_credential_label",
            permission_mode=PermissionMode.GOVERNED_WRITE,
        )
        return contracts.PlaybillWhoAmI.model_validate(value.model_dump(mode="json"))

    def list_playbill_principals(self, instance_id: str) -> contracts.PlaybillPrincipalList:
        assert instance_id == self.instance.descriptor.instance_id
        value = service_list_playbill_principals(self.instance)
        return contracts.PlaybillPrincipalList.model_validate(value.model_dump(mode="json"))

    def get_playbill_claim(
        self,
        instance_id: str,
        claim_id: str,
        *,
        at: AcceptedCoordinate | None,
        evaluation_time: str,
    ) -> contracts.PlaybillClaimViewV2:
        assert instance_id == self.instance.descriptor.instance_id
        value = service_get_playbill_claim(
            self.instance,
            identity=claim_id,
            at=at,
            evaluation_time=datetime.fromisoformat(evaluation_time),
        )
        return contracts.PlaybillClaimViewV2.model_validate(value.model_dump(mode="json"))

    def get_playbill_subject(
        self,
        instance_id: str,
        subject_kind: str,
        subject_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate,
    ) -> contracts.PlaybillSubjectView:
        assert instance_id == self.instance.descriptor.instance_id
        value = service_get_playbill_subject(
            self.instance,
            identity=f"Subject:{subject_kind}/{subject_id}",
            at=at,
        )
        return contracts.PlaybillSubjectView.model_validate(value.model_dump(mode="json"))

    def append_playbill_claim_attestation(
        self,
        instance_id: str,
        *,
        request: ClaimAttestationAppendRequestV1,
    ) -> ClaimAttestationAppendResultV1:
        assert instance_id == self.instance.descriptor.instance_id
        return service_append_claim_attestation(
            self.instance,
            request=request,
            actor_id=self.actor_id,
        )

    def server_info(self) -> contracts.ServerInfoResult:
        return contracts.ServerInfoResult(
            server_required=False,
            state_dir=str(self.state_dir),
            version="0.5.0",
            instance_count=1,
            auth_enabled=False,
            auth_required=False,
        )


__all__ = ["ServiceAttestationClient"]

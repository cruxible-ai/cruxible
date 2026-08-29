"""Canonical objects for the append-only Claim-attestation evidence store."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationV2,
    VerifiedClaimAttestationV2,
)
from cruxible_client.contracts.projection import AcceptedCoordinate

CLAIM_ATTESTATION_EVENT_PAYLOAD_V1_DOMAIN = "playbill-claim-attestation-event-payload-v1"
CLAIM_ATTESTATION_EVENT_V1_DOMAIN = "playbill-claim-attestation-event-v1"
CLAIM_ATTESTATION_PARTITION_V1_DOMAIN = "playbill-claim-attestation-partition-v1"
CLAIM_ATTESTATION_PARTITION_GENESIS_V1_DOMAIN = "playbill-claim-attestation-partition-genesis-v1"
CLAIM_ATTESTATION_PARTITION_HEAD_V1_DOMAIN = "playbill-claim-attestation-partition-head-v1"
CLAIM_ATTESTATION_HEAD_MAP_NODE_V1_DOMAIN = "playbill-claim-attestation-head-map-node-v1"
CLAIM_ATTESTATION_PUBLISHED_ROOT_V1_DOMAIN = "playbill-claim-attestation-published-root-v1"


class _StrictStoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


class ClaimAttestationStoreManifestV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-store-manifest-v1"] = (
        "playbill-claim-attestation-store-manifest-v1"
    )
    instance_id: str = Field(min_length=1, max_length=256)
    initialized_coordinate: AcceptedCoordinate
    initialized_at: datetime

    @field_validator("initialized_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("attestation store time must be timezone-aware")
        return value


class ClaimAttestationEventPayloadV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-event-payload-v1"] = (
        "playbill-claim-attestation-event-payload-v1"
    )
    statement_digest: str
    envelope_digest: str
    verification_account_digest: str
    attestation: ClaimAttestationV2
    verification_account: VerifiedClaimAttestationV2
    note: str | None = None
    recorded_coordinate: AcceptedCoordinate
    current_at_append: bool
    attesting_principal_id: str
    submitted_by: str
    recorded_at: datetime
    payload_digest: str

    _digests = field_validator(
        "statement_digest", "envelope_digest", "verification_account_digest", "payload_digest"
    )(_digest)

    @field_validator("recorded_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("attestation payload time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _reproduces(self) -> "ClaimAttestationEventPayloadV1":
        from cruxible_client.contracts.claim_attestations import (
            claim_attestation_v2_envelope_digest,
            claim_attestation_v2_statement_digest,
            claim_attestation_verification_account_digest,
        )

        if self.statement_digest != claim_attestation_v2_statement_digest(
            self.attestation.statement
        ):
            raise ValueError("attestation payload statement digest differs")
        if self.envelope_digest != claim_attestation_v2_envelope_digest(self.attestation):
            raise ValueError("attestation payload envelope digest differs")
        if self.verification_account_digest != claim_attestation_verification_account_digest(
            self.verification_account
        ):
            raise ValueError("attestation payload verification account digest differs")
        if self.payload_digest != claim_attestation_event_payload_digest(self):
            raise ValueError("attestation event payload digest does not reproduce")
        return self


class ClaimAttestationEventV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-event-v1"] = "playbill-claim-attestation-event-v1"
    instance_id: str
    partition_digest: str
    sequence: int = Field(ge=1)
    previous_event_digest: str
    payload_digest: str
    event_digest: str

    _digests = field_validator(
        "partition_digest", "previous_event_digest", "payload_digest", "event_digest"
    )(_digest)

    @model_validator(mode="after")
    def _reproduces(self) -> "ClaimAttestationEventV1":
        if self.event_digest != claim_attestation_event_digest(self):
            raise ValueError("attestation event digest does not reproduce")
        return self


class ClaimAttestationPartitionGenesisV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-partition-genesis-v1"] = (
        "playbill-claim-attestation-partition-genesis-v1"
    )
    partition_digest: str
    sequence: Literal[0] = 0
    previous_event_digest: None = None
    genesis_digest: str

    _digests = field_validator("partition_digest", "genesis_digest")(_digest)

    @model_validator(mode="after")
    def _reproduces(self) -> "ClaimAttestationPartitionGenesisV1":
        if self.genesis_digest != claim_attestation_partition_genesis_digest(self.partition_digest):
            raise ValueError("attestation partition genesis does not reproduce")
        return self


class ClaimAttestationPartitionHeadV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-partition-head-v1"] = (
        "playbill-claim-attestation-partition-head-v1"
    )
    partition_digest: str
    sequence: int = Field(ge=1)
    event_digest: str
    head_digest: str

    _digests = field_validator("partition_digest", "event_digest", "head_digest")(_digest)

    @model_validator(mode="after")
    def _reproduces(self) -> "ClaimAttestationPartitionHeadV1":
        if self.head_digest != claim_attestation_partition_head_digest(self):
            raise ValueError("attestation partition head does not reproduce")
        return self


class ClaimAttestationHeadMapEntryV1(_StrictStoreModel):
    partition_digest: str
    head: ClaimAttestationPartitionHeadV1

    _partition = field_validator("partition_digest")(_digest)

    @model_validator(mode="after")
    def _matches(self) -> "ClaimAttestationHeadMapEntryV1":
        if self.partition_digest != self.head.partition_digest:
            raise ValueError("attestation head-map entry names a different partition")
        return self


class ClaimAttestationHeadMapNodeV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-head-map-node-v1"] = (
        "playbill-claim-attestation-head-map-node-v1"
    )
    entries: tuple[ClaimAttestationHeadMapEntryV1, ...]
    map_digest: str

    _map_digest = field_validator("map_digest")(_digest)

    @model_validator(mode="after")
    def _shape(self) -> "ClaimAttestationHeadMapNodeV1":
        expected = tuple(
            sorted(self.entries, key=lambda item: item.partition_digest.encode("ascii"))
        )
        if self.entries != expected or len({item.partition_digest for item in self.entries}) != len(
            self.entries
        ):
            raise ValueError("attestation head map must be sorted and unique")
        if self.map_digest != claim_attestation_head_map_node_digest(self):
            raise ValueError("attestation head-map digest does not reproduce")
        return self


class ClaimAttestationPublishedRootV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-published-root-v1"] = (
        "playbill-claim-attestation-published-root-v1"
    )
    instance_id: str
    sequence: int = Field(ge=0)
    previous_published_root_digest: str | None
    event_digest: str | None
    partition_map_digest: str
    root_digest: str

    @field_validator(
        "previous_published_root_digest", "event_digest", "partition_map_digest", "root_digest"
    )
    @classmethod
    def _root_digests(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ClaimAttestationPublishedRootV1":
        genesis = self.sequence == 0
        if genesis != (self.previous_published_root_digest is None and self.event_digest is None):
            raise ValueError("attestation published-root genesis sentinels disagree")
        if not genesis and (
            self.previous_published_root_digest is None or self.event_digest is None
        ):
            raise ValueError("non-genesis attestation root requires predecessor and event")
        if self.root_digest != claim_attestation_published_root_digest(self):
            raise ValueError("attestation published-root digest does not reproduce")
        return self


class ClaimAttestationPublishedPointerV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-published-pointer-v1"] = (
        "playbill-claim-attestation-published-pointer-v1"
    )
    store_version: Literal[1] = 1
    root_digest: str

    _root_digest = field_validator("root_digest")(_digest)


class ClaimAttestationOutstandingMembershipV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-outstanding-membership-v1"] = (
        "playbill-claim-attestation-outstanding-membership-v1"
    )
    claim_identity: ArtifactIdentity
    capture_digest: str
    event_digest: str

    _digests = field_validator("capture_digest", "event_digest")(_digest)


class ClaimAttestationAcceleratorV1(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-accelerator-v1"] = (
        "playbill-claim-attestation-accelerator-v1"
    )
    at_published_root_digest: str
    # (partition_digest, basis, principal_id, event_digest)
    latest_event_by_principal: tuple[tuple[str, str, str, str], ...] = ()
    # (partition_digest, principal_id, statement_digest, event_digest)
    idempotency_entries: tuple[tuple[str, str, str, str], ...] = ()
    outstanding_memberships: tuple[ClaimAttestationOutstandingMembershipV1, ...] = ()

    _root_digest = field_validator("at_published_root_digest")(_digest)

    @model_validator(mode="after")
    def _canonical(self) -> "ClaimAttestationAcceleratorV1":
        latest = tuple(sorted(set(self.latest_event_by_principal)))
        idempotency = tuple(sorted(set(self.idempotency_entries)))
        memberships = tuple(
            sorted(
                set(self.outstanding_memberships),
                key=lambda item: (
                    item.claim_identity.qualified.encode("utf-8"),
                    item.capture_digest.encode("ascii"),
                    item.event_digest.encode("ascii"),
                ),
            )
        )
        if (
            self.latest_event_by_principal != latest
            or self.idempotency_entries != idempotency
            or self.outstanding_memberships != memberships
        ):
            raise ValueError("attestation accelerator entries must be sorted and unique")
        for partition, _basis, _principal, event in latest:
            _digest(partition)
            _digest(event)
        for partition, _principal, statement, event in idempotency:
            _digest(partition)
            _digest(statement)
            _digest(event)
        return self


def claim_attestation_partition_digest(
    *, instance_id: str, claim_identity: ArtifactIdentity, claim_artifact_digest: str
) -> str:
    return typed_digest(
        Sha256Value,
        CLAIM_ATTESTATION_PARTITION_V1_DOMAIN,
        {
            "instance_id": instance_id,
            "claim_identity": claim_identity.model_dump(mode="json"),
            "claim_artifact_digest": claim_artifact_digest,
        },
    ).tagged


def claim_attestation_partition_genesis_digest(partition_digest: str) -> str:
    return typed_digest(
        Sha256Value,
        CLAIM_ATTESTATION_PARTITION_GENESIS_V1_DOMAIN,
        {"partition_digest": partition_digest, "sequence": 0, "previous_event_digest": None},
    ).tagged


def claim_attestation_event_payload_digest(payload: ClaimAttestationEventPayloadV1) -> str:
    value = payload.model_dump(mode="json")
    value.pop("tag")
    value.pop("payload_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_EVENT_PAYLOAD_V1_DOMAIN, value).tagged


def claim_attestation_event_digest(event: ClaimAttestationEventV1) -> str:
    value = event.model_dump(mode="json")
    value.pop("tag")
    value.pop("event_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_EVENT_V1_DOMAIN, value).tagged


def claim_attestation_partition_head_digest(head: ClaimAttestationPartitionHeadV1) -> str:
    value = head.model_dump(mode="json")
    value.pop("tag")
    value.pop("head_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_PARTITION_HEAD_V1_DOMAIN, value).tagged


def claim_attestation_head_map_node_digest(node: ClaimAttestationHeadMapNodeV1) -> str:
    value = node.model_dump(mode="json")
    value.pop("tag")
    value.pop("map_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_HEAD_MAP_NODE_V1_DOMAIN, value).tagged


def claim_attestation_published_root_digest(root: ClaimAttestationPublishedRootV1) -> str:
    value = root.model_dump(mode="json")
    value.pop("tag")
    value.pop("root_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_PUBLISHED_ROOT_V1_DOMAIN, value).tagged


__all__ = [
    name
    for name in globals()
    if name.startswith("ClaimAttestation") or name.startswith("claim_attestation_")
]

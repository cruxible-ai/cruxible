"""Canonical objects for the append-only Claim-attestation evidence store."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    VerifiedClaimAttestation,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc

CLAIM_ATTESTATION_EVENT_PAYLOAD_V1_DOMAIN = "playbill-claim-attestation-event-payload-v1"
CLAIM_ATTESTATION_EVENT_V1_DOMAIN = "playbill-claim-attestation-event-v1"
CLAIM_ATTESTATION_PARTITION_V1_DOMAIN = "playbill-claim-attestation-partition-v1"
CLAIM_ATTESTATION_PARTITION_GENESIS_V1_DOMAIN = "playbill-claim-attestation-partition-genesis-v1"
CLAIM_ATTESTATION_PARTITION_HEAD_V1_DOMAIN = "playbill-claim-attestation-partition-head-v1"
CLAIM_ATTESTATION_HEAD_MAP_NODE_V1_DOMAIN = "playbill-claim-attestation-head-map-node-v1"
CLAIM_ATTESTATION_HEAD_MAP_DELTA_V1_DOMAIN = "playbill-claim-attestation-head-map-delta-v1"
CLAIM_ATTESTATION_PUBLISHED_ROOT_V1_DOMAIN = "playbill-claim-attestation-published-root-v1"


class _StrictStoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


class ClaimAttestationStoreManifest(_StrictStoreModel):
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
        return ensure_utc(value)


class ClaimAttestationEventPayload(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-event-payload-v1"] = (
        "playbill-claim-attestation-event-payload-v1"
    )
    statement_digest: str
    envelope_digest: str
    verification_account_digest: str
    attestation: ClaimAttestation
    verification_account: VerifiedClaimAttestation
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
        return ensure_utc(value)

    @model_validator(mode="after")
    def _reproduces(self) -> "ClaimAttestationEventPayload":
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
        if (
            self.verification_account.statement != self.attestation.statement
            or self.verification_account.statement_digest != self.statement_digest
            or self.verification_account.envelope_digest != self.envelope_digest
            or self.verification_account.attesting_principal_id != self.attesting_principal_id
            or self.verification_account.submitted_by != self.submitted_by
            or self.verification_account.append_coordinate != self.recorded_coordinate
            or self.verification_account.current_at_append != self.current_at_append
            or self.verification_account.recorded_at != self.recorded_at
        ):
            raise ValueError("attestation payload and verification account bindings differ")
        if self.payload_digest != claim_attestation_event_payload_digest(self):
            raise ValueError("attestation event payload digest does not reproduce")
        return self


class ClaimAttestationEvent(_StrictStoreModel):
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
    def _reproduces(self) -> "ClaimAttestationEvent":
        if self.event_digest != claim_attestation_event_digest(self):
            raise ValueError("attestation event digest does not reproduce")
        return self


class ClaimAttestationPartitionGenesis(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-partition-genesis-v1"] = (
        "playbill-claim-attestation-partition-genesis-v1"
    )
    partition_digest: str
    sequence: Literal[0] = 0
    previous_event_digest: None = None
    genesis_digest: str

    _digests = field_validator("partition_digest", "genesis_digest")(_digest)

    @model_validator(mode="after")
    def _reproduces(self) -> "ClaimAttestationPartitionGenesis":
        if self.genesis_digest != claim_attestation_partition_genesis_digest(self.partition_digest):
            raise ValueError("attestation partition genesis does not reproduce")
        return self


class ClaimAttestationPartitionHead(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-partition-head-v1"] = (
        "playbill-claim-attestation-partition-head-v1"
    )
    partition_digest: str
    sequence: int = Field(ge=1)
    event_digest: str
    head_digest: str

    _digests = field_validator("partition_digest", "event_digest", "head_digest")(_digest)

    @model_validator(mode="after")
    def _reproduces(self) -> "ClaimAttestationPartitionHead":
        if self.head_digest != claim_attestation_partition_head_digest(self):
            raise ValueError("attestation partition head does not reproduce")
        return self


class ClaimAttestationHeadMapEntry(_StrictStoreModel):
    partition_digest: str
    head: ClaimAttestationPartitionHead

    _partition = field_validator("partition_digest")(_digest)

    @model_validator(mode="after")
    def _matches(self) -> "ClaimAttestationHeadMapEntry":
        if self.partition_digest != self.head.partition_digest:
            raise ValueError("attestation head-map entry names a different partition")
        return self


class ClaimAttestationHeadMapNode(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-head-map-node-v1"] = (
        "playbill-claim-attestation-head-map-node-v1"
    )
    entries: tuple[ClaimAttestationHeadMapEntry, ...]
    map_digest: str

    _map_digest = field_validator("map_digest")(_digest)

    @model_validator(mode="after")
    def _shape(self) -> "ClaimAttestationHeadMapNode":
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


class ClaimAttestationHeadMapDelta(_StrictStoreModel):
    """One published transition's map: the head it changed, chained to the map before.

    ``map_digest`` commits to ``previous_map_digest`` and the changed head, so it
    commits by induction to every head in the map without restating them: an
    append writes one head, not one per attested Claim. Stores written before
    the delta keep their full ``ClaimAttestationHeadMapNode`` maps; a delta may
    follow either kind.
    """

    tag: Literal["playbill-claim-attestation-head-map-delta-v1"] = (
        "playbill-claim-attestation-head-map-delta-v1"
    )
    previous_map_digest: str
    head: ClaimAttestationPartitionHead
    map_digest: str

    _digests = field_validator("previous_map_digest", "map_digest")(_digest)

    @model_validator(mode="after")
    def _reproduces(self) -> "ClaimAttestationHeadMapDelta":
        if self.map_digest != claim_attestation_head_map_delta_digest(self):
            raise ValueError("attestation head-map delta digest does not reproduce")
        return self


class ClaimAttestationPublishedRoot(_StrictStoreModel):
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
    def _shape(self) -> "ClaimAttestationPublishedRoot":
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


class ClaimAttestationPublishedPointer(_StrictStoreModel):
    tag: Literal["playbill-claim-attestation-published-pointer-v1"] = (
        "playbill-claim-attestation-published-pointer-v1"
    )
    store_version: Literal[1] = 1
    root_digest: str

    _root_digest = field_validator("root_digest")(_digest)


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


def claim_attestation_event_payload_digest(payload: ClaimAttestationEventPayload) -> str:
    value = payload.model_dump(mode="json")
    value.pop("tag")
    value.pop("payload_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_EVENT_PAYLOAD_V1_DOMAIN, value).tagged


def claim_attestation_event_digest(event: ClaimAttestationEvent) -> str:
    value = event.model_dump(mode="json")
    value.pop("tag")
    value.pop("event_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_EVENT_V1_DOMAIN, value).tagged


def claim_attestation_partition_head_digest(head: ClaimAttestationPartitionHead) -> str:
    value = head.model_dump(mode="json")
    value.pop("tag")
    value.pop("head_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_PARTITION_HEAD_V1_DOMAIN, value).tagged


def claim_attestation_head_map_node_digest(node: ClaimAttestationHeadMapNode) -> str:
    value = node.model_dump(mode="json")
    value.pop("tag")
    value.pop("map_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_HEAD_MAP_NODE_V1_DOMAIN, value).tagged


def claim_attestation_head_map_delta_digest(delta: ClaimAttestationHeadMapDelta) -> str:
    value = delta.model_dump(mode="json")
    value.pop("tag")
    value.pop("map_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_HEAD_MAP_DELTA_V1_DOMAIN, value).tagged


def claim_attestation_published_root_digest(root: ClaimAttestationPublishedRoot) -> str:
    value = root.model_dump(mode="json")
    value.pop("tag")
    value.pop("root_digest")
    return typed_digest(Sha256Value, CLAIM_ATTESTATION_PUBLISHED_ROOT_V1_DOMAIN, value).tagged


__all__ = [
    name
    for name in globals()
    if name.startswith("ClaimAttestation") or name.startswith("claim_attestation_")
]

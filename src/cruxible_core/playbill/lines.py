"""Operational Line deployment: runner, backend binding, and writer lease.

A ``LineDeploymentV1`` is operational state, never a governed artifact.  It
binds one accepted LineSpec identity to the runner that executes it and to the
logical journal streams that record it.  Runner and backend identity live here
and in operational audit envelopes only: they never enter a governed artifact,
an occurrence identity, or any digest preimage a Claim can depend on.

A deployment revision may rebind the physical backend, but a rebind is a
verified head handoff that preserves the logical stream identity, the accepted
LineSpec digest, and the occurrence epoch exactly.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.errors import PlaybillError, PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    JournalHeadSignerProtocol,
    JournalHeadVectorV1,
    JournalRangeV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    build_journal_head_manifest,
    verified_journal_handoff,
)
from cruxible_core.playbill.procedures.line_specs import AcceptedLineSpecV1
from cruxible_core.temporal import ensure_utc, format_datetime

_LINE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_LABEL_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PARTITION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class LineRuntimeRefusal(PlaybillError):
    """One typed, dot-namespaced refusal from the operational Line plane."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class _StrictLineRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _tagged_digest(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


def _optional_digest(value: str | None) -> str | None:
    return None if value is None else _tagged_digest(value)


class LineRunnerIdentityV1(_StrictLineRuntimeModel):
    """Nonsecret operational identity of the process that drives a deployment."""

    tag: Literal["playbill-line-runner-identity-v1"] = "playbill-line-runner-identity-v1"
    runner_id: str
    runner_class: Literal["local_scheduler"] = "local_scheduler"

    @field_validator("runner_id")
    @classmethod
    def _runner_id(cls, value: str) -> str:
        if not _LABEL_RE.fullmatch(value):
            raise ValueError("Line runner_id must be a canonical nonsecret label")
        return value


class LineJournalBindingV1(_StrictLineRuntimeModel):
    """Logical stream identity plus the physical backend currently serving it."""

    tag: Literal["playbill-line-journal-binding-v1"] = "playbill-line-journal-binding-v1"
    logical_stream: JournalStreamIdentityV1
    control_partition_id: str
    run_partition_id: str
    backend_id: str
    backend_kind: Literal["local"] = "local"

    @field_validator("control_partition_id", "run_partition_id")
    @classmethod
    def _partition(cls, value: str) -> str:
        if not _PARTITION_RE.fullmatch(value):
            raise ValueError("Line journal partition_id must be a canonical identifier")
        return value

    @field_validator("backend_id")
    @classmethod
    def _backend_id(cls, value: str) -> str:
        if not _LABEL_RE.fullmatch(value):
            raise ValueError("Line backend_id must be a canonical nonsecret label, not a path")
        return value

    @model_validator(mode="after")
    def _partitions_differ(self) -> "LineJournalBindingV1":
        if self.control_partition_id == self.run_partition_id:
            raise ValueError("Line control and run partitions must be distinct")
        return self

    @property
    def logical_identity(self) -> tuple[JournalStreamIdentityV1, str, str]:
        """Return everything a backend rebind must preserve exactly."""

        return (self.logical_stream, self.control_partition_id, self.run_partition_id)


class LineDeploymentV1(_StrictLineRuntimeModel):
    """Operational binding of one accepted LineSpec to a runner and its journals."""

    tag: Literal["playbill-line-deployment-v1"] = "playbill-line-deployment-v1"
    instance_id: str
    deployment_id: str
    revision: int = Field(ge=1)
    line_id: str
    line_spec_digest: str
    occurrence_epoch: int = Field(ge=1)
    runner: LineRunnerIdentityV1
    journal_binding: LineJournalBindingV1
    predecessor_deployment_digest: str | None = None
    handoff_head_vector_digest: str | None = None
    activated_at: datetime

    @field_validator("instance_id", "deployment_id")
    @classmethod
    def _identifier(cls, value: str, info: object) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError(
                f"{getattr(info, 'field_name', 'Line deployment identifier')} is not canonical"
            )
        return value

    @field_validator("line_id")
    @classmethod
    def _line_id(cls, value: str) -> str:
        if not _LINE_ID_RE.fullmatch(value):
            raise ValueError("Line deployment line_id must be canonical")
        return value

    _digests = field_validator(
        "line_spec_digest",
        "predecessor_deployment_digest",
        "handoff_head_vector_digest",
    )(_optional_digest)

    @field_validator("activated_at")
    @classmethod
    def _activated_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("activated_at", when_used="json")
    def _serialize_activated_at(self, value: datetime) -> str | None:
        return format_datetime(value)

    @model_validator(mode="after")
    def _shape(self) -> "LineDeploymentV1":
        if self.journal_binding.logical_stream.instance_id != self.instance_id:
            raise ValueError("Line deployment and its journal stream name different instances")
        if (self.revision == 1) != (self.predecessor_deployment_digest is None):
            raise ValueError("only deployment revision one has no predecessor")
        if self.revision == 1 and self.handoff_head_vector_digest is not None:
            raise ValueError("a genesis deployment cannot claim a backend handoff")
        return self


def line_deployment_digest(deployment: LineDeploymentV1) -> str:
    payload = deployment.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, "playbill-line-deployment-v1", payload).tagged


def line_partition_pairs(
    deployment: LineDeploymentV1,
) -> tuple[tuple[JournalStreamIdentityV1, str], ...]:
    """Return the exact logical partitions one deployment writes, in sorted order."""

    binding = deployment.journal_binding
    return tuple(
        sorted(
            (
                (binding.logical_stream, binding.control_partition_id),
                (binding.logical_stream, binding.run_partition_id),
            ),
            key=lambda pair: pair[1].encode("utf-8"),
        )
    )


def bind_line_deployment(
    accepted: AcceptedLineSpecV1,
    *,
    deployment_id: str,
    runner: LineRunnerIdentityV1,
    journal_binding: LineJournalBindingV1,
    activated_at: datetime,
) -> LineDeploymentV1:
    """Create the genesis operational deployment for one accepted LineSpec."""

    return LineDeploymentV1(
        instance_id=journal_binding.logical_stream.instance_id,
        deployment_id=deployment_id,
        revision=1,
        line_id=accepted.line.identity.name,
        line_spec_digest=accepted.artifact_digest,
        occurrence_epoch=accepted.line.occurrence_epoch,
        runner=runner,
        journal_binding=journal_binding,
        activated_at=activated_at,
    )


def revise_line_deployment(
    previous: LineDeploymentV1,
    *,
    accepted: AcceptedLineSpecV1 | None = None,
    runner: LineRunnerIdentityV1 | None = None,
    journal_binding: LineJournalBindingV1 | None = None,
    handoff_head_vector_digest: str | None = None,
    activated_at: datetime,
) -> LineDeploymentV1:
    """Advance one deployment revision under the rebind and identity-preservation law."""

    binding = journal_binding if journal_binding is not None else previous.journal_binding
    line_spec_digest = previous.line_spec_digest
    occurrence_epoch = previous.occurrence_epoch
    if accepted is not None:
        if accepted.line.identity.name != previous.line_id:
            raise LineRuntimeRefusal(
                "playbill.line.deployment_line_identity_changed",
                "A deployment revision cannot move to a different Line identity.",
            )
        line_spec_digest = accepted.artifact_digest
        occurrence_epoch = accepted.line.occurrence_epoch
    rebind = binding != previous.journal_binding
    if rebind:
        if binding.logical_identity != previous.journal_binding.logical_identity:
            raise LineRuntimeRefusal(
                "playbill.line.deployment_stream_identity_changed",
                "A backend rebind must preserve the exact logical stream and partitions.",
            )
        if (
            line_spec_digest != previous.line_spec_digest
            or occurrence_epoch != previous.occurrence_epoch
        ):
            raise LineRuntimeRefusal(
                "playbill.line.deployment_rebind_changed_spec",
                "A backend rebind cannot change the accepted LineSpec digest or epoch.",
            )
        if handoff_head_vector_digest is None:
            raise LineRuntimeRefusal(
                "playbill.line.deployment_rebind_unverified",
                "A backend rebind requires a verified head-handoff commitment.",
            )
    elif handoff_head_vector_digest is not None:
        raise LineRuntimeRefusal(
            "playbill.line.deployment_rebind_unverified",
            "A revision that rebinds no backend cannot claim a head handoff.",
        )
    return LineDeploymentV1(
        instance_id=previous.instance_id,
        deployment_id=previous.deployment_id,
        revision=previous.revision + 1,
        line_id=previous.line_id,
        line_spec_digest=line_spec_digest,
        occurrence_epoch=occurrence_epoch,
        runner=runner if runner is not None else previous.runner,
        journal_binding=binding,
        predecessor_deployment_digest=line_deployment_digest(previous),
        handoff_head_vector_digest=handoff_head_vector_digest,
        activated_at=activated_at,
    )


def rebind_line_deployment_backend(
    previous: LineDeploymentV1,
    *,
    source: LocalJournalBackend,
    target: LocalJournalBackend,
    backend_id: str,
    source_fencing_token: str,
    target_fencing_token: str,
    signer: JournalHeadSignerProtocol,
    expected_head_public_key: str,
    asserted_at: datetime,
    activated_at: datetime,
    runner: LineRunnerIdentityV1 | None = None,
) -> tuple[LineDeploymentV1, JournalHeadVectorV1]:
    """Move one deployment's journals to another backend by verified head handoff."""

    if backend_id == previous.journal_binding.backend_id:
        raise LineRuntimeRefusal(
            "playbill.line.deployment_rebind_reuses_backend_id",
            "A backend rebind must name a distinct backend label.",
        )
    pairs = line_partition_pairs(previous)
    source_vector = source.read_head_vector(pairs)
    populated = tuple(head for head in source_vector.partitions if head.sequence > 0)
    manifest = build_journal_head_manifest(
        JournalHeadVectorV1(partitions=populated),
        asserted_at=asserted_at,
        signer=signer,
    )
    ranges: tuple[JournalRangeV1, ...] = tuple(
        source.range_from_sequences(
            head.stream,
            head.partition_id,
            first_sequence=1,
            last_sequence=head.sequence,
        )
        for head in populated
    )
    try:
        verified_journal_handoff(
            source,
            target,
            ranges=ranges,
            head_manifest=manifest,
            source_fencing_token=source_fencing_token,
            target_fencing_token=target_fencing_token,
            expected_head_public_key=expected_head_public_key,
        )
    except PlaybillJournalError as exc:
        raise LineRuntimeRefusal(
            "playbill.line.deployment_handoff_failed",
            f"Verified journal handoff refused: {exc}",
        ) from exc
    for stream, partition_id in pairs:
        head = target.read_head(stream, partition_id)
        if head.sequence > 0:
            continue
        if source.writer_state(stream, partition_id) is not None:
            source.fence_writer(
                stream,
                partition_id,
                expected_fencing_token=source_fencing_token,
            )
        target.activate_writer(
            stream,
            partition_id,
            fencing_token=target_fencing_token,
            expected_head=head,
        )
    target_vector = target.read_head_vector(pairs)
    if target_vector != source_vector:
        raise LineRuntimeRefusal(
            "playbill.line.deployment_handoff_incomplete",
            "Handoff target does not reproduce the exact source head vector.",
        )
    revised = revise_line_deployment(
        previous,
        runner=runner,
        journal_binding=previous.journal_binding.model_copy(update={"backend_id": backend_id}),
        handoff_head_vector_digest=target_vector.vector_digest,
        activated_at=activated_at,
    )
    return revised, target_vector


class LineLeaseV1(_StrictLineRuntimeModel):
    """One fenced right to write a deployment's journals; never a governed grant."""

    tag: Literal["playbill-line-lease-v1"] = "playbill-line-lease-v1"
    line_id: str
    deployment_digest: str
    runner: LineRunnerIdentityV1
    fencing_token: str
    generation: int = Field(ge=1)
    acquired_at: datetime

    @field_validator("line_id")
    @classmethod
    def _line_id(cls, value: str) -> str:
        if not _LINE_ID_RE.fullmatch(value):
            raise ValueError("Line lease line_id must be canonical")
        return value

    _deployment = field_validator("deployment_digest")(_tagged_digest)

    @field_validator("fencing_token")
    @classmethod
    def _fencing_token(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("Line lease fencing token must be a canonical identifier")
        return value

    @field_validator("acquired_at")
    @classmethod
    def _acquired_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("acquired_at", when_used="json")
    def _serialize_acquired_at(self, value: datetime) -> str | None:
        return format_datetime(value)


def acquire_line_lease(
    backend: LocalJournalBackend,
    deployment: LineDeploymentV1,
    *,
    fencing_token: str,
    acquired_at: datetime,
) -> LineLeaseV1:
    """Activate the deployment's writer fence on every partition it owns."""

    generation = 0
    for stream, partition_id in line_partition_pairs(deployment):
        try:
            state = backend.activate_writer(
                stream,
                partition_id,
                fencing_token=fencing_token,
                expected_head=backend.read_head(stream, partition_id),
            )
        except PlaybillJournalError as exc:
            raise LineRuntimeRefusal(
                "playbill.line.lease_held",
                f"Another fenced writer already holds this Line partition: {exc}",
            ) from exc
        generation = max(generation, state.generation)
    return LineLeaseV1(
        line_id=deployment.line_id,
        deployment_digest=line_deployment_digest(deployment),
        runner=deployment.runner,
        fencing_token=fencing_token,
        generation=generation,
        acquired_at=acquired_at,
    )


def take_over_line_lease(
    backend: LocalJournalBackend,
    deployment: LineDeploymentV1,
    *,
    previous: LineLeaseV1,
    fencing_token: str,
    acquired_at: datetime,
) -> LineLeaseV1:
    """Fence the prior holder first, then acquire; the old token can never append."""

    if previous.fencing_token == fencing_token:
        raise LineRuntimeRefusal(
            "playbill.line.lease_takeover_token_reused",
            "A lease takeover must present a new fencing token.",
        )
    for stream, partition_id in line_partition_pairs(deployment):
        if backend.writer_state(stream, partition_id) is None:
            continue
        try:
            backend.fence_writer(
                stream,
                partition_id,
                expected_fencing_token=previous.fencing_token,
            )
        except PlaybillJournalError as exc:
            raise LineRuntimeRefusal(
                "playbill.line.lease_takeover_token_mismatch",
                f"Lease takeover does not present the current fencing token: {exc}",
            ) from exc
    return acquire_line_lease(
        backend,
        deployment,
        fencing_token=fencing_token,
        acquired_at=acquired_at,
    )


def verify_line_lease(
    backend: LocalJournalBackend,
    deployment: LineDeploymentV1,
    lease: LineLeaseV1,
) -> None:
    """Refuse a fenced or superseded lease before any write is attempted."""

    if lease.deployment_digest != line_deployment_digest(deployment):
        raise LineRuntimeRefusal(
            "playbill.line.lease_not_current",
            "Lease was granted against a different deployment revision.",
        )
    for stream, partition_id in line_partition_pairs(deployment):
        state = backend.writer_state(stream, partition_id)
        if state is None or not state.active or state.fencing_token != lease.fencing_token:
            raise LineRuntimeRefusal(
                "playbill.line.lease_fenced",
                "This lease no longer holds the active writer fence.",
            )


__all__ = [
    "LineDeploymentV1",
    "LineJournalBindingV1",
    "LineLeaseV1",
    "LineRunnerIdentityV1",
    "LineRuntimeRefusal",
    "acquire_line_lease",
    "bind_line_deployment",
    "line_deployment_digest",
    "line_partition_pairs",
    "rebind_line_deployment_backend",
    "revise_line_deployment",
    "take_over_line_lease",
    "verify_line_lease",
]

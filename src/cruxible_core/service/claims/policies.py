"""Coordinate-pinned inventory of governed Cruxible policies in force."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from cruxible_client import contracts
from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_digest,
    parse_acquisition_policy,
)
from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_IDENTITY,
    approval_policy_digest,
    parse_approval_policy,
)
from cruxible_client.contracts.captures import capture_contract_digest, parse_capture_contract
from cruxible_client.contracts.claim_types import claim_type_digest, parse_claim_type
from cruxible_client.contracts.documents import document_digest, parse_document
from cruxible_client.contracts.procedure_runtime_policy import (
    PROCEDURE_RUNTIME_POLICY_IDENTITY,
    parse_procedure_runtime_policy,
    procedure_runtime_policy_digest,
)
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_artifact_digest,
)
from cruxible_client.contracts.query.definitions import (
    parse_query_definition,
    query_definition_digest,
)
from cruxible_client.contracts.triggers import parse_trigger, trigger_digest
from cruxible_core.compiler.compiler import (
    artifact_codec_for_compiler,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.list_pages import (
    ListCursorMismatch,
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)


def _coordinate(
    instance: PlaybillInstance,
    at: contracts.AcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _row(
    *,
    placement: Literal["embedded", "standalone"],
    policy_kind: contracts.PolicyKind,
    identity: str,
    artifact_kind: str,
    digest: str,
    path: str,
    field_path: str,
    policy: Mapping[str, object],
) -> contracts.PolicyInForce:
    return contracts.PolicyInForce(
        placement=placement,
        policy_kind=policy_kind,
        declaring_artifact_identity=identity,
        declaring_artifact_kind=artifact_kind,
        declaring_artifact_digest=digest,
        path=path,
        field_path=field_path,
        policy=dict(policy),
    )


def _embedded(
    *,
    policy_kind: contracts.PolicyKind,
    identity: str,
    artifact_kind: str,
    digest: str,
    path: str,
    field_path: str,
    value: object,
) -> contracts.PolicyInForce:
    policy = value if isinstance(value, Mapping) else {field_path.rsplit("/", 1)[-1]: value}
    return _row(
        placement="embedded",
        policy_kind=policy_kind,
        identity=identity,
        artifact_kind=artifact_kind,
        digest=digest,
        path=path,
        field_path=field_path,
        policy=policy,
    )


def service_playbill_policies_in_force(
    instance: PlaybillInstance,
    *,
    at: contracts.AcceptedCoordinate | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> contracts.PolicyInForceList:
    """List every live accepted policy carrier at exactly one coordinate.

    ``limit`` bounds the page (``None`` reads them all). A cursor continues its
    first page at that page's coordinate.
    """

    continuation = (
        None if cursor is None else decode_list_cursor(cursor, list_name=_POLICY_LIST, selection={})
    )
    if continuation is not None:
        pinned = contracts.AcceptedCoordinate.model_validate(continuation.coordinate)
        if at is not None and at != pinned:
            raise ListCursorMismatch(
                f"{ListCursorMismatch.error_code}: the cursor continues a different "
                "coordinate; list the policies again without a cursor"
            )
        at = pinned
    coordinate = _coordinate(instance, at)
    artifact_codec = artifact_codec_for_compiler(coordinate.compiler)
    kinds = (
        "approval-policy",
        "procedure-runtime-policy",
        "source-acquisition-policy",
        "claim-type",
        "capture-contract",
        "query-definition",
        "document",
        "procedure",
        "trigger",
    )
    with instance.bind_accepted_projection(coordinate) as projection:
        selected = [
            (row.path, kind, projection.typed.member_bytes(row.path))
            for kind in kinds
            for row in projection.typed.envelopes(kind=kind)
        ]
    rows: list[contracts.PolicyInForce] = []
    for path, kind, content in selected:
        if kind == "approval-policy":
            approval_policy = parse_approval_policy(content, path=path, codec=artifact_codec)
            rows.append(
                _row(
                    placement="standalone",
                    policy_kind="approval_policy",
                    identity=APPROVAL_POLICY_IDENTITY,
                    artifact_kind="ApprovalPolicy",
                    digest=approval_policy_digest(approval_policy).tagged,
                    path=path,
                    field_path="/",
                    policy=approval_policy.model_dump(mode="json"),
                )
            )
        elif kind == "procedure-runtime-policy":
            runtime_policy = parse_procedure_runtime_policy(
                content, path=path, codec=artifact_codec
            )
            rows.append(
                _row(
                    placement="standalone",
                    policy_kind="procedure_runtime_policy",
                    identity=PROCEDURE_RUNTIME_POLICY_IDENTITY,
                    artifact_kind="ProcedureRuntimePolicy",
                    digest=procedure_runtime_policy_digest(runtime_policy).tagged,
                    path=path,
                    field_path="/",
                    policy=runtime_policy.model_dump(mode="json"),
                )
            )
        elif kind == "source-acquisition-policy":
            acquisition_policy = parse_acquisition_policy(content, path=path, codec=artifact_codec)
            if acquisition_policy.lifecycle.state != "live":
                continue
            rows.append(
                _row(
                    placement="standalone",
                    policy_kind="source_acquisition_policy",
                    identity=acquisition_policy.identity.qualified,
                    artifact_kind=acquisition_policy.identity.kind,
                    digest=acquisition_policy_digest(acquisition_policy).tagged,
                    path=path,
                    field_path="/",
                    policy=acquisition_policy.model_dump(mode="json"),
                )
            )
        elif kind == "claim-type":
            claim_type = parse_claim_type(content, path=path, codec=artifact_codec)
            if claim_type.lifecycle.state != "live":
                continue
            digest = claim_type_digest(claim_type).tagged
            values: tuple[tuple[contracts.PolicyKind, str, object | None], ...] = (
                (
                    "claim_evidence_admission_policy",
                    "/evidence_admission_policy",
                    claim_type.evidence_admission_policy.model_dump(mode="json"),
                ),
                (
                    "claim_admission_policy",
                    "/admission_policy",
                    claim_type.admission_policy.model_dump(mode="json"),
                ),
                (
                    "claim_resolution_policy",
                    "/resolution_policy",
                    claim_type.resolution_policy.model_dump(mode="json"),
                ),
                (
                    "claim_evidence_freshness_policy",
                    "/evidence_freshness",
                    None
                    if claim_type.evidence_freshness is None
                    else claim_type.evidence_freshness.model_dump(mode="json"),
                ),
                (
                    "claim_attestation_consequence_policy",
                    "/attestation_consequence_policy",
                    None
                    if claim_type.attestation_consequence_policy is None
                    else claim_type.attestation_consequence_policy.model_dump(mode="json"),
                ),
            )
            rows.extend(
                _embedded(
                    policy_kind=policy_kind,
                    identity=claim_type.identity.qualified,
                    artifact_kind=claim_type.identity.kind,
                    digest=digest,
                    path=path,
                    field_path=field_path,
                    value=value,
                )
                for policy_kind, field_path, value in values
                if value is not None
            )
        elif kind == "capture-contract":
            contract = parse_capture_contract(content, path=path, codec=artifact_codec)
            if contract.lifecycle.state != "live":
                continue
            rows.append(
                _embedded(
                    policy_kind="capture_retention_erasure_policy",
                    identity=contract.identity.qualified,
                    artifact_kind=contract.identity.kind,
                    digest=capture_contract_digest(contract).tagged,
                    path=path,
                    field_path="/retention_erasure_policy",
                    value=contract.retention_erasure_policy.model_dump(mode="json"),
                )
            )
        elif kind == "query-definition":
            query = parse_query_definition(content, path=path, codec=artifact_codec)
            if query.lifecycle.state != "live":
                continue
            rows.append(
                _embedded(
                    policy_kind="query_evaluation_policy",
                    identity=query.identity.qualified,
                    artifact_kind=query.identity.kind,
                    digest=query_definition_digest(query).tagged,
                    path=path,
                    field_path="/evaluation_policy",
                    value=query.evaluation_policy.model_dump(mode="json"),
                )
            )
        elif kind == "document":
            document = parse_document(content, path=path, codec=artifact_codec)
            rows.append(
                _embedded(
                    policy_kind="document_activation_policy",
                    identity=document.identity,
                    artifact_kind="document",
                    digest=document_digest(document).tagged,
                    path=path,
                    field_path="/lifecycle/activation_policy",
                    value=document.lifecycle.activation_policy,
                )
            )
        elif kind == "procedure":
            procedure = parse_procedure(content, path=path, codec=artifact_codec)
            if procedure.lifecycle.state != "live":
                continue
            rows.append(
                _embedded(
                    policy_kind="procedure_activation_policy",
                    identity=procedure.identity.qualified,
                    artifact_kind=procedure.identity.kind,
                    digest=procedure_artifact_digest(procedure).tagged,
                    path=path,
                    field_path="/activation_policy",
                    value=procedure.activation_policy,
                )
            )
        elif kind == "trigger":
            trigger = parse_trigger(content, path=path, codec=artifact_codec)
            if trigger.lifecycle.state != "live":
                continue
            rows.append(
                _embedded(
                    policy_kind="trigger_schedule",
                    identity=trigger.identity.qualified,
                    artifact_kind=trigger.identity.kind,
                    digest=trigger_digest(trigger).tagged,
                    path=path,
                    field_path="/schedule",
                    value=trigger.schedule.model_dump(mode="json"),
                )
            )
    rows.sort(
        key=lambda item: (
            item.declaring_artifact_identity.encode("utf-8"),
            item.field_path.encode("utf-8"),
            item.policy_kind.encode("utf-8"),
        )
    )
    keys = tuple(
        (item.declaring_artifact_identity, item.field_path, item.policy_kind) for item in rows
    )
    snapshot = list_snapshot([list(key) for key in keys])
    page, truncated = page_after_boundary(
        rows,
        keys=keys,
        snapshot=snapshot,
        continuation=continuation,
        limit=len(rows) if limit is None else limit,
        list_name=_POLICY_LIST,
    )
    served = contracts.AcceptedCoordinate(
        git_oid=coordinate.git_oid,
        semantic_root=coordinate.semantic_root,
        generation_root=coordinate.generation_root,
        compiler_digest=coordinate.compiler.rule_digest,
    )
    return contracts.PolicyInForceList(
        coordinate=served,
        policies=list(page),
        truncated=truncated,
        next_cursor=(
            encode_list_cursor(
                list_name=_POLICY_LIST,
                coordinate=served.model_dump(mode="json"),
                selection={},
                snapshot=snapshot,
                last_key=(
                    page[-1].declaring_artifact_identity,
                    page[-1].field_path,
                    page[-1].policy_kind,
                ),
            )
            if truncated and page
            else None
        ),
    )


_POLICY_LIST = "policies-in-force"


__all__ = ["service_playbill_policies_in_force"]

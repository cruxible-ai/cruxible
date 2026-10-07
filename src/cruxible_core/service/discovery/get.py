"""``get``: one governed thing by reference, as values first.

A reference arrives in whatever form an agent saw it -- ``CLM-…``, ``kind/id``,
a predicate, ``Document:<name>``, an artifact path, a proposal id or prefix --
and resolves to exactly one accepted artifact at one coordinate, or refuses
with the candidates it could have meant. ``detail`` then chooses what comes
back: a compact card (``summary``), what backs a Claim (``evidence``), today's
explain output (``why``), revisions (``history``), today's full envelope
(``proof``), or a byte range of a Document body (``body``). Every detail level
reuses the service that already answers it; this module only resolves, shapes,
and names things by identity.
"""

from __future__ import annotations

import base64
import functools
import json
import re
import shlex
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from cruxible_client.contracts import AcceptedCoordinate as ClientCoordinate
from cruxible_client.contracts.acquisition_policies import SourceAcquisitionPolicy
from cruxible_client.contracts.approval_policy import ApprovalPolicy
from cruxible_client.contracts.captures import (
    CaptureContract,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_reads import ClaimValueRecord, ClaimValuesRequest
from cruxible_client.contracts.claim_types import (
    ClaimType,
    effective_evidence_requirement,
    effective_revision_evidence,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ExactContentClaimObject,
    SubjectClaimObject,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.get_display import (
    GET_CLI_HISTORY_VALUE_WIDTH,
    GET_CLI_VALUE_WIDTH,
    get_value_display,
)
from cruxible_client.contracts.get_reads import (
    GET_BODY_DEFAULT_MAX_BYTES,
    GET_DETAILS_BY_KIND,
    GET_HISTORY_DEFAULT_LIMIT,
    ByteRange,
    GetAcquisitionInput,
    GetApprovalPolicyCard,
    GetAttestationEvidence,
    GetBlueprintCard,
    GetBlueprintSlot,
    GetBody,
    GetCaptureContractCard,
    GetCaptureEvidence,
    GetCard,
    GetClaimCard,
    GetClaimTypeCard,
    GetContender,
    GetCoordinate,
    GetDocumentCard,
    GetEvidence,
    GetEvidenceRule,
    GetHistory,
    GetPrincipalCard,
    GetProcedureCard,
    GetProcedureNode,
    GetProcedureRuntimePolicyCard,
    GetProcedureTrackRecord,
    GetProposalCard,
    GetProposalChange,
    GetProposalRefusal,
    GetProviderInterfaceCard,
    GetProviderInterfaceProvider,
    GetQueryCard,
    GetQueryParameter,
    GetRefKind,
    GetRequest,
    GetResult,
    GetRevision,
    GetSourceAcquisitionPolicyCard,
    GetSubjectCard,
    GetSubjectClaim,
    GetTriggerCard,
    GetTruncatedText,
    ReadFlag,
    ReadSurface,
    summary_value,
)
from cruxible_client.contracts.operational_reads import capture_handle
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionRule
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.repairs import RepairOperation, served_repair_for_refusal
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.triggers import Trigger
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.discovery import (
    AcceptedProviderInterface,
    accepted_provider_interfaces,
)
from cruxible_core.service.discovery.exact_content import ExactContentReader
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.discovery.operational import (
    LIVE_CARD_FIELDS,
    capture_card,
    capture_hex,
    captures_with_prefix,
    is_line_digest,
    line_card,
    lines_with_digest,
    live_view,
    mandate_card,
    resolution_contract_card,
    uncited_capture_present,
)
from cruxible_core.service.discovery.operational_viewer import OperationalViewer
from cruxible_core.service.discovery.read_flags import (
    answer_flags,
    ordered_flags,
    unsure_holds,
    verdict_flags,
)
from cruxible_core.service.discovery.runs import (
    procedure_run_card,
    procedure_run_proof,
    run_ids_with_prefix,
)
from cruxible_core.service.list_pages import (
    ListContinuation,
    ListCursorMismatch,
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)
from cruxible_core.service.read_refusals import (
    ReadRefusalError,
    nearest,
    resolve_read_coordinate,
)
from cruxible_core.storage.cas import BodyAccessContext

_CLAIM_ID = re.compile(r"^CLM-[0-9a-f]{32}$")
_CLAIM_PREFIX = re.compile(r"^CLM-[0-9a-f]{4,31}$")
_SUBJECT = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*)/(?P<id>[a-z][a-z0-9_.-]{0,255})$"
)
_SERVICE_ACCESS = BodyAccessContext(principal_id="playbill-service", can_read_body=True)
_MAX_CANDIDATES = 10
_MAX_CHANGES = 25

# Typed reference prefixes an agent sees, and the kind each names.
_TYPED_PREFIXES: Mapping[str, GetRefKind] = {
    "Claim": "claim",
    "Subject": "subject",
    "ClaimType": "claim_type",
    "Document": "document",
    "document": "document",
    "Procedure": "procedure",
    "Blueprint": "blueprint",
    "QueryDefinition": "query",
    "query": "query",
    "CaptureContract": "capture_contract",
    "Trigger": "trigger",
    "Proposal": "proposal",
    "Line": "line",
    "Capture": "capture",
    "ResolutionContract": "resolution_contract",
    "Mandate": "mandate",
    "ProcedureMandate": "mandate",
    "ProcedureRun": "procedure_run",
    "Principal": "principal",
    "ApprovalPolicy": "approval_policy",
    "ProcedureRuntimePolicy": "procedure_runtime_policy",
    "SourceAcquisitionPolicy": "source_acquisition_policy",
    "ProviderInterface": "provider_interface",
}
# The projection's artifact kind for each reference kind, and back.
_PROJECTION_KIND: Mapping[GetRefKind, str] = {
    "claim": "claim",
    "subject": "subject",
    "claim_type": "claim-type",
    "document": "document",
    "procedure": "procedure",
    "blueprint": "blueprint",
    "query": "query-definition",
    "capture_contract": "capture-contract",
    "trigger": "trigger",
    "line": "line",
    "resolution_contract": "resolution-contract",
    "mandate": "procedure-mandate",
    "approval_policy": "approval-policy",
    "procedure_runtime_policy": "procedure-runtime-policy",
    "source_acquisition_policy": "source-acquisition-policy",
    "provider_interface": "provider-interface",
}
_REF_KIND = {value: key for key, value in _PROJECTION_KIND.items()}
_QUALIFIER: Mapping[GetRefKind, str] = {
    "claim": "Claim",
    "subject": "Subject",
    "claim_type": "ClaimType",
    "document": "document",
    "procedure": "Procedure",
    "blueprint": "Blueprint",
    "query": "QueryDefinition",
    "capture_contract": "CaptureContract",
    "trigger": "Trigger",
    "line": "Line",
    "resolution_contract": "ResolutionContract",
    "mandate": "ProcedureMandate",
    "approval_policy": "ApprovalPolicy",
    "procedure_runtime_policy": "ProcedureRuntimePolicy",
    "source_acquisition_policy": "SourceAcquisitionPolicy",
    "provider_interface": "ProviderInterface",
}
_NAMED_KINDS: tuple[GetRefKind, ...] = (
    "document",
    "procedure",
    "blueprint",
    "query",
    "capture_contract",
    "trigger",
    "line",
    "resolution_contract",
    "mandate",
)
_DISPLAY_PREFIX: Mapping[GetRefKind, str] = {
    "claim_type": "ClaimType",
    "document": "Document",
    "procedure": "Procedure",
    "blueprint": "Blueprint",
    "query": "query",
    "capture_contract": "CaptureContract",
    "trigger": "Trigger",
    "proposal": "Proposal",
    "line": "Line",
    "resolution_contract": "ResolutionContract",
    "mandate": "Mandate",
    "capture": "Capture",
    "procedure_run": "ProcedureRun",
    "principal": "Principal",
    "approval_policy": "ApprovalPolicy",
    "procedure_runtime_policy": "ProcedureRuntimePolicy",
    "source_acquisition_policy": "SourceAcquisitionPolicy",
    "provider_interface": "ProviderInterface",
}
# The orient section that lists each operational kind, named by a refusal
# that has no nearer candidate to offer.
_ORIENT_SECTION: Mapping[GetRefKind, str] = {
    "line": "lines",
    "capture": "captures",
    "resolution_contract": "predictions",
    "mandate": "mandates",
    "capture_contract": "capture_contracts",
    "procedure_run": "runs",
    "principal": "principals",
    "approval_policy": "policies",
    "procedure_runtime_policy": "policies",
    "source_acquisition_policy": "policies",
    "provider_interface": "interfaces",
}


@dataclass(frozen=True)
class ResolvedRef:
    """One reference resolved to exactly one accepted (or proposed) thing."""

    kind: GetRefKind
    # The projection identity (``Claim:CLM-…``, ``document:<name>``), or the
    # full proposal id.
    identity: str
    # The canonical reference shown back to the caller.
    display: str
    path: str | None = None


def _name(identity: str) -> str:
    return identity.split(":", 1)[1]


def _display(kind: GetRefKind, identity: str) -> str:
    if kind == "capture":
        return f"Capture:{identity}"
    if kind == "procedure_run":
        return f"ProcedureRun:{identity}"
    name = _name(identity) if kind != "proposal" else identity
    if kind in {"claim", "subject"}:
        return name
    return f"{_DISPLAY_PREFIX[kind]}:{name}"


def _subject_ref(path: str) -> str:
    """``subjects/<kind>/<id>.json`` as the ``kind/id`` shorthand an agent types."""

    if path.startswith("subjects/") and path.endswith(".json"):
        return path[len("subjects/") : -len(".json")]
    return path


def _short_digest(digest: str, *, length: int = 12) -> str:
    algorithm, _, value = digest.partition(":")
    return f"{algorithm}:{value[:length]}" if value else digest[:length]


def _live_predicates(projection: Any) -> frozenset[str]:
    """Every live accepted predicate: the vocabulary field names are shortened against."""

    return frozenset(
        str(identity).removeprefix("ClaimType:")
        for (identity,) in projection.typed.connection.execute(
            "SELECT identity FROM claim_types WHERE lifecycle='live'"
        )
    )


# -- reference resolution ------------------------------------------------------


def _not_found(
    what: str,
    ref: str,
    candidates: Sequence[str],
    *,
    surface: ReadSurface,
    section: str | None = None,
) -> ReadRefusalError:
    if candidates:
        repair = RepairOperation(operation="cruxible.get", arguments={"ref": candidates[0]})
        line = f"Run {_render_get(surface, candidates[0])}"
    elif section is not None:
        repair = RepairOperation(operation="cruxible.orient", arguments={"section": section})
        line = f'Run orient(section="{section}") to list them, then get one of its rows'
    else:
        repair = RepairOperation(operation="cruxible.orient")
        line = "Run orient to see what exists, then get one of its names"
    return ReadRefusalError(
        "cruxible.get.ref_not_found",
        f"no accepted {what} {ref!r}",
        http_status=404,
        candidates=candidates,
        repair=repair,
        repair_line=line,
        context={"ref": ref},
    )


def _ambiguous(ref: str, candidates: Sequence[str]) -> ReadRefusalError:
    return ReadRefusalError(
        "cruxible.get.ref_ambiguous",
        f"{ref!r} names {len(candidates)} artifacts",
        http_status=409,
        candidates=candidates[:_MAX_CANDIDATES],
        repair=RepairOperation(operation="cruxible.get", arguments={"ref": candidates[0]}),
        repair_line="Name one of them exactly",
        context={"ref": ref},
    )


def resolve_get_ref(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    ref: str,
    *,
    surface: ReadSurface = "mcp",
) -> ResolvedRef:
    """Resolve every reference form an agent sees to exactly one thing, or refuse."""

    value = ref.strip()
    if not value:
        raise _not_found("artifact", ref, (), surface=surface)
    if value.startswith("sha256:"):
        # The identity digest next names a due Line by, else a proposal id.
        if is_line_digest(value):
            with instance.bind_accepted_projection(coordinate) as projection:
                lines = lines_with_digest(
                    projection.typed.connection, value, limit=_MAX_CANDIDATES + 1
                )
                if len(lines) > 1:
                    raise _ambiguous(value, [_display("line", item) for item in lines])
                if lines:
                    row = _envelope(projection, lines[0])
                    return ResolvedRef(
                        "line", lines[0], _display("line", lines[0]), row.path if row else None
                    )
        return _resolve_proposal(instance, value)
    if value.startswith("refs/"):
        return _resolve_proposal(instance, value)
    if value.startswith("CAP-"):
        return _resolve_capture(instance, coordinate, value, ref=value, surface=surface)
    if value.startswith("RUN-"):
        return _resolve_run(instance, value, ref=value, surface=surface)
    head, separator, rest = value.partition(":")
    if separator and head in _TYPED_PREFIXES:
        kind = _TYPED_PREFIXES[head]
        if kind == "proposal":
            return _resolve_proposal(instance, rest)
        if kind == "capture":
            return _resolve_capture(instance, coordinate, rest, ref=value, surface=surface)
        if kind == "procedure_run":
            return _resolve_run(instance, rest, ref=value, surface=surface)
        if kind == "principal":
            return _resolve_principal(instance, coordinate, rest, ref=value, surface=surface)
        with instance.bind_accepted_projection(coordinate) as projection:
            return _resolve_typed(projection, kind, rest, ref=value, surface=surface)
    with instance.bind_accepted_projection(coordinate) as projection:
        if value.endswith(".json") and "/" in value:
            rows = projection.typed.envelopes(paths=(value,))
            if len(rows) == 1 and rows[0].kind in _REF_KIND:
                kind = _REF_KIND[rows[0].kind]
                return ResolvedRef(kind, rows[0].identity, _display(kind, rows[0].identity), value)
            raise _not_found("artifact at path", value, (), surface=surface)
        if value.startswith("CLM-"):
            return _resolve_typed(projection, "claim", value, ref=value, surface=surface)
        if "/" in value:
            return _resolve_typed(projection, "subject", value, ref=value, surface=surface)
        return _resolve_bare(projection, value, surface=surface)


def _resolve_proposal(instance: PlaybillInstance, selector: str) -> ResolvedRef:
    """A proposal id or unique prefix, or a target ref, without requiring every record.

    Retained partial evidence is still a proposal: the by-ID status read answers
    it as ``incomplete``, so resolution must not demand the admission first.
    """

    from cruxible_client.contracts.errors import ProposalSelectorAmbiguousError
    from cruxible_core.authoring.id_prefixes import AmbiguousIdPrefix
    from cruxible_core.service.proposals.proposals import (
        service_playbill_proposal_status,
        service_resolve_playbill_proposal_selector,
    )

    if selector.startswith("refs/"):
        resolved = service_resolve_playbill_proposal_selector(instance, selector=selector)
        return ResolvedRef("proposal", resolved.proposal_id, f"Proposal:{resolved.proposal_id}")
    try:
        entry = service_playbill_proposal_status(instance, proposal_id=selector)
    except AmbiguousIdPrefix as exc:
        evidence = instance.proposal_evidence()
        candidates = (
            ()
            if evidence.index is None
            else tuple(
                row["proposal_id"]
                for row in evidence.index.rows(
                    evidence, "proposal_id>=? AND proposal_id<?", (selector, selector + "\uffff")
                )
            )
        )
        raise ProposalSelectorAmbiguousError(selector, candidates) from exc
    return ResolvedRef("proposal", entry.proposal_id, f"Proposal:{entry.proposal_id}")


def _envelope(projection: Any, identity: str) -> Any | None:
    return projection.typed.envelope(identity)


_RUN_PREFIX = re.compile(r"^RUN-[0-9a-f]{12,64}$")


def _resolve_run(
    instance: PlaybillInstance, value: str, *, ref: str, surface: ReadSurface
) -> ResolvedRef:
    """A Procedure run id, or a unique prefix of at least 12 hex after ``RUN-``."""

    if not _RUN_PREFIX.fullmatch(value):
        raise ReadRefusalError(
            "cruxible.get.ref_malformed",
            f"{ref!r} is not a Procedure run id",
            candidates=(),
            repair=RepairOperation(operation="cruxible.orient", arguments={"section": "runs"}),
            repair_line="Pass RUN- plus at least 12 lowercase hex of the run id",
            context={"ref": ref},
        )
    matches = run_ids_with_prefix(instance, value, limit=_MAX_CANDIDATES + 1)
    if len(matches) > 1:
        raise _ambiguous(ref, [_display("procedure_run", item) for item in matches])
    if not matches:
        raise _not_found("Procedure run", ref, (), surface=surface, section="runs")
    return ResolvedRef("procedure_run", matches[0], _display("procedure_run", matches[0]))


def _principals_at(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
) -> dict[str, PrincipalRecord]:
    """The principal registry of the accepted generation at ``coordinate``, by ID."""

    generation = next(
        item for item in instance.accepted_history() if item.oid == coordinate.git_oid
    )
    return {
        item.principal_id: PrincipalRecord.model_validate(item.model_dump(mode="json"))
        for item in generation.principals.principals
    }


def _resolve_principal(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    name: str,
    *,
    ref: str,
    surface: ReadSurface,
) -> ResolvedRef:
    """``Principal:<id>``: one record of the registry at the read coordinate."""

    registry = _principals_at(instance, coordinate)
    if name in registry:
        identity = f"Principal:{name}"
        return ResolvedRef("principal", identity, identity, f"principals/{name}.json")
    raise _not_found(
        "Principal",
        ref,
        tuple(f"Principal:{item}" for item in nearest(name, registry)),
        surface=surface,
        section="principals",
    )


def _resolve_capture(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    value: str,
    *,
    ref: str,
    surface: ReadSurface,
) -> ResolvedRef:
    """``CAP-<12+ hex>``, ``Capture:<digest>`` or a unique digest prefix, to one Capture.

    A full digest names itself: cited, or held in the store uncited. A prefix
    resolves as every verb resolves one (``resolve_capture_handle``): among
    Captures cited at the coordinate and retained ones that verify there, so a
    handle the write verbs accepted opens here too.
    """

    hex_digits = capture_hex(value)
    if hex_digits is None:
        raise ReadRefusalError(
            "cruxible.get.ref_malformed",
            f"{ref!r} is not a Capture reference",
            candidates=(),
            repair=RepairOperation(operation="cruxible.orient", arguments={"section": "captures"}),
            repair_line=(
                "Pass CAP- plus at least 12 lowercase hex of the digest, or Capture:sha256:<64 hex>"
            ),
            context={"ref": ref},
        )
    if len(hex_digits) == 64:
        digest = "sha256:" + hex_digits
        with instance.bind_accepted_projection(coordinate) as projection:
            cited = captures_with_prefix(projection.typed.connection, hex_digits, limit=1)
        if cited or uncited_capture_present(instance, digest):
            return ResolvedRef("capture", digest, _display("capture", digest))
        raise _not_found("Capture", ref, (), surface=surface, section="captures")
    from cruxible_core.service.evidence.capture_reads import (
        CaptureHandleAmbiguous,
        CaptureHandleExhausted,
        CaptureHandleResolved,
        capture_handle_exhausted,
        resolve_capture_handle,
    )

    resolution = resolve_capture_handle(instance, coordinate, hex_digits)
    if isinstance(resolution, CaptureHandleResolved):
        return ResolvedRef("capture", resolution.digest, _display("capture", resolution.digest))
    if isinstance(resolution, CaptureHandleAmbiguous):
        raise _ambiguous(ref, [_display("capture", item) for item in resolution.candidates])
    if isinstance(resolution, CaptureHandleExhausted):
        raise capture_handle_exhausted(ref, field="ref")
    raise _not_found("Capture", ref, (), surface=surface, section="captures")


def _resolve_typed(
    projection: Any,
    kind: GetRefKind,
    name: str,
    *,
    ref: str,
    surface: ReadSurface,
) -> ResolvedRef:
    if kind == "claim":
        return _resolve_claim(projection, name.removeprefix("Claim:"), ref=ref, surface=surface)
    if kind == "claim_type":
        return _resolve_claim_type(projection, name, ref=ref, surface=surface)
    if kind == "subject":
        name = name.removeprefix("Subject:")
    identity = f"{_QUALIFIER[kind]}:{name}"
    row = _envelope(projection, identity)
    if row is not None:
        return ResolvedRef(kind, row.identity, _display(kind, row.identity), row.path)
    names = _names_of(projection, kind)
    if kind == "subject":
        match = _SUBJECT.fullmatch(name)
        if match is not None and not any(item.startswith(match["kind"] + "/") for item in names):
            kinds = nearest(match["kind"], {item.split("/", 1)[0] for item in names})
            raise ReadRefusalError(
                "cruxible.get.ref_not_found",
                f"no accepted Subject has kind {match['kind']!r} ({ref!r})",
                http_status=404,
                candidates=kinds,
                repair=RepairOperation(operation="cruxible.orient"),
                repair_line="Use one of these Subject kinds; orient lists every kind",
                context={"ref": ref},
            )
    what = {
        "subject": "Subject",
        "document": "Document",
        "procedure": "Procedure",
        "blueprint": "Blueprint",
        "query": "QueryDefinition",
        "capture_contract": "CaptureContract",
        "trigger": "Trigger",
        "line": "Line",
        "resolution_contract": "ResolutionContract",
        "mandate": "ProcedureMandate",
        "approval_policy": "ApprovalPolicy",
        "procedure_runtime_policy": "ProcedureRuntimePolicy",
        "source_acquisition_policy": "SourceAcquisitionPolicy",
        "provider_interface": "ProviderInterface",
    }[kind]
    candidates = tuple(
        item if kind == "subject" else f"{_DISPLAY_PREFIX[kind]}:{item}"
        for item in nearest(name, names)
    )
    raise _not_found(what, ref, candidates, surface=surface, section=_ORIENT_SECTION.get(kind))


def _names_of(projection: Any, kind: GetRefKind) -> tuple[str, ...]:
    return tuple(
        _name(row.identity) for row in projection.typed.envelopes(kind=_PROJECTION_KIND[kind])
    )


def _resolve_claim(projection: Any, name: str, *, ref: str, surface: ReadSurface) -> ResolvedRef:
    if _CLAIM_ID.fullmatch(name):
        identity = f"Claim:{name}"
        row = _envelope(projection, identity)
        if row is None:
            raise _not_found("Claim", ref, (), surface=surface)
        return ResolvedRef("claim", identity, name, row.path)
    if not _CLAIM_PREFIX.fullmatch(name):
        raise ReadRefusalError(
            "cruxible.get.ref_malformed",
            f"{ref!r} is not a Claim id or a unique CLM- prefix",
            candidates=(),
            repair=RepairOperation(operation="cruxible.orient"),
            repair_line="Pass CLM- plus 32 lowercase hex, or a unique prefix of at least 4 hex",
            context={"ref": ref},
        )
    lower = f"Claim:{name}"
    rows = projection.typed.connection.execute(
        "SELECT identity FROM claims WHERE identity >= ? AND identity < ? "
        "ORDER BY identity LIMIT ?",
        (lower, lower + "￿", _MAX_CANDIDATES + 1),
    ).fetchall()
    matches = [str(row[0]) for row in rows]
    if not matches:
        raise _not_found("Claim with prefix", ref, (), surface=surface)
    if len(matches) > 1:
        raise _ambiguous(ref, [_name(item) for item in matches])
    row = _envelope(projection, matches[0])
    return ResolvedRef("claim", matches[0], _name(matches[0]), row.path if row else None)


def _resolve_claim_type(
    projection: Any, name: str, *, ref: str, surface: ReadSurface
) -> ResolvedRef:
    row = _envelope(projection, f"ClaimType:{name}")
    if row is not None:
        return ResolvedRef(
            "claim_type", row.identity, _display("claim_type", row.identity), row.path
        )
    predicates = _names_of(projection, "claim_type")
    leaf = [item for item in predicates if item.endswith(f".{name}")]
    if len(leaf) == 1:
        identity = f"ClaimType:{leaf[0]}"
        found = _envelope(projection, identity)
        return ResolvedRef(
            "claim_type", identity, _display("claim_type", identity), found.path if found else None
        )
    if len(leaf) > 1:
        raise _ambiguous(ref, [f"ClaimType:{item}" for item in sorted(leaf)])
    raise _not_found(
        "ClaimType",
        ref,
        tuple(f"ClaimType:{item}" for item in nearest(name, predicates)),
        surface=surface,
    )


def _resolve_bare(projection: Any, value: str, *, surface: ReadSurface) -> ResolvedRef:
    """A bare name: a predicate (full or unique leaf), or another artifact's name."""

    matches: list[ResolvedRef] = []
    predicates = _names_of(projection, "claim_type")
    if value in predicates:
        identity = f"ClaimType:{value}"
        matches.append(ResolvedRef("claim_type", identity, _display("claim_type", identity)))
    else:
        for predicate in predicates:
            if predicate.endswith(f".{value}"):
                identity = f"ClaimType:{predicate}"
                matches.append(
                    ResolvedRef("claim_type", identity, _display("claim_type", identity))
                )
    for typed_kind in _NAMED_KINDS:
        identity = f"{_QUALIFIER[typed_kind]}:{value}"
        if _envelope(projection, identity) is not None:
            matches.append(ResolvedRef(typed_kind, identity, _display(typed_kind, identity)))
    if len(matches) == 1:
        found = matches[0]
        row = _envelope(projection, found.identity)
        return ResolvedRef(found.kind, found.identity, found.display, row.path if row else None)
    if matches:
        raise _ambiguous(value, [item.display for item in matches])
    names = [f"ClaimType:{item}" for item in predicates]
    for typed_kind in _NAMED_KINDS:
        names.extend(
            f"{_DISPLAY_PREFIX[typed_kind]}:{item}" for item in _names_of(projection, typed_kind)
        )
    by_name = {item.split(":", 1)[1]: item for item in names}
    candidates = tuple(by_name[item] for item in nearest(value, by_name))
    raise _not_found("artifact named", value, candidates, surface=surface)


# -- rendering next steps for the caller's surface -----------------------------


def _render_get(
    surface: ReadSurface,
    ref: str,
    detail: str | None = None,
    *,
    window: str | None = None,
    at: ClientCoordinate | None = None,
) -> str:
    """One ``get`` call spelled for the caller's surface (R07)."""

    if surface == "cli":
        rendered = f"cruxible get {shlex.quote(ref)}"
        if detail:
            rendered += f" --detail {detail}"
        if at is not None:
            rendered += f" --at {at.git_oid}"
        return rendered + (f" --range {window}" if window else "")
    arguments = [json.dumps(ref)]
    if detail:
        arguments.append(f"detail={json.dumps(detail)}")
    if window:
        start, _, end = window.partition(":")
        arguments.append(
            f"range=({start}, {end})"
            if surface == "sdk"
            else f'range={{"start": {start}, "end": {end}}}'
        )
    if surface == "sdk":
        context = "cx"
        if at is not None:
            fields = ", ".join(
                f"{key}={json.dumps(value)}" for key, value in at.model_dump().items()
            )
            context += f".at(AcceptedCoordinate({fields}))"
        return f"{context}.get({', '.join(arguments)})"
    if at is not None:
        arguments.append(f"at={json.dumps(at.git_oid)}")
    arguments[0] = f"ref={arguments[0]}"
    return f"cruxible_get({', '.join(arguments)})"


def _render_procedure_run(surface: ReadSurface, name: str, runnable: str) -> tuple[str, ...]:
    """How this Procedure runs, spelled for the caller's surface: itself, or as a Line."""

    if runnable == "direct":
        return (
            {
                "cli": f"cruxible procedure run {shlex.quote(name)} -",
                "mcp": f"cruxible_procedure_run(name={json.dumps(name)}, input={{...}})",
                "sdk": f"cx.accepted_procedure({json.dumps(name)}).run(input=...)",
            }[surface],
        )
    if runnable == "line":
        target = json.dumps("Procedure:" + name)
        return (
            {
                "cli": f"cruxible query Line --where procedure=Procedure:{shlex.quote(name)}",
                "mcp": (
                    'cruxible_query(kind="Line", where=[{"field": "procedure", "eq": '
                    + target
                    + "}])"
                ),
                "sdk": f'cx.query("Line", where=[{{"field": "procedure", "eq": {target}}}])',
            }[surface],
        )
    return ()


def _render_read_capture(surface: ReadSurface, digest: str) -> str:
    """The body-permission read of one Capture's material, spelled for the surface."""

    if surface == "cli":
        return f"cruxible capture read {digest}"
    if surface == "sdk":
        return (
            "client.read_capture(instance_id, "
            f"CaptureReadRequest(capture_digest={json.dumps(digest)}))"
        )
    return f'cruxible_capture_read(request={{"capture_digest": {json.dumps(digest)}}})'


def _value_was_cut(
    value: object,
    *,
    surface: ReadSurface,
    width: int = GET_CLI_VALUE_WIDTH,
) -> bool:
    if surface == "cli":
        return get_value_display(value, width=width).truncated
    if isinstance(value, GetTruncatedText):
        return True
    return isinstance(value, list | tuple) and any(
        _value_was_cut(item, surface=surface, width=width) for item in value
    )


def _render_proposal_step(surface: ReadSurface, step: str, proposal_id: str) -> str:
    if surface == "cli":
        return f"cruxible proposal {step} {proposal_id}"
    if surface == "sdk":
        return f"cx.proposal({json.dumps(proposal_id)}).{step}()"
    tool = {
        "review": "cruxible_proposal_review",
        "readmit": "cruxible_proposal_readmit",
    }[step]
    return f"{tool}(proposal_id={json.dumps(proposal_id)})"


# -- per-kind builders ------------------------------------------------------------


def _claim_value(row: ClaimValueRecord) -> object:
    if row.object_kind == "subject" and isinstance(row.value, str):
        return _subject_ref(row.value)
    return row.value


def _value_key(row: ClaimValueRecord) -> str:
    """What makes two slot values one value; an exact-content value keeps its span."""

    if isinstance(row.object, ExactContentClaimObject):
        return repr(("exact_content", row.object.content_digest, row.object.span))
    return repr(_claim_value(row))


def _artifact_value(claim: ClaimArtifactAny) -> object:
    obj = claim.statement.object
    if isinstance(obj, SubjectClaimObject):
        return _subject_ref(obj.address.artifact_path)
    if isinstance(obj, ExactContentClaimObject):
        return obj.content_digest
    return obj.value


def _shown(
    obj: object, value: Callable[[], object], content: ExactContentReader
) -> tuple[object, str | None]:
    """A value as a card shows it, with an exact-content value's digest beside its text."""

    if isinstance(obj, ExactContentClaimObject):
        return content.of(obj), obj.content_digest
    return value(), None


def _slot_values(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    subject_path: str,
    predicates: tuple[str, ...] = (),
    evaluation_time: datetime,
) -> tuple[ClaimValueRecord, ...]:
    from cruxible_core.service.claims.claim_reads import service_read_claim_values

    public = AcceptedCoordinate.from_internal(coordinate)
    result = service_read_claim_values(
        instance,
        request=ClaimValuesRequest(
            at=ClientCoordinate.model_validate(public.model_dump(mode="json")),
            subject_paths=(subject_path,),
            predicates=predicates,
            evaluation_time=evaluation_time,
        ),
    )
    return result.values


def _generation_timestamp(instance: PlaybillInstance, history: Any, sequence: int) -> str:
    record = history.read_generation_record(sequence, instance.blob_at)
    return str(record.candidate.timestamp)


def _claim_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    surface: ReadSurface,
    content: ExactContentReader,
) -> GetClaimCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        claim = cast(ClaimArtifactAny, projection.typed.source(resolved.identity))
        row = projection.typed.envelope(resolved.identity)
    statement = claim.statement
    subject_path = statement.subject.artifact_path
    subject_kind = _subject_ref(subject_path).split("/", 1)[0]
    slot = tuple(
        item
        for item in _slot_values(
            instance,
            coordinate,
            subject_path=subject_path,
            predicates=(statement.predicate,),
            evaluation_time=evaluation_time,
        )
        if item.qualifier == statement.qualifier
    )
    own = next((item for item in slot if item.claim_id == claim.identity.name), None)
    verdict = own.verdict if own is not None else "retired"
    status = own.status if own is not None else "retired"
    with instance.bind_accepted_projection(coordinate) as projection:
        accepted_predicates = _live_predicates(projection)
        contenders = tuple(
            cast(ClaimArtifactAny, projection.typed.source(f"Claim:{item.claim_id}"))
            for item in slot
            if item.claim_id != claim.identity.name
        )
    held = unsure_holds(
        instance,
        coordinate,
        (claim, *contenders),
        statuses={item.claim_id: item.status for item in slot},
        evaluation_time=evaluation_time,
    )
    with instance.accepted_history_reader(at=AcceptedCoordinate.from_internal(coordinate)) as h:
        latest = h.latest_member(claim_path(claim.identity.name))
        accepted = None if latest is None else _generation_timestamp(instance, h, latest.sequence)
    name = claim.identity.name
    value, content_digest = _shown(statement.object, lambda: _artifact_value(claim), content)
    contender_values = {
        item.claim_id: _shown(item.object, functools.partial(_claim_value, item), content)
        for item in slot
        if item.claim_id != name
    }
    return GetClaimCard(
        claim=name,
        subject=_subject_ref(subject_path),
        predicate=short_field_name(statement.predicate, subject_kind, accepted_predicates),
        predicate_full=statement.predicate,
        qualifier=statement.qualifier,
        value=summary_value(value),
        content_digest=content_digest,
        verdict=verdict,
        status=status,
        revision=int(row.revision) if row is not None else 1,
        accepted=accepted,
        contenders=tuple(
            GetContender(
                claim=item.claim_id,
                value=summary_value(contender_values[item.claim_id][0]),
                content_digest=contender_values[item.claim_id][1],
                verdict=item.verdict,
            )
            for item in slot
            if item.claim_id != name
        ),
        flags=verdict_flags(verdict, status, held=claim.identity.qualified in held),
        next=(
            _render_get(surface, name, "evidence"),
            *(
                _render_get(surface, item, "evidence")
                for item, (value, _digest) in contender_values.items()
                if _value_was_cut(summary_value(value), surface=surface)
            ),
            _render_get(surface, name, "why"),
            _render_get(surface, _subject_ref(subject_path)),
        ),
    )


def _subject_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    surface: ReadSurface,
    content: ExactContentReader,
) -> GetSubjectCard:
    subject = _name(resolved.identity)
    kind = subject.split("/", 1)[0]
    path = resolved.path or f"subjects/{subject}.json"
    rows = _slot_values(instance, coordinate, subject_path=path, evaluation_time=evaluation_time)
    with instance.bind_accepted_projection(coordinate) as projection:
        shell = cast(SubjectShell, projection.typed.source(resolved.identity))
        accepted_predicates = _live_predicates(projection)
        claim_types: dict[str, ClaimType] = {}
        claims: list[ClaimArtifactAny] = []
        for item in rows:
            if item.predicate not in claim_types:
                claim_types[item.predicate] = cast(
                    ClaimType, projection.typed.source(f"ClaimType:{item.predicate}")
                )
            claims.append(cast(ClaimArtifactAny, projection.typed.source(f"Claim:{item.claim_id}")))
        incoming = int(
            projection.typed.connection.execute(
                "SELECT count(*) FROM claims WHERE object_kind='subject' AND object_path=? "
                "AND lifecycle='live'",
                (path,),
            ).fetchone()[0]
        )
    held = unsure_holds(
        instance,
        coordinate,
        claims,
        statuses={item.claim_id: item.status for item in rows},
        evaluation_time=evaluation_time,
    )
    slots: dict[tuple[str, str | None], list[ClaimValueRecord]] = defaultdict(list)
    for item in rows:
        slots[(item.predicate, item.qualifier)].append(item)
    entries: list[GetSubjectClaim] = []
    evidence_steps: list[str] = []
    for (predicate, qualifier), members in sorted(
        slots.items(), key=lambda pair: (pair[0][0], pair[0][1] or "")
    ):
        declared = claim_types.get(predicate)
        many = declared is not None and declared.cardinality == "many"
        # The slot's answer: what resolution selected, or every live contender
        # while it is contested. Overturned and refused contenders are not values.
        shown = [item for item in members if item.status in {"accepted", "conflicted"}] or members
        # Distinctness is judged on the accepted values before any is shown as
        # text: an exact-content value is its digest AND its span, so two spans
        # of one body are two values.
        marks: set[ReadFlag] = set(
            answer_flags("many" if many else "one", len({_value_key(item) for item in shown}))
        )
        pairs = [
            _shown(item.object, functools.partial(_claim_value, item), content) for item in shown
        ]
        for item, (value, _digest) in zip(shown, pairs, strict=True):
            if _value_was_cut(summary_value(value), surface=surface):
                evidence_steps.append(_render_get(surface, item.claim_id, "evidence"))
        values = [value for value, _digest in pairs]
        digests = tuple(digest for _value, digest in pairs if digest is not None)
        for item in shown:
            marks.update(
                verdict_flags(item.verdict, item.status, held=f"Claim:{item.claim_id}" in held)
            )
        listed = many or len(values) > 1
        claims_shown = tuple(item.claim_id for item in shown)
        displayed_value = summary_value(values if listed else values[0])
        if listed and surface == "cli" and _value_was_cut(displayed_value, surface=surface):
            evidence_steps.extend(_render_get(surface, item, "evidence") for item in claims_shown)
        entries.append(
            GetSubjectClaim(
                predicate=short_field_name(predicate, kind, accepted_predicates),
                qualifier=qualifier,
                claim=claims_shown if listed else claims_shown[0],
                value=displayed_value,
                content_digest=(digests if listed else digests[0]) if digests else None,
                flags=tuple(ordered_flags(marks)),
            )
        )
    return GetSubjectCard(
        subject=subject,
        kind=kind,
        lifecycle=shell.lifecycle.state,
        claims=tuple(entries),
        incoming_count=incoming,
        next=(
            *dict.fromkeys(evidence_steps),
            _render_get(surface, subject, "why"),
            _render_get(surface, subject, "history"),
        ),
    )


def _object_description(claim_type: ClaimType) -> tuple[str, tuple[Any, ...] | None]:
    if claim_type.object_kind == "subject":
        kinds = ",".join(claim_type.allowed_object_subject_kinds)
        return (f"subject:{kinds}" if kinds else "subject"), None
    if claim_type.object_kind == "exact_content":
        return "exact_content", None
    schema = claim_type.literal_schema or {}
    members = schema.get("enum")
    kind = schema.get("type")
    return (
        str(kind) if isinstance(kind, str) else "literal",
        tuple(members) if isinstance(members, list) else None,
    )


def _claim_type_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetClaimTypeCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        claim_type = cast(ClaimType, projection.typed.source(resolved.identity))
        live = int(
            projection.typed.connection.execute(
                "SELECT count(*) FROM claims WHERE predicate=? AND lifecycle='live'",
                (claim_type.predicate,),
            ).fetchone()[0]
        )
    names = CaptureContractNames(instance, coordinate)
    evidence = names.admitted(claim_type, qualified=True)
    rules = tuple(
        GetEvidenceRule(
            rule_id=rule.rule_id,
            roles=rule.claim_roles,
            contracts=(
                tuple(item.target.qualified for item in rule.capture_contracts)
                if isinstance(rule, ClaimEvidenceAdmissionRule)
                else tuple(
                    names.name(digest, qualified=True) for digest in rule.capture_contract_digests
                )
            ),
            admission=rule.admission,
        )
        for rule in claim_type.evidence_admission_policy.rules
    )
    object_type, members = _object_description(claim_type)
    next_steps = [_render_get(surface, resolved.display, "proof")]
    next_steps.extend(
        _render_get(surface, name) for name in evidence[:1] if not name.startswith("unresolved:")
    )
    return GetClaimTypeCard(
        predicate=claim_type.predicate,
        subject_kinds=claim_type.allowed_subject_kinds,
        object=object_type,
        cardinality=claim_type.cardinality,
        members=members,
        description=claim_type.description,
        member_descriptions=claim_type.member_descriptions,
        roles=claim_type.permitted_roles,
        default_role=claim_type.default_role,
        evidence_requirement=effective_evidence_requirement(claim_type),
        revision_evidence=effective_revision_evidence(claim_type),
        evidence=evidence,
        evidence_rules=rules,
        live_claims=live,
        next=tuple(next_steps),
    )


def _document_size(instance: PlaybillInstance, body_digest: str) -> int | None:
    metadata = instance.body_store().metadata(body_digest, access=_SERVICE_ACCESS)
    return metadata.byte_length


def _document_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetDocumentCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        shell = cast(DocumentShell, projection.typed.source(resolved.identity))
    return GetDocumentCard(
        document=_name(resolved.identity),
        title=shell.title,
        document_kind=shell.document_kind,
        media_type=shell.media_type,
        size=_document_size(instance, shell.body_digest),
        revision=shell.lifecycle.revision,
        next=(
            _render_get(surface, resolved.display, "body"),
            _render_get(surface, resolved.display, "why"),
            _render_get(surface, resolved.display, "history"),
        ),
    )


def _principal_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetPrincipalCard:
    record = _principals_at(instance, coordinate)[_name(resolved.identity)]
    return GetPrincipalCard(
        principal=record.principal_id,
        kind=record.kind,
        status=record.status,
        algorithm=record.algorithm,
        public_key=record.public_key,
        next=(_render_get(surface, resolved.display, "proof"),),
    )


def _approval_policy_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetApprovalPolicyCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        policy = cast(ApprovalPolicy, projection.typed.source(resolved.identity))
    return GetApprovalPolicyCard(
        policy=resolved.display,
        mode=policy.mode,
        next=(
            _render_get(surface, resolved.display, "history"),
            _render_get(surface, resolved.display, "proof"),
        ),
    )


def _runtime_policy_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetProcedureRuntimePolicyCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        policy = cast(ProcedureRuntimePolicy, projection.typed.source(resolved.identity))
    return GetProcedureRuntimePolicyCard(
        policy=resolved.display,
        provider_output_bytes_cap=policy.provider_output_bytes_cap,
        result_bytes_cap=policy.result_bytes_cap,
        repeat_attempts_cap=policy.repeat_attempts_cap,
        next=(
            _render_get(surface, resolved.display, "history"),
            _render_get(surface, resolved.display, "proof"),
        ),
    )


def _duration_text(microseconds: int) -> str:
    return f"{microseconds / 1_000_000:g}s"


def _acquisition_policy_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetSourceAcquisitionPolicyCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        policy = cast(SourceAcquisitionPolicy, projection.typed.source(resolved.identity))
    return GetSourceAcquisitionPolicyCard(
        policy=resolved.display,
        lifecycle=policy.lifecycle.state,
        coherence=policy.coherence.kind,
        inputs=tuple(
            GetAcquisitionInput(
                input=rule.input_name,
                requirement=rule.requirement,
                max_age=None if rule.max_age is None else _duration_text(rule.max_age.microseconds),
            )
            for rule in policy.inputs
        ),
        next=(
            _render_get(surface, resolved.display, "history"),
            _render_get(surface, resolved.display, "proof"),
        ),
    )


def _live_interface(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    installed_classifier_digests: frozenset[str] = frozenset(),
) -> AcceptedProviderInterface:
    """The live accepted inventory entry of one provider interface, or a coded refusal."""

    for item in accepted_provider_interfaces(
        instance, coordinate, installed_classifier_digests=installed_classifier_digests
    ):
        if item.entry.identity == resolved.identity:
            return item
    raise ReadRefusalError(
        "cruxible.get.ref_not_found",
        f"{resolved.display} is retired; no live provider interface carries that name",
        http_status=404,
        candidates=(),
        repair=RepairOperation(operation="cruxible.orient", arguments={"section": "interfaces"}),
        repair_line='Run orient(section="interfaces") to list the live interfaces',
        context={"ref": resolved.display},
    )


def _provider_interface_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetProviderInterfaceCard:
    from cruxible_core.service.discovery.orient import interface_row

    row = interface_row(_live_interface(instance, coordinate, resolved))
    return GetProviderInterfaceCard(
        interface=resolved.display,
        description=row.description,
        input=row.input,
        output=row.output,
        effect=row.effect,
        providers=tuple(
            GetProviderInterfaceProvider(
                provider=item.provider, implementation_digest=item.implementation_digest
            )
            for item in row.providers
        ),
        interface_digest=row.interface_digest,
        next=(_render_get(surface, resolved.display, "proof"),),
    )


def _pin_name(binding: Any) -> str:
    target = getattr(binding, "target", None)
    if target is not None:
        return str(target.qualified)
    return f"slot:{getattr(binding, 'slot_name', '?')}"


def _procedure_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    surface: ReadSurface,
) -> GetProcedureCard:
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureReadinessRequestV1,
        service_playbill_procedure_readiness,
    )

    readiness = service_playbill_procedure_readiness(
        instance,
        name=_name(resolved.identity),
        request=ProcedureReadinessRequestV1(at=AcceptedCoordinate.from_internal(coordinate)),
    )
    definition = readiness.artifact.definition
    inputs: dict[str, Any] = {"input": _pin_name(definition.contract_in)}
    if definition.parameter_contract is not None:
        inputs["parameters"] = _pin_name(definition.parameter_contract)
    with instance.bind_accepted_projection(coordinate) as projection:
        promoted = projection.typed.facts(
            "cruxible.procedure.track_record", identity=resolved.identity
        )
    return GetProcedureCard(
        procedure=_name(resolved.identity),
        description=definition.description,
        inputs=inputs,
        runnable=readiness.runnable,
        unsupported_nodes=tuple(
            GetProcedureNode(node_id=item.node_id, kind=item.kind, runs_on=item.runs_on)
            for item in readiness.unsupported_nodes
        ),
        track_record=tuple(
            _track_record_entry(fact)
            for fact in sorted(promoted, key=lambda item: item.fact_key.encode("utf-8"))
        ),
        next=(
            *_render_procedure_run(surface, _name(resolved.identity), readiness.runnable),
            _render_get(surface, resolved.display, "proof"),
        ),
    )


def _track_record_entry(fact: ProjectionFact) -> GetProcedureTrackRecord:
    """One ``cruxible.procedure.track_record`` fact, as the Procedure card shows it."""

    value = cast(Mapping[str, Any], fact.value)

    def digest(name: str) -> str:
        tagged = value[name]
        return str(tagged["$digest"] if isinstance(tagged, Mapping) else tagged)

    return GetProcedureTrackRecord(
        promotion=fact.fact_key,
        first_sequence=value["first_sequence"],
        last_sequence=value["last_sequence"],
        output=value["output"],
        output_digest=digest("output_digest"),
        promotion_digest=digest("promotion_digest"),
    )


def _query_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetQueryCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        query = cast(QueryDefinition, projection.typed.source(resolved.identity))
    return GetQueryCard(
        query=query.identity.name,
        description=query.description,
        params=tuple(
            GetQueryParameter(name=item.name, type=str(item.value_type), required=item.required)
            for item in query.parameters
        ),
        next=(_render_get(surface, resolved.display, "proof"),),
    )


def _blueprint_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetBlueprintCard:
    from cruxible_client.contracts.procedures.artifacts import BlueprintArtifact
    from cruxible_client.contracts.procedures.blueprints import (
        blueprint_slot_interface,
        fitting_implementation,
    )
    from cruxible_client.contracts.providers import AcceptedProvider

    with instance.bind_accepted_projection(coordinate) as projection:
        blueprint = cast(BlueprintArtifact, projection.typed.source(resolved.identity))
        providers = []
        for row in projection.typed.envelopes(kind="provider"):
            provider = projection.typed.source(row.identity)
            if provider is not None and provider.lifecycle.state == "live":
                providers.append(
                    AcceptedProvider(
                        path=row.path, provider=provider, artifact_digest=row.artifact_digest
                    )
                )
    definition = blueprint.definition
    inputs: dict[str, Any] = {"input": _pin_name(definition.contract_in)}
    if definition.parameter_contract is not None:
        inputs["parameters"] = _pin_name(definition.parameter_contract)
    slots = []
    for slot in definition.pin_slots:
        interface = blueprint_slot_interface(blueprint, slot)
        slots.append(
            GetBlueprintSlot(
                slot=slot.slot_name,
                interface="" if interface is None else interface.target.qualified,
                fits=tuple(
                    sorted(
                        item.provider.identity.name
                        for item in providers
                        if fitting_implementation(item, slot) is not None
                    )
                ),
            )
        )
    name = _name(resolved.identity)
    example = {
        "cli": "cruxible authoring example blueprint-instance",
        "mcp": 'cruxible_authoring_example(name="blueprint-instance")',
        "sdk": "cx.procedure(definition=source.bind(...))",
    }[surface]
    return GetBlueprintCard(
        blueprint=name,
        description=definition.description,
        lifecycle=blueprint.lifecycle.state,
        inputs=inputs,
        slots=tuple(slots),
        next=(example, _render_get(surface, resolved.display, "proof")),
    )


def _trigger_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetTriggerCard:
    with instance.bind_accepted_projection(coordinate) as projection:
        trigger = cast(Trigger, projection.typed.source(resolved.identity))
    target = trigger.line.qualified if trigger.line is not None else str(trigger.action)
    return GetTriggerCard(
        trigger=resolved.identity,
        lifecycle=trigger.lifecycle.state,
        schedule=trigger.schedule.model_dump(mode="json"),
        target=target,
        next=(_render_get(surface, resolved.display, "history"),),
    )


def _capture_contract_card(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetCaptureContractCard:
    names = CaptureContractNames(instance, coordinate)
    with instance.bind_accepted_projection(coordinate) as projection:
        contract = cast(CaptureContract, projection.typed.source(resolved.identity))
        row = projection.typed.envelope(resolved.identity)
        claim_types = [
            cast(ClaimType, projection.typed.source(item.identity))
            for item in projection.typed.envelopes(kind="claim-type")
        ]
    admitted_by = tuple(
        sorted(
            item.predicate
            for item in claim_types
            if item.lifecycle.state == "live"
            and resolved.identity in names.admitted(item, qualified=True)
        )
    )
    return GetCaptureContractCard(
        contract=resolved.identity,
        version=names.version_number(resolved.identity, row.artifact_digest) if row else 1,
        lifecycle=contract.lifecycle.state,
        captures={
            "sources": list(contract.logical_source_identities),
            "evidence_kinds": list(contract.evidence_kinds),
            "grade": contract.epistemic_grade,
            "source_kinds": list(contract.allowed_source_kinds),
        },
        admitted_by=admitted_by,
        next=tuple(
            [_render_get(surface, resolved.display, "history")]
            + [_render_get(surface, f"ClaimType:{item}") for item in admitted_by[:1]]
        ),
    )


def _proposal_records(instance: PlaybillInstance, proposal_id: str) -> dict[str, Any]:
    """The proposal's list entry and whichever of its records are retained.

    The entry is the by-ID status read's, which authenticates every record that
    is present and answers ``incomplete`` with its reasons for any that is not.
    """

    from cruxible_core.service.proposals.proposals import service_playbill_proposal_status

    entry = service_playbill_proposal_status(instance, proposal_id=proposal_id)
    missing = set(entry.incomplete_reasons)
    evidence = instance.proposal_evidence()
    admission = None if "missing_admission" in missing else evidence.read_admission(proposal_id)
    evaluation = None if "missing_evaluation" in missing else evidence.read_evaluation(proposal_id)
    candidate = (
        None
        if evaluation is None
        or evaluation.candidate_digest is None
        or "missing_candidate" in missing
        else evidence.read_candidate(evaluation.candidate_digest)
    )
    return {
        "status": entry,
        "admission": admission,
        "evaluation": evaluation,
        "candidate": candidate,
    }


def _proposal_card(
    instance: PlaybillInstance,
    resolved: ResolvedRef,
    *,
    surface: ReadSurface,
) -> GetProposalCard:
    records = _proposal_records(instance, resolved.identity)
    entry = records["status"]
    admission = records["admission"]
    candidate = records["candidate"]
    changes: list[GetProposalChange] = []
    members = () if candidate is None else candidate.members
    for member in members[:_MAX_CHANGES]:
        changes.append(GetProposalChange(path=member.path, change=member.disposition))
    if len(members) > _MAX_CHANGES:
        changes.append(
            GetProposalChange(
                path="…", change=f"{len(members) - _MAX_CHANGES} more; see detail=proof"
            )
        )
    # A refused proposal says why on the card itself; there is no further step.
    evaluation = records["evaluation"]
    refusal = (
        tuple(
            GetProposalRefusal(
                code=item.code,
                message=item.message,
                repair=served_repair_for_refusal(item.code),
            )
            for item in evaluation.diagnostics
        )
        if evaluation is not None and evaluation.verdict == "refused"
        else ()
    )
    step: str | None = None
    if entry.status == "open" and entry.verdict == "candidate":
        step = "review"
    elif entry.terminal_reason == "stale":
        step = "readmit"
    return GetProposalCard(
        proposal=resolved.identity,
        status=entry.status,
        incomplete=entry.incomplete_reasons,
        verdict=entry.verdict,
        reason=entry.terminal_reason,
        actor=entry.actor_id,
        admitted_at=entry.admitted_at,
        rationale=None if admission is None else admission.rationale,
        changes=tuple(changes),
        refusal=refusal,
        next=() if step is None else (_render_proposal_step(surface, step, resolved.identity),),
    )


# -- evidence ------------------------------------------------------------------------


def _claim_rationale(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate, claim_id: str
) -> str | None:
    """The change-set rationale of the proposal that accepted this Claim revision."""

    with instance.accepted_history_reader(at=AcceptedCoordinate.from_internal(coordinate)) as h:
        latest = h.latest_member(claim_path(claim_id))
        if latest is None:
            return None
        candidate_digest = h.generation(latest.sequence).candidate_digest
    if candidate_digest is None:
        return None
    evidence = instance.proposal_evidence()
    if evidence.index is None:
        return None
    for row in evidence.index.rows(evidence, "candidate_digest=?", (candidate_digest,)):
        admission = evidence.read_admission(row["proposal_id"])
        if admission.rationale:
            return admission.rationale
    return None


def _claim_evidence(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    content: ExactContentReader,
) -> GetEvidence:
    from cruxible_core.service.claims.claims import service_explain_playbill_claim

    explanation = service_explain_playbill_claim(
        instance,
        identity=resolved.identity,
        at=AcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluation_time,
    )
    names = CaptureContractNames(instance, coordinate)
    bodies = instance.body_store()
    captures: list[GetCaptureEvidence] = []
    for account in explanation.admission_accounts:
        envelope = parse_capture_envelope(
            bodies.read(account.capture_digest, access=_SERVICE_ACCESS)
        )
        version = names.version_of(account.capture_contract_digest)
        source = getattr(envelope.source, "source_identity", None)
        if source is None and version is not None:
            source = version.contract.logical_source_identities[0]
        captures.append(
            GetCaptureEvidence(
                capture=capture_handle(account.capture_digest),
                contract=account.capture_contract_identity,
                version=names.version_number(
                    account.capture_contract_identity, account.capture_contract_digest
                ),
                source=str(source or "unknown"),
                observed_at=envelope.observed_at,
                role=account.citation_role,
                admitted=account.status == "admitted",
            )
        )
    attestations = tuple(
        GetAttestationEvidence(
            stance=item.statement.stance,
            principal=item.statement.provider_or_principal.name,
            at=item.statement.observed_at,
            current=item.current,
        )
        for item in explanation.exact_attestations
    )
    with instance.bind_accepted_projection(coordinate) as projection:
        claim = cast(ClaimArtifactAny, projection.typed.source(resolved.identity))
    value, content_digest = _shown(claim.statement.object, lambda: _artifact_value(claim), content)
    return GetEvidence(
        value=value,
        content_digest=content_digest,
        captures=tuple(captures),
        attestations=attestations,
        rationale=_claim_rationale(instance, coordinate, _name(resolved.identity)),
    )


# -- history -------------------------------------------------------------------------

_HISTORY_LIST = "get history"


@dataclass(frozen=True)
class _RevisionEntry:
    """One accepted revision, located but not yet read."""

    revision: int
    sequence: int
    digest: str
    lifecycle: str | None
    # The revision's value and, for exact content, its digest.
    value: Callable[[], tuple[object, str | None]] | None = None


def _revision(
    instance: PlaybillInstance,
    history: Any,
    entry: _RevisionEntry,
    *,
    ref: str,
    surface: ReadSurface,
) -> GetRevision:
    generation = history.generation(entry.sequence)
    # Genesis carries no change-set record: its instant is the bootstrap's and
    # nobody approved it.
    record = (
        None
        if generation.source_record_path is None
        else history.read_generation_record(entry.sequence, instance.blob_at)
    )
    value, content_digest = (None, None) if entry.value is None else entry.value()
    cut_value = summary_value(value)
    return GetRevision(
        revision=entry.revision,
        sequence=entry.sequence,
        git_oid=generation.git_oid[:12],
        accepted=(
            instance.accepted_evaluation_time(generation.git_oid).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if record is None
            else str(record.candidate.timestamp)
        ),
        actor=generation.actor_id or (None if record is None else record.actor_binding.actor_id),
        approved_by=(
            ()
            if record is None
            else tuple(dict.fromkeys(item.attestation.signer_id for item in record.approvals))
        ),
        lifecycle=entry.lifecycle,
        value=cut_value,
        next=(
            _render_get(
                surface,
                ref,
                "evidence",
                at=ClientCoordinate(
                    git_oid=generation.git_oid,
                    semantic_root=generation.semantic_root,
                    generation_root=generation.generation_root,
                    compiler_digest=generation.compiler_digest,
                ),
            ),
        )
        if _value_was_cut(cut_value, surface=surface, width=GET_CLI_HISTORY_VALUE_WIDTH)
        else (),
        content_digest=content_digest,
        digest=_short_digest(entry.digest),
    )


def _history_entries(
    instance: PlaybillInstance,
    resolved: ResolvedRef,
    history: Any,
    content: ExactContentReader,
) -> list[_RevisionEntry]:
    """Every accepted revision up to the read coordinate, oldest first."""

    from cruxible_core.service.authoring.documents import service_playbill_document_history
    from cruxible_core.service.claims.claims import service_playbill_claim_history
    from cruxible_core.service.claims.subjects import service_playbill_subject_history

    cutoff = history.sequence
    entries: list[_RevisionEntry] = []
    if resolved.kind == "claim":
        path = claim_path(_name(resolved.identity))

        def claim_value(git_oid: str) -> Callable[[], tuple[object, str | None]]:
            def read() -> tuple[object, str | None]:
                member = instance.blob_at(git_oid, path)
                if member is None:
                    return None, None
                claim = parse_claim(member, path=path)
                return _shown(claim.statement.object, lambda: _artifact_value(claim), content)

            return read

        for claim_entry in service_playbill_claim_history(
            instance, identity=resolved.identity
        ).entries:
            if claim_entry.sequence <= cutoff:
                entries.append(
                    _RevisionEntry(
                        revision=len(entries) + 1,
                        sequence=claim_entry.sequence,
                        digest=claim_entry.artifact_digest,
                        lifecycle=claim_entry.lifecycle_state,
                        value=claim_value(claim_entry.coordinate.git_oid),
                    )
                )
    elif resolved.kind == "subject":
        for subject_entry in service_playbill_subject_history(
            instance, identity=resolved.identity
        ).entries:
            if subject_entry.sequence <= cutoff:
                entries.append(
                    _RevisionEntry(
                        revision=len(entries) + 1,
                        sequence=subject_entry.sequence,
                        digest=subject_entry.artifact_digest,
                        lifecycle=subject_entry.lifecycle_state,
                    )
                )
    elif resolved.kind == "document":
        for document_entry in service_playbill_document_history(
            instance, identity=resolved.identity
        ).entries:
            if document_entry.sequence <= cutoff:
                body: object = {"body": _short_digest(document_entry.body_digest)}
                entries.append(
                    _RevisionEntry(
                        revision=len(entries) + 1,
                        sequence=document_entry.sequence,
                        digest=document_entry.envelope_digest,
                        lifecycle=None,
                        value=functools.partial(lambda value: (value, None), body),
                    )
                )
    else:
        # Definitions keep one version per accepted digest of their identity.
        seen: set[str] = set()
        for location in history.occurrences(resolved.identity):
            if location.artifact_digest in seen:
                continue
            seen.add(location.artifact_digest)
            entries.append(
                _RevisionEntry(
                    revision=len(entries) + 1,
                    sequence=location.occurrence_sequence,
                    digest=location.artifact_digest,
                    lifecycle=None,
                )
            )
    return entries


def _history(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    ref: str,
    surface: ReadSurface,
    limit: int,
    continuation: ListContinuation | None,
    content: ExactContentReader,
) -> tuple[GetHistory, bool, str | None]:
    """One page of revisions, newest first, and the cursor that continues it."""

    at = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=at) as history:
        newest_first = list(reversed(_history_entries(instance, resolved, history, content)))
        keys = [(str(entry.sequence), entry.digest) for entry in newest_first]
        snapshot = list_snapshot(keys)
        page, truncated = page_after_boundary(
            newest_first,
            keys=keys,
            snapshot=snapshot,
            continuation=continuation,
            limit=limit,
            list_name=_HISTORY_LIST,
        )
        revisions = tuple(
            _revision(instance, history, entry, ref=resolved.display, surface=surface)
            for entry in page
        )
    next_cursor = (
        encode_list_cursor(
            list_name=_HISTORY_LIST,
            coordinate=at.model_dump(mode="json"),
            selection={"ref": ref},
            snapshot=snapshot,
            last_key=(str(page[-1].sequence), page[-1].digest),
        )
        if truncated and page
        else None
    )
    return GetHistory(revisions=revisions), truncated, next_cursor


def _history_continuation(
    instance: PlaybillInstance,
    request: GetRequest,
) -> tuple[ListContinuation | None, ClientCoordinate | str | None]:
    """The page a history cursor continues, pinned to the coordinate it was cut at."""

    if request.cursor is None:
        return None, request.at
    continuation = decode_list_cursor(
        request.cursor, list_name=_HISTORY_LIST, selection={"ref": request.ref}
    )
    pinned = ClientCoordinate.model_validate(continuation.coordinate)
    at = request.at
    if at is not None and AcceptedCoordinate.from_internal(
        resolve_read_coordinate(instance, at)
    ).model_dump(mode="json") != pinned.model_dump(mode="json"):
        raise ListCursorMismatch(
            f"{ListCursorMismatch.error_code}: the cursor continues a different "
            "coordinate; omit at to continue it, or read history again without a cursor"
        )
    return continuation, pinned


# -- body ------------------------------------------------------------------------------


def _body(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    requested: ByteRange | None,
    access: BodyAccessContext,
    surface: ReadSurface,
) -> GetBody:
    from cruxible_core.service.authoring.documents import service_dereference_playbill_document

    read = service_dereference_playbill_document(
        instance,
        identity=resolved.identity,
        access=access,
        at=AcceptedCoordinate.from_internal(coordinate),
    )
    content = base64.b64decode(read.content_base64)
    size = len(content)
    if requested is None:
        if size > GET_BODY_DEFAULT_MAX_BYTES:
            window = f"0:{GET_BODY_DEFAULT_MAX_BYTES}"
            line = (
                f"Run {_render_get(surface, resolved.display, 'body', window=window)}, "
                "then read the following ranges"
            )
            raise ReadRefusalError(
                "cruxible.get.body_too_large",
                f"{resolved.display} is {size} bytes, over the {GET_BODY_DEFAULT_MAX_BYTES}-byte "
                "whole-body cap",
                repair=RepairOperation(
                    operation="cruxible.get",
                    arguments={"ref": resolved.display, "detail": "body", "range": window},
                ),
                repair_line=line,
                context={"size": size, "cap": GET_BODY_DEFAULT_MAX_BYTES},
            )
        window_range: ByteRange | None = ByteRange(start=0, end=size) if size else None
    else:
        # An empty Document has no bytes to range over: a range from 0 reads
        # its empty body, and any later start is out of bounds.
        if requested.start > 0 and requested.start >= size:
            arguments: dict[str, object] = {"ref": resolved.display, "detail": "body"}
            if size:
                arguments["range"] = f"0:{size}"
            raise ReadRefusalError(
                "cruxible.get.range_out_of_bounds",
                f"range starts at {requested.start} but {resolved.display} is {size} bytes",
                repair=RepairOperation(operation="cruxible.get", arguments=arguments),
                repair_line=(
                    f"Pass a range inside 0:{size}" if size else "Omit range; the body is empty"
                ),
                context={"size": size},
            )
        window_range = (
            ByteRange(start=requested.start, end=min(requested.end, size)) if size else None
        )
    chunk = b"" if window_range is None else content[window_range.start : window_range.end]
    text: str | None = None
    encoded: str | None = None
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        encoded = base64.b64encode(chunk).decode("ascii")
    return GetBody(
        document=_name(resolved.identity),
        media_type=read.media_type,
        size=size,
        body_digest=read.body_digest,
        range=window_range,
        text=text,
        content_base64=encoded,
    )


# -- proof and why ----------------------------------------------------------------------


def _proof(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    access: BodyAccessContext,
    viewer: OperationalViewer | None = None,
    installed_classifier_digests: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    at = AcceptedCoordinate.from_internal(coordinate)
    # A run id and a Capture digest are bare identities, not `Kind:name`.
    if resolved.kind == "procedure_run":
        return procedure_run_proof(instance, resolved.identity, viewer=viewer)
    if resolved.kind == "capture":
        envelope = parse_capture_envelope(
            instance.body_store().read(resolved.identity, access=_SERVICE_ACCESS)
        )
        return {
            "coordinate": at.model_dump(mode="json"),
            "capture_digest": resolved.identity,
            "envelope": envelope.model_dump(mode="json"),
        }
    name = _name(resolved.identity) if resolved.kind != "proposal" else resolved.identity
    if resolved.kind == "claim":
        from cruxible_core.service.claims.claims import service_get_playbill_claim

        return service_get_playbill_claim(
            instance, identity=resolved.identity, at=at, evaluation_time=evaluation_time
        ).model_dump(mode="json")
    if resolved.kind == "subject":
        from cruxible_core.service.claims.subjects import service_get_playbill_subject

        return service_get_playbill_subject(instance, identity=resolved.identity, at=at).model_dump(
            mode="json"
        )
    if resolved.kind == "claim_type":
        from cruxible_core.service.claims.claim_types import service_get_playbill_claim_type

        return service_get_playbill_claim_type(instance, predicate=name, at=at).model_dump(
            mode="json"
        )
    if resolved.kind == "document":
        from cruxible_core.service.authoring.documents import service_get_playbill_document

        return service_get_playbill_document(
            instance, identity=resolved.identity, access=access, at=at
        ).model_dump(mode="json")
    if resolved.kind == "query":
        from cruxible_core.service.discovery.query_definitions import (
            service_get_playbill_query_definition,
        )

        return service_get_playbill_query_definition(instance, name=name, at=at).model_dump(
            mode="json"
        )
    if resolved.kind == "procedure":
        from cruxible_core.service.procedures.procedure_runs import (
            ProcedureReadinessRequestV1,
            service_playbill_procedure_readiness,
        )

        return service_playbill_procedure_readiness(
            instance, name=name, request=ProcedureReadinessRequestV1(at=at)
        ).model_dump(mode="json")
    if resolved.kind == "provider_interface":
        item = _live_interface(
            instance,
            coordinate,
            resolved,
            installed_classifier_digests=installed_classifier_digests,
        )
        return {
            "coordinate": at.model_dump(mode="json"),
            "entry": item.entry.model_dump(mode="json"),
        }
    if resolved.kind == "principal":
        record = _principals_at(instance, coordinate)[name]
        return {
            "coordinate": at.model_dump(mode="json"),
            "path": resolved.path,
            "record": record.model_dump(mode="json"),
        }
    if resolved.kind in {
        "blueprint",
        "capture_contract",
        "trigger",
        "line",
        "resolution_contract",
        "mandate",
        "approval_policy",
        "procedure_runtime_policy",
        "source_acquisition_policy",
    }:
        with instance.bind_accepted_projection(coordinate) as projection:
            source = projection.typed.source(resolved.identity)
            row = projection.typed.envelope(resolved.identity)
        assert source is not None
        return {
            "coordinate": at.model_dump(mode="json"),
            "path": row.path if row else None,
            "artifact_digest": row.artifact_digest if row else None,
            "envelope": None if source is None else source.model_dump(mode="json"),
        }
    # Proposals are operational: the status entry and the retained records.
    return {
        name: None if value is None else value.model_dump(mode="json")
        for name, value in _proposal_records(instance, resolved.identity).items()
    }


def _why(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    resolved: ResolvedRef,
    *,
    evaluation_time: datetime,
    access: BodyAccessContext,
) -> dict[str, Any]:
    at = AcceptedCoordinate.from_internal(coordinate)
    if resolved.kind == "claim":
        from cruxible_core.service.claims.claims import service_explain_playbill_claim

        return service_explain_playbill_claim(
            instance, identity=resolved.identity, at=at, evaluation_time=evaluation_time
        ).model_dump(mode="json")
    from cruxible_core.service.discovery.explain import service_explain_playbill_subject

    return service_explain_playbill_subject(
        instance,
        subject=SemanticAddress.whole_artifact(
            resolved.path or f"subjects/{_name(resolved.identity)}.json"
        ),
        at=at,
        detail="summary",
        access=access,
    ).model_dump(mode="json")


# -- entry point ----------------------------------------------------------------------------


def service_playbill_get(
    instance: PlaybillInstance,
    *,
    request: GetRequest,
    access: BodyAccessContext,
    viewer: OperationalViewer | None = None,
    installed_classifier_digests: frozenset[str] = frozenset(),
) -> GetResult:
    """Resolve one reference and answer it at one ``detail`` level.

    ``access`` gates Document bodies. Exact-content Claim values are Claim
    values, so every caller reads them as text. ``viewer`` is the
    authenticated reader: a Line or run card names a runtime arming
    credential only to that credential or an admin (``None`` sees none).
    """

    continuation, at = _history_continuation(instance, request)
    coordinate = resolve_read_coordinate(instance, at)
    evaluation_time = request.evaluation_time or utc_now()
    resolved = resolve_get_ref(instance, coordinate, request.ref, surface=request.surface)
    if resolved.kind == "proposal":
        # Proposals are operational state, read only as of the current head;
        # one response never mixes an older requested generation with it. (A
        # Procedure run is read live whatever at names, and says so: `live`.)
        head = instance.accepted_coordinate()
        if request.at is not None and coordinate.git_oid != head.git_oid:
            raise ReadRefusalError(
                "cruxible.get.historical_read_unsupported",
                f"{resolved.display} is operational state, readable only at the current "
                "head, not at an earlier accepted generation",
                repair=RepairOperation(
                    operation="cruxible.get", arguments={"ref": resolved.display}
                ),
                repair_line="Omit at to read the proposal as of the current head",
                context={"ref": resolved.display, "kind": resolved.kind},
            )
        coordinate = head
    allowed = GET_DETAILS_BY_KIND[resolved.kind]
    if request.detail not in allowed:
        raise ReadRefusalError(
            "cruxible.get.detail_unsupported",
            f'detail="{request.detail}" does not apply to a {resolved.kind.replace("_", " ")}; '
            f"it takes {', '.join(allowed)}",
            repair=RepairOperation(
                operation="cruxible.get", arguments={"ref": resolved.display, "detail": "summary"}
            ),
            repair_line=f"Run {_render_get(request.surface, resolved.display)}",
            context={"ref": resolved.display, "kind": resolved.kind, "allowed": list(allowed)},
        )
    card: GetCard | None = None
    fields: dict[str, Any] = {}
    surface = request.surface
    content = ExactContentReader(instance)

    def render(ref: str, detail: str | None) -> str:
        return _render_get(surface, ref, detail)

    if request.detail == "summary":
        if resolved.kind == "claim":
            card = _claim_card(
                instance,
                coordinate,
                resolved,
                evaluation_time=evaluation_time,
                surface=surface,
                content=content,
            )
        elif resolved.kind == "subject":
            card = _subject_card(
                instance,
                coordinate,
                resolved,
                evaluation_time=evaluation_time,
                surface=surface,
                content=content,
            )
        elif resolved.kind == "claim_type":
            card = _claim_type_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "document":
            card = _document_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "procedure":
            card = _procedure_card(
                instance, coordinate, resolved, evaluation_time=evaluation_time, surface=surface
            )
        elif resolved.kind == "query":
            card = _query_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "capture_contract":
            card = _capture_contract_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "trigger":
            card = _trigger_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "blueprint":
            card = _blueprint_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "line":
            card = line_card(
                instance,
                coordinate,
                resolved.identity,
                evaluation_time=evaluation_time,
                render=render,
                viewer=viewer,
            )
        elif resolved.kind == "capture":
            card = capture_card(
                instance,
                coordinate,
                resolved.identity,
                render=render,
                read_capture=functools.partial(_render_read_capture, surface),
            )
        elif resolved.kind == "resolution_contract":
            card = resolution_contract_card(
                instance,
                coordinate,
                resolved.identity,
                evaluation_time=evaluation_time,
                render=render,
            )
        elif resolved.kind == "procedure_run":
            card = procedure_run_card(
                instance,
                resolved.identity,
                evaluation_time=evaluation_time,
                render=render,
                viewer=viewer,
            )
        elif resolved.kind == "principal":
            card = _principal_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "approval_policy":
            card = _approval_policy_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "procedure_runtime_policy":
            card = _runtime_policy_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "source_acquisition_policy":
            card = _acquisition_policy_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "provider_interface":
            card = _provider_interface_card(instance, coordinate, resolved, surface=surface)
        elif resolved.kind == "mandate":
            card = mandate_card(
                instance,
                coordinate,
                resolved.identity,
                evaluation_time=evaluation_time,
                render=render,
            )
        else:
            card = _proposal_card(instance, resolved, surface=surface)
        fields["card"] = card
        live = LIVE_CARD_FIELDS.get(resolved.kind)
        if live is not None:
            fields["live"] = live_view(instance, live)
    elif request.detail == "evidence":
        fields["evidence"] = _claim_evidence(
            instance, coordinate, resolved, evaluation_time=evaluation_time, content=content
        )
    elif request.detail == "why":
        fields["why"] = _why(
            instance, coordinate, resolved, evaluation_time=evaluation_time, access=access
        )
    elif request.detail == "history":
        fields["history"], fields["truncated"], fields["next_cursor"] = _history(
            instance,
            coordinate,
            resolved,
            ref=request.ref,
            surface=surface,
            limit=request.limit or GET_HISTORY_DEFAULT_LIMIT,
            continuation=continuation,
            content=content,
        )
    elif request.detail == "proof":
        fields["proof"] = _proof(
            instance,
            coordinate,
            resolved,
            evaluation_time=evaluation_time,
            access=access,
            viewer=viewer,
            installed_classifier_digests=installed_classifier_digests,
        )
        if resolved.kind == "procedure_run":
            fields["live"] = live_view(instance, ("proof",))
    else:
        fields["body"] = _body(
            instance,
            coordinate,
            resolved,
            requested=request.range,
            access=access,
            surface=surface,
        )
    served = AcceptedCoordinate.from_internal(coordinate)
    with instance.accepted_history_reader(at=served) as history:
        generation = int(history.sequence)
    if request.detail == "proof" or request.full_coordinate:
        fields["accepted_coordinate"] = ClientCoordinate.model_validate(
            served.model_dump(mode="json")
        )
    return GetResult(
        ref=resolved.display,
        kind=resolved.kind,
        detail=request.detail,
        coordinate=GetCoordinate(git_oid=served.git_oid[:12], generation=generation),
        evaluation_time=evaluation_time,
        **fields,
    )


__all__ = [
    "ResolvedRef",
    "resolve_get_ref",
    "service_playbill_get",
]

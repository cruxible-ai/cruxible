"""Indexed reads of exactly the Subjects and slots a Claims terminal selects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import ClaimArtifact
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subject_kinds import SubjectKind, live_subject_kind
from cruxible_core.indexes.subject_identity import (
    IdentityView,
    KeyTuple,
    canonical_key,
    tree_key_reader,
)

if TYPE_CHECKING:
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.procedures.egress import TerminalEgressRequestV2
    from cruxible_core.procedures.execution import ProcedureRunAdmission
    from cruxible_core.runtime.instance import PlaybillInstance


class ClaimsContext(Protocol):
    def claim_type(self, predicate: str) -> ClaimType | None: ...
    def subject(self, kind: str, key: Mapping[str, object]) -> SemanticAddress | None: ...
    def reference(self, kind: str, value: str) -> SemanticAddress | None: ...
    def slot(
        self, subject: SemanticAddress, predicate: str, qualifier: str | None
    ) -> tuple[ClaimArtifact, ...]: ...


@dataclass(frozen=True)
class _Slot:
    subject: SemanticAddress
    predicate: str


class AcceptedClaimsContext:
    def __init__(
        self, instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate
    ) -> None:
        self.instance = instance
        self.coordinate = coordinate
        self.tree = instance.immutable_tree_at(coordinate.git_oid)
        self._types: dict[str, ClaimType | None] = {}
        self._kinds: dict[str, SubjectKind | None] = {}
        self._subjects: dict[tuple[str, KeyTuple], SemanticAddress | None] = {}
        self._slots: dict[tuple[SemanticAddress, str, str | None], tuple[ClaimArtifact, ...]] = {}

    def claim_type(self, predicate: str) -> ClaimType | None:
        if predicate not in self._types:
            path = claim_type_path(predicate)
            content = self.tree.get(path)
            self._types[predicate] = (
                None if content is None else parse_claim_type(content, path=path)
            )
        return self._types[predicate]

    def subject(self, kind: str, key: Mapping[str, object]) -> SemanticAddress | None:
        if kind not in self._kinds:
            self._kinds[kind] = live_subject_kind(self.tree, kind)
        declared = self._kinds[kind]
        if declared is None:
            raise ValueError(f"SubjectKind {kind} is not live")
        canonical = canonical_key(declared, key, claim_type=self.claim_type)
        return self._subject(kind, canonical)

    def _subject(self, kind: str, key: KeyTuple) -> SemanticAddress | None:
        if (kind, key) in self._subjects:
            return self._subjects[(kind, key)]
        with self.instance.bind_accepted_projection(self.coordinate) as projection:
            view = IdentityView(projection.typed.connection, key_reader=tree_key_reader(self.tree))
            # Exact key holders, not id/alias guessing: the template declares a key.
            paths = view.holders(kind, key)
        if len(paths) > 1:
            raise ValueError(f"subject_key_ambiguous: {kind} has several holders")
        result = None if not paths else SemanticAddress.whole_artifact(paths[0])
        self._subjects[(kind, key)] = result
        return result

    def reference(self, kind: str, value: str) -> SemanticAddress | None:
        from cruxible_client.contracts.subjects import parse_subject, subject_path

        if value.startswith("subjects/"):
            path = value
        elif "/" in value:
            path = "subjects/" + value + ".json"
        else:
            path = subject_path(kind, value)
        if not path.startswith(f"subjects/{kind}/"):
            return None
        content = self.tree.get(path)
        if content is None or parse_subject(content, path=path).lifecycle.state != "live":
            return None
        return SemanticAddress.whole_artifact(path)

    def slot(
        self, subject: SemanticAddress, predicate: str, qualifier: str | None
    ) -> tuple[ClaimArtifact, ...]:
        key = (subject, predicate, qualifier)
        if key in self._slots:
            return self._slots[key]
        # claims_for is indexed by subject/predicate and returns live Claims only.
        statement = _Slot(subject=subject, predicate=predicate)
        result = tuple(
            c for c in self.tree.claims_for(statement) if c.statement.qualifier == qualifier
        )
        self._slots[key] = result
        return result


def reselect_retained_claims(
    instance: PlaybillInstance, request: TerminalEgressRequestV2, admission: ProcedureRunAdmission
) -> TerminalEgressRequestV2:
    """Recompile only retained items' slots at the new head, under their pinned templates."""
    from cruxible_client.contracts.claims import claim_artifact_digest
    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from cruxible_client.contracts.procedures.claims import ClaimsNode
    from cruxible_core.procedures.claims_changes import CoreClaimChange, CoreReobservationChange
    from cruxible_core.procedures.terminal_services import ProposalDeliveryRefused

    coordinate = instance.resolve_accepted_coordinate(
        git_oid=request.accepted_coordinate.git_oid,
        semantic_root=request.accepted_coordinate.semantic_root,
        generation_root=request.accepted_coordinate.generation_root,
    )
    context = AcceptedClaimsContext(instance, coordinate)
    content = instance.blob_at(admission.bound_coordinate.git_oid, admission.procedure_path)
    if content is None:
        raise ProposalDeliveryRefused(
            "proposal_item_invalid", "The retained claims template is unavailable"
        )
    procedure = parse_procedure(content, path=admission.procedure_path)
    node = next(
        (
            n
            for n in procedure.definition.nodes
            if isinstance(n, ClaimsNode) and n.node_id == request.node_id
        ),
        None,
    )
    if node is None:
        raise ProposalDeliveryRefused(
            "proposal_item_invalid", "The retained operation has no claims node"
        )
    changes = dict(request.claims_changes)
    for key, change in changes.items():
        if not isinstance(change, CoreClaimChange):
            continue
        statement = change.statement
        receipt = request.claims_evidence.get(key)
        if receipt is None or receipt.template >= len(node.claims):
            raise ProposalDeliveryRefused(
                "proposal_item_invalid", "The retained item has no pinned claims template"
            )
        mapping = node.claims[receipt.template]
        ct = context.claim_type(statement.predicate)
        if ct is None:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid", "The retained item's ClaimType is unavailable"
            )
        reference = statement.subject
        subject: SemanticAddress | None = (
            reference
            if isinstance(reference, SemanticAddress)
            else context.subject(
                reference.subject_kind,
                reference.key if isinstance(reference.key, dict) else {"slug": reference.key},
            )
        )
        held = (
            ()
            if subject is None
            else context.slot(subject, statement.predicate, statement.qualifier)
        )
        replaces = ct.precedence.replacements(statement.role, mapping.replaces)
        eligible = [c for c in held if c.statement.role in replaces]
        if any(c.statement.role not in replaces for c in held) and ct.cardinality == "one":
            raise ProposalDeliveryRefused(
                "proposal_item_invalid", "A higher-precedence Claim now holds this retained slot"
            )
        if ct.cardinality == "one" and len(eligible) > 1:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid", "The retained item's slot is now contested"
            )
        refresh = [
            c
            for c in eligible
            if c.statement.object == statement.object
            and c.statement.role == statement.role
            and c.statement.effective_from == statement.effective_from
            and c.statement.effective_until == statement.effective_until
        ]
        matched = [
            c
            for c in refresh
            if receipt.value.basis == "extracted"
            and c.backing.procedure_evidence is not None
            and c.backing.procedure_evidence.value.basis == "extracted"
            and c.backing.procedure_evidence.value.extract_spec_digest
            == receipt.value.extract_spec_digest
            and c.backing.procedure_evidence.value.record_key == receipt.value.record_key
        ]
        if len(matched) == 1:
            claim = matched[0]
            changes[key] = CoreReobservationChange(
                rationale="Re-observed the same declared value.",
                claim_id=claim.identity.name,
                artifact_digest=claim_artifact_digest(claim).tagged,
                value=statement.object.model_dump(mode="json"),
            )
        else:
            predecessor = (
                eligible[0]
                if ct.cardinality == "one" and eligible
                else refresh[0]
                if len(refresh) == 1
                else None
            )
            changes[key] = change.model_copy(
                update={"predecessor": None if predecessor is None else predecessor.identity.name}
            )
    return request.model_copy(update={"claims_changes": changes})

"""Indexed reads of exactly the Subjects and slots a Claims terminal selects."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    def logical_source(self, capture: str) -> str | None: ...
    def slots(self, subject: SemanticAddress, predicate: str) -> tuple[ClaimArtifact, ...]: ...
    def claim_type(self, predicate: str) -> ClaimType | None: ...
    def subject(self, kind: str, key: Mapping[str, object]) -> SemanticAddress | None: ...
    def reference(self, kind: str, value: str) -> SemanticAddress | None: ...
    def creation_paths(
        self, kind: str, keys: Sequence[Mapping[str, object]]
    ) -> tuple[str | None, ...]: ...
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
        self._capture_sources: dict[str, str | None] = {}
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

    def creation_paths(
        self, kind: str, keys: Sequence[Mapping[str, object]]
    ) -> tuple[str | None, ...]:
        """Where each keyed creation of ``kind`` lands, as shared lowering names it.

        The same machinery and order ``_assign_creations`` uses over this base:
        canonical keys in key order, each resolved through
        ``IdentityView.resolve_creation`` (a one-key creation's text as an alias
        of the kind, then the key's live holders) and, when it creates, named
        by ``assign_creation_id`` and laid over the view so a later key sees
        it. A creation lowering would refuse (ambiguous, unassignable) has no
        path (None). One projection binding for all of ``keys``.
        """

        from cruxible_client.contracts.subject_kinds import SubjectIdUnassignable
        from cruxible_client.contracts.subjects import subject_path
        from cruxible_core.indexes.subject_identity import assign_creation_id, key_order

        if not keys:
            return ()
        if kind not in self._kinds:
            self._kinds[kind] = live_subject_kind(self.tree, kind)
        declared = self._kinds[kind]
        if declared is None:
            raise ValueError(f"SubjectKind {kind} is not live")
        canonical = [canonical_key(declared, key, claim_type=self.claim_type) for key in keys]
        single: dict[KeyTuple, str | None] = {}
        for given, key in zip(keys, canonical, strict=True):
            # lowering's ``_single_text``: a one-key creation's text as written.
            raw = next(iter(given.values())) if len(given) == 1 else None
            single.setdefault(
                key, raw if len(declared.keys) == 1 and isinstance(raw, str) else None
            )
        paths: dict[KeyTuple, str | None] = {}
        names: set[str] = set()
        with self.instance.bind_accepted_projection(self.coordinate) as projection:
            view = IdentityView(projection.typed.connection)
            for key in sorted(single, key=key_order):
                resolution = view.resolve_creation(
                    kind, key, single_text=single[key], create_new=False
                )
                if resolution.path is not None or resolution.ambiguous:
                    paths[key] = resolution.path
                    continue
                try:
                    subject_id = assign_creation_id(view, kind, key, names=names)
                except SubjectIdUnassignable:
                    paths[key] = None
                    continue
                names.add(subject_id)
                path = subject_path(kind, subject_id)
                view.add_creation(path, key)
                paths[key] = path
        return tuple(paths[key] for key in canonical)

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

    def logical_source(self, capture: str) -> str | None:
        from cruxible_client.contracts.captures import parse_capture_envelope
        from cruxible_client.contracts.cas_contracts import BodyAccessContext
        from cruxible_core.errors import CoreError

        if capture not in self._capture_sources:
            # Logical-source attribution reads the Capture envelope, never the
            # source payload. B5 owns retained-payload access classification.
            try:
                envelope = parse_capture_envelope(
                    self.instance.body_store().read(
                        capture,
                        access=BodyAccessContext(principal_id="claims-absence", can_read_body=True),
                    )
                )
                source = getattr(envelope.source, "source_identity", None)
            except (CoreError, ValueError):
                source = None
            self._capture_sources[capture] = source
        return self._capture_sources[capture]

    def slots(self, subject: SemanticAddress, predicate: str) -> tuple[ClaimArtifact, ...]:
        return self.tree.claims_for(_Slot(subject=subject, predicate=predicate))

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
    from cruxible_client.contracts.claims import claim_artifact_digest, claim_path, parse_claim
    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from cruxible_client.contracts.procedures.claims import ClaimsNode
    from cruxible_core.procedures.claims_changes import (
        CoreClaimChange,
        CoreReobservationChange,
        CoreRetirementChange,
    )
    from cruxible_core.procedures.claims_units import (
        ClaimUnitUnresolved,
        same_content,
        unit_holders,
    )
    from cruxible_core.procedures.claims_units import unit_key as contract_unit
    from cruxible_core.procedures.terminal_services import ProposalDeliveryRefused

    def unit_key(obj: object, ct: ClaimType, context: ClaimsContext) -> bytes:
        try:
            return contract_unit(obj, ct, context)
        except ClaimUnitUnresolved as exc:
            raise ProposalDeliveryRefused("claims_inverse_unit_unresolved", str(exc)) from exc

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
    retired_ids = {c.claim_id for c in changes.values() if isinstance(c, CoreRetirementChange)}
    for key, change in changes.items():
        if isinstance(change, CoreRetirementChange):
            path = claim_path(change.claim_id)
            content = context.tree.get(path)
            claim = None if content is None else parse_claim(content, path=path)
            receipt = request.claims_evidence.get(key)
            ct = context.claim_type(change.claim_type)
            if (
                claim is None
                or claim.lifecycle.state != "live"
                or claim_artifact_digest(claim).tagged != change.artifact_digest
                or receipt is None
                or receipt.template >= len(node.claims)
                or ct is None
            ):
                raise ProposalDeliveryRefused(
                    "proposal_item_invalid",
                    "The Claim selected for absence retirement changed; run the Line again",
                )
            mapping = node.claims[receipt.template]
            role = mapping.role or ct.default_role
            if (
                role is None
                or mapping.when_absent != "mirror"
                and mapping.when_absent != "inverse"
                or claim.statement.role not in ct.precedence.replacements(role, mapping.replaces)
                or (
                    mapping.when_absent in {"mirror", "inverse"}
                    and not any(
                        context.logical_source(c) == change.witness.logical_source
                        for c in claim.backing.capture_digests
                    )
                )
            ):
                raise ProposalDeliveryRefused(
                    "proposal_item_invalid",
                    "The absence retirement is no longer eligible; run the Line again",
                )
            continue
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
        if receipt.absence is not None:
            # A retained proof says what the old complete source contained. It
            # cannot authorize replacing a different unit or a new protected
            # holder when delivery is recompiled against a later head.
            if ct.inverse is None:
                raise ProposalDeliveryRefused(
                    "proposal_item_invalid", "The retained ClaimType no longer declares an inverse"
                )
            unit = unit_key(statement.object.model_dump(mode="json"), ct, context)
            held = tuple(
                c
                for c in held
                if unit_key(c.statement.object.model_dump(mode="json"), ct, context) == unit
            )
            if any(
                c.statement.object.kind != "none"
                and not any(
                    context.logical_source(cap) == receipt.absence.logical_source
                    for cap in c.backing.capture_digests
                )
                for c in held
            ):
                raise ProposalDeliveryRefused(
                    "claims_absence_binding_invalid",
                    "A retained positive belongs to another logical source; run the Line again",
                )
            if any(c.statement.role not in replaces for c in held):
                raise ProposalDeliveryRefused(
                    "proposal_item_invalid", "A higher-precedence Claim now holds this absence unit"
                )
            remaining = [c for c in held if c.identity.name not in retired_ids]
            if len(remaining) > 1:
                raise ProposalDeliveryRefused(
                    "proposal_item_invalid", "The retained absence unit is now contested"
                )
            same = [
                c
                for c in remaining
                if c.statement.object.model_dump(mode="json")
                == statement.object.model_dump(mode="json")
            ]
            if same:
                claim = same[0]
                changes[key] = CoreReobservationChange(
                    rationale="Re-confirmed complete source absence.",
                    claim_id=claim.identity.name,
                    artifact_digest=claim_artifact_digest(claim).tagged,
                    value=statement.object.model_dump(mode="json"),
                )
            elif mapping.when_absent == "inverse":
                predecessor = remaining[0] if remaining else None
                if (
                    None if predecessor is None else predecessor.identity.name
                ) != change.predecessor:
                    raise ProposalDeliveryRefused(
                        "proposal_item_invalid",
                        "The Claim selected for inverse replacement changed; run the Line again",
                    )
            elif remaining:
                raise ProposalDeliveryRefused(
                    "proposal_item_invalid",
                    "A Claim now holds the unit selected for mirror absence; run the Line again",
                )
            continue
        # The terminal's unit law (D-R30), re-read at the delivery head.
        incoming = statement.object.model_dump(mode="json")
        try:
            holders = unit_holders(held, incoming, ct, context)
        except ClaimUnitUnresolved as exc:
            raise ProposalDeliveryRefused("claims_inverse_unit_unresolved", str(exc)) from exc
        if any(c.statement.role not in replaces for c in holders):
            raise ProposalDeliveryRefused(
                "proposal_item_invalid", "A higher-precedence Claim now holds this retained slot"
            )
        if len(holders) > 1:
            raise ProposalDeliveryRefused(
                "proposal_item_invalid", "The retained item's slot is now contested"
            )
        predecessor = holders[0] if holders else None
        refresh = (
            [predecessor]
            if predecessor is not None
            and same_content(
                predecessor.statement,
                obj=incoming,
                role=statement.role,
                period=(statement.effective_from, statement.effective_until),
            )
            else []
        )
        matched = [
            c
            for c in refresh
            if receipt.value.basis == "extracted"
            and c.backing.procedure_evidence is not None
            and c.backing.procedure_evidence.value.basis == "extracted"
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
            changes[key] = change.model_copy(
                update={"predecessor": None if predecessor is None else predecessor.identity.name}
            )
    return request.model_copy(update={"claims_changes": changes})

"""Indexed reads of exactly the Subjects and slots a Claims terminal selects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import ClaimArtifact
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subject_kinds import SubjectKind, live_subject_kind
from cruxible_core.indexes.subject_identity import (
    IdentityView,
    KeyTuple,
    PendingKey,
    SubjectKeyInvalid,
    canonical_key,
    canonical_key_value,
    tree_key_reader,
)
from cruxible_core.procedures.claims_units import (
    ClaimUnitUnresolved,
    NamedSubject,
    ValueTarget,
    unit_key,
)

if TYPE_CHECKING:
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.procedures.egress import TerminalEgressRequestV2
    from cruxible_core.procedures.execution import ProcedureRunAdmission
    from cruxible_core.runtime.instance import PlaybillInstance


class KeyPartUnresolved(ValueError):
    """A pair's key part names nothing: an end that is ambiguous, invalid or malformed,
    or a part whose field is absent. Only that row's Subject is unresolved."""


class ClaimsContext(Protocol):
    def logical_source(self, capture: str) -> str | None: ...
    def slots(self, subject: SemanticAddress, predicate: str) -> tuple[ClaimArtifact, ...]: ...
    def claim_type(self, predicate: str) -> ClaimType | None: ...
    def subject(self, kind: str, key: Mapping[str, object]) -> SemanticAddress | None: ...
    def named(self, kind: str, key: object) -> NamedSubject: ...
    def canonical(self, kind: str, key: Mapping[str, object]) -> PendingKey: ...
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
        self._capture_sources: dict[str, str | None] = {}
        self._kinds: dict[str, SubjectKind | None] = {}
        self._subjects: dict[tuple[str, KeyTuple], SemanticAddress | None] = {}
        self._references: dict[tuple[str, str], SemanticAddress | None] = {}
        self._slots: dict[tuple[SemanticAddress, str, str | None], tuple[ClaimArtifact, ...]] = {}
        self._named: dict[tuple[str, bytes], NamedSubject] = {}
        self._ends: dict[str, dict[str, str]] = {}
        self._units: dict[
            tuple[SemanticAddress, str, str | None],
            dict[bytes, tuple[ClaimArtifact, ...]] | ClaimUnitUnresolved,
        ] = {}

    def claim_type(self, predicate: str) -> ClaimType | None:
        if predicate not in self._types:
            path = claim_type_path(predicate)
            content = self.tree.get(path)
            self._types[predicate] = (
                None if content is None else parse_claim_type(content, path=path)
            )
        return self._types[predicate]

    def subject(self, kind: str, key: Mapping[str, object]) -> SemanticAddress | None:
        return self.named(kind, key).address

    def canonical(self, kind: str, key: Mapping[str, object]) -> PendingKey:
        """``key``'s canonical form under ``kind`` (its key ClaimTypes' forms, then R12).

        A pair kind's ends are their paths, or pending ends the run creates.
        """

        target = self.named(kind, key).target
        if target is None:
            raise KeyPartUnresolved(f"{kind}: an end is absent and is not created here")
        return target.key

    def _declared(self, kind: str) -> SubjectKind:
        if kind not in self._kinds:
            self._kinds[kind] = live_subject_kind(self.tree, kind)
        declared = self._kinds[kind]
        if declared is None:
            raise ValueError(f"SubjectKind {kind} is not live")
        return declared

    def _end_kinds(self, declared: SubjectKind) -> dict[str, str]:
        """Each Subject-valued key part of a kind and its end kind (none: a literal kind)."""

        from cruxible_client.contracts.shapes import SubjectType

        kind = declared.subject_kind
        if kind not in self._ends:
            ends: dict[str, str] = {}
            for predicate in declared.keys:
                found = self.claim_type(predicate)
                value = None if found is None else found.value
                if isinstance(value, SubjectType) and len(value.subject_kinds) == 1:
                    ends[predicate] = value.subject_kinds[0]
            self._ends[kind] = ends
        return self._ends[kind]

    def named(self, kind: str, key: object) -> NamedSubject:
        """The Subject ``kind`` and ``key`` name, and how this run would create it (V1, V5).

        A literal kind's key is canonicalized as before. A pair kind's
        Subject-valued part names its end as a nested ``{kind, key, create}``
        template, resolved the same way, recursively: an existing end is its
        path; an absent end with ``create: if_absent`` is a pending end the run
        creates first (its token is its kind and canonical key, so ends and pairs
        dedupe by final key); an absent end without it means the pair can be
        neither found nor created (``target`` None). A malformed, ambiguous or
        invalid end, or an absent literal part, raises ``KeyPartUnresolved``:
        that row alone, never a guess. Indexed seeks only, once per key.
        """

        from cruxible_client.contracts.canonical import canonical_bytes

        try:
            memo: tuple[str, bytes] | None = (kind, canonical_bytes(key))
        except (TypeError, ValueError):
            memo = None
        if memo is not None and memo in self._named:
            return self._named[memo]
        found = self._resolve_named(kind, key)
        if memo is not None:
            self._named[memo] = found
        return found

    def _resolve_named(self, kind: str, key: object) -> NamedSubject:
        from cruxible_client.contracts.subject_kinds import SubjectEnd, key_predicate
        from cruxible_core.procedures.claims_units import _present_key

        declared = self._declared(kind)
        ends = self._end_kinds(declared)
        if not ends:
            if not isinstance(key, Mapping):
                raise ValueError(f"key must map each key part of {kind}")
            literal = canonical_key(declared, key, claim_type=self.claim_type)
            return NamedSubject(self._subject(kind, literal), ValueTarget(kind=kind, key=literal))
        if not isinstance(key, Mapping):
            raise KeyPartUnresolved(f"key must map each key part of {kind}")
        given: dict[str, object] = {}
        present = _present_key(dict(key))
        assert isinstance(present, dict)
        for name, value in present.items():
            predicate = key_predicate(declared, str(name))
            if predicate is None or predicate in given:
                raise KeyPartUnresolved(
                    f"{name!r} is not a key of {kind}, or is given twice; its keys are "
                    + ", ".join(declared.keys)
                )
            given[predicate] = value
        missing = [predicate for predicate in declared.keys if predicate not in given]
        if missing:
            raise KeyPartUnresolved(f"a {kind} is named by its ends; missing " + ", ".join(missing))
        canonical: list[tuple[str, object]] = []
        parts: list[tuple[str, object]] = []
        pending = False
        for predicate in declared.keys:
            raw = given[predicate]
            end_kind = ends.get(predicate)
            if end_kind is None:
                definition = self.claim_type(predicate)
                if definition is None:
                    raise KeyPartUnresolved(f"key ClaimType {predicate!r} is not accepted")
                try:
                    value = canonical_key_value(
                        raw, definition, declared.normalizer(predicate), predicate=predicate
                    )
                except SubjectKeyInvalid as exc:
                    raise KeyPartUnresolved(str(exc)) from exc
                canonical.append((predicate, value))
                parts.append((predicate, value))
                continue
            if (
                not isinstance(raw, Mapping)
                or raw.get("kind") != end_kind
                or set(raw) - {"kind", "key", "create"}
            ):
                raise KeyPartUnresolved(
                    f"{predicate}: a pair's end is named {{kind: {end_kind}, key, create}}"
                )
            try:
                end = self.named(end_kind, _present_key(raw.get("key")))
            except KeyPartUnresolved:
                raise
            except (ValueError, KeyError, TypeError) as exc:
                raise KeyPartUnresolved(f"{predicate}: {exc}") from exc
            if end.address is not None:
                canonical.append((predicate, SubjectEnd(path=end.address.artifact_path)))
                parts.append((predicate, end.address))
            elif end.target is not None and raw.get("create", "never") == "if_absent":
                canonical.append((predicate, end.target.token))
                parts.append((predicate, end.target))
                pending = True
            else:
                # The end is absent and the template does not create it: no such pair.
                return NamedSubject(None, None)
        final = tuple(canonical)
        target = ValueTarget(kind=kind, key=final, parts=tuple(parts))  # type: ignore[arg-type]
        # A pending end is new, so no pair of it exists yet (no seek).
        address = None if pending else self._subject(kind, final)  # type: ignore[arg-type]
        return NamedSubject(address, target)

    def created_subject(self, kind: str, wire: Mapping[str, object]) -> SemanticAddress | None:
        """The live Subject a terminal's created-Subject reference names now, if any.

        ``wire`` is a Subject item's key (``CoreCreatedSubject.key``): literals,
        existing ends (Subject Claim objects) and ends the terminal creates.
        """

        from cruxible_client.contracts.subject_kinds import SubjectEnd, key_predicate

        declared = self._declared(kind)
        found: dict[str, object] = {}
        for name, value in wire.items():
            predicate = key_predicate(declared, str(name))
            if predicate is None:
                return None
            if isinstance(value, Mapping) and value.get("kind") == "subject":
                address = SemanticAddress.model_validate(value.get("address"))
                found[predicate] = SubjectEnd(path=address.artifact_path)
            elif isinstance(value, Mapping) and value.get("kind") == "created":
                end = self.created_subject(
                    str(value.get("subject_kind")),
                    value.get("key") if isinstance(value.get("key"), Mapping) else {},  # type: ignore[arg-type]
                )
                if end is None:
                    return None
                found[predicate] = SubjectEnd(path=end.artifact_path)
            else:
                definition = self.claim_type(predicate)
                if definition is None:
                    return None
                found[predicate] = canonical_key_value(
                    value, definition, declared.normalizer(predicate), predicate=predicate
                )
        if set(found) != set(declared.keys):
            return None
        final = cast("KeyTuple", tuple((p, found[p]) for p in declared.keys))
        return self._subject(kind, final)

    def pair_candidates(
        self, end: SemanticAddress, part: str, predicates: tuple[str, ...]
    ) -> tuple[str, ...]:
        """The pairs whose ``part`` is ``end`` and that hold a live fact of ``predicates``.

        One seek on ``pair_units`` (R12): candidates only, never authority -- a
        historical shell holds no row and costs nothing; every caller judges a
        proposed pair from its own Claims.
        """

        if not predicates:
            return ()
        marks = ",".join("?" for _ in predicates)
        with self.instance.bind_accepted_projection(self.coordinate) as projection:
            rows = projection.typed.connection.execute(
                "SELECT DISTINCT pair_path FROM main.pair_units WHERE end_path=? AND part=? "
                f"AND predicate IN ({marks}) ORDER BY pair_path",
                (end.artifact_path, part, *predicates),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def slot_units(
        self, subject: SemanticAddress, claim_type: ClaimType, qualifier: str | None
    ) -> dict[bytes, tuple[ClaimArtifact, ...]]:
        """A slot's held Claims grouped by their D-R30 unit, derived once per run.

        Raises ``ClaimUnitUnresolved`` (every time) when a held unit cannot be read.
        """

        key = (subject, claim_type.predicate, qualifier)
        if key not in self._units:
            try:
                grouped: dict[bytes, list[ClaimArtifact]] = {}
                for held in self.slot(subject, claim_type.predicate, qualifier):
                    grouped.setdefault(
                        unit_key(held.statement.object, claim_type, self), []
                    ).append(held)
                self._units[key] = {unit: tuple(found) for unit, found in grouped.items()}
            except ClaimUnitUnresolved as exc:
                self._units[key] = exc
        found = self._units[key]
        if isinstance(found, ClaimUnitUnresolved):
            raise found
        return found

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
        """The live Subject of ``kind`` a text names (V4), resolved once per text.

        A path (``subjects/...``) or ``kind/<id>`` names its Subject exactly.
        Any other text takes core's one text-reference order
        (``IdentityView.resolve``): exact live id, then alias, then the
        kind's single key (its R12 canonical form). Several matches refuse by
        the shared rule (``ValueError``); a retired Subject names nothing.
        """

        from cruxible_client.contracts.subjects import parse_subject

        if (kind, value) in self._references:
            return self._references[(kind, value)]
        if value.startswith("subjects/"):
            path: str | None = value
        elif value.startswith(kind + "/"):
            path = "subjects/" + value + ".json"
        else:
            with self.instance.bind_accepted_projection(self.coordinate) as projection:
                view = IdentityView(
                    projection.typed.connection, key_reader=tree_key_reader(self.tree)
                )
                resolution = view.resolve(kind, value)
            if resolution.ambiguous:
                raise ValueError(
                    f"subject_key_ambiguous: {kind} {value!r} names several Subjects: "
                    + ", ".join(resolution.candidates)
                )
            path = resolution.path
        result = None
        if path is not None and path.startswith(f"subjects/{kind}/"):
            content = self.tree.get(path)
            if content is not None and parse_subject(content, path=path).lifecycle.state == "live":
                result = SemanticAddress.whole_artifact(path)
        self._references[(kind, value)] = result
        return result

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
        CoreCreatedSubject,
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
            # A pair the run creates: held now only if it exists by now.
            else context.created_subject(
                reference.subject_kind, reference.model_dump(mode="json")["key"]
            )
            if isinstance(reference, CoreCreatedSubject)
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

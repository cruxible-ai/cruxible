"""Line-grained track records folded from one accepted ExhaustPromotion.

A Line's track record is keyed by the Line's own identity and occurrence epoch,
so it spans deployment revisions and provider rebinds: a rebind is a deployment
act that preserves the accepted LineSpec digest and epoch, and the deployment
digest never enters a key.  Credit never merges across implementations: the
implementation digest, the slot-interface digest, and the declared input bucket
are three separate dimensions, and every fact carries the exact dimension key it
was folded under.  Only an accepted promotion contributes, and only through the
promotion law that already verified the exact record range.

This module is deliberately not re-exported from ``playbill.exhaust``: it reads
the Procedure/LineSpec artifact families, which already depend on the journal
records, so re-exporting it would close an import cycle.  Import it by path.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CanonicalValue,
    Sha256Value,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedureV1
from cruxible_client.contracts.procedures.line_specs import AcceptedLineSpecV1
from cruxible_client.contracts.procedures.models import (
    ExhaustTapNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
)
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_core.playbill.exhaust.promotions import (
    AcceptedExhaustPromotionV1,
    VerifiedExhaustRecordV1,
)
from cruxible_core.playbill.procedures.egress import (
    EFFECTIVE_RUNG_TERMS,
    NO_TERMINAL_EGRESS,
    EffectiveRungTermV1,
    TerminalEgressKindV1,
)

LINE_TRACK_RECORD_TAG = "playbill-line-track-record-v1"

#: The three terminal-egress verdicts §8.5.2 lets one run report.
LineEgressVerdictV1 = Literal[
    "delivered",
    "refused_effective_rung",
    "dependencies_bound_egress_pending",
]

_INPUT_PLANES: tuple[str, ...] = ("accepted_state", "landed_capture", "exhaust")

_ITEM_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@=/+-]{0,255}$")


class LineTrackRecordError(PlaybillFormatError):
    """A Line track record cannot be folded from this exact promoted range."""


class _StrictTrackRecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _tagged(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


# ---------------------------------------------------------------------------
# The three separate credit dimensions
# ---------------------------------------------------------------------------


class LineDeclaredInputV1(_StrictTrackRecordModel):
    """One declared input plane coordinate, named before any run bound it."""

    tag: Literal["playbill-line-declared-input-v1"] = "playbill-line-declared-input-v1"
    plane: Literal["accepted_state", "landed_capture", "exhaust"]
    input_name: str


class LineTrackRecordDimensionsV1(_StrictTrackRecordModel):
    """Three independent axes.  Credit is never aggregated across any of them.

    ``implementation_digest`` is the exact accepted Procedure artifact a run
    executed.  ``slot_interface_digest`` is the nominal interface surface the
    LineSpec closed, which two different implementations may share.
    ``declared_input_bucket`` commits the declared input planes, so the same
    implementation reading a different declared plane earns separate credit.
    """

    tag: Literal["playbill-line-track-record-dimensions-v1"] = (
        "playbill-line-track-record-dimensions-v1"
    )
    implementation_digest: str
    slot_interface_digest: str
    declared_input_bucket: str

    _digests = field_validator(
        "implementation_digest",
        "slot_interface_digest",
        "declared_input_bucket",
    )(_tagged)


def line_track_record_dimension_key(dimensions: LineTrackRecordDimensionsV1) -> str:
    """Return the fact-key fragment that keeps the three axes from merging."""

    digest = typed_digest(
        Sha256Value,
        "playbill-line-track-record-dimensions-v1",
        dimensions.model_dump(mode="json", exclude={"tag"}),
    )
    return digest.value[:32]


def line_slot_interface_digest(
    accepted_line: AcceptedLineSpecV1,
    accepted_procedure: AcceptedProcedureV1,
) -> str:
    """Digest the nominal interface surface this LineSpec closed.

    Only slots the LineSpec actually binds contribute, and only their declared
    role/kind/interface, never the implementation bound into them.
    """

    declarations = {
        slot.slot_name: slot for slot in accepted_procedure.procedure.definition.pin_slots
    }
    surface: list[dict[str, str]] = []
    for binding in accepted_line.line.slot_bindings:
        declaration = declarations.get(binding.slot_name)
        if declaration is None:
            raise LineTrackRecordError(
                f"LineSpec binds slot {binding.slot_name!r} that this Procedure never declares"
            )
        surface.append(
            {
                "artifact_kind": declaration.artifact_kind,
                "interface_digest": declaration.interface_digest,
                "pin_role": declaration.pin_role,
                "slot_name": declaration.slot_name,
            }
        )
    surface.sort(key=lambda item: item["slot_name"].encode("utf-8"))
    return typed_digest(
        Sha256Value,
        "playbill-line-slot-interface-surface-v1",
        {"slots": surface},
    ).tagged


def line_declared_inputs(
    accepted_procedure: AcceptedProcedureV1,
) -> tuple[LineDeclaredInputV1, ...]:
    """Return the declared input planes of one accepted Procedure definition."""

    declared: list[LineDeclaredInputV1] = []
    for node in accepted_procedure.procedure.definition.nodes:
        if isinstance(node, StateTapNodeV3):
            declared.append(LineDeclaredInputV1(plane="accepted_state", input_name=node.as_))
        elif isinstance(node, SourceNodeV3):
            declared.append(LineDeclaredInputV1(plane="landed_capture", input_name=node.as_))
        elif isinstance(node, ExhaustTapNodeV3):
            declared.append(LineDeclaredInputV1(plane="exhaust", input_name=node.as_))
    declared.sort(
        key=lambda item: (_INPUT_PLANES.index(item.plane), item.input_name.encode("utf-8"))
    )
    return tuple(declared)


def line_declared_input_bucket(declared: tuple[LineDeclaredInputV1, ...]) -> str:
    """Digest the declared input planes so a plane change never merges credit."""

    return typed_digest(
        Sha256Value,
        "playbill-line-declared-input-bucket-v1",
        {"declared": [item.model_dump(mode="json", exclude={"tag"}) for item in declared]},
    ).tagged


def line_track_record_dimensions(
    accepted_line: AcceptedLineSpecV1,
    accepted_procedure: AcceptedProcedureV1,
) -> LineTrackRecordDimensionsV1:
    """Compute the three separate dimensions of one Line/Procedure binding."""

    if accepted_line.line.procedure.artifact_digest != accepted_procedure.artifact_digest:
        raise LineTrackRecordError(
            "accepted Procedure is not the exact implementation this LineSpec pins"
        )
    return LineTrackRecordDimensionsV1(
        implementation_digest=accepted_procedure.artifact_digest,
        slot_interface_digest=line_slot_interface_digest(accepted_line, accepted_procedure),
        declared_input_bucket=line_declared_input_bucket(line_declared_inputs(accepted_procedure)),
    )


# ---------------------------------------------------------------------------
# Egress history
# ---------------------------------------------------------------------------


class LineEgressChildV1(_StrictTrackRecordModel):
    """One fanout child, addressed by the manifest its own closure produced."""

    tag: Literal["playbill-line-egress-child-v1"] = "playbill-line-egress-child-v1"
    child_index: int = Field(ge=0)
    item_key: str
    manifest_digest: str

    _manifest = field_validator("manifest_digest")(_tagged)

    @field_validator("item_key")
    @classmethod
    def _item_key(cls, value: str) -> str:
        if not _ITEM_KEY_RE.fullmatch(value):
            raise ValueError("Line egress child item_key is not canonical")
        return value


class LineEgressTermReadingV1(_StrictTrackRecordModel):
    """One of the five independent §8.5.1 ceilings, as this run read it."""

    tag: Literal["playbill-line-egress-term-reading-v1"] = "playbill-line-egress-term-reading-v1"
    term: EffectiveRungTermV1
    rung: int = Field(ge=NO_TERMINAL_EGRESS, le=3)
    basis_digest: str | None = None

    @field_validator("basis_digest")
    @classmethod
    def _basis(cls, value: str | None) -> str | None:
        return None if value is None else _tagged(value)


class LineEgressReadingV1(_StrictTrackRecordModel):
    """One terminal's egress outcome: delivered, or capped and by which term."""

    tag: Literal["playbill-line-egress-reading-v1"] = "playbill-line-egress-reading-v1"
    sequence: int = Field(ge=1)
    run_id: str
    occurrence_id: str
    attempt: int = Field(ge=1)
    node_id: str
    kind: TerminalEgressKindV1
    verdict: LineEgressVerdictV1
    required_rung: int = Field(ge=0, le=3)
    effective_rung: int | None = Field(default=None, ge=NO_TERMINAL_EGRESS, le=3)
    effective_rung_digest: str | None = None
    limiting_term: EffectiveRungTermV1 | None = None
    terms: tuple[LineEgressTermReadingV1, ...] = ()
    children: tuple[LineEgressChildV1, ...]

    @field_validator("effective_rung_digest")
    @classmethod
    def _rung_digest(cls, value: str | None) -> str | None:
        return None if value is None else _tagged(value)

    @model_validator(mode="after")
    def _shape(self) -> "LineEgressReadingV1":
        bound = self.verdict != "dependencies_bound_egress_pending"
        if bound != (self.effective_rung is not None):
            raise ValueError("only a rung-bound egress reading carries an effective rung")
        if bound != (self.limiting_term is not None):
            raise ValueError("only a rung-bound egress reading names a limiting term")
        if bound != (self.effective_rung_digest is not None):
            raise ValueError("only a rung-bound egress reading carries its authority handle")
        if bound and tuple(item.term for item in self.terms) != EFFECTIVE_RUNG_TERMS:
            raise ValueError("a rung-bound egress reading carries exactly the five terms in order")
        if not bound and self.terms:
            raise ValueError("an unbound egress reading has no term readings to carry")
        if not self.children:
            raise ValueError("every egress reading owes its bound per-item closure")
        if tuple(item.child_index for item in self.children) != tuple(range(len(self.children))):
            raise ValueError("Line egress children must be in declared fanout order")
        return self


class LineCappedTermTallyV1(_StrictTrackRecordModel):
    """How many of this Line's terminals one term capped."""

    tag: Literal["playbill-line-capped-term-tally-v1"] = "playbill-line-capped-term-tally-v1"
    term: EffectiveRungTermV1
    capped_count: int = Field(ge=1)


class LineEgressTallyV1(_StrictTrackRecordModel):
    """The PC-G status feed: delivered versus capped, and by which term."""

    tag: Literal["playbill-line-egress-tally-v1"] = "playbill-line-egress-tally-v1"
    delivered: int = Field(ge=0)
    capped: int = Field(ge=0)
    pending: int = Field(ge=0)
    capped_by_term: tuple[LineCappedTermTallyV1, ...] = ()

    @model_validator(mode="after")
    def _totals(self) -> "LineEgressTallyV1":
        if sum(item.capped_count for item in self.capped_by_term) != self.capped:
            raise ValueError("capped-term tallies do not sum to the capped total")
        terms = tuple(item.term for item in self.capped_by_term)
        if terms != tuple(term for term in EFFECTIVE_RUNG_TERMS if term in set(terms)):
            raise ValueError("capped-term tallies must be in declared term order")
        return self


class LineTrackRecordV1(_StrictTrackRecordModel):
    """One Line's accepted operational record over one promoted exhaust range.

    ``deployment_snapshot_digests`` records every deployment revision the range
    spans.  It is reported, never keyed on, which is exactly how the record
    survives a provider rebind without splitting the Line's history.
    """

    tag: Literal["playbill-line-track-record-v1"] = "playbill-line-track-record-v1"
    line_id: str
    occurrence_epoch: int = Field(ge=1)
    line_spec_digest: str
    dimensions: LineTrackRecordDimensionsV1
    deployment_snapshot_digests: tuple[str, ...] = ()
    declared_inputs: tuple[LineDeclaredInputV1, ...]
    occurrence_ids: tuple[str, ...] = ()
    readings: tuple[LineEgressReadingV1, ...] = ()
    tally: LineEgressTallyV1

    _spec = field_validator("line_spec_digest")(_tagged)

    @field_validator("deployment_snapshot_digests")
    @classmethod
    def _deployments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("deployment snapshot digests must be sorted and unique")
        for item in value:
            _tagged(item)
        return value

    @field_validator("occurrence_ids")
    @classmethod
    def _occurrences(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("occurrence identifiers must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "LineTrackRecordV1":
        sequences = tuple(item.sequence for item in self.readings)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("Line egress readings must be in unique journal order")
        if len(self.readings) != self.tally.delivered + self.tally.capped + self.tally.pending:
            raise ValueError("Line egress tally does not account for every reading")
        return self


def line_track_record_digest(record: LineTrackRecordV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-line-track-record-v1",
        record.model_dump(mode="json", exclude={"tag"}),
    ).tagged


# ---------------------------------------------------------------------------
# The pure fold and the reducer that pins it
# ---------------------------------------------------------------------------


def _payload_mapping(record: VerifiedExhaustRecordV1) -> dict[str, object]:
    if not isinstance(record.payload, dict):
        raise LineTrackRecordError(
            f"exhaust record {record.sequence} carries no canonical payload object"
        )
    return dict(record.payload)


def _egress_reading(record: VerifiedExhaustRecordV1) -> LineEgressReadingV1:
    payload = _payload_mapping(record)
    if record.run_id is None or record.occurrence_id is None or record.attempt is None:
        raise LineTrackRecordError(
            f"terminal egress record {record.sequence} names no Line run coordinate"
        )
    children = payload.get("children")
    if not isinstance(children, list):
        raise LineTrackRecordError(
            f"terminal egress record {record.sequence} carries no child closure"
        )
    terms_payload = payload.get("terms")
    terms: list[dict[str, object]] = []
    if isinstance(terms_payload, list):
        terms = [
            {
                "term": item.get("term"),
                "rung": item.get("rung"),
                "basis_digest": item.get("basis_digest"),
            }
            for item in terms_payload
            if isinstance(item, dict)
        ]
    try:
        return LineEgressReadingV1.model_validate(
            {
                "sequence": record.sequence,
                "run_id": record.run_id,
                "occurrence_id": record.occurrence_id,
                "attempt": record.attempt,
                "node_id": payload.get("node_id"),
                "kind": payload.get("kind"),
                "verdict": payload.get("verdict"),
                "required_rung": payload.get("required_rung"),
                "effective_rung": payload.get("effective_rung"),
                "effective_rung_digest": payload.get("effective_rung_digest"),
                "limiting_term": payload.get("limiting_term"),
                "terms": terms,
                "children": [
                    {
                        "child_index": child.get("child_index"),
                        "item_key": child.get("item_key"),
                        "manifest_digest": child.get("manifest_digest"),
                    }
                    for child in children
                    if isinstance(child, dict)
                ],
            }
        )
    except (TypeError, ValueError) as exc:
        raise LineTrackRecordError(
            f"terminal egress record {record.sequence} is not a readable egress payload"
        ) from exc


def _tally(readings: tuple[LineEgressReadingV1, ...]) -> LineEgressTallyV1:
    capped = Counter(
        item.limiting_term for item in readings if item.verdict == "refused_effective_rung"
    )
    return LineEgressTallyV1(
        delivered=sum(1 for item in readings if item.verdict == "delivered"),
        capped=sum(capped.values()),
        pending=sum(1 for item in readings if item.verdict == "dependencies_bound_egress_pending"),
        capped_by_term=tuple(
            LineCappedTermTallyV1(term=term, capped_count=capped[term])
            for term in EFFECTIVE_RUNG_TERMS
            if capped[term]
        ),
    )


def build_line_track_record(
    records: tuple[VerifiedExhaustRecordV1, ...],
    *,
    accepted_line: AcceptedLineSpecV1,
    accepted_procedure: AcceptedProcedureV1,
) -> LineTrackRecordV1:
    """Fold one verified exhaust range into one Line's track record.

    Every record in the range must name this exact LineSpec digest and this
    exact implementation digest.  That is what stops a range that quietly spans
    two implementations, or two Lines, from merging their credit; a deployment
    revision or provider rebind changes neither and is recorded instead.
    """

    dimensions = line_track_record_dimensions(accepted_line, accepted_procedure)
    deployments: set[str] = set()
    occurrences: set[str] = set()
    readings: list[LineEgressReadingV1] = []
    for record in records:
        if record.line_spec_digest != accepted_line.artifact_digest:
            raise LineTrackRecordError(
                f"exhaust record {record.sequence} names another accepted LineSpec"
            )
        if record.procedure_artifact_digest != accepted_procedure.artifact_digest:
            raise LineTrackRecordError(
                f"exhaust record {record.sequence} names another Procedure implementation"
            )
        if record.occurrence_id is not None:
            occurrences.add(record.occurrence_id)
        if record.event_kind == "admission_bound":
            snapshot = _payload_mapping(record).get("deployment_snapshot_digest")
            if isinstance(snapshot, str):
                deployments.add(snapshot)
            continue
        if record.event_kind == "terminal_egress":
            readings.append(_egress_reading(record))
    ordered = tuple(sorted(readings, key=lambda item: item.sequence))
    return LineTrackRecordV1(
        line_id=accepted_line.line.identity.name,
        occurrence_epoch=accepted_line.line.occurrence_epoch,
        line_spec_digest=accepted_line.artifact_digest,
        dimensions=dimensions,
        deployment_snapshot_digests=tuple(
            sorted(deployments, key=lambda item: item.encode("ascii"))
        ),
        declared_inputs=line_declared_inputs(accepted_procedure),
        occurrence_ids=tuple(sorted(occurrences, key=lambda item: item.encode("ascii"))),
        readings=ordered,
        tally=_tally(ordered),
    )


def line_track_record_reducer_digest(
    accepted_line: AcceptedLineSpecV1,
    accepted_procedure: AcceptedProcedureV1,
) -> str:
    """Address the reducer by the exact LineSpec and implementation it folds."""

    return typed_digest(
        ArtifactDigest,
        "playbill-line-track-record-reducer-v1",
        {
            "line_spec_digest": accepted_line.artifact_digest,
            "procedure_artifact_digest": accepted_procedure.artifact_digest,
        },
    ).tagged


class LineTrackRecordReducer:
    """Reducer whose pinned digest commits the exact Line and implementation."""

    def __init__(
        self,
        *,
        accepted_line: AcceptedLineSpecV1,
        accepted_procedure: AcceptedProcedureV1,
    ) -> None:
        self._line = accepted_line
        self._procedure = accepted_procedure
        self._digest = line_track_record_reducer_digest(accepted_line, accepted_procedure)

    @property
    def reducer_digest(self) -> str:
        return self._digest

    def reduce(self, records: tuple[VerifiedExhaustRecordV1, ...]) -> object:
        return build_line_track_record(
            records,
            accepted_line=self._line,
            accepted_procedure=self._procedure,
        ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def parse_line_track_record_output(output: CanonicalValue) -> LineTrackRecordV1 | None:
    """Return the Line track record a promotion output declares, if it is one."""

    if not isinstance(output, dict) or output.get("tag") != LINE_TRACK_RECORD_TAG:
        return None
    try:
        return LineTrackRecordV1.model_validate(output)
    except ValueError as exc:
        raise LineTrackRecordError(
            "promotion output declares a malformed Line track record"
        ) from exc


def line_track_record_facts(
    accepted: AcceptedExhaustPromotionV1,
    *,
    output: CanonicalValue,
) -> tuple[ProjectionFact, ...]:
    """Only an accepted promotion can create canonical Line track-record facts.

    The subject is the Line the promotion exactly pins, so a rebind or a
    deployment revision leaves the subject alone and the history continues.  The
    fact key carries the dimension digest, so a different implementation, slot
    interface, or declared input bucket lands on its own fact instead of being
    summed into an existing one.
    """

    record = parse_line_track_record_output(output)
    if record is None:
        return ()
    promotion = accepted.promotion
    line_pins = tuple(
        pin for pin in promotion.pins if pin.role == "line" and pin.target.kind == "Line"
    )
    if len(line_pins) != 1:
        raise LineTrackRecordError(
            "a Line track-record promotion must pin exactly one accepted Line"
        )
    line_pin = line_pins[0]
    if line_pin.target.name != record.line_id:
        raise LineTrackRecordError("promotion output names a Line the promotion does not pin")
    if line_pin.artifact_digest != record.line_spec_digest:
        raise LineTrackRecordError("promotion output names another accepted LineSpec revision")
    if not any(
        pin.role == "procedure"
        and pin.target.kind == "Procedure"
        and pin.artifact_digest == record.dimensions.implementation_digest
        for pin in promotion.pins
    ):
        raise LineTrackRecordError(
            "promotion output names an implementation the promotion does not pin"
        )
    dimension_key = line_track_record_dimension_key(record.dimensions)
    return (
        ProjectionFact(
            schema_id="playbill.line.track_record",
            schema_version=1,
            subject_identity=line_pin.target.qualified,
            fact_key=f"{promotion.identity.name}.epoch-{record.occurrence_epoch}.{dimension_key}",
            value={
                "accepted_coordinate": accepted.accepted_coordinate.model_dump(mode="json"),
                "declared_input_bucket": {"$digest": record.dimensions.declared_input_bucket},
                "dimension_key": dimension_key,
                "implementation_digest": {"$digest": record.dimensions.implementation_digest},
                "line_id": record.line_id,
                "line_spec_digest": {"$digest": record.line_spec_digest},
                "occurrence_epoch": record.occurrence_epoch,
                "promotion_digest": {"$digest": accepted.artifact_digest},
                "slot_interface_digest": {"$digest": record.dimensions.slot_interface_digest},
                "track_record": record.model_dump(mode="json"),
                "track_record_digest": {"$digest": line_track_record_digest(record)},
            },
        ),
    )


__all__ = [
    "LINE_TRACK_RECORD_TAG",
    "LineCappedTermTallyV1",
    "LineDeclaredInputV1",
    "LineEgressChildV1",
    "LineEgressReadingV1",
    "LineEgressTallyV1",
    "LineEgressTermReadingV1",
    "LineEgressVerdictV1",
    "LineTrackRecordDimensionsV1",
    "LineTrackRecordError",
    "LineTrackRecordReducer",
    "LineTrackRecordV1",
    "build_line_track_record",
    "line_declared_input_bucket",
    "line_declared_inputs",
    "line_slot_interface_digest",
    "line_track_record_digest",
    "line_track_record_dimension_key",
    "line_track_record_dimensions",
    "line_track_record_facts",
    "line_track_record_reducer_digest",
    "parse_line_track_record_output",
]

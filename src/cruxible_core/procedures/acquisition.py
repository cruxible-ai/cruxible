"""Execution-time source acquisition: the only place a typed result may be read.

``on_unavailable``/``on_stale``/``on_oversized`` never fire from a scan, a plan,
or an absence.  They fire from a :class:`ProcedureSourceAcquisitionResultV1`,
which one acquirer produced by really attempting the read.  The Capture such an
attempt produces belongs to the acquisition manifest; it never re-enters the
admitted-input tuple and never relabels its capture time.
"""

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Mapping as MappingABC
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.canonical import (
    Sha256Value,
    normalize_canonical,
)
from cruxible_client.contracts.captures import (
    CaptureContractAny as CaptureContract,
)
from cruxible_client.contracts.captures import (
    CaptureEnvelopeAny,
    capture_contract_is_self_asserted,
    capture_digest,
)
from cruxible_client.contracts.claim_verdicts import (
    EvidenceEpistemicGrade,
    EvidenceProvenanceGrade,
)
from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionInputDecision,
    AcquisitionRule,
    UnavailableBehavior,
)
from cruxible_client.contracts.source_references import ExternalSourceReference
from cruxible_core.evidence.source_readers import ExternalCaptureAcquisitionV1

ProcedureAcquisitionOutcomeV1 = Literal[
    "acquired",
    "unavailable",
    "stale",
    "oversized",
    "refused",
]

ACQUISITION_UNAVAILABLE = "cruxible.acquisition.unavailable"
ACQUISITION_STALE = "cruxible.acquisition.stale"
ACQUISITION_OVERSIZED = "cruxible.acquisition.oversized"
ACQUISITION_REFUSED = "cruxible.acquisition.refused"
#: F05: a Source whose Capture replays outside the rule's declared set. A
#: hard eligibility gate, never an omit choice (unlike the three above).
ACQUISITION_REPLAYABILITY_INELIGIBLE = "cruxible.acquisition.replayability_ineligible"
#: F05/§4.1,§4.4: a ``url``/``final_url`` field naming an origin the Line's
#: ``acquisition.origins`` does not permit. A hard eligibility gate, never an
#: omit choice, exactly like the replayability gate above.
ACQUISITION_ORIGIN_INELIGIBLE = "cruxible.acquisition.origin_ineligible"

#: The field names core treats as URL-bearing wherever they appear in a
#: request or a result (§4.1: "the URL fields and final_url").
_ORIGIN_CHECKED_FIELD_NAMES = frozenset({"url", "final_url"})


def url_origin(url: str) -> str:
    """``scheme://host[:port]`` for an absolute URL; raises on anything else."""

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"{url!r} is not an absolute URL")
    return f"{parts.scheme}://{parts.netloc}"


def _named_urls(value: object) -> Iterator[str]:
    """Every string reached under a ``url``/``final_url`` key, at any depth."""

    if isinstance(value, MappingABC):
        for key, item in value.items():
            if key in _ORIGIN_CHECKED_FIELD_NAMES and isinstance(item, str):
                yield item
            else:
                yield from _named_urls(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _named_urls(item)


def disallowed_origin(rule: AcquisitionRule, *values: object) -> str | None:
    """The first URL outside ``rule.origins``, or ``None`` when every one named
    ``url``/``final_url`` in ``values`` is permitted (F05/§4.1, §4.4).

    ``origins`` declared ``None`` permits any, including a value this run
    cannot parse as absolute: an ordinary string-typed Contract field gives
    no URL-origin validation of its own, so there is nothing else to fail
    open to. Once ``origins`` is declared, it is a hard eligibility gate --
    never an omit choice, like :data:`ACQUISITION_REPLAYABILITY_INELIGIBLE`
    -- and a value whose origin cannot be established (scheme-relative,
    relative, or otherwise not ``scheme://host[:port]``) fails closed,
    exactly like one naming a disallowed origin: a restricted Line never
    dispatches a URL it cannot prove belongs to the permitted set.
    """

    if rule.origins is None:
        return None
    for value in values:
        for url in _named_urls(value):
            try:
                origin = url_origin(url)
            except ValueError:
                return url
            if origin not in rule.origins:
                return url
    return None


_OUTCOME_REASONS: dict[ProcedureAcquisitionOutcomeV1, str] = {
    "unavailable": ACQUISITION_UNAVAILABLE,
    "stale": ACQUISITION_STALE,
    "oversized": ACQUISITION_OVERSIZED,
    "refused": ACQUISITION_REFUSED,
}

#: F05: the rule field each omittable failure reason reads its behaviour
#: from. A reason with no entry here (e.g. the replayability gate, or a bare
#: "refused" outcome) can never be omitted.
_OMITTABLE_REASON_FIELDS: dict[str, str] = {
    ACQUISITION_UNAVAILABLE: "on_unavailable",
    ACQUISITION_STALE: "on_stale",
    ACQUISITION_OVERSIZED: "on_oversized",
}


def _omit_behavior(rule: AcquisitionRule, reason: str) -> UnavailableBehavior:
    field = _OMITTABLE_REASON_FIELDS.get(reason)
    return "refuse" if field is None else getattr(rule, field)


class _StrictAcquisitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureSourceAcquisitionResultV1(_StrictAcquisitionModel):
    """One real, typed acquisition attempt outcome for one declared input."""

    tag: Literal["playbill-procedure-source-acquisition-v1"] = (
        "playbill-procedure-source-acquisition-v1"
    )
    node_id: str
    input_name: str
    outcome: ProcedureAcquisitionOutcomeV1
    acquisition: ExternalCaptureAcquisitionV1 | None = None
    reason_code: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureSourceAcquisitionResultV1":
        if (self.outcome == "acquired") != (self.acquisition is not None):
            raise ValueError("only an acquired source result carries a Capture acquisition")
        if self.outcome == "acquired":
            if self.reason_code is not None:
                raise ValueError("an acquired source result names no failure reason")
        elif self.reason_code != _OUTCOME_REASONS[self.outcome]:
            raise ValueError("source acquisition reason code disagrees with its outcome")
        return self


class ProcedureCaptureMaterialV1(_StrictAcquisitionModel):
    """One dereferenced Capture: exact envelope, committed value, and its grades."""

    tag: Literal["playbill-procedure-capture-material-v1"] = (
        "playbill-procedure-capture-material-v1"
    )
    capture_digest: str
    capture_contract_digest: str
    envelope: CaptureEnvelopeAny
    value: object
    epistemic_grade: EvidenceEpistemicGrade
    provenance_grade: EvidenceProvenanceGrade

    @field_validator("capture_digest", "capture_contract_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _binding(self) -> "ProcedureCaptureMaterialV1":
        if capture_digest(self.envelope).tagged != self.capture_digest:
            raise ValueError("Capture material digest does not reproduce its envelope")
        if self.envelope.capture_contract_digest != self.capture_contract_digest:
            raise ValueError("Capture material names a different CaptureContract")
        return self


def capture_provenance_grade(contract: CaptureContract) -> EvidenceProvenanceGrade:
    """Reuse the accepted Claim-side derivation; never invent a second ladder."""

    if capture_contract_is_self_asserted(contract):
        return "self-asserted"
    return "daemon-fetched"


def _refusal(
    node_id: str,
    input_name: str,
    outcome: ProcedureAcquisitionOutcomeV1,
    detail: str,
) -> ProcedureSourceAcquisitionResultV1:
    return ProcedureSourceAcquisitionResultV1(
        node_id=node_id,
        input_name=input_name,
        outcome=outcome,
        reason_code=_OUTCOME_REASONS[outcome],
        detail=detail,
    )


def capture_selection_failure(
    rule: AcquisitionRule,
    envelope: CaptureEnvelopeAny,
    *,
    evaluation_time: datetime,
) -> str | None:
    """Eligibility shared by newly acquired and retained Capture inputs."""
    if rule.max_age is not None and (
        evaluation_time - envelope.observed_at > timedelta(microseconds=rule.max_age.microseconds)
    ):
        return ACQUISITION_STALE
    if (
        rule.permitted_replayability is not None
        and isinstance(envelope.source, ExternalSourceReference)
        and envelope.source.replayability not in rule.permitted_replayability
    ):
        # F05: the old replayability eligibility check. Never an omit choice:
        # a Source outside the declared replay class is never selected.
        return ACQUISITION_REPLAYABILITY_INELIGIBLE
    return None


def apply_acquisition_result(
    rule: AcquisitionRule,
    result: ProcedureSourceAcquisitionResultV1,
    *,
    default_authorized: bool = False,
    evaluation_time: datetime,
) -> AcquisitionInputDecision:
    """Apply the declared behaviour to a real typed acquisition result only.

    An unavailable, stale, or oversized result is omitted when the rule's
    matching field (``on_unavailable``/``on_stale``/``on_oversized``) says
    ``omit``, and refuses otherwise (F05). A replayability-ineligible or bare
    "refused" result always refuses: eligibility is a gate, not a choice.
    """

    del default_authorized
    if result.input_name != rule.input_name:
        raise ValueError("acquisition result names a different declared input")
    considered: tuple[str, ...] = ()
    if result.outcome == "acquired":
        acquisition = result.acquisition
        if acquisition is None:  # pragma: no cover - model invariant
            raise ValueError("acquired result lost its Capture")
        considered = (acquisition.capture_digest,)
        failure = capture_selection_failure(
            rule, acquisition.envelope, evaluation_time=evaluation_time
        )
        # Producing bytes is not admission: apply the same eligibility rules as
        # accepted Capture selection before those bytes enter the run context.
        if failure is None:
            return AcquisitionInputDecision(
                input_name=rule.input_name,
                disposition="selected",
                considered_capture_digests=considered,
                selected_capture_digests=considered,
            )
        reason = failure
    else:
        reason = _OUTCOME_REASONS[result.outcome]
    if _omit_behavior(rule, reason) == "omit":
        return AcquisitionInputDecision(
            input_name=rule.input_name,
            disposition="omitted",
            considered_capture_digests=considered,
            reason_codes=(reason,),
        )
    return AcquisitionInputDecision(
        input_name=rule.input_name,
        disposition="refused",
        considered_capture_digests=considered,
        reason_codes=(reason,),
    )


__all__ = [
    "ACQUISITION_ORIGIN_INELIGIBLE",
    "ACQUISITION_OVERSIZED",
    "ACQUISITION_REFUSED",
    "ACQUISITION_REPLAYABILITY_INELIGIBLE",
    "ACQUISITION_STALE",
    "ACQUISITION_UNAVAILABLE",
    "ProcedureAcquisitionOutcomeV1",
    "ProcedureCaptureMaterialV1",
    "ProcedureSourceAcquisitionResultV1",
    "apply_acquisition_result",
    "capture_provenance_grade",
    "disallowed_origin",
]

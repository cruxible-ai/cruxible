"""Erasing one retained Capture's body under the authority its contract grants.

A Capture is an envelope (what was observed, when, by which run, under which
contract) and, for most sources, a retained body: the exact bytes observed. A
body can hold something that must not stay, such as a secret printed by a tool.
Erasure destroys those bytes in the body store and keeps everything else: the
envelope, its digest, the contract, the landing record and an erasure record
naming who erased it, when and why.

Only a Capture whose CaptureContract declares ``erasure: authorized_by_rule``
(with the registered erasure-rule pin) may be erased; that declaration is
governed state, accepted under the instance's approval policy like any other
definition, and it is fixed for the contract's identity. Erasing cannot be
undone, so it previews by default and commits only with ``at``, the state
coordinate of its preview (R12).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import CasDigest
from cruxible_client.contracts.change_control import DryRun, PreviewAt, StateCoordinate
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import ServedRepair

#: A Capture named by the handle a card prints (``CAP-`` plus 12+ hex) or a
#: digest prefix of 12+ hex; the daemon resolves it as the write verbs do: among
#: Captures accepted Claims cite and retained ones that verify.
_CAPTURE_PREFIX = re.compile(r"^(?:CAP-|sha256:)[0-9a-f]{12,63}$")
#: The longest ``because`` an erasure records.
CAPTURE_ERASURE_BECAUSE_MAX_LENGTH = 1000
#: The citing Claims an erasure result names; ``citing_claim_count`` counts them all.
CAPTURE_ERASURE_MAX_LISTED_CLAIMS = 50


def validate_capture_ref(value: str) -> str:
    """A full Capture digest, a ``CAP-`` handle, or a ``sha256:`` prefix of 12+ hex."""

    if _CAPTURE_PREFIX.fullmatch(value):
        return value
    CasDigest.from_tagged(value)
    return value


def validate_erasure_because(value: str) -> str:
    """One normalized line of prose, stored with the erasure and echoed back.

    It is shown wherever the erased Capture is read, so it must not repeat the
    erased bytes, and a control character in it could forge a line of output.
    """

    if not value.strip() or value.strip() != value:
        raise ValueError("because must be nonblank and carry no surrounding whitespace")
    if any(not character.isprintable() for character in value):
        raise ValueError("because may not contain control characters")
    return value


CaptureEraseRefusalCode: TypeAlias = Literal[
    "cruxible.capture.erasure_head_moved",
    "cruxible.capture.erasure_unavailable",
    "cruxible.capture.erasure_contract_unavailable",
    "cruxible.capture.erasure_prohibited",
    "cruxible.capture.erasure_ledger_material",
    "cruxible.capture.erasure_nothing_retained",
    "cruxible.capture.erasure_body_in_accepted_state",
]


class CaptureEraseRefusedError(CruxibleError):
    """An erase refused before anything was destroyed or recorded, with its repair.

    Each code carries the repair that fits it (``repair``, a runnable operation
    or a hand edit) and one line saying what to do (``repair_line``); the HTTP
    boundary answers 409 with both. The message names public things only: the
    Capture, its contract, the citing Claim, and ``authorize_erasure``.
    """

    http_status = 409

    def __init__(
        self,
        code: CaptureEraseRefusalCode,
        message: str,
        *,
        capture_digest: str,
        repair: ServedRepair,
        repair_line: str,
    ) -> None:
        self.error_code = code
        self.capture_digest = capture_digest
        self.repair = repair
        self.repair_line = repair_line
        self.context: dict[str, Any] = {
            "capture_digest": capture_digest,
            "repair_line": repair_line,
        }
        super().__init__(f"{code}: {message}. {repair_line}")


class _StrictErasureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaptureErasureRecord(_StrictErasureModel):
    """Who erased one Capture's retained body, when, why, and under which authority."""

    tag: Literal["playbill-capture-erasure-record-v1"] = "playbill-capture-erasure-record-v1"
    capture_digest: str
    capture_contract: str = Field(description="The CaptureContract identity it was taken under.")
    capture_contract_digest: str
    erasure_rule_digest: str = Field(description="The erasure rule the contract pins.")
    body_digests: tuple[str, ...] = Field(
        description="The retained body objects destroyed: the CAS commitment and CAS source."
    )
    erased_by: str
    erased_at: datetime
    because: str = Field(min_length=1, max_length=CAPTURE_ERASURE_BECAUSE_MAX_LENGTH)

    @field_validator("capture_digest", "capture_contract_digest", "erasure_rule_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value

    @field_validator("body_digests")
    @classmethod
    def _bodies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            CasDigest.from_tagged(item)
        if not value or value != tuple(sorted(set(value))):
            raise ValueError("body_digests must be nonempty, sorted and unique")
        return value

    @field_validator("because")
    @classmethod
    def _because(cls, value: str) -> str:
        return validate_erasure_because(value)


class ErasureLandingPosition(_StrictErasureModel):
    """Where the body store's landing log stood when an erasure was recorded.

    A Capture whose envelope had landed by then held the bytes the erasure
    destroys; one that landed later holds bytes stored again, its own. The
    position is the storage event order; no clock decides it.
    """

    log: str = Field(min_length=1, description="The landing log's identity.")
    sequence: int = Field(ge=0, description="The log's last sequence at the record.")


class RecordedCaptureErasure(_StrictErasureModel):
    """One erasure as the erasure journal holds it: the record and where it was made.

    ``sequence`` is its position in the erasure journal: a judgment counts the
    erasures recorded by the time it was made (live, every record the journal
    holds; on replay, those at or before the position its evidence recorded),
    whatever the clocks or heads say. ``coordinate`` is the accepted head it was
    recorded under: the contract authorizing it is resolved as of that head.
    ``landing`` is the landing log position at the record (None for a record
    written before positions were kept).
    """

    record: CaptureErasureRecord
    coordinate: AcceptedCoordinate
    sequence: int = Field(ge=1)
    landing: ErasureLandingPosition | None = None


class CaptureEraseRequest(_StrictErasureModel):
    """Erase one Capture's retained body; previews unless committed with ``at``."""

    tag: Literal["playbill-capture-erase-request-v1"] = "playbill-capture-erase-request-v1"
    capture_digest: str = Field(
        description=(
            "The Capture's full digest (sha256:<64 hex>), or the CAP-<12+ hex> handle a get "
            "card prints."
        )
    )
    because: str = Field(
        min_length=1,
        max_length=CAPTURE_ERASURE_BECAUSE_MAX_LENGTH,
        description="Why the body is erased, recorded and shown; never repeat the erased bytes.",
    )
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("capture_digest")
    @classmethod
    def _ref(cls, value: str) -> str:
        return validate_capture_ref(value)

    @field_validator("because")
    @classmethod
    def _because(cls, value: str) -> str:
        return validate_erasure_because(value)


class CaptureEraseResult(_StrictErasureModel):
    """What one erasure destroyed (or, previewing, would destroy) and what it leaves.

    ``would_erase`` answers a preview: nothing was erased or recorded, and
    ``erasure`` shows the record a commit would write (its instant is the
    preview's). ``already_erased`` answers a Capture erased before: nothing new
    is recorded and ``erasure`` is the existing record. ``citing_claims`` are
    the live Claims that cite the Capture at the head; ``left_without_evidence``
    those of them whose every cited Capture other than their own words would
    then have an erased body (``next`` lists each as ``claim_evidence_erased``).
    An erasure adds no rule of its own: verdicts read the evidence as no longer
    replayable, and each ClaimType's own rules decide what that means (one that
    requires current replay reads the Claim ``stale``). A live Claim citing an
    erased Capture moves on by a re-bind onto a fresh Capture under the same
    contract, and can be revised or retired: what it inherits verifies on the
    envelope and this record (compiler revision 35).
    """

    tag: Literal["playbill-capture-erase-result-v1"] = "playbill-capture-erase-result-v1"
    status: Literal["erased", "would_erase", "already_erased"]
    erasure: CaptureErasureRecord
    citing_claims: tuple[str, ...]
    citing_claim_count: int = Field(ge=0)
    left_without_evidence: tuple[str, ...]
    #: The Capture's erasure state the change was checked against, read where it
    #: writes. Commit a preview by passing its digest as ``at``.
    coordinate: StateCoordinate


__all__ = [
    "CAPTURE_ERASURE_BECAUSE_MAX_LENGTH",
    "CAPTURE_ERASURE_MAX_LISTED_CLAIMS",
    "CaptureEraseRefusalCode",
    "CaptureEraseRefusedError",
    "CaptureEraseRequest",
    "CaptureEraseResult",
    "CaptureErasureRecord",
    "ErasureLandingPosition",
    "RecordedCaptureErasure",
    "validate_capture_ref",
    "validate_erasure_because",
]

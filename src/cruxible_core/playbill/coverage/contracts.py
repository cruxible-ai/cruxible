"""The frozen coverage-delivery grammar: match state, health, and the cards.

Three separations carry the whole §11.6 contract, and each one is structural
here rather than conventional:

*match state versus health* -- ``CoverageMatchStateV1`` answers "what
relationship," ``CoverageHealthV1`` answers "how trustworthy is that answer over
the declared boundary." They are orthogonal, so a span result carries both, and
the one place they meet is a law: ``none`` is a factual absence only when health
is ``complete``, and a health that cannot prove freshness or access can never
carry ``exact``. Both are model validators, so a false ``none`` and a
freshness-free ``exact`` are unrepresentable rather than merely discouraged.

*identity versus presentation* -- a source-occurrence identity is
``(logical source, observed commitment digest, ordinal among equal digests)``
and nothing else. Byte offsets and line numbers ride along in
``CoverageLineOverlayV1``, which is excluded from every preimage, so unchanged
cited content that moved within its source keeps its identity and stays
deterministically discoverable. Line movement alone can never break ``exact``.

*pointing versus granting* -- a coverage card points at accepted state and
history. It carries ``grants_mutation_authority`` as a ``Literal[False]`` and
refuses any match basis that resolves equivalence, so no card can be constructed
that claims copied bytes inherited the governance of the source they were copied
from.

Nothing in this module reads a clock. The manifest epoch is a counter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, normalize_ledger_path, typed_digest
from cruxible_client.contracts.claim_verdicts import ObservationTrustGrade
from cruxible_client.contracts.claims import (
    ClaimCitationV1,
    LegacyCitationReferenceV1,
    claim_citation_id,
)
from cruxible_client.contracts.discovery import DiscoveryMatchBasis
from cruxible_client.contracts.errors import CanonicalEncodingError, PlaybillError
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import CoverageDescriptorV1, SourceAccessClass
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.semantic_discovery import MATCH_BASIS_RESOLVES_EQUIVALENCE

OCCURRENCE_IDENTITY_DIGEST_DOMAIN = "playbill-coverage-occurrence-identity-v1"

CoverageMatchStateV1 = Literal["exact", "drifted", "candidate", "none"]
"""§11.6.2. Drift is its own state and its own card, never `exact` with a flag
and never an ordinary lexical `candidate`."""

COVERAGE_MATCH_STATES: tuple[str, ...] = ("exact", "drifted", "candidate", "none")

MATCH_STATE_PRECEDENCE: Mapping[str, int] = {
    "exact": 0,
    "drifted": 1,
    "candidate": 2,
    "none": 3,
}
"""Which state a span reports when several cards apply: the strongest one.

A span that holds both an exact card and a drifted card is exact *and* has
drift to show; reporting the weaker state would hide the verified match, and
reporting only the stronger card would hide the drift, so the state takes the
strongest and the card list keeps both.
"""

CoverageHealthV1 = Literal["complete", "partial", "stale", "denied", "unavailable"]
"""§11.6.3, over the declared scope, accepted coordinate, access profile, index
generation, and working snapshot -- never a global claim."""

COVERAGE_HEALTH_STATES: tuple[str, ...] = (
    "complete",
    "partial",
    "stale",
    "denied",
    "unavailable",
)

COVERAGE_HEALTH_RANK: Mapping[str, int] = {
    "unavailable": 0,
    "denied": 1,
    "stale": 2,
    "partial": 3,
    "complete": 4,
}
"""The floor order: combining health takes the weakest, never the friendliest."""

COVERAGE_HEALTH_PROVES_FRESHNESS: Mapping[str, bool] = {
    "complete": True,
    "partial": True,
    "stale": False,
    "denied": False,
    "unavailable": False,
}
"""§11.6.6, failing closed: only these healths may carry an `exact` match.

``partial`` proves freshness and admits `exact`. Truncation bounds *recall* --
some of the declared boundary was not scanned -- and a positive verified match
inside the part that was scanned stays sound. ``stale``, ``denied``, and
``unavailable`` cannot prove the working occurrence is the one the manifest
committed to, so the resolver lowers what would have been `exact` to a labeled
`candidate` rather than asserting a match it cannot stand behind.
"""

COVERAGE_HEALTH_ABSENCE_IS_FACTUAL: Mapping[str, bool] = {
    "complete": True,
    "partial": False,
    "stale": False,
    "denied": False,
    "unavailable": False,
}
"""§11.6.3: `none` means "nothing within this complete boundary," never
"globally ungoverned." Restricted or inaccessible coverage reports
`denied`/`unavailable` and is never allowed to read as a factual absence."""

CoverageWatcherHealthV1 = Literal["absent", "healthy", "degraded", "overflowed"]
"""§11.6.6. `absent` is honest -- there is no watcher in this slice -- and it is
not degraded: the manifest's own per-source commitments still prove freshness
against the observed snapshot. `degraded` and `overflowed` cannot."""


class CoverageError(PlaybillError):
    """A coverage answer could not be produced deterministically."""


class CoverageCommitmentMaterializationCorrupt(CoverageError):
    """Retained exact bytes exist but do not reproduce their commitment."""

    error_code = "playbill.coverage.commitment_materialization_corrupt"


class _StrictCoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- logical source identity ----------------------------------------------

_EXTERNAL_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class LogicalSourceIdentityV1(_StrictCoverageModel):
    """The stable logical source a coverage answer is keyed on.

    Not a filesystem path, not a line number, not a byte offset, and not a
    content digest: those are all presentation or content, and §11.6.1 needs an
    identity that survives an edit. Two planes exist because accepted state has
    exactly two locator-free ways to name a source -- a ledger artifact path and
    a registered external `source_identity`. A CAS reference deliberately has no
    logical source: it is addressed by its content, so identical bytes found
    anywhere are the same object and the question "which source is this?" has no
    answer to give. Working sources that have no accepted counterpart at all
    enter as `external` under a declared identity; the adapter that knows the
    filesystem declares the binding, and the resolver never sees a path.
    """

    tag: Literal["playbill-logical-source-identity-v1"] = "playbill-logical-source-identity-v1"
    plane: Literal["ledger", "external"]
    identity: str

    @model_validator(mode="after")
    def _identity_grammar(self) -> "LogicalSourceIdentityV1":
        if self.plane == "ledger":
            try:
                normalized = normalize_ledger_path(self.identity)
            except CanonicalEncodingError as exc:
                raise ValueError("ledger logical source must be a canonical ledger path") from exc
            if normalized != self.identity:
                raise ValueError("ledger logical source must already be NFC-normalized")
            return self
        if not _EXTERNAL_IDENTITY_RE.fullmatch(self.identity):
            raise ValueError("external logical source identity must be canonical and locator-free")
        return self

    @property
    def sort_key(self) -> bytes:
        return f"{self.plane}\x00{self.identity}".encode()


class CoverageCommitmentScanProofV1(_StrictCoverageModel):
    """Complete local scan proof for one source/commitment/length tuple."""

    tag: Literal["playbill-coverage-commitment-scan-proof-v1"] = (
        "playbill-coverage-commitment-scan-proof-v1"
    )
    source: LogicalSourceIdentityV1
    commitment_digest: str
    byte_length: int = Field(ge=0)
    complete: Literal[True] = True

    @field_validator("commitment_digest")
    @classmethod
    def _commitment_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @property
    def sort_key(self) -> tuple[bytes, bytes, int]:
        return (
            self.source.sort_key,
            self.commitment_digest.encode("ascii"),
            self.byte_length,
        )


class PlaybillCitationWindowObservationV1(_StrictCoverageModel):
    """Observed bytes at one accepted citation's original source window."""

    tag: Literal["playbill-citation-window-observation-v1"] = (
        "playbill-citation-window-observation-v1"
    )
    source: LogicalSourceIdentityV1
    citation_id: str
    commitment_digest: str
    original_start: int = Field(ge=0)
    original_end: int = Field(ge=0)
    addressable: bool
    observed_window_digest: str | None = None

    @field_validator("citation_id", "commitment_digest", "observed_window_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _window_shape(self) -> "PlaybillCitationWindowObservationV1":
        if self.original_end < self.original_start:
            raise ValueError("citation window end must not precede its start")
        if self.addressable != (self.observed_window_digest is not None):
            raise ValueError(
                "an addressable citation window requires its observed digest; "
                "an unaddressable window forbids one"
            )
        return self


def logical_sources_sorted(
    values: tuple[LogicalSourceIdentityV1, ...],
) -> tuple[LogicalSourceIdentityV1, ...]:
    """Return logical sources in canonical order without duplicates."""

    seen: dict[bytes, LogicalSourceIdentityV1] = {item.sort_key: item for item in values}
    return tuple(seen[key] for key in sorted(seen))


# -- occurrence identity and its presentation overlay ---------------------


class CoverageLineOverlayV1(_StrictCoverageModel):
    """Where an occurrence currently sits, for rendering only.

    Every field here is a presentation overlay over the stable source-occurrence
    identity. None of it enters an identity preimage, a manifest commitment, or
    a match decision, so relocating unchanged cited content within its source
    changes this record and nothing else.
    """

    tag: Literal["playbill-coverage-line-overlay-v1"] = "playbill-coverage-line-overlay-v1"
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _range(self) -> "CoverageLineOverlayV1":
        if self.end_byte < self.start_byte:
            raise ValueError("coverage overlay byte range must be increasing")
        if self.end_line < self.start_line:
            raise ValueError("coverage overlay line range must be increasing")
        return self


def occurrence_identity_digest(
    *,
    source: LogicalSourceIdentityV1,
    observed_commitment_digest: str,
    ordinal: int,
) -> str:
    """Digest exactly the three values that identify one source occurrence.

    Byte offsets and line numbers are absent on purpose. The ordinal counts
    only among occurrences of the *same* observed digest in the same source, so
    a unique occurrence keeps ordinal 0 wherever it moves to, and a duplicated
    one keeps a stable index the resolver can name when it refuses to bind.
    """

    Sha256Value.from_tagged(observed_commitment_digest)
    if ordinal < 0:
        raise ValueError("occurrence ordinal must be non-negative")
    return typed_digest(
        Sha256Value,
        OCCURRENCE_IDENTITY_DIGEST_DOMAIN,
        {
            "observed_commitment_digest": observed_commitment_digest,
            "ordinal": ordinal,
            "source": source.model_dump(mode="json"),
        },
    ).tagged


# -- request grammar ------------------------------------------------------


class CoverageSelectionV1(_StrictCoverageModel):
    """The byte window of a working source a caller is asking about."""

    tag: Literal["playbill-coverage-selection-v1"] = "playbill-coverage-selection-v1"
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)

    @model_validator(mode="after")
    def _range(self) -> "CoverageSelectionV1":
        if self.end_byte <= self.start_byte:
            raise ValueError("coverage selection must be a non-empty increasing byte range")
        return self


class CoverageSpanRequestV1(_StrictCoverageModel):
    """One span to resolve: a whole working source, or a window within it."""

    tag: Literal["playbill-coverage-span-request-v1"] = "playbill-coverage-span-request-v1"
    source: LogicalSourceIdentityV1
    selection: CoverageSelectionV1 | None = None


class CoverageCardBudgetV1(_StrictCoverageModel):
    """§11.6.4: candidate cards stay conservatively budgeted and say when clipped."""

    tag: Literal["playbill-coverage-card-budget-v1"] = "playbill-coverage-card-budget-v1"
    max_cards_per_span: int = Field(default=8, ge=1)
    max_candidate_cards_per_span: int = Field(default=4, ge=0)


class CoverageAccessProfileV1(_StrictCoverageModel):
    """Which access classes this caller may be told about, and how to refuse.

    ``disclose_restricted_existence`` is the §11.6.3 non-disclosure branch: when
    it is false, the boundary is reported incomplete (`partial`) without naming
    the restricted material at all, rather than reporting `denied` and thereby
    revealing that restricted coverage exists.
    """

    tag: Literal["playbill-coverage-access-profile-v1"] = "playbill-coverage-access-profile-v1"
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    permitted_access_classes: tuple[SourceAccessClass, ...] = ("public", "instance")
    disclose_restricted_existence: bool = True

    @field_validator("permitted_access_classes")
    @classmethod
    def _classes(cls, value: tuple[SourceAccessClass, ...]) -> tuple[SourceAccessClass, ...]:
        if value != byte_sorted(tuple(value)):
            raise ValueError("permitted access classes must be sorted and unique")
        return value

    def permits(self, access_class: SourceAccessClass) -> bool:
        return access_class in self.permitted_access_classes


class CoverageRequestV1(_StrictCoverageModel):
    """One coverage operation over one accepted coordinate."""

    tag: Literal["playbill-coverage-request-v1"] = "playbill-coverage-request-v1"
    instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    at: AcceptedCoordinate
    spans: tuple[CoverageSpanRequestV1, ...]
    budget: CoverageCardBudgetV1 = CoverageCardBudgetV1()

    @field_validator("spans")
    @classmethod
    def _spans(cls, value: tuple[CoverageSpanRequestV1, ...]) -> tuple[CoverageSpanRequestV1, ...]:
        if not value:
            raise ValueError("a coverage request must name at least one span")
        return value


# -- result grammar -------------------------------------------------------


class CoverageCardV1(_StrictCoverageModel):
    """One relationship between a working occurrence and accepted state.

    A drift card binds the complete §11.6.2 tuple: the accepted Claim/Capture
    handle, the accepted coordinate, the expected commitment digest, the
    observed working digest, the source/selection identity, the dereference
    handle when the caller projected one, and a bounded dependent count when it
    is known. It points at accepted history; it grants the changed material
    nothing.
    """

    tag: Literal["playbill-coverage-card-v1"] = "playbill-coverage-card-v1"
    match_state: Literal["exact", "drifted", "candidate"]
    match_basis: DiscoveryMatchBasis | None = None
    resolves_equivalence: Literal[False] = False
    grants_mutation_authority: Literal[False] = False
    at: AcceptedCoordinate
    claim_addresses: tuple[SemanticAddress, ...] = ()
    capture_digests: tuple[str, ...] = ()
    expected_commitment_digest: str
    observed_commitment_digest: str | None = None
    accepted_source: LogicalSourceIdentityV1 | None = None
    observed_source: LogicalSourceIdentityV1
    occurrence_identity_digest: str | None = None
    line_overlay: CoverageLineOverlayV1 | None = None
    dereference_handle_digest: str | None = None
    dependent_claim_count: int | None = Field(default=None, ge=0)
    reason_codes: tuple[str, ...] = ()

    @field_validator("capture_digests")
    @classmethod
    def _captures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("coverage card capture digests must be sorted and unique")
        for item in value:
            Sha256Value.from_tagged(item)
        return value

    @field_validator("reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("coverage card reason codes must be sorted and unique")
        return value

    @field_validator(
        "expected_commitment_digest",
        "observed_commitment_digest",
        "occurrence_identity_digest",
        "dereference_handle_digest",
    )
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _card_law(self) -> "CoverageCardV1":
        if self.match_basis is not None and MATCH_BASIS_RESOLVES_EQUIVALENCE[self.match_basis]:
            raise ValueError("a coverage card may not carry an equivalence-resolving match basis")
        if self.match_state == "exact":
            if self.accepted_source != self.observed_source:
                raise ValueError("an exact card requires the accepted and observed source to agree")
            if self.observed_commitment_digest != self.expected_commitment_digest:
                raise ValueError("an exact card requires the observed commitment to reproduce")
            if self.occurrence_identity_digest is None:
                raise ValueError("an exact card requires the verified occurrence identity")
        if self.match_state == "drifted":
            if self.accepted_source != self.observed_source:
                raise ValueError("drift is located in the cited source, not a foreign one")
            if self.observed_commitment_digest is None:
                raise ValueError("a drift card requires the newly observed working digest")
            if self.observed_commitment_digest == self.expected_commitment_digest:
                raise ValueError("a drift card requires the observed commitment to differ")
            if not self.capture_digests and self.dereference_handle_digest is None:
                raise ValueError("a drift card requires the accepted Capture or dereference handle")
        if (
            self.match_basis == "content_equivalent"
            and self.accepted_source == self.observed_source
        ):
            raise ValueError("content_equivalent labels a foreign occurrence, not the cited source")
        return self

    @property
    def sort_key(self) -> tuple[int, bytes, bytes, bytes]:
        return (
            MATCH_STATE_PRECEDENCE[self.match_state],
            self.observed_source.sort_key,
            self.expected_commitment_digest.encode("ascii"),
            (self.occurrence_identity_digest or "").encode("ascii"),
        )


CoverageCitationReferenceV2: TypeAlias = Annotated[
    ClaimCitationV1 | LegacyCitationReferenceV1,
    Field(discriminator="tag"),
]


class CoverageClaimCitationV2(_StrictCoverageModel):
    """One Claim-to-Capture association retained by the disposable v2 index."""

    tag: Literal["playbill-coverage-claim-citation-v2"] = "playbill-coverage-claim-citation-v2"
    claim_address: SemanticAddress
    capture_digest: str
    reference: CoverageCitationReferenceV2
    observation_trust: ObservationTrustGrade

    @field_validator("capture_digest")
    @classmethod
    def _capture_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reference_agrees(self) -> "CoverageClaimCitationV2":
        if self.capture_digest != self.reference.capture_digest:
            raise ValueError("coverage citation reference names a different Capture")
        if self.claim_address.selector.scheme != "claim-statement-v1":
            raise ValueError("coverage citation must address one exact Claim statement")
        claim_name = self.claim_address.artifact_path.rsplit("/", 1)[-1].removesuffix(".yaml")
        if not re.fullmatch(r"CLM-[0-9a-f]{32}", claim_name):
            raise ValueError("coverage citation address has no Claim identity")
        if isinstance(self.reference, LegacyCitationReferenceV1):
            expected_path = self.claim_address.artifact_path
            if not expected_path.endswith(f"/{self.reference.claim_identity.name}.yaml"):
                raise ValueError("legacy coverage citation addresses a different Claim")
        else:
            expected = claim_citation_id(
                ArtifactIdentity(kind="Claim", name=claim_name),
                capture_digest=self.reference.capture_digest,
                role=self.reference.role,
                origin=self.reference.origin,
            ).tagged
            if self.reference.citation_id != expected:
                raise ValueError("coverage citation ID does not match its Claim address")
        return self

    @property
    def sort_key(self) -> tuple[bytes, bytes]:
        return (
            self.reference.citation_id.encode("ascii"),
            self.claim_address.artifact_path.encode("utf-8"),
        )


class CoverageCardV2(CoverageCardV1):
    tag: Literal["playbill-coverage-card-v2"] = "playbill-coverage-card-v2"  # type: ignore[assignment]
    citation_associations: tuple[CoverageClaimCitationV2, ...] = ()

    @field_validator("citation_associations")
    @classmethod
    def _associations(
        cls,
        value: tuple[CoverageClaimCitationV2, ...],
    ) -> tuple[CoverageClaimCitationV2, ...]:
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("coverage citation associations must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _association_projection(self) -> "CoverageCardV2":
        if not {item.capture_digest for item in self.citation_associations}.issubset(
            self.capture_digests
        ):
            raise ValueError("coverage card associations must name its Capture set")
        if not {item.claim_address for item in self.citation_associations}.issubset(
            self.claim_addresses
        ):
            raise ValueError("coverage card associations must name its Claim set")
        return self

    @property
    def is_self_published_copy(self) -> bool:
        """Whether this card contains the explicit publication association."""

        return any(
            isinstance(item.reference, ClaimCitationV1)
            and item.reference.role == "copy"
            and item.reference.origin == "self_published"
            for item in self.citation_associations
        )


class CoverageSpanResultV1(_StrictCoverageModel):
    """One span's relationship and the trustworthiness of that answer."""

    tag: Literal["playbill-coverage-span-result-v1"] = "playbill-coverage-span-result-v1"
    request: CoverageSpanRequestV1
    match_state: CoverageMatchStateV1
    health: CoverageHealthV1
    absence_is_factual: bool
    cards: tuple[CoverageCardV1, ...] = ()
    ambiguous_occurrence_count: int = Field(default=0, ge=0)
    omitted_card_count: int = Field(default=0, ge=0)
    coverage: CoverageDescriptorV1

    @model_validator(mode="after")
    def _span_law(self) -> "CoverageSpanResultV1":
        expected_absence = (
            self.match_state == "none" and COVERAGE_HEALTH_ABSENCE_IS_FACTUAL[self.health]
        )
        if self.absence_is_factual != expected_absence:
            raise ValueError("a coverage absence is factual exactly when the boundary is complete")
        if self.match_state == "exact" and not COVERAGE_HEALTH_PROVES_FRESHNESS[self.health]:
            raise ValueError("an exact match requires health that proves freshness and access")
        if self.match_state == "none":
            if self.cards:
                raise ValueError("a `none` span cannot carry coverage cards")
        else:
            if not self.cards:
                raise ValueError("a non-`none` span requires at least one card")
            strongest = min(MATCH_STATE_PRECEDENCE[card.match_state] for card in self.cards)
            if MATCH_STATE_PRECEDENCE[self.match_state] != strongest:
                raise ValueError("a span reports the strongest state its cards carry")
        if self.ambiguous_occurrence_count and self.match_state == "exact":
            raise ValueError("indistinguishable occurrences are never silently bound to one")
        if tuple(card.sort_key for card in self.cards) != tuple(
            sorted(card.sort_key for card in self.cards)
        ):
            raise ValueError("coverage cards must be in canonical order")
        return self


class CoverageSpanResultV2(CoverageSpanResultV1):
    tag: Literal["playbill-coverage-span-result-v2"] = "playbill-coverage-span-result-v2"  # type: ignore[assignment]
    cards: tuple[CoverageCardV2, ...] = ()


class CoverageSpanResultV3(_StrictCoverageModel):
    """One locally proved span, independent of unrelated batch truncation."""

    tag: Literal["playbill-coverage-span-result-v3"] = "playbill-coverage-span-result-v3"
    request: CoverageSpanRequestV1
    match_state: CoverageMatchStateV1
    health: CoverageHealthV1
    absence_is_factual: bool
    cards: tuple[CoverageCardV2, ...] = ()
    ambiguous_occurrence_count: int = Field(default=0, ge=0)
    omitted_card_count: int = Field(default=0, ge=0)
    commitment_scan_proofs: tuple[CoverageCommitmentScanProofV1, ...] = ()
    citation_window_observations: tuple[PlaybillCitationWindowObservationV1, ...] = ()
    coverage: CoverageDescriptorV1

    @model_validator(mode="after")
    def _span_law(self) -> "CoverageSpanResultV3":
        expected_absence = (
            self.match_state == "none" and COVERAGE_HEALTH_ABSENCE_IS_FACTUAL[self.health]
        )
        if self.absence_is_factual != expected_absence:
            raise ValueError("a coverage absence is factual exactly when the boundary is complete")
        if self.match_state == "exact" and not COVERAGE_HEALTH_PROVES_FRESHNESS[self.health]:
            raise ValueError("an exact match requires health that proves freshness and access")
        if self.match_state == "none":
            if self.cards:
                raise ValueError("a `none` span cannot carry coverage cards")
        else:
            if not self.cards:
                raise ValueError("a non-`none` span requires at least one card")
            strongest = min(MATCH_STATE_PRECEDENCE[card.match_state] for card in self.cards)
            if MATCH_STATE_PRECEDENCE[self.match_state] != strongest:
                raise ValueError("a span reports the strongest state its cards carry")
        if self.ambiguous_occurrence_count and self.match_state == "exact":
            raise ValueError("indistinguishable occurrences are never silently bound to one")
        if tuple(card.sort_key for card in self.cards) != tuple(
            sorted(card.sort_key for card in self.cards)
        ):
            raise ValueError("coverage cards must be in canonical order")
        proof_keys = tuple(item.sort_key for item in self.commitment_scan_proofs)
        if proof_keys != tuple(sorted(set(proof_keys))):
            raise ValueError("coverage span scan proofs must be sorted and unique")
        if any(item.source != self.request.source for item in self.commitment_scan_proofs):
            raise ValueError("coverage span scan proofs must name the requested source")
        window_keys = tuple(
            (
                item.source.sort_key,
                item.citation_id.encode("ascii"),
                item.original_start,
                item.original_end,
            )
            for item in self.citation_window_observations
        )
        if window_keys != tuple(sorted(set(window_keys))):
            raise ValueError("citation window observations must be sorted and unique")
        if any(item.source != self.request.source for item in self.citation_window_observations):
            raise ValueError("citation window observations must name the requested source")
        return self


class CoverageBatchSummaryV1(_StrictCoverageModel):
    """§11.6.4: one summary per operation, not one `none` beside every line."""

    tag: Literal["playbill-coverage-batch-summary-v1"] = "playbill-coverage-batch-summary-v1"
    exact: int = Field(ge=0)
    drifted: int = Field(ge=0)
    candidate: int = Field(ge=0)
    none: int = Field(ge=0)
    returned_spans: int = Field(ge=0)
    omitted_card_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _totals(self) -> "CoverageBatchSummaryV1":
        if self.exact + self.drifted + self.candidate + self.none != self.returned_spans:
            raise ValueError("coverage summary states must total the returned spans")
        return self


class CoverageBatchSummaryV2(CoverageBatchSummaryV1):
    tag: Literal["playbill-coverage-batch-summary-v2"] = "playbill-coverage-batch-summary-v2"  # type: ignore[assignment]


class CoverageBatchSummaryV3(_StrictCoverageModel):
    tag: Literal["playbill-coverage-batch-summary-v3"] = "playbill-coverage-batch-summary-v3"
    exact: int = Field(ge=0)
    drifted: int = Field(ge=0)
    candidate: int = Field(ge=0)
    none: int = Field(ge=0)
    returned_spans: int = Field(ge=0)
    omitted_card_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _totals(self) -> "CoverageBatchSummaryV3":
        if self.exact + self.drifted + self.candidate + self.none != self.returned_spans:
            raise ValueError("coverage summary states must total the returned spans")
        return self


class CoverageResultV1(_StrictCoverageModel):
    """The result of one coverage operation, bound to everything it depended on.

    §11.6.3 requires every result to bind the instance and accepted coordinate,
    the compiler/index digest, the working-set scope, the per-file or snapshot
    commitments, the access profile, the manifest epoch, watcher health, and
    completeness/truncation. The snapshot commitments live in the manifest this
    result names by digest rather than being copied in beside it, so a result
    and its manifest cannot disagree about what was observed.
    """

    tag: Literal["playbill-coverage-result-v1"] = "playbill-coverage-result-v1"
    at: AcceptedCoordinate
    instance_id: str
    index_digest: str
    overlay_digest: str
    manifest_digest: str | None
    epoch: int | None = Field(default=None, ge=0)
    watcher_health: CoverageWatcherHealthV1
    access_profile: CoverageAccessProfileV1
    scope: tuple[LogicalSourceIdentityV1, ...] = ()
    spans: tuple[CoverageSpanResultV1, ...]
    summary: CoverageBatchSummaryV1
    health: CoverageHealthV1
    coverage: CoverageDescriptorV1

    @field_validator("index_digest", "overlay_digest", "manifest_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _result_law(self) -> "CoverageResultV1":
        counts = {state: 0 for state in COVERAGE_MATCH_STATES}
        for span in self.spans:
            counts[span.match_state] += 1
        if (
            self.summary.exact,
            self.summary.drifted,
            self.summary.candidate,
            self.summary.none,
            self.summary.returned_spans,
        ) != (
            counts["exact"],
            counts["drifted"],
            counts["candidate"],
            counts["none"],
            len(self.spans),
        ):
            raise ValueError("coverage summary must reproduce from the span results")
        floor = min(
            (COVERAGE_HEALTH_RANK[span.health] for span in self.spans),
            default=COVERAGE_HEALTH_RANK["complete"],
        )
        if COVERAGE_HEALTH_RANK[self.health] != floor:
            raise ValueError("batch coverage health is the weakest span health, never the best")
        if (self.manifest_digest is None) != (self.epoch is None):
            raise ValueError("a manifest digest and its epoch are bound together or absent")
        if self.scope != logical_sources_sorted(self.scope):
            raise ValueError("coverage scope must be sorted and unique")
        return self


class CoverageResultV2(CoverageResultV1):
    tag: Literal["playbill-coverage-result-v2"] = "playbill-coverage-result-v2"  # type: ignore[assignment]
    spans: tuple[CoverageSpanResultV2, ...]
    summary: CoverageBatchSummaryV2


class CoverageResultV3(_StrictCoverageModel):
    """Coverage with source-local proof health and explicit global completeness."""

    tag: Literal["playbill-coverage-result-v3"] = "playbill-coverage-result-v3"
    at: AcceptedCoordinate
    instance_id: str
    index_digest: str
    overlay_digest: str
    manifest_digest: str | None
    epoch: int | None = Field(default=None, ge=0)
    watcher_health: CoverageWatcherHealthV1
    access_profile: CoverageAccessProfileV1
    scope: tuple[LogicalSourceIdentityV1, ...] = ()
    spans: tuple[CoverageSpanResultV3, ...]
    summary: CoverageBatchSummaryV3
    health: CoverageHealthV1
    global_scan_complete: bool
    truncation_reason_codes: tuple[str, ...] = ()
    coverage: CoverageDescriptorV1

    @field_validator("index_digest", "overlay_digest", "manifest_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("truncation_reason_codes")
    @classmethod
    def _truncation_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("coverage truncation reasons must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _result_law(self) -> "CoverageResultV3":
        counts = {state: 0 for state in COVERAGE_MATCH_STATES}
        for span in self.spans:
            counts[span.match_state] += 1
        if (
            self.summary.exact,
            self.summary.drifted,
            self.summary.candidate,
            self.summary.none,
            self.summary.returned_spans,
        ) != (
            counts["exact"],
            counts["drifted"],
            counts["candidate"],
            counts["none"],
            len(self.spans),
        ):
            raise ValueError("coverage summary must reproduce from the span results")
        expected_health = weakest_health(
            *(span.health for span in self.spans),
            *("stale",) if self.watcher_health in {"degraded", "overflowed"} else (),
            *("partial",) if not self.global_scan_complete else (),
        )
        if self.health != expected_health:
            raise ValueError(
                "batch coverage health derives from spans, watcher health, and global scan health"
            )
        if self.global_scan_complete == bool(self.truncation_reason_codes):
            raise ValueError("global scan incompleteness must state its reasons")
        if (self.manifest_digest is None) != (self.epoch is None):
            raise ValueError("a manifest digest and its epoch are bound together or absent")
        if self.scope != logical_sources_sorted(self.scope):
            raise ValueError("coverage scope must be sorted and unique")
        return self


CoverageResultAny: TypeAlias = Annotated[
    CoverageResultV1 | CoverageResultV2 | CoverageResultV3,
    Field(discriminator="tag"),
]


# -- the manifest family --------------------------------------------------


class CoverageManifestProfileV1(_StrictCoverageModel):
    """The fields every §11.6.3 manifest binds, in one place, so profiles inherit.

    §11.6.3 requires every coverage result *or manifest* to bind the instance and
    accepted coordinate, the compiler/index digest, the working-set scope, the
    access profile, the manifest epoch, watcher health, and completeness with its
    truncation. Surfaces that publish a boundary differ only in what they can
    honestly fill in and what they add: an exported floor observes no working
    snapshot, so its epoch is absent; a render adds a lens and per-file
    baselines. Those are **profiles of one family**, not separate schemas, and
    they say so by subclassing this record rather than by re-listing its fields
    and hoping they stay in step.

    Two fields stay wide here on purpose. ``epoch`` and ``watcher_health`` are
    filled by profiles that observe a working snapshot and left at "no snapshot"
    by profiles that do not; a profile that can never observe one narrows them
    with its own validator rather than with a narrowed annotation, so the family
    keeps one type for one field.
    """

    format: Literal["playbill-coverage-manifest-v1"] = "playbill-coverage-manifest-v1"
    instance_id: str
    coordinate: AcceptedCoordinate
    index_digest: str
    access_profile_id: str
    watcher_health: CoverageWatcherHealthV1 = "absent"
    epoch: int | None = Field(default=None, ge=0)
    completeness: Literal["complete", "partial"]
    truncation_reason_codes: tuple[str, ...] = ()
    scope: tuple[LogicalSourceIdentityV1, ...] = ()

    @field_validator("index_digest")
    @classmethod
    def _index_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("truncation_reason_codes")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("manifest truncation reason codes must be sorted and unique")
        return value

    @field_validator("scope")
    @classmethod
    def _scope(
        cls, value: tuple[LogicalSourceIdentityV1, ...]
    ) -> tuple[LogicalSourceIdentityV1, ...]:
        if value != logical_sources_sorted(value):
            raise ValueError("manifest scope sources must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _completeness_is_explained(self) -> "CoverageManifestProfileV1":
        if (self.completeness == "partial") != bool(self.truncation_reason_codes):
            raise ValueError("manifest completeness must agree with its truncation reasons")
        return self


class CoverageManifestProfileV2(CoverageManifestProfileV1):
    format: Literal["playbill-coverage-manifest-v2"] = "playbill-coverage-manifest-v2"  # type: ignore[assignment]


def weakest_health(*values: CoverageHealthV1) -> CoverageHealthV1:
    """Combine health floors: the weakest wins, always."""

    weakest: CoverageHealthV1 = "complete"
    for value in values:
        if COVERAGE_HEALTH_RANK[value] < COVERAGE_HEALTH_RANK[weakest]:
            weakest = value
    return weakest


def strongest_match_state(states: Iterable[CoverageMatchStateV1]) -> CoverageMatchStateV1:
    """Return the state a span reports for the cards it holds."""

    strongest: CoverageMatchStateV1 = "none"
    for state in states:
        if MATCH_STATE_PRECEDENCE[state] < MATCH_STATE_PRECEDENCE[strongest]:
            strongest = state
    return strongest


__all__ = [
    "COVERAGE_HEALTH_ABSENCE_IS_FACTUAL",
    "COVERAGE_HEALTH_PROVES_FRESHNESS",
    "COVERAGE_HEALTH_RANK",
    "COVERAGE_HEALTH_STATES",
    "COVERAGE_MATCH_STATES",
    "MATCH_STATE_PRECEDENCE",
    "OCCURRENCE_IDENTITY_DIGEST_DOMAIN",
    "CoverageAccessProfileV1",
    "CoverageBatchSummaryV1",
    "CoverageBatchSummaryV2",
    "CoverageBatchSummaryV3",
    "CoverageCardBudgetV1",
    "CoverageCardV1",
    "CoverageCardV2",
    "CoverageClaimCitationV2",
    "CoverageCommitmentMaterializationCorrupt",
    "CoverageCommitmentScanProofV1",
    "CoverageError",
    "CoverageHealthV1",
    "CoverageLineOverlayV1",
    "CoverageManifestProfileV1",
    "CoverageManifestProfileV2",
    "CoverageMatchStateV1",
    "CoverageRequestV1",
    "CoverageResultV1",
    "CoverageResultAny",
    "CoverageResultV2",
    "CoverageResultV3",
    "CoverageSelectionV1",
    "CoverageSpanRequestV1",
    "CoverageSpanResultV1",
    "CoverageSpanResultV2",
    "CoverageSpanResultV3",
    "CoverageWatcherHealthV1",
    "LogicalSourceIdentityV1",
    "PlaybillCitationWindowObservationV1",
    "logical_sources_sorted",
    "strongest_match_state",
    "occurrence_identity_digest",
    "weakest_health",
]

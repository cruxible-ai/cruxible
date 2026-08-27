"""The reference rendering of a coverage answer (§11.6.4).

§11.7 makes the CLI and the file-based context floor the *reference* surface --
"reference," not "canonical," because canonical already means accepted state in
Playbill -- and requires every adapter to reproduce their coverage semantics.
Rendering therefore lives here rather than in the CLI: an adapter that appends
cards to a tool result renders the same bytes the CLI prints, from the same
`CoverageResultV1`, without re-deriving anything.

Three laws, all structural
--------------------------
*Annotate the governed minority inline.* Every `exact`, `drifted`, and surviving
`candidate` card is rendered on its own line, in the canonical card order, so a
reader can see which spans have a relationship and what that relationship is.

*Summarize the ungoverned majority once.* A span that resolved to `none` inside
a complete boundary renders **nothing at all**. It is counted in the one batch
summary and never mentioned again. Forty-one ungoverned lines produce forty-one
counted `none`s and zero lines of output, which is the whole difference between
delivering coverage and spamming context.

*Never let a health read as an absence.* A span whose health is not `complete`
renders a line naming that health and its reason codes, whatever its match
state. `denied` and `unavailable` are answers about the boundary, and rendering
them as silence would turn a restricted or unproven answer into a false `none`
-- exactly the failure §11.6.3 forbids.

Omitted and truncated counts are stated on every operation, including when they
are zero, so a reader never has to infer from silence that nothing was clipped.

Nothing here reads a clock, and nothing here decides anything: every value
printed is already in the result.

One line that renders no result
-------------------------------
:func:`render_unavailable_note` is the exception that proves the rule. An
adapter that fails open has no `CoverageResultV1` to render -- that is what
failing open means -- and it still owes the reader one line saying so, because
silence would let an infrastructure outage read as "nothing here is governed,"
which is the §11.6.3 false-`none` failure arriving through the back door. It
lives here rather than in the adapter that needs it so that every byte of
rendered coverage has exactly one spelling, and an adapter can be held to the
stronger law that it emits nothing this module did not produce.
"""

from __future__ import annotations

from typing import Literal

from cruxible_core.playbill.coverage.contracts import (
    CoverageCardV1,
    CoverageCardV2,
    CoverageResultAny,
    CoverageSpanResultV1,
    CoverageSpanResultV2,
    CoverageSpanResultV3,
    LogicalSourceIdentityV1,
)

BATCH_SUMMARY_PREFIX = "Playbill coverage:"
MANIFEST_SUMMARY_PREFIX = "Playbill coverage manifest:"
UNAVAILABLE_NOTE_PREFIX = "Playbill coverage: unavailable"

# The closed set of reasons an adapter may fail open. Deterministic by
# construction: a note carries the class of failure and never an exception
# string, so the same stdin against the same state renders the same byte.
CoverageUnavailableCodeV1 = Literal[
    "working_source_unreadable",
    "coverage_operation_unavailable",
]


def source_label(source: LogicalSourceIdentityV1) -> str:
    """Name a logical source the way every coverage line names it."""

    return f"{source.plane}:{source.identity}"


def _reasons(codes: tuple[str, ...]) -> str:
    return "" if not codes else "  [" + " ".join(codes) + "]"


def render_card(card: CoverageCardV1 | CoverageCardV2) -> str:
    """Render one card as a single greppable line.

    A drift line carries the whole §11.6.2 binding -- accepted Claim addresses,
    accepted coordinate, expected commitment, observed commitment, source
    identity, dereference handle, and the bounded dependent count -- because
    that binding is the card's entire content and abbreviating it would leave a
    reader unable to check the claim it is making.
    """

    parts = [card.match_state, source_label(card.observed_source)]
    if card.line_overlay is not None:
        parts.append(f"lines {card.line_overlay.start_line}-{card.line_overlay.end_line}")
    if card.match_state == "drifted":
        parts.append(f"expected {card.expected_commitment_digest}")
        parts.append(f"observed {card.observed_commitment_digest}")
    else:
        parts.append(f"commitment {card.expected_commitment_digest}")
    if card.match_basis is not None:
        parts.append(f"basis {card.match_basis}")
    if card.claim_addresses:
        parts.append("claims " + " ".join(item.artifact_path for item in card.claim_addresses))
    if card.capture_digests:
        parts.append("captures " + " ".join(card.capture_digests))
    if isinstance(card, CoverageCardV2) and card.citation_associations:
        if card.is_self_published_copy:
            parts.append("published copy")
        parts.append(
            "citations "
            + " ".join(item.reference.citation_id for item in card.citation_associations)
        )
        parts.append(
            "roles "
            + " ".join(
                (item.reference.role if hasattr(item.reference, "role") else "legacy_evidence")
                for item in card.citation_associations
            )
        )
        parts.append(
            "origins "
            + " ".join(
                (item.reference.origin if hasattr(item.reference, "origin") else "legacy")
                for item in card.citation_associations
            )
        )
        parts.append(
            "trust " + " ".join(item.observation_trust for item in card.citation_associations)
        )
        if card.is_self_published_copy:
            parts.append("not independent evidence")
    if card.dereference_handle_digest is not None:
        parts.append(f"handle {card.dereference_handle_digest}")
    parts.append(f"at generation {card.at.generation_root}")
    if card.dependent_claim_count is not None:
        parts.append(f"dependents {card.dependent_claim_count}")
    return "  ".join(parts) + _reasons(card.reason_codes)


def render_span(
    span: CoverageSpanResultV1 | CoverageSpanResultV2 | CoverageSpanResultV3,
) -> tuple[str, ...]:
    """Render one span: its cards, then whatever qualifies them.

    A `none` span inside a complete boundary renders nothing. That silence is
    the §11.6.4 rule, and it is safe only because the summary states the count
    and the boundary that makes the absence factual.
    """

    label = source_label(span.request.source)
    lines = [render_card(card) for card in span.cards]
    if span.health != "complete":
        lines.append(f"{span.health}  {label}" + _reasons(span.coverage.reason_codes))
    if span.ambiguous_occurrence_count:
        lines.append(
            f"ambiguous  {label}  "
            f"{span.ambiguous_occurrence_count} indistinguishable occurrence(s), none bound"
        )
    if span.omitted_card_count:
        lines.append(f"omitted  {label}  {span.omitted_card_count} card(s) clipped by budget")
    return tuple(lines)


def render_batch_summary(result: CoverageResultAny) -> tuple[str, ...]:
    """The one summary an operation emits, in the §11.6.4 shape."""

    summary = result.summary
    truncated = sum(1 for span in result.spans if span.coverage.truncated_facets)
    return (
        f"{BATCH_SUMMARY_PREFIX} {summary.exact} exact, {summary.drifted} drifted, "
        f"{summary.candidate} candidates, {summary.none} none",
        f"coverage {result.health} for {summary.returned_spans} returned spans "
        f"at generation {result.at.generation_root}",
        f"omitted cards: {summary.omitted_card_count}, truncated spans: {truncated}",
    )


def render_coverage_result(result: CoverageResultAny) -> tuple[str, ...]:
    """Render one whole coverage operation: annotations, then one summary."""

    lines: list[str] = []
    for span in result.spans:
        lines.extend(render_span(span))
    lines.extend(render_batch_summary(result))
    return tuple(lines)


def render_coverage_manifest(result: CoverageResultAny) -> tuple[str, ...]:
    """Render the manifest a coverage answer was resolved against.

    Epoch, health, completeness, and scope, plus the digests that make the
    answer checkable. The epoch is a counter and the scope is the boundary a
    `none` would have been factual inside; printing them together is what lets a
    reader judge an absence rather than take one on trust.
    """

    boundary = (
        "boundary complete"
        if result.health == "complete" and not result.coverage.truncated_facets
        else "boundary incomplete"
    )
    epoch = "absent" if result.epoch is None else str(result.epoch)
    lines = [
        f"{MANIFEST_SUMMARY_PREFIX} epoch {epoch}, health {result.health}, {boundary}",
        f"instance {result.instance_id} at generation {result.at.generation_root}",
        f"index {result.index_digest}",
        f"overlay {result.overlay_digest}",
        f"manifest {result.manifest_digest or 'absent'}",
        f"watcher {result.watcher_health}, access profile {result.access_profile.profile_id}",
        f"scope {len(result.scope)} source(s):",
    ]
    lines.extend(f"  {source_label(item)}" for item in result.scope)
    if result.coverage.reason_codes:
        lines.append("reasons  " + " ".join(result.coverage.reason_codes))
    return tuple(lines)


def render_unavailable_note(code: CoverageUnavailableCodeV1) -> tuple[str, ...]:
    """The one line an adapter that failed open is allowed to emit.

    It states a class of failure, never an exception message, so a reader learns
    that the answer is missing rather than that nothing is governed, and two runs
    over the same inputs still render the same bytes.
    """

    return (f"{UNAVAILABLE_NOTE_PREFIX}  [{code}]",)


__all__ = [
    "BATCH_SUMMARY_PREFIX",
    "MANIFEST_SUMMARY_PREFIX",
    "UNAVAILABLE_NOTE_PREFIX",
    "CoverageUnavailableCodeV1",
    "render_batch_summary",
    "render_card",
    "render_coverage_manifest",
    "render_coverage_result",
    "render_span",
    "render_unavailable_note",
    "source_label",
]

"""Evidence never comes from a projection window, and the DAEMON says so.

The two-block-kinds law was a client convention: a guard in the SDK, gated on
the `evidence` role, that any `copy` citation and any raw wire caller walked
past. These pin the daemon's own reading of it -- at lowering, where a claim
enters, and at the citation gate every proposal evaluation runs -- for every
role and every origin, with the cited capture's own bytes as the manifest of
its windows.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV1,
    SelfSourceBodyV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.candidates import CandidateRecordV3
from cruxible_client.contracts.captures import (
    DirectForeignSourceSelectionV1,
    build_foreign_source_capture,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    render_projection_opening,
)
from cruxible_client.contracts.projection import AcceptedCoordinate as ClientCoordinate
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_core.playbill.authoring import lowering
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import (
    _coordinator,
    _seed_claim_surface,
    _self_source_payload,
)
from tests.test_playbill.test_claim_citations import (
    PAGE_SOURCE_IDENTITY,
    _claim_citing,
    _submit,
    _type_naming,
)

SOURCE_ID = "repo.work-items"
TIMESTAMP = "2026-08-21T12:00:00.000000Z"
EVIDENCE_CODE = "playbill.projection.evidence_from_projection"
UNVERIFIABLE_CODE = "playbill.projection.window_unverifiable"

PROSE = b"status: ready\n"
BLOCK_BODY = b"projected: the block says the work is ready\n"


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _stamp(*, source_id: str = SOURCE_ID) -> ProjectionBlockStampV1:
    return ProjectionBlockStampV1(
        source_id=source_id,
        block_id="status",
        declared_generation=1,
        declared_coordinate=ClientCoordinate(
            git_oid="1" * 64,
            semantic_root="sha256:" + "2" * 64,
            generation_root="sha256:" + "3" * 64,
            compiler_digest="sha256:" + "4" * 64,
        ),
        backing=(
            ProjectionClaimBackingV1(
                identity=ArtifactIdentity(kind="Claim", name="CLM-" + "a" * 32),
                statement_digest="sha256:" + "5" * 64,
            ),
        ),
        body_digest=_digest(BLOCK_BODY),
    )


def _page(*, source_id: str = SOURCE_ID) -> bytes:
    """Authored prose, then one stamped projection block, then more prose."""

    return (
        PROSE
        + render_projection_opening(_stamp(source_id=source_id))
        + BLOCK_BODY
        + b"<!-- /playbill:block:status -->\n"
        + b"trailing prose the author also wrote\n"
    )


def _selection_payload(
    page: bytes,
    *,
    anchor: bytes,
    citation_role: str,
    present_page: bool = True,
) -> ClaimAuthoringPayloadV1:
    start = page.index(anchor)
    end = start + len(anchor)
    return ClaimAuthoringPayloadV1(
        statement=_self_source_payload().statement,
        rationale="The page says so.",
        source=WorkingSelectionObservationV1(
            source_id=SOURCE_ID,
            coordinate=WorkingDigestCoordinateV1(
                source_content_digest=_digest(page),
                source_byte_length=len(page),
            ),
            source_content_base64=(
                base64.b64encode(page).decode("ascii") if present_page else None
            ),
            selected_content_base64=base64.b64encode(page[start:end]).decode("ascii"),
            selected_bytes_digest=_digest(page[start:end]),
            selector=WorkingAnchorWindowV1(
                anchor=anchor.decode("utf-8").strip(),
                start_byte=start,
                end_byte=end,
                observed_occurrence_count=1,
            ),
        ),
        citation_role=citation_role,  # type: ignore[arg-type]
    )


def _world(tmp_path: Path) -> tuple[Any, AuthoringIntentCoordinator, AuthenticatedActor]:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner, contract=foreign_source_capture_contract(SOURCE_ID))
    return instance, _coordinator(instance), AuthenticatedActor(actor_id="owner")


def _preflight_codes(
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    payload: ClaimAuthoringPayloadV1,
) -> tuple[str, list[str]]:
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    result = coordinator.preflight(intent.intent_id, actor=actor)
    return result.verdict, [item.code for item in result.frontier.diagnostics]


@pytest.mark.parametrize("citation_role", ["evidence", "copy"])
def test_the_daemon_refuses_a_citation_inside_a_stamped_block_whatever_its_role(
    tmp_path: Path,
    citation_role: str,
) -> None:
    """The wire payload, no SDK: the role used to decide whether any guard ran."""

    _instance, coordinator, actor = _world(tmp_path)
    page = _page()

    verdict, codes = _preflight_codes(
        coordinator,
        actor,
        _selection_payload(page, anchor=b"the block says", citation_role=citation_role),
    )

    assert verdict == "refused"
    assert EVIDENCE_CODE in codes


@pytest.mark.parametrize("citation_role", ["evidence", "copy"])
def test_prose_outside_the_block_is_admitted_and_the_page_is_kept(
    tmp_path: Path,
    citation_role: str,
) -> None:
    """Self-source coverage of the author's own prose is the whole point of a page."""

    instance, coordinator, actor = _world(tmp_path)
    page = _page()

    verdict, codes = _preflight_codes(
        coordinator,
        actor,
        _selection_payload(page, anchor=b"status: ready", citation_role=citation_role),
    )

    assert verdict == "passed", codes
    assert EVIDENCE_CODE not in codes
    # The page is the manifest of its own windows: lowering keeps it so the
    # citation gate can read the windows back at every later evaluation.
    assert instance.body_store().verify(_digest(page))


def test_a_selection_that_spans_the_marker_refuses_too(tmp_path: Path) -> None:
    _instance, coordinator, actor = _world(tmp_path)
    page = _page()

    verdict, codes = _preflight_codes(
        coordinator,
        actor,
        _selection_payload(page, anchor=b"ready\n<!-- playbill", citation_role="evidence"),
    )

    assert verdict == "refused"
    assert EVIDENCE_CODE in codes


def test_a_self_source_body_that_carries_a_stamped_block_is_its_own_projection(
    tmp_path: Path,
) -> None:
    """A coordinator body is its own source, and a stamped window inside it is not evidence."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _self_source_payload().model_copy(
        update={"source": SelfSourceBodyV1(content_base64=base64.b64encode(_page()).decode())}
    )

    verdict, codes = _preflight_codes(coordinator, actor, payload)

    assert verdict == "refused"
    assert EVIDENCE_CODE in codes


def test_an_unprovable_span_into_a_source_that_registers_blocks_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the page the daemon cannot prove the span outside the windows.

    A raw wire caller that omits the page does not get the benefit of the
    doubt on a page the instance knows carries projection blocks; an unknown
    page still passes, because nothing registers a block in it.
    """

    instance, _coordinator_, actor = _world(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    page = _page()
    registered = (SimpleNamespace(preparation=SimpleNamespace(source_id=SOURCE_ID)),)
    monkeypatch.setattr(lowering, "bound_publication_registrations", lambda _instance: registered)

    verdict, codes = _preflight_codes(
        coordinator,
        actor,
        _selection_payload(
            page, anchor=b"status: ready", citation_role="evidence", present_page=False
        ),
    )
    assert verdict == "refused"
    assert UNVERIFIABLE_CODE in codes

    monkeypatch.setattr(lowering, "bound_publication_registrations", lambda _instance: None)
    verdict, codes = _preflight_codes(
        coordinator,
        actor,
        _selection_payload(
            page, anchor=b"trailing prose", citation_role="evidence", present_page=False
        ),
    )
    assert verdict == "refused"
    assert UNVERIFIABLE_CODE in codes

    monkeypatch.setattr(lowering, "bound_publication_registrations", lambda _instance: ())
    verdict, codes = _preflight_codes(
        coordinator,
        actor,
        _selection_payload(
            page, anchor=b"the author also", citation_role="evidence", present_page=False
        ),
    )
    assert verdict == "passed", codes


def _page_span_capture(instance: Any, base: Any, page: bytes, *, anchor: bytes) -> Any:
    stored = instance.body_store().store(page)
    start = page.index(anchor)
    return build_foreign_source_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id="CLM-0123456789abcdef0123456789abcdef",
        rationale="a span of the governed page",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        accepted_coordinate=AcceptedCoordinate.from_internal(base),
        selection=DirectForeignSourceSelectionV1(
            logical_source_identity=PAGE_SOURCE_IDENTITY,
            span=ContentSpan(
                content_digest=stored.digest,
                start_byte=start,
                end_byte=start + len(anchor),
            ),
        ),
    )


@pytest.mark.parametrize("role", ["evidence", "copy"])
def test_the_citation_gate_refuses_a_raw_candidate_citing_into_a_window(
    tmp_path: Path,
    role: str,
) -> None:
    """No SDK, no lowering: a hand-built Claim in a raw candidate tree.

    The gate resolves the span against the page the capture names, which the
    store holds, and reads the windows from it -- the capture is the manifest.
    """

    instance, _owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    contract = foreign_source_capture_contract(PAGE_SOURCE_IDENTITY)
    page = _page(source_id=PAGE_SOURCE_IDENTITY)
    claim_type = _type_naming(contract)

    inside = _claim_citing(
        _page_span_capture(instance, base, page, anchor=b"the block says"),
        contract=contract,
        claim_type=claim_type,
        role=role,
    )
    proposed = _submit(
        instance,
        _candidate_tree(instance, base, contract, claim_type, inside),
        base.git_oid,
        f"window-{role}",
        "2026-08-20T12:00:00.000000Z",
    )
    assert EVIDENCE_CODE in {item.code for item in proposed.evaluation.diagnostics}

    outside = _claim_citing(
        _page_span_capture(instance, base, page, anchor=b"status: ready"),
        contract=contract,
        claim_type=claim_type,
        role=role,
    )
    proposed = _submit(
        instance,
        _candidate_tree(instance, base, contract, claim_type, outside),
        base.git_oid,
        f"prose-{role}",
        "2026-08-20T12:00:00.000000Z",
    )
    assert proposed.evaluation.diagnostics == (), proposed.evaluation.diagnostics
    assert isinstance(proposed.candidate, CandidateRecordV3)
    # Un-windowed page prose under a declared contract still covers the Claim.
    evidence = _claim_law_evidence(proposed.candidate)
    assert evidence.initial_verdict == "supported"


def _candidate_tree(instance: Any, base: Any, contract: Any, claim_type: Any, claim: Any) -> dict:
    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from cruxible_client.contracts.claim_types import render_claim_type
    from cruxible_client.contracts.claims import render_claim
    from cruxible_client.contracts.subjects import render_subject, subject_path
    from tests.test_playbill.test_claims import _subject

    shell = _subject()
    return {
        **instance.tree_at(base.git_oid),
        subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
        "claim-types/project.work_item/status.json": render_claim_type(claim_type),
        capture_contract_path(contract.identity.name): render_capture_contract(contract),
        claim_path(claim.identity.name): render_claim(claim),
    }


def _claim_law_evidence(candidate: CandidateRecordV3) -> Any:
    from tests.test_playbill.test_claim_citations import _claim_evidence

    return _claim_evidence(candidate)


def test_lowered_claims_keep_citing_the_selection_not_the_page(tmp_path: Path) -> None:
    """Handing the page over changes what the daemon can PROVE, not what the Claim cites."""

    instance, coordinator, actor = _world(tmp_path)
    page = _page()
    intent = coordinator.create(
        actor=actor,
        payload=_selection_payload(page, anchor=b"status: ready", citation_role="evidence"),
        canonical_timestamp=TIMESTAMP,
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.candidate_digest is not None, submitted.status
    assert submitted.status.proposal_id is not None
    tree = instance.proposal_tree(
        instance.proposal_evidence()
        .read_evaluation(submitted.status.proposal_id)
        .evaluated_tree_oid
    )
    claim = parse_claim(
        tree[claim_path(intent.semantic_identity)], path=claim_path(intent.semantic_identity)
    )
    (mapping,) = claim.backing.source_mappings
    (span,) = mapping.spans
    assert span.content_digest == _digest(b"status: ready")
    assert (span.start_byte, span.end_byte) == (0, len(b"status: ready"))

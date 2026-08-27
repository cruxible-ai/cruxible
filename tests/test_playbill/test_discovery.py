"""Exact/lexical discovery, semantic expansion, and source dereference laws.

Discovery is deterministic and totally ordered by explicit match basis. Source
handles remain coordinate-bound and expose only committed selections with
explicit coverage for unavailable or denied material.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.discovery import (
    DiscoveryBudgetV1,
    DiscoveryHitV1,
    DiscoveryMatchBasis,
    DiscoveryMatchBasisV1,
    DiscoveryRequestV1,
    ExpandRequestV1,
    ExpansionBudgetV1,
)
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    EvidenceCommitmentV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
    OpenSourceRequestV1,
    SourceHandleV1,
    source_handle_digest,
)
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.dereference import dereference_source_handle
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.query.backends import subject_query_view
from cruxible_core.playbill.query.cards import InterfaceMatchBasisV1
from cruxible_core.playbill.query.semantic_discovery import (
    MATCH_BASIS_PRIORITY,
    MATCH_BASIS_RESOLVES_EQUIVALENCE,
    DiscoveryError,
    build_discovery_vocabulary,
    discover,
    discovery_vocabulary_digest,
    resolved_equivalence_address,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import service_expand_playbill_semantic
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_claim_query_engine import (
    claim_fact,
    coordinate,
    facts,
    reviewer_claim,
    status_claim,
    subject,
)
from tests.test_playbill.test_query_definitions import (
    REVIEWER_PREDICATE,
    STATUS_PREDICATE,
    TIMESTAMP,
    accepted_query,
    active_work_query,
    claim_type,
    single_status_query,
)

ALIAS_PREDICATE = "semantic.alias"
TAG_PREDICATE = "semantic.tag"
WI1_PATH = subject_path("project.work_item", "wi-1")
WI2_PATH = subject_path("project.work_item", "wi-2")
EVALUATION_TIME = "2026-08-16T12:00:00.000000Z"


# -- fixtures -------------------------------------------------------------


def _descriptor(index: int, item: str, predicate: str, value: str):
    return claim_fact(
        index,
        subject_row=subject("project.work_item", item),
        predicate=predicate,
        obj=LiteralClaimObject(value=value),
    )


def _facts():
    """Accepted facts with descriptor Claims and one Claim the view must omit."""

    return facts(
        (
            status_claim(1, "wi-1", "ready"),
            status_claim(2, "wi-2", "blocked"),
            reviewer_claim(3, "wi-1", "ada"),
            _descriptor(10, "wi-1", ALIAS_PREDICATE, "Ready Queue"),
            _descriptor(11, "wi-2", ALIAS_PREDICATE, "Ready Queue"),
            _descriptor(12, "wi-1", TAG_PREDICATE, "triage"),
        )
    )


def _claim_types():
    return (
        claim_type(STATUS_PREDICATE),
        claim_type(REVIEWER_PREDICATE, object_kind="subject"),
    )


def _definitions():
    return (accepted_query(active_work_query()), accepted_query(single_status_query()))


def _vocabulary(fact_rows=None):
    rows = _facts() if fact_rows is None else fact_rows
    return build_discovery_vocabulary(
        view=subject_query_view(rows),
        facts=rows,
        claim_types=_claim_types(),
        definitions=_definitions(),
    )


def _request(**overrides: object) -> DiscoveryRequestV1:
    fields: dict[str, object] = {
        "at": AcceptedCoordinate.from_internal(coordinate()),
        "evaluation_time": EVALUATION_TIME,
        "profile": "all",
    }
    fields.update(overrides)
    return DiscoveryRequestV1(**fields)  # type: ignore[arg-type]


# -- named entrypoints and compact handles --------------------------------


def test_accepted_query_definitions_are_discoverable_by_name_and_kind() -> None:
    vocabulary = _vocabulary()

    assert tuple(item.entrypoint_name for item in vocabulary.entrypoints()) == (
        "project.active_work",
        "project.work_item_status",
    )
    assert vocabulary.entrypoint("project.active_work") is not None
    assert vocabulary.entrypoint("project.absent") is None

    page = discover(_request(entrypoint="project.active_work"), vocabulary=vocabulary)
    assert tuple(hit.address.artifact_path for hit in page.hits) == (
        query_definition_path("project.active_work"),
    )
    hit = page.hits[0]
    assert tuple(item.basis for item in hit.match_basis) == ("named_entrypoint",)
    # A handle is a compact interface, never an expansion: no body, no evidence,
    # and no verdict, because nothing here was evaluated at a time.
    assert hit.verdict is None
    assert hit.currency == "not_applicable"
    assert hit.source_handles == ()
    assert claim_type_path(STATUS_PREDICATE) in {
        item.artifact_path for item in hit.dependency_addresses
    }

    interfaces = discover(
        _request(query="project.active_work", profile="interfaces"),
        vocabulary=vocabulary,
    )
    subjects = discover(
        _request(query="project.active_work", profile="subjects"),
        vocabulary=vocabulary,
    )
    assert {hit.kind for hit in interfaces.hits} <= {
        "ClaimType",
        "LineSpec",
        "Procedure",
        "QueryDefinition",
    }
    assert subjects.hits == ()
    assert subjects.coverage.requested_facets == ("Subject",)


# -- the closed match-basis set -------------------------------------------


def test_the_match_basis_set_is_closed_and_both_frozen_maps_are_total() -> None:
    closed = frozenset(get_args(DiscoveryMatchBasis))

    assert closed == {
        "content_equivalent",
        "dependency_walk",
        "exact_address",
        "exact_alias",
        "lexical",
        "named_entrypoint",
        "structural_signature",
        "tag",
    }
    assert frozenset(MATCH_BASIS_PRIORITY) == closed
    assert frozenset(MATCH_BASIS_RESOLVES_EQUIVALENCE) == closed
    # Priority is a total order over the closed set: two bases that tie would
    # make the page order depend on dictionary insertion rather than on law.
    assert len(set(MATCH_BASIS_PRIORITY.values())) == len(closed)
    assert [
        basis for basis, _ in sorted(MATCH_BASIS_PRIORITY.items(), key=lambda item: item[1])
    ] == [
        "exact_address",
        "named_entrypoint",
        "exact_alias",
        "content_equivalent",
        "structural_signature",
        "dependency_walk",
        "tag",
        "lexical",
    ]


def test_a_content_equivalent_match_never_resolves_equivalence() -> None:
    """`dd-match-basis-content-equivalent`: copied bytes are not identity.

    Priority ranks `content_equivalent` above every recall-only basis, so this
    is the law that keeps the rank from being read as a grant: it may never
    resolve an expression to a target, never merge two Subjects, and never
    satisfy the §6.3.1 identity resolution a reuse disposition turns on.
    """

    assert MATCH_BASIS_RESOLVES_EQUIVALENCE["content_equivalent"] is False
    assert MATCH_BASIS_PRIORITY["content_equivalent"] > MATCH_BASIS_PRIORITY["exact_address"]
    assert MATCH_BASIS_PRIORITY["content_equivalent"] > MATCH_BASIS_PRIORITY["named_entrypoint"]
    assert MATCH_BASIS_PRIORITY["content_equivalent"] > MATCH_BASIS_PRIORITY["exact_alias"]
    assert all(
        MATCH_BASIS_PRIORITY["content_equivalent"] < MATCH_BASIS_PRIORITY[basis]
        for basis, resolves in MATCH_BASIS_RESOLVES_EQUIVALENCE.items()
        if not resolves and basis != "content_equivalent"
    )

    vocabulary = _vocabulary()
    at = vocabulary.at
    hits = tuple(
        DiscoveryHitV1(
            address=SemanticAddress.whole_artifact(path),
            at=at,
            kind="Subject",
            label=path,
            match_basis=(DiscoveryMatchBasisV1(basis="content_equivalent", matched_text=None),),
            currency="not_applicable",
        )
        for path in (WI1_PATH, WI2_PATH)
    )
    single = discover(_request(query="wi-1"), vocabulary=vocabulary).model_copy(
        update={"hits": hits[:1]}
    )
    both = single.model_copy(update={"hits": hits})

    # Not even a lone content-equivalent hit resolves: an unambiguous page is
    # exactly where a weaker basis would otherwise be promoted by accident.
    assert resolved_equivalence_address(single) is None
    assert resolved_equivalence_address(both) is None

    # The card projection refuses to render the grade any other way.
    with pytest.raises(ValidationError, match="equivalence grade differs"):
        InterfaceMatchBasisV1(
            basis="content_equivalent",
            terms=("copied text",),
            resolves_equivalence=True,
        )
    assert (
        InterfaceMatchBasisV1(
            basis="content_equivalent",
            terms=("copied text",),
            resolves_equivalence=False,
        ).resolves_equivalence
        is False
    )


# -- ordering, ambiguity, and determinism ---------------------------------


def test_hits_are_ordered_by_basis_priority_then_kind_qualified_address_bytes() -> None:
    vocabulary = _vocabulary()
    page = discover(_request(query="Ready Queue"), vocabulary=vocabulary)

    assert [hit.address.artifact_path for hit in page.hits] == [WI1_PATH, WI2_PATH]
    assert all(
        tuple(item.basis for item in hit.match_basis) == ("exact_alias", "lexical")
        for hit in page.hits
    )
    # An alias that validly names several subjects is an ambiguity to surface,
    # never a choice the server makes silently.
    assert "alias_ambiguous" in page.coverage.reason_codes

    tagged = discover(_request(query="triage"), vocabulary=vocabulary)
    assert tuple(item.basis for item in tagged.hits[0].match_basis) == ("tag", "lexical")
    assert MATCH_BASIS_PRIORITY["tag"] > MATCH_BASIS_PRIORITY["exact_alias"]
    assert MATCH_BASIS_PRIORITY["lexical"] > MATCH_BASIS_PRIORITY["exact_address"]


def test_the_same_coordinate_and_request_yield_byte_identical_pages() -> None:
    vocabulary = _vocabulary()
    request = _request(query="project.work_item.status")

    first = discover(request, vocabulary=vocabulary)
    second = discover(request, vocabulary=_vocabulary())
    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )
    assert first.selection_basis_digest == second.selection_basis_digest
    assert discovery_vocabulary_digest(vocabulary) == discovery_vocabulary_digest(_vocabulary())


def test_an_exact_address_outranks_every_lexical_and_tag_match() -> None:
    vocabulary = _vocabulary()
    page = discover(_request(query=WI1_PATH), vocabulary=vocabulary)

    assert page.hits[0].address.artifact_path == WI1_PATH
    assert page.hits[0].match_basis[0].basis == "exact_address"
    for hit in page.hits[1:]:
        assert MATCH_BASIS_PRIORITY[hit.match_basis[0].basis] > 0


def test_discovery_refuses_foreign_coordinates_blank_queries_and_locator_text() -> None:
    vocabulary = _vocabulary()
    foreign = AcceptedCoordinate.from_internal(coordinate(generation="33"))

    with pytest.raises(DiscoveryError, match="differs from the built vocabulary"):
        discover(_request(query="wi-1", at=foreign), vocabulary=vocabulary)
    with pytest.raises(DiscoveryError, match="blank discovery query"):
        discover(_request(query="   "), vocabulary=vocabulary)
    with pytest.raises(ValueError, match="forbidden locator"):
        discover(_request(query="https://internal.example/wi-1"), vocabulary=vocabulary)


# -- budgets --------------------------------------------------------------


def test_budgets_clip_the_low_priority_tail_and_always_state_the_clip() -> None:
    vocabulary = _vocabulary()
    full = discover(_request(query="Ready Queue"), vocabulary=vocabulary)
    assert len(full.hits) == 2

    clipped = discover(
        _request(query="Ready Queue", budget=DiscoveryBudgetV1(max_hits=1)),
        vocabulary=vocabulary,
    )
    assert [hit.address.artifact_path for hit in clipped.hits] == [WI1_PATH]
    assert clipped.coverage.truncated_facets == ("hits",)
    assert "hit_budget_exceeded" in clipped.coverage.reason_codes

    starved = discover(
        _request(query="Ready Queue", budget=DiscoveryBudgetV1(max_bytes=1)),
        vocabulary=vocabulary,
    )
    assert starved.hits == ()
    assert starved.coverage.truncated_facets == ("hits",)
    assert "byte_budget_exceeded" in starved.coverage.reason_codes


# -- the F3 exclusion law -------------------------------------------------


def test_discovery_counts_claims_the_materialized_view_deliberately_omits() -> None:
    # F3 law (b): a Claim whose statement Subject is absent is not materialized,
    # so a count taken from the view alone would understate what was read.
    rows = _facts()
    unresolved = subject("project.work_item", "wi-9")
    orphan = claim_fact(
        20,
        subject_row=unresolved,
        predicate=ALIAS_PREDICATE,
        obj=LiteralClaimObject(value="Orphaned"),
    )
    with_orphan = rows.model_copy(
        update={
            "claims": tuple(
                sorted(
                    (*rows.claims, orphan),
                    key=lambda item: item.accepted.path.encode("utf-8"),
                )
            )
        }
    )
    view = subject_query_view(with_orphan)
    assert unresolved.path not in {row.path for row in view.subjects}
    assert orphan.accepted.path not in {row.claim_path for row in view.claims}

    vocabulary = build_discovery_vocabulary(view=view, facts=with_orphan)
    assert vocabulary.excluded_claim_count == 1
    assert not discover(_request(query="Orphaned"), vocabulary=vocabulary).hits


# -- dereference ----------------------------------------------------------


class _FakeResolver:
    """Deterministic material seam: retained bytes and exact external selections."""

    def __init__(
        self,
        *,
        ledger: dict[str, bytes] | None = None,
        cas: dict[str, bytes] | None = None,
        external: dict[bytes, object] | None = None,
    ) -> None:
        self.ledger = ledger or {}
        self.cas = cas or {}
        self.external = external or {}

    def read_ledger(self, artifact_path: str) -> bytes | None:
        return self.ledger.get(artifact_path)

    def read_cas(self, content_digest: str, *, access: BodyAccessContext) -> bytes | None:
        return self.cas.get(content_digest)

    def read_external(self, source: ExternalSourceReferenceV1) -> object | None:
        return self.external.get(canonical_bytes(source.model_dump(mode="json")))


def _cas_handle(
    content: bytes, *, spans: tuple[ContentSpan, ...] = (), access_class: str = "instance"
):
    digest = Sha256Value(hashlib.sha256(content).hexdigest()).tagged
    return SourceHandleV1(
        subject=SemanticAddress.whole_artifact(WI1_PATH),
        at=AcceptedCoordinate.from_internal(coordinate()),
        source=CasSourceReferenceV1(content_digest=digest),
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=digest,
            byte_length=len(content),
            materialization="cas",
        ),
        media_type="text/markdown",
        exact_spans=spans,
        access_class=access_class,  # type: ignore[arg-type]
    )


def test_byte_span_dereference_returns_only_the_committed_selection() -> None:
    body = b"# Work item\n\nStatus is ready.\n\n- triage\n"
    paragraph_start = body.index(b"Status is ready.")
    paragraph_end = paragraph_start + len(b"Status is ready.")
    handle = _cas_handle(
        body,
        spans=(
            ContentSpan(
                content_digest=Sha256Value(hashlib.sha256(body).hexdigest()).tagged,
                start_byte=paragraph_start,
                end_byte=paragraph_end,
            ),
        ),
    )

    result = dereference_source_handle(
        OpenSourceRequestV1(source_handle=handle, resource_budget_bytes=4096),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
        resolver=_FakeResolver(cas={handle.source.content_digest: body}),
    )

    assert result.status == "verified"
    assert result.commitment_verified is True
    assert result.body_access is not None
    import base64

    selected = base64.b64decode(result.body_access.body_base64)
    assert selected == body[paragraph_start:paragraph_end]
    assert selected != body
    assert result.coverage.reason_codes == ("exact_span_selection",)
    assert result.source_handle_digest == source_handle_digest(handle)


def test_ledger_dereference_verifies_the_accepted_artifact_bytes() -> None:
    body = b'{"artifact_format":"playbill-subject-v1"}\n'
    digest = Sha256Value(hashlib.sha256(body).hexdigest()).tagged
    handle = SourceHandleV1(
        subject=SemanticAddress.whole_artifact(WI1_PATH),
        at=AcceptedCoordinate.from_internal(coordinate()),
        source=LedgerSourceReferenceV1(
            address=SemanticAddress.whole_artifact(WI1_PATH),
            coordinate=AcceptedCoordinate.from_internal(coordinate()),
        ),
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=digest,
            byte_length=len(body),
            materialization="ledger",
        ),
        access_class="instance",
    )
    request = OpenSourceRequestV1(source_handle=handle, resource_budget_bytes=4096)
    access = BodyAccessContext(principal_id="owner", can_read_body=True)

    verified = dereference_source_handle(
        request,
        access=access,
        resolver=_FakeResolver(ledger={WI1_PATH: body}),
    )
    assert verified.status == "verified"
    assert verified.observed_commitment_digest == digest

    drifted = dereference_source_handle(
        request,
        access=access,
        resolver=_FakeResolver(ledger={WI1_PATH: body + b"drift"}),
    )
    assert drifted.status == "drifted"
    assert drifted.commitment_verified is False
    assert drifted.observed_commitment_digest != digest

    missing = dereference_source_handle(request, access=access, resolver=_FakeResolver())
    assert missing.status == "unavailable"
    assert missing.material_kind == "metadata_only"
    assert missing.coverage.reason_codes == ("body_unavailable",)


def _external_handle(*, replayability: str, digest_kind: str, digest: str):
    source = ExternalSourceReferenceV1(
        source_identity="orders.primary",
        producer_binding_digest="sha256:" + "11" * 32,
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "0/16B6C50"},
        selector_type="relation-primary-key-v1",
        selector={"key": {"order_id": "ord-482"}, "relation": "orders"},
        replayability=replayability,  # type: ignore[arg-type]
    )
    return SourceHandleV1(
        subject=SemanticAddress.whole_artifact(WI1_PATH),
        at=AcceptedCoordinate.from_internal(coordinate()),
        source=source,
        commitment=EvidenceCommitmentV1(
            digest_kind=digest_kind,  # type: ignore[arg-type]
            digest=digest,
            materialization="external" if replayability == "exact" else "none",
        ),
        access_class="instance",
    )


def test_external_record_and_query_dereference_state_exact_coverage() -> None:
    record = {"order_id": "ord-482", "status": "shipped"}
    digest = Sha256Value(hashlib.sha256(canonical_bytes(record)).hexdigest()).tagged
    handle = _external_handle(
        replayability="exact",
        digest_kind="canonical_value",
        digest=digest,
    )
    key = canonical_bytes(handle.source.model_dump(mode="json"))
    request = OpenSourceRequestV1(source_handle=handle, resource_budget_bytes=4096)
    access = BodyAccessContext(principal_id="owner", can_read_body=True)

    verified = dereference_source_handle(
        request,
        access=access,
        resolver=_FakeResolver(external={key: record}),
    )
    assert verified.status == "verified"
    assert verified.material_kind == "canonical_value"
    assert verified.canonical_material == record
    assert verified.coverage.reason_codes == ("external_exact_replay",)

    drifted = dereference_source_handle(
        request,
        access=access,
        resolver=_FakeResolver(external={key: {**record, "status": "cancelled"}}),
    )
    assert drifted.status == "drifted"
    assert drifted.observed_commitment_digest != digest

    retired = dereference_source_handle(request, access=access, resolver=_FakeResolver())
    assert retired.status == "unavailable"
    assert retired.coverage.reason_codes == ("external_version_retired",)

    query_handle = _external_handle(
        replayability="exact",
        digest_kind="query_result",
        digest=digest,
    )
    query_result = dereference_source_handle(
        OpenSourceRequestV1(source_handle=query_handle, resource_budget_bytes=4096),
        access=access,
        resolver=_FakeResolver(
            external={canonical_bytes(query_handle.source.model_dump(mode="json")): record}
        ),
    )
    assert query_result.material_kind == "query_result"


def test_attested_only_unavailable_and_denied_return_metadata_with_coverage() -> None:
    attested = _external_handle(
        replayability="attested_only",
        digest_kind="canonical_value",
        digest="sha256:" + "22" * 32,
    )
    result = dereference_source_handle(
        OpenSourceRequestV1(source_handle=attested, resource_budget_bytes=4096),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
        resolver=_FakeResolver(),
    )
    assert result.status == "attested_only"
    assert result.material_kind == "metadata_only"
    assert result.canonical_material is None
    assert result.coverage.reason_codes == ("external_attested_only",)

    body = b"status: ready\n"
    handle = _cas_handle(body)
    resolver = _FakeResolver(cas={handle.source.content_digest: body})
    denied = dereference_source_handle(
        OpenSourceRequestV1(source_handle=handle, resource_budget_bytes=4096),
        access=BodyAccessContext(principal_id="reader", can_read_body=False),
        resolver=resolver,
    )
    assert denied.status == "denied"
    assert denied.coverage.omitted_for_access == ("source_material",)
    # A caller who may see the handle but not its bytes still gets its identity.
    assert denied.source_handle_digest == source_handle_digest(handle)

    restricted = _cas_handle(body, access_class="restricted")
    refused = dereference_source_handle(
        OpenSourceRequestV1(source_handle=restricted, resource_budget_bytes=4096),
        access=BodyAccessContext(principal_id="reader", can_read_body=False),
        resolver=resolver,
    )
    assert refused.status == "denied"
    assert refused.coverage.reason_codes == ("restricted_access_class",)

    starved = dereference_source_handle(
        OpenSourceRequestV1(
            source_handle=handle,
            structural_context_bytes=4,
            resource_budget_bytes=8,
        ),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
        resolver=resolver,
    )
    assert starved.status == "unavailable"
    assert starved.coverage.truncated_facets == ("source_material",)
    assert starved.coverage.reason_codes == ("resource_budget_exceeded",)


# -- expand generalized to a discovery handle ------------------------------


def _sign(material, candidate_digest: str, parent_root: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from cruxible_client.contracts.attestations import (
        ApprovalAttestation,
        ApprovalStatement,
        ApprovalSubmission,
        approval_statement_bytes,
    )

    private = serialization.load_ssh_private_key(
        material.private_key_path.read_bytes(),
        password=None,
    )
    assert isinstance(private, Ed25519PrivateKey)
    statement = ApprovalStatement(
        signer_id=material.principal.principal_id,
        signing_semantic_root=parent_root,
        payload_digest=candidate_digest,
    )
    return ApprovalSubmission(
        submitted_by="approval-relay",
        attestation=ApprovalAttestation(
            **statement.model_dump(),
            sig=private.sign(approval_statement_bytes(statement)).hex(),
        ),
    )


def _accept(instance, owner, tree: dict[str, bytes]) -> None:
    from cruxible_core.playbill.settlement import ChangeActorBinding

    base = instance.accepted_coordinate()
    submitted = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/discovery",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    candidate = submitted.candidate
    assert candidate is not None, submitted.evaluation.diagnostics
    evaluated_oid = submitted.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=1,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def test_expand_generalizes_to_a_named_query_definition_handle(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    query = single_status_query()
    path = query_definition_path(query.identity.name)
    _accept(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            claim_type_path(STATUS_PREDICATE): render_claim_type(claim_type(STATUS_PREDICATE)),
            path: render_query_definition(query),
        },
    )
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    capsule = service_expand_playbill_semantic(
        instance,
        request=ExpandRequestV1(
            address=SemanticAddress.whole_artifact(path),
            at=accepted,
            evaluation_time=TIMESTAMP,
            facets=("governance", "provenance", "summary"),
            budget=ExpansionBudgetV1(max_bytes=8192),
        ),
    )
    summary = capsule.canonical_summary
    assert isinstance(summary, dict)
    assert summary["entrypoint_name"] == "project.work_item_status"
    assert summary["result_cardinality"] == "one"
    assert summary["referenced_predicates"] == [STATUS_PREDICATE]
    assert capsule.at == accepted
    assert capsule.coverage.available_facets == ("governance", "provenance", "summary")

    # PC-B's exact-identity law still holds for the generalized entry.
    with pytest.raises(ProposalIntegrityError, match="whole-artifact identity"):
        service_expand_playbill_semantic(
            instance,
            request=ExpandRequestV1(
                address=SemanticAddress.claim_statement(path),
                at=accepted,
                evaluation_time=TIMESTAMP,
                facets=("summary",),
            ),
        )


def test_a_discovery_entry_kind_outside_the_closed_set_is_refused() -> None:
    from cruxible_core.playbill.query.semantic_discovery import DiscoveryEntryV1

    with pytest.raises(ValueError, match="unknown discovery entry kind"):
        DiscoveryEntryV1(
            kind="Document",
            address=SemanticAddress.whole_artifact(WI1_PATH),
            identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-1").qualified,
            label="wi-1",
        )

"""Typed citation publication and scoped reads reproduce the frozen cold oracle."""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    FOREIGN_SOURCE_SELECTOR_TYPE,
    CaptureEnvelopeV1,
    CaptureRunCoordinateV1,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    build_claim_citation,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import PlaybillCasError, ProjectionFormatError
from cruxible_client.contracts.source_references import (
    EvidenceCommitmentV1,
    ExternalSourceReferenceV1,
)
from cruxible_core.coverage.indexes import (
    evidence_citation_index_digest,
)
from cruxible_core.evidence.citation_relations import (
    RELATION_RETIRED_CONFLICT_SCHEMA,
    _same_version_span_key,
)
from cruxible_core.indexes.evidence import citation_sql
from cruxible_core.indexes.evidence.citation_coverage import coverage_rows
from cruxible_core.indexes.evidence.citation_sql import (
    SCHEMA_SQL,
    CitationReader,
    populate_citations,
    remove_owner_citations,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.storage.cas import ContentAddressedBodyStore
from tests.core_support._citation_index_oracle import (
    CaptureCitationInputV1,
    CaptureCitationInputV2,
    build_evidence_citation_index,
    build_evidence_citation_index_v2,
)
from tests.core_support._citation_relations_oracle import build_citation_relation_facts
from tests.core_support._pc_c_support import capture_contract
from tests.test_claims.test_claims import _claim

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
DIGEST = "sha256:" + "1" * 64
AT = AcceptedCoordinate(
    git_oid="1" * 40,
    semantic_root=DIGEST,
    generation_root=DIGEST,
    compiler_digest=DIGEST,
)


@dataclass
class World:
    connection: sqlite3.Connection
    store: ContentAddressedBodyStore
    sources: dict[str, bytes] = field(default_factory=dict)
    envelopes: dict[str, CaptureEnvelopeV1] = field(default_factory=dict)

    @property
    def reader(self):
        return CitationReader(self.connection, self.sources.__getitem__)

    def capture(self, n, *, source=0, start=0, end=10, binding=DIGEST, byte_length=10):
        contract = capture_contract()
        envelope = CaptureEnvelopeV1(
            capture_contract_digest=capture_contract_digest(contract).tagged,
            source=ExternalSourceReferenceV1(
                source_identity=f"source-{source}",
                producer_binding_digest=binding,
                coordinate_type=FOREIGN_SOURCE_COORDINATE_TYPE,
                coordinate={"version": 1},
                selector_type=FOREIGN_SOURCE_SELECTOR_TYPE,
                selector={"start_byte": start, "end_byte": end},
                replayability="exact",
            ),
            commitment=EvidenceCommitmentV1(
                digest_kind="exact_bytes",
                digest=DIGEST,
                byte_length=byte_length,
                materialization="external",
            ),
            run_coordinate=CaptureRunCoordinateV1(
                run_kind="provider",
                run_id=f"run-{n}",
                bound_generation=DIGEST,
                executable_identity=ArtifactIdentity(kind="Provider", name="test.provider"),
                executable_digest=DIGEST,
            ),
            run_receipt_digest=DIGEST,
            producer=ArtifactIdentity(kind="Provider", name="test.provider"),
            producer_binding_digest=binding,
            observed_at=NOW,
        )
        digest = self.store.store(canonical_bytes(envelope.model_dump(mode="json"))).digest
        self.envelopes[digest] = envelope
        return digest

    def claim(self, n, captures, *, retired=False, roles=("evidence",)):
        identity = ArtifactIdentity(kind="Claim", name=f"CLM-{n:032x}")
        value = _claim(
            claim_id=identity.name,
            capture_digest=captures[0],
            source_digest=DIGEST,
            source_length=10,
        )
        citations = tuple(
            sorted(
                (
                    build_claim_citation(
                        identity, capture_digest=digest, role=role, origin="independent"
                    )
                    for digest in captures
                    for role in roles
                ),
                key=lambda item: item.citation_id,
            )
        )
        return value.model_copy(
            update={
                "backing": value.backing.model_copy(
                    update={
                        "capture_digests": tuple(sorted(set(captures))),
                        "citations": citations,
                    }
                ),
                "lifecycle": ArtifactLifecycle(state="retired" if retired else "live"),
            }
        )

    def publish(self, *claims):
        changed = {}
        for claim in claims:
            path = claim_path(claim.identity.name)
            content = render_claim(claim)
            # Parse the actual source before test SQL, like the accepted compiler.
            parse_claim(content, path=path)
            self.connection.execute(
                "INSERT OR REPLACE INTO claims VALUES (?,?,?,?)",
                (
                    claim.identity.qualified,
                    path,
                    claim_artifact_digest(claim).tagged,
                    claim.lifecycle.state,
                ),
            )
            changed[path] = content
        populate_citations(
            self.connection,
            changed,
            bodies=self.store,
            owner_exists=lambda kind, key: (
                kind == "Claim"
                and self.connection.execute(
                    "SELECT 1 FROM claims WHERE identity=?",
                    (key,),
                ).fetchone()
                is not None
            ),
        )
        self.sources.update(changed)

    def remove(self, claim):
        remove_owner_citations(self.connection, [("Claim", claim.identity.qualified)])
        self.connection.execute("DELETE FROM claims WHERE identity=?", (claim.identity.qualified,))
        self.sources.pop(claim_path(claim.identity.name), None)

    def cold_conflicts(self):
        return tuple(
            sorted(
                (
                    f
                    for f in build_citation_relation_facts(
                        self.sources,
                        bodies=self.store,
                    )
                    if f.schema_id == RELATION_RETIRED_CONFLICT_SCHEMA
                ),
                key=lambda f: f.fact_key,
            )
        )


@pytest.fixture
def world(tmp_path: Path):
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        "CREATE TABLE claims(identity TEXT PRIMARY KEY,path TEXT,"
        "artifact_digest TEXT,lifecycle TEXT) STRICT;"
        "CREATE INDEX claims_by_lifecycle ON claims(lifecycle,identity);"
        "CREATE TABLE capture_contracts(identity TEXT PRIMARY KEY,path TEXT,"
        "artifact_digest TEXT) STRICT;" + SCHEMA_SQL
    )
    root = tmp_path / "cas"
    root.mkdir()
    value = World(connection, ContentAddressedBodyStore(root))
    contract = capture_contract()
    path = capture_contract_path(contract.identity.name)
    value.sources[path] = render_capture_contract(contract)
    connection.execute(
        "INSERT INTO capture_contracts VALUES (?,?,?)",
        (
            contract.identity.qualified,
            path,
            capture_contract_digest(contract).tagged,
        ),
    )
    yield value
    connection.close()


def test_precedence_removal_restores_untouched_weaker_groups(world):
    a, b, c = world.capture(1, source=1), world.capture(2, source=2), world.capture(3, source=2)
    live = world.claim(1, [a, b])
    retired = world.claim(2, [a], retired=True)
    other = world.claim(3, [c], retired=True)
    world.publish(live, retired, other)
    before = world.reader.conflicts()
    assert before == world.cold_conflicts()
    assert {f.value["relation_kind"] for f in before} == {"capture"}
    world.remove(retired)
    after = world.reader.conflicts(claim_identities=[live.identity.qualified])
    assert after == world.cold_conflicts()
    assert {f.value["relation_kind"] for f in after} == {"exact_external", "same_version_span"}
    assert world.reader.owners_for_capture(a)  # live still retains the shared Capture


def test_randomized_old_new_memberships_and_owner_replacement_match_cold(world):
    rng = random.Random(8097)
    captures = [world.capture(n, source=n % 4, start=n % 8, end=n % 8 + 4) for n in range(12)]
    claims = {n: world.claim(n, [captures[n % 12]], retired=n % 5 == 0) for n in range(1, 31)}
    world.publish(*claims.values())
    for _ in range(40):
        n = rng.randrange(1, 31)
        if rng.randrange(5) == 0:
            old = claims.pop(n, None)
            if old is not None:
                world.remove(old)
        else:
            claims[n] = world.claim(
                n, rng.sample(captures, rng.randrange(1, 4)), retired=bool(rng.randrange(2))
            )
            world.publish(claims[n])
        assert world.reader.conflicts() == world.cold_conflicts()


def test_roles_witness_bounds_and_half_open_spans(world):
    first = world.capture(1, source=1)
    touching = world.capture(2, source=1, start=10, end=20)
    live = world.claim(1, [first], roles=("evidence", "copy"))
    world.publish(
        live,
        world.claim(2, [touching]),
        *(world.claim(n, [first], retired=True) for n in range(10, 22)),
    )
    assert len(world.reader.owner_uses("Claim", live.identity.qualified)) == 2
    facts = world.reader.conflicts()
    assert facts == world.cold_conflicts()
    assert len(facts) == 1
    assert facts[0].value["retired_claim_count"] == 12
    assert len(facts[0].value["retired_claim_witnesses"]) == 8


def test_indexed_spans_filter_ends_and_retain_arbitrary_precision_candidates(world):
    spans = [
        (0, 2),
        (1, 12),
        (10, 15),
        (1 << 70, (1 << 70) + 10),
        ((1 << 70) + 5, (1 << 70) + 15),
    ]
    digests = [world.capture(n, start=start, end=end) for n, (start, end) in enumerate(spans)]
    world.publish(
        *(world.claim(n + 1, [digest], retired=n == 3) for n, digest in enumerate(digests))
    )
    assert world.reader.conflicts() == world.cold_conflicts()
    version = _same_version_span_key(
        {"source": world.envelopes[digests[0]].source.model_dump(mode="json")}
    )[0]
    for start, end in [(2, 10), (9, 11), ((1 << 70) + 1, (1 << 70) + 2)]:
        result = world.reader.overlapping_uses(version, start, end, bodies=world.store)
        assert {u["capture_digest"]["$digest"] for u in result} == {
            d for d, (a, b) in zip(digests, spans) if a < end and start < b
        }
    row = world.connection.execute(
        "SELECT start_byte,end_byte,selector_type,source_version_key FROM source_references "
        "WHERE start_byte IS NULL"
    ).fetchone()
    assert row == (None, None, FOREIGN_SOURCE_SELECTOR_TYPE, version)
    for invalid in (True, 1.5, "1"):
        with pytest.raises(ValueError, match="exact integers"):
            world.reader.overlapping_uses(version, invalid, 10, bodies=world.store)
    expected = world.cold_conflicts()
    world.store._path(digests[3]).unlink()
    assert world.reader.conflicts() == expected


def test_failed_cas_population_rolls_back_relationships_and_does_not_commit_outer_transaction(
    world,
):
    digest = world.capture(1)
    claim = world.claim(1, [digest])
    world.publish(claim)
    before = world.reader.owner_uses("Claim", claim.identity.qualified)
    missing = world.capture(2)
    world.store._path(missing).unlink()
    changed = world.claim(1, [digest, missing])
    with pytest.raises(PlaybillCasError):
        world.publish(changed)
    assert world.reader.owner_uses("Claim", claim.identity.qualified) == before
    assert world.connection.in_transaction
    with pytest.raises(ProjectionFormatError, match="owner"):
        populate_citations(
            world.connection,
            {claim_path(claim.identity.name): render_claim(claim)},
            bodies=world.store,
            owner_exists=lambda *_: False,
        )


def test_coverage_exports_match_frozen_versions_and_recheck_availability(world):
    a, b = world.capture(1, source=1), world.capture(2, source=1)
    claims = [world.claim(1, [a], roles=("evidence", "copy")), world.claim(2, [b], retired=True)]
    world.publish(*claims)
    accepted = tuple(
        AcceptedClaim(
            claim=claim,
            path=claim_path(claim.identity.name),
            statement_digest=claim_statement_digest(claim.statement).tagged,
            artifact_digest=claim_artifact_digest(claim).tagged,
        )
        for claim in claims
    )
    for version in (1, 2):
        sql, _envelopes = coverage_rows(world.reader, bodies=world.store, at=AT, version=version)
        inputs = dict(at=AT, claims=accepted)
        if version == 1:
            cold = build_evidence_citation_index(
                **inputs,
                captures=tuple(
                    CaptureCitationInputV1(capture_digest=d, envelope=world.envelopes[d])
                    for d in sorted((a, b))
                ),
            )
        else:
            cold = build_evidence_citation_index_v2(
                **inputs,
                captures=tuple(
                    CaptureCitationInputV2(
                        capture_digest=d,
                        envelope=world.envelopes[d],
                        observation_trust="daemon_fetched",
                    )
                    for d in sorted((a, b))
                ),
            )
        assert sql == cold
        assert evidence_citation_index_digest(sql) == evidence_citation_index_digest(cold)
    world.store._path(a).unlink()
    with pytest.raises(PlaybillCasError):
        coverage_rows(world.reader, bodies=world.store, at=AT)


@pytest.mark.parametrize("unrelated", [0, 100, 500])
def test_selected_group_work_is_independent_of_unrelated_citation_growth(
    world, monkeypatch, unrelated
):
    digest = world.capture(1)
    live, retired = world.claim(1, [digest]), world.claim(2, [digest], retired=True)
    world.publish(
        live,
        retired,
        *(world.claim(n + 10, [world.capture(n + 10, source=n + 10)]) for n in range(unrelated)),
    )
    publication_reads = []
    original_insert = citation_sql._insert_capture

    def counted_insert(connection, capture, bodies):
        publication_reads.append(capture)
        return original_insert(connection, capture, bodies)

    monkeypatch.setattr(citation_sql, "_insert_capture", counted_insert)
    world.publish(live)
    assert publication_reads == [digest]
    visited = []
    original = citation_sql._conflict_group_facts

    def count(captures, external, spans):
        visited.append(
            sum(len(group) for groups in (captures, external, spans) for group in groups.values())
        )
        return original(captures, external, spans)

    monkeypatch.setattr(citation_sql, "_conflict_group_facts", count)
    assert world.reader.conflicts(claim_identities=[live.identity.qualified])
    assert visited == [2, 2, 2]


def _global_conflicts_with_sql_steps(world):
    steps = 0

    def count():
        nonlocal steps
        steps += 1
        return 0

    world.connection.set_progress_handler(count, 1)
    try:
        # This is next's unfiltered production entry point, including the SQL
        # that discovers relevant groups before the shared kernel sees them.
        facts = world.reader.conflicts()
    finally:
        world.connection.set_progress_handler(None, 0)
    return facts, steps


def test_global_conflict_sql_ignores_unrelated_live_citations(world, record_property):
    digest = world.capture(1)
    world.publish(world.claim(1, [digest], retired=True), world.claim(10_000, [digest]))
    expected = world.cold_conflicts()
    counts = []
    previous = 0
    for unrelated in (0, 100, 500):
        world.publish(
            *(
                world.claim(n + 20_000, [world.capture(n + 20, source=n + 20)])
                for n in range(previous, unrelated)
            )
        )
        facts, steps = _global_conflicts_with_sql_steps(world)
        assert facts == expected
        counts.append(steps)
        previous = unrelated
    record_property("global_conflict_sql_steps", counts)
    assert max(counts) <= counts[0] * 1.1 + 100, counts


def test_global_conflict_sql_probes_each_shared_group_once(world, record_property):
    digest = world.capture(1)
    # Sort the live identity after all retired identities, so EXISTS has to
    # reach the end of the group. Multiple roles also share the same group.
    world.publish(world.claim(10_000, [digest], roles=("evidence", "copy")))
    counts = []
    previous = 0
    for retired in (10, 100, 500):
        world.publish(
            *(
                world.claim(n, [digest], retired=True, roles=("evidence", "copy"))
                for n in range(previous, retired)
            )
        )
        facts, steps = _global_conflicts_with_sql_steps(world)
        assert facts == world.cold_conflicts()
        counts.append(steps)
        previous = retired
    record_property("shared_group_sql_steps", counts)
    # Complete group reads are still linear in membership. A per-retired-use
    # correlated probe grows quadratically and exceeds these bounds.
    assert counts[1] < counts[0] * 12, counts
    assert counts[2] < counts[1] * 7, counts


def test_owner_rename_and_historical_binding_keep_original_relationships(world):
    digest = world.capture(1)
    old = world.claim(1, [digest])
    retired = world.claim(2, [digest], retired=True)
    world.publish(old, retired)
    world.connection.commit()
    historical = sqlite3.connect(":memory:")
    world.connection.backup(historical)
    historical_sources = dict(world.sources)
    reader = CitationReader(historical, historical_sources.__getitem__)
    before = reader.conflicts()
    renamed = world.claim(3, [digest])
    world.publish(renamed)
    world.remove(old)
    assert not world.reader.owner_uses("Claim", old.identity.qualified)
    assert world.reader.owner_uses("Claim", renamed.identity.qualified)
    assert reader.conflicts() == before
    assert world.reader.conflicts() == world.cold_conflicts()
    assert world.reader.conflicts() != before
    assert len(world.reader.owners_for_capture(digest)) == 2
    assert world.connection.execute(
        "SELECT capture_digest,evidence_commitment_digest FROM captures"
    ).fetchone() == (digest, DIGEST)
    assert digest != DIGEST
    historical.close()


def test_span_candidates_stay_within_source_version_and_filter_actual_overlap(world, monkeypatch):
    starts = [0, 1, 7, 20]
    selected = [world.capture(n, start=start, end=start + 4) for n, start in enumerate(starts)]
    unrelated = [world.capture(n + 20, source=n + 1) for n in range(100)]
    world.publish(
        *(world.claim(n + 1, [digest]) for n, digest in enumerate([*selected, *unrelated]))
    )
    version = _same_version_span_key(
        {"source": world.envelopes[selected[0]].source.model_dump(mode="json")}
    )[0]
    original = world.reader._envelope
    seen = []

    def counted(digest, bodies, envelopes):
        seen.append(digest)
        return original(digest, bodies, envelopes)

    monkeypatch.setattr(CitationReader, "_envelope", staticmethod(counted))
    uses = world.reader.overlapping_uses(version, 4, 10, bodies=world.store)
    assert set(seen) == set(selected[:3])
    assert {u["capture_digest"]["$digest"] for u in uses} == set(selected[1:3])


def test_exact_external_group_retains_original_producer_binding_exclusion(world):
    first = world.capture(1, binding=DIGEST)
    second = world.capture(2, binding="sha256:" + "2" * 64)
    world.publish(world.claim(1, [first]), world.claim(2, [second], retired=True))
    assert world.connection.execute("SELECT count(*) FROM source_references").fetchone()[0] == 2
    facts = world.reader.conflicts()
    assert facts == world.cold_conflicts()
    assert {fact.value["relation_kind"] for fact in facts} == {
        "exact_external",
        "same_version_span",
    }


def _next_relation_findings(world, reader):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from cruxible_core.compiler.compiler import P2_B5_COMPILER
    from cruxible_core.coverage.contracts import (
        CoverageAccessProfileV1,
        CoverageCommitmentScanProofV1,
        CoverageLineOverlayV1,
        LogicalSourceIdentityV1,
        occurrence_identity_digest,
    )
    from cruxible_core.coverage.indexes import WorkingOccurrenceV1
    from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
    from cruxible_core.service.discovery.next import (
        PlaybillNextSourceObservationV4,
        PlaybillNextWorkspaceObservationV1,
        _citation_relation_items,
    )

    source = LogicalSourceIdentityV1(plane="external", identity="source-0")
    occurrence = WorkingOccurrenceV1(
        source=source,
        observed_commitment_digest=DIGEST,
        byte_length=10,
        ordinal=0,
        identity_digest=occurrence_identity_digest(
            source=source,
            observed_commitment_digest=DIGEST,
            ordinal=0,
        ),
        line_overlay=CoverageLineOverlayV1(start_byte=0, end_byte=10, start_line=1, end_line=1),
    )
    observation = PlaybillNextSourceObservationV4(
        source_id="source-0",
        observed_source_digest=DIGEST,
        byte_length=20,
        marker_summaries=(),
        occurrences=(occurrence,),
        commitment_scan_proofs=(
            CoverageCommitmentScanProofV1(
                source=source,
                commitment_digest=DIGEST,
                byte_length=10,
            ),
        ),
        citation_window_observations=(),
        scan_notes=(),
        marker_notes=(),
    )
    coordinate = AcceptedProjectionCoordinate(
        instance_id="citation-test",
        repository_path="/fixture/ledger.git",
        git_object_format="sha1",
        git_oid=AT.git_oid,
        semantic_root=AT.semantic_root,
        generation_root=AT.generation_root,
        compiler=P2_B5_COMPILER,
    )
    def refuse_tree_read(*_args):
        pytest.fail("citation relation service enumerated the accepted tree")

    instance = SimpleNamespace(
        paths_at=refuse_tree_read,
        tree_at=refuse_tree_read,
        bind_accepted_projection=lambda _coordinate: nullcontext(SimpleNamespace(citations=reader)),
    )
    return _citation_relation_items(
        instance,
        coordinate=coordinate,
        access_profile=CoverageAccessProfileV1(
            profile_id="test",
            permitted_access_classes=("instance",),
        ),
        observation=PlaybillNextWorkspaceObservationV1(source_observations=(observation,)),
    )


def test_next_relation_findings_use_sql_without_accepted_tree_reads(world):
    digest = world.capture(1)
    live = world.claim(1, [digest])
    retired = world.claim(2, [digest], retired=True)
    world.publish(live, retired)
    findings = _next_relation_findings(world, world.reader)
    assert len(findings) == 1
    assert findings[0].detail["relation_kind"] == "capture"
    assert findings[0].subject_identity == live.identity.qualified


@pytest.mark.parametrize("unavailable", ["missing", "corrupt"])
@pytest.mark.parametrize("shared", [True, False])
def test_next_findings_survive_unavailable_cas_like_retained_facts(world, unavailable, shared):
    from types import SimpleNamespace

    from cruxible_core.indexes.evidence.citation_sql import CitationSourceUse
    from tests.core_support._citation_relations_oracle import RELATION_SOURCE_USE_SCHEMA

    first = world.capture(1, start=0, end=5)
    second = first if shared else world.capture(2, start=10, end=15)
    world.publish(world.claim(1, [first]), world.claim(2, [second], retired=True))
    # These are the exact accepted rows the prior semantic-fact reader retained.
    retained = build_citation_relation_facts(world.sources, bodies=world.store)
    conflicts = tuple(
        sorted(
            (f for f in retained if f.schema_id == RELATION_RETIRED_CONFLICT_SCHEMA),
            key=lambda f: f.fact_key,
        )
    )
    uses = [f.value for f in retained if f.schema_id == RELATION_SOURCE_USE_SCHEMA]
    old_reader = SimpleNamespace(
        conflicts=lambda: conflicts,
        uses_for_source=lambda source: tuple(
            CitationSourceUse(
                capture_digest=u["capture_digest"]["$digest"],
                citation_id=u["citation_id"],
                claim_artifact_digest=u["claim_artifact_digest"]["$digest"],
                claim_identity=u["claim_identity"],
                lifecycle=u["claim_lifecycle"],
                commitment_digest=u["commitment"]["digest"],
                byte_length=u["commitment"]["byte_length"],
                source_identity=u["source"]["source_identity"],
                coordinate_type=u["source"]["coordinate_type"],
                selector_type=u["source"]["selector_type"],
            )
            for u in uses
            if u["source"]["source_identity"] == source
        ),
    )
    expected = _next_relation_findings(world, old_reader)
    assert len(expected) == 1
    assert expected[0].detail["relation_kind"] == ("capture" if shared else "current_span_overlap")
    assert _next_relation_findings(world, world.reader) == expected
    if unavailable == "missing":
        world.store._path(first).unlink()
    else:
        world.store._path(first).write_bytes(b"corrupt Capture bytes")
    assert _next_relation_findings(world, old_reader) == expected
    assert _next_relation_findings(world, world.reader) == expected
    assert world.reader.conflicts() == conflicts
    with pytest.raises(PlaybillCasError):
        coverage_rows(world.reader, bodies=world.store, at=AT)


def test_source_metadata_retains_exact_large_commitment_length_without_body(world):
    length = 1 << 70
    digest = world.capture(1, byte_length=length)
    world.publish(world.claim(1, [digest]))
    before = world.reader.uses_for_source("source-0")
    assert before[0].byte_length == length
    world.store._path(digest).unlink()
    assert world.reader.uses_for_source("source-0") == before

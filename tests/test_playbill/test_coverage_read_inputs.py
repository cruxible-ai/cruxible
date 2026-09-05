"""Coverage reads authoritative artifacts once without retaining mutable read state."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_is_self_asserted,
    parse_capture_contract,
    parse_capture_envelope,
)
from cruxible_client.contracts.claim_verdicts import observation_trust_grade
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    claim_artifact_digest,
    claim_statement_digest,
)
from cruxible_client.contracts.errors import PlaybillCasError, PlaybillFormatError
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.coverage.adapter import (
    WorkingSourceObservationV1,
    observe_working_source,
)
from cruxible_core.playbill.coverage.contracts import LogicalSourceIdentityV1
from cruxible_core.playbill.coverage.indexes import (
    CaptureCitationInputV2,
    build_evidence_citation_index_v2,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service import playbill_coverage as coverage
from cruxible_core.service.playbill_claims import _claim_from_view, service_list_playbill_claims
from tests.test_playbill.test_authoring_existing_capture import shared_capture_world
from tests.test_playbill.test_citation_retirement_relations import _retire_claim
from tests.test_playbill.test_projection_scanner_integration import _foreign_world


def _projected_index(instance, *, at):  # type: ignore[no-untyped-def]
    """The previous projection-to-artifact route, retained only as a differential oracle."""
    listing = service_list_playbill_claims(instance, at=at, include_retired=True)
    contracts = {}
    for path, content in instance.tree_at(at.git_oid).items():
        if path.startswith("capture-contracts/"):
            contract = parse_capture_contract(content, path=path)
            contracts[capture_contract_digest(contract).tagged] = contract
    claims = []
    captures = {}
    for view in listing.claims:
        artifact = _claim_from_view(view)
        claims.append(
            AcceptedClaim(
                path=view.envelope["path"],
                claim=artifact,
                statement_digest=claim_statement_digest(artifact.statement).tagged,
                artifact_digest=claim_artifact_digest(artifact).tagged,
            )
        )
        for digest in artifact.backing.capture_digests:
            if digest in captures:
                continue
            envelope = parse_capture_envelope(
                instance.body_store().read(
                    digest,
                    access=BodyAccessContext(principal_id="coverage-test", can_read_body=True),
                )
            )
            provenance = (
                "self-asserted"
                if capture_contract_is_self_asserted(contracts[envelope.capture_contract_digest])
                else "daemon-fetched"
            )
            captures[digest] = CaptureCitationInputV2(
                capture_digest=digest,
                envelope=envelope,
                access_class=coverage.COVERAGE_EVIDENCE_ACCESS_CLASS,
                observation_trust=observation_trust_grade(provenance),
            )
    return build_evidence_citation_index_v2(
        at=at,
        captures=tuple(captures[digest] for digest in sorted(captures)),
        claims=tuple(claims),
    )


def test_tree_inputs_equal_projection_inputs_before_and_after_retirement(tmp_path: Path) -> None:
    instance, owner, _actor, first, _second, *_rest = shared_capture_world(tmp_path)
    previous = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    before = coverage.build_accepted_evidence_index_v2(instance, at=previous)
    assert before == _projected_index(instance, at=previous)

    _retire_claim(instance, owner, first)
    current = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    assert coverage.build_accepted_evidence_index_v2(instance, at=previous) == before
    after = coverage.build_accepted_evidence_index_v2(instance, at=current)
    assert after == _projected_index(instance, at=current)
    assert sum(len(row.citation_associations) for row in before.citations) == 2
    assert sum(len(row.citation_associations) for row in after.citations) == 1
    assert {d for row in after.citations for d in row.capture_digests} == {
        d for row in before.citations for d in row.capture_digests
    }
    forged = current.model_copy(update={"semantic_root": "sha256:" + "f" * 64})
    with pytest.raises(PlaybillFormatError):
        coverage.build_accepted_evidence_index_v2(instance, at=forged)


def test_service_reuses_captures_and_matches_projection_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, source, _workspace = _foreign_world(tmp_path)
    observation = observe_working_source(
        LogicalSourceIdentityV1(plane="external", identity="corpus.runbook"), source.read_bytes()
    )
    at = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    expected_index = _projected_index(instance, at=at)
    capture_count = len({d for row in expected_index.citations for d in row.capture_digests})
    original_parse = coverage.parse_capture_envelope
    parsed = 0

    def counted(content):  # type: ignore[no-untyped-def]
        nonlocal parsed
        parsed += 1
        return original_parse(content)

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("coverage must not materialize inspection projections")

    monkeypatch.setattr(coverage, "parse_capture_envelope", counted)
    monkeypatch.setattr(coverage, "service_list_playbill_claims", forbidden)
    arguments = dict(
        instance_id=instance.descriptor.instance_id, observations=(observation,), at=at
    )
    actual = coverage.service_resolve_playbill_coverage(instance, **arguments)
    assert parsed == capture_count

    def projected_inputs(instance, *, at):  # type: ignore[no-untyped-def]
        index = _projected_index(instance, at=at)
        return index, coverage._capture_envelopes(instance, index=index)

    monkeypatch.setattr(coverage, "_accepted_evidence_inputs_v2", projected_inputs)
    assert coverage.service_resolve_playbill_coverage(instance, **arguments) == actual


def test_subsequent_reads_recheck_source_bytes_and_capture_cas(tmp_path: Path) -> None:
    instance, source, _workspace = _foreign_world(tmp_path)
    logical = LogicalSourceIdentityV1(plane="external", identity="corpus.runbook")
    arguments = dict(instance_id=instance.descriptor.instance_id)
    before = coverage.service_resolve_playbill_coverage(
        instance, observations=(observe_working_source(logical, source.read_bytes()),), **arguments
    )
    source.write_bytes(source.read_bytes().replace(b"ready", b"other"))
    after = coverage.service_resolve_playbill_coverage(
        instance, observations=(observe_working_source(logical, source.read_bytes()),), **arguments
    )
    assert before.at == after.at
    assert before != after
    at = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    index, first = coverage._accepted_evidence_inputs_v2(instance, at=at)
    digest = next(iter(first))
    first[digest].source.selector["test_mutation"] = True
    _, second = coverage._accepted_evidence_inputs_v2(instance, at=at)
    assert "test_mutation" not in second[digest].source.selector
    path = instance.body_store()._path(digest)
    content = path.read_bytes()
    path.unlink()
    with pytest.raises(PlaybillCasError):
        coverage.build_accepted_evidence_index_v2(instance, at=at)
    path.write_bytes(b"corrupt")
    with pytest.raises(PlaybillCasError):
        coverage.build_accepted_evidence_index_v2(instance, at=at)
    path.write_bytes(content)
    assert coverage.build_accepted_evidence_index_v2(instance, at=at) == index


def test_many_citation_windows_decode_each_source_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, source, _workspace = _foreign_world(tmp_path)
    at = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    index, envelopes = coverage._accepted_evidence_inputs_v2(instance, at=at)
    logical = LogicalSourceIdentityV1(plane="external", identity="corpus.runbook")
    observation = observe_working_source(logical, source.read_bytes())
    digest = next(iter(envelopes))
    retired = tuple(
        (logical, "sha256:" + hashlib.sha256(str(n).encode()).hexdigest(), digest)
        for n in range(20)
    )
    original = WorkingSourceObservationV1.content.fget
    calls = 0

    def counted(self):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(WorkingSourceObservationV1, "content", property(counted))
    windows = coverage._citation_window_observations(
        index=index,
        observations=(observation,),
        envelopes=envelopes,
        retired_associations=retired,
    )
    assert len(windows) == 21
    assert calls == 1


def test_historical_codec_matches_projection_before_existing_index_path_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct decoding preserves old bytes; AcceptedClaim's old path limit stays explicit."""
    from types import SimpleNamespace

    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.claims import ClaimFormatError, claim_path
    from cruxible_core.playbill.compiler import (
        P2_B0_COMPILER,
        artifact_codec_for_compiler,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
    from cruxible_core.playbill.projection_artifacts import parse_projection_tree
    from cruxible_core.playbill.projection_claims import claim_projection_view
    from cruxible_core.service.playbill_claims import _public_claim
    from tests.test_playbill.test_claim_citations import _v2_claim

    claim = _v2_claim()
    path = claim_path(claim.identity.name).removesuffix(".json") + ".yaml"
    tree = {path: canonical_bytes(claim.model_dump(mode="json")) + b"\n"}
    coordinate = AcceptedProjectionCoordinate(
        instance_id="legacy-coverage-test",
        repository_path="/fixture/ledger.git",
        git_object_format="sha1",
        git_oid="1" * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler=P2_B0_COMPILER,
    )
    at = PlaybillAcceptedCoordinate.from_internal(coordinate)
    parsed = parse_projection_tree(
        tree,
        registry=projection_registry_for_compiler(coordinate.compiler),
        artifact_kinds=artifact_kinds_for_compiler(coordinate.compiler),
        artifact_codec=artifact_codec_for_compiler(coordinate.compiler),
    )
    projected = _public_claim(
        claim_projection_view(parsed.envelopes[0], parsed.semantic_facts, coordinate=coordinate)
    )
    reconstructed = _claim_from_view(projected)
    assert reconstructed == claim
    expected = dict(
        path=projected.envelope["path"],
        claim=reconstructed,
        statement_digest=claim_statement_digest(reconstructed.statement).tagged,
        artifact_digest=claim_artifact_digest(reconstructed).tagged,
    )
    # This existing frozen-model constraint also refused the old projection route.
    # Do not rewrite historical addresses or claim new historical coverage support.
    with pytest.raises(ClaimFormatError, match="identity/path disagreement"):
        AcceptedClaim(**expected)

    def resolve(**values):  # type: ignore[no-untyped-def]
        assert values == {
            "git_oid": at.git_oid,
            "semantic_root": at.semantic_root,
            "generation_root": at.generation_root,
            "compiler_digest": at.compiler_digest,
        }
        return coordinate

    instance = SimpleNamespace(
        resolve_accepted_coordinate=resolve,
        coordinate_for_oid=lambda oid: coordinate if oid == at.git_oid else None,
        body_store=lambda: None,
        tree_at=lambda oid: tree if oid == at.git_oid else {},
    )
    observed = []

    def accepted_claim(**values):  # type: ignore[no-untyped-def]
        observed.append(values)
        return AcceptedClaim(**values)

    monkeypatch.setattr(coverage, "AcceptedClaim", accepted_claim)
    with pytest.raises(ClaimFormatError, match="identity/path disagreement"):
        coverage.build_accepted_evidence_index_v2(instance, at=at)
    assert observed == [expected]

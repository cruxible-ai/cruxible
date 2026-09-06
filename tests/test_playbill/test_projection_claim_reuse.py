"""Exact projection parity across Claim cache hits, revisions and failures."""

import json

import pytest

from cruxible_client.contracts import claims
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.projection_extensions import ProjectionExtensionRegistry
from cruxible_core.playbill import projection_artifacts
from cruxible_core.playbill.assembler import PYTHON_REFERENCE_ASSEMBLER
from cruxible_core.playbill.claim_retirement import service_retire_claim
from cruxible_core.playbill.projection_artifacts import parse_projection_tree
from cruxible_core.playbill.projection_claim_cache import ClaimCompilationCache
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.storage.playbill_projection import (
    canonical_logical_export,
    initialize_projection_database,
    projection_logical_digest,
)
from tests.test_playbill._support import client_material
from tests.test_playbill.test_activation_handoff_guards import _input
from tests.test_playbill.test_claim_retirement import _activate, _request
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world


def _parse(instance, cache=None, *, blobs=None, registry=None):
    assembler = instance.projection_assembler()
    request = assembler.request(
        output_staging_directory=assembler.publication_directory / ".stage-reuse-test"
    )
    parsed = parse_projection_tree(
        instance.tree_at(instance.accepted_coordinate().git_oid) if blobs is None else blobs,
        registry=assembler.registry if registry is None else registry,
        artifact_kinds=assembler.artifact_kinds,
        artifact_codec=assembler.artifact_codec,
        bodies=instance.body_store(),
        coordinate=request,
        accepted_coordinates_by_sequence=instance._accepted_coordinates_by_sequence(),
        claim_compilation_cache=cache,
    )
    return parsed, request, assembler.registry


def _assert_same_rows_and_digest(tmp_path, cold, warm):
    assert cold[:2] == warm[:2]
    exports, digests = [], []
    for label, (parsed, request, registry) in zip(("cold", "warm"), (cold, warm), strict=True):
        path = tmp_path / f"{label}.sqlite"
        initialize_projection_database(
            path,
            request=request,
            parsed=parsed,
            registry=registry,
            assembler_implementation=PYTHON_REFERENCE_ASSEMBLER,
        )
        exports.append(canonical_logical_export(path))
        digests.append(projection_logical_digest(path))
    assert exports[0] == exports[1]
    assert digests[0] == digests[1]


def test_warm_claims_skip_compilation_with_identical_rows_and_logical_digest(tmp_path, monkeypatch):
    instance, _claim_id, _owner = _accepted_claim_world(tmp_path)
    cache = ClaimCompilationCache()
    cold = _parse(instance)
    assert _parse(instance, cache)[0] == cold[0]
    assert cache.entry_count == 1

    def unexpected(*args, **kwargs):
        pytest.fail("unchanged warm Claim was recompiled")

    with monkeypatch.context() as guarded:
        guarded.setattr(claims, "parse_claim", unexpected)
        guarded.setattr(projection_artifacts, "_claim_static_facts", unexpected)
        warm = _parse(instance, cache)
    _assert_same_rows_and_digest(tmp_path, cold, warm)
    value = next(f.value for f in warm[0].semantic_facts if f.schema_id == "playbill.claim.backing")
    assert isinstance(value, dict)
    value.clear()
    assert _parse(instance, cache)[0] == cold[0]
    cache.clear()
    assert _parse(instance, cache)[0] == cold[0]


def test_new_generation_reuses_static_claim_but_refreshes_coordinate_proofs(tmp_path, monkeypatch):
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    cache = ClaimCompilationCache()
    old = _parse(instance, cache)[0]
    before = instance.accepted_coordinate()
    tree_before = instance.tree_at(before.git_oid)
    prepared = _input(instance, client_material(instance.root.parent, instance))

    def unexpected(*args, **kwargs):
        pytest.fail("served prebuild did not retain its static Claim cache")

    with monkeypatch.context() as guarded:
        guarded.setattr(projection_artifacts, "_claim_static_facts", unexpected)
        result = instance.settle_and_activate(**prepared)
    assert result.status == "accepted"
    after = instance.accepted_coordinate()
    assert before != after
    assert (
        instance.tree_at(after.git_oid)[claim_path(claim_id)] == tree_before[claim_path(claim_id)]
    )
    cold = _parse(instance)
    with monkeypatch.context() as guarded:
        guarded.setattr(claims, "parse_claim", unexpected)
        warm = _parse(instance, cache)
    _assert_same_rows_and_digest(tmp_path, cold, warm)
    old_proof = next(
        f.value for f in old.semantic_facts if f.schema_id == "playbill.claim.provenance"
    )
    new_proof = next(
        f.value for f in warm[0].semantic_facts if f.schema_id == "playbill.claim.provenance"
    )
    assert old_proof != new_proof
    assert after.git_oid in json.dumps(new_proof)
    instance.refresh()
    assert instance._claim_compilation_cache.entry_count == 0


def test_retirement_misses_old_bytes_and_rebuilds_lifecycle_revision_and_proofs(
    tmp_path, monkeypatch
):
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    cache = ClaimCompilationCache()
    old = _parse(instance, cache)[0]
    result = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=_request(instance, mode="submit"),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    _activate(instance, owner, result)
    calls = []
    original = claims.parse_claim

    def parse(content, *, path, **kwargs):
        calls.append(path)
        return original(content, path=path, **kwargs)

    with monkeypatch.context() as guarded:
        guarded.setattr(claims, "parse_claim", parse)
        changed = _parse(instance, cache)
    assert calls == [claim_path(claim_id)]
    cold = _parse(instance)
    _assert_same_rows_and_digest(tmp_path, cold, changed)
    assert f"Claim:{claim_id}" in changed[0].retired_identities
    before = next(e for e in old.envelopes if e.kind == "claim")
    after = next(e for e in changed[0].envelopes if e.kind == "claim")
    assert after.revision == before.revision + 1
    assert after.artifact_digest != before.artifact_digest
    assert _parse(instance, cache)[0] == changed[0]


@pytest.mark.parametrize("target", ["claim", "changeset"])
def test_warm_entries_do_not_hide_corrupt_claim_or_history_bytes(tmp_path, target):
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    cache = ClaimCompilationCache()
    _parse(instance, cache)
    blobs = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = (
        claim_path(claim_id)
        if target == "claim"
        else next(path for path in blobs if path.startswith("changesets/"))
    )
    blobs[path] = b"{}\n"
    errors = []
    for selected in (None, cache):
        with pytest.raises(ProjectionFormatError) as refused:
            _parse(instance, selected, blobs=blobs)
        errors.append((type(refused.value), str(refused.value), str(refused.value.__cause__)))
    assert errors[0] == errors[1]


def test_warm_static_facts_still_require_current_registry_declarations(tmp_path):
    instance, _claim_id, _owner = _accepted_claim_world(tmp_path)
    cache = ClaimCompilationCache()
    _parse(instance, cache)
    registry = instance.projection_assembler().registry
    without_statement = ProjectionExtensionRegistry(
        [
            d
            for kind in ("semantic", "presentation")
            for d in registry.declarations(kind)
            if d.schema_id != "playbill.claim.statement"
        ],
        artifact_kinds=registry._artifact_kinds,
    )
    for selected in (None, cache):
        with pytest.raises(ProjectionFormatError, match="undeclared.*playbill.claim.statement"):
            _parse(instance, selected, registry=without_statement)


def test_byte_budget_bypass_preserves_output(tmp_path):
    instance, _claim_id, _owner = _accepted_claim_world(tmp_path)
    cache = ClaimCompilationCache(max_bytes=1)
    cold = _parse(instance)[0]
    assert _parse(instance, cache)[0] == cold
    assert _parse(instance, cache)[0] == cold
    assert cache.entry_count == cache.retained_bytes == 0

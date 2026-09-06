"""Exact immutable Claim snapshots are disposable and bounded under contention."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier

import pytest

from cruxible_client.contracts.canonical import ArtifactCodec, canonical_bytes
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_core.playbill.projection_claim_cache import (
    CachedClaim,
    ClaimCompilationCache,
    FrozenClaimFact,
)


def _fact():
    return ProjectionFact(
        schema_id="claim.statement",
        schema_version=1,
        subject_identity="claim:example",
        fact_key="statement",
        value={"nested": [{"text": "e\u0301", "flags": [True, None, 4]}]},
    )


def _entry():
    return CachedClaim(
        identity="claim:example",
        format_tag="claim-v1",
        input_digest="sha256:" + "1" * 64,
        artifact_digest="sha256:" + "2" * 64,
        statement_digest="sha256:" + "3" * 64,
        predecessor_digest=None,
        retired=False,
        pins=(("subject:example", "sha256:" + "4" * 64),),
        facts=(FrozenClaimFact.from_fact(_fact()),),
    )


def _key(**changes):
    return {
        "compiler_digest": "sha256:" + "a" * 64,
        "codec": ArtifactCodec.CURRENT_PRETTY_JSON,
        "path": "claims/example.json",
        "content": b'{"claim":"example"}\n',
        **changes,
    }


def _weight(entry=None, **key):
    cache = ClaimCompilationCache()
    cache.put(**_key(**key), entry=entry or _entry())
    return cache.retained_bytes


def test_fact_snapshot_and_materialization_detach_every_nested_value():
    source = _fact()
    original = canonical_bytes(source.value)
    frozen = FrozenClaimFact.from_fact(source)
    entry = replace(_entry(), facts=(frozen,))
    cache = ClaimCompilationCache()
    cache.put(**_key(), entry=entry)
    first = cache.get(**_key()).materialize_facts()[0]
    assert first == source
    assert first is not source and first.value is not source.value
    first.value["nested"][0]["flags"].append("changed result")
    assert canonical_bytes(source.value) == original
    source.value["nested"][0]["text"] = "changed source"
    second = cache.get(**_key()).materialize_facts()[0]
    assert canonical_bytes(second.value) == original
    assert second is not first and second.value is not first.value
    with pytest.raises(FrozenInstanceError):
        frozen.value_json = b"null"
    with pytest.raises(FrozenInstanceError):
        entry.retired = True


@pytest.mark.parametrize(
    "change",
    [
        {"compiler_digest": "sha256:" + "b" * 64},
        {"codec": ArtifactCodec.P2_B0_COMPACT_JSON},
        {"path": "claims/another.json"},
        {"content": b'{"claim":"example"} '},
    ],
)
def test_each_exact_key_component_separates_entries(change):
    cache = ClaimCompilationCache()
    original, different = _entry(), replace(_entry(), retired=True)
    cache.put(**_key(), entry=original)
    assert cache.get(**_key(**change)) is None
    cache.put(**_key(**change), entry=different)
    assert cache.get(**_key()) is original
    assert cache.get(**_key(**change)) is different
    assert cache.entry_count == 2


def test_replacement_accounting_and_oversized_replacement_remove_old_entry():
    original = _entry()
    larger = replace(original, predecessor_digest="sha256:" + "f" * 64)
    cache = ClaimCompilationCache(max_bytes=_weight(larger))
    cache.put(**_key(), entry=original)
    assert cache.retained_bytes == _weight(original)
    cache.put(**_key(), entry=larger)
    assert cache.entry_count == 1 and cache.retained_bytes == _weight(larger)
    cache.put(**_key(), entry=original)
    assert cache.retained_bytes == _weight(original)
    huge = replace(
        original, facts=(replace(original.facts[0], value_json=b'"' + b"x" * 4000 + b'"'),)
    )
    cache.put(**_key(), entry=huge)
    assert cache.get(**_key()) is None
    assert cache.entry_count == 0 and cache.retained_bytes == 0


@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_lru_touch_controls_eviction_with_count_or_byte_limit(bound):
    entry = _entry()
    weight = _weight(content=b"a")
    cache = ClaimCompilationCache(
        max_entries=2 if bound == "count" else 20,
        max_bytes=weight * 2 if bound == "bytes" else weight * 20,
    )
    for content in (b"a", b"b"):
        cache.put(**_key(content=content), entry=entry)
    assert cache.get(**_key(content=b"a")) is entry
    cache.put(**_key(content=b"c"), entry=entry)
    assert cache.get(**_key(content=b"b")) is None
    assert cache.get(**_key(content=b"a")) is entry
    assert cache.get(**_key(content=b"c")) is entry
    assert cache.entry_count == 2 and cache.retained_bytes == 2 * weight
    cache.clear()
    assert cache.entry_count == 0 and cache.retained_bytes == 0
    assert cache.get(**_key(content=b"c")) is None
    cache.clear()
    assert cache.retained_bytes == 0


@pytest.mark.parametrize("limits", [{"max_entries": 0}, {"max_bytes": 0}, {"max_bytes": 1}])
def test_disabled_and_oversized_entries_bypass_retention(limits):
    cache = ClaimCompilationCache(**limits)
    entry = _entry()
    cache.put(**_key(), entry=entry)
    assert cache.get(**_key()) is None
    assert cache.entry_count == cache.retained_bytes == 0
    assert entry.materialize_facts()[0] == _fact()


@pytest.mark.parametrize("limits", [{"max_entries": -1}, {"max_bytes": -1}])
def test_negative_limits_refused(limits):
    with pytest.raises(ValueError, match="nonnegative"):
        ClaimCompilationCache(**limits)


def test_accounting_includes_full_content_and_every_serialized_field():
    base = _entry()
    weight = _weight(base)
    variants = [
        replace(base, **{field: getattr(base, field) + '\\\n"é'})
        for field in (
            "identity",
            "format_tag",
            "input_digest",
            "artifact_digest",
            "statement_digest",
        )
    ]
    variants += [
        replace(base, predecessor_digest="sha256:" + "5" * 64),
        replace(base, pins=base.pins * 2),
        replace(base, facts=base.facts * 2),
    ]
    assert all(_weight(entry) > weight for entry in variants)
    assert _weight(content=_key()["content"] + b"payload") == weight + len(b"payload")
    assert _weight(path="claims/a-much-longer-path.json") > weight
    assert _weight(compiler_digest="sha256:" + "a" * 128) > weight


def test_concurrent_get_put_clear_remain_bounded_and_return_independent_facts():
    cache = ClaimCompilationCache(max_entries=3, max_bytes=6000)
    barrier = Barrier(4)
    entry = _entry()

    def worker(worker_id):
        barrier.wait(timeout=5)
        for index in range(100):
            key = _key(content=f"{worker_id}:{index % 5}".encode())
            cache.put(**key, entry=entry)
            hit = cache.get(**key)
            if hit is not None:
                fact = hit.materialize_facts()[0]
                fact.value["nested"].clear()
                assert hit.materialize_facts()[0] == _fact()
            if index % 11 == 0:
                cache.clear()
            assert 0 <= cache.entry_count <= 3
            assert 0 <= cache.retained_bytes <= 6000

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker, worker_id) for worker_id in range(4)]
        for future in futures:
            future.result(timeout=10)
    cache.clear()
    assert cache.entry_count == cache.retained_bytes == 0

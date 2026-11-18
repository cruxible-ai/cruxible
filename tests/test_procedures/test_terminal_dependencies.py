"""Per-terminal-item dependency closure derived from exact tokens."""

import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import pytest

from cruxible_core.procedures.terminal_dependencies import (
    TAINT_ACCEPTED_STATE,
    AcquisitionInputOutcomeV1,
    CaptureRootCache,
    DependencyEvidenceFactsV1,
    _prove_root,
    accepted_state_token,
    admitted_capture_token,
    build_terminal_item_manifest,
    build_terminal_item_manifest_v2,
    capture_root,
    derive_terminal_item_facts,
    policy_token,
    terminal_item_manifest_digest,
)


def _digest(byte: str) -> str:
    return f"sha256:{byte * 64}"


def test_manifest_and_facts_fold_only_the_item_dependency_closure() -> None:
    accepted = _digest("a")
    capture = _digest("b")
    policy = _digest("c")
    tokens = frozenset(
        {
            policy_token(policy),
            admitted_capture_token(capture),
            accepted_state_token(accepted),
        }
    )
    manifest = build_terminal_item_manifest_v2(
        tokens,
        terminal_node_id="egress",
        item_key="00000000.item",
    )

    assert manifest.accepted_state_input_digests == (accepted,)
    assert manifest.admitted_capture_digests == (capture,)
    assert manifest.policy_and_law_digests == (policy,)
    assert terminal_item_manifest_digest(manifest).startswith("sha256:")

    derived = derive_terminal_item_facts(
        tokens,
        manifest=manifest,
        child_index=0,
        facts={
            accepted: DependencyEvidenceFactsV1(taint_labels=(TAINT_ACCEPTED_STATE,)),
            capture: DependencyEvidenceFactsV1(
                epistemic_grade="predicted",
                provenance_grade="self-asserted",
                selector_privacy="pseudonymous_required",
                acquisition_input_name="orders",
            ),
        },
        outcomes=(
            AcquisitionInputOutcomeV1(input_name="returns", disposition="omitted"),
            AcquisitionInputOutcomeV1(
                input_name="orders",
                disposition="acquired",
                capture_digests=(capture,),
            ),
        ),
    )

    assert derived.manifest_digest == terminal_item_manifest_digest(manifest)
    assert derived.epistemic_grade == "predicted"
    assert derived.provenance_grade == "self-asserted"
    assert derived.selector_privacy == "pseudonymous_required"
    assert derived.taint_labels == (TAINT_ACCEPTED_STATE,)
    assert tuple((row.input_name, row.disposition) for row in derived.source_coverage) == (
        ("orders", "consumed"),
        ("returns", "absent"),
    )


def test_unconsumed_evidence_cannot_taint_or_weaken_an_item() -> None:
    accepted = _digest("d")
    unconsumed = _digest("e")
    tokens = frozenset({accepted_state_token(accepted)})
    manifest = build_terminal_item_manifest_v2(
        tokens,
        terminal_node_id="egress",
        item_key="00000000.item",
    )

    derived = derive_terminal_item_facts(
        tokens,
        manifest=manifest,
        child_index=0,
        facts={
            unconsumed: DependencyEvidenceFactsV1(
                epistemic_grade="predicted",
                provenance_grade="self-asserted",
                taint_labels=("caller-supplied",),
            )
        },
    )

    assert derived.epistemic_grade == "observed"
    assert derived.provenance_grade == "witnessed"
    assert derived.taint_labels == ()


def test_capture_root_is_the_one_capture_deriving_from_all_the_others() -> None:
    source, view, chained, other = (_digest(byte) for byte in "1234")
    derivations = {view: (source,), chained: (source, view)}
    assert capture_root((), derivations) is None
    assert capture_root((source,), {}) == source
    assert capture_root((source, view), derivations) == view
    # A chain roots at the last derive, transitively.
    assert capture_root((source, view, chained), derivations) == chained
    assert capture_root((source, chained), {chained: (view,), view: (source,)}) == chained
    # Two unrelated Captures, or a derive that did not read the other one, have none.
    assert capture_root((source, other), derivations) is None
    assert capture_root((view, other), derivations) is None
    assert capture_root((source, view), {}) is None


class _CountedInputs(Sequence[str]):
    """One Capture's recorded inputs, counting every entry a caller iterates or indexes.

    ``len()`` is free (a tuple's length is O(1)) and is not counted; reading
    an entry is.
    """

    def __init__(self, owner: "_CountingDerivations", data: tuple[str, ...]) -> None:
        self._owner, self._data = owner, data

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, index):  # type: ignore[no-untyped-def,override]
        found = self._data[index]
        self._owner.edges += len(found) if isinstance(index, slice) else 1
        return found

    def __iter__(self) -> Iterator[str]:
        for item in self._data:
            self._owner.edges += 1
            yield item

    def __contains__(self, item: object) -> bool:
        self._owner.edges += len(self._data)
        return item in self._data


class _CountingDerivations(Mapping[str, Sequence[str]]):
    """Wraps a derivations mapping, counting every recorded-input entry actually read.

    ``edges`` counts each entry each time it is read, so repeating or sharing
    a read across candidates or calls is visible in the total; ``lookups``
    counts mapping lookups.
    """

    def __init__(self, data: dict[str, tuple[str, ...]]) -> None:
        self._data = data
        self.edges = 0
        self.lookups = 0

    def get(self, key: str, default: Sequence[str] = ()) -> Sequence[str]:  # type: ignore[override]
        self.lookups += 1
        found = self._data.get(key)
        return default if found is None else _CountedInputs(self, found)

    def __getitem__(self, key: str) -> Sequence[str]:
        self.lookups += 1
        return _CountedInputs(self, self._data[key])

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _cumulative_chain(n: int) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """An n-node derive chain as the executor records it: each derive lists EVERY prior input.

    Node i's manifest names nodes 0..i-1, so the graph itself holds n(n-1)/2
    edges. Digests ascend with i, so the root sorts LAST (the worst order for
    a sorted candidate scan).
    """

    digests = [f"sha256:{i:064x}" for i in range(n)]
    derivations = {digests[i]: tuple(digests[:i]) for i in range(1, n)}
    return digests, derivations


def _sparse_chain(n: int) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """A straight n-node derive chain: node i's one recorded input is node i-1."""

    digests = [f"sha256:{i:064x}" for i in range(n)]
    derivations = {digests[i]: (digests[i - 1],) for i in range(1, n)}
    return digests, derivations


#: Linear-work bound: a small multiple of n, where the old per-vertex ancestor
#: sets cost 120 -> 161,700 union entries (cubic) on the same cumulative chains.
def _linear(n: int) -> int:
    return 3 * n


@pytest.mark.parametrize("n", [10, 100])
def test_capture_root_on_a_cumulative_chain_reads_a_linear_number_of_edges(n: int) -> None:
    """D-R37 cost: the whole cumulative chain as the closure, root sorting last.

    The graph holds n(n-1)/2 edges; the proof reads only the root's own input
    list. No per-vertex ancestor set is built, no set union or subset check is
    done (each edge read is O(1) set membership work), and the traversal's
    transient state stays linear and is dropped when the call returns.
    """

    digests, derivations = _cumulative_chain(n)
    counted = _CountingDerivations(derivations)
    proof = _prove_root(frozenset(digests), counted)

    assert proof.root == digests[-1]
    assert counted.edges == proof.edges_read
    assert proof.edges_read <= _linear(n)
    assert proof.held <= _linear(n)
    assert counted.lookups <= 2 * n + 1


@pytest.mark.parametrize("n", [10, 100])
def test_capture_root_is_free_for_every_item_after_the_first_in_one_closure(n: int) -> None:
    """D-R37 cost: many items (Claims, or the journal's per-item loop) citing
    the SAME closure share one ``CaptureRootCache`` for the run: the root is
    proven once, every later item's call is a cache hit, and the cache retains
    exactly one entry per distinct closure (no per-vertex state).
    """

    digests, derivations = _cumulative_chain(n)
    counted = _CountingDerivations(derivations)
    cache = CaptureRootCache()

    for closure in (tuple(digests), (digests[0], digests[-1])):
        work_by_item = []
        for _ in range(50):
            before = counted.edges + counted.lookups
            assert capture_root(closure, counted, cache=cache) == digests[-1]
            work_by_item.append(counted.edges + counted.lookups - before)
        first, later = work_by_item[0], work_by_item[1:]
        assert first <= 2 * _linear(n)
        assert later == [0] * len(later)
    assert len(cache.roots) == 2
    # The cache may only hold the per-closure root memo (``roots``) and (lane
    # B) the per-witness verdict memo (``witnessed``) -- never a per-vertex
    # ancestor set. Both are keyed/valued by digests and verdicts only, never
    # by a retained traversal state (e.g. a per-vertex visited/ancestor set).
    cache_fields = vars(cache)
    assert cache_fields.keys() <= {"roots", "witnessed"}
    for value in cache_fields.values():
        assert isinstance(value, dict)


@pytest.mark.parametrize("n", [10, 100])
def test_capture_root_stays_linear_when_the_widest_member_is_not_the_root(n: int) -> None:
    """Selection's first walk misses the root, so the survivor is proven by a second walk.

    The root R records one input, X; X records the n-node cumulative chain's
    every node. X (the widest member) is tried first and eliminates the chain;
    R then eliminates X and is proven from R alone. Both walks stay linear,
    also when X is outside the closure and R reaches the chain only through it.
    """

    digests, derivations = _cumulative_chain(n)
    x, r = "sha256:" + "e" * 64, "sha256:" + "f" * 64
    derivations = {**derivations, x: tuple(digests), r: (x,)}
    counted = _CountingDerivations(derivations)
    proof = _prove_root(frozenset((*digests, x, r)), counted)

    assert proof.root == r
    assert proof.edges_read <= 2 * _linear(n)
    assert proof.held <= 2 * _linear(n)

    # X recorded but outside the closure: R reaches the chain only through it.
    counted = _CountingDerivations(derivations)
    proof = _prove_root(frozenset((*digests, r)), counted)
    assert proof.root == r
    assert proof.edges_read <= 2 * _linear(n)
    assert proof.held <= 2 * _linear(n)


@pytest.mark.parametrize("n", [10, 100])
def test_capture_root_on_a_sparse_chain_is_linear(n: int) -> None:
    """One recorded input per derive, root sorting last: select then prove, each O(n)."""

    digests, derivations = _sparse_chain(n)
    counted = _CountingDerivations(derivations)
    proof = _prove_root(frozenset(digests), counted)

    assert proof.root == digests[-1]
    assert proof.edges_read <= _linear(n)
    assert proof.held <= _linear(n)


def test_two_unrelated_tops_have_no_root_whatever_the_chain_beneath() -> None:
    digests, derivations = _cumulative_chain(10)
    other = "sha256:" + "f" * 64
    derivations = {**derivations, other: (digests[0],)}
    assert capture_root((*digests, other), derivations) is None
    # A cycle (impossible for digest-fixed inputs) never yields an unproven root.
    a, b = digests[0], digests[1]
    assert capture_root((a, b), {a: (b,), b: (a,)}) in {a, b}


def _closures(captures: int, items: int):  # type: ignore[no-untyped-def]
    """``items`` closures over one shared list of ``captures``, each with one own Capture."""

    from cruxible_core.procedures.terminal_dependencies import (
        produced_capture_token,
        receipt_token,
    )

    shared = [f"sha256:{index:064x}" for index in range(captures)]
    own = [f"sha256:{'f' * 56}{index:08x}" for index in range(items)]
    tokens = [
        frozenset(
            {
                *(produced_capture_token(digest) for digest in shared),
                *(receipt_token(digest) for digest in shared),
                produced_capture_token(own[index]),
                policy_token(_digest("c")),
            }
        )
        for index in range(items)
    ]
    facts = {
        digest: DependencyEvidenceFactsV1(
            epistemic_grade="observed",
            provenance_grade="daemon-fetched",
            acquisition_input_name="doc.pages",
        )
        for digest in (*shared, *own)
    }
    outcomes = (
        AcquisitionInputOutcomeV1(
            input_name="doc.pages",
            disposition="acquired",
            capture_digests=tuple(sorted((*shared, *own))),
        ),
    )
    manifests, derived = [], []
    for index, closure in enumerate(tokens):
        manifest = build_terminal_item_manifest_v2(
            closure, terminal_node_id="egress", item_key=f"{index:08d}.item"
        )
        manifests.append(manifest)
        derived.append(
            derive_terminal_item_facts(
                closure, manifest=manifest, child_index=index, facts=facts, outcomes=outcomes
            )
        )
    return manifests, derived


def _cas_digest(content: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(content).hexdigest()


def test_a_terminal_journals_one_grouped_record_that_restores_every_item_exactly() -> None:
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_core.procedures.terminal_dependencies import (
        TERMINAL_ITEM_CHUNK_SIZE,
        build_terminal_items,
        resolve_terminal_items,
        shared_terminal_dependencies,
    )

    items = TERMINAL_ITEM_CHUNK_SIZE + 3
    manifests, derived = _closures(captures=5, items=items)
    shared = shared_terminal_dependencies(manifests, derived)
    assert len(shared.digests.produced_capture_digests) == 5  # the own Captures are not shared
    assert shared.coverage[0].input_name == "doc.pages"
    built = build_terminal_items(
        manifests, derived, terminal_node_id="egress", shared=shared, digest=_cas_digest
    )
    store = {_cas_digest(body): body for body in built.bodies}
    assert len(store) == len(built.bodies)  # each distinct body once
    payload = built.payload
    assert payload["count"] == items
    assert len(payload["chunks"]) == 2  # type: ignore[arg-type]
    restored = resolve_terminal_items(payload, store.__getitem__)
    # The manifest digest is the v2 rule over the whole closure, run-free.
    assert restored == tuple(zip(manifests, derived, strict=True))
    for manifest, facts in restored:
        assert terminal_item_manifest_digest(manifest) == facts.manifest_digest
    # No body names a run, a time or a journal position: the same closures
    # build the same bytes, so an unchanged terminal stores nothing new.
    again = build_terminal_items(
        manifests, derived, terminal_node_id="egress", shared=shared, digest=_cas_digest
    )
    assert again == built
    for body in built.bodies:
        assert b"run_id" not in body and b"recorded_at" not in body
    # An item body the shared set does not reproduce is refused.
    first_chunk = store[payload["chunks"][0]]  # type: ignore[index]
    import json

    entry = json.loads(first_chunk)["entries"][0]
    body = json.loads(store[entry[1]])
    tampered = canonical_bytes({**body, "own": {**body["own"], "produced_capture_digests": []}})
    with pytest.raises(ValueError, match="manifest digest"):
        resolve_terminal_items(payload, {**store, entry[1]: tampered}.__getitem__)
    with pytest.raises(ValueError, match="canonical"):
        resolve_terminal_items(
            payload,
            {**store, payload["shared_digest"]: store[payload["shared_digest"]] + b" "}.__getitem__,  # type: ignore[index]
        )
    with pytest.raises(ValueError, match="count"):
        resolve_terminal_items({**payload, "count": items + 1}, store.__getitem__)


def test_per_item_records_of_earlier_builds_still_resolve() -> None:
    """v1 (inline) and v2 (shared-set) per-item records stay readable."""

    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_core.procedures.terminal_dependencies import (
        render_shared_terminal_dependencies,
        resolve_item_dependencies,
        shared_terminal_dependencies,
    )

    manifest = build_terminal_item_manifest(
        frozenset({admitted_capture_token(_digest("a"))}),
        run_id="run-1",
        terminal_node_id="egress",
        item_key="00000000.item",
    )
    facts = {
        "tag": "playbill-terminal-item-derived-facts-v1",
        "run_id": "run-1",
        "terminal_node_id": "egress",
        "item_key": "00000000.item",
        "child_index": 0,
        "manifest_digest": terminal_item_manifest_digest(manifest),
        "epistemic_grade": "observed",
        "provenance_grade": "witnessed",
        "selector_privacy": "direct_allowed",
        "taint_labels": [],
        "source_coverage": [],
    }
    v1 = {
        "tag": "playbill-procedure-item-dependencies-v1",
        "manifest": manifest.model_dump(mode="json"),
        "derived": facts,
        "audit": {"deployment_digest": None, "recorded_at": "2026-11-15T00:00:00Z"},
    }
    restored_manifest, restored_facts = resolve_item_dependencies(v1, {}.__getitem__)
    assert (
        restored_manifest == manifest and restored_facts.manifest_digest == facts["manifest_digest"]
    )
    from cruxible_core.procedures.terminal_dependencies import (
        TerminalDependencyDigestsV1,
        TerminalSharedDependenciesV1,
    )

    shared = TerminalSharedDependenciesV1(
        digests=TerminalDependencyDigestsV1(admitted_capture_digests=(_digest("a"),))
    )
    content = render_shared_terminal_dependencies(shared)
    v2 = {
        "tag": "playbill-procedure-item-dependencies-v2",
        "shared_digest": _digest("e"),
        "own": {},
        "derived": facts,
        "audit": {"deployment_digest": None, "recorded_at": "2026-11-15T00:00:00Z"},
    }
    assert resolve_item_dependencies(v2, {_digest("e"): content}.__getitem__)[0] == manifest
    with pytest.raises(ValueError, match="canonical"):
        resolve_item_dependencies(
            v2,
            {_digest("e"): canonical_bytes(shared.model_dump(mode="json")) + b" "}.__getitem__,
        )
    assert shared_terminal_dependencies  # still exported for the writer


@contextmanager
def _model_builds() -> Iterator[Counter[str]]:
    """Count Pydantic model constructions on this thread, as the settle scale guard does."""

    from pydantic import BaseModel

    codes = {
        getattr(BaseModel, name).__code__: name
        for name in (
            "__init__",
            "model_validate",
            "model_validate_json",
            "model_copy",
            "model_construct",
        )
    }
    counts: Counter[str] = Counter()
    previous = sys.getprofile()

    def count(frame: Any, event: str, arg: Any) -> None:
        if event == "call" and frame.f_code in codes:
            value = frame.f_locals.get("self", frame.f_locals.get("cls"))
            name = value.__name__ if isinstance(value, type) else type(value).__name__
            counts[f"{name}.{codes[frame.f_code]}"] += 1

    sys.setprofile(count)
    try:
        yield counts
    finally:
        sys.setprofile(previous)


def test_grouping_item_records_builds_no_model_per_item() -> None:
    """A settle groups every item's closure: building the group adds no model per item.

    The scale guard (tests/test_scale/test_settle_per_item.py) caps model builds
    per settled item; the group's bodies are built as JSON directly.
    """

    from cruxible_core.procedures.terminal_dependencies import (
        build_terminal_items,
        shared_terminal_dependencies,
    )

    builds = {}
    for items in (3, 30):
        manifests, derived = _closures(captures=8, items=items)
        shared = shared_terminal_dependencies(manifests, derived)
        with _model_builds() as counts:
            built = build_terminal_items(
                manifests, derived, terminal_node_id="egress", shared=shared, digest=_cas_digest
            )
        assert built.payload["count"] == items
        builds[items] = dict(counts)
    assert builds == {3: {}, 30: {}}

"""Per-terminal-item dependency closure derived from exact tokens."""

from collections.abc import Iterator, Mapping, Sequence

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
    manifest = build_terminal_item_manifest(
        tokens,
        run_id="run-1",
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
    manifest = build_terminal_item_manifest(
        tokens,
        run_id="run-2",
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

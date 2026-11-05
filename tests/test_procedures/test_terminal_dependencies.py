"""Per-terminal-item dependency closure derived from exact tokens."""

from collections.abc import Mapping

import pytest

from cruxible_core.procedures.terminal_dependencies import (
    TAINT_ACCEPTED_STATE,
    AcquisitionInputOutcomeV1,
    CaptureRootCache,
    DependencyEvidenceFactsV1,
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


class _CountingDerivations(Mapping[str, tuple[str, ...]]):
    """Wraps a derivations mapping, counting every edge ``.get`` yields.

    Matches the review's own measurement: an edge is counted once each time
    it is returned from a lookup, so repeating or sharing a lookup across
    candidates or calls is visible in the total.
    """

    def __init__(self, data: dict[str, tuple[str, ...]]) -> None:
        self._data = data
        self.edges = 0

    def get(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        found = self._data.get(key, default)
        self.edges += len(found)
        return found

    def __getitem__(self, key: str) -> tuple[str, ...]:
        return self._data[key]

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _chain(n: int) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """A straight n-node derive chain: node i's one recorded input is node i-1."""

    digests = [f"sha256:{i:064x}" for i in range(n)]
    derivations = {digests[i]: (digests[i - 1],) for i in range(1, n)}
    return digests, derivations


@pytest.mark.parametrize("n", [10, 100])
def test_capture_root_is_free_for_every_item_after_the_first_in_one_closure(n: int) -> None:
    """D-R37 cost: many items (Claims, or the journal's per-item loop) citing
    the SAME closure share one ``CaptureRootCache`` for the run: the chain is
    walked once, and every later item's call is a cache hit, not a re-walk.
    """

    digests, derivations = _chain(n)
    closure = (digests[0], digests[-1])
    counted = _CountingDerivations(derivations)
    cache = CaptureRootCache()

    items = 50
    visited_by_item = []
    for _ in range(items):
        before = counted.edges
        root = capture_root(closure, counted, cache=cache)
        assert root == digests[-1]
        visited_by_item.append(counted.edges - before)

    first, later = visited_by_item[0], visited_by_item[1:]
    # The first item walks the chain -- linear in its length, never quadratic.
    assert first <= 4 * n
    # Every later item citing the SAME closure costs nothing further: its
    # root came from the cache, not a repeated walk.
    assert later == [0] * len(later)


@pytest.mark.parametrize("n", [10, 100])
def test_capture_root_settles_many_candidates_sharing_a_chain_in_one_pass(n: int) -> None:
    """The traversal shares ancestor work across candidates in one call: a
    node another candidate already settled is never walked past again, so
    visiting the whole closure costs a small multiple of n, never n^2.
    """

    digests, derivations = _chain(n)
    counted = _CountingDerivations(derivations)
    cache = CaptureRootCache()

    root = capture_root(tuple(digests), counted, cache=cache)

    assert root == digests[-1]
    assert counted.edges <= 6 * n  # sub-quadratic: O(n), not O(n^2)

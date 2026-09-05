"""One request shares fact assembly; later requests observe mutable bodies anew."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.claims import parse_claim, render_claim
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.service import playbill_query as query
from tests.test_playbill._knowledge_loop_support import seed_claims


def _retired_tree(instance):
    """A read-source fixture with one retired head, retaining its law history."""

    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    path = sorted(path for path in tree if path.startswith("claims/"))[0]
    claim = parse_claim(tree[path], path=path)
    tree[path] = render_claim(
        claim.model_copy(
            update={"lifecycle": claim.lifecycle.model_copy(update={"state": "retired"})}
        )
    )
    return coordinate, tree, path


@pytest.mark.parametrize("first_include_retired", (False, True))
def test_read_assembles_each_row_and_parses_each_claim_and_type_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_include_retired: bool
) -> None:
    instance, _owner = seed_claims(tmp_path)
    coordinate, tree, _retired = _retired_tree(instance)
    monkeypatch.setattr(instance, "tree_at", lambda oid: dict(tree))
    expected = {
        include: query.build_accepted_query_facts(
            instance, coordinate=coordinate, include_retired=include
        )
        for include in (False, True)
    }
    calls: Counter[str] = Counter()
    for name in ("parse_claim", "parse_claim_type", "_fact_row", "_accepted_subjects"):
        original = getattr(query, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(query, name, counted)

    reader = query._AcceptedQueryFactsRead(instance, coordinate=coordinate)
    assert not calls
    first = reader.build(include_retired=first_include_retired)
    assert first == expected[first_include_retired]
    assert calls["_fact_row"] == (2 if first_include_retired else 1)
    assert (
        reader.build(include_retired=not first_include_retired)
        == expected[not first_include_retired]
    )
    for include in (False, True, False, True):
        assert reader.build(include_retired=include) == expected[include]
    assert calls == Counter(parse_claim=2, parse_claim_type=1, _fact_row=2, _accepted_subjects=1)


def test_live_only_read_does_not_require_retired_law_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    coordinate, tree, retired = _retired_tree(instance)
    history = query._claim_read_history_index(instance, coordinate=coordinate)
    history = replace(
        history,
        law_evidence={path: law for path, law in history.law_evidence.items() if path != retired},
    )
    monkeypatch.setattr(instance, "tree_at", lambda oid: dict(tree))
    monkeypatch.setattr(query, "_claim_read_history_index", lambda *args, **kwargs: history)
    reader = query._AcceptedQueryFactsRead(instance, coordinate=coordinate)
    assert len(reader.build().claims) == 1
    with pytest.raises(ProposalIntegrityError, match="no reproducible Claim law evidence"):
        reader.build(include_retired=True)
    assert len(reader.build().claims) == 1


@pytest.mark.parametrize("include_retired", (False, True))
def test_evidence_lookup_and_claim_parse_keep_original_refusal_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_retired: bool
) -> None:
    instance, _owner = seed_claims(tmp_path)
    coordinate, tree, path = _retired_tree(instance)
    history = query._claim_read_history_index(instance, coordinate=coordinate)
    history = replace(history, law_evidence={})
    tree[path] = b"not a Claim"
    monkeypatch.setattr(instance, "tree_at", lambda oid: dict(tree))
    monkeypatch.setattr(query, "_claim_read_history_index", lambda *args, **kwargs: history)
    calls = []
    original = query.parse_claim

    def counted(content, *, path):
        calls.append(path)
        return original(content, path=path)

    monkeypatch.setattr(query, "parse_claim", counted)
    reader = query._AcceptedQueryFactsRead(instance, coordinate=coordinate)
    if include_retired:
        with pytest.raises(ProposalIntegrityError, match="no reproducible Claim law evidence"):
            reader.build(include_retired=True)
        assert not calls
    else:
        with pytest.raises(Exception) as direct:
            original(tree[path], path=path)
        with pytest.raises(type(direct.value)) as observed:
            reader.build()
        assert str(observed.value) == str(direct.value)
        assert calls == [path]


def test_new_readers_and_public_builds_observe_changed_capture_availability(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    reader = query._AcceptedQueryFactsRead(instance, coordinate=coordinate)
    before = reader.build()
    capture = before.claims[0].captures[0]
    assert capture.current_replay_available
    store = instance.body_store()
    path = store._path(capture.capture_digest)
    original = path.read_bytes()
    path.unlink()
    assert reader.build() == before
    after = query._AcceptedQueryFactsRead(instance, coordinate=coordinate).build()
    assert not after.claims[0].captures[0].current_replay_available
    assert query.build_accepted_query_facts(instance, coordinate=coordinate) == after
    store.store(original)
    assert query.build_accepted_query_facts(instance, coordinate=coordinate) == before

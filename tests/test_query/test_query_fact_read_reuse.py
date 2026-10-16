"""One request shares fact assembly; later requests observe mutable bodies anew."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.claims import parse_claim, render_claim
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.service.discovery import query as query
from tests.core_support._knowledge_loop_support import seed_claims
from tests.core_support._subject_kind_support import fixture_claim_paths


def _retired_tree(instance):
    """A read-source fixture with one retired head, retaining its law history."""

    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    path = fixture_claim_paths(tree)[0]
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
    monkeypatch.setattr(
        instance, "blobs_at", lambda oid, paths: {p: tree[p] for p in paths if p in tree}
    )
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
    # Every Claim the world holds -- the work items' key Claims among them -- and
    # the retired one only when asked.
    live = sum(1 for item in tree if item.startswith("claims/")) - 1
    assert calls["_fact_row"] == live + (1 if first_include_retired else 0)
    assert (
        reader.build(include_retired=not first_include_retired)
        == expected[not first_include_retired]
    )
    for include in (False, True, False, True):
        assert reader.build(include_retired=include) == expected[include]
    assert calls == Counter(
        parse_claim=live + 1,
        parse_claim_type=len({row.accepted.claim.statement.predicate for row in first.claims}),
        _fact_row=live + 1,
        _accepted_subjects=1,
    )


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
    monkeypatch.setattr(
        instance, "blobs_at", lambda oid, paths: {p: tree[p] for p in paths if p in tree}
    )
    monkeypatch.setattr(query, "_claim_read_history_index", lambda *args, **kwargs: history)
    reader = query._AcceptedQueryFactsRead(instance, coordinate=coordinate)
    live = sum(1 for item in tree if item.startswith("claims/")) - 1
    assert len(reader.build().claims) == live
    with pytest.raises(ProposalIntegrityError, match="no reproducible Claim law evidence"):
        reader.build(include_retired=True)
    assert len(reader.build().claims) == live


@pytest.mark.parametrize("include_retired", (False, True))
def test_evidence_lookup_and_claim_parse_keep_original_refusal_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_retired: bool
) -> None:
    instance, _owner = seed_claims(tmp_path)
    coordinate, tree, path = _retired_tree(instance)
    history = query._claim_read_history_index(instance, coordinate=coordinate)
    history = replace(
        history,
        law_evidence={item: law for item, law in history.law_evidence.items() if item != path},
    )
    tree[path] = b"not a Claim"
    monkeypatch.setattr(
        instance, "blobs_at", lambda oid, paths: {p: tree[p] for p in paths if p in tree}
    )
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
        assert path not in calls
    else:
        with pytest.raises(Exception) as direct:
            original(tree[path], path=path)
        with pytest.raises(type(direct.value)) as observed:
            reader.build()
        assert str(observed.value) == str(direct.value)
        assert calls[-1] == path


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
    assert store.availability(capture.capture_digest) == "missing"
    after = query._AcceptedQueryFactsRead(instance, coordinate=coordinate).build()
    assert not after.claims[0].captures[0].current_replay_available
    assert query.build_accepted_query_facts(instance, coordinate=coordinate) == after
    store.store(original)
    assert query.build_accepted_query_facts(instance, coordinate=coordinate) == before


def test_a_live_build_never_reads_retired_claims_and_a_scoped_one_selects_subjects_by_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live facts select live Claims only; a served query's facts select its Subject kinds.

    The retired head is not prefetched or parsed for a live build, a later retired
    build of the same reader still answers as a fresh one, and the scoped build's
    Subjects are its entry kinds' plus those its Claims name.
    """

    from tests.core_support._retirement_support import retire_claim

    instance, owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    retired_path = fixture_claim_paths(tree)[0]
    retired = parse_claim(tree[retired_path], path=retired_path)
    retire_claim(instance, owner, retired.identity.name)
    coordinate = instance.accepted_coordinate()
    whole_live = query.build_accepted_query_facts(instance, coordinate=coordinate)
    whole_all = query.build_accepted_query_facts(
        instance, coordinate=coordinate, include_retired=True
    )
    assert retired_path in {row.accepted.path for row in whole_all.claims}

    parsed: list[str] = []
    original = query.parse_claim

    def counted(content, *, path, **kwargs):  # type: ignore[no-untyped-def]
        parsed.append(path)
        return original(content, path=path, **kwargs)

    monkeypatch.setattr(query, "parse_claim", counted)
    reader = query._AcceptedQueryFactsRead(instance, coordinate=coordinate)
    assert reader.build() == whole_live
    assert retired_path not in parsed
    assert reader.build(include_retired=True) == whole_all

    predicate = retired.statement.predicate
    kinds = {path.split("/")[1] for path in tree if path.startswith("subjects/")}
    for entry_kinds in (tuple(sorted(kinds)), ("absent.kind",)):
        scoped = query.build_accepted_query_facts(
            instance, coordinate=coordinate, predicates=(predicate,), subject_kinds=entry_kinds
        )
        named = {row.subject_path for row in scoped.claims} | {
            row.object_subject_path for row in scoped.claims if row.object_subject_path
        }
        expected = {
            subject.path
            for subject in whole_live.subjects
            if subject.path.split("/")[1] in entry_kinds or subject.path in named
        }
        assert {subject.path for subject in scoped.subjects} == expected
        if entry_kinds == ("absent.kind",):
            # Only the Subjects its Claims name: none of another kind is read.
            assert expected == {item for item in named if item in tree}

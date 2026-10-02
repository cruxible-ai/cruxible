"""Compact ``query`` follows relations backwards: which Subjects point at this one."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import pytest

from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from cruxible_core.service.discovery.compact_query import (
    _CompactPlan,
    _selection,
    service_playbill_query,
)
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.discovery.query_vocabulary import PredicateInfo, QueryVocabulary
from cruxible_core.service.list_pages import PlaybillListCursorMismatch
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._knowledge_loop_support import EVALUATION_TIME, SUBJECT_KIND, seed_claims
from tests.core_support._relation_query_support import (
    BATCH_KIND,
    DELIVERS,
    GOVERNS,
    PARENT,
    seed_relations,
)

WHEN = datetime.fromisoformat(EVALUATION_TIME)
BATCH = {"field": DELIVERS, "as": "batch", "direction": "reverse"}


@pytest.fixture(scope="module")
def instance(tmp_path_factory: pytest.TempPathFactory) -> Any:
    seeded, owner = seed_claims(tmp_path_factory.mktemp("reverse-follow"))
    seed_relations(seeded, owner)
    return seeded


def _query(instance: Any, **fields: Any) -> Any:
    fields.setdefault("evaluation_time", WHEN)
    fields.setdefault("kind", SUBJECT_KIND)
    return service_playbill_query(instance, request=PlaybillQueryRequestV1.model_validate(fields))


def _pairs(result: Any, alias: str = "batch") -> list[tuple[str, str | None]]:
    return sorted((row["subject_id"], row[alias]) for row in result.rows)


def test_a_reverse_follow_answers_which_subjects_point_here(instance: Any) -> None:
    result = _query(instance, follow=[BATCH], select=["batch", "batch.state"])

    # wi-42 is delivered by two batches: one row per (item, batch) pair.
    assert sorted((row["subject_id"], row["batch"], row["batch.state"]) for row in result.rows) == [
        ("wi-42", f"{BATCH_KIND}/b-1", "open"),
        ("wi-42", f"{BATCH_KIND}/b-2", "closed"),
        ("wi-43", f"{BATCH_KIND}/b-1", "open"),
    ]
    assert [(column.name, column.predicate, column.type) for column in result.columns] == [
        ("batch", DELIVERS, "subject"),
        ("batch.state", "project.batch.state", "enum"),
    ]
    assert result.receipt.mode == "inline" and result.truncated is False


def test_a_reverse_field_resolves_by_the_source_kinds_naming_rule(instance: Any) -> None:
    by_full = _query(instance, follow=[BATCH], select=["batch"])
    by_short = _query(
        instance, follow=[{"field": "delivers", "as": "batch", "direction": "reverse"}]
    )

    assert _pairs(by_short) == _pairs(by_full)
    assert by_short.receipt.spec_digest == _query(instance, follow=[BATCH]).receipt.spec_digest
    # A Subject nothing points at keeps its row, with no bound Subject.
    governed = _query(
        instance,
        follow=[{"field": "governs", "as": "decision", "direction": "reverse"}],
        select=["decision"],
    )
    assert _pairs(governed, "decision") == [
        ("wi-42", None),
        ("wi-43", "project.decision/d-1"),
    ]


def test_a_self_relation_reverses_to_its_children(instance: Any) -> None:
    children = _query(
        instance,
        follow=[{"field": "parent", "as": "child", "direction": "reverse"}],
        select=["child"],
    )

    assert _pairs(children, "child") == [
        ("wi-42", f"{SUBJECT_KIND}/wi-43"),
        ("wi-43", None),
    ]


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("project.batch.state", "does not name project.work_item Subjects"),
        ("project.work_item.status", "does not name project.work_item Subjects"),
        ("delivrs", "no predicate named 'delivrs' points at"),
    ],
)
def test_a_reverse_follow_along_a_wrong_predicate_refuses_with_the_incoming_ones(
    instance: Any, field: str, message: str
) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, follow=[{"field": field, "as": "batch", "direction": "reverse"}])

    error = refused.value
    assert error.error_code == "playbill.query.follow_not_incoming"
    assert message in str(error)
    assert error.context["field_path"] == "follow[0].field"
    assert set(error.candidates) <= {DELIVERS, GOVERNS, PARENT}
    assert error.candidates
    assert error.repair_line and "orient kind=project.work_item" in error.repair_line
    if field == "delivrs":
        assert error.candidates[0] == DELIVERS


def test_following_an_incoming_predicate_forward_points_at_reverse(instance: Any) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, follow=[{"field": DELIVERS, "as": "batch"}])

    assert refused.value.error_code == "playbill.query.follow_not_relation"
    assert refused.value.candidates == ("parent",)
    assert refused.value.repair_line is not None
    assert 'direction "reverse"' in refused.value.repair_line


def test_forward_and_reverse_follows_mix(instance: Any) -> None:
    result = _query(
        instance,
        follow=[BATCH, {"field": "parent", "as": "up"}],
        select=["batch", "up"],
    )

    assert sorted((row["subject_id"], row["batch"], row["up"]) for row in result.rows) == [
        ("wi-42", f"{BATCH_KIND}/b-1", None),
        ("wi-42", f"{BATCH_KIND}/b-2", None),
        ("wi-43", f"{BATCH_KIND}/b-1", f"{SUBJECT_KIND}/wi-42"),
    ]
    both = _query(
        instance,
        follow=[BATCH, {"field": "governs", "as": "decision", "direction": "reverse"}],
        select=["batch", "decision"],
    )
    assert sorted((row["subject_id"], row["batch"], row["decision"]) for row in both.rows) == [
        ("wi-42", f"{BATCH_KIND}/b-1", None),
        ("wi-42", f"{BATCH_KIND}/b-2", None),
        ("wi-43", f"{BATCH_KIND}/b-1", "project.decision/d-1"),
    ]


def test_reverse_rows_page_by_cursor_once_each(instance: Any) -> None:
    whole = _query(instance, follow=[BATCH], select=["batch"])
    seen: list[tuple[str, str | None]] = []
    cursor = None
    for _ in range(len(whole.rows) + 1):
        page = _query(instance, follow=[BATCH], select=["batch"], limit=1, cursor=cursor)
        assert len(page.rows) == 1
        seen.extend((row["subject_id"], row["batch"]) for row in page.rows)
        cursor = page.next_cursor
        if cursor is None:
            assert page.truncated is False
            break
        assert page.truncated is True
    assert seen == [(row["subject_id"], row["batch"]) for row in whole.rows]
    assert len(set(seen)) == 3

    # The cursor binds the listing, direction included: another listing refuses it.
    first = _query(instance, follow=[BATCH], select=["batch"], limit=1)
    with pytest.raises(PlaybillListCursorMismatch):
        _query(
            instance,
            follow=[{"field": "governs", "as": "batch", "direction": "reverse"}],
            select=["batch"],
            limit=1,
            cursor=first.next_cursor,
        )


def test_follow_rows_sort_by_the_queried_subject_without_order_by(instance: Any) -> None:
    def rows(result: Any, *aliases: str) -> list[tuple[Any, ...]]:
        return [(row["subject_id"], *(row[alias] for alias in aliases)) for row in result.rows]

    # Reverse: the alias `batch` sorts before `subject`, yet rows group by item.
    assert rows(_query(instance, follow=[BATCH], select=["batch"]), "batch") == [
        ("wi-42", f"{BATCH_KIND}/b-1"),
        ("wi-42", f"{BATCH_KIND}/b-2"),
        ("wi-43", f"{BATCH_KIND}/b-1"),
    ]
    # Forward, and mixed: the queried Subject first, then each alias in request
    # order, an unbound alias before a bound one.
    assert rows(
        _query(instance, follow=[{"field": "parent", "as": "a_up"}], select=["a_up"]), "a_up"
    ) == [("wi-42", None), ("wi-43", f"{SUBJECT_KIND}/wi-42")]
    mixed = _query(
        instance,
        follow=[
            {"field": "governs", "as": "decision", "direction": "reverse"},
            BATCH,
        ],
        select=["decision", "batch"],
    )
    assert rows(mixed, "decision", "batch") == [
        ("wi-42", None, f"{BATCH_KIND}/b-1"),
        ("wi-42", None, f"{BATCH_KIND}/b-2"),
        ("wi-43", "project.decision/d-1", f"{BATCH_KIND}/b-1"),
    ]
    # Every page continues the same order.
    paged: list[tuple[Any, ...]] = []
    cursor = None
    while True:
        page = _query(
            instance,
            follow=[{"field": "governs", "as": "decision", "direction": "reverse"}, BATCH],
            select=["decision", "batch"],
            limit=1,
            cursor=cursor,
        )
        paged.extend(rows(page, "decision", "batch"))
        cursor = page.next_cursor
        if cursor is None:
            break
    assert paged == rows(mixed, "decision", "batch")


def test_order_by_groups_reverse_rows_by_subject(instance: Any) -> None:
    result = _query(instance, follow=[BATCH], select=["batch"], order_by=["subject_id"])

    assert [row["subject_id"] for row in result.rows] == ["wi-42", "wi-42", "wi-43"]


def test_contains_and_where_filter_reverse_rows(instance: Any) -> None:
    # contains matches the queried Subject's own values, never the followed ones.
    ready = _query(instance, follow=[BATCH], select=["batch"], contains="REA")
    assert _pairs(ready) == [("wi-42", f"{BATCH_KIND}/b-1"), ("wi-42", f"{BATCH_KIND}/b-2")]
    assert _query(instance, follow=[BATCH], select=["batch"], contains="open").rows == ()

    # where on the followed Subject's value (lowered) and on its id (inline).
    open_batches = _query(
        instance,
        follow=[BATCH],
        select=["batch"],
        where=[{"field": "batch.state", "eq": "open"}],
    )
    assert _pairs(open_batches) == [("wi-42", f"{BATCH_KIND}/b-1"), ("wi-43", f"{BATCH_KIND}/b-1")]
    by_id = _query(
        instance,
        follow=[BATCH],
        select=["batch"],
        where=[{"field": "batch.subject_id", "contains": "2"}],
    )
    assert _pairs(by_id) == [("wi-42", f"{BATCH_KIND}/b-2")]

    # where on the root, with contains, and on a followed Subject together.
    combined = _query(
        instance,
        follow=[BATCH],
        select=["batch", "status"],
        contains="block",
        where=[{"field": "batch.state", "ne": "closed"}],
    )
    assert [(row["subject_id"], row["batch"], row["status"]) for row in combined.rows] == [
        ("wi-43", f"{BATCH_KIND}/b-1", "blocked")
    ]

    # A followed Subject that does not exist refuses rather than answering empty.
    with pytest.raises(ReadRefusalError) as refused:
        _query(
            instance,
            follow=[BATCH],
            where=[{"field": "batch.subject_id", "eq": "b-9"}],
        )
    assert refused.value.error_code == "playbill.query.unknown_ref"


def test_orient_lists_the_reverse_follows_query_resolves(instance: Any) -> None:
    detail = service_playbill_orient(instance, kind=SUBJECT_KIND, surface="mcp").kind_detail
    assert detail is not None

    assert detail.incoming == (DELIVERS, GOVERNS, PARENT)
    for predicate in detail.incoming:
        result = _query(
            instance,
            follow=[{"field": predicate, "as": "other", "direction": "reverse"}],
            select=["other"],
        )
        assert result.columns[0].predicate == predicate
    # A kind nothing points at has no incoming list on the wire.
    batch = service_playbill_orient(instance, kind=BATCH_KIND).kind_detail
    assert batch is not None and batch.incoming == ()
    assert "incoming" not in batch.model_dump(mode="json")


def test_orient_suggests_a_runnable_reverse_follow_on_every_surface(instance: Any) -> None:
    rendered = {
        surface: service_playbill_orient(instance, kind=SUBJECT_KIND, surface=surface).next
        for surface in ("mcp", "cli", "sdk")
    }

    assert (
        f'cruxible_playbill_query(kind="{SUBJECT_KIND}", follow=[{{"field": "{DELIVERS}", '
        '"as": "batch", "direction": "reverse"}], select=["batch"], limit=10)'
    ) in rendered["mcp"]
    assert (
        f"cruxible playbill query {SUBJECT_KIND} --follow-in {DELIVERS}:batch "
        "--select batch --limit 10"
    ) in rendered["cli"]
    assert (
        f'pb.query(kind="{SUBJECT_KIND}", follow=[{{"field": "{DELIVERS}", "as": "batch", '
        '"direction": "reverse"}], select=["batch"], limit=10)'
    ) in rendered["sdk"]
    # The suggestion runs as written.
    ran = _query(instance, follow=[BATCH], select=["batch"], limit=10)
    assert len(ran.rows) == 3


def _info(predicate: str, *kinds: str, objects: tuple[str, ...] = ()) -> PredicateInfo:
    return PredicateInfo(
        predicate=predicate,
        claim_type=cast(Any, None),
        claim_type_digest="sha256:" + "0" * 64,
        value_type="subject" if objects else "string",
        members=(),
        cardinality="many",
        subject_kinds=kinds,
        object_kinds=objects,
    )


def test_a_short_reverse_field_shared_by_two_source_kinds_is_ambiguous() -> None:
    target = "dev.roadmap_item"
    infos = [
        _info("dev.batch.delivers", "dev.batch", objects=(target,)),
        _info("dev.release.delivers", "dev.release", objects=(target,)),
        _info("dev.batch.delivers_too", "dev.batch", objects=("dev.other",)),
        _info("dev.roadmap_item.title", target),
    ]
    vocabulary = QueryVocabulary(
        predicates={info.predicate: info for info in infos},
        kinds=(target, "dev.batch", "dev.other", "dev.release"),
    )

    def plan(field: str) -> _CompactPlan:
        request = PlaybillQueryRequestV1.model_validate(
            {"kind": target, "follow": [{"field": field, "as": "src", "direction": "reverse"}]}
        )
        return _CompactPlan(vocabulary, request)

    with pytest.raises(ReadRefusalError) as refused:
        plan("delivers")
    assert refused.value.error_code == "playbill.query.ambiguous_field"
    assert refused.value.candidates == ("dev.batch.delivers", "dev.release.delivers")

    follow = plan("dev.release.delivers").follows["src"]
    assert (follow.direction, follow.target_kinds) == ("reverse", ("dev.release",))
    assert follow.lowered_targets == ("dev.release",)
    # A predicate pointing at another kind is not incoming here.
    with pytest.raises(ReadRefusalError) as wrong:
        plan("dev.batch.delivers_too")
    assert wrong.value.error_code == "playbill.query.follow_not_incoming"
    assert set(wrong.value.candidates) <= {"dev.batch.delivers", "dev.release.delivers"}


def test_a_kind_nothing_points_at_says_so() -> None:
    vocabulary = QueryVocabulary(
        predicates={"k.title": _info("k.title", "k")},
        kinds=("k",),
    )
    request = PlaybillQueryRequestV1.model_validate(
        {"kind": "k", "follow": [{"field": "title", "as": "src", "direction": "reverse"}]}
    )
    with pytest.raises(ReadRefusalError) as refused:
        _CompactPlan(vocabulary, request)
    assert refused.value.error_code == "playbill.query.follow_not_incoming"
    assert "no Subject-valued predicate points at k" in str(refused.value)
    assert refused.value.candidates == ()


def test_the_wire_omits_the_default_direction_and_the_cursor_binds_it() -> None:
    forward = PlaybillQueryRequestV1.model_validate(
        {"kind": "k", "follow": [{"field": "parent", "as": "up"}]}
    )
    reverse = PlaybillQueryRequestV1.model_validate({"kind": "k", "follow": [BATCH]})

    assert forward.model_dump(mode="json")["follow"] == [{"field": "parent", "as": "up"}]
    assert reverse.model_dump(mode="json")["follow"] == [BATCH]
    with pytest.raises(ValueError, match="direction"):
        PlaybillQueryRequestV1.model_validate(
            {"kind": "k", "follow": [{"field": "p", "as": "a", "direction": "backwards"}]}
        )
    flipped = PlaybillQueryRequestV1.model_validate(
        {"kind": "k", "follow": [{**BATCH, "direction": "forward"}]}
    )
    assert _selection(flipped, "inline") != _selection(reverse, "inline")


def test_follow_refusals_stay_bounded_as_the_vocabulary_grows() -> None:
    """Refusal candidates are capped, in stable order, however many names qualify."""

    from cruxible_core.service.read_refusals import NEAREST_LIMIT

    target = "t.item"
    sources = [f"src{index:02d}" for index in range(12)]
    infos = [
        *(_info(f"{source}.delivers", source, objects=(target,)) for source in sources),
        *(_info(f"{source}.state", source) for source in sources),
        *(_info(f"{target}.rel{index:02d}", target, objects=tuple(sources)) for index in range(12)),
        _info(f"{target}.title", target),
    ]
    vocabulary = QueryVocabulary(
        predicates={info.predicate: info for info in infos},
        kinds=(target, *sources),
    )

    def refused(**fields: Any) -> ReadRefusalError:
        request = PlaybillQueryRequestV1.model_validate({"kind": target, **fields})
        with pytest.raises(ReadRefusalError) as caught:
            plan = _CompactPlan(vocabulary, request)
            for index, name in enumerate(request.select):
                plan.field(name, field_path=f"select[{index}]")
        return caught.value

    incoming = sorted(f"{source}.delivers" for source in sources)
    relations = sorted(f"rel{index:02d}" for index in range(12))

    # Reverse: a name nothing resembles falls back to the first incoming names.
    unknown = refused(follow=[{"field": "zzzzzzzzzzzz", "as": "a", "direction": "reverse"}])
    assert unknown.error_code == "playbill.query.follow_not_incoming"
    assert unknown.candidates == tuple(incoming[:NEAREST_LIMIT])
    # Reverse: a short name every source kind carries is ambiguous, capped.
    ambiguous = refused(follow=[{"field": "delivers", "as": "a", "direction": "reverse"}])
    assert ambiguous.error_code == "playbill.query.ambiguous_field"
    assert ambiguous.candidates == tuple(incoming[:NEAREST_LIMIT])
    assert "names 12 predicates" in str(ambiguous)

    # Forward: a literal predicate of the kind falls back to the first relations.
    literal = refused(follow=[{"field": "title", "as": "a"}])
    assert literal.error_code == "playbill.query.follow_not_relation"
    assert literal.candidates == tuple(relations[:NEAREST_LIMIT])
    # Forward: an unknown name, and an alias field shared by every target kind.
    assert len(refused(follow=[{"field": "zzzzzzzzzzzz", "as": "a"}]).candidates) <= 5
    shared = refused(follow=[{"field": "rel00", "as": "a"}], select=["a.state"])
    assert shared.error_code == "playbill.query.ambiguous_field"
    assert shared.candidates == tuple(sorted(f"{s}.state" for s in sources)[:NEAREST_LIMIT])

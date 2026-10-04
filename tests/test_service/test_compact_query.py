"""The ``query`` read verb over accepted state: compact, spec and named modes."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cruxible_client.contracts.compact_query import PlaybillQueryRequest
from cruxible_client.contracts.query.definitions import QueryDefinitionSpec
from cruxible_core.service.discovery import compact_query as compact_module
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.discovery.query_vocabulary import (
    PredicateInfo,
    QueryVocabulary,
)
from cruxible_core.service.list_pages import PlaybillListCursorMismatch
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._candidate_support import submit_query_definition_candidate
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    QUERY_NAME,
    SUBJECT_KIND,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)

WHEN = datetime.fromisoformat(EVALUATION_TIME)


@pytest.fixture(scope="module")
def instance(tmp_path_factory: pytest.TempPathFactory) -> Any:
    tmp_path = tmp_path_factory.mktemp("compact-query")
    seeded, owner = seed_claims(tmp_path)
    inspection = submit_query_definition_candidate(
        seeded,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(seeded, owner, inspection)
    return seeded


def _query(instance: Any, **fields: Any) -> Any:
    fields.setdefault("evaluation_time", WHEN)
    return service_playbill_query(instance, request=PlaybillQueryRequest.model_validate(fields))


def _ids(result: Any) -> list[str]:
    return [row["subject_id"] for row in result.rows]


def test_compact_filter_lowers_to_an_inline_definition_and_answers_values(instance: Any) -> None:
    result = _query(
        instance,
        kind=SUBJECT_KIND,
        where=[{"field": "status", "eq": "ready"}],
        select=["status"],
    )

    assert _ids(result) == ["wi-42"]
    assert result.rows[0] == {
        "subject": f"{SUBJECT_KIND}/wi-42",
        "subject_id": "wi-42",
        "status": "ready",
        "flags": [],
    }
    assert [(column.name, column.type, column.cardinality) for column in result.columns] == [
        ("status", "enum", "one")
    ]
    assert result.columns[0].members == ("blocked", "done", "ready")
    assert result.receipt.mode == "inline"
    assert result.receipt.spec_digest.startswith("sha256:")
    assert result.receipt.evaluation_time == WHEN
    assert result.truncated is False and result.next_cursor is None


def test_qualified_field_ne_in_and_exists(instance: Any) -> None:
    assert _ids(
        _query(instance, kind=SUBJECT_KIND, where=[{"field": PREDICATE, "ne": "ready"}])
    ) == ["wi-43"]
    both = _query(
        instance, kind=SUBJECT_KIND, where=[{"field": "status", "in": ["ready", "blocked"]}]
    )
    assert _ids(both) == ["wi-42", "wi-43"]
    assert (
        _ids(_query(instance, kind=SUBJECT_KIND, where=[{"field": "status", "exists": False}]))
        == []
    )
    ordered = _query(instance, kind=SUBJECT_KIND, order_by=["-status"])
    assert _ids(ordered) == ["wi-42", "wi-43"]


def test_inline_filters_and_contains_never_enter_the_definition(instance: Any) -> None:
    by_id = _query(instance, kind=SUBJECT_KIND, where=[{"field": "subject_id", "contains": "43"}])
    assert _ids(by_id) == ["wi-43"]
    by_text = _query(instance, kind=SUBJECT_KIND, contains="REA")
    assert _ids(by_text) == ["wi-42"]
    plain = _query(instance, kind=SUBJECT_KIND)
    assert by_text.receipt.spec_digest == plain.receipt.spec_digest


def test_contains_without_kind_searches_every_live_claim_value(instance: Any) -> None:
    result = _query(instance, contains="block")

    assert [column.name for column in result.columns] == ["kind", "predicate", "value", "claim"]
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["kind"] == SUBJECT_KIND
    assert row["predicate"] == PREDICATE
    assert row["value"] == "blocked"
    assert row["subject"] == f"{SUBJECT_KIND}/wi-43"
    assert row["claim"].startswith("CLM-")


@pytest.mark.parametrize(
    ("fields", "code", "nearest"),
    [
        ({"kind": "project.work_itme"}, "playbill.query.unknown_kind", SUBJECT_KIND),
        (
            {"kind": SUBJECT_KIND, "where": [{"field": "stauts", "eq": "ready"}]},
            "playbill.query.unknown_field",
            "status",
        ),
        (
            {"kind": SUBJECT_KIND, "where": [{"field": "status", "eq": "redy"}]},
            "playbill.query.unknown_member",
            "ready",
        ),
        (
            {"kind": SUBJECT_KIND, "where": [{"field": "status", "lt": "ready"}]},
            "playbill.query.operator_not_applicable",
            "eq",
        ),
        (
            {"kind": SUBJECT_KIND, "select": ["statsu"]},
            "playbill.query.unknown_field",
            "status",
        ),
    ],
)
def test_wrong_names_refuse_with_the_nearest_valid_ones(
    instance: Any, fields: dict[str, Any], code: str, nearest: str
) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, **fields)

    assert refused.value.error_code == code
    assert refused.value.http_status == 400
    assert nearest in refused.value.candidates
    assert refused.value.repair_line and refused.value.repair_line in str(refused.value)
    assert refused.value.context["candidates"]
    assert refused.value.context["field_path"]


def test_an_unaccepted_at_refuses_with_the_shared_read_code(instance: Any) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, kind=SUBJECT_KIND, at="0" * 40)

    assert refused.value.error_code == "playbill.read.coordinate_not_accepted"
    assert refused.value.http_status == 404
    assert refused.value.context["field_path"] == "at"


def test_exactly_one_mode(instance: Any) -> None:
    with pytest.raises(ReadRefusalError, match="playbill.query.mode_invalid"):
        _query(instance, kind=SUBJECT_KIND, name=QUERY_NAME)
    with pytest.raises(ReadRefusalError, match="playbill.query.mode_invalid"):
        _query(instance)
    with pytest.raises(ReadRefusalError, match="playbill.query.mode_invalid"):
        _query(instance, name=QUERY_NAME, select=["status"])


def test_pages_continue_by_cursor_and_refuse_a_foreign_one(instance: Any) -> None:
    first = _query(instance, kind=SUBJECT_KIND, limit=1)
    assert _ids(first) == ["wi-42"]
    assert first.truncated is True and first.next_cursor is not None

    second = _query(instance, kind=SUBJECT_KIND, limit=1, cursor=first.next_cursor)
    assert _ids(second) == ["wi-43"]
    assert second.truncated is False and second.next_cursor is None
    assert second.receipt.coordinate == first.receipt.coordinate

    with pytest.raises(PlaybillListCursorMismatch):
        _query(
            instance,
            kind=SUBJECT_KIND,
            where=[{"field": "status", "exists": True}],
            limit=1,
            cursor=first.next_cursor,
        )


def test_claim_type_rows_name_capture_contracts_never_digests(instance: Any) -> None:
    result = _query(instance, kind="ClaimType", where=[{"field": "namespace", "eq": SUBJECT_KIND}])

    assert [row["predicate"] for row in result.rows] == [PREDICATE]
    row = result.rows[0]
    assert row["object"] == "enum"
    assert row["members"] == ["blocked", "done", "ready"]
    assert row["cardinality"] == "one"
    assert row["subject_kinds"] == [SUBJECT_KIND]
    assert row["evidence"]
    assert all(not name.startswith("sha256:") for name in row["evidence"])
    assert "description" not in row

    narrowed = _query(instance, kind="ClaimType", select=["predicate", "evidence"])
    assert set(narrowed.rows[0]) == {"predicate", "evidence"}

    with pytest.raises(ReadRefusalError, match="playbill.query.unknown_field"):
        _query(instance, kind="ClaimType", where=[{"field": "predicat", "eq": "x"}])


def test_named_query_runs_as_run_query_does(instance: Any) -> None:
    result = _query(instance, name=QUERY_NAME)

    assert result.receipt.mode == "named"
    assert [column.name for column in result.columns] == ["item_id", "status"]
    assert [(row["item_id"], row["status"]) for row in result.rows] == [
        ("wi-42", "ready"),
        ("wi-43", "blocked"),
    ]

    with pytest.raises(ReadRefusalError) as missing:
        _query(instance, name="project.work_itmes")
    assert missing.value.error_code == "playbill.query.name_not_found"
    assert missing.value.http_status == 404
    assert QUERY_NAME in missing.value.candidates

    with pytest.raises(ReadRefusalError, match="playbill.query.parameter_undeclared"):
        _query(instance, name=QUERY_NAME, params={"stray": "x"})


def test_spec_query_pins_claim_types_at_the_coordinate(instance: Any) -> None:
    declared = work_item_query("project.adhoc")
    spec = QueryDefinitionSpec.model_validate({**declared.model_dump(mode="json"), "pins": []})
    result = _query(instance, spec=spec)

    assert result.receipt.mode == "spec"
    assert [row["status"] for row in result.rows] == ["ready", "blocked"]


def test_flags_come_from_the_verdict_machinery(instance: Any, monkeypatch: Any) -> None:
    from cruxible_core.service.discovery.read_flags import ClaimRead

    def flagged(*_args: Any, identities: Any, **_kwargs: Any) -> dict[str, ClaimRead]:
        return {
            identity: ClaimRead(flags=("stale", "unsure_hold"), verdict="stale", status="accepted")
            for identity in identities
        }

    monkeypatch.setattr(compact_module, "claim_reads", flagged)
    result = _query(instance, kind=SUBJECT_KIND, select=["status"])

    assert [row["flags"] for row in result.rows] == [
        ["stale", "unsure_hold"],
        ["stale", "unsure_hold"],
    ]


def test_a_contested_slot_shows_every_live_value(tmp_path: Path) -> None:
    from cruxible_client.contracts.captures import DirectForeignSourceSelection
    from cruxible_client.contracts.semantic import ContentSpan
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._knowledge_loop_support import activate, authoring

    seeded, owner = seed_claims(tmp_path)
    body = seeded.body_store().store(b"status: done")
    proposed = service_propose_playbill_claim(
        seeded,
        authoring=authoring("wi-42", "done", with_claim_type=False).model_copy(
            update={
                "source_selection": DirectForeignSourceSelection(
                    logical_source_identity="fixture.work-items",
                    span=ContentSpan(
                        content_digest=body.digest, start_byte=0, end_byte=len(b"status: done")
                    ),
                )
            }
        ),
        actor_id="owner",
        proposal_name="seed-contender",
        timestamp=TIMESTAMP,
    )
    activate(seeded, owner, proposed)

    result = _query(seeded, kind=SUBJECT_KIND, select=["status"])
    row = result.rows[0]
    assert row["subject_id"] == "wi-42"
    assert row["status"] == ["done", "ready"]
    assert "contested" in row["flags"]
    # A contested slot never matches a value filter; it stays visible unfiltered.
    assert _ids(_query(seeded, kind=SUBJECT_KIND, where=[{"field": "status", "eq": "ready"}])) == []
    # ... and never any other value filter: ne, in, or an inline contains.
    for where in (
        [{"field": "status", "ne": "blocked"}],
        [{"field": "status", "ne": "ready"}],
        [{"field": "status", "in": ["ready", "done"]}],
        [{"field": "status", "contains": "rea"}],
    ):
        assert "wi-42" not in _ids(_query(seeded, kind=SUBJECT_KIND, where=where)), where
    assert _ids(_query(seeded, kind=SUBJECT_KIND, where=[{"field": "status", "ne": "ready"}])) == [
        "wi-43"
    ]
    assert _ids(_query(seeded, kind=SUBJECT_KIND, contains="rea")) == []


def test_evidence_names_resolve_digests_and_read_identity_rules() -> None:
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
    from cruxible_client.contracts.claim_types import ClaimType
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )
    from cruxible_core.service.discovery.contract_names import CaptureContractNames
    from tests.test_claims.test_claims import _claim_type

    legacy = _claim_type()
    digest = legacy.evidence_admission_policy.rules[0].capture_contract_digests[0]
    names = CaptureContractNames.__new__(CaptureContractNames)
    names._versions = {}
    names._names = {}
    names._lineages = {}
    names._connection = None
    names._at = None  # type: ignore[assignment]
    names._instance = SimpleNamespace(  # type: ignore[assignment]
        accepted_capture_contract_version=lambda _at, found: (
            SimpleNamespace(contract=SimpleNamespace(identity=SimpleNamespace(name="direct")))
            if found == digest
            else None
        )
    )
    assert names.admitted(legacy) == ("direct",)
    assert names.admitted(legacy, qualified=True) == ("CaptureContract:direct",)
    assert names.name("sha256:" + "ab" * 32) == "unresolved:" + "ab" * 6

    identity_rule = ClaimEvidenceAdmissionRule(
        rule_id="by-identity",
        claim_roles=("observation",),
        evidence_kinds=("self_asserted",),
        admission="direct",
        subject_binding="exact_claim_subject",
        capture_contracts=(
            ArtifactRef(
                role="capture-contract",
                target=ArtifactIdentity(kind="CaptureContract", name="fixture.reports"),
            ),
        ),
    )
    current = ClaimType.model_validate(
        {
            **legacy.model_dump(mode="json"),
            "artifact_format": "playbill-claim-type-v6",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(identity_rule,)
            ).model_dump(mode="json"),
        }
    )
    assert names.admitted(current) == ("fixture.reports",)
    assert names.names_by_digest(legacy) and not names.names_by_digest(current)


def test_a_filter_naming_a_missing_subject_refuses(instance: Any) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, kind=SUBJECT_KIND, where=[{"field": "subject_id", "eq": "wi-44"}])

    assert refused.value.error_code == "playbill.query.unknown_ref"
    assert "wi-42" in refused.value.candidates or "wi-43" in refused.value.candidates
    assert _ids(
        _query(instance, kind=SUBJECT_KIND, where=[{"field": "subject_id", "in": ["wi-43"]}])
    ) == ["wi-43"]


def test_a_definition_filter_naming_an_unknown_namespace_refuses(instance: Any) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, kind="ClaimType", where=[{"field": "namespace", "eq": "project.work_itm"}])

    assert refused.value.error_code == "playbill.query.unknown_ref"
    assert SUBJECT_KIND in refused.value.candidates


def _contend(instance: Any, owner: Any, against: Any, value: str, name: str) -> None:
    from cruxible_client.contracts.claims import claim_statement_digest
    from tests.core_support._claim_authoring_support import (
        ExistingStatementHandoffV1,
        service_propose_playbill_claim,
    )
    from tests.core_support._knowledge_loop_support import activate, authoring

    activate(
        instance,
        owner,
        service_propose_playbill_claim(
            instance,
            authoring=authoring("wi-42", value, with_claim_type=False).model_copy(
                update={
                    "existing_statement_handoffs": (
                        ExistingStatementHandoffV1(
                            statement_digest=claim_statement_digest(against.statement).tagged,
                            disposition="contradict",
                        ),
                    )
                }
            ),
            actor_id="owner",
            proposal_name=name,
            timestamp="2026-08-24T17:00:03.000000Z",
        ),
    )


def test_unsure_hold_follows_next_and_ends_when_a_new_contender_arrives(tmp_path: Path) -> None:
    from tests.test_integration.test_next_closed_loop import EVALUATION_TIME as LATER
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _all_claims, _attest, _next, _rows

    seeded, owner = seed_claims(tmp_path)
    first = _current_claim(seeded)
    _contend(seeded, owner, first, "blocked", "hold-conflict")
    contenders = [
        claim
        for claim in _all_claims(seeded)
        if claim.statement.subject.artifact_path.endswith("wi-42.json")
    ]
    for offset, claim in enumerate(contenders):
        _attest(seeded, owner, claim, tmp_path, at=LATER - timedelta(minutes=2 - offset))

    def flags() -> list[str]:
        result = _query(
            seeded,
            kind=SUBJECT_KIND,
            where=[{"field": "subject_id", "eq": "wi-42"}],
            select=["status"],
            evaluation_time=LATER,
        )
        return list(result.rows[0]["flags"])

    assert not _rows(_next(seeded), "claim_conflicted")
    assert "unsure_hold" in flags()

    # A contender nobody examined brings the conflict back; the flag follows next.
    _contend(seeded, owner, contenders[-1], "done", "hold-conflict-new")
    assert _rows(_next(seeded), "claim_conflicted")
    assert "unsure_hold" not in flags()


def test_parallel_relation_paths_page_once_per_bound_pair() -> None:
    from cruxible_core.service.discovery.compact_query import _bound_rows
    from cruxible_core.service.list_pages import list_snapshot, page_after_boundary

    def row(*bound: tuple[str, str | None]) -> Any:
        return SimpleNamespace(
            bindings=tuple(SimpleNamespace(binding=name, subject_path=path) for name, path in bound)
        )

    # Two relation Claims bind the same (card, batch) pair: two engine paths.
    rows = [
        row(("subject", "subjects/k/a.json"), ("parent", "subjects/p/x.json")),
        row(("subject", "subjects/k/a.json"), ("parent", "subjects/p/x.json")),
        row(("subject", "subjects/k/b.json"), ("parent", None)),
    ]
    candidates, keys = _bound_rows(rows, ("subject", "parent"))
    assert len(candidates) == len(set(keys)) == 2

    seen: list[tuple[str, ...]] = []
    continuation = None
    snapshot = list_snapshot([list(key) for key in keys])
    while True:
        page, truncated = page_after_boundary(
            candidates,
            keys=keys,
            snapshot=snapshot,
            continuation=continuation,
            limit=1,
            list_name="query",
        )
        seen.extend(keys[candidates.index(item)] for item in page)
        if not truncated:
            break
        continuation = SimpleNamespace(snapshot=snapshot, last_key=seen[-1])
        assert len(seen) <= 2, "paging must advance"
    assert seen == keys


def test_a_continuation_refuses_a_different_evaluation_time(instance: Any) -> None:
    from cruxible_core.service.list_pages import PlaybillListCursorStale

    first = _query(instance, kind=SUBJECT_KIND, limit=1)
    same = _query(instance, kind=SUBJECT_KIND, limit=1, cursor=first.next_cursor)
    assert _ids(same) == ["wi-43"]
    with pytest.raises(PlaybillListCursorStale, match="evaluation"):
        _query(
            instance,
            kind=SUBJECT_KIND,
            limit=1,
            cursor=first.next_cursor,
            evaluation_time=WHEN + timedelta(days=1),
        )


@contextmanager
def _finishes_within(seconds: int) -> Iterator[None]:
    """Turn a hang into a failure: the cursor encoder once looped forever."""

    def expired(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"did not finish within {seconds}s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_a_cursor_pins_an_evaluation_time_before_1970(instance: Any) -> None:
    before_epoch = datetime(1969, 12, 31, tzinfo=UTC)

    with _finishes_within(20):
        first = _query(instance, kind=SUBJECT_KIND, limit=1, evaluation_time=before_epoch)
        assert first.truncated is True and first.next_cursor is not None
        second = _query(
            instance, kind=SUBJECT_KIND, limit=1, cursor=first.next_cursor, evaluation_time=None
        )

    assert _ids(first) + _ids(second) == ["wi-42", "wi-43"]
    assert second.receipt.evaluation_time == before_epoch


@pytest.mark.parametrize(
    "outside",
    [
        # Year one local time, an hour before year one in UTC.
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        # The last local microsecond of 9999, an hour past it in UTC: a cursor
        # minted for it could not be continued.
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone(timedelta(hours=-1))),
    ],
)
def test_an_instant_outside_the_utc_range_refuses_typed(instance: Any, outside: datetime) -> None:
    with pytest.raises(ReadRefusalError) as refused:
        _query(instance, kind=SUBJECT_KIND, limit=1, evaluation_time=outside)
    assert refused.value.error_code == "playbill.query.evaluation_time_invalid"


def test_a_cursor_pins_the_last_utc_instant(instance: Any) -> None:
    last = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    first = _query(instance, kind=SUBJECT_KIND, limit=1, evaluation_time=last)
    second = _query(
        instance, kind=SUBJECT_KIND, limit=1, cursor=first.next_cursor, evaluation_time=None
    )
    assert _ids(first) + _ids(second) == ["wi-42", "wi-43"]
    assert second.receipt.evaluation_time == last


@pytest.mark.parametrize(
    ("part", "forged"),
    [
        (2, "z" * 50),  # an instant past any datetime: int() once overflowed timedelta
        (2, "z" * 12),  # in length, but still past the last representable instant
        (5, "\u00b2"),  # a superscript two: str.isdigit() admits it, int() refuses
        (5, "1" * 20),  # an offset longer than any answer
    ],
)
def test_a_malformed_cursor_refuses_as_a_typed_mismatch(
    instance: Any, part: int, forged: str
) -> None:
    first = _query(instance, kind=SUBJECT_KIND, limit=1)
    parts = first.next_cursor.split(".")
    parts[part] = forged

    with pytest.raises(PlaybillListCursorMismatch, match="not a query cursor"):
        _query(instance, kind=SUBJECT_KIND, limit=1, cursor=".".join(parts))


def test_a_column_named_like_row_metadata_keeps_its_values(instance: Any) -> None:
    declared = work_item_query("project.collide").model_dump(mode="json")
    fields = declared["projection"]["fields"]
    fields[0]["name"], fields[1]["name"] = "subject", "flags"
    declared["projection"]["fields"] = sorted(fields, key=lambda item: item["name"])
    spec = QueryDefinitionSpec.model_validate({**declared, "pins": []})

    result = _query(instance, spec=spec)

    assert [column.name for column in result.columns] == ["value.flags", "value.subject"]
    assert result.rows[0]["subject"] == f"{SUBJECT_KIND}/wi-42"
    assert result.rows[0]["value.flags"] == "ready"
    assert result.rows[0]["value.subject"] == "wi-42"
    assert result.rows[0]["flags"] == []


@pytest.mark.parametrize(
    ("value_type", "cell", "wanted", "matched"),
    [
        (
            "timestamp",
            ["2026-09-01T00:00:00Z", "2026-09-02T12:00:00Z"],
            ("2026-09-02T12:00:00+00:00",),
            True,
        ),
        ("timestamp", ["2026-09-01T00:00:00Z"], ("2026-09-03T00:00:00Z",), False),
        ("decimal", [1, 3], ("3.0",), True),
        ("boolean", [True], (1,), False),
        ("json", [[1]], ("[1]",), False),
        ("json", [{"a": 1}], ("{'a': 1}",), False),
        ("json", [{"a": 1}], ('{"a":1}',), False),
        ("json", ["[1]"], ([1],), False),
        ("json", [[1]], ([1],), True),
        ("json", [{"b": [True], "a": 1}], ({"a": 1, "b": [True]},), True),
        ("json", [{"a": [True]}], ({"a": [1]},), False),
    ],
)
def test_inline_in_compares_temporal_and_decimal_values(
    value_type: str, cell: list[object], wanted: tuple[object, ...], matched: bool
) -> None:
    from cruxible_core.service.discovery.compact_query import (
        _Field,
        _inline_matches,
        _InlineFilter,
    )

    info = SimpleNamespace(value_type=value_type, cardinality="many", predicate="k.p")
    item = _InlineFilter(
        field=_Field(name="p", binding="subject", info=info, label="k.p"),  # type: ignore[arg-type]
        operator="in",
        value=wanted,
    )
    assert _inline_matches(item, cell) is matched


def test_named_mode_applies_the_server_ceiling(instance: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(compact_module, "COMPACT_QUERY_MAX_RESULTS", 1)

    result = _query(instance, name=QUERY_NAME)

    assert len(result.rows) == 1
    assert result.capped == ("max_results=1",)
    assert result.truncated is True


def _predicate_info(predicate: str, *kinds: str) -> PredicateInfo:
    return PredicateInfo(
        predicate=predicate,
        claim_type=cast(Any, None),
        claim_type_digest="sha256:" + "0" * 64,
        value_type="string",
        members=(),
        cardinality="one",
        subject_kinds=kinds,
        object_kinds=(),
    )


def test_every_shown_field_name_resolves_back_to_its_predicate() -> None:
    """Addendum 2: one naming rule, round-tripped over the whole vocabulary."""

    kind = SUBJECT_KIND
    infos = [
        _predicate_info(PREDICATE, kind),
        _predicate_info("other.status", kind, "other"),
        _predicate_info(f"{kind}.other.status", kind),
        _predicate_info("third.status", "third"),
        _predicate_info("third.other.status", "third"),
        _predicate_info("sec.vuln.severity", kind),
        _predicate_info(f"{kind}.subject_id", kind),
        _predicate_info(f"{kind}.flags", kind),
        _predicate_info("value.flags", kind),
    ]
    vocabulary = QueryVocabulary(
        predicates={info.predicate: info for info in infos},
        kinds=(kind, "other", "third"),
    )

    for owner in vocabulary.kinds:
        for info in vocabulary.predicates_of(owner):
            shown = vocabulary.field_name(info, (owner,))
            assert shown == short_field_name(info.predicate, owner, vocabulary.predicates)
            resolved = vocabulary.resolve_field((owner,), shown, field_path="select[0]")
            assert not isinstance(resolved, str) and resolved.predicate == info.predicate

    # A follow over several target kinds: every shown name resolves back too, and
    # an exact full name applicable to any target wins over prefix expansion.
    targets = (kind, "third")
    for info in {item for owner in targets for item in vocabulary.predicates_of(owner)}:
        shown = vocabulary.field_name(info, targets)
        resolved = vocabulary.resolve_field(targets, shown, field_path="select[0]")
        assert not isinstance(resolved, str) and resolved.predicate == info.predicate
    exact = vocabulary.resolve_field(targets, "other.status", field_path="select[0]")
    assert not isinstance(exact, str) and exact.predicate == "other.status"

    assert vocabulary.field_name(vocabulary.predicates[PREDICATE], (kind,)) == "status"
    # The short form of project.work_item.other.status is another predicate's full name.
    collided = vocabulary.predicates[f"{kind}.other.status"]
    assert vocabulary.field_name(collided, (kind,)) == f"{kind}.other.status"
    # Shortening never produces a reserved name, and a reserved name keeps its
    # reserved meaning while no accepted predicate is fully named it.
    for reserved in (f"{kind}.subject_id", f"{kind}.flags"):
        assert vocabulary.field_name(vocabulary.predicates[reserved], (kind,)) == reserved
    assert vocabulary.resolve_field((kind,), "subject_id", field_path="select[0]") == "subject_id"
    named_so = QueryVocabulary(
        predicates={"subject_id": _predicate_info("subject_id", kind)}, kinds=(kind,)
    )
    resolved = named_so.resolve_field((kind,), "subject_id", field_path="select[0]")
    assert not isinstance(resolved, str) and resolved.predicate == "subject_id"
    # No last-segment form: `severity` is not project.work_item.severity.
    with pytest.raises(ReadRefusalError) as refused:
        vocabulary.resolve_field((kind,), "severity", field_path="where[0].field")
    assert refused.value.error_code == "playbill.query.unknown_field"
    assert "sec.vuln.severity" in refused.value.candidates


def test_query_columns_use_the_shared_short_name(instance: Any) -> None:
    by_full = _query(instance, kind=SUBJECT_KIND, select=[PREDICATE], order_by=[f"-{PREDICATE}"])

    assert [column.name for column in by_full.columns] == ["status"]
    assert [row["status"] for row in by_full.rows] == ["ready", "blocked"]
    assert _query(instance, kind=SUBJECT_KIND).columns[0].name == "status"


def _follow_vocabulary() -> QueryVocabulary:
    parent = PredicateInfo(
        predicate=f"{SUBJECT_KIND}.parent",
        claim_type=cast(Any, None),
        claim_type_digest="sha256:" + "0" * 64,
        value_type="subject",
        members=(),
        cardinality="one",
        subject_kinds=(SUBJECT_KIND,),
        object_kinds=("project.batch",),
    )
    infos = [
        parent,
        _predicate_info(PREDICATE, SUBJECT_KIND),
        _predicate_info(f"{SUBJECT_KIND}.flags", SUBJECT_KIND),
        _predicate_info("value.flags", SUBJECT_KIND),
        _predicate_info("project.batch.status", "project.batch"),
    ]
    return QueryVocabulary(
        predicates={info.predicate: info for info in infos},
        kinds=(SUBJECT_KIND, "project.batch"),
    )


def _columns(**fields: Any) -> list[tuple[str, str | None]]:
    from cruxible_core.service.discovery.compact_query import _compact_columns, _CompactPlan

    request = PlaybillQueryRequest.model_validate({"kind": SUBJECT_KIND, **fields})
    _columns_, output, _notes = _compact_columns(
        _CompactPlan(_follow_vocabulary(), request), request
    )
    keys = [column.name for column in output]
    assert len(keys) == len(set(keys)), keys
    return [(column.name, column.predicate) for column in output]


def test_two_columns_never_share_a_row_key() -> None:
    """Addendum 3: every column key maps to exactly one column."""

    follow = [{"field": "parent", "as": "status"}]
    # The alias keeps `status`; the predicate whose short name it took falls back.
    assert _columns(follow=follow, select=[PREDICATE, "status"]) == [
        (PREDICATE, PREDICATE),
        ("status", f"{SUBJECT_KIND}.parent"),
    ]
    by_default = dict(_columns(follow=follow))
    assert by_default["status"] == f"{SUBJECT_KIND}.parent"
    assert by_default[PREDICATE] == PREDICATE
    assert by_default[f"{SUBJECT_KIND}.flags"] == f"{SUBJECT_KIND}.flags"
    assert by_default["value.flags"] == "value.flags"
    # A follow field keeps its alias prefix and never meets a root column.
    assert _columns(follow=follow, select=["status.status", PREDICATE]) == [
        ("status.status", "project.batch.status"),
        ("status", PREDICATE),
    ]

    # An alias escaped under value.flags meets the predicate value.flags, and
    # neither has another name: refuse rather than serve one over the other.
    with pytest.raises(ReadRefusalError) as refused:
        _columns(follow=[{"field": "parent", "as": "flags"}], select=["value.flags", "flags"])
    assert refused.value.error_code == "playbill.query.column_collision"


def test_spec_mode_clips_path_budgets_at_execution_not_in_the_spec(
    instance: Any, monkeypatch: Any
) -> None:
    declared = work_item_query("project.pathy").model_dump(mode="json")
    declared.update(
        traversal=[
            {
                "binding": "next",
                "from_binding": "item",
                "predicate": PREDICATE,
                "direction": "forward",
            }
        ],
        result_shape="path",
        dedupe="path",
        default_budgets={
            "max_results": 10,
            "max_traversal_depth": 1,
            "max_paths": 10,
            "max_paths_per_result": 10,
        },
        maximum_budgets={
            "max_results": 50,
            "max_traversal_depth": 1,
            "max_paths": 50,
            "max_paths_per_result": 50,
        },
    )
    spec = QueryDefinitionSpec.model_validate({**declared, "pins": []})
    seen: list[tuple[str, Any]] = []

    class Evaluated(Exception):
        pass

    def evaluate(_instance: Any, definition: Any, **values: Any) -> Any:
        seen.append((definition.artifact_digest, values.get("budgets")))
        raise Evaluated

    monkeypatch.setattr(compact_module, "evaluate_accepted_query", evaluate)
    with pytest.raises(Evaluated):
        _query(instance, spec=spec)
    monkeypatch.setattr(compact_module, "COMPACT_QUERY_MAX_RESULTS", 1)
    with pytest.raises(Evaluated):
        _query(instance, spec=spec)

    (digest, _), (clipped_digest, budgets) = seen
    assert clipped_digest == digest
    assert (budgets.max_results, budgets.max_paths, budgets.max_paths_per_result) == (1, 1, 1)
    assert budgets.max_traversal_depth == 1

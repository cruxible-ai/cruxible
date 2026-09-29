"""The ``query`` read verb over accepted state: compact, spec and named modes."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from cruxible_client.contracts.query.definitions import QueryDefinitionSpecV1
from cruxible_core.service.discovery import compact_query as compact_module
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.query_values import LiveValue, unsure_holds
from cruxible_core.service.discovery.query_vocabulary import (
    PlaybillQueryNotFound,
    PlaybillQueryRefused,
)
from cruxible_core.service.list_pages import PlaybillListCursorMismatch
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
    return service_playbill_query(instance, request=PlaybillQueryRequestV1.model_validate(fields))


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
    with pytest.raises(PlaybillQueryRefused) as refused:
        _query(instance, **fields)

    assert refused.value.error_code == code
    assert nearest in refused.value.nearest
    assert "repair:" in str(refused.value)
    assert refused.value.served_context["nearest"]


def test_exactly_one_mode(instance: Any) -> None:
    with pytest.raises(PlaybillQueryRefused, match="playbill.query.mode_invalid"):
        _query(instance, kind=SUBJECT_KIND, name=QUERY_NAME)
    with pytest.raises(PlaybillQueryRefused, match="playbill.query.mode_invalid"):
        _query(instance)
    with pytest.raises(PlaybillQueryRefused, match="playbill.query.mode_invalid"):
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

    with pytest.raises(PlaybillQueryRefused, match="playbill.query.unknown_field"):
        _query(instance, kind="ClaimType", where=[{"field": "predicat", "eq": "x"}])


def test_named_query_runs_as_run_query_does(instance: Any) -> None:
    result = _query(instance, name=QUERY_NAME)

    assert result.receipt.mode == "named"
    assert [column.name for column in result.columns] == ["item_id", "status"]
    assert [(row["item_id"], row["status"]) for row in result.rows] == [
        ("wi-42", "ready"),
        ("wi-43", "blocked"),
    ]

    with pytest.raises(PlaybillQueryNotFound) as missing:
        _query(instance, name="project.work_itmes")
    assert QUERY_NAME in missing.value.nearest

    with pytest.raises(PlaybillQueryRefused, match="playbill.query.parameter_undeclared"):
        _query(instance, name=QUERY_NAME, params={"stray": "x"})


def test_spec_query_pins_claim_types_at_the_coordinate(instance: Any) -> None:
    declared = work_item_query("project.adhoc")
    spec = QueryDefinitionSpecV1.model_validate({**declared.model_dump(mode="json"), "pins": []})
    result = _query(instance, spec=spec)

    assert result.receipt.mode == "spec"
    assert [row["status"] for row in result.rows] == ["ready", "blocked"]


def test_flags_come_from_the_verdict_machinery(instance: Any, monkeypatch: Any) -> None:
    def flagged(*_args: Any, claims: Any, **_kwargs: Any) -> dict[str, set[str]]:
        return {item.identity: {"stale", "unsure_hold"} for item in claims}

    monkeypatch.setattr(compact_module, "claim_flags", flagged)
    result = _query(instance, kind=SUBJECT_KIND, select=["status"])

    assert [row["flags"] for row in result.rows] == [
        ["stale", "unsure_hold"],
        ["stale", "unsure_hold"],
    ]


def test_unsure_hold_lapses_at_valid_until_or_the_hold_period() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE attestations (claim_identity TEXT, claim_artifact_digest TEXT, "
        "principal_id TEXT, basis TEXT, stance TEXT, attested_at_us INTEGER, "
        "valid_until_us INTEGER)"
    )
    day = 86_400_000_000
    now = datetime.fromisoformat("2026-09-28T00:00:00+00:00")
    now_us = int(now.timestamp()) * 1_000_000
    rows = [
        ("Claim:A", "d-a", "p1", "examined_existing", "unsure", now_us - day, None),
        ("Claim:B", "d-b", "p1", "examined_existing", "unsure", now_us - 40 * day, None),
        ("Claim:C", "d-c", "p1", "examined_existing", "unsure", now_us - day, now_us - 1),
        ("Claim:D", "d-d", "p1", "examined_existing", "unsure", now_us - 2 * day, None),
        ("Claim:D", "d-d", "p1", "examined_existing", "support", now_us - day, None),
        ("Claim:E", "old", "p1", "examined_existing", "unsure", now_us - day, None),
    ]
    connection.executemany("INSERT INTO attestations VALUES (?,?,?,?,?,?,?)", rows)

    @contextmanager
    def bind(_coordinate: Any):
        yield SimpleNamespace(typed=SimpleNamespace(connection=connection))

    fake = SimpleNamespace(bind_accepted_projection=bind)
    claims = [
        LiveValue(
            identity=f"Claim:{name}",
            subject_path="s",
            predicate="p",
            value=None,
            artifact_digest=f"d-{name.lower()}",
        )
        for name in "ABCDE"
    ]
    held = unsure_holds(
        fake,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        claims=claims,
        evaluation_time=now,
        hold_for={},
        default_hold=timedelta(days=30),
    )

    assert held == {"Claim:A"}


def test_a_contested_slot_shows_every_live_value(tmp_path: Path) -> None:
    from cruxible_client.contracts.captures import DirectForeignSourceSelectionV1
    from cruxible_client.contracts.semantic import ContentSpan
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._knowledge_loop_support import activate, authoring

    seeded, owner = seed_claims(tmp_path)
    body = seeded.body_store().store(b"status: done")
    proposed = service_propose_playbill_claim(
        seeded,
        authoring=authoring("wi-42", "done", with_claim_type=False).model_copy(
            update={
                "source_selection": DirectForeignSourceSelectionV1(
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


def test_evidence_names_resolve_digests_and_read_identity_rules() -> None:
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
    from cruxible_client.contracts.claim_types import ClaimType
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicyV3,
        ClaimEvidenceAdmissionRuleV3,
    )
    from cruxible_core.service.discovery.query_vocabulary import CaptureContractNames
    from tests.test_claims.test_claims import _claim_type

    legacy = _claim_type()
    digest = legacy.evidence_admission_policy.rules[0].capture_contract_digests[0]
    names = CaptureContractNames.__new__(CaptureContractNames)
    names._by_digest = {}
    names._at = None  # type: ignore[assignment]
    names._instance = SimpleNamespace(  # type: ignore[assignment]
        accepted_capture_contract_version=lambda _at, found: (
            SimpleNamespace(contract=SimpleNamespace(identity=SimpleNamespace(name="direct")))
            if found == digest
            else None
        )
    )
    assert names.of(legacy) == ("direct",)
    assert names.by_digest("sha256:" + "ab" * 32) == "unresolved:" + "ab" * 6

    identity_rule = ClaimEvidenceAdmissionRuleV3(
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
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV3(
                rules=(identity_rule,)
            ).model_dump(mode="json"),
        }
    )
    assert names.of(current) == ("fixture.reports",)
    assert names.names_by_digest(legacy) and not names.names_by_digest(current)


def test_a_filter_naming_a_missing_subject_refuses(instance: Any) -> None:
    with pytest.raises(PlaybillQueryRefused) as refused:
        _query(instance, kind=SUBJECT_KIND, where=[{"field": "subject_id", "eq": "wi-44"}])

    assert refused.value.error_code == "playbill.query.unknown_ref"
    assert "wi-42" in refused.value.nearest or "wi-43" in refused.value.nearest
    assert _ids(
        _query(instance, kind=SUBJECT_KIND, where=[{"field": "subject_id", "in": ["wi-43"]}])
    ) == ["wi-43"]


def test_a_definition_filter_naming_an_unknown_namespace_refuses(instance: Any) -> None:
    with pytest.raises(PlaybillQueryRefused) as refused:
        _query(instance, kind="ClaimType", where=[{"field": "namespace", "eq": "project.work_itm"}])

    assert refused.value.error_code == "playbill.query.unknown_ref"
    assert SUBJECT_KIND in refused.value.nearest

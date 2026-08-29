"""PC-G-S1a accepted-state query execution, receipts, and receipt journalling."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_client.contracts.query.definitions import query_definition_path
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_core.errors import DataValidationError
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust.backends import LocalJournalBackend
from cruxible_core.playbill.exhaust.records import (
    QUERY_RECEIPT_EVENT_KIND,
    QUERY_RECEIPT_JOURNAL_FAMILY,
    journal_payload_bytes,
)
from cruxible_core.playbill.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.playbill.query.engine import claim_query_result_digest
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import (
    service_propose_playbill_query_definition,
)
from cruxible_core.service.playbill_query import (
    DEFAULT_RECEIPT_PARTITION_ID,
    DEFAULT_RECEIPT_STREAM_ID,
    PlaybillQueryReceiptJournal,
    build_accepted_query_facts,
    service_run_playbill_query,
)
from tests.test_playbill._knowledge_loop_support import (
    PREDICATE,
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)

READ_TIME = datetime(2026, 8, 16, 21, 0, tzinfo=UTC)


def _instance_with_query(tmp_path: Path):
    instance, owner = seed_claims(tmp_path)
    inspection = service_propose_playbill_query_definition(
        instance,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    return instance, owner


def _journal(tmp_path: Path, instance_id: str) -> PlaybillQueryReceiptJournal:
    journal_root = tmp_path / "journal"
    cas_root = tmp_path / "journal-cas"
    journal_root.mkdir(mode=0o700)
    cas_root.mkdir(mode=0o700)
    backend = LocalJournalBackend(journal_root)
    receipt_journal = PlaybillQueryReceiptJournal(
        writer=ProcedureExhaustWriter(
            journal=backend,
            bodies=ContentAddressedBodyStore(cas_root),
            fencing_token="writer-a",
        ),
        instance_id=instance_id,
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="owner",
            org_id="org-a",
            operation_id="query-run",
            timestamp=READ_TIME,
        ),
    )
    backend.activate_writer(
        receipt_journal.stream,
        DEFAULT_RECEIPT_PARTITION_ID,
        fencing_token="writer-a",
        expected_head=backend.read_head(receipt_journal.stream, DEFAULT_RECEIPT_PARTITION_ID),
    )
    return receipt_journal


# -- accepted-state facts -------------------------------------------------


def test_accepted_facts_carry_every_live_claim_with_its_adjudication_rule(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)

    facts = build_accepted_query_facts(instance, coordinate=instance.accepted_coordinate())

    assert facts.coordinate == instance.accepted_coordinate()
    assert len(facts.subjects) == 2
    assert len(facts.claims) == 2
    assert {row.accepted.claim.statement.predicate for row in facts.claims} == {PREDICATE}
    assert all(row.rule.claim_type_digest for row in facts.claims)
    assert all(row.referent_current for row in facts.claims)


# -- execution ------------------------------------------------------------


def test_query_run_projects_every_accepted_work_item_with_its_status(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    run = service_run_playbill_query(instance, name=QUERY_NAME, evaluation_time=READ_TIME)

    assert run.result.verdict == "completed"
    assert run.definition_path == query_definition_path(QUERY_NAME)
    projected = tuple(
        (
            next(field.value for field in row.fields if field.name == "item_id"),
            next(field.value for field in row.fields if field.name == "status"),
        )
        for row in run.result.rows
    )
    assert projected == (("wi-42", "ready"), ("wi-43", "blocked"))


def test_query_run_is_byte_stable_for_one_coordinate_and_read_time(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    first = service_run_playbill_query(instance, name=QUERY_NAME, evaluation_time=READ_TIME)
    second = service_run_playbill_query(instance, name=QUERY_NAME, evaluation_time=READ_TIME)

    assert first.result == second.result
    assert first.receipt == second.receipt


# -- receipts -------------------------------------------------------------


def test_receipt_names_the_exact_definition_coordinate_and_result_digest(
    tmp_path: Path,
) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    run = service_run_playbill_query(instance, name=QUERY_NAME, evaluation_time=READ_TIME)

    receipt = run.receipt
    assert receipt.definition_path == run.definition_path
    assert receipt.definition_digest == run.definition_digest
    assert receipt.coordinate == instance.accepted_coordinate()
    assert receipt.evaluation_time == READ_TIME
    assert receipt.verdict == "completed"
    assert receipt.refusal_code is None
    assert receipt.result_digest == claim_query_result_digest(run.result)
    assert run.coordinate == accepted
    assert run.journal_record_digest is None


def test_budget_refusal_is_surfaced_in_the_result_and_its_receipt(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    run = service_run_playbill_query(
        instance,
        name=QUERY_NAME,
        evaluation_time=READ_TIME,
        budgets=QueryBudgetsV1(max_results=500, max_traversal_depth=0),
    )

    assert run.result.verdict == "refused"
    assert run.result.refusal is not None
    assert run.receipt.verdict == "refused"
    assert run.receipt.refusal_code == run.result.refusal.code
    assert run.result.rows == ()


# -- receipt journalling --------------------------------------------------


def test_receipt_is_recorded_in_the_registered_query_receipt_family(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    receipt_journal = _journal(tmp_path, instance.descriptor.instance_id)

    run = service_run_playbill_query(
        instance,
        name=QUERY_NAME,
        evaluation_time=READ_TIME,
        receipt_journal=receipt_journal,
    )

    assert receipt_journal.stream.journal_family == QUERY_RECEIPT_JOURNAL_FAMILY
    assert receipt_journal.stream.stream_id == DEFAULT_RECEIPT_STREAM_ID
    assert run.journal_record_digest is not None
    head = receipt_journal.writer.journal.read_head(
        receipt_journal.stream,
        DEFAULT_RECEIPT_PARTITION_ID,
    )
    assert head.record_digest == run.journal_record_digest
    payload = journal_payload_bytes(run.receipt.model_dump(mode="json"))
    assert receipt_journal.writer.bodies.verify("sha256:" + hashlib.sha256(payload).hexdigest())


def test_journalled_receipt_carries_no_procedure_run_coordinates(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    receipt_journal = _journal(tmp_path, instance.descriptor.instance_id)

    service_run_playbill_query(
        instance,
        name=QUERY_NAME,
        evaluation_time=READ_TIME,
        receipt_journal=receipt_journal,
    )

    backend = receipt_journal.writer.journal
    records = backend.read_exact_range(
        backend.range_from_sequences(
            receipt_journal.stream,
            DEFAULT_RECEIPT_PARTITION_ID,
            first_sequence=1,
            last_sequence=1,
        )
    )
    record = records[0].record
    assert record.event_kind == QUERY_RECEIPT_EVENT_KIND
    assert record.run_id is None
    assert record.procedure_artifact_digest is None


# -- refusals -------------------------------------------------------------


def test_absent_query_definition_is_refused_before_any_evaluation(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    with pytest.raises(ClaimNotFoundError):
        service_run_playbill_query(
            instance,
            name="project.absent_query",
            evaluation_time=READ_TIME,
        )


def test_naive_evaluation_time_is_refused_rather_than_localized(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    with pytest.raises(DataValidationError, match="timezone-aware"):
        service_run_playbill_query(
            instance,
            name=QUERY_NAME,
            evaluation_time=datetime(2026, 8, 16, 21, 0),
        )


def test_query_fact_projection_indexes_claim_law_history_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    original = instance.accepted_history
    calls = 0

    def counted():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(instance, "accepted_history", counted)
    facts = build_accepted_query_facts(
        instance,
        coordinate=instance.accepted_coordinate(),
    )

    assert len(facts.claims) == 2
    assert calls == 1

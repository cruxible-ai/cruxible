"""A Runbook's preview (D-R36): a static composed graph, from accepted definitions only.

Per step: reads (Queries and the ClaimTypes, kinds and keys behind them,
including late reads), writes, provider calls with slots and bindings, Mandate
scope (what settles and what goes to a proposal and would stop the run), and
the capture_landing / generation_accepted Triggers its writes can set off,
inside the Runbook or outside it. Across steps: order problems, reported and
never fixed. It never executes, never reads state beyond definitions, and its
work is linear in the Runbook's steps and constant in unrelated definitions.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput, TriggerInput
from cruxible_client.contracts.query.authored import QuerySpecInput
from cruxible_client.contracts.runbooks import RunbookPreview, runbook_path
from cruxible_core.service.runbook_preview import service_preview_runbook
from tests.core_support._identity_support import accept_tree
from tests.support.runbooks import (
    FAIL,
    PROPOSE,
    SETTLE,
    author,
    author_runbook,
    author_runbook_trigger,
    line_steps,
    propose_line,
    runbook_world,
)

WATCHER = "test.watcher"
WATCHER_LINE = "test.watcher-line"
NOTE = "security.component.note_text"


def _queries(instance: Any) -> None:
    from tests.support.procedures import boundary_contract, contract_files
    from tests.test_procedures.test_claims_value_upsert import AFFECTED, COMPONENT, KIND, _key_type

    note = _key_type(NOTE, COMPONENT)
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type

    cve_row = boundary_contract("test.cve-row", {"cve_id": "string"})
    purl_row = boundary_contract("test.purl-row", {"purl": "string"})
    id_row = boundary_contract("test.id-row", {"subject_id": "string"})
    accept_tree(
        instance,
        {
            **contract_files(cve_row),
            **contract_files(purl_row),
            **contract_files(id_row),
            claim_type_path(NOTE): render_claim_type(note),
        },
        name="preview-vocabulary",
    )
    for spec in (
        {
            "query": "test.watched",
            "params": {"cve": "string"},
            "from": {"v": KIND},
            "where": "v.cve_id == $cve",
            "row": cve_row.ref.spelling,
            "fields": ["v.cve_id"],
        },
        {
            "query": "test.affected",
            "from": {"c": COMPONENT},
            "where": f"exists c.{AFFECTED}",
            "row": purl_row.ref.spelling,
            "fields": ["c.purl"],
        },
        {
            # Reads no predicate at all: only its entry kind's Subjects.
            "query": "test.cve-subjects",
            "from": {"v": KIND},
            "row": id_row.ref.spelling,
            "fields": ["v.subject_id"],
        },
        {
            "query": "test.noted",
            "from": {"c": COMPONENT},
            "where": f"exists c.{NOTE}",
            "row": purl_row.ref.spelling,
            "fields": ["c.purl"],
        },
    ):
        author(
            instance,
            QueryDefinitionInput(
                kind="query_definition", query_definition=QuerySpecInput.model_validate(spec)
            ),
            name="query-" + str(spec["query"]),
        )


def _watcher(instance: Any) -> None:
    """The override Procedure plus three reads: two at admission, one late (D-R38)."""

    from tests.support.operation_world import author_line, author_procedure
    from tests.support.procedures import procedure_spec
    from tests.test_procedures.test_line_run_parameter_override import override_spec

    raw = override_spec().model_dump(mode="python", by_alias=True)
    raw["procedure"] = WATCHER
    raw["steps"][1]["header"] = {"first": {"at": "/entries/0/cve", "type": "string{max_len: 256}"}}
    raw["steps"].insert(0, {"read": "affected", "query": "test.affected"})
    raw["steps"].insert(1, {"read": "noted", "query": "test.noted"})
    raw["steps"].insert(2, {"read": "subjects", "query": "test.cve-subjects"})
    raw["steps"].insert(
        5,
        {
            "read": "late",
            "query": "test.watched",
            "params": {"cve": "$steps.entries.header.first.value"},
        },
    )
    author_procedure(instance, procedure_spec(base=raw, reference_text=True), name="watcher")
    # Observe only: it reads, and nothing it would write ever leaves the run.
    author_line(
        instance,
        name=WATCHER_LINE,
        procedure=WATCHER,
        parameters={"path": "data/doc.json"},
        max_authority="observe",
        label="watcher-line",
    )


def preview_world(tmp_path: Path) -> tuple[Any, Path]:
    instance, root = runbook_world(tmp_path)
    _queries(instance)
    _watcher(instance)
    # The fetch lands test.orders-v1 Captures: a Trigger on that landing runs a
    # Line outside the Runbook; any accepted write sets off generation_accepted.
    author(
        instance,
        TriggerInput.model_validate(
            {
                "kind": "trigger",
                "name": "test.on-orders",
                "schedule": {"kind": "capture_landing", "capture_contract": "test.orders-v1"},
                "line_name": FAIL,
            }
        ),
        name="trigger-landing",
    )
    return instance, root


def _step(preview: RunbookPreview, index: int) -> Any:
    return preview.steps[index - 1]


def test_the_preview_composes_what_each_step_can_do(tmp_path: Path) -> None:
    from tests.test_procedures.test_claims_value_upsert import AFFECTED, COMPONENT, KIND

    instance, _root = preview_world(tmp_path)
    author_runbook(
        instance,
        "test.rb",
        [{"line": WATCHER_LINE}, {"line": SETTLE}, {"action": "evidence.sweep"}, {"line": PROPOSE}],
    )
    author_runbook_trigger(
        instance,
        "test.rb-nightly",
        runbook="test.rb",
        schedule={"kind": "cron", "expression": "0 3 * * *"},
    )
    preview = service_preview_runbook(instance, name="test.rb")
    assert preview.runbook == "test.rb"
    assert preview.generation_root == instance.accepted_coordinate().generation_root

    watcher = _step(preview, 1)
    assert watcher.procedure == f"Procedure:{WATCHER}"
    reads = {read.node_id: read for read in watcher.reads}
    assert set(reads) == {"affected", "noted", "subjects", "late"}
    # A Query that reads no predicate still reads its entry kind and its key (F5).
    assert reads["subjects"].claim_types == ()
    assert reads["subjects"].subject_kinds == (KIND,)
    assert reads["subjects"].key_claim_types == (f"{KIND}.cve_id",)
    assert reads["affected"].query == "test.affected"
    assert set(reads["affected"].claim_types) >= {AFFECTED}
    assert COMPONENT in reads["affected"].subject_kinds
    assert f"{COMPONENT}.purl" in reads["affected"].key_claim_types
    # The late read is the one fed by the extract's header (D-R38).
    assert [read.node_id for read in watcher.reads if read.late] == ["late"]
    assert reads["late"].claim_types == (f"{KIND}.cve_id",)
    assert watcher.authority is not None and watcher.authority.max_authority == "observe"
    assert watcher.authority.may_stop_run is False
    # Observe only: no Claim leaves the run, so no generation cascade; its fetch
    # still lands Captures, so the landing Trigger fires.
    assert [(item.trigger, item.schedule) for item in watcher.cascades] == [
        ("Trigger:test.on-orders", "capture_landing")
    ]

    settle = _step(preview, 2)
    (call,) = settle.calls
    assert (call.slot, call.signature_in, call.effect) == (
        "fetch",
        "workspace.file.request@1",
        "pure",
    )
    assert (call.build, call.operation) == ("Provider:cruxible-builtin", "workspace.file")
    assert call.captures == ("test.orders-v1",)
    (write,) = settle.writes
    assert (write.subject_kind, write.claim_types) == (COMPONENT, (AFFECTED,))
    assert write.creates_object_kinds == (KIND,)
    authority = settle.authority
    assert authority is not None and authority.grants == "settle"
    assert [(item.claim_type, item.changes) for item in authority.settles] == [
        (AFFECTED, ("create",))
    ]
    assert authority.settles_creation == (KIND,)
    # The grant settles creates only: a revise of a held unit would go to review.
    assert authority.proposes == (AFFECTED,)
    assert authority.may_stop_run is True
    cascades = {item.trigger: item for item in settle.cascades}
    landing = cascades["Trigger:test.on-orders"]
    assert (landing.schedule, landing.target, landing.capture_contract, landing.in_runbook) == (
        "capture_landing",
        f"Line:{FAIL}",
        "test.orders-v1",
        False,
    )
    assert cascades["Trigger:floor-refresh"].schedule == "generation_accepted"
    # The Runbook's own cron Trigger is not a cascade: it fires on time, not on writes.
    assert "Trigger:test.rb-nightly" not in cascades

    action = _step(preview, 3)
    assert (action.action_effect, action.reads, action.writes) == ("findings", (), ())

    propose = _step(preview, 4)
    assert propose.authority is not None
    assert (propose.authority.grants, propose.authority.settles) == ("propose", ())
    assert propose.authority.proposes == (AFFECTED,)
    assert propose.authority.proposes_creation == (KIND,)

    problems = {(item.kind, item.step, item.claim_type): item for item in preview.order_problems}
    later = problems[("reads_later_write", 1, AFFECTED)]
    assert later.written_by_steps == (2, 4)
    assert later.outside_writers == (f"Line:{FAIL}",)
    # Creating vulnerabilities writes their key: the late read reads a later write.
    assert problems[("reads_later_write", 1, f"{KIND}.cve_id")].written_by_steps == (2, 4)
    # Nothing writes the note ClaimType: direct entry only.
    assert ("reads_unwritten", 1, NOTE) in problems
    # A key ClaimType nothing in the Runbook creates is never reported as unwritten.
    assert not any(item.claim_type == f"{COMPONENT}.purl" for item in preview.order_problems)


def test_order_problems_are_reported_never_fixed(tmp_path: Path) -> None:
    from tests.test_procedures.test_claims_value_upsert import AFFECTED

    instance, _root = preview_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(WATCHER_LINE, SETTLE))
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    before = tree[runbook_path("test.rb")]
    preview = service_preview_runbook(instance, name="test.rb")
    assert any(item.claim_type == AFFECTED for item in preview.order_problems)
    assert [step.target for step in preview.steps][0].line.target.name == WATCHER_LINE
    assert (
        instance.tree_at(instance.accepted_coordinate().git_oid)[runbook_path("test.rb")] == before
    )
    # In the served order, nothing is read before it is written.
    author_runbook(instance, "test.rb-ordered", line_steps(SETTLE, WATCHER_LINE))
    ordered = service_preview_runbook(instance, name="test.rb-ordered")
    assert not any(item.kind == "reads_later_write" for item in ordered.order_problems)


def test_the_preview_never_executes_or_reads_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.procedures import procedure_runs
    from cruxible_core.triggers import journal

    instance, _root = preview_world(tmp_path)
    author_runbook(
        instance,
        "test.rb",
        [{"line": WATCHER_LINE}, {"line": SETTLE}, {"action": "evidence.sweep"}],
    )
    base = instance.accepted_coordinate()

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the preview executed or read state")

    monkeypatch.setattr(procedure_runs, "service_run_playbill_line", refuse)
    monkeypatch.setattr(journal, "_open", refuse)
    monkeypatch.setattr(journal, "fire_manual", refuse)
    work: Counter[str] = Counter()
    preview = service_preview_runbook(instance, name="test.rb", work=work)
    assert instance.accepted_coordinate() == base
    read_kinds = {key.split(":", 2)[1] for key in work if key.startswith("source:")}
    assert read_kinds <= {
        "Runbook",
        "Line",
        "Procedure",
        "ProcedureMandate",
        "QueryDefinition",
        "ClaimType",
        "SubjectKind",
        "Trigger",
    }, read_kinds
    assert {key for key in work if key.startswith("seek:")} <= {
        "seek:mandates_by_line",
        "seek:pins_by_target",
        "seek:lines_by_procedure",
        "seek:triggers_by_schedule",
    }
    assert len(preview.steps) == 3


# -- work counts ----------------------------------------------------------------------


def _lines(instance: Any, prefix: str, count: int) -> list[str]:
    names = [f"{prefix}-{index:02d}" for index in range(count)]
    for name in names:
        propose_line(instance, name)
    return names


def _unrelated(instance: Any, count: int, offset: int) -> None:
    """Definitions no previewed step reaches: their own Lines, Triggers and ClaimTypes."""

    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from tests.test_procedures.test_claims_value_upsert import COMPONENT, _key_type

    accept_tree(
        instance,
        {
            claim_type_path(f"{COMPONENT}.extra_{offset + index}"): render_claim_type(
                _key_type(f"{COMPONENT}.extra_{offset + index}", COMPONENT)
            )
            for index in range(count)
        },
        name=f"unrelated-types-{offset}",
    )
    for name in _lines(instance, f"test.unrelated-{offset}", count):
        author(
            instance,
            TriggerInput.model_validate(
                {
                    "kind": "trigger",
                    "name": name + "-hourly",
                    "schedule": {"kind": "cadence", "interval_seconds": 3600},
                    "line_name": name,
                }
            ),
            name=name + "-trigger",
        )


def _work(instance: Any, name: str) -> Counter[str]:
    work: Counter[str] = Counter()
    service_preview_runbook(instance, name=name, work=work)
    return work


def test_preview_work_is_linear_in_steps_and_constant_in_unrelated_definitions(
    tmp_path: Path,
) -> None:
    instance, _root = runbook_world(tmp_path)
    names = _lines(instance, "test.step", 20)
    for size in (5, 10, 20):
        author_runbook(instance, f"test.rb-{size}", line_steps(*names[:size]))
    five, ten, twenty = (_work(instance, f"test.rb-{size}") for size in (5, 10, 20))
    keys = set(five) | set(ten) | set(twenty)
    for key in keys:
        # Linear: every counter grows by the same amount per added step.
        assert twenty[key] - ten[key] == 2 * (ten[key] - five[key]), key
        # And by at most one read or seek per step for each kind of definition.
        assert ten[key] - five[key] <= 5, key
    # Per step: the step's Line, its Mandate seek and Mandate, its two cascade seeks.
    assert ten["source:Line"] - five["source:Line"] == 5
    assert ten["seek:mandates_by_line"] - five["seek:mandates_by_line"] == 5
    # What the steps share (one Procedure, its vocabulary) is read once.
    assert five["source:Procedure"] == twenty["source:Procedure"] == 1
    # Grow everything the Runbooks do not reach: the same Runbook costs the same.
    _unrelated(instance, 8, 0)
    assert _work(instance, "test.rb-5") == five
    _unrelated(instance, 16, 100)
    assert _work(instance, "test.rb-5") == five
    assert _work(instance, "test.rb-20") == twenty

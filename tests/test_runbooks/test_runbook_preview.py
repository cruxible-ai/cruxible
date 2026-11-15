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
    raw["steps"][1]["header"] = ["first"]
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
    assert (call.slot, call.capability, call.signature_in, call.effect) == (
        "fetch",
        "workspace.file@1",
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
    assert [
        (item.claim_type, [(change.change, change.basis) for change in item.changes])
        for item in authority.settles
    ] == [(AFFECTED, [("create", ("extracted",))])]
    assert authority.settles_creation == (KIND,)
    # The grant settles creates only: a revise of a held unit would go to review.
    assert authority.proposes == (AFFECTED,)
    assert [(gap.change, gap.basis, gap.term) for gap in authority.review_gaps] == [
        ("revise", "extracted", "change_kind")
    ]
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
        "Capability",
        "Runbook",
        "Line",
        "Procedure",
        "ProcedureMandate",
        "QueryDefinition",
        "ClaimType",
        "SubjectKind",
        "Trigger",
        # What a step's external inputs declare: the params Contract and the
        # CaptureContract each input lands under.
        "Contract",
        "CaptureContract",
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


# -- Mandate scope: every restriction, and review whenever settling is unproven (F4) --


def _scoped_grant(line: Any, claim_type: Any, **overrides: Any) -> Any:
    from datetime import UTC, datetime

    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.claim_types import claim_type_digest
    from cruxible_client.contracts.procedure_mandates import MandateClaimScope
    from cruxible_client.contracts.procedures.line_specs import line_spec_digest
    from tests.support.mandates import procedure_mandate, scope_changes

    scope_fields = {
        "changes": scope_changes("create", "revise", basis=("extracted",)),
        **overrides.pop("scope", {}),
    }
    return procedure_mandate(
        "scoped",
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        grants="settle",
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=claim_type.identity,
                    artifact_digest=claim_type_digest(claim_type).tagged,
                ),
                **scope_fields,
            ),
        ),
        subject_creation=("security.vulnerability",),
        **overrides,
    )


def test_a_scope_restriction_the_preview_cannot_prove_shows_possible_review(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.mandate_scope import MandateValueCondition
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_core.service.runbook_preview import _authority
    from tests.support.mandates import scope_changes
    from tests.test_procedures.test_claims_value_upsert import AFFECTED

    instance, _root = runbook_world(tmp_path)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        line = projection.typed.source(f"Line:{SETTLE}")
        definition = projection.typed.source(f"Procedure:{line.procedure.target.name}").definition
        claim_type = projection.typed.source(f"ClaimType:{AFFECTED}")

    def gaps(grant: Any) -> list[tuple[str, str | None, str]]:
        authority = _authority(line, (grant,), definition)
        assert authority.may_stop_run == bool(authority.review_gaps)
        assert authority.proposes == ((AFFECTED,) if authority.review_gaps else ())
        return [(gap.change, gap.basis, gap.term) for gap in authority.review_gaps]

    # Create and revise on extracted evidence: the extract-fed writer is proven to settle.
    assert gaps(_scoped_grant(line, claim_type)) == []
    # A grant on derived evidence only does not cover a direct extraction.
    derived = _scoped_grant(
        line, claim_type, scope={"changes": scope_changes("create", "revise", basis=("derived",))}
    )
    assert gaps(derived) == [("create", "extracted", "basis"), ("revise", "extracted", "basis")]
    # A value restriction cannot be proven from definitions.
    valued = _scoped_grant(
        line, claim_type, scope={"value": MandateValueCondition.model_validate({"to": ["x"]})}
    )
    assert {term for _change, _basis, term in gaps(valued)} == {"value"}
    authority = _authority(line, (valued,), definition)
    assert authority.settles[0].value_to == ("x",)
    # Nor can an explicit Subject scope.
    fenced = _scoped_grant(
        line,
        claim_type,
        subject_scope=(SemanticAddress.whole_artifact("subjects/security.component/a.json"),),
    )
    assert {term for _change, _basis, term in gaps(fenced)} == {"subject"}
    assert _authority(line, (fenced,), definition).subject_scope == (
        "subjects/security.component/a.json",
    )


# -- inputs: what each step's run takes from outside accepted state ---------------------

DESCRIBED = "test.described"
DESCRIBED_LINE = "test.described-line"
DOC_HELP = "The advisory document: export it from the scanner and save it under data/."


def _reader(instance: Any, root: Path) -> Any:
    from tests.support.runbooks import readers

    return readers(instance, root)["workspace_file_reader"]


def _described(instance: Any) -> None:
    """The override Procedure again, its path parameter now carrying a description."""

    from tests.support.operation_world import author_line, author_procedure
    from tests.support.procedures import procedure_spec
    from tests.test_procedures.test_line_run_parameter_override import override_spec

    raw = override_spec().model_dump(mode="python", by_alias=True)
    raw["procedure"] = DESCRIBED
    raw["params"] = {
        "path": {
            "type": 'string{min_len: 1, max_len: 64, pattern: "^data/[a-z]+[.]json$"}',
            "description": DOC_HELP,
        }
    }
    author_procedure(instance, procedure_spec(base=raw, reference_text=True), name="described")
    author_line(
        instance,
        name=DESCRIBED_LINE,
        procedure=DESCRIBED,
        parameters={"path": "data/doc.json"},
        max_authority="observe",
        label="described-line",
    )


def _only_input(preview: RunbookPreview, index: int) -> Any:
    (item,) = _step(preview, index).inputs
    return item


def test_each_workspace_file_input_says_where_it_reads_and_whether_it_is_there(
    tmp_path: Path,
) -> None:
    from tests.support.runbooks import propose_line
    from tests.test_procedures.test_claims_value_upsert import CAPTURE

    instance, root = runbook_world(tmp_path)
    propose_line(instance, "test.linked-line", path="data/link.json")
    (root / "data" / "link.json").symlink_to(root / "data" / "doc.json")
    author_runbook(instance, "test.rb", line_steps(SETTLE, FAIL, "test.linked-line", PROPOSE))
    preview = service_preview_runbook(
        instance, name="test.rb", workspace_file_reader=_reader(instance, root)
    )
    present = _only_input(preview, 1)
    assert (present.kind, present.location, present.parameter) == (
        "workspace_file",
        "data/doc.json",
        "path",
    )
    assert (present.state, present.refused) == ("present", None)
    assert present.capture_contract == CAPTURE
    assert present.format == "json"
    assert present.limits.max_bytes == 262144
    # The author wrote no description for this parameter.
    assert present.description is None
    assert present.model_dump(mode="json")["description"] is None
    absent = _only_input(preview, 2)
    assert (absent.location, absent.state, absent.refused) == ("data/missing.json", "absent", None)
    linked = _only_input(preview, 3)
    # The reader's own rule: a symbolic link is never read.
    assert (linked.state, linked.refused) == ("refused", "symlink")
    assert _only_input(preview, 4).state == "present"

    # No daemon reader (none attached, or construction refused): no_workspace.
    bare = service_preview_runbook(instance, name="test.rb")
    assert {item.state for step in bare.steps for item in step.inputs} == {"no_workspace"}


def test_presence_refuses_what_the_reader_would_refuse(tmp_path: Path) -> None:
    """The path classes come from the reader's own rules, before any stat of the file."""

    instance, root = runbook_world(tmp_path)
    reader = _reader(instance, root)
    assert reader.presence("data/doc.json") == ("present", None)
    assert reader.presence("data/missing.json") == ("absent", None)
    # A file where a directory should be: the reader refuses it as non_regular.
    assert reader.presence("data/doc.json/inner.json") == ("refused", "non_regular")
    assert reader.presence(".git/config") == ("refused", "git_metadata")
    assert reader.presence("../outside.json") == ("refused", "path_grammar")
    assert reader.presence("data/doc.json", max_bytes=1) == ("refused", "size_budget")
    (root / "data" / "pair.json").write_text("{}")
    (root / "data" / "pair-twin.json").hardlink_to(root / "data" / "pair.json")
    assert reader.presence("data/pair.json") == ("refused", "hardlink")
    (root / "data" / "sub").mkdir()
    assert reader.presence("data/sub") == ("refused", "non_regular")


def test_presence_stats_each_path_component_and_lists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    instance, root = runbook_world(tmp_path)
    (root / "data" / "a" / "b").mkdir(parents=True)
    (root / "data" / "a" / "b" / "deep.json").write_text("{}")
    reader = _reader(instance, root)
    stats: list[str] = []
    real_stat = os.stat

    def counted(path: Any, *args: Any, **kwargs: Any) -> Any:
        # Only the reader's own walk stats through a directory descriptor.
        if "dir_fd" in kwargs:
            assert kwargs.get("follow_symlinks") is False
            stats.append(str(path))
        return real_stat(path, *args, **kwargs)

    def no_listing(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("presence listed a directory")

    monkeypatch.setattr(os, "stat", counted)
    monkeypatch.setattr(os, "listdir", no_listing)
    monkeypatch.setattr(os, "scandir", no_listing)

    def no_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("presence read a byte of the file")

    monkeypatch.setattr(os, "read", no_read)
    assert reader.presence("data/a/b/deep.json") == ("present", None)
    # One no-follow stat per component, nothing else.
    assert stats == ["data", "a", "b", "deep.json"]
    stats.clear()
    assert reader.presence("data/missing.json") == ("absent", None)
    assert stats == ["data", "missing.json"]


def test_an_authored_parameter_description_is_shown_as_written(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from cruxible_core.cli.commands.playbill import _echo_runbook_preview

    instance, root = runbook_world(tmp_path)
    _described(instance)
    author_runbook(instance, "test.rb", line_steps(DESCRIBED_LINE, FAIL))
    preview = service_preview_runbook(
        instance, name="test.rb", workspace_file_reader=_reader(instance, root)
    )
    described = _only_input(preview, 1)
    assert (described.parameter, described.description) == ("path", DOC_HELP)
    assert _only_input(preview, 2).description is None

    import click

    @click.command()
    def show() -> None:
        _echo_runbook_preview(preview)

    output = CliRunner().invoke(show).output
    assert (
        "      needs  file data/doc.json (json, at most 256 KiB, parameter path): present\n"
        in output
    )
    assert f"        {DOC_HELP}\n" in output
    assert (
        "      needs  file data/missing.json (json, at most 256 KiB, parameter path): absent\n"
        in output
    )


def test_a_step_with_no_external_input_lists_none(tmp_path: Path) -> None:
    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"line": SETTLE}, {"action": "evidence.sweep"}])
    preview = service_preview_runbook(
        instance, name="test.rb", workspace_file_reader=_reader(instance, root)
    )
    assert _step(preview, 2).inputs == ()
    assert preview.model_dump(mode="json")["steps"][1]["inputs"] == []


class _Typed:
    """A projection's typed source over a fixed set of definitions, with no index rows."""

    def __init__(self, sources: dict[str, Any]) -> None:
        self.sources = sources
        self.connection = self

    def source(self, identity: str) -> Any:
        return self.sources.get(identity)

    def execute(self, sql: str, parameters: Any) -> Any:
        return self

    def fetchall(self) -> list[Any]:
        return []


def _fetch_definition(name: str) -> Any:
    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
    from cruxible_client.contracts.procedures.models import CallNode, ProjectNode
    from tests.support.procedures import procedure_definition

    capture = ArtifactNameRef(target=ArtifactIdentity(kind="CaptureContract", name="test.feed"))
    return procedure_definition(
        name,
        slots={
            "fetch": {
                "capability": {"name": "web.fetch", "version": 1, "digest": "sha256:" + "1" * 64}
            }
        },
        nodes=(
            CallNode(
                node_id="catalog",
                slot="fetch",
                input={"url": "$params.url", "logical_source": "test.feed"},
                captures={"capture": capture},
                as_="catalog",
                next="result",
            ),
            ProjectNode(node_id="result", fields={}, as_="result"),
        ),
    )


def test_a_fetch_input_names_its_url_and_origins_and_no_presence() -> None:
    from cruxible_client.contracts.procedures.acquisition_rules import LineAcquisitionRule
    from cruxible_core.service.runbook_preview import DefinitionReader, _inputs
    from tests.core_support._light_capture_support import JSON_DECODING, http_contract
    from tests.support.lines import line_spec

    contract = http_contract(name="test.feed", max_bytes=4 << 20, decoding=JSON_DECODING)
    reader = DefinitionReader(_Typed({"CaptureContract:test.feed": contract}))
    line = line_spec(
        "test.feed-line",
        parameters={"url": "https://feeds.example.test/catalog.json"},
        acquisition={
            "catalog.capture": LineAcquisitionRule(origins=("https://feeds.example.test",))
        },
    )
    (item,) = _inputs(reader, _fetch_definition("test.feed-procedure"), line, None)
    assert (item.kind, item.location, item.parameter) == (
        "fetch",
        "https://feeds.example.test/catalog.json",
        "url",
    )
    assert item.origins == ("https://feeds.example.test",)
    assert (item.capture_contract, item.format, item.limits.max_bytes) == (
        "test.feed",
        "json",
        4 << 20,
    )
    # A fetch is the run's to make: nothing to stat, so no presence.
    assert item.state is None
    assert "state" not in item.model_dump(mode="json")
    # A Line with no acquisition rule permits any origin.
    (open_item,) = _inputs(
        reader,
        _fetch_definition("test.feed-procedure"),
        line_spec("test.open", parameters={"url": "https://x.example.test/a.json"}),
        None,
    )
    assert open_item.origins is None

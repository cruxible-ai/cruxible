"""An inbox-only Procedure needs no declared output or return (coldkit-11)."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from tests.test_procedures.test_line_inbox_delivery import (
    ROWS,
    ROWS_ALIAS,
    _accept_sdk_input,
    _entries,
    _feed,
    _row_contract,
    _rows_layout,
)
from tests.test_procedures.test_manual_line_runs import manual_run
from tests.test_procedures.test_procedure_source_runs import (
    NOW,
    SOURCE_ALIAS,
    _accept_more,
    _served_line,
    _world,
)


def test_a_procedure_that_only_posts_to_the_inbox_returns_nothing_and_runs(
    tmp_path: Path,
) -> None:
    from cruxible_client.authoring.procedures import ProcedureLimits, Sequence, field
    from cruxible_client.authoring.sdk import Cruxible
    from cruxible_client.contracts.shapes import contract_path, render_contract
    from tests.core_support._light_capture_support import file_contract as capture_contract
    from tests.support.procedures import MAX_WALL_CLOCK
    from tests.test_procedures.test_procedure_source_runs import _source_request

    instance, owner, procedure, root, policy = _world(
        tmp_path, accept_procedure=False, contents=_feed()
    )
    row = _row_contract()
    _accept_more(
        instance,
        owner,
        {contract_path(row.name, row.version): render_contract(row), **_rows_layout(instance)},
        name="row",
    )
    # No output and no returns: before, this refused procedure_output_undeclared,
    # and `output={}` posted, then refused contract_output_refused on the read.
    plan = Sequence(
        "inbox-only",
        input={},
        output={},
        hard_caps=ProcedureLimits(
            wall_clock=f"PT{MAX_WALL_CLOCK.microseconds // 1_000_000}S",
            provider_calls=4,
            capture_bytes=131_072,
            items=200,
            repeat_attempts=1,
        ),
        slots=dict(procedure.definition.slots),
    )
    source = plan.call(
        SOURCE_ALIAS,
        slot="read",
        input=_source_request(instance, root).model_dump(mode="json"),
        captures={"capture": capture_contract().identity.name},
    )
    rows = plan.extract(ROWS_ALIAS, capture=source["capture"], records="rows")
    plan.inbox(
        f"{row.name}@{row.version}",
        for_each=rows["records"],
        fields={"cve": field("cve")},
        id="inbox",
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    plan.build()

    _accept_sdk_input(instance, owner, Cruxible.procedure(object(), definition=plan).payload)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    accepted = parse_procedure(
        tree[procedure_path("inbox-only")], path=procedure_path("inbox-only")
    )
    assert accepted.definition.returns is None
    line = _served_line(instance, accepted, policy)
    _accept_more(
        instance,
        owner,
        {line_spec_path(line.identity.name): render_line_spec(line)},
        name="inbox-only-line",
    )

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    assert sorted(item.payload["cve"] for item in _entries(instance)) == [
        item["cve"] for item in ROWS
    ]

"""One request reuses each bound projection instead of re-binding it per lookup (perf B3)."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import WriteRequest
from cruxible_core.indexes.evaluated_state import EvaluationRows
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._write_support import KIND, caller, seed_write_surface


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_write_surface(tmp_path)[0]


def _counted_binds(instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    binds: list[str] = []
    real = instance._bind_accepted_projection

    def bind(coordinate: Any) -> Any:
        binds.append(coordinate.git_oid)
        return real(coordinate)

    monkeypatch.setattr(instance, "_bind_accepted_projection", bind)
    return binds


def test_a_scope_hands_a_closed_handle_out_again_and_closes_all_at_exit(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = instance.accepted_coordinate()
    binds = _counted_binds(instance, monkeypatch)
    with instance.projection_scope():
        with instance.bind_accepted_projection(head) as first:
            # A handle is exclusive while held: a second holder gets its own.
            with instance.bind_accepted_projection(head) as second:
                assert second is not first
        with instance.bind_accepted_projection(head) as again:
            assert again in (first, second)
            assert not again._closed
        with instance.projection_scope():  # a nested scope joins the outer one
            with instance.bind_accepted_projection(head) as nested:
                assert nested in (first, second)
        assert len(binds) == 2
    assert first._closed and second._closed
    # Outside a scope every bind is fresh and really closes.
    with instance.bind_accepted_projection(head) as fresh:
        assert fresh not in (first, second)
    assert fresh._closed and len(binds) == 3


def test_a_handle_carrying_a_candidate_overlay_is_closed_never_reused(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = instance.accepted_coordinate()
    binds = _counted_binds(instance, monkeypatch)
    with instance.projection_scope():
        rows = EvaluationRows(instance.bind_accepted_projection(head))
        rows.overlay({})  # TEMP rows on the handle's connection
        overlaid = rows.projection
        rows.close()
        assert overlaid._closed
        with instance.bind_accepted_projection(head) as clean:
            assert clean is not overlaid
            assert (
                clean._connection.execute("SELECT count(*) FROM temp.sqlite_master").fetchone()[0]
                == 0
            )
        assert len(binds) == 2


def test_a_write_reads_the_same_answers_with_far_fewer_binds(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = WriteRequest.model_validate(
        {
            "because": "Counted binds.",
            "changes": [
                {"op": "set", "subject": f"{KIND}/scope-{n}", "field": "title", "value": f"t{n}"}
                for n in range(6)
            ],
            "dry_run": True,
        }
    )
    binds = _counted_binds(instance, monkeypatch)
    scoped = service_playbill_write(instance, request=request, caller=caller())
    scoped_binds = len(binds)
    binds.clear()
    monkeypatch.setattr(instance, "projection_scope", nullcontext)
    unscoped = service_playbill_write(instance, request=request, caller=caller())
    assert scoped.status == unscoped.status == "would_accept"

    def same_answer(outcome):  # type: ignore[no-untyped-def]
        # A dry run mints fresh Claim IDs each time; everything else is the answer.
        dumped = outcome.model_dump(mode="json")
        for change in dumped.get("changes") or ():
            change.pop("claim", None)
        return dumped

    assert same_answer(scoped) == same_answer(unscoped)
    assert scoped_binds < len(binds), (scoped_binds, len(binds))

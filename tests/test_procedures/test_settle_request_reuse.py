"""One settle request does its shared work once: per-item work stays flat as the batch grows.

A fan-out settle of N Claims over one shared ClaimType, CaptureContract and
Provider is run at two batch sizes in one world. Each test counts one kind of
work the request used to repeat per item (or per pass) and requires the count
to stay fixed, or to grow at most linearly, as N grows; the settled outcome is
the same as before.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.indexes import evaluated_state
from tests.test_procedures import test_procedure_settlement_delivery as SD
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import item_template

FAN_OUT = {"items": f"$steps.{fixtures.SOURCE_ALIAS}.content.json.proposals"}
SMALL, LARGE = 2, 6


def _element(index: int, batch: int) -> dict[str, object]:
    statement = {
        **item_template()["statement"],  # type: ignore[dict-item]
        "qualifier": f"q{index:03d}",
        "object": {"kind": "literal", "value": ("high", "low", "medium")[(index + batch) % 3]},
    }
    return item_template(statement=statement, rationale=f"Severity item {index}.")


def _world(tmp_path: Path) -> tuple[Any, Path, Any]:
    return SD.settle_world(tmp_path, candidate_templates=FAN_OUT)  # type: ignore[no-any-return]


def _settle(instance: Any, root: Path, line: Any, *, items: range, batch: int) -> Any:
    proposals = [_element(index, batch) for index in items]
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical_bytes({**fixtures.ADVISORY, "proposals": proposals})
    )
    state = SD.run_settle(
        instance, root, line, now=fixtures.NOW + timedelta(minutes=10 * (batch + 1))
    )
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled"), egress
    assert len(egress.children) == len(items)
    assert egress.accepted_git_oid == instance.accepted_coordinate().git_oid
    return egress


def _two_batches(tmp_path: Path, measure: Callable[[], Any]) -> tuple[Counter[str], Counter[str]]:
    """Settle SMALL then LARGE new Claims, each inside its own fresh ``measure()`` window."""

    instance, root, line = _world(tmp_path)
    counts = []
    for batch, items in enumerate((range(SMALL), range(SMALL, SMALL + LARGE))):
        with measure() as counter:
            _settle(instance, root, line, items=items, batch=batch)
        counts.append(counter)
    return counts[0], counts[1]


@contextmanager
def _patched(owner: Any, name: str, wrap: Callable[[Any], Any]) -> Iterator[None]:
    original = getattr(owner, name)
    setattr(owner, name, wrap(original))
    try:
        yield
    finally:
        setattr(owner, name, original)


# --- L3: one evaluation resolves each shared dependency once -------------------------


def test_an_evaluation_reads_each_shared_dependency_once_however_many_items_cite_it(
    tmp_path: Path,
) -> None:
    """The shared ClaimType, CaptureContract and Provider rows are read once per selection.

    Before, every member's lookup re-ran the row SELECT, read the source bytes,
    parsed and built the accepted model again: about a dozen ClaimType reads per
    item per evaluation.
    """

    owner_tables = ("claim_types", "capture_contracts", "providers")

    @contextmanager
    def measure() -> Iterator[Counter[str]]:
        counter: Counter[str] = Counter()

        def wrap(original: Any) -> Any:
            def one(self: Any, sql: str, key: str) -> Any:
                for table in owner_tables:
                    if f"{table} WHERE" in sql:
                        counter[table] += 1
                return original(self, sql, key)

            return one

        with _patched(evaluated_state.EvaluationRows, "one", wrap):
            yield counter

    small, large = _two_batches(tmp_path, measure)
    assert small["claim_types"] > 0, small
    for table in owner_tables:
        # Fixed per evaluation: the larger batch reads each shared row no more often.
        assert large[table] <= small[table], (table, small, large)

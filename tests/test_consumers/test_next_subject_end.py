"""A judgment on a pair resurfaces in ``next`` when the pair's end is no longer observed.

An app-level judgment (``app_exposure.justification``) stands on an occurrence
pair. When an import retires the occurrence's last observation (its scope),
the judgment is left standing on nothing current: ``next`` lists it as a stale
dependency naming the end and the retired observation, until the end is
observed again or the judgment is retired. The incremental rows equal the
fold's at every step, and the stored tables equal a rebuild.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from cruxible_core.consumers.next import facts
from cruxible_core.service.discovery.next import SUBJECT_END_UNOBSERVED
from tests.core_support._pair_world import (
    APP_EXPOSURE,
    COMPONENT,
    INVENTORY,
    OCCURRENCE,
    VULN,
    accepted,
    create,
    pair_world,
    write,
)
from tests.test_consumers.test_next_queue import _assert_equivalent, _drain, _served

AT = datetime.fromisoformat("2026-11-07T00:00:00+00:00")


def _claim(outcome: Any, field: str) -> str:
    return next(item.claim for item in outcome.changes if item.op == "set" and item.field == field)


def _stale(instance: Any) -> list[Any]:
    _drain(instance, AT)
    found = _served(instance, AT)
    assert found is not None
    return [item for item in found.rows if item.reason == "claim_dependency_stale"]


def _check(instance: Any) -> None:
    _assert_equivalent(instance, AT)
    assert facts.stored_tables(instance) == facts.rebuilt_tables(instance, at=AT)


def test_a_judgment_on_a_pair_whose_end_lost_its_observation_resurfaces(tmp_path: Path) -> None:
    instance = pair_world(tmp_path)
    built = accepted(
        write(
            instance,
            create("c", COMPONENT, "pkg:maven/log4j-core@2.14.1"),
            create("i", INVENTORY, "web"),
            create("v", VULN, "CVE-2021-44832"),
            create("o", OCCURRENCE, {"inventory": {"created": "i"}, "component": {"created": "c"}}),
            {"op": "set", "subject": {"created": "o"}, "field": "scope", "value": "required"},
            create(
                "a",
                APP_EXPOSURE,
                {"occurrence": {"created": "o"}, "vulnerability": {"created": "v"}},
            ),
            {
                "op": "set",
                "subject": {"created": "a"},
                "field": "justification",
                "value": "vulnerable_code_not_in_execute_path",
            },
        )
    )
    scope, judgment = _claim(built, "scope"), _claim(built, "justification")
    _drain(instance, AT)
    assert _stale(instance) == []
    _check(instance)

    # The import no longer lists the component: its scope observation is retired.
    accepted(write(instance, {"op": "retire", "target": scope, "reason": "was-rescinded"}))
    _assert_equivalent(instance, AT)  # behind: derived on read, as the fold folds
    (row,) = _stale(instance)
    assert row.subject_identity == f"Claim:{judgment.removeprefix('Claim:')}"
    assert row.related_identities == (f"Claim:{scope.removeprefix('Claim:')}",)
    (entry,) = row.detail["stale_inputs"]
    assert entry["impact_reasons"] == [SUBJECT_END_UNOBSERVED]
    assert entry["subject_end"].startswith(f"Subject:{OCCURRENCE}/")
    _check(instance)

    # Observed again: the end holds a live scope, so the judgment stands again.
    again = accepted(
        write(
            instance,
            {
                "op": "set",
                "subject": entry["subject_end"].removeprefix("Subject:"),
                "field": "scope",
                "value": "optional",
            },
        )
    )
    assert _stale(instance) == []
    _check(instance)

    # Gone again, then the judge retires the judgment: nothing is left to resurface.
    accepted(
        write(
            instance, {"op": "retire", "target": _claim(again, "scope"), "reason": "was-rescinded"}
        )
    )
    assert len(_stale(instance)) == 1
    accepted(write(instance, {"op": "retire", "target": judgment, "reason": "was-rescinded"}))
    assert _stale(instance) == []
    _check(instance)

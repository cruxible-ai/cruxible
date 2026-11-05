"""Runs record item changes (D-R37 g): a stable identity and a change digest per item.

Each claims item's child receipt carries its identity ({node, ClaimType,
Subject, unit}), the kind of the one canonical change it made, and that
change's digest: digests only, never the change body. The run read serves
them, so two runs of one Line diff by identity: an item moved to a new child
index keeps its identity and change digest, and a changed value (or a
re-observation) keeps its identity under a new change digest. A retirement from absence
carries the identity of the positive it retires.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.procedures.claims_units import claims_item_change_digest
from cruxible_core.procedures.terminal_dependencies import TerminalChildReceiptV1
from cruxible_core.service.procedures.procedure_runs import service_get_playbill_procedure_run
from tests.test_procedures.test_claims_absence_safety import safety_world
from tests.test_procedures.test_claims_terminal import (
    activate,
    claims_spec,
    claims_world,
    install,
    run,
)


def _entries(*items: tuple[str, str]) -> dict[str, Any]:
    return {"entries": [{"id": key, "status": status} for key, status in items]}


def _changes(instance: Any, state: Any) -> dict[str, tuple[str, str, int]]:
    """identity -> (change kind, change digest, child index), as the run read serves them."""

    assert state.status == "succeeded", state.terminal
    served = service_get_playbill_procedure_run(instance, run_id=state.run_id)
    (egress,) = served.terminal_egress
    assert egress.children == state.terminal_egress[0].children
    found: dict[str, tuple[str, str, int]] = {}
    for child in egress.children:
        assert child.item_identity_digest is not None, child
        assert child.change_kind is not None and child.change_digest is not None
        assert child.item_identity_digest not in found
        found[child.item_identity_digest] = (
            child.change_kind,
            child.change_digest,
            child.child_index,
        )
    return found


def test_two_runs_diff_by_item_identity(tmp_path: Path) -> None:
    instance, root = claims_world(
        tmp_path, _entries(("CVE-2026-0001", "open"), ("CVE-2026-0002", "open"))
    )
    install(instance, claims_spec())
    first = run(instance, root)
    before = _changes(instance, first)
    assert len(before) == 2
    assert {kind for kind, _, _ in before.values()} == {"claim"}

    # The same document again, in another order: the same items, the same
    # changes, at other child indexes (item_key moves; identity does not).
    (root / "data/doc.json").write_text(
        json.dumps(_entries(("CVE-2026-0002", "open"), ("CVE-2026-0001", "open")))
    )
    again = run(instance, root, minute=1)
    reordered = _changes(instance, again)
    assert {k: v[:2] for k, v in reordered.items()} == {k: v[:2] for k, v in before.items()}
    assert {k: v[2] for k, v in reordered.items()} != {k: v[2] for k, v in before.items()}

    # The later run's proposal superseded the first; accept it.
    activate(instance, again)
    (root / "data/doc.json").write_text(
        json.dumps(_entries(("CVE-2026-0001", "closed"), ("CVE-2026-0002", "open")))
    )
    second = run(instance, root, minute=2)
    after = _changes(instance, second)
    # Both items keep their identities: the changed value is a new claim change,
    # the same value read from the new document bytes a re-observation.
    assert set(after) == set(before)
    kinds = sorted(kind for kind, _, _ in after.values())
    assert kinds == ["claim", "reobservation"]
    assert all(after[k][1] != before[k][1] for k in before)


def test_a_retirement_from_absence_carries_the_retired_positives_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_procedures import test_claims_absence_safety as safety

    runs: list[Any] = []

    def recorded(*args: Any, **kwargs: Any) -> Any:
        runs.append(run(*args, **kwargs))
        return runs[-1]

    monkeypatch.setattr(safety, "run", recorded)
    instance, root, _raw = safety_world(tmp_path, "mirror")
    (first,) = runs
    positives = _changes(instance, first)
    assert len(positives) == 2
    assert {kind for kind, _, _ in positives.values()} == {"claim"}

    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    retired = _changes(instance, run(instance, root, minute=1))
    assert {kind for kind, _, _ in retired.values()} == {"retirement"}
    assert set(retired) == set(positives)


def test_a_child_receipt_records_its_change_as_digests_only() -> None:
    from cruxible_core.procedures.claims_changes import CoreSubjectChange

    change = CoreSubjectChange(subject_kind="k", key={"id": "x"}, rationale="New Subject.")
    digest = claims_item_change_digest(change)
    assert digest == claims_item_change_digest(change.model_copy())
    base = {
        "child_index": 0,
        "item_key": "00000000.a",
        "manifest_digest": "sha256:" + "1" * 64,
        "record_digest": "sha256:" + "2" * 64,
        "sequence": 1,
    }
    # Other terminals' items carry none of the fields, and keep their bytes.
    plain = TerminalChildReceiptV1.model_validate(base)
    assert set(plain.model_dump(mode="json")) == {"tag", *base}
    receipt = TerminalChildReceiptV1.model_validate(
        {
            **base,
            "item_identity_digest": "sha256:" + "3" * 64,
            "change_kind": "subject",
            "change_digest": digest,
        }
    )
    assert receipt.change_digest == digest
    with pytest.raises(ValueError, match="come together"):
        TerminalChildReceiptV1.model_validate({**base, "change_digest": digest})

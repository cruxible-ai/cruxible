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
    KIND,
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


def test_a_subject_created_with_its_claim_keeps_one_identity_after_acceptance(
    tmp_path: Path,
) -> None:
    """D-R37 g fix: one canonical Subject identity across creation and later runs.

    The creating run has no resolved Subject address yet, so its items hash
    the key-derived address computed before acceptance. Once the Subject and
    Claim are accepted, a reobservation and a revision each keep the claim
    item's identity -- before the fix, every later run hashed the resolved
    ``SemanticAddress`` instead (a different shape than the creating run's),
    so the comparison saw an unrelated removal/addition rather than the same
    item changing.
    """

    instance, root = claims_world(
        tmp_path,
        _entries(("CVE-2026-0001", "open"), ("CVE-2026-0002", "open")),
        seed_subjects=False,
    )
    install(instance, claims_spec(subject_create="if_absent"))

    created = run(instance, root)
    before = _changes(instance, created)
    assert len(before) == 4  # two Subject creations, two Claims
    assert {kind for kind, _, _ in before.values()} == {"subject", "claim"}
    claim_identities = {k for k, (kind, _, _) in before.items() if kind == "claim"}
    assert len(claim_identities) == 2
    activate(instance, created)

    # 0001 keeps its value; 0002's changes. The whole document's bytes
    # change either way, so 0001 is re-observed from a fresh Capture and
    # 0002 is revised.
    (root / "data/doc.json").write_text(
        json.dumps(_entries(("CVE-2026-0001", "open"), ("CVE-2026-0002", "closed")))
    )
    later = run(instance, root, minute=1)
    after = _changes(instance, later)
    # No new Subject items: both Subjects already exist. Both claim items
    # keep the identities the creating run gave them.
    assert set(after) == claim_identities
    assert sorted(kind for kind, _, _ in after.values()) == ["claim", "reobservation"]


@pytest.mark.executor_only
def test_a_subject_created_with_its_claim_then_retired_by_absence_keeps_its_identity(
    tmp_path: Path,
) -> None:
    """D-R37 g fix: an absence retirement carries the created positive's identity.

    The creating run has no resolved Subject address yet. Once the Subject
    and Claim are accepted, the source Capture no longer mentions this CVE
    at all; the resulting retirement must carry the SAME item identity the
    creating run gave its Claim -- the pre-fix code hashed the resolved
    ``SemanticAddress`` (``context.subject``'s whole-artifact address) for
    the retirement, an identity unrelated to the creating run's.
    """

    from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
    from cruxible_client.contracts.query.authored import QuerySpecInput
    from tests.core_support._identity_support import accept_tree
    from tests.support.operation_world import author
    from tests.support.procedures import boundary_contract, contract_files, procedure_spec

    document = {**_entries(("CVE-2026-0001", "open")), "count": 1}
    instance, root = claims_world(tmp_path, document, seed_subjects=False)

    # A key-shaped expect list (``{"cve_id": ...}``, no subject_id/path): the
    # same key reference the creating run's ``node.subject.key`` resolved,
    # so the fix's identity-subject rebuild applies to it, unlike a
    # path/id-named expect.
    watch_row = boundary_contract("test.watched-by-key", {"cve_id": "string"})
    accept_tree(instance, contract_files(watch_row), name="watch-row")
    author(
        instance,
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": "test.watched-by-key",
                    "from": {"v": KIND},
                    "row": watch_row.ref.spelling,
                    "fields": ["v.cve_id"],
                }
            ),
        ),
        name="watched-by-key-query",
    )

    raw = claims_spec(subject_create="if_absent").model_dump(mode="python", by_alias=True)
    raw["steps"].insert(1, {"read": "watched", "query": "test.watched-by-key"})
    raw["steps"][2]["complete_when"] = {"count_at": "/count"}
    write = raw["steps"][-1]
    write["claims"][0]["when_absent"] = "mirror"
    write["expect"] = {"$ref": "step", "step": "watched", "path": ["rows"]}
    install(instance, procedure_spec(base=raw, reference_text=True))

    created = run(instance, root)
    before = _changes(instance, created)
    (claim_identity,) = [k for k, (kind, _, _) in before.items() if kind == "claim"]
    activate(instance, created)

    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    retired = _changes(instance, run(instance, root, minute=1))
    assert set(retired) == {claim_identity}
    assert retired[claim_identity][0] == "retirement"


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


def test_a_many_valued_units_identity_is_its_object_however_it_is_held() -> None:
    """The template's resolved object (a dict) and a held Claim's object (a model)
    name one unit: the identity a write records is the one its retirement records."""

    from types import SimpleNamespace

    from cruxible_client.contracts.claims import LiteralClaimObject
    from cruxible_core.procedures.claims_units import claims_item_identity_digest

    many = SimpleNamespace(predicate="k.status", cardinality="many", inverse=None)
    one = SimpleNamespace(predicate="k.status", cardinality="one", inverse=None)

    def identity(ct: Any, obj: Any) -> str:
        return claims_item_identity_digest(
            node_id="write", subject={"kind": "k"}, claim_type=ct, obj=obj
        )

    held = LiteralClaimObject(value="open")
    assert identity(many, {"kind": "literal", "value": "open"}) == identity(many, held)
    assert identity(many, held) != identity(many, LiteralClaimObject(value="closed"))
    # A one-valued slot is one unit, whatever its value.
    assert identity(one, held) == identity(one, LiteralClaimObject(value="closed"))

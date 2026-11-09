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
from tests.support.procedures import procedure_spec
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

    The creating run identifies its Claim by the whole-artifact address
    lowering assigns the Subject it creates. Once both are accepted, the
    source Capture no longer mentions this CVE at all; the retirement, whose
    key-shaped ``expect`` resolves to that Subject's address, carries the
    SAME item identity the creating run gave its Claim.
    """

    from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
    from cruxible_client.contracts.query.authored import QuerySpecInput
    from tests.core_support._identity_support import accept_tree
    from tests.support.operation_world import author
    from tests.support.procedures import boundary_contract, contract_files, procedure_spec

    document = {**_entries(("CVE-2026-0001", "open")), "count": 1}
    instance, root = claims_world(tmp_path, document, seed_subjects=False)

    # A key-shaped expect list (``{"cve_id": ...}``, no subject_id/path).
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


_EXPECT_SPELLINGS = ("key", "bare_path", "path_row", "subject_id")


def _subdir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def _created_then_retired(tmp_path: Path, spelling: str) -> tuple[str, str, str]:
    """Create a Subject with its Claim, accept, then retire it by absence.

    ``expect`` comes from a Line parameter, spelled one way for the retiring
    run. Returns (created Claim identity, retirement identity, Subject path).
    """

    from cruxible_client.contracts.subjects import subject_path
    from tests.core_support._identity_support import accept_tree
    from tests.support.procedures import boundary_contract, contract_files

    document = {**_entries(("CVE-2026-0001", "open")), "count": 1}
    instance, root = claims_world(tmp_path, document, seed_subjects=False)
    if spelling == "bare_path":
        expected_type = "list<string>{max_items: 4}"
    else:
        field = {"key": "cve_id", "path_row": "path", "subject_id": "subject_id"}[spelling]
        row = boundary_contract(f"test.claims-item-changes.expect-{field}", {field: "string"})
        accept_tree(instance, contract_files(row), name="expect-row")
        expected_type = f"list<{row.ref.spelling}>{{max_items: 4}}"
    raw = claims_spec(subject_create="if_absent").model_dump(mode="python", by_alias=True)
    raw["params"] = {"expected": expected_type}
    raw["steps"][1]["complete_when"] = {"count_at": "/count"}
    write = raw["steps"][-1]
    write["claims"][0]["when_absent"] = "mirror"
    write["expect"] = "$params.expected"

    def line(name: str, expected: list[Any]) -> None:
        raw["procedure"] = "test." + name
        install(
            instance,
            procedure_spec(base=raw, reference_text=True),
            line_name=name,
            parameters={"expected": expected},
        )

    line("create-line", [])
    created = run(instance, root, line="create-line")
    before = _changes(instance, created)
    (claim_identity,) = [k for k, (kind, _, _) in before.items() if kind == "claim"]
    activate(instance, created)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    (path,) = [p for p in tree if p.startswith(f"subjects/{KIND}/")]
    subject_id = path.removeprefix(f"subjects/{KIND}/").removesuffix(".json")
    assert path == subject_path(KIND, subject_id)
    expected = {
        "key": {"cve_id": "CVE-2026-0001"},
        "bare_path": path,
        "path_row": {"path": path},
        "subject_id": {"subject_id": subject_id},
    }[spelling]
    line("retire-line", [expected])
    (root / "data/doc.json").write_text(json.dumps({"entries": [], "count": 0}))
    retired = _changes(instance, run(instance, root, minute=1, line="retire-line"))
    ((retired_identity, (kind, _, _)),) = retired.items()
    assert kind == "retirement"
    return claim_identity, retired_identity, path


@pytest.mark.executor_only
def test_a_created_subjects_retirement_keeps_its_identity_however_expect_spells_it(
    tmp_path: Path,
) -> None:
    """D-R37 g fix: one canonical identity, the resolved whole-artifact address.

    The creating run identifies its Claim by the address lowering assigns the
    Subject it creates; a retirement by absence resolves its ``expect`` to the
    same accepted Subject. A key-shaped, bare-path, path-row and ID-only
    ``expect`` give one identity digest, the created Claim's.
    """

    found = {
        spelling: _created_then_retired(_subdir(tmp_path, spelling), spelling)
        for spelling in _EXPECT_SPELLINGS
    }
    for claim_identity, retired_identity, _path in found.values():
        assert retired_identity == claim_identity
    # The four worlds create the same Subject, so they agree on one digest.
    assert len({identity for identity, _, _ in found.values()}) == 1
    assert len({path for _, _, path in found.values()}) == 1


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


def _identity_children(instance: Any, state: Any) -> dict[str, tuple[str, str | None, str]]:
    """identity -> (change kind, Subject path, child path), as the run read serves them.

    The Subject path is the created Subject's own path for a Subject item, and
    the Subject a Claim item's member names, read from the proposal tree.
    """

    from cruxible_client.contracts.claims import parse_claim
    from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal

    (served,) = service_get_playbill_procedure_run(instance, run_id=state.run_id).terminal_egress
    assert served.children == state.terminal_egress[0].children
    tree: Any = {}
    if served.proposal_id is not None:
        inspection = service_inspect_playbill_proposal(instance, proposal_id=served.proposal_id)
        tree = instance.proposal_tree(inspection.proposal.evaluation.evaluated_tree_oid)
    accepted = instance.tree_at(instance.accepted_coordinate().git_oid)
    found: dict[str, tuple[str, str | None, str]] = {}
    for child in served.children:
        assert child.item_identity_digest is not None and child.change_kind is not None
        assert child.item_identity_digest not in found, child
        path = str(child.path)
        subject: str | None = path if path.startswith("subjects/") else None
        if path.startswith("claims/"):
            content = tree.get(path) if path in tree else accepted.get(path)
            if content is not None:
                subject = parse_claim(content, path=path).statement.subject.artifact_path
        found[child.item_identity_digest] = (child.change_kind, subject, path)
    return found


def test_a_creation_whose_readable_id_a_sibling_alias_reserves_keeps_one_identity(
    tmp_path: Path,
) -> None:
    """D-R37 g fix 3: the creating run's identity is the address lowering assigns.

    One terminal gives the live ``existing`` package the normative alias
    ``new-value`` and creates the package keyed ``New Value``. That sibling
    alias reserves the creation's readable id, so lowering names it
    ``new-value-<digest>``, not ``new-value``. The creating run's items carry
    the identity of that assigned address: a later revision and an absence
    retirement of the created Subject's Claim carry the same identity.
    """

    from datetime import timedelta

    from cruxible_client.contracts.subjects import subject_path
    from tests.test_procedures import test_procedure_proposal_delivery as delivery
    from tests.test_procedures import test_procedure_source_runs as fixtures
    from tests.test_procedures import test_procedure_subject_items as items

    instance, owner, root = items._identity_world(tmp_path, "existing")
    alias = items._mapping("semantic.alias", "$item.fields.alias", role="normative")
    line = items._identity_terminal(
        instance,
        owner,
        root,
        [
            {"id": "existing", "alias": "new-value", "severity": "high"},
            {"id": "New Value", "severity": "low"},
        ],
        mappings=[alias, items._SEVERITY],
        create="if_absent",
    )
    created_run = delivery.run_line(instance, root, line)
    created = items._delivered(instance, created_run)
    before = _identity_children(instance, created_run)
    ((subject_identity, (_, created_path, _)),) = [
        (k, v) for k, v in before.items() if v[0] == "subject"
    ]
    assert created_path is not None
    # The sibling alias reserves the readable id: lowering suffixes it.
    assert created_path != subject_path(items.KIND, "new-value")
    assert created_path.startswith(f"subjects/{items.KIND}/new-value-"), created_path
    (claim_identity,) = [
        k for k, (kind, subject, _) in before.items() if kind == "claim" and subject == created_path
    ]
    # The identity is the assigned address's, not the predicted readable id's.
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_core.procedures.claims_units import claims_item_identity_digest

    assert subject_identity == claims_item_identity_digest(
        node_id="claims", subject=SemanticAddress.whole_artifact(created_path)
    )
    items._identity_accept(instance, owner, created)
    assert items._holder(instance, "New Value") == created_path

    # Rerun: the created package's severity changes (a revision of its Claim).
    (root / fixtures.RELATIVE_PATH).write_bytes(
        fixtures.canonical_bytes(
            {
                "proposals": [
                    {"id": "existing", "alias": "new-value", "severity": "high"},
                    {"id": "New Value", "severity": "medium"},
                ]
            }
        )
    )
    rerun_run = delivery.run_line(instance, root, line, at=fixtures.NOW + timedelta(minutes=1))
    rerun = items._delivered(instance, rerun_run)
    after = _identity_children(instance, rerun_run)
    assert after[claim_identity][:2] == ("claim", created_path), after
    assert subject_identity not in after  # the Subject exists now: no Subject item
    items._identity_accept(instance, owner, rerun)

    # Retire by absence: the source no longer lists the package it expects.
    retiring = items._identity_terminal(
        instance,
        owner,
        root,
        [],
        mappings=[{**items._SEVERITY, "when_absent": "mirror"}],
        name="identity-retire",
        expect=(created_path,),
    )
    retired = delivery.run_line(instance, root, retiring, at=fixtures.NOW + timedelta(minutes=2))
    items._delivered(instance, retired)
    assert _identity_children(instance, retired) == {
        claim_identity: ("retirement", created_path, after[claim_identity][2])
    }

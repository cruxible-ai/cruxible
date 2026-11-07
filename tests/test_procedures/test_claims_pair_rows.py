"""Procedures with pair Subjects as rows (S3a): nested end templates and absence by one end.

A claims step's row Subject may be a pair kind: its key names each end as a
nested ``{kind, key, create}`` template, resolved as a Subject-valued value is
(V1): an existing end is its path; an absent one with ``create: if_absent`` is
created by the run's one Subject item for it (V5), before the pair. A pair is
deduped by its final key, so two rows naming one pair are one pair.

``expect: {by, ends}`` judges absence by one end: the pairs of each listed end
that hold a live fact of a template's ClaimType come from the pair index
(``pair_units``, candidates only), and each is judged from its own Claims by the
ordinary gates (logical source, role, ``within``, completeness). The SBOM shape:
an occurrence per listed component, ``scope`` mirrored; the complete SBOM no
longer listing a component retires that occurrence's scope and leaves the
occurrence as a historical shell.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.core_support._pair_procedure_support import (
    APP_EXPOSURE,
    COMPONENT,
    INVENTORY,
    NOTE,
    OCCURRENCE,
    SCOPE,
    SEEN_IN,
    VULN,
    activate,
    end,
    install,
    keys_by_path,
    live_claims,
    memberships,
    occurrence_key,
    pair_procedure_world,
    pair_spec,
    rows,
    run,
    subjects_of,
    write_doc,
)

A, B, C, D = "pkg:pypi/a@1", "pkg:pypi/b@1", "pkg:pypi/c@1", "pkg:pypi/d@1"
LINE = "test.sbom-pairs"


def _changes(state: Any) -> list[str]:
    (egress,) = state.terminal_egress
    return sorted(child.change_kind for child in egress.children if child.change_kind)


def _diagnostics(state: Any) -> list[Any]:
    return [d for egress in state.terminal_egress for d in egress.claims_diagnostics]


def _expanded(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Each run's terminal changes, by run (the executor's own expansion)."""

    from cruxible_core.procedures import claims_terminal

    seen: dict[str, Any] = {}
    original = claims_terminal.expand_claims

    def recorded(node: Any, **kwargs: Any) -> Any:
        expanded = original(node, **kwargs)
        seen[kwargs["admission"].run_id] = expanded.changes
        return expanded

    monkeypatch.setattr(claims_terminal, "expand_claims", recorded)
    return seen


def _sbom(tmp_path: Path, **spec: Any) -> tuple[Any, Path]:
    instance, root = pair_procedure_world(tmp_path)
    install(
        instance, pair_spec("test.sbom-import", doc="sbom", source="test.sbom", **spec), line=LINE
    )
    return instance, root


# -- creation: ends first, pairs by final key -----------------------------------------


def test_an_import_creates_its_ends_then_its_pairs_and_writes_their_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expanded = _expanded(monkeypatch)
    instance, root = _sbom(tmp_path)
    write_doc(root, "sbom", rows("web", [A, B]), inventory="web")
    first = run(instance, root, line=LINE)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    # One inventory, two components, two occurrences; ends precede their pairs.
    from cruxible_core.procedures.claims_changes import CoreSubjectChange

    created = [c for c in expanded[first.run_id] if isinstance(c, CoreSubjectChange)]
    assert [c.subject_kind for c in created] == [
        INVENTORY,
        COMPONENT,
        OCCURRENCE,
        COMPONENT,
        OCCURRENCE,
    ]
    assert _changes(first) == ["claim", "claim", *["subject"] * 5]
    activate(instance, first)
    assert memberships(instance) == {"web": {A: "required", B: "required"}}
    assert len(subjects_of(instance, OCCURRENCE)) == 2

    # The same source again changes nothing: every pair is found by its ends.
    again = run(instance, root, line=LINE, minute=1)
    assert again.status == "succeeded", again.terminal
    assert again.terminal_egress[0].proposal_id is None
    assert not [c for c in expanded[again.run_id] if isinstance(c, CoreSubjectChange)]


def test_two_rows_naming_one_pair_are_one_pair(tmp_path: Path) -> None:
    """V5 on final keys: two spellings of one inventory name one end, so one occurrence."""

    instance, root = _sbom(tmp_path, by=None)
    # Duplicate extract keys would be refused; the rows differ in spelling only
    # after the key ClaimType's canonical form, so name the same end twice by value.
    write_doc(root, "sbom", rows("web", [A]) + rows("web", [B]), inventory="web")
    state = run(instance, root, line=LINE)
    activate(instance, state)
    (inventory,) = subjects_of(instance, INVENTORY)
    assert inventory.startswith(f"subjects/{INVENTORY}/")
    assert len(subjects_of(instance, OCCURRENCE)) == 2


def test_a_pair_of_existing_ends_is_created_once_and_then_found(tmp_path: Path) -> None:
    instance, root = _sbom(tmp_path)
    write_doc(root, "sbom", rows("web", [A]), inventory="web")
    activate(instance, run(instance, root, line=LINE))
    # The ends exist now; a new pair of them is created, an existing one found.
    write_doc(root, "sbom", rows("web", [A]) + rows("api", [A]), inventory="web")
    second = run(instance, root, line=LINE, minute=1)
    # api, (api, a) and its scope; (web, a) re-observed from the new bytes.
    assert _changes(second) == ["claim", "reobservation", "subject", "subject"]
    activate(instance, second)
    assert memberships(instance) == {"web": {A: "required"}, "api": {A: "required"}}


def test_a_pair_of_a_pair_is_created_with_every_end_in_one_run(tmp_path: Path) -> None:
    """app_exposure(occurrence(inventory, component), vulnerability): depth two, one run."""

    instance, root = pair_procedure_world(tmp_path)
    subject = {
        "kind": APP_EXPOSURE,
        "key": {
            "occurrence": end(OCCURRENCE, occurrence_key()),
            "vulnerability": end(VULN, {"vid": "$item.key.vid"}),
        },
        "create": "if_absent",
    }
    claims = [{"type": NOTE, "value": "$item.fields.inventory", "rationale": "Seen."}]
    spec = pair_spec(
        "test.app",
        doc="app",
        source="test.app",
        by=None,
        subject=subject,
        claims=claims,
        vid_key=True,
    )
    install(instance, spec, line="test.app-line")
    entries = [{**row, "vid": "CVE-1"} for row in rows("web", [A, B])]
    write_doc(root, "app", entries)
    state = run(instance, root, line="test.app-line")
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    activate(instance, state)
    keys = keys_by_path(live_claims(instance))
    found = sorted(keys[path] for path in subjects_of(instance, APP_EXPOSURE))
    assert found == [((A, "web"), "CVE-1"), ((B, "web"), "CVE-1")]
    assert len(subjects_of(instance, VULN)) == 1
    again = run(instance, root, line="test.app-line", minute=1)
    assert again.terminal_egress[0].proposal_id is None


def test_a_value_may_name_a_pair_and_create_it_with_its_ends(tmp_path: Path) -> None:
    """Value-side upsert of a pair (V1): the object is the pair's Subject item, ends first."""

    instance, root = pair_procedure_world(tmp_path)
    # The row is the inventory itself: the pair's inventory end is the row's
    # Subject item (V5), created once with the row's spelled key.
    subject = {"kind": INVENTORY, "key": {"name": "$item.key.inv"}, "create": "if_absent"}
    claims = [
        {
            "type": SEEN_IN,
            "value": {**end(OCCURRENCE, occurrence_key()), "create": "if_absent"},
            "rationale": "Seen in the occurrence.",
        }
    ]
    spec = pair_spec(
        "test.seen", doc="seen", source="test.seen", by=None, subject=subject, claims=claims
    )
    install(instance, spec, line="test.seen-line")
    write_doc(root, "seen", rows("web", [A, B]))
    state = run(instance, root, line="test.seen-line")
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    activate(instance, state)
    claims_now = live_claims(instance)
    keys = keys_by_path(claims_now)
    seen = sorted(
        keys[c.statement.object.address.artifact_path]
        for c in claims_now
        if c.statement.predicate == SEEN_IN
    )
    assert seen == [(A, "web"), (B, "web")]
    again = run(instance, root, line="test.seen-line", minute=1)
    assert again.terminal_egress[0].proposal_id is None


def _holds_spec() -> Any:
    subject = {"kind": INVENTORY, "key": {"name": "$item.key.inv"}, "create": "if_absent"}
    claims = [
        {
            "type": SEEN_IN,
            "value": {**end(OCCURRENCE, occurrence_key()), "create": "if_absent"},
            "rationale": "Seen in the occurrence.",
        }
    ]
    return pair_spec(
        "test.seen", doc="seen", source="test.seen", by=None, subject=subject, claims=claims
    )


@pytest.mark.parametrize("size", [10, 40])
def test_n_writes_into_one_slot_derive_each_unit_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """``slot_units``: N re-observations into one many-valued slot read N held units once."""

    from cruxible_core.procedures import claims_units

    instance, root = pair_procedure_world(tmp_path)
    install(instance, _holds_spec(), line="test.seen-line")
    write_doc(root, "seen", rows("web", [f"pkg:pypi/p{index}@1" for index in range(size)]))
    activate(instance, run(instance, root, line="test.seen-line"))
    calls = {"units": 0}
    original = claims_units._unit_value

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls["units"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(claims_units, "_unit_value", counted)
    write_doc(
        root, "seen", rows("web", [f"pkg:pypi/p{index}@1" for index in range(size)]), count=size
    )
    again = run(instance, root, line="test.seen-line", minute=1)
    assert again.status == "succeeded", again.terminal
    # The incoming unit, its item identity and each held unit once: 3N, never N^2.
    assert calls["units"] <= 3 * size + 8, calls


# -- absence by one end -----------------------------------------------------------


def test_absence_by_inventory_retires_the_scope_the_complete_sbom_no_longer_lists(
    tmp_path: Path,
) -> None:
    instance, root = _sbom(tmp_path)
    write_doc(root, "sbom", rows("web", [A, B]), inventory="web")
    activate(instance, run(instance, root, line=LINE))
    occurrences = subjects_of(instance, OCCURRENCE)

    write_doc(root, "sbom", rows("web", [A, C]), inventory="web")
    second = run(instance, root, line=LINE, minute=1)
    assert second.status == "succeeded", json.dumps(second.terminal.model_dump(mode="json"))
    assert _changes(second) == ["claim", "reobservation", "retirement", "subject", "subject"]
    activate(instance, second)
    assert memberships(instance) == {"web": {A: "required", C: "required"}}
    # B's occurrence stays, a historical shell: listed again, it is found, not re-created.
    assert set(occurrences) <= set(subjects_of(instance, OCCURRENCE))
    write_doc(root, "sbom", rows("web", [A, B, C]), inventory="web")
    third = run(instance, root, line=LINE, minute=2)
    assert _changes(third) == ["claim", "reobservation", "reobservation"]
    activate(instance, third)
    assert len(subjects_of(instance, OCCURRENCE)) == 3


def test_absence_never_crosses_into_another_inventory(tmp_path: Path) -> None:
    instance, root = _sbom(tmp_path)
    write_doc(root, "sbom", rows("other", [A, B]), inventory="other")
    activate(instance, run(instance, root, line=LINE))
    write_doc(root, "sbom", rows("web", [C]), inventory="web")
    mine = run(instance, root, line=LINE, minute=1)
    assert "retirement" not in _changes(mine)
    activate(instance, mine)
    assert memberships(instance) == {
        "other": {A: "required", B: "required"},
        "web": {C: "required"},
    }


def test_a_first_import_of_an_inventory_the_run_creates_judges_nothing(tmp_path: Path) -> None:
    """The listed end does not exist yet: this run creates it, so it holds no pair to judge."""

    instance, root = _sbom(tmp_path)
    write_doc(root, "sbom", rows("web", [A]), inventory="web")
    state = run(instance, root, line=LINE)
    assert state.status == "succeeded", state.terminal
    assert "retirement" not in _changes(state)


def test_an_end_that_names_nothing_and_is_not_created_refuses(tmp_path: Path) -> None:
    instance, root = _sbom(tmp_path)
    write_doc(root, "sbom", rows("web", [A]), inventory="nowhere")
    state = run(instance, root, line=LINE)
    assert state.status == "node_refused", state.status
    assert state.terminal.code == "claims_absence_invalid", state.terminal


@pytest.mark.parametrize("problem", ["count", "refused_record", "absent_header"])
def test_incomplete_input_retires_nothing(tmp_path: Path, problem: str) -> None:
    instance, root = _sbom(tmp_path)
    write_doc(root, "sbom", rows("web", [A, B]), inventory="web")
    activate(instance, run(instance, root, line=LINE))
    entries: list[Any] = rows("web", [A])
    if problem == "count":
        write_doc(root, "sbom", entries, inventory="web", count=5)
    elif problem == "refused_record":
        write_doc(root, "sbom", [*entries, "not a record"], inventory="web")
    else:
        write_doc(root, "sbom", entries)
    state = run(instance, root, line=LINE, minute=1)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert "retirement" not in _changes(state)
    if problem == "absent_header":
        assert any(d.kind == "incomplete_source" for d in _diagnostics(state))
    else:
        assert state.completeness is not None and not state.completeness.complete
    if state.terminal_egress[0].proposal_id is not None:
        activate(instance, state)
    assert memberships(instance) == {"web": {A: "required", B: "required"}}


@pytest.mark.parametrize("holder", ["other_source", "normative"])
def test_a_proposed_pair_holding_only_anothers_claim_is_never_retired(
    tmp_path: Path, holder: str
) -> None:
    """F7: the index proposes the pair; its scope is another source's or normative."""

    instance, root = _sbom(tmp_path)
    other = pair_spec(
        "test.other-import",
        doc="other",
        source="test.other" if holder == "other_source" else "test.sbom",
        by=None,
        role="normative" if holder == "normative" else None,
    )
    install(instance, other, line="test.other-line")
    write_doc(root, "other", rows("web", [B]))
    activate(instance, run(instance, root, line="test.other-line"))
    write_doc(root, "sbom", rows("web", [A]), inventory="web")
    state = run(instance, root, line=LINE, minute=1)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert "retirement" not in _changes(state)
    findings = [d for d in _diagnostics(state) if d.kind == "claim_not_in_source"]
    assert len(findings) == 1 and findings[0].claim_type == SCOPE
    activate(instance, state)
    assert memberships(instance) == {"web": {A: "required", B: "required"}}


def _scopes(instance: Any) -> set[tuple[str, str, str | None]]:
    """(inventory, purl, qualifier) of each live ``scope`` fact."""

    claims = live_claims(instance)
    keys = keys_by_path(claims)
    found = set()
    for c in claims:
        if c.statement.predicate == SCOPE:
            component, inventory = keys[c.statement.subject.artifact_path]
            found.add((inventory, component, c.statement.qualifier))
    return found


@pytest.mark.parametrize("by", ["inventory", "component"])
def test_a_keep_template_never_hides_its_claimtypes_mirror_template(
    tmp_path: Path, by: str
) -> None:
    """Review F1: one ClaimType under two qualifiers, one kept, one mirrored.

    By-end discovery proposes the pairs holding a ClaimType any non-keep
    template judges; a keep template of the same ClaimType never removes it.
    A complete empty enumeration retires only the mirror-qualified holder.
    """

    claims = [
        {
            "type": SCOPE,
            "value": "$item.fields.scope",
            "qualifier": qualifier,
            "when_absent": policy,
            "rationale": "The source lists the component in the inventory.",
        }
        for qualifier, policy in (("kept", "keep"), ("mirrored", "mirror"))
    ]
    instance, root = _sbom(tmp_path, by=by, claims=claims)
    header = {"inventory": "web"} if by == "inventory" else {"component": A}
    write_doc(root, "sbom", rows("web", [A]), **header)
    activate(instance, run(instance, root, line=LINE))
    assert _scopes(instance) == {("web", A, "kept"), ("web", A, "mirrored")}

    write_doc(root, "sbom", [], **header)
    state = run(instance, root, line=LINE, minute=1)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert _changes(state) == ["retirement"]
    activate(instance, state)
    assert _scopes(instance) == {("web", A, "kept")}


def test_absence_by_component_judges_that_components_occurrences_only(tmp_path: Path) -> None:
    """The other orientation: one component's occurrences across inventories."""

    instance, root = _sbom(tmp_path, by="component")
    write_doc(root, "sbom", rows("web", [A]) + rows("api", [A]) + rows("web", [B]), component=A)
    activate(instance, run(instance, root, line=LINE))
    write_doc(root, "sbom", rows("web", [A]), component=A)
    state = run(instance, root, line=LINE, minute=1)
    assert _changes(state) == ["reobservation", "retirement"]
    activate(instance, state)
    assert memberships(instance) == {"web": {A: "required", B: "required"}}


def test_an_occurrence_outside_within_is_counted_never_retired(tmp_path: Path) -> None:
    instance, root = _sbom(tmp_path)
    write_doc(root, "sbom", rows("web", [A, B]), inventory="web")
    activate(instance, run(instance, root, line=LINE))
    keys = keys_by_path(live_claims(instance))
    inside = next(p for p in subjects_of(instance, OCCURRENCE) if keys[p] == (A, "web"))
    spec = pair_spec("test.sbom-within", doc="sbom", source="test.sbom", within=True)
    install(instance, spec, line="test.within-line", parameters={"within": [inside]})
    write_doc(root, "sbom", [], inventory="web")
    state = run(instance, root, line="test.within-line", minute=1)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert _changes(state) == ["retirement"]
    assert state.completeness is not None
    assert state.completeness.expect_outside_within == 1
    activate(instance, state)
    assert memberships(instance) == {"web": {B: "required"}}


# -- ambiguous and malformed ends never create --------------------------------------


def test_an_ambiguous_end_key_refuses_its_row_and_retires_nothing(tmp_path: Path) -> None:
    from cruxible_client.contracts.authoring.inputs import SubjectInput
    from tests.core_support._pair_world import accept, change_set

    instance, root = pair_procedure_world(tmp_path, hint=True)
    install(
        instance, pair_spec("test.sbom-import", doc="sbom", source="test.sbom", by=None), line=LINE
    )
    for index in range(2):
        accept(
            instance,
            change_set(
                SubjectInput(
                    kind="subject",
                    subject_kind=INVENTORY,
                    key={"name": "web"},
                    create_new=bool(index),
                    rationale="Two holders of one hint key.",
                )
            ),
        )
    write_doc(root, "sbom", rows("web", [A]) + rows("api", [B]))
    state = run(instance, root, line=LINE)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    refused = [d for d in _diagnostics(state) if d.problem == "key_part_unresolved"]
    assert len(refused) == 1
    activate(instance, state)
    assert memberships(instance) == {"api": {B: "required"}}


def test_an_end_absent_without_create_skips_its_row(tmp_path: Path) -> None:
    subject = {
        "kind": OCCURRENCE,
        "key": occurrence_key(create="never"),
        "create": "if_absent",
    }
    instance, root = _sbom(tmp_path, by=None, subject=subject)
    write_doc(root, "sbom", rows("web", [A]))
    state = run(instance, root, line=LINE)
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress[0].proposal_id is None
    assert subjects_of(instance, OCCURRENCE) == []


# -- authoring refusals ---------------------------------------------------------------


def _refusal(tmp_path: Path, spec: Any) -> Any:
    from cruxible_client.contracts.authoring.inputs import AuthoringInputError
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    instance, _root = pair_procedure_world(tmp_path)
    with pytest.raises((AuthoringLoweringError, AuthoringInputError)) as refused:
        install(instance, spec, line=LINE)
    return refused.value


@pytest.mark.parametrize("by", ["nonexistent", "scope"])
def test_expect_by_must_name_an_end_of_the_row_kind(tmp_path: Path, by: str) -> None:
    spec = pair_spec(
        "test.sbom-import",
        doc="sbom",
        source="test.sbom",
        expect={"by": by, "ends": ["$steps.entries.header.inventory"]},
    )
    refused = _refusal(tmp_path, spec)
    assert refused.code == "cruxible.authoring.claims_expect_by_invalid", refused


def test_on_is_refused_at_authoring_as_deferred(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.authored import (
        ProcedureSpecError,
        lower_procedure_spec,
    )

    spec = pair_spec(
        "test.sbom-import",
        doc="sbom",
        source="test.sbom",
        by=None,
        claims=[{"type": SCOPE, "value": "$item.fields.scope", "on": "component"}],
    )
    with pytest.raises(ProcedureSpecError) as refused:
        lower_procedure_spec(spec)
    assert refused.value.code == "cruxible.authoring.claims_on_deferred"
    assert refused.value.field_path.endswith(".claims[0].on")
    assert _refusal(tmp_path, spec).code == "cruxible.authoring.claims_on_deferred"


def test_a_compiled_template_naming_on_is_refused() -> None:
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.claims import ClaimTemplate

    with pytest.raises(ValidationError, match="claims_on_deferred"):
        ClaimTemplate.model_validate(
            {"type": {"target": {"kind": "ClaimType", "name": SCOPE}}, "value": "x", "on": "c"}
        )


def test_a_pair_end_named_by_text_is_refused_at_authoring(tmp_path: Path) -> None:
    subject = {
        "kind": OCCURRENCE,
        "key": {
            "inventory": "$item.key.inv",
            "component": end(COMPONENT, {"purl": "$item.key.purl"}),
        },
        "create": "if_absent",
    }
    refused = _refusal(
        tmp_path,
        pair_spec("test.sbom-import", doc="sbom", source="test.sbom", by=None, subject=subject),
    )
    assert refused.code == "cruxible.authoring.claims_type_invalid", refused
    assert "nested Subject template" in refused.message

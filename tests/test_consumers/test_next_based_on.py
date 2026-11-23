"""A judgment pins the observation it rests on (``based-on``, compiler revision 74).

The pin is provenance: revising or retiring the observation never carries or
retires the judgment. ``next`` lists the judgment as ``claim_dependency_stale``
until it is revised to pin the observation's current version, drops the pin,
or is retired. The incremental rows equal the fold's at every step.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.claims import (
    BASED_ON_CLAIM_PIN_ROLE,
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_core.consumers.next import facts
from tests.core_support._pair_world import (
    AFFECTED_BY,
    APP_EXPOSURE,
    COMPONENT,
    INVENTORY,
    OCCURRENCE,
    VULN,
    accepted,
    create,
    head_tree,
    pair_world,
    write,
)
from tests.test_consumers.test_next_queue import _assert_equivalent, _drain, _served

AT = datetime.fromisoformat("2026-11-07T00:00:00+00:00")


def _claim(outcome: Any, field: str) -> str:
    return next(
        item.claim.removeprefix("Claim:")
        for item in outcome.changes
        if item.op == "set" and item.field == field
    )


def _stale(instance: Any) -> list[Any]:
    _drain(instance, AT)
    found = _served(instance, AT)
    assert found is not None
    return [item for item in found.rows if item.reason == "claim_dependency_stale"]


def _check(instance: Any) -> None:
    _assert_equivalent(instance, AT)
    assert facts.stored_tables(instance) == facts.rebuilt_tables(instance, at=AT)


def _read(instance: Any, claim_id: str) -> Any:
    path = claim_path(claim_id)
    return parse_claim(head_tree(instance)[path], path=path)


def _based_on(instance: Any, claim_id: str) -> dict[str, str]:
    return {
        pin.target.name: pin.artifact_digest
        for pin in _read(instance, claim_id).pins
        if pin.role == BASED_ON_CLAIM_PIN_ROLE
    }


def _world(tmp_path: Path) -> tuple[Any, str, str, str]:
    """An occurrence's scope (an observation) and an app-level judgment based on it."""

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
        )
    )
    scope = _claim(built, "scope")
    exposure = next(
        item.subject
        for item in built.changes
        if item.op == "create" and item.subject and item.subject.startswith(APP_EXPOSURE)
    )
    occurrence = next(
        item.subject
        for item in built.changes
        if item.op == "create" and item.subject and item.subject.startswith(OCCURRENCE)
    )
    judged = accepted(
        write(
            instance,
            {
                "op": "set",
                "subject": exposure,
                "field": "justification",
                "value": "vulnerable_code_not_in_execute_path",
                "based_on": [scope],
            },
        )
    )
    return instance, scope, _claim(judged, "justification"), occurrence


def test_a_judgment_pins_its_observation_and_resurfaces_when_it_is_revised(
    tmp_path: Path,
) -> None:
    instance, scope, judgment, occurrence = _world(tmp_path)
    assert _based_on(instance, judgment) == {
        scope: claim_artifact_digest(_read(instance, scope)).tagged
    }
    assert _stale(instance) == []
    _check(instance)

    # The observation is revised: the judgment stays live and resurfaces.
    accepted(
        write(instance, {"op": "set", "subject": occurrence, "field": "scope", "value": "optional"})
    )
    _assert_equivalent(instance, AT)  # behind: derived on read, as the fold folds
    (row,) = _stale(instance)
    assert row.subject_identity == f"Claim:{judgment}"
    assert row.related_identities == (f"Claim:{scope}",)
    (entry,) = row.detail["stale_inputs"]
    assert entry["pin_role"] == BASED_ON_CLAIM_PIN_ROLE
    assert entry["used_artifact_digest"] != entry["current_artifact_digest"]
    assert entry["current_artifact_digest"] == claim_artifact_digest(_read(instance, scope)).tagged
    assert _read(instance, judgment).lifecycle.state == "live"
    _check(instance)

    # Re-affirmed against the current observation: the row clears.
    accepted(
        write(
            instance,
            {
                "op": "set",
                "subject": _read(instance, judgment)
                .statement.subject.artifact_path.removeprefix("subjects/")
                .removesuffix(".json"),
                "field": "justification",
                "value": "vulnerable_code_not_in_execute_path",
                "based_on": [scope],
            },
        )
    )
    assert _based_on(instance, judgment) == {
        scope: claim_artifact_digest(_read(instance, scope)).tagged
    }
    assert _stale(instance) == []
    _check(instance)


def test_retiring_the_observation_never_retires_the_judgment(tmp_path: Path) -> None:
    instance, scope, judgment, _occurrence = _world(tmp_path)
    outcome = accepted(write(instance, {"op": "retire", "target": scope, "reason": "was-wrong"}))
    retired = {item.claim for item in outcome.changes if item.op == "retire"}
    assert retired == {f"Claim:{scope}"} or retired == {scope}
    assert _read(instance, scope).lifecycle.state == "retired"
    assert _read(instance, judgment).lifecycle.state == "live"
    rows = _stale(instance)
    based = [
        row
        for row in rows
        if any(
            item.get("pin_role") == BASED_ON_CLAIM_PIN_ROLE for item in row.detail["stale_inputs"]
        )
    ]
    (row,) = based
    assert row.subject_identity == f"Claim:{judgment}"
    _check(instance)

    # Dropping the pin clears the based-on entry; the Claim keeps its value.
    subject = (
        _read(instance, judgment)
        .statement.subject.artifact_path.removeprefix("subjects/")
        .removesuffix(".json")
    )
    accepted(
        write(
            instance,
            {
                "op": "set",
                "subject": subject,
                "field": "justification",
                "value": "vulnerable_code_not_in_execute_path",
                "based_on": [],
            },
        )
    )
    assert _based_on(instance, judgment) == {}
    assert not any(
        item.get("pin_role") == BASED_ON_CLAIM_PIN_ROLE
        for row in _stale(instance)
        for item in row.detail["stale_inputs"]
    )
    _check(instance)


def test_an_omitted_based_on_keeps_the_pins_a_revision_carries(tmp_path: Path) -> None:
    instance, scope, judgment, occurrence = _world(tmp_path)
    pinned = _based_on(instance, judgment)
    accepted(
        write(instance, {"op": "set", "subject": occurrence, "field": "scope", "value": "optional"})
    )
    subject = (
        _read(instance, judgment)
        .statement.subject.artifact_path.removeprefix("subjects/")
        .removesuffix(".json")
    )
    accepted(
        write(
            instance,
            {
                "op": "set",
                "subject": subject,
                "field": "justification",
                "value": "component_not_present",
            },
        )
    )
    # A new value without based_on keeps the stale pin, so next keeps asking.
    assert _based_on(instance, judgment) == pinned
    (row,) = _stale(instance)
    assert row.subject_identity == f"Claim:{judgment}"
    _check(instance)


@pytest.mark.parametrize("case", ["missing", "retired", "self"])
def test_based_on_refuses_a_target_that_is_not_a_live_other_claim(
    tmp_path: Path, case: str
) -> None:
    instance, scope, judgment, _occurrence = _world(tmp_path)
    subject = (
        _read(instance, judgment)
        .statement.subject.artifact_path.removeprefix("subjects/")
        .removesuffix(".json")
    )
    if case == "retired":
        accepted(write(instance, {"op": "retire", "target": scope, "reason": "was-wrong"}))
        target, code = scope, "cruxible.authoring.based_on_retired"
    elif case == "missing":
        target, code = "CLM-" + "ab" * 16, "cruxible.authoring.based_on_not_found"
    else:
        target, code = judgment, "cruxible.authoring.based_on_self"
    before = instance.accepted_coordinate()
    outcome = write(
        instance,
        {
            "op": "set",
            "subject": subject,
            "field": "justification",
            "value": "component_not_present",
            "based_on": [target],
        },
    )
    assert outcome.status != "accepted"
    assert outcome.refusal is not None and outcome.refusal.code == code, outcome.refusal
    assert instance.accepted_coordinate() == before


def test_a_revision_73_instance_refuses_based_on_until_it_upgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.compiler.compiler import COMPILER_REVISION_73

    with monkeypatch.context() as patch:
        patch.setattr(
            "cruxible_core.runtime.instance.current_compiler_coordinate",
            lambda: COMPILER_REVISION_73,
        )
        instance = pair_world(tmp_path)
    assert instance.accepted_coordinate().compiler == COMPILER_REVISION_73
    built = accepted(
        write(
            instance,
            create("c", COMPONENT, "pkg:maven/log4j-core@2.14.1"),
            create("i", INVENTORY, "web"),
            create("o", OCCURRENCE, {"inventory": {"created": "i"}, "component": {"created": "c"}}),
            {"op": "set", "subject": {"created": "o"}, "field": "scope", "value": "required"},
        )
    )
    scope = _claim(built, "scope")
    occurrence = next(
        item.subject
        for item in built.changes
        if item.op == "create" and item.subject and item.subject.startswith(OCCURRENCE)
    )
    before = instance.accepted_coordinate()
    outcome = write(
        instance,
        {
            "op": "set",
            "subject": occurrence,
            "field": "scope",
            "value": "optional",
            "based_on": [scope],
        },
    )
    assert outcome.refusal is not None
    assert outcome.refusal.code == "cruxible.authoring.based_on_requires_compiler_upgrade"
    assert instance.accepted_coordinate() == before


# -- kind/id#field: a field's one live Claim, resolved by the write ---------------


def _exposure(instance: Any, judgment: str) -> str:
    return (
        _read(instance, judgment)
        .statement.subject.artifact_path.removeprefix("subjects/")
        .removesuffix(".json")
    )


def _judge(instance: Any, subject: str, *based_on: str) -> Any:
    return write(
        instance,
        {
            "op": "set",
            "subject": subject,
            "field": "justification",
            "value": "component_not_present",
            "based_on": list(based_on),
        },
    )


@pytest.mark.parametrize("field", ["scope", f"{OCCURRENCE}.scope"])
def test_based_on_names_a_fields_one_live_claim_as_subject_hash_field(
    tmp_path: Path, field: str
) -> None:
    instance, scope, judgment, occurrence = _world(tmp_path)
    accepted(
        write(instance, {"op": "set", "subject": occurrence, "field": "scope", "value": "optional"})
    )
    # Named by the shorthand (short field or predicate, '@' optional) and by id
    # at once: one pin, on the Claim's current version, which clears the row.
    accepted(_judge(instance, _exposure(instance, judgment), f"@{occurrence}#{field}", scope))
    assert _based_on(instance, judgment) == {
        scope: claim_artifact_digest(_read(instance, scope)).tagged
    }
    assert _stale(instance) == []
    _check(instance)


def test_based_on_shorthand_resolves_a_subject_by_its_key(tmp_path: Path) -> None:
    instance = pair_world(tmp_path)
    built = accepted(
        write(
            instance,
            create("c", COMPONENT, "pkg:maven/log4j-core@2.14.1"),
            create("v", VULN, "CVE-2021-44228"),
            create("w", VULN, "CVE-2021-44832"),
            {
                "op": "add",
                "subject": {"created": "c"},
                "field": "affected_by",
                "value": {"created": "v"},
            },
            create("i", INVENTORY, "web"),
            create("o", OCCURRENCE, {"inventory": {"created": "i"}, "component": {"created": "c"}}),
        )
    )
    affected = next(item.claim for item in built.changes if item.op == "add")
    assert affected is not None
    occurrence = next(
        item.subject
        for item in built.changes
        if item.op == "create" and item.subject and item.subject.startswith(OCCURRENCE)
    )
    component = f"{COMPONENT}/pkg:maven/log4j-core@2.14.1"
    outcome = accepted(
        write(
            instance,
            {
                "op": "set",
                "subject": occurrence,
                "field": "scope",
                "value": "required",
                "based_on": [f"{component}#affected_by"],
            },
        )
    )
    scope = _claim(outcome, "scope")
    assert set(_based_on(instance, scope)) == {affected.removeprefix("Claim:")}

    # A second live value: the shorthand no longer names one Claim.
    second = accepted(
        write(
            instance,
            {
                "op": "add",
                "subject": component,
                "field": "affected_by",
                "value": f"{VULN}/CVE-2021-44832",
            },
        )
    )
    other = next(item.claim for item in second.changes if item.op == "add")
    assert other is not None
    before = instance.accepted_coordinate()
    refused = write(
        instance,
        {
            "op": "set",
            "subject": occurrence,
            "field": "scope",
            "value": "optional",
            "based_on": [f"{component}#{AFFECTED_BY}"],
        },
    )
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.write.based_on_ambiguous", refused.refusal
    assert set(refused.refusal.candidates) == {
        affected.removeprefix("Claim:"),
        other.removeprefix("Claim:"),
    }
    assert refused.refusal.repair is not None and "by ID" in refused.refusal.repair
    assert instance.accepted_coordinate() == before


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("empty", "cruxible.write.based_on_not_found"),
        ("retired_here", "cruxible.write.based_on_not_found"),
        ("unknown_field", "cruxible.write.unknown_field"),
        ("unknown_subject", "cruxible.write.subject_not_found"),
        ("unknown_kind", "cruxible.write.unknown_kind"),
    ],
)
def test_based_on_shorthand_refuses_rather_than_guessing(
    tmp_path: Path, case: str, code: str
) -> None:
    instance, scope, judgment, occurrence = _world(tmp_path)
    exposure = _exposure(instance, judgment)
    changes: list[dict[str, Any]] = []
    if case == "empty":
        accepted(write(instance, {"op": "retire", "target": scope, "reason": "was-wrong"}))
        named = f"{occurrence}#scope"
    elif case == "retired_here":
        # Retired by this same write: what the write leaves holds no Claim.
        changes.append({"op": "retire", "target": scope, "reason": "was-wrong"})
        named = f"{occurrence}#scope"
    elif case == "unknown_field":
        named = f"{occurrence}#no_such_field"
    elif case == "unknown_subject":
        named = f"{OCCURRENCE}/no-such-occurrence#scope"
    else:
        named = "sec.nothing/x#scope"
    before = instance.accepted_coordinate()
    outcome = write(
        instance,
        *changes,
        {
            "op": "set",
            "subject": exposure,
            "field": "justification",
            "value": "component_not_present",
            "based_on": [named],
        },
    )
    assert outcome.status != "accepted"
    assert outcome.refusal is not None and outcome.refusal.code == code, outcome.refusal
    assert outcome.refusal.field_path is not None
    assert outcome.refusal.field_path.endswith(".based_on")
    assert instance.accepted_coordinate() == before


def test_based_on_shorthand_on_the_slot_written_refuses_as_self(tmp_path: Path) -> None:
    instance, _scope, judgment, _occurrence = _world(tmp_path)
    exposure = _exposure(instance, judgment)
    outcome = _judge(instance, exposure, f"{exposure}#justification")
    assert outcome.refusal is not None
    assert outcome.refusal.code == "cruxible.authoring.based_on_self", outcome.refusal


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        (["@sec.finding/a#kev_status"], ("sec.finding/a#kev_status",)),
        (
            ["Claim:CLM-" + "a" * 32, "sec.finding/CVE#1#x.y", "CLM-" + "a" * 32],
            ("CLM-" + "a" * 32, "sec.finding/CVE#1#x.y"),
        ),
    ],
)
def test_the_write_contract_takes_claim_ids_and_subject_hash_field(
    given: list[str], kept: tuple[str, ...]
) -> None:
    from cruxible_client.contracts.write import SetChange, based_on_slot

    change = SetChange(field="f", value=1, based_on=tuple(given))
    assert change.based_on == kept
    assert based_on_slot("sec.finding/CVE#1#x.y") == ("sec.finding/CVE#1", "x.y")


@pytest.mark.parametrize(
    "bad", ["CLM-123", "kev_status", "sec.finding/a#", "#kev_status", "Sec/a#f", "a#f"]
)
def test_the_write_contract_refuses_other_based_on_names(bad: str) -> None:
    from pydantic import ValidationError

    from cruxible_client.contracts.write import SetChange

    with pytest.raises(ValidationError, match="CLM-<32 hex> or kind/id#field"):
        SetChange(field="f", value=1, based_on=(bad,))

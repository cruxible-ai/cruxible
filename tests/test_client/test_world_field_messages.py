"""World write refusals a first-time kit author meets (coldkit-06, coldkit-07)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import Cruxible
from cruxible_client.authoring.world import (
    World,
    WorldFieldValueRefused,
    WorldReservedKeyword,
    WorldStructureError,
)
from tests.test_client._read_fakes import ClaimTypeListing
from tests.test_client.test_playbill_sdk_world import _claim_type, _workspace, _WorldClient

ROLE = "sec.package.role"
SKILLS = "sec.package.skills"


class _FieldsClient(_WorldClient):
    """A package kind with a field named like the `role=` keyword and a many-valued field."""

    def _claim_type_list(self, _instance_id: str, *, at: Any = None) -> ClaimTypeListing:
        self.claim_type_list_calls += 1
        return ClaimTypeListing(
            coordinate=self.coordinate,
            claim_types=[
                _claim_type(
                    ROLE,
                    value_type={"type": "string", "enum": ["primary", "secondary"]},
                    allowed_subject_kinds=("sec.package",),
                ),
                _claim_type(
                    SKILLS,
                    value_type={"type": "string"},
                    allowed_subject_kinds=("sec.package",),
                    cardinality="many",
                ),
            ],
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    _workspace(tmp_path)
    playbill = Cruxible._from_client(  # type: ignore[arg-type]
        _FieldsClient(),
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    return playbill.world()


def test_a_field_named_role_refuses_as_reserved_and_names_the_full_name_form(world: World):
    with pytest.raises(WorldReservedKeyword) as caught:
        world.sec.package.define("click", role="primary")
    message = str(caught.value)
    assert "cruxible.sdk.reserved_keyword" in message
    assert "role= is reserved for the Claim role" in message
    assert "define(**{'sec.package.role': 'primary'})" in message
    assert "name the field differently" in message
    assert isinstance(caught.value, WorldStructureError)

    # The full predicate name is the working form.
    draft = world.sec.package.define("click", **{ROLE: "primary"})
    assert [(change.field, change.value) for change in draft._changes] == [(ROLE, "primary")]
    with pytest.raises(WorldReservedKeyword):
        draft.set(role="secondary")
    # A real Claim role still means the Claim role.
    assert draft.set(role="observation", **{ROLE: "secondary"})._changes[-1].role == "observation"


def test_a_role_that_is_no_claim_role_refuses_plainly_without_such_a_field(tmp_path: Path):
    _workspace(tmp_path)
    playbill = Cruxible._from_client(  # type: ignore[arg-type]
        _WorldClient(),
        instance_id="inst_world",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )
    with pytest.raises(WorldReservedKeyword, match="role= is the Claim role"):
        playbill.world().sec.package.define("click", role="primary")


def test_a_list_for_a_many_valued_field_adds_one_value_per_item_in_order(world: World):
    draft = world.sec.package.define("click", skills=["a", "b"])
    assert [(change.op, change.field, change.value) for change in draft._changes] == [
        ("add", SKILLS, "a"),
        ("add", SKILLS, "b"),
    ]
    # A tuple too, and add() on a draft, an edit or a Subject's edit.
    grown = draft.add(skills=("c", "d"))
    assert [change.value for change in grown._changes] == ["a", "b", "c", "d"]
    edit = world.sec.package["cryptography"].edit().add(skills=["x", "y"], expect_absent=True)
    assert [(change.value, change.expect_absent) for change in edit._changes] == [
        ("x", True),
        ("y", True),
    ]
    # A string is one value, never its characters.
    assert [change.value for change in world.sec.package.define("c", skills="ab")._changes] == [
        "ab"
    ]


def test_a_collection_no_add_can_order_or_hold_still_refuses(world: World):
    with pytest.raises(WorldFieldValueRefused) as caught:
        world.sec.package.define("click", skills={"a", "b"})
    message = str(caught.value)
    assert "cruxible.sdk.field_value_refused" in message
    assert "skills was given a set" in message
    assert "pass a list or a tuple" in message
    with pytest.raises(WorldFieldValueRefused, match="empty list"):
        world.sec.package.define("click", skills=[])
    with pytest.raises(WorldFieldValueRefused, match="it holds one value"):
        world.sec.package.define("click", skills=[["a"]])
    draft = world.sec.package.define("click")
    with pytest.raises(WorldFieldValueRefused, match="it holds one value"):
        draft.set(**{ROLE: ["primary"]})
    # set() on a many-valued field takes one value; the repair names add().
    with pytest.raises(WorldFieldValueRefused, match=r"\.add\(skills=\[a, b\]\)"):
        draft.set(skills=["a", "b"])


def test_a_positional_field_names_the_keyword_form(world: World):
    draft = world.sec.package.define("click")
    with pytest.raises(WorldFieldValueRefused) as caught:
        draft.add("skills", "x")  # type: ignore[misc]
    assert "write .add(skills='x')" in str(caught.value)
    subject = world.sec.package["cryptography"]
    with pytest.raises(WorldFieldValueRefused, match=r"write \.set\(<field>=<value>\)"):
        subject.edit().set("x")  # type: ignore[misc]

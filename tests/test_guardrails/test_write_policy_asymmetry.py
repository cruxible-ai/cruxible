"""Guardrail: the entity/relationship ``write_policy`` asymmetry stays unreachable.

``apply_entity`` branches on ``mint_only`` and refuses every source except
``token_mint``. ``apply_relationship`` has NO such branch — it only handles
``proposal_only``. Today that asymmetry is harmless because
``RelationshipSchema.write_policy`` is a Literal that does not admit
``mint_only``, so the missing branch is unreachable.

The trap: widening that ONE Literal — a plausible, self-contained-looking config
change — would not add a refusal. It would make every ``mint_only`` relationship
type FREELY WRITABLE, because the chokepoint falls through the ``proposal_only``
check that does not match and then writes. The declared governance would read as
the strictest policy in the vocabulary while enforcing nothing.

These tests are BEHAVIORAL: they force the policy through the resolver and drive
the real chokepoint, rather than grepping the chokepoint's source for the string
``mint_only`` — which a comment (including this one) would satisfy.
"""

from __future__ import annotations

from typing import Any, Literal, Union, get_args, get_origin

import pytest

from cruxible_core.config.loader import load_config_from_string
from cruxible_core.config.schema import CoreConfig, RelationshipSchema
from cruxible_core.errors import DirectWriteRefusedError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.operations import (
    apply_entity,
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.types import EntityInstance, RelationshipInstance

_MINT_ONLY = "mint_only"

_CONFIG_YAML = """\
version: '1.0'
name: asymmetry
entity_types:
  Widget:
    properties:
      widget_id: {type: string, primary_key: true}
  Holder:
    properties:
      holder_id: {type: string, primary_key: true}
relationships:
  - name: held_by
    from: Widget
    to: Holder
"""


def _write_policy_literal_members(schema: type) -> set[str]:
    """Return the string members of a ``Literal[...] | None`` write_policy annotation."""
    annotation = schema.model_fields["write_policy"].annotation
    members: set[str] = set()
    candidates = get_args(annotation) if get_origin(annotation) is Union else (annotation,)
    for candidate in candidates:
        if get_origin(candidate) is Literal:
            members.update(arg for arg in get_args(candidate) if isinstance(arg, str))
    return members


@pytest.fixture
def seeded() -> tuple[CoreConfig, EntityGraph]:
    """A config plus a graph holding both endpoints of a ``held_by`` edge."""
    config = load_config_from_string(_CONFIG_YAML)
    graph = EntityGraph()
    graph.add_entity(EntityInstance(entity_type="Widget", entity_id="W-1", properties={}))
    graph.add_entity(EntityInstance(entity_type="Holder", entity_id="H-1", properties={}))
    return config, graph


def _apply_edge(config: CoreConfig, graph: EntityGraph) -> RelationshipInstance:
    return apply_relationship(
        graph,
        validate_relationship(
            config,
            graph,
            "Widget",
            "W-1",
            "held_by",
            "Holder",
            "H-1",
        ),
        "direct",
        "asymmetry-test",
        config=config,
    )


def test_apply_entity_really_refuses_a_mint_only_type(
    seeded: tuple[CoreConfig, EntityGraph],
) -> None:
    """The reference half, DRIVEN not read: where mint_only is admitted, it bites."""
    config, graph = seeded
    config.entity_types["Widget"].write_policy = _MINT_ONLY

    with pytest.raises(DirectWriteRefusedError):
        apply_entity(
            graph,
            validate_entity(config, graph, "Widget", "W-2", {"widget_id": "W-2"}),
            config=config,
            source="direct",
        )


def test_relationship_mint_only_stays_unreachable_or_apply_relationship_enforces_it(
    seeded: tuple[CoreConfig, EntityGraph],
) -> None:
    """Widening the relationship Literal without adding the branch fails HERE.

    Two ways to keep this green:

    1. leave ``RelationshipSchema.write_policy`` without ``mint_only`` (the
       status quo — the missing branch is unreachable), or
    2. add ``mint_only`` to the Literal AND add the matching refusal to
       ``apply_relationship``, mirroring ``apply_entity``.

    Doing only (2)'s first half is the trap this exists to catch.
    """
    config, graph = seeded
    if _MINT_ONLY not in _write_policy_literal_members(RelationshipSchema):
        pytest.skip("relationship write_policy does not admit mint_only; see the sibling test")

    schema = config.get_relationship("held_by")
    assert schema is not None
    schema.write_policy = _MINT_ONLY

    with pytest.raises(DirectWriteRefusedError):
        _apply_edge(config, graph)


def test_only_the_literal_is_stopping_a_mint_only_relationship_from_being_writable(
    seeded: tuple[CoreConfig, EntityGraph],
) -> None:
    """Demonstrate the hazard is REAL, not hypothetical — and name what guards it.

    ``model_construct`` bypasses the Literal exactly the way widening it would.
    The resolver then reports ``mint_only`` and the chokepoint writes the edge
    anyway, with no refusal: proof that nothing downstream enforces the policy,
    and that the type annotation is the entire defense.

    When the sibling test above stops skipping, this one must start failing —
    that is the intended coupling, and the assertion message says what to do.
    """
    from cruxible_core.service.direct_write_policy import effective_relationship_write_policy

    config, graph = seeded
    literal_members = _write_policy_literal_members(RelationshipSchema)
    assert literal_members == {"direct", "proposal_only"}, (
        "RelationshipSchema.write_policy members changed; re-derive whether "
        f"apply_relationship handles each of them: {sorted(literal_members)}"
    )

    original = config.get_relationship("held_by")
    assert original is not None
    fields: dict[str, Any] = {**original.__dict__, "write_policy": _MINT_ONLY}
    config.relationships = [RelationshipSchema.model_construct(**fields)]

    assert effective_relationship_write_policy(config, "held_by") == _MINT_ONLY

    applied = _apply_edge(config, graph)

    assert applied is not None, (
        "apply_relationship now refuses a mint_only edge. That is the FIX, not a "
        "regression: delete this test and unskip its sibling."
    )

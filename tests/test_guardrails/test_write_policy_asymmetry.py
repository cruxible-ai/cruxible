"""Guardrail: the entity/relationship ``write_policy`` asymmetry stays unreachable.

``apply_entity`` branches on ``mint_only`` and refuses every source except
``token_mint``. ``apply_relationship`` has NO such branch — it only handles
``proposal_only``. Today that asymmetry is harmless because
``RelationshipSchema.write_policy`` is a Literal that does not admit
``mint_only``, so the missing branch is unreachable.

The trap: widening that ONE Literal — a plausible, self-contained-looking config
change — would not add a refusal, it would make every ``mint_only`` relationship
type FREELY WRITABLE, because the chokepoint would fall through to the
``proposal_only`` check that does not match and then write. The declared
governance would read as the strictest policy in the vocabulary while enforcing
nothing.

These tests pin the two halves of that invariant so the widening cannot land
silently. Neither test changes behavior; they fail loudly at the moment the
asymmetry becomes reachable and tell the author to add the branch.
"""

from __future__ import annotations

import inspect
from typing import Literal, Union, get_args, get_origin

from cruxible_core.config.schema import EntityTypeSchema, RelationshipSchema
from cruxible_core.graph.operations import apply_entity, apply_relationship

_MINT_ONLY = "mint_only"


def _write_policy_literal_members(schema: type) -> set[str]:
    """Return the string members of a ``Literal[...] | None`` write_policy annotation."""
    annotation = schema.model_fields["write_policy"].annotation
    members: set[str] = set()
    candidates = get_args(annotation) if get_origin(annotation) is Union else (annotation,)
    for candidate in candidates:
        if get_origin(candidate) is Literal:
            members.update(arg for arg in get_args(candidate) if isinstance(arg, str))
    return members


def test_entity_write_policy_admits_mint_only_and_apply_entity_branches_on_it() -> None:
    """The reference half: where ``mint_only`` IS admitted, the chokepoint handles it."""
    assert _MINT_ONLY in _write_policy_literal_members(EntityTypeSchema)
    assert _MINT_ONLY in inspect.getsource(apply_entity), (
        "EntityTypeSchema.write_policy admits 'mint_only' but apply_entity no longer "
        "branches on it — mint_only entity types would become freely writable."
    )


def test_relationship_mint_only_stays_unreachable_or_apply_relationship_handles_it() -> None:
    """Widening the relationship Literal without adding the branch fails HERE.

    Two ways to keep this green:

    1. leave ``RelationshipSchema.write_policy`` without ``mint_only`` (the
       status quo — the missing branch is unreachable), or
    2. add ``mint_only`` to the Literal AND add the matching refusal to
       ``apply_relationship``, mirroring ``apply_entity``.

    Doing only (2)'s first half is the trap this exists to catch.
    """
    literal_members = _write_policy_literal_members(RelationshipSchema)
    chokepoint_source = inspect.getsource(apply_relationship)

    if _MINT_ONLY in literal_members:
        assert _MINT_ONLY in chokepoint_source, (
            "RelationshipSchema.write_policy now admits 'mint_only', but "
            "apply_relationship does not branch on it. As written the chokepoint "
            "falls through the proposal_only check and WRITES, so every mint_only "
            "relationship type is freely writable while declaring the strictest "
            "policy in the vocabulary. Add the refusal to apply_relationship the "
            "way apply_entity does before widening this Literal."
        )
    else:
        # Pin the status quo explicitly so the branch above is not vacuous
        # bookkeeping: this is the fact that makes the missing branch safe.
        assert literal_members == {"direct", "proposal_only"}, (
            "RelationshipSchema.write_policy members changed; re-derive whether "
            f"apply_relationship handles each of them: {sorted(literal_members)}"
        )

"""Every widened SDK vocabulary accepts its plain string at the boundary.

The builders take `Enum | str` so a caller can write `"one"` instead of
importing `Cardinality`. That widening is only safe if the string is
coerced where it arrives: an uncoerced string reaches `.value` deep inside the
builder and raises `AttributeError`, which names nothing the caller did wrong.

One test per widened parameter, because a parameter was once widened without
its coercion and nothing here caught it. (Procedure activation policy is now a
plain Literal string on the builders, so it is no longer an enum boundary.)
"""

from __future__ import annotations

import pytest

from cruxible_client.authoring.sdk import (
    Cardinality,
    ClaimObjectKind,
    ClaimRole,
    Disposition,
    ReferentSensitivity,
    _enum,
)

VOCABULARIES = [
    (Cardinality, "cardinality"),
    (ClaimObjectKind, "object kind"),
    (ClaimRole, "claim role"),
    (Disposition, "disposition"),
    (ReferentSensitivity, "referent sensitivity"),
]


@pytest.mark.parametrize(
    ("kind", "label"), VOCABULARIES, ids=lambda item: getattr(item, "__name__", item)
)
def test_every_widened_vocabulary_coerces_its_own_string_values(kind: type, label: str) -> None:
    """Each member's `.value` round-trips back to the member itself."""
    for member in kind:
        assert _enum(member.value, kind, label=label) is member
        assert _enum(member, kind, label=label) is member


@pytest.mark.parametrize(
    ("kind", "label"), VOCABULARIES, ids=lambda item: getattr(item, "__name__", item)
)
def test_an_unknown_string_is_refused_naming_the_admissible_values(kind: type, label: str) -> None:
    """The refusal has to say what would have worked, or it is a dead end."""
    with pytest.raises(ValueError) as raised:
        _enum("not-a-member", kind, label=label)

    message = str(raised.value)
    assert label in message
    for member in kind:
        assert member.value in message

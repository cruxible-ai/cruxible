"""Every widened SDK vocabulary accepts its plain string at the boundary.

The builders take `Enum | str` so a caller can write `"drain"` instead of
importing `ActivationPolicy`. That widening is only safe if the string is
coerced where it arrives: an uncoerced string reaches `.value` deep inside the
builder and raises `AttributeError`, which names nothing the caller did wrong.

One test per widened parameter, because `activation_policy` was widened without
its coercion and nothing here caught it.
"""

from __future__ import annotations

import pytest

from cruxible_client.authoring.sdk import (
    ActivationPolicy,
    Cardinality,
    ClaimObjectKind,
    ClaimRole,
    Disposition,
    ReferentSensitivity,
    _enum,
)

VOCABULARIES = [
    (ActivationPolicy, "activation policy"),
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


def test_the_procedure_builder_accepts_a_plain_activation_policy_string() -> None:
    """The regression: this parameter was widened without its coercion.

    A plain string used to reach `.value` inside the builder and raise
    AttributeError, so the widening was advertised and did not work.
    """
    assert _enum("drain", ActivationPolicy, label="procedure activation policy") is (
        ActivationPolicy.DRAIN
    )
    assert ActivationPolicy("drain") is ActivationPolicy.DRAIN

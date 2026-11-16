"""A derivation input outside the row's closure says how to bring it in."""

from __future__ import annotations

import pytest

from cruxible_core.procedures.claims_terminal import bind_claim_input
from cruxible_core.procedures.terminal_dependencies import AliasProvenanceV1, accepted_state_token


def test_an_unreferenced_input_names_the_template_reference_that_brings_it_in() -> None:
    read = {"claim_id": "CLM-" + "0" * 32, "artifact_digest": "sha256:" + "a" * 64}
    own = accepted_state_token("sha256:" + "a" * 64)
    other = accepted_state_token("sha256:" + "b" * 64)
    with pytest.raises(ValueError) as caught:
        bind_claim_input(
            read,
            outputs={"status": read},
            provenance={"status": AliasProvenanceV1(whole=frozenset({other}))},
            read_aliases=frozenset({"status"}),
            own_tokens=frozenset({own}),
            read_digests={},
        )
    message = str(caught.value)
    assert message.startswith("claims_inputs_invalid: the read status is not in this row's closure")
    assert "an input must be referenced by the Claim's value, key or rationale" in message
    assert "{$steps.status.value}" in message

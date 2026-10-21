"""The one stored-to-public name mapping: reserved families only, served as data."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CONTRACT_ID,
    COORDINATOR_SELF_SOURCE_IDENTITY,
    DIRECT_SELF_ASSERTED_CONTRACT_ID,
    foreign_source_contract_id,
)
from cruxible_client.contracts.public_names import (
    RESERVED_TAINT_LABELS,
    public_name,
    public_name_map,
    stored_contract_name,
)


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        (COORDINATOR_SELF_SOURCE_CONTRACT_ID, "cruxible.coordinator-self-source-v1"),
        (DIRECT_SELF_ASSERTED_CONTRACT_ID, "cruxible.direct-self-asserted-v1"),
        (COORDINATOR_SELF_SOURCE_IDENTITY, "cruxible.coordinator-authoring"),
        (
            "CaptureContract:playbill.coordinator-self-source-v1",
            "CaptureContract:cruxible.coordinator-self-source-v1",
        ),
        (foreign_source_contract_id("repo"), "cruxible.foreign-source.repo"),
        ("playbill.taint.accepted-state", "cruxible.taint.accepted-state"),
        ("playbill.external.proposer-asserted-v1", "cruxible.external.proposer-asserted-v1"),
    ],
)
def test_reserved_names_read_in_their_public_spelling(stored: str, shown: str) -> None:
    assert public_name(stored) == shown


@pytest.mark.parametrize(
    "name",
    [
        # An author may name a contract `playbill.x`; it is not core's to rename.
        "playbill.x",
        "CaptureContract:playbill.page",
        "playbill.foreign-source.",
        "docs.page-v1",
        "a sentence about playbill.coordinator-self-source-v1",
    ],
)
def test_other_names_read_as_themselves(name: str) -> None:
    assert public_name(name) == name


def test_contract_input_maps_public_spellings_back_to_the_stored_identity() -> None:
    for stored in (
        COORDINATOR_SELF_SOURCE_CONTRACT_ID,
        DIRECT_SELF_ASSERTED_CONTRACT_ID,
        foreign_source_contract_id("repo"),
    ):
        assert stored_contract_name(public_name(stored)) == stored
        assert stored_contract_name(stored) == stored
        qualified = f"CaptureContract:{stored}"
        assert stored_contract_name(public_name(qualified)) == qualified
    # Only contract names map back; a logical source or user name is untouched.
    assert stored_contract_name("cruxible.coordinator-authoring") == (
        "cruxible.coordinator-authoring"
    )
    assert stored_contract_name("cruxible.custom-v1") == "cruxible.custom-v1"
    assert stored_contract_name("cruxible.foreign-source.") == "cruxible.foreign-source."


def test_served_map_is_the_function_as_data() -> None:
    table = public_name_map()
    exact = {pair.stored: pair.public for pair in table.exact}
    assert [pair.stored for pair in table.exact] == sorted(exact)
    assert all(public_name(stored) == shown for stored, shown in exact.items())
    assert {(pair.stored, pair.public) for pair in table.prefixes} == {
        ("playbill.foreign-source.", "cruxible.foreign-source.")
    }
    assert all(stored.startswith("playbill.") for stored in exact)


def test_core_taint_labels_are_all_reserved() -> None:
    from cruxible_core.procedures import terminal_dependencies

    labels = {
        value
        for name, value in vars(terminal_dependencies).items()
        if name.startswith("TAINT_") and isinstance(value, str)
    }
    assert labels == set(RESERVED_TAINT_LABELS)

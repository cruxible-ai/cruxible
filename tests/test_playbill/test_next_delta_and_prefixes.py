"""The two ergonomics that must fail safe: queue deltas and short id prefixes.

Both trade completeness for convenience, so both are only acceptable if the
degenerate case is the SAFE one. A delta against a queue the server no longer
remembers returns the whole queue, never an empty one that reads as "nothing to
do". A prefix that names more than one artifact is refused with the candidates
listed, never resolved to whichever sorted first.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cruxible_core.playbill.id_prefixes import (
    MINIMUM_PREFIX_HEX,
    AmbiguousIdPrefix,
    resolve_id_prefix,
)

# A and B share their first eight hex characters, which is exactly the shortest
# prefix the resolver will consider; C shares nothing with either.
CLAIM_A = "CLM-" + "abababab" + "ab" * 12
CLAIM_B = "CLM-" + "abababab" + "cd" * 12
CLAIM_C = "CLM-" + "ff" * 16


def test_a_prefix_naming_exactly_one_artifact_resolves_to_its_full_id() -> None:
    resolved = resolve_id_prefix(
        "CLM-ffffffff",
        (CLAIM_A, CLAIM_B, CLAIM_C),
        marker="CLM-",
        label="Claim",
    )

    assert resolved == CLAIM_C


def test_an_ambiguous_prefix_is_refused_with_every_candidate_named() -> None:
    """Refusing blankly would make the caller guess; the candidates are the repair."""
    with pytest.raises(AmbiguousIdPrefix) as raised:
        resolve_id_prefix(
            "CLM-" + "abababab"[:MINIMUM_PREFIX_HEX],
            (CLAIM_A, CLAIM_B, CLAIM_C),
            marker="CLM-",
            label="Claim",
        )

    message = str(raised.value)
    assert AmbiguousIdPrefix.error_code in message
    assert CLAIM_A in message
    assert CLAIM_B in message
    assert CLAIM_C not in message


def test_a_full_id_passes_through_untouched_even_when_it_is_also_a_prefix() -> None:
    """A caller who pasted the whole id must not be re-interpreted."""
    assert resolve_id_prefix(CLAIM_A, (CLAIM_A, CLAIM_B), marker="CLM-", label="Claim") == CLAIM_A


def test_a_full_id_that_matches_nothing_is_returned_for_the_caller_to_refuse() -> None:
    """Resolution is not a lookup: not-found stays the caller's own refusal."""
    assert resolve_id_prefix(CLAIM_C, (CLAIM_A,), marker="CLM-", label="Claim") == CLAIM_C


@pytest.mark.parametrize(
    "value",
    [
        "CLM-abc",  # shorter than the minimum
        "CLM-abcdefgh",  # not hex
        "PRP-abcdefff",  # a different marker
        "abcdefff",  # no marker at all
    ],
)
def test_only_a_well_formed_short_prefix_resolves(value: str) -> None:
    assert resolve_id_prefix(value, (CLAIM_A, CLAIM_B), marker="CLM-", label="Claim") == value


def _result(item_ids: tuple[str, ...]):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.service.playbill_next import (
        PlaybillNextItemV1,
        PlaybillNextRepairV1,
        PlaybillNextResultV1,
        playbill_next_item_id,
    )

    items = []
    for item_id in item_ids:
        item = PlaybillNextItemV1.model_construct(
            item_id="",
            severity="warning",
            domain="accepted_state",
            reason="claim_conflicted",
            subject_identity=f"Claim:{item_id}",
            related_identities=(),
            detail={},
            repair=PlaybillNextRepairV1(
                operation="playbill.authoring.create",
                target=f"Claim:{item_id}",
                required_change="restate",
                arguments={},
            ),
        )
        items.append(item.model_copy(update={"item_id": playbill_next_item_id(item)}))
    return PlaybillNextResultV1.model_construct(
        coordinate=AcceptedCoordinate(
            git_oid="1" * 64,
            semantic_root="sha256:" + "2" * 64,
            generation_root="sha256:" + "3" * 64,
            compiler_digest="sha256:" + "4" * 64,
        ),
        evaluation_time=datetime(2026, 8, 20, tzinfo=UTC),
        observed_domains=("accepted_state", "workspace_floor", "workspace_sources"),
        unobserved_domains=(),
        items=tuple(items),
        result_digest="sha256:" + "9" * 64,
    )


def test_a_delta_against_an_unknown_digest_returns_the_whole_queue() -> None:
    """The server forgot; the safe answer is everything, not nothing."""
    from cruxible_core.service.playbill_next import _delta_of

    full = _result(("one", "two"))

    delta = _delta_of(full, since="sha256:" + "0" * 64)

    assert delta is full
    assert len(delta.items) == 2
    assert delta.delta_since is None


def test_a_delta_against_a_known_digest_carries_additions_and_removals() -> None:
    from cruxible_core.service.playbill_next import (
        _delta_of,
        _remember_queue,
        playbill_next_result_digest,
    )

    first = _result(("one", "removed"))
    _remember_queue(first.result_digest, first.items)
    second = _result(("one", "two"))

    delta = _delta_of(second, since=first.result_digest)

    assert {item.subject_identity for item in delta.items} == {"Claim:removed", "Claim:two"}
    assert delta.delta_since == first.result_digest
    assert delta.result_digest == playbill_next_result_digest(delta)

    unchanged = _delta_of(second, since=delta.result_digest)
    assert unchanged.items == ()
    assert unchanged.result_digest == playbill_next_result_digest(unchanged)


@pytest.mark.parametrize(
    ("operation", "arguments", "forbidden"),
    [
        ("playbill.claim.retire", {"claim_id": "CLM-" + "1" * 32}, "REQUEST_FILE"),
        ("playbill.authoring.create", {}, "PAYLOAD_FILE"),
        ("playbill.authoring.bind", {}, "PAYLOAD_FILE"),
        ("playbill.document.propose", {}, "ENVELOPE_FILE"),
    ],
)
def test_a_repair_command_never_carries_an_unfilled_file_placeholder(
    operation: str,
    arguments: dict[str, str],
    forbidden: str,
) -> None:
    """`command` is advertised as runnable, so it must not contain a fake path."""
    from cruxible_core.service.playbill_next import _repair_command

    command = _repair_command(operation, arguments=arguments)  # type: ignore[arg-type]

    assert forbidden not in command
    assert command.startswith("cruxible ")


def test_a_repair_command_fills_the_placeholder_when_the_row_names_the_file() -> None:
    from cruxible_core.service.playbill_next import _repair_command

    command = _repair_command(
        "playbill.authoring.bind",
        arguments={"payload_file": "/tmp/bind payload.json"},
    )

    assert command == "cruxible playbill authoring bind --payload-file '/tmp/bind payload.json'"


def test_dropping_a_placeholder_drops_the_flag_that_introduced_it() -> None:
    """A dangling `--payload-file` with no operand is worse than neither."""
    from cruxible_core.service.playbill_next import _repair_command

    assert _repair_command("playbill.authoring.bind", arguments={}) == (
        "cruxible playbill authoring bind"
    )

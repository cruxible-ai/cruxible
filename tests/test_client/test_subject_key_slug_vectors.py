"""Golden vectors for the ids core assigns from a canonical key (identity D1).

A keyed Subject's id is the readable rendering of its canonical key: each part
NFKC-normalized and lowercased, every run outside ``[a-z0-9.]`` turned into
``-`` and trimmed; composite parts joined with ``--``; an ``x`` prefix when the
rendering does not begin with a letter; ``x-<8 hex>`` when nothing readable is
left; cut and suffixed with 8 hex past the id length. These vectors pin the
rule byte for byte: a change to any of them moves Subject ids.
"""

from __future__ import annotations

import pytest

from cruxible_client.contracts.subject_kinds import (
    distinct_holder_id,
    is_distinct_holder_id,
    render_key_slug,
    subject_id_candidates,
)

VECTORS: tuple[tuple[list[object], str], ...] = (
    (["ship-it"], "ship-it"),
    (["Ship It!"], "ship-it"),
    (["ＦＵＬＬＷＩＤＴＨ"], "fullwidth"),
    (["café au lait"], "caf-au-lait"),
    (["2026 plan"], "x2026-plan"),
    (["---"], "x-ceaa6dd8"),
    (["日本語"], "x-60891743"),
    ([42], "x42"),
    ([True], "true"),
    (["a_b.c"], "a-b.c"),
    (["acme", "web app"], "acme--web-app"),
    (["Straße"], "stra-e"),
    (["  lead  trail  "], "lead-trail"),
    (["x" * 300], "x" * 247 + "-4bea4281"),
)


@pytest.mark.parametrize(("key", "rendered"), VECTORS, ids=[str(item[0])[:24] for item in VECTORS])
def test_a_canonical_key_renders_to_its_pinned_id(key: list[object], rendered: str) -> None:
    assert render_key_slug(key) == rendered  # type: ignore[arg-type]


def test_a_different_key_with_the_same_rendering_takes_the_key_digest_suffix() -> None:
    assert list(subject_id_candidates(["ship-it"]))[:3] == [
        "ship-it",
        "ship-it-2b94ec31",
        "ship-it-2b94ec31-2",
    ]


def test_a_further_hint_holder_takes_a_digest_of_its_operation_and_member() -> None:
    member = "SubjectCreation:dev.note@" + "1" * 32
    held = distinct_holder_id(["ship-it"], operation="AIT-" + "0" * 32, member=member)
    assert held == "ship-it-4c634a79"
    assert is_distinct_holder_id(held, ["ship-it"])
    assert not is_distinct_holder_id("ship-it", ["ship-it"])

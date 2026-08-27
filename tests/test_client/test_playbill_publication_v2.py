"""Client-side publication-v2 framing and whole-file CAS laws."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.authoring.insertions import (
    PlaybillInsertionApplyError,
    apply_playbill_publication,
    replace_publication_file,
)
from cruxible_core.playbill.authoring.insertions import (
    build_publication_preparation,
    mark_publication_prepared,
)
from tests.test_playbill.test_authoring_insertions_v2 import COORDINATE, _expectation, _observation


def _prepared():  # type: ignore[no-untyped-def]
    expectation = _expectation()
    preparation = build_publication_preparation(
        expectation,
        observation=_observation(b"status: \n"),
        body=b"ready\n",
        accepted_coordinate=COORDINATE,
        accepted_generation=7,
    )
    return mark_publication_prepared(expectation, preparation=preparation), preparation


def test_publication_apply_is_byte_deterministic_and_idempotent() -> None:
    expectation, preparation = _prepared()

    applied = apply_playbill_publication(
        b"status: \n",
        intent_id="AIT-" + "a" * 32,
        expectation=expectation.model_dump(mode="json"),
        retained_body=b"ready\n",
    )
    retry = apply_playbill_publication(
        applied.content,
        intent_id="AIT-" + "a" * 32,
        expectation=expectation.model_dump(mode="json"),
        retained_body=b"ready\n",
    )

    assert applied.outcome == "applied"
    assert retry.outcome == "already_applied"
    assert retry.content == applied.content
    assert retry.observation == applied.observation
    assert retry.observation["preparation_digest"] == preparation.preparation_digest


def test_publication_apply_refuses_wrong_body_and_duplicate_block() -> None:
    expectation, _preparation = _prepared()
    payload = expectation.model_dump(mode="json")

    with pytest.raises(PlaybillInsertionApplyError, match="retained accepted body"):
        apply_playbill_publication(
            b"status: \n",
            intent_id="AIT-" + "a" * 32,
            expectation=payload,
            retained_body=b"wrong\n",
        )
    applied = apply_playbill_publication(
        b"status: \n",
        intent_id="AIT-" + "a" * 32,
        expectation=payload,
        retained_body=b"ready\n",
    )
    with pytest.raises(PlaybillInsertionApplyError, match="neither prepared preimage"):
        apply_playbill_publication(
            applied.content + applied.content,
            intent_id="AIT-" + "a" * 32,
            expectation=payload,
            retained_body=b"ready\n",
        )


def test_publication_file_replace_refuses_a_concurrent_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "runbook.md"
    source.write_bytes(b"before\n")
    concurrent = b"concurrent author edit\n"
    original = Path.read_bytes

    def edit_before_compare(path: Path) -> bytes:
        if path == source:
            source.write_bytes(concurrent)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", edit_before_compare)
    with pytest.raises(PlaybillInsertionApplyError, match="compare-and-swap"):
        replace_publication_file(source, expected=b"before\n", replacement=b"after\n")

    monkeypatch.setattr(Path, "read_bytes", original)
    assert source.read_bytes() == concurrent

"""Local instances keep authoring intents only while in progress; managed ones keep all."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from cruxible_core.authoring.store import (
    AUTHORING_INTENTS_ENV,
    AuthoringIntentStore,
    AuthoringIntentStoreError,
)


def _intent(root: Path, name: str, *, state: str, age_seconds: float = 0) -> Path:
    events = root / "authoring-intents" / name / "events"
    events.mkdir(parents=True)
    last = events / "00000000000000000000.json"
    last.write_text(json.dumps({"intent": {"candidate_status": {"state": state}}}))
    stamp = time.time() - age_seconds
    os.utime(last, (stamp, stamp))
    return last.parent.parent


def test_retention_off_keeps_only_live_drafts(tmp_path, monkeypatch):
    monkeypatch.delenv(AUTHORING_INTENTS_ENV, raising=False)
    store = AuthoringIntentStore(tmp_path)
    for index, state in enumerate(("accepted", "superseded", "terminal")):
        _intent(tmp_path, f"AIT-done-{index}", state=state)
    draft = _intent(tmp_path, "AIT-draft", state="draft")
    stale = _intent(tmp_path, "AIT-stale", state="draft", age_seconds=2 * 24 * 60 * 60)
    with store._locked():
        store._prune_unretained()
    assert {path.name for path in store._intent_directories()} == {draft.name}
    assert not stale.exists()
    with pytest.raises(AuthoringIntentStoreError, match="only while it is in progress"):
        store._event_paths(tmp_path / "authoring-intents" / "AIT-done-0")


def test_durable_retention_keeps_every_intent(tmp_path, monkeypatch):
    monkeypatch.setenv(AUTHORING_INTENTS_ENV, "durable")
    store = AuthoringIntentStore(tmp_path)
    for index in range(20):
        _intent(tmp_path, f"AIT-done-{index:02d}", state="accepted")
    _intent(tmp_path, "AIT-stale", state="draft", age_seconds=2 * 24 * 60 * 60)
    with store._locked():
        store._prune_unretained()
    assert len(store._intent_directories()) == 21


def test_an_unknown_retention_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(AUTHORING_INTENTS_ENV, "ephemeral")
    with pytest.raises(AuthoringIntentStoreError, match="'off' or 'durable'"):
        AuthoringIntentStore(tmp_path)


def test_finished_intents_leave_only_a_bounded_receipt(tmp_path, monkeypatch):
    monkeypatch.delenv(AUTHORING_INTENTS_ENV, raising=False)
    store = AuthoringIntentStore(tmp_path)
    receipts = tmp_path / "authoring-intents" / ".finished"
    receipts.mkdir()
    for index in range(20):
        path = receipts / f"AIT-{index:032x}.json"
        path.write_text("{}")
        stamp = time.time() - 100 + index
        os.utime(path, (stamp, stamp))
    assert [path.stem for path in store._finished_receipts()][:1] == [f"AIT-{0:032x}"]
    # A forged receipt never answers as a finished intent.
    with pytest.raises(AuthoringIntentStoreError, match="receipt is malformed"):
        store._finished_receipt(f"AIT-{0:032x}")

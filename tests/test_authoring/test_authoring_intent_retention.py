"""Local instances keep authoring intents only while they matter; managed ones keep all."""

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


def test_ephemeral_retention_keeps_newest_finished_and_live_drafts(tmp_path, monkeypatch):
    monkeypatch.delenv(AUTHORING_INTENTS_ENV, raising=False)
    store = AuthoringIntentStore(tmp_path)
    finished = [
        _intent(tmp_path, f"AIT-done-{index:02d}", state="accepted", age_seconds=100 - index)
        for index in range(20)
    ]
    draft = _intent(tmp_path, "AIT-draft", state="draft")
    stale = _intent(tmp_path, "AIT-stale", state="draft", age_seconds=2 * 24 * 60 * 60)
    with store._locked():
        store._prune_ephemeral()
    kept = {path.name for path in store._intent_directories()}
    # The 16 most recently finished survive, oldest first to go.
    assert kept == {path.name for path in finished[4:]} | {draft.name}
    assert not stale.exists()


def test_durable_retention_keeps_every_intent(tmp_path, monkeypatch):
    monkeypatch.setenv(AUTHORING_INTENTS_ENV, "durable")
    store = AuthoringIntentStore(tmp_path)
    for index in range(20):
        _intent(tmp_path, f"AIT-done-{index:02d}", state="accepted")
    _intent(tmp_path, "AIT-stale", state="draft", age_seconds=2 * 24 * 60 * 60)
    with store._locked():
        store._prune_ephemeral()
    assert len(store._intent_directories()) == 21


def test_an_unknown_retention_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(AUTHORING_INTENTS_ENV, "forever")
    with pytest.raises(AuthoringIntentStoreError, match="ephemeral' or 'durable"):
        AuthoringIntentStore(tmp_path)

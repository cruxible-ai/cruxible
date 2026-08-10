"""The CLI REFUSES the inputs the 0.4.0 removals retired.

``feedback batch`` reads its items from a caller-authored file and builds each
``FeedbackBatchItemInput`` from named keys, so a retired key in that file was
read past without a word. The single-edge commands lost their options outright,
which Click already refuses; the batch file is the path that needed a check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_core.cli.main import cli

_ITEM = {
    "receipt_id": "RCP-1",
    "action": "accept",
    "target": {
        "from_type": "Part",
        "from_id": "BP-1",
        "relationship_type": "fits",
        "to_type": "Vehicle",
        "to_id": "V-1",
    },
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.mark.parametrize(
    ("key", "value", "guidance"),
    [
        ("source", "agent", "actor_context"),
        ("group_override", True, "no public replacement"),
    ],
)
def test_feedback_batch_refuses_a_retired_item_key(
    runner: CliRunner,
    tmp_path: Path,
    key: str,
    value: object,
    guidance: str,
) -> None:
    items_file = tmp_path / "items.json"
    items_file.write_text(json.dumps([dict(_ITEM), {**_ITEM, key: value}]))

    result = runner.invoke(cli, ["feedback", "batch", "--items-file", str(items_file)])

    assert result.exit_code != 0
    # Names the offending item, the key, and what to send instead.
    assert "items[1]" in result.output
    assert f"'{key}' was removed in 0.4.0" in result.output
    assert guidance in result.output


@pytest.mark.parametrize(
    ("command", "option"),
    [
        (["feedback", "record"], "--group-override"),
        (["feedback", "record"], "--source"),
        (["feedback", "from-query"], "--group-override"),
        (["outcome", "record"], "--source"),
    ],
)
def test_retired_cli_options_are_gone(runner: CliRunner, command: list[str], option: str) -> None:
    result = runner.invoke(cli, [*command, option, "x"])

    assert result.exit_code != 0
    assert "No such option" in result.output

"""``print(cx.next())`` and ``print(cx.get(ref))`` show what the CLI prints for the same read.

The CLI and the SDK share one renderer per answer
(``contracts.text_display.next_lines``, ``contracts.get_text.render_get``); ``str()``
makes no request. A repair command is rendered for its caller's surface by the
daemon (a CLI command for the CLI, an SDK call for the SDK), so those lines name
the same repair in each surface's own syntax; every other line is the same text.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import KnowledgeCard
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli
from tests.test_server.test_sdk_vocabulary_authoring import _bare_vocabulary, _cx


def _cli(http: TestClient, instance_id: str, monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    result = CliRunner().invoke(
        cli, ["--server-url", "http://cruxible", "--instance-id", instance_id, *args]
    )
    assert result.exit_code == 0, result.output
    return result.stdout


@pytest.fixture
def world(playbill_http: tuple[TestClient, str, Path], tmp_path: Path):  # type: ignore[no-untyped-def]
    http, instance_id, _ = playbill_http
    cx = _cx(http, instance_id, tmp_path)
    _bare_vocabulary(cx)
    # A field with no evidence rule: its Claims land uncovered, and next names them.
    claim = cx.set("dev.item/a", "dev.item.state", "open", because="It is open.").changes[0].claim
    cx.set("dev.item/a", "dev.item.state", "done", because="It is done now.")
    cx.refresh()
    return http, instance_id, cx, claim


_SURFACE_PREFIXES = ("  repair: ", "  or: ", "next: ")
_INSTANT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)")


def _same_text(cli_text: str, sdk_text: str, *, instants: bool = False) -> None:
    """Line for line the same, but for the calls each surface spells its own way.

    A repair or next-call line names the same step as a CLI command on the CLI
    and an SDK call on the SDK (the daemon renders it for its caller). With
    ``instants``, the read instant each request was evaluated at is masked: two
    requests are two instants, so a why answer stamps different ones.
    """

    cli_lines, sdk_lines = cli_text.rstrip("\n").splitlines(), sdk_text.splitlines()
    assert len(cli_lines) == len(sdk_lines), (cli_lines, sdk_lines)
    for cli_line, sdk_line in zip(cli_lines, sdk_lines, strict=True):
        prefix = next((item for item in _SURFACE_PREFIXES if cli_line.startswith(item)), None)
        if prefix is not None and cli_line != sdk_line:
            assert sdk_line.startswith(prefix), (cli_line, sdk_line)
            assert cli_line.removeprefix(prefix).startswith("cruxible "), cli_line
            assert sdk_line.removeprefix(prefix).startswith("cx."), sdk_line
            continue
        if instants:
            cli_line, sdk_line = _INSTANT.sub("<t>", cli_line), _INSTANT.sub("<t>", sdk_line)
        assert sdk_line == cli_line


def test_a_next_page_prints_what_cruxible_next_prints(
    world,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, instance_id, cx, _claim = world
    # Two reads, so both must see one state: the daemon's consumers follow
    # the last write in the background, and status says "consumers lagging"
    # until they catch up (CI caught one read on each side of that). Nothing
    # is written after this, so once caught up both reads see the same queue.
    deadline = time.monotonic() + 30
    page = cx.next()
    while page.status.consumers.state == "lagging" and time.monotonic() < deadline:
        time.sleep(0.05)
        page = cx.next()
    assert page.status.consumers.state != "lagging", page.status.consumers
    assert page.items, "the world needs a page with items"
    workspace = str(cx._workspace)

    cli_text = _cli(http, instance_id, monkeypatch, "next", "--workspace-root", workspace)

    _same_text(cli_text, str(page))
    assert repr(page).startswith("NextPage(")


@pytest.mark.parametrize(
    ("ref", "detail"),
    [
        ("dev.item/a", "summary"),
        ("dev.item/a", "history"),
        ("dev.item/a", "incoming"),
        ("dev.item/a", "backlinks"),
        ("dev.item/a", "why"),
        ("claim", "evidence"),
        ("claim", "history"),
        ("claim", "why"),
        ("dev.item.state", "summary"),
        ("dev.item.state", "history"),
        ("SubjectKind:dev.item", "summary"),
    ],
)
def test_a_get_card_prints_what_cruxible_get_prints(
    world,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    ref: str,
    detail: str,
) -> None:
    http, instance_id, cx, claim = world
    target = claim if ref == "claim" else ref
    card = cx.get(target, detail=detail)  # type: ignore[arg-type]
    assert isinstance(card, KnowledgeCard)

    cli_text = _cli(http, instance_id, monkeypatch, "get", target, "--detail", detail)

    _same_text(cli_text, str(card), instants=detail == "why")
    assert repr(card).startswith("KnowledgeCard(")


def test_str_makes_no_request(world) -> None:  # type: ignore[no-untyped-def]
    _http, _instance_id, cx, claim = world
    page, card = cx.next(), cx.get(claim, detail="evidence")
    calls: list[str] = []
    for name in ("get", "get_batch", "next", "orient", "query", "whoami"):
        setattr(cx._client, name, lambda *args, _name=name, **kwargs: calls.append(_name))
    assert str(page) and str(card)
    assert calls == []


def test_a_claim_summary_prints_the_view_the_sdk_read(world) -> None:  # type: ignore[no-untyped-def]
    """The SDK reads a Claim summary from its proof (a ClaimView), not the CLI's card."""

    _http, _instance_id, cx, claim = world
    text = str(cx.get(claim))
    first, second = text.splitlines()
    assert first == "dev.item/a  dev.item.state = done"
    assert second.startswith(f"{claim.removeprefix('Claim:')}  verdict=")

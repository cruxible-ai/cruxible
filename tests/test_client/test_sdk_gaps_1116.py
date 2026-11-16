"""Ruling 10: the agent-facing SDK reaches reject, withdraw, Runbooks, whoami and kits.

Each is a thin wrapper over one ``CruxibleClient`` call: the stub records the
call it was asked for and returns a sentinel, so the wrapper's argument
mapping is what is checked. The refusals stay the daemon's (Part 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client import AccessProfile
from cruxible_client.authoring.sdk import Cruxible, Runbook
from cruxible_client.contracts.repair_spelling import next_repair_command, render_repair
from cruxible_client.contracts.repairs import RepairOperation


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> str:
            self.calls.append((name, args, kwargs))
            return f"<{name}>"

        return call


def _cx(tmp_path: Path) -> tuple[Cruxible, _Recorder]:
    client = _Recorder()
    cx = Cruxible(
        client=client,  # type: ignore[arg-type]
        instance_id="inst",
        workspace=tmp_path,
        access_profile=AccessProfile("test", (), False),
        clock=lambda: None,
    )
    return cx, client


def test_a_proposal_handle_rejects_and_withdraws_with_a_reason(tmp_path: Path) -> None:
    cx, client = _cx(tmp_path)
    proposal = cx.proposal("sha256:" + "a" * 64)

    assert proposal.reject("not this change") == "<reject_proposal>"
    assert proposal.withdraw("changed my mind", dry_run=True) == "<withdraw_proposal>"
    assert client.calls == [
        (
            "reject_proposal",
            ("inst", "sha256:" + "a" * 64),
            {"reason": "not this change", "dry_run": None, "at": None},
        ),
        (
            "withdraw_proposal",
            ("inst", "sha256:" + "a" * 64),
            {"reason": "changed my mind", "dry_run": True, "at": None},
        ),
    ]


def test_runbooks_whoami_and_kits_are_one_client_call_each(tmp_path: Path) -> None:
    cx, client = _cx(tmp_path)
    runbook = cx.runbook("Runbook:maintain")

    assert isinstance(runbook, Runbook) and runbook.ref == "Runbook:maintain"
    assert (runbook.preview(), runbook.run()) == ("<preview_runbook>", "<run_runbook>")
    assert cx.whoami() == "<whoami>"
    assert cx.kits() == "<kit_status>"
    assert client.calls == [
        ("preview_runbook", ("inst", "maintain"), {}),
        ("run_runbook", ("inst", "maintain"), {}),
        ("whoami", ("inst",), {}),
        ("kit_status", ("inst",), {}),
    ]


def test_a_runbook_repair_is_spelled_as_the_sdk_handle() -> None:
    repair = RepairOperation(operation="cruxible.runbook.run", arguments={"runbook": "maintain"})

    assert render_repair(repair, surface="sdk") == 'cx.runbook("maintain").run()'
    assert (
        next_repair_command(repair.operation, arguments=repair.arguments, surface="sdk")
        == 'cx.runbook("maintain").run()'
    )

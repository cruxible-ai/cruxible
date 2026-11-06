"""W5: a settled, accepted proposal's card names the generation it produced.

The card reads ``generation`` from the proposal index's maintained settlement
record (``accepted_sequence``), so the read costs the same keyed lookups
however many other proposals and generations the instance holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.documents import DocumentLifecycle, DocumentShell
from cruxible_client.contracts.get_reads import GetProposalCard, GetRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_document,
    service_store_playbill_body,
)
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import seed_write_surface
from tests.test_scale._counters import counting, trace_sqlite

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


@pytest.fixture(autouse=True)
def traced_sqlite(tmp_path: Path):  # type: ignore[no-untyped-def]
    with trace_sqlite(tmp_path):
        yield


def _propose(instance: PlaybillInstance, name: str, minute: int) -> str:
    body = service_store_playbill_body(instance, content=f"# {name}\n".encode()).digest
    proposal = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity=f"document:{name}",
            document_kind="guidance",
            title=name,
            media_type="text/markdown",
            body_digest=body,
            links=(),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name=f"doc-{name}",
        timestamp=f"2026-09-29T12:{minute:02d}:00.000000Z",
    )
    assert proposal.proposal.candidate is not None, proposal
    return proposal.proposal.admission.proposal_id


def _accept(instance: PlaybillInstance, proposal_id: str) -> int:
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt
    return int(instance.accepted_history()[-1].sequence)


def _card(instance: PlaybillInstance, proposal_id: str) -> GetProposalCard:
    card = service_playbill_get(instance, request=GetRequest(ref=proposal_id), access=_ACCESS).card
    assert isinstance(card, GetProposalCard)
    return card


def _world(root: Path, others: int) -> tuple[PlaybillInstance, str, int]:
    root.mkdir()
    instance, _ = seed_write_surface(root)
    for index in range(others):
        _accept(instance, _propose(instance, f"other-{index}", index % 50))
        _propose(instance, f"open-{index}", index % 50)
    target = _propose(instance, "target", 59)
    generation = _accept(instance, target)
    _accept(instance, _propose(instance, "after", 58))
    return instance, target, generation


def test_an_accepted_proposal_names_its_generation_and_an_open_one_none(
    tmp_path: Path,
) -> None:
    instance, target, generation = _world(tmp_path / "w", 1)
    card = _card(instance, target)
    assert card.status == "settled" and card.reason == "accepted"
    assert card.generation == generation
    assert generation < int(instance.accepted_history()[-1].sequence)
    pending = _propose(instance, "pending", 30)
    assert _card(instance, pending).generation is None
    assert "generation" not in _card(instance, pending).model_dump(exclude_none=True)


def test_the_card_generation_read_is_flat_in_unrelated_proposals(tmp_path: Path) -> None:
    counts: dict[int, Any] = {}
    for size in (2, 12):
        instance, target, generation = _world(tmp_path / str(size), size)
        assert _card(instance, target).generation == generation  # warm
        with counting(tmp_path / str(size)) as window:
            assert _card(instance, target).generation == generation
        assert not window.unexplained, window.unexplained
        counts[size] = window.counts
    small, large = counts[2], counts[12]
    print("W5 proposal card work", {size: dict(c) for size, c in counts.items()})
    assert large["sql_statements"] == small["sql_statements"], (small, large)
    assert large["sql_vm_steps"] <= small["sql_vm_steps"] * 1.05 + 50, (small, large)

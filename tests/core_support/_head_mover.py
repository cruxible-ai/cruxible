"""Accept one ordinary Document change, so an instance's accepted head moves.

Interleaving tests (R12) inject this between a change's entry check and its
write: a commit pinned to the earlier coordinate must then refuse.
"""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_propose_playbill_document
from tests.core_support._knowledge_loop_support import accept_proposal


def move_head(instance: PlaybillInstance, owner: Any, name: str) -> str:
    """Accept a fresh Document named ``name``; return the new head's git oid."""

    body = instance.store_document_body(f"{name}\n".encode())
    shell = DocumentShell(
        identity=f"document:{name}",
        document_kind="design",
        title=name,
        media_type="text/plain",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    inspection = service_propose_playbill_document(
        instance,
        shell=shell,
        actor_id="owner",
        proposal_name=name,
        timestamp="2026-08-26T18:30:00.000000Z",
    )
    accept_proposal(instance, owner, inspection)
    return instance.accepted_coordinate().git_oid


__all__ = ["move_head"]

"""Inbox terminal delivery: a run's ``post_inbox`` items, posted as they are.

The sink attaches to each item the Captures its own dependency closure reached
(the same rule a proposal item's evidence follows: an item cannot cite evidence
the run did not see), and posts it to the Line inbox under the Line that ran.
Posting is inert: nothing here reads or writes accepted state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_core.procedures.egress import (
    TerminalEgressChildReceiptV1,
    TerminalEgressError,
    TerminalEgressReceiptV1,
    TerminalEgressRequestV1,
)
from cruxible_core.procedures.terminal_dependencies import (
    TerminalItemDependencyManifestV1,
    citable_captures,
)

if TYPE_CHECKING:
    from cruxible_core.exhaust.line_inbox import LineInboxStore


class InboxTerminalEgressSink:
    """Rung-1 egress: post each item to the Line inbox with its own evidence attached."""

    def __init__(self, *, store: LineInboxStore, line: ArtifactIdentity) -> None:
        self.store = store
        self.line = line

    def deliver_inbox_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
        manifests: Mapping[str, TerminalItemDependencyManifestV1],
    ) -> TerminalEgressReceiptV1:
        from cruxible_core.exhaust.line_inbox import InboxPosting

        if request.kind != "post_inbox":
            raise TerminalEgressError(
                f"the inbox sink serves post_inbox only, not {request.kind!r}"
            )
        postings = [
            InboxPosting(
                line=self.line.qualified,
                procedure=request.procedure_identity.qualified,
                procedure_artifact_digest=request.procedure_artifact_digest,
                run_id=request.run_id,
                node_id=request.node_id,
                item_key=item.item_key,
                payload=cast(CanonicalValue, item.value),
                source_capture_digests=citable_captures(manifests.get(item.item_key)),
                posted_at=request.prepared_at,
            )
            for item in request.items
        ]
        keys = self.store.post(
            postings,
            actor=request.actor_context,
            now=request.prepared_at,
            coordinate=request.accepted_coordinate,
        )
        return TerminalEgressReceiptV1(
            kind="post_inbox",
            run_id=request.run_id,
            node_id=request.node_id,
            disposition="posted",
            children=tuple(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index, item_key=item.item_key, egress_digest=key
                )
                for item, key in zip(request.items, keys, strict=True)
            ),
        )


__all__ = ["InboxTerminalEgressSink"]

"""Inbox terminal delivery: a run's ``post_inbox`` items, posted as they are.

The sink attaches to each item the Captures its own dependency closure reached
(the same rule a proposal item's evidence follows: an item cannot cite evidence
the run did not see), and posts it to the Line inbox under the Line that ran.
Posting is inert: nothing here reads or writes accepted state.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, cast

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.claim_evidence import ClaimsDiagnostic
from cruxible_client.contracts.procedures.line_specs import LineFindings
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
    from cruxible_core.procedures.execution import ProcedureRunAdmission


class InboxTerminalEgressSink:
    """Rung-1 egress: post each item to the Line inbox with its own evidence attached."""

    def __init__(
        self,
        *,
        store: LineInboxStore,
        line: ArtifactIdentity,
        findings: LineFindings = LineFindings(),
    ) -> None:
        self.store = store
        self.line = line
        self.finding_kinds = findings.inbox

    def route_findings(
        self,
        *,
        admission: ProcedureRunAdmission,
        node_id: str,
        diagnostics: tuple[ClaimsDiagnostic, ...],
        at: datetime,
    ) -> None:
        """Post typed extraction refusals once per finding, after terminal delivery."""
        from cruxible_core.exhaust.line_inbox import InboxPosting

        self.store.post(
            [
                InboxPosting(
                    line=self.line.qualified,
                    procedure=admission.procedure_identity.qualified,
                    procedure_artifact_digest=admission.procedure_artifact_digest,
                    run_id=admission.run_id,
                    node_id=node_id,
                    item_key=f"diagnostic-{index}",
                    payload=d.model_dump(mode="json"),
                    source_capture_digests=(d.capture,)
                    if d.kind == "claim_not_in_source" and d.capture is not None
                    else (),
                    dedupe_payload={"kind": d.kind, "claim": d.claim}
                    if d.kind == "claim_not_in_source" and d.claim is not None
                    else None,
                    dedupe_node_id="claim_not_in_source"
                    if d.kind == "claim_not_in_source" and d.claim is not None
                    else None,
                    posted_at=at,
                    only_if_new=True,
                )
                for index, d in enumerate(diagnostics)
            ],
            actor=admission.actor_context,
            now=at,
            coordinate=admission.accepted_coordinate,
        )

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
                # A declared dedupe_key makes those fields the item's identity.
                dedupe_payload=(
                    {
                        name: item.value[name]
                        for name in request.inbox_dedupe_key
                        if isinstance(item.value, dict) and name in item.value
                    }
                    if request.inbox_dedupe_key
                    else None
                ),
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

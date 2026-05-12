"""Service policy for local governed mutation receipts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

import structlog

from cruxible_core.errors import CoreError, MutationError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.types import OperationType, Receipt

logger = structlog.get_logger()
ResultT = TypeVar("ResultT", bound="SupportsReceiptId")


class SupportsReceiptId(Protocol):
    """Result objects that can be annotated with a mutation receipt."""

    receipt_id: str | None


class Closeable(Protocol):
    """Minimal closeable resource used by mutation services."""

    def close(self) -> None: ...


@dataclass
class MutationReceiptContext(Generic[ResultT]):
    """Mutable state shared between a mutation call site and receipt wrapper."""

    builder: ReceiptBuilder | None
    result: ResultT | None = None

    def set_result(self, result: ResultT) -> None:
        self.result = result


def _persist_receipt(instance: InstanceProtocol, receipt: Receipt) -> bool:
    """Best-effort receipt persistence. Returns True if saved."""
    store = instance.get_receipt_store()
    try:
        store.save_receipt(receipt)
        return True
    except Exception:
        logger.warning("Failed to persist receipt %s", receipt.receipt_id, exc_info=True)
        return False
    finally:
        store.close()


def save_graph_for_mutation(instance: InstanceProtocol, graph: EntityGraph) -> None:
    """Save graph, wrapping non-CoreError failures so mutation_receipt_id flows."""
    try:
        instance.save_graph(graph)
    except CoreError:
        raise
    except Exception as exc:
        raise MutationError(f"Failed to save graph: {exc}") from exc


@contextmanager
def mutation_receipt(
    instance: InstanceProtocol,
    operation_type: OperationType,
    parameters: dict[str, Any],
    *,
    store: Closeable | None = None,
    enabled: bool = True,
) -> Iterator[MutationReceiptContext[ResultT]]:
    """Wrap local governed mutation execution with receipt persistence and tagging."""
    builder = (
        ReceiptBuilder(operation_type=operation_type, parameters=parameters)
        if enabled
        else None
    )
    ctx: MutationReceiptContext[ResultT] = MutationReceiptContext(builder=builder)
    exc_to_tag: CoreError | None = None
    try:
        yield ctx
    except CoreError as exc:
        exc_to_tag = exc
        raise
    except Exception as exc:
        wrapped = MutationError(f"Unexpected failure: {exc}")
        exc_to_tag = wrapped
        raise wrapped from exc
    else:
        if builder is not None and ctx.result is not None:
            builder.mark_committed()
    finally:
        if store is not None:
            store.close()
        if builder is not None:
            receipt = builder.build()
            if _persist_receipt(instance, receipt):
                if exc_to_tag is not None:
                    exc_to_tag.mutation_receipt_id = receipt.receipt_id
                elif ctx.result is not None:
                    ctx.result.receipt_id = receipt.receipt_id

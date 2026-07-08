"""Service policy for local governed mutation receipts."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol

import structlog

from cruxible_core.errors import CoreError, MutationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.mutation_payloads import MutationPayloadRetention
from cruxible_core.receipt.types import OperationType, Receipt

logger = structlog.get_logger()


class SupportsReceiptId(Protocol):
    """Result objects that can be annotated with a mutation receipt."""

    receipt_id: str | None


class Closeable(Protocol):
    """Minimal closeable resource used by mutation services."""

    def close(self) -> None: ...


@dataclass
class MutationReceiptContext:
    """Mutable state shared between a mutation call site and receipt wrapper."""

    builder: ReceiptBuilder | None
    uow: Any | None = None
    result: SupportsReceiptId | None = None

    def set_result(self, result: SupportsReceiptId) -> None:
        self.result = result


def _resolve_mutation_payload_retention(
    instance: InstanceProtocol,
) -> MutationPayloadRetention:
    """Read the configured mutation-payload retention mode, defaulting safely."""
    try:
        return instance.load_config().runtime.mutation_payloads
    except Exception:
        logger.warning("Failed to read mutation_payloads retention; using metadata", exc_info=True)
        return "metadata"


def _build_retained_receipt(
    builder: ReceiptBuilder,
    retention: MutationPayloadRetention,
) -> Receipt:
    """Stamp payload retention on the mutation node, then build the receipt."""
    builder.apply_mutation_payload_retention(retention=retention)
    return builder.build()


def _persist_receipt(instance: InstanceProtocol, receipt: Receipt) -> bool:
    """Best-effort receipt persistence. Returns True if saved."""
    try:
        with instance.write_transaction() as uow:
            uow.receipts.save_receipt(receipt)
        return True
    except Exception:
        logger.warning("Failed to persist receipt %s", receipt.receipt_id, exc_info=True)
        return False


def _close_transaction(
    manager: Any | None,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    traceback: TracebackType | None,
) -> None:
    if manager is not None:
        manager.__exit__(exc_type, exc, traceback)


def save_graph_for_mutation(
    instance: InstanceProtocol,
    graph: EntityGraph,
    *,
    entities: Sequence[EntityInstance] | None = None,
    relationships: Sequence[RelationshipInstance] | None = None,
    uow: Any | None = None,
) -> None:
    """Persist graph changes, wrapping non-CoreError failures for receipt tagging."""
    try:
        manager = instance.write_transaction() if uow is None else nullcontext(uow)
        with manager as target_uow:
            if entities is not None or relationships is not None:
                target_uow.graph.upsert_entities(entities or ())
                target_uow.graph.upsert_relationships(relationships or ())
            else:
                target_uow.graph.save_graph(graph)
    except CoreError:
        raise
    except Exception as exc:
        raise MutationError(f"Failed to save graph: {exc}") from exc
    finally:
        instance.invalidate_graph_cache()


@contextmanager
def mutation_receipt(
    instance: InstanceProtocol,
    operation_type: OperationType,
    parameters: dict[str, Any],
    *,
    store: Closeable | None = None,
    enabled: bool = True,
    actor_context: GovernedActorContext | None = None,
) -> Iterator[MutationReceiptContext]:
    """Wrap local governed mutation execution with receipt persistence and tagging.

    ``actor_context`` is the runtime actor identity for the operation: credential-
    derived when auth is on, and the declared local operator when auth is off.
    Older/local direct service calls may still leave it null.
    """
    builder = (
        ReceiptBuilder(
            operation_type=operation_type,
            parameters=parameters,
            actor_context=actor_context,
        )
        if enabled
        else None
    )
    retention = _resolve_mutation_payload_retention(instance) if builder is not None else "metadata"
    ctx = MutationReceiptContext(builder=builder)
    exc_to_tag: CoreError | None = None
    tx_manager: Any | None = None
    uow: Any | None = None
    tx_closed = False
    try:
        tx_manager = instance.write_transaction()
        uow = tx_manager.__enter__()
        ctx.uow = uow
        yield ctx
    except CoreError as exc:
        exc_to_tag = exc
        _close_transaction(tx_manager, type(exc), exc, exc.__traceback__)
        tx_closed = True
        instance.invalidate_graph_cache()
        if builder is not None:
            receipt = _build_retained_receipt(builder, retention)
            if _persist_receipt(instance, receipt):
                exc_to_tag.mutation_receipt_id = receipt.receipt_id
        raise
    except Exception as exc:
        wrapped = MutationError(f"Unexpected failure: {exc}")
        exc_to_tag = wrapped
        _close_transaction(tx_manager, type(wrapped), wrapped, wrapped.__traceback__)
        tx_closed = True
        instance.invalidate_graph_cache()
        if builder is not None:
            receipt = _build_retained_receipt(builder, retention)
            if _persist_receipt(instance, receipt):
                exc_to_tag.mutation_receipt_id = receipt.receipt_id
        raise wrapped from exc
    else:
        if builder is not None and ctx.result is not None:
            builder.mark_committed()
            receipt = _build_retained_receipt(builder, retention)
            try:
                assert uow is not None
                uow.receipts.save_receipt(receipt)
                ctx.result.receipt_id = receipt.receipt_id
            except Exception as exc:
                wrapped = MutationError(f"Failed to persist mutation receipt: {exc}")
                _close_transaction(tx_manager, type(wrapped), wrapped, wrapped.__traceback__)
                tx_closed = True
                instance.invalidate_graph_cache()
                raise wrapped from exc
            else:
                _close_transaction(tx_manager, None, None, None)
                tx_closed = True
    finally:
        if not tx_closed:
            _close_transaction(tx_manager, None, None, None)
        if store is not None:
            store.close()

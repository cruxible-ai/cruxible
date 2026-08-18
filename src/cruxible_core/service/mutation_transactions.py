"""Transaction wrapper retained by unserved graph-mutation donor code."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol

from cruxible_core.errors import CoreError, MutationError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import InstanceProtocol


class SupportsReceiptId(Protocol):
    """Legacy donor result carrying its now-always-empty receipt coordinate."""

    receipt_id: str | None


class Closeable(Protocol):
    """Minimal closeable resource used by mutation donors."""

    def close(self) -> None: ...


@dataclass
class MutationTransactionContext:
    """State shared between one donor mutation and its transaction wrapper."""

    # Retained temporarily so the donor call sites can shed their receipt-tree
    # branches incrementally. It is structurally always None: PC-E1 has no
    # legacy mutation receipt path.
    builder: None = None
    uow: Any | None = None
    result: SupportsReceiptId | None = None

    def set_result(self, result: SupportsReceiptId) -> None:
        self.result = result


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
    """Persist donor graph changes and normalize unexpected storage failures."""
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
def mutation_transaction(
    instance: InstanceProtocol,
    operation_type: str,
    parameters: dict[str, Any],
    *,
    store: Closeable | None = None,
    enabled: bool = True,
    actor_context: Any | None = None,
) -> Iterator[MutationTransactionContext]:
    """Keep donor graph writes atomic without recreating receipt authority.

    PC-E1 moved operational evidence to authenticated Procedure journals and
    promoted exhaust. The legacy graph-mutation services are no longer served,
    but later query transplants still use some of them as fixtures. This wrapper
    preserves only their transaction boundary and deliberately emits no durable
    receipt, refusal tag, or hidden replacement store.
    """
    del operation_type, parameters, enabled, actor_context
    ctx = MutationTransactionContext()
    tx_manager: Any | None = None
    tx_closed = False
    try:
        tx_manager = instance.write_transaction()
        ctx.uow = tx_manager.__enter__()
        yield ctx
    except CoreError as exc:
        _close_transaction(tx_manager, type(exc), exc, exc.__traceback__)
        tx_closed = True
        instance.invalidate_graph_cache()
        raise
    except Exception as exc:
        wrapped = MutationError(f"Unexpected failure: {exc}")
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

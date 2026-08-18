"""Focused tests for the post-PC-E1 donor transaction wrapper."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from cruxible_core.errors import ConfigError, MutationError
from cruxible_core.service.mutation_transactions import mutation_transaction


@dataclass
class _DummyResult:
    receipt_id: str | None = None


class _DummyCloseable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _DummyUnitOfWork:
    pass


class _DummyInstance:
    def __init__(self) -> None:
        self.uow = _DummyUnitOfWork()
        self.invalidations = 0

    @contextmanager
    def write_transaction(self) -> Iterator[_DummyUnitOfWork]:
        yield self.uow

    def invalidate_graph_cache(self) -> None:
        self.invalidations += 1


def test_success_keeps_transaction_without_emitting_receipt() -> None:
    instance = _DummyInstance()

    with mutation_transaction(instance, "add_entity", {"count": 1}) as ctx:
        assert ctx.builder is None
        ctx.set_result(_DummyResult())

    assert ctx.result is not None
    assert ctx.result.receipt_id is None


def test_core_error_is_not_tagged_with_retired_receipt() -> None:
    instance = _DummyInstance()

    with pytest.raises(ConfigError) as exc_info:
        with mutation_transaction(instance, "add_entity", {"count": 1}):
            raise ConfigError("boom")

    assert exc_info.value.mutation_receipt_id is None
    assert instance.invalidations == 1


def test_unexpected_exception_is_still_wrapped() -> None:
    instance = _DummyInstance()

    with pytest.raises(MutationError, match="Unexpected failure: boom") as exc_info:
        with mutation_transaction(instance, "add_entity", {"count": 1}):
            raise RuntimeError("boom")

    assert exc_info.value.mutation_receipt_id is None
    assert instance.invalidations == 1


def test_external_donor_resource_is_closed() -> None:
    instance = _DummyInstance()
    external_store = _DummyCloseable()

    with mutation_transaction(
        instance,
        "add_entity",
        {"count": 1},
        store=external_store,
    ):
        pass

    assert external_store.closed is True

"""Focused tests for service mutation receipt policy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from cruxible_core.errors import ConfigError, MutationError
from cruxible_core.service.mutation_receipts import mutation_receipt


@dataclass
class _DummyResult:
    receipt_id: str | None = None


class _DummyReceiptStore:
    def __init__(self) -> None:
        self.saved_receipts = []
        self.closed = False

    def save_receipt(self, receipt):
        self.saved_receipts.append(receipt)
        return receipt.receipt_id

    def close(self) -> None:
        self.closed = True


class _DummyCloseable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _DummyUnitOfWork:
    def __init__(self, receipt_store: _DummyReceiptStore) -> None:
        self.receipts = receipt_store


class _DummyInstance:
    def __init__(self) -> None:
        self.receipt_store = _DummyReceiptStore()
        self.uow = _DummyUnitOfWork(self.receipt_store)

    def get_receipt_store(self) -> _DummyReceiptStore:
        return self.receipt_store

    @contextmanager
    def write_transaction(self) -> Iterator[_DummyUnitOfWork]:
        yield self.uow

    def invalidate_graph_cache(self) -> None:
        pass

    def verify_serving_config_receipted(self) -> None:
        # The write-path verification chokepoint no-ops for instances without
        # a config source pointer, which is what this dummy stands in for.
        pass


class TestMutationReceiptHelper:
    def test_success_attaches_receipt_id(self) -> None:
        instance = _DummyInstance()

        with mutation_receipt(instance, "add_entity", {"count": 1}) as ctx:
            assert ctx.builder is not None
            ctx.builder.record_validation(passed=True, detail={"ok": True})
            ctx.set_result(_DummyResult())

        assert ctx.result is not None
        assert ctx.result.receipt_id is not None
        assert len(instance.receipt_store.saved_receipts) == 1
        assert instance.receipt_store.saved_receipts[0].committed is True

    def test_core_error_re_raises_and_tags_receipt(self) -> None:
        instance = _DummyInstance()

        with pytest.raises(ConfigError) as exc_info:
            with mutation_receipt(instance, "add_entity", {"count": 1}) as ctx:
                assert ctx.builder is not None
                ctx.builder.record_validation(passed=False, detail={"ok": False})
                raise ConfigError("boom")

        assert exc_info.value.mutation_receipt_id is not None
        assert len(instance.receipt_store.saved_receipts) == 1
        assert instance.receipt_store.saved_receipts[0].committed is False

    def test_unexpected_exception_wraps_and_tags_receipt(self) -> None:
        instance = _DummyInstance()

        with pytest.raises(MutationError) as exc_info:
            with mutation_receipt(instance, "add_entity", {"count": 1}) as ctx:
                assert ctx.builder is not None
                ctx.builder.record_validation(passed=False, detail={"ok": False})
                raise RuntimeError("boom")

        assert "Unexpected failure: boom" in str(exc_info.value)
        assert exc_info.value.mutation_receipt_id is not None
        assert len(instance.receipt_store.saved_receipts) == 1
        assert instance.receipt_store.saved_receipts[0].committed is False

    def test_enabled_false_skips_receipt_persistence(self) -> None:
        instance = _DummyInstance()
        external_store = _DummyCloseable()

        with mutation_receipt(
            instance,
            "add_entity",
            {"count": 1},
            store=external_store,
            enabled=False,
        ) as ctx:
            assert ctx.builder is None
            ctx.set_result(_DummyResult())

        assert ctx.result is not None
        assert ctx.result.receipt_id is None
        assert external_store.closed is True
        assert instance.receipt_store.saved_receipts == []

    def test_store_is_closed_on_error(self) -> None:
        instance = _DummyInstance()
        external_store = _DummyCloseable()

        with pytest.raises(ConfigError):
            with mutation_receipt(
                instance,
                "add_entity",
                {"count": 1},
                store=external_store,
            ) as ctx:
                assert ctx.builder is not None
                raise ConfigError("boom")

        assert external_store.closed is True

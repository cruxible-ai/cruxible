"""Reserved contract-subject dispatch stays total and read resolution centralized."""

from __future__ import annotations

import inspect

import pytest

from cruxible_core.resolution_contracts.subjects import (
    RESERVED_SUBJECT_KINDS,
    RESERVED_SUBJECT_OPENERS,
    RESERVED_SUBJECT_RESOLVERS,
)
from cruxible_core.service.resolution_contracts import (
    _build_list_items,
    _subject_is_live,
)


def test_reserved_kind_opener_and_resolver_dispatch_are_total() -> None:
    registry_keys = set(RESERVED_SUBJECT_KINDS)
    opener_keys = {entry.opener_dispatch_key for entry in RESERVED_SUBJECT_KINDS.values()}
    resolver_keys = {
        entry.subject_resolver_dispatch_key for entry in RESERVED_SUBJECT_KINDS.values()
    }
    assert registry_keys == opener_keys == resolver_keys
    assert registry_keys == set(RESERVED_SUBJECT_OPENERS)
    assert registry_keys == set(RESERVED_SUBJECT_RESOLVERS)
    assert RESERVED_SUBJECT_KINDS["Procedure"].status == "live"


def test_reserved_kind_registry_is_immutable() -> None:
    with pytest.raises(TypeError):
        RESERVED_SUBJECT_KINDS["Other"] = RESERVED_SUBJECT_KINDS["Procedure"]  # type: ignore[index]


def test_contract_list_and_queue_liveness_use_the_shared_subject_resolver() -> None:
    assert "resolve_contract_subject(" in inspect.getsource(_build_list_items)
    assert "resolve_contract_subject(" in inspect.getsource(_subject_is_live)

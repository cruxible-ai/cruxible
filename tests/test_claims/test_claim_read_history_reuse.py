"""Single-Claim reads reuse accepted history without caching the read result."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.service.claims import claims as playbill_claims
from cruxible_core.service.claims.claims import (
    _claim_law_evidence_index,
    service_get_playbill_claim,
    service_list_playbill_claims,
)
from cruxible_core.service.evidence import evidence as playbill_evidence
from tests.core_support._knowledge_loop_support import seed_claims


def test_repeated_claim_reads_select_only_their_evidence_and_rebuild_admission_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    identities = tuple(
        str(claim.envelope["identity"]) for claim in service_list_playbill_claims(instance).claims
    )
    parsed = 0
    accounts_built = 0
    original_parse = playbill_claims.parse_claim_law_evidence
    original_accounts = playbill_claims._claim_admission_accounts

    def parse(raw: object) -> Any:
        nonlocal parsed
        parsed += 1
        return original_parse(raw)

    def accounts(*args: Any, **kwargs: Any) -> Any:
        nonlocal accounts_built
        accounts_built += 1
        return original_accounts(*args, **kwargs)

    def full_index(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("one-Claim reads must not copy the whole law-evidence index")

    monkeypatch.setattr(playbill_claims, "parse_claim_law_evidence", parse)
    monkeypatch.setattr(playbill_evidence, "parse_claim_law_evidence", parse)
    monkeypatch.setattr(playbill_claims, "_claim_admission_accounts", accounts)
    monkeypatch.setattr(playbill_claims, "_claim_law_evidence_index", full_index)
    monkeypatch.setattr(instance, "accepted_history", full_index)
    monkeypatch.setattr(instance, "tree_at", full_index)
    monkeypatch.setattr(instance, "paths_at", full_index)
    instant = datetime(2026, 8, 21, 14, tzinfo=UTC)
    for index in range(20):
        identity = identities[index % len(identities)]
        evaluated_at = instant + timedelta(seconds=index)
        view = service_get_playbill_claim(instance, identity=identity, evaluation_time=evaluated_at)
        assert view.envelope["identity"] == identity
        assert view.admission_evaluation_time == evaluated_at
        assert view.admission_accounts

    assert parsed == 20  # Each read verifies only its selected retained account.
    assert accounts_built == 20


def test_claim_history_mapping_is_owned_and_bounded_to_its_coordinate(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    current = instance.accepted_coordinate()
    previous = instance.coordinate_for_oid(instance.accepted_history()[-2].oid)
    current_index = _claim_law_evidence_index(instance, at=current)
    previous_index = _claim_law_evidence_index(instance, at=previous)
    assert len(previous_index) == 1
    assert len(current_index) == 2
    assert previous_index.items() <= current_index.items()

    current_index.clear()
    previous_index.clear()
    assert len(_claim_law_evidence_index(instance, at=current)) == 2
    assert len(_claim_law_evidence_index(instance, at=previous)) == 1


def test_short_claim_id_resolves_from_typed_owner_rows(tmp_path: Path, monkeypatch) -> None:
    instance, _owner = seed_claims(tmp_path)
    identity = str(service_list_playbill_claims(instance).claims[0].envelope["identity"])
    prefix = identity.removeprefix("Claim:")[:12]

    def forbidden(*args, **kwargs):
        pytest.fail("Claim prefix selection must not enumerate accepted Git files")

    monkeypatch.setattr(instance, "tree_at", forbidden)
    monkeypatch.setattr(instance, "paths_at", forbidden)
    assert service_get_playbill_claim(instance, identity=prefix).envelope["identity"] == identity


def test_claim_reads_rebuild_history_after_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    identity = str(service_list_playbill_claims(instance).claims[0].envelope["identity"])

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("live reads must not rebuild a whole-history Claim index")

    monkeypatch.setattr(playbill_evidence, "_build_claim_read_history_index", forbidden)
    before = service_get_playbill_claim(instance, identity=identity)
    assert service_get_playbill_claim(instance, identity=identity) == before
    instance.refresh()
    assert not hasattr(instance, "claim_read_history_memo")
    assert service_get_playbill_claim(instance, identity=identity) == before

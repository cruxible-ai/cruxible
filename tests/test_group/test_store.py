"""Tests for GroupStore SQLite persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.store import GroupStore
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    CandidateSignal,
    QuerySourceEvidence,
    SignalBucketBasis,
)
from cruxible_core.temporal import format_datetime, utc_now


def _now() -> datetime:
    return utc_now()


def _create_legacy_group_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE group_resolutions (
            resolution_id TEXT PRIMARY KEY,
            relationship_type TEXT NOT NULL,
            group_signature TEXT NOT NULL,
            action TEXT NOT NULL,
            rationale TEXT DEFAULT '',
            thesis_text TEXT NOT NULL DEFAULT '',
            thesis_facts TEXT NOT NULL DEFAULT '{}',
            analysis_state TEXT NOT NULL DEFAULT '{}',
            trust_status TEXT NOT NULL DEFAULT 'watch',
            trust_reason TEXT NOT NULL DEFAULT '',
            confirmed INTEGER NOT NULL DEFAULT 0,
            resolved_by TEXT NOT NULL,
            resolved_at TEXT NOT NULL
        );
        CREATE TABLE candidate_groups (
            group_id TEXT PRIMARY KEY,
            relationship_type TEXT NOT NULL,
            signature TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_review',
            thesis_text TEXT NOT NULL DEFAULT '',
            thesis_facts TEXT NOT NULL DEFAULT '{}',
            analysis_state TEXT NOT NULL DEFAULT '{}',
            signal_sources_used TEXT NOT NULL DEFAULT '[]',
            proposed_by TEXT NOT NULL,
            member_count INTEGER NOT NULL DEFAULT 0,
            review_priority TEXT NOT NULL DEFAULT 'normal',
            suggested_priority TEXT,
            resolution_id TEXT REFERENCES group_resolutions(resolution_id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE candidate_members (
            group_id TEXT NOT NULL REFERENCES candidate_groups(group_id),
            from_type TEXT NOT NULL,
            from_id TEXT NOT NULL,
            to_type TEXT NOT NULL,
            to_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            signals TEXT NOT NULL DEFAULT '[]',
            properties TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (group_id, from_type, from_id, to_type, to_id, relationship_type)
        );
        """
    )
    conn.commit()
    conn.close()


def _group(
    group_id: str = "GRP-test000001",
    relationship_type: str = "fits",
    signature: str = "abc123",
    status: str = "pending_review",
    thesis_text: str = "test thesis",
    thesis_facts: dict | None = None,
    analysis_state: dict | None = None,
    review_priority: str = "normal",
    suggested_priority: str | None = None,
    source_workflow_name: str | None = None,
    source_workflow_receipt_id: str | None = None,
    source_trace_ids: list[str] | None = None,
    source_step_ids: list[str] | None = None,
    resolution_id: str | None = None,
) -> CandidateGroup:
    return CandidateGroup(
        group_id=group_id,
        relationship_type=relationship_type,
        signature=signature,
        status=status,
        thesis_text=thesis_text,
        thesis_facts=thesis_facts or {"style": "casual"},
        analysis_state=analysis_state or {"centroid": [0.1, 0.2]},
        signal_sources_used=["cosine_v1"],
        proposed_by="agent",
        member_count=2,
        review_priority=review_priority,
        suggested_priority=suggested_priority,
        source_workflow_name=source_workflow_name,
        source_workflow_receipt_id=source_workflow_receipt_id,
        source_trace_ids=source_trace_ids or [],
        source_step_ids=source_step_ids or [],
        resolution_id=resolution_id,
        created_at=_now(),
    )


def _member(
    from_id: str = "shoe-1",
    to_id: str = "outfit-1",
    signals: list[CandidateSignal] | None = None,
) -> CandidateMember:
    return CandidateMember(
        from_type="Shoe",
        from_id=from_id,
        to_type="Outfit",
        to_id=to_id,
        relationship_type="fits",
        signals=signals
        or [CandidateSignal(signal_source="cosine_v1", signal="support", evidence="high sim")],
        properties={"raw_score": 0.95},
    )


@pytest.fixture
def store() -> GroupStore:
    s = GroupStore(":memory:")
    yield s
    s.close()


class TestTableCreation:
    def test_tables_exist(self, store: GroupStore) -> None:
        tables = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in tables}
        assert "candidate_groups" in names
        assert "candidate_members" in names
        assert "group_resolutions" in names

    def test_foreign_keys_enabled(self, store: GroupStore) -> None:
        """Orphan member insert should fail due to FK constraint."""
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO candidate_members "
                "(group_id, from_type, from_id, to_type, to_id, relationship_type) "
                "VALUES ('NONEXISTENT', 'A', '1', 'B', '2', 'r')"
            )


class TestGroupRoundTrip:
    def test_save_get(self, store: GroupStore) -> None:
        g = _group()
        with store.transaction():
            store.save_group(g)
        loaded = store.get_group(g.group_id)
        assert loaded is not None
        assert loaded.group_id == g.group_id
        assert loaded.relationship_type == "fits"
        assert loaded.signature == "abc123"
        assert loaded.status == "pending_review"

    def test_thesis_fields(self, store: GroupStore) -> None:
        g = _group(thesis_facts={"color": "warm"}, analysis_state={"embed": [1, 2, 3]})
        with store.transaction():
            store.save_group(g)
        loaded = store.get_group(g.group_id)
        assert loaded is not None
        assert loaded.thesis_text == "test thesis"
        assert loaded.thesis_facts == {"color": "warm"}
        assert loaded.analysis_state == {"embed": [1, 2, 3]}

    def test_priority_fields(self, store: GroupStore) -> None:
        g = _group(review_priority="critical", suggested_priority="high")
        with store.transaction():
            store.save_group(g)
        loaded = store.get_group(g.group_id)
        assert loaded is not None
        assert loaded.review_priority == "critical"
        assert loaded.suggested_priority == "high"

    def test_workflow_lineage_fields(self, store: GroupStore) -> None:
        g = _group(
            source_workflow_name="recommend",
            source_workflow_receipt_id="RCP-1",
            source_trace_ids=["TRC-1", "TRC-2"],
            source_step_ids=["recommend"],
        )
        with store.transaction():
            store.save_group(g)
        loaded = store.get_group(g.group_id)
        assert loaded is not None
        assert loaded.source_workflow_name == "recommend"
        assert loaded.source_workflow_receipt_id == "RCP-1"
        assert loaded.source_trace_ids == ["TRC-1", "TRC-2"]
        assert loaded.source_step_ids == ["recommend"]

    def test_resolution_id_stored(self, store: GroupStore) -> None:
        # First create a resolution (so FK resolves)
        with store.transaction():
            res_id = store.save_resolution("fits", "abc123", "approve", "", "", {}, {}, "human")
        g = _group(resolution_id=res_id)
        with store.transaction():
            store.save_group(g)
        loaded = store.get_group(g.group_id)
        assert loaded is not None
        assert loaded.resolution_id == res_id

    def test_resolution_id_fk_enforced(self, store: GroupStore) -> None:
        g = _group(resolution_id="NONEXISTENT")
        with pytest.raises(sqlite3.IntegrityError):
            with store.transaction():
                store.save_group(g)

    def test_get_not_found(self, store: GroupStore) -> None:
        assert store.get_group("NONEXISTENT") is None

    def test_list_groups(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_group(_group("GRP-1", status="pending_review"))
            store.save_group(_group("GRP-2", status="resolved"))
        all_groups = store.list_groups()
        assert len(all_groups) == 2
        pending = store.list_groups(status="pending_review")
        assert len(pending) == 1

    def test_list_by_relationship_type(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_group(_group("GRP-1", relationship_type="fits"))
            store.save_group(_group("GRP-2", relationship_type="replaces"))
        fits = store.list_groups(relationship_type="fits")
        assert len(fits) == 1

    def test_count_groups(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_group(_group("GRP-1"))
            store.save_group(_group("GRP-2", signature="sigv1:def456"))
        assert store.count_groups() == 2
        assert store.count_groups(status="pending_review") == 2
        assert store.count_groups(status="resolved") == 0


class TestStatusUpdate:
    def test_update_status(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_group(_group("GRP-1"))
            updated = store.update_group_status("GRP-1", "applying")
        loaded = store.get_group("GRP-1")
        assert loaded is not None
        assert updated is True
        assert loaded.status == "applying"

    def test_update_status_with_resolution_id(self, store: GroupStore) -> None:
        with store.transaction():
            res_id = store.save_resolution("fits", "abc123", "approve", "", "", {}, {}, "human")
            store.save_group(_group("GRP-1"))
            updated = store.update_group_status("GRP-1", "applying", resolution_id=res_id)
        loaded = store.get_group("GRP-1")
        assert loaded is not None
        assert updated is True
        assert loaded.resolution_id == res_id

    def test_update_status_returns_false_for_missing_group(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_group(_group("GRP-1"))
            store.update_group_status("GRP-1", "applying")
            updated = store.update_group_status("GRP-missing", "resolved")

        assert updated is False


class TestMembers:
    def test_save_get_members(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_group(_group("GRP-1"))
            store.save_members("GRP-1", [_member("s1", "o1"), _member("s2", "o2")])
        members = store.get_members("GRP-1")
        assert len(members) == 2
        assert members[0].signals[0].signal_source == "cosine_v1"
        assert members[0].properties["raw_score"] == 0.95

    def test_save_get_members_preserves_signal_basis(self, store: GroupStore) -> None:
        signal = CandidateSignal(
            signal_source="cosine_v1",
            signal="support",
            evidence="high sim",
            basis=SignalBucketBasis(
                mode="score",
                path="score",
                value=0.95,
                matched="support_gte",
            ),
        )
        with store.transaction():
            store.save_group(_group("GRP-1"))
            store.save_members("GRP-1", [_member("s1", "o1", signals=[signal])])

        members = store.get_members("GRP-1")

        assert members[0].signals[0].basis is not None
        assert members[0].signals[0].basis.model_dump(mode="json") == {
            "mode": "score",
            "path": "score",
            "value": 0.95,
            "matched": "support_gte",
        }

    def test_save_get_members_preserves_query_source_evidence(self, store: GroupStore) -> None:
        member = _member("s1", "o1")
        member.source_query_evidence = [
            QuerySourceEvidence(
                query_receipt_id="RCP-query000001",
                row_index=0,
                source_step="candidate_query",
                row_shape="relationship",
                relationship={
                    "relationship_type": "fits",
                    "from_id": "s1",
                    "to_id": "o1",
                    "properties": {"score": 0.95},
                },
            )
        ]
        with store.transaction():
            store.save_group(_group("GRP-1"))
            store.save_members("GRP-1", [member])

        loaded = store.get_members("GRP-1")

        evidence = loaded[0].source_query_evidence[0]
        assert isinstance(evidence, QuerySourceEvidence)
        assert evidence.query_receipt_id == "RCP-query000001"
        assert evidence.row_index == 0
        assert evidence.feedback_addressable is True
        assert evidence.row_shape == "relationship"
        assert evidence.relationship == {
            "relationship_type": "fits",
            "from_id": "s1",
            "to_id": "o1",
            "properties": {"score": 0.95},
        }


class TestQuerySourceEvidence:
    def test_rejects_negative_row_index(self) -> None:
        with pytest.raises(ValidationError, match="row_index must be non-negative"):
            QuerySourceEvidence(query_receipt_id="RCP-query000001", row_index=-1)

    def test_missing_row_index_is_not_feedback_addressable(self) -> None:
        evidence = QuerySourceEvidence(
            query_receipt_id="RCP-query000001",
            row_shape="unknown",
        )

        assert evidence.row_index is None
        assert evidence.feedback_addressable is False
        assert evidence.model_dump(mode="json") == {
            "query_receipt_id": "RCP-query000001",
            "feedback_addressable": False,
            "row_shape": "unknown",
        }

    def test_accepts_flexible_nested_payloads(self) -> None:
        evidence = QuerySourceEvidence(
            query_receipt_id="RCP-query000001",
            row_index=2,
            row_shape="path",
            entry={"entity_type": "Campaign", "entity_id": "CMP-1"},
            result={"entity_type": "Product", "entity_id": "SKU-123"},
            path=[
                {
                    "alias": "candidate",
                    "relationship_type": "candidate_product",
                    "properties": {"scores": [1, 2, {"k": "v"}]},
                }
            ],
        )

        assert evidence.feedback_addressable is True
        assert evidence.model_dump(mode="json") == {
            "query_receipt_id": "RCP-query000001",
            "row_index": 2,
            "row_shape": "path",
            "path": [
                {
                    "alias": "candidate",
                    "relationship_type": "candidate_product",
                    "properties": {"scores": [1, 2, {"k": "v"}]},
                }
            ],
            "entry": {"entity_type": "Campaign", "entity_id": "CMP-1"},
            "result": {"entity_type": "Product", "entity_id": "SKU-123"},
        }


class TestResolutions:
    def test_save_find(self, store: GroupStore) -> None:
        with store.transaction():
            res_id = store.save_resolution(
                "fits", "sig1", "approve", "looks good", "thesis", {"k": "v"}, {"state": 1}, "human"
            )
        res = store.find_resolution("fits", "sig1")
        assert res is not None
        assert res.resolution_id == res_id
        assert res.action == "approve"
        assert res.thesis_facts == {"k": "v"}
        assert res.analysis_state == {"state": 1}

    def test_find_with_action_filter(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_resolution("fits", "sig1", "reject", "", "", {}, {}, "human")
            store.save_resolution("fits", "sig1", "approve", "", "", {}, {}, "human")
        # Filter approve only
        res = store.find_resolution("fits", "sig1", action="approve")
        assert res is not None
        assert res.action == "approve"
        # Filter reject only
        res = store.find_resolution("fits", "sig1", action="reject")
        assert res is not None
        assert res.action == "reject"

    def test_find_with_confirmed_filter(self, store: GroupStore) -> None:
        with store.transaction():
            res_id = store.save_resolution(
                "fits", "sig1", "approve", "", "", {}, {}, "human", confirmed=False
            )
        # Unconfirmed — not found when filtering confirmed=True
        assert store.find_resolution("fits", "sig1", confirmed=True) is None
        # Found when filtering confirmed=False
        assert store.find_resolution("fits", "sig1", confirmed=False) is not None
        # Confirm it
        with store.transaction():
            store.confirm_resolution(res_id)
        assert store.find_resolution("fits", "sig1", confirmed=True) is not None

    def test_find_with_both_filters(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_resolution(
                "fits", "sig1", "approve", "", "", {}, {}, "human", confirmed=False
            )
            store.save_resolution("fits", "sig1", "reject", "", "", {}, {}, "human", confirmed=True)
        # Only confirmed approvals
        assert store.find_resolution("fits", "sig1", action="approve", confirmed=True) is None
        # Confirmed rejects exist
        assert store.find_resolution("fits", "sig1", action="reject", confirmed=True) is not None

    def test_confirmed_defaults_false(self, store: GroupStore) -> None:
        with store.transaction():
            res_id = store.save_resolution("fits", "sig1", "approve", "", "", {}, {}, "human")
        res = store.get_resolution(res_id)
        assert res is not None
        assert res.confirmed is False

    def test_confirm_resolution_sets_confirmed(self, store: GroupStore) -> None:
        with store.transaction():
            res_id = store.save_resolution("fits", "sig1", "approve", "", "", {}, {}, "human")
            store.confirm_resolution(res_id)
        res = store.get_resolution(res_id)
        assert res is not None
        assert res.confirmed is True

    def test_confirm_with_trust_override(self, store: GroupStore) -> None:
        with store.transaction():
            res_id = store.save_resolution(
                "fits", "sig1", "approve", "", "", {}, {}, "human", trust_status="trusted"
            )
            store.confirm_resolution(res_id, trust_status="watch")
        res = store.get_resolution(res_id)
        assert res is not None
        assert res.confirmed is True
        assert res.trust_status == "watch"

    def test_get_resolution_by_id(self, store: GroupStore) -> None:
        with store.transaction():
            res_id = store.save_resolution(
                "fits", "sig1", "approve", "good", "thesis", {"a": 1}, {"b": 2}, "human"
            )
        res = store.get_resolution(res_id)
        assert res is not None
        assert res.resolution_id == res_id
        assert res.confirmed is False

    def test_multiple_resolutions_same_signature(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_resolution("fits", "sig1", "approve", "", "", {}, {}, "human")
            store.save_resolution("fits", "sig1", "reject", "", "", {}, {}, "human")
        resolutions = store.list_resolutions(relationship_type="fits")
        assert len(resolutions) == 2

    def test_list_resolutions_returns_fields(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_resolution(
                "fits",
                "sig1",
                "approve",
                "ok",
                "thesis",
                {"k": 1},
                {"state": "x"},
                "human",
                trust_status="trusted",
            )
            store.update_resolution_trust_status(
                store.find_resolution("fits", "sig1").resolution_id,
                "trusted",
                "earned by review",
            )
        resolutions = store.list_resolutions()
        assert len(resolutions) == 1
        r = resolutions[0]
        assert r.analysis_state == {"state": "x"}
        assert r.thesis_facts == {"k": 1}
        assert r.trust_status == "trusted"
        assert r.trust_reason == "earned by review"

    def test_update_trust_status(self, store: GroupStore) -> None:
        with store.transaction():
            res_id = store.save_resolution("fits", "sig1", "approve", "", "", {}, {}, "human")
            store.update_resolution_trust_status(res_id, "trusted", "promoted")
        res = store.get_resolution(res_id)
        assert res is not None
        assert res.trust_status == "trusted"
        assert res.trust_reason == "promoted"


class TestTransaction:
    def test_commit_on_success(self, store: GroupStore) -> None:
        with store.transaction():
            store.save_group(_group("GRP-1"))
        assert store.get_group("GRP-1") is not None

    def test_rollback_on_failure(self, store: GroupStore) -> None:
        with pytest.raises(ValueError):
            with store.transaction():
                store.save_group(_group("GRP-1"))
                raise ValueError("boom")
        assert store.get_group("GRP-1") is None

    def test_no_auto_commit(self, store: GroupStore) -> None:
        """Write methods do NOT auto-commit."""
        store.save_group(_group("GRP-1"))
        # Rollback should undo the save
        store._conn.rollback()
        assert store.get_group("GRP-1") is None


class TestClose:
    def test_close_idempotent(self) -> None:
        store = GroupStore(":memory:")
        store.close()
        store.close()  # should not raise


class TestCoexistence:
    def test_shares_db_with_feedback_store(self, tmp_path) -> None:
        """GroupStore and FeedbackStore can coexist in the same DB file."""
        db = tmp_path / "feedback.db"
        fs = FeedbackStore(db)
        gs = GroupStore(db)
        with gs.transaction():
            gs.save_group(_group("GRP-1"))
        assert gs.get_group("GRP-1") is not None
        # FeedbackStore tables still work
        assert fs.count_feedback() == 0
        gs.close()
        fs.close()


class TestMigration:
    def test_backfill_resolved_signatures_matches_compute_function(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy-feedback.db"
        _create_legacy_group_db(db_path)
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        created_at = format_datetime(_now())
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO group_resolutions "
            "(resolution_id, relationship_type, group_signature, action, rationale, thesis_text, "
            "thesis_facts, analysis_state, trust_status, trust_reason, confirmed, resolved_by, "
            "resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "RES-1",
                "fits",
                "legacy-signature",
                "approve",
                "ok",
                "fit rule",
                json.dumps(facts),
                "{}",
                "trusted",
                "",
                1,
                "human",
                created_at,
            ),
        )
        conn.execute(
            "INSERT INTO candidate_groups "
            "(group_id, relationship_type, signature, status, thesis_text, thesis_facts, "
            "analysis_state, signal_sources_used, proposed_by, member_count, review_priority, "
            "suggested_priority, resolution_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "GRP-1",
                "fits",
                "legacy-signature",
                "resolved",
                "fit rule",
                json.dumps(facts),
                "{}",
                "[]",
                "agent",
                1,
                "review",
                None,
                "RES-1",
                created_at,
            ),
        )
        conn.commit()
        conn.close()

        store = GroupStore(db_path)
        try:
            expected = compute_group_signature("fits", facts)
            group = store.get_group("GRP-1")
            resolution = store.get_resolution("RES-1")
        finally:
            store.close()

        assert group is not None
        assert resolution is not None
        assert group.signature == expected
        assert resolution.group_signature == expected

    def test_migration_drops_pending_and_preserves_empty_thesis_resolved(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "legacy-feedback.db"
        _create_legacy_group_db(db_path)
        created_at = format_datetime(_now())
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO candidate_groups "
            "(group_id, relationship_type, signature, status, thesis_text, thesis_facts, "
            "analysis_state, signal_sources_used, proposed_by, member_count, review_priority, "
            "suggested_priority, resolution_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "GRP-pending",
                "fits",
                "legacy-pending",
                "pending_review",
                "pending",
                json.dumps({"rule_id": "fit_rule"}),
                "{}",
                "[]",
                "agent",
                1,
                "review",
                None,
                None,
                created_at,
            ),
        )
        conn.execute(
            "INSERT INTO candidate_members "
            "(group_id, from_type, from_id, to_type, to_id, relationship_type, "
            "signals, properties) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("GRP-pending", "Part", "BP-1", "Vehicle", "V-1", "fits", "[]", "{}"),
        )
        conn.execute(
            "INSERT INTO group_resolutions "
            "(resolution_id, relationship_type, group_signature, action, rationale, thesis_text, "
            "thesis_facts, analysis_state, trust_status, trust_reason, confirmed, resolved_by, "
            "resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "RES-empty",
                "fits",
                "legacy-empty",
                "approve",
                "",
                "",
                "{}",
                "{}",
                "watch",
                "",
                1,
                "human",
                created_at,
            ),
        )
        conn.execute(
            "INSERT INTO candidate_groups "
            "(group_id, relationship_type, signature, status, thesis_text, thesis_facts, "
            "analysis_state, signal_sources_used, proposed_by, member_count, review_priority, "
            "suggested_priority, resolution_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "GRP-resolved",
                "fits",
                "legacy-empty",
                "resolved",
                "",
                "{}",
                "{}",
                "[]",
                "agent",
                0,
                "review",
                None,
                "RES-empty",
                created_at,
            ),
        )
        conn.commit()
        conn.close()

        store = GroupStore(db_path)
        try:
            pending = store.get_group("GRP-pending")
            resolved = store.get_group("GRP-resolved")
            resolution = store.get_resolution("RES-empty")
        finally:
            store.close()

        assert pending is None
        assert resolved is not None
        assert resolution is not None
        assert resolved.signature == "legacy-empty"
        assert resolution.group_signature == "legacy-empty"

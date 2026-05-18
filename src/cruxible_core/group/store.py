"""SQLite persistence for candidate groups, members, and resolutions.

Shares feedback.db with FeedbackStore. Tables are created on init.
Write methods do NOT auto-commit — use transaction() for compound writes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    CandidateSignal,
    GroupResolution,
)
from cruxible_core.instance_protocol import GroupStoreProtocol
from cruxible_core.primitives import new_id
from cruxible_core.temporal import format_datetime, utc_now

logger = logging.getLogger(__name__)

# group_resolutions FIRST (referenced by candidate_groups.resolution_id)
_SCHEMA = """\
CREATE TABLE IF NOT EXISTS group_resolutions (
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
CREATE INDEX IF NOT EXISTS idx_group_resolutions_match
    ON group_resolutions(relationship_type, group_signature);
CREATE INDEX IF NOT EXISTS idx_group_resolutions_signature_action_confirmed
    ON group_resolutions(relationship_type, group_signature, action, confirmed);

CREATE TABLE IF NOT EXISTS candidate_groups (
    group_id TEXT PRIMARY KEY,
    relationship_type TEXT NOT NULL,
    signature TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    group_kind TEXT NOT NULL DEFAULT 'propose',
    thesis_text TEXT NOT NULL DEFAULT '',
    thesis_facts TEXT NOT NULL DEFAULT '{}',
    analysis_state TEXT NOT NULL DEFAULT '{}',
    signal_sources_used TEXT NOT NULL DEFAULT '[]',
    proposed_by TEXT NOT NULL,
    member_count INTEGER NOT NULL DEFAULT 0,
    pending_version INTEGER NOT NULL DEFAULT 1,
    review_priority TEXT NOT NULL DEFAULT 'normal',
    suggested_priority TEXT,
    source_workflow_name TEXT,
    source_workflow_receipt_id TEXT,
    source_trace_ids TEXT NOT NULL DEFAULT '[]',
    source_step_ids TEXT NOT NULL DEFAULT '[]',
    resolution_id TEXT REFERENCES group_resolutions(resolution_id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_groups_signature ON candidate_groups(signature);
CREATE INDEX IF NOT EXISTS idx_candidate_groups_status ON candidate_groups(status);
CREATE INDEX IF NOT EXISTS idx_candidate_groups_rel_type ON candidate_groups(relationship_type);
CREATE INDEX IF NOT EXISTS idx_candidate_groups_signature_status
    ON candidate_groups(relationship_type, signature, status);

CREATE TABLE IF NOT EXISTS candidate_members (
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
CREATE INDEX IF NOT EXISTS idx_candidate_members_group_identity
    ON candidate_members(group_id, relationship_type, from_type, from_id, to_type, to_id);
CREATE INDEX IF NOT EXISTS idx_candidate_members_pending_tuple
    ON candidate_members(relationship_type, from_type, from_id, to_type, to_id);

CREATE TABLE IF NOT EXISTS group_store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class GroupStore(GroupStoreProtocol):
    """Stores and retrieves candidate groups, members, and resolutions."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        # PRAGMA must be set before executescript (separate statement)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(candidate_groups)").fetchall()
        }
        additions = [
            ("group_kind", "TEXT NOT NULL DEFAULT 'propose'"),
            ("signal_sources_used", "TEXT NOT NULL DEFAULT '[]'"),
            ("pending_version", "INTEGER NOT NULL DEFAULT 1"),
            ("source_workflow_name", "TEXT"),
            ("source_workflow_receipt_id", "TEXT"),
            ("source_trace_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("source_step_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ]
        for name, ddl in additions:
            if name not in columns:
                self._conn.execute(f"ALTER TABLE candidate_groups ADD COLUMN {name} {ddl}")

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_resolutions_signature_action_confirmed "
            "ON group_resolutions(relationship_type, group_signature, action, confirmed)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidate_groups_pending_signature "
            "ON candidate_groups(relationship_type, signature) "
            "WHERE status = 'pending_review' AND group_kind = 'propose'"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_groups_pending_unique "
            "ON candidate_groups(relationship_type, signature) "
            "WHERE status = 'pending_review' AND group_kind = 'propose'"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidate_groups_signature_status "
            "ON candidate_groups(relationship_type, signature, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidate_members_group_identity "
            "ON candidate_members(group_id, relationship_type, from_type, from_id, to_type, to_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidate_members_pending_tuple "
            "ON candidate_members(relationship_type, from_type, from_id, to_type, to_id)"
        )

        self._run_signature_bucket_migration()
        self._conn.commit()

    def _run_signature_bucket_migration(self) -> None:
        migrated = self._conn.execute(
            "SELECT value FROM group_store_meta WHERE key = 'signature_bucket_v1'"
        ).fetchone()
        if migrated is not None:
            return

        dropped = self._drop_legacy_pending_groups()
        backfilled, preserved = self._backfill_resolved_signatures()
        self._conn.execute(
            "INSERT OR REPLACE INTO group_store_meta(key, value) VALUES (?, ?)",
            ("signature_bucket_v1", format_datetime(utc_now())),
        )
        logger.info("Group store migration: dropped %s legacy pending/transient groups", dropped)
        logger.info("Group store migration: backfilled %s resolved signatures to sigv1", backfilled)
        logger.info(
            "Group store migration: preserved %s legacy resolved rows "
            "with empty thesis_facts as read-only",
            preserved,
        )

    def _drop_legacy_pending_groups(self) -> int:
        rows = self._conn.execute(
            "SELECT group_id FROM candidate_groups WHERE status IN "
            "('pending_review', 'auto_resolved', 'applying')"
        ).fetchall()
        group_ids = [row["group_id"] for row in rows]
        if not group_ids:
            return 0
        placeholders = ",".join("?" for _ in group_ids)
        self._conn.execute(
            f"DELETE FROM candidate_members WHERE group_id IN ({placeholders})",
            tuple(group_ids),
        )
        self._conn.execute(
            f"DELETE FROM candidate_groups WHERE group_id IN ({placeholders})",
            tuple(group_ids),
        )
        return len(group_ids)

    def _backfill_resolved_signatures(self) -> tuple[int, int]:
        rows = self._conn.execute(
            "SELECT resolution_id, relationship_type, thesis_facts FROM group_resolutions"
        ).fetchall()
        backfilled = 0
        preserved = 0
        for row in rows:
            thesis_facts = json.loads(row["thesis_facts"])
            if not thesis_facts:
                preserved += 1
                continue
            signature = compute_group_signature(row["relationship_type"], thesis_facts)
            self._conn.execute(
                "UPDATE group_resolutions SET group_signature = ? WHERE resolution_id = ?",
                (signature, row["resolution_id"]),
            )
            self._conn.execute(
                "UPDATE candidate_groups SET signature = ? WHERE resolution_id = ?",
                (signature, row["resolution_id"]),
            )
            backfilled += 1
        return backfilled, preserved

    # -----------------------------------------------------------------
    # Transaction support
    # -----------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Context manager for atomic compound writes."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def commit(self) -> None:
        """Explicit commit for callers managing their own transactions."""
        self._conn.commit()

    # -----------------------------------------------------------------
    # Groups
    # -----------------------------------------------------------------

    def save_group(self, group: CandidateGroup) -> str:
        """Persist a CandidateGroup. Does NOT commit."""
        self._conn.execute(
            "INSERT INTO candidate_groups "
            "(group_id, relationship_type, signature, status, group_kind, thesis_text, "
            "thesis_facts, analysis_state, signal_sources_used, proposed_by, "
            "member_count, pending_version, review_priority, suggested_priority, "
            "source_workflow_name, source_workflow_receipt_id, source_trace_ids, "
            "source_step_ids, resolution_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id) DO UPDATE SET "
            "relationship_type = excluded.relationship_type, "
            "signature = excluded.signature, "
            "status = excluded.status, "
            "group_kind = excluded.group_kind, "
            "thesis_text = excluded.thesis_text, "
            "thesis_facts = excluded.thesis_facts, "
            "analysis_state = excluded.analysis_state, "
            "signal_sources_used = excluded.signal_sources_used, "
            "proposed_by = excluded.proposed_by, "
            "member_count = excluded.member_count, "
            "pending_version = excluded.pending_version, "
            "review_priority = excluded.review_priority, "
            "suggested_priority = excluded.suggested_priority, "
            "source_workflow_name = excluded.source_workflow_name, "
            "source_workflow_receipt_id = excluded.source_workflow_receipt_id, "
            "source_trace_ids = excluded.source_trace_ids, "
            "source_step_ids = excluded.source_step_ids, "
            "resolution_id = excluded.resolution_id, "
            "created_at = excluded.created_at",
            (
                group.group_id,
                group.relationship_type,
                group.signature,
                group.status,
                group.group_kind,
                group.thesis_text,
                json.dumps(group.thesis_facts),
                json.dumps(group.analysis_state),
                json.dumps(group.signal_sources_used),
                group.proposed_by,
                group.member_count,
                group.pending_version,
                group.review_priority,
                group.suggested_priority,
                group.source_workflow_name,
                group.source_workflow_receipt_id,
                json.dumps(group.source_trace_ids),
                json.dumps(group.source_step_ids),
                group.resolution_id,
                format_datetime(group.created_at),
            ),
        )
        return group.group_id

    def get_group(self, group_id: str) -> CandidateGroup | None:
        """Load a CandidateGroup by ID."""
        row = self._conn.execute(
            "SELECT * FROM candidate_groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_group(row)

    def get_group_by_resolution(self, resolution_id: str) -> CandidateGroup | None:
        """Load the candidate group associated with a resolution, if any."""
        row = self._conn.execute(
            "SELECT * FROM candidate_groups WHERE resolution_id = ?",
            (resolution_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_group(row)

    def list_groups(
        self,
        *,
        relationship_type: str | None = None,
        signature: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[CandidateGroup]:
        """List groups with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if relationship_type is not None:
            clauses.append("relationship_type = ?")
            params.append(relationship_type)
        if signature is not None:
            clauses.append("signature = ?")
            params.append(signature)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM candidate_groups{where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._row_to_group(r) for r in rows]

    def count_groups(
        self,
        *,
        relationship_type: str | None = None,
        signature: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count groups with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if relationship_type is not None:
            clauses.append("relationship_type = ?")
            params.append(relationship_type)
        if signature is not None:
            clauses.append("signature = ?")
            params.append(signature)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM candidate_groups{where}",
            tuple(params),
        ).fetchone()
        return int(row["count"]) if row else 0

    def update_group_status(
        self,
        group_id: str,
        status: str,
        resolution_id: str | None = None,
    ) -> bool:
        """Update group status, optionally setting resolution_id. Does NOT commit."""
        if resolution_id is not None:
            cursor = self._conn.execute(
                "UPDATE candidate_groups SET status = ?, resolution_id = ? WHERE group_id = ?",
                (status, resolution_id, group_id),
            )
        else:
            cursor = self._conn.execute(
                "UPDATE candidate_groups SET status = ? WHERE group_id = ?",
                (status, group_id),
            )
        return cursor.rowcount > 0

    def update_group(
        self,
        group_id: str,
        *,
        status: str | None = None,
        pending_version: int | None = None,
        member_count: int | None = None,
        resolution_id: str | None = None,
        review_priority: str | None = None,
    ) -> bool:
        """Update selected group fields. Does NOT commit."""
        assignments: list[str] = []
        params: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if pending_version is not None:
            assignments.append("pending_version = ?")
            params.append(pending_version)
        if member_count is not None:
            assignments.append("member_count = ?")
            params.append(member_count)
        if resolution_id is not None:
            assignments.append("resolution_id = ?")
            params.append(resolution_id)
        if review_priority is not None:
            assignments.append("review_priority = ?")
            params.append(review_priority)
        if not assignments:
            return False
        params.append(group_id)
        cursor = self._conn.execute(
            f"UPDATE candidate_groups SET {', '.join(assignments)} WHERE group_id = ?",
            tuple(params),
        )
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_group(row: sqlite3.Row) -> CandidateGroup:
        return CandidateGroup(
            group_id=row["group_id"],
            relationship_type=row["relationship_type"],
            signature=row["signature"],
            status=row["status"],
            group_kind=row["group_kind"],
            thesis_text=row["thesis_text"],
            thesis_facts=json.loads(row["thesis_facts"]),
            analysis_state=json.loads(row["analysis_state"]),
            signal_sources_used=json.loads(row["signal_sources_used"]),
            proposed_by=row["proposed_by"],
            member_count=row["member_count"],
            pending_version=row["pending_version"],
            review_priority=row["review_priority"],
            suggested_priority=row["suggested_priority"],
            source_workflow_name=row["source_workflow_name"],
            source_workflow_receipt_id=row["source_workflow_receipt_id"],
            source_trace_ids=json.loads(row["source_trace_ids"]),
            source_step_ids=json.loads(row["source_step_ids"]),
            resolution_id=row["resolution_id"],
            created_at=row["created_at"],
        )

    # -----------------------------------------------------------------
    # Members
    # -----------------------------------------------------------------

    def save_members(self, group_id: str, members: list[CandidateMember]) -> None:
        """Batch insert candidate members. Does NOT commit."""
        for m in members:
            signals_json = json.dumps(
                [s.model_dump(mode="json", exclude_none=True) for s in m.signals]
            )
            self._conn.execute(
                "INSERT INTO candidate_members "
                "(group_id, from_type, from_id, to_type, to_id, relationship_type, "
                "signals, properties) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    group_id,
                    m.from_type,
                    m.from_id,
                    m.to_type,
                    m.to_id,
                    m.relationship_type,
                    signals_json,
                    json.dumps(m.properties),
                ),
            )

    def replace_members(self, group_id: str, members: list[CandidateMember]) -> None:
        """Replace the full member payload for a group. Does NOT commit."""
        self._conn.execute("DELETE FROM candidate_members WHERE group_id = ?", (group_id,))
        self.save_members(group_id, members)

    def get_members(self, group_id: str) -> list[CandidateMember]:
        """Load members for a group."""
        rows = self._conn.execute(
            "SELECT * FROM candidate_members WHERE group_id = ?",
            (group_id,),
        ).fetchall()
        return [self._row_to_member(r) for r in rows]

    def delete_group(self, group_id: str) -> bool:
        """Delete a group and its members. Does NOT commit."""
        self._conn.execute("DELETE FROM candidate_members WHERE group_id = ?", (group_id,))
        cursor = self._conn.execute("DELETE FROM candidate_groups WHERE group_id = ?", (group_id,))
        return cursor.rowcount > 0

    def find_pending_group(
        self,
        relationship_type: str,
        signature: str,
        *,
        group_kind: str = "propose",
    ) -> CandidateGroup | None:
        """Find the current pending bucket for a signature."""
        row = self._conn.execute(
            "SELECT * FROM candidate_groups WHERE relationship_type = ? "
            "AND signature = ? AND status = 'pending_review' AND group_kind = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (relationship_type, signature, group_kind),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_group(row)

    def find_pending_groups_for_tuples(
        self,
        relationship_type: str,
        tuples: list[tuple[str, str, str, str, str]],
        *,
        exclude_group_id: str | None = None,
        statuses: tuple[str, ...] = ("pending_review", "applying"),
    ) -> dict[tuple[str, str, str, str, str], CandidateGroup]:
        """Return live proposal groups containing any of the given tuple identities."""
        mismatched = [item for item in tuples if item[4] != relationship_type]
        if mismatched:
            raise ValueError(
                "all tuple identities must match the requested relationship_type"
            )
        tuple_keys = sorted(set(tuples))
        if not tuple_keys or not statuses:
            return {}

        status_placeholders = ", ".join("?" for _ in statuses)
        tuple_clauses = " OR ".join(
            "(m.from_type = ? AND m.from_id = ? AND m.to_type = ? "
            "AND m.to_id = ? AND m.relationship_type = ?)"
            for _ in tuple_keys
        )
        params: list[Any] = [relationship_type, *statuses]
        for from_type, from_id, to_type, to_id, tuple_relationship_type in tuple_keys:
            params.extend([from_type, from_id, to_type, to_id, tuple_relationship_type])

        exclude_clause = ""
        if exclude_group_id is not None:
            exclude_clause = " AND g.group_id != ?"
            params.append(exclude_group_id)

        rows = self._conn.execute(
            "SELECT g.*, "
            "m.from_type AS member_from_type, "
            "m.from_id AS member_from_id, "
            "m.to_type AS member_to_type, "
            "m.to_id AS member_to_id, "
            "m.relationship_type AS member_relationship_type "
            "FROM candidate_members m "
            "JOIN candidate_groups g ON g.group_id = m.group_id "
            f"WHERE g.relationship_type = ? AND g.status IN ({status_placeholders}) "
            "AND g.group_kind = 'propose' "
            f"AND ({tuple_clauses})"
            f"{exclude_clause} "
            "ORDER BY g.created_at DESC",
            tuple(params),
        ).fetchall()

        conflicts: dict[tuple[str, str, str, str, str], CandidateGroup] = {}
        for row in rows:
            key = (
                row["member_from_type"],
                row["member_from_id"],
                row["member_to_type"],
                row["member_to_id"],
                row["member_relationship_type"],
            )
            conflicts.setdefault(key, self._row_to_group(row))
        return conflicts

    @staticmethod
    def _row_to_member(row: sqlite3.Row) -> CandidateMember:
        signals_data = json.loads(row["signals"])
        return CandidateMember(
            from_type=row["from_type"],
            from_id=row["from_id"],
            to_type=row["to_type"],
            to_id=row["to_id"],
            relationship_type=row["relationship_type"],
            signals=[CandidateSignal(**s) for s in signals_data],
            properties=json.loads(row["properties"]),
        )

    # -----------------------------------------------------------------
    # Resolutions
    # -----------------------------------------------------------------

    def save_resolution(
        self,
        relationship_type: str,
        signature: str,
        action: str,
        rationale: str,
        thesis_text: str,
        thesis_facts: dict[str, Any],
        analysis_state: dict[str, Any],
        resolved_by: str,
        trust_status: str = "watch",
        confirmed: bool = False,
    ) -> str:
        """Persist a resolution. Does NOT commit. Returns resolution_id."""
        resolution_id = new_id("RES")
        self._conn.execute(
            "INSERT INTO group_resolutions "
            "(resolution_id, relationship_type, group_signature, action, rationale, "
            "thesis_text, thesis_facts, analysis_state, trust_status, confirmed, "
            "resolved_by, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resolution_id,
                relationship_type,
                signature,
                action,
                rationale,
                thesis_text,
                json.dumps(thesis_facts),
                json.dumps(analysis_state),
                trust_status,
                1 if confirmed else 0,
                resolved_by,
                format_datetime(utc_now()),
            ),
        )
        return resolution_id

    def confirm_resolution(
        self,
        resolution_id: str,
        trust_status: str | None = None,
    ) -> None:
        """Set confirmed=1 on a resolution. Optionally overwrite trust_status. Does NOT commit."""
        if trust_status is not None:
            self._conn.execute(
                "UPDATE group_resolutions SET confirmed = 1, trust_status = ? "
                "WHERE resolution_id = ?",
                (trust_status, resolution_id),
            )
        else:
            self._conn.execute(
                "UPDATE group_resolutions SET confirmed = 1 WHERE resolution_id = ?",
                (resolution_id,),
            )

    def get_resolution(self, resolution_id: str) -> GroupResolution | None:
        """Load a resolution by ID."""
        row = self._conn.execute(
            "SELECT * FROM group_resolutions WHERE resolution_id = ?",
            (resolution_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_resolution(row)

    def find_resolution(
        self,
        relationship_type: str,
        signature: str,
        action: str | None = None,
        confirmed: bool | None = None,
    ) -> GroupResolution | None:
        """Find the most recent resolution for a signature, with optional filters."""
        clauses = ["relationship_type = ?", "group_signature = ?"]
        params: list[Any] = [relationship_type, signature]
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if confirmed is not None:
            clauses.append("confirmed = ?")
            params.append(1 if confirmed else 0)

        where = " AND ".join(clauses)
        row = self._conn.execute(
            f"SELECT * FROM group_resolutions WHERE {where} ORDER BY resolved_at DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_resolution(row)

    def list_resolutions(
        self,
        *,
        relationship_type: str | None = None,
        signature: str | None = None,
        action: str | None = None,
        confirmed: bool | None = None,
        limit: int = 50,
    ) -> list[GroupResolution]:
        """List resolutions with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if relationship_type is not None:
            clauses.append("relationship_type = ?")
            params.append(relationship_type)
        if signature is not None:
            clauses.append("group_signature = ?")
            params.append(signature)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if confirmed is not None:
            clauses.append("confirmed = ?")
            params.append(1 if confirmed else 0)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM group_resolutions{where} ORDER BY resolved_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._row_to_resolution(r) for r in rows]

    def list_approved_relationship_tuples(
        self,
        relationship_type: str,
        signature: str,
        *,
        group_kind: str = "propose",
    ) -> set[tuple[str, str, str, str, str]]:
        """Return approved tuple identities for a signature bucket."""
        rows = self._conn.execute(
            "SELECT DISTINCT m.from_type, m.from_id, m.to_type, m.to_id, m.relationship_type "
            "FROM candidate_members m "
            "JOIN candidate_groups g ON g.group_id = m.group_id "
            "JOIN group_resolutions r ON r.resolution_id = g.resolution_id "
            "WHERE g.relationship_type = ? AND g.signature = ? AND g.group_kind = ? "
            "AND g.status = 'resolved' AND r.action = 'approve' AND r.confirmed = 1",
            (relationship_type, signature, group_kind),
        ).fetchall()
        return {
            (
                row["from_type"],
                row["from_id"],
                row["to_type"],
                row["to_id"],
                row["relationship_type"],
            )
            for row in rows
        }

    def update_resolution_trust_status(
        self,
        resolution_id: str,
        trust_status: str,
        trust_reason: str = "",
    ) -> bool:
        """Update trust_status + trust_reason on a resolution. Does NOT commit."""
        cursor = self._conn.execute(
            "UPDATE group_resolutions SET trust_status = ?, trust_reason = ? "
            "WHERE resolution_id = ?",
            (trust_status, trust_reason, resolution_id),
        )
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_resolution(row: sqlite3.Row) -> GroupResolution:
        return GroupResolution(
            resolution_id=row["resolution_id"],
            relationship_type=row["relationship_type"],
            group_signature=row["group_signature"],
            action=row["action"],
            rationale=row["rationale"],
            thesis_text=row["thesis_text"],
            thesis_facts=json.loads(row["thesis_facts"]),
            analysis_state=json.loads(row["analysis_state"]),
            trust_status=row["trust_status"],
            trust_reason=row["trust_reason"],
            confirmed=bool(row["confirmed"]),
            resolved_by=row["resolved_by"],
            resolved_at=row["resolved_at"],
        )

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

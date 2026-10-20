"""Derived query seeks and kind counts, maintained by changed Subject rows.

A Subject insert/delete/update changes at most two counters. Cold projection
construction populates them through the same triggers; reads never recount a
kind. Literal indexes carry exactly the scalar columns already projected.
"""

SCHEMA_SQL = """
CREATE INDEX subjects_by_kind_id ON subjects(subject_kind,subject_id,path);
CREATE INDEX subjects_by_kind_path ON subjects(subject_kind,path);
CREATE INDEX subjects_by_kind_lifecycle_path ON subjects(subject_kind,lifecycle,path);
CREATE INDEX claims_by_literal_text
    ON claims(predicate,literal_type,literal_text,lifecycle,subject_path);
CREATE INDEX claims_by_literal_integer
    ON claims(predicate,literal_type,literal_integer_text,lifecycle,subject_path);
CREATE INDEX claims_by_literal_boolean
    ON claims(predicate,literal_type,literal_boolean,lifecycle,subject_path);
CREATE TABLE query_subject_counts (
    subject_kind TEXT NOT NULL, lifecycle TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    PRIMARY KEY(subject_kind,lifecycle)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER query_subject_insert AFTER INSERT ON subjects BEGIN
    INSERT INTO query_subject_counts VALUES (NEW.subject_kind,NEW.lifecycle,1)
    ON CONFLICT(subject_kind,lifecycle) DO UPDATE SET amount=amount+1;
END;
CREATE TRIGGER query_subject_delete AFTER DELETE ON subjects BEGIN
    UPDATE query_subject_counts SET amount=amount-1
    WHERE subject_kind=OLD.subject_kind AND lifecycle=OLD.lifecycle;
    DELETE FROM query_subject_counts WHERE amount=0
    AND subject_kind=OLD.subject_kind AND lifecycle=OLD.lifecycle;
END;
CREATE TRIGGER query_subject_update AFTER UPDATE OF subject_kind,lifecycle ON subjects BEGIN
    UPDATE query_subject_counts SET amount=amount-1
    WHERE subject_kind=OLD.subject_kind AND lifecycle=OLD.lifecycle;
    DELETE FROM query_subject_counts WHERE amount=0
    AND subject_kind=OLD.subject_kind AND lifecycle=OLD.lifecycle;
    INSERT INTO query_subject_counts VALUES (NEW.subject_kind,NEW.lifecycle,1)
    ON CONFLICT(subject_kind,lifecycle) DO UPDATE SET amount=amount+1;
END;
"""

#: Every kind's live Subject count, from the maintained counters (vocabulary-sized).
LIVE_SUBJECT_COUNTS_SQL = (
    "SELECT subject_kind, amount FROM query_subject_counts WHERE lifecycle='live'"
)
#: One kind's live Subject count: a primary-key seek of its counter.
LIVE_SUBJECT_COUNT_SQL = (
    "SELECT amount FROM query_subject_counts WHERE subject_kind=? AND lifecycle='live'"
)

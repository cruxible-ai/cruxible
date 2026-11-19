"""Derived query seeks and counts, maintained by changed Subject and Claim rows.

A Subject insert/delete/update changes at most two kind counters. A live
Claim's insert, delete or move (lifecycle, predicate or Subject) changes at
most two rows of each predicate count table: ``predicate_subjects`` holds how
many live Claims of a predicate each Subject has (a refcount, row present
while above zero), and ``predicate_counts`` holds, per Subject kind and
predicate, the live Claims and the Subjects with at least one. Cold projection
construction populates them through the same triggers; reads never recount.
Literal indexes carry exactly the scalar columns already projected.
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


def _kind_of(row: str) -> str:
    """The Subject kind a Claim row's ``subject_path`` names (``subjects/<kind>/...``)."""

    path = f"{row}.subject_path"
    return (
        f"CASE WHEN substr({path},1,9)='subjects/' AND instr(substr({path},10),'/')>0\n"
        f"        THEN substr({path},10,instr(substr({path},10),'/')-1) ELSE '' END"
    )


#: A live Claim enters its (predicate, Subject) refcount and its (kind, predicate) counts.
_CLAIM_ENTERS = f"""
    INSERT INTO predicate_subjects VALUES (NEW.predicate,NEW.subject_path,1)
    ON CONFLICT(predicate,subject_path) DO UPDATE SET claims=claims+1;
    INSERT INTO predicate_counts VALUES ({_kind_of("NEW")},NEW.predicate,1,
        (SELECT claims FROM predicate_subjects
         WHERE predicate=NEW.predicate AND subject_path=NEW.subject_path)=1)
    ON CONFLICT(subject_kind,predicate) DO UPDATE
    SET claims=claims+1, subjects=subjects+excluded.subjects;"""
#: A live Claim leaves them; a row at zero is deleted, so the rows are a function of the Claims.
_CLAIM_LEAVES = f"""
    UPDATE predicate_subjects SET claims=claims-1
    WHERE predicate=OLD.predicate AND subject_path=OLD.subject_path;
    UPDATE predicate_counts SET claims=claims-1, subjects=subjects-(
        (SELECT claims FROM predicate_subjects
         WHERE predicate=OLD.predicate AND subject_path=OLD.subject_path)=0)
    WHERE subject_kind={_kind_of("OLD")} AND predicate=OLD.predicate;
    DELETE FROM predicate_subjects WHERE claims=0
    AND predicate=OLD.predicate AND subject_path=OLD.subject_path;
    DELETE FROM predicate_counts WHERE claims=0
    AND subject_kind={_kind_of("OLD")} AND predicate=OLD.predicate;"""
_MOVED = "AFTER UPDATE OF lifecycle,predicate,subject_path ON claims"
SCHEMA_SQL += f"""CREATE TABLE predicate_subjects (
    predicate TEXT NOT NULL, subject_path TEXT NOT NULL,
    claims INTEGER NOT NULL CHECK(claims >= 0),
    PRIMARY KEY(predicate,subject_path)
) STRICT, WITHOUT ROWID;
CREATE TABLE predicate_counts (
    subject_kind TEXT NOT NULL, predicate TEXT NOT NULL,
    claims INTEGER NOT NULL CHECK(claims >= 0),
    subjects INTEGER NOT NULL CHECK(subjects >= 0 AND subjects <= claims),
    PRIMARY KEY(subject_kind,predicate)
) STRICT, WITHOUT ROWID;
CREATE INDEX predicate_counts_by_predicate ON predicate_counts(predicate,subject_kind);
CREATE TRIGGER predicate_claim_insert AFTER INSERT ON claims
WHEN NEW.lifecycle='live' BEGIN{_CLAIM_ENTERS}
END;
CREATE TRIGGER predicate_claim_delete AFTER DELETE ON claims
WHEN OLD.lifecycle='live' BEGIN{_CLAIM_LEAVES}
END;
CREATE TRIGGER predicate_claim_leave {_MOVED}
WHEN OLD.lifecycle='live' BEGIN{_CLAIM_LEAVES}
END;
CREATE TRIGGER predicate_claim_enter {_MOVED}
WHEN NEW.lifecycle='live' BEGIN{_CLAIM_ENTERS}
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
#: One kind's live Claims and Subjects with a value, per predicate: a primary-key prefix seek.
KIND_PREDICATE_COUNTS_SQL = (
    "SELECT predicate, claims, subjects FROM predicate_counts WHERE subject_kind=?"
)
#: One predicate's live Claims and Subjects with a value, over its kinds: an index seek.
PREDICATE_COUNTS_SQL = (
    "SELECT coalesce(sum(claims),0), coalesce(sum(subjects),0) FROM predicate_counts "
    "WHERE predicate=?"
)

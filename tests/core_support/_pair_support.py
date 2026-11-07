"""A raw world of pair Subjects: ends, pair kinds keyed by Subject-valued parts, facts.

Every artifact is built directly (no lowering), so a test can lay a tree over
the candidate boundary (``candidate_identity_issues``) or publish it into a
typed projection (``replace_rows``) exactly as written.

- ``pt.component`` (key ``pt.component.purl``) and ``pt.vuln`` (key
  ``pt.vuln.vid``): plain keyed ends;
- ``pt.exposure``: a pair kind keyed by ``component`` (a ``pt.component``) and
  ``vulnerability`` (a ``pt.vuln``), unique; facts ``scope`` (one-valued) and
  ``note`` (many-valued);
- ``pt.owned``: a mixed key, ``owner`` (a ``pt.component``) and ``serial`` (a
  string).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
    render_claim,
)
from cruxible_client.contracts.identity_normalizers import NO_NORMALIZER
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subject_kinds import (
    SubjectKeyRule,
    SubjectKind,
    SubjectKindKeys,
    SubjectKindMinted,
    render_subject_kind,
    subject_kind_digest,
    subject_kind_path,
)
from cruxible_client.contracts.subjects import (
    SubjectShell,
    render_subject,
    subject_digest,
    subject_path,
)
from tests.support.claim_types import claim_type

COMPONENT = "pt.component"
VULN = "pt.vuln"
EXPOSURE = "pt.exposure"
OWNED = "pt.owned"
PURL = f"{COMPONENT}.purl"
VID = f"{VULN}.vid"
E_COMPONENT = f"{EXPOSURE}.component"
E_VULN = f"{EXPOSURE}.vulnerability"
#: A second component part, for a successor that replaces ``component`` (F1).
E_COMPONENT2 = f"{EXPOSURE}.component2"
SCOPE = f"{EXPOSURE}.scope"
NOTE = f"{EXPOSURE}.note"
O_OWNER = f"{OWNED}.owner"
O_SERIAL = f"{OWNED}.serial"
O_FACT = f"{OWNED}.state"
#: A minted end kind and a one-part pair kind over it.
HOST = "pt.host"
RUN = "pt.run"
R_HOST = f"{RUN}.host"


def _type(predicate: str, value: Any = None, *, cardinality: str = "one") -> ClaimType:
    return claim_type(
        predicate,
        value_type={"type": "string"} if value is None else value,
        cardinality=cardinality,
    )


TYPES: dict[str, ClaimType] = {
    item.predicate: item
    for item in (
        _type(PURL),
        _type(VID),
        _type(E_COMPONENT, (COMPONENT,)),
        _type(E_VULN, (VULN,)),
        _type(E_COMPONENT2, (COMPONENT,)),
        _type(SCOPE),
        _type(NOTE, cardinality="many"),
        _type(O_OWNER, (COMPONENT,)),
        _type(O_SERIAL),
        _type(O_FACT),
        _type(R_HOST, (HOST,)),
    )
}


def keyed(kind: str, *keys: str, uniqueness: str = "unique") -> SubjectKind:
    return SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=kind),
        identity_rule=SubjectKindKeys(
            parts=tuple(SubjectKeyRule(claim_type=key, normalizer=NO_NORMALIZER) for key in keys),
            uniqueness=uniqueness,  # type: ignore[arg-type]
        ),
    )


KINDS: dict[str, SubjectKind] = {
    COMPONENT: keyed(COMPONENT, PURL),
    VULN: keyed(VULN, VID),
    EXPOSURE: keyed(EXPOSURE, E_COMPONENT, E_VULN),
    OWNED: keyed(OWNED, O_OWNER, O_SERIAL),
    HOST: SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=HOST), identity_rule=SubjectKindMinted()
    ),
    RUN: keyed(RUN, R_HOST),
}


def definitions(*kinds: str) -> dict[str, bytes]:
    """The ClaimTypes and the named SubjectKinds (all of them by default)."""

    found = {claim_type_path(item.predicate): render_claim_type(item) for item in TYPES.values()}
    for kind in kinds or tuple(KINDS):
        found[subject_kind_path(kind)] = render_subject_kind(KINDS[kind])
    return found


def shell(kind: str, subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{kind}/{subject_id}"),
        subject_kind=kind,
        subject_id=subject_id,
    )


def path_of(kind: str, subject_id: str) -> str:
    return subject_path(kind, subject_id)


def claim(
    number: int,
    predicate: str,
    obj: Any,
    *,
    subject: str,
    live: bool = True,
    qualifier: str | None = None,
) -> ClaimArtifact:
    """A Claim of ``predicate`` about ``subject`` (retired: an attributed superseded one)."""

    from tests.test_indexes.test_claim_units import claim as unit_claim

    return unit_claim(
        number, TYPES[predicate], obj, subject=subject, live=live, qualifier=qualifier
    )


def to(path: str) -> SubjectClaimObject:
    return SubjectClaimObject(address=SemanticAddress.whole_artifact(path))


def literal(value: object) -> LiteralClaimObject:
    return LiteralClaimObject(value=value)


def put(tree: dict[str, bytes], *items: ClaimArtifact | SubjectShell) -> dict[str, bytes]:
    """Write each Claim or Subject shell into ``tree`` at its path; return the paths' bytes."""

    written: dict[str, bytes] = {}
    for item in items:
        if isinstance(item, SubjectShell):
            written[subject_path(item.subject_kind, item.subject_id)] = render_subject(item)
        else:
            written[claim_path(item.identity.name)] = render_claim(item)
    tree.update(written)
    return written


def end(kind: str, subject_id: str, number: int) -> tuple[SubjectShell, ClaimArtifact]:
    """A plain keyed end and its key Claim (its id is its key)."""

    key = PURL if kind == COMPONENT else VID
    return shell(kind, subject_id), claim(
        number, key, literal(subject_id), subject=path_of(kind, subject_id)
    )


def exposure(
    component: str, vuln: str, number: int
) -> tuple[SubjectShell, ClaimArtifact, ClaimArtifact]:
    """The pair ``pt.exposure`` of two ends (by id) and its two endpoint Claims."""

    pair = f"{component}--{vuln}"
    at = path_of(EXPOSURE, pair)
    return (
        shell(EXPOSURE, pair),
        claim(number, E_COMPONENT, to(path_of(COMPONENT, component)), subject=at),
        claim(number + 1, E_VULN, to(path_of(VULN, vuln)), subject=at),
    )


def retired_shell(item: SubjectShell) -> SubjectShell:
    return item.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_digest(item).tagged
            )
        }
    )


def retired_claim(item: ClaimArtifact) -> ClaimArtifact:
    """The lifecycle-only retirement of ``item`` (its statement unchanged)."""

    from cruxible_core.claims.key_claims import retired_key_claim

    return retired_key_claim(item)


def successor_kind(previous: SubjectKind, **changes: Any) -> SubjectKind:
    lifecycle = changes.pop("lifecycle", None) or ArtifactLifecycle(
        predecessor_digest=subject_kind_digest(previous).tagged
    )
    return previous.model_copy(update={"lifecycle": lifecycle, **changes})


def judge(parent: Mapping[str, bytes], changes: Mapping[str, bytes]) -> list[str]:
    """The identity codes ``changes`` earn at the candidate boundary over ``parent``."""

    from cruxible_core.indexes.subject_identity import candidate_identity_issues

    return [
        issue.code
        for issue in candidate_identity_issues(
            current_tree=dict(parent),
            candidate_tree={**parent, **changes},
            scope=tuple(sorted(changes)),
        )
    ]


# -- a typed projection fed directly ----------------------------------------------


def projection(sources: Mapping[str, bytes], *, path: Any = None) -> Any:
    """A fresh typed projection holding ``sources`` (a full build), in memory or at ``path``."""

    import sqlite3

    from cruxible_core.indexes.typed_sqlite import complete_schema_sql

    connection = sqlite3.connect(":memory:" if path is None else str(path), isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(complete_schema_sql())
    publish(connection, sources, changed=None)
    return connection


def publish(connection: Any, sources: Mapping[str, bytes], *, changed: Any) -> None:
    """Publish ``sources`` (every changed path's new bytes) into a projection.

    ``changed=None`` is a full build; otherwise the changed paths, which must
    all be in ``sources``.
    """

    from cruxible_core.compiler.compiler import CURRENT_COMPILER
    from cruxible_core.indexes.typed_sqlite import parse_static_owners, replace_rows

    accepted = SimpleNamespace(compiler=CURRENT_COMPILER)
    request = SimpleNamespace(git_object_format="sha1", semantic_root=None)
    replace_rows(
        connection,
        request=request,  # type: ignore[arg-type]
        parsed=parse_static_owners(dict(sources), accepted=accepted),
        sources=dict(sources),
        changed_paths=() if changed is None else tuple(changed),
        whole_checks=False,
    )


class HeldTree(dict):  # type: ignore[type-arg]
    """An accepted tree standing on its projection, as the candidate boundary reads one.

    ``tree_index_connection`` then reads the projection's indexes instead of
    deriving an index from the tree's members.
    """

    _parent = None

    def __init__(self, sources: Mapping[str, bytes], connection: Any) -> None:
        super().__init__(sources)
        self._connection = connection

    def _accepted_reader(self) -> Any:
        from contextlib import contextmanager

        @contextmanager
        def bind() -> Any:
            yield SimpleNamespace(typed=SimpleNamespace(connection=self._connection))

        return bind()


def held(sources: Mapping[str, bytes], *, path: Any = None) -> HeldTree:
    """``sources`` as an accepted tree on a fresh projection of it (in memory or at ``path``)."""

    return HeldTree(sources, projection(sources, path=path))


PAIR_TABLES = ("pair_ends", "pair_slot_live", "pair_units")


def pair_rows(connection: Any) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: sorted(
            (tuple(row) for row in connection.execute(f"SELECT * FROM {table}")), key=repr
        )
        for table in PAIR_TABLES
    }


__all__ = [
    "COMPONENT",
    "EXPOSURE",
    "HOST",
    "RUN",
    "R_HOST",
    "E_COMPONENT",
    "E_COMPONENT2",
    "E_VULN",
    "KINDS",
    "NOTE",
    "OWNED",
    "O_FACT",
    "O_OWNER",
    "O_SERIAL",
    "PAIR_TABLES",
    "PURL",
    "SCOPE",
    "TYPES",
    "VID",
    "VULN",
    "claim",
    "definitions",
    "end",
    "exposure",
    "held",
    "judge",
    "keyed",
    "literal",
    "pair_rows",
    "path_of",
    "projection",
    "publish",
    "put",
    "retired_claim",
    "retired_shell",
    "shell",
    "successor_kind",
    "to",
]

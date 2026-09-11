"""Request-owned SQL evaluation views over a bound accepted publication.

The only retained derived structures are the existing Merkle tries. Membership,
owner resolution, Claim membership and reverse edges come from typed SQL. A
candidate owns temporary changed rows and never mutates its accepted base.
"""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from cruxible_client.contracts.canonical import canonical_bytes, file_digest, is_candidate_card_path
from cruxible_client.contracts.claims import ClaimStatement
from cruxible_client.contracts.documents import DocumentArtifactAdapter
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.claims.closure import (
    ArtifactDependencyStateV1,
    DependencyIndexV1,
    _outgoing_edges,
    _sorted_edges,
    build_dependency_edge_tree,
    update_dependency_edge_tree,
)
from cruxible_core.indexes.claims.claim_subject_index import ClaimSubjectIndex
from cruxible_core.indexes.typed_sqlite import insert_members
from cruxible_core.indexes.typed_state import OWNER_BY_KIND, OWNER_CODECS, insert_owners, schema_sql

T = TypeVar("T")


class SelectedRows(Mapping[str, T], Generic[T]):
    """A lookup function with an explicit, separately charged enumeration path."""

    def __init__(
        self,
        get: Callable[[str], T],
        keys: Callable[[], Iterable[str]],
        *,
        owner: EvaluationRows | SelectionSpec | None = None,
    ) -> None:
        self._get, self._keys = get, keys
        self.owner = owner

    def __getitem__(self, key: str) -> T:
        return self._get(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys())

    def __len__(self) -> int:
        return sum(1 for _ in self._keys())


@dataclass(frozen=True)
class EvaluationProofs:
    members: Any
    edges: Any


class FrozenProjectionStorage(ProjectionIntegrityError):
    """The frozen publication uses the source-only evaluation oracle."""


class EvaluationRows:
    """One accepted or candidate selection, valid only inside its owning request."""

    def __init__(self, projection: Any) -> None:
        if projection.typed is None:
            projection.close()
            raise FrozenProjectionStorage("indexed evaluation requires typed accepted storage")
        authenticate = getattr(projection, "require_source_authentication", None)
        if authenticate is not None:
            try:
                authenticate()
            except BaseException:
                projection.close()
                raise
        self.projection = projection
        self.connection = projection._connection
        self.reader = projection.typed
        self.prefix = "main."
        self.parent: EvaluationRows | None = None
        self.changed: Mapping[str, bytes | None] = {}
        self.children: list[EvaluationRows] = []
        self.binding = canonical_bytes(projection.accepted.model_dump(mode="json"))
        self.states = SelectedRows(
            self.state, lambda: self.keys("artifact_lookup", "path", "kind!='fixture'"), owner=self
        )
        self.identities = SelectedRows(
            self.identity_path, lambda: self.keys("artifact_lookup", "identity", "kind!='fixture'")
        )
        self.members = SelectedRows(
            self.member_digest, lambda: self.keys("members", "path", "path NOT LIKE 'changesets/%'")
        )

    def table(self, name: str) -> str:
        return self.prefix + name

    def keys(self, table: str, key: str, predicate: str = "1") -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self.connection.execute(
                f"SELECT DISTINCT {key} FROM {self.table(table)} WHERE {predicate} ORDER BY {key}"
            )
        )

    def one(self, sql: str, key: str) -> Any:
        row = self.connection.execute(sql, (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return row

    def member_digest(self, path: str) -> str:
        row = self.one(
            f"SELECT file_digest FROM {self.table('members')} WHERE path=? AND path NOT LIKE 'changesets/%'",
            path,
        )
        return str(row[0]).removeprefix("sha256:")

    def source_bytes(self, path: str) -> bytes:
        if path in self.changed:
            value = self.changed[path]
            if value is None:
                raise KeyError(path)
            return value
        return bytes(self.reader.member_bytes(path))

    def identity_path(self, identity: str) -> str:
        return str(
            self.one(
                f"SELECT path FROM {self.table('artifact_lookup')} WHERE identity=? AND kind!='fixture'",
                identity,
            )[0]
        )

    def state(self, path: str) -> ArtifactDependencyStateV1:
        row = self.one(
            f"SELECT kind,format_tag,artifact_digest FROM {self.table('artifact_lookup')} WHERE path=? AND kind!='fixture'",
            path,
        )
        source = OWNER_BY_KIND[row[0]].parse(
            self.source_bytes(path), path=path, codec=self.reader.codec
        )
        adapted = DocumentArtifactAdapter(source) if row[0] == "document" else source
        return ArtifactDependencyStateV1(
            path=path,
            artifact_kind=row[0],
            artifact_tag=row[1],
            identity=adapted.identity,
            artifact_digest=row[2],
            pins=adapted.pins,
            lifecycle=adapted.lifecycle,
        )

    def pin_sources(self, identity: str, *, consumed: bool = False) -> frozenset[str]:
        kind = "" if consumed else "AND p.edge_kind='required_pin'"
        return frozenset(
            row[0]
            for row in self.connection.execute(
                f"SELECT DISTINCT a.path FROM {self.table('pins')} p JOIN {self.table('artifact_lookup')} a ON a.identity=p.source_identity WHERE p.target_identity=? {kind}",
                (identity,),
            )
        )

    def outgoing(self, path: str) -> tuple[Any, ...]:
        state = self.states.get(path)
        return (
            ()
            if state is None
            else _outgoing_edges(state, states=self.states, paths_by_identity=self.identities)
        )

    def incoming(self, path: str) -> tuple[Any, ...]:
        state = self.states.get(path)
        if state is None:
            return ()
        return _sorted_edges(
            edge
            for source in self.pin_sources(state.identity.qualified)
            for edge in self.outgoing(source)
            if edge.target_path == path
        )

    def dependencies(self, edge_tree: Any = None) -> DependencyIndexV1:
        outgoing = SelectedRows(self.outgoing, lambda: self.resolved_edge_paths("source"))
        incoming = SelectedRows(self.incoming, lambda: self.resolved_edge_paths("target"))
        pin_sources = SelectedRows(
            self.pin_sources,
            lambda: self.keys("pins", "target_identity", "edge_kind='required_pin'"),
        )
        if edge_tree is None:
            edge_tree = build_dependency_edge_tree(
                tuple(edge for values in outgoing.values() for edge in values)
            )
        return DependencyIndexV1(
            self.states, self.identities, pin_sources, outgoing, incoming, edge_tree
        )

    def resolved_edge_paths(self, side: str) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self.connection.execute(
                f"SELECT DISTINCT {side}.path FROM {self.table('pins')} p JOIN {self.table('artifact_lookup')} source ON source.identity=p.source_identity JOIN {self.table('artifact_lookup')} target ON target.identity=p.target_identity AND target.artifact_digest=p.target_digest WHERE p.edge_kind='required_pin' AND source.kind!='fixture' AND target.kind!='fixture' ORDER BY {side}.path"
            )
        )

    def claim_subjects(self) -> ClaimSubjectIndex:
        def subject(path: str) -> str:
            return str(
                self.one(f"SELECT subject_path FROM {self.table('claims')} WHERE path=?", path)[0]
            )

        def claims(subject_path: str) -> frozenset[str]:
            return frozenset(
                row[0]
                for row in self.connection.execute(
                    f"SELECT path FROM {self.table('claims')} WHERE subject_path=?", (subject_path,)
                )
            )

        return ClaimSubjectIndex(
            SelectedRows(subject, lambda: self.keys("claims", "path")),
            SelectedRows(claims, lambda: self.keys("claims", "subject_path")),
        )

    def claim_type_items(self, identity: str) -> tuple[tuple[str, bytes], ...]:
        paths = self.connection.execute(
            f"SELECT DISTINCT c.path FROM {self.table('pins')} p "
            f"JOIN {self.table('claims')} c ON c.identity=p.source_identity "
            "WHERE p.edge_kind='required_pin' AND p.target_identity=? "
            "AND c.claim_type_identity=? ORDER BY c.path",
            (identity, identity),
        )
        return tuple((row[0], self.source_bytes(row[0])) for row in paths)

    def claim_items(self, statement: ClaimStatement) -> tuple[tuple[str, bytes], ...]:
        subject = statement.subject
        paths = self.connection.execute(
            f"SELECT path FROM {self.table('claims')} WHERE subject_path=? AND predicate=? AND subject_selector_scheme=? AND subject_selector_value=? AND lifecycle='live' ORDER BY path",
            (
                subject.artifact_path,
                statement.predicate,
                subject.selector.scheme,
                subject.selector.value,
            ),
        )
        return tuple((row[0], self.source_bytes(row[0])) for row in paths)

    def artifact_rows(
        self, kind: str, convert: Callable[[str, bytes, str], T], *, path_key: bool = False
    ) -> Mapping[str, T]:
        table = self.table(OWNER_BY_KIND[kind].table)
        key = "path" if path_key else "identity"

        def get(value: str) -> T:
            row = self.one(f"SELECT path,artifact_digest FROM {table} WHERE {key}=?", value)
            return convert(row[0], self.source_bytes(row[0]), row[1])

        return SelectedRows(
            get,
            lambda: (
                row[0]
                for row in self.connection.execute(f"SELECT {key} FROM {table} ORDER BY {key}")
            ),
        )

    def overlay(self, edits: Mapping[str, bytes | None]) -> EvaluationRows:
        """Create a bound changed-row selection; exact source identity suppresses edges."""
        if self.parent is not None or self.children:
            raise ProjectionIntegrityError("each candidate selection requires its own accepted handle")
        candidate = EvaluationRows(self.projection)
        candidate.parent = self
        # TEMP rows live on the already authenticated SQLite connection. Reopening
        # its pathname could bind a replacement inode between authentication and
        # ATTACH, even while the original verified connection remains sound.
        self.children.append(candidate)
        candidate.connection.executescript(
            schema_sql()
            .replace("CREATE TABLE", "CREATE TEMP TABLE")
            .replace("CREATE VIEW", "CREATE TEMP VIEW")
        )
        candidate.connection.executescript(
            "CREATE TEMP TABLE changed_paths(path TEXT PRIMARY KEY); CREATE TEMP TABLE changed_sources(identity TEXT PRIMARY KEY);"
        )
        candidate.changed = {
            path: content for path, content in edits.items() if not is_candidate_card_path(path)
        }
        edits = candidate.changed
        candidate.binding = canonical_bytes(
            {
                "base": self.binding.decode(),
                "edits": [
                    [path, None if content is None else file_digest(content).tagged]
                    for path, content in sorted(edits.items())
                ],
            }
        )
        candidate.connection.execute("CREATE TEMP TABLE overlay_binding(binding BLOB NOT NULL)")
        candidate.connection.execute("INSERT INTO overlay_binding VALUES (?)", (candidate.binding,))
        candidate.connection.executemany(
            "INSERT INTO changed_paths VALUES (?)", ((path,) for path in edits)
        )
        candidate.connection.execute(
            "INSERT INTO changed_sources SELECT identity FROM main.artifact_lookup WHERE path IN (SELECT path FROM changed_paths)"
        )
        blobs = {path: content for path, content in edits.items() if content is not None}
        insert_members(candidate.connection, blobs, self.reader.accepted.git_object_format)
        from cruxible_core.indexes.typed_sqlite import parse_static_owners

        parsed = parse_static_owners(blobs, accepted=self.reader.accepted)
        for row in parsed.envelopes:
            old = candidate.connection.execute(
                "SELECT path FROM main.artifact_lookup WHERE identity=?", (row.identity,)
            ).fetchone()
            if old is not None and old[0] not in edits and old[0] != row.path:
                raise ValueError("candidate tree contains a duplicate semantic artifact identity")
        insert_owners(
            candidate.connection,
            parsed=parsed,
            blobs=blobs,
            codec=self.reader.codec,
            resolve_digest=self.resolve_claim_digest,
        )
        candidate.connection.execute(
            "INSERT OR IGNORE INTO changed_sources SELECT identity FROM artifact_lookup"
        )
        for table in ("members", *(owner.table for owner in OWNER_CODECS)):
            candidate.connection.execute(
                f"CREATE TEMP VIEW selected_{table} AS SELECT * FROM main.{table} WHERE path NOT IN (SELECT path FROM changed_paths) UNION ALL SELECT * FROM temp.{table}"
            )
        branches = [
            f"SELECT identity,'{owner.kind}' AS kind,format_tag,path,artifact_digest,predecessor_digest,revision,{('lifecycle' if owner.lifecycle else 'NULL AS lifecycle')} FROM selected_{owner.table}"
            for owner in OWNER_CODECS
        ]
        candidate.connection.execute(
            "CREATE TEMP VIEW selected_artifact_lookup AS " + " UNION ALL ".join(branches)
        )
        candidate.connection.execute(
            "CREATE TEMP VIEW selected_pins AS SELECT * FROM main.pins WHERE source_identity NOT IN (SELECT identity FROM changed_sources) UNION ALL SELECT * FROM temp.pins"
        )
        candidate.prefix = "selected_"
        return candidate

    def resolve_claim_digest(self, digest: str) -> tuple[str, ...]:
        identities = {
            row[0]
            for row in self.connection.execute(
                f"SELECT identity FROM {self.table('claims')} WHERE artifact_digest=?",
                (digest,),
            )
        }
        if self.reader.history is not None:
            with self.reader.history() as history:
                identities.update(history.identities_for_digest(digest))
        return tuple(sorted(identities))

    def reverse_neighbors(
        self, identity: str
    ) -> tuple[tuple[ArtifactDependencyStateV1, tuple[str, ...]], ...]:
        values = []
        for path in sorted(self.pin_sources(identity, consumed=True)):
            state = self.state(path)
            roles = {pin.role for pin in state.pins if pin.target.qualified == identity}
            consumed = self.connection.execute(
                f"SELECT 1 FROM {self.table('pins')} WHERE source_identity=? AND edge_kind='consumed_claim_input' AND target_identity=? LIMIT 1",
                (state.identity.qualified, identity),
            ).fetchone()
            if consumed is not None:
                roles.add("backing-input")
            values.append((state, tuple(sorted(roles))))
        return tuple(values)

    def close(self) -> None:
        for child in self.children:
            child.close()
        if self.parent is None:
            self.projection.close()

    def advanced_dependencies(
        self, previous: DependencyIndexV1, changed: Iterable[str]
    ) -> DependencyIndexV1:
        affected = set(changed)
        identities = set()
        for path in tuple(affected):
            for states in (previous.states, self.states):
                row = states.get(path)
                if row is not None:
                    identities.add(row.identity.qualified)
        for identity in identities:
            affected.update(previous.sources_by_pinned_identity.get(identity, ()))
            affected.update(self.pin_sources(identity))
        updates = {path: self.outgoing(path) for path in affected}
        return self.dependencies(update_dependency_edge_tree(previous.edge_tree, updated=updates))


def mapped_values(rows: Mapping[str, T], transform: Callable[[T], Any]) -> Mapping[str, Any]:
    return SelectedRows(lambda key: transform(rows[key]), lambda: rows)


class MerkleMembers(Mapping[str, str]):
    """Read the member leaves already required by the existing Merkle algorithm."""

    def __init__(self, tree: Any) -> None:
        self.tree = tree

    def __getitem__(self, key: str) -> str:
        node = self.tree.nodes.get(key)
        if node is None or not node.is_leaf:
            raise KeyError(key)
        return str(node.member_digest)

    def __iter__(self) -> Iterator[str]:
        return (path for path in self.tree.nodes if self.tree.nodes[path].is_leaf)

    def __len__(self) -> int:
        return sum(1 for _ in self)


class ChangedMembers(Mapping[str, str]):
    """The pre-parse commitment phase reads only the complete changed region."""

    def __init__(self, parent: Mapping[str, str], changed: Mapping[str, str | None]) -> None:
        self.parent, self.changed = parent, changed

    def __getitem__(self, key: str) -> str:
        if key not in self.changed:
            return self.parent[key]
        value = self.changed[key]
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(
            sorted(
                {path for path in self.parent if path not in self.changed}
                | {path for path, value in self.changed.items() if value is not None}
            )
        )

    def __len__(self) -> int:
        return sum(1 for _ in self)


_ACTIVE_SELECTIONS: ContextVar[tuple[ExitStack, dict[SelectionSpec, EvaluationRows]] | None] = ContextVar(
    "evaluation_selections", default=None
)


@dataclass(eq=False, frozen=True)
class SelectionSpec:
    """Detached selection recipe: no connection, compiled owner map or result cache."""

    reader_factory: Callable[[], Any]
    edits: Mapping[str, bytes | None] | None = None

    def __post_init__(self) -> None:
        if self.edits is not None:
            object.__setattr__(self, "edits", MappingProxyType(dict(self.edits)))

    def call(self, operation: Callable[[EvaluationRows], T]) -> T:
        active = _ACTIVE_SELECTIONS.get()
        if active is not None:
            stack, selections = active
            selected = selections.get(self)
            if selected is None:
                base = EvaluationRows(self.reader_factory())
                stack.callback(base.close)
                selected = base if self.edits is None else base.overlay(self.edits)
                selections[self] = selected
            return operation(selected)
        base = EvaluationRows(self.reader_factory())
        try:
            return operation(base if self.edits is None else base.overlay(self.edits))
        finally:
            base.close()

    @contextmanager
    def scope(self) -> Iterator[None]:
        if _ACTIVE_SELECTIONS.get() is not None:
            yield
            return
        with ExitStack() as stack:
            token = _ACTIVE_SELECTIONS.set((stack, {}))
            try:
                yield
            finally:
                _ACTIVE_SELECTIONS.reset(token)

    def rows(
        self,
        table: str,
        getter: Callable[[EvaluationRows, str], T],
        key: str,
        predicate: str = "1",
        *,
        owner: bool = False,
    ) -> SelectedRows[T]:
        return SelectedRows(
            lambda value: self.call(lambda rows: getter(rows, value)),
            lambda: self.call(lambda rows: rows.keys(table, key, predicate)),
            owner=self if owner else None,
        )

    def dependencies(self, edge_tree: Any) -> DependencyIndexV1:
        states = self.rows(
            "artifact_lookup",
            lambda rows, path: rows.state(path),
            "path",
            "kind!='fixture'",
            owner=True,
        )
        identities = self.rows(
            "artifact_lookup",
            lambda rows, identity: rows.identity_path(identity),
            "identity",
            "kind!='fixture'",
        )
        sources = self.rows(
            "pins",
            lambda rows, identity: rows.pin_sources(identity),
            "target_identity",
            "edge_kind='required_pin'",
        )
        outgoing = SelectedRows(
            lambda path: self.call(lambda rows: rows.outgoing(path)),
            lambda: self.call(lambda rows: rows.resolved_edge_paths("source")),
        )
        incoming = SelectedRows(
            lambda path: self.call(lambda rows: rows.incoming(path)),
            lambda: self.call(lambda rows: rows.resolved_edge_paths("target")),
        )
        return DependencyIndexV1(states, identities, sources, outgoing, incoming, edge_tree)

    def claim_subjects(self) -> ClaimSubjectIndex:
        by_claim = self.rows(
            "claims", lambda rows, path: rows.claim_subjects().subject_by_claim[path], "path"
        )
        by_subject = self.rows(
            "claims",
            lambda rows, subject: rows.claim_subjects().claims_by_subject[subject],
            "subject_path",
        )
        return ClaimSubjectIndex(by_claim, by_subject)

    def artifact_rows(
        self, kind: str, convert: Callable[[str, bytes, str], T], *, path_key: bool = False
    ) -> Mapping[str, T]:
        return SelectedRows(
            lambda value: self.call(
                lambda rows: rows.artifact_rows(kind, convert, path_key=path_key)[value]
            ),
            lambda: self.call(
                lambda rows: tuple(rows.artifact_rows(kind, convert, path_key=path_key))
            ),
        )

    def overlay(self, edits: Mapping[str, bytes | None]) -> SelectionSpec:
        merged = dict(self.edits or {})
        merged.update(edits)
        return SelectionSpec(self.reader_factory, merged)


def derive_indexed_state(tree: Any) -> Any:
    try:
        return _derive_indexed_state(tree)
    except FrozenProjectionStorage:
        from cruxible_core.proposals.proposals import build_tree_state

        return build_tree_state(tree)


def _derive_indexed_state(tree: Any) -> Any:
    from cruxible_client.contracts.merkle import build_merkle_manifest, update_merkle_manifest
    from cruxible_core.proposals.proposals import build_tree_state

    if getattr(tree, "_accepted_reader", None) is None:
        return build_tree_state(tree)
    selection = SelectionSpec(tree._accepted_reader)
    if tree._parent is not None:
        selection = selection.overlay(tree._edits)
    with selection.scope(), tree._lock:
        proofs = tree._proofs
        if proofs is None:
            seed = tree._proof_seed
            if seed is None and tree._parent is not None:
                with tree._parent._lock:
                    if tree._parent._proofs is not None:
                        seed = (tree._parent._proofs, tree._accepted_reader, tree._edits)
            if seed is None:
                dependencies = selection.call(lambda rows: rows.dependencies())
                merkle = selection.call(lambda rows: build_merkle_manifest(rows.members))
            else:
                previous_proofs, previous_reader, edits = seed
                previous = SelectionSpec(previous_reader)
                dependencies = selection.call(
                    lambda rows: rows.advanced_dependencies(
                        previous.dependencies(previous_proofs.edges), edits
                    )
                )
                merkle = update_merkle_manifest(
                    previous_proofs.members,
                    updated={
                        path: file_digest(body).value
                        for path, body in edits.items()
                        if body is not None
                        and not path.startswith("changesets/")
                        and not is_candidate_card_path(path)
                    },
                    removed=[
                        path
                        for path, body in edits.items()
                        if body is None
                        and not path.startswith("changesets/")
                        and not is_candidate_card_path(path)
                    ],
                )
            proofs = EvaluationProofs(merkle, dependencies.edge_tree)
            tree._proofs = proofs
            tree._proof_seed = None
        return indexed_state(selection, proofs.members, proofs.edges)


def indexed_state(selection: SelectionSpec, merkle: Any, edge_tree: Any) -> Any:
    from cruxible_client.contracts.merkle import detach_merkle_tree
    from cruxible_core.proposals.proposals import EvaluatedTreeState

    merkle = detach_merkle_tree(merkle)
    edge_tree = detach_merkle_tree(edge_tree)

    @dataclass(frozen=True)
    class IndexedTreeState(EvaluatedTreeState):
        selection: SelectionSpec

        def scope(self) -> Any:
            return self.selection.scope()

        def advance(self, tree: Mapping[str, bytes], advanced: Any) -> Any:
            candidate = self.selection.overlay({path: tree.get(path) for path in advanced.scope})
            dependencies = candidate.call(
                lambda rows: rows.advanced_dependencies(self.dependencies, advanced.scope)
            )
            return indexed_state(candidate, advanced.merkle, dependencies.edge_tree)

    return IndexedTreeState(
        MerkleMembers(merkle),
        merkle,
        selection.dependencies(edge_tree),
        selection.claim_subjects(),
        selection,
    )

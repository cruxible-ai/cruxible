"""Git-compatible in-process ledger primitives; system Git for transport and maintenance."""

from __future__ import annotations

import atexit
import bisect
import configparser
import fcntl
import functools
import hashlib
import os
import re
import secrets
import signal
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, ParamSpec, TypeVar, cast, overload

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cruxible_client.contracts.canonical import CandidateDigest, normalize_manifest_paths
from cruxible_client.contracts.errors import GitError
from cruxible_client.contracts.persistent import MapMutation, PersistentMap
from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.types import GitObjectFormat
from cruxible_core.derived.derived_state import (
    BlobRef,
    SnapshotTree,
    edited_paths,
    path_facts,
    root_and_edits,
    row_values,
    same_row,
)
from cruxible_core.governance.keys import raw_public_key_hex_from_openssh
from cruxible_core.ledger.generation_facts import AcceptingAdmission, parse_accepting_admission
from cruxible_core.ledger.git_objects import PackStore
from cruxible_core.ledger.git_signing import load_private, sign_commit
from cruxible_core.ledger.git_signing import verify_commit as verify_signature
from cruxible_core.runtime.phase_timing import timed_phase

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROPOSAL_REF_RE = re.compile(r"^refs/proposals/[a-z][a-z0-9_.-]{0,127}/[a-z][a-z0-9_.-]{0,127}$")
_PROPOSAL_REVIEW_REF_RE = re.compile(r"^refs/heads/proposals/[0-9a-f]{64}$")

# Exact snapshots and explicit leases protect both accepted and review history.
MIRROR_PUSH_TIMEOUT_SECONDS: Final = 30.0
_MIRROR_ARG_BYTES: Final = 64 * 1024
_MIRROR_MAX_REFS: Final = 4096
_MIRROR_MAIN: Final = "refs/heads/main"
PROPOSAL_ARCHIVE_REF: Final = "refs/settled/archive"
_LEGACY_SETTLED_RE = re.compile(r"^refs/settled/[0-9a-f]{64}$")
_MIRROR_PREFIXES: Final = ("refs/heads/proposals/",)

# Every Cruxible note ref, in one table. The generation descriptor was the
# first; the proposal evaluation and the approval list are projections of the
# evidence store that reach Git through exactly the same write, so a note a
# reviewer reads is never a second mechanism with its own persistence rules.
NOTE_REFS: Final[Mapping[str, str]] = {
    "generation": "refs/notes/cruxible-gen",
    "evaluation": "refs/notes/cruxible-eval",
    "approval": "refs/notes/cruxible-approval",
}
# The names these refs carried before the public rename, each mapped to its
# current name. They are read only by the one-time move at instance open
# (`GitLedger.migrate_legacy_note_refs`) and by mirror publication, which
# accepts them in a recorded snapshot or a remote advertisement so the next
# push deletes them under lease. No note is ever read or written under them.
LEGACY_NOTE_REFS: Final[Mapping[str, str]] = {
    "refs/notes/playbill-gen": NOTE_REFS["generation"],
    "refs/notes/playbill-eval": NOTE_REFS["evaluation"],
    "refs/notes/playbill-approval": NOTE_REFS["approval"],
}
_PASSTHROUGH_ENVIRONMENT = ("PATH", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT")
_COMMAND_ENVIRONMENT = frozenset(
    {
        "GIT_INDEX_FILE",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
        # Publication only. A push is the one ledger operation that talks to a
        # host outside this daemon, so it is also the only one that needs a
        # credential: `HOME`/`SSH_AUTH_SOCK`/`GIT_SSH_COMMAND` let the daemon's
        # own SSH identity answer, and the three `GIT_CONFIG_*` names are Git's
        # environment-config protocol, which carries an HTTPS token without ever
        # putting it in an argument vector every process on the host can read.
        "HOME",
        "SSH_AUTH_SOCK",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    }
)


@dataclass(frozen=True)
class GitTreeEntry:
    """Metadata for one recursive tree entry, before any blob is read."""

    path: str
    mode: str
    object_type: str
    oid: str
    size: int | None


@dataclass(frozen=True)
class GitTreeChange:
    """One raw add, modification, type change, or deletion between two trees.

    `mode` and `oid` describe the *destination* entry; a deletion carries
    `oid=None` and the caller drops the path. No rename or copy detection ever
    runs, so a rename is reported as exactly one deletion plus one addition and
    the caller never has to reason about a similarity score.
    """

    path: str
    status: str
    mode: str
    oid: str | None
    # The source entry's object, absent for an addition.
    previous_oid: str | None = None


def _validate_commit_message(message: str) -> None:
    """Refuse a commit message that is not the prose summary a reviewer reads.

    Nothing ever parses a commit message back, so the only obligations are that
    it exists, that it is not the blank subject Git would otherwise accept, and
    that it carries no NUL -- which `commit-tree` truncates at, silently
    dropping the rest of the summary.
    """

    if not message.strip():
        raise GitError("commit message must be a nonblank prose summary")
    if "\x00" in message:
        raise GitError("commit message must not contain a NUL byte")


def _entry_size(entry: GitTreeEntry) -> int:
    if entry.size is None:
        raise GitError(f"ledger blob has no size: {entry.path}")
    return entry.size


def _proven_blob_entries(entries: tuple[GitTreeEntry, ...]) -> tuple[GitTreeEntry, ...]:
    """Refuse a tree member that is anything but a plain committed file.

    A symlink, a submodule or an executable bit reaches a reader as something
    other than the bytes the ledger claims to carry, so every path Cruxible
    hands out — read, listed, or fetched by name — passes this one proof.
    """

    for entry in entries:
        if entry.object_type != "blob" or entry.mode != "100644":
            raise GitError(
                f"ledger tree contains unsupported {entry.mode} {entry.object_type}: {entry.path}"
            )
    return entries


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _pack_transaction(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Group a logical ledger operation; publish its objects before any ref."""

    @functools.wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        ledger = cast("GitLedger", args[0])
        reader = _batch_reader(ledger.path)
        with reader.transaction():
            return method(*args, **kwargs)

    return wrapped


class GitLedger:
    """A daemon-owned bare repository with Git-compatible objects, signatures and refs."""

    def __init__(
        self,
        path: Path,
        *,
        signing_key_path: Path,
        allowed_signers_path: Path,
    ) -> None:
        self.path = path
        self._signing_key_path = signing_key_path
        self._allowed_signers_path = allowed_signers_path
        self._object_format_cache: GitObjectFormat | None = None

    @classmethod
    def initialize(
        cls,
        path: Path,
        *,
        object_format: GitObjectFormat,
        signing_key_path: Path,
        allowed_signers_path: Path,
    ) -> "GitLedger":
        if path.exists():
            raise GitError(f"ledger path already exists: {path}")
        _command(["git", "init", "--bare", f"--object-format={object_format}", str(path)])
        ledger = cls(
            path,
            signing_key_path=signing_key_path,
            allowed_signers_path=allowed_signers_path,
        )
        ledger.configure_signing()
        ledger._git(["symbolic-ref", "HEAD", "refs/heads/main"])
        if ledger.object_format() != object_format:
            raise GitError("initialized ledger object format does not match request")
        return ledger

    def configure_signing(self) -> None:
        settings = {
            "user.name": "cruxible-daemon",
            "user.email": "daemon@cruxible.invalid",
            "gpg.format": "ssh",
            "user.signingkey": str(self._signing_key_path),
            "commit.gpgsign": "true",
            "core.fsync": "committed,reference",
            "core.fsyncMethod": "fsync",
        }
        for name, value in settings.items():
            self._git(["config", name, value])

    def object_format(self) -> GitObjectFormat:
        if self._object_format_cache is not None:
            return self._object_format_cache
        config = configparser.ConfigParser(interpolation=None, strict=False)
        if not config.read(self.path / "config"):
            value = self._git(["rev-parse", "--show-object-format"]).decode().strip()
        else:
            value = config.get("extensions", "objectformat", fallback="sha1")
        if value not in {"sha1", "sha256"}:
            raise GitError(f"unsupported Git object format: {value!r}")
        self._object_format_cache = cast(GitObjectFormat, value)
        return self._object_format_cache

    @_pack_transaction
    def create_signed_genesis(
        self,
        tree: Mapping[str, bytes],
        *,
        timestamp: str,
    ) -> str:
        """Create one signed no-parent commit from exact normalized tree bytes."""

        tree_oid = self._write_tree(
            tree,
            collision_message="genesis paths collide after normalization",
        )

        commit_environment = {
            "GIT_AUTHOR_NAME": "cruxible-daemon",
            "GIT_AUTHOR_EMAIL": "daemon@cruxible.invalid",
            "GIT_COMMITTER_NAME": "cruxible-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@cruxible.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        oid = self._create_commit(
            tree_oid, (), "Initialize Cruxible instance", commit_environment, signed=True
        )
        self._validate_oid(oid)
        if self.parent_of(oid) is not None:
            raise GitError("genesis commit unexpectedly has a parent")
        if not self.verify_commit(oid):
            raise GitError("new genesis commit signature does not verify")
        _batch_reader(self.path)._transaction.retain = True
        return oid

    @_pack_transaction
    def create_signed_generation(
        self,
        tree: Mapping[str, bytes],
        *,
        parent_oid: str,
        sequence: int,
        timestamp: str,
        message: str,
        accepting_admission: AcceptingAdmission | None = None,
        extends_tree: str | None = None,
        extends_rows: Mapping[str, bytes] | None = None,
    ) -> str:
        """Create one signed, still-unsettled generation commit over an exact parent.

        ``extends_tree`` names a stored tree whose members ``tree`` carries
        unchanged, as a settled proposal's tree is carried into its generation.
        Only the members it lacks are written; the caller's readback of the
        stored generation still compares every member. ``extends_rows`` is the
        caller's proven tree at ``extends_tree``, when it holds one.
        """

        self._validate_oid(parent_oid)
        if sequence < 1:
            raise GitError("non-genesis generation sequence must be positive")
        _validate_commit_message(message)
        tree_oid = (
            self._write_tree(tree, accepted_parent=parent_oid)
            if extends_tree is None
            else self._extend_tree(extends_tree, tree, base_rows=extends_rows)
        )
        environment = {
            "GIT_AUTHOR_NAME": "cruxible-daemon",
            "GIT_AUTHOR_EMAIL": "daemon@cruxible.invalid",
            "GIT_COMMITTER_NAME": "cruxible-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@cruxible.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        # Marked before the commit exists, so a crash at any later point leaves
        # a marker for recovery's collection of unsettled generations. The
        # attempt keeps recovery from treating the marker or commit as residue
        # while this writer is live; callers that go on to activate hold one
        # across both.
        with self.generation_attempt():
            return self._commit_generation(
                tree_oid, parent_oid, message, environment, accepting_admission=accepting_admission
            )

    def _commit_generation(
        self,
        tree_oid: str,
        parent_oid: str,
        message: str,
        environment: Mapping[str, str],
        *,
        accepting_admission: AcceptingAdmission | None = None,
    ) -> str:
        pending = self._mark_generation_in_flight(f"pending-{secrets.token_hex(8)}")
        oid = self._create_commit(
            tree_oid,
            (parent_oid,),
            message,
            environment,
            signed=True,
            accepting_admission=accepting_admission,
        )
        self._validate_oid(oid)
        self._mark_generation_in_flight(oid)
        _batch_reader(self.path)._transaction.retain = True
        pending.unlink()
        _fsync_directory(pending.parent)
        if self.parent_of(oid) != parent_oid:
            raise GitError("new generation commit parent differs from settlement base")
        if not self.verify_commit(oid):
            raise GitError("new generation commit signature does not verify")
        return oid

    def _create_commit(
        self,
        tree_oid: str,
        parents: Sequence[str],
        message: str,
        environment: Mapping[str, str],
        *,
        signed: bool = False,
        accepting_admission: AcceptingAdmission | None = None,
    ) -> str:
        timestamp = environment["GIT_AUTHOR_DATE"]
        try:
            instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.year < 1970:
                raise ValueError("commit timestamp must have a timezone")
            date = f"{int(instant.timestamp())} {instant.strftime('%z')}"
        except ValueError as exc:
            raise GitError("invalid Git commit timestamp") from exc
        headers = [f"tree {tree_oid}", *(f"parent {parent}" for parent in parents)]
        for role in ("AUTHOR", "COMMITTER"):
            name, email = environment[f"GIT_{role}_NAME"], environment[f"GIT_{role}_EMAIL"]
            if any(char in name + email for char in "<>\n\x00"):
                raise GitError("invalid Git commit identity")
            headers.append(f"{role.lower()} {name} <{email}> {date}")
        encoding = self.review_commit_context().decode("utf-8").strip() or "UTF-8"
        if encoding.lower() not in {"utf8", "utf-8"}:
            headers.append(f"encoding {encoding}")
        if accepting_admission is not None:
            headers.append("cruxible-accepting-admission " + accepting_admission.header_value())
        body = ("\n".join(headers) + "\n\n" + message).encode("utf-8")
        if not body.endswith(b"\n"):
            body += b"\n"
        if signed:
            try:
                key = load_private(self._signing_key_path.read_bytes())
                body = sign_commit(body, key, sha256=self.object_format() == "sha256")
            except (OSError, ValueError) as exc:
                raise GitError("daemon commit signing failed") from exc
        oid = self._object_oid("commit", body)
        self._store_objects({oid: ("commit", body)})
        repository = _repository_key(self.path)
        for sized in (True, False):
            listing = _remembered_listing(repository, tree_oid, with_sizes=sized)
            if listing is not None:
                _remember_listing(repository, oid, listing)
        return oid

    def _blob_oid(self, content: bytes) -> str:
        """Compute Git's own content address for one blob without spawning Git.

        A blob's object ID is the repository hash of ``blob <size>\\0`` followed
        by the exact bytes. Deriving it in process is what lets a tree write ask
        Git to store only the members it does not already hold, instead of
        paying one `hash-object` process per member of the whole tree. Git still
        confirms the address of every object this writes.
        """

        header = f"blob {len(content)}".encode("ascii") + b"\x00"
        if self.object_format() == "sha1":
            return hashlib.sha1(header + content).hexdigest()  # noqa: S324
        return hashlib.sha256(header + content).hexdigest()

    def _object_oid(self, kind: str, body: bytes) -> str:
        header = f"{kind} {len(body)}".encode("ascii") + b"\x00"
        if self.object_format() == "sha1":
            return hashlib.sha1(header + body).hexdigest()  # noqa: S324 - Git identity
        return hashlib.sha256(header + body).hexdigest()

    def _store_objects(self, objects: Mapping[str, tuple[str, bytes]]) -> None:
        """Publish one in-process pack/index, with no per-object file operations."""
        reader = _batch_reader(self.path)
        pending = getattr(reader._transaction, "pending", None)
        if pending is None:
            reader.store.write(objects)
        else:
            sealed: set[str] = getattr(reader._transaction, "sealed", set())
            pending.update((oid, value) for oid, value in objects.items() if oid not in sealed)

    def _tree_entries_of(self, tree_oid: str) -> dict[str, tuple[bytes, str]]:
        found = _batch_reader(self.path).objects((tree_oid,))[tree_oid]
        if found is None or found[0] != "tree":
            raise GitError(f"ledger tree object is unavailable: {tree_oid}")
        raw_length = 20 if self.object_format() == "sha1" else 32
        entries = _tree_entries(found[1], raw_length=raw_length)
        if entries is None:
            raise GitError(f"ledger tree object is malformed: {tree_oid}")
        return entries

    def _encoded_tree(self, oid: str | None) -> _EncodedTree:
        if oid is None:
            return _EncodedTree(PersistentMap(), (), ())
        with _ENCODED_TREES_LOCK:
            cached = _ENCODED_TREES.get(oid)
            if cached is not None:
                _ENCODED_TREES.move_to_end(oid)
                return cached
        entries = self._tree_entries_of(oid)
        ordered = sorted(entries, key=lambda name: _tree_entry_key(name, entries[name][0]))
        result = _EncodedTree(
            PersistentMap(entries),
            tuple(_tree_entry_key(name, entries[name][0]) for name in ordered),
            tuple(_encode_tree_entry(name, *entries[name]) for name in ordered),
        )
        _remember_encoded_tree(oid, result)
        return result

    def _apply_to_tree(
        self,
        tree_oid: str | None,
        changes: Mapping[str, str | None],
        written: dict[str, tuple[str, bytes]],
    ) -> str | None:
        """The tree ``tree_oid`` with ``changes`` applied: blob ID, or None to remove.

        Only the directories on a changed path are read and rewritten; every
        other subtree keeps its object ID. Entries sort as Git sorts them (a
        directory compares as its name plus ``/``), modes are Git's ``100644``
        and ``40000``, and a directory left empty disappears, as ``write-tree``
        omits it. Returns None for an empty tree.
        """

        listing = self._encoded_tree(tree_oid)
        entries = MapMutation(listing.entries)
        changed: set[str] = set()
        files: dict[str, str | None] = {}
        subtrees: dict[str, dict[str, str | None]] = {}
        for path, oid in changes.items():
            head, separator, rest = path.partition("/")
            if separator:
                subtrees.setdefault(head, {})[rest] = oid
            else:
                files[head] = oid
        # Judge conflicts on the final tree: removed files go first, then each
        # changed directory, then added files, so a path may turn from a file
        # into a directory (or back) within one change.
        for name, oid in files.items():
            changed.add(name)
            current = entries.get(name)
            if oid is None and current is not None and current[0] != b"40000":
                entries.pop(name)
        for name, nested in subtrees.items():
            changed.add(name)
            current = entries.get(name)
            if current is not None and current[0] != b"40000":
                raise GitError(f"ledger path is both a file and a directory: {name}")
            child = self._apply_to_tree(None if current is None else current[1], nested, written)
            if child is None:
                entries.pop(name, None)
            else:
                entries[name] = (b"40000", child)
        for name, oid in files.items():
            changed.add(name)
            if oid is None:
                continue
            current = entries.get(name)
            if current is not None and current[0] == b"40000":
                raise GitError(f"ledger path is both a file and a directory: {name}")
            entries[name] = (b"100644", oid)
        if not entries:
            return None
        keys, encoded = list(listing.keys), list(listing.encoded)
        for name in changed:
            old = listing.entries.get(name)
            if old is not None:
                key = _tree_entry_key(name, old[0])
                index = bisect.bisect_left(keys, key)
                del keys[index]
                del encoded[index]
            new = entries.get(name)
            if new is not None:
                index = bisect.bisect_left(keys, _tree_entry_key(name, new[0]))
                keys.insert(index, _tree_entry_key(name, new[0]))
                encoded.insert(index, _encode_tree_entry(name, *new))
        body = b"".join(encoded)
        oid = self._object_oid("tree", body)
        written[oid] = ("tree", body)
        # Transfer needs the same change record as tree encoding. Never derive
        # this frontier later by comparing or visiting the unchanged siblings.
        children = tuple(
            entry[1]
            for name in changed
            if (entry := entries.get(name)) is not None and entry[0] != b"160000"
        )
        frontier = _TreeTransferFrontier(tree_oid, children) if oid != tree_oid else None
        _remember_encoded_tree(
            oid, _EncodedTree(entries.finish(), tuple(keys), tuple(encoded), frontier)
        )
        return oid

    def _commit_changes_to_tree(
        self,
        base_tree: str | None,
        changes: Mapping[str, str | None],
        blobs: Mapping[str, bytes],
    ) -> str:
        """Store new blobs and the rewritten trees; return the new root tree ID."""

        written: dict[str, tuple[str, bytes]] = {
            oid: ("blob", content) for oid, content in blobs.items()
        }
        root = self._apply_to_tree(base_tree, changes, written)
        if root is None:
            body = b""
            root = self._object_oid("tree", body)
            written[root] = ("tree", body)
        self._store_objects(written)
        return root

    @_pack_transaction
    def _write_tree(
        self,
        tree: Mapping[str, bytes],
        *,
        collision_message: str = "generation paths collide after normalization",
        accepted_parent: str | None = None,
    ) -> str:
        """Write one exact normalized tree, storing only its not-yet-held members.

        ``accepted_parent`` names an accepted commit this tree descends from. Its
        blobs stay reachable from accepted history, so only members it does not
        already carry need the batched existence check.

        Successive accepted trees differ in a handful of members, so hashing and
        re-storing every member would make each write cost O(members) Git
        processes for bytes the repository already holds. Blob addresses are
        computed in process; only changed members' blobs and the directories on
        their paths are written in a pack, with no Git process. The
        resulting tree object ID is byte-for-byte the one a member-by-member
        write produces.
        """

        if accepted_parent is not None:
            delta_oid = self._write_tree_delta(tree, accepted_parent=accepted_parent)
            if delta_oid is not None:
                return delta_oid
        normalized_to_raw: dict[str, str] = {}
        for raw_path in tree:
            normalized = normalize_manifest_paths([raw_path])[0]
            if normalized in normalized_to_raw:
                raise GitError(collision_message)
            normalized_to_raw[normalized] = raw_path

        ordered = normalize_manifest_paths(list(tree))
        # A snapshot row that is a blob reference already names its object and
        # size; only rows held as bytes are hashed, and only staged bytes read.
        rows = row_values(tree)
        oids: dict[str, str] = {}
        sizes: dict[str, int] = {}
        for path in ordered:
            row = rows[normalized_to_raw[path]] if rows is not None else None
            if isinstance(row, BlobRef):
                oids[path], sizes[path] = row.oid, row.size
            else:
                content = tree[normalized_to_raw[path]] if row is None else row
                oids[path], sizes[path] = self._blob_oid(content), len(content)
        held: frozenset[str] = frozenset()
        parent_entries: dict[str, GitTreeEntry] = {}
        if accepted_parent is not None:
            parent_entries = {
                entry.path: entry
                for entry in self._list_tree(accepted_parent, with_sizes=False)
                if entry.object_type == "blob"
            }
            held = frozenset(entry.oid for entry in parent_entries.values())
        # Starting from the parent's own tree keeps every subtree this write
        # leaves alone, so only changed paths and their parent directories are
        # rewritten. The result is the same tree object a from-empty write makes.
        start = None if accepted_parent is None else self._commit_tree(accepted_parent)
        staged = (
            ordered
            if start is None
            else [
                path
                for path in ordered
                if (entry := parent_entries.get(path)) is None
                or entry.oid != oids[path]
                or entry.mode != "100644"
            ]
        )
        removed = [] if start is None else [path for path in parent_entries if path not in oids]
        blobs: dict[str, bytes] = {}
        for path in staged:
            blob_oid = oids[path]
            if blob_oid in held:
                continue
            content = tree[normalized_to_raw[path]]
            if blob_oid in blobs and blobs[blob_oid] != content:
                raise GitError("different blob bytes share a computed content address")
            blobs[blob_oid] = content
        oid = self._commit_changes_to_tree(
            start,
            {**{path: oids[path] for path in staged}, **{path: None for path in removed}},
            blobs,
        )
        self._validate_oid(oid)
        # This process just wrote every entry of this tree: record its listing so
        # the readers that follow (projection assembly) need not re-list it.
        listing = tuple(
            GitTreeEntry(
                path=path,
                mode="100644",
                object_type="blob",
                oid=oids[path],
                size=sizes[path],
            )
            for path in ordered
        )
        _remember_listing(_repository_key(self.path), oid, listing)
        return oid

    def _write_tree_delta(self, tree: Mapping[str, bytes], *, accepted_parent: str) -> str | None:
        """``_write_tree`` for a fork of the accepted root ``accepted_parent`` names.

        The fork is that root's rows plus its edits, so the root's carried path
        facts answer the normalization checks and only edited paths are staged.
        None when ``tree`` is not such a fork, or when a path check fails and the
        whole-tree write must report it.
        """

        found = root_and_edits(tree)
        rows = row_values(tree)
        if found is None or rows is None or found[0]._commit_oid != accepted_parent:
            return None
        root, edits = found
        facts = path_facts(root).advanced(root._rows, edits)
        if facts.noncanonical or facts.colliding:
            return None
        changes: dict[str, str | None] = {}
        sizes: dict[str, int] = {}
        blobs: dict[str, bytes] = {}
        for path in sorted(edits, key=lambda item: item.encode("utf-8")):
            new, old = rows.get(path), root._rows.get(path)
            if new is None:
                if old is not None:
                    changes[path] = None
                continue
            if old is not None and same_row(new, old):
                continue
            if isinstance(new, BlobRef):
                changes[path], sizes[path] = new.oid, new.size
                continue
            blob_oid = self._blob_oid(new)
            if blob_oid in blobs and blobs[blob_oid] != new:
                raise GitError("different blob bytes share a computed content address")
            blobs[blob_oid] = new
            changes[path], sizes[path] = blob_oid, len(new)
        oid = self._commit_changes_to_tree(self._commit_tree(accepted_parent), changes, blobs)
        self._validate_oid(oid)
        repository = _repository_key(self.path)
        parent_listing = _remembered_listing(repository, accepted_parent, with_sizes=True)
        if parent_listing is None:
            # The root's rows are the accepted tree's blob references, with the
            # object ID and size a sized listing carries, in the same order.
            parent_listing = tuple(
                GitTreeEntry(
                    path=path,
                    mode="100644",
                    object_type="blob",
                    oid=row.oid if isinstance(row, BlobRef) else self._blob_oid(row),
                    size=row.size if isinstance(row, BlobRef) else len(row),
                )
                for path, row in root._rows.items()
            )
            if parent_listing:
                _remember_listing(repository, accepted_parent, parent_listing)
        _remember_listing(repository, oid, _listing_with(parent_listing, changes, sizes))
        return oid

    @_pack_transaction
    def _extend_tree(
        self,
        base_tree: str,
        tree: Mapping[str, bytes],
        *,
        base_rows: Mapping[str, bytes] | None = None,
    ) -> str:
        """Write ``tree`` as ``base_tree`` plus the members ``base_tree`` lacks.

        Only the added paths' blobs and their parent directories are written;
        every untouched subtree keeps its object ID, so the cost follows the
        added members rather than the size of the whole tree. ``base_rows``,
        when the caller holds the proven tree at ``base_tree`` as a fork of the
        root ``tree`` descends from, lets the added members follow from the two
        trees' edits instead of from a listing of every member.
        """

        self._validate_oid(base_tree)
        _batch_reader(self.path).import_pending_pack(base_tree)
        shared = None if base_rows is None else edited_paths(tree, base_rows)
        base: _Listing | None = None
        if base_rows is not None and shared is not None:
            if any(path in base_rows and path not in tree for path in shared):
                raise GitError("extended tree does not carry every member of its base")
            added = [path for path in shared if path in tree and path not in base_rows]
        else:
            base = self._list_tree(base_tree, with_sizes=True)
            base_paths = {entry.path for entry in base}
            if any(entry.object_type != "blob" for entry in base) or not base_paths <= set(tree):
                raise GitError("extended tree does not carry every member of its base")
            added = [path for path in tree if path not in base_paths]
        ordered_added = normalize_manifest_paths(added)
        if set(ordered_added) != set(added):
            raise GitError("extended tree adds a path that is not normalized")
        oids = {path: self._blob_oid(tree[path]) for path in ordered_added}
        blobs: dict[str, bytes] = {}
        for path in ordered_added:
            blob_oid = oids[path]
            if blob_oid in blobs and blobs[blob_oid] != tree[path]:
                raise GitError("different blob bytes share a computed content address")
            blobs[blob_oid] = tree[path]
        oid = self._commit_changes_to_tree(base_tree, dict(oids), blobs)
        self._validate_oid(oid)
        repository = _repository_key(self.path)
        if base is None:
            base = _remembered_listing(repository, base_tree, with_sizes=True)
        if base is not None:
            listing = _listing_with(
                base,
                dict(oids),
                {path: len(tree[path]) for path in ordered_added},
            )
            _remember_listing(repository, oid, listing)
        return oid

    @_pack_transaction
    def create_proposal_commit(
        self,
        tree: Mapping[str, bytes],
        *,
        base_oid: str,
        target_ref: str,
        actor_id: str,
        timestamp: str,
        expected_ref_oid: str | None,
        message: str,
    ) -> tuple[str, str]:
        """Write one unsigned proposal commit and CAS only its actor namespace ref."""

        self._validate_oid(base_oid)
        _validate_commit_message(message)
        if not _PROPOSAL_REF_RE.fullmatch(target_ref):
            raise GitError("proposal transport may update only canonical proposal refs")
        actor_namespace = target_ref.split("/")[2]
        if actor_namespace != actor_id:
            raise GitError("proposal ref namespace differs from authenticated actor")
        current = self.read_proposal_ref(target_ref)
        if current != expected_ref_oid:
            raise GitError("proposal ref moved before its parent-bound update")

        tree_oid = self._write_tree(
            tree,
            collision_message="proposal paths collide after normalization",
            accepted_parent=base_oid,
        )

        commit_environment = {
            "GIT_AUTHOR_NAME": actor_id,
            "GIT_AUTHOR_EMAIL": f"{actor_id}@proposal.playbill.invalid",
            "GIT_COMMITTER_NAME": "cruxible-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@cruxible.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        commit_oid = self._create_commit(tree_oid, (base_oid,), message, commit_environment)
        self._validate_oid(commit_oid)
        zero_oid = "0" * (40 if self.object_format() == "sha1" else 64)
        self._update_refs([f"update {target_ref} {commit_oid} {expected_ref_oid or zero_oid}"])
        return commit_oid, tree_oid

    def read_proposal_ref(self, target_ref: str) -> str | None:
        if not _PROPOSAL_REF_RE.fullmatch(target_ref):
            raise GitError("proposal transport may read only canonical proposal refs")
        return self._resolve_ref(target_ref)

    def retain_proposal_review(self, proposal_id: str, oid: str) -> None:
        """Protect a completed active admission independently of its reusable author ref."""
        ref = "refs/heads/proposals/" + proposal_id.removeprefix("sha256:")
        if not _PROPOSAL_REVIEW_REF_RE.fullmatch(ref):
            raise GitError("proposal review ref name is malformed")
        self._validate_oid(oid)
        # Prepare the deterministic retention object in this same request pack.
        # Its ref still publishes only when retirement later removes the review
        # ref; preparing bytes does not change authority/publication order.
        if getattr(_batch_reader(self.path)._transaction, "request", False):
            self._archive_update((oid,))
        self._update_refs([f"update {ref} {oid}"])

    def proposal_refs(self) -> dict[str, str]:
        """Snapshot the current author slots, including interrupted publications."""
        return self._refs("refs/proposals/")

    def _archive_members(self, tip: str) -> PersistentMap[bool]:
        key = (_repository_key(self.path), tip)
        cached = _ARCHIVE_MEMBERS.get(key)
        if cached is not None:
            return cached
        # Cold archive seed only; subsequent heads extend this immutable map
        # from newly retained commits, stopping at already-retained ancestry.
        return self._extend_archive_members(PersistentMap(), (tip,) if tip else ())

    def _extend_archive_members(
        self, known: PersistentMap[bool], roots: Sequence[str]
    ) -> PersistentMap[bool]:
        pending = list(roots)
        while pending:
            oid = pending.pop()
            if oid in known:
                continue
            found = _batch_reader(self.path).objects((oid,))[oid]
            if found is None or found[0] != "commit":
                raise GitError("proposal archive target is not a retained commit")
            known = known.set(oid, True)
            pending.extend(_commit_parents(found[1]))
        return known

    @_pack_transaction
    def _archive_update(self, oids: Sequence[str]) -> list[str]:
        """Build a fixed-tree retention chain; callers CAS its head with ref removal."""
        previous = self._resolve_ref(PROPOSAL_ARCHIVE_REF) or ""
        tip = previous
        known = self._archive_members(previous)
        empty_tree = None
        for oid in sorted(set(oids)):
            self._validate_oid(oid)
            if oid in known:
                continue
            found = _batch_reader(self.path).objects((oid,))[oid]
            if found is None or found[0] != "commit":
                raise GitError("proposal archive target is not a retained commit")
            if empty_tree is None:
                empty_tree = self._object_oid("tree", b"")
                self._store_objects({empty_tree: ("tree", b"")})
            parents = ([tip] if tip else []) + [oid]
            # No growing manifest/tree and no authoring ancestry. The archive is
            # unsigned retention metadata, outside accepted-state authority.
            raw = (
                f"tree {empty_tree}\n"
                + "".join(f"parent {parent}\n" for parent in parents)
                + "author cruxible-daemon <daemon@cruxible.invalid> 0 +0000\n"
                + "committer cruxible-daemon <daemon@cruxible.invalid> 0 +0000\n\n"
                + "Retain closed proposal\n"
            ).encode()
            tip = self._object_oid("commit", raw)
            self._store_objects({tip: ("commit", raw)})
            known = self._extend_archive_members(known, (oid,)).set(tip, True)
            _ARCHIVE_MEMBERS[(_repository_key(self.path), tip)] = known
            while len(_ARCHIVE_MEMBERS) > 16:
                _ARCHIVE_MEMBERS.popitem(last=False)
        if tip == previous:
            # Deleting a duplicate root still relies on this exact archive head.
            return [f"verify {PROPOSAL_ARCHIVE_REF} {previous}"] if oids and previous else []
        return [f"update {PROPOSAL_ARCHIVE_REF} {tip} {previous or '0' * len(tip)}"]

    @_pack_transaction
    def archive_proposal_commits(self, oids: Sequence[str]) -> None:
        """Retain a completed refusal before its author slot can be reused."""
        commands = self._archive_update(oids)
        if commands:
            self._update_refs(commands)

    @_pack_transaction
    def replace_proposal_review_refs(
        self, refs: Mapping[str, str], *, retired_targets: Mapping[str, str] | None = None
    ) -> None:
        """Archive closed candidates atomically with releasing their public/private refs."""
        normalized = {}
        for proposal_id, oid in refs.items():
            ref = f"refs/heads/proposals/{proposal_id}"
            if not _PROPOSAL_REVIEW_REF_RE.fullmatch(ref):
                raise GitError("proposal review ref name is malformed")
            self._validate_oid(oid)
            normalized[ref] = oid
        current = self._refs("refs/heads/proposals/")
        retired = {ref: oid for ref, oid in current.items() if ref not in normalized}
        for ref, oid in (retired_targets or {}).items():
            if not _PROPOSAL_REF_RE.fullmatch(ref):
                raise GitError("proposal ref name is malformed")
            self._validate_oid(oid)
            retired[ref] = oid
        # One local conversion only: fold old per-proposal pins into the same
        # chain before deleting them. Unrelated refs in this namespace refuse.
        for ref, oid in self._refs("refs/settled/").items():
            if ref == PROPOSAL_ARCHIVE_REF:
                continue
            if not _LEGACY_SETTLED_RE.fullmatch(ref):
                raise GitError("unrecognized local proposal archive ref")
            retired[ref] = oid
        commands = self._archive_update(tuple(retired.values()))
        commands.extend(
            f"update {ref} {oid} {current.get(ref, '0' * len(oid))}"
            for ref, oid in sorted(normalized.items())
            if current.get(ref) != oid
        )
        commands.extend(f"delete {ref} {oid}" for ref, oid in sorted(retired.items()))
        if commands:
            self._update_refs(commands)

    def mirror_refs(self) -> dict[str, str]:
        """Capture owned public refs, excluding private proposal/pinning refs."""
        refs = {
            ref: oid
            for ref in (_MIRROR_MAIN, PROPOSAL_ARCHIVE_REF, *NOTE_REFS.values())
            if (oid := self._resolve_ref(ref)) is not None
        }
        for prefix in _MIRROR_PREFIXES:
            refs.update(self._refs(prefix))
        return self._validate_mirror_snapshot(refs)

    @staticmethod
    def _mirror_owned_ref(ref: str) -> bool:
        return (
            ref in (_MIRROR_MAIN, PROPOSAL_ARCHIVE_REF)
            or ref in NOTE_REFS.values()
            or ref.startswith(_MIRROR_PREFIXES)
        )

    def _validate_mirror_snapshot(
        self, refs: Mapping[str, str], *, require_main: bool = True, legacy: bool = False
    ) -> dict[str, str]:
        """Validate a snapshot of owned refs.

        ``legacy`` admits the pre-rename note ref names, for a snapshot this
        ledger recorded or a remote advertised before the rename: publication
        then deletes them under lease. A desired snapshot never carries them.
        """
        if len(refs) > _MIRROR_MAX_REFS:
            raise GitError("mirror snapshot exceeds the ref count limit")
        result = dict(refs)
        for ref, oid in result.items():
            if _LEGACY_SETTLED_RE.fullmatch(ref):
                raise GitError(
                    "mirror state uses retired per-proposal archive refs; "
                    "bind a new mirror URL to start a fresh publication snapshot"
                )
            valid = ref in (_MIRROR_MAIN, PROPOSAL_ARCHIVE_REF) or ref in NOTE_REFS.values()
            valid = valid or (legacy and ref in LEGACY_NOTE_REFS)
            valid = valid or bool(_PROPOSAL_REVIEW_REF_RE.fullmatch(ref))
            if not valid:
                raise GitError("mirror snapshot contains an unowned or malformed ref")
            self._validate_oid(oid)
        if require_main and _MIRROR_MAIN not in result:
            raise GitError("mirror snapshot omits accepted main")
        return result

    @contextmanager
    def _retain_mirror_snapshot(self, snapshot: Mapping[str, str]) -> Iterator[None]:
        """Keep captured objects reachable when live derived refs are replaced."""
        prefix = f"refs/playbill-mirror-pins/{new_id('pin', length=32)}"
        pins = {
            f"{prefix}/{index}": oid for index, oid in enumerate(sorted(set(snapshot.values())))
        }
        commands = [
            "start",
            *(f"create {ref} {oid}" for ref, oid in pins.items()),
            "prepare",
            "commit",
        ]
        self._git(["update-ref", "--stdin"], input_bytes=("\n".join(commands) + "\n").encode())
        try:
            yield
        finally:
            commands = [
                "start",
                *(f"delete {ref} {oid}" for ref, oid in pins.items()),
                "prepare",
                "commit",
            ]
            self._git(["update-ref", "--stdin"], input_bytes=("\n".join(commands) + "\n").encode())

    def push_mirror(
        self,
        url: str,
        *,
        environment: Mapping[str, str] | None = None,
        snapshot: Mapping[str, str] | None = None,
        expected_remote: Mapping[str, str] | None = None,
        previous_attempt: Mapping[str, str] | None = None,
        retired_proposal: Callable[[str, str], bool] | None = None,
    ) -> str | None:
        """Atomically publish exact refs, returning None or an operational failure.

        ``expected_remote`` is the last acknowledged snapshot for this remote.
        ``previous_attempt`` records a possibly acknowledged earlier attempt.
        Known refs may be deleted; matching attempted or desired values permit
        retry after uncertain outcomes. Missing state is recoverable only with
        local ancestry or exact settlement proof. Main always fast-forwards.
        Unknown nonempty remote values otherwise refuse publication. Success
        acknowledges this snapshot, never later local refs. Each remote command
        has a deadline; failure never undoes the already durable local ledger.
        """
        try:
            desired = self._validate_mirror_snapshot(
                self.mirror_refs() if snapshot is None else snapshot
            )
            expected = self._validate_mirror_snapshot(
                {} if expected_remote is None else expected_remote,
                require_main=False,
                legacy=True,
            )
            attempted = self._validate_mirror_snapshot(
                {} if previous_attempt is None else previous_attempt,
                require_main=False,
                legacy=True,
            )
            if PROPOSAL_ARCHIVE_REF not in desired and (
                PROPOSAL_ARCHIVE_REF in expected or PROPOSAL_ARCHIVE_REF in attempted
            ):
                raise GitError("local proposal archive is missing; restore it before publication")
            owned = set(desired) | set(expected) | set(attempted)
            # Refuse rather than split the atomic update across commands.
            planned = [f"{desired.get(ref, '')}:{ref}" for ref in owned]
            planned += [f"--force-with-lease={ref}:{'0' * 64}" for ref in owned]
            if (
                sum(len(arg.encode()) + 1 for arg in [url, str(self.path), *planned])
                > _MIRROR_ARG_BYTES
            ):
                raise GitError("mirror snapshot exceeds the atomic push argument limit")
            with self._retain_mirror_snapshot(desired):
                result = _command(
                    [
                        "git",
                        f"--git-dir={self.path}",
                        "ls-remote",
                        "--refs",
                        "--",
                        url,
                        _MIRROR_MAIN,
                        *NOTE_REFS.values(),
                        *LEGACY_NOTE_REFS,
                        "refs/settled/*",
                        *(prefix + "*" for prefix in _MIRROR_PREFIXES),
                    ],
                    environment=environment,
                    check=False,
                    timeout=MIRROR_PUSH_TIMEOUT_SECONDS,
                )
                if result.returncode != 0:
                    return self._mirror_failure(result, "ls-remote")
                remote: dict[str, str] = {}
                for line in result.stdout.decode("utf-8").splitlines():
                    oid, separator, ref = line.partition("\t")
                    if _LEGACY_SETTLED_RE.fullmatch(ref):
                        raise GitError(
                            "remote mirror still has per-proposal archive refs; "
                            "bind a new mirror URL or explicitly migrate/reset this remote"
                        )
                    if (
                        not separator
                        or not (self._mirror_owned_ref(ref) or ref in LEGACY_NOTE_REFS)
                        or ref in remote
                    ):
                        raise GitError("remote mirror advertisement is malformed")
                    remote[ref] = oid
                remote = self._validate_mirror_snapshot(remote, require_main=False, legacy=True)
                for ref in set(remote) | owned:
                    actual = remote.get(ref)
                    renamed = LEGACY_NOTE_REFS.get(ref)
                    if renamed is not None:
                        # A pre-rename note ref is deleted, never published:
                        # under lease when this ledger recorded it, else only
                        # when its history is already under the renamed ref.
                        if actual is not None and actual not in (
                            expected.get(ref),
                            attempted.get(ref),
                        ):
                            target = desired.get(renamed)
                            if target is None or not self.is_ancestor(actual, target):
                                raise GitError(f"remote mirror ref diverged: {ref}")
                        if actual is None:
                            owned.discard(ref)
                        else:
                            owned.add(ref)
                        continue
                    # An empty/recreated remote has no conflicting history.
                    if actual is None or actual in (
                        desired.get(ref),
                        expected.get(ref),
                        attempted.get(ref),
                    ):
                        continue
                    # A successful-but-unacknowledged push may be older than
                    # the retained attempt after a crash. These local proofs
                    # are safe independently of operational snapshot retention.
                    target = desired.get(ref)
                    if target is not None and self.is_ancestor(actual, target):
                        continue
                    if (
                        ref.startswith("refs/heads/proposals/")
                        and ref not in desired
                        and retired_proposal is not None
                        and retired_proposal(ref, actual)
                        and PROPOSAL_ARCHIVE_REF in desired
                        and self.is_ancestor(actual, desired[PROPOSAL_ARCHIVE_REF])
                    ):
                        owned.add(ref)
                        continue
                    raise GitError(f"remote mirror ref diverged: {ref}")
                remote_archive = remote.get(PROPOSAL_ARCHIVE_REF)
                if remote_archive is not None and (
                    PROPOSAL_ARCHIVE_REF not in desired
                    or not self.is_ancestor(remote_archive, desired[PROPOSAL_ARCHIVE_REF])
                ):
                    raise GitError(
                        "proposal archive cannot be deleted or rewound; "
                        "restore its retained history"
                    )
                remote_main = remote.get(_MIRROR_MAIN)
                if remote_main is not None and remote_main != desired[_MIRROR_MAIN]:
                    if not self.is_ancestor(remote_main, desired[_MIRROR_MAIN]):
                        raise GitError("remote accepted main is not an ancestor of the snapshot")
                leases: list[str] = []
                refspecs: list[str] = []
                for ref in sorted(owned, key=str.encode):
                    if ref != _MIRROR_MAIN:
                        leases.append(f"--force-with-lease={ref}:{remote.get(ref, '')}")
                    refspecs.append(f"{desired.get(ref, '')}:{ref}")
                if (
                    sum(len(arg.encode()) + 1 for arg in [url, str(self.path), *leases, *refspecs])
                    > _MIRROR_ARG_BYTES
                ):
                    raise GitError("mirror snapshot exceeds the atomic push argument limit")
                result = _command(
                    [
                        "git",
                        f"--git-dir={self.path}",
                        "push",
                        "--atomic",
                        "--porcelain",
                        *leases,
                        "--",
                        url,
                        *refspecs,
                    ],
                    environment=environment,
                    check=False,
                    timeout=MIRROR_PUSH_TIMEOUT_SECONDS,
                )
                return None if result.returncode == 0 else self._mirror_failure(result, "push")
        except subprocess.TimeoutExpired:
            return (
                f"mirror transport did not finish within {MIRROR_PUSH_TIMEOUT_SECONDS:g}s "
                "and was killed; local state is durable, remote publication is unconfirmed"
            )
        except (GitError, OSError, UnicodeError) as exc:
            return str(exc)[:500]

    @staticmethod
    def _mirror_failure(result: subprocess.CompletedProcess[bytes], operation: str) -> str:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return (detail or f"git {operation} exited {result.returncode}")[:500]

    def _refs(self, prefix: str) -> dict[str, str]:
        """Enumerate a requested ref namespace, including packed refs (maintenance)."""
        packed = self.path / "packed-refs"
        identity = _file_identity(packed)
        key = (_repository_key(self.path), identity, prefix)
        with _PACKED_REFS_LOCK:
            cached = _PACKED_PREFIX_REFS.get(key)
            if cached is not None:
                _PACKED_PREFIX_REFS.move_to_end(key)
        if cached is None:
            cached = {}
            if identity is not None:
                # Cold packed-ref version only. Ordinary reconciliation never
                # parses an unchanged whole-repository packed table again.
                for line in packed.read_text().splitlines():
                    if line and not line.startswith(("#", "^")):
                        oid, ref = line.split(" ", 1)
                        if ref.startswith(prefix):
                            cached[ref] = oid
            if _file_identity(packed) == identity:
                with _PACKED_REFS_LOCK:
                    _PACKED_PREFIX_REFS[key] = cached
                    while len(_PACKED_PREFIX_REFS) > 1 and (
                        len(_PACKED_PREFIX_REFS) > 64
                        or sum(len(refs) for refs in _PACKED_PREFIX_REFS.values()) > 400_000
                    ):
                        _PACKED_PREFIX_REFS.popitem(last=False)
        result = dict(cached)
        root = self.path / prefix.rstrip("/")
        paths = root.rglob("*") if root.is_dir() else (root,)
        for path in paths:
            if path.is_file() and not path.name.endswith(".lock"):
                ref = path.relative_to(self.path).as_posix()
                resolved = self._resolve_ref(ref)
                if resolved is not None:
                    result[ref] = resolved
        return result

    @contextmanager
    def request_pack(self) -> Iterator[None]:
        """Stage advisory publications together; acceptance seals one durable pack."""
        with _batch_reader(self.path).request():
            yield

    @_pack_transaction
    def _update_refs(self, commands: Sequence[str]) -> None:
        """Git files-backend lock/verify/rename transaction, with packed deletes.

        All leases are checked while every changed ref is locked, before any
        publication. A failure before publication leaves refs untouched. Git's
        files backend also publishes a multi-ref transaction by successive
        renames; the archive is published before any retired root is removed.
        """
        reader = _batch_reader(self.path)
        deferred = getattr(reader._transaction, "request", False)

        def sync_directory(path: Path) -> None:
            if deferred:
                reader._transaction.directories.add(path)
            else:
                _fsync_directory(path)

        def sync_file(fd: int, path: Path) -> None:
            if deferred:
                reader._transaction.files.add(path)
            else:
                os.fsync(fd)

        parsed: dict[str, tuple[str, str | None, str | None]] = {}
        for command in commands:
            fields = command.split()
            operation, ref = fields[:2]
            if (
                not _SAFE_REF_RE.fullmatch(ref)
                or ".." in ref
                or ref.endswith(".lock")
                or any(part.startswith(".") for part in ref.split("/"))
            ):
                raise GitError("unsafe Git reference")
            if operation == "update":
                new, expected = fields[2], fields[3] if len(fields) > 3 else None
                self._validate_oid(new)
            elif operation in ("delete", "verify"):
                new, expected = None, fields[2] if len(fields) > 2 else None
            else:
                raise GitError("unsupported ref transaction command")
            if ref in parsed:
                raise GitError("duplicate ref in transaction")
            parsed[ref] = (operation, new, expected)
        main_oid = parsed.get("refs/heads/main", (None, None, None))[1]
        if main_oid is not None:
            # A bundle may have been prepared in an earlier advisory request.
            # Import its change-sized pack before sealing, even when this CAS
            # creates no objects of its own (including outside request_pack).
            reader.import_pending_pack(main_oid)
        reader.flush()
        locks: dict[str, tuple[Path, int]] = {}
        published: set[str] = set()
        packed_published = False
        packed_lock: Path | None = None
        packed_fd: int | None = None
        try:
            for ref in sorted(parsed):
                path = self.path / ref
                missing: list[Path] = []
                parent = path.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                path.parent.mkdir(parents=True, exist_ok=True)
                for created in reversed(missing):
                    sync_directory(created.parent)
                lock = path.with_name(path.name + ".lock")
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                locks[ref] = (lock, fd)
            for ref, (_operation, new, expected) in parsed.items():
                current = self._resolve_ref(ref)
                if expected is not None and current != (
                    None if set(expected) == {"0"} else expected
                ):
                    raise GitError(f"Git ref compare-and-set failed: {ref}")
                if new is not None and not self.object_exists(new):
                    raise GitError("Git ref target object is unavailable")
            deletions = {ref for ref, value in parsed.items() if value[0] == "delete"}
            packed = self.path / "packed-refs"
            if deletions and packed.exists():
                packed_lock = self.path / "packed-refs.lock"
                packed_fd = os.open(packed_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                retained = []
                keep = True
                # Packed refs are one frozen file. Removing a packed entry
                # necessarily rewrites that file, only during retirement.
                for line in packed.read_bytes().splitlines(keepends=True):
                    if line.startswith(b"^"):
                        if keep:
                            retained.append(line)
                        continue
                    keep = (
                        line.startswith(b"#")
                        or not line.strip()
                        or (line.split(b" ", 1)[-1].strip().decode() not in deletions)
                    )
                    if keep:
                        retained.append(line)
                with os.fdopen(packed_fd, "wb", closefd=False) as stream:
                    stream.write(b"".join(retained))
                    stream.flush()
                    sync_file(packed_fd, packed)
            for ref, (_operation, new, _expected) in parsed.items():
                if new is not None:
                    _lock, fd = locks[ref]
                    os.write(fd, (new + "\n").encode("ascii"))
                    sync_file(fd, self.path / ref)
            if main_oid is not None:
                # Authority must never name advisory bytes, even if publication
                # is interrupted before the activation lock's final ref sync.
                reader.flush(final=True, accepting=True)
            # Publish replacement roots before deletion; leases already proved.
            for ref, (operation, new, _expected) in parsed.items():
                if new is not None:
                    os.replace(locks[ref][0], self.path / ref)
                    published.add(ref)
                    sync_directory((self.path / ref).parent)
            if packed_fd is not None and packed_lock is not None:
                os.replace(packed_lock, packed)
                packed_published = True
                sync_directory(self.path)
            for ref, (operation, _new, _expected) in parsed.items():
                if operation == "delete":
                    (self.path / ref).unlink(missing_ok=True)
                    sync_directory((self.path / ref).parent)
            if deferred:
                if main_oid is not None:
                    reader._transaction.accepted = True
            else:
                for _operation, new, _expected in parsed.values():
                    if new is not None:
                        reader.store.release_oid(new)
                reader.release_keeps()
        except OSError as exc:
            raise GitError(f"Git ref transaction failed: {exc}") from exc
        finally:
            for ref, (lock, fd) in locks.items():
                os.close(fd)
                # Rename consumed our pathname. A successor may already own a
                # new lock there; descriptor cleanup never implies path ownership.
                if ref not in published:
                    lock.unlink(missing_ok=True)
            if packed_fd is not None:
                os.close(packed_fd)
                assert packed_lock is not None
                if not packed_published:
                    packed_lock.unlink(missing_ok=True)

    def _resolve_ref(self, ref: str) -> str | None:
        """One full ref name's object ID, or None when the ref does not exist.

        The files backend is read directly: a loose ref file, else the
        packed-refs table. Git replaces both only by renaming a complete lock
        file, so a read sees the old or the new value, never a torn one.
        Anything this reader does not model -- a symbolic ref, the reftable
        backend, an unexpected file -- is answered by Git itself.
        """

        found = _files_backend_ref(self.path, ref)
        if found is None or isinstance(found, str):
            if found is not None:
                self._validate_oid(found)
            return found
        result = _command(
            ["git", f"--git-dir={self.path}", "rev-parse", "--verify", "--quiet", ref],
            check=False,
        )
        if result.returncode != 0:
            return None
        oid = result.stdout.decode().strip()
        self._validate_oid(oid)
        return oid

    def _ref_exists(self, ref: str) -> bool:
        return self._resolve_ref(ref) is not None

    @staticmethod
    def _review_commit_environment(actor_id: str, timestamp: str) -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": actor_id,
            "GIT_AUTHOR_EMAIL": f"{actor_id}@proposal.playbill.invalid",
            "GIT_COMMITTER_NAME": "cruxible-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@cruxible.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }

    def review_commit_context(self) -> bytes:
        """Read the mutable Git configuration that affects review commit bytes."""
        raw = (self.path / "config").read_text()
        if _CONFIG_INCLUDE_RE.search(raw.encode()) is not None:
            return self._config_read(
                ["config", "--default", "UTF-8", "--get", "i18n.commitencoding"]
            )
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read_string(raw)
        return (config.get("i18n", "commitencoding", fallback="UTF-8") + "\n").encode()

    def _review_commit_identities(self, actor_id: str, timestamp: str) -> dict[str, str]:
        """Git's author/committer identity lines and commit encoding for a review commit.

        For a plain actor id and the canonical UTC timestamp, Git's ident
        normalization changes nothing and its date parser yields the whole
        seconds at ``+0000``, so the lines are formed here without a Git
        process per proposal. Anything else is left to ``git var -l``.
        """

        if any(char in actor_id for char in "<>\n\x00") or not actor_id.strip():
            raise GitError("invalid Git review actor")
        try:
            instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.year < 1970:
                raise ValueError("timestamp must have timezone")
        except ValueError as exc:
            raise GitError("invalid Git review timestamp") from exc
        date = f"{int(instant.timestamp())} {instant.strftime('%z')}"
        return {
            "GIT_AUTHOR_IDENT": f"{actor_id} <{actor_id}@proposal.playbill.invalid> {date}",
            "GIT_COMMITTER_IDENT": f"cruxible-daemon <daemon@cruxible.invalid> {date}",
            "i18n.commitencoding": self.review_commit_context().decode().strip() or "UTF-8",
        }

    def proposal_review_commit_oid(
        self, *, tree_oid: str, base_oid: str, actor_id: str, timestamp: str, message: str
    ) -> str:
        """Derive Git's unsigned review representation without creating objects.

        This is a rebuildable Git address, never a new accepted digest rule.
        Git itself normalizes/refuses identities and dates. Materialization
        verifies the resulting address against commit-tree before retaining it.
        """
        self._validate_oid(tree_oid)
        self._validate_oid(base_oid)
        _validate_commit_message(message)
        values = self._review_commit_identities(actor_id, timestamp)
        author = values.get("GIT_AUTHOR_IDENT")
        committer = values.get("GIT_COMMITTER_IDENT")
        if author is None or committer is None:
            raise GitError("Git omitted review commit identities")
        headers = [
            f"tree {tree_oid}",
            f"parent {base_oid}",
            f"author {author}",
            f"committer {committer}",
        ]
        encoding = values.get("i18n.commitencoding", "UTF-8")
        if encoding.lower() not in {"utf-8", "utf8"}:
            headers.append(f"encoding {encoding}")
        body = ("\n".join(headers) + "\n\n" + message).encode("utf-8")
        if not body.endswith(b"\n"):
            body += b"\n"
        preimage = f"commit {len(body)}".encode("ascii") + b"\x00" + body
        if self.object_format() == "sha1":
            return hashlib.sha1(preimage).hexdigest()  # noqa: S324 - Git object identity
        return hashlib.sha256(preimage).hexdigest()

    @_pack_transaction
    def proposal_review_commit(
        self,
        *,
        tree_oid: str,
        base_oid: str,
        actor_id: str,
        timestamp: str,
        message: str,
    ) -> str:
        """Reproduce the evaluated proposal commit used only by advisory review refs."""

        self._validate_oid(tree_oid)
        self._validate_oid(base_oid)
        _validate_commit_message(message)
        environment = self._review_commit_environment(actor_id, timestamp)
        oid = self._create_commit(tree_oid, (base_oid,), message, environment)
        self._validate_oid(oid)
        expected = self.proposal_review_commit_oid(
            tree_oid=tree_oid,
            base_oid=base_oid,
            actor_id=actor_id,
            timestamp=timestamp,
            message=message,
        )
        if oid != expected:
            raise GitError("Git review commit differs from its derived representation")
        if self.tree_oid(oid) != tree_oid or self.parent_of(oid) != base_oid:
            raise GitError("proposal review commit does not reproduce its evidence")
        return oid

    def read_review_projection(
        self,
        oids: Sequence[str],
        *,
        dependencies: Mapping[str, str],
    ) -> tuple[dict[str, bool], dict[tuple[str, str], bytes | None]]:
        """Read one fresh review snapshot while the caller holds its review lock.

        Rehash actual objects, including the trees and parents commit-tree would
        require, before an existing advisory commit can replace materialization.
        Nothing is cached across reconciliation calls. Notes are still compared
        against authoritative evidence by the caller, including valid-subset
        repair and corruption refusals.
        """
        expected = {oid: "commit" for oid in oids}
        for oid, kind in dependencies.items():
            if kind not in {"tree", "commit"} or expected.get(oid, kind) != kind:
                raise GitError("review object has conflicting expected types")
            expected[oid] = kind
        objects = self._read_review_objects(expected)
        if any(oid not in objects for oid in dependencies):
            raise GitError("review commit tree or parent is missing")
        presence = {oid: oid in objects for oid in oids}
        note_oids = self._review_note_oids(tuple(oid for oid, exists in presence.items() if exists))
        blobs = self._read_review_objects({oid: "blob" for oid in note_oids.values()})
        if len(blobs) != len(set(note_oids.values())):
            raise GitError("review note body is missing")
        notes = {
            (kind, oid): blobs[note_oids[kind, oid]] if (kind, oid) in note_oids else None
            for kind in ("evaluation", "approval")
            for oid in presence
        }
        return presence, notes

    def _batch_paths(self, expressions: Sequence[str], kind: str) -> dict[str, str]:
        """Resolve selected tree paths using Git's structured stdin protocol."""
        found: dict[str, str] = {}
        reader = _batch_reader(self.path)
        checked: dict[str, tuple[str, bytes] | None] = {}

        def read(oid: str) -> tuple[str, bytes] | None:
            if oid not in checked:
                checked[oid] = reader.objects((oid,))[oid]
            return checked[oid]

        for expression in expressions:
            name, separator, path = expression.partition(":")
            tree_suffix = name.endswith("^{tree}")
            if tree_suffix:
                name = name[:-7]
            oid = self._resolve_ref(name) if name.startswith("refs/") else name
            if oid is None:
                continue
            value = read(oid)
            if value is not None and value[0] == "commit" and (separator or tree_suffix):
                first = value[1].split(b"\n", 1)[0]
                if not first.startswith(b"tree "):
                    raise GitError("Git note lookup commit has no tree")
                oid = first[5:].decode("ascii")
                value = read(oid)
            if separator:
                for segment in path.split("/"):
                    if value is None or value[0] != "tree":
                        value = None
                        break
                    # read() checks current bytes before using immutable parsed
                    # entries. Corrupted objects never hide behind this memo.
                    entries = self._parsed_tree(_repository_key(self.path), oid)
                    child = entries.get(segment)
                    if child is None:
                        value = None
                        break
                    oid = child[1]
                    value = read(oid)
            if value is not None:
                if value[0] != kind:
                    raise GitError("Git note lookup returned the wrong object type")
                found[expression] = oid
        return found

    def _review_note_oids(self, targets: Sequence[str]) -> dict[tuple[str, str], str]:
        if not targets:
            return {}
        roots = self._batch_paths(
            [self._note_ref(kind) + "^{tree}" for kind in ("evaluation", "approval")], "tree"
        )
        self._read_review_objects({oid: "tree" for oid in roots.values()})
        paths: dict[str, set[tuple[str, str]]] = {}
        for kind in ("evaluation", "approval"):
            root = roots.get(self._note_ref(kind) + "^{tree}")
            if root is None:
                if self._ref_exists(self._note_ref(kind)):
                    raise GitError("review notes ref has no retained tree")
                continue
            for target in targets:
                # Git notes uses zero or more two-hex-digit fanout directories
                # (git/git notes.c construct_path_with_fanout). Probe only the
                # possible paths of selected OIDs, never the historical inventory.
                for split in range(0, len(target), 2):
                    path = "/".join(
                        [target[i : i + 2] for i in range(0, split, 2)] + [target[split:]]
                    )
                    paths.setdefault(f"{root}:{path}", set()).add((kind, target))
        found = {}
        for expression, oid in self._batch_paths(tuple(paths), "blob").items():
            for key in paths[expression]:
                if key in found:
                    raise GitError("review notes repeat an annotated object")
                found[key] = oid
        return found

    def _read_review_objects(self, expected: Mapping[str, str]) -> dict[str, bytes]:
        """Batch exact object bytes with positional, type, and Git-hash proofs."""
        objects: dict[str, bytes] = {}
        for oid, kind in expected.items():
            self._validate_oid(oid)
            try:
                value = _batch_reader(self.path).objects((oid,))[oid]
            except GitError as exc:
                raise GitError("Git review object bytes do not reproduce its OID") from exc
            if value is not None:
                if value[0] != kind:
                    raise GitError("Git review object differs from the requested object")
                objects[oid] = value[1]
        return objects

    def tree_oid(self, commit_oid: str) -> str:
        """A commit's tree (a tree names itself), from hash-checked object bytes."""

        self._validate_oid(commit_oid)
        tree = self._commit_tree(commit_oid)
        if tree is not None:
            return tree
        found = _batch_reader(self.path).objects((commit_oid,))[commit_oid]
        if found is None or found[0] != "tree":
            raise GitError(f"ledger object names no tree: {commit_oid}")
        return commit_oid

    def set_main_genesis(self, oid: str) -> None:
        self._validate_oid(oid)
        zero_oid = "0" * (40 if self.object_format() == "sha1" else 64)
        self._update_refs([f"update refs/heads/main {oid} {zero_oid}"])

    def compare_and_set_main(self, oid: str, *, expected_oid: str) -> bool:
        """Advance main exactly once over its expected parent, or report a race loss."""

        self._validate_oid(oid)
        self._validate_oid(expected_oid)
        if self.parent_of(oid) != expected_oid:
            raise GitError("main CAS target is not parented by the expected OID")
        try:
            self._update_refs([f"update refs/heads/main {oid} {expected_oid}"])
        except GitError:
            if self.read_main() != expected_oid:
                return False
            raise
        return True

    @contextmanager
    def activation_lock(self) -> Iterator[None]:
        """Serialize activation/publication and targeted loser collection across processes."""

        with self._exclusive_lock("playbill-activation.lock"):
            try:
                yield
            finally:
                reader = _batch_reader(self.path)
                if getattr(reader._transaction, "request", False) and reader._transaction.accepted:
                    # A successor must never rely on a predecessor whose pack
                    # is still best effort. Finish before another writer enters.
                    reader.seal()

    @contextmanager
    def approval_note_lock(self, candidate_digest: str) -> Iterator[None]:
        """Serialize one candidate's approval read-modify-write across processes.

        The approval note is not a copy of any one file: the store keeps one
        file per signer and the note is a re-render of the whole canonical list,
        which is the only shape Git's one-note-per-object rule allows. That
        makes it a read-modify-write, and `_note_lock` covers only the Git call
        at the end of it. Two approvers on one candidate could therefore have A
        render `[A]`, B render `[A, B]` and write, then A force-write `[A]` --
        leaving the store holding two approvals and Git holding one, and
        activation refusing `note_disagrees_with_evidence` on a proposal nobody
        tampered with, repairable only by re-submitting an approval that already
        exists.

        Per candidate rather than global: two candidates' approvals contend for
        nothing but the note REF, which `_note_lock` already serializes, and a
        single approval lock would make every signer on the instance queue
        behind every other. Deliberately not `_note_lock` itself, which this is
        nested inside: `flock` is per-open-file-description, so re-acquiring the
        same lock file in one process deadlocks.
        """

        CandidateDigest.from_tagged(candidate_digest)
        with self._exclusive_lock(
            f"playbill-approval-{candidate_digest.removeprefix('sha256:')}.lock"
        ):
            yield

    @contextmanager
    def _note_lock(self) -> Iterator[None]:
        """Serialize every note write across processes.

        A note ref is an ordinary ref carrying one commit per update, so two
        writers attaching notes to two *different* commits still contend for the
        same ref lock. Without this the loser surfaces as an opaque Git ref-lock
        failure on a write its caller has no way to retry. This lock is
        deliberately not the activation lock: the generation note is written
        while activation already holds that one, and a second acquisition of the
        same file in the same process would deadlock.
        """

        with self._exclusive_lock("playbill-notes.lock"):
            yield

    @contextmanager
    def _exclusive_lock(self, name: str) -> Iterator[None]:
        path = self.path / name
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _in_flight_directory(self) -> Path:
        return self.path / _GENERATIONS_IN_FLIGHT

    @contextmanager
    def generation_attempt(self) -> Iterator[None]:
        """Keep this thread's new generations out of recovery's collection until it ends.

        A writer holds the in-flight lock shared from before its commit exists
        until the attempt ends, settled or not. Recovery collects unsettled
        generations only while it holds the lock exclusively, so it never sees
        a live writer's marker or commit as residue. Nested attempts share the
        outer one.
        """

        key = str(self.path)
        active: set[str] = getattr(_ATTEMPTS, "paths", set())
        if key in active:
            yield
            return
        descriptor = os.open(self.path / _IN_FLIGHT_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            _ATTEMPTS.paths = active | {key}
            try:
                yield
            finally:
                _ATTEMPTS.paths = active
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def unaccepted_cleanup(self) -> Iterator[tuple[str, ...] | None]:
        """Hold off writers and yield the markers a collection must answer for.

        Yields None when no collection is due, or when a writer's attempt is in
        flight (its markers are not residue yet); the markers then wait for a
        later recovery.
        """

        descriptor = os.open(self.path / _IN_FLIGHT_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            try:
                yield self.unaccepted_cleanup_due()
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _mark_generation_in_flight(self, name: str) -> Path:
        directory = self._in_flight_directory()
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            # The directory entry itself must survive a crash with the marker.
            _fsync_directory(self.path)
        marker = directory / name
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(directory)
        return marker

    def settle_generation_in_flight(self, oid: str) -> None:
        """Forget one generation's marker: it is on main, or it was collected."""

        self._validate_oid(oid)
        reader = _batch_reader(self.path)
        reader.store.release_oid(oid)
        if getattr(reader._transaction, "request", False):
            reader._transaction.retain = False
        marker = self._in_flight_directory() / oid
        if marker.exists():
            marker.unlink()
            _fsync_directory(marker.parent)

    def unaccepted_cleanup_due(self) -> tuple[str, ...] | None:
        """The markers a full unsettled-generation collection must answer for, or None.

        Collection is due when a generation was left in flight (a crash after
        its commit could exist), or when this ledger has never completed one.
        """

        directory = self._in_flight_directory()
        markers = (
            tuple(sorted(path.name for path in directory.iterdir())) if directory.is_dir() else ()
        )
        if markers or not (self.path / _UNSETTLED_CLEANUP_BASELINE).exists():
            return markers
        return None

    def complete_unaccepted_cleanup(self, markers: tuple[str, ...]) -> None:
        """Record a finished full collection and drop the markers it covered."""

        directory = self._in_flight_directory()
        for name in markers:
            if _OID_RE.fullmatch(name):
                _batch_reader(self.path).store.release_oid(name)
            (directory / name).unlink(missing_ok=True)
        if markers:
            _fsync_directory(directory)
        baseline = self.path / _UNSETTLED_CLEANUP_BASELINE
        if not baseline.exists():
            descriptor = os.open(baseline, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory(self.path)

    def loose_object_count(self) -> int:
        """Compaction backlog: loose objects plus excess per-operation packs."""

        values = dict(
            line.split(": ", 1) for line in self._git(["count-objects", "-v"]).decode().splitlines()
        )
        return int(values.get("count", "0")) + max(0, int(values.get("packs", "0")) - 1)

    def compact_objects(self) -> None:
        """Pack every reachable object and write the commit-graph.

        Safe beside live writers: Git keep files protect a writer's pack until
        a durable ref reaches it (or recovery collects a losing generation).
        Git replaces the consolidated pack and commit-graph atomically.
        Unreachable loose objects are left for ``prune_unreachable``.
        """

        self._git(["repack", "-a", "-d", "-q"])
        self._git(["commit-graph", "write", "--reachable"])

    def pack_refs(self) -> None:
        """Pack every ref; the caller holds the review projection and activation locks.

        Those exclude the review, slot and main writers; the note lock taken
        here excludes note writers, so no ref update meets a held ref lock.
        """

        with self._note_lock():
            self._git(["pack-refs", "--all", "--prune"])

    def prune_unreachable(self) -> bool:
        """Delete unreachable loose objects, only while no writer can be in flight.

        A generation's and a proposal's objects exist before the ref that
        reaches them, and the write dedup reuses an old loose object without
        refreshing it, so this runs only quiesced: the caller holds the review
        projection and activation locks, and this takes the note lock and the
        in-flight generations lock exclusively without waiting. Returns False
        (nothing pruned) when a generation attempt is in flight.
        """

        descriptor = os.open(self.path / _IN_FLIGHT_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                with self._note_lock():
                    self._git(["prune", "--expire=now"])
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        return True

    def collect_unreachable_generation(self, oid: str) -> tuple[str, ...]:
        """Delete objects proven reachable solely from one losing generation, including packs."""

        self._validate_oid(oid)
        if self.read_main() == oid:
            raise GitError("refusing to collect the accepted main generation")
        reachable_commits = set(self._git(["rev-list", "--all"]).decode().splitlines())
        if oid in reachable_commits:
            raise GitError("refusing to collect a generation reachable from refs")
        candidate_objects = {
            row.split()[0]
            for row in self._git(["rev-list", "--objects", oid]).decode().splitlines()
            if row.strip()
        }
        protected_objects = {
            row.split()[0]
            for row in self._git(["rev-list", "--objects", "--all"]).decode().splitlines()
            if row.strip()
        }
        object_ids = tuple(sorted(candidate_objects - protected_objects))
        if oid not in object_ids:
            raise GitError("losing generation is not an independently collectable object")
        for object_id in object_ids:
            self._validate_oid(object_id)
        deleted: list[str] = []
        fsync_directories: set[Path] = set()
        for object_id in object_ids:
            path = self.path / "objects" / object_id[:2] / object_id[2:]
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise GitError("loose Git object cleanup target is not a regular file")
            path.unlink()
            deleted.append(object_id)
            fsync_directories.add(path.parent)
        for directory in sorted(fsync_directories, key=str):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        reader = _batch_reader(self.path)
        packed_deleted = reader.store.remove(set(object_ids))
        pending = getattr(reader._transaction, "pending", None)
        if pending is not None:
            for object_id in object_ids:
                pending.pop(object_id, None)
            # Repair may have replaced/removed the provisional pack. Never
            # republish its collected members when the enclosing request exits.
            if getattr(reader._transaction, "request", False):
                reader._transaction.pack = None
                reader._transaction.staged_count = -1
        deleted.extend(object_id for object_id in packed_deleted if object_id not in deleted)
        return tuple(deleted)

    def object_exists(self, oid: str) -> bool:
        self._validate_oid(oid)
        return _batch_reader(self.path).object_info((oid,))[oid] is not None

    def unreachable_commits(self) -> tuple[str, ...]:
        """List unreachable commits while excluding in-process pack publication.

        Git scans the whole object store, so callers reserve this for recovery
        or explicit inspection. Immutable, index-last publication is safe for
        independent Git readers. Keep this scan serialized with targeted loser
        removal, which deliberately deletes unreachable objects fsck inventories.
        """

        with _batch_reader(self.path).store.lock:
            rows = (
                self._git(["fsck", "--unreachable", "--no-reflogs", "--no-progress"])
                .decode()
                .splitlines()
            )
        commits: list[str] = []
        for row in rows:
            fields = row.split()
            if len(fields) == 3 and fields[:2] == ["unreachable", "commit"]:
                self._validate_oid(fields[2])
                commits.append(fields[2])
        return tuple(sorted(set(commits)))

    def migrate_legacy_note_refs(self) -> tuple[str, ...]:
        """Move each pre-rename note ref to its current name, once; return the moved names.

        Runs at instance open. Each move is one atomic ref transaction that
        creates the current ref (leased absent) at the legacy ref's commit and
        deletes the legacy ref (leased at that commit), so a concurrent opener
        that already moved it simply loses the lease and the move is not
        repeated. A legacy ref whose current ref already names the same commit
        is a finished move and is deleted; one whose current ref differs is
        left alone. Cost is a constant number of ref reads.
        """
        if all(self._resolve_ref(legacy) is None for legacy in LEGACY_NOTE_REFS):
            # The common case once moved: three ref reads, no lock, no write.
            return ()
        moved: list[str] = []
        with self._note_lock():
            for legacy, current in LEGACY_NOTE_REFS.items():
                old = self._resolve_ref(legacy)
                if old is None:
                    continue
                new = self._resolve_ref(current)
                if new is None:
                    commands = [
                        f"update {current} {old} {'0' * len(old)}",
                        f"delete {legacy} {old}",
                    ]
                elif new == old:
                    commands = [f"delete {legacy} {old}"]
                else:
                    continue
                try:
                    self._update_refs(commands)
                except GitError:
                    if self._resolve_ref(legacy) == old:
                        raise
                    continue
                moved.append(legacy)
        return tuple(moved)

    def _note_ref(self, kind: str) -> str:
        ref = NOTE_REFS.get(kind)
        if ref is None:
            raise GitError(f"unknown Cruxible note kind: {kind!r}")
        return ref

    @_pack_transaction
    def _write_note(self, kind: str, oid: str, content: bytes, *, replace: bool) -> None:
        """Attach one note through the one write every note ref shares.

        `replace` separates the two note lifetimes this ledger has: a generation
        descriptor is written once and is immutable, while a proposal's
        evaluation and approval notes are projections of an evidence store that
        legitimately grows -- a re-evaluation, a second approver -- and must be
        allowed to restate. Every write proves its own bytes persisted exactly,
        so neither lifetime depends on Git's reporting.
        """

        self._validate_oid(oid)
        with self._note_lock():
            command = self._prepare_note(kind, oid, content, replace=replace)
            self._update_refs([command])
        if self._read_note(kind, oid) != content:
            raise GitError(f"{kind} note did not persist exactly")

    def _prepare_note(self, kind: str, oid: str, content: bytes, *, replace: bool) -> str:
        """Build a note update under the caller's note lock, without publishing it."""
        ref = self._note_ref(kind)
        head = self._resolve_ref(ref)
        existing = self._read_note(kind, oid)
        if existing is not None and not replace:
            raise GitError("note already exists")
        tree = None if head is None else self._commit_tree(head)
        # Preserve Git's existing fanout, descending only the target path.
        directory, remaining, path = tree, oid, ""
        while directory is not None:
            entries = self._encoded_tree(directory).entries
            if remaining in entries:
                break
            child = entries.get(remaining[:2])
            if child is None or child[0] != b"40000":
                break
            path += remaining[:2] + "/"
            directory, remaining = child[1], remaining[2:]
        if existing is None and not path:
            path, remaining = oid[:2] + "/", oid[2:]
        blob = self._blob_oid(content)
        root = self._commit_changes_to_tree(tree, {path + remaining: blob}, {blob: content})
        now = datetime.now(timezone.utc).isoformat()
        environment = {
            "GIT_AUTHOR_NAME": "cruxible-daemon",
            "GIT_AUTHOR_EMAIL": "daemon@cruxible.invalid",
            "GIT_COMMITTER_NAME": "cruxible-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@cruxible.invalid",
            "GIT_AUTHOR_DATE": now,
            "GIT_COMMITTER_DATE": now,
        }
        commit = self._create_commit(
            root,
            () if head is None else (head,),
            f"Notes added by Cruxible ({kind})",
            environment,
        )
        return f"update {ref} {commit} {head or '0' * len(commit)}"

    @contextmanager
    def prepared_generation_note(self, oid: str, content: bytes) -> Iterator[Callable[[], None]]:
        """Stage the descriptor before main; publish it only after the winning CAS.

        Hold the note lease across preparation and publication, so the prepared
        notes tree cannot overwrite another writer's update. All note objects
        join the accepting request's single durable pack.
        """
        self._validate_oid(oid)
        with _batch_reader(self.path).transaction(), self._note_lock():
            command = self._prepare_note("generation", oid, content, replace=False)

            def publish() -> None:
                if self.read_main() != oid:
                    raise GitError("generation note target is not the current main ref")
                self._update_refs([command])
                if self.read_generation_note(oid) != content:
                    raise GitError("generation note did not persist exactly")

            yield publish

    def _read_note(self, kind: str, oid: str) -> bytes | None:
        self._validate_oid(oid)
        found = self._resident_note(kind, oid)
        if found is not _ASK_GIT:
            return cast(bytes | None, found)
        result = _command(
            [
                "git",
                f"--git-dir={self.path}",
                "notes",
                f"--ref={self._note_ref(kind)}",
                "show",
                oid,
            ],
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def _resident_note(self, kind: str, oid: str) -> bytes | None | object:
        """Walk the notes tree through the resident reader; no process per note.

        Git notes stores a target at its full hex name under zero or more
        two-hex-digit fanout directories (git/git notes.c
        construct_path_with_fanout). At each level the remaining name is either
        a note blob or the next fanout directory. A shape this walk does not
        expect is left to ``git notes show``.
        """

        head = self._resolve_ref(self._note_ref(kind))
        if head is None:
            return None
        tree = self._commit_tree(head)
        if tree is None:
            return _ASK_GIT
        reader = _batch_reader(self.path)
        remaining = oid
        while True:
            found = reader.objects((tree,))[tree]
            if found is None or found[0] != "tree":
                return _ASK_GIT
            entries = self._parsed_tree(_repository_key(self.path), tree)
            note = entries.get(remaining)
            if note is not None:
                mode, note_oid = note
                if mode != b"100644":
                    return _ASK_GIT
                blob = reader.objects((note_oid,))[note_oid]
                return None if blob is None or blob[0] != "blob" else blob[1]
            fanout = entries.get(remaining[:2])
            if fanout is None or len(remaining) <= 2:
                return None
            if fanout[0] != b"40000":
                return _ASK_GIT
            tree, remaining = fanout[1], remaining[2:]

    def write_generation_note(self, oid: str, content: bytes) -> None:
        """Durably attach one immutable descriptor note after the winning main CAS."""

        self._validate_oid(oid)
        if self.read_main() != oid:
            raise GitError("generation note target is not the current main ref")
        if self.read_generation_note(oid) is not None:
            raise GitError("generation already carries a descriptor note")
        self._write_note("generation", oid, content, replace=False)

    def write_recovered_generation_note(self, oid: str, content: bytes) -> None:
        """Repair a missing note only for a replay-proven commit on accepted main."""

        self._validate_oid(oid)
        if not self.is_ancestor(oid, self.read_main()):
            raise GitError("recovered generation note target is outside main history")
        if self.read_generation_note(oid) is not None:
            raise GitError("generation already carries a descriptor note")
        self._write_note("generation", oid, content, replace=False)

    def read_generation_note(self, oid: str) -> bytes | None:
        return self._read_note("generation", oid)

    def write_proposal_note(self, kind: str, oid: str, content: bytes) -> None:
        """Project one proposal's evidence onto its own candidate commit.

        Only the proposal note kinds are reachable here: the generation
        descriptor keeps its own doors, which refuse a second write, because a
        settled generation's note is a fact about accepted history rather than
        a restatable projection.
        """

        if kind not in {"evaluation", "approval"}:
            raise GitError(f"unknown Cruxible proposal note kind: {kind!r}")
        self._write_note(kind, oid, content, replace=True)

    def read_proposal_note(self, kind: str, oid: str) -> bytes | None:
        if kind not in {"evaluation", "approval"}:
            raise GitError(f"unknown Cruxible proposal note kind: {kind!r}")
        return self._read_note(kind, oid)

    def read_proposal_notes(
        self, pairs: Sequence[tuple[str, str]], *, notes_commit: str | None = None
    ) -> dict[tuple[str, str], bytes | None]:
        """Read several proposal notes in one Git process, resolving refs now.

        Git notes stores a target under zero or more two-hex-digit fanout
        directories (git/git notes.c construct_path_with_fanout), so each target
        is asked for at every path it could occupy; at most one exists.
        ``notes_commit`` reads one immutable notes commit instead of the ref.
        """

        if notes_commit is not None:
            self._validate_oid(notes_commit)
        wanted: dict[str, tuple[str, str]] = {}
        for kind, oid in pairs:
            if kind not in {"evaluation", "approval"}:
                raise GitError(f"unknown Cruxible proposal note kind: {kind!r}")
            self._validate_oid(oid)
            ref = notes_commit or self._note_ref(kind)
            for split in range(0, len(oid), 2):
                path = "/".join([oid[i : i + 2] for i in range(0, split, 2)] + [oid[split:]])
                wanted[f"{ref}:{path}"] = (kind, oid)
        found: dict[tuple[str, str], bytes] = {}
        resolved = self._batch_paths(tuple(wanted), "blob")
        bodies = self._read_review_objects({oid: "blob" for oid in resolved.values()})
        for expression, oid in resolved.items():
            key = wanted[expression]
            if key in found:
                raise GitError("Git notes repeat an annotated object")
            found[key] = bodies[oid]
        return {pair: found.get(pair) for pair in pairs}

    def read_main(self) -> str:
        oid = self._resolve_ref("refs/heads/main")
        if oid is None:
            raise GitError("ledger has no main ref")
        return oid

    def parent_of(self, oid: str) -> str | None:
        self._validate_oid(oid)
        # A commit's parents are part of its immutable bytes: read its headers
        # through the resident reader instead of spawning `rev-list`.
        found = _batch_reader(self.path).objects((oid,))[oid]
        if found is None or found[0] != "commit":
            raise GitError(f"ledger object is not a commit: {oid}")
        headers = found[1].split(b"\n\n", 1)[0].split(b"\n")
        parents = [
            line[len(b"parent ") :].decode("ascii")
            for line in headers
            if line.startswith(b"parent ")
        ]
        if not parents:
            return None
        if len(parents) == 1:
            self._validate_oid(parents[0])
            return parents[0]
        raise GitError("Cruxible refuses merge commits on main")

    def changed_tree_paths(self, before: str, after: str) -> tuple[str, ...]:
        """Exact physical path delta between two caller-verified accepted commits.

        Rename detection is disabled: moves are a removal and an insertion.
        Mode changes are included and the blob reader checks successor modes.
        The delta comes from hash-checked tree objects, as ``changed_entries``.
        """
        return tuple(change.path for change in self.changed_entries(before, after))

    def read_tree(self, oid: str) -> dict[str, bytes]:
        entries = _proven_blob_entries(self.list_tree(oid))
        # One batched read keeps whole-tree cost independent of the artifact count.
        blobs = self.read_blobs(tuple(entry.oid for entry in entries))
        return {entry.path: blobs[entry.oid] for entry in entries}

    def read_tree_delta(
        self, parent_oid: str, oid: str, *, parent_tree: Mapping[str, bytes]
    ) -> Mapping[str, bytes]:
        """Read a physical successor from an already-proven exact parent tree.

        The caller owns the proof that parent_tree is the complete regular-file
        tree at parent_oid. Git's complete mode/object diff proves the unchanged
        complement; only new/changed blobs are read. This does not infer physical
        changes from semantic candidate scope, which omits derivative/daemon files.
        """
        changes = self.changed_entries(parent_oid, oid)
        for change in changes:
            if change.oid is not None and change.mode != "100644":
                raise GitError(
                    f"ledger tree contains unsupported {change.mode} member: {change.path}"
                )
            if (change.status == "A") != (change.path not in parent_tree):
                raise GitError(f"tree delta differs from its proven parent: {change.path}")
        blobs = self.read_blobs([c.oid for c in changes if c.oid is not None])
        if isinstance(parent_tree, SnapshotTree):
            # A snapshot parent yields a fork of it: only changed bytes are read
            # and applied, unchanged rows are shared, and the result still names
            # its parent, so evaluation can take its incremental path.
            builder = parent_tree.fork()
            for change in changes:
                if change.oid is None:
                    del builder[change.path]
                else:
                    builder[change.path] = blobs[change.oid]
            return builder.snapshot()
        result = dict(parent_tree)
        for change in changes:
            if change.oid is None:
                del result[change.path]
            else:
                result[change.path] = blobs[change.oid]
        return {path: result[path] for path in sorted(result, key=lambda p: p.encode("utf-8"))}

    def blob_refs_at(self, oid: str) -> dict[str, BlobRef]:
        """The whole tree as blob references: the same proof ``read_tree`` applies,
        with no blob payload read. Bytes are read through ``read_blobs`` on demand."""

        entries = _proven_blob_entries(self.list_tree_with_sizes(oid))
        load = self.read_blobs  # one loader object, so a batch read groups every ref
        return {entry.path: BlobRef(entry.oid, _entry_size(entry), load) for entry in entries}

    def blob_ref_changes(self, parent_oid: str, oid: str) -> dict[str, BlobRef | None]:
        """A successor as a delta of blob references from Git's structural diff.

        Unreported paths are byte-identical to the parent; each changed path is
        a regular-file blob (proven by mode here and by type and size from the
        object headers), or None when removed. No payload is read.
        """

        changes = self.changed_entries(parent_oid, oid)
        for change in changes:
            if change.oid is not None and change.mode != "100644":
                raise GitError(
                    f"ledger tree contains unsupported {change.mode} member: {change.path}"
                )
        infos = self.object_sizes([c.oid for c in changes if c.oid is not None])
        load = self.read_blobs
        result: dict[str, BlobRef | None] = {}
        for change in changes:
            if change.oid is None:
                result[change.path] = None
                continue
            info = infos.get(change.oid)
            if info is None or info[0] != "blob":
                raise GitError(f"ledger tree names a missing or non-blob object: {change.path}")
            result[change.path] = BlobRef(change.oid, info[1], load)
        return result

    def paths_at(self, oid: str) -> tuple[str, ...]:
        """List one commit's paths under the same proof ``read_tree`` applies.

        A name-only listing has to refuse exactly the generations a whole-tree
        read refuses. Otherwise a caller that lists is answered where a caller
        that reads is refused, and — because a listing may be served from a
        memo filled by ``read_tree`` — the answer would depend on whether that
        memo happened to be warm.
        """

        return tuple(entry.path for entry in _proven_blob_entries(self.list_tree(oid)))

    def record_at(self, oid: str, path: str) -> bytes | None:
        """One accepted change-set record's bytes through the resident reader.

        Callers verify the bytes against a digest recovery already checked, so
        this skips the tree-mode proof ``blob_at`` applies and costs one pipe
        round trip instead of a Git process.
        """

        self._validate_oid(oid)
        if not path.startswith("changesets/") or "\n" in path:
            raise GitError("record reads name a change-set path")
        return _batch_reader(self.path).path_blob(oid, path)

    def blob_at(self, oid: str, path: str) -> bytes | None:
        """Read one exact committed blob without materializing its whole tree."""

        return self.blobs_at(oid, (path,)).get(path)

    def blobs_at(self, oid: str, paths: Sequence[str]) -> dict[str, bytes]:
        """Read an exact set of committed paths without materializing the tree.

        Git walks only the subtrees the pathspec names, so the cost tracks the
        requested paths rather than the size of the generation. Mode and object
        type are proven exactly as ``read_tree`` proves them, so a caller
        cannot reach a symlink or a submodule by naming it, and a path the
        commit does not carry is simply absent from the result.
        """

        self._validate_oid(oid)
        ordered = tuple(dict.fromkeys(paths))
        if not ordered:
            return {}
        if any(not path for path in ordered):
            raise GitError("ledger blob read requires an exact path")
        # Each path is found by reading only the tree objects above it, which
        # are remembered by object ID, so the cost follows the paths asked for.
        selected: list[GitTreeEntry] = []
        for path in ordered:
            directory, _separator, name = path.rpartition("/")
            entries = self._directory_entries(oid, directory)
            found = None if entries is None else entries.get(name)
            if found is None or found[0] == b"40000":
                continue  # absent, or a directory: no file by that exact name
            mode = found[0].decode("ascii")
            selected.append(
                GitTreeEntry(
                    path=path,
                    mode=mode,
                    object_type="commit" if mode == "160000" else "blob",
                    oid=found[1],
                    size=None,
                )
            )
        _proven_blob_entries(tuple(selected))
        blobs = self.read_blobs(tuple(entry.oid for entry in selected))
        return {entry.path: blobs[entry.oid] for entry in selected}

    def changed_entries(self, base_oid: str, target_oid: str) -> tuple[GitTreeChange, ...]:
        """Report exactly the paths whose (mode, object) differs between two commits.

        Git compares the two trees structurally and skips every subtree whose
        object ID already matches, so the cost tracks the number of changed
        members rather than the size of the tree. The complement of this report
        is the load-bearing part: a path Git omits has byte-identical content in
        both trees, because identical content under an identical mode is the
        same content-addressed object by construction. That is what lets a
        caller carry a parent tree forward instead of re-reading it.

        Rename and copy detection are disabled: a similarity heuristic would
        turn one delete plus one add into a single record and lose the exact
        add/delete pair the caller must apply.
        """

        self._validate_oid(base_oid)
        self._validate_oid(target_oid)
        # Two commits never change, so neither does the diff between them.
        key = (_repository_key(self.path), base_oid, target_oid)
        with _TREE_CHANGES_LOCK:
            remembered = _TREE_CHANGES.get(key)
            if remembered is not None:
                _TREE_CHANGES.move_to_end(key)
                return remembered
        changes = self._read_changed_entries(base_oid, target_oid)
        with _TREE_CHANGES_LOCK:
            _TREE_CHANGES[key] = changes
            _TREE_CHANGES.move_to_end(key)
            while len(_TREE_CHANGES) > _TREE_CHANGES_CAPACITY:
                _TREE_CHANGES.popitem(last=False)
        return changes

    def _read_changed_entries(self, base_oid: str, target_oid: str) -> tuple[GitTreeChange, ...]:
        """``diff-tree -r --no-renames`` over hash-checked tree objects.

        Git reads trees without re-hashing them, so a tree object replaced on
        disk under its ID would change the reported delta. Only subtrees whose
        IDs differ are read; a path that turns between a file and a directory
        is a deletion plus additions, exactly as Git reports it.
        """

        repository = _repository_key(self.path)
        changes: list[GitTreeChange] = []

        def compare(prefix: str, before: str | None, after: str | None) -> None:
            old = {} if before is None else self._parsed_tree(repository, before)
            new = {} if after is None else self._parsed_tree(repository, after)
            for name in sorted(old.keys() | new.keys(), key=_tree_order_key(old, new)):
                source, destination = old.get(name), new.get(name)
                if source == destination:
                    continue
                path = _listing_path(prefix, name)
                source_tree = source is not None and source[0] == _TREE_MODE
                destination_tree = destination is not None and destination[0] == _TREE_MODE
                if source_tree and destination_tree:
                    assert source is not None and destination is not None
                    compare(path + "/", source[1], destination[1])
                    continue
                if (
                    source is not None
                    and destination is not None
                    and not (source_tree or destination_tree)
                ):
                    status = "M" if _mode_kind(source[0]) == _mode_kind(destination[0]) else "T"
                    changes.append(
                        GitTreeChange(
                            path=path,
                            status=status,
                            mode=_listing_mode(destination[0]),
                            oid=self._checked_oid(destination[1]),
                            previous_oid=self._checked_oid(source[1]),
                        )
                    )
                    continue
                if source is not None:
                    if source_tree:
                        compare(path + "/", source[1], None)
                    else:
                        changes.append(
                            GitTreeChange(
                                path=path,
                                status="D",
                                mode="000000",
                                oid=None,
                                previous_oid=self._checked_oid(source[1]),
                            )
                        )
                if destination is not None:
                    if destination_tree:
                        compare(path + "/", None, destination[1])
                    else:
                        changes.append(
                            GitTreeChange(
                                path=path,
                                status="A",
                                mode=_listing_mode(destination[0]),
                                oid=self._checked_oid(destination[1]),
                                previous_oid=None,
                            )
                        )

        compare("", self._root_tree(base_oid), self._root_tree(target_oid))
        changes.sort(key=lambda change: change.path.encode("utf-8"))
        return tuple(changes)

    def _root_tree(self, oid: str) -> str:
        """The tree a commit names, or ``oid`` itself when it is a tree."""

        self._validate_oid(oid)
        tree = self._commit_tree(oid)
        return oid if tree is None else tree

    def _checked_oid(self, oid: str) -> str:
        self._validate_oid(oid)
        return oid

    def list_tree(self, oid: str) -> tuple[GitTreeEntry, ...]:
        """List an exact commit recursively without reading any blob payload.

        Entries carry no `size`: reporting it costs Git one object-size lookup
        per entry, so only `list_tree_with_sizes` pays for it.
        """

        return self._list_tree(oid, with_sizes=False)

    def tree_paths_containing_literal(
        self,
        oid: str,
        *,
        literal: str,
        paths: Sequence[str],
    ) -> tuple[str, ...]:
        """Find exact committed blob text without materializing the accepted tree."""

        self._validate_oid(oid)
        if not literal or not paths:
            raise GitError("ledger literal search requires text and scoped paths")
        result = _command(
            [
                "git",
                f"--git-dir={self.path}",
                "grep",
                "--fixed-strings",
                "--files-with-matches",
                "-z",
                literal,
                oid,
                "--",
                *paths,
            ],
            check=False,
        )
        if result.returncode == 1:
            return ()
        if result.returncode != 0:
            raise GitError(f"system Git operation 'grep' failed with exit code {result.returncode}")
        prefix = f"{oid}:".encode("ascii")
        found: list[str] = []
        for raw_path in result.stdout.split(b"\x00"):
            if not raw_path:
                continue
            if not raw_path.startswith(prefix):
                raise GitError("ledger literal search returned an unexpected coordinate")
            try:
                found.append(raw_path[len(prefix) :].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise GitError("ledger literal search returned a malformed path") from exc
        return tuple(found)

    def object_sizes(self, oids: Sequence[str]) -> dict[str, tuple[str, int] | None]:
        """Type and size of each object, read without loading any payload."""

        for oid in oids:
            self._validate_oid(oid)
        return _batch_reader(self.path).object_info(tuple(dict.fromkeys(oids)))

    def tree_has_path(self, oid: str, path: str) -> bool:
        """Whether this commit's tree names ``path`` as a file or a nonempty directory."""

        listing = self._remembered_whole_listing(oid)
        if listing is not None:
            return _listing_has_path(listing, path)
        directory, _separator, name = path.rpartition("/")
        entries = self._directory_entries(oid, directory)
        return entries is not None and name in entries

    def tree_child_names(self, oid: str, directory: str) -> tuple[str, ...]:
        """Immediate child names of one directory ("" is the root), in tree order."""

        listing = self._remembered_whole_listing(oid)
        if listing is not None:
            return _listing_child_names(listing, directory)
        entries = self._directory_entries(oid, directory)
        return () if entries is None else tuple(sorted(entries, key=_tree_order_key(entries)))

    def _directory_entries(
        self, oid: str, directory: str
    ) -> Mapping[str, tuple[bytes, str]] | None:
        """One directory's own tree entries, read down its path; None if it is absent.

        Only the tree objects on the path are read, so the cost follows the
        directory's depth and width, never the size of the whole tree. Git
        stores no empty tree, so a directory present here is nonempty, exactly
        as a recursive listing of files implies it.
        """

        self._validate_oid(oid)
        repository = _repository_key(self.path)
        with _PARSED_TREES_LOCK:
            root = _COMMIT_ROOTS.get((repository, oid), _UNREAD)
        if root is _UNREAD:
            root = self._commit_tree(oid) or oid
            with _PARSED_TREES_LOCK:
                _COMMIT_ROOTS[(repository, oid)] = root
                while len(_COMMIT_ROOTS) > _COMMIT_ROOTS_CAPACITY:
                    _COMMIT_ROOTS.popitem(last=False)
        entries = self._parsed_tree(repository, str(root))
        for part in directory.split("/") if directory else ():
            entry = entries.get(part)
            if entry is None or entry[0] != b"40000":
                return None
            entries = self._parsed_tree(repository, entry[1])
        return entries

    def _parsed_tree(
        self, repository: tuple[str, int, int], oid: str
    ) -> Mapping[str, tuple[bytes, str]]:
        """One tree object's immutable entries, sharing encoded write-side listings."""

        key = (repository, oid)
        with _PARSED_TREES_LOCK:
            cached = _PARSED_TREES.get(key)
            if cached is not None:
                _PARSED_TREES.move_to_end(key)
                return cached
        with _ENCODED_TREES_LOCK:
            encoded = _ENCODED_TREES.get(oid)
            if encoded is not None:
                _ENCODED_TREES.move_to_end(oid)
        # A read that compares all children needs dict lookups. Materialize
        # once per cold immutable object using the linear items iterator;
        # returning the AVL map here makes a full comparison O(n log n).
        entries = self._tree_entries_of(oid) if encoded is None else dict(encoded.entries.items())
        global _PARSED_TREE_ENTRIES
        with _PARSED_TREES_LOCK:
            if key not in _PARSED_TREES:
                _PARSED_TREES[key] = entries
                _PARSED_TREE_ENTRIES += len(entries)
            while _PARSED_TREES and _PARSED_TREE_ENTRIES > _PARSED_TREE_CAPACITY:
                _old, evicted = _PARSED_TREES.popitem(last=False)
                _PARSED_TREE_ENTRIES -= len(evicted)
        return entries

    def _remembered_whole_listing(self, oid: str) -> _Listing | None:
        """Whichever whole listing of this object is remembered, sized or not."""

        self._validate_oid(oid)
        repository = _repository_key(self.path)
        for candidate in (oid, self._commit_tree(oid)):
            if candidate is None:
                continue
            with _TREE_LISTINGS_LOCK:
                for with_sizes in (True, False):
                    listing = _TREE_LISTINGS.get((repository, candidate, with_sizes))
                    if listing is not None:
                        return listing
        return None

    def list_tree_with_sizes(self, oid: str) -> tuple[GitTreeEntry, ...]:
        """List an exact commit recursively, with the size Git reports per entry.

        Reserved for callers that gate on declared sizes before reading blobs.
        The per-entry size lookup dominates listing cost on a loose-object
        ledger, so callers that ignore `size` must use `list_tree` instead.
        """

        return self._list_tree(oid, with_sizes=True)

    def _list_tree(
        self,
        oid: str,
        *,
        with_sizes: bool,
        paths: Sequence[str] | None = None,
    ) -> tuple[GitTreeEntry, ...]:
        self._validate_oid(oid)
        # A whole listing of one object ID never changes: reuse it per repository.
        repository = _repository_key(self.path)
        if paths is not None:
            # Answer a path-restricted listing from a remembered whole listing
            # of this object (or of the tree its commit names) when there is
            # one, with a literal pathspec's semantics: the exact path, or every
            # entry beneath it when it names a directory.
            whole = _remembered_listing(repository, oid, with_sizes=with_sizes)
            if whole is None:
                tree = self._commit_tree(oid)
                if tree is not None:
                    whole = _remembered_listing(repository, tree, with_sizes=with_sizes)
            if whole is None:
                return self._read_tree_listing(oid, with_sizes=with_sizes, paths=paths)
            return _select_from_listing(whole, paths)
        cached = _remembered_listing(repository, oid, with_sizes=with_sizes)
        if cached is not None:
            return tuple(cached)
        # A commit lists as its root tree, and the tree is usually what this
        # process just wrote or listed: resolve it from the commit's own bytes.
        tree = self._commit_tree(oid)
        if tree is not None:
            cached = _remembered_listing(repository, tree, with_sizes=with_sizes)
            if cached is not None:
                _remember_listing(repository, oid, cached)
                return tuple(cached)
        listing = self._read_tree_listing(oid, with_sizes=with_sizes, paths=None)
        if listing:
            _remember_listing(repository, oid, listing)
            if tree is not None:
                _remember_listing(repository, tree, listing)
        return listing

    def _commit_tree(self, oid: str) -> str | None:
        """The root tree a commit names, or None when the object is not a commit."""

        found = _batch_reader(self.path).objects((oid,))[oid]
        if found is None or found[0] != "commit":
            return None
        first = found[1].split(b"\n", 1)[0]
        if not first.startswith(b"tree "):
            raise GitError(f"ledger commit has no tree header: {oid}")
        tree = first[len(b"tree ") :].decode("ascii")
        self._validate_oid(tree)
        return tree

    def _read_tree_listing(
        self,
        oid: str,
        *,
        with_sizes: bool,
        paths: Sequence[str] | None,
    ) -> tuple[GitTreeEntry, ...]:
        """``ls-tree -r [-l] --full-tree`` over hash-checked tree objects.

        Git lists a tree without re-hashing the objects it traverses, so a tree
        object replaced on disk under its ID would list other members. Every
        tree here comes through the checked batch reader, from the commit's own
        bytes down. A literal ``paths`` selects an exact entry, or every entry
        beneath a directory. Sizes come from object headers; a blob's bytes are
        checked against its size and ID when they are read.
        """

        root = self._root_tree(oid)
        rows: dict[str, tuple[bytes, str]] = {}
        pending: list[tuple[str, str]] = []
        if paths is None:
            pending.append(("", root))
        else:
            repository = _repository_key(self.path)
            for requested in paths:
                directory, _separator, name = requested.rpartition("/")
                entries: Mapping[str, tuple[bytes, str]] | None = self._parsed_tree(
                    repository, root
                )
                for part in directory.split("/") if directory else ():
                    found = entries.get(part) if entries is not None else None
                    entries = (
                        self._parsed_tree(repository, found[1])
                        if found is not None and found[0] == _TREE_MODE
                        else None
                    )
                entry = None if entries is None else entries.get(name)
                if entry is None:
                    continue
                if entry[0] == _TREE_MODE:
                    pending.append((requested + "/", entry[1]))
                else:
                    rows[requested] = entry
        reader = _batch_reader(self.path)
        raw_length = 20 if self.object_format() == "sha1" else 32
        while pending:
            level = pending
            pending = []
            trees = reader.objects(tuple(dict.fromkeys(tree for _prefix, tree in level)))
            for prefix, tree in level:
                found_tree = trees[tree]
                if found_tree is None or found_tree[0] != "tree":
                    raise GitError(f"ledger tree object is unavailable: {tree}")
                entries = _tree_entries(found_tree[1], raw_length=raw_length)
                if entries is None:
                    raise GitError(f"ledger tree object is malformed: {tree}")
                for name, entry in entries.items():
                    path = _listing_path(prefix, name)
                    if entry[0] == _TREE_MODE:
                        pending.append((path + "/", entry[1]))
                    else:
                        rows[path] = entry
        ordered = sorted(rows.items(), key=lambda item: item[0].encode("utf-8"))
        sizes: dict[str, tuple[str, int] | None] = {}
        if with_sizes:
            sizes = self.object_sizes(
                [entry[1] for _path, entry in ordered if _listing_type(entry[0]) == "blob"]
            )
        result: list[GitTreeEntry] = []
        for path, (raw_mode, object_oid) in ordered:
            self._validate_oid(object_oid)
            object_type = _listing_type(raw_mode)
            size: int | None = None
            if with_sizes and object_type == "blob":
                info = sizes.get(object_oid)
                if info is None or info[0] != "blob":
                    raise GitError(f"ledger tree names a missing or non-blob object: {path}")
                size = info[1]
            result.append(
                GitTreeEntry(
                    path=path,
                    mode=_listing_mode(raw_mode),
                    object_type=object_type,
                    oid=object_oid,
                    size=size,
                )
            )
        return tuple(result)

    def read_blob(self, oid: str) -> bytes:
        return self.read_blobs((oid,))[oid]

    def read_blobs(self, oids: Sequence[str]) -> dict[str, bytes]:
        """Read a bounded set of blobs through the repository's resident batch reader."""

        ordered = tuple(dict.fromkeys(oids))
        for oid in ordered:
            self._validate_oid(oid)
        if not ordered:
            return {}
        return _batch_reader(self.path).read(ordered)

    def verify_commit(self, oid: str, *, principal_id: str = "daemon") -> bool:
        """Verify against exactly one expected signer, not any configured signer."""

        self._validate_oid(oid)
        signer = self._allowed_signer_entry(principal_id)
        # A commit's signed bytes never change under its object ID, so a check
        # that passed for this exact signer entry passes again. Only successes
        # are remembered; a changed or rotated entry is a different key.
        key = (_repository_key(self.path), oid, signer)
        with _VERIFIED_COMMITS_LOCK:
            if key in _VERIFIED_COMMITS:
                _VERIFIED_COMMITS.move_to_end(key)
                return True
        public = serialization.load_ssh_public_key(b" ".join(signer.split()[1:]))
        if not isinstance(public, Ed25519PublicKey):
            return False
        if not self._verify_public(oid, public):
            return False
        with _VERIFIED_COMMITS_LOCK:
            _VERIFIED_COMMITS[key] = None
            _VERIFIED_COMMITS.move_to_end(key)
            while len(_VERIFIED_COMMITS) > _VERIFIED_COMMIT_CAPACITY:
                _VERIFIED_COMMITS.popitem(last=False)
        return True

    def verify_commit_with_public_key(
        self,
        oid: str,
        *,
        principal_id: str,
        public_key_hex: str,
    ) -> bool:
        """Verify a historical commit against exactly the replayed parent-root key."""

        self._validate_oid(oid)
        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        except ValueError as exc:
            raise GitError("historical daemon public key is malformed") from exc
        return self._verify_public(oid, public_key)

    def _verify_public(self, oid: str, public: Ed25519PublicKey) -> bool:
        key = (_repository_key(self.path), oid, public.public_bytes_raw())
        with _VERIFIED_COMMITS_LOCK:
            if key in _VERIFIED_COMMITS:
                _VERIFIED_COMMITS.move_to_end(key)
                return True
        found = _batch_reader(self.path).objects((oid,))[oid]
        if found is None or found[0] != "commit":
            return False
        if not verify_signature(found[1], public, sha256=self.object_format() == "sha256"):
            return False
        with _VERIFIED_COMMITS_LOCK:
            _VERIFIED_COMMITS[key] = None
            while len(_VERIFIED_COMMITS) > _VERIFIED_COMMIT_CAPACITY:
                _VERIFIED_COMMITS.popitem(last=False)
        return True

    def main_history(self) -> tuple[str, ...]:
        """Return the non-merge main chain in oldest-first order.

        One walk reports each commit with its own parent list, so listing a long
        history costs a single Git process rather than two per generation. The
        walk deliberately does not follow first parents only: a merge would then
        be silently flattened, whereas here it surfaces as a commit with more
        than one parent and is refused. Ancestry is checked as it is walked --
        every commit after the root must name its predecessor in the returned
        order -- so the result is a proven linear chain, not just a listing.
        """

        # Parents are read from each commit's own hash-checked bytes, never
        # from Git's unverified traversal, so a commit object replaced on disk
        # cannot splice another chain in under a known ID.
        reader = _batch_reader(self.path)
        newest_first: list[str] = []
        oid: str | None = self.read_main()
        while oid is not None:
            self._validate_oid(oid)
            found = reader.objects((oid,))[oid]
            if found is None or found[0] != "commit":
                raise GitError(f"ledger object is not a commit: {oid}")
            parents = _commit_parents(found[1])
            if len(parents) > 1:
                raise GitError("Cruxible refuses merge commits on main")
            newest_first.append(oid)
            if len(newest_first) > _MAIN_HISTORY_LIMIT:
                raise GitError("Cruxible main history is not a single parent chain")
            oid = parents[0] if parents else None
        return tuple(reversed(newest_first))

    def accepting_admission(self, oid: str) -> AcceptingAdmission | None:
        """Read one signed generation header; absence means pre-succession.

        Only header lines are interpreted, never the prose commit message.
        The hash-checked object reader authenticates the immutable bytes; the
        accepted-history epoch has already verified the commit's signature.
        """

        self._validate_oid(oid)
        found = _batch_reader(self.path).objects((oid,))[oid]
        if found is None or found[0] != "commit":
            raise GitError(f"ledger object is not a commit: {oid}")
        values = [
            line.removeprefix(b"cruxible-accepting-admission ")
            for line in found[1].split(b"\n\n", 1)[0].splitlines()
            if line.startswith(b"cruxible-accepting-admission ")
        ]
        if not values:
            return None
        if len(values) != 1:
            raise GitError("generation has multiple accepting admission headers")
        return parse_accepting_admission(values[0])

    def commit_timestamps(self, oid: str) -> tuple[datetime, datetime]:
        """Return one commit's embedded author and committer instants in UTC."""

        self._validate_oid(oid)
        found = _batch_reader(self.path).objects((oid,))[oid]
        if found is None or found[0] != "commit":
            raise GitError(f"ledger object is not a commit: {oid}")
        content = found[1].split(b"\n\n", 1)[0].decode("utf-8")
        timestamps: dict[str, datetime] = {}
        for line in content.splitlines():
            kind = (
                "author"
                if line.startswith("author ")
                else ("committer" if line.startswith("committer ") else None)
            )
            if kind is None:
                continue
            fields = line.rsplit(" ", 2)
            if len(fields) != 3:
                raise GitError("Git commit identity timestamp is malformed")
            try:
                seconds = int(fields[-2])
                zone = fields[-1]
                if not re.fullmatch(r"[+-][0-9]{4}", zone):
                    raise ValueError
            except (ValueError, IndexError) as exc:
                raise GitError("Git commit identity timestamp is malformed") from exc
            timestamps[kind] = datetime.fromtimestamp(
                seconds,
                tz=timezone.utc,
            )
        if set(timestamps) != {"author", "committer"}:
            raise GitError("Git commit omits an identity timestamp")
        return timestamps["author"], timestamps["committer"]

    def is_ancestor(self, ancestor_oid: str, descendant_oid: str) -> bool:
        self._validate_oid(ancestor_oid)
        self._validate_oid(descendant_oid)
        pending = [descendant_oid]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == ancestor_oid:
                return True
            if current in visited:
                continue
            visited.add(current)
            found = _batch_reader(self.path).objects((current,))[current]
            if found is not None and found[0] == "commit":
                pending.extend(reversed(_commit_parents(found[1])))
        return False

    def _allowed_signer_entry(self, principal_id: str) -> bytes:
        matches: list[bytes] = []
        for line in self._allowed_signers_path.read_bytes().splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0].decode("utf-8") == principal_id:
                matches.append(b" ".join(fields[:3]))
        if len(matches) != 1:
            raise GitError(f"allowed signers must contain exactly one key for {principal_id!r}")
        return matches[0]

    def allowed_signer_public_key_hex(self, principal_id: str) -> str:
        fields = self._allowed_signer_entry(principal_id).split()
        return raw_public_key_hex_from_openssh(b" ".join(fields[1:3]))

    def _config_read(
        self, arguments: Sequence[str], *, environment: Mapping[str, str] | None = None
    ) -> bytes:
        """A read-only query of repository configuration, remembered per config file.

        Every command runs with global and system configuration disabled, so the
        repository's own config file is the only input besides the arguments and
        the environment; the answer is reused while that file is unchanged. A
        config that includes other files is always asked afresh.
        """

        config = self.path / "config"
        before = _file_identity(config)
        if before is None:
            return self._git(arguments, environment=environment)
        key = (
            _repository_key(self.path),
            before,
            tuple(arguments),
            tuple(sorted((environment or {}).items())),
            tuple(os.environ.get(name) for name in _PASSTHROUGH_ENVIRONMENT),
        )
        with _CONFIG_READS_LOCK:
            remembered = _CONFIG_READS.get(key)
            if remembered is not None:
                _CONFIG_READS.move_to_end(key)
                return remembered
        output = self._git(arguments, environment=environment)
        try:
            # Section names are case-insensitive: [include], [Include],
            # [includeIf "..."] all pull in other files this cache cannot see.
            includes = _CONFIG_INCLUDE_RE.search(config.read_bytes()) is not None
        except OSError:
            includes = True
        if not includes and _file_identity(config) == before:
            with _CONFIG_READS_LOCK:
                _CONFIG_READS[key] = output
                _CONFIG_READS.move_to_end(key)
                while len(_CONFIG_READS) > _CONFIG_READS_CAPACITY:
                    _CONFIG_READS.popitem(last=False)
        return output

    def _validate_oid(self, oid: str) -> None:
        if not _OID_RE.fullmatch(oid):
            raise GitError(f"malformed Git OID: {oid!r}")
        expected_length = 40 if self.object_format() == "sha1" else 64
        if len(oid) != expected_length:
            raise GitError("Git OID length does not match repository object format")

    def _git(
        self,
        arguments: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> bytes:
        result = _command(
            ["git", f"--git-dir={self.path}", *arguments],
            input_bytes=input_bytes,
            environment=environment,
        )
        return result.stdout


@dataclass(frozen=True, slots=True)
class _TreeTransferFrontier:
    base: str | None
    children: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _EncodedTree:
    entries: PersistentMap[tuple[bytes, str]]
    keys: tuple[bytes, ...]
    encoded: tuple[bytes, ...]
    transfer: _TreeTransferFrontier | None = None


_ENCODED_TREES: OrderedDict[str, _EncodedTree] = OrderedDict()
_ENCODED_TREES_LOCK = threading.Lock()
_ENCODED_TREE_ENTRIES = 0


def _remember_encoded_tree(oid: str, listing: _EncodedTree) -> None:
    global _ENCODED_TREE_ENTRIES
    with _ENCODED_TREES_LOCK:
        old = _ENCODED_TREES.pop(oid, None)
        _ENCODED_TREE_ENTRIES += len(listing.entries) - (0 if old is None else len(old.entries))
        _ENCODED_TREES[oid] = listing
        while len(_ENCODED_TREES) > 1 and (
            _ENCODED_TREE_ENTRIES > 400_000 or len(_ENCODED_TREES) > 4096
        ):
            _, old = _ENCODED_TREES.popitem(last=False)
            _ENCODED_TREE_ENTRIES -= len(old.entries)


def _tree_transfer_frontier(oid: str) -> _TreeTransferFrontier | None:
    """The writer's changed children, available while its immutable seed is warm."""
    with _ENCODED_TREES_LOCK:
        cached = _ENCODED_TREES.get(oid)
        if cached is not None:
            _ENCODED_TREES.move_to_end(oid)
            return cached.transfer
    return None


def _tree_entry_key(name: str, mode: bytes) -> bytes:
    return name.encode("utf-8", errors="surrogateescape") + (b"/" if mode == b"40000" else b"")


def _encode_tree_entry(name: str, mode: bytes, oid: str) -> bytes:
    return (
        mode + b" " + name.encode("utf-8", errors="surrogateescape") + b"\x00" + bytes.fromhex(oid)
    )


def _tree_entries(body: bytes, *, raw_length: int) -> dict[str, tuple[bytes, str]] | None:
    """Parse one tree object into name -> (mode, object ID); None when malformed."""

    entries: dict[str, tuple[bytes, str]] = {}
    position = 0
    while position < len(body):
        space = body.find(b" ", position)
        end = body.find(b"\x00", space + 1)
        if space < 0 or end < 0 or end + 1 + raw_length > len(body):
            return None
        mode = body[position:space]
        name = body[space + 1 : end].decode("utf-8", errors="surrogateescape")
        entries[name] = (mode, body[end + 1 : end + 1 + raw_length].hex())
        position = end + 1 + raw_length
    return entries


_TREE_MODE: Final = b"40000"
# A generous bound on accepted generations, so a parent cycle cannot spin.
_MAIN_HISTORY_LIMIT: Final = 10_000_000


def _commit_parents(body: bytes) -> list[str]:
    headers = body.split(b"\n\n", 1)[0].split(b"\n")
    return [
        line[len(b"parent ") :].decode("ascii") for line in headers if line.startswith(b"parent ")
    ]


def _listing_path(prefix: str, name: str) -> str:
    """One tree entry's full path; names Git would not list as UTF-8 are refused."""

    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GitError("ledger tree contains malformed metadata") from exc
    return prefix + name


def _listing_mode(raw_mode: bytes) -> str:
    """A tree entry's mode as ``ls-tree`` and ``diff-tree`` print it."""

    try:
        return raw_mode.decode("ascii").rjust(6, "0")
    except UnicodeDecodeError as exc:
        raise GitError("ledger tree contains malformed metadata") from exc


def _listing_type(raw_mode: bytes) -> str:
    if raw_mode == _TREE_MODE:
        return "tree"
    return "commit" if raw_mode == b"160000" else "blob"


def _mode_kind(raw_mode: bytes) -> str:
    """Git's type-change classes: a regular file, a symlink, or a submodule."""

    return {b"120000": "symlink", b"160000": "gitlink"}.get(raw_mode, "file")


def _tree_order_key(
    *trees: Mapping[str, tuple[bytes, str]],
) -> Callable[[str], bytes]:
    """Git's tree order: a directory sorts as its name followed by ``/``."""

    def key(name: str) -> bytes:
        entry = next((tree[name] for tree in trees if name in tree), None)
        suffix = b"/" if entry is not None and entry[0] == _TREE_MODE else b""
        return name.encode("utf-8", errors="surrogateescape") + suffix

    return key


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_CONFIG_READS_CAPACITY = 64
_CONFIG_INCLUDE_RE = re.compile(rb"\[\s*include", re.IGNORECASE)
_CONFIG_READS: OrderedDict[tuple[object, ...], bytes] = OrderedDict()
_CONFIG_READS_LOCK = threading.Lock()


def _file_identity(path: Path) -> tuple[int, ...] | None:
    try:
        metadata = os.stat(path)
    except OSError:
        return None
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


_ASK_GIT: Final = object()
_SAFE_REF_RE = re.compile(r"^refs/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
_PACKED_REFS_CAPACITY = 16
# Repository identity -> (packed-refs file identity, ref -> object ID).
_PACKED_REFS: OrderedDict[tuple[str, int, int], tuple[tuple[int, ...], dict[str, str]]] = (
    OrderedDict()
)
_PACKED_PREFIX_REFS: OrderedDict[tuple[object, ...], dict[str, str]] = OrderedDict()
_PACKED_REFS_LOCK = threading.Lock()


def _files_backend_ref(repository: Path, ref: str) -> str | None | object:
    """Resolve a full ref from the files backend, or ``_ASK_GIT`` when unsure."""

    if not _SAFE_REF_RE.fullmatch(ref) or ".." in ref or ref.endswith(".lock"):
        return _ASK_GIT
    if (repository / "reftable").exists():
        return _ASK_GIT
    try:
        with open(repository / ref, "rb") as handle:
            content = handle.read(200)
    except (FileNotFoundError, NotADirectoryError):
        content = None
    except OSError:
        return _ASK_GIT
    if content is not None:
        value = content.decode("ascii", errors="replace").strip()
        return value if _OID_RE.fullmatch(value) else _ASK_GIT
    return _packed_ref(repository, ref)


def _packed_ref(repository: Path, ref: str) -> str | None | object:
    path = repository / "packed-refs"
    try:
        metadata = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return _ASK_GIT
    identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    key = _repository_key(repository)
    with _PACKED_REFS_LOCK:
        remembered = _PACKED_REFS.get(key)
    if remembered is None or remembered[0] != identity:
        try:
            raw = path.read_bytes()
        except OSError:
            return _ASK_GIT
        table: dict[str, str] = {}
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            oid, _space, name = line.partition(" ")
            if not _OID_RE.fullmatch(oid):
                return _ASK_GIT
            table[name] = oid
        # A packed-refs rewrite within the stat granularity could look
        # unchanged; re-stat and only remember a table read between two
        # identical observations.
        try:
            after = os.stat(path)
        except OSError:
            return _ASK_GIT
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity:
            return _ASK_GIT
        remembered = (identity, table)
        with _PACKED_REFS_LOCK:
            _PACKED_REFS[key] = remembered
            _PACKED_REFS.move_to_end(key)
            while len(_PACKED_REFS) > _PACKED_REFS_CAPACITY:
                _PACKED_REFS.popitem(last=False)
    return remembered[1].get(ref)


def _command_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """The isolated environment every system Git process runs under."""

    merged_environment = {
        name: os.environ[name] for name in _PASSTHROUGH_ENVIRONMENT if name in os.environ
    }
    merged_environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    if environment is not None:
        unexpected = set(environment) - _COMMAND_ENVIRONMENT
        if unexpected:
            raise GitError(
                "unsupported Git command environment override: " + ", ".join(sorted(unexpected))
            )
        merged_environment.update(environment)
    return merged_environment


class _BatchBlobReader:
    """Shared in-process object reader, including Git's delta-compressed packs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.store = PackStore(path)
        self._transaction = threading.local()

    @contextmanager
    def request(self) -> Iterator[None]:
        if getattr(self._transaction, "pending", None) is not None:
            yield
            return
        self._transaction.pending = {}
        self._transaction.keeps = []
        self._transaction.sealed = set()
        self._transaction.retained_keeps = []
        self._transaction.retain = False
        self._transaction.request = True
        self._transaction.accepted = False
        self._transaction.pack = None
        self._transaction.staged_count = -1
        self._transaction.durable_count = -1
        self._transaction.encoded = {}
        self._transaction.files = set()
        self._transaction.directories = set()
        try:
            yield
        finally:
            try:
                if self._transaction.accepted:
                    self.seal()
                else:
                    self.flush(final=True)
                self.store.release(self._transaction.retained_keeps)
                if not self._transaction.retain:
                    self.release_keeps()
            finally:
                self._transaction.pending = None
                self._transaction.request = False
                self._transaction.encoded = None
                self._transaction.sealed = set()

    def import_pending_pack(self, oid: str) -> None:
        """Carry an earlier best-effort proposal's objects into its acceptance.

        A pack is the prior request's change-sized frontier, not a recursive
        listing of its accepted tree. Its durable accepted base needs no walk.
        """
        pending = getattr(self._transaction, "pending", None)
        if pending is None or oid in pending or not self.store.is_pending(oid):
            return
        with self.store.lock:
            path, _ = self.store.locations[oid]
            for member in self.store.members[path]:
                if member not in pending:
                    value = self.store.read(member)
                    if value is None:
                        raise GitError("accepted proposal dependency is unavailable")
                    pending[member] = value

    def seal(self) -> None:
        """Seal accepted objects/refs while the activation lock excludes successors."""
        self.flush(final=True)
        # Live compaction may pack and prune these refs once their writer has
        # released the review lock: `pack-refs --prune` deletes the loose files
        # and the directories it empties. Their values then live in
        # packed-refs, which Git syncs itself (core.fsync=reference); sync the
        # directory that names it instead.
        packed = False
        for path in sorted(self._transaction.files):
            try:
                fd = os.open(path, os.O_RDONLY)
            except FileNotFoundError:
                packed = True
                continue
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        for path in sorted(self._transaction.directories):
            try:
                _fsync_directory(path)
            except FileNotFoundError:
                packed = True
        if packed:
            _fsync_directory(self.path)
        self._transaction.sealed.update(self._transaction.pending)
        self._transaction.pending.clear()
        self._transaction.retained_keeps.extend(self._transaction.keeps)
        self._transaction.keeps.clear()
        self._transaction.files.clear()
        self._transaction.directories.clear()
        self._transaction.pack = None
        self._transaction.encoded.clear()
        self._transaction.staged_count = -1
        self._transaction.durable_count = -1
        self._transaction.accepted = False
        self._transaction.retain = False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if getattr(self._transaction, "pending", None) is not None:
            yield
            return
        self._transaction.pending = {}
        self._transaction.keeps = []
        self._transaction.retain = False
        try:
            yield
            self.flush()
        finally:
            if not self._transaction.retain:
                self.release_keeps()
            self._transaction.pending = None

    def release_keeps(self) -> None:
        keeps = getattr(self._transaction, "keeps", [])
        if keeps:
            self.store.release(keeps)
            keeps.clear()

    def flush(self, *, final: bool = False, accepting: bool = False) -> None:
        pending = getattr(self._transaction, "pending", None)
        if not pending:
            return
        if getattr(self._transaction, "request", False):
            durable_count = self._transaction.durable_count
            durable = accepting or (final and self._transaction.accepted) or durable_count >= 0
            if len(pending) == durable_count or (
                not final and not durable and len(pending) == self._transaction.staged_count
            ):
                return
            path = self.store.write(
                pending,
                keep=True,
                durable=durable,
                replacing=self._transaction.pack,
                encoded=self._transaction.encoded,
            )
            self._transaction.pack = path
            self._transaction.staged_count = len(pending)
            if durable:
                self._transaction.durable_count = len(pending)
            self._transaction.keeps[:] = [path] if path is not None else []
        else:
            path = self.store.write(pending, keep=True)
            if path is not None:
                self._transaction.keeps.append(path)
            pending.clear()

    def close(self) -> None:
        self.store.close()

    def forget_inherited(self) -> None:
        self._lock = threading.RLock()
        self.store.lock = threading.RLock()
        self.close()

    def path_blob(self, oid: str, path: str) -> bytes | None:
        found = self.objects((oid,))[oid]
        if found is not None and found[0] == "commit":
            first = found[1].split(b"\n", 1)[0]
            if not first.startswith(b"tree "):
                raise GitError("ledger commit has no tree header")
            oid = first[5:].decode("ascii")
        segments = path.split("/")
        for index, segment in enumerate(segments):
            found = self.objects((oid,))[oid]
            if found is None or found[0] != "tree":
                return None
            entries = _tree_entries(found[1], raw_length=len(oid) // 2)
            if entries is None:
                raise GitError("ledger tree object is malformed")
            entry = entries.get(segment)
            if entry is None:
                return None
            mode, oid = entry
            if index < len(segments) - 1 and mode != b"40000":
                return None
        found = self.objects((oid,))[oid]
        if found is None:
            return None
        if found[0] != "blob":
            raise GitError(f"ledger path is not a regular blob: {path}")
        return found[1]

    def object_info(self, oids: Sequence[str]) -> dict[str, tuple[str, int] | None]:
        pending = getattr(self._transaction, "pending", None) or {}
        stored = self.store.infos([oid for oid in oids if oid not in pending])
        return {
            oid: (pending[oid][0], len(pending[oid][1])) if oid in pending else stored[oid]
            for oid in oids
        }

    def objects(self, oids: Sequence[str]) -> dict[str, tuple[str, bytes] | None]:
        with self._lock:
            pending = getattr(self._transaction, "pending", None) or {}
            return {oid: pending[oid] if oid in pending else self.store.read(oid) for oid in oids}

    def read(self, oids: Sequence[str]) -> dict[str, bytes]:
        blobs: dict[str, bytes] = {}
        for oid, value in self.objects(oids).items():
            if value is None or value[0] != "blob":
                raise GitError("Git batch blob output differs from the requested blob")
            blobs[oid] = value[1]
        return blobs


# A generation commit is marked here from before it exists until it is on main
# or collected; recovery scans for unsettled generations only when one is left.
_GENERATIONS_IN_FLIGHT = "playbill-generations-in-flight"
_IN_FLIGHT_LOCK = "playbill-generations-in-flight.lock"
# The ledgers this thread holds a generation attempt on.
_ATTEMPTS = threading.local()
_UNSETTLED_CLEANUP_BASELINE = "playbill-unsettled-cleanup-v1"

_BATCH_READER_CAPACITY = 16
_BATCH_READERS: OrderedDict[tuple[str, int, int], _BatchBlobReader] = OrderedDict()
_BATCH_READERS_LOCK = threading.Lock()


def _repository_key(path: Path) -> tuple[str, int, int]:
    """This exact repository directory: a replacement at the same path differs."""

    resolved = os.path.realpath(path)
    identity = os.stat(resolved)
    return (resolved, identity.st_dev, identity.st_ino)


_ARCHIVE_MEMBERS: OrderedDict[tuple[tuple[str, int, int], str], PersistentMap[bool]] = OrderedDict()

_VERIFIED_COMMIT_CAPACITY = 256
_VERIFIED_COMMITS: OrderedDict[tuple[tuple[str, int, int], str, bytes], None] = OrderedDict()
_VERIFIED_COMMITS_LOCK = threading.Lock()

# A commit and its root tree share one listing under two keys, sized and unsized.
_TREE_LISTING_CAPACITY = 32
# Listings are also bounded by their total entries, so the memory they hold
# follows the tree size rather than a fixed count of whole trees.
_TREE_LISTING_ENTRY_CAPACITY = 400_000
_TREE_LISTINGS: OrderedDict[tuple[tuple[str, int, int], str, bool], _Listing] = OrderedDict()
_TREE_LISTINGS_LOCK = threading.Lock()


# Parsed tree objects and commit roots are immutable per object ID; the budget
# counts entries across every remembered tree.
_PARSED_TREE_CAPACITY = 200_000
_PARSED_TREES: OrderedDict[tuple[tuple[str, int, int], str], dict[str, tuple[bytes, str]]] = (
    OrderedDict()
)
_PARSED_TREE_ENTRIES = 0
_COMMIT_ROOTS_CAPACITY = 256
_COMMIT_ROOTS: OrderedDict[tuple[tuple[str, int, int], str], object] = OrderedDict()
_UNREAD = object()
_PARSED_TREES_LOCK = threading.Lock()


_TREE_CHANGES_CAPACITY = 32
_TREE_CHANGES: OrderedDict[tuple[tuple[str, int, int], str, str], tuple[GitTreeChange, ...]] = (
    OrderedDict()
)
_TREE_CHANGES_LOCK = threading.Lock()


_LISTING_INDEX_CAPACITY = 16
_LISTING_INDEX_ENTRY_CAPACITY = 200_000
# id(listing) -> (the listing itself, path -> position, paths in sorted order).
# The listing is held so its id cannot be reused while the index is remembered.
_LISTING_INDEXES: OrderedDict[int, tuple[_Listing, dict[str, int], tuple[str, ...]]] = OrderedDict()
_LISTING_INDEXES_LOCK = threading.Lock()


def _listing_index(
    listing: _Listing,
) -> tuple[dict[str, int], tuple[str, ...]]:
    """Path -> position and the sorted paths, built once per remembered listing."""

    key = id(listing)
    with _LISTING_INDEXES_LOCK:
        indexed = _LISTING_INDEXES.get(key)
        if indexed is not None and indexed[0] is listing:
            _LISTING_INDEXES.move_to_end(key)
        else:
            positions = {entry.path: position for position, entry in enumerate(listing)}
            indexed = (listing, positions, tuple(sorted(positions)))
            _LISTING_INDEXES[key] = indexed
            _LISTING_INDEXES.move_to_end(key)
            # Bounded by count and by total entries, so the memory follows the
            # tree size; the newest index always stays.
            while len(_LISTING_INDEXES) > 1 and (
                len(_LISTING_INDEXES) > _LISTING_INDEX_CAPACITY
                or sum(len(item[0]) for item in _LISTING_INDEXES.values())
                > _LISTING_INDEX_ENTRY_CAPACITY
            ):
                _LISTING_INDEXES.popitem(last=False)
    return indexed[1], indexed[2]


def _listing_has_path(listing: _Listing, path: str) -> bool:
    """Whether a file, or a directory with any entry beneath it, is named path."""

    if isinstance(listing, _PersistentListing):
        if path in listing.rows:
            return True
        _, subtree = listing.rows.split_prefix(path + "/")
        return bool(subtree)
    positions, ordered = _listing_index(listing)
    if path in positions:
        return True
    prefix = path + "/"
    cursor = bisect.bisect_left(ordered, prefix)
    return cursor < len(ordered) and ordered[cursor].startswith(prefix)


def _listing_child_names(listing: _Listing, directory: str) -> tuple[str, ...]:
    """A directory's immediate child names, skipping each child's own subtree.

    A file child is one entry and advances by one. A directory child's first
    entry is ``child/...``; every sibling that shares its name as a prefix
    and sorts before ``child/`` (``child-x``, ``child.y``) was already passed,
    and every entry beneath ``child/`` sorts before ``child0`` ('/' < '0' and
    nothing sorts between them), so one bisect steps over exactly that
    subtree: the work tracks the number of children, not the subtree sizes.
    """

    if isinstance(listing, _PersistentListing):
        prefix = directory + "/" if directory else ""
        rows = listing.rows
        paths = rows.iter_from(prefix)
        persistent_names = []
        while (path := next(paths, None)) is not None and path.startswith(prefix):
            name, separator, _ = path[len(prefix) :].partition("/")
            persistent_names.append(name)
            if separator:
                # Seek past this subtree; file siblings advance the iterator
                # directly, with no AVL deletion or path-copy per child.
                paths = rows.iter_from(prefix + name + "0")
        return tuple(persistent_names)
    _positions, ordered = _listing_index(listing)
    prefix = directory + "/" if directory else ""
    names: list[str] = []
    cursor = bisect.bisect_left(ordered, prefix)
    while cursor < len(ordered) and ordered[cursor].startswith(prefix):
        name, separator, _rest = ordered[cursor][len(prefix) :].partition("/")
        names.append(name)
        if separator:
            cursor = bisect.bisect_left(ordered, prefix + name + "0", cursor + 1)
        else:
            cursor += 1
    return tuple(names)


def _select_from_listing(listing: _Listing, paths: Sequence[str]) -> tuple[GitTreeEntry, ...]:
    """A literal pathspec's selection from a whole listing, in listing order.

    Each requested path selects its exact entry, or every entry beneath it when
    it names a directory. An index built once per remembered listing makes an
    exact path a lookup and a directory a sorted range, so the work tracks the
    selection rather than the size of the tree.
    """

    if isinstance(listing, _PersistentListing):
        chosen: dict[str, GitTreeEntry] = {}
        for path in paths:
            row = listing.rows.get(path)
            if row is not None:
                chosen[path] = row
            _, subtree = listing.rows.split_prefix(path + "/")
            chosen.update(subtree.items())
        return tuple(
            entry if listing.sized else replace(entry, size=None)
            for _, entry in sorted(chosen.items())
        )
    positions, ordered = _listing_index(listing)
    selected: set[int] = set()
    for path in paths:
        exact = positions.get(path)
        if exact is not None:
            selected.add(exact)
        prefix = path + "/"
        cursor = bisect.bisect_left(ordered, prefix)
        while cursor < len(ordered) and ordered[cursor].startswith(prefix):
            selected.add(positions[ordered[cursor]])
            cursor += 1
    return tuple(listing[position] for position in sorted(selected))


@dataclass(frozen=True, slots=True)
class _PersistentListing(Sequence[GitTreeEntry]):
    rows: PersistentMap[GitTreeEntry]
    sized: bool = True

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[GitTreeEntry]:
        if self.sized:
            return iter(self.rows.values())
        return (replace(entry, size=None) for entry in self.rows.values())

    @overload
    def __getitem__(self, index: int) -> GitTreeEntry: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[GitTreeEntry, ...]: ...

    def __getitem__(self, index: int | slice) -> GitTreeEntry | tuple[GitTreeEntry, ...]:
        return tuple(self)[index]


_Listing = tuple[GitTreeEntry, ...] | _PersistentListing


def _listing_with(
    listing: _Listing,
    changes: Mapping[str, str | None],
    sizes: Mapping[str, int],
) -> _Listing:
    """A path-ordered listing with changed paths replaced, added or removed."""

    rows = (
        listing.rows
        if isinstance(listing, _PersistentListing)
        else PersistentMap((entry.path, entry) for entry in listing)
    )
    for path, oid in changes.items():
        if oid is None:
            rows = rows.delete(path)
        else:
            rows = rows.set(
                path,
                GitTreeEntry(
                    path=path, mode="100644", object_type="blob", oid=oid, size=sizes[path]
                ),
            )
    return _PersistentListing(rows)


def _remembered_listing(
    repository: tuple[str, int, int], oid: str, *, with_sizes: bool
) -> _Listing | None:
    with _TREE_LISTINGS_LOCK:
        key = (repository, oid, with_sizes)
        cached = _TREE_LISTINGS.get(key)
        if cached is not None:
            _TREE_LISTINGS.move_to_end(key)
            return cached
        sized = None if with_sizes else _TREE_LISTINGS.get((repository, oid, True))
    if sized is None:
        return None
    # A sized listing carries every unsized field; only the size differs. The
    # derived listing is remembered too, so repeated reads share one object.
    unsized = (
        _PersistentListing(sized.rows, sized=False)
        if isinstance(sized, _PersistentListing)
        else tuple(replace(entry, size=None) for entry in sized)
    )
    if unsized:
        _remember_listing(repository, oid, unsized)
    return unsized


def _remember_listing(repository: tuple[str, int, int], oid: str, listing: _Listing) -> None:
    # Seed once per cold listing. Repeated candidates fork this shared map;
    # leaving the cached seed as a tuple would rebuild all nodes per fork.
    if not isinstance(listing, _PersistentListing):
        listing = _PersistentListing(
            PersistentMap((entry.path, entry) for entry in listing),
            sized=listing[0].size is not None if listing else True,
        )
    key = (repository, oid, listing.sized)
    with _TREE_LISTINGS_LOCK:
        _TREE_LISTINGS[key] = listing
        _TREE_LISTINGS.move_to_end(key)
        while len(_TREE_LISTINGS) > 1 and (
            len(_TREE_LISTINGS) > _TREE_LISTING_CAPACITY
            or sum(len(item) for item in _TREE_LISTINGS.values()) > _TREE_LISTING_ENTRY_CAPACITY
        ):
            _TREE_LISTINGS.popitem(last=False)


def _batch_reader(path: Path) -> _BatchBlobReader:
    """The resident reader for this exact repository directory.

    The key includes the directory's device and inode, so a repository replaced
    at the same path never answers from a process holding the old one open.
    """

    key = _repository_key(path)
    retired: list[_BatchBlobReader] = []
    with _BATCH_READERS_LOCK:
        reader = _BATCH_READERS.get(key)
        if reader is None:
            reader = _BATCH_READERS[key] = _BatchBlobReader(Path(key[0]))
        _BATCH_READERS.move_to_end(key)
        while len(_BATCH_READERS) > _BATCH_READER_CAPACITY:
            retired.append(_BATCH_READERS.popitem(last=False)[1])
    for old in retired:
        with old._lock:
            old.close()
    return reader


def _before_fork() -> None:
    # A child inherits locks in whatever state they were in. Holding every
    # reader across fork means none is mid-request, so the child can drop its
    # copies of the pipes without disturbing the parent's protocol.
    _BATCH_READERS_LOCK.acquire()
    for reader in _BATCH_READERS.values():
        reader._lock.acquire()


def _after_fork_in_parent() -> None:
    for reader in _BATCH_READERS.values():
        reader._lock.release()
    _BATCH_READERS_LOCK.release()


def _after_fork_in_child() -> None:
    global _BATCH_READERS_LOCK, _TREE_LISTINGS_LOCK, _VERIFIED_COMMITS_LOCK
    global _LISTING_INDEXES_LOCK, _TREE_CHANGES_LOCK, _PACKED_REFS_LOCK, _CONFIG_READS_LOCK
    global _PARSED_TREES_LOCK, _ENCODED_TREES_LOCK
    inherited = tuple(_BATCH_READERS.values())
    _BATCH_READERS.clear()
    for reader in inherited:
        reader.forget_inherited()
    # The memos themselves are consistent (the GIL completes each operation);
    # only a lock another thread held at fork time would never be released.
    _BATCH_READERS_LOCK = threading.Lock()
    _TREE_LISTINGS_LOCK = threading.Lock()
    _VERIFIED_COMMITS_LOCK = threading.Lock()
    _LISTING_INDEXES_LOCK = threading.Lock()
    _TREE_CHANGES_LOCK = threading.Lock()
    _PACKED_REFS_LOCK = threading.Lock()
    _CONFIG_READS_LOCK = threading.Lock()
    _PARSED_TREES_LOCK = threading.Lock()
    _ENCODED_TREES_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )


@atexit.register
def _close_batch_readers() -> None:
    with _BATCH_READERS_LOCK:
        readers = tuple(_BATCH_READERS.values())
    for reader in readers:
        reader.close()


@timed_phase("git")
def _command(
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one Git command, optionally under a deadline it cannot outlive.

    A bounded run gets its own session so the deadline reaches the whole tree.
    Git delegates the network to a transport child -- `git-remote-https`, `ssh`
    -- and killing only the process Python started leaves that child holding the
    connection, so a timeout that killed the parent alone would report a
    deadline it had not actually enforced.
    """

    merged_environment = _command_environment(environment)
    if timeout is None:
        result = subprocess.run(
            list(arguments),
            input=input_bytes,
            capture_output=True,
            check=False,
            env=merged_environment,
        )
    else:
        result = _bounded_command(
            list(arguments),
            input_bytes=input_bytes,
            environment=merged_environment,
            timeout=timeout,
        )
    if check and result.returncode != 0:
        # Do not echo command arguments or stderr: Git signing failures can
        # include managed credential paths, which inspection/logging must not expose.
        command = next((arg for arg in arguments[1:] if not arg.startswith("-")), "git")
        raise GitError(
            f"system Git operation {command!r} failed with exit code {result.returncode}"
        )
    return result


def _bounded_command(
    arguments: list[str],
    *,
    input_bytes: bytes | None,
    environment: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run one command under a deadline, killing its whole process group on expiry."""

    with subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environment),
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):  # pragma: no cover - already gone
                process.kill()
            # Drain without a second deadline: the group is dead, so the pipes
            # are at EOF and this cannot block.
            process.communicate()
            raise
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


__all__ = [
    "LEGACY_NOTE_REFS",
    "MIRROR_PUSH_TIMEOUT_SECONDS",
    "NOTE_REFS",
    "GitLedger",
    "GitTreeChange",
    "GitTreeEntry",
]

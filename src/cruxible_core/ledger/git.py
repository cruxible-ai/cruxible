"""System-Git ledger primitives for Playbill generation zero."""

from __future__ import annotations

import atexit
import bisect
import fcntl
import hashlib
import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cruxible_client.contracts.canonical import CandidateDigest, normalize_manifest_paths
from cruxible_client.contracts.errors import PlaybillGitError
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

# Every Playbill note ref, in one table. The generation descriptor was the
# first; the proposal evaluation and the approval list are projections of the
# evidence store that reach Git through exactly the same write, so a note a
# reviewer reads is never a second mechanism with its own persistence rules.
NOTE_REFS: Final[Mapping[str, str]] = {
    "generation": "refs/notes/playbill-gen",
    "evaluation": "refs/notes/playbill-eval",
    "approval": "refs/notes/playbill-approval",
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
        raise PlaybillGitError("commit message must be a nonblank prose summary")
    if "\x00" in message:
        raise PlaybillGitError("commit message must not contain a NUL byte")


def _entry_size(entry: GitTreeEntry) -> int:
    if entry.size is None:
        raise PlaybillGitError(f"ledger blob has no size: {entry.path}")
    return entry.size


def _proven_blob_entries(entries: tuple[GitTreeEntry, ...]) -> tuple[GitTreeEntry, ...]:
    """Refuse a tree member that is anything but a plain committed file.

    A symlink, a submodule or an executable bit reaches a reader as something
    other than the bytes the ledger claims to carry, so every path Playbill
    hands out — read, listed, or fetched by name — passes this one proof.
    """

    for entry in entries:
        if entry.object_type != "blob" or entry.mode != "100644":
            raise PlaybillGitError(
                f"ledger tree contains unsupported {entry.mode} {entry.object_type}: {entry.path}"
            )
    return entries


class GitLedger:
    """A daemon-owned bare repository accessed only through system Git."""

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
            raise PlaybillGitError(f"ledger path already exists: {path}")
        _command(["git", "init", "--bare", f"--object-format={object_format}", str(path)])
        ledger = cls(
            path,
            signing_key_path=signing_key_path,
            allowed_signers_path=allowed_signers_path,
        )
        ledger.configure_signing()
        ledger._git(["symbolic-ref", "HEAD", "refs/heads/main"])
        if ledger.object_format() != object_format:
            raise PlaybillGitError("initialized ledger object format does not match request")
        return ledger

    def configure_signing(self) -> None:
        settings = {
            "user.name": "playbill-daemon",
            "user.email": "daemon@playbill.invalid",
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
        value = self._git(["rev-parse", "--show-object-format"]).decode().strip()
        if value not in {"sha1", "sha256"}:
            raise PlaybillGitError(f"unsupported Git object format: {value!r}")
        self._object_format_cache = cast(GitObjectFormat, value)
        return self._object_format_cache

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
            "GIT_AUTHOR_NAME": "playbill-daemon",
            "GIT_AUTHOR_EMAIL": "daemon@playbill.invalid",
            "GIT_COMMITTER_NAME": "playbill-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@playbill.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        oid = (
            self._git(
                ["commit-tree", "-S", tree_oid, "-m", "Initialize Playbill instance"],
                environment=commit_environment,
            )
            .decode()
            .strip()
        )
        self._validate_oid(oid)
        if self.parent_of(oid) is not None:
            raise PlaybillGitError("genesis commit unexpectedly has a parent")
        if not self.verify_commit(oid):
            raise PlaybillGitError("new genesis commit signature does not verify")
        return oid

    def create_signed_generation(
        self,
        tree: Mapping[str, bytes],
        *,
        parent_oid: str,
        sequence: int,
        timestamp: str,
        message: str,
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
            raise PlaybillGitError("non-genesis generation sequence must be positive")
        _validate_commit_message(message)
        tree_oid = (
            self._write_tree(tree, accepted_parent=parent_oid)
            if extends_tree is None
            else self._extend_tree(extends_tree, tree, base_rows=extends_rows)
        )
        environment = {
            "GIT_AUTHOR_NAME": "playbill-daemon",
            "GIT_AUTHOR_EMAIL": "daemon@playbill.invalid",
            "GIT_COMMITTER_NAME": "playbill-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@playbill.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        # Marked before the commit exists, so a crash at any later point leaves
        # a marker for recovery's collection of unsettled generations. The
        # attempt keeps recovery from treating the marker or commit as residue
        # while this writer is live; callers that go on to activate hold one
        # across both.
        with self.generation_attempt():
            return self._commit_generation(tree_oid, parent_oid, message, environment)

    def _commit_generation(
        self,
        tree_oid: str,
        parent_oid: str,
        message: str,
        environment: Mapping[str, str],
    ) -> str:
        pending = self._mark_generation_in_flight(f"pending-{secrets.token_hex(8)}")
        oid = (
            self._git(
                [
                    "commit-tree",
                    "-S",
                    tree_oid,
                    "-p",
                    parent_oid,
                    "-m",
                    message,
                ],
                environment=environment,
            )
            .decode()
            .strip()
        )
        self._validate_oid(oid)
        self._mark_generation_in_flight(oid)
        pending.unlink()
        _fsync_directory(pending.parent)
        if self.parent_of(oid) != parent_oid:
            raise PlaybillGitError("new generation commit parent differs from settlement base")
        if not self.verify_commit(oid):
            raise PlaybillGitError("new generation commit signature does not verify")
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

    def _store_loose_objects(self, objects: Mapping[str, tuple[str, bytes]]) -> None:
        """Write objects exactly as Git writes a loose object, without a Git process.

        Each object is the zlib stream of ``<type> <size>\\0<body>`` at
        ``objects/<2 hex>/<rest>``: written to a temporary file in that
        directory, fsynced, then hard-linked into place, which is Git's own
        create-only publication (an existing object is left untouched). The
        directory is fsynced too, so the durability matches ``core.fsync``
        covering loose objects. Git reads these like any object it wrote.
        """

        root = self.path / "objects"
        touched: set[Path] = set()
        for oid, (kind, body) in objects.items():
            if self._object_oid(kind, body) != oid:
                raise PlaybillGitError("object bytes differ from their content address")
            directory = root / oid[:2]
            final = directory / oid[2:]
            if final.exists():
                continue
            directory.mkdir(mode=0o755, exist_ok=True)
            header = f"{kind} {len(body)}".encode("ascii") + b"\x00"
            descriptor, temporary = tempfile.mkstemp(prefix="tmp_obj_", dir=directory)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(zlib.compress(header + body))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o444)
                try:
                    os.link(temporary, final)
                except FileExistsError:
                    pass
            finally:
                os.unlink(temporary)
            touched.add(directory)
        for directory in touched:
            _fsync_directory(directory)

    def _tree_entries_of(self, tree_oid: str) -> dict[str, tuple[bytes, str]]:
        found = _batch_reader(self.path).objects((tree_oid,))[tree_oid]
        if found is None or found[0] != "tree":
            raise PlaybillGitError(f"ledger tree object is unavailable: {tree_oid}")
        raw_length = 20 if self.object_format() == "sha1" else 32
        entries = _tree_entries(found[1], raw_length=raw_length)
        if entries is None:
            raise PlaybillGitError(f"ledger tree object is malformed: {tree_oid}")
        return entries

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

        entries = {} if tree_oid is None else self._tree_entries_of(tree_oid)
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
            current = entries.get(name)
            if oid is None and current is not None and current[0] != b"40000":
                entries.pop(name)
        for name, nested in subtrees.items():
            current = entries.get(name)
            if current is not None and current[0] != b"40000":
                raise PlaybillGitError(f"ledger path is both a file and a directory: {name}")
            child = self._apply_to_tree(None if current is None else current[1], nested, written)
            if child is None:
                entries.pop(name, None)
            else:
                entries[name] = (b"40000", child)
        for name, oid in files.items():
            if oid is None:
                continue
            current = entries.get(name)
            if current is not None and current[0] == b"40000":
                raise PlaybillGitError(f"ledger path is both a file and a directory: {name}")
            entries[name] = (b"100644", oid)
        if not entries:
            return None
        body = b"".join(
            mode
            + b" "
            + name.encode("utf-8", errors="surrogateescape")
            + b"\x00"
            + bytes.fromhex(oid)
            for name, (mode, oid) in sorted(
                entries.items(),
                key=lambda item: (
                    item[0].encode("utf-8", errors="surrogateescape")
                    + (b"/" if item[1][0] == b"40000" else b"")
                ),
            )
        )
        oid = self._object_oid("tree", body)
        written[oid] = ("tree", body)
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
        self._store_loose_objects(written)
        return root

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
        their paths are written, as loose objects, with no Git process. The
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
                raise PlaybillGitError(collision_message)
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
                raise PlaybillGitError("different blob bytes share a computed content address")
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
                raise PlaybillGitError("different blob bytes share a computed content address")
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
        shared = None if base_rows is None else edited_paths(tree, base_rows)
        base: tuple[GitTreeEntry, ...] | None = None
        if base_rows is not None and shared is not None:
            if any(path in base_rows and path not in tree for path in shared):
                raise PlaybillGitError("extended tree does not carry every member of its base")
            added = [path for path in shared if path in tree and path not in base_rows]
        else:
            base = self._list_tree(base_tree, with_sizes=True)
            base_paths = {entry.path for entry in base}
            if any(entry.object_type != "blob" for entry in base) or not base_paths <= set(tree):
                raise PlaybillGitError("extended tree does not carry every member of its base")
            added = [path for path in tree if path not in base_paths]
        ordered_added = normalize_manifest_paths(added)
        if set(ordered_added) != set(added):
            raise PlaybillGitError("extended tree adds a path that is not normalized")
        oids = {path: self._blob_oid(tree[path]) for path in ordered_added}
        blobs: dict[str, bytes] = {}
        for path in ordered_added:
            blob_oid = oids[path]
            if blob_oid in blobs and blobs[blob_oid] != tree[path]:
                raise PlaybillGitError("different blob bytes share a computed content address")
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
            raise PlaybillGitError("proposal transport may update only canonical proposal refs")
        actor_namespace = target_ref.split("/")[2]
        if actor_namespace != actor_id:
            raise PlaybillGitError("proposal ref namespace differs from authenticated actor")
        current = self.read_proposal_ref(target_ref)
        if current != expected_ref_oid:
            raise PlaybillGitError("proposal ref moved before its parent-bound update")

        tree_oid = self._write_tree(
            tree,
            collision_message="proposal paths collide after normalization",
            accepted_parent=base_oid,
        )

        commit_environment = {
            "GIT_AUTHOR_NAME": actor_id,
            "GIT_AUTHOR_EMAIL": f"{actor_id}@proposal.playbill.invalid",
            "GIT_COMMITTER_NAME": "playbill-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@playbill.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        commit_oid = (
            self._git(
                [
                    "commit-tree",
                    tree_oid,
                    "-p",
                    base_oid,
                    "-m",
                    message,
                ],
                environment=commit_environment,
            )
            .decode()
            .strip()
        )
        self._validate_oid(commit_oid)
        zero_oid = "0" * (40 if self.object_format() == "sha1" else 64)
        self._git(
            ["update-ref", target_ref, commit_oid, expected_ref_oid or zero_oid],
        )
        return commit_oid, tree_oid

    def read_proposal_ref(self, target_ref: str) -> str | None:
        if not _PROPOSAL_REF_RE.fullmatch(target_ref):
            raise PlaybillGitError("proposal transport may read only canonical proposal refs")
        return self._resolve_ref(target_ref)

    def retain_proposal_review(self, proposal_id: str, oid: str) -> None:
        """Protect a completed active admission independently of its reusable author ref."""
        ref = "refs/heads/proposals/" + proposal_id.removeprefix("sha256:")
        if not _PROPOSAL_REVIEW_REF_RE.fullmatch(ref):
            raise PlaybillGitError("proposal review ref name is malformed")
        self._validate_oid(oid)
        self._git(["update-ref", ref, oid])

    def proposal_refs(self) -> dict[str, str]:
        """Snapshot the current author slots, including interrupted publications."""
        return {
            ref: oid
            for line in self._git(
                ["for-each-ref", "--format=%(objectname) %(refname)", "refs/proposals/"]
            )
            .decode()
            .splitlines()
            for oid, ref in (line.split(" ", 1),)
        }

    def _archive_update(self, oids: Sequence[str]) -> list[str]:
        """Build a fixed-tree retention chain; callers CAS its head with ref removal."""
        previous = (
            self._git(["for-each-ref", "--format=%(objectname)", PROPOSAL_ARCHIVE_REF])
            .decode()
            .strip()
        )
        tip = previous
        empty_tree = None
        for oid in sorted(set(oids)):
            self._validate_oid(oid)
            if tip and self.is_ancestor(oid, tip):
                continue
            if (
                not self.object_exists(oid)
                or self._git(["cat-file", "-t", oid]).strip() != b"commit"
            ):
                raise PlaybillGitError("proposal archive target is not a retained commit")
            if empty_tree is None:
                empty_tree = self._git(["mktree"], input_bytes=b"").decode().strip()
            parents = ([tip] if tip else []) + [oid]
            # No growing manifest/tree and no authoring ancestry. The archive is
            # unsigned retention metadata, outside accepted-state authority.
            raw = (
                f"tree {empty_tree}\n"
                + "".join(f"parent {parent}\n" for parent in parents)
                + "author playbill-daemon <daemon@playbill.invalid> 0 +0000\n"
                + "committer playbill-daemon <daemon@playbill.invalid> 0 +0000\n\n"
                + "Retain closed proposal\n"
            ).encode()
            tip = (
                self._git(["hash-object", "-t", "commit", "-w", "--stdin"], input_bytes=raw)
                .decode()
                .strip()
            )
        if tip == previous:
            # Deleting a duplicate root still relies on this exact archive head.
            return [f"verify {PROPOSAL_ARCHIVE_REF} {previous}"] if oids and previous else []
        return [f"update {PROPOSAL_ARCHIVE_REF} {tip} {previous or '0' * len(tip)}"]

    def archive_proposal_commits(self, oids: Sequence[str]) -> None:
        """Retain a completed refusal before its author slot can be reused."""
        commands = self._archive_update(oids)
        if commands:
            self._git(
                ["update-ref", "--stdin"],
                input_bytes=("\n".join(["start", *commands, "prepare", "commit"]) + "\n").encode(),
            )

    def replace_proposal_review_refs(
        self, refs: Mapping[str, str], *, retired_targets: Mapping[str, str] | None = None
    ) -> None:
        """Archive closed candidates atomically with releasing their public/private refs."""
        normalized = {}
        for proposal_id, oid in refs.items():
            ref = f"refs/heads/proposals/{proposal_id}"
            if not _PROPOSAL_REVIEW_REF_RE.fullmatch(ref):
                raise PlaybillGitError("proposal review ref name is malformed")
            self._validate_oid(oid)
            normalized[ref] = oid
        current = {
            ref: oid
            for line in self._git(
                ["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/proposals/"]
            )
            .decode()
            .splitlines()
            for oid, ref in (line.split(" ", 1),)
        }
        retired = {ref: oid for ref, oid in current.items() if ref not in normalized}
        for ref, oid in (retired_targets or {}).items():
            if not _PROPOSAL_REF_RE.fullmatch(ref):
                raise PlaybillGitError("proposal ref name is malformed")
            self._validate_oid(oid)
            retired[ref] = oid
        # One local conversion only: fold old per-proposal pins into the same
        # chain before deleting them. Unrelated refs in this namespace refuse.
        for line in (
            self._git(["for-each-ref", "--format=%(objectname) %(refname)", "refs/settled/"])
            .decode()
            .splitlines()
        ):
            oid, ref = line.split(" ", 1)
            if ref == PROPOSAL_ARCHIVE_REF:
                continue
            if not _LEGACY_SETTLED_RE.fullmatch(ref):
                raise PlaybillGitError("unrecognized local proposal archive ref")
            retired[ref] = oid
        commands = self._archive_update(tuple(retired.values()))
        commands.extend(
            f"update {ref} {oid} {current.get(ref, '0' * len(oid))}"
            for ref, oid in sorted(normalized.items())
            if current.get(ref) != oid
        )
        commands.extend(f"delete {ref} {oid}" for ref, oid in sorted(retired.items()))
        if commands:
            self._git(
                ["update-ref", "--stdin"],
                input_bytes=("\n".join(["start", *commands, "prepare", "commit"]) + "\n").encode(),
            )

    def mirror_refs(self) -> dict[str, str]:
        """Capture owned public refs, excluding private proposal/pinning refs."""
        rows = self._git(
            [
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                _MIRROR_MAIN,
                *NOTE_REFS.values(),
                PROPOSAL_ARCHIVE_REF,
                *_MIRROR_PREFIXES,
            ]
        )
        refs: dict[str, str] = {}
        for line in rows.decode("utf-8").splitlines():
            oid, _, ref = line.partition(" ")
            if self._mirror_owned_ref(ref):
                refs[ref] = oid
        return self._validate_mirror_snapshot(refs)

    @staticmethod
    def _mirror_owned_ref(ref: str) -> bool:
        return (
            ref in (_MIRROR_MAIN, PROPOSAL_ARCHIVE_REF)
            or ref in NOTE_REFS.values()
            or ref.startswith(_MIRROR_PREFIXES)
        )

    def _validate_mirror_snapshot(
        self, refs: Mapping[str, str], *, require_main: bool = True
    ) -> dict[str, str]:
        if len(refs) > _MIRROR_MAX_REFS:
            raise PlaybillGitError("mirror snapshot exceeds the ref count limit")
        result = dict(refs)
        for ref, oid in result.items():
            if _LEGACY_SETTLED_RE.fullmatch(ref):
                raise PlaybillGitError(
                    "mirror state uses retired per-proposal archive refs; "
                    "bind a new mirror URL to start a fresh publication snapshot"
                )
            valid = ref in (_MIRROR_MAIN, PROPOSAL_ARCHIVE_REF) or ref in NOTE_REFS.values()
            valid = valid or bool(_PROPOSAL_REVIEW_REF_RE.fullmatch(ref))
            if not valid:
                raise PlaybillGitError("mirror snapshot contains an unowned or malformed ref")
            self._validate_oid(oid)
        if require_main and _MIRROR_MAIN not in result:
            raise PlaybillGitError("mirror snapshot omits accepted main")
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
                {} if expected_remote is None else expected_remote, require_main=False
            )
            attempted = self._validate_mirror_snapshot(
                {} if previous_attempt is None else previous_attempt, require_main=False
            )
            if PROPOSAL_ARCHIVE_REF not in desired and (
                PROPOSAL_ARCHIVE_REF in expected or PROPOSAL_ARCHIVE_REF in attempted
            ):
                raise PlaybillGitError(
                    "local proposal archive is missing; restore it before publication"
                )
            owned = set(desired) | set(expected) | set(attempted)
            # Refuse rather than split the atomic update across commands.
            planned = [f"{desired.get(ref, '')}:{ref}" for ref in owned]
            planned += [f"--force-with-lease={ref}:{'0' * 64}" for ref in owned]
            if (
                sum(len(arg.encode()) + 1 for arg in [url, str(self.path), *planned])
                > _MIRROR_ARG_BYTES
            ):
                raise PlaybillGitError("mirror snapshot exceeds the atomic push argument limit")
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
                        raise PlaybillGitError(
                            "remote mirror still has per-proposal archive refs; "
                            "bind a new mirror URL or explicitly migrate/reset this remote"
                        )
                    if not separator or not self._mirror_owned_ref(ref) or ref in remote:
                        raise PlaybillGitError("remote mirror advertisement is malformed")
                    remote[ref] = oid
                remote = self._validate_mirror_snapshot(remote, require_main=False)
                for ref in set(remote) | owned:
                    actual = remote.get(ref)
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
                    raise PlaybillGitError(f"remote mirror ref diverged: {ref}")
                remote_archive = remote.get(PROPOSAL_ARCHIVE_REF)
                if remote_archive is not None and (
                    PROPOSAL_ARCHIVE_REF not in desired
                    or not self.is_ancestor(remote_archive, desired[PROPOSAL_ARCHIVE_REF])
                ):
                    raise PlaybillGitError(
                        "proposal archive cannot be deleted or rewound; "
                        "restore its retained history"
                    )
                remote_main = remote.get(_MIRROR_MAIN)
                if remote_main is not None and remote_main != desired[_MIRROR_MAIN]:
                    if not self.is_ancestor(remote_main, desired[_MIRROR_MAIN]):
                        raise PlaybillGitError(
                            "remote accepted main is not an ancestor of the snapshot"
                        )
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
                    raise PlaybillGitError("mirror snapshot exceeds the atomic push argument limit")
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
        except (PlaybillGitError, OSError, UnicodeError) as exc:
            return str(exc)[:500]

    @staticmethod
    def _mirror_failure(result: subprocess.CompletedProcess[bytes], operation: str) -> str:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return (detail or f"git {operation} exited {result.returncode}")[:500]

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
            "GIT_COMMITTER_NAME": "playbill-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@playbill.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }

    def review_commit_context(self) -> bytes:
        """Read the mutable Git configuration that affects review commit bytes."""
        return self._config_read(["config", "--default", "UTF-8", "--get", "i18n.commitencoding"])

    def _review_commit_identities(self, actor_id: str, timestamp: str) -> dict[str, str]:
        """Git's author/committer identity lines and commit encoding for a review commit.

        For a plain actor id and the canonical UTC timestamp, Git's ident
        normalization changes nothing and its date parser yields the whole
        seconds at ``+0000``, so the lines are formed here without a Git
        process per proposal. Anything else is left to ``git var -l``.
        """

        if _PLAIN_ACTOR_RE.fullmatch(actor_id):
            try:
                instant = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                instant = None
            if instant is not None and instant.year >= 1970:
                date = f"{int(instant.timestamp())} +0000"
                encoding = self.review_commit_context().decode("utf-8").strip() or "UTF-8"
                return {
                    "GIT_AUTHOR_IDENT": (
                        f"{actor_id} <{actor_id}@proposal.playbill.invalid> {date}"
                    ),
                    "GIT_COMMITTER_IDENT": f"playbill-daemon <daemon@playbill.invalid> {date}",
                    "i18n.commitencoding": encoding,
                }
        return dict(
            line.partition("=")[::2]
            for line in self._config_read(
                ["var", "-l"], environment=self._review_commit_environment(actor_id, timestamp)
            )
            .decode("utf-8")
            .splitlines()
            if "=" in line
        )

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
            raise PlaybillGitError("Git omitted review commit identities")
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
        oid = (
            self._git(
                [
                    "commit-tree",
                    tree_oid,
                    "-p",
                    base_oid,
                    "-m",
                    message,
                ],
                environment=environment,
            )
            .decode()
            .strip()
        )
        self._validate_oid(oid)
        expected = self.proposal_review_commit_oid(
            tree_oid=tree_oid,
            base_oid=base_oid,
            actor_id=actor_id,
            timestamp=timestamp,
            message=message,
        )
        if oid != expected:
            raise PlaybillGitError("Git review commit differs from its derived representation")
        if self.tree_oid(oid) != tree_oid or self.parent_of(oid) != base_oid:
            raise PlaybillGitError("proposal review commit does not reproduce its evidence")
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
                raise PlaybillGitError("review object has conflicting expected types")
            expected[oid] = kind
        objects = self._read_review_objects(expected)
        if any(oid not in objects for oid in dependencies):
            raise PlaybillGitError("review commit tree or parent is missing")
        presence = {oid: oid in objects for oid in oids}
        note_oids = self._review_note_oids(tuple(oid for oid, exists in presence.items() if exists))
        blobs = self._read_review_objects({oid: "blob" for oid in note_oids.values()})
        if len(blobs) != len(set(note_oids.values())):
            raise PlaybillGitError("review note body is missing")
        notes = {
            (kind, oid): blobs[note_oids[kind, oid]] if (kind, oid) in note_oids else None
            for kind in ("evaluation", "approval")
            for oid in presence
        }
        return presence, notes

    def _batch_paths(self, expressions: Sequence[str], kind: str) -> dict[str, str]:
        """Resolve selected tree paths using Git's structured stdin protocol."""
        found = {}
        for start in range(0, len(expressions), 256):
            batch = expressions[start : start + 256]
            rows = self._git(
                ["--no-replace-objects", "cat-file", "--batch-check"],
                input_bytes=("\n".join(batch) + "\n").encode("ascii"),
            ).splitlines()
            if len(rows) != len(batch):
                raise PlaybillGitError("Git note lookup returned an incomplete batch")
            for expression, row in zip(batch, rows):
                if row == (expression + " missing").encode("ascii"):
                    continue
                try:
                    oid, actual_kind, size = row.decode("ascii").split()
                    self._validate_oid(oid)
                    valid = actual_kind == kind and int(size) >= 0
                except (UnicodeError, ValueError) as exc:
                    raise PlaybillGitError("Git note lookup returned malformed metadata") from exc
                if not valid:
                    raise PlaybillGitError("Git note lookup returned the wrong object type")
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
                    raise PlaybillGitError("review notes ref has no retained tree")
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
                    raise PlaybillGitError("review notes repeat an annotated object")
                found[key] = oid
        return found

    def _read_review_objects(self, expected: Mapping[str, str]) -> dict[str, bytes]:
        """Batch exact object bytes with positional, type, and Git-hash proofs."""
        ordered = tuple(expected)
        for oid in ordered:
            self._validate_oid(oid)
        objects: dict[str, bytes] = {}
        for start in range(0, len(ordered), 128):
            batch = ordered[start : start + 128]
            output = self._git(
                ["--no-replace-objects", "cat-file", "--batch"],
                input_bytes=("\n".join(batch) + "\n").encode("ascii"),
            )
            position = 0
            for oid in batch:
                end = output.find(b"\n", position)
                if end < 0:
                    raise PlaybillGitError("Git review object output has no header")
                header = output[position:end]
                position = end + 1
                if header == f"{oid} missing".encode("ascii"):
                    continue
                try:
                    actual, kind, raw_size = header.decode("ascii").split()
                    size = int(raw_size)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise PlaybillGitError("Git review object metadata is malformed") from exc
                if actual != oid or kind != expected[oid] or size < 0:
                    raise PlaybillGitError("Git review object differs from the requested object")
                end = position + size
                if end >= len(output) or output[end : end + 1] != b"\n":
                    raise PlaybillGitError("Git review object payload is truncated")
                body = output[position:end]
                preimage = f"{kind} {size}".encode("ascii") + b"\x00" + body
                digest = (
                    hashlib.sha1(preimage).hexdigest()  # noqa: S324 - Git object identity
                    if self.object_format() == "sha1"
                    else hashlib.sha256(preimage).hexdigest()
                )
                if digest != oid:
                    raise PlaybillGitError("Git review object bytes do not reproduce its OID")
                objects[oid] = body
                position = end + 1
            if position != len(output):
                raise PlaybillGitError("Git review object output has trailing bytes")
        return objects

    def tree_oid(self, commit_oid: str) -> str:
        """A commit's tree (a tree names itself), from hash-checked object bytes."""

        self._validate_oid(commit_oid)
        tree = self._commit_tree(commit_oid)
        if tree is not None:
            return tree
        found = _batch_reader(self.path).objects((commit_oid,))[commit_oid]
        if found is None or found[0] != "tree":
            raise PlaybillGitError(f"ledger object names no tree: {commit_oid}")
        return commit_oid

    def set_main_genesis(self, oid: str) -> None:
        self._validate_oid(oid)
        zero_oid = "0" * (40 if self.object_format() == "sha1" else 64)
        self._git(["update-ref", "refs/heads/main", oid, zero_oid])

    def compare_and_set_main(self, oid: str, *, expected_oid: str) -> bool:
        """Advance main exactly once over its expected parent, or report a race loss."""

        self._validate_oid(oid)
        self._validate_oid(expected_oid)
        if self.parent_of(oid) != expected_oid:
            raise PlaybillGitError("main CAS target is not parented by the expected OID")
        result = _command(
            [
                "git",
                f"--git-dir={self.path}",
                "update-ref",
                "refs/heads/main",
                oid,
                expected_oid,
            ],
            check=False,
        )
        if result.returncode == 0:
            return True
        if self.read_main() != expected_oid:
            return False
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PlaybillGitError(f"main CAS failed without a competing ref update: {detail}")

    @contextmanager
    def activation_lock(self) -> Iterator[None]:
        """Serialize activation/publication and targeted loser collection across processes."""

        with self._exclusive_lock("playbill-activation.lock"):
            yield

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

    def collect_unreachable_generation(self, oid: str) -> tuple[str, ...]:
        """Delete only loose objects proven reachable solely from one losing generation."""

        self._validate_oid(oid)
        if self.read_main() == oid:
            raise PlaybillGitError("refusing to collect the accepted main generation")
        reachable_commits = set(self._git(["rev-list", "--all"]).decode().splitlines())
        if oid in reachable_commits:
            raise PlaybillGitError("refusing to collect a generation reachable from refs")
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
            raise PlaybillGitError("losing generation is not an independently collectable object")
        for object_id in object_ids:
            self._validate_oid(object_id)
        deleted: list[str] = []
        fsync_directories: set[Path] = set()
        for object_id in object_ids:
            path = self.path / "objects" / object_id[:2] / object_id[2:]
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise PlaybillGitError("loose Git object cleanup target is not a regular file")
            path.unlink()
            deleted.append(object_id)
            fsync_directories.add(path.parent)
        for directory in sorted(fsync_directories, key=str):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return tuple(deleted)

    def object_exists(self, oid: str) -> bool:
        self._validate_oid(oid)
        return _batch_reader(self.path).objects((oid,))[oid] is not None

    def unreachable_commits(self) -> tuple[str, ...]:
        """List unreachable commit OIDs without pruning or mutating object storage."""

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

    def _note_ref(self, kind: str) -> str:
        ref = NOTE_REFS.get(kind)
        if ref is None:
            raise PlaybillGitError(f"unknown Playbill note kind: {kind!r}")
        return ref

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
        ref = self._note_ref(kind)
        arguments = ["notes", f"--ref={ref}", "add"]
        if replace:
            arguments.append("-f")
        arguments.extend(("-F", "-", oid))
        with self._note_lock():
            self._git(arguments, input_bytes=content)
        if self._read_note(kind, oid) != content:
            raise PlaybillGitError(f"{kind} note did not persist exactly")

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
            entries = _tree_entries(found[1], raw_length=len(oid) // 2)
            if entries is None:
                return _ASK_GIT
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
            raise PlaybillGitError("generation note target is not the current main ref")
        if self.read_generation_note(oid) is not None:
            raise PlaybillGitError("generation already carries a descriptor note")
        self._write_note("generation", oid, content, replace=False)

    def write_recovered_generation_note(self, oid: str, content: bytes) -> None:
        """Repair a missing note only for a replay-proven commit on accepted main."""

        self._validate_oid(oid)
        if not self.is_ancestor(oid, self.read_main()):
            raise PlaybillGitError("recovered generation note target is outside main history")
        if self.read_generation_note(oid) is not None:
            raise PlaybillGitError("generation already carries a descriptor note")
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
            raise PlaybillGitError(f"unknown Playbill proposal note kind: {kind!r}")
        self._write_note(kind, oid, content, replace=True)

    def read_proposal_note(self, kind: str, oid: str) -> bytes | None:
        if kind not in {"evaluation", "approval"}:
            raise PlaybillGitError(f"unknown Playbill proposal note kind: {kind!r}")
        return self._read_note(kind, oid)

    def read_proposal_notes(
        self, pairs: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], bytes | None]:
        """Read several proposal notes in one Git process, resolving refs now.

        Git notes stores a target under zero or more two-hex-digit fanout
        directories (git/git notes.c construct_path_with_fanout), so each target
        is asked for at every path it could occupy; at most one exists.
        """

        wanted: dict[str, tuple[str, str]] = {}
        for kind, oid in pairs:
            if kind not in {"evaluation", "approval"}:
                raise PlaybillGitError(f"unknown Playbill proposal note kind: {kind!r}")
            self._validate_oid(oid)
            ref = self._note_ref(kind)
            for split in range(0, len(oid), 2):
                path = "/".join([oid[i : i + 2] for i in range(0, split, 2)] + [oid[split:]])
                wanted[f"{ref}:{path}"] = (kind, oid)
        found: dict[tuple[str, str], bytes] = {}
        if wanted:
            ordered = tuple(wanted)
            output = self._git(
                ["--no-replace-objects", "cat-file", "--batch"],
                input_bytes=("\n".join(ordered) + "\n").encode("ascii"),
            )
            position = 0
            for expression in ordered:
                end = output.find(b"\n", position)
                if end < 0:
                    raise PlaybillGitError("Git note output has no header")
                header = output[position:end]
                position = end + 1
                if header == f"{expression} missing".encode("ascii"):
                    continue
                try:
                    _oid, object_type, raw_size = header.decode("ascii").split()
                    size = int(raw_size)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise PlaybillGitError("Git note metadata is malformed") from exc
                if object_type != "blob" or size < 0:
                    raise PlaybillGitError("Git note is not a blob")
                end = position + size
                if end >= len(output) or output[end : end + 1] != b"\n":
                    raise PlaybillGitError("Git note payload is truncated")
                key = wanted[expression]
                if key in found:
                    raise PlaybillGitError("Git notes repeat an annotated object")
                found[key] = output[position:end]
                position = end + 1
            if position != len(output):
                raise PlaybillGitError("Git note output has trailing bytes")
        return {pair: found.get(pair) for pair in pairs}

    def read_main(self) -> str:
        oid = self._resolve_ref("refs/heads/main")
        if oid is None:
            raise PlaybillGitError("ledger has no main ref")
        return oid

    def parent_of(self, oid: str) -> str | None:
        self._validate_oid(oid)
        # A commit's parents are part of its immutable bytes: read its headers
        # through the resident reader instead of spawning `rev-list`.
        found = _batch_reader(self.path).objects((oid,))[oid]
        if found is None or found[0] != "commit":
            raise PlaybillGitError(f"ledger object is not a commit: {oid}")
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
        raise PlaybillGitError("Playbill refuses merge commits on main")

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
                raise PlaybillGitError(
                    f"ledger tree contains unsupported {change.mode} member: {change.path}"
                )
            if (change.status == "A") != (change.path not in parent_tree):
                raise PlaybillGitError(f"tree delta differs from its proven parent: {change.path}")
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
                raise PlaybillGitError(
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
                raise PlaybillGitError(
                    f"ledger tree names a missing or non-blob object: {change.path}"
                )
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
            raise PlaybillGitError("record reads name a change-set path")
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
            raise PlaybillGitError("ledger blob read requires an exact path")
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
            raise PlaybillGitError("ledger literal search requires text and scoped paths")
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
            raise PlaybillGitError(
                f"system Git operation 'grep' failed with exit code {result.returncode}"
            )
        prefix = f"{oid}:".encode("ascii")
        found: list[str] = []
        for raw_path in result.stdout.split(b"\x00"):
            if not raw_path:
                continue
            if not raw_path.startswith(prefix):
                raise PlaybillGitError("ledger literal search returned an unexpected coordinate")
            try:
                found.append(raw_path[len(prefix) :].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise PlaybillGitError("ledger literal search returned a malformed path") from exc
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
        return () if entries is None else tuple(entries)

    def _directory_entries(self, oid: str, directory: str) -> dict[str, tuple[bytes, str]] | None:
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
    ) -> dict[str, tuple[bytes, str]]:
        """One tree object's entries, remembered by object ID; callers must not mutate it."""

        key = (repository, oid)
        with _PARSED_TREES_LOCK:
            cached = _PARSED_TREES.get(key)
            if cached is not None:
                _PARSED_TREES.move_to_end(key)
                return cached
        entries = self._tree_entries_of(oid)
        global _PARSED_TREE_ENTRIES
        with _PARSED_TREES_LOCK:
            if key not in _PARSED_TREES:
                _PARSED_TREES[key] = entries
                _PARSED_TREE_ENTRIES += len(entries)
            while _PARSED_TREES and _PARSED_TREE_ENTRIES > _PARSED_TREE_CAPACITY:
                _old, evicted = _PARSED_TREES.popitem(last=False)
                _PARSED_TREE_ENTRIES -= len(evicted)
        return entries

    def _remembered_whole_listing(self, oid: str) -> tuple[GitTreeEntry, ...] | None:
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
            return cached
        # A commit lists as its root tree, and the tree is usually what this
        # process just wrote or listed: resolve it from the commit's own bytes.
        tree = self._commit_tree(oid)
        if tree is not None:
            cached = _remembered_listing(repository, tree, with_sizes=with_sizes)
            if cached is not None:
                _remember_listing(repository, oid, cached)
                return cached
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
            raise PlaybillGitError(f"ledger commit has no tree header: {oid}")
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
                entries: dict[str, tuple[bytes, str]] | None = self._parsed_tree(repository, root)
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
                    raise PlaybillGitError(f"ledger tree object is unavailable: {tree}")
                entries = _tree_entries(found_tree[1], raw_length=raw_length)
                if entries is None:
                    raise PlaybillGitError(f"ledger tree object is malformed: {tree}")
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
                    raise PlaybillGitError(
                        f"ledger tree names a missing or non-blob object: {path}"
                    )
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
        with tempfile.NamedTemporaryFile(prefix="playbill-allowed-signer-") as exact_signers:
            exact_signers.write(signer + b"\n")
            exact_signers.flush()
            os.fsync(exact_signers.fileno())
            result = _command(
                [
                    "git",
                    f"--git-dir={self.path}",
                    "-c",
                    f"gpg.ssh.allowedSignersFile={exact_signers.name}",
                    "verify-commit",
                    oid,
                ],
                check=False,
            )
        if result.returncode != 0:
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
            raise PlaybillGitError("historical daemon public key is malformed") from exc
        openssh = public_key.public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        with tempfile.NamedTemporaryFile(prefix="playbill-historical-signer-") as signers:
            signers.write(principal_id.encode("utf-8") + b" " + openssh + b"\n")
            signers.flush()
            os.fsync(signers.fileno())
            result = _command(
                [
                    "git",
                    f"--git-dir={self.path}",
                    "-c",
                    f"gpg.ssh.allowedSignersFile={signers.name}",
                    "verify-commit",
                    oid,
                ],
                check=False,
            )
        return result.returncode == 0

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
                raise PlaybillGitError(f"ledger object is not a commit: {oid}")
            parents = _commit_parents(found[1])
            if len(parents) > 1:
                raise PlaybillGitError("Playbill refuses merge commits on main")
            newest_first.append(oid)
            if len(newest_first) > _MAIN_HISTORY_LIMIT:
                raise PlaybillGitError("Playbill main history is not a single parent chain")
            oid = parents[0] if parents else None
        return tuple(reversed(newest_first))

    def commit_timestamps(self, oid: str) -> tuple[datetime, datetime]:
        """Return one commit's embedded author and committer instants in UTC."""

        self._validate_oid(oid)
        found = _batch_reader(self.path).objects((oid,))[oid]
        if found is None or found[0] != "commit":
            raise PlaybillGitError(f"ledger object is not a commit: {oid}")
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
                raise PlaybillGitError("Git commit identity timestamp is malformed")
            try:
                seconds = int(fields[-2])
                zone = fields[-1]
                if not re.fullmatch(r"[+-][0-9]{4}", zone):
                    raise ValueError
            except (ValueError, IndexError) as exc:
                raise PlaybillGitError("Git commit identity timestamp is malformed") from exc
            timestamps[kind] = datetime.fromtimestamp(
                seconds,
                tz=timezone.utc,
            )
        if set(timestamps) != {"author", "committer"}:
            raise PlaybillGitError("Git commit omits an identity timestamp")
        return timestamps["author"], timestamps["committer"]

    def is_ancestor(self, ancestor_oid: str, descendant_oid: str) -> bool:
        self._validate_oid(ancestor_oid)
        self._validate_oid(descendant_oid)
        result = _command(
            [
                "git",
                f"--git-dir={self.path}",
                "merge-base",
                "--is-ancestor",
                ancestor_oid,
                descendant_oid,
            ],
            check=False,
        )
        return result.returncode == 0

    def _allowed_signer_entry(self, principal_id: str) -> bytes:
        matches: list[bytes] = []
        for line in self._allowed_signers_path.read_bytes().splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0].decode("utf-8") == principal_id:
                matches.append(b" ".join(fields[:3]))
        if len(matches) != 1:
            raise PlaybillGitError(
                f"allowed signers must contain exactly one key for {principal_id!r}"
            )
        return matches[0]

    def allowed_signer_public_key_hex(self, principal_id: str) -> str:
        fields = self._allowed_signer_entry(principal_id).split()
        return raw_public_key_hex_from_openssh(b" ".join(fields[1:3]))

    def durability_policy(self) -> tuple[str, str]:
        return (
            self._config_read(["config", "--get", "core.fsync"]).decode().strip(),
            self._config_read(["config", "--get", "core.fsyncMethod"]).decode().strip(),
        )

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
            raise PlaybillGitError(f"malformed Git OID: {oid!r}")
        expected_length = 40 if self.object_format() == "sha1" else 64
        if len(oid) != expected_length:
            raise PlaybillGitError("Git OID length does not match repository object format")

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
        raise PlaybillGitError("ledger tree contains malformed metadata") from exc
    return prefix + name


def _listing_mode(raw_mode: bytes) -> str:
    """A tree entry's mode as ``ls-tree`` and ``diff-tree`` print it."""

    try:
        return raw_mode.decode("ascii").rjust(6, "0")
    except UnicodeDecodeError as exc:
        raise PlaybillGitError("ledger tree contains malformed metadata") from exc


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
# An actor id Git's ident normalization leaves exactly as written.
_PLAIN_ACTOR_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")
_SAFE_REF_RE = re.compile(r"^refs/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
_PACKED_REFS_CAPACITY = 16
# Repository identity -> (packed-refs file identity, ref -> object ID).
_PACKED_REFS: OrderedDict[tuple[str, int, int], tuple[tuple[int, ...], dict[str, str]]] = (
    OrderedDict()
)
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
            raise PlaybillGitError(
                "unsupported Git command environment override: " + ", ".join(sorted(unexpected))
            )
        merged_environment.update(environment)
    return merged_environment


class _BatchBlobReader:
    """One long-lived `cat-file --batch` per repository, shared by every handle.

    Objects are immutable and content-addressed, so a resident reader returns
    exactly what a fresh process would, without paying a process spawn per
    read. Requests are strictly one object at a time under a lock: a pipe never
    holds an unread response while another request is written. Any error,
    including a missing object, retires the process; the next read starts one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        # A sibling `--batch-check` answers type and size without any payload.
        self._check_process: subprocess.Popen[bytes] | None = None
        self._owner = 0

    def _spawn(self, mode: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["git", f"--git-dir={self.path}", "cat-file", mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_command_environment(),
        )

    def _running(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None or process.poll() is not None or self._owner != os.getpid():
            if self._owner != os.getpid():
                self._check_process = None
            process = self._spawn("--batch")
            self._process, self._owner = process, os.getpid()
        return process

    def _checking(self) -> subprocess.Popen[bytes]:
        if self._owner != os.getpid():
            self._running()
        process = self._check_process
        if process is None or process.poll() is not None:
            process = self._check_process = self._spawn("--batch-check")
        return process

    def close(self) -> None:
        processes = (self._process, self._check_process)
        self._process = self._check_process = None
        if self._owner != os.getpid():
            return
        for process in processes:
            if process is None:
                continue
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def forget_inherited(self) -> None:
        """In a forked child, release this copy of the parent's processes untouched.

        The fork hooks hold every reader's lock across ``fork``, so no request
        is in flight and no buffered bytes can reach the parent's pipe here.
        """

        processes = (self._process, self._check_process)
        self._process = self._check_process = None
        self._lock = threading.Lock()
        for process in processes:
            if process is None:
                continue
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def path_blob(self, oid: str, path: str) -> bytes | None:
        """One committed path's blob through ``<commit>:<path>``; None when absent."""

        expression = f"{oid}:{path}".encode()
        with self._lock:
            process = self._running()
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(expression + b"\n")
                process.stdin.flush()
                header = process.stdout.readline()
                if not header.endswith(b"\n"):
                    raise PlaybillGitError("Git batch output ended before its header")
                if header == expression + b" missing\n":
                    return None
                try:
                    object_oid, object_type, raw_size = header[:-1].decode("ascii").split()
                    size = int(raw_size)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise PlaybillGitError("Git batch output has malformed metadata") from exc
                payload = process.stdout.read(size + 1)
                if len(payload) != size + 1 or payload[-1:] != b"\n":
                    raise PlaybillGitError("Git batch output has a truncated payload")
                if object_type != "blob":
                    raise PlaybillGitError(f"ledger path is not a regular blob: {path}")
                _require_object_hash(object_oid, object_type, payload[:-1])
                return payload[:-1]
            except BaseException:
                self.close()
                raise

    def object_info(self, oids: Sequence[str]) -> dict[str, tuple[str, int] | None]:
        """Each object's type and size, never its bytes; ``None`` when Git lacks it."""

        found: dict[str, tuple[str, int] | None] = {}
        with self._lock:
            process = self._checking()
            assert process.stdin is not None and process.stdout is not None
            try:
                # Requests go in chunks small enough that neither pipe can fill
                # while the other waits, so a whole listing's sizes cost a few
                # round trips rather than one per object.
                ordered = list(oids)
                for start in range(0, len(ordered), _OBJECT_INFO_CHUNK):
                    chunk = ordered[start : start + _OBJECT_INFO_CHUNK]
                    process.stdin.write(b"".join(oid.encode("ascii") + b"\n" for oid in chunk))
                    process.stdin.flush()
                    for expected_oid in chunk:
                        found[expected_oid] = _object_info_row(
                            process.stdout.readline(), expected_oid
                        )
            except BaseException:
                self.close()
                raise
        return found

    def objects(self, oids: Sequence[str]) -> dict[str, tuple[str, bytes] | None]:
        """Each object's type and bytes by exact ID; ``None`` when Git lacks it."""

        found: dict[str, tuple[str, bytes] | None] = {}
        with self._lock:
            process = self._running()
            assert process.stdin is not None and process.stdout is not None
            try:
                for expected_oid in oids:
                    process.stdin.write(expected_oid.encode("ascii") + b"\n")
                    process.stdin.flush()
                    header = process.stdout.readline()
                    if not header.endswith(b"\n"):
                        raise PlaybillGitError("Git batch blob output ended before its header")
                    if header == expected_oid.encode("ascii") + b" missing\n":
                        found[expected_oid] = None
                        continue
                    try:
                        actual_oid, object_type, raw_size = header[:-1].decode("ascii").split()
                        size = int(raw_size)
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise PlaybillGitError(
                            "Git batch blob output has malformed metadata"
                        ) from exc
                    if actual_oid != expected_oid or size < 0:
                        raise PlaybillGitError(
                            "Git batch blob output differs from the requested blob"
                        )
                    payload = process.stdout.read(size + 1)
                    if len(payload) != size + 1 or payload[-1:] != b"\n":
                        raise PlaybillGitError("Git batch blob output has a truncated payload")
                    _require_object_hash(expected_oid, object_type, payload[:-1])
                    found[expected_oid] = (object_type, payload[:-1])
            except BaseException:
                self.close()
                raise
        return found

    def read(self, oids: Sequence[str]) -> dict[str, bytes]:
        blobs: dict[str, bytes] = {}
        for oid, value in self.objects(oids).items():
            if value is None:
                raise PlaybillGitError("Git batch blob output has malformed metadata")
            if value[0] != "blob":
                raise PlaybillGitError("Git batch blob output differs from the requested blob")
            blobs[oid] = value[1]
        return blobs


_OBJECT_INFO_CHUNK: Final = 256


def _object_info_row(header: bytes, expected_oid: str) -> tuple[str, int] | None:
    if not header.endswith(b"\n"):
        raise PlaybillGitError("Git object metadata ended before its header")
    if header == expected_oid.encode("ascii") + b" missing\n":
        return None
    try:
        actual_oid, object_type, raw_size = header[:-1].decode("ascii").split()
        size = int(raw_size)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlaybillGitError("Git object metadata is malformed") from exc
    if actual_oid != expected_oid or size < 0:
        raise PlaybillGitError("Git object metadata differs from its request")
    return (object_type, size)


def _require_object_hash(oid: str, object_type: str, body: bytes) -> None:
    """Refuse object bytes that do not hash to the ID they were read under.

    Git serves a stored object without re-hashing it, so bytes replaced on disk
    after the ledger was verified would otherwise be returned under the
    original ID. Every object this reader hands out is checked.
    """

    algorithm = {40: "sha1", 64: "sha256"}.get(len(oid))
    if algorithm is None:
        raise PlaybillGitError(f"ledger object ID has an unknown length: {oid}")
    digest = hashlib.new(algorithm)
    digest.update(f"{object_type} {len(body)}".encode("ascii") + b"\x00")
    digest.update(body)
    if digest.hexdigest() != oid:
        raise PlaybillGitError(f"ledger object bytes do not hash to their ID: {oid}")


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


_VERIFIED_COMMIT_CAPACITY = 256
_VERIFIED_COMMITS: OrderedDict[tuple[tuple[str, int, int], str, bytes], None] = OrderedDict()
_VERIFIED_COMMITS_LOCK = threading.Lock()

# A commit and its root tree share one listing under two keys, sized and unsized.
_TREE_LISTING_CAPACITY = 32
_TREE_LISTINGS: OrderedDict[tuple[tuple[str, int, int], str, bool], tuple[GitTreeEntry, ...]] = (
    OrderedDict()
)
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
# id(listing) -> (the listing itself, path -> position, paths in sorted order).
# The listing is held so its id cannot be reused while the index is remembered.
_LISTING_INDEXES: OrderedDict[
    int, tuple[tuple[GitTreeEntry, ...], dict[str, int], tuple[str, ...]]
] = OrderedDict()
_LISTING_INDEXES_LOCK = threading.Lock()


def _listing_index(
    listing: tuple[GitTreeEntry, ...],
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
            while len(_LISTING_INDEXES) > _LISTING_INDEX_CAPACITY:
                _LISTING_INDEXES.popitem(last=False)
    return indexed[1], indexed[2]


def _listing_has_path(listing: tuple[GitTreeEntry, ...], path: str) -> bool:
    """Whether a file, or a directory with any entry beneath it, is named path."""

    positions, ordered = _listing_index(listing)
    if path in positions:
        return True
    prefix = path + "/"
    cursor = bisect.bisect_left(ordered, prefix)
    return cursor < len(ordered) and ordered[cursor].startswith(prefix)


def _listing_child_names(listing: tuple[GitTreeEntry, ...], directory: str) -> tuple[str, ...]:
    """A directory's immediate child names, skipping each child's own subtree.

    A file child is one entry and advances by one. A directory child's first
    entry is ``child/...``; every sibling that shares its name as a prefix
    and sorts before ``child/`` (``child-x``, ``child.y``) was already passed,
    and every entry beneath ``child/`` sorts before ``child0`` ('/' < '0' and
    nothing sorts between them), so one bisect steps over exactly that
    subtree: the work tracks the number of children, not the subtree sizes.
    """

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


def _select_from_listing(
    listing: tuple[GitTreeEntry, ...], paths: Sequence[str]
) -> tuple[GitTreeEntry, ...]:
    """A literal pathspec's selection from a whole listing, in listing order.

    Each requested path selects its exact entry, or every entry beneath it when
    it names a directory. An index built once per remembered listing makes an
    exact path a lookup and a directory a sorted range, so the work tracks the
    selection rather than the size of the tree.
    """

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


def _listing_with(
    listing: tuple[GitTreeEntry, ...],
    changes: Mapping[str, str | None],
    sizes: Mapping[str, int],
) -> tuple[GitTreeEntry, ...]:
    """A path-ordered listing with changed paths replaced, added or removed."""

    entries = list(listing)
    for path, oid in changes.items():
        position = bisect.bisect_left(
            entries, path.encode("utf-8"), key=lambda entry: entry.path.encode("utf-8")
        )
        present = position < len(entries) and entries[position].path == path
        if oid is None:
            if present:
                del entries[position]
            continue
        entry = GitTreeEntry(
            path=path, mode="100644", object_type="blob", oid=oid, size=sizes[path]
        )
        if present:
            entries[position] = entry
        else:
            entries.insert(position, entry)
    return tuple(entries)


def _remembered_listing(
    repository: tuple[str, int, int], oid: str, *, with_sizes: bool
) -> tuple[GitTreeEntry, ...] | None:
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
    unsized = tuple(replace(entry, size=None) for entry in sized)
    if unsized:
        _remember_listing(repository, oid, unsized)
    return unsized


def _remember_listing(
    repository: tuple[str, int, int], oid: str, listing: tuple[GitTreeEntry, ...]
) -> None:
    key = (repository, oid, listing[0].size is not None if listing else True)
    with _TREE_LISTINGS_LOCK:
        _TREE_LISTINGS[key] = listing
        _TREE_LISTINGS.move_to_end(key)
        while len(_TREE_LISTINGS) > _TREE_LISTING_CAPACITY:
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
    global _PARSED_TREES_LOCK
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
        raise PlaybillGitError(
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


__all__ = ["MIRROR_PUSH_TIMEOUT_SECONDS", "NOTE_REFS", "GitLedger", "GitTreeChange", "GitTreeEntry"]

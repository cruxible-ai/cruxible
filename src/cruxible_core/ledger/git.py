"""System-Git ledger primitives for Playbill generation zero."""

from __future__ import annotations

import atexit
import bisect
import fcntl
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
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


# One `ls-tree` invocation carries a bounded pathspec so a large request
# cannot overrun the system argument limit.
_PATHSPEC_BATCH = 256

# Bound temporary material per Git invocation. A single already-admissible blob
# larger than the byte target travels alone rather than gaining a new size gate.
_BLOB_WRITE_BATCH_OBJECTS = 256
_BLOB_WRITE_BATCH_BYTES = 32 * 1024 * 1024


def _quoted_stdin_path(path: Path) -> bytes:
    """Git's C-quoted path form, independent of TMPDIR's filesystem spelling."""

    return b'"' + b"".join(f"\\{byte:03o}".encode("ascii") for byte in os.fsencode(path)) + b'"'


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
    ) -> str:
        """Create one signed, still-unsettled generation commit over an exact parent.

        ``extends_tree`` names a stored tree whose members ``tree`` carries
        unchanged, as a settled proposal's tree is carried into its generation.
        Only the members it lacks are written; the caller's readback of the
        stored generation still compares every member.
        """

        self._validate_oid(parent_oid)
        if sequence < 1:
            raise PlaybillGitError("non-genesis generation sequence must be positive")
        _validate_commit_message(message)
        tree_oid = (
            self._write_tree(tree, accepted_parent=parent_oid)
            if extends_tree is None
            else self._extend_tree(extends_tree, tree)
        )
        environment = {
            "GIT_AUTHOR_NAME": "playbill-daemon",
            "GIT_AUTHOR_EMAIL": "daemon@playbill.invalid",
            "GIT_COMMITTER_NAME": "playbill-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@playbill.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
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

    def _absent_objects(self, oids: Sequence[str]) -> set[str]:
        """Report which of these exact object IDs the repository does not hold."""

        ordered = tuple(dict.fromkeys(oids))
        if not ordered:
            return set()
        output = self._git(
            ["cat-file", "--batch-check"],
            input_bytes=("\n".join(ordered) + "\n").encode("ascii"),
        )
        absent: set[str] = set()
        try:
            rows = output.decode("ascii").splitlines()
        except UnicodeDecodeError as exc:
            raise PlaybillGitError("Git object existence output is malformed") from exc
        if len(rows) != len(ordered):
            raise PlaybillGitError("Git object existence output does not match its request")
        for expected_oid, row in zip(ordered, rows, strict=True):
            fields = row.split()
            if not fields or fields[0] != expected_oid:
                raise PlaybillGitError("Git object existence output does not match its request")
            if len(fields) == 2 and fields[1] == "missing":
                absent.add(expected_oid)
                continue
            if len(fields) != 3 or fields[1] != "blob":
                raise PlaybillGitError(f"ledger object is not a blob: {expected_oid}")
        return absent

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
        computed in process, one batched existence check names the members Git
        is actually missing, bounded Git writes store those blobs, and one
        batched index update builds the tree. The
        resulting tree object ID is byte-for-byte the one a member-by-member
        write produces.
        """

        normalized_to_raw: dict[str, str] = {}
        for raw_path in tree:
            normalized = normalize_manifest_paths([raw_path])[0]
            if normalized in normalized_to_raw:
                raise PlaybillGitError(collision_message)
            normalized_to_raw[normalized] = raw_path

        ordered = normalize_manifest_paths(list(tree))
        contents = {path: tree[normalized_to_raw[path]] for path in ordered}
        oids = {path: self._blob_oid(content) for path, content in contents.items()}
        held: frozenset[str] = frozenset()
        parent_entries: dict[str, GitTreeEntry] = {}
        if accepted_parent is not None:
            parent_entries = {
                entry.path: entry
                for entry in self._list_tree(accepted_parent, with_sizes=False)
                if entry.object_type == "blob"
            }
            held = frozenset(entry.oid for entry in parent_entries.values())
        absent = self._absent_objects(tuple(oid for oid in oids.values() if oid not in held))
        missing: dict[str, bytes] = {}
        for path in ordered:
            blob_oid = oids[path]
            if blob_oid not in absent:
                continue
            if blob_oid in missing and missing[blob_oid] != contents[path]:
                raise PlaybillGitError("different blob bytes share a computed content address")
            missing[blob_oid] = contents[path]
        self._write_missing_blobs(missing)

        # Starting from the parent's own tree keeps Git's cache of every subtree
        # this write leaves alone, so only changed paths and their parents are
        # rehashed. The result is the same tree object a from-empty write makes.
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
        removed = [] if start is None else [path for path in parent_entries if path not in contents]
        zero = "0" * (40 if self.object_format() == "sha1" else 64)
        index_info = b"".join(
            b"100644 " + oids[path].encode("ascii") + b"\t" + path.encode("utf-8") + b"\x00"
            for path in staged
        ) + b"".join(
            b"0 " + zero.encode("ascii") + b"\t" + path.encode("utf-8") + b"\x00"
            for path in removed
        )
        with tempfile.TemporaryDirectory(prefix="playbill-tree-index-") as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._git(
                ["read-tree", "--empty"] if start is None else ["read-tree", start],
                environment=environment,
            )
            if index_info:
                self._git(
                    ["update-index", "-z", "--index-info"],
                    input_bytes=index_info,
                    environment=environment,
                )
            oid = self._git(["write-tree"], environment=environment).decode().strip()
        self._validate_oid(oid)
        # This process just wrote every entry of this tree: record its listing so
        # the readers that follow (projection assembly) need not re-list it.
        listing = tuple(
            GitTreeEntry(
                path=path,
                mode="100644",
                object_type="blob",
                oid=oids[path],
                size=len(contents[path]),
            )
            for path in ordered
        )
        _remember_listing(_repository_key(self.path), oid, listing)
        return oid

    def _extend_tree(self, base_tree: str, tree: Mapping[str, bytes]) -> str:
        """Write ``tree`` as ``base_tree`` plus the members ``base_tree`` lacks.

        Loading the stored tree into the index keeps Git's cache of its unchanged
        subtrees, so only the added paths and their parent trees are hashed and
        written, instead of every member of the whole tree.
        """

        self._validate_oid(base_tree)
        base = self._list_tree(base_tree, with_sizes=True)
        base_paths = {entry.path for entry in base}
        if any(entry.object_type != "blob" for entry in base) or not base_paths <= set(tree):
            raise PlaybillGitError("extended tree does not carry every member of its base")
        added = [path for path in tree if path not in base_paths]
        ordered_added = normalize_manifest_paths(added)
        if set(ordered_added) != set(added):
            raise PlaybillGitError("extended tree adds a path that is not normalized")
        oids = {path: self._blob_oid(tree[path]) for path in ordered_added}
        absent = self._absent_objects(tuple(oids.values()))
        missing: dict[str, bytes] = {}
        for path in ordered_added:
            blob_oid = oids[path]
            if blob_oid not in absent:
                continue
            if blob_oid in missing and missing[blob_oid] != tree[path]:
                raise PlaybillGitError("different blob bytes share a computed content address")
            missing[blob_oid] = tree[path]
        self._write_missing_blobs(missing)
        index_info = b"".join(
            b"100644 " + oids[path].encode("ascii") + b"\t" + path.encode("utf-8") + b"\x00"
            for path in ordered_added
        )
        with tempfile.TemporaryDirectory(prefix="playbill-tree-index-") as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._git(["read-tree", base_tree], environment=environment)
            if index_info:
                self._git(
                    ["update-index", "-z", "--index-info"],
                    input_bytes=index_info,
                    environment=environment,
                )
            oid = self._git(["write-tree"], environment=environment).decode().strip()
        self._validate_oid(oid)
        entries = {entry.path: entry for entry in base}
        for path in ordered_added:
            entries[path] = GitTreeEntry(
                path=path,
                mode="100644",
                object_type="blob",
                oid=oids[path],
                size=len(tree[path]),
            )
        listing = tuple(entries[path] for path in normalize_manifest_paths(list(entries)))
        _remember_listing(_repository_key(self.path), oid, listing)
        return oid

    def _write_missing_blobs(self, missing: Mapping[str, bytes]) -> None:
        """Have system Git write unique blobs in bounded, exact-byte batches."""

        batch: list[tuple[str, bytes]] = []
        size = 0
        for oid, content in missing.items():
            if batch and (
                len(batch) >= _BLOB_WRITE_BATCH_OBJECTS
                or size + len(content) > _BLOB_WRITE_BATCH_BYTES
            ):
                self._write_blob_batch(batch)
                batch = []
                size = 0
            batch.append((oid, content))
            size += len(content)
        if batch:
            self._write_blob_batch(batch)

    def _write_blob_batch(self, batch: Sequence[tuple[str, bytes]]) -> None:
        # Filenames are private ordinal names, never authored ledger paths.
        # --no-filters matches the former --stdin behavior even if this repository
        # or a temporary parent directory contains Git attribute rules.
        try:
            with tempfile.TemporaryDirectory(prefix="playbill-tree-blobs-") as temporary:
                paths: list[bytes] = []
                for index, (_oid, content) in enumerate(batch):
                    path = Path(temporary) / f"{index:04x}"
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content)
                    paths.append(_quoted_stdin_path(path))
                output = self._git(
                    ["hash-object", "-w", "--stdin-paths", "--no-filters"],
                    input_bytes=b"\n".join(paths) + b"\n",
                )
        except OSError as exc:
            raise PlaybillGitError("temporary Git blob batch could not be written") from exc
        try:
            written = output.decode("ascii").splitlines()
        except UnicodeDecodeError as exc:
            raise PlaybillGitError("Git blob write output is malformed") from exc
        if len(written) != len(batch):
            raise PlaybillGitError("Git blob write output does not match its request")
        for actual_oid, (expected_oid, _content) in zip(written, batch, strict=True):
            self._validate_oid(actual_oid)
            if actual_oid != expected_oid:
                raise PlaybillGitError("stored blob differs from its computed content address")

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
        result = _command(
            ["git", f"--git-dir={self.path}", "rev-parse", "--verify", target_ref],
            check=False,
        )
        if result.returncode != 0:
            return None
        oid = result.stdout.decode().strip()
        self._validate_oid(oid)
        return oid

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

    def _ref_exists(self, ref: str) -> bool:
        return (
            _command(
                ["git", f"--git-dir={self.path}", "rev-parse", "--verify", "--quiet", ref],
                check=False,
            ).returncode
            == 0
        )

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
        return self._git(["config", "--default", "UTF-8", "--get", "i18n.commitencoding"])

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
        values = dict(
            line.partition("=")[::2]
            for line in self._git(
                ["var", "-l"], environment=self._review_commit_environment(actor_id, timestamp)
            )
            .decode("utf-8")
            .splitlines()
            if "=" in line
        )
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
        self._validate_oid(commit_oid)
        oid = self._git(["rev-parse", f"{commit_oid}^{{tree}}"]).decode().strip()
        self._validate_oid(oid)
        return oid

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
        result = self._git(["rev-parse", "--verify", "refs/heads/main"])
        oid = result.decode().strip()
        self._validate_oid(oid)
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
        """
        self._validate_oid(before)
        self._validate_oid(after)
        raw = self._git(
            [
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "--no-renames",
                "-z",
                before,
                after,
                "--",
            ]
        )
        return tuple(path.decode("utf-8") for path in raw.split(b"\0") if path)

    def read_tree(self, oid: str) -> dict[str, bytes]:
        entries = _proven_blob_entries(self.list_tree(oid))
        # One batched read keeps whole-tree cost independent of the artifact count.
        blobs = self.read_blobs(tuple(entry.oid for entry in entries))
        return {entry.path: blobs[entry.oid] for entry in entries}

    def read_tree_delta(
        self, parent_oid: str, oid: str, *, parent_tree: Mapping[str, bytes]
    ) -> dict[str, bytes]:
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
        result = dict(parent_tree)
        for change in changes:
            if change.oid is None:
                del result[change.path]
            else:
                result[change.path] = blobs[change.oid]
        return {path: result[path] for path in sorted(result, key=lambda p: p.encode("utf-8"))}

    def paths_at(self, oid: str) -> tuple[str, ...]:
        """List one commit's paths under the same proof ``read_tree`` applies.

        A name-only listing has to refuse exactly the generations a whole-tree
        read refuses. Otherwise a caller that lists is answered where a caller
        that reads is refused, and — because a listing may be served from a
        memo filled by ``read_tree`` — the answer would depend on whether that
        memo happened to be warm.
        """

        return tuple(entry.path for entry in _proven_blob_entries(self.list_tree(oid)))

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
        wanted = set(ordered)
        selected: list[GitTreeEntry] = []
        for start in range(0, len(ordered), _PATHSPEC_BATCH):
            batch = ordered[start : start + _PATHSPEC_BATCH]
            selected.extend(
                _proven_blob_entries(
                    tuple(
                        entry
                        for entry in self._list_tree(oid, with_sizes=False, paths=batch)
                        if entry.path in wanted
                    )
                )
            )
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
        listing = self._git(
            [
                "diff-tree",
                "-r",
                "-z",
                "--no-renames",
                "--no-abbrev",
                "--no-commit-id",
                base_oid,
                target_oid,
            ]
        )
        fields = [field for field in listing.split(b"\x00") if field]
        if len(fields) % 2 != 0:
            raise PlaybillGitError("Git tree diff ended before a changed path")
        changes: list[GitTreeChange] = []
        for index in range(0, len(fields), 2):
            metadata, raw_path = fields[index], fields[index + 1]
            if not metadata.startswith(b":"):
                raise PlaybillGitError("Git tree diff contains malformed metadata")
            try:
                parts = metadata[1:].decode("ascii").split()
                path = raw_path.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PlaybillGitError("Git tree diff contains malformed metadata") from exc
            if len(parts) != 5:
                raise PlaybillGitError("Git tree diff contains malformed metadata")
            _source_mode, mode, source_oid, destination_oid, status = parts
            if status not in {"A", "M", "D", "T"}:
                raise PlaybillGitError(f"Git tree diff reported an unsupported status: {status}")
            previous = None
            if status != "A":
                self._validate_oid(source_oid)
                previous = source_oid
            if status == "D":
                changes.append(
                    GitTreeChange(
                        path=path, status=status, mode=mode, oid=None, previous_oid=previous
                    )
                )
                continue
            self._validate_oid(destination_oid)
            changes.append(
                GitTreeChange(
                    path=path,
                    status=status,
                    mode=mode,
                    oid=destination_oid,
                    previous_oid=previous,
                )
            )
        return tuple(changes)

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

    def tree_has_path(self, oid: str, path: str) -> bool:
        """Whether this commit's tree names ``path`` as a file or a nonempty directory."""

        return _listing_has_path(self._whole_listing(oid), path)

    def tree_child_names(self, oid: str, directory: str) -> tuple[str, ...]:
        """Immediate child names of one directory ("" is the root), in tree order."""

        return _listing_child_names(self._whole_listing(oid), directory)

    def _whole_listing(self, oid: str) -> tuple[GitTreeEntry, ...]:
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
        return self._list_tree(oid, with_sizes=False)

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
        entries: list[GitTreeEntry] = []
        size_flag = ["-l"] if with_sizes else []
        expected_fields = 4 if with_sizes else 3
        # ``:(literal)`` disables pathspec globbing so a path that carries
        # wildcard bytes names exactly itself.
        scope = [] if paths is None else ["--", *(f":(literal){item}" for item in paths)]
        listing = self._git(["ls-tree", "-r", *size_flag, "-z", "--full-tree", oid, *scope])
        for row in listing.split(b"\x00"):
            if not row:
                continue
            try:
                metadata, raw_path = row.split(b"\t", 1)
                fields = metadata.decode("ascii").split()
                if len(fields) != expected_fields:
                    raise ValueError("unexpected tree metadata field count")
                mode, object_type, object_oid = fields[0], fields[1], fields[2]
                path = raw_path.decode("utf-8")
            except (UnicodeDecodeError, ValueError) as exc:
                raise PlaybillGitError("ledger tree contains malformed metadata") from exc
            self._validate_oid(object_oid)
            size: int | None = None
            if with_sizes:
                try:
                    size = None if fields[3] == "-" else int(fields[3])
                except ValueError as exc:
                    raise PlaybillGitError("ledger tree contains a malformed object size") from exc
            entries.append(
                GitTreeEntry(
                    path=path,
                    mode=mode,
                    object_type=object_type,
                    oid=object_oid,
                    size=size,
                )
            )
        return tuple(entries)

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

        rows = self._git(["rev-list", "--parents", "--reverse", "refs/heads/main"]).decode()
        history: list[str] = []
        for row in rows.splitlines():
            fields = row.split()
            if len(fields) > 2:
                raise PlaybillGitError("Playbill refuses merge commits on main")
            oid = fields[0]
            self._validate_oid(oid)
            if not history:
                if len(fields) != 1:
                    raise PlaybillGitError(
                        "Playbill main history is not rooted at a parentless commit"
                    )
            else:
                if len(fields) != 2:
                    raise PlaybillGitError(
                        "Playbill main history contains a second parentless commit"
                    )
                self._validate_oid(fields[1])
                if fields[1] != history[-1]:
                    raise PlaybillGitError("Playbill main history is not a single parent chain")
            history.append(oid)
        return tuple(history)

    def commit_timestamps(self, oid: str) -> tuple[datetime, datetime]:
        """Return one commit's embedded author and committer instants in UTC."""

        self._validate_oid(oid)
        content = self._git(["cat-file", "commit", oid]).decode("utf-8")
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
            self._git(["config", "--get", "core.fsync"]).decode().strip(),
            self._git(["config", "--get", "core.fsyncMethod"]).decode().strip(),
        )

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
        self._owner = 0

    def _running(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None or process.poll() is not None or self._owner != os.getpid():
            process = subprocess.Popen(
                ["git", f"--git-dir={self.path}", "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_command_environment(),
            )
            self._process, self._owner = process, os.getpid()
        return process

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None or self._owner != os.getpid():
            return
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
        """In a forked child, release this copy of the parent's process untouched.

        The fork hooks hold every reader's lock across ``fork``, so no request
        is in flight and no buffered bytes can reach the parent's pipe here.
        """

        process, self._process = self._process, None
        self._lock = threading.Lock()
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

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

    Every entry beneath ``child/`` sorts before ``child0`` ('/' < '0' and
    nothing sorts between them), so one bisect per child steps over it: the
    work tracks the number of children, not the size of the subtree.
    """

    _positions, ordered = _listing_index(listing)
    prefix = directory + "/" if directory else ""
    names: list[str] = []
    cursor = bisect.bisect_left(ordered, prefix)
    while cursor < len(ordered) and ordered[cursor].startswith(prefix):
        name = ordered[cursor][len(prefix) :].split("/", 1)[0]
        names.append(name)
        cursor = bisect.bisect_left(ordered, prefix + name + "0", cursor + 1)
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
    global _LISTING_INDEXES_LOCK, _TREE_CHANGES_LOCK
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

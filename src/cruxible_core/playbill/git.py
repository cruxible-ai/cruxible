"""System-Git ledger primitives for Playbill generation zero."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cruxible_client.contracts.canonical import normalize_manifest_paths
from cruxible_client.contracts.errors import PlaybillGitError
from cruxible_client.contracts.types import GitObjectFormat
from cruxible_core.playbill.keys import raw_public_key_hex_from_openssh

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROPOSAL_REF_RE = re.compile(r"^refs/proposals/[a-z][a-z0-9_.-]{0,127}/[a-z][a-z0-9_.-]{0,127}$")
_PROPOSAL_REVIEW_REF_RE = re.compile(r"^refs/heads/proposals/[0-9a-f]{64}$")
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
    ) -> str:
        """Create one signed, still-unsettled generation commit over an exact parent."""

        self._validate_oid(parent_oid)
        if sequence < 1:
            raise PlaybillGitError("non-genesis generation sequence must be positive")
        tree_oid = self._write_tree(tree)
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
                    f"Accept Playbill generation {sequence}",
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
    ) -> str:
        """Write one exact normalized tree, storing only its not-yet-held members.

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
        absent = self._absent_objects(tuple(oids.values()))
        missing: dict[str, bytes] = {}
        for path in ordered:
            blob_oid = oids[path]
            if blob_oid not in absent:
                continue
            if blob_oid in missing and missing[blob_oid] != contents[path]:
                raise PlaybillGitError("different blob bytes share a computed content address")
            missing[blob_oid] = contents[path]
        self._write_missing_blobs(missing)

        index_info = b"".join(
            b"100644 " + oids[path].encode("ascii") + b"\t" + path.encode("utf-8") + b"\x00"
            for path in ordered
        )
        with tempfile.TemporaryDirectory(prefix="playbill-tree-index-") as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._git(["read-tree", "--empty"], environment=environment)
            if index_info:
                self._git(
                    ["update-index", "-z", "--index-info"],
                    input_bytes=index_info,
                    environment=environment,
                )
            oid = self._git(["write-tree"], environment=environment).decode().strip()
        self._validate_oid(oid)
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
    ) -> tuple[str, str]:
        """Write one unsigned proposal commit and CAS only its actor namespace ref."""

        self._validate_oid(base_oid)
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
                    "Record Playbill proposal",
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

    def replace_proposal_review_refs(self, refs: Mapping[str, str]) -> None:
        """Atomically replace the standard-Git projection of open proposal refs."""

        normalized: dict[str, str] = {}
        for proposal_id, oid in refs.items():
            ref = f"refs/heads/proposals/{proposal_id}"
            if not _PROPOSAL_REVIEW_REF_RE.fullmatch(ref):
                raise PlaybillGitError("proposal review ref name is malformed")
            self._validate_oid(oid)
            normalized[ref] = oid
        current = {
            line
            for line in self._git(["for-each-ref", "--format=%(refname)", "refs/heads/proposals"])
            .decode("utf-8")
            .splitlines()
            if line
        }
        commands = ["start"]
        commands.extend(
            f"update {ref} {normalized[ref]}" for ref in sorted(normalized, key=str.encode)
        )
        commands.extend(
            f"delete {ref}" for ref in sorted(current - set(normalized), key=str.encode)
        )
        commands.extend(("prepare", "commit"))
        self._git(["update-ref", "--stdin"], input_bytes=("\n".join(commands) + "\n").encode())

    def proposal_review_commit(
        self,
        *,
        tree_oid: str,
        base_oid: str,
        actor_id: str,
        timestamp: str,
    ) -> str:
        """Reproduce the evaluated proposal commit used only by advisory review refs."""

        self._validate_oid(tree_oid)
        self._validate_oid(base_oid)
        environment = {
            "GIT_AUTHOR_NAME": actor_id,
            "GIT_AUTHOR_EMAIL": f"{actor_id}@proposal.playbill.invalid",
            "GIT_COMMITTER_NAME": "playbill-daemon",
            "GIT_COMMITTER_EMAIL": "daemon@playbill.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        oid = (
            self._git(
                [
                    "commit-tree",
                    tree_oid,
                    "-p",
                    base_oid,
                    "-m",
                    "Record Playbill proposal",
                ],
                environment=environment,
            )
            .decode()
            .strip()
        )
        self._validate_oid(oid)
        if self.tree_oid(oid) != tree_oid or self.parent_of(oid) != base_oid:
            raise PlaybillGitError("proposal review commit does not reproduce its evidence")
        return oid

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

        path = self.path / "playbill-activation.lock"
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
        result = _command(
            ["git", f"--git-dir={self.path}", "cat-file", "-e", oid],
            check=False,
        )
        return result.returncode == 0

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

    def write_generation_note(self, oid: str, content: bytes) -> None:
        """Durably attach one immutable descriptor note after the winning main CAS."""

        self._validate_oid(oid)
        if self.read_main() != oid:
            raise PlaybillGitError("generation note target is not the current main ref")
        if self.read_generation_note(oid) is not None:
            raise PlaybillGitError("generation already carries a descriptor note")
        self._git(
            ["notes", "--ref=refs/notes/playbill-gen", "add", "-F", "-", oid],
            input_bytes=content,
        )
        if self.read_generation_note(oid) != content:
            raise PlaybillGitError("generation descriptor note did not persist exactly")

    def write_recovered_generation_note(self, oid: str, content: bytes) -> None:
        """Repair a missing note only for a replay-proven commit on accepted main."""

        self._validate_oid(oid)
        if not self.is_ancestor(oid, self.read_main()):
            raise PlaybillGitError("recovered generation note target is outside main history")
        if self.read_generation_note(oid) is not None:
            raise PlaybillGitError("generation already carries a descriptor note")
        self._git(
            ["notes", "--ref=refs/notes/playbill-gen", "add", "-F", "-", oid],
            input_bytes=content,
        )
        if self.read_generation_note(oid) != content:
            raise PlaybillGitError("recovered generation descriptor note did not persist exactly")

    def read_generation_note(self, oid: str) -> bytes | None:
        self._validate_oid(oid)
        result = _command(
            [
                "git",
                f"--git-dir={self.path}",
                "notes",
                "--ref=refs/notes/playbill-gen",
                "show",
                oid,
            ],
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def read_main(self) -> str:
        result = self._git(["rev-parse", "--verify", "refs/heads/main"])
        oid = result.decode().strip()
        self._validate_oid(oid)
        return oid

    def parent_of(self, oid: str) -> str | None:
        self._validate_oid(oid)
        ancestry = self._git(["rev-list", "--parents", "-n", "1", oid]).decode().split()
        if len(ancestry) == 1:
            return None
        if len(ancestry) == 2:
            return ancestry[1]
        raise PlaybillGitError("Playbill refuses merge commits on main")

    def read_tree(self, oid: str) -> dict[str, bytes]:
        entries = _proven_blob_entries(self.list_tree(oid))
        # One batched read keeps whole-tree cost independent of the artifact count.
        blobs = self.read_blobs(tuple(entry.oid for entry in entries))
        return {entry.path: blobs[entry.oid] for entry in entries}

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
            _source_mode, mode, _source_oid, destination_oid, status = parts
            if status not in {"A", "M", "D", "T"}:
                raise PlaybillGitError(f"Git tree diff reported an unsupported status: {status}")
            if status == "D":
                changes.append(GitTreeChange(path=path, status=status, mode=mode, oid=None))
                continue
            self._validate_oid(destination_oid)
            changes.append(GitTreeChange(path=path, status=status, mode=mode, oid=destination_oid))
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
        """Read a bounded set of blobs through one `cat-file --batch` process."""

        ordered = tuple(dict.fromkeys(oids))
        for oid in ordered:
            self._validate_oid(oid)
        if not ordered:
            return {}
        output = self._git(
            ["cat-file", "--batch"],
            input_bytes=("\n".join(ordered) + "\n").encode("ascii"),
        )
        position = 0
        blobs: dict[str, bytes] = {}
        for expected_oid in ordered:
            header_end = output.find(b"\n", position)
            if header_end < 0:
                raise PlaybillGitError("Git batch blob output ended before its header")
            try:
                actual_oid, object_type, raw_size = (
                    output[position:header_end].decode("ascii").split()
                )
                size = int(raw_size)
            except (UnicodeDecodeError, ValueError) as exc:
                raise PlaybillGitError("Git batch blob output has malformed metadata") from exc
            if actual_oid != expected_oid or object_type != "blob" or size < 0:
                raise PlaybillGitError("Git batch blob output differs from the requested blob")
            content_start = header_end + 1
            content_end = content_start + size
            if content_end >= len(output) or output[content_end : content_end + 1] != b"\n":
                raise PlaybillGitError("Git batch blob output has a truncated payload")
            blobs[expected_oid] = output[content_start:content_end]
            position = content_end + 1
        if position != len(output):
            raise PlaybillGitError("Git batch blob output contains trailing bytes")
        return blobs

    def verify_commit(self, oid: str, *, principal_id: str = "daemon") -> bool:
        """Verify against exactly one expected signer, not any configured signer."""

        self._validate_oid(oid)
        signer = self._allowed_signer_entry(principal_id)
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
        return result.returncode == 0

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


def _command(
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
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
    result = subprocess.run(
        list(arguments),
        input=input_bytes,
        capture_output=True,
        check=False,
        env=merged_environment,
    )
    if check and result.returncode != 0:
        # Do not echo command arguments or stderr: Git signing failures can
        # include managed credential paths, which inspection/logging must not expose.
        command = next((arg for arg in arguments[1:] if not arg.startswith("-")), "git")
        raise PlaybillGitError(
            f"system Git operation {command!r} failed with exit code {result.returncode}"
        )
    return result


__all__ = ["GitLedger", "GitTreeChange", "GitTreeEntry"]

"""System-Git ledger primitives for Playbill generation zero."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cruxible_core.playbill.canonical import normalize_manifest_paths
from cruxible_core.playbill.errors import PlaybillGitError
from cruxible_core.playbill.keys import raw_public_key_hex_from_openssh
from cruxible_core.playbill.types import GitObjectFormat

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROPOSAL_REF_RE = re.compile(r"^refs/proposals/[a-z][a-z0-9_.-]{0,127}/[a-z][a-z0-9_.-]{0,127}$")
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

        normalized_to_raw: dict[str, str] = {}
        for raw_path in tree:
            normalized = normalize_manifest_paths([raw_path])[0]
            if normalized in normalized_to_raw:
                raise PlaybillGitError("genesis paths collide after normalization")
            normalized_to_raw[normalized] = raw_path

        with tempfile.TemporaryDirectory(prefix="playbill-index-") as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._git(["read-tree", "--empty"], environment=environment)
            for path in normalize_manifest_paths(list(tree)):
                content = tree[normalized_to_raw[path]]
                blob_oid = self._git(["hash-object", "-w", "--stdin"], input_bytes=content)
                self._git(
                    [
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        "100644",
                        blob_oid.decode().strip(),
                        path,
                    ],
                    environment=environment,
                )
            tree_oid = self._git(["write-tree"], environment=environment).decode().strip()

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

    def _write_tree(self, tree: Mapping[str, bytes]) -> str:
        normalized_to_raw: dict[str, str] = {}
        for raw_path in tree:
            normalized = normalize_manifest_paths([raw_path])[0]
            if normalized in normalized_to_raw:
                raise PlaybillGitError("generation paths collide after normalization")
            normalized_to_raw[normalized] = raw_path

        with tempfile.TemporaryDirectory(prefix="playbill-generation-index-") as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._git(["read-tree", "--empty"], environment=environment)
            for path in normalize_manifest_paths(list(tree)):
                blob_oid = self._git(
                    ["hash-object", "-w", "--stdin"],
                    input_bytes=tree[normalized_to_raw[path]],
                )
                self._git(
                    [
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        "100644",
                        blob_oid.decode().strip(),
                        path,
                    ],
                    environment=environment,
                )
            oid = self._git(["write-tree"], environment=environment).decode().strip()
        self._validate_oid(oid)
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

        normalized_to_raw: dict[str, str] = {}
        for raw_path in tree:
            normalized = normalize_manifest_paths([raw_path])[0]
            if normalized in normalized_to_raw:
                raise PlaybillGitError("proposal paths collide after normalization")
            normalized_to_raw[normalized] = raw_path

        with tempfile.TemporaryDirectory(prefix="playbill-proposal-index-") as temporary:
            environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
            self._git(["read-tree", "--empty"], environment=environment)
            for path in normalize_manifest_paths(list(tree)):
                content = tree[normalized_to_raw[path]]
                blob_oid = self._git(["hash-object", "-w", "--stdin"], input_bytes=content)
                self._git(
                    [
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        "100644",
                        blob_oid.decode().strip(),
                        path,
                    ],
                    environment=environment,
                )
            tree_oid = self._git(["write-tree"], environment=environment).decode().strip()

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
        entries = self.list_tree(oid)
        for entry in entries:
            if entry.object_type != "blob" or entry.mode != "100644":
                raise PlaybillGitError(
                    f"ledger tree contains unsupported {entry.mode} "
                    f"{entry.object_type}: {entry.path}"
                )
        # One batched read keeps whole-tree cost independent of the artifact count.
        blobs = self.read_blobs(tuple(entry.oid for entry in entries))
        return {entry.path: blobs[entry.oid] for entry in entries}

    def list_tree(self, oid: str) -> tuple[GitTreeEntry, ...]:
        """List an exact commit recursively without reading any blob payload.

        Entries carry no `size`: reporting it costs Git one object-size lookup
        per entry, so only `list_tree_with_sizes` pays for it.
        """

        return self._list_tree(oid, with_sizes=False)

    def list_tree_with_sizes(self, oid: str) -> tuple[GitTreeEntry, ...]:
        """List an exact commit recursively, with the size Git reports per entry.

        Reserved for callers that gate on declared sizes before reading blobs.
        The per-entry size lookup dominates listing cost on a loose-object
        ledger, so callers that ignore `size` must use `list_tree` instead.
        """

        return self._list_tree(oid, with_sizes=True)

    def _list_tree(self, oid: str, *, with_sizes: bool) -> tuple[GitTreeEntry, ...]:
        self._validate_oid(oid)
        entries: list[GitTreeEntry] = []
        size_flag = ["-l"] if with_sizes else []
        expected_fields = 4 if with_sizes else 3
        listing = self._git(["ls-tree", "-r", *size_flag, "-z", "--full-tree", oid])
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
        """Return the non-merge main chain in oldest-first order."""

        rows = (
            self._git(["rev-list", "--reverse", "--first-parent", "refs/heads/main"])
            .decode()
            .splitlines()
        )
        for oid in rows:
            self._validate_oid(oid)
            if (
                self.parent_of(oid) is not None
                and len(self._git(["rev-list", "--parents", "-n", "1", oid]).decode().split()) != 2
            ):
                raise PlaybillGitError("Playbill main history contains a merge commit")
        return tuple(rows)

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


__all__ = ["GitLedger", "GitTreeEntry"]

"""Daemon-local, no-follow reads for the ``workspace.file`` Provider."""

from __future__ import annotations

import base64
import errno
import fcntl
import fnmatch
import hashlib
import os
import stat
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_client.contracts.workspace_file import (
    ExecutedWorkspaceFileSourceRequestV2,
    SourceReadReceipt,
    WorkspaceFileGlobRequestV1,
    WorkspaceFilePathClass,
    WorkspaceFileSourceRequest,
    WorkspaceFileSourceRequestV2,
)

#: What a presence check finds at one workspace path: the file, nothing, or a
#: path the reader would refuse (with its path class).
WorkspaceFilePresence = tuple[
    Literal["present", "absent", "refused"], WorkspaceFilePathClass | None
]


class WorkspaceFileReadRefused(Exception):
    """Typed pre-spawn refusal with a runnable repair."""

    code = "workspace_file_read_refused"

    def __init__(
        self,
        path_class: WorkspaceFilePathClass,
        message: str,
        *,
        workspace_names: tuple[str, ...] = (),
    ) -> None:
        self.path_class = path_class
        #: The logical names of the authorized roots, when naming one is the repair.
        self.workspace_names = workspace_names
        self.repair_commands = (
            (
                "name one of the authorized workspaces in the Source request's `workspace`: "
                + ", ".join(workspace_names),
            )
            if workspace_names
            else (
                "select a regular non-linked file inside an attached or operationally allowed root",
            )
        )
        super().__init__(message)


def workspace_binding_digest(*, instance_id: str, canonical_root: Path) -> str:
    """Return the only root identity allowed to leave daemon-local configuration."""

    return typed_digest(
        Sha256Value,
        "playbill-workspace-binding-v1",
        {"instance_id": instance_id, "canonical_root": str(canonical_root)},
    ).tagged


@dataclass(frozen=True)
class WorkspaceFileReadResultV1:
    provider_input: CanonicalValue
    receipt: SourceReadReceipt


#: One directory's identity at the moment it was listed: ``(device, inode,
#: st_mtime_ns, st_ctime_ns)``. A cache entry is reused only while a fresh
#: stat of the same directory still matches it, so content changed since (or
#: a directory inode reused for a different directory) normally relists. The
#: listing is only ever a hint: a name it substitutes is checked against the
#: opened descriptor before use, so a stale entry costs a relist or a refusal,
#: never a wrong read.
DirectoryIdentity = tuple[int, int, int, int]

#: One glob operation's shared directory-entry cache (D-R41): a real
#: directory's identity, listing and name-set, keyed by its ``(device,
#: inode)`` so every matched member under it reuses one listing instead of
#: relisting it. Callers create a fresh cache per glob operation; it is
#: never kept longer.
DirectoryEntryCache = dict[
    tuple[int, int], tuple[DirectoryIdentity, tuple[str, ...], frozenset[str]]
]


class WorkspaceFileReader:
    """Read one regular file under an exact authorized root without following links."""

    def __init__(
        self,
        *,
        instance_id: str,
        operating_profile: Literal["local", "cloud"],
        attached_roots: Sequence[Path],
        operational_allowed_roots: Sequence[Path] = (),
        managed_roots: Sequence[Path] = (),
        daemon_owned: bool = False,
    ) -> None:
        self.instance_id = instance_id
        self.operating_profile = operating_profile
        #: Every root is one the daemon itself staged (a Capture upload's
        #: ``uploads`` workspace), never a client mount, so a cloud profile
        #: reads it too.
        self.daemon_owned = daemon_owned
        self._attached = self._canonical_roots(attached_roots)
        self._roots = self._canonical_roots((*attached_roots, *operational_allowed_roots))
        self._managed_roots = self._canonical_roots(managed_roots, require_directory=False)

    @staticmethod
    def _canonical_roots(
        roots: Sequence[Path], *, require_directory: bool = True
    ) -> tuple[Path, ...]:
        resolved: set[Path] = set()
        for raw in roots:
            try:
                root = raw.expanduser().resolve(strict=require_directory)
            except OSError as exc:
                raise WorkspaceFileReadRefused("binding", "authorized root is unavailable") from exc
            if require_directory and not root.is_dir():
                raise WorkspaceFileReadRefused("binding", "authorized root is not a directory")
            resolved.add(root)
        return tuple(sorted(resolved, key=lambda item: str(item).encode("utf-8")))

    @property
    def binding_digests(self) -> tuple[str, ...]:
        return tuple(
            workspace_binding_digest(instance_id=self.instance_id, canonical_root=root)
            for root in self._roots
        )

    @property
    def workspace_names(self) -> tuple[str, ...]:
        """Each authorized root's logical name (its last component), sorted and unique."""

        return tuple(sorted({root.name for root in self._roots}, key=lambda n: n.encode("utf-8")))

    def resolve(
        self, request: WorkspaceFileSourceRequestV2
    ) -> ExecutedWorkspaceFileSourceRequestV2:
        """Resolve an authored request's workspace to the root the daemon will read.

        Unnamed: the instance's attached workspace, else its only authorized
        root; several and none attached refuses ``workspace_unnamed`` listing
        the names. Named: the one authorized root of that name; none, or
        several, refuses ``workspace_unknown`` listing the names. The binding
        digest of the resolved root is the daemon's to record, never the
        author's.
        """

        root = self._resolve_root(request.workspace)
        return ExecutedWorkspaceFileSourceRequestV2.executing(
            request,
            workspace=root.name,
            binding_digest=workspace_binding_digest(
                instance_id=self.instance_id, canonical_root=root
            ),
        )

    def _resolve_root(self, workspace: str | None) -> Path:
        """The one authorized root an authored ``workspace`` (or its absence) names."""

        if self.operating_profile == "cloud" and not self.daemon_owned:
            raise WorkspaceFileReadRefused(
                "cloud_no_mounts", "cloud profile has no daemon-local workspace mounts"
            )
        if workspace is None:
            candidates = self._attached if len(self._attached) == 1 else self._roots
            if not candidates:
                raise WorkspaceFileReadRefused(
                    "binding", "no workspace is attached or operationally allowed"
                )
            if len(candidates) > 1:
                raise WorkspaceFileReadRefused(
                    "workspace_unnamed",
                    "several workspaces are authorized and the Source request names none",
                    workspace_names=self.workspace_names,
                )
        else:
            candidates = tuple(root for root in self._roots if root.name == workspace)
            if len(candidates) != 1:
                raise WorkspaceFileReadRefused(
                    "workspace_unknown",
                    (
                        f"no authorized workspace is named {workspace!r}"
                        if not candidates
                        else f"several authorized workspaces are named {workspace!r}"
                    ),
                    workspace_names=self.workspace_names,
                )
        (root,) = candidates
        return root

    def expand(self, request: WorkspaceFileGlobRequestV1) -> tuple[str, ...]:
        """The relative paths a glob matches under its workspace, sorted, within its cap.

        One directory listing per directory a pattern component reads, never a
        tree walk. A match is a candidate only: each is read afterwards as an
        ordinary ``workspace.file`` read, which applies every containment and
        no-follow check. Hidden entries match only a pattern that starts with
        ``.``, and Git metadata, Cruxible control and key paths and managed
        roots never match. More matches than ``max_files`` refuses
        ``glob_over_cap``.
        """

        root = self._resolve_root(request.workspace)
        found: list[str] = [""]
        parts = request.glob.split("/")
        for position, part in enumerate(parts):
            leaf = position == len(parts) - 1
            pattern = any(character in part for character in "*?[")
            following: list[str] = []
            for prefix in found:
                directory = root.joinpath(*prefix.split("/")) if prefix else root
                if pattern:
                    try:
                        names = sorted(
                            name
                            for name in os.listdir(directory)
                            if fnmatch.fnmatchcase(name, part)
                            and (part.startswith(".") or not name.startswith("."))
                        )
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    except PermissionError as exc:
                        raise WorkspaceFileReadRefused(
                            "unreadable", "a workspace glob directory cannot be listed"
                        ) from exc
                else:
                    names = [part]
                for name in names:
                    relative = f"{prefix}/{name}" if prefix else name
                    try:
                        self._deny_path(tuple(relative.split("/")))
                        self._check_managed_root(root.joinpath(*relative.split("/")))
                        status = os.lstat(root.joinpath(*relative.split("/")))
                    except (WorkspaceFileReadRefused, FileNotFoundError, NotADirectoryError):
                        continue
                    # A directory is never a match; only a directory (never a
                    # link to one) is walked into. A link or special file that
                    # matches is a candidate, and its read refuses it.
                    if stat.S_ISDIR(status.st_mode) != leaf:
                        following.append(relative)
            found = following
        matches = tuple(sorted(found, key=lambda item: item.encode("utf-8")))
        if len(matches) > request.max_files:
            raise WorkspaceFileReadRefused(
                "glob_over_cap",
                f"the glob matches more than its {request.max_files} files",
            )
        return matches

    def presence(
        self,
        relative_path: str,
        *,
        workspace: str | None = None,
        max_bytes: int | None = None,
    ) -> WorkspaceFilePresence:
        """Whether a read of ``relative_path`` would find a file, reading none of it.

        The same rules as ``read`` decide: the root the request resolves to,
        the path grammar, the never-readable paths and managed roots, no
        symbolic link on the way, read access to the leaf, a regular file
        with one hard link, and the byte budget when ``max_bytes`` is given.
        One no-follow stat per path component, walked through directory
        descriptors, then the leaf opened no-follow from its parent's
        descriptor and closed: no directory is listed and no byte of the file
        is read. A refusal of the root itself (no workspace attached, several
        unnamed) raises as ``resolve`` does.
        """

        root = self._resolve_root(workspace)
        try:
            WorkspaceFileSourceRequest._relative_path(relative_path)
        except ValueError:
            return "refused", "path_grammar"
        parts = tuple(relative_path.split("/"))
        try:
            self._deny_path(parts)
            self._check_managed_root(root.joinpath(*parts))
        except WorkspaceFileReadRefused as exc:
            return "refused", exc.path_class
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        leaf_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptors: list[int] = []
        try:
            descriptors.append(os.open(root, flags))
            for component in parts[:-1]:
                status = os.stat(component, dir_fd=descriptors[-1], follow_symlinks=False)
                if stat.S_ISLNK(status.st_mode):
                    return "refused", "symlink"
                if not stat.S_ISDIR(status.st_mode):
                    return "refused", "non_regular"
                descriptors.append(os.open(component, flags, dir_fd=descriptors[-1]))
            status = os.stat(parts[-1], dir_fd=descriptors[-1], follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode):
                return "refused", "symlink"
            if stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode):
                # Read access is decided as ``read`` decides it: the same
                # no-follow open from the parent's descriptor, closed at once
                # with no byte read. Only a file or directory is opened (a
                # device or FIFO is refused without touching it).
                os.close(os.open(parts[-1], leaf_flags, dir_fd=descriptors[-1]))
        except FileNotFoundError:
            return "absent", None
        except PermissionError:
            return "refused", "unreadable"
        except OSError as exc:
            if exc.errno == errno.ENOTDIR:
                return "absent", None
            return "refused", "symlink" if exc.errno == errno.ELOOP else "non_regular"
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        if not stat.S_ISREG(status.st_mode):
            return "refused", "non_regular"
        if status.st_nlink != 1:
            return "refused", "hardlink"
        if max_bytes is not None and status.st_size > max_bytes:
            return "refused", "size_budget"
        return "present", None

    def _root_for(self, binding_digest: str) -> Path:
        for root in self._roots:
            if (
                workspace_binding_digest(instance_id=self.instance_id, canonical_root=root)
                == binding_digest
            ):
                return root
        raise WorkspaceFileReadRefused("binding", "workspace binding is not authorized")

    @staticmethod
    def _fold(component: str) -> str:
        """Fold one component the way a case-insensitive volume compares names."""

        return unicodedata.normalize("NFC", component).casefold()

    @classmethod
    def _deny_path(cls, parts: tuple[str, ...]) -> None:
        folded = tuple(cls._fold(part) for part in parts)
        if any(part == ".git" for part in folded):
            raise WorkspaceFileReadRefused("git_metadata", "Git metadata is never readable")
        if any(part == ".cruxible" for part in folded):
            raise WorkspaceFileReadRefused(
                "cruxible_control", "Cruxible control paths are never readable"
            )
        leaf = folded[-1]
        if (
            leaf.endswith(".ed25519")
            or leaf.endswith(".ed25519.pub")
            or leaf in {"daemon_ed25519", "daemon_ed25519.pub", "allowed_signers"}
            or (leaf.startswith(".cruxible-init-resume-") and leaf.endswith(".json"))
        ):
            raise WorkspaceFileReadRefused(
                "client_custody", "client custody and key paths are never readable"
            )

    @staticmethod
    def _within(candidate: Path, root: Path) -> bool:
        return candidate == root or root in candidate.parents

    @classmethod
    def _within_folded(cls, candidate: Path, root: Path) -> bool:
        """Contain case-insensitively so a case flip never under-denies a root."""

        root_parts = tuple(cls._fold(part) for part in root.parts)
        candidate_parts = tuple(cls._fold(part) for part in candidate.parts)
        return candidate_parts[: len(root_parts)] == root_parts

    def _check_managed_root(self, candidate: Path) -> None:
        if any(self._within_folded(candidate, root) for root in self._managed_roots):
            raise WorkspaceFileReadRefused(
                "managed_root", "Cruxible managed roots are never readable"
            )

    @staticmethod
    def _directory_entries(
        parent_fd: int, *, cache: DirectoryEntryCache | None = None, force_refresh: bool = False
    ) -> tuple[tuple[str, ...], frozenset[str]]:
        """List a directory once, reusing the result for sibling files in one glob.

        ``cache`` is keyed by the parent descriptor's ``(device, inode)``,
        never by the fd number itself (each file read reopens the directory,
        so the same real directory gets a fresh, numerically different fd):
        this is what lets sibling members of a glob share one listing. An
        entry is reused only while a fresh stat of ``parent_fd`` still
        matches the directory's identity when it was listed (device, inode,
        and its own ``st_mtime_ns``/``st_ctime_ns``): a directory whose
        content changed since, or whose inode was reused for a different
        directory, is relisted. Correctness never rests on this: ``read``
        verifies any substituted name against the descriptor it opened.
        ``force_refresh`` relists
        unconditionally (still updating the cache), for a caller that already
        knows a cached candidate let it down.
        """

        key: tuple[int, int] | None = None
        identity: DirectoryIdentity | None = None
        if cache is not None:
            try:
                parent_stat = os.fstat(parent_fd)
            except OSError:
                parent_stat = None
            if parent_stat is not None:
                key = (parent_stat.st_dev, parent_stat.st_ino)
                identity = (
                    parent_stat.st_dev,
                    parent_stat.st_ino,
                    parent_stat.st_mtime_ns,
                    parent_stat.st_ctime_ns,
                )
                if not force_refresh:
                    cached = cache.get(key)
                    if cached is not None and cached[0] == identity:
                        return cached[1], cached[2]
        try:
            entries = tuple(os.listdir(parent_fd))
        except PermissionError as exc:
            raise WorkspaceFileReadRefused(
                "unreadable", "workspace source directory cannot be listed"
            ) from exc
        by_name = frozenset(entries)
        if cache is not None and key is not None and identity is not None:
            cache[key] = (identity, entries, by_name)
        return entries, by_name

    @staticmethod
    def _same_object(parent_fd: int, one: str, other: str) -> tuple[int, int] | None:
        """The shared ``(device, inode)`` if a fresh stat of ``one`` and ``other`` agree.

        A stat is a snapshot, not a hold: nothing is open, so the object can
        be replaced the instant after this returns, and on ext4 the
        replacement can even reuse the freed inode, so the identity returned
        is no fingerprint to compare a later open against. It only decides
        whether a cached candidate is worth trying (else the directory is
        relisted); ``read`` checks what it actually opened with
        ``_require_names_held`` while holding the descriptor.
        """

        try:
            stat_one = os.stat(one, dir_fd=parent_fd, follow_symlinks=False)
            stat_other = os.stat(other, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return None
        identity = (stat_one.st_dev, stat_one.st_ino)
        if identity != (stat_other.st_dev, stat_other.st_ino):
            return None
        return identity

    @staticmethod
    def _resolve_component(
        parent_fd: int, requested: str, cache: DirectoryEntryCache | None
    ) -> tuple[str, tuple[int, int] | None] | None:
        """``requested``'s real spelling in ``parent_fd``'s listing, or ``None`` if unplaced.

        A case-folding candidate recovered from ``cache`` is returned only
        once a fresh stat finds it naming the same object as ``requested``,
        so a stale listing's candidate triggers a relist (``None`` lets the
        caller relist and retry once before refusing) rather than a doomed
        open.

        The second element is the identity that stat saw for a substituted
        candidate (``None`` when the exact requested name was used, or no
        cache was given). It is a hint, not a guarantee: whenever the name
        returned differs from ``requested``, ``read`` refuses unless
        ``requested`` names the descriptor it opened, checked while it is
        held (``_require_names_held``).
        """

        entries, by_name = WorkspaceFileReader._directory_entries(parent_fd, cache=cache)
        if requested in by_name:
            return requested, None
        try:
            os.stat(requested, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        requested_key = unicodedata.normalize("NFC", requested).casefold()
        matches = tuple(
            entry
            for entry in entries
            if unicodedata.normalize("NFC", entry).casefold() == requested_key
        )
        if len(matches) != 1:
            return None
        candidate = matches[0]
        if cache is None:
            return candidate, None
        identity = WorkspaceFileReader._same_object(parent_fd, requested, candidate)
        if identity is None:
            return None
        return candidate, identity

    @staticmethod
    def _actual_component(
        parent_fd: int, requested: str, *, cache: DirectoryEntryCache | None = None
    ) -> tuple[str, tuple[int, int] | None]:
        """Resolve one component using the directory's real spelling.

        ``cache``, scoped to one glob operation (D-R41), reuses a directory's
        entries across sibling reads instead of relisting it per file; every
        descriptor, no-follow and real-name check below still runs per file.
        A cached listing that cannot place ``requested`` (absent from it, or
        resolving to an object a fresh stat no longer agrees is the same one)
        is never trusted as final: the directory is relisted once and
        ``requested`` resolved again from that fresh result before refusing.

        Returns the resolved name together with the identity a stat saw for
        it (``None`` when the exact requested name was used unchanged). That
        identity only steers the relist; it is not compared against what is
        opened, since a freed inode can be reused by a replacement between
        this stat and the open. A caller that opens a name differing from
        ``requested`` must check, while holding the descriptor, that
        ``requested`` still names it (``_require_names_held``).
        """

        resolved = WorkspaceFileReader._resolve_component(parent_fd, requested, cache)
        if resolved is None and cache is not None:
            WorkspaceFileReader._directory_entries(parent_fd, cache=cache, force_refresh=True)
            resolved = WorkspaceFileReader._resolve_component(parent_fd, requested, cache)
        if resolved is None:
            raise WorkspaceFileReadRefused("missing", "workspace source is unavailable")
        return resolved

    @staticmethod
    def _require_names_held(parent_fd: int, requested: str, held_fd: int) -> None:
        """Refuse unless ``requested`` names the object ``held_fd`` holds, right now.

        This is what makes a substituted spelling safe to read: the open
        went through the directory's real spelling, so the requested one
        must name the very object opened. The comparison runs while the
        descriptor is held, so the held object's inode cannot be freed and
        handed to a replacement (ext4 reuses a freed inode at once): equal
        ``(device, inode)`` here is the same object, never a look-alike. A
        requested name that is gone, or names anything else, refuses
        ``changed_during_read``.
        """

        held = os.fstat(held_fd)
        try:
            named = os.stat(requested, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise WorkspaceFileReadRefused(
                "changed_during_read", "workspace source changed while it was read"
            ) from exc
        if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
            raise WorkspaceFileReadRefused(
                "changed_during_read", "workspace source changed while it was read"
            )

    @staticmethod
    def _fd_path(descriptor: int) -> Path:
        """Return the kernel's path for an open descriptor, or fail closed."""

        try:
            if sys.platform == "darwin":
                raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
                assert isinstance(raw, bytes)
                value = raw.split(b"\0", 1)[0].decode("utf-8")
            elif sys.platform.startswith("linux"):
                value = os.readlink(f"/proc/self/fd/{descriptor}")
            else:
                raise OSError(errno.ENOTSUP, "descriptor paths are unsupported")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise WorkspaceFileReadRefused(
                "changed_during_read", "workspace source path cannot be confirmed"
            ) from exc
        if not value or value.endswith(" (deleted)"):
            raise WorkspaceFileReadRefused(
                "changed_during_read", "workspace source changed while it was read"
            )
        return Path(value)

    def read(
        self,
        request: ExecutedWorkspaceFileSourceRequestV2,
        *,
        run_id: str,
        admission_binding_digest: str,
        occurrence_path: str,
        policy_coordinate: AcceptedCoordinate,
        resolved_max_bytes: int,
        derived_request_digest: str,
        read_at: datetime,
        directory_cache: DirectoryEntryCache | None = None,
    ) -> WorkspaceFileReadResultV1:
        """Read one file, with every containment and no-follow check strict.

        ``directory_cache``, scoped to one glob operation (D-R41), reuses a
        directory's entries across sibling reads instead of relisting it per
        file; omitted (the default), every call lists its own directory, as
        before.

        Every check that decides what is read runs on a held descriptor:
        each component is opened no-follow from the root's descriptor, and
        a component opened under the directory's real spelling instead of
        the requested one is read or traversed only once the requested
        spelling is confirmed to name that held object. A write that lands
        before the open is an ordinary earlier write: its bytes are read,
        and the receipt digests exactly them.
        """

        if self.operating_profile == "cloud" and not self.daemon_owned:
            raise WorkspaceFileReadRefused(
                "cloud_no_mounts", "cloud profile has no daemon-local workspace mounts"
            )
        try:
            request = ExecutedWorkspaceFileSourceRequestV2.model_validate(request)
        except ValueError as exc:
            raise WorkspaceFileReadRefused(
                "path_grammar", "workspace path is not normalized relative POSIX"
            ) from exc
        root = self._root_for(request.workspace_binding_digest)
        parts = tuple(request.relative_path.split("/"))
        self._deny_path(parts)
        # The requested spelling is denied before any descriptor exists, so a
        # managed root refuses identically on case-sensitive and folding volumes.
        self._check_managed_root(root.joinpath(*parts))
        descriptors: list[int] = []
        flags_directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptors.append(os.open(root, flags_directory))
            root_real = self._fd_path(descriptors[0])
            self._check_managed_root(root_real)
            for component in parts[:-1]:
                actual, _ = self._actual_component(
                    descriptors[-1], component, cache=directory_cache
                )
                before = os.stat(actual, dir_fd=descriptors[-1], follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode):
                    raise WorkspaceFileReadRefused(
                        "symlink", "workspace source may not traverse a symbolic link"
                    )
                if not stat.S_ISDIR(before.st_mode):
                    raise WorkspaceFileReadRefused(
                        "non_regular", "workspace source parent must be a directory"
                    )
                descriptors.append(os.open(actual, flags_directory, dir_fd=descriptors[-1]))
                # ``actual`` may be a case-folding substitution for
                # ``component`` (D-R41/F3): before traversing into it, the
                # requested spelling must name the directory just opened.
                if actual != component:
                    self._require_names_held(descriptors[-2], component, descriptors[-1])
            actual_leaf, _ = self._actual_component(
                descriptors[-1], parts[-1], cache=directory_cache
            )
            before = os.stat(actual_leaf, dir_fd=descriptors[-1], follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise WorkspaceFileReadRefused(
                    "symlink", "workspace source may not be a symbolic link"
                )
            descriptors.append(os.open(actual_leaf, flags_file, dir_fd=descriptors[-1]))
            # Same for the leaf, before any byte of it is read.
            if actual_leaf != parts[-1]:
                self._require_names_held(descriptors[-2], parts[-1], descriptors[-1])
            opened = os.fstat(descriptors[-1])
            if not stat.S_ISREG(opened.st_mode):
                raise WorkspaceFileReadRefused(
                    "non_regular", "workspace source must be a regular file"
                )
            if opened.st_nlink != 1:
                raise WorkspaceFileReadRefused(
                    "hardlink", "workspace source must have exactly one hard link"
                )
            opened_path = self._fd_path(descriptors[-1])
            self._deny_path(tuple(opened_path.parts))
            if not self._within(opened_path, root_real):
                raise WorkspaceFileReadRefused(
                    "changed_during_read", "workspace source escaped its authorized root"
                )
            self._check_managed_root(opened_path)
            data = bytearray()
            while len(data) <= resolved_max_bytes:
                chunk = os.read(descriptors[-1], min(64 * 1024, resolved_max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > resolved_max_bytes:
                raise WorkspaceFileReadRefused(
                    "size_budget", "workspace source exceeds the admitted byte budget"
                )
            after = os.stat(actual_leaf, dir_fd=descriptors[-2], follow_symlinks=False)
            resolved = self._fd_path(descriptors[-1])
            if (
                after.st_dev != opened.st_dev
                or after.st_ino != opened.st_ino
                or not self._within(resolved, root_real)
            ):
                raise WorkspaceFileReadRefused(
                    "changed_during_read", "workspace source changed while it was read"
                )
            self._check_managed_root(resolved)
            self._deny_path(tuple(resolved.parts))
            # The receipt attests what was READ, so it carries the real on-disk
            # component names the kernel just confirmed, not the spelling the
            # request asked for: on a case-folding volume a request for
            # DOCS/NOTE.TXT reads docs/note.txt, and a receipt echoing the
            # request would attest a name no directory holds.
            try:
                real_relative_path = "/".join(resolved.relative_to(root_real).parts)
            except ValueError as exc:
                raise WorkspaceFileReadRefused(
                    "changed_during_read", "workspace source path cannot be confirmed"
                ) from exc
            try:
                WorkspaceFileSourceRequestV2._relative_path(real_relative_path)
            except ValueError as exc:
                raise WorkspaceFileReadRefused(
                    "path_grammar", "real workspace path is not normalized relative POSIX"
                ) from exc
        except WorkspaceFileReadRefused:
            raise
        except FileNotFoundError as exc:
            raise WorkspaceFileReadRefused("missing", "workspace source is unavailable") from exc
        except PermissionError as exc:
            raise WorkspaceFileReadRefused("unreadable", "workspace source is unreadable") from exc
        except OSError as exc:
            path_class: WorkspaceFilePathClass
            if exc.errno == errno.ELOOP:
                path_class = "symlink"
            elif exc.errno in {errno.EACCES, errno.EPERM}:
                path_class = "unreadable"
            else:
                path_class = "non_regular"
            raise WorkspaceFileReadRefused(path_class, "workspace source cannot be opened") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

        content = bytes(data)
        bytes_digest = "sha256:" + hashlib.sha256(content).hexdigest()
        provider_input = normalize_canonical(
            {
                "logical_source": request.logical_source,
                "commitment_digest": derived_request_digest,
                "content_encoding": "base64",
                "bytes": base64.b64encode(content).decode("ascii"),
                "byte_length": len(content),
                "bytes_digest": bytes_digest,
            }
        )
        provider_input_digest = typed_digest(
            ArtifactDigest,
            "playbill-procedure-run-provider-input-v1",
            {"value": provider_input},
        ).tagged
        receipt = SourceReadReceipt(
            run_id=run_id,
            admission_binding_digest=admission_binding_digest,
            occurrence_path=occurrence_path,
            logical_source=request.logical_source,
            workspace_binding_digest=request.workspace_binding_digest,
            relative_path=real_relative_path,
            requested_path=request.relative_path,
            bytes_digest=bytes_digest,
            byte_length=len(content),
            policy_coordinate=policy_coordinate,
            resolved_max_bytes=resolved_max_bytes,
            derived_request_digest=derived_request_digest,
            provider_input_digest=provider_input_digest,
            read_at=ensure_utc(read_at),
        )
        return WorkspaceFileReadResultV1(provider_input=provider_input, receipt=receipt)


__all__ = [
    "WorkspaceFileReadRefused",
    "WorkspaceFileReadResultV1",
    "WorkspaceFileReader",
    "workspace_binding_digest",
]

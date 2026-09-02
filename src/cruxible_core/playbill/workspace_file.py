"""Daemon-local, no-follow reads for the ``workspace.file`` Provider."""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_client.contracts.workspace_file import (
    SourceReadReceiptV1,
    WorkspaceFileSourceRequestV1,
)

WorkspaceFilePathClass = Literal[
    "cloud_no_mounts",
    "binding",
    "path_grammar",
    "git_metadata",
    "playbill_control",
    "client_custody",
    "managed_root",
    "symlink",
    "hardlink",
    "non_regular",
    "changed_during_read",
    "missing",
    "size_budget",
]


class WorkspaceFileReadRefused(Exception):
    """Typed pre-spawn refusal with a runnable repair."""

    code = "workspace_file_read_refused"

    def __init__(self, path_class: WorkspaceFilePathClass, message: str) -> None:
        self.path_class = path_class
        self.repair_commands = (
            "select a regular non-linked file inside an attached or operationally allowed root",
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
    receipt: SourceReadReceiptV1


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
        after_open_hook: Callable[[Path], None] | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.operating_profile = operating_profile
        self._roots = self._canonical_roots((*attached_roots, *operational_allowed_roots))
        self._managed_roots = self._canonical_roots(managed_roots, require_directory=False)
        self._after_open_hook = after_open_hook

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
            if require_directory and (root.is_symlink() or not root.is_dir()):
                raise WorkspaceFileReadRefused("binding", "authorized root is not a directory")
            resolved.add(root)
        return tuple(sorted(resolved, key=lambda item: str(item).encode("utf-8")))

    @property
    def binding_digests(self) -> tuple[str, ...]:
        return tuple(
            workspace_binding_digest(instance_id=self.instance_id, canonical_root=root)
            for root in self._roots
        )

    def _root_for(self, binding_digest: str) -> Path:
        for root in self._roots:
            if (
                workspace_binding_digest(instance_id=self.instance_id, canonical_root=root)
                == binding_digest
            ):
                return root
        raise WorkspaceFileReadRefused("binding", "workspace binding is not authorized")

    @staticmethod
    def _deny_path(parts: tuple[str, ...]) -> None:
        if any(part == ".git" for part in parts):
            raise WorkspaceFileReadRefused("git_metadata", "Git metadata is never readable")
        if any(part == ".playbill" for part in parts):
            raise WorkspaceFileReadRefused(
                "playbill_control", "Playbill control paths are never readable"
            )
        leaf = parts[-1]
        if (
            leaf.endswith(".ed25519")
            or leaf.endswith(".ed25519.pub")
            or leaf in {"daemon_ed25519", "daemon_ed25519.pub", "allowed_signers"}
            or (leaf.startswith(".playbill-init-resume-") and leaf.endswith(".json"))
        ):
            raise WorkspaceFileReadRefused(
                "client_custody", "client custody and key paths are never readable"
            )

    @staticmethod
    def _within(candidate: Path, root: Path) -> bool:
        return candidate == root or root in candidate.parents

    def _check_managed_root(self, candidate: Path) -> None:
        if any(self._within(candidate, root) for root in self._managed_roots):
            raise WorkspaceFileReadRefused(
                "managed_root", "Playbill managed roots are never readable"
            )

    def read(
        self,
        request: WorkspaceFileSourceRequestV1,
        *,
        run_id: str,
        admission_binding_digest: str,
        occurrence_path: str,
        policy_coordinate: AcceptedCoordinate,
        resolved_max_bytes: int,
        derived_request_digest: str,
        read_at: datetime,
    ) -> WorkspaceFileReadResultV1:
        if self.operating_profile == "cloud":
            raise WorkspaceFileReadRefused(
                "cloud_no_mounts", "cloud profile has no daemon-local workspace mounts"
            )
        try:
            request = WorkspaceFileSourceRequestV1.model_validate(request)
        except ValueError as exc:
            raise WorkspaceFileReadRefused(
                "path_grammar", "workspace path is not normalized relative POSIX"
            ) from exc
        root = self._root_for(request.workspace_binding_digest)
        parts = tuple(request.relative_path.split("/"))
        self._deny_path(parts)
        candidate = root.joinpath(*parts)
        self._check_managed_root(candidate)

        descriptors: list[int] = []
        flags_directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptors.append(os.open(root, flags_directory))
            for component in parts[:-1]:
                descriptors.append(os.open(component, flags_directory, dir_fd=descriptors[-1]))
            descriptors.append(os.open(parts[-1], flags_file, dir_fd=descriptors[-1]))
            opened = os.fstat(descriptors[-1])
            if self._after_open_hook is not None:
                self._after_open_hook(candidate)
            if not stat.S_ISREG(opened.st_mode):
                raise WorkspaceFileReadRefused(
                    "non_regular", "workspace source must be a regular file"
                )
            if opened.st_nlink != 1:
                raise WorkspaceFileReadRefused(
                    "hardlink", "workspace source must have exactly one hard link"
                )
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
            after = os.stat(candidate, follow_symlinks=False)
            resolved = candidate.resolve(strict=True)
            if (
                after.st_dev != opened.st_dev
                or after.st_ino != opened.st_ino
                or not self._within(resolved, root)
            ):
                raise WorkspaceFileReadRefused(
                    "changed_during_read", "workspace source changed while it was read"
                )
            self._check_managed_root(resolved)
        except WorkspaceFileReadRefused:
            raise
        except FileNotFoundError as exc:
            raise WorkspaceFileReadRefused("missing", "workspace source is unavailable") from exc
        except OSError as exc:
            path_class: WorkspaceFilePathClass = (
                "symlink" if exc.errno == errno.ELOOP else "non_regular"
            )
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
            Sha256Value,
            "playbill-provider-invocation-input-v1",
            {"input": provider_input},
        ).tagged
        receipt = SourceReadReceiptV1(
            run_id=run_id,
            admission_binding_digest=admission_binding_digest,
            occurrence_path=occurrence_path,
            logical_source=request.logical_source,
            workspace_binding_digest=request.workspace_binding_digest,
            relative_path=request.relative_path,
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

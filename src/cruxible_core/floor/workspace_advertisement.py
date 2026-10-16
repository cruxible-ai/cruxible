"""Copy daemon-owned advisory refs between local repositories without executing Git."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import cast

from cruxible_client.contracts.errors import GitError
from cruxible_client.contracts.types import GitObjectFormat
from cruxible_client.contracts.workspace_advertisement import (
    WorkspaceAdvertisement,
    WorkspaceAdvertisementFailureCode,
)
from cruxible_core.ledger.git import (
    _ASK_GIT,
    NOTE_REFS,
    GitLedger,
    _batch_reader,
    _files_backend_ref,
    _tree_entries,
)

_REMOTE_NAME = "cruxible-ledger"
_REMOTE_SECTION = 'remote "cruxible-ledger"'
_ACCEPTED_REFSPEC = "+refs/heads/main:refs/remotes/cruxible-ledger/accepted"
_PROPOSAL_REFSPEC = "+refs/heads/proposals/*:refs/remotes/cruxible-ledger/proposals/*"
_NOTE_REFS = tuple(sorted(NOTE_REFS.values()))
_SECTION = re.compile(r'^\s*\[\s*([\w.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(?:[#;].*)?$')
_KEY = re.compile(r"^\s*([\w-]+)\s*(?:=\s*(.*))?$")


def _repository(workspace: Path) -> tuple[Path, Path]:
    """Resolve ordinary and linked worktrees, without consulting ambient Git variables."""
    marker = workspace / ".git"
    if marker.is_symlink():
        raise ValueError("workspace_path_invalid")
    if marker.is_file():
        raw = marker.read_text().strip()
        if not raw.startswith("gitdir: "):
            raise ValueError("workspace_not_git")
        gitdir = (workspace / raw[8:]).resolve(strict=True)
    elif marker.is_dir():
        gitdir = marker.resolve(strict=True)
    else:
        raise ValueError("workspace_not_git")
    common_file = gitdir / "commondir"
    common = (
        (gitdir / common_file.read_text().strip()).resolve(strict=True)
        if common_file.is_file()
        else gitdir
    )
    if not (common / "config").is_file() or not (common / "objects").is_dir():
        raise ValueError("workspace_not_git")
    return gitdir, common


def _value(raw: str) -> str:
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#;"
    # Git preserves whitespace inside double quotes; join only unquoted words.
    return " ".join(lexer)


def _config(path: Path, *, depth: int = 0) -> list[tuple[str, str, str]]:
    """Read ordered, repeated local values, including static include files.

    No global/environment config or executable setting is interpreted. Conditional
    includes are refused rather than guessing which remote or rewrite they select.
    """
    if depth > 10:
        raise ValueError("remote_conflict")
    rows: list[tuple[str, str, str]] = []
    section = ""
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith(("#", ";")):
            continue
        match = _SECTION.fullmatch(line)
        if match:
            name, subsection = match.groups()
            section = name.lower() + (f' "{subsection}"' if subsection is not None else "")
            if name.lower() == "includeif":
                raise ValueError("remote_conflict")
            continue
        match = _KEY.fullmatch(line)
        if match is None:
            raise ValueError("remote_conflict")
        key, raw = match.groups()
        value = "true" if raw is None else _value(raw)
        rows.append((section, key.lower(), value))
        if section == "include" and key.lower() == "path":
            included = Path(value).expanduser()
            if not included.is_absolute():
                included = path.parent / included
            if included.exists():
                rows.extend(_config(included, depth=depth + 1))
    return rows


def _local_url_matches(workspace: Path, raw_url: str, expected_url: str) -> bool:
    candidate = Path(raw_url).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        return str(candidate.resolve()) == expected_url
    except (OSError, RuntimeError, ValueError):
        return False


def containing_git_workspace_root(workspace_path: Path) -> Path | None:
    try:
        candidate = workspace_path.expanduser().resolve(strict=True)
        for root in (candidate, *candidate.parents):
            if (root / ".git").exists():
                _repository(root)
                return root
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def workspace_git_object_format(workspace_root: Path) -> GitObjectFormat:
    workspace = workspace_root.expanduser()
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("workspace_missing")
    try:
        _, common = _repository(workspace.resolve())
        values = [
            v
            for section, key, v in _config(common / "config")
            if (section, key) == ("extensions", "objectformat")
        ]
    except (OSError, RuntimeError) as exc:
        raise ValueError("workspace_path_invalid") from exc
    except UnicodeError as exc:
        raise ValueError("remote_conflict") from exc
    value = values[-1] if values else "sha1"
    if value not in {"sha1", "sha256"}:
        raise ValueError("workspace_not_git")
    return cast(GitObjectFormat, value)


class _LocalLedger(GitLedger):
    """Files-backend operations only; advisory copies never need signing custody."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path, signing_key_path=path / "unused", allowed_signers_path=path / "unused"
        )

    def _resolve_ref(self, ref: str) -> str | None:
        for _ in range(8):
            path = self.path / ref
            if path.is_symlink() or not ref.startswith("refs/") or ".." in ref:
                raise GitError("unsafe workspace reference")
            try:
                raw = path.read_text().strip()
            except (FileNotFoundError, NotADirectoryError):
                found = _files_backend_ref(self.path, ref)
                if found is _ASK_GIT:
                    raise GitError("unsupported workspace reference backend")
                if isinstance(found, str):
                    self._validate_oid(found)
                    return found
                return None
            if raw.startswith("ref: "):
                ref = raw[5:]
                continue
            self._validate_oid(raw)
            return raw
        raise GitError("workspace symbolic reference cycle")


def _configure_remote(
    common: Path, rows: list[tuple[str, str, str]], expected: str, workspace: Path
) -> None:
    urls = [v for section, key, v in rows if (section, key) == (_REMOTE_SECTION, "url")]
    if any(not _local_url_matches(workspace, value, expected) for value in urls):
        raise ValueError("remote_conflict")
    # We copy from the supplied local repository, never from config. Refuse a
    # transport rewrite anyway: later human `git fetch` must not inherit it.
    rewrites = [
        (value, section[5:-1])
        for section, key, value in rows
        if section.startswith('url "') and key == "insteadof"
    ]
    for raw_url in urls or [expected]:
        matches = [
            (prefix, replacement) for prefix, replacement in rewrites if raw_url.startswith(prefix)
        ]
        if matches:
            prefix, replacement = max(matches, key=lambda row: len(row[0]))
            raw_url = replacement + raw_url[len(prefix) :]
        if not _local_url_matches(workspace, raw_url, expected):
            raise ValueError("remote_conflict")
    desired = {
        "fetch": [_ACCEPTED_REFSPEC, _PROPOSAL_REFSPEC],
        "tagopt": ["--no-tags"],
        "skipfetchall": ["true"],
    }
    if not urls:
        desired["url"] = [expected]
    if all(
        [v for section, k, v in rows if (section, k) == (_REMOTE_SECTION, key)] == values
        for key, values in desired.items()
    ):
        return
    config = common / "config"
    lock = common / "config.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    published = False
    try:
        # Preserve unrelated bytes and settings; replace only the owned keys.
        output = []
        owned = False
        for line in config.read_text().splitlines(keepends=True):
            section_match = _SECTION.fullmatch(line.rstrip("\n"))
            if section_match:
                name, subsection = section_match.groups()
                owned = name.lower() == "remote" and subsection == _REMOTE_NAME
            key_match = _KEY.fullmatch(line.rstrip("\n"))
            if owned and key_match and key_match[1].lower() in desired:
                continue
            output.append(line)
        output.append(f"\n[{_REMOTE_SECTION}]\n")
        for key, values in desired.items():
            output.extend(
                f"\t{key} = {json.dumps(value, ensure_ascii=False)}\n" for value in values
            )
        stream = os.fdopen(fd, "wb")
        fd = -1
        with stream:
            stream.write("".join(output).encode())
        os.replace(lock, config)
        published = True
    finally:
        if fd >= 0:
            os.close(fd)
        if not published:
            lock.unlink(missing_ok=True)


def _copy_objects(source: Path, target: Path, roots: tuple[str, ...]) -> None:
    """Copy the missing frontier; an existing object ends ancestry traversal.

    Cold attachment visits reachable history once. Warm refresh follows only
    newly advertised commits/notes and changed trees, stopping at copied objects.
    """
    reader, writer = _batch_reader(source), _batch_reader(target).store
    objects: dict[str, tuple[str, bytes]] = {}
    pending = list(roots)
    while pending:
        oid = pending.pop()
        if oid in objects or writer.info(oid) is not None:
            continue
        found = reader.objects((oid,))[oid]
        if found is None:
            raise GitError("advertised object is unavailable")
        kind, body = found
        objects[oid] = found
        if kind == "commit":
            for line in body.split(b"\n\n", 1)[0].splitlines():
                if line.startswith((b"tree ", b"parent ")):
                    pending.append(line.split(b" ", 1)[1].decode("ascii"))
        elif kind == "tree":
            entries = _tree_entries(body, raw_length=len(oid) // 2)
            if entries is None:
                raise GitError("advertised tree is malformed")
            pending.extend(child for mode, child in entries.values() if mode != b"160000")
        elif kind == "tag":
            pending.append(body.split(b"\n", 1)[0].removeprefix(b"object ").decode("ascii"))
    # Workspace advertisements are advisory, not accepted-state authority.
    writer.write(objects, durable=False)


def _advertise_workspace_refs(
    *, workspace_root: Path | None, ledger_path: Path, ledger_object_format: GitObjectFormat
) -> WorkspaceAdvertisement:
    if workspace_root is None:
        return WorkspaceAdvertisement(status="not_attached", workspace_path=None)
    workspace = workspace_root.expanduser().resolve(strict=False)

    def failed(code: WorkspaceAdvertisementFailureCode) -> WorkspaceAdvertisement:
        return WorkspaceAdvertisement(
            status="failed", workspace_path=str(workspace), failure_code=code
        )

    try:
        workspace_format = workspace_git_object_format(workspace)
        if workspace_format != ledger_object_format:
            return failed("object_format_mismatch")
        _, common = _repository(workspace)
        _configure_remote(common, _config(common / "config"), str(ledger_path.resolve()), workspace)
    except UnicodeError:
        return failed("remote_conflict")
    except ValueError as exc:
        return failed(cast(WorkspaceAdvertisementFailureCode, exc.args[0]))
    source, destination = _LocalLedger(ledger_path), _LocalLedger(common)
    prefix = "refs/remotes/cruxible-ledger/"
    try:
        desired = {
            prefix + "proposals/" + ref.removeprefix("refs/heads/proposals/"): oid
            for ref, oid in source._refs("refs/heads/proposals/").items()
        }
        head = source._resolve_ref("refs/heads/main")
        if head is not None:
            desired[prefix + "accepted"] = head
        for ref in _NOTE_REFS:
            oid = source._resolve_ref(ref)
            if oid is not None:
                desired[ref] = oid
        _copy_objects(ledger_path, common, tuple(desired.values()))
        existing = destination._refs(prefix)
        commands = [
            f"update {ref} {oid}"
            for ref, oid in desired.items()
            if destination._resolve_ref(ref) != oid
        ]
        commands.extend(f"delete {ref}" for ref in existing if ref not in desired)
        # Reuse the files-backend lease protocol, with no advisory fsyncs.
        with destination.request_pack():
            if commands:
                destination._update_refs(commands)
    except (OSError, ValueError, GitError):
        return failed("fetch_failed")
    return WorkspaceAdvertisement(
        status="updated",
        workspace_path=str(workspace),
        advertised_refs=tuple(sorted(ref for ref in desired if ref.startswith(prefix))),
    )


def advertise_workspace_refs(
    *, workspace_root: Path | None, ledger_path: Path, ledger_object_format: GitObjectFormat
) -> WorkspaceAdvertisement:
    """Refresh the daemon-owned namespace and reduce every failure to typed advice."""
    try:
        return _advertise_workspace_refs(
            workspace_root=workspace_root,
            ledger_path=ledger_path,
            ledger_object_format=ledger_object_format,
        )
    except BaseException:
        return WorkspaceAdvertisement(
            status="failed",
            workspace_path=None if workspace_root is None else str(workspace_root),
            failure_code="unexpected_failure",
        )


__all__ = [
    "advertise_workspace_refs",
    "containing_git_workspace_root",
    "workspace_git_object_format",
]

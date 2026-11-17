"""Shared CLI/MCP adapter for deterministic local Cruxible file compilation."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.file_catalog import (
    FileCatalog,
    FileCatalogEntry,
    FileCatalogEntryAny,
    FileCatalogRenameRequired,
    FileCompilationBundle,
    ProcedureProjectionCatalogEntry,
    compile_file_catalog,
    merge_file_catalogs,
    refuse_legacy_file_catalogs,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.workspace_layout import workspace_path
from cruxible_client.errors import DataValidationError

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]


class WorkspaceFileError(DataValidationError):
    """Local catalog paths or aliases are not a valid compilation request."""


class FileContextClient(Protocol):
    def file_context(
        self, instance_id: str, *, documents: Sequence[str] | None = None
    ) -> contracts.FileContext: ...


def root_aliases(values: Iterable[str]) -> dict[str, Path]:
    aliases: dict[str, Path] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in aliases:
            raise WorkspaceFileError("root aliases must be unique NAME=PATH values")
        aliases[name] = Path(path).expanduser()
    return aliases


def mapped_root_aliases(values: Mapping[str, Path]) -> dict[str, Path]:
    aliases: dict[str, Path] = {}
    for name, path in values.items():
        if not name or "=" in name or name in aliases:
            raise WorkspaceFileError("root alias names must be unique nonempty text")
        aliases[name] = path
    return aliases


def load_file_catalog(
    portable_path: Path,
    local_path: Path | None,
) -> FileCatalog:
    for path in (portable_path, local_path):
        if path is not None and path.name in {"sources.yaml", "sources.local.yaml"}:
            raise FileCatalogRenameRequired((path,))
    portable = FileCatalog.model_validate(yaml.safe_load(portable_path.read_bytes()))
    local = (
        None
        if local_path is None
        else FileCatalog.model_validate(yaml.safe_load(local_path.read_bytes()))
    )
    return merge_file_catalogs(portable, local)


def compile_client_file_context(
    client: FileContextClient,
    instance_id: str,
    *,
    catalog: FileCatalog,
    repository_root: Path,
    aliases: Mapping[str, Path],
    documents: Sequence[str] | None = None,
) -> FileCompilationBundle:
    """Read local bytes against path-free accepted context from the daemon.

    ``documents`` names the only accepted Documents the catalog compiles
    against (one file's, for ``link --guides``), so the daemon looks those up
    instead of listing every Document; None asks for all of them.
    """

    context = (
        client.file_context(instance_id)
        if documents is None
        else client.file_context(instance_id, documents=documents)
    )
    accepted = {
        shell.document_id: shell
        for value in context.documents
        for shell in (DocumentShell.model_validate(value),)
    }
    return compile_file_catalog(
        catalog,
        repository_root=repository_root,
        root_aliases=dict(aliases),
        accepted_base=AcceptedCoordinate.model_validate(
            context.accepted_coordinate.model_dump(mode="json")
        ),
        accepted_documents=accepted,
    )


#: Why ``files add`` or ``files remove`` refused, changing nothing.
FileCatalogEditRefusal = Literal[
    "entry_exists",
    "entry_missing",
    "entry_invalid",
    "name_invalid",
    "catalog_invalid",
    "catalog_ambiguous",
    "path_outside_workspace",
    "path_not_a_file",
]


class FileCatalogEditError(WorkspaceFileError):
    """The workspace file catalog edit was refused; the catalog file is unchanged."""

    error_code = "cruxible.files.catalog_edit_refused"

    def __init__(self, reason: FileCatalogEditRefusal, detail: str) -> None:
        self.reason: FileCatalogEditRefusal = reason
        self.detail = detail
        super().__init__(f"{self.error_code} ({reason}): {detail}")


class FileCatalogEdit(BaseModel):
    """One entry added to, replaced in or removed from the workspace's portable catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["added", "replaced", "removed"]
    name: str
    catalog_path: str
    entry: FileCatalogEntry | None = None
    #: The last entry went, so the catalog file went with it (a catalog is never empty).
    catalog_removed: bool = False


#: The media type ``files add`` declares for a Document by its file's suffix.
_SUFFIX_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".csv": "text/csv",
}


def media_type_for(path: Path) -> str | None:
    """The media type a Document entry for ``path`` declares when none is given."""

    return _SUFFIX_MEDIA_TYPES.get(path.suffix.lower())


def _portable_catalog_path(root: Path) -> Path:
    """The workspace's one portable catalog file, or where a new one goes."""

    refuse_legacy_file_catalogs(root)
    found = tuple(
        path for path in (workspace_path(root, "files.yaml"), root / "files.yaml") if path.is_file()
    )
    if len(found) > 1:
        raise FileCatalogEditError(
            "catalog_ambiguous",
            "workspace has both .cruxible/files.yaml and files.yaml; keep exactly one",
        )
    return found[0] if found else workspace_path(root, "files.yaml")


def _read_portable(path: Path) -> tuple[FileCatalogEntryAny, ...]:
    if not path.is_file():
        return ()
    try:
        catalog = FileCatalog.model_validate(yaml.safe_load(path.read_bytes()))
    except (ValidationError, ValueError, yaml.YAMLError) as exc:
        raise FileCatalogEditError("catalog_invalid", f"{path.name}: {exc}") from exc
    if catalog.catalog_kind != "portable":
        raise FileCatalogEditError(
            "catalog_invalid", f"{path.name} declares catalog_kind {catalog.catalog_kind!r}"
        )
    return catalog.entries


def _entry_document(entry: FileCatalogEntryAny) -> dict[str, object]:
    if isinstance(entry, ProcedureProjectionCatalogEntry):
        # The entry kind stays spelled: it is what tells the two entry shapes apart.
        return {"kind": entry.kind, **entry.model_dump(mode="json", exclude_defaults=True)}
    return entry.model_dump(mode="json", exclude_defaults=True)


def _write_portable(path: Path, entries: tuple[FileCatalogEntryAny, ...]) -> None:
    """Validate the whole catalog, then replace the file atomically in its directory."""

    try:
        catalog = FileCatalog(catalog_kind="portable", entries=entries)
    except ValidationError as exc:
        raise FileCatalogEditError("entry_invalid", str(exc)) from exc
    document = {
        "catalog_kind": "portable",
        "entries": [_entry_document(entry) for entry in catalog.entries],
    }
    content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _workspace_locator(root: Path, path: Path) -> str:
    resolved = (path if path.is_absolute() else Path.cwd() / path).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise FileCatalogEditError(
            "path_outside_workspace",
            f"{path} is outside the workspace {root}; a portable catalog names "
            "workspace-relative files only",
        )
    if not resolved.is_file():
        raise FileCatalogEditError("path_not_a_file", f"{path} is not a regular file")
    return resolved.relative_to(root).as_posix()


#: A catalog name, as ``FileCatalogEntry.name`` takes it.
_CATALOG_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,127}")


def default_catalog_name(root: Path, path: Path) -> str:
    """The catalog name a workspace file takes when none is given: ``doc-`` and its path.

    A notes file a write creates, a workspace file a write cites, and ``files
    add`` without NAME all name the file this way. The path is lowercased and
    each ``/`` (and any other character a name cannot hold) becomes ``-``; when
    that cannot be read back to the one path (the path held a ``-``, a capital,
    such a character, or was cut to length), the name ends with ``-`` and 12 hex
    of the path's sha256, so two paths never share a name (``a/b.py`` is
    ``doc-a-b.py``; ``a-b.py`` is ``doc-a-b.py-<hex>``). A file already in the
    catalog keeps the name it has.
    """

    relative = path.relative_to(root).as_posix()
    flat = re.sub(r"[^a-z0-9_.-]+", "-", relative.lower()).strip("-.")
    if len(flat) <= 120 and flat.replace("-", "/") == relative:
        return "doc-" + flat
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    return f"doc-{flat[:110]}-{digest}"


@contextmanager
def _catalog_lock(root: Path) -> Iterator[None]:
    """Hold ``.cruxible/files.yaml.lock`` for one catalog read-modify-write.

    Two processes editing the catalog (two writes cataloguing files they cite,
    or ``files add`` beside one) take turns, so neither replaces the catalog
    from a snapshot taken before the other's edit.
    """

    if fcntl is None:  # pragma: no cover - no advisory locks on this platform
        yield
        return
    lock_path = workspace_path(root, "files.yaml.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o644
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)  # closing releases the lock


def add_catalog_entry(
    root: Path,
    *,
    name: str,
    path: Path,
    fields: Mapping[str, object],
    replace: bool = False,
) -> FileCatalogEdit:
    """Add NAME for PATH to the workspace's portable catalog (``.cruxible/files.yaml``).

    ``fields`` are the entry's other catalog fields (``document_id`` ...). The
    whole catalog is validated before anything is written: an existing NAME
    refuses unless ``replace``, and a duplicate Document target, a partial set
    of Document fields or a malformed value refuses with the model's reason.
    The file is rewritten canonically (entries sorted, defaults omitted).
    """

    root = root.expanduser().resolve()
    with _catalog_lock(root):
        return _add_catalog_entry(root, name=name, path=path, fields=fields, replace=replace)


def _add_catalog_entry(
    root: Path, *, name: str, path: Path, fields: Mapping[str, object], replace: bool
) -> FileCatalogEdit:
    catalog_path = _portable_catalog_path(root)
    entries = _read_portable(catalog_path)
    locator = _workspace_locator(root, path)
    if not _CATALOG_NAME.fullmatch(name):
        raise FileCatalogEditError(
            "name_invalid",
            f"{name!r} is not a catalog name: lowercase letters, digits, '_', '.' and '-', "
            f"starting with a letter, at most 128; e.g. "
            f"{default_catalog_name(root, root / locator)} (leave the name out to use that one)",
        )
    try:
        entry = FileCatalogEntry.model_validate({"name": name, "locator": locator, **fields})
    except ValidationError as exc:
        raise FileCatalogEditError("entry_invalid", str(exc)) from exc
    existing = tuple(
        item for item in entries if isinstance(item, FileCatalogEntry) and item.name == name
    )
    if existing and not replace:
        raise FileCatalogEditError(
            "entry_exists",
            f"file {name!r} is already catalogued at {existing[0].locator}; pass --replace "
            "(replace=True in the SDK) to update it",
        )
    kept = tuple(item for item in entries if item not in existing)
    _write_portable(catalog_path, (*kept, entry))
    return FileCatalogEdit(
        status="replaced" if existing else "added",
        name=name,
        catalog_path=catalog_path.relative_to(root).as_posix(),
        entry=entry,
    )


def catalog_file(
    root: Path,
    *,
    name: str | None = None,
    path: Path,
    document_id: str | None = None,
    document_kind: str | None = None,
    title: str | None = None,
    media_type: str | None = None,
    governance_scope: Iterable[str] = (),
    required_tier: Literal["governed_write", "graph_write", "admin"] | None = None,
    public_uri: str | None = None,
    replace: bool = False,
) -> FileCatalogEdit:
    """Catalogue PATH under NAME: what ``cruxible files add`` and ``cx.files.add`` both do.

    With ``document_id`` (and its kind, title and scope) the entry also declares
    the Document the file compiles to; its media type defaults from the file
    suffix and its scopes are sorted. Without one the file is a source to cite.
    ``name`` None names the file as a write that cites it would (``doc-<path>``).
    """

    scopes = tuple(governance_scope)
    fields: dict[str, object] = {
        key: value
        for key, value in {
            "document_id": document_id,
            "document_kind": document_kind,
            "title": title,
            "media_type": media_type,
            "required_tier": required_tier,
            "public_uri": public_uri,
        }.items()
        if value is not None
    }
    if document_id is not None:
        fields.setdefault("media_type", media_type_for(path))
        fields["governance_scope"] = tuple(
            sorted(set(scopes), key=lambda item: item.encode("utf-8"))
        )
        fields = {key: value for key, value in fields.items() if value is not None}
    elif scopes:
        fields["governance_scope"] = scopes
    if name is None:
        root = root.expanduser().resolve()
        name = default_catalog_name(root, root / _workspace_locator(root, path))
    return add_catalog_entry(root, name=name, path=path, fields=fields, replace=replace)


def remove_catalog_entry(root: Path, *, name: str) -> FileCatalogEdit:
    """Remove NAME from the workspace's portable catalog; the last entry removes the file."""

    root = root.expanduser().resolve()
    with _catalog_lock(root):
        return _remove_catalog_entry(root, name=name)


def _remove_catalog_entry(root: Path, *, name: str) -> FileCatalogEdit:
    catalog_path = _portable_catalog_path(root)
    entries = _read_portable(catalog_path)
    kept = tuple(
        item for item in entries if not (isinstance(item, FileCatalogEntry) and item.name == name)
    )
    if len(kept) == len(entries):
        known = ", ".join(item.name for item in entries if isinstance(item, FileCatalogEntry))
        raise FileCatalogEditError(
            "entry_missing", f"no catalogued file {name!r} (catalogued: {known or 'none'})"
        )
    if kept:
        _write_portable(catalog_path, kept)
    else:
        catalog_path.unlink()
    return FileCatalogEdit(
        status="removed",
        name=name,
        catalog_path=catalog_path.relative_to(root).as_posix(),
        catalog_removed=not kept,
    )


class WorkspaceCatalog:
    """The workspace's file catalog (``.cruxible/files.yaml``), from the SDK: ``cx.files``.

    The same edits as ``cruxible files add`` and ``cruxible files remove``, through
    the same code. A relative path is read from the workspace root. Writes only
    the catalog file. Next: ``cx.file(path).section(...)`` cites the file.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = root

    def add(
        self,
        name: str | Path | None = None,
        path: str | Path | None = None,
        *,
        document_id: str | None = None,
        document_kind: str | None = None,
        title: str | None = None,
        media_type: str | None = None,
        governance_scope: Iterable[str] = (),
        required_tier: Literal["governed_write", "graph_write", "admin"] | None = None,
        public_uri: str | None = None,
        replace: bool = False,
    ) -> FileCatalogEdit:
        """Catalogue PATH under NAME, the source name evidence is cited under.

        ``add(path)`` alone names the file ``doc-<path>``, as a write citing it would.

        With ``document_id``, ``document_kind``, ``title`` and ``governance_scope``
        the entry also declares the Document the file compiles to. An existing
        NAME refuses (``cruxible.files.catalog_edit_refused``, ``entry_exists``)
        unless ``replace=True``.
        Next: ``cx.file(path).section("Heading")`` to cite it.
        """

        if path is None:
            if name is None:
                raise TypeError("files.add() takes (path) or (name, path)")
            name, path = None, name
        assert path is not None
        target = Path(path).expanduser()
        return catalog_file(
            self._root,
            name=None if name is None else str(name),
            path=target if target.is_absolute() else self._root / target,
            document_id=document_id,
            document_kind=document_kind,
            title=title,
            media_type=media_type,
            governance_scope=governance_scope,
            required_tier=required_tier,
            public_uri=public_uri,
            replace=replace,
        )

    def remove(self, name: str) -> FileCatalogEdit:
        """Remove NAME's entry; citations already made keep their retained passage.

        Next: ``cx.files.add(name, path)`` to catalogue it again.
        """

        return remove_catalog_entry(self._root, name=name)


__all__ = [
    "FileCatalogEdit",
    "FileCatalogEditError",
    "FileCatalogEditRefusal",
    "WorkspaceCatalog",
    "WorkspaceFileError",
    "add_catalog_entry",
    "catalog_file",
    "media_type_for",
    "remove_catalog_entry",
    "compile_client_file_context",
    "load_file_catalog",
    "mapped_root_aliases",
    "root_aliases",
]

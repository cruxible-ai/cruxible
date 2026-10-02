"""Client-owned Playbill floor verification and workspace replacement.

The daemon returns inert bytes. This module is the shared CLI/MCP adapter that
verifies those bytes and writes them locally without ever sending a path to the
daemon.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from cruxible_client import contracts
from cruxible_client.authoring.blocks import (
    ProjectionMarkerError,
    parse_projection_blocks,
    sync_projection_blocks,
)
from cruxible_client.authoring.floor_apply import (
    _DIRECTORY,
    PlaybillFloorApplyError,
    _directory,
    _read_at,
    _write_file,
    apply_floor_delta,
    read_floor_manifest,
)
from cruxible_client.authoring.projection_manifests import load_projection_manifests
from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_CARDS_PER_SOURCE,
    MAX_PROJECTION_COVERAGE_BINDINGS,
    PlaybillPresentationPolicyAny,
    PlaybillPresentationPolicyNoteV1,
    PlaybillPresentationPolicyV1,
    PlaybillPresentationPolicyV2,
    PlaybillProjectionCoverageBindingV1,
    PlaybillProjectionCoverageObservationV1,
    projection_manifest_refs,
    projection_processing_policy,
    read_projection_source,
    resolve_projection_manifest_digest,
    upgrade_playbill_presentation_policy,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.floor import (
    PLAYBILL_FLOOR_FORMAT,
    PLAYBILL_FLOOR_LOCAL_PATHS,
    PlaybillFloorApplyResultV1,
    PlaybillFloorDeltaV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.workspace_layout import PLAYBILL_FLOOR_PATH

_CONFIG_PATH = PurePosixPath(".playbill/coverage.json")
_CONFIG_EXCLUDE_RULE = b"/.playbill/coverage.json\n"
_FLOOR_DOMAIN = PLAYBILL_FLOOR_FORMAT
_FLOOR_DOMAINS = {"playbill-floor-export-v2", _FLOOR_DOMAIN}
_WORKSPACE_CONFIG_TAG = "playbill-coverage-workspace-config-v2"
_FLOOR_OUTPUT = {
    "tag": "playbill-floor-output-v1",
    "format": _FLOOR_DOMAIN,
}
_FLOOR_PARTS: tuple[contracts.PlaybillFloorExportPart, ...] = ("discovery",)


def _floor_output(include: Sequence[str] = ()) -> dict[str, Any]:
    """The floor_output profile: the fixed format, plus any opt-in export parts."""

    parts = sorted(set(include))
    unknown = [part for part in parts if part not in _FLOOR_PARTS]
    if unknown:
        raise PlaybillWorkspaceError(f"unsupported floor export part(s): {', '.join(unknown)}")
    return {**_FLOOR_OUTPUT, **({"include": parts} if parts else {})}


def _profile_include(output: Mapping[str, Any]) -> tuple[contracts.PlaybillFloorExportPart, ...]:
    include = output.get("include", [])
    if not isinstance(include, list) or any(item not in _FLOOR_PARTS for item in include):
        raise PlaybillWorkspaceError("coverage floor_output.include is not a list of export parts")
    if include != sorted(set(include)) or not include and "include" in output:
        raise PlaybillWorkspaceError("coverage floor_output.include must be sorted and nonempty")
    return cast(tuple[contracts.PlaybillFloorExportPart, ...], tuple(include))


_WORKSPACE_CONFIG_FIELDS = frozenset(
    {
        "tag",
        "instance_id",
        "server_url",
        "server_socket",
        "root",
        "rules",
        "scan_budget",
        "max_observed_paths",
        "floor_output",
    }
)
_SECRET_FIELD_FRAGMENTS = ("bearer", "credential", "password", "secret", "token")


class PlaybillWorkspaceError(PlaybillError, ValueError):
    """A client workspace or exported floor failed deterministic validation."""


class PlaybillWorkspaceAttachmentError(PlaybillWorkspaceError):
    """Daemon registration and the requested client workspace disagree."""

    error_code = "playbill.workspace.registration_disagrees"

    def __init__(
        self,
        *,
        instance_id: str,
        requested_workspace: str,
        registered_workspace: str | None,
    ) -> None:
        self.instance_id = instance_id
        self.requested_workspace = requested_workspace
        self.registered_workspace = registered_workspace
        self.repair_commands = (
            f"cruxible playbill host create --instance-id {instance_id} "
            f"--workspace {requested_workspace}",
        )
        super().__init__(
            f"{self.error_code}: host {instance_id!r} is not registered to workspace "
            f"{requested_workspace!r} (registered={registered_workspace!r}); repair: "
            f"{self.repair_commands[0]}"
        )


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(fragment in normalized for fragment in _SECRET_FIELD_FRAGMENTS):
                return True
            if _contains_secret_field(child):
                return True
    elif isinstance(value, list | tuple):
        return any(_contains_secret_field(child) for child in value)
    elif isinstance(value, str):
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return parsed.username is not None or parsed.password is not None
    return False


def _workspace_git_common_dir(workspace: Path) -> Path | None:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT")
        if key in os.environ
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise PlaybillWorkspaceError(f"Git common directory cannot be resolved: {exc}") from exc


def _ensure_workspace_config_ignored(workspace: Path) -> None:
    common_dir = _workspace_git_common_dir(workspace)
    if common_dir is None:
        return
    info_dir = common_dir / "info"
    exclude_path = info_dir / "exclude"
    if info_dir.is_symlink() or exclude_path.is_symlink():
        raise PlaybillWorkspaceError("Git info/exclude path must not be a symbolic link")
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_bytes() if exclude_path.exists() else b""
    except OSError as exc:
        raise PlaybillWorkspaceError(f"Git info/exclude cannot be read: {exc}") from exc
    if _CONFIG_EXCLUDE_RULE.rstrip(b"\n") in existing.splitlines():
        return
    content = existing
    if content and not content.endswith(b"\n"):
        content += b"\n"
    content += _CONFIG_EXCLUDE_RULE
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=".exclude.",
            dir=info_dir,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        assert temporary is not None
        mode = exclude_path.stat().st_mode & 0o777 if exclude_path.exists() else 0o644
        os.chmod(temporary, mode)
        os.replace(temporary, exclude_path)
        temporary = None
        descriptor = os.open(info_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PlaybillWorkspaceError(
            f"Git info/exclude could not be written atomically: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_workspace_config(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise PlaybillWorkspaceError("coverage config must not be a symbolic link")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaybillWorkspaceError(f"coverage config is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlaybillWorkspaceError("coverage config is not an object")
    if _contains_secret_field(payload):
        raise PlaybillWorkspaceError(
            "coverage config contains a forbidden bearer, credential, password, secret, or "
            "token field"
        )
    unknown = sorted(set(payload).difference(_WORKSPACE_CONFIG_FIELDS))
    if unknown:
        raise PlaybillWorkspaceError(
            f"coverage config contains unsupported field(s): {', '.join(unknown)}"
        )
    return payload


def _atomic_write_workspace_config(path: Path, payload: Mapping[str, Any]) -> None:
    if _contains_secret_field(payload):  # defensive: writer inputs are fixed below
        raise PlaybillWorkspaceError("coverage config writer refuses secret-bearing data")
    content = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _write_workspace_local(path.parent.parent, _CONFIG_PATH.as_posix(), content, durable=True)


def _planned_workspace_config(
    workspace: str | Path,
    *,
    instance_id: str | None,
    server_url: str | None = None,
    server_socket: str | None = None,
    replace: bool = False,
) -> tuple[Path, dict[str, Any] | None, bool]:
    root = _workspace_root(workspace)
    if instance_id is not None and not instance_id.strip():
        raise PlaybillWorkspaceError("workspace instance_id must be nonempty")
    transports = [value for value in (server_url, server_socket) if value is not None]
    if len(transports) != 1 or not transports[0].strip():
        raise PlaybillWorkspaceError("workspace config requires exactly one nonempty transport")
    if _contains_secret_field({"transport": transports[0]}):
        raise PlaybillWorkspaceError(
            "workspace config refuses URL user information; pass credentials with "
            "CRUXIBLE_SERVER_BEARER_TOKEN"
        )
    path = root / _CONFIG_PATH
    try:
        existing = _read_workspace_config(path)
    except PlaybillWorkspaceError:
        if not replace:
            raise
        existing = None
    if instance_id is None:
        if existing is not None and not replace:
            raise PlaybillWorkspaceError(
                f"refusing to overwrite differing workspace config {path}; rerun with --replace"
            )
        return path, None, False
    desired: dict[str, Any] = (
        dict(existing)
        if existing is not None
        and existing.get("tag") in {"playbill-coverage-workspace-config-v1", _WORKSPACE_CONFIG_TAG}
        else {}
    )
    previous_output = (existing or {}).get("floor_output")
    previous_include = (
        previous_output.get("include", []) if isinstance(previous_output, Mapping) else []
    )
    desired.update(
        {
            "tag": _WORKSPACE_CONFIG_TAG,
            "instance_id": instance_id,
            "floor_output": _floor_output(
                previous_include if isinstance(previous_include, list) else ()
            ),
        }
    )
    desired.pop("server_url", None)
    desired.pop("server_socket", None)
    desired["server_url" if server_url is not None else "server_socket"] = (
        server_url if server_url is not None else server_socket
    )
    if existing == desired:
        return path, desired, True
    if existing is not None and not replace:
        raise PlaybillWorkspaceError(
            f"refusing to overwrite differing workspace config {path}; rerun with --replace"
        )
    return path, desired, False


def validate_playbill_workspace_config_write(
    workspace: str | Path,
    *,
    instance_id: str | None,
    server_url: str | None = None,
    server_socket: str | None = None,
    replace: bool = False,
) -> None:
    """Refuse a differing config before a host or init request mutates daemon state."""

    _planned_workspace_config(
        workspace,
        instance_id=instance_id,
        server_url=server_url,
        server_socket=server_socket,
        replace=replace,
    )


def write_playbill_workspace_config(
    workspace: str | Path,
    *,
    instance_id: str,
    server_url: str | None = None,
    server_socket: str | None = None,
    replace: bool = False,
) -> Path:
    """Attach one workspace target without ever accepting or persisting a secret."""

    path, desired, current = _planned_workspace_config(
        workspace,
        instance_id=instance_id,
        server_url=server_url,
        server_socket=server_socket,
        replace=replace,
    )
    assert desired is not None
    _ensure_workspace_config_ignored(path.parent.parent)
    if current:
        return path
    _atomic_write_workspace_config(path, desired)
    return path


def record_playbill_floor_output(
    workspace: str | Path,
    *,
    instance_id: str,
    server_url: str | None = None,
    server_socket: str | None = None,
    include: Sequence[contracts.PlaybillFloorExportPart] = (),
) -> Path:
    """Record the fixed floor output and its opt-in parts, keeping safe coverage fields.

    Refresh after an activation exports the parts recorded here. A profile an
    earlier build wrote (the v2 or v3 format) is rewritten to the current one.
    """

    root = _workspace_root(workspace)
    path = root / _CONFIG_PATH
    desired_output = _floor_output(include)
    existing = _read_workspace_config(path)
    if existing is None:
        written = write_playbill_workspace_config(
            root,
            instance_id=instance_id,
            server_url=server_url,
            server_socket=server_socket,
        )
        current = _read_workspace_config(written)
        if current is not None and current.get("floor_output") != desired_output:
            _atomic_write_workspace_config(written, {**current, "floor_output": desired_output})
        return written
    tag = existing.get("tag")
    if tag not in {
        "playbill-coverage-workspace-config-v1",
        _WORKSPACE_CONFIG_TAG,
    }:
        raise PlaybillWorkspaceError("coverage config has an unsupported tag")
    output = existing.get("floor_output")
    if output is not None:
        if not isinstance(output, Mapping) or output.get("tag") != "playbill-floor-output-v1":
            raise PlaybillWorkspaceError("coverage floor_output has an unsupported profile")
        if output == desired_output:
            return path
    desired = dict(existing)
    desired["tag"] = _WORKSPACE_CONFIG_TAG
    desired["floor_output"] = desired_output
    _atomic_write_workspace_config(path, desired)
    return path


def _presentation_policy(
    root: Path,
    *,
    known_source_ids: Sequence[str],
) -> tuple[PlaybillPresentationPolicyV2 | None, tuple[PlaybillPresentationPolicyNoteV1, ...]]:
    path = root / ".playbill" / "presentation-policy.json"
    try:
        if not path.exists():
            return PlaybillPresentationPolicyV2(), ()
        resolved = path.resolve(strict=True)
    except OSError:
        return None, ("presentation_policy_unreadable",)
    if not resolved.is_relative_to(root):
        return None, ("presentation_policy_path_escape",)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, Mapping) and raw.get("tag") == "playbill-presentation-policy-v2":
            parsed: PlaybillPresentationPolicyAny = PlaybillPresentationPolicyV2.model_validate(raw)
        else:
            parsed = PlaybillPresentationPolicyV1.model_validate(raw)
        policy = upgrade_playbill_presentation_policy(parsed)
    except OSError:
        return None, ("presentation_policy_unreadable",)
    except (ValueError, json.JSONDecodeError):
        return None, ("presentation_policy_malformed",)
    unknown = tuple(
        source_id
        for source_id in policy.archival_source_ids
        if source_id not in set(known_source_ids)
    )
    if unknown:
        return None, ("presentation_policy_unknown_source_id",)
    return policy, ()


def _observe_presentation_policy(
    observation: dict[str, object],
    root: Path,
    *,
    known_source_ids: Sequence[str],
) -> None:
    policy, notes = _presentation_policy(root, known_source_ids=known_source_ids)
    observation["presentation_policy"] = None if policy is None else policy.model_dump(mode="json")
    observation["presentation_policy_notes"] = list(notes)


class _FloorClient(Protocol):
    def activate_playbill_proposal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillActivationReceipt: ...

    def export_playbill_floor(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        include: Sequence[contracts.PlaybillFloorExportPart] = (),
    ) -> contracts.PlaybillFloorExport: ...

    def playbill_floor_delta(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        base_generation: int | None = None,
        base_renderer: str | None = None,
    ) -> PlaybillFloorDeltaV1: ...

    def check_playbill_projection_blocks(
        self,
        instance_id: str,
        *,
        request: contracts.PlaybillProjectionCheckRequestV1,
    ) -> contracts.PlaybillProjectionCheckResultV1: ...


class _CoverageClient(Protocol):
    def resolve_playbill_coverage(
        self,
        instance_id: str,
        *,
        observations: Sequence[Mapping[str, Any]],
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        budget: Mapping[str, Any] | None = None,
        scan_budget: Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillCoverageResult: ...

    def playbill_head(self, instance_id: str) -> contracts.PlaybillHeadV1: ...


def _canonical_json(value: object) -> bytes:
    def normalize(item: object) -> object:
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            raise PlaybillWorkspaceError("floor manifest contains a floating-point value")
        if isinstance(item, str):
            normalized = unicodedata.normalize("NFC", item)
            if normalized != item:
                raise PlaybillWorkspaceError("floor manifest text is not NFC-normalized")
            return item
        if isinstance(item, list):
            return [normalize(value) for value in item]
        if isinstance(item, Mapping):
            normalized_map: dict[str, object] = {}
            for key, value in item.items():
                if not isinstance(key, str):
                    raise PlaybillWorkspaceError("floor manifest keys must be strings")
                normalized_key = unicodedata.normalize("NFC", key)
                if normalized_key in normalized_map:
                    raise PlaybillWorkspaceError("floor manifest keys collide after NFC")
                normalized_map[normalized_key] = normalize(value)
            return normalized_map
        raise PlaybillWorkspaceError(f"floor manifest contains unsupported {type(item).__name__}")

    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _typed_digest(domain: str, payload: Mapping[str, object]) -> str:
    if "tag" in payload:
        raise PlaybillWorkspaceError("floor digest payload may not supply tag")
    digest = hashlib.sha256(_canonical_json({"tag": domain, **payload})).hexdigest()
    return f"sha256:{digest}"


def _safe_export_path(value: object) -> str:
    if not isinstance(value, str):
        raise PlaybillWorkspaceError("floor export path is not text")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise PlaybillWorkspaceError(f"floor export path escapes its root: {value}")
    return value


def verified_floor_files(export: contracts.PlaybillFloorExport) -> dict[str, bytes]:
    """Verify the v2 envelope, manifest, inventory, and bytes."""

    if export.tag not in _FLOOR_DOMAINS:
        raise PlaybillWorkspaceError("configured floor refresh requires floor export v2 or v5")
    manifest = export.manifest
    if manifest.get("tag") != export.tag.replace("export", "manifest"):
        raise PlaybillWorkspaceError("floor export manifest has an unsupported tag")
    if manifest.get("format") != export.tag:
        raise PlaybillWorkspaceError("floor export manifest has an unsupported format")
    coordinate = manifest.get("coordinate")
    if coordinate != export.coordinate.model_dump(mode="json"):
        raise PlaybillWorkspaceError("floor export envelope and manifest coordinates differ")
    inventory = manifest.get("files")
    if not isinstance(inventory, list):
        raise PlaybillWorkspaceError("floor export manifest inventory is not a list")

    decoded: dict[str, bytes] = {}
    for exported_file in export.files:
        path = _safe_export_path(exported_file.path)
        if path in decoded:
            raise PlaybillWorkspaceError(f"floor export repeats path: {path}")
        try:
            decoded[path] = base64.b64decode(exported_file.content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise PlaybillWorkspaceError("floor export contains invalid base64 bytes") from exc

    expected_paths = {"manifest.json"}
    for raw_item in inventory:
        if not isinstance(raw_item, Mapping):
            raise PlaybillWorkspaceError("floor manifest inventory entry is not an object")
        expected_paths.add(_safe_export_path(raw_item.get("path")))
    if set(decoded) != expected_paths:
        raise PlaybillWorkspaceError("floor export files differ from the manifest inventory")
    try:
        decoded_manifest = json.loads(decoded["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaybillWorkspaceError("floor export manifest bytes are invalid") from exc
    if decoded_manifest != manifest:
        raise PlaybillWorkspaceError("floor export manifest bytes differ from the envelope")

    for raw_item in inventory:
        assert isinstance(raw_item, Mapping)
        path = _safe_export_path(raw_item.get("path"))
        content = decoded[path]
        byte_length = raw_item.get("byte_length")
        content_digest = raw_item.get("content_digest")
        if not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length < 0:
            raise PlaybillWorkspaceError(f"floor export byte length is invalid for {path}")
        if len(content) != byte_length:
            raise PlaybillWorkspaceError(f"floor export byte length differs for {path}")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if digest != content_digest:
            raise PlaybillWorkspaceError(f"floor export content digest differs for {path}")
    expected_floor_digest = _typed_digest(export.tag, {"files": inventory})
    if manifest.get("floor_digest") != expected_floor_digest:
        raise PlaybillWorkspaceError("floor export root digest differs from its inventory")
    return decoded


def _workspace_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve()


def _relative_destination(workspace: Path, relative_path: str) -> Path:
    if relative_path != PLAYBILL_FLOOR_PATH:
        raise PlaybillWorkspaceError(f"floor output path is fixed at {PLAYBILL_FLOOR_PATH}")
    destination = workspace / relative_path
    try:
        resolved = destination.resolve()
    except OSError as exc:
        raise PlaybillWorkspaceError(f"could not resolve configured floor output: {exc}") from exc
    if not resolved.is_relative_to(workspace):
        raise PlaybillWorkspaceError("configured floor output escapes the workspace root")
    return destination


def configured_floor_output(
    workspace: str | Path,
) -> tuple[str, tuple[contracts.PlaybillFloorExportPart, ...]] | None:
    """The declared floor path and its opt-in export parts, or ``None`` when unconfigured."""

    root = _workspace_root(workspace)
    config_path = root / _CONFIG_PATH
    if not config_path.exists():
        return None
    try:
        config: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaybillWorkspaceError(f"coverage config is invalid: {exc}") from exc
    if not isinstance(config, Mapping):
        raise PlaybillWorkspaceError("coverage config is not an object")
    if config.get("tag") != "playbill-coverage-workspace-config-v2":
        return None
    output = config.get("floor_output")
    if output is None:
        return None
    if not isinstance(output, Mapping):
        raise PlaybillWorkspaceError("coverage floor_output is not an object")
    if "path" in output:
        raise PlaybillWorkspaceError(
            f"coverage floor_output.path is obsolete; the path is fixed at {PLAYBILL_FLOOR_PATH}"
        )
    if (
        output.get("tag") != "playbill-floor-output-v1"
        or output.get("format") not in _FLOOR_DOMAINS
        or set(output) - {"tag", "format", "include"}
    ):
        raise PlaybillWorkspaceError(
            "coverage floor_output has an unsupported profile; rewrite it with "
            "`cruxible playbill floor export --force`"
        )
    include = _profile_include(output)
    _relative_destination(root, PLAYBILL_FLOOR_PATH)
    return PLAYBILL_FLOOR_PATH, include


def configured_floor_path(workspace: str | Path) -> str | None:
    """Return the declared floor path, or ``None`` when absent/unconfigured."""

    configured = configured_floor_output(workspace)
    return None if configured is None else configured[0]


def _holds_exactly(destination: Path, files: Mapping[str, bytes]) -> bool:
    """Whether `destination` holds exactly these files, byte for byte, and no links."""

    if not destination.is_dir() or destination.is_symlink():
        return False
    observed: set[str] = set()
    for parent, directories, filenames in os.walk(destination, followlinks=False):
        if any((Path(parent) / name).is_symlink() for name in directories):
            return False
        for name in filenames:
            source = Path(parent) / name
            relative = source.relative_to(destination).as_posix()
            if relative in PLAYBILL_FLOOR_LOCAL_PATHS:
                # Client-written from workspace bindings; never daemon bytes.
                continue
            observed.add(relative)
            if source.is_symlink() or relative not in files:
                return False
            try:
                if source.read_bytes() != files[relative]:
                    return False
            except OSError:
                return False
    return observed == set(files)


def _replace_exact(destination: Path, files: Mapping[str, bytes], *, root: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.parent.resolve().is_relative_to(root):
        raise PlaybillWorkspaceError("configured floor output parent escapes the workspace root")
    # Verify bytes, not just the manifest: a locally edited derived file must
    # be repaired even when the accepted coordinate has not moved. Never
    # reuse symlinks or share writable inodes with the previous installation.
    if _holds_exactly(destination, files):
        return
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.playbill-floor-", dir=destination.parent)
    )
    backup = destination.parent / f".{destination.name}.playbill-backup-{secrets.token_hex(8)}"
    moved_old = False
    installed = False
    try:
        stage_root = stage.resolve()
        for path, content in files.items():
            target = (stage / path).resolve()
            if not target.is_relative_to(stage_root):  # pragma: no cover - prevalidated
                raise PlaybillWorkspaceError(f"floor export path escapes its stage: {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if destination.exists() or destination.is_symlink():
            destination.rename(backup)
            moved_old = True
        stage.rename(destination)
        installed = True
    except Exception:
        if moved_old and not (destination.exists() or destination.is_symlink()):
            backup.rename(destination)
            moved_old = False
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if installed and moved_old and backup.exists():
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()


def materialize_playbill_floor(
    workspace: str | Path,
    *,
    export: contracts.PlaybillFloorExport,
    force: bool = True,
) -> contracts.PlaybillWorkspaceFloorWriteResult:
    """Verify and exactly replace one workspace-relative floor directory."""

    root = _workspace_root(workspace)
    relative_path = PLAYBILL_FLOOR_PATH
    destination = _relative_destination(root, relative_path)
    files = verified_floor_files(export)
    if destination.exists() and any(destination.iterdir()) and not force:
        # Already exactly this floor (as it is right after an activation's
        # refresh): nothing to write, and nothing of the caller's is at risk.
        if not _holds_exactly(destination, files):
            raise PlaybillWorkspaceError(
                f"refusing to write the floor into a non-empty directory: {destination}"
            )
        write_projection_index(root)
        return contracts.PlaybillWorkspaceFloorWriteResult(
            status="unchanged",
            path=relative_path,
            destination=str(destination),
            floor_digest=str(export.manifest["floor_digest"]),
            coordinate=export.coordinate,
            file_count=len(export.files),
        )
    _replace_exact(destination, files, root=root)
    write_projection_index(root)
    return contracts.PlaybillWorkspaceFloorWriteResult(
        path=relative_path,
        destination=str(destination),
        floor_digest=str(export.manifest["floor_digest"]),
        coordinate=export.coordinate,
        file_count=len(export.files),
    )


PROJECTIONS_INDEX_PATH = "projections/INDEX"
SOURCES_INDEX_PATH = "sources/INDEX"
_SOURCES_LEDGER_PATH = "sources/LEDGER"


def _sources_ledger(floor: Path) -> tuple[str, list[list[str]]] | None:
    """The floor's ``sources/LEDGER``: its header and its five-cell rows."""

    try:
        text = (floor / _SOURCES_LEDGER_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    header = ""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("#"):
            header = header or line
            continue
        cells = line.split("\t")
        if len(cells) == 5 and cells[4].isdigit():
            rows.append(cells)
    return header, rows


def _sources_generations(rows: Sequence[Sequence[str]]) -> dict[str, str]:
    """Each source, and each Document ref a source lives at, to the generation it last changed."""

    generations: dict[str, str] = {}
    for source, _contracts, locator, _citing, changed in rows:
        for key in (source, locator) if locator.startswith("Document:") else (source,):
            known = generations.get(key)
            if known is None or int(changed) > int(known):
                generations[key] = changed
    return generations


def _workspace_locators(root: Path, sources: WorkspaceSources | None) -> dict[str, str]:
    """Each catalog source name and ``Document:<id>`` to its workspace path.

    A path outside the workspace stays absolute; a bound file that does not
    exist is marked ``(missing)``, which ``next`` reports for repair.
    """

    found: dict[str, str] = {}
    for entry in () if sources is None else sources.document_entries:
        try:
            path = sources.path_for_source(entry.name) if sources is not None else None
        except (OSError, ValueError, PlaybillError):
            path = None
        if path is None:
            shown = entry.locator
            missing = False
        else:
            shown = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
            missing = not path.is_file()
        located = f"{shown} (missing)" if missing else shown
        found.setdefault(entry.name, located)
        found.setdefault(f"Document:{entry.document_id}", located)
    return found


def _write_workspace_local(
    workspace: Path,
    relative: str,
    content: bytes,
    *,
    durable: bool = False,
    only_if_changed: bool = False,
) -> None:
    """Anchor the workspace and every output component without following links.

    Keep directory creation, staging and replacement on floor_apply's held
    descriptors: a workspace process swapping a parent must not redirect a
    local join or coverage profile outside the workspace.
    """

    anchor = os.open(workspace.anchor, _DIRECTORY)
    try:
        with _directory(anchor, workspace.parts[1:], create=True) as directory:
            assert directory is not None
            if only_if_changed:
                parts = PurePosixPath(relative).parts
                with _directory(directory, parts[:-1], create=True) as parent:
                    assert parent is not None
                    try:
                        if _read_at(parent, parts[-1]) == content:
                            return
                    except FileNotFoundError:
                        pass
                    _write_file(parent, parts[-1], content, mode=0o600, durable=durable)
            else:
                _write_file(directory, relative, content, mode=0o600, durable=durable)
    except (OSError, PlaybillFloorApplyError) as exc:
        raise PlaybillWorkspaceError(f"{relative} could not be written atomically: {exc}") from exc
    finally:
        os.close(anchor)


def _write_floor_local(workspace: Path, relative: str, text: str) -> None:
    _write_workspace_local(workspace, f"{PLAYBILL_FLOOR_PATH}/{relative}", text.encode("utf-8"))


def _rendered_blocks(root: Path, content: bytes) -> list[tuple[str, str]]:
    """Each compact projection block in ``content``: its ref and declared generation."""

    directory = root / ".playbill/manifests"
    blocks: list[tuple[str, str]] = []
    try:
        refs = projection_manifest_refs(content)
    except (PlaybillError, ValueError):
        return blocks
    for ref in refs:
        try:
            digest = (
                ref
                if ref.startswith("sha256:")
                else resolve_projection_manifest_digest(
                    ref, ("sha256:" + path.stem for path in directory.glob(ref + "*.json"))
                )
            )
            manifest = json.loads(
                (directory / (digest.removeprefix("sha256:") + ".json")).read_text(encoding="utf-8")
            )
        except (PlaybillError, OSError, ValueError):
            continue
        if not isinstance(manifest, Mapping):
            continue
        source_id, block_id = manifest.get("source_id"), manifest.get("block_id")
        generation = manifest.get("declared_generation")
        if isinstance(source_id, str) and isinstance(block_id, str):
            blocks.append(
                (f"{source_id}#{block_id}", str(generation) if isinstance(generation, int) else "-")
            )
    return blocks


def write_projection_index(workspace: str | Path) -> int | None:
    """Join the floor's ledger-pure sources with this workspace's bindings.

    Writes local files outside the coordinate-pure manifest whichever workspace
    writer applies it:

    - ``.gitignore``: ``*`` to ignore every floor file, including itself;

    - ``sources/INDEX``: ``sources/LEDGER`` with each source's locator replaced
      by the workspace path its catalog entry (by source name, or by the
      Document it lives at) binds it to; a source the catalog does not bind
      keeps its ledger locator;
    - ``projections/INDEX``: one line per workspace file bound to accepted
      state -- a Document body, an evidence source, or a rendered projection
      block -- with its role, the ref it is bound to, and the generation that
      ref last changed. A bound file that does not exist is left out; ``next``
      reports it for repair.

    Returns the number of ``projections/INDEX`` lines, or ``None`` when there
    is no v5 floor.
    """

    # Keep the supplied workspace path for no-follow output traversal. Resolving
    # it again must not bless a replacement symlink after a caller's check.
    output_root = Path(workspace).expanduser().absolute()
    root = _workspace_root(workspace)
    floor = _relative_destination(root, PLAYBILL_FLOOR_PATH)
    if not floor.is_dir():
        return None
    _write_workspace_local(
        output_root, f"{PLAYBILL_FLOOR_PATH}/.gitignore", b"*\n", only_if_changed=True
    )
    ledger = _sources_ledger(floor)
    if ledger is None:
        return None
    ledger_header, ledger_rows = ledger
    generations = _sources_generations(ledger_rows)
    try:
        sources: WorkspaceSources | None = WorkspaceSources(root)
    except (OSError, ValueError, PlaybillError):
        sources = None
    locators = _workspace_locators(root, sources)
    joined = [
        [source, contracts_cell, locators.get(source) or locators.get(locator) or locator, *rest]
        for source, contracts_cell, locator, *rest in ledger_rows
    ]
    stamp = ledger_header.rsplit("  ", 1)[-1]
    sources_header = (
        f"# sources INDEX  {len(joined)} sources  columns: source, contracts, locator, "
        f"citing claims, changed gen  {stamp}  (locators joined from the local "
        "workspace catalog)"
    )
    _write_floor_local(
        output_root,
        SOURCES_INDEX_PATH,
        "".join(f"{line}\n" for line in (sources_header, *("\t".join(row) for row in joined))),
    )
    rows: list[tuple[str, str, str, str]] = []
    for entry in () if sources is None else sources.document_entries:
        try:
            path = sources.path_for_source(entry.name) if sources is not None else None
        except (OSError, ValueError, PlaybillError):
            continue
        if path is None or not path.is_file() or not path.is_relative_to(root):
            continue
        relative = path.relative_to(root).as_posix()
        document = f"Document:{entry.document_id}"
        if document in generations:
            rows.append((relative, "document-body", document, generations[document]))
        if entry.name in generations:
            rows.append((relative, "evidence-source", entry.name, generations[entry.name]))
        try:
            content = read_projection_source(path)
        except (OSError, ValueError, PlaybillError):
            continue
        rows.extend(
            (relative, "rendered-block", ref, generation)
            for ref, generation in _rendered_blocks(root, content)
        )
    rows = sorted(set(rows), key=lambda row: tuple(cell.encode() for cell in row))
    header = (
        f"# projections INDEX  {len(rows)} bindings  columns: workspace path, role, "
        "bound ref, changed gen  (written from the local workspace)"
    )
    _write_floor_local(
        output_root,
        PROJECTIONS_INDEX_PATH,
        "".join(f"{line}\n" for line in (header, *("\t".join(row) for row in rows))),
    )
    return len(rows)


def floor_export_parts(
    include: Sequence[contracts.PlaybillFloorExportPart],
) -> dict[str, Any]:
    """Keyword arguments naming opt-in parts, empty for the default floor.

    A client that predates opt-in parts is only ever asked for the default.
    """

    return {"include": tuple(sorted(set(include)))} if include else {}


FloorDeltaFetch = Callable[[int | None, str | None], PlaybillFloorDeltaV1]


def sync_floor_directory(
    fetch_delta: FloorDeltaFetch, floor_dir: Path
) -> tuple[PlaybillFloorDeltaV1, PlaybillFloorApplyResultV1]:
    """Bring ``floor_dir`` to the daemon's answer through the one shared apply.

    Sends the generation and renderer of the floor the directory holds, so the
    answer is a delta; a directory that turns out not to hold that base gets
    the whole floor on a second ask.
    """

    local = read_floor_manifest(floor_dir)
    delta = fetch_delta(
        None if local is None else local.generation,
        None if local is None else local.renderer,
    )
    result = apply_floor_delta(floor_dir, delta)
    if result.status == "base_mismatch":
        delta = fetch_delta(None, None)
        result = apply_floor_delta(floor_dir, delta)
    if result.status == "base_mismatch":  # pragma: no cover - a full floor names no base
        raise PlaybillWorkspaceError("a full floor could not be applied")
    return delta, result


class _FloorDeliveryClient(Protocol):
    socket_path: str | None

    def playbill_host_workspace_registration(
        self, instance_id: str
    ) -> contracts.PlaybillHostWorkspaceRegistrationV1: ...

    def deliver_playbill_floor_now(
        self,
        instance_id: str,
        *,
        include: tuple[contracts.PlaybillFloorExportPart, ...] = (),
        at: contracts.PlaybillAcceptedCoordinate | None = None,
    ) -> contracts.PlaybillFloorDeliveryResultV1: ...


def daemon_floor_delivery(
    client: _FloorDeliveryClient,
    instance_id: str,
    workspace: str | Path,
    *,
    include: tuple[contracts.PlaybillFloorExportPart, ...] = (),
    at: contracts.PlaybillAcceptedCoordinate | None = None,
) -> contracts.PlaybillFloorDeliveryResultV1 | None:
    """A local daemon opted into this exact workspace is its floor's only writer."""

    if getattr(client, "socket_path", None) is None:
        return None
    registration = client.playbill_host_workspace_registration(instance_id)
    if not registration.floor_delivery:
        return None
    if registration.workspace_path is None or Path(
        registration.workspace_path
    ).resolve() != _workspace_root(workspace):
        raise PlaybillWorkspaceError("Daemon delivery is bound to another workspace")
    if include or at is not None:
        return client.deliver_playbill_floor_now(instance_id, include=include, at=at)
    return client.deliver_playbill_floor_now(instance_id)


def write_workspace_floor_delta(
    fetch_delta: FloorDeltaFetch,
    *,
    instance_id: str,
    workspace: str | Path,
    force: bool = True,
    server_url: str | None = None,
    server_socket: str | None = None,
    delivery: Callable[[], contracts.PlaybillFloorDeliveryResultV1 | None] | None = None,
) -> tuple[PlaybillFloorDeltaV1, contracts.PlaybillWorkspaceFloorWriteResult]:
    """Write the default floor through the shared apply and record its refresh profile.

    The CLI and MCP write path for a floor without opt-in parts: it asks for a
    delta from the floor already there, so an up-to-date floor costs nothing.
    A non-empty directory that holds no floor is replaced only with ``force``.
    """

    root = _workspace_root(workspace)
    destination = _relative_destination(root, PLAYBILL_FLOOR_PATH)
    if (
        not force
        and destination.is_dir()
        and any(destination.iterdir())
        and read_floor_manifest(destination) is None
    ):
        raise PlaybillWorkspaceError(
            f"refusing to write the floor into a non-empty directory: {destination}"
        )
    delivered = None if delivery is None else delivery()
    if delivered is None:
        delta, applied = sync_floor_directory(fetch_delta, destination)
        write_projection_index(root)
        assert applied.floor_digest is not None
        written = contracts.PlaybillWorkspaceFloorWriteResult(
            status="unchanged" if applied.status == "unchanged" else "written",
            path=PLAYBILL_FLOOR_PATH,
            destination=str(destination),
            floor_digest=applied.floor_digest,
            coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
                delta.head.coordinate().model_dump(mode="json")
            ),
            file_count=applied.file_count + 1,
        )
    else:
        delta, written = delivered.delta, delivered.written
    if (root / _CONFIG_PATH).exists() or server_url is not None or server_socket is not None:
        record_playbill_floor_output(
            workspace,
            instance_id=instance_id,
            server_url=server_url,
            server_socket=server_socket,
        )
    return delta, written


def write_workspace_floor(
    export_floor: Callable[[], contracts.PlaybillFloorExport],
    *,
    instance_id: str,
    workspace: str | Path,
    include: Sequence[contracts.PlaybillFloorExportPart] = (),
    force: bool = True,
    server_url: str | None = None,
    server_socket: str | None = None,
    delivery: Callable[[], contracts.PlaybillFloorDeliveryResultV1 | None] | None = None,
) -> tuple[contracts.PlaybillFloorExport, contracts.PlaybillWorkspaceFloorWriteResult]:
    """Write an exported floor into the workspace and record its refresh profile.

    The one write path every surface (CLI, MCP) takes, so a floor written with
    opt-in parts is refreshed with the same parts after an activation.
    ``export_floor`` exports exactly ``include`` (see ``floor_export_parts``).
    The profile is recorded into an existing coverage config, or a new one
    naming the given transport; with neither, there is no daemon to name and
    nothing is recorded.
    """

    delivered = None if delivery is None else delivery()
    if delivered is None:
        export = export_floor()
        written = materialize_playbill_floor(workspace, export=export, force=force)
    else:
        if delivered.export is None:
            raise PlaybillWorkspaceError("daemon delivery omitted the requested full export")
        export, written = delivered.export, delivered.written
    if (
        (_workspace_root(workspace) / _CONFIG_PATH).exists()
        or server_url is not None
        or server_socket is not None
    ):
        record_playbill_floor_output(
            workspace,
            instance_id=instance_id,
            server_url=server_url,
            server_socket=server_socket,
            include=include,
        )
    return export, written


def inspect_workspace_floor(
    workspace: str | Path,
    *,
    current_coordinate: contracts.PlaybillAcceptedCoordinate | None,
) -> contracts.PlaybillWorkspaceFloorStatus:
    """Compare the installed configured floor with a daemon coordinate."""

    root = _workspace_root(workspace)
    try:
        relative_path = configured_floor_path(root)
    except PlaybillWorkspaceError as exc:
        return contracts.PlaybillWorkspaceFloorStatus(status="invalid", message=str(exc))
    if relative_path is None:
        return contracts.PlaybillWorkspaceFloorStatus(
            status="not_configured", current_coordinate=current_coordinate
        )
    destination = _relative_destination(root, relative_path)
    manifest_path = destination / "manifest.json"
    if not manifest_path.is_file():
        return contracts.PlaybillWorkspaceFloorStatus(
            status="missing",
            path=relative_path,
            destination=str(destination),
            current_coordinate=current_coordinate,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        installed = contracts.PlaybillAcceptedCoordinate.model_validate(manifest["coordinate"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return contracts.PlaybillWorkspaceFloorStatus(
            status="invalid",
            path=relative_path,
            destination=str(destination),
            current_coordinate=current_coordinate,
            message=str(exc),
        )
    status: Literal["current", "stale"]
    if current_coordinate is not None and installed == current_coordinate:
        status = "current"
    else:
        status = "stale"
    return contracts.PlaybillWorkspaceFloorStatus(
        status=status,
        path=relative_path,
        destination=str(destination),
        installed_coordinate=installed,
        current_coordinate=current_coordinate,
    )


def workspace_floor_freshness(
    workspace: str | Path,
    orientation: contracts.PlaybillOrientResultV1,
) -> contracts.PlaybillOrientResultV1:
    """``orientation`` with ``floor`` set when the workspace holds this instance's floor.

    Cheap by construction: it reads the floor's manifest (its coordinate and
    generation), never re-exports, and
    leaves ``orientation`` as it is when there is no readable floor, or when the
    workspace's coverage config names another instance.
    """

    root = _workspace_root(workspace)
    floor = root / PLAYBILL_FLOOR_PATH
    try:
        manifest = json.loads((floor / "manifest.json").read_text(encoding="utf-8"))
        at = manifest["coordinate"]["git_oid"]
        if not isinstance(at, str):
            return orientation
        config = _read_workspace_config(root / _CONFIG_PATH)
    except (OSError, KeyError, TypeError, ValueError):
        return orientation
    if config is not None and config.get("instance_id") not in (None, orientation.instance):
        return orientation
    behind: int | None = None
    if at == orientation.coordinate.git_oid:
        behind = 0
    else:
        generation = manifest.get("generation")
        if isinstance(generation, int) and not isinstance(generation, bool):
            behind = max(0, orientation.generation - generation)
    return orientation.model_copy(
        update={"floor": contracts.PlaybillOrientFloorV1(at=at, generations_behind=behind)}
    )


def observe_playbill_next_workspace(workspace: str | Path) -> dict[str, object]:
    """Observe the configured floor and every resolvable installed catalog source.

    The daemon compares ``installed_coordinate`` with its resolved coordinate.  Therefore
    the local ``stale`` spelling produced without a daemon coordinate is only a transport
    hint; it cannot manufacture a stale or current queue item. Invalid catalogs leave
    sources unobserved; individual unavailable sources are omitted from an otherwise
    valid observation so the daemon can explain each accepted citation separately.
    """

    root = _workspace_root(workspace)
    floor = inspect_workspace_floor(workspace, current_coordinate=None)
    observation: dict[str, object] = {
        "tag": "playbill-next-workspace-observation-v1",
        "floor_status": floor.status,
        "installed_coordinate": (
            None
            if floor.installed_coordinate is None
            else floor.installed_coordinate.model_dump(mode="json")
        ),
        "drift_observations": None,
        "presentation_policy": PlaybillPresentationPolicyV2().model_dump(mode="json"),
        "presentation_policy_notes": [],
    }
    try:
        candidates = (
            root / ".playbill" / "sources.yaml",
            root / "sources.yaml",
        )
        existing = tuple(path for path in candidates if path.is_file())
        if not existing or any(not path.resolve().is_relative_to(root) for path in existing):
            _observe_presentation_policy(observation, root, known_source_ids=())
            return observation
        overlay_path = root / ".playbill" / "sources.local.yaml"
        if overlay_path.is_file() and not overlay_path.resolve().is_relative_to(root):
            return observation
        sources = WorkspaceSources(root)
    except (OSError, ValueError, PlaybillError):
        _observe_presentation_policy(observation, root, known_source_ids=())
        return observation
    _observe_presentation_policy(
        observation,
        root,
        known_source_ids=tuple(entry.name for entry in sources.document_entries),
    )
    source_observations: list[dict[str, str]] = []
    missing: list[dict[str, str | None]] = []
    for entry in sources.document_entries:
        try:
            path = sources.path_for_source(entry.name)
        except (OSError, ValueError, PlaybillError):
            continue
        if not path.exists() and not path.is_symlink():
            # A binding to a file that is not there is repair work, not an
            # unobserved source: next names it with the catalog entry to fix.
            missing.append(
                {
                    "tag": "playbill-next-missing-binding-v1",
                    "source_id": entry.name,
                    "document_id": entry.document_id,
                    "locator": entry.locator,
                }
            )
            continue
        try:
            content = read_projection_source(path)
        except (OSError, ValueError, PlaybillError):
            continue
        source_observations.append(
            {
                "source_id": entry.name,
                "document_id": entry.document_id,
                "observed_source_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    observation["source_observations"] = source_observations
    if missing:
        observation["missing_bindings"] = sorted(
            missing, key=lambda item: str(item["source_id"]).encode("utf-8")
        )
    return observation


def _manifest_observation(root: Path, content: bytes) -> dict[str, object]:
    try:
        manifests = load_projection_manifests(root, content)
    except ProjectionMarkerError:
        return {}
    if not manifests:
        return {}
    return {
        "projection_manifests": {
            key: base64.b64encode(body).decode("ascii") for key, body in manifests.items()
        }
    }


def _projection_marker_observation(
    source_id: str, content: bytes, *, workspace: Path
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    try:
        blocks = parse_projection_blocks(
            content,
            source_id=source_id,
            allow_bootstrap=True,
            manifests=load_projection_manifests(workspace, content),
        )
    except (ProjectionMarkerError, ValueError):
        return [], ("projection_marker_invalid",)
    marker_notes = (
        ("projection_block_unstamped",) if any(block.stamp is None for block in blocks) else ()
    )
    return (
        [
            block.summary().model_dump(mode="json")
            for block in sorted(blocks, key=lambda item: item.block_id.encode("utf-8"))
            if block.stamp is not None
        ],
        marker_notes,
    )


def observe_playbill_projection_coverage(
    workspace: str | Path,
    *,
    coordinate: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any],
) -> dict[str, object] | None:
    """Build bounded, coordinate-bound proof of configured local projections.

    A valid catalog completely describes Procedure projection intent. Claim
    coverage is complete only when every Document source can be read and its
    complete marker set parses. Missing or malformed evidence removes that
    kind from ``complete_kinds`` instead of manufacturing absence.
    """

    root = _workspace_root(workspace)
    try:
        sources = WorkspaceSources(root)
    except (OSError, ValueError, PlaybillError):
        return None

    accepted = contracts.PlaybillAcceptedCoordinate.model_validate(coordinate)
    procedure_bindings: list[PlaybillProjectionCoverageBindingV1] = []
    procedures_complete = True
    for procedure_entry in sources.procedure_projection_entries:
        try:
            sources.path_for_procedure(procedure_entry.procedure_identity.qualified)
        except (OSError, ValueError, PlaybillError):
            procedures_complete = False
            procedure_bindings.clear()
            break
        procedure_bindings.append(
            PlaybillProjectionCoverageBindingV1(
                artifact=procedure_entry.procedure_identity,
                workspace_path=procedure_entry.locator,
                evidence_kind="procedure_catalog",
            )
        )

    claim_bindings: list[PlaybillProjectionCoverageBindingV1] = []
    claims_complete = True
    scanned_bytes = 0
    for document_entry in sources.document_entries:
        try:
            content = read_projection_source(sources.path_for_source(document_entry.name))
        except (OSError, ValueError, PlaybillError):
            claims_complete = False
            break
        scanned_bytes += len(content)
        if scanned_bytes > projection_processing_policy().max_bytes:
            claims_complete = False
            break
        try:
            blocks = parse_projection_blocks(
                content,
                source_id=document_entry.name,
                allow_bootstrap=True,
                manifests=load_projection_manifests(root, content),
            )
        except (ProjectionMarkerError, ValueError):
            claims_complete = False
            break
        if any(block.stamp is None for block in blocks):
            claims_complete = False
            break
        for block in blocks:
            assert block.stamp is not None
            for backing in block.stamp.backing:
                if backing.identity.kind == "Claim":
                    claim_bindings.append(
                        PlaybillProjectionCoverageBindingV1(
                            artifact=backing.identity,
                            workspace_path=document_entry.locator,
                            evidence_kind="claim_marker",
                        )
                    )
    if not claims_complete:
        claim_bindings.clear()

    complete_kinds: list[Literal["Claim", "Procedure"]] = []
    bindings: list[PlaybillProjectionCoverageBindingV1] = []
    if claims_complete and len(claim_bindings) <= MAX_PROJECTION_COVERAGE_BINDINGS:
        complete_kinds.append("Claim")
        bindings.extend(claim_bindings)
    if procedures_complete and (
        len(bindings) + len(procedure_bindings) <= MAX_PROJECTION_COVERAGE_BINDINGS
    ):
        complete_kinds.append("Procedure")
        bindings.extend(procedure_bindings)
    ordered_bindings = tuple(
        sorted(
            set(bindings),
            key=lambda item: (
                item.artifact.qualified.encode("utf-8"),
                item.workspace_path.encode("utf-8"),
                item.evidence_kind.encode("utf-8"),
            ),
        )
    )
    result = PlaybillProjectionCoverageObservationV1(
        coordinate=AcceptedCoordinate.model_validate(accepted.model_dump(mode="json")),
        complete_kinds=tuple(sorted(complete_kinds, key=lambda item: item.encode("utf-8"))),
        bindings=ordered_bindings,
    )
    return result.model_dump(mode="json")


def _coverage_v3_fields(
    span: Mapping[str, Any],
    *,
    source_id: str,
    content: bytes,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    tuple[str, ...],
]:
    notes: set[str] = set()
    if span.get("tag") != "playbill-coverage-span-result-v3":
        return [], [], [], ("coverage_result_version_unsupported",)
    if span.get("health") != "complete":
        notes.add("coverage_" + str(span.get("health", "unavailable")))
    if span.get("ambiguous_occurrence_count", 0):
        notes.add("coverage_occurrence_ambiguous")
    if span.get("omitted_card_count", 0):
        notes.add("coverage_cards_omitted")
    raw_cards = span.get("cards", [])
    cards_clipped = not isinstance(raw_cards, list) or (
        len(raw_cards) > MAX_PROJECTION_CARDS_PER_SOURCE
    )
    cards = raw_cards
    if cards_clipped:
        notes.add("coverage_card_limit_exceeded")
        cards = []

    expected_source = {
        "tag": "playbill-logical-source-identity-v1",
        "plane": "external",
        "identity": source_id,
    }
    proofs: dict[tuple[str, int], dict[str, object]] = {}
    raw_proofs = span.get("commitment_scan_proofs", [])
    if not isinstance(raw_proofs, list) or len(raw_proofs) > MAX_PROJECTION_CARDS_PER_SOURCE:
        notes.add("coverage_proof_limit_exceeded")
        raw_proofs = []
    for proof in raw_proofs:
        if not isinstance(proof, Mapping) or proof.get("source") != expected_source:
            notes.add("coverage_proof_invalid")
            continue
        digest, length = proof.get("commitment_digest"), proof.get("byte_length")
        if (
            proof.get("tag") != "playbill-coverage-commitment-scan-proof-v1"
            or proof.get("complete") is not True
            or not isinstance(digest, str)
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length < 0
        ):
            notes.add("coverage_proof_invalid")
            continue
        try:
            Sha256Value.from_tagged(digest)
        except ValueError:
            notes.add("coverage_proof_invalid")
            continue
        proof_value = dict(proof)
        proof_previous = proofs.setdefault((digest, length), proof_value)
        if proof_previous != proof_value:
            notes.add("coverage_proof_invalid")
    if cards_clipped:
        proofs.clear()

    skipped_occurrences: dict[tuple[str, int], set[str]] = {}
    forced_drops: set[tuple[str, int]] = set()

    def discard_proof_for_skipped_card(
        card: Mapping[str, object] | None,
        *,
        force: bool = False,
    ) -> None:
        """A proof may survive only beside a complete occurrence enumeration."""

        if card is None:
            proofs.clear()
            return
        match_state = card.get("match_state")
        if match_state not in {"exact", "candidate"}:
            return
        digest = card.get("expected_commitment_digest")
        overlay = card.get("line_overlay")
        if not isinstance(digest, str) or not isinstance(overlay, Mapping):
            proofs.clear()
            return
        try:
            Sha256Value.from_tagged(digest)
        except ValueError:
            proofs.clear()
            return
        start, end = overlay.get("start_byte"), overlay.get("end_byte")
        observed_digest = card.get("observed_commitment_digest")
        identity = card.get("occurrence_identity_digest")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start <= end <= len(content)
            or not isinstance(observed_digest, str)
            or observed_digest != "sha256:" + hashlib.sha256(content[start:end]).hexdigest()
            or not isinstance(identity, str)
        ):
            for key in tuple(proofs):
                if key[0] == digest:
                    proofs.pop(key)
            return
        pair = (digest, end - start)
        skipped_occurrences.setdefault(pair, set()).add(identity)
        if force:
            forced_drops.add(pair)

    windows: dict[tuple[str, int, int], dict[str, object]] = {}
    raw_windows = span.get("citation_window_observations", [])
    if not isinstance(raw_windows, list) or len(raw_windows) > MAX_PROJECTION_CARDS_PER_SOURCE:
        notes.add("coverage_window_limit_exceeded")
        raw_windows = []
    for window in raw_windows:
        if not isinstance(window, Mapping) or window.get("source") != expected_source:
            notes.add("coverage_window_invalid")
            continue
        citation_id = window.get("citation_id")
        commitment_digest = window.get("commitment_digest")
        start, end = window.get("original_start"), window.get("original_end")
        addressable = window.get("addressable")
        observed_digest = window.get("observed_window_digest")
        if (
            window.get("tag") != "playbill-citation-window-observation-v1"
            or not isinstance(citation_id, str)
            or not isinstance(commitment_digest, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(addressable, bool)
            or not 0 <= start <= end
        ):
            notes.add("coverage_window_invalid")
            continue
        try:
            Sha256Value.from_tagged(citation_id)
            Sha256Value.from_tagged(commitment_digest)
        except ValueError:
            notes.add("coverage_window_invalid")
            continue
        if addressable:
            expected_digest = (
                "sha256:" + hashlib.sha256(content[start:end]).hexdigest()
                if end <= len(content)
                else None
            )
            if observed_digest != expected_digest:
                notes.add("coverage_window_invalid")
                continue
        elif observed_digest is not None:
            notes.add("coverage_window_invalid")
            continue
        value = dict(window)
        key = (citation_id, start, end)
        window_previous = windows.setdefault(key, value)
        if window_previous != value:
            notes.add("coverage_window_invalid")

    occurrences: dict[str, dict[str, object]] = {}
    for card in cards:
        if not isinstance(card, Mapping):
            notes.add("coverage_card_invalid")
            discard_proof_for_skipped_card(None)
            continue
        observed_source = card.get("observed_source")
        accepted_source = card.get("accepted_source")
        if observed_source != expected_source:
            notes.add("coverage_source_mismatch")
            discard_proof_for_skipped_card(card)
            continue
        if accepted_source != expected_source:
            notes.add("coverage_source_mismatch")
            discard_proof_for_skipped_card(card)
            continue
        expected_digest = card.get("expected_commitment_digest")
        if not isinstance(expected_digest, str):
            notes.add("coverage_card_invalid")
            discard_proof_for_skipped_card(card)
            continue
        try:
            Sha256Value.from_tagged(expected_digest)
        except ValueError:
            notes.add("coverage_card_invalid")
            discard_proof_for_skipped_card(card)
            continue
        if card.get("match_state") not in {"exact", "candidate"}:
            continue
        overlay = card.get("line_overlay")
        observed_digest = card.get("observed_commitment_digest")
        identity = card.get("occurrence_identity_digest")
        if (
            not isinstance(overlay, Mapping)
            or not isinstance(observed_digest, str)
            or not isinstance(identity, str)
        ):
            notes.add("coverage_occurrence_invalid")
            discard_proof_for_skipped_card(card)
            continue
        start, end = overlay.get("start_byte"), overlay.get("end_byte")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start <= end <= len(content)
            or observed_digest != "sha256:" + hashlib.sha256(content[start:end]).hexdigest()
        ):
            notes.add("coverage_occurrence_invalid")
            discard_proof_for_skipped_card(card)
            continue
        ordinal = next(
            (
                candidate
                for candidate in range(max(len(cards), 1))
                if typed_digest(
                    Sha256Value,
                    "playbill-coverage-occurrence-identity-v1",
                    {
                        "source": expected_source,
                        "observed_commitment_digest": observed_digest,
                        "ordinal": candidate,
                    },
                ).tagged
                == identity
            ),
            None,
        )
        if ordinal is None:
            notes.add("coverage_occurrence_ambiguous")
            discard_proof_for_skipped_card(card)
            continue
        if (observed_digest, end - start) not in proofs:
            notes.add(
                "coverage_occurrence_unverified"
                if card.get("match_state") == "candidate"
                else "coverage_occurrence_unproved"
            )
            continue
        occurrence: dict[str, object] = {
            "tag": "playbill-coverage-working-occurrence-v1",
            "source": expected_source,
            "observed_commitment_digest": observed_digest,
            "byte_length": end - start,
            "ordinal": ordinal,
            "identity_digest": identity,
            "line_overlay": dict(overlay),
        }
        occurrence_previous = occurrences.get(identity)
        if occurrence_previous is not None and occurrence_previous != occurrence:
            notes.add("coverage_occurrence_ambiguous")
            discard_proof_for_skipped_card(card, force=True)
            continue
        occurrences[identity] = occurrence
    for pair, identities in skipped_occurrences.items():
        if pair in forced_drops or not identities.issubset(occurrences):
            proofs.pop(pair, None)
    occurrences = {
        identity: occurrence
        for identity, occurrence in occurrences.items()
        if (
            cast(str, occurrence["observed_commitment_digest"]),
            cast(int, occurrence["byte_length"]),
        )
        in proofs
    }
    return (
        sorted(
            occurrences.values(),
            key=lambda item: (
                str(item["source"]).encode("utf-8"),
                str(item["observed_commitment_digest"]).encode("ascii"),
                cast(int, item["ordinal"]),
            ),
        ),
        [proofs[key] for key in sorted(proofs)],
        [windows[key] for key in sorted(windows)],
        tuple(sorted(notes, key=lambda item: item.encode("utf-8"))),
    )


def observe_playbill_next_workspace_with_coverage(
    client: _CoverageClient,
    instance_id: str,
    workspace: str | Path,
    *,
    observation: Mapping[str, object] | None = None,
    coordinate: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    access_profile: Mapping[str, Any] | None = None,
    resolve_coordinate: Callable[[], contracts.PlaybillAcceptedCoordinate] | None = None,
) -> tuple[dict[str, object], contracts.PlaybillAcceptedCoordinate | None]:
    """Enrich next with one existing, coordinate-bound coverage-scanner read.

    This adapter never searches source bytes. Every accepted occurrence comes
    from the existing server coverage card; the local slice check only verifies
    that card against the exact bytes it previously sent to the sole scanner.
    """

    base = dict(observation or observe_playbill_next_workspace(workspace))
    entries = base.get("source_observations")
    if not isinstance(entries, list) or not entries:
        resolved_coordinate: contracts.PlaybillAcceptedCoordinate | None = None
        try:
            local_sources = WorkspaceSources(_workspace_root(workspace))
        except (OSError, ValueError, PlaybillError):
            local_sources = None
        if local_sources is not None and local_sources.procedure_projection_entries:
            if coordinate is not None:
                resolved_coordinate = contracts.PlaybillAcceptedCoordinate.model_validate(
                    coordinate
                )
            else:
                resolved_coordinate = (
                    resolve_coordinate()
                    if resolve_coordinate is not None
                    else contracts.PlaybillAcceptedCoordinate.model_validate(
                        client.playbill_head(instance_id).coordinate.model_dump(mode="json")
                    )
                )
        if resolved_coordinate is not None:
            projection = observe_playbill_projection_coverage(
                workspace,
                coordinate=resolved_coordinate,
            )
            if projection is not None:
                base["projection_coverage"] = projection
        return base, resolved_coordinate

    root = _workspace_root(workspace)
    try:
        sources = WorkspaceSources(root)
    except (OSError, ValueError, PlaybillError):
        base.pop("source_observations", None)
        return base, None

    material: dict[str, bytes] = {}
    document_ids: dict[str, str | None] = {}
    payloads: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("source_id"), str):
            continue
        source_id = entry["source_id"]
        try:
            content = read_projection_source(sources.path_for_source(source_id))
        except (OSError, ValueError, PlaybillError):
            continue
        material[source_id] = content
        document_id = entry.get("document_id")
        document_ids[source_id] = document_id if isinstance(document_id, str) else None
        payloads.append(
            {
                "tag": "playbill-coverage-working-source-observation-v1",
                "source": {
                    "tag": "playbill-logical-source-identity-v1",
                    "plane": "external",
                    "identity": source_id,
                },
                "content_base64": base64.b64encode(content).decode("ascii"),
                **_manifest_observation(root, content),
                "content_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "byte_length": len(content),
                "selections": [],
            }
        )

    if not payloads:
        base["source_observations"] = []
        return base, None

    coverage = client.resolve_playbill_coverage(
        instance_id,
        observations=payloads,
        at=coordinate,
        budget={
            "tag": "playbill-coverage-card-budget-v1",
            "max_cards_per_span": MAX_PROJECTION_CARDS_PER_SOURCE,
            "max_candidate_cards_per_span": MAX_PROJECTION_CARDS_PER_SOURCE,
        },
        scan_budget={
            "tag": "playbill-coverage-scan-budget-v1",
            "max_scanned_bytes": projection_processing_policy().max_bytes,
        },
    )
    returned_at = coverage.coordinate.model_dump(mode="json")
    expected_at = (
        coordinate.model_dump(mode="json")
        if isinstance(coordinate, contracts.PlaybillAcceptedCoordinate)
        else dict(coordinate)
        if coordinate is not None
        else None
    )
    coordinate_matches = coverage.result.get("at") == returned_at and (
        expected_at is None or expected_at == returned_at
    )
    returned_profile = coverage.result.get("access_profile")
    profile_matches = isinstance(returned_profile, Mapping) and (
        access_profile is None
        or (
            returned_profile.get("permitted_access_classes")
            == access_profile.get("permitted_access_classes")
            and returned_profile.get("disclose_restricted_existence")
            == access_profile.get("disclose_restricted_existence")
        )
    )
    spans = coverage.result.get("spans", [])
    by_source: dict[str, list[Mapping[str, Any]]] = {}
    if isinstance(spans, list):
        for span in spans:
            if not isinstance(span, Mapping):
                continue
            request = span.get("request")
            source = request.get("source") if isinstance(request, Mapping) else None
            if isinstance(source, Mapping) and isinstance(source.get("identity"), str):
                by_source.setdefault(source["identity"], []).append(span)

    enriched: dict[str, dict[str, object]] = {}
    for source_id, content in material.items():
        markers, marker_notes = _projection_marker_observation(source_id, content, workspace=root)
        notes: list[str] = []
        if not coordinate_matches:
            notes.append("coverage_coordinate_mismatch")
        if not profile_matches:
            notes.append("coverage_access_mismatch")
        candidates = by_source.get(source_id, [])
        if len(candidates) != 1:
            notes.append("coverage_span_missing" if not candidates else "coverage_span_ambiguous")
        occurrences: list[dict[str, object]] = []
        proofs: list[dict[str, object]] = []
        windows: list[dict[str, object]] = []
        if not notes:
            occurrences, proofs, windows, scan_notes = _coverage_v3_fields(
                candidates[0], source_id=source_id, content=content
            )
            notes.extend(scan_notes)
        enriched[source_id] = {
            "tag": "playbill-next-source-observation-v4",
            "source_id": source_id,
            "document_id": document_ids[source_id],
            "observed_source_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "byte_length": len(content),
            "marker_summaries": markers,
            "occurrences": occurrences,
            "commitment_scan_proofs": proofs,
            "citation_window_observations": windows,
            "scan_notes": sorted(set(notes), key=lambda item: item.encode("utf-8")),
            "marker_notes": list(marker_notes),
        }
    base["source_observations"] = [
        enriched[source_id] for source_id in sorted(enriched, key=lambda item: item.encode("utf-8"))
    ]
    resolved_coordinate = coverage.coordinate if coordinate_matches else None
    if resolved_coordinate is not None:
        projection = observe_playbill_projection_coverage(
            workspace,
            coordinate=resolved_coordinate,
        )
        if projection is not None:
            base["projection_coverage"] = projection
    return base, resolved_coordinate


def refresh_workspace_floor(
    client: _FloorClient,
    instance_id: str,
    *,
    workspace: str | Path,
    at: contracts.PlaybillAcceptedCoordinate | None = None,
) -> contracts.PlaybillFloorRefreshResult:
    """Refresh only the local floor and report the coordinate actually written.

    A pinned request refuses a mismatched export before touching local files.
    inspect_workspace_floor reports the installed coordinate independently,
    including after a failed refresh. No projection prose or declaration changes.
    """

    try:
        configured = configured_floor_output(workspace)
        if configured is None:
            return contracts.PlaybillFloorRefreshResult(status="not_configured")
        relative_path, include = configured
        if getattr(client, "socket_path", None) is not None:
            delivered = daemon_floor_delivery(
                cast(_FloorDeliveryClient, client),
                instance_id,
                workspace,
                include=tuple(include),
                at=at,
            )
            if delivered is not None:
                written = delivered.written
                return contracts.PlaybillFloorRefreshResult(
                    status="refreshed",
                    path=relative_path,
                    destination=written.destination,
                    floor_digest=written.floor_digest,
                    coordinate=written.coordinate,
                )
        if include:
            # Opt-in discovery cards are a full export's; they never travel in a delta.
            export = client.export_playbill_floor(instance_id, at=at, **floor_export_parts(include))
            if at is not None and export.coordinate != at:
                raise PlaybillWorkspaceError("floor export differs from requested coordinate")
            written = materialize_playbill_floor(workspace, export=export)
            return contracts.PlaybillFloorRefreshResult(
                status="refreshed",
                path=relative_path,
                destination=written.destination,
                floor_digest=written.floor_digest,
                coordinate=export.coordinate,
            )
        root = _workspace_root(workspace)
        destination = _relative_destination(root, relative_path)

        def fetch(generation: int | None, renderer: str | None) -> PlaybillFloorDeltaV1:
            delta = client.playbill_floor_delta(
                instance_id, at=at, base_generation=generation, base_renderer=renderer
            )
            # A pinned request refuses a mismatched answer before anything is written:
            # the whole coordinate, not only its Git OID.
            if at is not None and delta.head.coordinate().model_dump(mode="json") != at.model_dump(
                mode="json"
            ):
                raise PlaybillWorkspaceError("floor delta differs from requested coordinate")
            return delta

        delta, applied = sync_floor_directory(fetch, destination)
        write_projection_index(root)
        return contracts.PlaybillFloorRefreshResult(
            status="refreshed",
            path=relative_path,
            destination=str(destination),
            floor_digest=applied.floor_digest,
            coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
                delta.head.coordinate().model_dump(mode="json")
            ),
        )
    except Exception as exc:
        return contracts.PlaybillFloorRefreshResult(status="failed", message=str(exc))


def activate_with_workspace_refresh(
    client: _FloorClient,
    instance_id: str,
    proposal_id: str,
    *,
    workspace: str | Path,
    sync: bool = True,
) -> contracts.PlaybillWorkspaceActivationResult:
    """Activate once, refresh the floor, then independently sync local blocks."""

    activation = client.activate_playbill_proposal(instance_id, proposal_id)
    refresh = refresh_workspace_floor(
        client,
        instance_id,
        workspace=workspace,
        at=activation.accepted_coordinate,
    )
    block_sync = None
    if sync and activation.status == "accepted":
        try:
            block_sync = sync_projection_blocks(
                cast(Any, client),
                instance_id,
                workspace=workspace,
                all_sources=True,
            )
            if block_sync.items and all(
                item.reason == "workspace_not_attached" for item in block_sync.items
            ):
                skipped = tuple(
                    contracts.PlaybillBlockSyncItemV1.model_validate(
                        {**item.model_dump(mode="json"), "outcome": "skipped"}
                    )
                    for item in block_sync.items
                )
                block_sync = contracts.PlaybillBlockSyncResultV1(
                    items=skipped,
                    changed_file_count=0,
                    would_change=False,
                    has_refusals=False,
                )
        except Exception as exc:  # report activation and sync truth together
            block_sync = contracts.PlaybillBlockSyncResultV1(
                items=(
                    contracts.PlaybillBlockSyncItemV1(
                        path=".",
                        outcome="refused",
                        reason="block_sync_failed",
                        detail={"message": str(exc)},
                    ),
                ),
                changed_file_count=0,
                would_change=False,
                has_refusals=True,
            )
    return contracts.PlaybillWorkspaceActivationResult(
        **activation.model_dump(mode="json"),
        floor_refresh=refresh,
        block_sync=block_sync,
    )


__all__ = [
    "PlaybillWorkspaceAttachmentError",
    "PlaybillWorkspaceError",
    "activate_with_workspace_refresh",
    "configured_floor_path",
    "inspect_workspace_floor",
    "observe_playbill_next_workspace",
    "observe_playbill_next_workspace_with_coverage",
    "observe_playbill_projection_coverage",
    "materialize_playbill_floor",
    "configured_floor_output",
    "floor_export_parts",
    "record_playbill_floor_output",
    "sync_floor_directory",
    "daemon_floor_delivery",
    "write_workspace_floor",
    "write_workspace_floor_delta",
    "refresh_workspace_floor",
    "validate_playbill_workspace_config_write",
    "verified_floor_files",
    "write_playbill_workspace_config",
    "write_projection_index",
]

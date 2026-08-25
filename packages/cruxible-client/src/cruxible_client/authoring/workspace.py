"""Client-owned Playbill floor verification and workspace replacement.

The daemon returns inert bytes. This module is the shared CLI/MCP adapter that
verifies those bytes and writes them locally without ever sending a path to the
daemon.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import shutil
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from cruxible_client import contracts
from cruxible_client.authoring.blocks import ProjectionMarkerError, parse_projection_blocks
from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_CARDS_PER_SOURCE,
    MAX_PROJECTION_SCAN_BYTES,
    MAX_PROJECTION_SOURCE_BYTES,
    PlaybillPresentationPolicyV1,
)
from cruxible_client.contracts.errors import PlaybillError

_CONFIG_PATH = PurePosixPath(".playbill/coverage.json")
_FLOOR_DOMAIN = "playbill-floor-export-v2"


class PlaybillWorkspaceError(ValueError):
    """A client workspace or exported floor failed deterministic validation."""


def _presentation_policy(
    root: Path,
    *,
    known_source_ids: Sequence[str],
) -> PlaybillPresentationPolicyV1:
    path = root / ".playbill" / "presentation-policy.json"
    if not path.exists():
        return PlaybillPresentationPolicyV1()
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise PlaybillWorkspaceError("presentation policy escapes the workspace")
        raw = json.loads(path.read_text(encoding="utf-8"))
        policy = PlaybillPresentationPolicyV1.model_validate(raw)
    except PlaybillWorkspaceError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PlaybillWorkspaceError(f"presentation policy is invalid: {exc}") from exc
    unknown = tuple(
        source_id
        for source_id in policy.archival_source_ids
        if source_id not in set(known_source_ids)
    )
    if unknown:
        raise PlaybillWorkspaceError(
            "presentation policy names unknown source IDs: " + ", ".join(unknown)
        )
    return policy


class _FloorClient(Protocol):
    def activate_playbill_proposal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillActivationReceipt: ...

    def export_playbill_floor(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillFloorExport: ...


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

    if export.tag != _FLOOR_DOMAIN:
        raise PlaybillWorkspaceError("configured floor refresh requires playbill-floor-export-v2")
    manifest = export.manifest
    if manifest.get("tag") != "playbill-floor-manifest-v2":
        raise PlaybillWorkspaceError("floor export manifest has an unsupported tag")
    if manifest.get("format") != _FLOOR_DOMAIN:
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
    expected_floor_digest = _typed_digest(_FLOOR_DOMAIN, {"files": inventory})
    if manifest.get("floor_digest") != expected_floor_digest:
        raise PlaybillWorkspaceError("floor export root digest differs from its inventory")
    return decoded


def _workspace_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve()


def _relative_destination(workspace: Path, relative_path: str) -> Path:
    path = PurePosixPath(relative_path)
    if (
        not relative_path
        or path.is_absolute()
        or path.as_posix() != relative_path
        or ".." in path.parts
        or not path.parts
        or path.parts[0] == ".playbill"
    ):
        raise PlaybillWorkspaceError(
            "floor output path must be a normalized workspace-relative directory"
        )
    destination = workspace / relative_path
    try:
        resolved = destination.resolve()
    except OSError as exc:
        raise PlaybillWorkspaceError(f"could not resolve configured floor output: {exc}") from exc
    if not resolved.is_relative_to(workspace):
        raise PlaybillWorkspaceError("configured floor output escapes the workspace root")
    return destination


def configured_floor_path(workspace: str | Path) -> str | None:
    """Return the declared v2 floor path, or ``None`` when absent/unconfigured."""

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
    if output.get("tag") != "playbill-floor-output-v1" or output.get("format") != _FLOOR_DOMAIN:
        raise PlaybillWorkspaceError("coverage floor_output has an unsupported profile")
    value = output.get("path")
    if not isinstance(value, str):
        raise PlaybillWorkspaceError("coverage floor_output path is not text")
    _relative_destination(root, value)
    return value


def _replace_exact(destination: Path, files: Mapping[str, bytes], *, root: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.parent.resolve().is_relative_to(root):
        raise PlaybillWorkspaceError("configured floor output parent escapes the workspace root")
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
    relative_path: str,
    export: contracts.PlaybillFloorExport,
    force: bool = True,
) -> contracts.PlaybillWorkspaceFloorWriteResult:
    """Verify and exactly replace one workspace-relative floor directory."""

    root = _workspace_root(workspace)
    destination = _relative_destination(root, relative_path)
    if destination.exists() and any(destination.iterdir()) and not force:
        raise PlaybillWorkspaceError(
            f"refusing to write the floor into a non-empty directory: {destination}"
        )
    files = verified_floor_files(export)
    _replace_exact(destination, files, root=root)
    return contracts.PlaybillWorkspaceFloorWriteResult(
        path=relative_path,
        destination=str(destination),
        floor_digest=str(export.manifest["floor_digest"]),
        coordinate=export.coordinate,
        file_count=len(export.files),
    )


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
        "presentation_policy": PlaybillPresentationPolicyV1().model_dump(mode="json"),
    }
    try:
        candidates = (
            root / ".playbill" / "sources.yaml",
            root / "sources.yaml",
        )
        existing = tuple(path for path in candidates if path.is_file())
        if not existing or any(not path.resolve().is_relative_to(root) for path in existing):
            observation["presentation_policy"] = _presentation_policy(
                root, known_source_ids=()
            ).model_dump(mode="json")
            return observation
        overlay_path = root / ".playbill" / "sources.local.yaml"
        if overlay_path.is_file() and not overlay_path.resolve().is_relative_to(root):
            return observation
        sources = WorkspaceSources(root)
    except (OSError, ValueError, PlaybillError):
        observation["presentation_policy"] = _presentation_policy(
            root, known_source_ids=()
        ).model_dump(mode="json")
        return observation
    observation["presentation_policy"] = _presentation_policy(
        root,
        known_source_ids=tuple(entry.name for entry in sources.catalog.entries),
    ).model_dump(mode="json")
    source_observations: list[dict[str, str]] = []
    for entry in sources.catalog.entries:
        try:
            path = sources.path_for_source(entry.name)
            content = path.read_bytes()
        except (OSError, ValueError):
            continue
        source_observations.append(
            {
                "source_id": entry.name,
                "observed_source_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    observation["source_observations"] = source_observations
    return observation


def _unobserved_projection_source(
    source_id: str,
    *,
    content: bytes,
    scan_notes: Sequence[str],
    marker_summaries: Sequence[dict[str, object]] = (),
    marker_notes: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "tag": "playbill-next-source-observation-v2",
        "source_id": source_id,
        "observed_source_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "byte_length": len(content),
        "marker_summaries": list(marker_summaries),
        "occurrences": [],
        "scanned_commitment_digests": [],
        "scan_complete": False,
        "scan_notes": sorted(set(scan_notes), key=lambda item: item.encode("utf-8")),
        "marker_notes": sorted(set(marker_notes), key=lambda item: item.encode("utf-8")),
    }


def _projection_marker_observation(
    source_id: str, content: bytes
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    try:
        blocks = parse_projection_blocks(content, source_id=source_id, allow_bootstrap=True)
    except (ProjectionMarkerError, ValueError):
        return [], ("projection_marker_invalid",)
    if any(block.stamp is None for block in blocks):
        return [], ("projection_block_unstamped",)
    return (
        [
            block.summary().model_dump(mode="json")
            for block in sorted(blocks, key=lambda item: item.block_id.encode("utf-8"))
        ],
        (),
    )


def _coverage_occurrences(
    span: Mapping[str, Any], *, source_id: str, content: bytes
) -> tuple[list[dict[str, object]], list[str], tuple[str, ...]]:
    notes: set[str] = set()
    if span.get("health") != "complete":
        notes.add("coverage_" + str(span.get("health", "unavailable")))
    if span.get("ambiguous_occurrence_count", 0):
        notes.add("coverage_occurrence_ambiguous")
    if span.get("omitted_card_count", 0):
        notes.add("coverage_cards_omitted")
    cards = span.get("cards", [])
    if not isinstance(cards, list) or len(cards) > MAX_PROJECTION_CARDS_PER_SOURCE:
        notes.add("coverage_card_limit_exceeded")
        return [], [], tuple(sorted(notes))

    occurrences: dict[str, dict[str, object]] = {}
    scanned: set[str] = set()
    expected_source = {
        "tag": "playbill-logical-source-identity-v1",
        "plane": "external",
        "identity": source_id,
    }
    for card in cards:
        if not isinstance(card, Mapping):
            notes.add("coverage_card_invalid")
            continue
        observed_source = card.get("observed_source")
        accepted_source = card.get("accepted_source")
        if observed_source != expected_source:
            notes.add("coverage_source_mismatch")
            continue
        if card.get("match_state") == "candidate":
            if accepted_source == expected_source:
                notes.add("coverage_occurrence_unverified")
            continue
        if accepted_source != expected_source:
            notes.add("coverage_source_mismatch")
            continue
        expected_digest = card.get("expected_commitment_digest")
        if not isinstance(expected_digest, str):
            notes.add("coverage_card_invalid")
            continue
        try:
            Sha256Value.from_tagged(expected_digest)
        except ValueError:
            notes.add("coverage_card_invalid")
            continue
        scanned.add(expected_digest)
        if card.get("match_state") != "exact":
            continue
        overlay = card.get("line_overlay")
        observed_digest = card.get("observed_commitment_digest")
        identity = card.get("occurrence_identity_digest")
        if not isinstance(overlay, Mapping) or not isinstance(observed_digest, str):
            notes.add("coverage_occurrence_invalid")
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
            continue
        expected_identity = typed_digest(
            Sha256Value,
            "playbill-coverage-occurrence-identity-v1",
            {
                "source": expected_source,
                "observed_commitment_digest": observed_digest,
                "ordinal": 0,
            },
        ).tagged
        if identity != expected_identity:
            notes.add("coverage_occurrence_ambiguous")
            continue
        occurrence: dict[str, object] = {
            "tag": "playbill-coverage-working-occurrence-v1",
            "source": expected_source,
            "observed_commitment_digest": observed_digest,
            "byte_length": end - start,
            "ordinal": 0,
            "identity_digest": identity,
            "line_overlay": dict(overlay),
        }
        previous = occurrences.get(identity)
        if previous is not None and previous != occurrence:
            notes.add("coverage_occurrence_ambiguous")
            continue
        occurrences[identity] = occurrence
    if notes:
        return [], [], tuple(sorted(notes, key=lambda item: item.encode("utf-8")))
    return (
        sorted(
            occurrences.values(),
            key=lambda item: str(item["observed_commitment_digest"]).encode("ascii"),
        ),
        sorted(scanned, key=lambda item: item.encode("ascii")),
        (),
    )


def observe_playbill_next_workspace_with_coverage(
    client: _CoverageClient,
    instance_id: str,
    workspace: str | Path,
    *,
    observation: Mapping[str, object] | None = None,
    coordinate: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    access_profile: Mapping[str, Any] | None = None,
) -> tuple[dict[str, object], contracts.PlaybillAcceptedCoordinate | None]:
    """Enrich next with one existing, coordinate-bound coverage-scanner read.

    This adapter never searches source bytes. Every accepted occurrence comes
    from the existing server coverage card; the local slice check only verifies
    that card against the exact bytes it previously sent to the sole scanner.
    """

    base = dict(observation or observe_playbill_next_workspace(workspace))
    entries = base.get("source_observations")
    if not isinstance(entries, list) or not entries:
        return base, None

    root = _workspace_root(workspace)
    try:
        sources = WorkspaceSources(root)
    except (OSError, ValueError, PlaybillError):
        base.pop("source_observations", None)
        return base, None

    material: dict[str, bytes] = {}
    payloads: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("source_id"), str):
            continue
        source_id = entry["source_id"]
        try:
            content = sources.path_for_source(source_id).read_bytes()
        except (OSError, ValueError, PlaybillError):
            continue
        if len(content) > MAX_PROJECTION_SOURCE_BYTES:
            # The nested contract refuses oversized sources; omission truthfully
            # leaves every citation to this logical source explicitly unobserved.
            continue
        material[source_id] = content
        payloads.append(
            {
                "tag": "playbill-coverage-working-source-observation-v1",
                "source": {
                    "tag": "playbill-logical-source-identity-v1",
                    "plane": "external",
                    "identity": source_id,
                },
                "content_base64": base64.b64encode(content).decode("ascii"),
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
            "max_scanned_bytes": MAX_PROJECTION_SCAN_BYTES,
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
        markers, marker_notes = _projection_marker_observation(source_id, content)
        notes: list[str] = []
        if not coordinate_matches:
            notes.append("coverage_coordinate_mismatch")
        if not profile_matches:
            notes.append("coverage_access_mismatch")
        candidates = by_source.get(source_id, [])
        if len(candidates) != 1:
            notes.append("coverage_span_missing" if not candidates else "coverage_span_ambiguous")
        occurrences: list[dict[str, object]] = []
        scanned: list[str] = []
        if not notes:
            occurrences, scanned, scan_notes = _coverage_occurrences(
                candidates[0], source_id=source_id, content=content
            )
            notes.extend(scan_notes)
        if notes:
            enriched[source_id] = _unobserved_projection_source(
                source_id,
                content=content,
                scan_notes=notes,
                marker_summaries=markers,
                marker_notes=marker_notes,
            )
            continue
        enriched[source_id] = {
            "tag": "playbill-next-source-observation-v2",
            "source_id": source_id,
            "observed_source_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "byte_length": len(content),
            "marker_summaries": markers,
            "occurrences": occurrences,
            "scanned_commitment_digests": scanned,
            "scan_complete": True,
            "scan_notes": [],
            "marker_notes": list(marker_notes),
        }
    base["source_observations"] = [
        enriched[source_id] for source_id in sorted(enriched, key=lambda item: item.encode("utf-8"))
    ]
    return base, coverage.coordinate if coordinate_matches else None


def activate_with_workspace_refresh(
    client: _FloorClient,
    instance_id: str,
    proposal_id: str,
    *,
    workspace: str | Path,
) -> contracts.PlaybillWorkspaceActivationResult:
    """Activate once, then independently refresh the current configured floor."""

    activation = client.activate_playbill_proposal(instance_id, proposal_id)
    try:
        relative_path = configured_floor_path(workspace)
        if relative_path is None:
            refresh = contracts.PlaybillFloorRefreshResult(status="not_configured")
        else:
            export = client.export_playbill_floor(instance_id)
            written = materialize_playbill_floor(
                workspace,
                relative_path=relative_path,
                export=export,
            )
            refresh = contracts.PlaybillFloorRefreshResult(
                status="refreshed",
                path=relative_path,
                destination=written.destination,
                floor_digest=written.floor_digest,
            )
    except Exception as exc:  # report activation and refresh truth together
        refresh = contracts.PlaybillFloorRefreshResult(status="failed", message=str(exc))
    return contracts.PlaybillWorkspaceActivationResult(
        **activation.model_dump(mode="json"),
        floor_refresh=refresh,
    )


__all__ = [
    "PlaybillWorkspaceError",
    "activate_with_workspace_refresh",
    "configured_floor_path",
    "inspect_workspace_floor",
    "observe_playbill_next_workspace",
    "materialize_playbill_floor",
    "verified_floor_files",
]

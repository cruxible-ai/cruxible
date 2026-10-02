"""Floor fires warm a derived index and optionally deliver its coordinate-pure tree.

The journal supplies every refresh signal; this kind owns no clock. One flight
per instance folds all pending fires into the current head. Delivery is confined
to the registered workspace's floor and holds ``FLOOR_ADMISSION``, the one floor
admission per instance that deliver-now, the export and delta routes and
attachment changes share, so no two floor writers of an instance overlap.
The retained outcomes and cursor are derived bookkeeping, never floor authority.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from cruxible_client import contracts
from cruxible_client.authoring.workspace import (
    PlaybillWorkspaceError,
    configured_floor_output,
    sync_floor_directory,
    write_projection_index,
)
from cruxible_client.contracts.floor import (
    PlaybillFloorHeadV1,
    PlaybillFloorManifestV5,
    floor_manifest_digest,
    seal_floor_delta,
)
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_core.consumers.protocol import (
    ConsumerHealth,
    ConsumerRepair,
    ConsumerWork,
    CursorPolicy,
    EffectClass,
)
from cruxible_core.consumers.state import DisposableState
from cruxible_core.errors import RequestRefusedError
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.admission import FLOOR_ADMISSION
from cruxible_core.server.registry import InstanceRecord, get_registry
from cruxible_core.service.floor.floor import service_export_playbill_floor
from cruxible_core.service.floor.floor_delta import service_playbill_floor_delta
from cruxible_core.service.floor.floor_index import advance_floor_index
from cruxible_core.triggers.journal import internal_triggers, latest_sequence

_STATE = DisposableState(
    "floor",
    "CREATE TABLE progress (id INTEGER PRIMARY KEY CHECK(id=1), covered INTEGER NOT NULL, "
    "target TEXT NOT NULL, completed TEXT, failed TEXT) STRICT;"
    "CREATE TABLE outcomes (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL) STRICT;",
)


def _target(instance: Any, record: InstanceRecord | None, head: Any = None) -> str:
    return json.dumps(
        [
            (instance.accepted_coordinate() if head is None else head).generation_root,
            None if record is None else record.workspace_root,
            False if record is None else record.floor_delivery,
        ],
        separators=(",", ":"),
    )


def _floor_directory(workspace: str) -> tuple[Path, Path]:
    registered = Path(workspace)
    root = registered.resolve(strict=True)
    if registered.is_symlink() or str(registered.absolute()).casefold() != str(root).casefold():
        raise PlaybillWorkspaceError("floor delivery refuses a symlinked workspace root")
    floor = root / ".playbill" / "floor"
    # Check the real on-disk spellings too: a differently cased symlink is
    # still the same path on the local case-insensitive filesystem.
    parent = root
    for name in (".playbill", "floor"):
        matches = (
            []
            if not parent.exists()
            else [child for child in parent.iterdir() if child.name.casefold() == name.casefold()]
        )
        if any(child.is_symlink() for child in matches):
            raise PlaybillWorkspaceError("floor delivery refuses symlinked workspace components")
        parent = parent / name
    resolved = floor.resolve(strict=False)
    if not resolved.is_relative_to(root) or resolved == root:
        raise PlaybillWorkspaceError("floor delivery escapes the registered workspace")
    return root, floor


def _post_apply_joins(workspace: Path) -> None:
    """The workspace joins the client writes after it applies a floor, in its order.

    ``write_projection_index`` joins the workspace's source catalogs into
    ``sources/INDEX`` (the daemon's ``sources/LEDGER`` with workspace locators)
    and binds workspace files into ``projections/INDEX``. Both stay outside the
    coordinate-pure manifest, so a daemon-delivered floor and a client-applied
    one leave the same local files.
    """

    write_projection_index(workspace)


def _progress(instance: Any) -> tuple[int, str, str | None, str | None] | None:
    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return None
        return cast(
            tuple[int, str, str | None, str | None] | None,
            connection.execute("SELECT covered,target,completed,failed FROM progress").fetchone(),
        )


def floor_outcomes(instance: Any) -> tuple[contracts.PlaybillFloorConsumerOutcomeV1, ...]:
    """The latest refresh outcome; health needs no per-generation history."""

    with _STATE.open(instance, create=False) as connection:
        if connection is None:
            return ()
        return tuple(
            contracts.PlaybillFloorConsumerOutcomeV1.model_validate_json(payload)
            for (payload,) in connection.execute(
                "SELECT payload FROM outcomes ORDER BY sequence DESC LIMIT 1"
            )
        )


def _included_floor(
    instance: Any, head: Any, include: tuple[contracts.PlaybillFloorExportPart, ...]
) -> tuple[Any, contracts.PlaybillFloorExport]:
    """Opt-in cards use the existing coordinate-pure full export and shared apply."""

    files = service_export_playbill_floor(
        instance, at=AcceptedCoordinate.from_internal(head), include=include
    )
    manifest = PlaybillFloorManifestV5.model_validate_json(files["manifest.json"])
    delta = seal_floor_delta(
        {
            "kind": "full",
            "renderer": manifest.renderer,
            "head": PlaybillFloorHeadV1(
                **manifest.coordinate.model_dump(mode="json", exclude={"tag"}),
                generation=manifest.generation,
                notes_digest=manifest.notes_digest,
            ).model_dump(mode="json"),
            "head_manifest_digest": floor_manifest_digest(manifest),
            "files": [
                {
                    "path": row.path,
                    "content_b64": base64.b64encode(files[row.path]).decode("ascii"),
                    "sha256": row.content_digest,
                    "changed_at": row.changed_at,
                }
                for row in manifest.files
            ],
        }
    )
    export = contracts.PlaybillFloorExport(
        tag="playbill-floor-export-v5",
        coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
            AcceptedCoordinate.from_internal(head).model_dump(mode="json")
        ),
        manifest=manifest.model_dump(mode="json"),
        files=[
            contracts.PlaybillFloorFile(
                path=path, content_base64=base64.b64encode(content).decode("ascii")
            )
            for path, content in files.items()
        ],
    )
    return delta, export


def refresh_floor(
    instance: Any,
    instance_id: str,
    *,
    require_delivery: bool = False,
    follow_fire: bool = False,
    include: tuple[contracts.PlaybillFloorExportPart, ...] = (),
    at: contracts.PlaybillAcceptedCoordinate | None = None,
) -> contracts.PlaybillFloorDeliveryResultV1 | None:
    """Render at the current head and, when opted in, apply through the shared writer."""

    with FLOOR_ADMISSION.hold(instance_id):
        return _refresh_floor_admitted(
            instance,
            instance_id,
            require_delivery=require_delivery,
            follow_fire=follow_fire,
            include=include,
            at=at,
        )


def _refresh_floor_admitted(
    instance: Any,
    instance_id: str,
    *,
    require_delivery: bool = False,
    follow_fire: bool = False,
    include: tuple[contracts.PlaybillFloorExportPart, ...] = (),
    at: contracts.PlaybillAcceptedCoordinate | None = None,
) -> contracts.PlaybillFloorDeliveryResultV1 | None:
    """Refresh with floor admission already held by the caller."""

    record = get_registry().get(instance_id)
    if require_delivery and (
        record is None or not record.floor_delivery or record.workspace_root is None
    ):
        raise PlaybillWorkspaceError("Daemon floor delivery is off for this workspace")
    covered = latest_sequence(instance, action="floor.refresh")
    head = instance.accepted_coordinate()
    if at is not None and at != contracts.PlaybillAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(head).model_dump(mode="json")
    ):
        raise RequestRefusedError(
            "playbill.floor.delivery_head_only",
            "Daemon floor delivery owns this workspace's floor and writes only the "
            "current accepted head; at names a different coordinate. To read an older "
            "coordinate, use get or query with at. To write a pinned floor, turn daemon "
            "delivery off: cruxible playbill workspace floor-delivery off "
            f"--instance-id {instance_id}.",
            repair=RepairOperationV1(
                operation="playbill.workspace.floor-delivery",
                arguments={"state": "off", "instance_id": instance_id},
            ),
        )
    target = _target(instance, record, head)
    progress = _progress(instance)
    if (
        follow_fire
        and progress is not None
        and (progress[3] == target or (progress[2] == target and progress[0] >= covered))
    ):
        return None
    with instance.accepted_history_reader() as history:
        location = history.generation_for_oid(head.git_oid)
        assert location is not None
        generation = location.sequence
    written = None
    error = None
    try:
        advance_floor_index(instance, head)
        if record is not None and record.floor_delivery and record.workspace_root is not None:
            root, floor = _floor_directory(record.workspace_root)
            profile = configured_floor_output(root)
            parts = include or (() if profile is None else profile[1])
            export = None
            if parts:
                full, export = _included_floor(instance, head, tuple(parts))
                delta, applied = sync_floor_directory(lambda _base, _renderer: full, floor)
            else:
                delta, applied = sync_floor_directory(
                    lambda base, renderer: service_playbill_floor_delta(
                        instance,
                        head=AcceptedCoordinate.from_internal(head),
                        base_generation=base,
                        base_renderer=renderer,
                    ),
                    floor,
                )
            _post_apply_joins(root)
            assert applied.floor_digest is not None
            receipt = contracts.PlaybillWorkspaceFloorWriteResult(
                status="unchanged" if applied.status == "unchanged" else "written",
                path=".playbill/floor",
                destination=str(floor),
                floor_digest=applied.floor_digest,
                coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
                    delta.head.coordinate().model_dump(mode="json")
                ),
                file_count=applied.file_count + 1,
            )
            written = contracts.PlaybillFloorDeliveryResultV1(
                delta=delta, written=receipt, export=export
            )
        outcome = contracts.PlaybillFloorConsumerOutcomeV1(
            generation=generation,
            status="unchanged" if written is None else written.written.status,
            file_count=0 if written is None else written.written.file_count,
        )
    except Exception as exc:
        error = exc
        outcome = contracts.PlaybillFloorConsumerOutcomeV1(
            generation=generation, status="failed", error=f"{type(exc).__name__}: {exc}"
        )
    with _STATE.open(instance) as connection:
        assert connection is not None
        connection.execute(
            "INSERT INTO progress VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "covered=excluded.covered,target=excluded.target,"
            "completed=excluded.completed,failed=excluded.failed",
            (covered, target, target if error is None else None, target if error else None),
        )
        connection.execute("INSERT INTO outcomes(payload) VALUES (?)", (outcome.model_dump_json(),))
        connection.execute(
            "DELETE FROM outcomes WHERE sequence < (SELECT max(sequence) FROM outcomes)"
        )
    if error is not None:
        raise error
    return written


class FloorConsumers:
    name = "floor"
    cursor_policy: CursorPolicy = "resume"
    effect_class: EffectClass = "workspace_output"
    workers = 2

    def active(self, instance: Any) -> bool:
        if any(item.action == "floor.refresh" for item in internal_triggers(instance)):
            return True
        progress = _progress(instance)
        return progress is not None and progress[1] != progress[2] and progress[1] != progress[3]

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        target = _target(instance, get_registry().get(instance.descriptor.instance_id))
        progress = _progress(instance)
        sequence = latest_sequence(instance, action="floor.refresh")
        if progress is None:
            if sequence == 0:
                return
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute(
                    "INSERT OR IGNORE INTO progress VALUES (1,0,?,NULL,NULL)", (target,)
                )
        elif progress[1] != target and (
            sequence > progress[0]
            or progress[3] == progress[1]
            or json.loads(progress[1])[1:] != json.loads(target)[1:]
        ):
            with _STATE.open(instance) as connection:
                assert connection is not None
                connection.execute("UPDATE progress SET target=? WHERE id=1", (target,))

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        progress = _progress(instance)
        if progress is None:
            return ()
        covered, target, completed, failed = progress
        if failed == target:
            return ()
        if target != completed or latest_sequence(instance, action="floor.refresh") > covered:
            return (ConsumerWork(key="deliver", item=None),)
        return ()

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        refresh_floor(manager.get(instance_id), instance_id, follow_fire=True)

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        progress = _progress(instance)
        if progress is None:
            return ()
        stalled = progress[3] == progress[1]
        with _STATE.open(instance, create=False) as connection:
            row = (
                None
                if connection is None
                else connection.execute(
                    "SELECT payload FROM outcomes ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
            )
        outcome = (
            None
            if row is None
            else contracts.PlaybillFloorConsumerOutcomeV1.model_validate_json(row[0])
        )
        return (
            ConsumerHealth(
                kind=self.name,
                consumer_id="consumer:floor",
                state="stalled"
                if stalled
                else "lagging"
                if tuple(self.due(instance, now=now))
                else "running",
                detail={
                    "covered": progress[0],
                    "outcome": None if outcome is None else outcome.model_dump(mode="json"),
                },
                repair=ConsumerRepair(
                    operation="playbill.floor.export",
                    required_change="repair_the_workspace_floor_then_write_it",
                    arguments={"mode": "write"},
                )
                if stalled
                else None,
            ),
        )


FLOOR = FloorConsumers()

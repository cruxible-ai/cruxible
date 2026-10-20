"""A read at the head never refuses because a settle published the next serving pointer.

Activation publishes the new serving pointer before this process advances its
in-memory head. A read that started at the previous head binds that coordinate's
own immutable manifest instead (dev.roadmap_item/serving-pointer-read-race-1019).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

import cruxible_core.ledger.activation as activation_module
import cruxible_core.runtime.instance as instance_module
from cruxible_client.contracts.captures import DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.indexes.serving import (
    SERVING_MANIFEST_FILE,
    ServingPointerMoved,
    load_serving_manifest,
    render_serving_manifest,
)
from cruxible_core.runtime.retention import RetentionSettings
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import _seed_claim_surface


def _projections(instance: Any) -> Path:
    return instance._validated_paths(instance.root, instance.descriptor.storage)["projections"]


@pytest.mark.parametrize("generations", [None, 1])
def test_a_read_at_the_head_answers_while_the_next_pointer_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generations: int | None
) -> None:
    instance, owner = initialize_local(tmp_path)
    if generations is not None:
        # Even a one-generation window keeps the head's parent: a read there
        # binds it by name while the next pointer is out.
        instance.retention = RetentionSettings(generations=generations)
    directory = _projections(instance)
    published = activation_module.publish_serving_manifest
    seen: dict[str, Any] = {}
    reader_builds: list[str] = []
    reader_thread: dict[str, threading.Thread] = {}

    # Cost: the fallback binds an existing manifest; it never assembles.
    assembler = instance_module.ProjectionAssembler

    def counted_assembler(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread() is reader_thread.get("thread"):
            reader_builds.append("assembler")
        return assembler(*args, **kwargs)

    monkeypatch.setattr(instance_module, "ProjectionAssembler", counted_assembler)
    replayed = instance._accepted_coordinates_by_sequence

    def counted_replay() -> Any:
        if threading.current_thread() is reader_thread.get("thread"):
            reader_builds.append("history")
        return replayed()

    monkeypatch.setattr(instance, "_accepted_coordinates_by_sequence", counted_replay)

    def read_at(head: AcceptedProjectionCoordinate) -> None:
        try:
            with instance.bind_accepted_projection(head) as projection:
                seen["answered"] = projection.accepted
                seen["files"] = (
                    projection.manifest_path,
                    *(
                        projection.manifest_path.parent / piece.name
                        for piece in projection.manifest.pieces
                    ),
                )
        except BaseException as exc:  # pragma: no cover - the failure being asserted
            seen["refused"] = exc

    def publish_then_read(*args: Any, **kwargs: Any) -> Any:
        manifest = published(*args, **kwargs)
        if "head" not in seen:
            # The next pointer is out; this process has not advanced its head.
            head = instance.accepted_coordinate()
            seen["head"] = head
            seen["serving_oid"] = load_serving_manifest(directory).git_oid
            reader = threading.Thread(target=read_at, args=(head,))
            reader_thread["thread"] = reader
            reader.start()
            reader.join(timeout=60)
            assert not reader.is_alive()
        return manifest

    monkeypatch.setattr(activation_module, "publish_serving_manifest", publish_then_read)
    _seed_claim_surface(instance, owner, contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)

    head = seen["head"]
    assert seen["serving_oid"] != head.git_oid, "the hook ran inside the publish window"
    assert "refused" not in seen, seen.get("refused")
    assert seen["answered"] == head
    assert reader_builds == []
    assert instance.accepted_coordinate() != head

    # The settle's own pass ran at N+1; another pass (as another process at
    # N+1 would run) still keeps N, and a read at N binds without building.
    instance.evict_projection_pieces()
    assert all(path.exists() for path in seen["files"])

    def no_rebuild(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a retained parent was rebuilt")

    monkeypatch.setattr(assembler, "assemble", no_rebuild)
    with instance.bind_accepted_projection(head) as projection:
        assert projection.accepted == head


def test_a_foreign_or_malformed_serving_pointer_still_refuses(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    directory = _projections(instance)
    head = instance.accepted_coordinate()
    serving = directory / SERVING_MANIFEST_FILE
    original = serving.read_bytes()

    def replace(content: bytes) -> None:
        temporary = directory / ".serving-test.tmp"
        temporary.write_bytes(content)
        os.replace(temporary, serving)

    foreign = load_serving_manifest(directory).model_copy(update={"instance_id": "inst_other"})
    replace(render_serving_manifest(foreign))
    with pytest.raises(ProjectionIntegrityError) as refused:
        instance.bind_accepted_projection(head)
    assert not isinstance(refused.value, ServingPointerMoved)

    replace(original.rstrip(b"\n") + b" \n")
    with pytest.raises(ProjectionIntegrityError, match="not canonical"):
        instance.bind_accepted_projection(head)

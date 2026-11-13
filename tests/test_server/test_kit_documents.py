"""Item 11: a kit ships governed Documents.

A Document envelope travels like any definition (digest, receipt membership,
keep/keep-local-edits previews, removal on ``kit remove``); its body rides the
body-store staging that bundled provider files use, so a missing body refuses
before anything is written; a ``guides`` link to the kit's own ClaimType
resolves within the same change set; a kit Document never takes a receipt's
path (``documents/kit-<id>.json``).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.backlinks import BacklinksRequest
from cruxible_client.contracts.documents import DocumentLifecycle, DocumentLink, DocumentShell
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitArtifact,
    KitBuildRequest,
    KitBundle,
    KitRemoveRequest,
)
from cruxible_client.errors import CoreError as ClientCoreError
from cruxible_core.errors import CoreError
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.test_server.test_playbill_kits import SEATS, _claim_type, _open_worlds, _World

GUIDE = "acme.seats-review"


def _added(status: Any) -> tuple[Any, ...]:
    """The kits these tests add: every instance also holds creation's base kit."""

    return tuple(kit for kit in status.kits if kit.kit_id != "base")


@pytest.fixture
def worlds(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The publisher and consumer pair of the kit tests."""

    yield from _open_worlds(tmp_path, monkeypatch, independent=False)


GUIDE_PATH = f"documents/{GUIDE}.json"


def _document(
    world: _World, name: str, text: bytes, *, title: str | None = None, guides: bool = True
) -> None:
    stored = playbill_api.playbill_store_body(
        world.instance_id, content_base64=base64.b64encode(text).decode("ascii")
    )
    shell = DocumentShell(
        identity=f"document:{name}",
        document_kind="guidance",
        title=title or f"Guide {name}",
        media_type="text/markdown",
        body_digest=stored.digest,
        links=(
            (DocumentLink(relation="guides", target_identity=f"ClaimType:{SEATS}"),)
            if guides
            else ()
        ),
        governance_scope=("acme",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = playbill_api.playbill_propose_document(
        world.instance_id, shell=shell, proposal_name=f"doc-{name}", accept="never"
    )
    world.activate(proposed.proposal["admission"]["proposal_id"])


def _revise(world: _World, name: str, *, title: str) -> None:
    """A local edit of an installed Document: its successor with a new title."""

    from cruxible_client.contracts.documents import document_digest, parse_document

    path = f"documents/{name}.json"
    current = parse_document(world.tree()[path], path=path)
    successor = current.model_copy(
        update={
            "title": title,
            "predecessor_digest": document_digest(current).tagged,
            "lifecycle": DocumentLifecycle(revision=current.lifecycle.revision + 1),
        }
    )
    proposed = playbill_api.playbill_propose_document(
        world.instance_id, shell=successor, proposal_name=f"edit-{name}", accept="never"
    )
    world.activate(proposed.proposal["admission"]["proposal_id"])


def _staged(world: _World, bundle: KitBundle) -> KitBundle:
    """What the CLI and MCP adapters do: stage each body, send the bundle without them."""

    for body in bundle.document_bodies:
        stored = playbill_api.playbill_store_body(
            world.instance_id, content_base64=body.content_base64
        )
        assert stored.digest == body.digest
    return bundle.model_copy(update={"document_bodies": ()})


def _instance(world: _World) -> Any:
    return get_playbill_manager().get(world.instance_id)


def _publisher_kit(publisher: _World, version: str = "1.0.0") -> KitBundle:
    bundle = publisher.build(version)
    assert GUIDE_PATH in bundle.contents()
    assert {body.digest for body in bundle.document_bodies} == set(
        bundle.document_body_digests().values()
    )
    return bundle


def test_a_template_builds_beside_an_exported_document_with_a_non_genesis_body(
    worlds: tuple[_World, _World],
) -> None:
    """D-R48 review fix round 2, R3: the throwaway closure check runs after the
    kit's own Document bodies land in its body store, so a kit that also ships
    an automation template and a governed Document (a real body, never the
    empty one a fresh instance starts with) builds -- it previously refused
    ``cruxible.document.body_missing`` because the check ran before bodies
    were collected and landed only artifact bytes.
    """

    from cruxible_client.contracts.kits import KitAutomationTemplate
    from tests.test_server.test_playbill_kits import _START, _START_DAILY, _template

    publisher, _consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    _document(publisher, GUIDE, b"# Seats\nReal guidance content, not the empty body.\n")
    content = _template(_START, _START_DAILY)

    bundle = playbill_api.playbill_kit_build(
        publisher.instance_id,
        KitBuildRequest(
            kit_id="acme",
            version="1.0.0",
            owns=("acme.",),
            automation_template=KitAutomationTemplate(path="automation.json", content=content),
        ),
    ).bundle

    assert bundle.manifest.automation_template == "automation.json"
    assert GUIDE_PATH in bundle.contents()
    assert {body.digest for body in bundle.document_bodies} == set(
        bundle.document_body_digests().values()
    )


def test_a_fresh_consumer_previews_document_bodies_without_persisting_then_commits(
    worlds: tuple[_World, _World], tmp_path: Path
) -> None:
    from tests.support.store_snapshot import assert_writes_nothing

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    _document(publisher, GUIDE, b"# Seats\nNew guidance absent from consumer genesis.\n")
    bundle = _publisher_kit(publisher)
    (body,) = bundle.document_bodies
    manager = get_playbill_manager()
    manager.consumer_runner.close()

    def settle_background() -> None:
        for world in (publisher, consumer):
            manager.get(world.instance_id).settled_workspace_advertisement()
        consumer.tree()

    store = _instance(consumer).body_store()
    assert store.availability(body.digest) == "missing"
    preview = assert_writes_nothing(
        [tmp_path],
        lambda: playbill_api.playbill_kit_add(
            consumer.instance_id, KitAddRequest(bundle=bundle, source="test")
        ),
        warm=settle_background,
    )
    assert preview.status == "would_propose", preview
    assert preview.proposal_id is None and preview.coordinate is not None
    assert {item.path: item.action for item in preview.plan}[GUIDE_PATH] == "add"
    assert store.availability(body.digest) == "missing"
    assert GUIDE_PATH not in consumer.tree()

    committed = playbill_api.playbill_kit_add(
        consumer.instance_id,
        KitAddRequest(
            bundle=_staged(consumer, bundle),
            source="test",
            dry_run=False,
            at=preview.coordinate.git_oid,
        ),
    )
    consumer.settle(committed)
    assert GUIDE_PATH in consumer.tree()
    assert store.availability(body.digest) == "present"


def test_a_kit_installs_upgrades_and_removes_its_documents(
    worlds: tuple[_World, _World],
) -> None:
    from cruxible_core.service.discovery.backlinks import review_guidance

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    _document(publisher, GUIDE, b"# Seats\nCount paid seats only.\n")
    first = _publisher_kit(publisher)

    installed = consumer.add(_staged(consumer, first))
    assert installed.status in {"accepted", "proposed"}, installed
    plan = {item.path: item.action for item in installed.plan}
    assert plan[GUIDE_PATH] == "add"
    tree = consumer.tree()
    assert GUIDE_PATH in tree and "documents/kit-acme.json" in tree
    from cruxible_core.service.kits import _read_receipt

    instance = _instance(consumer)
    found = _read_receipt(
        instance, instance.immutable_tree_at(instance.accepted_coordinate().git_oid), "acme"
    )
    assert found is not None
    assert GUIDE_PATH in {item.path for item in found[1].artifacts}
    (installed,) = _added(playbill_api.playbill_kit_status(consumer.instance_id))
    assert installed.drifted == ()
    # The guides link resolved within the kit's own change set and is served.
    guidance = playbill_api.playbill_backlinks(
        consumer.instance_id, request=BacklinksRequest(ref=f"ClaimType:{SEATS}")
    )
    assert [(link.relation, link.source) for link in guidance.links] == [
        ("guides", f"document:{GUIDE}")
    ]
    shown = review_guidance(_instance(consumer), (f"ClaimType:{SEATS}",))
    assert [item.document for item in shown] == [f"document:{GUIDE}"]

    # An upgrade that leaves the Document alone plans it unchanged.
    unchanged = consumer.add(_staged(consumer, _publisher_kit(publisher, "1.1.0")))
    assert {item.path: item.action for item in unchanged.plan}[GUIDE_PATH] == "unchanged"

    # A local edit is taken back by default, and kept under keep_local_edits.
    _revise(consumer, GUIDE, title="Seats, our way")
    taken = consumer.add(_staged(consumer, _publisher_kit(publisher, "1.2.0")))
    (row,) = [item for item in taken.plan if item.path == GUIDE_PATH]
    assert (row.action, row.consequence) == ("replace", "overwrites_your_edit")
    assert b"Seats, our way" not in consumer.tree()[GUIDE_PATH]
    _revise(consumer, GUIDE, title="Seats, edited again")
    kept = consumer.add(
        _staged(consumer, _publisher_kit(publisher, "1.3.0")), keep_local_edits=True
    )
    (row,) = [item for item in kept.plan if item.path == GUIDE_PATH]
    assert (row.action, row.consequence) == ("keep", "overwrites_your_edit")
    assert b"Seats, edited again" in consumer.tree()[GUIDE_PATH]

    removed = playbill_api.playbill_kit_remove(
        consumer.instance_id, KitRemoveRequest(kit_id="acme", dry_run=False)
    )
    consumer.settle(removed)
    # Accepted state refuses a Document's removal and Documents have no
    # retired state, so the kit leaves it accepted (as a Contract version)
    # while the receipt lets it go.
    (row,) = [item for item in removed.plan if item.path == GUIDE_PATH]
    assert (
        row.action == "keep" and row.detail == "Documents have no retired state and remain accepted"
    )
    assert GUIDE_PATH in consumer.tree()
    assert _added(playbill_api.playbill_kit_status(consumer.instance_id)) == ()


def test_a_missing_document_body_refuses_before_anything_is_written(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    _document(publisher, GUIDE, b"# Seats\nOnly in the publisher's store.\n")
    bundle = _publisher_kit(publisher).model_copy(update={"document_bodies": ()})
    before = consumer.tree()
    with pytest.raises((CoreError, ClientCoreError)) as refused:
        consumer.add(bundle)
    assert "document_bodies_not_staged" in str(refused.value)
    assert consumer.tree() == before


def test_a_kit_document_never_takes_a_receipt_path() -> None:
    with pytest.raises(ValueError):
        KitArtifact(path="documents/kit-acme.json", artifact_digest="sha256:" + "a" * 64)
    KitArtifact(path="documents/acme.guide.json", artifact_digest="sha256:" + "a" * 64)


def test_install_cost_is_linear_in_kit_members(worlds: tuple[_World, _World]) -> None:
    """Each Document a kit installs costs the same statements; nothing else grows.

    Three kits (2, 4 and 8 Documents) install one after another into one
    consumer, so each later install also runs over more unrelated state.
    Statements are counted on this thread, on every connection the install
    opens (the instance is reopened before each, so none is untraced).
    """

    import sqlite3
    import threading

    from cruxible_client.contracts.kits import KitAddRequest, KitBuildRequest

    publisher, consumer = worlds
    # Sizes 1 and 3 install first, unmeasured: the first writes after creation
    # also carry one-time work (a copy of the projection creation's base kit add
    # published, and the first kit beside the base kit), which would otherwise
    # land on the size-2 install and hide or fake the per-member slope.
    sizes = (1, 3, 2, 4, 8)
    for size in sizes:
        for index in range(size):
            _document(
                publisher,
                f"p{size}.guide-{index}",
                f"# Guide {size}-{index}\n".encode(),
                guides=False,
            )
    counts: dict[int, int] = {}
    original = sqlite3.connect
    measuring = threading.get_ident()
    for size in sizes:
        bundle = playbill_api.playbill_kit_build(
            publisher.instance_id,
            KitBuildRequest(kit_id=f"p{size}", version="1.0.0", owns=(f"p{size}.",)),
        ).bundle
        staged = _staged(consumer, bundle)
        statements = [0]

        def count(_sql: str) -> None:
            if threading.get_ident() == measuring:
                statements[0] += 1

        def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection = original(*args, **kwargs)
            connection.set_trace_callback(count)
            return connection

        get_playbill_manager().clear()
        sqlite3.connect = traced  # type: ignore[assignment]
        try:
            result = playbill_api.playbill_kit_add(
                consumer.instance_id, KitAddRequest(bundle=staged, source="test", dry_run=False)
            )
        finally:
            sqlite3.connect = original  # type: ignore[assignment]
        assert result.status == "accepted", result
        counts[size] = statements[0]
    print("item 11 kit install statements", counts)
    small, middle, large = (counts[size] for size in sizes[2:])
    # Linear in members: 4 more Documents cost about twice what 2 more cost.
    assert large - middle <= 2 * (middle - small) * 1.25 + 60, counts

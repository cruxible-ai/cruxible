"""Equivalence over a varied history: every (base, head) delta rebuilds the full floor exactly.

The history mixes Claim changes, retirements, contenders, Subject-valued edges
moved in place (fan-in to old and new targets), new ClaimTypes, a short-name
shadow that appears and is retired, a ClaimType cardinality migration, long
text that appears and goes away, evidence-cited Claims, a Document revised in
place, a review rationale revised after acceptance, and one source cited at two
external coordinate/selector types by two Claims, the first of which (in
identity order) is then revised, so an incremental render meets them in the
other order. For every checked pair of generations (see ``_pairs``) the delta
applied to the base floor is byte-identical to the full floor at the head; a
floor installed before the notes revision is repaired to it; the deltas are
the same whether the index was cold, warm or advanced in one coalesced step;
and applying one twice changes nothing.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.floor_apply import apply_floor_delta
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    document_path,
    render_document,
)
from cruxible_client.contracts.floor import FloorDelta
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDispositionV1,
    ClaimTypeMigrationRequestV1,
    service_migrate_claim_type,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.floor.floor_delta import (
    advance_floor_index,
    service_playbill_floor_delta,
)
from tests.core_support._write_support import (
    KIND,
    _claim_type,
    report_evidence,
    seed_write_surface,
)
from tests.test_floor.test_floor_index import _set, _write, accept_edit, add_lead_field
from tests.test_floor.test_floor_notes_and_retention import _revise_rationale

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"


def _document(instance: PlaybillInstance, body: bytes, previous: DocumentShell | None) -> Any:
    shell = DocumentShell(
        identity="document:design-note",
        document_kind="design",
        title="Design note",
        media_type="text/markdown",
        body_digest=instance.store_document_body(body).digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1 if previous is None else 2),
        **({} if previous is None else {"predecessor_digest": document_digest(previous).tagged}),
    )
    accept_edit(
        instance,
        f"design-note-{1 if previous is None else 2}",
        {document_path("design-note"): render_document(shell)},
    )
    return shell


def _shadow_and_cardinality(instance: PlaybillInstance) -> None:
    """A field whose short name a new ClaimType shadows, then a cardinality migration."""

    note = _claim_type("ext.note", literal_schema={"type": "string"})
    accept_edit(instance, "ext-note", {claim_type_path(note.predicate): render_claim_type(note)})
    _write(instance, _set(WI1, "ext.note", "Hello"))
    shadow = note.model_copy(
        update={
            "predicate": "ext.note",
            "identity": ArtifactIdentity(kind="ClaimType", name="ext.note"),
        }
    )
    accept_edit(instance, "shadow", {claim_type_path("ext.note"): render_claim_type(shadow)})
    retired = shadow.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=claim_type_digest(shadow).tagged
            )
        }
    )
    accept_edit(
        instance, "shadow-retired", {claim_type_path("ext.note"): render_claim_type(retired)}
    )
    path = claim_type_path(f"{KIND}.title")
    current = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    successor = current.model_copy(
        update={
            "cardinality": "many",
            "resolution_policy": current.resolution_policy.model_copy(
                update={"cardinality": "many", "selector": "all"}
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged),
        }
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        claims = sorted(
            str(row[0]).removeprefix("Claim:")
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE predicate=?", (f"{KIND}.title",)
            )
        )
    migration = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV1(
            successor=successor,
            dependents=tuple(
                ClaimTypeDependentDispositionV1(claim_id=claim, disposition="successor")
                for claim in claims
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    proposal = migration.proposal.proposal
    assert proposal.candidate is not None
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"


# Claim ID -> the external coordinate/selector types its Capture is described at.
_EXTERNAL_LOCATORS: dict[str, str] = {}


def _two_locators(instance: PlaybillInstance, workspace: Path) -> None:
    """Two Claims citing one source at two external selector types, then the first revised.

    The write verbs take workspace evidence through the foreign-source contract,
    whose Captures carry no locator; the description of these two is replaced by
    two non-foreign external ones (``_describe`` is the one place a Capture's
    source is read), keyed by the Claim each Capture's selector names.
    """

    two = _write(instance, _set(WI2, "measured", 4, evidence=report_evidence(workspace, "C: 4")))
    three = _write(instance, _set(WI3, "measured", 5, evidence=report_evidence(workspace, "C: 5")))
    by_claim = {two.changes[0].claim: WI2, three.changes[0].claim: WI3}
    first, second = sorted(by_claim)
    _EXTERNAL_LOCATORS.update(
        {
            first: "postgres-lsn-v1/relation-primary-key-v1",
            second: "http-response-v1/whole-response-v1",
        }
    )
    _write(
        instance, _set(by_claim[first], "measured", 6, evidence=report_evidence(workspace, "C: 6"))
    )


def _describe_external(original: Any) -> Any:
    from cruxible_client.contracts.source_references import ExternalSourceReference
    from cruxible_core.service.floor.floor_sources import CaptureSource

    def describe(contract_digest: str, source: object) -> Any:
        found = original(contract_digest, source)
        if isinstance(source, ExternalSourceReference) and isinstance(source.selector, dict):
            locator = _EXTERNAL_LOCATORS.get(str(source.selector.get("claim_id")))
            if locator is not None:
                return CaptureSource(contract_digest, found.source, locator)
        return found

    return describe


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    from cruxible_core.service.floor import floor_sources

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(floor_sources, "_describe", _describe_external(floor_sources._describe))
        yield _build_world(tmp_path_factory)


def _build_world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("floor-delta-properties")
    (root / "instance").mkdir()
    instance, _owner = seed_write_surface(root / "instance")
    workspace = root / "workspace"
    _write(instance, _set(WI1, "status", "ready"), _set(WI1, "title", "One"))
    first = _write(instance, _set(WI2, "status", "ready"))
    _write(instance, _set(WI2, "status", "blocked"))
    _write(instance, _set(WI2, "status", "done", contend=True), at=first.coordinate.git_oid)
    add_lead_field(instance)
    _write(instance, _set(WI1, "lead", WI2), _set(WI3, "lead", WI2))
    _write(instance, _set(WI3, "ruling", "".join(f"Clause {i}.\n" for i in range(60))))
    _write(instance, _set(WI1, "measured", 3, evidence=report_evidence(workspace, "Count: 3")))
    note = _document(instance, b"# Note\n\nFirst.\n", None)
    _write(
        instance,
        _set(WI1, "lead", WI3),
        {"op": "add", "subject": WI2, "field": "governs", "value": WI1},
    )
    _document(instance, b"# Note\n\nSecond.\n", note)
    _write(instance, _set(WI3, "ruling", "Short now.\n"))
    _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "measured"}})
    _write(instance, _set(WI2, "title", "Two"), _set(WI3, "title", "Three"))
    _shadow_and_cardinality(instance)
    _write(instance, {"op": "add", "subject": WI2, "field": "title", "value": "Two, again"})
    _write(instance, {"op": "retire", "target": {"subject": WI3, "field": "ruling"}})
    _two_locators(instance, workspace)
    head = len(instance.accepted_history()) - 1
    # Every generation's floor as a client installed it before the notes moved.
    installed = root / "installed"
    for generation in range(head + 1):
        apply_floor_delta(installed / str(generation), _delta(instance, generation, None, None))
    # The change that added "Two, again", still shown under changes/.
    _revise_rationale(instance, "The writer checked it.", "Later review text.", back=1)
    return {"instance": instance, "head": head, "installed": installed}


def _coordinate(instance: PlaybillInstance, generation: int) -> AcceptedCoordinate:
    with instance.accepted_history_reader() as history:
        oid = history.generation(generation).git_oid
    return AcceptedCoordinate.from_internal(instance.coordinate_for_oid(oid))


def _delta(
    instance: PlaybillInstance, head: int, base: int | None, renderer: str | None
) -> FloorDelta:
    return service_playbill_floor_delta(
        instance,
        head=_coordinate(instance, head),
        base_generation=base,
        base_renderer=renderer,
    )


# Mid-history spans checked beyond the structural pairs; fixed seed, so every
# run checks the same pairs.
_SAMPLED_PAIRS = 16


def _pairs(head: int) -> list[tuple[int, int]]:
    """The (base, target) generation pairs the delta law is checked on.

    Every pair is quadratic in the history length. These keep each distance
    the law has to hold over: none (base == target), one step (adjacent),
    genesis to every head, every base to the last head, and a fixed sample of
    the remaining mid-history spans.
    """

    chosen = {(target, target) for target in range(head + 1)}
    chosen |= {(target - 1, target) for target in range(1, head + 1)}
    chosen |= {(0, target) for target in range(head + 1)}
    chosen |= {(base, head) for base in range(head + 1)}
    rest = sorted(
        {(base, target) for target in range(head + 1) for base in range(target + 1)} - chosen
    )
    chosen |= set(random.Random(0).sample(rest, min(_SAMPLED_PAIRS, len(rest))))
    return sorted(chosen, key=lambda pair: (pair[1], pair[0]))


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_every_delta_rebuilds_the_full_floor_byte_for_byte(
    world: dict[str, Any], tmp_path: Path
) -> None:
    instance: PlaybillInstance = world["instance"]
    head = world["head"]
    fulls: dict[int, dict[str, bytes]] = {}
    renderer = _delta(instance, head, None, None).renderer
    for generation in range(head + 1):
        directory = tmp_path / f"full-{generation}"
        apply_floor_delta(directory, _delta(instance, generation, None, None))
        fulls[generation] = _tree(directory)
    # Every file family moved somewhere in this history.
    paths = set().union(*fulls.values())
    assert any(path.endswith(".txt") for path in paths)
    assert any(b"incoming:" in content for tree in fulls.values() for content in tree.values())
    assert any(
        b"Document:design-note" in tree.get("sources/LEDGER", b"") for tree in fulls.values()
    )
    shown = b"".join(content for tree in fulls.values() for content in tree.values())
    assert b"project.work_item.ext.note: Hello" in shown and b"\next.note: Hello" in shown
    assert b"\ntitle:\n  - " in shown
    assert b"Later review text." in shown
    assert (
        b"http-response-v1/whole-response-v1,postgres-lsn-v1/relation-primary-key-v1"
        in fulls[head]["sources/LEDGER"]
    )
    for base, target in _pairs(head):
        directory = tmp_path / f"apply-{base}-{target}"
        apply_floor_delta(directory, _delta(instance, base, None, None))
        delta = _delta(instance, target, base, renderer)
        first = apply_floor_delta(directory, delta)
        assert first.status in {"applied", "unchanged"}, (base, target)
        assert _tree(directory) == fulls[target], (base, target)
        again = apply_floor_delta(directory, delta)
        assert again.status == "unchanged" and _tree(directory) == fulls[target]


def test_a_floor_installed_before_the_notes_moved_reaches_every_head(
    world: dict[str, Any], tmp_path: Path
) -> None:
    import shutil

    from cruxible_client.authoring.workspace import sync_floor_directory

    instance: PlaybillInstance = world["instance"]
    head = world["head"]
    fulls: dict[int, dict[str, bytes]] = {}
    for base, target in _pairs(head):
        if target not in fulls:
            full = tmp_path / f"full-{target}"
            apply_floor_delta(full, _delta(instance, target, None, None))
            fulls[target] = _tree(full)
        directory = tmp_path / f"sync-{base}-{target}"
        shutil.copytree(world["installed"] / str(base), directory)

        def fetch(generation: int | None, renderer: str | None) -> FloorDelta:
            return _delta(instance, target, generation, renderer)

        sync_floor_directory(fetch, directory)
        assert _tree(directory) == fulls[target], (base, target)


def test_deltas_are_the_same_cold_warm_and_coalesced(world: dict[str, Any]) -> None:
    instance: PlaybillInstance = world["instance"]
    head = world["head"]
    renderer = _delta(instance, head, None, None).renderer
    pairs = _pairs(head)

    def every() -> list[FloorDelta]:
        return [_delta(instance, target, base, renderer) for base, target in pairs]

    warm = every()
    instance.floor_current_memo.clear()
    cold = every()
    instance.floor_current_memo.clear()
    advance_floor_index(instance, _coordinate(instance, 2))
    advance_floor_index(instance)
    coalesced = every()
    assert warm == cold == coalesced

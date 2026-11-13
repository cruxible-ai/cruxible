"""The base kit: bundled with core, checked against its pin, installed when an instance is created.

Creation runs the ordinary ``kit add`` with the bundled kit right after
genesis, as the creating owner, recording source ``bundled``: it lands at once
under self-approval and stops at ``proposed`` under independent approval, with
no network use either way. ``kit add base`` resolves to the bundled copy;
``kit status`` compares the installed ``base`` with it. Version 1 of a
capability under ``workspace.`` is accepted only as the bundled one, byte for
byte; later versions are the adopter's.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import (
    CapabilityInput,
    ChangeSetInput,
    ContractInput,
    lower_authoring_input,
)
from cruxible_client.contracts.capabilities import (
    capability_digest,
    capability_path,
    capability_path_parts,
    parse_capability,
    render_capability,
    validate_capability,
)
from cruxible_client.contracts.errors import BootstrapError
from cruxible_client.contracts.kits import KitAddRequest, kit_receipt_document_id
from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.kits import BundledKitPin
from cruxible_core.kits import base_kit as base_kit_module
from cruxible_core.kits.base_kit import (
    BASE_KIT_PENDING_FILE,
    BASE_KIT_PIN,
    BUNDLED_KIT_SOURCE,
    base_kit_directory,
    bundled_base_kit,
    bundled_kit_reference,
    install_bundled_base_kit,
    load_bundled_kit,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.kits import service_add_kit, service_kit_status
from cruxible_core.service.proposals.review import service_prepare_playbill_approval
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._identity_support import accept_tree
from tests.core_support._support import FIXED_TIMESTAMP, generate_client
from tests.test_ledger.test_activation import _sign

STAMP = "2026-11-12T12:00:00.000000Z"
OWNER = AuthenticatedActor(actor_id="owner")


def _base_capabilities() -> tuple[str, ...]:
    return tuple(
        sorted(
            parts[0]
            for item in bundled_base_kit().manifest.artifacts
            if (parts := capability_path_parts(item.path)) is not None
        )
    )


def _create(
    tmp_path: Path, *, independent: bool = False
) -> tuple[PlaybillInstance, GeneratedKeyMaterial, GeneratedKeyMaterial]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    managed = tmp_path / "managed"
    owner = generate_client(tmp_path, managed_root=managed, principal_id="owner", roles=("o",))
    reviewer = generate_client(
        tmp_path, managed_root=managed, principal_id="reviewer", roles=("r",)
    )
    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_base_kit",
        client_principals=(owner.principal, reviewer.principal),
        workspace_roots=(tmp_path / "workspace",),
        require_independent_approval=independent,
        timestamp=FIXED_TIMESTAMP,
    )
    return instance, owner, reviewer


def _tree(instance: PlaybillInstance) -> dict[str, bytes]:
    return dict(instance.tree_at(instance.accepted_coordinate().git_oid))


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Fail the test on any socket: creating one, connecting, or a name lookup."""

    used: list[str] = []

    def refuse(what: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            used.append(what)
            raise AssertionError(f"instance creation used the network ({what})")

        return call

    monkeypatch.setattr(socket.socket, "__init__", refuse("socket"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    yield used


# -- the bundle ---------------------------------------------------------------------------


def test_the_bundle_is_the_pinned_base_kit_with_eight_capabilities() -> None:
    bundle = bundled_base_kit()
    manifest = bundle.manifest
    assert (manifest.kit_id, manifest.version, manifest.owns) == (
        "base",
        "1.0.0",
        ("web.", "workspace."),
    )
    assert manifest.content_digest == BASE_KIT_PIN.content_digest
    assert manifest.providers == ()
    assert _base_capabilities() == (
        "web.fetch",
        "web.fetch.pages",
        "web.render",
        "web.render.dom",
        "web.search",
        "web.search.results",
        "workspace.file",
        "workspace.files",
    )
    # Definitions only: capabilities, the Contracts they pin, and the one shared kind.
    assert {item.path.split("/", 1)[0] for item in manifest.artifacts} == {
        "capabilities",
        "capture-contracts",
        "contracts",
    }
    assert [
        item.path for item in manifest.artifacts if item.path.startswith("capture-contracts/")
    ] == ["capture-contracts/web.search-results.json"]


def test_the_shared_kind_carries_the_settled_layout() -> None:
    """``web.search-results`` holds the wave 0 example's decoding and layout exactly."""

    from cruxible_client.contracts.captures import parse_capture_contract

    path = "capture-contracts/web.search-results.json"
    kind = parse_capture_contract(bundled_base_kit().contents()[path], path=path)
    example = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "pilot_contract_batch/examples/base/capture-kinds/web.search-results.json"
        ).read_bytes()
    )
    assert kind.identity.name == example["name"]
    assert kind.epistemic_grade == "derived"
    assert kind.decoding is not None and kind.layout is not None
    assert kind.decoding.model_dump(mode="json") == example["decoding"]
    assert kind.layout.model_dump(mode="json") == example["layout"]


def test_a_drifted_bundle_refuses_to_load(tmp_path: Path) -> None:
    drifted = tmp_path / "base"
    shutil.copytree(base_kit_directory(), drifted)
    assert load_bundled_kit(drifted, BASE_KIT_PIN).manifest.content_digest == (
        BASE_KIT_PIN.content_digest
    )
    # Bytes that differ from the digest their manifest names.
    target = drifted / "artifacts" / "capabilities" / "web.fetch" / "v1.json"
    payload = json.loads(target.read_bytes())
    payload["description"] = "Something else."
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    with pytest.raises(BootstrapError, match="differs from the digest its manifest names"):
        load_bundled_kit(drifted, BASE_KIT_PIN)
    # A pin that names another version or content.
    with pytest.raises(BootstrapError, match="but this core ships"):
        load_bundled_kit(
            base_kit_directory(),
            BundledKitPin(
                kit_id="base", version="1.0.1", content_digest=BASE_KIT_PIN.content_digest
            ),
        )


def test_kit_add_base_resolves_to_the_bundled_copy() -> None:
    resolved = bundled_kit_reference("base")
    assert resolved is not None
    bundle, source = resolved
    assert bundle is bundled_base_kit() and source == BUNDLED_KIT_SOURCE == "bundled"
    assert bundled_kit_reference("./base") is None
    assert bundled_kit_reference("security:1.3.0") is None


# -- installed at creation ----------------------------------------------------------------


def test_creation_installs_the_base_kit_as_the_owner_with_no_network(
    tmp_path: Path, no_sockets: list[str]
) -> None:
    instance, _owner, _reviewer = _create(tmp_path)
    assert no_sockets == []

    result = instance.base_kit_install
    assert result is not None
    assert (result.kit_id, result.version, result.status) == ("base", "1.0.0", "accepted")
    assert result.transition == "install"
    # Genesis stays generation zero; the base kit is the first change after it.
    history = instance.accepted_history()
    assert [item.sequence for item in history] == [0, 1]
    tree = _tree(instance)
    for path, content in bundled_base_kit().contents().items():
        assert tree[path] == content
    assert f"documents/{kit_receipt_document_id('base')}.json" in tree
    (installed,) = service_kit_status(instance).kits
    assert (installed.kit_id, installed.version, installed.source) == ("base", "1.0.0", "bundled")
    assert installed.content_digest == BASE_KIT_PIN.content_digest
    assert installed.providers == () and installed.drifted == ()
    # A local comparison with the bundled copy: no registry is asked.
    assert (installed.latest_available, installed.update_check) == ("1.0.0", "checked")


def test_under_independent_approval_creation_succeeds_and_the_kit_awaits_approval(
    tmp_path: Path, no_sockets: list[str]
) -> None:
    instance, _owner, reviewer = _create(tmp_path, independent=True)
    assert no_sockets == []

    result = instance.base_kit_install
    assert result is not None
    assert (result.status, result.approval_required) == ("proposed", True)
    assert result.proposal_id is not None
    assert [item.sequence for item in instance.accepted_history()] == [0]
    assert service_kit_status(instance).kits == ()
    assert capability_path("workspace.file", 1) not in _tree(instance)

    # A second principal's approval lands it like any kit add.
    challenge = service_prepare_playbill_approval(
        instance,
        proposal_id=result.proposal_id,
        signer_id="reviewer",
        access=BodyAccessContext(principal_id="reviewer", can_read_body=True),
    )
    statement = challenge.statement
    approval = _sign(reviewer, statement.payload_digest, statement.signing_semantic_root)
    service_submit_playbill_approval(
        instance,
        proposal_id=result.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="reviewer",
    )
    activated = service_activate_playbill_proposal(
        instance, proposal_id=result.proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"
    (installed,) = service_kit_status(instance).kits
    assert (installed.kit_id, installed.source) == ("base", "bundled")


def test_an_older_bundled_base_kit_shows_behind_and_kit_add_base_previews_the_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = _older_bundle(tmp_path / "older")
    monkeypatch.setattr(base_kit_module, "bundled_base_kit", lambda: older)
    instance, _owner, _reviewer = _create(tmp_path / "world")
    monkeypatch.undo()

    (installed,) = service_kit_status(instance).kits
    assert (installed.version, installed.latest_available, installed.update_check) == (
        "0.9.0",
        "1.0.0",
        "checked",
    )
    resolved = bundled_kit_reference("base")
    assert resolved is not None
    bundle, source = resolved
    preview = service_add_kit(
        instance,
        KitAddRequest(bundle=bundle, source=source),
        actor_id="owner",
        timestamp=STAMP,
    )
    assert (preview.status, preview.transition, preview.installed_version) == (
        "would_propose",
        "upgrade",
        "0.9.0",
    )
    assert preview.proposal_id is None
    added = {item.path for item in preview.plan if item.action == "add"}
    assert capability_path("web.render.dom", 1) in added


def _older_bundle(root: Path) -> Any:
    """A base kit 0.9.0 without ``web.render.dom``, rendered by the build script."""

    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[2] / "scripts" / "build_base_kit.py"
    spec = importlib.util.spec_from_file_location("build_base_kit_for_test", path)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = script
    spec.loader.exec_module(script)
    source = root / "source"
    shutil.copytree(script.SOURCE, source)
    shutil.rmtree(source / "capabilities" / "web.render.dom")
    for name in ("web.render.dom.request", "web.render.dom.result"):
        shutil.rmtree(source / "contracts" / name)
    kit = json.loads((source / "kit.json").read_bytes())
    (source / "kit.json").write_text(json.dumps({**kit, "version": "0.9.0"}))
    directory, pin_module = root / "base", root / "base_kit_pin.py"
    script.write(source, directory, pin_module)
    manifest = json.loads((directory / "cruxible-kit.json").read_bytes())
    from cruxible_client.contracts.kits import KitManifest

    digest = KitManifest.model_validate(manifest).content_digest
    return load_bundled_kit(
        directory, BundledKitPin(kit_id="base", version="0.9.0", content_digest=digest)
    )


# -- reserved names -----------------------------------------------------------------------


def _without_base_kit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    """An instance whose creation skipped the base kit (as if removed before it landed)."""

    monkeypatch.setattr(base_kit_module, "install_bundled_base_kit", lambda *a, **k: None)
    instance, owner, _reviewer = _create(tmp_path)
    monkeypatch.undo()
    assert capability_path("workspace.file", 1) not in _tree(instance)
    return instance, owner


def _squatter(tree: dict[str, bytes]) -> tuple[str, bytes]:
    """``workspace.file@1`` over the bundled Contracts, worded differently."""

    path = capability_path("workspace.file", 1)
    bundled = parse_capability(bundled_base_kit().contents()[path], path=path)
    other = validate_capability(
        {**bundled.model_dump(mode="json", by_alias=True), "description": "Reads any file."}
    )
    return path, render_capability(other)


def _bundled_contracts() -> dict[str, bytes]:
    return {
        path: content
        for path, content in bundled_base_kit().contents().items()
        if path.startswith("contracts/workspace.file.")
    }


def test_an_agent_authored_workspace_file_v1_is_refused_at_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _without_base_kit(tmp_path, monkeypatch)
    accept_tree(instance, _bundled_contracts(), name="file-contracts")
    base = instance.accepted_coordinate()
    path, content = _squatter(_tree(instance))
    proposed = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/squat", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**_tree(instance), path: content},
        timestamp=STAMP,
    )
    assert proposed.candidate is None
    assert "cruxible.capability.reserved" in {item.code for item in proposed.evaluation.diagnostics}


def test_an_agent_authored_workspace_file_v1_is_refused_at_authoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _without_base_kit(tmp_path, monkeypatch)
    accept_tree(instance, _bundled_contracts(), name="file-contracts")
    compiled = AuthoringIntentCoordinator.for_instance(instance).compile(
        actor=OWNER,
        payload=lower_authoring_input(  # type: ignore[arg-type]
            CapabilityInput.model_validate(
                {
                    "kind": "capability",
                    "name": "workspace.file",
                    "version": 1,
                    "description": "Reads any file.",
                    "effect": "pure",
                    "in": "workspace.file.request@1",
                    "out": "workspace.file.result@1",
                }
            )
        ),
        canonical_timestamp=STAMP,
    )
    assert compiled.verdict == "refused"
    (diagnostic, *_rest) = compiled.frontier.diagnostics
    assert diagnostic.code == "cruxible.capability.reserved"
    assert "kit add base" in diagnostic.repairs[0].description


def test_the_bundled_workspace_file_v1_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _without_base_kit(tmp_path, monkeypatch)
    result = install_bundled_base_kit(instance, actor_id="owner", at=_at(STAMP))
    assert result.status == "accepted"
    path = capability_path("workspace.file", 1)
    assert _tree(instance)[path] == bundled_base_kit().contents()[path]


def test_an_adopter_authors_workspace_file_v2(tmp_path: Path) -> None:
    instance, owner, _reviewer = _create(tmp_path)
    first_path = capability_path("workspace.file", 1)
    first = parse_capability(_tree(instance)[first_path], path=first_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    submitted = coordinator.compile_and_submit(
        actor=OWNER,
        payload=lower_authoring_input(  # type: ignore[arg-type]
            ChangeSetInput(
                kind="change_set",
                members=(
                    CapabilityInput.model_validate(
                        {
                            "kind": "capability",
                            "name": "workspace.file",
                            "version": 2,
                            "description": first.description + " Second wording.",
                            "effect": "pure",
                            "in": "workspace.file.request@1",
                            "out": "workspace.file.result@1",
                        }
                    ),
                    ContractInput(
                        kind="contract",
                        name="acme.unused",
                        version=1,
                        fields={"note": "string"},  # type: ignore[dict-item]
                    ),
                ),
            )
        ),
        canonical_timestamp=STAMP,
    )
    assert submitted.status.proposal_id is not None, submitted.status.model_dump(mode="json")
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner, submitted.status.candidate_digest, instance.accepted_coordinate().semantic_root
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"
    second_path = capability_path("workspace.file", 2)
    second = parse_capability(_tree(instance)[second_path], path=second_path)
    assert second.predecessor_digest == capability_digest(first).tagged


def _at(stamp: str) -> Any:
    from cruxible_client.contracts.temporal import parse_datetime

    return parse_datetime(stamp)


def test_init_says_whether_the_base_kit_landed_or_awaits_approval() -> None:
    from cruxible_client.contracts.kits import KitChangeResult
    from cruxible_core.runtime.playbill_api import _init_base_kit

    landed = _init_base_kit(KitChangeResult(kit_id="base", version="1.0.0", status="accepted"))
    assert landed is not None
    assert (landed.status, landed.proposal_id) == ("accepted", None)
    waiting = _init_base_kit(
        KitChangeResult(
            kit_id="base",
            version="1.0.0",
            status="proposed",
            proposal_id="sha256:" + "a" * 64,
            approval_required=True,
        )
    )
    assert waiting is not None
    assert (waiting.status, waiting.proposal_id) == ("proposed", "sha256:" + "a" * 64)
    assert _init_base_kit(None) is None


# -- an interrupted creation resumes on the next open -------------------------------------

_DIE_DURING_CREATION = """
import json, os, sys
from pathlib import Path

from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.kits import base_kit
from cruxible_core.runtime.instance import PlaybillInstance

root, point, independent = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "1"
managed = root / "managed"
keys = [
    generate_client_principal_key(
        root / f"{name}-key", principal_id=name, kind="ordinary", forbidden_roots=(managed,)
    )
    for name in ("owner", "reviewer")
]
original = base_kit.install_bundled_base_kit


def die(instance, **kwargs):
    # The process dies: before the add, or after it produced its proposal or receipt
    # but before the record was removed.
    found = {"trust_root": instance.trust_root.model_dump(mode="json")}
    if point == "after":
        found["proposal_id"] = original(instance, **kwargs).proposal_id
    (root / "died.json").write_text(json.dumps(found))
    os._exit(73)


base_kit.install_bundled_base_kit = die
PlaybillInstance.initialize(
    managed,
    instance_id="inst_base_kit",
    client_principals=tuple(key.principal for key in keys),
    workspace_roots=(),
    require_independent_approval=independent,
    timestamp="2026-08-10T12:00:00+00:00",
)
raise SystemExit("creation finished; the process was meant to die in it")
"""


def _die_during_creation(
    tmp_path: Path, *, point: str, independent: bool = False
) -> tuple[Path, Any, str | None]:
    import subprocess
    import sys

    from cruxible_client.contracts.types import TrustRoot

    repo = Path(__file__).resolve().parents[2]
    script = tmp_path / "die.py"
    script.write_text(_DIE_DURING_CREATION)
    completed = subprocess.run(
        [sys.executable, str(script), str(tmp_path), point, "1" if independent else "0"],
        cwd=repo,
        env={
            **os.environ,
            "PYTHONPATH": f"{repo / 'src'}:{repo / 'packages/cruxible-client/src'}",
        },
        capture_output=True,
        timeout=300,
    )
    assert completed.returncode == 73, completed.stderr.decode()[-2000:]
    died = json.loads((tmp_path / "died.json").read_text())
    managed = tmp_path / "managed"
    assert (managed / BASE_KIT_PENDING_FILE).is_file()
    return managed, TrustRoot.model_validate(died["trust_root"]), died.get("proposal_id")


def _open_proposals(instance: PlaybillInstance) -> list[str]:
    from cruxible_core.service.proposals.proposals import service_list_playbill_proposals

    listed = service_list_playbill_proposals(instance, status="open")
    return [entry.proposal_id for entry in listed.entries]


def test_a_creation_that_died_before_the_add_finishes_on_the_next_open(tmp_path: Path) -> None:
    managed, trust_root, _proposal = _die_during_creation(tmp_path, point="before")

    reopened = PlaybillInstance.open(managed, trust_root=trust_root)
    assert reopened.base_kit_install is not None
    assert reopened.base_kit_install.status == "accepted"
    assert [item.sequence for item in reopened.accepted_history()] == [0, 1]
    (installed,) = service_kit_status(reopened).kits
    assert (installed.kit_id, installed.source) == ("base", "bundled")
    assert not (managed / BASE_KIT_PENDING_FILE).exists()

    again = PlaybillInstance.open(managed, trust_root=trust_root)
    assert again.base_kit_install is None
    assert [item.sequence for item in again.accepted_history()] == [0, 1]


def test_a_creation_that_died_after_the_kit_landed_adds_nothing_more(tmp_path: Path) -> None:
    managed, trust_root, _proposal = _die_during_creation(tmp_path, point="after")

    reopened = PlaybillInstance.open(managed, trust_root=trust_root)
    assert reopened.base_kit_install is not None
    assert reopened.base_kit_install.status == "unchanged"
    assert [item.sequence for item in reopened.accepted_history()] == [0, 1]
    assert not (managed / BASE_KIT_PENDING_FILE).exists()


def test_a_creation_that_died_after_its_proposal_reuses_that_proposal(tmp_path: Path) -> None:
    managed, trust_root, proposal_id = _die_during_creation(
        tmp_path, point="after", independent=True
    )
    assert proposal_id is not None

    reopened = PlaybillInstance.open(managed, trust_root=trust_root)
    assert reopened.base_kit_install is not None
    assert (reopened.base_kit_install.status, reopened.base_kit_install.proposal_id) == (
        "proposed",
        proposal_id,
    )
    assert _open_proposals(reopened) == [proposal_id]
    assert [item.sequence for item in reopened.accepted_history()] == [0]
    assert not (managed / BASE_KIT_PENDING_FILE).exists()


def test_a_pending_proposal_under_independent_approval_is_not_duplicated_on_reopen(
    tmp_path: Path,
) -> None:
    instance, _owner, _reviewer = _create(tmp_path, independent=True)
    assert instance.base_kit_install is not None
    proposal_id = instance.base_kit_install.proposal_id
    assert not (instance.root / BASE_KIT_PENDING_FILE).exists()

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.base_kit_install is None
    assert _open_proposals(reopened) == [proposal_id]


def test_a_base_kit_removed_on_purpose_stays_removed_on_reopen(tmp_path: Path) -> None:
    from cruxible_client.contracts.kits import KitRemoveRequest
    from cruxible_core.service.kits import service_remove_kit

    instance, _owner, _reviewer = _create(tmp_path)
    removed = service_remove_kit(
        instance,
        KitRemoveRequest(kit_id="base", dry_run=False),
        actor_id="owner",
        timestamp=STAMP,
    )
    assert removed.status == "accepted", removed
    assert service_kit_status(instance).kits == ()
    history = [item.sequence for item in instance.accepted_history()]

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.base_kit_install is None
    assert service_kit_status(reopened).kits == ()
    assert [item.sequence for item in reopened.accepted_history()] == history

"""A kit that requires a kit: build leaves the required kit's definitions out, add
checks the required kit and every left-out pin, remove refuses under a dependent,
status shows each requirement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import pretty_canonical_bytes
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.kit_requires import KitRequirement
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitAutomationTemplate,
    KitBuildRequest,
    KitBundle,
    KitManifest,
    KitReceipt,
    KitRemoveRequest,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
)
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from cruxible_core.errors import RequestRefusedError
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.kits import (
    _read_receipt,
    _retirement,
    build_kit,
    service_add_kit,
    service_build_kit,
    service_kit_status,
    service_remove_kit,
)
from tests.core_support._identity_support import accept_tree
from tests.core_support._light_capture_support import JSON_DECODING, http_contract
from tests.core_support._subject_kind_support import keyed_subject_kind
from tests.core_support._support import initialize_local
from tests.test_e2e.test_skeleton_kev_epss import _claim_type, _key_type

STAMP = "2026-11-12T12:00:00.000000Z"
HIT = Contract.model_validate(
    {"name": "web.search-result", "version": 1, "fields": {"title": "string{max_len: 1024}"}}
)
OTHER = Contract.model_validate({"name": "web.other", "version": 1, "fields": {"n": "int{0..100}"}})
KIND = http_contract(name="web.page-capture", decoding=JSON_DECODING)


def _contract_files(*contracts: Contract) -> dict[str, bytes]:
    return {contract_path(c.name, c.version): render_contract(c) for c in contracts}


def _base(version: str = "1.0.0", *contracts: Contract, kind: bool = True) -> KitBundle:
    tree = _contract_files(*(contracts or (HIT, OTHER)))
    if kind:
        tree[capture_contract_path(KIND.identity.name)] = render_capture_contract(KIND)
    return build_kit(tree, KitBuildRequest(kit_id="base", version=version, owns=("web.",)))


def _instance(root: Path, *kits: KitBundle) -> PlaybillInstance:
    root.mkdir()
    instance, _ = initialize_local(root)
    for bundle in kits:
        assert _add(instance, bundle).status == "accepted"
    return instance


def _add(instance: PlaybillInstance, bundle: KitBundle, **options: object):
    return service_add_kit(
        instance,
        KitAddRequest(bundle=bundle, dry_run=False, **options),
        actor_id="owner",
        timestamp=STAMP,
    )


def _remove(instance: PlaybillInstance, kit_id: str):
    return service_remove_kit(
        instance,
        KitRemoveRequest(kit_id=kit_id, dry_run=False),
        actor_id="owner",
        timestamp=STAMP,
    )


def _build(instance: PlaybillInstance) -> KitBundle:
    return service_build_kit(
        instance, KitBuildRequest(kit_id="acme", version="0.1.0", owns=("acme.",))
    ).bundle


def _row(target: Contract, name: str = "acme.hit-row") -> Contract:
    """A Contract of this kit nesting ``target`` at its exact digest."""

    pin = {"name": target.name, "version": target.version, "digest": contract_digest(target).tagged}
    return Contract.model_validate(
        {
            "name": name,
            "version": 1,
            "fields": {"hit": {"type": {"kind": "contract", "contract": pin}}},
        }
    )


def _receipt(instance: PlaybillInstance, kit_id: str) -> KitReceipt:
    found = _read_receipt(
        instance, instance.tree_at(instance.accepted_coordinate().git_oid), kit_id
    )
    assert found is not None
    return found[1]


@pytest.fixture(scope="module")
def dependent(tmp_path_factory: pytest.TempPathFactory) -> KitBundle:
    """Kit acme 0.1.0, built where base 1.0.0 is installed: it nests base's Contract."""

    publisher = _instance(tmp_path_factory.mktemp("kit-requires") / "publisher", _base())
    accept_tree(publisher, _contract_files(_row(HIT)), name="row")
    return _build(publisher)


def test_build_leaves_the_required_kits_definitions_out_and_requires_it(
    dependent: KitBundle,
) -> None:
    assert dependent.manifest.requires == (KitRequirement(kit_id="base", min_version="1.0.0"),)
    assert sorted(dependent.contents()) == [contract_path("acme.hit-row", 1)]
    rendered = pretty_canonical_bytes(dependent.manifest.model_dump(mode="json"))
    assert (
        b'"requires": [\n    {\n      "kit_id": "base",\n      "min_version": "1.0.0"' in rendered
    )
    # The left-out pin stays in the release, at base's exact digest.
    assert (
        contract_digest(HIT).tagged.encode()
        in dependent.contents()[contract_path("acme.hit-row", 1)]
    )


def test_a_kit_that_requires_nothing_keeps_its_bytes_and_digest() -> None:
    manifest = KitManifest(
        kit_id="acme",
        version="1.2.3",
        owns=("acme.",),
        artifacts=[
            {"path": "contracts/acme.value/v1.json", "artifact_digest": "sha256:" + "1" * 64}
        ],
    )
    assert "requires" not in manifest.model_dump(mode="json")
    # Computed by the manifest model before ``requires`` existed (rc/0.5.0, 2141f366e).
    assert manifest.content_digest == (
        "sha256:5fac1d826fe3a2a8f7ffaab6ae6d3a97934af29be5c7db7c870faa6fd16f9e2f"
    )
    built = _base()
    assert built.manifest.requires == ()
    assert "requires" not in built.manifest.model_dump(mode="json")
    required = manifest.model_copy(
        update={"requires": (KitRequirement(kit_id="base", min_version="1.0.0"),)}
    )
    assert required.content_digest != manifest.content_digest


def test_build_refuses_a_left_out_pin_the_required_kit_does_not_hold(tmp_path: Path) -> None:
    publisher = _instance(tmp_path / "publisher", _base())
    extra = Contract.model_validate(
        {"name": "web.local-extra", "version": 1, "fields": {"n": "int{0..9}"}}
    )
    accept_tree(publisher, _contract_files(extra), name="extra")
    accept_tree(publisher, _contract_files(_row(extra)), name="row")
    with pytest.raises(RequestRefusedError) as refused:
        _build(publisher)
    assert refused.value.error_code == "cruxible.kit.requires_unresolved"
    assert "Contract:web.local-extra/v1" in str(refused.value)
    assert "kit base 1.0.0 does not hold it at the pinned digest" in str(refused.value)


def test_add_refuses_without_the_required_kit_and_accepts_beside_it(
    tmp_path: Path, dependent: KitBundle
) -> None:
    bare = _instance(tmp_path / "bare")
    with pytest.raises(RequestRefusedError) as missing:
        _add(bare, dependent)
    assert missing.value.error_code == "cruxible.kit.requires_missing"
    assert str(missing.value).endswith(
        "Kit acme 0.1.0 requires kit base 1.0.0 or later; this instance holds none. "
        "Add it first: cruxible kit add base."
    )
    assert missing.value.repair.arguments == {"kit": "base"}  # type: ignore[attr-defined]
    # A preview refuses the same way: nothing is proposed.
    with pytest.raises(RequestRefusedError):
        service_add_kit(bare, KitAddRequest(bundle=dependent), actor_id="owner", timestamp=STAMP)

    older = _instance(tmp_path / "older", _base("0.9.0"))
    with pytest.raises(RequestRefusedError) as behind:
        _add(older, dependent)
    assert behind.value.error_code == "cruxible.kit.requires_missing"
    assert "this instance holds base 0.9.0. Upgrade it first: cruxible kit add base." in str(
        behind.value
    )

    present = _instance(tmp_path / "present", _base())
    assert _add(present, dependent).status == "accepted"
    receipt = _receipt(present, "acme")
    assert receipt.requires == dependent.manifest.requires
    assert receipt.carried == ()
    # Base's receipt, which requires nothing, records no requires field at all.
    assert b"requires" not in pretty_canonical_bytes(
        _receipt(present, "base").model_dump(mode="json")
    )


def test_add_refuses_a_left_out_pin_this_instance_does_not_hold(
    tmp_path: Path, dependent: KitBundle
) -> None:
    # Base 1.0.0 is installed, but a release of it without the pinned Contract.
    stripped = _instance(tmp_path / "stripped", _base("1.0.0", OTHER))
    with pytest.raises(RequestRefusedError) as refused:
        _add(stripped, dependent)
    assert refused.value.error_code == "cruxible.kit.requires_unresolved"
    assert str(refused.value).endswith(
        "Kit acme 0.1.0 pins Contract:web.search-result/v1 from kit base; this instance "
        "does not hold it at the pinned digest."
    )


def test_a_name_reference_to_a_required_kind_resolves_live(tmp_path: Path) -> None:
    publisher = _instance(tmp_path / "publisher", _base())
    title = _claim_type(
        "acme.page.title",
        "string",
        allowed_subject_kinds=["acme.page"],
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="captured",
                    claim_roles=("observation",),
                    capture_contracts=(KIND.identity.name,),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )
    accept_tree(
        publisher,
        {
            subject_kind_path("acme.page"): render_subject_kind(
                keyed_subject_kind("acme.page", ("acme.page.url",))
            ),
            **{
                claim_type_path(t.predicate): render_claim_type(t)
                for t in (title, _key_type("acme.page.url", "acme.page"))
            },
        },
        name="vocabulary",
    )
    bundle = _build(publisher)
    assert bundle.manifest.requires == (KitRequirement(kit_id="base", min_version="1.0.0"),)
    assert not any(path.startswith("capture-contracts/") for path in bundle.contents())

    consumer = _instance(tmp_path / "consumer", _base())
    tree = consumer.tree_at(consumer.accepted_coordinate().git_oid)
    path = capture_contract_path(KIND.identity.name)
    accept_tree(consumer, {path: _retirement(path, tree[path])}, name="retire-kind")
    with pytest.raises(RequestRefusedError) as refused:
        _add(consumer, bundle)
    assert refused.value.error_code == "cruxible.kit.requires_unresolved"
    assert (
        "CaptureContract:web.page-capture from kit base; this instance does not hold it live"
        in (str(refused.value))
    )

    fresh = _instance(tmp_path / "fresh", _base())
    assert _add(fresh, bundle).status == "accepted"


def test_remove_refuses_a_required_kit_and_status_shows_requirements(
    tmp_path: Path, dependent: KitBundle
) -> None:
    consumer = _instance(tmp_path / "consumer", _base(), dependent)
    with pytest.raises(RequestRefusedError) as held:
        _remove(consumer, "base")
    assert held.value.error_code == "cruxible.kit.remove_required"
    assert str(held.value).endswith("Kit base cannot be removed: kit acme requires it.")
    assert held.value.repair.arguments == {"kit_id": "acme"}  # type: ignore[attr-defined]

    status = {kit.kit_id: kit for kit in service_kit_status(consumer).kits}
    assert set(status) == {"base", "acme"}
    assert status["base"].requires == ()
    [requirement] = status["acme"].requires
    assert requirement.model_dump() == {
        "kit_id": "base",
        "min_version": "1.0.0",
        "installed_version": "1.0.0",
        "met": True,
    }

    # Nothing stops a downgrade of base under its dependent; status says so.
    assert _add(consumer, _base("0.9.0"), allow_downgrade=True).status == "accepted"
    [behind] = {kit.kit_id: kit for kit in service_kit_status(consumer).kits}["acme"].requires
    assert (behind.installed_version, behind.met) == ("0.9.0", False)

    assert _remove(consumer, "acme").status == "accepted"
    assert _remove(consumer, "base").status == "accepted"
    assert service_kit_status(consumer).kits == ()


def test_the_automation_template_is_checked_beside_the_required_kits(tmp_path: Path) -> None:
    publisher = _instance(tmp_path / "publisher", _base())
    accept_tree(publisher, _contract_files(_row(HIT)), name="row")
    template = {
        "kind": "change_set",
        "members": [
            {"kind": "runbook", "name": "acme.start", "steps": [{"action": "evidence.sweep"}]}
        ],
    }
    bundle = service_build_kit(
        publisher,
        KitBuildRequest(
            kit_id="acme",
            version="0.1.0",
            owns=("acme.",),
            automation_template=KitAutomationTemplate(
                path="automation.json", content=json.dumps(template)
            ),
        ),
        template_actor_id="owner",
    ).bundle
    assert bundle.manifest.requires == (KitRequirement(kit_id="base", min_version="1.0.0"),)
    assert bundle.manifest.automation_template == "automation.json"

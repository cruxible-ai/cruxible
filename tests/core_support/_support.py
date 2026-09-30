"""Small custody-safe test helpers for Playbill bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.types import GitObjectFormat, PlaybillTrustRoot
from cruxible_core.governance.keys import GeneratedKeyMaterial, generate_client_principal_key
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._world_templates import TEMPLATES, Template, copy_template

FIXED_TIMESTAMP = "2026-08-10T12:00:00+00:00"


def generate_client(
    tmp_path: Path,
    *,
    managed_root: Path,
    principal_id: str,
    roles: tuple[str, ...],
) -> GeneratedKeyMaterial:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return generate_client_principal_key(
        tmp_path / f"client-custody-{principal_id}",
        principal_id=principal_id,
        kind="recovery" if roles == ("recovery",) else "ordinary",
        forbidden_roots=(workspace, managed_root),
    )


def initialize_local(
    tmp_path: Path,
    *,
    object_format: GitObjectFormat = "sha256",
) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    """Return a fresh genesis instance and its owner key under ``tmp_path``.

    The world is a copy of a session template when one applies (see
    `tests/core_support/_world_templates.py`); it is indistinguishable from a
    fresh `initialize_fresh` except that it shares keys with the same-ordinal
    world of other tests.
    """

    template = TEMPLATES.template(
        ("genesis", object_format),
        lambda root: TemplateWorld.capture(*initialize_fresh(root, object_format=object_format)),
    )
    if template is not None:
        opened = template_world(template, tmp_path)
        if opened is not None:
            return opened
    return initialize_fresh(tmp_path, object_format=object_format)


def initialize_fresh(
    tmp_path: Path,
    *,
    object_format: GitObjectFormat = "sha256",
) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    """Build a genesis instance from nothing: new keys, a new signed ledger."""

    managed_root = tmp_path / f"managed-{object_format}"
    owner = generate_client(
        tmp_path,
        managed_root=managed_root,
        principal_id="owner",
        roles=("owner",),
    )
    reviewer = generate_client(
        tmp_path,
        managed_root=managed_root,
        principal_id="reviewer",
        roles=("reviewer",),
    )
    instance = PlaybillInstance.initialize(
        managed_root,
        instance_id="inst_playbill_test",
        client_principals=(owner.principal, reviewer.principal),
        workspace_roots=(tmp_path / "workspace",),
        git_object_format=object_format,
        timestamp=FIXED_TIMESTAMP,
    )
    return instance, owner


@dataclass(frozen=True)
class TemplateWorld:
    """What a copy needs to reopen a templated instance and name its owner key."""

    managed: Path
    trust_root: PlaybillTrustRoot
    owner: GeneratedKeyMaterial
    owner_private: Path
    owner_public: Path

    @classmethod
    def capture(cls, instance: PlaybillInstance, owner: GeneratedKeyMaterial) -> TemplateWorld:
        root = instance.root.parent
        return cls(
            managed=instance.root.relative_to(root),
            trust_root=instance.trust_root,
            owner=owner,
            owner_private=owner.private_key_path.relative_to(root),
            owner_public=owner.public_key_path.relative_to(root),
        )


def template_world(
    template: Template[TemplateWorld], tmp_path: Path
) -> tuple[PlaybillInstance, GeneratedKeyMaterial] | None:
    """Copy ``template`` under ``tmp_path`` and reopen it, or ``None`` if it cannot."""

    copied = copy_template(template, tmp_path)
    if copied is None:
        return None
    TEMPLATES.copies += 1
    world = template.value
    instance = PlaybillInstance.open(copied / world.managed, trust_root=world.trust_root)
    restamp_proposal_index(instance)
    owner = GeneratedKeyMaterial(
        principal=world.owner.principal,
        private_key_path=tmp_path / world.owner_private,
        public_key_path=tmp_path / world.owner_public,
    )
    return instance, owner


def restamp_proposal_index(instance: PlaybillInstance) -> None:
    """Re-sync the proposal index checkpoint a copy invalidated.

    The checkpoint pins the device, inode and times of the directories it
    indexes, which no copy can keep. One read re-verifies the evidence and
    writes the copy's own checkpoint, leaving the state a fresh build leaves.
    """

    evidence = instance.proposal_evidence()
    if evidence.index is None or not (evidence.root / ".proposal-source.json").exists():
        return
    with evidence.index.read(evidence):
        pass


def client_material(
    tmp_path: Path,
    instance: PlaybillInstance,
    *,
    principal_id: str = "reviewer",
) -> GeneratedKeyMaterial:
    """Recover test-only custody metadata for a bootstrap client key."""

    key_directory = tmp_path / f"client-custody-{principal_id}"
    return GeneratedKeyMaterial(
        principal=instance._recovered.head.principals.require_active(principal_id),
        private_key_path=key_directory / f"{principal_id}.ed25519",
        public_key_path=key_directory / f"{principal_id}.ed25519.pub",
    )


def restamp_state_root(state_root: Path) -> None:
    """Restamp every instance a copied daemon state root registers.

    The daemon opens these itself; this throwaway open only rewrites each
    copy's proposal index checkpoint (see `restamp_proposal_index`).
    """

    trust_directory = state_root / "trust"
    for trust_path in sorted(trust_directory.glob("*.json")):
        managed = state_root / "instances" / trust_path.stem
        if not managed.is_dir():
            continue
        trust_root = PlaybillTrustRoot.model_validate_json(trust_path.read_bytes())
        restamp_proposal_index(PlaybillInstance.open(managed, trust_root=trust_root))

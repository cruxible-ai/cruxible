"""Direct regressions for the PC-DF2 reviewer reproduction probes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cruxible_client.contracts import laws as laws_module
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.laws import CLAIM_LAW_V3_IDENTIFIER, _artifact_law_coordinate
from cruxible_core.playbill import instance as instance_module
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV3,
    ClaimTypeMigrationPreflightV1,
    ClaimTypeMigrationRequestV3,
    ClaimTypeMigrationResultV3,
    service_migrate_claim_type,
)
from cruxible_core.playbill.compiler import (
    P2_B1_COMPILER,
    P2_B2_COMPILER,
    P2_B4_COMPILER,
    P2_B4_UNIT2_COMPILER,
    P2_C_COMPILER,
    PC_DF2_COMPILER,
    PC_HR_ARTIFACT_CODEC_COMPILERS,
    PC_HR_COMPILER,
    SUPPORTED_COMPILERS,
    current_compiler_coordinate,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import client_material
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claim_type_migrations import (
    _accepted_affects_package_world,
    _subject_valued_affects_package_successor,
)

ROOT = Path(__file__).resolve().parents[2]


def _genesis_replay_at_retained_compiler(
    instance: PlaybillInstance, tmp_path: Path, name: str
) -> None:
    clone = tmp_path / name
    shutil.copytree(instance.root, clone)
    checkpoint = PlaybillInstance._checkpoint_directory(clone)
    if checkpoint.exists():
        shutil.rmtree(checkpoint)
    script = textwrap.dedent(
        """
        import json, sys
        from pathlib import Path
        from cruxible_client.contracts.types import PlaybillTrustRoot
        from cruxible_core.playbill import compiler
        from cruxible_core.playbill.instance import PlaybillInstance

        assert compiler.current_compiler_coordinate() == compiler.P2_B4_UNIT2_COMPILER
        reopened = PlaybillInstance.open(
            Path(sys.argv[1]),
            trust_root=PlaybillTrustRoot.model_validate(json.loads(sys.argv[2])),
        )
        assert reopened.descriptor.compiler.model_dump(mode="json") == json.loads(sys.argv[4])
        assert reopened.accepted_coordinate().git_oid == sys.argv[3]
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "src"), str(ROOT / "packages" / "cruxible-client" / "src"))
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(clone),
            json.dumps(instance.trust_root.model_dump(mode="json")),
            instance.accepted_coordinate().git_oid,
            json.dumps(instance.descriptor.compiler.model_dump(mode="json")),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_rev15_and_rev12_remain_exact_codec_lineage_members() -> None:
    assert current_compiler_coordinate() == P2_B4_UNIT2_COMPILER
    assert P2_B4_UNIT2_COMPILER in SUPPORTED_COMPILERS
    assert P2_B4_UNIT2_COMPILER in PC_HR_ARTIFACT_CODEC_COMPILERS
    assert P2_B4_COMPILER in SUPPORTED_COMPILERS
    assert P2_B4_COMPILER in PC_HR_ARTIFACT_CODEC_COMPILERS
    assert P2_B2_COMPILER in SUPPORTED_COMPILERS
    assert P2_B2_COMPILER in PC_HR_ARTIFACT_CODEC_COMPILERS
    for retained in (PC_DF2_COMPILER, PC_HR_COMPILER):
        assert retained in SUPPORTED_COMPILERS
        assert retained in PC_HR_ARTIFACT_CODEC_COMPILERS


@pytest.mark.parametrize(
    "retained",
    [
        PC_HR_COMPILER,
        P2_B1_COMPILER,
        P2_C_COMPILER,
        PC_DF2_COMPILER,
        P2_B2_COMPILER,
        P2_B4_COMPILER,
    ],
)
def test_retained_codec_instance_stays_writable_and_replays_under_current_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retained
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(instance_module, "current_compiler_coordinate", lambda: retained)
    instance, claim_id, _owner = _accepted_affects_package_world(tmp_path)
    assert instance.descriptor.compiler == retained
    monkeypatch.undo()

    before = instance.accepted_coordinate().git_oid
    _accept(instance, _migration(instance, claim_id))
    assert instance.accepted_coordinate().git_oid != before
    assert instance.descriptor.compiler == retained
    _genesis_replay_at_retained_compiler(instance, tmp_path, f"clone-{retained.rule_digest[7:15]}")


def _migration(instance: PlaybillInstance, claim_id: str) -> ClaimTypeMigrationResultV3:
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="submit",
            successor=_subject_valued_affects_package_successor(instance),
            dependents=(
                ClaimTypeDependentDispositionV3(
                    identity=ArtifactIdentity(kind="Claim", name=claim_id),
                    disposition="retire",
                    claim_retirement_reason="was-rescinded",
                ),
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(result, ClaimTypeMigrationResultV3)
    return result


def _accept(instance: PlaybillInstance, result: ClaimTypeMigrationResultV3) -> None:
    proposal = result.proposal.proposal
    assert proposal.candidate is not None
    approval = _sign(
        client_material(instance.root.parent, instance),
        proposal.candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposal.admission.proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )


def test_release_level_law_bump_replays_rev8_tombstone_from_fresh_clone(
    tmp_path: Path,
) -> None:
    instance, claim_id, _owner = _accepted_affects_package_world(tmp_path)
    _accept(instance, _migration(instance, claim_id))
    clone = tmp_path / "fresh-rev9-clone"
    shutil.copytree(instance.root, clone)
    checkpoint = PlaybillInstance._checkpoint_directory(clone)
    if checkpoint.exists():
        shutil.rmtree(checkpoint)
    expected = instance.accepted_coordinate().git_oid
    trust_root = json.dumps(instance.trust_root.model_dump(mode="json"))
    script = textwrap.dedent(
        """
        from __future__ import annotations

        from dataclasses import replace
        import json
        from pathlib import Path
        import sys

        from cruxible_client.contracts import laws
        from cruxible_client.contracts.laws import InstalledAcceptanceLaw

        revision_9 = laws._artifact_law_coordinate(
            laws.CLAIM_LAW_V3_IDENTIFIER,
            "playbill-claim-v3",
            semantic_revision=9,
        )
        installed = tuple(laws.PLAYBILL_ACCEPTANCE_LAWS._by_coordinate.values())
        rev8 = next(item for item in installed if item.coordinate == laws.CLAIM_LAW_V3_REVISION_8)
        rev7 = next(item for item in installed if item.coordinate == laws.CLAIM_LAW_V3_REVISION_7)
        non_v3 = tuple(item for item in installed if item.artifact_tag != "playbill-claim-v3")
        laws.CLAIM_LAW_V3 = revision_9
        laws.PLAYBILL_ACCEPTANCE_LAWS = laws.AcceptanceLawRegistry(
            (
                *non_v3,
                InstalledAcceptanceLaw(
                    coordinate=revision_9,
                    artifact_kind="claim",
                    artifact_tag="playbill-claim-v3",
                ),
                replace(rev8, current=False),
                rev7,
            )
        )

        from cruxible_core.playbill import proposals
        from cruxible_client.contracts.types import PlaybillTrustRoot
        from cruxible_core.playbill.instance import PlaybillInstance

        assert proposals.CLAIM_LAW_V3_REVISION_8.digest == (
            "sha256:8aae4d764d32c52792d7ef2a81715c92d7c198b69cc74ec2f8882bcda0a16aa9"
        )
        assert proposals.CLAIM_LAW_V3_REVISION_8 != laws.CLAIM_LAW_V3
        reopened = PlaybillInstance.open(
            Path(sys.argv[1]),
            trust_root=PlaybillTrustRoot.model_validate(json.loads(sys.argv[2])),
        )
        assert reopened.accepted_coordinate().git_oid == sys.argv[3]
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "src"), str(ROOT / "packages" / "cruxible-client" / "src"))
    )
    subprocess.run(
        [sys.executable, "-c", script, str(clone), trust_root, expected],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_probe_reopen_after_law_bump_survives_checkpoint_and_genesis_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, claim_id, _owner = _accepted_affects_package_world(tmp_path)
    result = _migration(instance, claim_id)
    _accept(instance, result)
    revision_9 = _artifact_law_coordinate(
        CLAIM_LAW_V3_IDENTIFIER, "playbill-claim-v3", semantic_revision=9
    )
    monkeypatch.setattr(laws_module, "CLAIM_LAW_V3", revision_9)

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    assert reopened.refresh() == instance.accepted_coordinate()
    checkpoint = PlaybillInstance._checkpoint_directory(instance.root)
    assert checkpoint.exists()
    shutil.rmtree(checkpoint)
    genesis_replay = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert genesis_replay.accepted_coordinate() == instance.accepted_coordinate()
    assert (
        parse_claim(
            genesis_replay.tree_at(genesis_replay.accepted_coordinate().git_oid)[
                claim_path(claim_id)
            ],
            path=claim_path(claim_id),
        ).lifecycle.state
        == "retired"
    )


def test_probe_double_migration_rederives_an_existing_tombstone(tmp_path: Path) -> None:
    instance, claim_id, _owner = _accepted_affects_package_world(tmp_path)
    result = _migration(instance, claim_id)
    _accept(instance, result)
    path = claim_type_path("sec.vuln.affects_package")
    current = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    values = current.model_dump(mode="json")
    for mechanical in ("artifact_format", "identity", "lifecycle", "subject_scope", "slot_policy"):
        values.pop(mechanical, None)
    values["anticipated_source_ids"] = tuple(values.get("anticipated_source_ids") or ()) + (
        "extra-source",
    )
    second = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="preflight", successor=ClaimTypeInputV1.model_validate(values)
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(second, ClaimTypeMigrationPreflightV1)
    assert any(item.identity.name == claim_id for item in second.dependents)

    submitted = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV3(
            mode="submit",
            successor=ClaimTypeInputV1.model_validate(values),
            dependents=tuple(
                ClaimTypeDependentDispositionV3(
                    identity=item.identity,
                    disposition="successor",
                )
                for item in second.dependents
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(submitted, ClaimTypeMigrationResultV3)
    _accept(instance, submitted)
    tombstone = parse_claim(
        instance.tree_at(instance.accepted_coordinate().git_oid)[claim_path(claim_id)],
        path=claim_path(claim_id),
    )
    assert tombstone.lifecycle.state == "retired"

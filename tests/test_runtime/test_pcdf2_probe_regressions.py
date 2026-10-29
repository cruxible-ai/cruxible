"""Direct regressions for the PC-DF2 reviewer reproduction probes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from cruxible_client.contracts import laws as laws_module
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.laws import CLAIM_LAW_V3_IDENTIFIER, _artifact_law_coordinate
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDisposition,
    ClaimTypeMigrationPreflightV1,
    ClaimTypeMigrationRequest,
    ClaimTypeMigrationResultV3,
    service_migrate_claim_type,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.core_support._support import client_material
from tests.test_claims.test_claim_type_migrations import (
    _accepted_affects_package_world,
    _subject_valued_affects_package_successor,
)
from tests.test_ledger.test_activation import _sign

ROOT = Path(__file__).resolve().parents[2]


def _migration(
    instance: PlaybillInstance,
    claim_id: str,
    *,
    successor: ClaimTypeInputRecord | ClaimType | None = None,
) -> ClaimTypeMigrationResultV3:
    result = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequest(
            mode="submit",
            successor=(
                _subject_valued_affects_package_successor(instance)
                if successor is None
                else successor
            ),
            dependents=(
                ClaimTypeDependentDisposition(
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
    instance.settled_workspace_advertisement()
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

        # The next release bumps the current Claim v3 law; every installed
        # revision, including today's current one, stays for replay.
        next_revision = laws._artifact_law_coordinate(
            laws.CLAIM_LAW_V3_IDENTIFIER,
            "playbill-claim-v3",
            semantic_revision=99,
        )
        installed = tuple(laws.ACCEPTANCE_LAWS._by_coordinate.values())
        v3 = tuple(
            replace(item, current=False)
            for item in installed
            if item.artifact_tag == "playbill-claim-v3"
        )
        non_v3 = tuple(item for item in installed if item.artifact_tag != "playbill-claim-v3")
        laws.CLAIM_LAW_V3 = next_revision
        laws.ACCEPTANCE_LAWS = laws.AcceptanceLawRegistry(
            (
                *non_v3,
                InstalledAcceptanceLaw(
                    coordinate=next_revision,
                    artifact_kind="claim",
                    artifact_tag="playbill-claim-v3",
                ),
                *v3,
            )
        )

        from cruxible_core.proposals import proposals
        from cruxible_client.contracts.types import TrustRoot
        from cruxible_core.runtime.instance import PlaybillInstance

        assert proposals.CLAIM_LAW_V3_REVISION_8.digest == (
            "sha256:8aae4d764d32c52792d7ef2a81715c92d7c198b69cc74ec2f8882bcda0a16aa9"
        )
        assert proposals.CLAIM_LAW_V3_REVISION_8 != laws.CLAIM_LAW_V3
        reopened = PlaybillInstance.open(
            Path(sys.argv[1]),
            trust_root=TrustRoot.model_validate(json.loads(sys.argv[2])),
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
        request=ClaimTypeMigrationRequest(
            mode="preflight", successor=ClaimTypeInputRecord.model_validate(values)
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    assert isinstance(second, ClaimTypeMigrationPreflightV1)
    assert any(item.identity.name == claim_id for item in second.dependents)

    submitted = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequest(
            mode="submit",
            successor=ClaimTypeInputRecord.model_validate(values),
            dependents=tuple(
                ClaimTypeDependentDisposition(
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

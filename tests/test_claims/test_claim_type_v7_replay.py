"""History made before ClaimType v7 replays exactly after it (spec 8.2, 8.3, 8.9).

A child process builds a ledger with the acceptance laws a pre-v7 daemon
installs: Claim law v2 revision 7 / v3 revision 9 current, no ClaimType v7 law.
It holds ClaimType v5 and v6 creations and revisions, accumulating Claim
revisions, a ClaimType succession carry, an upgrade-evidence-rules carry, an
origin-only Claim, and one proposal left pending. This build then replays it
from genesis, settles the pending proposal under its recorded law, accepts a v7
upgrade and a replacing revision, and replays again.

Two mutation probes show the replay is load-bearing: a daemon without the new
coordinates fails closed at ``require_historical``, and letting origin-only
evidence support under the v1 adjudication rule makes replay diverge.

(The same scenario against the real pre-v7 code runs as a walk probe; it cannot
live in the suite, which has only this build.)
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.claim_type_upgrade import ClaimTypeUpgradeRequestV1
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.laws import (
    CLAIM_LAW_V2_REVISION_7,
    CLAIM_LAW_V2_REVISION_8,
    CLAIM_TYPE_LAW_V7_REVISION_1,
)
from cruxible_client.contracts.types import PlaybillTrustRoot
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.proposals.settlement import parse_change_set_record
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.claims.claim_type_upgrade import service_upgrade_claim_types
from tests.test_claims.test_claim_type_v7_revisions import _own_words, _V7World
from tests.test_claims.test_identity_evidence_rules import PREDICATE

ROOT = Path(__file__).resolve().parents[2]
PRE_V7_LAWS = textwrap.dedent(
    """
    from dataclasses import replace

    from cruxible_client.contracts import laws

    new = {
        laws.CLAIM_LAW_V2_REVISION_8,
        laws.CLAIM_LAW_V3_REVISION_10,
        laws.CLAIM_TYPE_LAW_V7_REVISION_1,
    }
    old_current = {laws.CLAIM_LAW_V2_REVISION_7, laws.CLAIM_LAW_V3_REVISION_9}
    laws.PLAYBILL_ACCEPTANCE_LAWS = laws.AcceptanceLawRegistry(
        tuple(
            replace(item, current=True) if item.coordinate in old_current else item
            for item in laws.PLAYBILL_ACCEPTANCE_LAWS._by_coordinate.values()
            if item.coordinate not in new
        )
    )
    """
)
BUILD = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
    from cruxible_client.contracts.captures import (
        capture_contract_path,
        foreign_source_capture_contract,
        render_capture_contract,
    )
    from cruxible_client.contracts.claim_types import claim_type_digest, claim_type_path
    from cruxible_client.contracts.claim_types import render_claim_type
    from cruxible_client.contracts.evidence_rule_upgrade import EvidenceRuleUpgradeRequestV1
    from cruxible_core.service.claims.evidence_rule_upgrade import service_upgrade_evidence_rules
    from tests.test_claims.test_claim_type_v7_revisions import _selection, _V7World
    from tests.test_claims.test_identity_evidence_rules import (
        ORIGINAL, _digest, _digest_rule, _v5_type, _v6_type,
    )

    root = Path(sys.argv[1])
    world = _V7World(root)
    v5 = _v5_type(_digest_rule(_digest(ORIGINAL)))
    world.seed(v5)
    note = "project.work_item.owner_note"
    v6 = _v6_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=note),
            "predicate": note,
            "literal_schema": {"type": "string"},
        }
    )
    tree = world.tree()
    tree[claim_type_path(note)] = render_claim_type(v6)
    other = foreign_source_capture_contract("repo.other")
    tree[capture_contract_path(other.identity.name)] = render_capture_contract(other)
    world.accept(tree, name="v6-create")
    status = world.say(b"status: ready")
    world.say(b"status: done", value="done", claim_ref=status)
    # Evidence no rule admits: it enters the verdict as origin-only.
    import base64, hashlib
    from cruxible_client.contracts.authoring.models import (
        WorkingAnchorWindowV1, WorkingDigestCoordinateV1, WorkingSelectionObservationV1,
    )
    text = b"status: blocked"
    digest = "sha256:" + hashlib.sha256(text).hexdigest()
    elsewhere = WorkingSelectionObservationV1(
        source_id="repo.other",
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=digest, source_byte_length=len(text)
        ),
        selected_content_base64=base64.b64encode(text).decode("ascii"),
        selected_bytes_digest=digest,
        selector=WorkingAnchorWindowV1(
            anchor=text.decode("ascii"),
            start_byte=0,
            end_byte=len(text),
            observed_occurrence_count=1,
        ),
    )
    candidate, _path = world.author(elsewhere, value="blocked", predicate=note)
    world.accept(candidate, name="origin-only")
    current = world.claim_type()
    world.succeed(current.model_copy(update={
        "permitted_roles": ("normative", "observation"),
        "literal_schema": {"enum": ["blocked", "done", "ready", "shipped"], "type": "string"},
        "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged),
    }))
    upgraded = service_upgrade_evidence_rules(
        world.instance,
        request=EvidenceRuleUpgradeRequestV1(dry_run=False),
        actor_id="owner",
        timestamp=world.timestamp(),
    )
    assert upgraded.status == "proposed", upgraded
    world.activate_proposal(upgraded.proposal_id)
    # Judged under Claim law v2 revision 7 and left for the next daemon to settle.
    pending_tree, _path = world.author(_selection(b"status: ready again"), claim_ref=status)
    pending = world.propose(pending_tree, name="pending")
    assert pending.candidate is not None
    (root / "state.json").write_text(json.dumps({
        "trust_root": world.instance.trust_root.model_dump(mode="json"),
        "managed_root": str(world.instance.root),
        "status": status,
        "pending": pending.admission.proposal_id,
    }))
    """
)
REOPEN = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    from cruxible_client.contracts.types import PlaybillTrustRoot
    from cruxible_core.runtime.instance import PlaybillInstance

    state = json.loads(Path(sys.argv[1]).read_text())
    PlaybillInstance.open(
        Path(sys.argv[2]), trust_root=PlaybillTrustRoot.model_validate(state["trust_root"])
    )
    """
)
DROP_HISTORICAL_CLAIM_LAWS = textwrap.dedent(
    """
    from cruxible_client.contracts import laws

    historical = {laws.CLAIM_LAW_V2_REVISION_7, laws.CLAIM_LAW_V3_REVISION_9}
    laws.PLAYBILL_ACCEPTANCE_LAWS = laws.AcceptanceLawRegistry(
        tuple(
            item
            for item in laws.PLAYBILL_ACCEPTANCE_LAWS._by_coordinate.values()
            if item.coordinate not in historical
        )
    )
    """
)
ORIGIN_SUPPORTS_UNDER_V1 = textwrap.dedent(
    """
    from cruxible_client.contracts import claim_verdicts

    claim_verdicts._admitted_kinds = lambda _rule: frozenset(
        {"origin_only", "direct", "derivational"}
    )
    """
)


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT), str(ROOT / "src"), str(ROOT / "packages" / "cruxible-client" / "src"))
    )
    environment.pop("PYTEST_CURRENT_TEST", None)
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )


def _drop_checkpoints(managed: Path) -> None:
    checkpoint = PlaybillInstance._checkpoint_directory(managed)
    if checkpoint.exists():
        shutil.rmtree(checkpoint)


def _attach(instance: PlaybillInstance) -> _V7World:
    world = _V7World.__new__(_V7World)
    world.instance = instance
    world._clock = itertools.count(40)
    world._claims = itertools.count(500)
    tokens = itertools.count(5000)
    world.coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: f"{next(tokens):032x}",
        ),
        claim_id_factory=lambda: f"CLM-{next(world._claims):032x}",
    )
    return world


def _law_digests(instance: PlaybillInstance, prefix: str) -> set[str]:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    found: set[str] = set()
    for path in (item for item in tree if item.startswith("changesets/")):
        record = parse_change_set_record(tree[path], path=path)
        found.update(
            item.law_digest
            for item in getattr(record, "law_evidence", ())
            if item.path.startswith(prefix)
        )
    return found


def _copy(managed: Path, target: Path) -> Path:
    shutil.copytree(managed, target)
    _drop_checkpoints(target)
    return target


def test_pre_v7_history_replays_settles_and_extends_under_the_new_laws(tmp_path: Path) -> None:
    built = _run(PRE_V7_LAWS + BUILD, str(tmp_path))
    assert built.returncode == 0, built.stderr[-4000:]
    state = json.loads((tmp_path / "state.json").read_text())
    managed = Path(state["managed_root"])
    trust = PlaybillTrustRoot.model_validate(state["trust_root"])

    _drop_checkpoints(managed)
    instance = PlaybillInstance.open(managed, trust_root=trust)
    claim_laws = _law_digests(instance, "claims/")
    assert CLAIM_LAW_V2_REVISION_7.digest in claim_laws
    assert CLAIM_LAW_V2_REVISION_8.digest not in claim_laws

    # 8.9: the proposal judged under revision 7 settles on this daemon.
    world = _attach(instance)
    world.activate_proposal(state["pending"])
    status = state["status"]
    before = world.claim(status)
    assert len(before.backing.capture_digests) == 3

    upgraded = service_upgrade_claim_types(
        instance,
        request=ClaimTypeUpgradeRequestV1(claim_types=(PREDICATE,), dry_run=False),
        actor_id="owner",
        timestamp=world.timestamp(),
    )
    assert upgraded.status == "proposed", upgraded
    world.activate_proposal(upgraded.proposal_id)  # type: ignore[arg-type]
    type_path = claim_type_path(PREDICATE)
    assert parse_claim_type(world.tree()[type_path], path=type_path).revision_evidence == (
        "replace"
    )
    assert world.claim(status).backing == before.backing
    world.say(b"status: shipped", value="shipped", claim_ref=status)
    assert len(world.claim(status).backing.capture_digests) == 1
    # A none ClaimType, so an origin-supported verdict is on the record too.
    current = world.claim_type()
    world.succeed(
        current.model_copy(
            update={
                "evidence_requirement": "none",
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=claim_type_digest(current).tagged
                ),
            }
        )
    )
    tree, _path = world.author(
        _own_words(b"status: done\n"), value="done", claim_ref=status, citation_role=None
    )
    world.accept(tree, name="own-words")
    assert world.verdict(status) == "supported"
    assert CLAIM_TYPE_LAW_V7_REVISION_1.digest in _law_digests(instance, "claim-types/")
    assert CLAIM_LAW_V2_REVISION_8.digest in _law_digests(instance, "claims/")

    head = instance.accepted_coordinate()
    _drop_checkpoints(managed)
    replayed = PlaybillInstance.open(managed, trust_root=trust)
    assert replayed.accepted_coordinate() == head
    path = claim_path(status)
    assert parse_claim(replayed.tree_at(head.git_oid)[path], path=path) == world.claim(status)

    # 8.2: a daemon without the new coordinates fails closed on this ledger.
    old = _run(
        PRE_V7_LAWS + REOPEN,
        str(tmp_path / "state.json"),
        str(_copy(managed, tmp_path / "pre-v7-reopen")),
    )
    assert old.returncode != 0
    assert "cannot be reproduced at its recorded digest" in old.stderr, old.stderr[-4000:]

    # 8.3 (a): dropping the historical Claim coordinates fails at require_historical.
    dropped = _run(
        DROP_HISTORICAL_CLAIM_LAWS + REOPEN,
        str(tmp_path / "state.json"),
        str(_copy(managed, tmp_path / "dropped-reopen")),
    )
    assert dropped.returncode != 0
    assert "cannot be reproduced at its recorded digest" in dropped.stderr, dropped.stderr[-4000:]

    # 8.3 (b): origin-only evidence supporting under the v1 rule diverges.
    leaked = _run(
        ORIGIN_SUPPORTS_UNDER_V1 + REOPEN,
        str(tmp_path / "state.json"),
        str(_copy(managed, tmp_path / "leaked-reopen")),
    )
    assert leaked.returncode != 0
    assert "diverged" in leaked.stderr or "does not reproduce" in leaked.stderr, leaked.stderr[
        -4000:
    ]

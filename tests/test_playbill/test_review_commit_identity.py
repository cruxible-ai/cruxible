"""Hash-only review identities reproduce Git without creating dangling commits."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.errors import PlaybillGitError
from cruxible_core.playbill.git import GitLedger


@pytest.fixture(params=("sha1", "sha256"))
def ledger(tmp_path, request):
    result = GitLedger.initialize(
        tmp_path / "ledger.git",
        object_format=request.param,
        signing_key_path=tmp_path / "unused-key",
        allowed_signers_path=tmp_path / "unused-signers",
    )
    tree = result._git(["mktree"], input_bytes=b"").decode().strip()
    base = result._git(["commit-tree", tree, "-m", "base"]).decode().strip()
    result._git(["update-ref", "refs/heads/main", base])
    return result


@pytest.mark.parametrize(
    "actor,timestamp,message,encoding",
    [
        ("owner", "2026-08-11T12:30:00.000000Z", "Summary\n", "UTF-8"),
        ("owner", "2026-08-11T12:30:00.999999Z", "Summary\n", "UTF-8"),
        ("owner", "2026-08-11T12:30:00+00:00", "Summary", "UTF-8"),
        ("owner", "2026-08-11T15:00:00+02:30", "Summary\n\nBody\n", "UTF-8"),
        ("owner.", "2026-08-11T12:30:00.000000Z", "Summary\n\n\n", "UTF-8"),
        ("owner_-.", "2026-08-11T12:30:00.000000Z", "A café and 雪\n", "UTF-8"),
        ("owner", "2026-08-11T12:30:00.000000Z", "A café\n", "ISO-8859-1"),
        ("owner", "2026-08-11T12:30:00.000000Z", "Summary\n", "utf8"),
    ],
)
def test_hash_only_matches_real_git_identity_and_prose(ledger, actor, timestamp, message, encoding):
    ledger._git(["config", "i18n.commitEncoding", encoding])
    base = ledger.read_main()
    tree = ledger.tree_oid(base)
    before = ledger.unreachable_commits()
    args = dict(tree_oid=tree, base_oid=base, actor_id=actor, timestamp=timestamp, message=message)
    derived = ledger.proposal_review_commit_oid(**args)
    assert not ledger.object_exists(derived)
    assert ledger.unreachable_commits() == before
    assert ledger.proposal_review_commit(**args) == derived


@pytest.mark.parametrize(
    "timestamp,message",
    [
        ("not-a-date", "Summary\n"),
        ("2026-08-11T12:30:00.000000Z", ""),
        ("2026-08-11T12:30:00.000000Z", " \n\t"),
        ("2026-08-11T12:30:00.000000Z", "before\x00after"),
    ],
)
def test_hash_only_preserves_git_and_message_refusals(ledger, timestamp, message):
    base = ledger.read_main()
    args = dict(
        tree_oid=ledger.tree_oid(base),
        base_oid=base,
        actor_id="owner",
        timestamp=timestamp,
        message=message,
    )
    with pytest.raises(PlaybillGitError):
        ledger.proposal_review_commit_oid(**args)
    with pytest.raises(PlaybillGitError):
        ledger.proposal_review_commit(**args)
    assert ledger.unreachable_commits() == ()

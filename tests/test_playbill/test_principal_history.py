"""Principal registry snapshot parsing and exact-root lookup tests."""

from __future__ import annotations

import pytest

from cruxible_core.playbill.bootstrap import render_principal
from cruxible_core.playbill.errors import PrincipalIntegrityError
from cruxible_core.playbill.principals import principal_registry_from_tree
from cruxible_core.playbill.types import PrincipalRecord

ROOT = "sha256:" + "11" * 32


def _principal(principal_id: str, roles: tuple[str, ...]) -> PrincipalRecord:
    return PrincipalRecord(
        principal_id=principal_id,
        public_key=(principal_id.encode().hex() + "00" * 32)[:64],
        authority_roles=roles,
    )


def test_registry_replays_canonical_principals_at_exact_root() -> None:
    daemon = _principal("daemon", ("daemon",))
    owner = _principal("owner", ("owner",))
    snapshot = principal_registry_from_tree(
        {
            "principals/daemon.yaml": render_principal(daemon),
            "principals/owner.yaml": render_principal(owner),
            "documents/ignored.yaml": b"{}\n",
        },
        semantic_root=ROOT,
    )

    assert snapshot.require_active("owner") == owner
    assert snapshot.key_history_reference("owner") == f"principals/owner.yaml@{ROOT}"


def test_registry_refuses_path_substitution_and_noncanonical_bytes() -> None:
    daemon = _principal("daemon", ("daemon",))
    owner = _principal("owner", ("owner",))
    with pytest.raises(PrincipalIntegrityError, match="path and identity"):
        principal_registry_from_tree(
            {
                "principals/daemon.yaml": render_principal(daemon),
                "principals/other.yaml": render_principal(owner),
            },
            semantic_root=ROOT,
        )
    with pytest.raises(PrincipalIntegrityError, match="not canonical"):
        principal_registry_from_tree(
            {
                "principals/daemon.yaml": render_principal(daemon),
                "principals/owner.yaml": render_principal(owner).replace(b":", b": ", 1),
            },
            semantic_root=ROOT,
        )

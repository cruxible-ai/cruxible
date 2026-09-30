"""Who reads operational state, and which arming credentials they may see.

A Line arm records who armed it: the local operator, or a runtime credential
by id and label. Principal lists expose accepted principals, and whoami only
the caller's own label, while enumerating credentials takes ADMIN. So a
runtime credential's id and label appear on a Line or run card only for that
credential itself or an admin; every other reader sees only the principal kind.
"""

from __future__ import annotations

from dataclasses import dataclass

from cruxible_client.contracts.line_dispatch import LineArmPrincipalV1


@dataclass(frozen=True)
class OperationalViewer:
    """Who reads operational state, as the transport authenticated them.

    ``credential_id`` is the caller's runtime credential, if any; ``admin`` is
    an ADMIN-tier caller, who may already enumerate every credential. ``None``
    in place of a viewer is an unauthenticated read and sees no credential.
    """

    credential_id: str | None
    admin: bool

    def may_see(self, principal: LineArmPrincipalV1) -> bool:
        if principal.kind == "local_operator":
            # The local operator is one fixed identity with no credential.
            return True
        return self.admin or (
            self.credential_id is not None and self.credential_id == principal.credential_id
        )


def may_see_arming(viewer: OperationalViewer | None, principal: LineArmPrincipalV1) -> bool:
    """Whether this reader may see who armed a Line: its runtime credential id and label."""

    if principal.kind == "local_operator":
        return True
    return viewer is not None and viewer.may_see(principal)


__all__ = ["OperationalViewer", "may_see_arming"]

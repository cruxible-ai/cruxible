"""Who reads operational state, and which arming credentials they may see.

A Line arm records who armed it: the local operator, or a runtime credential
by id and label. Principal lists expose accepted principals, and whoami only
the caller's own label, while enumerating credentials takes ADMIN. So a
runtime credential's id and label appear on a Line or run card only for an
admin, that credential itself, or another bearer credential bound to the same
principal (the credential acts as that principal, and a rotation replaces its
id); every other reader sees only the principal kind. An unbound credential or
an unauthenticated principal claim never sees another credential.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from cruxible_client.contracts.line_dispatch import (
    LineArmPrincipalV1,
    is_current_arm_principal_record,
)
from cruxible_client.contracts.operational_reads import PlaybillArmPrincipalKind


@dataclass(frozen=True)
class OperationalViewer:
    """Who reads operational state, as the transport authenticated them.

    ``credential_id`` is the caller's runtime credential, if any; ``admin`` is
    an ADMIN-tier caller, who may already enumerate every credential. ``None``
    in place of a viewer is an unauthenticated read and sees no credential.

    ``principal_id`` is the principal the caller's bearer credential is bound
    to, and ``credential_principal`` resolves an arming credential's bound
    principal; both stay ``None`` for an unbound credential or a claim, so
    they never widen what such a caller sees.
    """

    credential_id: str | None
    admin: bool
    principal_id: str | None = None
    credential_principal: Callable[[str], str | None] | None = field(
        default=None, compare=False, repr=False
    )

    def may_see(self, principal: LineArmPrincipalV1) -> bool:
        if principal.kind != "runtime_credential":
            # The local operator is one fixed identity with no credential, and
            # a claimed principal's label is its principal ID, which principal
            # lists already show; only a credential's id and label are withheld.
            return True
        if self.admin:
            return True
        arming = principal.credential_id
        if arming is None:
            return False
        if self.credential_id is not None and self.credential_id == arming:
            return True
        if self.principal_id is None or self.credential_principal is None:
            return False
        return self.credential_principal(arming) == self.principal_id


def may_see_arming(viewer: OperationalViewer | None, principal: LineArmPrincipalV1) -> bool:
    """Whether this reader may see who armed a Line: its runtime credential id and label."""

    if principal.kind != "runtime_credential":
        return True
    return viewer is not None and viewer.may_see(principal)


def arm_principal_kind(record: object, principal: LineArmPrincipalV1) -> PlaybillArmPrincipalKind:
    """The kind a card shows for a persisted ``armed_by``.

    A record persisted before arms named their provenance parses under the
    current model with a defaulted tag, so it would read as the implicit local
    operator; it is shown as ``unverified`` instead, as dispatch treats it.
    """

    return principal.kind if is_current_arm_principal_record(record) else "unverified"


__all__ = ["OperationalViewer", "arm_principal_kind", "may_see_arming"]

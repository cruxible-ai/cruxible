"""Exact preview for every change (rule R12): one vocabulary on every surface.

Every operation that changes governed state, operational state or anything
outside the daemon takes ``dry_run`` (``--dry-run`` on the CLI). A dry run takes
the change's own path up to the commit, writes nothing anywhere, and answers in
the change's own outcome shape with a ``would_*`` status, pinned to the
accepted ``coordinate`` it was evaluated at.

``at`` carries that coordinate back. A commit carrying it refuses
``playbill.preview.state_moved`` when accepted state moved since the preview;
the check runs again where the change commits, under the lock its write holds.
A change to state outside the accepted ledger (a runtime credential, a host's
worktree binding, a page's projection markers) answers instead with a
``StateCoordinate``: a digest of exactly the records it changes.

Defaults, per operation:

- a change the server derives across several artifacts (installing or removing
  a kit, upgrading ClaimTypes or evidence rules) previews unless ``dry_run`` is
  false;
- a change that cannot be undone (decommissioning, revoking or rotating a
  credential, binding a ledger mirror) previews unless ``dry_run`` is false, and
  then commits only with ``at``: the confirmation is the preview's coordinate;
- everything else commits unless ``dry_run`` is true.

Which operations preview follows one principle (r12-principle-1002): an
operation previews when its effect is derived (computed by the server from more
than its input), or when it is irreversible or reaches outside the daemon and
its effect is not already shown to the caller. An operation whose full effect
is determined by its input and that writes nothing when refused is exempt:

- claim attest: the signed statement is the whole effect;
- body store: an inert content-addressed put;
- proposal approve: signs an evaluation ``proposal inspect`` already shows;
- proposal activate: its preview is the proposal's evaluation, already shown,
  and the commit-time ``at`` check covers head movement;
- workspace floor-delivery (on/off): its effect is its input;
- floor deliver-now: its result is fully determined by the accepted head (the
  floor is a pure function of the accepted coordinate), it is idempotent, and
  it writes only the derived, regenerable ``.playbill/floor``.

Exempt in v1 as well (the maintainer's scope ruling, r12-scope-1001), and so
taking no ``dry_run``:

- the exhaust paths (settle, predict, Procedure run and measure, Line
  evaluate, dispatch and run): append-only observations that need a separate
  dry-run-execution feature;
- client-local writes (context connect/use/clear, kit build/pull, hook): they
  change only the caller's own configuration or output files;
- init (genesis) and server stop/restart: there is no coordinate to preview
  against.

Provider installation is a labelled v1 exception: its preview validates and
writes nothing but does not run the whole install, and its outcome says so
(``preview_scope="validation_only"`` and ``not_run``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DRY_RUN_DESCRIPTION = (
    "true: run every check up to the commit and write nothing, answering with a "
    "would_* status; false: commit. Omitted: this operation's default (a change "
    "derived across several artifacts, or one that cannot be undone, previews)."
)
AT_DESCRIPTION = (
    "The coordinate a preview answered with: its git oid, or for a change to operational "
    "state its state digest (either may be shortened to a unique prefix of 12+ hex "
    "characters). A commit carrying it refuses playbill.preview.state_moved if that state "
    "moved since. Required to commit a change that cannot be undone."
)

DryRun = Annotated[bool | None, Field(default=None, description=DRY_RUN_DESCRIPTION)]
PreviewAt = Annotated[
    str | None, Field(default=None, pattern=r"^[0-9a-f]{12,64}$", description=AT_DESCRIPTION)
]

#: How an operation's default and confirmation are decided.
#: ``direct``: commits unless asked to preview. ``derived``: the server derives
#: a change across several artifacts, so it previews by default.
#: ``irreversible``: previews by default and commits only with ``at``.
ChangeKind = Literal["direct", "derived", "irreversible"]

ChangeRefusalCode = Literal[
    "playbill.preview.state_moved",
    "playbill.preview.confirmation_required",
    "playbill.preview.recovery_pending",
]


class StateCoordinate(BaseModel):
    """The operational state one change was evaluated against.

    ``subject`` names the records (``runtime_credential:<id>``,
    ``host_workspace:<instance>``, ...) and ``digest`` is the sha256 of their
    canonical JSON. A commit carrying ``at`` recomputes it where it writes and
    refuses ``playbill.preview.state_moved`` when it differs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-state-coordinate-v1"] = "playbill-state-coordinate-v1"
    subject: str = Field(min_length=1, max_length=512)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def of(cls, subject: str, state: object) -> StateCoordinate:
        """The coordinate of ``state`` (JSON-serializable; None for an absent subject)."""

        encoded = json.dumps(
            {"subject": subject, "state": state},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return cls(subject=subject, digest=hashlib.sha256(encoded).hexdigest())


class ChangeControlRequest(BaseModel):
    """The whole request body of a change that takes nothing else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-change-control-request-v1"] = "playbill-change-control-request-v1"
    dry_run: DryRun = None
    at: PreviewAt = None


__all__ = [
    "AT_DESCRIPTION",
    "DRY_RUN_DESCRIPTION",
    "ChangeControlRequest",
    "ChangeKind",
    "ChangeRefusalCode",
    "DryRun",
    "StateCoordinate",
    "PreviewAt",
]

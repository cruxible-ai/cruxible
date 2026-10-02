"""Exact preview for every change (rule R12): one vocabulary on every surface.

Every operation that changes governed state, operational state or anything
outside the daemon takes ``dry_run`` (``--dry-run`` on the CLI). A dry run takes
the change's own path up to the commit, writes nothing anywhere, and answers in
the change's own outcome shape with a ``would_*`` status, pinned to the
accepted ``coordinate`` it was evaluated at.

``at`` carries that coordinate back. A commit carrying it refuses
``playbill.preview.state_moved`` when accepted state moved since the preview.

Defaults, per operation:

- a change the server derives across several artifacts (installing or removing
  a kit, upgrading ClaimTypes or evidence rules) previews unless ``dry_run`` is
  false;
- a change that cannot be undone (decommissioning, revoking or rotating a
  credential, binding a ledger mirror) previews unless ``dry_run`` is false, and
  then commits only with ``at``: the confirmation is the preview's coordinate;
- everything else commits unless ``dry_run`` is true.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DRY_RUN_DESCRIPTION = (
    "true: run every check up to the commit and write nothing, answering with a "
    "would_* status; false: commit. Omitted: this operation's default (a change "
    "derived across several artifacts, or one that cannot be undone, previews)."
)
AT_DESCRIPTION = (
    "The coordinate a preview answered with (its git oid, or a unique prefix of 12+ "
    "hex characters). A commit carrying it refuses playbill.preview.state_moved if "
    "accepted state moved since. Required to commit a change that cannot be undone."
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
]


class ChangeControlRequestV1(BaseModel):
    """The whole request body of a change that takes nothing else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-change-control-request-v1"] = "playbill-change-control-request-v1"
    dry_run: DryRun = None
    at: PreviewAt = None


__all__ = [
    "AT_DESCRIPTION",
    "DRY_RUN_DESCRIPTION",
    "ChangeControlRequestV1",
    "ChangeKind",
    "ChangeRefusalCode",
    "DryRun",
    "PreviewAt",
]

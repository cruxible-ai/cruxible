"""Typed result for the daemon's advisory workspace Git advertisement."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, model_validator

WorkspaceAdvertisementFailureCode: TypeAlias = Literal[
    "workspace_missing",
    "workspace_not_git",
    "workspace_path_invalid",
    "git_unavailable",
    "remote_conflict",
    "object_format_mismatch",
    "fetch_failed",
    "unexpected_failure",
]


class PlaybillWorkspaceAdvertisement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-workspace-advertisement-v1"] = "playbill-workspace-advertisement-v1"
    status: Literal["updated", "not_attached", "failed"]
    workspace_path: str | None
    remote_name: Literal["playbill"] = "playbill"
    advertised_refs: tuple[str, ...] = ()
    failure_code: WorkspaceAdvertisementFailureCode | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> "PlaybillWorkspaceAdvertisement":
        if self.status == "updated" and (
            self.workspace_path is None or self.failure_code is not None
        ):
            raise ValueError("updated workspace advertisement has an invalid result shape")
        if self.status == "not_attached" and (
            self.workspace_path is not None or self.advertised_refs or self.failure_code is not None
        ):
            raise ValueError("not-attached workspace advertisement has an invalid result shape")
        if self.status == "failed" and (
            self.failure_code is None or self.advertised_refs
        ):
            raise ValueError("failed workspace advertisement has an invalid result shape")
        return self


NOT_ATTACHED_ADVERTISEMENT = PlaybillWorkspaceAdvertisement(
    status="not_attached",
    workspace_path=None,
)


__all__ = [
    "NOT_ATTACHED_ADVERTISEMENT",
    "PlaybillWorkspaceAdvertisement",
    "WorkspaceAdvertisementFailureCode",
]

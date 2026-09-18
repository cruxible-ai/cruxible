"""Administrative provider installation requests contain bytes references, never host paths."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .canonical import Sha256Value
from .providers import ProviderLocalDistributionPinV1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderWheelObjectV1(_Strict):
    filename: str
    digest: str

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        ProviderLocalDistributionPinV1._filename(value)
        if not value.endswith(".whl"):
            raise ValueError("provider installation accepts built wheels")
        return value

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class PlaybillProviderInstallRequestV1(_Strict):
    package: str | None = None
    wheel: ProviderWheelObjectV1 | None = None
    lock_digest: str | None = None
    dependencies: tuple[ProviderWheelObjectV1, ...] = ()
    extras: tuple[str, ...] = ()
    control_domain: str = "operator"
    reverify: bool = False

    @field_validator("lock_digest")
    @classmethod
    def _lock_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _source(self) -> "PlaybillProviderInstallRequestV1":
        if self.package is not None:
            if self.wheel is not None or self.lock_digest is not None or self.dependencies:
                raise ValueError("choose a catalog package or transferred wheel and lock")
            if not self.package or any(
                c not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for c in self.package
            ):
                raise ValueError("package must be a distribution name, not a path")
        elif self.wheel is None or self.lock_digest is None:
            raise ValueError("transferred installation requires wheel and lock digests")
        names = [item.filename for item in self.dependencies]
        if len(set(names)) != len(names) or (self.wheel and self.wheel.filename in names):
            raise ValueError("transferred wheels must have distinct filenames")
        if self.extras != tuple(sorted(set(self.extras))) or any(
            not extra or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for c in extra)
            for extra in self.extras
        ):
            raise ValueError("extras must be sorted unique package extra names")
        return self


class ProviderOperationReadinessV1(_Strict):
    interface_id: str
    installed: bool
    missing_requirements: tuple[str, ...] = ()


class PlaybillProviderInstallResultV1(_Strict):
    tag: Literal["playbill-provider-install-result-v1"] = "playbill-provider-install-result-v1"
    installation_id: str
    provider_id: str
    status: Literal["ready", "awaiting_approval", "blocked"]
    installed: bool
    registered: bool
    operations: tuple[ProviderOperationReadinessV1, ...] = ()
    proposal_id: str | None = None
    candidate_digest: str | None = None
    detail: str | None = None


class ProviderPackageSummaryV1(_Strict):
    name: str
    version: str
    interfaces: tuple[str, ...]


class PlaybillProviderCatalogV1(_Strict):
    tag: Literal["playbill-provider-catalog-v1"] = "playbill-provider-catalog-v1"
    packages: tuple[ProviderPackageSummaryV1, ...] = ()
    detail: str | None = None

"""Administrative provider installation requests contain bytes references, never host paths."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .canonical import Sha256Value
from .change_control import DryRun, PreviewAt
from .projection import AcceptedCoordinate
from .providers import ProviderLocalDistributionPin


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderWheelObject(_Strict):
    filename: str
    digest: str

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        ProviderLocalDistributionPin._filename(value)
        if not value.endswith(".whl"):
            raise ValueError("provider installation accepts built wheels")
        return value

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ProviderInstallRequest(_Strict):
    package: str | None = None
    # An exact release of a package installed by name from the provider index;
    # omitted, the newest final release. A configured repository has one version.
    version: str | None = None
    wheel: ProviderWheelObject | None = None
    lock_digest: str | None = None
    dependencies: tuple[ProviderWheelObject, ...] = ()
    extras: tuple[str, ...] = ()
    control_domain: str = "operator"
    reverify: bool = False
    #: Preview: resolve the package and, for one already prepared here, evaluate
    #: the registration it would propose; fetch, build and register nothing.
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("lock_digest")
    @classmethod
    def _lock_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _source(self) -> "ProviderInstallRequest":
        if self.package is not None:
            if self.wheel is not None or self.lock_digest is not None or self.dependencies:
                raise ValueError("choose a catalog package or transferred wheel and lock")
            if not self.package or any(
                c not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for c in self.package
            ):
                raise ValueError("package must be a distribution name, not a path")
            if self.version is not None and (
                not self.version
                or any(c not in "0123456789abcdefghijklmnopqrstuvwxyz.!+-_" for c in self.version)
            ):
                raise ValueError("version must be a release version")
        elif self.version is not None:
            raise ValueError("a version applies only to a package installed by name")
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


class ProviderOperationReadiness(_Strict):
    interface_id: str
    installed: bool
    missing_requirements: tuple[str, ...] = ()


#: The steps of an install a v1 preview does NOT run (the maintainer's v1
#: exception to R12, r12-scope-1001): preparing the package (fetching its
#: wheels, building its environment), checking deployment readiness, and --
#: for a package not yet prepared -- evaluating its registration.
ProviderInstallPreviewStep = Literal["package_preparation", "deployment_readiness", "registration"]


class ProviderInstallResult(_Strict):
    tag: Literal["playbill-provider-install-result-v1"] = "playbill-provider-install-result-v1"
    installation_id: str
    provider_id: str
    #: ``would_install`` answers a preview, which installed and proposed nothing.
    status: Literal["ready", "awaiting_approval", "blocked", "would_install"]
    installed: bool
    registered: bool
    operations: tuple[ProviderOperationReadiness, ...] = ()
    proposal_id: str | None = None
    candidate_digest: str | None = None
    detail: str | None = None
    #: A v1 install preview validates and writes nothing, but is not the whole
    #: install: ``validation_only`` says so, and ``not_run`` names the steps it
    #: did not run. Present exactly on a preview.
    preview_scope: Literal["validation_only"] | None = None
    not_run: tuple[ProviderInstallPreviewStep, ...] = ()
    #: The accepted coordinate a preview evaluated at; commit it with ``at``.
    coordinate: AcceptedCoordinate | None = None

    @model_validator(mode="after")
    def _preview_label(self) -> "ProviderInstallResult":
        previewed = self.status == "would_install"
        if previewed != (self.preview_scope == "validation_only"):
            raise ValueError("exactly an install preview is labelled validation_only")
        if previewed and (self.coordinate is None or not self.not_run):
            raise ValueError("an install preview names its coordinate and the steps not run")
        if not previewed and (self.not_run or self.coordinate is not None):
            raise ValueError("only an install preview names steps not run and a coordinate")
        return self


class ProviderPackageSummary(_Strict):
    name: str
    version: str
    interfaces: tuple[str, ...]


class ProviderCatalog(_Strict):
    tag: Literal["playbill-provider-catalog-v1"] = "playbill-provider-catalog-v1"
    packages: tuple[ProviderPackageSummary, ...] = ()
    detail: str | None = None

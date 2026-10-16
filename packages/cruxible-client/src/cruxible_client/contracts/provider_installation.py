"""Administrative provider installation requests contain bytes references, never host paths."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .canonical import Sha256Value
from .change_control import DryRun, PreviewAt
from .projection import AcceptedCoordinate
from .providers import ProviderLocalDistributionPin, parse_provider_build_name


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
    #: The build this install registers (``name/<build key>``), once the package
    #: is prepared. Installing adds this build beside any other build of the
    #: name; every Procedure keeps the build it pins.
    build: str | None = None

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


class InstalledProvider(_Strict):
    """One live provider build at the accepted head: what it runs and implements."""

    #: The provider name; several builds of one name can be live together.
    provider: str
    #: The distribution it runs, when it runs one (the built-in runs inside core).
    package: str | None = None
    version: str | None = None
    interfaces: tuple[str, ...]
    #: The build's identity name (``name/<build key>``, or the bare name of a
    #: single-path Provider such as the built-in).
    build: str
    #: The most recently registered live build of its name: what new authoring
    #: and unpinned references resolve to.
    newest: bool = True
    #: Live definitions pinning this exact build; a build is retired only at 0.
    pinned_by: int = 0


class ProviderCatalog(_Strict):
    """Installed Providers, then the packages the configured repository offers."""

    tag: Literal["playbill-provider-catalog-v1"] = "playbill-provider-catalog-v1"
    installed: tuple[InstalledProvider, ...] = ()
    packages: tuple[ProviderPackageSummary, ...] = ()
    detail: str | None = None


class ProviderBuildSummaryV1(_Strict):
    """One build of a provider name, as resolution and the build verbs see it."""

    tag: Literal["cruxible-provider-build-summary-v1"] = "cruxible-provider-build-summary-v1"
    #: The build's identity name: ``name/<build key>``, or a single-path name.
    build: str
    version: str | None = None
    state: Literal["live", "retired"]
    newest: bool = False
    #: Live definitions pinning this exact build.
    pinned_by: int = 0


#: How a provider reference resolved: ``exact`` (it named one build) or
#: ``most_recently_registered`` (a bare name: the live build registered last).
ProviderBuildRule = Literal["exact", "most_recently_registered"]


class ProviderBuildResolutionV1(_Strict):
    """Which build a provider reference resolved to, and from what."""

    tag: Literal["cruxible-provider-build-resolution-v1"] = "cruxible-provider-build-resolution-v1"
    requested: str
    rule: ProviderBuildRule
    #: The build's identity name and exact artifact digest: what gets pinned.
    resolved: str
    artifact_digest: str
    version: str | None = None
    #: The live builds the rule chose among (one for an exact reference).
    considered: tuple[ProviderBuildSummaryV1, ...] = ()
    coordinate: AcceptedCoordinate

    @field_validator("artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


#: Why one definition was not moved to a build: ``contract_changed`` (the build
#: implements none of its interfaces' exact contracts), ``not_movable`` (its
#: family cannot be re-pinned automatically, or its Blueprint cannot be
#: instantiated again), ``keep_build`` (the request kept it on its build).
ProviderRepinReason = Literal["contract_changed", "not_movable", "keep_build"]


class ProviderRepinV1(_Strict):
    """One live definition moved (or not) from a provider build to another."""

    tag: Literal["cruxible-provider-repin-v1"] = "cruxible-provider-repin-v1"
    #: The definition (``Procedure:<name>`` or ``Blueprint:<name>``).
    identity: str
    from_build: str
    to_build: str
    action: Literal["successor", "kept", "refused"]
    reason: ProviderRepinReason | None = None

    @model_validator(mode="after")
    def _reason(self) -> "ProviderRepinV1":
        if (self.action == "successor") != (self.reason is None):
            raise ValueError("a re-pin names a reason exactly when it does not move")
        return self


class StoppedAutomationV1(_Strict):
    """A Line, Trigger or Mandate a re-pin retires: it pinned the moved definition's
    old version and cannot be carried to the new one, so its automation stops.
    Author it again over the moved definition to resume it."""

    tag: Literal["cruxible-stopped-automation-v1"] = "cruxible-stopped-automation-v1"
    identity: str
    kind: Literal["line", "trigger", "mandate"]
    #: The moved definition whose re-pin stops it.
    stopped_by: str


class ProviderRetireRequest(_Strict):
    """Retire one provider build: new authoring and unpinned references stop
    resolving to it. Refused while any live definition pins it; its runtime
    environment stays, so what ran on it stays replayable."""

    #: The exact build: ``name/<build key>`` (or a single-path Provider's name).
    build: str
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("build")
    @classmethod
    def _build(cls, value: str) -> str:
        return parse_provider_build_name(value.removeprefix("Provider:")).identity_name


class ProviderRetireResult(_Strict):
    tag: Literal["cruxible-provider-retire-result-v1"] = "cruxible-provider-retire-result-v1"
    build: str
    status: Literal["would_retire", "retired", "proposed", "blocked", "would_block", "unchanged"]
    #: The live definitions pinning the build, when that blocks it.
    pinned_by: tuple[str, ...] = ()
    proposal_id: str | None = None
    approval_required: bool = False
    detail: str | None = None
    coordinate: AcceptedCoordinate | None = None


class ProviderMoveRequest(_Strict):
    """Move the live definitions pinning other builds of one provider to one build,
    as one governed change: each moves to the build's exact implementation of
    the contract it uses, and its dependents are carried with it."""

    provider: str
    #: ``newest`` (the most recently registered live build) or one exact build,
    #: by its build key or ``name/<build key>``.
    to: str = "newest"
    #: Only these definitions (``Procedure:<name>``); empty moves every live
    #: definition pinning another build of the provider.
    only: tuple[str, ...] = ()
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        parsed = parse_provider_build_name(value.removeprefix("Provider:"))
        if parsed.build is not None:
            raise ValueError("provider names a provider, and to names its build")
        return parsed.name

    @model_validator(mode="after")
    def _target(self) -> "ProviderMoveRequest":
        if self.to != "newest":
            target = self.to if "/" in self.to else f"{self.provider}/{self.to}"
            parsed = parse_provider_build_name(target)
            if parsed.name != self.provider:
                raise ValueError("the target build belongs to another provider")
        return self

    @field_validator("only")
    @classmethod
    def _only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("only must be sorted and unique")
        for identity in value:
            kind, separator, name = identity.partition(":")
            if not separator or kind not in {"Procedure", "Blueprint"} or not name:
                raise ValueError("only names definitions as Procedure:NAME or Blueprint:NAME")
        return value


class ProviderMoveResult(_Strict):
    tag: Literal["cruxible-provider-move-result-v1"] = "cruxible-provider-move-result-v1"
    provider: str
    to: ProviderBuildResolutionV1
    moved: tuple[ProviderRepinV1, ...] = ()
    #: Automation the move stops (carrying it is not automatic yet).
    stops: tuple[StoppedAutomationV1, ...] = ()
    #: Other live dependents carried (re-pinned) with the moved definitions.
    carried_dependents: int = 0
    status: Literal["unchanged", "would_propose", "would_block", "proposed", "accepted", "blocked"]
    proposal_id: str | None = None
    approval_required: bool = False
    detail: str | None = None
    coordinate: AcceptedCoordinate | None = None

"""One administrative installation service for repository and transferred packages."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import TypeAdapter

from cruxible_client.contracts.canonical import canonical_bytes, canonical_digest
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_installation import (
    InstalledProvider,
    ProviderCatalog,
    ProviderInstallRequest,
    ProviderInstallResult,
    ProviderOperationReadiness,
    ProviderPackageSummary,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    ProviderInterfaceContract,
    parse_provider_interface,
    provider_interface_digest,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    Provider,
    ProviderAny,
    ProviderBuild,
    ProviderLocalDistributionPin,
    evaluate_provider_law,
    parse_provider,
    provider_path,
    render_provider,
)
from cruxible_client.contracts.repairs import (
    HandEditInstruction,
    HandEditRepair,
    RepairOperation,
    served_repair_for_refusal,
)
from cruxible_core.compiler.compiler import CLAIM_REBIND_COMPILER, PROVIDER_BUILDS_COMPILER
from cruxible_core.derived.derived_state import fork_tree
from cruxible_core.errors import ConfigError, ProviderLaneRefusedError, RequestRefusedError
from cruxible_core.governance.seed_artifacts.workspace_file import (
    is_builtin_workspace_file_registration,
)
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.providers.package_index import (
    DEFAULT_PROVIDER_INDEX_URLS,
    IndexRelease,
    embedded_lock,
    fetch_release,
    find_release,
)
from cruxible_core.providers.package_materialization import (
    ArtifactTransport,
    package_preparation_errors,
    prepare_provider_package,
    toolchain,
    uv_executable,
)
from cruxible_core.providers.package_metadata import read_package_registration
from cruxible_core.providers.package_probe import run_package_probe
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_core.providers.provider_local_runtime import (
    verification_format_upgrade,
    verify_provider_installation,
)
from cruxible_core.providers.provider_process_leases import (
    PROVIDER_RUNTIME_DIRECTORY_UNAVAILABLE,
)
from cruxible_core.runtime.execution_policy import enforce_customer_code_execution_supported
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.provider_runtime import (
    ProviderDeploymentConfigV1,
    ProviderRuntimeOperator,
)
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.change_preview import ChangeMode, admit_change_set, change_scope
from cruxible_core.service.proposals.proposals import open_proposal_at_target
from cruxible_core.storage.preview_fence import refuse_write_while_previewing


def _repository_projects(operator: ProviderRuntimeOperator) -> dict[str, Path]:
    configured = operator.config.provider_repository
    if configured is None:
        return {}
    try:
        root = Path(configured).resolve(strict=True)
    except OSError as exc:
        raise ConfigError("configured provider repository is unavailable") from exc
    result = {}
    for path in sorted(root.glob("packages/*/pyproject.toml")):
        if not path.resolve().is_relative_to(root):
            raise ConfigError("provider repository package escapes its configured root")
        try:
            project = tomllib.loads(path.read_text())["project"]
            name = project["name"]
        except (OSError, ValueError, KeyError) as exc:
            raise ConfigError("provider repository has unreadable project metadata") from exc
        if name in result:
            raise ConfigError("provider repository contains duplicate distribution names")
        result[name] = path.parent
    return result


def installed_providers(instance: PlaybillInstance) -> tuple[InstalledProvider, ...]:
    """Every live provider build at the accepted head, with what it implements.

    One row per build: several builds of a name can be live, and ``newest``
    marks the one new authoring resolves a bare name to.
    """

    from cruxible_core.providers.builds import live_pinners

    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        rows = connection.execute(
            "SELECT p.identity,p.path,p.artifact_digest,p.provider_name,p.distribution_version,"
            "coalesce(h.identity=p.identity AND h.ambiguous=0,0) FROM main.providers p "
            "LEFT JOIN main.provider_heads h ON h.provider_name=p.provider_name "
            "WHERE p.lifecycle='live' ORDER BY p.provider_name,p.identity"
        ).fetchall()
        contents = {row[1]: projection.typed.member_bytes(row[1]) for row in rows}
        pinned = {row[0]: len(live_pinners(connection, row[0], row[2])) for row in rows}
    installed = []
    for identity, path, _digest, name, _version, is_newest in rows:
        provider = parse_provider(contents[path], path=path)
        distribution = getattr(getattr(provider, "runtime_artifact", None), "distribution", None)
        installed.append(
            InstalledProvider(
                provider=name,
                build=provider.identity.name,
                newest=bool(is_newest),
                pinned_by=pinned[identity],
                package=None if distribution is None else distribution.name,
                version=None if distribution is None else distribution.version,
                interfaces=tuple(
                    sorted({item.interface_id for item in getattr(provider, "implementations", ())})
                ),
            )
        )
    return tuple(installed)


def _repository_packages(operator: ProviderRuntimeOperator) -> tuple[ProviderPackageSummary, ...]:
    """The packages the configured provider repository offers, by name."""

    packages = []
    for name, path in _repository_projects(operator).items():
        descriptors = tuple(path.glob("src/*/registration.json"))
        if not descriptors:
            continue
        if len(descriptors) != 1:
            raise ConfigError("provider package must have exactly one registration descriptor")
        with package_preparation_errors():
            document = read_package_registration(descriptors[0].parent)
        if document.manifest.distribution.name != name:
            raise ConfigError("provider catalog name differs from package manifest")
        packages.append(
            ProviderPackageSummary(
                name=name,
                version=document.manifest.distribution.version,
                interfaces=tuple(sorted(item.interface_id for item in document.interfaces)),
            )
        )
    return tuple(sorted(packages, key=lambda item: item.name))


def service_provider_catalog(
    operator: ProviderRuntimeOperator, instance: PlaybillInstance
) -> ProviderCatalog:
    """The Providers installed here, then the packages the configured repository offers."""

    return ProviderCatalog(
        installed=installed_providers(instance),
        packages=_repository_packages(operator),
        detail=None
        if operator.config.provider_repository
        else "No provider repository configured; packages install by name from the provider "
        "index, and built wheels can still be transferred.",
    )


def _write(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _index_urls(operator: ProviderRuntimeOperator) -> tuple[str, ...]:
    return operator.config.provider_index_urls or DEFAULT_PROVIDER_INDEX_URLS


def _index_transport(index_urls: tuple[str, ...]) -> ArtifactTransport:
    return ArtifactTransport(
        *(Path(unquote(urlsplit(url).path)) for url in index_urls if url.startswith("file:"))
    )


@dataclass(frozen=True)
class ExpectedProviderBuild:
    """The exact build an install by name must fetch, as a kit's default names it.

    ``wheel_sha256`` and ``lock_sha256`` are tagged digests of the published wheel
    and of the lock it embeds. The index's listing is checked before anything is
    fetched and the embedded lock before any dependency is fetched or any
    environment prepared, so another build at the same name and version runs no
    code here, registers no deployment and proposes nothing.
    """

    wheel_sha256: str
    lock_sha256: str


def _refuse_unexpected_build(package: str, what: str, served: str, expected: str) -> None:
    if served == expected:
        return
    raise RequestRefusedError(
        "cruxible.provider.index_build_differs",
        f"the provider index serves {package} with {what} {served[:19]}, not the build "
        f"expected ({expected[:19]}); nothing was prepared, registered or proposed",
        repair=HandEditRepair(
            hand_edit=HandEditInstruction(
                target="provider_index_urls in the daemon's provider runtime configuration",
                required_change=(
                    "Point the provider index at one that serves the published build the "
                    "kit names, or rebuild the kit against the build this index serves."
                ),
            )
        ),
    )


def _index_source_files(
    release: IndexRelease,
    index_urls: tuple[str, ...],
    custody: Path,
    expected: ExpectedProviderBuild | None = None,
) -> tuple[Path, Path, tuple[Path, ...]]:
    transport = _index_transport(index_urls)
    wheel = custody / release.filename
    _write(wheel, fetch_release(release, index_urls, transport))
    lock = custody / "uv.lock"
    _write(lock, embedded_lock(wheel))
    locked = toolchain("resolution").load_uv_lock(lock)
    if expected is not None:
        # Before any sibling is fetched or any environment prepared.
        _refuse_unexpected_build(
            release.name, "an embedded lock", locked.lock_sha256, expected.lock_sha256
        )
    # The lock names the provider's first-party siblings (its runtime) by path.
    # Each comes from the index that listed the provider, at its locked version.
    dependencies = []
    for row in sorted(locked.packages, key=lambda item: item["name"]):
        if row["name"] == release.name or "registry" in row.get("source", {}):
            continue
        sibling = find_release((release.index_url,), row["name"], str(row["version"]), transport)
        path = custody / sibling.filename
        _write(path, fetch_release(sibling, index_urls, transport))
        dependencies.append(path)
    return wheel, lock, tuple(dependencies)


def _source_files(
    instance: PlaybillInstance,
    operator: ProviderRuntimeOperator,
    request: ProviderInstallRequest,
    custody: Path,
    release: IndexRelease | None,
    expected: ExpectedProviderBuild | None = None,
) -> tuple[Path, Path, tuple[Path, ...]]:
    enforce_customer_code_execution_supported()
    if release is not None:
        return _index_source_files(release, _index_urls(operator), custody, expected)
    if request.package is None:
        assert request.wheel is not None and request.lock_digest is not None
        access = BodyAccessContext(principal_id="provider-installation", can_read_body=True)
        for item in (request.wheel, *request.dependencies):
            _write(custody / item.filename, instance.body_store().read(item.digest, access=access))
        lock = custody / "uv.lock"
        _write(lock, instance.body_store().read(request.lock_digest, access=access))
        return (
            custody / request.wheel.filename,
            lock,
            tuple(custody / item.filename for item in request.dependencies),
        )
    projects = _repository_projects(operator)
    if request.package not in {item.name for item in _repository_packages(operator)}:
        raise ConfigError("provider package is not in the configured repository")
    project = projects[request.package]
    lock = custody / "uv.lock"
    _write(lock, (project / "uv.lock").read_bytes())
    locked = toolchain("resolution").load_uv_lock(lock)
    # The root and every local dependency come from this configured repository;
    # all other dependencies remain constrained by lock hashes and index policy.
    names = {request.package}
    names.update(row["name"] for row in locked.packages if "registry" not in row.get("source", {}))
    uv = uv_executable()
    wheels = {}
    for name in sorted(names):
        if name not in projects:
            raise ConfigError(f"local dependency {name!r} is absent from the configured repository")
        output = custody / ("build-" + name)
        output.mkdir(exist_ok=True, mode=0o700)
        # This is an administrative build, gated by the caller before checkout
        # code can run. Provider invocation/probes use the supervised runtime.
        subprocess.run(
            (uv, "build", "--wheel", "--offline", "--out-dir", str(output), str(projects[name])),
            check=True,
            capture_output=True,
            timeout=180,
        )
        built = tuple(output.glob("*.whl"))
        if len(built) != 1:
            raise ConfigError("provider build did not produce exactly one wheel")
        wheels[name] = custody / built[0].name
        _write(wheels[name], built[0].read_bytes())
    return (
        wheels[request.package],
        lock,
        tuple(path for name, path in sorted(wheels.items()) if name != request.package),
    )


def _refuse_built_in_interfaces(
    instance: PlaybillInstance, document: PackageRegistrationDocumentV1, accepted_oid: str
) -> None:
    """Refuse a package exporting an interface this instance already has built in.

    A built-in interface (workspace.file, seeded at genesis) is implemented by
    core's own Provider; a package registration would have to succeed it and
    strand that Provider, which the proposal law refuses as an incomplete
    closure. Say why instead.
    """

    tree = instance.immutable_tree_at(accepted_oid)
    for exported in document.interfaces:
        path = provider_interface_path(exported.interface_id)
        current = tree.get(path)
        if current is None:
            continue
        registration = parse_provider_interface(current, path=path)
        if registration.lifecycle.state == "live" and is_builtin_workspace_file_registration(
            registration
        ):
            raise ConfigError(
                f"{exported.interface_id} is built in on this instance; no install is needed"
            )


def _interface_bindings(
    tree: Mapping[str, bytes], document: PackageRegistrationDocumentV1
) -> tuple[tuple[ProviderInterfaceContract, bool], ...]:
    """Each exported interface's contract registration, and whether it is already live.

    A registration is one contract, named by its contract key: a build binds the
    registration of the contract it implements when one is live here, and
    adds it otherwise. Nothing replaces a registration, so another build's
    pins never break.
    """

    result: list[tuple[ProviderInterfaceContract, bool]] = []
    for desired in document.interface_registrations():
        path = provider_interface_path(desired.identity.name)
        current_bytes = tree.get(path)
        if current_bytes is None:
            result.append((desired, False))
            continue
        current = parse_provider_interface(current_bytes, path=path)
        if current.lifecycle.state != "live":
            raise ProposalIntegrityError("install cannot silently restore a retired interface")
        assert isinstance(current, ProviderInterfaceContract)
        result.append((current, True))
    return tuple(result)


def _accepted(
    registration: ProviderInterfaceContract,
) -> AcceptedProviderInterfaceRegistration:
    return AcceptedProviderInterfaceRegistration(
        path=provider_interface_path(registration.identity.name),
        registration=registration,
        artifact_digest=provider_interface_digest(registration).tagged,
    )


def _refuse_unproven_provider(
    provider: ProviderBuild,
    interfaces: tuple[ProviderInterfaceContract, ...],
    sibling_control_domains: frozenset[str],
) -> None:
    """The Provider law, before anything is proposed, against the registrations it binds.

    A package bound onto a registration it did not build must implement that
    registration's exact definition and effect class, and a new build joins its
    name's control domain: a disagreement is refused here, typed, rather than
    as a blocked proposal.
    """

    path = provider_path(provider.identity.name)
    law = evaluate_provider_law(
        provider,
        path=path,
        predecessor=None,
        interface_registrations={item.identity.qualified: _accepted(item) for item in interfaces},
        sibling_control_domains=sibling_control_domains,
    )
    if law.verdict == "accepted":
        return
    diagnostic = law.diagnostics[0]
    if diagnostic.code == "cruxible.provider.control_domain_differs":
        raise RequestRefusedError(
            diagnostic.code,
            diagnostic.message,
            repair=HandEditRepair(
                hand_edit=HandEditInstruction(
                    target="the install request's control_domain (--control-domain)",
                    required_change="Install the build under its name's control domain.",
                )
            ),
        )
    raise RequestRefusedError(
        diagnostic.code,
        f"{provider.provider_name} cannot bind the registrations live here: {diagnostic.message}",
        repair=HandEditRepair(
            hand_edit=HandEditInstruction(
                target=f"the {provider.provider_name} package manifest",
                required_change=(
                    "Implement the exact interface definition and effect class the live "
                    "ProviderInterface registration carries "
                    "(cruxible get ProviderInterface:ID --detail proof shows them)."
                ),
            )
        ),
    )


def _carried_from_newest(
    instance: PlaybillInstance, tree: Mapping[str, bytes], name: str, accepted_oid: str
) -> ProviderAny | None:
    """The most recently registered live build of ``name``: a new build carries its
    independently governed signing keys and capture declarations."""

    from cruxible_core.providers.builds import indexed_newest_provider

    coordinate = instance.coordinate_for_oid(accepted_oid)
    with instance.bind_accepted_projection(coordinate) as projection:
        chosen = indexed_newest_provider(projection.typed.connection, name)
    return None if chosen is None else parse_provider(tree[chosen.path], path=chosen.path)


def _definition_changes(
    instance: PlaybillInstance,
    document: PackageRegistrationDocumentV1,
    provider: Provider,
    accepted_oid: str,
) -> tuple[Mapping[str, bytes], tuple[str, ...], ProviderBuild]:
    """The paths an install adds: the build's own path and any contract
    registration not live yet. No existing Provider or registration is written,
    so no live definition owes a successor and the install never strands one."""

    tree = instance.immutable_tree_at(accepted_oid)
    candidate = fork_tree(tree)
    changed = []
    bindings = _interface_bindings(tree, document)
    for registration, live in bindings:
        if live:
            continue
        path = provider_interface_path(registration.identity.name)
        candidate[path] = render_provider_interface(registration)
        changed.append(path)
    interfaces = tuple(registration for registration, _live in bindings)
    runtime = provider.runtime_artifact
    assert isinstance(runtime.distribution, ProviderLocalDistributionPin)
    assert runtime.local_env is not None
    desired = document.provider_definition(
        distribution=runtime.distribution,
        local_env=runtime.local_env,
        control_domain=provider.control_domain,
        interfaces=interfaces,
    )
    path = provider_path(desired.identity.name)
    current_bytes = tree.get(path)
    if current_bytes is not None:
        current = parse_provider(current_bytes, path=path)
        if current.lifecycle.state != "live":
            raise RequestRefusedError(
                "cruxible.provider.build_retired",
                f"build {desired.identity.name} is retired here; installing it again would "
                "restore it silently",
                repair=RepairOperation(operation="cruxible.provider.list"),
            )
        governed = {"lifecycle", "signing_keys", "capture_contract_digests", "upstream_provenance"}
        if current.model_dump(exclude={*governed, "pins"}) != desired.model_dump(
            exclude={*governed, "pins"}
        ) or [pin for pin in current.pins if pin.role == "provider-interface"] != list(
            desired.pins
        ):
            # One wheel and lock name one build on every host; a different
            # environment here for the same build is not a new build.
            raise RequestRefusedError(
                "cruxible.provider.build_rematerialized",
                f"build {desired.identity.name} is installed here with another runtime "
                "(environment or interface bindings); the same wheel and lock name one build",
                repair=HandEditRepair(
                    hand_edit=HandEditInstruction(
                        target=f"the {desired.provider_name} package",
                        required_change="Publish a new version to install a different build.",
                    )
                ),
            )
        return candidate, tuple(sorted(changed)), desired
    carried = _carried_from_newest(instance, tree, desired.provider_name, accepted_oid)
    if carried is not None:
        # Independently governed signing and capture configuration follows the
        # name from its most recent build to the new one.
        desired = desired.model_copy(
            update={
                "signing_keys": carried.signing_keys,
                "capture_contract_digests": carried.capture_contract_digests,
                "upstream_provenance": carried.upstream_provenance,
                "pins": tuple(
                    sorted(
                        (
                            *desired.pins,
                            *(pin for pin in carried.pins if pin.role == "capture-contract"),
                        ),
                        key=lambda pin: (
                            pin.role.encode("utf-8"),
                            pin.target.qualified.encode("utf-8"),
                        ),
                    )
                ),
            }
        )
        desired = ProviderBuild.model_validate(desired.model_dump(mode="json"))
    with instance.bind_accepted_projection(instance.coordinate_for_oid(accepted_oid)) as projection:
        siblings = frozenset(
            row[0]
            for row in projection.typed.connection.execute(
                "SELECT control_domain FROM main.provider_control_domains WHERE provider_name=?",
                (desired.provider_name,),
            )
        )
    _refuse_unproven_provider(desired, interfaces, siblings)
    candidate[path] = render_provider(desired)
    changed.append(path)
    return candidate, tuple(sorted(changed)), desired


_PREPARED_PROVIDER: TypeAdapter[ProviderAny] = TypeAdapter(ProviderAny)


def _prepared_provider(value: object) -> Provider:
    """A retained preparation's Provider, in whichever format it was written.

    Only its runtime pins, implementations and control domain are read: the
    build an install proposes is always derived again from the package document.
    """

    provider = _PREPARED_PROVIDER.validate_python(value)
    if not isinstance(provider, Provider):
        raise ConfigError("a retained provider preparation names no package runtime")
    return provider


def _repository_fingerprint(operator: ProviderRuntimeOperator, package: str) -> str:
    """An install request names the current checkout bytes, never just its label.

    This is install-time work only. Include local dependencies and build inputs;
    scratch/cache/output directories cannot affect the retained source identity.
    """
    projects = _repository_projects(operator)
    if package not in projects:
        raise ConfigError("provider package is absent from the configured repository")
    skipped = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
    lock = toolchain("resolution").load_uv_lock(projects[package] / "uv.lock")
    names = {package} | {
        row["name"] for row in lock.packages if "registry" not in row.get("source", {})
    }
    files = {}
    for name in sorted(names):
        if name not in projects:
            raise ConfigError(f"local dependency {name!r} is absent from the configured repository")
        root = projects[name]
        for directory, subdirs, filenames in os.walk(root):
            subdirs[:] = sorted(item for item in subdirs if item not in skipped)
            for item in (*subdirs, *sorted(filenames)):
                path = Path(directory) / item
                if path.is_symlink():
                    raise ConfigError("provider source checkout must not contain symlinks")
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    files[name + "/" + relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return canonical_digest("playbill-provider-repository-source-v1", files)


def service_install_provider(
    instance: PlaybillInstance,
    *,
    operator: ProviderRuntimeOperator,
    request: ProviderInstallRequest,
    actor_id: str,
    timestamp: str,
    expected_build: ExpectedProviderBuild | None = None,
) -> ProviderInstallResult:
    """Install one package and propose its registration (lands if the policy allows).

    A transferred wheel (a kit's bundled provider among them) resolves its
    registry dependencies from custody and the operator's configured indexes, or
    from the default index, PyPI, when none is configured, as an install by name
    does. A configured repository's checkout keeps to its configured indexes.
    The lock's hashes pin every dependency either way. ``expected_build`` pins an
    install by name (a kit's default provider) to one published wheel and lock.
    """

    instance.require_writable()
    enforce_customer_code_execution_supported()
    if instance.accepted_coordinate().compiler not in {
        PROVIDER_BUILDS_COMPILER,
        CLAIM_REBIND_COMPILER,
    }:
        raise ConfigError(
            "provider installation requires the provider-builds compiler; "
            "run cruxible compiler upgrade first"
        )
    source: str | dict[str, str] | None = None
    release = None
    if request.package and operator.config.provider_repository is not None:
        if request.version is not None:
            raise ConfigError(
                "a configured provider repository installs its checkout version, never an "
                "exact release"
                + (
                    "; a kit's default provider names one, so install the repository's build "
                    "with provider install first (the kit's default is then satisfied) or add "
                    "the kit on a daemon without provider_repository"
                    if expected_build is not None
                    else ""
                )
            )
        with package_preparation_errors():
            source = _repository_fingerprint(operator, request.package)
    elif request.package:
        with package_preparation_errors():
            release = find_release(
                _index_urls(operator),
                request.package,
                request.version,
                _index_transport(_index_urls(operator)),
            )
        if expected_build is not None:
            # The index's listing names the wheel's hash: refuse before fetching it.
            _refuse_unexpected_build(
                release.name, "a wheel", "sha256:" + release.sha256, expected_build.wheel_sha256
            )
        source = {"index": release.index_url, "wheel": release.filename, "sha256": release.sha256}
    if expected_build is not None and release is None:
        raise ConfigError("an expected build pins only an install by name from a provider index")
    identifier = "sha256:" + canonical_digest(
        "playbill-provider-installation-request-v1",
        {
            "request": request.model_dump(mode="json", exclude={"reverify", "dry_run", "at"}),
            "source": source,
        },
    )
    directory = (
        instance.root / "exhaust" / "provider-installations" / identifier.removeprefix("sha256:")
    )
    with change_scope(
        instance,
        dry_run=request.dry_run,
        at=request.at,
        kind="direct",
        operation="cruxible.provider.install",
        describe=f"installing provider {request.package or 'wheel'}",
    ) as mode:
        if mode.previewing:
            return _preview_installation(
                instance, mode, request, identifier, directory, actor_id, timestamp
            )
    refuse_write_while_previewing("provider installation")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "installation.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _install_locked(
                instance,
                operator,
                request,
                identifier,
                directory,
                actor_id,
                timestamp,
                source,
                release,
                confirm_head=mode.confirm_head,
                expected_build=expected_build,
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _preview_installation(
    instance: PlaybillInstance,
    mode: ChangeMode,
    request: ProviderInstallRequest,
    identifier: str,
    directory: Path,
    actor_id: str,
    timestamp: str,
) -> ProviderInstallResult:
    """What an install would do, with nothing fetched, built, registered or proposed (R12).

    The package's definitions come out of preparing it -- fetching its wheels
    and building its environment -- which is itself a write. So a package
    already prepared on this daemon has its registration evaluated on the
    proposal path exactly as the install would propose it; one not yet prepared
    is reported as the fetch and build it would need, with nothing evaluated.
    """

    prepared_path = directory / "prepared.json"
    name = request.package or "transferred wheel"
    assert mode.head is not None
    # The v1 exception (r12-scope-1001): validation only, labelled as such.
    label: dict[str, Any] = {
        "preview_scope": "validation_only",
        "coordinate": AcceptedCoordinate.from_internal(mode.head),
    }
    if not prepared_path.is_file():
        return ProviderInstallResult(
            installation_id=identifier,
            provider_id=name,
            status="would_install",
            installed=False,
            registered=False,
            not_run=("package_preparation", "deployment_readiness", "registration"),
            **label,
            detail=(
                f"would fetch and build {name} and then propose its definitions; the "
                "registration is evaluated once the package is prepared, so preview it again "
                "after installing reports it prepared"
            ),
        )
    saved = json.loads(prepared_path.read_bytes())
    document = PackageRegistrationDocumentV1.model_validate(saved["document"])
    provider = _prepared_provider(saved["provider"])
    _refuse_built_in_interfaces(instance, document, mode.head.git_oid)
    candidate_tree, changed, build = _definition_changes(
        instance, document, provider, mode.head.git_oid
    )
    if not changed:
        return ProviderInstallResult(
            installation_id=identifier,
            provider_id=build.provider_name,
            build=build.identity.name,
            status="would_install",
            installed=True,
            registered=True,
            not_run=("package_preparation", "deployment_readiness"),
            **label,
            detail="prepared and registered already; an install changes no definition",
        )
    suffix = canonical_digest(
        "playbill-provider-registration-target-v1",
        {
            "base": mode.head.semantic_root,
            "members": {path: candidate_tree[path].hex() for path in changed},
        },
    ).removeprefix("sha256:")
    admitted = admit_change_set(
        instance,
        mode,
        actor_id=actor_id,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/provider-install-{suffix}",
            proposed_base_oid=mode.head.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    return ProviderInstallResult(
        installation_id=identifier,
        provider_id=build.provider_name,
        build=build.identity.name,
        status="would_install",
        installed=True,
        registered=False,
        candidate_digest=admitted.candidate_digest,
        not_run=("package_preparation", "deployment_readiness"),
        **label,
        detail=(
            admitted.refusal_detail()
            if not admitted.admitted
            else "would propose its definitions"
            + (" (approval required)" if admitted.approval_required else " and activate them")
        ),
    )


def _refuse_unavailable_lane(operator: ProviderRuntimeOperator) -> None:
    """Refuse with the lane's own cause and repair when it cannot hold deployments."""

    if operator.process_leases is not None:
        return
    refusal = operator.lane_refusal()
    message = refusal.refusal_message
    if refusal.code != PROVIDER_RUNTIME_DIRECTORY_UNAVAILABLE:
        # The precondition names its cause itself; any other lane failure is
        # said by the lane reason `server status` also reports.
        message = f"{message}: {operator.unavailable_reason}"
    raise ProviderLaneRefusedError(
        refusal.code, message, repair=served_repair_for_refusal(refusal.code)
    )


def _install_locked(
    instance: PlaybillInstance,
    operator: ProviderRuntimeOperator,
    request: ProviderInstallRequest,
    identifier: str,
    directory: Path,
    actor_id: str,
    timestamp: str,
    source: str | dict[str, str] | None,
    release: IndexRelease | None,
    *,
    confirm_head: Callable[[str], None],
    expected_build: ExpectedProviderBuild | None = None,
) -> ProviderInstallResult:
    prepared_path = directory / "prepared.json"
    rewrite_prepared = False
    if prepared_path.exists():
        saved = json.loads(prepared_path.read_bytes())
        document = PackageRegistrationDocumentV1.model_validate(saved["document"])
        provider = _prepared_provider(saved["provider"])
        configured = ProviderDeploymentConfigV1.model_validate(saved["deployment"])
        # If publication completed before a crash updating prepared.json, reuse
        # the newer operational proof. Never downgrade it to the cached V1 proof.
        published = next(
            (
                item
                for item in operator.config.deployments
                if item.deployment_digest == configured.deployment_digest
            ),
            None,
        )
        if (
            published is not None
            and published.model_dump(exclude={"installation_verification"})
            == configured.model_dump(exclude={"installation_verification"})
            and verification_format_upgrade(
                configured.installation_verification, published.installation_verification
            )
        ):
            configured = published
            rewrite_prepared = True
        if expected_build is not None:
            # A retained preparation of this listing: its lock is the one checked.
            assert provider.runtime_artifact.local_env is not None
            _refuse_unexpected_build(
                provider.provider_name,
                "an embedded lock",
                provider.runtime_artifact.local_env.lock_sha256,
                expected_build.lock_sha256,
            )
        deployment = operator._deployment(configured)
        if request.reverify:
            verified = verify_provider_installation(provider, deployment)
            if verified != configured.installation_verification:
                if not verification_format_upgrade(configured.installation_verification, verified):
                    raise ConfigError("re-verification differs from retained installation")
                configured = configured.model_copy(update={"installation_verification": verified})
                deployment = operator._deployment(configured)
                rewrite_prepared = True
    else:
        # Before any build: a fresh installation's deployment needs the
        # Provider lane, so a lane that is down refuses here, naming its cause.
        _refuse_unavailable_lane(operator)
        custody = directory / "wheels"
        custody.mkdir(exist_ok=True, mode=0o700)
        with package_preparation_errors():
            wheel, lock_path, dependencies = _source_files(
                instance, operator, request, custody, release, expected_build
            )
            if (
                release is None
                and request.package
                and _repository_fingerprint(operator, request.package) != source
            ):
                raise ConfigError("provider checkout changed during build; retry installation")
            prepared = prepare_provider_package(
                wheel=wheel,
                lock_path=lock_path,
                dependency_wheels=dependencies,
                cache_root=operator.state_root / "provider-environments",
                extras=request.extras,
                control_domain=request.control_domain,
                index_urls=operator.config.provider_index_urls
                if release is None and request.package
                else _index_urls(operator),
            )
        document, provider, deployment = prepared.document, prepared.provider, prepared.deployment
        if document.governed_definitions:
            raise ConfigError("provider installation does not install kit definitions")
        verification = verify_provider_installation(provider, deployment)
        deployment = replace(deployment, installation_verification=verification)
        configured = ProviderDeploymentConfigV1.model_validate(
            dict(
                deployment_digest=deployment.deployment_digest,
                **{
                    name: str(getattr(deployment, name).relative_to(operator.state_root))
                    for name in (
                        "distribution_path",
                        "lock_path",
                        "environment_path",
                        "environment_manifest_path",
                        "interpreter_path",
                    )
                },
                environment_pin_key=deployment.environment_pin_key,
                provider_runtime_version=deployment.provider_runtime_version,
                installation_verification=verification,
            )
        )
        _write(
            prepared_path,
            canonical_bytes(
                {
                    "document": document.model_dump(mode="json"),
                    "provider": provider.model_dump(mode="json"),
                    "deployment": configured.model_dump(mode="json"),
                }
            ),
        )
    # Before the deployment is registered: an interface built into this
    # instance is never re-registered from a package, and the Provider law holds
    # against the registrations it binds.
    base = instance.accepted_coordinate()
    _refuse_built_in_interfaces(instance, document, base.git_oid)
    candidate_tree, changed, build = _definition_changes(instance, document, provider, base.git_oid)
    operator.register_deployment(configured)
    if rewrite_prepared:
        _write(
            prepared_path,
            canonical_bytes(
                {
                    "document": document.model_dump(mode="json"),
                    "provider": provider.model_dump(mode="json"),
                    "deployment": configured.model_dump(mode="json"),
                }
            ),
        )
    proposal_id = candidate_digest = None
    registered = not changed
    activation_blocked = False
    if changed:
        suffix = canonical_digest(
            "playbill-provider-registration-target-v1",
            {
                "base": base.semantic_root,
                "members": {path: candidate_tree[path].hex() for path in changed},
            },
        ).removeprefix("sha256:")
        target = f"refs/proposals/{actor_id}/provider-install-{suffix}"
        pending = open_proposal_at_target(instance, target)
        if pending is None:
            submitted = instance.proposal_service().submit(
                actor=AuthenticatedActor(actor_id=actor_id),
                request=ProposalAdmissionRequest(target_ref=target, proposed_base_oid=base.git_oid),
                candidate_tree=candidate_tree,
                timestamp=timestamp,
                confirm_head=confirm_head,
            )
            if (
                submitted.evaluation.verdict != "candidate"
                or submitted.evaluation.candidate_digest is None
            ):
                return ProviderInstallResult(
                    installation_id=identifier,
                    provider_id=build.provider_name,
                    build=build.identity.name,
                    status="blocked",
                    installed=True,
                    registered=False,
                    proposal_id=submitted.admission.proposal_id,
                    detail="Registration refused: "
                    + "; ".join(item.code for item in submitted.evaluation.diagnostics),
                )
            proposal_id, candidate_digest = (
                submitted.admission.proposal_id,
                submitted.evaluation.candidate_digest,
            )
        else:
            proposal_id, candidate_digest = pending.proposal_id, pending.candidate_digest
        assert candidate_digest is not None
        evidence = instance.proposal_evidence().read_candidate(candidate_digest)
        if not evidence.approval_requirements:
            activation = service_activate_playbill_proposal(
                instance, proposal_id=proposal_id, activated_by=actor_id
            )
            registered = activation.status == "accepted"
            activation_blocked = not registered
    operations = []
    prepared_interfaces = {item.interface_id for item in provider.implementations}
    for implementation in document.manifest.implementations:
        missing = [
            f"python extra: {extra}"
            for extra in implementation.requires_extras
            if extra not in request.extras
        ]
        for requirement in document.runtime_requirements:
            if (
                requirement.interface_id != implementation.interface_id
                or requirement.kind != "runtime_resource"
            ):
                continue
            available = False
            if (
                implementation.interface_id in prepared_interfaces
                and operator.process_leases is not None
            ):
                output = run_package_probe(
                    deployment,
                    operator.process_leases,
                    kind="resource",
                    digest=implementation.interface_digest,
                    value={"entrypoint": requirement.probe_entrypoint},
                )
                available = output.get("available") is True
            if not available:
                missing.append(f"runtime resource: {requirement.name}")
        if implementation.interface_id not in prepared_interfaces and not missing:
            missing.append("compatible local Python implementation")
        operations.append(
            ProviderOperationReadiness(
                interface_id=implementation.interface_id,
                installed=implementation.interface_id in prepared_interfaces,
                missing_requirements=tuple(missing),
            )
        )
    return ProviderInstallResult(
        installation_id=identifier,
        provider_id=build.provider_name,
        build=build.identity.name,
        status="blocked"
        if activation_blocked
        else "awaiting_approval"
        if not registered
        else "blocked"
        if any(item.missing_requirements for item in operations)
        else "ready",
        installed=True,
        registered=registered,
        operations=tuple(operations),
        proposal_id=proposal_id,
        candidate_digest=candidate_digest,
        detail=(
            "Installation and registration grant no execution permissions. "
            "Runtime resources require verification."
        )
        if any(item.missing_requirements for item in operations)
        else None,
    )

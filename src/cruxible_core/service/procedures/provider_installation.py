"""One administrative installation service for repository and transferred packages."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.canonical import canonical_bytes, canonical_digest
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_installation import (
    ProviderCatalog,
    ProviderInstallRequest,
    ProviderInstallResult,
    ProviderOperationReadiness,
    ProviderPackageSummary,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    ProviderInterfaceRegistration,
    parse_provider_interface,
    provider_interface_digest,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    Provider,
    ProviderLocalDistributionPin,
    parse_provider,
    provider_digest,
    provider_path,
    render_provider,
)
from cruxible_core.compiler.compiler import (
    AUTHORITY_VERBS_COMPILER,
    CLAIM_EVIDENCE_COMPILER,
    GOVERNED_TRIGGERS_COMPILER,
    PROVIDER_PACKAGE_COMPILER,
    RESOURCE_BUDGET_COMPILER,
    SDK_SOURCE_COMPILER,
    SOURCE_CHECKED_COMPILER,
    TRIGGER_CAPTURE_COMPILER,
)
from cruxible_core.derived.derived_state import fork_tree
from cruxible_core.errors import ConfigError
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.providers.package_classifier import PackageBucketClassifier, run_package_probe
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
)
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_core.providers.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.providers.provider_local_runtime import (
    verification_format_upgrade,
    verify_provider_installation,
)
from cruxible_core.runtime.execution_policy import enforce_customer_code_execution_supported
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.provider_runtime import (
    ProviderDeploymentConfigV1,
    ProviderRuntimeOperator,
)
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.change_preview import ChangeMode, admit_change_set, change_scope
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
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


def service_provider_catalog(operator: ProviderRuntimeOperator) -> ProviderCatalog:
    packages = []
    for name, path in _repository_projects(operator).items():
        descriptors = tuple(path.glob("src/*/registration.json"))
        if not descriptors:
            continue
        if len(descriptors) != 1:
            raise ConfigError("provider package must have exactly one registration descriptor")
        with package_preparation_errors():
            bundle = toolchain("registration").load_registration(descriptors[0].parent)
        if bundle.manifest.distribution.name != name:
            raise ConfigError("provider catalog name differs from package manifest")
        packages.append(
            ProviderPackageSummary(
                name=name,
                version=bundle.manifest.distribution.version,
                interfaces=tuple(sorted(bundle.definitions)),
            )
        )
    return ProviderCatalog(
        packages=tuple(sorted(packages, key=lambda item: item.name)),
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


def _index_source_files(
    release: IndexRelease, index_urls: tuple[str, ...], custody: Path
) -> tuple[Path, Path, tuple[Path, ...]]:
    transport = _index_transport(index_urls)
    wheel = custody / release.filename
    _write(wheel, fetch_release(release, index_urls, transport))
    lock = custody / "uv.lock"
    _write(lock, embedded_lock(wheel))
    locked = toolchain("resolution").load_uv_lock(lock)
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
) -> tuple[Path, Path, tuple[Path, ...]]:
    enforce_customer_code_execution_supported()
    if release is not None:
        return _index_source_files(release, _index_urls(operator), custody)
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
    if request.package not in {item.name for item in service_provider_catalog(operator).packages}:
        raise ConfigError("provider package is not in the configured repository")
    project = projects[request.package]
    lock = custody / "uv.lock"
    _write(lock, (project / "uv.lock").read_bytes())
    locked = toolchain("resolution").load_uv_lock(lock)
    # The root and every local dependency come from this configured repository;
    # all other dependencies remain constrained by lock hashes and index policy.
    names = {request.package}
    names.update(row["name"] for row in locked.packages if "registry" not in row.get("source", {}))
    uv = shutil.which("uv")
    if uv is None:
        raise ConfigError("provider installation requires uv")
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


def _definition_changes(
    instance: PlaybillInstance,
    document: PackageRegistrationDocumentV1,
    provider: Provider,
    accepted_oid: str,
) -> tuple[Mapping[str, bytes], tuple[str, ...]]:
    tree = instance.immutable_tree_at(accepted_oid)
    candidate = fork_tree(tree)
    changed = []
    interfaces = []
    for desired in document.interface_registrations():
        path = provider_interface_path(desired.interface_id)
        current_bytes = tree.get(path)
        if current_bytes is not None:
            current = parse_provider_interface(current_bytes, path=path)
            if current.lifecycle.state != "live":
                raise ProposalIntegrityError("install cannot silently restore a retired interface")
            if current.model_dump(exclude={"lifecycle"}) == desired.model_dump(
                exclude={"lifecycle"}
            ):
                desired = ProviderInterfaceRegistration.model_validate(current.model_dump())
            else:
                desired = desired.model_copy(
                    update={
                        "lifecycle": ArtifactLifecycle(
                            predecessor_digest=provider_interface_digest(current).tagged
                        )
                    }
                )
        interfaces.append(desired)
        raw = render_provider_interface(desired)
        if raw != current_bytes:
            candidate[path] = raw
            changed.append(path)
    assert isinstance(provider.runtime_artifact.distribution, ProviderLocalDistributionPin)
    assert provider.runtime_artifact.local_env is not None
    desired_provider = document.provider_definition(
        distribution=provider.runtime_artifact.distribution,
        local_env=provider.runtime_artifact.local_env,
        control_domain=provider.control_domain,
        interfaces=tuple(interfaces),
    )
    path = provider_path(provider.identity.name)
    current_bytes = tree.get(path)
    if current_bytes is not None:
        current_provider = parse_provider(current_bytes, path=path)
        if current_provider.lifecycle.state != "live":
            raise ProposalIntegrityError("install cannot silently restore a retired Provider")
        # Package updates replace implementation metadata, preserving independently
        # governed signing and capture configuration.
        desired_provider = desired_provider.model_copy(
            update={
                "signing_keys": current_provider.signing_keys,
                "capture_contract_digests": current_provider.capture_contract_digests,
                "upstream_provenance": current_provider.upstream_provenance,
            }
        )
        if current_provider.model_dump(exclude={"lifecycle"}) == desired_provider.model_dump(
            exclude={"lifecycle"}
        ):
            desired_provider = Provider.model_validate(current_provider.model_dump())
        else:
            desired_provider = desired_provider.model_copy(
                update={
                    "lifecycle": ArtifactLifecycle(
                        predecessor_digest=provider_digest(current_provider).tagged
                    )
                }
            )
    raw = render_provider(desired_provider)
    if raw != current_bytes:
        candidate[path] = raw
        changed.append(path)
    return candidate, tuple(sorted(changed))


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
) -> ProviderInstallResult:
    instance.require_writable()
    enforce_customer_code_execution_supported()
    if instance.accepted_coordinate().compiler not in (
        PROVIDER_PACKAGE_COMPILER,
        RESOURCE_BUDGET_COMPILER,
        SDK_SOURCE_COMPILER,
        CLAIM_EVIDENCE_COMPILER,
        SOURCE_CHECKED_COMPILER,
        TRIGGER_CAPTURE_COMPILER,
        AUTHORITY_VERBS_COMPILER,
        GOVERNED_TRIGGERS_COMPILER,
    ):
        raise ConfigError(
            "provider installation requires an explicit upgrade to the package compiler"
        )
    source: str | dict[str, str] | None = None
    release = None
    if request.package and operator.config.provider_repository is not None:
        if request.version is not None:
            raise ConfigError("a configured provider repository installs its checkout version")
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
        source = {"index": release.index_url, "wheel": release.filename, "sha256": release.sha256}
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
    provider = Provider.model_validate(saved["provider"])
    candidate_tree, changed = _definition_changes(instance, document, provider, mode.head.git_oid)
    if not changed:
        return ProviderInstallResult(
            installation_id=identifier,
            provider_id=provider.identity.name,
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
        provider_id=provider.identity.name,
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
) -> ProviderInstallResult:
    prepared_path = directory / "prepared.json"
    rewrite_prepared = False
    if prepared_path.exists():
        saved = json.loads(prepared_path.read_bytes())
        document = PackageRegistrationDocumentV1.model_validate(saved["document"])
        provider = Provider.model_validate(saved["provider"])
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
        custody = directory / "wheels"
        custody.mkdir(exist_ok=True, mode=0o700)
        with package_preparation_errors():
            wheel, lock_path, dependencies = _source_files(
                instance, operator, request, custody, release
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
                index_urls=_index_urls(operator)
                if release is not None
                else operator.config.provider_index_urls,
            )
        document, provider, deployment = prepared.document, prepared.provider, prepared.deployment
        if document.governed_definitions:
            raise ConfigError("provider installation does not install kit definitions")
        verification = verify_provider_installation(provider, deployment)
        deployment = replace(deployment, installation_verification=verification)
        if operator.process_leases is None:
            raise ConfigError("provider process runtime is unavailable")
        registry = ProviderBucketClassifierRegistry()
        installations = []
        for registration in document.interface_registrations():
            accepted = AcceptedProviderInterfaceRegistration(
                path=provider_interface_path(registration.interface_id),
                registration=registration,
                artifact_digest=provider_interface_digest(registration).tagged,
            )
            installations.append(
                registry.install(
                    accepted,
                    PackageBucketClassifier(registration, deployment, operator.process_leases),
                )
            )
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
                classifier_installations=tuple(installations),
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
    base = instance.accepted_coordinate()
    candidate_tree, changed = _definition_changes(instance, document, provider, base.git_oid)
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
        pending = next(
            (
                item
                for item in service_list_playbill_proposals(instance, status="open").entries
                if item.target_ref == target
            ),
            None,
        )
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
                    provider_id=provider.identity.name,
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
                    # Installation readiness runs outside any Procedure run.
                    deadline=None,
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
        provider_id=provider.identity.name,
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

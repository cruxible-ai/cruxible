"""``cruxible provider publish FILE``: a provider from one Python file, built and installed.

The file's operations are ``@derive`` functions (``cruxible-provider-runtime``
0.6.0 and later). Core never imports that runtime's packaging code: it drives
``python -m cruxible_provider_runtime.single_file`` in an interpreter that has
it, reads the one JSON document it prints, and does the rest here on the
client, as ``provider install WHEEL --lock FILE`` does:

1. ``describe`` the file, and gather the Contracts and CaptureKinds its
   operations reach from the selected instance (``get``, one reference at a
   time, following each Contract's pins), merged under any ``--declares``;
2. ``build`` its ``registration.json`` and ``manifest.json`` against that pool;
3. write a one-module project (the file as ``<module>/__init__.py``), lock it
   (``uv lock``) and build its wheel (``uv build --wheel``), both reproducible,
   so the same file and pool give the same version, wheel and build;
4. transfer the wheel, its lock and any local wheels through the daemon's CAS
   and install it (``ProviderInstallRequest``), with ``move`` when asked.

Without ``--version`` the version is ``0.0.0.dev<N>``, ``N`` taken from the
digest of everything the build reads: an edit names a new build, an unchanged
file the same one.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.provider_installation import ProviderInstallResult
from cruxible_core.cli.provider_wheels import install_provider_wheel
from cruxible_core.errors import ConfigError

if TYPE_CHECKING:
    from cruxible_client.transport.http import CruxibleClient

#: An interpreter to run the runtime's single-file packager in (else this one,
#: ``--runtime-wheel``, or the published runtime through ``uv run``).
RUNTIME_PYTHON_ENV = "CRUXIBLE_PROVIDER_RUNTIME_PYTHON"
SINGLE_FILE_MODULE = "cruxible_provider_runtime.single_file"
#: The first runtime release with ``@derive`` and the single-file packager.
RUNTIME_REQUIREMENT = "cruxible-provider-runtime>=0.6.0"
#: Pinned exactly, so one file and pool build one wheel on every host.
BUILD_BACKEND = "hatchling==1.33.0"
#: How long one packaging step (describe, build, lock, wheel) may take.
STEP_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class PublishedPackage:
    """What ``provider publish`` built, before the daemon installs it."""

    file: str
    distribution: str
    version: str
    module_name: str
    wheel: str
    operations: tuple[str, ...]
    #: The Contracts and CaptureKinds the registration carries, by name.
    carries: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublishResult:
    package: PublishedPackage
    install: ProviderInstallResult

    def as_json(self) -> dict[str, Any]:
        return {
            "package": {
                "file": self.package.file,
                "distribution": self.package.distribution,
                "version": self.package.version,
                "module_name": self.package.module_name,
                "wheel": self.package.wheel,
                "operations": list(self.package.operations),
                "carries": list(self.package.carries),
            },
            "install": self.install.model_dump(mode="json"),
        }


def default_distribution_name(file: Path) -> str:
    """The canonical distribution name a file publishes as: ``stock_signals.py`` ->
    ``stock-signals``."""

    return canonical_distribution_name(file.stem, label=f"{file.name}'s name")


def canonical_distribution_name(value: str, *, label: str = "--name") -> str:
    name = canonicalize_name(value)
    if not name or not all(part.isalnum() and part.isascii() for part in name.split("-")):
        raise ConfigError(
            f"{label} {value!r} is not a distribution name; use letters, digits and - "
            "(pass --name NAME)"
        )
    if name != value and label == "--name":
        raise ConfigError(f"--name {value!r} is not canonical; use {name!r}")
    return name


def normalized_version(value: str) -> str:
    try:
        normalized = str(Version(value))
    except InvalidVersion:
        raise ConfigError(f"--version {value!r} is not a version (PEP 440)") from None
    if normalized != value:
        raise ConfigError(f"--version {value!r} is not normalized; use {normalized!r}")
    return value


def content_version(
    source: bytes, *, distribution: str, module_name: str, pool: Mapping[str, Any]
) -> str:
    """``0.0.0.dev<N>``: ``N`` from the digest of the file, its names and its shape pool.

    A development release of 0.0.0 sorts below every real release, is accepted by
    the runtime's normalized-version check, ``uv lock`` and the install path, and
    names one build per distinct input.
    """

    digest = hashlib.sha256(
        canonical_bytes(
            {
                "source": hashlib.sha256(source).hexdigest(),
                "distribution": distribution,
                "module_name": module_name,
                "pool": dict(pool),
                "build_backend": BUILD_BACKEND,
            }
        )
    ).hexdigest()
    return f"0.0.0.dev{int(digest[:12], 16)}"


# -- the runtime's packager ---------------------------------------------------------------


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise ConfigError("provider publish needs uv on PATH (it locks and builds the wheel)")
    return uv


def _importable(python: str) -> bool:
    try:
        probe = subprocess.run(
            (python, "-c", f"import {SINGLE_FILE_MODULE}"),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except OSError:
        return False
    return probe.returncode == 0


def packager_command(*, runtime_wheel: Path | None, offline: bool) -> tuple[str, ...]:
    """How to run ``python -m cruxible_provider_runtime.single_file``.

    In order: the interpreter ``CRUXIBLE_PROVIDER_RUNTIME_PYTHON`` names; the
    ``--runtime-wheel`` given (so the build's runtime requirement is the wheel's
    own version); this interpreter, when its runtime has the packager; else the
    published runtime through ``uv run``.
    """

    configured = os.environ.get(RUNTIME_PYTHON_ENV)
    if configured:
        if not _importable(configured):
            raise ConfigError(
                f"{RUNTIME_PYTHON_ENV}={configured} cannot import {SINGLE_FILE_MODULE}; "
                f"install {RUNTIME_REQUIREMENT} there"
            )
        return (configured, "-m", SINGLE_FILE_MODULE)
    uv_run = (_uv(), "run", "--no-project", *(("--offline",) if offline else ()), "--with")
    if runtime_wheel is not None:
        return (*uv_run, str(runtime_wheel.resolve()), "python", "-m", SINGLE_FILE_MODULE)
    if importlib.util.find_spec("cruxible_provider_runtime") is not None and (
        importlib.util.find_spec(SINGLE_FILE_MODULE) is not None
    ):
        return (sys.executable, "-m", SINGLE_FILE_MODULE)
    return (*uv_run, RUNTIME_REQUIREMENT, "python", "-m", SINGLE_FILE_MODULE)


def run_packager(
    command: Sequence[str], arguments: Sequence[str], *, file: Path, cwd: Path
) -> dict[str, Any]:
    """One packager command's JSON document; its typed error as a ConfigError."""

    try:
        completed = subprocess.run(
            (*command, *arguments),
            capture_output=True,
            cwd=cwd,
            timeout=STEP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"could not run the provider runtime's packager: {exc}") from exc
    try:
        document = json.loads(completed.stdout)
    except ValueError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("error"), dict):
        error = document["error"]
        kind = str(error.get("kind", "error")).replace("_", " ")
        raise ConfigError(f"{file.name}: {kind}: {error.get('message', '')}".rstrip(": "))
    if completed.returncode != 0 or not isinstance(document, dict):
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()[-5:]
        raise ConfigError(
            f"the provider runtime's packager failed ({' '.join(command[:1])}); it needs "
            f"{RUNTIME_REQUIREMENT}: pass --runtime-wheel, or set {RUNTIME_PYTHON_ENV}"
            + (": " + " / ".join(detail) if detail else "")
        )
    return document


# -- the shape pool -------------------------------------------------------------------------


@dataclass
class _Pool:
    contracts: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    capture_kinds: dict[str, dict[str, Any]] = field(default_factory=dict)
    built_against: list[dict[str, Any]] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "contracts": [self.contracts[key] for key in sorted(self.contracts)],
            "capture_kinds": [self.capture_kinds[key] for key in sorted(self.capture_kinds)],
            "built_against": sorted(
                self.built_against, key=lambda item: json.dumps(item, sort_keys=True)
            ),
        }


def _read_declares(path: Path) -> _Pool:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"--declares {path}: not a readable JSON document ({exc})") from None
    if not isinstance(document, dict) or set(document) - {
        "contracts",
        "capture_kinds",
        "built_against",
    }:
        raise ConfigError(
            f"--declares {path}: a declares block, {{'contracts': [...], 'capture_kinds': "
            "[...], 'built_against': [...]}"
        )
    pool = _Pool()
    for item in document.get("contracts") or ():
        pool.contracts[(str(item["name"]), int(item["version"]))] = item
    for item in document.get("capture_kinds") or ():
        pool.capture_kinds[str(item["name"])] = item
    pool.built_against = list(document.get("built_against") or ())
    return pool


def _operation_refs(operations: Sequence[Mapping[str, Any]]) -> list[str]:
    refs: list[str] = []
    for operation in operations:
        refs.extend((str(operation["in"]), str(operation["out"])))
        refs.extend(
            str(refusal["contract"]) for refusal in (operation.get("refusals") or {}).values()
        )
    return refs


def _field_kinds(field_type: Any) -> list[str]:
    """The CaptureKinds one Contract field type names (``capture<K>``, through lists)."""

    from cruxible_client.contracts.shapes import CaptureType, FieldResultType, ListType

    if isinstance(field_type, CaptureType):
        return [] if field_type.capture_kind == "*" else [field_type.capture_kind]
    if isinstance(field_type, ListType):
        return _field_kinds(field_type.item)
    if isinstance(field_type, FieldResultType):
        return _field_kinds(field_type.value)
    return []


def _accepted_envelope(client: CruxibleClient, instance_id: str, ref: str) -> Any:
    """The accepted envelope of one definition, or None when the instance has none."""

    from cruxible_client.contracts.get_reads import GetRequest

    try:
        result = client.get(instance_id, request=GetRequest(ref=ref, detail="proof", surface="cli"))
    except Exception as exc:
        if getattr(exc, "error_code", None) == "cruxible.get.ref_not_found":
            return None
        raise
    proof = result.proof or {}
    return proof.get("envelope")


def instance_pool(
    client: CruxibleClient,
    instance_id: str,
    operations: Sequence[Mapping[str, Any]],
    given: _Pool,
) -> _Pool:
    """``given`` plus every Contract and CaptureKind the operations reach that the
    instance accepts and ``given`` lacks: their in, out and refusal Contracts, the
    Contracts those pin, the kinds their ``capture`` fields name and those kinds'
    record-set row Contracts. Cost: one read per shape reached."""

    from cruxible_client.contracts.shapes import Contract, all_field_refs, parse_contract_ref

    pool = _Pool(dict(given.contracts), dict(given.capture_kinds), list(given.built_against))
    queue = list(_operation_refs(operations))
    kinds: list[str] = []
    seen: set[str] = set()
    while queue or kinds:
        if kinds:
            kind = kinds.pop()
            if kind in seen or kind in pool.capture_kinds:
                continue
            seen.add(kind)
            envelope = _accepted_envelope(client, instance_id, f"CaptureContract:{kind}")
            if envelope is None:
                continue
            declared: dict[str, Any] = {"name": kind}
            for part in ("decoding", "layout"):
                if envelope.get(part) is not None:
                    declared[part] = envelope[part]
            pool.capture_kinds[kind] = declared
            for record_set in ((envelope.get("layout") or {}).get("records") or {}).values():
                if isinstance(record_set, dict) and isinstance(record_set.get("row"), str):
                    queue.append(record_set["row"])
            continue
        spelling = queue.pop()
        if spelling in seen:
            continue
        seen.add(spelling)
        try:
            ref = parse_contract_ref(spelling)
        except ValueError:
            continue
        key = (ref.name, ref.version)
        contract_document = pool.contracts.get(key)
        if contract_document is None:
            contract_document = _accepted_envelope(
                client, instance_id, f"Contract:{ref.name}@{ref.version}"
            )
            if contract_document is None:
                continue
            pool.contracts[key] = contract_document
        contract = Contract.model_validate(contract_document)
        queue.extend(f"{item.name}@{item.version}" for item in all_field_refs(contract))
        for declared_field in contract.fields.values():
            kinds.extend(_field_kinds(declared_field.type))
    return pool


# -- the project, lock and wheel ------------------------------------------------------------


def _pyproject(
    *,
    distribution: str,
    version: str,
    module_name: str,
    requirement: str,
    entry_points: Mapping[str, str],
    runtime_wheel: str | None,
) -> str:
    points = "".join(
        f"{json.dumps(name)} = {json.dumps(value)}\n"
        for name, value in sorted(entry_points.items())
    )
    sources = (
        ""
        if runtime_wheel is None
        else "\n[tool.uv.sources]\n"
        f'cruxible-provider-runtime = {{ path = "wheels/{runtime_wheel}" }}\n'
    )
    return (
        "[project]\n"
        f'name = "{distribution}"\n'
        f'version = "{version}"\n'
        'requires-python = ">=3.11"\n'
        f"dependencies = [{json.dumps(requirement)}]\n"
        '\n[project.entry-points."cruxible.providers"]\n'
        f"{points}"
        "\n[build-system]\n"
        f'requires = ["{BUILD_BACKEND}"]\n'
        'build-backend = "hatchling.build"\n'
        "\n[tool.hatch.build.targets.wheel]\n"
        f'packages = ["src/{module_name}"]\n'
        f"{sources}"
    )


def _uv_step(arguments: Sequence[str], *, cwd: Path, what: str) -> None:
    try:
        completed = subprocess.run(
            (_uv(), *arguments),
            capture_output=True,
            cwd=cwd,
            timeout=STEP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"{what} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()[-8:]
        raise ConfigError(f"{what} failed: " + " / ".join(detail))


def _wheel_name(path: Path, *, label: str) -> str:
    if not path.is_file() or path.suffix != ".whl":
        raise ConfigError(f"{label} {path} is not a wheel file")
    try:
        return canonicalize_name(parse_wheel_filename(path.name)[0])
    except InvalidWheelFilename as exc:
        raise ConfigError(f"{label} {path} is not a wheel file name") from exc


def publish_provider_file(  # noqa: PLR0913
    client: CruxibleClient,
    instance_id: str,
    file: Path,
    *,
    name: str | None = None,
    version: str | None = None,
    move: bool = False,
    only: tuple[str, ...] = (),
    control_domain: str = "operator",
    runtime_wheel: Path | None = None,
    dependency_wheels: tuple[Path, ...] = (),
    declares: Path | None = None,
    offline: bool = False,
    dry_run: bool | None = None,
    at: str | None = None,
) -> PublishResult:
    """Build ``file`` into a provider wheel and install it on ``instance_id``."""

    file = file.resolve()
    if file.suffix != ".py" or not file.is_file():
        raise ConfigError(f"{file} is not a Python file")
    distribution = canonical_distribution_name(name) if name else default_distribution_name(file)
    module_name = file.stem
    if runtime_wheel is not None and _wheel_name(runtime_wheel, label="--runtime-wheel") != (
        "cruxible-provider-runtime"
    ):
        raise ConfigError(
            f"--runtime-wheel {runtime_wheel} is not a cruxible-provider-runtime wheel"
        )
    for path in dependency_wheels:
        _wheel_name(path, label="--dependency")
    given = _read_declares(declares) if declares is not None else _Pool()
    source = file.read_bytes()
    command = packager_command(runtime_wheel=runtime_wheel, offline=offline)
    with tempfile.TemporaryDirectory(prefix="cruxible-publish-") as scratch:
        root = Path(scratch)
        described = run_packager(
            command,
            ("describe", str(file), "--module-name", module_name),
            file=file,
            cwd=root,
        )
        operations = described.get("operations") or ()
        pool = instance_pool(client, instance_id, operations, given).document()
        chosen = (
            normalized_version(version)
            if version
            else content_version(
                source, distribution=distribution, module_name=module_name, pool=pool
            )
        )
        (root / "declares.json").write_bytes(canonical_bytes(pool))
        metadata = root / "metadata"
        built = run_packager(
            command,
            (
                "build",
                str(file),
                "--module-name",
                module_name,
                "--distribution",
                distribution,
                "--version",
                chosen,
                "--declares",
                str(root / "declares.json"),
                "--out",
                str(metadata),
            ),
            file=file,
            cwd=root,
        )
        project = root / distribution
        package = project / "src" / module_name
        package.mkdir(parents=True)
        (package / "__init__.py").write_bytes(source)
        for item in ("registration.json", "manifest.json"):
            shutil.copyfile(metadata / item, package / item)
        wheels = project / "wheels"
        wheels.mkdir()
        local = [*(() if runtime_wheel is None else (runtime_wheel,)), *dependency_wheels]
        for path in local:
            shutil.copyfile(path, wheels / path.name)
        (project / "pyproject.toml").write_text(
            _pyproject(
                distribution=distribution,
                version=chosen,
                module_name=module_name,
                requirement=str(built.get("runtime_requirement") or RUNTIME_REQUIREMENT),
                entry_points=built.get("entry_points") or {},
                runtime_wheel=None if runtime_wheel is None else runtime_wheel.name,
            )
        )
        offline_flag = ("--offline",) if offline else ()
        links = ("--find-links", str(wheels)) if dependency_wheels else ()
        _uv_step(("lock", *offline_flag, *links), cwd=project, what="uv lock")
        _uv_step(
            ("build", "--wheel", *offline_flag, *links, "--out-dir", str(root / "dist")),
            cwd=project,
            what="uv build --wheel",
        )
        (wheel,) = tuple((root / "dist").glob("*.whl")) or (None,)
        if wheel is None:
            raise ConfigError("uv build --wheel produced no wheel")
        declared = json.loads((metadata / "registration.json").read_bytes()).get("declares") or {}
        published = PublishedPackage(
            file=str(file),
            distribution=distribution,
            version=chosen,
            module_name=module_name,
            wheel=wheel.name,
            operations=tuple(str(item["name"]) for item in operations),
            carries=tuple(
                [f"{item['name']}@{item['version']}" for item in declared.get("contracts") or ()]
                + [str(item["name"]) for item in declared.get("capture_kinds") or ()]
            ),
        )
        install = install_provider_wheel(
            client,
            instance_id,
            wheel=wheel,
            lock=project / "uv.lock",
            dependency_wheels=tuple(local),
            control_domain=control_domain,
            move=move,
            move_only=only,
            dry_run=dry_run,
            at=at,
        )
    return PublishResult(package=published, install=install)


__all__ = [
    "BUILD_BACKEND",
    "RUNTIME_PYTHON_ENV",
    "RUNTIME_REQUIREMENT",
    "PublishResult",
    "PublishedPackage",
    "canonical_distribution_name",
    "content_version",
    "default_distribution_name",
    "instance_pool",
    "normalized_version",
    "packager_command",
    "publish_provider_file",
    "run_packager",
]

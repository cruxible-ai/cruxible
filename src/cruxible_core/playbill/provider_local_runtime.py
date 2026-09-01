"""Daemon-local Provider binding, custody, budget, and invocation driver."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicyV1
from cruxible_client.contracts.procedures.models import ProcedureBudgetV3, ProcedureHardCapsV3
from cruxible_client.contracts.provider_execution import (
    ProviderBudgetTranslationV1,
    ProviderEgressObservationV1,
    ProviderSecretReferenceV1,
    ProviderSecretResolutionPlanV1,
    VerifiedProviderBindingV1,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderImplementationManifestV1,
    ProviderV2,
)
from cruxible_core.playbill.provider_runtime_contract import (
    MAX_PROVIDER_SECRET_BUNDLE_BYTES,
    PROVIDER_RUNTIME_PROTOCOL,
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeResultEnvelopeV1,
    ProviderRuntimeRunContextV1,
    ProviderRuntimeSecretChannelSpecV1,
    ProviderRuntimeSecretRefV1,
    ProviderRuntimeWireError,
    parse_provider_runtime_result,
)

_READ_CHUNK = 65_536


class ProviderLocalRuntimeRefused(PlaybillExecutionError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class LocalProviderDeploymentV1:
    """Operator-owned paths whose bytes must reproduce an accepted local pin."""

    deployment_digest: str
    distribution_path: Path
    lock_path: Path
    environment_path: Path
    environment_manifest_path: Path
    environment_pin_key: str
    interpreter_path: Path


@dataclass(frozen=True)
class BoundLocalProviderV1:
    binding: VerifiedProviderBindingV1
    interpreter_path: Path


@dataclass(frozen=True)
class ProviderDriverOutcomeV1:
    envelope: ProviderRuntimeResultEnvelopeV1
    stderr: str
    duration_seconds: float
    egress: ProviderEgressObservationV1


class ProviderSecretResolverProtocol(Protocol):
    resolver_kind: str

    def resolve(self, reference: ProviderSecretReferenceV1) -> str: ...


class EnvironmentProviderSecretResolver:
    resolver_kind = "environment"

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = os.environ if values is None else values

    def resolve(self, reference: ProviderSecretReferenceV1) -> str:
        key = f"CRUXIBLE_PROVIDER_SECRET_{reference.realm}_{reference.name}_{reference.epoch}"
        try:
            return self._values[key]
        except KeyError as exc:
            raise ProviderLocalRuntimeRefused(
                "secret_epoch_unavailable", f"environment secret epoch {reference.epoch!r} absent"
            ) from exc


class FileProviderSecretStore:
    """Local-operator-only, file-backed custody. It has no served management API."""

    resolver_kind = "file"

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)

    def put(self, reference: ProviderSecretReferenceV1, material: str) -> None:
        if reference.resolver_kind != self.resolver_kind:
            raise ProviderLocalRuntimeRefused(
                "secret_reference_invalid", "file store received a non-file reference"
            )
        target = self._path(reference)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".secret-", text=True)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(material)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    def resolve(self, reference: ProviderSecretReferenceV1) -> str:
        try:
            return self._path(reference).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "secret_epoch_unavailable", f"file secret epoch {reference.epoch!r} absent"
            ) from exc

    def _path(self, reference: ProviderSecretReferenceV1) -> Path:
        return self.root / reference.realm / reference.name / reference.epoch


class ProviderSecretResolverRegistry:
    def __init__(self, resolvers: tuple[ProviderSecretResolverProtocol, ...]) -> None:
        self._resolvers = {resolver.resolver_kind: resolver for resolver in resolvers}

    def validate_plan(self, plan: ProviderSecretResolutionPlanV1) -> None:
        for reference in plan.references:
            if reference.resolver_kind not in self._resolvers:
                raise ProviderLocalRuntimeRefused(
                    "secret_resolver_not_installed",
                    f"secret resolver {reference.resolver_kind!r} is not installed",
                )

    def resolve(self, plan: ProviderSecretResolutionPlanV1) -> dict[str, str]:
        self.validate_plan(plan)
        result = {
            reference.ref: self._resolvers[reference.resolver_kind].resolve(reference)
            for reference in plan.references
        }
        payload = canonical_bytes(result)
        if len(payload) > MAX_PROVIDER_SECRET_BUNDLE_BYTES:
            raise ProviderLocalRuntimeRefused(
                "secret_bundle_too_large",
                f"secret bundle exceeds {MAX_PROVIDER_SECRET_BUNDLE_BYTES} bytes",
            )
        return result


def translate_provider_budget(
    *,
    budget: ProcedureBudgetV3,
    hard_caps: ProcedureHardCapsV3,
    runtime_policy: ProcedureRuntimePolicyV1,
    remaining_wall_clock_microseconds: int,
    result_bytes_cap: int,
    produces_capture: bool,
) -> ProviderBudgetTranslationV1:
    remaining = min(
        remaining_wall_clock_microseconds,
        budget.wall_clock.microseconds,
        hard_caps.max_wall_clock.microseconds,
    )
    runtime_seconds = remaining // 1_000_000
    if runtime_seconds < 1:
        raise ProviderLocalRuntimeRefused(
            "budget_wall_clock", "less than one whole second remains before provider spawn"
        )
    if budget.max_provider_calls < 1 or hard_caps.max_provider_calls < 1:
        raise ProviderLocalRuntimeRefused(
            "budget_max_provider_calls_exceeded", "provider-call budget is exhausted"
        )
    procedure_output_cap = budget.max_capture_bytes if produces_capture else None
    if produces_capture and procedure_output_cap == 0:
        raise ProviderLocalRuntimeRefused(
            "budget_output_size", "Capture-producing provider has a zero-byte output budget"
        )
    hard_output_cap = hard_caps.max_capture_bytes
    if hard_output_cap < 1:
        raise ProviderLocalRuntimeRefused(
            "budget_output_size", "Procedure hard cap allows no provider output bytes"
        )
    candidates = [hard_output_cap, runtime_policy.provider_output_bytes_cap]
    if procedure_output_cap is not None:
        candidates.append(procedure_output_cap)
    return ProviderBudgetTranslationV1(
        remaining_wall_clock_microseconds=remaining_wall_clock_microseconds,
        procedure_wall_clock_microseconds=budget.wall_clock.microseconds,
        hard_cap_wall_clock_microseconds=hard_caps.max_wall_clock.microseconds,
        runtime_wall_clock_seconds=runtime_seconds,
        procedure_output_bytes_cap=procedure_output_cap,
        hard_output_bytes_cap=hard_output_cap,
        policy_output_bytes_cap=runtime_policy.provider_output_bytes_cap,
        runtime_output_bytes_cap=min(candidates),
        max_provider_calls=min(budget.max_provider_calls, hard_caps.max_provider_calls),
        max_items=(
            hard_caps.max_items
            if budget.max_items is None
            else min(budget.max_items, hard_caps.max_items)
        ),
        result_bytes_cap=result_bytes_cap,
    )


class LocalProviderExecutionDriver:
    """Verify a pre-materialized local environment and invoke its runtime child."""

    def bind(
        self,
        accepted_provider: AcceptedProviderV1,
        accepted_interface: AcceptedProviderInterfaceRegistrationV1,
        implementation_digest: str,
        deployment: LocalProviderDeploymentV1,
    ) -> BoundLocalProviderV1:
        provider = accepted_provider.provider
        if not isinstance(provider, ProviderV2):
            raise ProviderLocalRuntimeRefused(
                "unaccepted_provider", "local execution requires an accepted Provider v2"
            )
        if provider.runtime_artifact.status != "accepted":
            raise ProviderLocalRuntimeRefused(
                "acceptance_divergence", "Provider runtime artifact is not accepted"
            )
        registration = accepted_interface.registration
        implementation_record = next(
            (
                item
                for item in provider.implementations
                if item.implementation_digest == implementation_digest
            ),
            None,
        )
        manifest = next(
            (
                item
                for item in provider.runtime_artifact.manifest.implementations
                if item.interface_id == registration.interface_id
                and item.interface_digest == registration.interface_digest
                and self._implementation_digest(provider, item) == implementation_digest
            ),
            None,
        )
        if implementation_record is None or manifest is None:
            raise ProviderLocalRuntimeRefused(
                "ambiguous_implementation", "implementation is absent from accepted closure"
            )
        local_ref = next(
            (
                item
                for item in implementation_record.materialization_references
                if item.kind == "local_env"
                and item.environment_pin_key == deployment.environment_pin_key
            ),
            None,
        )
        if local_ref is None:
            raise ProviderLocalRuntimeRefused(
                "no_compatible_artifact", "accepted Provider has no matching local environment"
            )
        self._verify_file(
            deployment.distribution_path,
            provider.runtime_artifact.distribution.sha256,
            "artifact_hash_mismatch",
        )
        if provider.runtime_artifact.local_env is None:
            raise ProviderLocalRuntimeRefused(
                "unsupported_backend", "accepted Provider has no local environment pin"
            )
        self._verify_file(
            deployment.lock_path,
            provider.runtime_artifact.local_env.lock_sha256,
            "lock_bytes_mismatch",
        )
        environment_manifest = self._read_environment_manifest(deployment.environment_manifest_path)
        if environment_manifest.get("materialization_digest") != local_ref.materialization_digest:
            raise ProviderLocalRuntimeRefused(
                "environment_divergence", "environment seal names another materialization"
            )
        if (
            environment_manifest.get("lock_sha256")
            != provider.runtime_artifact.local_env.lock_sha256
        ):
            raise ProviderLocalRuntimeRefused(
                "environment_divergence", "environment seal names another lock"
            )
        installed = environment_manifest.get("installed_distributions")
        if not isinstance(installed, dict) or "cruxible-provider-runtime" not in installed:
            raise ProviderLocalRuntimeRefused(
                "provider_runtime_not_in_materialization",
                "verified environment does not contain cruxible-provider-runtime",
            )
        if not deployment.interpreter_path.is_file():
            raise ProviderLocalRuntimeRefused(
                "environment_divergence", "verified environment interpreter is absent"
            )
        provider_artifact_digest = accepted_provider.artifact_digest
        interface_pin = next(
            (
                pin
                for pin in provider.pins
                if pin.target.kind == "ProviderInterface"
                and pin.target.name == registration.interface_id
            ),
            None,
        )
        if (
            interface_pin is None
            or interface_pin.artifact_digest != accepted_interface.artifact_digest
        ):
            raise ProviderLocalRuntimeRefused(
                "undeclared_interface", "Provider does not pin this accepted interface"
            )
        return BoundLocalProviderV1(
            binding=VerifiedProviderBindingV1(
                provider_artifact_digest=provider_artifact_digest,
                interface_artifact_digest=accepted_interface.artifact_digest,
                interface_id=registration.interface_id,
                interface_digest=registration.interface_digest,
                implementation_digest=implementation_digest,
                deployment_digest=deployment.deployment_digest,
                materialization_digest=local_ref.materialization_digest,
                environment_manifest_digest=_sha256(
                    deployment.environment_manifest_path.read_bytes()
                ),
                entrypoint=manifest.entrypoint,
                declared_endpoints=tuple(
                    sorted(set(manifest.declared_endpoints), key=lambda item: item.encode())
                ),
            ),
            interpreter_path=deployment.interpreter_path,
        )

    def invoke(
        self,
        binding: BoundLocalProviderV1,
        context: ProviderRuntimeRunContextV1,
        *,
        secret_plan: ProviderSecretResolutionPlanV1,
        secret_resolvers: ProviderSecretResolverRegistry,
    ) -> ProviderDriverOutcomeV1:
        if context.implementation_digest != binding.binding.implementation_digest:
            raise ProviderLocalRuntimeRefused(
                "provider_protocol_violation", "run context names another implementation"
            )
        if context.protocol_version != PROVIDER_RUNTIME_PROTOCOL:
            raise ProviderLocalRuntimeRefused(
                "unsupported_protocol", "run context protocol is unsupported"
            )
        secrets = secret_resolvers.resolve(secret_plan)
        with _open_secret_channel(secrets) as secret_fd:
            channel = (
                ProviderRuntimeSecretChannelSpecV1(
                    fd=secret_fd,
                    refs=tuple(
                        ProviderRuntimeSecretRefV1(ref=item.ref, purpose=item.purpose)
                        for item in secret_plan.references
                    ),
                )
                if secret_fd is not None
                else None
            )
            actual_context = context.model_copy(update={"secret_channel": channel})
            # The external runtime wire law permits finite floats (the wall-clock
            # budget is one), so use its exact model-order JSON spelling rather
            # than Playbill's narrower governed-artifact canonical value law.
            context_bytes = actual_context.to_json()
            _assert_no_secret(context_bytes, secrets, where="run context")
            process = _run_child(
                binding.interpreter_path,
                entrypoint=binding.binding.entrypoint,
                context=context_bytes,
                budgets=actual_context.budgets,
                secret_fd=secret_fd,
            )
        _assert_no_secret(process.stdout, secrets, where="provider stdout")
        _assert_no_secret(process.stderr, secrets, where="provider stderr")
        try:
            envelope = parse_provider_runtime_result(process.stdout)
        except ProviderRuntimeWireError:
            raise
        if envelope.run_id != context.run_id:
            raise ProviderLocalRuntimeRefused(
                "provider_protocol_violation", "provider envelope names another run"
            )
        dynamic = cast(
            tuple[Literal["dynamic:target-from-run-input"], ...],
            tuple(
                value
                for value in binding.binding.declared_endpoints
                if value == "dynamic:target-from-run-input"
            ),
        )
        declared = tuple(
            value
            for value in binding.binding.declared_endpoints
            if not value.startswith("dynamic:")
        )
        observed = tuple(
            sorted(set(envelope.trace.endpoints_contacted), key=lambda item: item.encode())
        )
        return ProviderDriverOutcomeV1(
            envelope=envelope,
            stderr=process.stderr.decode("utf-8", "replace"),
            duration_seconds=round(process.duration_seconds, 4),
            egress=ProviderEgressObservationV1(
                declared_endpoints=declared,
                observed_endpoints=observed,
                dynamic_endpoint_forms=dynamic,
                observer_backend="local-instrumented-client",
                observer_grade="attribution",
            ),
        )

    @staticmethod
    def _implementation_digest(
        provider: ProviderV2, manifest: ProviderImplementationManifestV1
    ) -> str:
        return next(
            item.implementation_digest
            for item in provider.implementations
            if item.interface_id == manifest.interface_id and item.entrypoint == manifest.entrypoint
        )

    @staticmethod
    def _verify_file(path: Path, expected: str, code: str) -> None:
        try:
            actual = _sha256(path.read_bytes())
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                code, f"required file {path.name!r} is absent"
            ) from exc
        if actual != expected:
            raise ProviderLocalRuntimeRefused(code, f"{path.name!r} digest does not reproduce")

    @staticmethod
    def _read_environment_manifest(path: Path) -> dict[str, object]:
        try:
            raw = path.read_bytes()
            parsed = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ProviderLocalRuntimeRefused(
                "cache_integrity", "environment seal is absent or malformed"
            ) from exc
        if not isinstance(parsed, dict) or canonical_bytes(parsed) != raw:
            raise ProviderLocalRuntimeRefused(
                "cache_integrity", "environment seal is not canonical JSON"
            )
        return parsed


@dataclass(frozen=True)
class _ProcessOutcome:
    stdout: bytes
    stderr: bytes
    duration_seconds: float


@contextmanager
def _open_secret_channel(secrets: Mapping[str, str]) -> Iterator[int | None]:
    if not secrets:
        yield None
        return
    payload = canonical_bytes(dict(secrets))
    if len(payload) > MAX_PROVIDER_SECRET_BUNDLE_BYTES:
        raise ProviderLocalRuntimeRefused("secret_bundle_too_large", "secret bundle too large")
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)

    def write() -> None:
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(write_fd, payload[offset:])
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                os.close(write_fd)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        yield read_fd
    finally:
        with contextlib.suppress(OSError):
            os.close(read_fd)
        writer.join(timeout=5)


def _assert_no_secret(payload: bytes, secrets: Mapping[str, str], *, where: str) -> None:
    leaked = sorted(ref for ref, value in secrets.items() if value and value.encode() in payload)
    if leaked:
        raise ProviderLocalRuntimeRefused("secret_leak", f"secret material leaked in {where}")


def _run_child(
    interpreter: Path,
    *,
    entrypoint: str,
    context: bytes,
    budgets: ProviderRuntimeBudgetsV1,
    secret_fd: int | None,
) -> _ProcessOutcome:
    started = time.monotonic()
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with tempfile.TemporaryDirectory(prefix="cruxible-provider-") as scratch:
        process = subprocess.Popen(
            [
                str(interpreter),
                "-m",
                "cruxible_provider_runtime.child",
                "--entrypoint",
                entrypoint,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(() if secret_fd is None else (secret_fd,)),
            cwd=scratch,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )

        def write_stdin() -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(context)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        writer = threading.Thread(target=write_stdin, daemon=True)
        writer.start()
        assert process.stdout is not None and process.stderr is not None
        streams = (process.stdout, process.stderr)
        buffers = {stream.fileno(): bytearray() for stream in streams}
        selector = selectors.DefaultSelector()
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        deadline = started + budgets.wall_clock_seconds
        open_streams = len(streams)
        refusal: tuple[str, str] | None = None
        while open_streams and refusal is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                refusal = ("budget_wall_clock", "provider exceeded wall-clock budget")
                break
            for key, _ in selector.select(timeout=min(remaining, 0.1)):
                chunk = os.read(key.fd, _READ_CHUNK)
                if not chunk:
                    selector.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                buffers[key.fd].extend(chunk)
                if sum(len(value) for value in buffers.values()) > budgets.output_bytes:
                    refusal = ("budget_output_size", "provider exceeded aggregate output budget")
                    break
        selector.close()
        if refusal is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            raise ProviderLocalRuntimeRefused(*refusal)
        process.wait(timeout=max(deadline - time.monotonic(), 0.001))
        writer.join(timeout=1)
        return _ProcessOutcome(
            stdout=bytes(buffers[process.stdout.fileno()]),
            stderr=bytes(buffers[process.stderr.fileno()]),
            duration_seconds=time.monotonic() - started,
        )


__all__ = [
    "BoundLocalProviderV1",
    "EnvironmentProviderSecretResolver",
    "FileProviderSecretStore",
    "LocalProviderDeploymentV1",
    "LocalProviderExecutionDriver",
    "ProviderDriverOutcomeV1",
    "ProviderLocalRuntimeRefused",
    "ProviderSecretResolverRegistry",
    "translate_provider_budget",
]

"""Daemon-local Provider binding, custody, budget, and invocation driver."""

from __future__ import annotations

import base64
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
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicyV1
from cruxible_client.contracts.procedures.models import ProcedureBudgetV3, ProcedureHardCapsV3
from cruxible_client.contracts.provider_execution import (
    ProviderBudgetTranslationV1,
    ProviderEgressObservationV1,
    ProviderExternalOccurrencePlanV1,
    ProviderSecretBindingIdentityV1,
    ProviderSecretReferenceV1,
    ProviderSecretResolutionPlanV1,
    VerifiedProviderBindingV1,
    provider_secret_binding_identity_digest,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderImplementationManifestV1,
    ProviderV2,
)
from cruxible_core.playbill.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
)
from cruxible_core.playbill.provider_runtime_contract import (
    MAX_PROVIDER_SECRET_BUNDLE_BYTES,
    PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS,
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
    provider_runtime_version: str


@dataclass(frozen=True)
class BoundLocalProviderV1:
    binding: VerifiedProviderBindingV1
    interpreter_path: Path


@dataclass(frozen=True)
class ProviderDriverOutcomeV1:
    """Local result whose ``duration_seconds`` reads VALIDITY WINDOW."""

    envelope: ProviderRuntimeResultEnvelopeV1
    stderr: str
    duration_seconds: float
    egress: ProviderEgressObservationV1
    verified_binding: VerifiedProviderBindingV1


class ProviderSecretResolverProtocol(Protocol):
    resolver_kind: str

    def resolve(self, reference: ProviderSecretReferenceV1) -> str: ...


class EnvironmentProviderSecretResolver:
    resolver_kind = "environment"

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = os.environ if values is None else values

    def resolve(self, reference: ProviderSecretReferenceV1) -> str:
        key = provider_environment_secret_key(reference)
        try:
            return self._values[key]
        except KeyError as exc:
            raise ProviderLocalRuntimeRefused(
                "secret_epoch_unavailable", f"environment secret epoch {reference.epoch!r} absent"
            ) from exc


def provider_environment_secret_key(reference: ProviderSecretReferenceV1) -> str:
    """Return a collision-free daemon custody key for one secret epoch."""

    identity_digest = provider_secret_binding_identity_digest(
        ProviderSecretBindingIdentityV1(realm=reference.realm, name=reference.name)
    ).removeprefix("sha256:")
    epoch = reference.epoch.encode("utf-8")
    return f"CRUXIBLE_PROVIDER_SECRET_{identity_digest}_{len(epoch)}_{epoch.hex()}"


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


class ProviderLocalRuntimeInvoker:
    """Operator-wired adapter from an admitted occurrence plan to the local child."""

    def __init__(
        self,
        *,
        deployments_by_digest: Mapping[str, LocalProviderDeploymentV1],
        accepted_providers_by_digest: Mapping[str, AcceptedProviderV1],
        accepted_interfaces_by_digest: Mapping[str, AcceptedProviderInterfaceRegistrationV1],
        secret_resolvers: ProviderSecretResolverRegistry,
        process_leases: ProviderProcessLeaseStore,
        driver: LocalProviderExecutionDriver | None = None,
    ) -> None:
        self._deployments = dict(deployments_by_digest)
        self._accepted_providers = dict(accepted_providers_by_digest)
        self._accepted_interfaces = dict(accepted_interfaces_by_digest)
        self._secret_resolvers = secret_resolvers
        self._process_leases = process_leases
        self._driver = driver or LocalProviderExecutionDriver()

    def bind_provider(
        self,
        *,
        occurrence: ProviderExternalOccurrencePlanV1,
    ) -> BoundLocalProviderV1:
        try:
            deployment = self._deployments[occurrence.local_execution.deployment_digest]
        except KeyError as exc:
            raise ProviderLocalRuntimeRefused(
                "no_compatible_artifact",
                "the admitted local deployment is not installed by this operator",
            ) from exc
        try:
            accepted_provider = self._accepted_providers[occurrence.provider_artifact_digest]
        except KeyError as exc:
            raise ProviderLocalRuntimeRefused(
                "unaccepted_provider",
                "the admitted Provider artifact is unavailable at the bound coordinate",
            ) from exc
        try:
            accepted_interface = self._accepted_interfaces[occurrence.interface_artifact_digest]
        except KeyError as exc:
            raise ProviderLocalRuntimeRefused(
                "unknown_interface",
                "the admitted Provider interface is unavailable at the bound coordinate",
            ) from exc
        bound = self._driver.bind(
            accepted_provider,
            accepted_interface,
            occurrence.implementation_digest,
            deployment,
        )
        if bound.binding != occurrence.local_execution:
            raise ProviderLocalRuntimeRefused(
                "acceptance_divergence",
                "spawn-time Provider binding differs from the admitted binding",
            )
        return bound

    def invoke_provider(
        self,
        *,
        occurrence: ProviderExternalOccurrencePlanV1,
        context: ProviderRuntimeRunContextV1,
        invocation_id: str,
        bound: BoundLocalProviderV1,
    ) -> ProviderDriverOutcomeV1:
        # Rebind immediately before every spawn.  The earlier bound value is
        # journal-before-progress evidence; this second read closes mutation
        # between the durable start and process creation.
        fresh = self.bind_provider(occurrence=occurrence)
        if fresh != bound:
            raise ProviderLocalRuntimeRefused(
                "environment_divergence",
                "Provider binding changed after the durable invocation start",
            )
        return self._driver.invoke(
            fresh,
            context,
            secret_plan=occurrence.secret_plan,
            secret_resolvers=self._secret_resolvers,
            invocation_id=invocation_id,
            process_leases=self._process_leases,
        )


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
    hard_output_cap = hard_caps.max_capture_bytes if produces_capture else None
    if produces_capture and hard_output_cap == 0:
        raise ProviderLocalRuntimeRefused(
            "budget_output_size", "Procedure hard cap allows no provider output bytes"
        )
    candidates = [runtime_policy.provider_output_bytes_cap]
    if hard_output_cap is not None:
        candidates.append(hard_output_cap)
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
        environment_manifest, environment_manifest_bytes = self._read_environment_manifest(
            deployment.environment_manifest_path
        )
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
        if installed["cruxible-provider-runtime"] != deployment.provider_runtime_version:
            raise ProviderLocalRuntimeRefused(
                "provider_runtime_not_in_materialization",
                "verified environment contains another cruxible-provider-runtime version",
            )
        if not deployment.interpreter_path.is_file():
            raise ProviderLocalRuntimeRefused(
                "environment_divergence", "verified environment interpreter is absent"
            )
        try:
            environment_root = deployment.environment_path.resolve(strict=True)
            interpreter_path = deployment.interpreter_path.resolve(strict=True)
            manifest_path = deployment.environment_manifest_path.resolve(strict=True)
        except OSError as exc:
            raise ProviderLocalRuntimeRefused(
                "environment_divergence", "verified environment paths are unavailable"
            ) from exc
        if (
            not environment_root.is_dir()
            or not interpreter_path.is_relative_to(environment_root)
            or not manifest_path.is_relative_to(environment_root)
        ):
            raise ProviderLocalRuntimeRefused(
                "environment_divergence",
                "interpreter and environment seal must remain inside the verified environment",
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
                environment_manifest_digest=_sha256(environment_manifest_bytes),
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
        invocation_id: str,
        process_leases: ProviderProcessLeaseStore,
    ) -> ProviderDriverOutcomeV1:
        if context.implementation_digest != binding.binding.implementation_digest:
            raise ProviderLocalRuntimeRefused(
                "provider_protocol_violation", "run context names another implementation"
            )
        if context.protocol_version != PROVIDER_RUNTIME_PROTOCOL:
            raise ProviderLocalRuntimeRefused(
                "unsupported_protocol", "run context protocol is unsupported"
            )
        unknown_dynamic = tuple(
            endpoint
            for endpoint in binding.binding.declared_endpoints
            if endpoint.startswith("dynamic:")
            and endpoint not in PROVIDER_RUNTIME_DYNAMIC_ENDPOINT_FORMS
        )
        if unknown_dynamic:
            raise ProviderLocalRuntimeRefused(
                "undeclared_egress",
                "verified Provider binding contains an unknown dynamic endpoint form",
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
                invocation_id=invocation_id,
                process_leases=process_leases,
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
                observer_backend="child-self-report",
                observer_grade="attribution",
            ),
            verified_binding=binding.binding,
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
    def _read_environment_manifest(path: Path) -> tuple[dict[str, object], bytes]:
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
        return parsed, raw


@dataclass(frozen=True)
class _ProcessOutcome:
    """Child-process result whose ``duration_seconds`` reads VALIDITY WINDOW."""

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
    leaked = sorted(
        ref
        for ref, value in secrets.items()
        if value
        and any(
            variant in payload
            for raw in (value.encode("utf-8"),)
            for variant in (raw, raw[::-1], base64.b64encode(raw))
        )
    )
    if leaked:
        raise ProviderLocalRuntimeRefused("secret_leak", f"secret material leaked in {where}")


def _run_child(
    interpreter: Path,
    *,
    entrypoint: str,
    context: bytes,
    budgets: ProviderRuntimeBudgetsV1,
    secret_fd: int | None,
    invocation_id: str,
    process_leases: ProviderProcessLeaseStore,
) -> _ProcessOutcome:
    """Run one child; ``started``/``deadline``/elapsed duration read VALIDITY WINDOW."""

    started = time.monotonic()
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with tempfile.TemporaryDirectory(prefix="cruxible-provider-") as scratch:
        os.chmod(scratch, 0o700)
        environment["HOME"] = scratch
        environment["TMPDIR"] = scratch
        command = [
            str(interpreter),
            "-m",
            "cruxible_provider_runtime.child",
            "--entrypoint",
            entrypoint,
        ]
        control_path = process_leases.prepare_control_path(invocation_id)
        wrapper = Path(scratch) / "provider_child_fence.py"
        wrapper.write_text(_CHILD_FENCE_WRAPPER, encoding="utf-8")
        command = [
            str(interpreter),
            str(wrapper),
            invocation_id,
            str(control_path),
            entrypoint,
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(() if secret_fd is None else (secret_fd,)),
            cwd=scratch,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
        if secret_fd is not None:
            # The child owns its inherited duplicate from this point. Keeping a
            # parent reader open would defeat EOF-based secret-channel custody.
            with contextlib.suppress(OSError):
                os.close(secret_fd)
        try:
            process_leases.publish(
                invocation_id,
                pid=process.pid,
                process_group_id=process.pid,
            )
            lease = process_leases.require(invocation_id)
        except ProviderLocalRuntimeRefused:
            # A child that cannot prove its own lease must not outlive the
            # failed invocation boundary.
            _terminate_process_group(process, process_leases.recovery_timeout_seconds)
            record_path, control_path = process_leases.paths(invocation_id)
            for path in (record_path, control_path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            raise

        try:
            return _collect_child_output(
                process,
                context=context,
                budgets=budgets,
                started=started,
            )
        finally:
            _terminate_process_group(process, process_leases.recovery_timeout_seconds)
            process_leases.release(lease)


def _collect_child_output(
    process: subprocess.Popen[bytes],
    *,
    context: bytes,
    budgets: ProviderRuntimeBudgetsV1,
    started: float,
) -> _ProcessOutcome:
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
    try:
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
                total = sum(len(value) for value in buffers.values())
                chunk = os.read(key.fd, min(_READ_CHUNK, budgets.output_bytes - total + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                if total + len(chunk) > budgets.output_bytes:
                    refusal = ("budget_output_size", "provider exceeded aggregate output budget")
                    break
                buffers[key.fd].extend(chunk)
        if refusal is not None:
            raise ProviderLocalRuntimeRefused(*refusal)
        try:
            process.wait(timeout=max(deadline - time.monotonic(), 0.001))
        except subprocess.TimeoutExpired as exc:
            raise ProviderLocalRuntimeRefused(
                "budget_wall_clock", "provider exceeded wall-clock budget"
            ) from exc
        writer.join(timeout=1)
        return _ProcessOutcome(
            stdout=bytes(buffers[process.stdout.fileno()]),
            stderr=bytes(buffers[process.stderr.fileno()]),
            duration_seconds=time.monotonic() - started,
        )
    finally:
        selector.close()


def _terminate_process_group(process: subprocess.Popen[bytes], timeout_seconds: float) -> None:
    """SIGKILL and verify one child-owned group before releasing its fence."""

    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            process.wait(timeout=min(0.05, max(deadline - time.monotonic(), 0.001)))
        except subprocess.TimeoutExpired:
            continue
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            continue
    raise ProviderLocalRuntimeRefused(
        "provider_process_group_survived_recovery",
        "provider process group survived its configured termination deadline",
    )


_CHILD_FENCE_WRAPPER = """\
import os
import runpy
import socket
import sys
import threading

invocation_id, control_path, entrypoint = sys.argv[1:4]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
os.chmod(control_path, 0o600)
server.listen(2)

def echo():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            data = connection.recv(4096).decode("utf-8")
            connection.sendall(invocation_id.encode("utf-8") if data == invocation_id else b"")

threading.Thread(target=echo, daemon=True).start()
sys.argv = ["cruxible_provider_runtime.child", "--entrypoint", entrypoint]
runpy.run_module("cruxible_provider_runtime.child", run_name="__main__")
"""


__all__ = [
    "BoundLocalProviderV1",
    "EnvironmentProviderSecretResolver",
    "FileProviderSecretStore",
    "LocalProviderDeploymentV1",
    "LocalProviderExecutionDriver",
    "ProviderDriverOutcomeV1",
    "ProviderLocalRuntimeRefused",
    "ProviderLocalRuntimeInvoker",
    "ProviderSecretResolverRegistry",
    "provider_environment_secret_key",
    "translate_provider_budget",
]

"""Real built wheels cross the SDK/HTTP boundary; no substituted provider invoker."""

import base64
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import (
    CallStep,
    ProjectStep,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.provider_wheels import install_provider_wheel
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import PROVIDER_RUNTIME_CONFIG_PATH
from tests.support.procedures import procedure_limits, procedure_spec_data

# Executor-only fixtures: these tests author Procedure step kinds the template
# closes at authoring (D-R37 h1) to cover their executor handlers (h3).
pytestmark = pytest.mark.executor_only


@pytest.fixture
def installer_http(tmp_path, monkeypatch, request, provider_checkout):
    state = tmp_path / "server-state"
    config = state / PROVIDER_RUNTIME_CONFIG_PATH
    config.parent.mkdir(parents=True)
    # Explicit test operator policy. Production has no implicit network indexes.
    config.write_text(
        json.dumps(
            {
                "provider_repository": str(provider_checkout.repository),
                "provider_index_urls": [
                    "https://pypi.org/simple",
                    "https://files.pythonhosted.org/",
                ],
            }
        )
    )
    from tests.test_server.conftest import _playbill_http

    world = _playbill_http(
        tmp_path, monkeypatch, require_independent_approval=getattr(request, "param", False)
    )
    # No kit and no Contract is accepted before an install: a package carries
    # the shapes its operations use.
    http, instance_id, reviewer = next(world)
    yield http, instance_id, reviewer
    for _ in world:
        pass


def _accept(http, instance_id, reviewer, members, *, name):
    """Accept ``members`` over the head: one proposal, the reviewer's approval."""

    from tests.core_support._candidate_support import submit_member_candidate
    from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

    proposal = submit_member_candidate(
        get_playbill_manager().get(instance_id),
        members=members,
        actor_id="operator",
        proposal_name=name,
        proposal_family="procedure",
        timestamp="2026-10-29T11:00:00.000000Z",
    ).proposal
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    _approve_and_activate(http, instance_id, reviewer, proposal.admission.proposal_id)


def test_transfer_install_and_restart_reuse(
    installer_http, tmp_path, monkeypatch, provider_checkout
):
    from tests.support.provider_installation import build_local_call

    http, instance_id, _ = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    wheels = provider_checkout.wheels
    # File reading is core's built-in; the transferred wheel is the unpublished
    # local-call package.
    wheel, lock = build_local_call(tmp_path, repository)
    arguments = dict(
        wheel=wheel,
        lock=lock,
        dependency_wheels=(next(wheels.glob("cruxible_provider_runtime-*.whl")),),
    )
    result = install_provider_wheel(client, instance_id, **arguments)
    assert result.registered, result
    assert result.status == "ready", result
    operator = get_playbill_manager().provider_runtime_operator()
    assert len(operator.config.deployments) == 1
    assert operator.config.deployments[0].installation_verification
    from cruxible_core.service.procedures import provider_installation as service

    def forbidden(*args, **kwargs):
        raise AssertionError("retry must reuse prepared installation")

    monkeypatch.setattr(service, "prepare_provider_package", forbidden)
    monkeypatch.setattr(service, "verify_provider_installation", forbidden)
    get_playbill_manager().clear()
    again = install_provider_wheel(client, instance_id, **arguments)
    assert again.installation_id == result.installation_id
    assert again.registered and again.status == "ready"


def _author_line(  # noqa: PLR0913
    http, instance_id, reviewer, *, spec, line, bindings, parameters, name, acquisition=None
):
    """Author ``spec`` and a Line binding its slots, each accepted by the reviewer.

    The Line binds every slot to one exact ``{build, operation}`` at acceptance.
    """

    from cruxible_client.contracts.authoring.inputs import (
        LineInput,
        ProcedureInput,
        lower_authoring_input,
    )
    from cruxible_client.contracts.procedures.authored import ProcedureSpec
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.support.procedures import refs

    instance = get_playbill_manager().get(instance_id)
    for kind, payload in (
        (
            "procedure",
            ProcedureInput(kind="procedure", spec=ProcedureSpec.model_validate(refs(spec))),
        ),
        (
            "line",
            LineInput.model_validate(
                {
                    "kind": "line",
                    "name": line,
                    "procedure_name": spec["procedure"],
                    "bindings": bindings,
                    "parameters": parameters,
                    "acquisition": acquisition or {},
                    "occurrence_epoch": 1,
                }
            ),
        ),
    ):
        intent = (
            AuthoringIntentCoordinator.for_instance(instance)
            .create(
                actor=AuthenticatedActor(actor_id="operator"),
                payload=lower_authoring_input(payload),
                canonical_timestamp="2026-10-29T11:00:00.000000Z",
            )
            .intent
        )
        lowered = lower_authoring(instance, intent=intent, actor_id="operator")
        _accept(http, instance_id, reviewer, dict(lowered.changed_members), name=f"{name}-{kind}")


def _run_call(http, instance_id, reviewer, *, build, value, name="installed-package-call"):
    """Run ``local.increment`` of the installed ``build`` through a Line bound to it.

    The Procedure declares one slot of the operation's exact signature; the Line
    binds it to the installed build's operation at acceptance, and the run goes
    through the daemon's real provider operator to the installed runtime child.
    """

    from tests.support.provider_installation import (
        LOCAL_CALL_SLOT,
        local_call_capability_files,
        local_call_contract_files,
    )

    line = f"test.{name}-line"
    # The slot's capability pins its Contract on the instance; the build carries
    # its own copy, and the binding reads that one.
    members = {**local_call_contract_files(), **local_call_capability_files()}
    instance = get_playbill_manager().get(instance_id)
    if not set(members) <= set(instance.tree_at(instance.accepted_coordinate().git_oid)):
        _accept(http, instance_id, reviewer, members, name="local-call-capability")
    _author_line(
        http,
        instance_id,
        reviewer,
        spec=procedure_spec_data(
            procedure=f"test.{name}",
            params={"n": "int"},
            output={"n": "int"},
            slots={"increment": LOCAL_CALL_SLOT},
            steps=[
                CallStep(
                    call="result", slot="increment", input={"n": {"$ref": "param", "path": ["n"]}}
                )
            ],
            returns="result",
            hard_caps=procedure_limits(provider_calls=1),
        ),
        line=line,
        bindings={"increment": build},
        parameters=value,
        name=name,
    )
    return _run_line(instance_id, line)


def _run_line(instance_id, line):
    """One manual run of an accepted Line through the daemon's real provider operator."""

    state = _line_state(instance_id, line)
    assert state.status == "succeeded", state.terminal
    return state.result


def _line_state(instance_id, line, **arguments):
    """The run state of one manual run of an accepted Line, whatever its outcome."""

    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.support.operation_world import actor

    manager = get_playbill_manager()
    return service_run_playbill_line(
        manager.get(instance_id),
        path_identity_digest=line,
        request=LineRunRequest(line=line),
        actor_context=actor(),
        caller_rung=2,
        provider_runtime_operator=manager.provider_runtime_operator(),
        **arguments,
    )


@pytest.mark.parametrize("legacy_proof", [False, True])
def test_relocated_installation_reverifies_and_runs_after_restart(
    installer_http, tmp_path, monkeypatch, legacy_proof, provider_checkout
):
    import shutil

    from cruxible_core.providers.provider_local_runtime import _deployment_identity
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
    from cruxible_core.service.procedures import provider_installation as service
    from tests.support.provider_installation import build_local_call

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    wheels = provider_checkout.wheels
    wheel, lock = build_local_call(tmp_path, repository)
    arguments = dict(
        wheel=wheel,
        lock=lock,
        dependency_wheels=(next(wheels.glob("cruxible_provider_runtime-*.whl")),),
    )
    installed = install_provider_wheel(client, instance_id, **arguments)
    assert installed.status == "ready", installed
    manager = get_playbill_manager()
    original = manager.provider_runtime_operator()
    (deployment,) = original.config.deployments
    prepared = next(original.state_root.rglob("prepared.json"))
    saved = json.loads(prepared.read_bytes())
    if legacy_proof:
        # Persist a historical absolute-path proof, as the prior installer did.
        proof = deployment.installation_verification.model_dump(mode="json")
        proof.update(
            tag="cruxible-provider-installation-verification-v1",
            deployment_identity_digest=_deployment_identity(original._deployment(deployment)),
        )
        config = original.config.model_dump(mode="json")
        config["deployments"][0]["installation_verification"] = proof
        (original.state_root / PROVIDER_RUNTIME_CONFIG_PATH).write_text(json.dumps(config))
        saved["deployment"]["installation_verification"] = proof
        prepared.write_text(json.dumps(saved))
    old_prepared = prepared.read_bytes()
    # Move the provider runtime independently of the ledger/instance registry.
    # No environment file or persisted relative deployment path is rewritten.
    relocated = tmp_path / "relocated-runtime"
    relocated.mkdir()
    shutil.move(str(original.state_root / "provider-environments"), relocated)
    target_config = relocated / PROVIDER_RUNTIME_CONFIG_PATH
    target_config.parent.mkdir(parents=True)
    shutil.copyfile(original.state_root / PROVIDER_RUNTIME_CONFIG_PATH, target_config)
    operator = ProviderRuntimeOperator(relocated)
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda operator=operator: operator)

    def no_rebuild(*args, **kwargs):
        raise AssertionError("relocation must reuse the installed environment")

    monkeypatch.setattr(service, "prepare_provider_package", no_rebuild)
    verified = install_provider_wheel(client, instance_id, **arguments, reverify=True)
    assert verified.status == "ready", verified
    assert verified.installation_id == installed.installation_id
    (current,) = operator.config.deployments
    assert current.installation_verification.tag.endswith("-v2")
    if legacy_proof:
        # Replay a crash between publishing the proof and updating the cache.
        prepared.write_bytes(old_prepared)
    operator = ProviderRuntimeOperator(relocated)
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda operator=operator: operator)
    monkeypatch.setattr(service, "verify_provider_installation", no_rebuild)
    again = install_provider_wheel(client, instance_id, **arguments)
    assert again.status == "ready", again
    assert json.loads(prepared.read_bytes())["deployment"]["installation_verification"] == (
        current.installation_verification.model_dump(mode="json")
    )
    assert _run_call(http, instance_id, reviewer, build=installed.build, value={"n": 2}) == {"n": 3}


@pytest.fixture(scope="session")
def local_call_dependency_wheels(provider_checkout, tmp_path_factory):
    """Provision the host's exact locked wheels once, before testing installs.

    The runtime wheel alone leaves registry dependencies to a live PyPI download
    on every installation. Transfer the complete closure so an unrelated HTTP
    failure cannot interrupt the deployment-preservation scenario halfway through.
    ArtifactFetcher verifies every download against the lock's SHA256.
    """
    import platform
    import sys

    from cruxible_provider_runtime import index, resolution, wheels
    from packaging.markers import default_environment
    from packaging.tags import sys_tags

    from cruxible_core.providers.package_materialization import ArtifactTransport

    runtime = next(provider_checkout.wheels.glob("cruxible_provider_runtime-*.whl"))
    lock = resolution.load_uv_lock(
        provider_checkout.repository / "packages/cruxible-provider-noop/uv.lock"
    )
    environment = resolution.MarkerEnvironment(
        id=f"{sys.platform}-{platform.machine().lower()}-cp{sys.version_info.major}{sys.version_info.minor}",
        markers=default_environment(),
        tags=tuple(str(tag) for tag in sys_tags()),
    )
    resolved = resolution.resolve(
        lock,
        "cruxible-provider-noop",
        environment,
        local_wheels={"cruxible-provider-runtime": wheels.wheel_pin(runtime)},
    )
    fetcher = index.ArtifactFetcher(
        index.IndexConfig(index_urls=("https://files.pythonhosted.org/",)),
        ArtifactTransport(),
    )
    directory = tmp_path_factory.mktemp("local-call-dependencies")
    dependencies = [runtime]
    for distribution in resolved.distributions:
        if distribution.name == "cruxible-provider-runtime":
            continue
        target = directory / distribution.filename
        target.write_bytes(fetcher.fetch(distribution))
        dependencies.append(target)
    return tuple(dependencies)


def test_unpublished_local_call_installs_runs_and_preserves_old_deployment(
    installer_http, tmp_path, provider_checkout, local_call_dependency_wheels, monkeypatch
):
    from cruxible_core.providers.package_materialization import ArtifactTransport
    from tests.support.provider_installation import build_local_call

    original_get = ArtifactTransport.get

    def local_only(self, url):
        assert url.startswith("file:"), f"installation unexpectedly fetched {url}"
        return original_get(self, url)

    # This test exercises deployment preservation, with real wheels and children.
    # Network provisioning belongs to fixture setup, never to its installs.
    monkeypatch.setattr(ArtifactTransport, "get", local_only)

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    wheel, lock = build_local_call(tmp_path, repository)
    result = install_provider_wheel(
        client, instance_id, wheel=wheel, lock=lock, dependency_wheels=local_call_dependency_wheels
    )
    assert result.status == "ready"
    assert _run_call(http, instance_id, reviewer, build=result.build, value={"n": 2}) == {"n": 3}
    operator = get_playbill_manager().provider_runtime_operator()
    second, second_lock = build_local_call(tmp_path, repository, name="other-local-call")
    shared = install_provider_wheel(
        client,
        instance_id,
        wheel=second,
        lock=second_lock,
        dependency_wheels=local_call_dependency_wheels,
    )
    assert shared.status == "ready" and shared.registered
    # Another provider's build serving the same operation signature, beside it.
    assert shared.build != result.build
    old = dict(operator.deployments)
    updated, lock = build_local_call(tmp_path, repository, increment=2)
    result2 = install_provider_wheel(
        client,
        instance_id,
        wheel=updated,
        lock=lock,
        dependency_wheels=local_call_dependency_wheels,
    )
    assert result2.status == "ready", result2
    assert result2.installed and result2.registered
    assert result2.build != result.build
    # The accepted Line still runs its exact original build; a Line bound to the
    # new build runs that one.
    assert _run_line(instance_id, "test.installed-package-call-line") == {"n": 3}
    assert _run_call(
        http, instance_id, reviewer, build=result2.build, value={"n": 2}, name="updated-call"
    ) == {"n": 4}
    assert result2.installation_id != result.installation_id
    assert old.items() <= operator.deployments.items()
    assert len(operator.deployments) == 3
    assert all(item.environment_path.exists() for item in old.values())


def _web_contract_members(wheel: Path) -> dict[str, bytes]:
    """The Contracts the web wheel carries (its registration's ``declares``), as tree members."""

    from cruxible_client.contracts.provider_registration import PackageRegistration
    from cruxible_client.contracts.shapes import contract_path, render_contract

    with zipfile.ZipFile(wheel) as archive:
        (name,) = [item for item in archive.namelist() if item.endswith("/registration.json")]
        registration = PackageRegistration.model_validate_json(archive.read(name))
    return {
        contract_path(item.name, item.version): render_contract(item)
        for item in registration.declares.contracts
    }


@pytest.fixture
def installed_web(installer_http, provider_checkout):
    """The real web wheel, installed with no Contract accepted, then its carried copies.

    The install pins every operation to the copies the wheel carries; the same
    copies are accepted afterwards only because the test's Procedures name
    capabilities over them.
    """

    from cruxible_client.contracts.shapes import contract_digest, parse_contract

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    wheels = provider_checkout.wheels
    wheel = next(wheels.glob("cruxible_provider_web-*.whl"))
    members = _web_contract_members(wheel)
    installed = install_provider_wheel(
        client,
        instance_id,
        wheel=wheel,
        lock=provider_checkout.repository / "packages/cruxible-provider-web/uv.lock",
        dependency_wheels=(next(wheels.glob("cruxible_provider_runtime-*.whl")),),
        # web.render.dom's implementation declares the browser extra; without it
        # that implementation is not installed at all. The others need none.
        extras=("browser",),
    )
    assert installed.installed and installed.registered, installed
    instance = get_playbill_manager().get(instance_id)
    assert not set(members) & set(instance.tree_at(instance.accepted_coordinate().git_oid))
    _accept(http, instance_id, reviewer, members, name="web-contracts")
    digests = {
        path: contract_digest(parse_contract(content, path=path)).tagged
        for path, content in members.items()
    }
    return client, installed, digests


def test_installed_web_source_fetches_a_recorded_origin_and_retains_capture(
    installer_http, installed_web
):
    from cruxible_client.contracts.captures import (
        capture_contract_path,
        parse_capture_envelope,
        render_capture_contract,
    )
    from cruxible_client.contracts.provider_operations import operation_signature_digest
    from cruxible_client.contracts.providers import parse_provider, provider_path
    from cruxible_client.contracts.shapes import contract_path
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.core_support._light_capture_support import http_contract
    from tests.test_procedures.test_procedure_source_runs import _policy

    http, instance_id, reviewer = installer_http
    client, installed, digests = installed_web
    # Readiness is per operation and truthful. web.fetch, web.fetch.pages,
    # search.web and web.render (a pure derive) need nothing beyond the base
    # install. web.render.dom's implementation needs the browser extra (this
    # install asked for it) and a Chromium matching the installed Playwright,
    # which the install probes for rather than assumes: where none is present,
    # the operation is installed but not ready, and the install says so.
    readiness = {row.operation: row for row in installed.operations}
    assert set(readiness) == {
        "web.fetch",
        "web.fetch.pages",
        "search.web",
        "web.render",
        "web.render.dom",
    }
    assert all(row.installed for row in readiness.values()), installed.operations
    assert readiness["search.web"].missing_requirements == ()
    assert readiness["web.fetch"].missing_requirements == ()
    assert readiness["web.fetch.pages"].missing_requirements == ()
    assert readiness["web.render"].missing_requirements == ()
    assert set(readiness["web.render.dom"].missing_requirements) <= {
        "runtime resource: playwright.chromium"
    }
    assert installed.status == (
        "blocked" if readiness["web.render.dom"].missing_requirements else "ready"
    )
    # The install pinned every operation and every declared refusal to the
    # copies the wheel carries (accepted afterwards, byte for byte).
    instance = get_playbill_manager().get(instance_id)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    build_path = provider_path(installed.build)
    build = parse_provider(tree[build_path], path=build_path)
    served = {operation.name: operation for operation in build.operations}
    assert set(served) == {
        "web.fetch",
        "web.fetch.pages",
        "search.web",
        "web.render",
        "web.render.dom",
    }
    for operation in served.values():
        refs = (
            operation.in_,
            operation.out,
            *(item.contract for item in operation.refusals.values()),
        )
        for ref in refs:
            assert ref.digest == digests[contract_path(ref.name, ref.version)], ref
    assert set(served["web.fetch"].refusals) == {
        "credential_unavailable",
        "cross_origin_credentialed_redirect",
        "private_or_internal_address",
        "provider_declined",
        "redirect_limit",
        "unsupported_redirect_scheme",
    }
    assert set(served["web.fetch.pages"].refusals) == {
        "credential_unavailable",
        "cross_origin_credentialed_redirect",
        "private_or_internal_address",
        "provider_declined",
        "redirect_limit",
        "unsupported_redirect_scheme",
    }
    assert set(served["web.render"].refusals) == {"provider_declined"}
    assert set(served["web.render.dom"].refusals) == {
        "engine_unavailable",
        "private_or_internal_address",
        "provider_declined",
    }
    assert set(served["search.web"].refusals) == {
        "cross_origin_credentialed_redirect",
        "provider_declined",
        "redirect_limit",
        "undeclared_instance",
        "unsupported_redirect_scheme",
    }
    assert len(get_playbill_manager().provider_runtime_operator().config.deployments) == 1

    from cruxible_client.contracts.shapes import contract_path, render_contract, validate_contract
    from tests.core_support._light_capture_support import JSON_DECODING
    from tests.support.layouts import record_set, with_layout

    # The fetched body is the origin's JSON, read as one record (the kind's layout).
    record = validate_contract(
        {"name": "test.web-record", "version": 1, "fields": {"station": "string"}}
    )
    contract = with_layout(
        http_contract(decoding=JSON_DECODING),
        {
            "record": record_set(
                at="/", row="test.web-record@1", fields={"station": {"at": "station"}}
            )
        },
    )
    from tests.support.capabilities import head_tree, missing_capability_files

    head = head_tree(get_playbill_manager().get(instance_id))
    _accept(
        http,
        instance_id,
        reviewer,
        {
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
            contract_path(record.name, record.version): render_contract(record),
            # Unless the instance holds it (base kit).
            **missing_capability_files(
                head, ("web.fetch", "web.fetch.request@1", "web.fetch.result@1", "external_read")
            ),
        },
        name="web-capture-contract",
    )
    # web.fetch refuses private and loopback origins, so the run reads the
    # recording the web package ships for the reserved fixture.invalid host; it
    # opens no socket, and needs no browser.
    url = "https://fixture.invalid/api/v1/measurements.json"
    line = "test.installed-web-fetch-line"
    _author_line(
        http,
        instance_id,
        reviewer,
        spec=procedure_spec_data(
            procedure="test.installed-web-fetch",
            reference_text=True,
            input={},
            output={"station": "string"},
            slots={"fetch": {"capability": "web.fetch@1"}},
            steps=[
                CallStep(
                    call="observation",
                    captures={"capture": contract.identity.name},
                    slot="fetch",
                    input={"url": url, "expected_format": "json"},
                ),
                {"extract": "record", "capture": "$observation.capture", "records": "record"},
                ProjectStep(project="result", fields={"station": "$record.rows.0.station"}),
            ],
            returns="result",
            hard_caps=procedure_limits(provider_calls=1, capture_bytes=1048576),
        ),
        line=line,
        acquisition={"observation.capture": _policy().model_dump(mode="json")},
        bindings={"fetch": f"{installed.build}#web.fetch"},
        parameters={},
        name="installed-web-fetch",
    )
    state = _line_state(instance_id, line)
    assert state.status == "succeeded", state.terminal
    assert state.result["station"] == "newlyn"
    run = client.get_procedure_run(instance_id, state.run_id)
    assert run.receipt_digest
    (observation,) = run.source_observations
    assert observation.capture_digest
    # The run executed the exact operation of the exact build the Line bound.
    (receipt,) = _invocation_receipts(instance, state.run_id)
    assert receipt.operation == "web.fetch"
    assert receipt.provider_artifact_digest == _build_digest(build)
    assert receipt.operation_signature_digest == operation_signature_digest(
        served["web.fetch"].signature()
    )
    # The retained Capture: the exact body the origin sent, its digest and
    # length, under the request's source identity; how it was obtained is the
    # coordinate, recorded as served from a recording.
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    envelope = parse_capture_envelope(
        instance.body_store().read(observation.capture_digest, access=access)
    )
    material = instance.body_store().read(envelope.commitment.digest, access=access)
    assert envelope.commitment.byte_length == len(material)
    assert envelope.source.source_identity == "web.response"
    assert envelope.source.coordinate_type == "http-response-v1"
    assert envelope.source.selector == {}
    assert envelope.source.replayability == "attested_only"
    assert envelope.source_effective_time is None
    assert json.loads(material)["station"] == "newlyn"
    assert envelope.source.coordinate["source"] == "packaged-recording"
    assert envelope.source.coordinate["url"] == url
    assert envelope.source.coordinate["final_url"] == url
    assert envelope.source.coordinate["content_type"] == "application/json"


_SEARCH_IN_ITS_ENVIRONMENT = """
import json
from cruxible_provider_runtime.egress import EgressRecorder
from cruxible_provider_runtime.protocol import Budgets
from cruxible_provider_runtime.provider_api import ProviderRunContext
from cruxible_provider_web.search import SearxngSearch

result = SearxngSearch()(
    ProviderRunContext(
        run_id="installed-search",
        operation="search.web",
        operation_signature_digest="sha256:" + "a" * 64,
        build_digest="sha256:" + "b" * 64,
        implementation_digest="sha256:" + "c" * 64,
        input={"query": "tide gauge recalibration", "time_range": "year"},
        coordinates={"instance_url": "https://fixture.invalid"},
        budgets=Budgets(wall_clock_seconds=30.0, output_bytes=4_000_000),
        declared_endpoints=("dynamic:target-from-configuration",),
        capture_contract=None,
        secrets={},
        egress=EgressRecorder(),
    )
)
print(json.dumps({"status": result.status, "output": result.output}))
"""


def test_installed_web_search_returns_its_result_contract(installer_http, installed_web):
    """search.web's output, from the installed build's code, is exactly its Contract.

    No Line runs it here: a run names search.web's instance through run
    configuration (the ``instance_url`` coordinate), which a Procedure Source does
    not supply. The adapter runs in the installed deployment's own interpreter,
    over the packaged recording, and Core's operation checker reads its output
    against the accepted result Contract.
    """

    import subprocess

    from cruxible_client.contracts.captures import ProviderResultToExternalCapture
    from cruxible_client.contracts.provider_operations import validate_operation_value
    from cruxible_client.contracts.shapes import contract_path, parse_contract

    _http, instance_id, _reviewer = installer_http
    _client, installed, _digests = installed_web
    instance = get_playbill_manager().get(instance_id)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)

    def resolve(ref):
        path = contract_path(ref.name, ref.version)
        return None if path not in tree else parse_contract(tree[path], path=path)

    (deployment,) = get_playbill_manager().provider_runtime_operator().deployments.values()
    ran = subprocess.run(
        [str(deployment.interpreter_path), "-c", _SEARCH_IN_ITS_ENVIRONMENT],
        capture_output=True,
        check=True,
    )
    returned = json.loads(ran.stdout)
    assert returned["status"] == "ok", returned
    output = returned["output"]
    result = resolve(_served_operation(instance, installed.build, "search.web").out)
    assert result is not None
    assert "source_effective_time" not in output
    assert validate_operation_value(output, result, resolve=resolve) == output
    decoded = ProviderResultToExternalCapture.model_validate(output)
    assert decoded.source_identity == "search.response"
    assert decoded.source_effective_time is None
    # The Capture is the instance's answer, verbatim; no ranking is derived.
    answer = json.loads(base64.b64decode(decoded.content_base64))
    assert isinstance(answer["results"], list)
    assert decoded.coordinate["source"] == "packaged-recording"
    assert len(answer["results"]) == 3


def _served_operation(instance, build_name, operation):
    from cruxible_client.contracts.providers import parse_provider, provider_path

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = provider_path(build_name)
    build = parse_provider(tree[path], path=path)
    return next(item for item in build.operations if item.name == operation)


def _build_digest(build):
    from cruxible_client.contracts.providers import provider_digest

    return provider_digest(build).tagged


def _invocation_receipts(instance, run_id):
    from tests.test_procedures.test_b2_operations import invocation_receipts

    return invocation_receipts(instance, run_id)


@pytest.mark.parametrize("installer_http", [True], indirect=True)
def test_install_retry_reuses_pending_registration_until_approval(
    installer_http, tmp_path, monkeypatch, provider_checkout
):
    from tests.support.provider_installation import build_local_call
    from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    wheels = provider_checkout.wheels
    wheel, lock = build_local_call(tmp_path, repository)
    arguments = dict(
        wheel=wheel,
        lock=lock,
        dependency_wheels=(next(wheels.glob("cruxible_provider_runtime-*.whl")),),
    )
    pending = install_provider_wheel(client, instance_id, **arguments)
    assert pending.status == "awaiting_approval"
    assert pending.installed and not pending.registered

    def forbidden(*args, **kwargs):
        raise AssertionError("pending registration retry must not rebuild")

    monkeypatch.setattr(
        "cruxible_core.service.procedures.provider_installation.prepare_provider_package", forbidden
    )
    get_playbill_manager().clear()
    again = install_provider_wheel(client, instance_id, **arguments)
    assert again.proposal_id == pending.proposal_id
    assert again.candidate_digest == pending.candidate_digest
    _approve_and_activate(http, instance_id, reviewer, pending.proposal_id)
    accepted = install_provider_wheel(client, instance_id, **arguments)
    assert accepted.status == "ready" and accepted.registered


def test_an_install_whose_head_moves_before_registration_publishes_refuses_typed(
    installer_http, tmp_path, monkeypatch, provider_checkout
):
    """A concurrent acceptance between the registration's evaluation and its
    publication: the install refuses ``state_moved`` (409) with the install itself as
    its repair, publishes no registration, and the same install run again proposes it.
    (The status is the refusal's own, as the document and principal races assert.)"""

    from cruxible_client.contracts.repairs import RepairOperation
    from cruxible_core.proposals.proposals import ProposalService
    from tests.support.provider_installation import build_local_call
    from tests.test_server.test_exact_preview import _move_head

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    wheel, lock = build_local_call(tmp_path, provider_checkout.repository)
    arguments = dict(
        wheel=wheel,
        lock=lock,
        dependency_wheels=(next(provider_checkout.wheels.glob("cruxible_provider_runtime-*.whl")),),
    )
    original = ProposalService._evaluate_admission
    moved: list[str] = []

    def evaluate_then_move(self, **kwargs):
        evaluated = original(self, **kwargs)
        target_ref = kwargs["request"].target_ref
        if not moved and "/provider-install-" in target_ref:
            moved.append(target_ref)
            _move_head(http, instance_id, reviewer, "install-mover")
        return evaluated

    monkeypatch.setattr(ProposalService, "_evaluate_admission", evaluate_then_move)
    with pytest.raises(Exception) as refused:
        install_provider_wheel(client, instance_id, **arguments)

    assert len(moved) == 1
    assert getattr(refused.value, "error_code", None) == "cruxible.preview.state_moved"
    assert getattr(refused.value, "repair", None) == RepairOperation(
        operation="cruxible.provider.install", arguments={"dry_run": False}
    )
    assert get_playbill_manager().get(instance_id).proposal_ref_target(moved[0]) is None
    again = install_provider_wheel(client, instance_id, **arguments)
    assert again.status == "ready" and again.registered, again


def test_repository_catalog_install_and_retry(installer_http, monkeypatch):
    from cruxible_client.contracts.provider_installation import ProviderInstallRequest
    from cruxible_core.service.procedures import provider_installation as service

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    catalog = client.list_provider_packages(instance_id)
    # docs and quant are still at registration schema 2 in the repository: this
    # release cannot install them, so the catalog does not offer them.
    assert {item.name for item in catalog.packages} == {
        "cruxible-provider-noop",
        "cruxible-provider-web",
    }
    # noop installs by name with no kit and no Contract accepted.
    request = ProviderInstallRequest(package="cruxible-provider-noop")
    result = client.install_provider(instance_id, request)
    assert result.status == "ready", result
    monkeypatch.setattr(service, "_source_files", lambda *a: pytest.fail("retry rebuilt package"))
    again = client.install_provider(instance_id, request)
    assert again.status == "ready" and again.installation_id == result.installation_id


@pytest.fixture
def index_installer_http(tmp_path, monkeypatch, provider_checkout):
    """No repository: names install from a file index built from the test wheels."""
    wheels = provider_checkout.wheels
    import hashlib

    from packaging.utils import parse_wheel_filename

    simple = tmp_path / "simple"
    for wheel in sorted(Path(wheels).glob("*.whl")):
        page = simple / str(parse_wheel_filename(wheel.name)[0])
        page.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (page / "index.html").write_text(
            f'<a href="{wheel.resolve().as_uri()}#sha256={digest}">{wheel.name}</a>'
        )
    state = tmp_path / "server-state"
    config = state / PROVIDER_RUNTIME_CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "provider_index_urls": [
                    simple.as_uri() + "/",
                    Path(wheels).resolve().as_uri() + "/",
                    "https://pypi.org/simple",
                    "https://files.pythonhosted.org/",
                ],
            }
        )
    )
    from tests.test_server.conftest import _playbill_http

    yield from _playbill_http(tmp_path, monkeypatch, require_independent_approval=False)


def test_install_by_name_from_an_index_uses_the_embedded_lock(
    index_installer_http, monkeypatch, provider_checkout
):
    from cruxible_client.contracts.provider_installation import ProviderInstallRequest
    from cruxible_client.errors import ConfigError
    from cruxible_core.service.procedures import provider_installation as service

    http, instance_id, reviewer = index_installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    assert "install by name" in (client.list_provider_packages(instance_id).detail or "")
    request = ProviderInstallRequest(package="cruxible-provider-noop")
    result = client.install_provider(instance_id, request)
    assert result.status == "ready" and result.registered, result
    deployment = get_playbill_manager().provider_runtime_operator().config.deployments[0]
    wheels = provider_checkout.wheels
    noop = next(wheels.glob("cruxible_provider_noop-*.whl"))
    assert deployment.distribution_path.endswith(noop.name)
    monkeypatch.setattr(service, "_source_files", lambda *a: pytest.fail("retry refetched"))
    again = client.install_provider(instance_id, request)
    assert again.status == "ready" and again.installation_id == result.installation_id
    with pytest.raises(ConfigError, match="no installable wheel"):
        client.install_provider(instance_id, request.model_copy(update={"version": "99.0"}))


#: One workspace.file request: the bytes the daemon read, as the operation takes them.
_WORKSPACE_BYTES = b"high\nlow\n"


def _workspace_request() -> dict[str, object]:
    import hashlib

    return {
        "logical_source": "docs.readme",
        "commitment_digest": "sha256:" + "c0" * 32,
        "content_encoding": "base64",
        "bytes": base64.b64encode(_WORKSPACE_BYTES).decode("ascii"),
        "byte_length": len(_WORKSPACE_BYTES),
        "bytes_digest": "sha256:" + hashlib.sha256(_WORKSPACE_BYTES).hexdigest(),
    }


def _author_workspace_line(http, instance_id, reviewer, *, build, name):
    """A Procedure calling one workspace.file slot, and a Line binding it to ``build``."""

    from tests.support.capabilities import (
        WORKSPACE_FILE_SPEC,
        head_tree,
        missing_capability_files,
    )

    line = f"test.{name}-line"
    instance = get_playbill_manager().get(instance_id)
    members = missing_capability_files(head_tree(instance), WORKSPACE_FILE_SPEC)
    if members:
        _accept(http, instance_id, reviewer, members, name="workspace-file-capability")
    _author_line(
        http,
        instance_id,
        reviewer,
        spec=procedure_spec_data(
            procedure=f"test.{name}",
            input={},
            output={"lines": "int"},
            slots={"read": {"capability": "workspace.file@1"}},
            steps=[
                CallStep(call="structured", slot="read", input=_workspace_request()),
                ProjectStep(project="out", fields={"lines": "$structured.content.line_count"}),
            ],
            returns="out",
            hard_caps=procedure_limits(provider_calls=1),
        ),
        line=line,
        bindings={"read": f"{build}#workspace.file"},
        parameters={},
        name=name,
    )
    return line


def test_the_built_in_reads_a_workspace_file_and_an_install_beside_it_moves_no_seed(
    installer_http, tmp_path, provider_checkout
):
    """The seeded built-in runs a workspace Line in process; installing a package moves nothing.

    The built-in carries its own Contracts, so its Line binds with none accepted
    for it. Installing another package registers a build of its own: nothing
    seeded moves, nor the Line bound to the built-in, which runs the same
    before and after. A package may not serve a ``workspace.`` capability: its
    install is refused before anything is proposed.
    """

    from cruxible_client.contracts.providers import parse_provider, provider_digest, provider_path
    from cruxible_client.errors import CoreError
    from cruxible_core.governance.seed_artifacts.workspace_file import (
        WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST,
        WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
    )
    from cruxible_core.ledger.bootstrap import GENESIS_SEEDS
    from tests.support.provider_installation import build_local_call

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    instance = get_playbill_manager().get(instance_id)

    def accepted_tree():
        return instance.tree_at(instance.accepted_coordinate().git_oid)

    def seeded(tree):
        return {path: tree[path] for path, _digest in GENESIS_SEEDS}

    builtin_path = provider_path(WORKSPACE_FILE_BUILTIN_PROVIDER_ID)
    before = accepted_tree()
    builtin = parse_provider(before[builtin_path], path=builtin_path)
    builtin_line = _author_workspace_line(
        http, instance_id, reviewer, build=WORKSPACE_FILE_BUILTIN_PROVIDER_ID, name="builtin-read"
    )
    line_path = f"lines/{builtin_line}.json"
    line_bytes = accepted_tree()[line_path]

    def run():
        state = _line_state(instance_id, builtin_line)
        assert state.status == "succeeded", state.terminal
        assert state.result == {"lines": 2}
        (receipt,) = _invocation_receipts(instance, state.run_id)
        assert receipt.operation == "workspace.file"
        assert receipt.provider_artifact_digest == provider_digest(builtin).tagged
        assert receipt.implementation_digest == WORKSPACE_FILE_BUILTIN_IMPLEMENTATION_DIGEST
        assert receipt.fence_scope == "in_process"
        return receipt

    first = run()
    wheels = provider_checkout.wheels
    runtime = (next(wheels.glob("cruxible_provider_runtime-*.whl")),)
    wheel, lock = build_local_call(tmp_path, provider_checkout.repository)
    installed = install_provider_wheel(
        client, instance_id, wheel=wheel, lock=lock, dependency_wheels=runtime
    )
    assert installed.status == "ready" and installed.registered, installed
    after = accepted_tree()
    assert provider_path(installed.build) != builtin_path
    assert seeded(after) == seeded(before)
    assert after[line_path] == line_bytes
    again = run()
    assert again.provider_artifact_digest == first.provider_artifact_digest

    reserved, reserved_lock = build_local_call(
        tmp_path / "reserved", provider_checkout.repository, operations=("workspace.file",)
    )
    with pytest.raises(CoreError, match="capability_reserved"):
        install_provider_wheel(
            client, instance_id, wheel=reserved, lock=reserved_lock, dependency_wheels=runtime
        )
    assert seeded(accepted_tree()) == seeded(before)


def test_a_transferred_wheel_resolves_registry_dependencies_from_the_default_index(
    provider_checkout, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transferred wheel (plain or a kit's) falls back to PyPI when the operator
    configured no index, as an install by name does; configured indexes take
    precedence, and a configured repository's checkout keeps to them."""

    from cruxible_client.contracts.provider_installation import (
        ProviderInstallRequest,
        ProviderWheelObject,
    )
    from cruxible_core.providers.package_index import DEFAULT_PROVIDER_INDEX_URLS
    from cruxible_core.service.change_preview import ChangeMode
    from cruxible_core.service.procedures import provider_installation as service

    zero = "sha256:" + "0" * 64
    seen: list[tuple[str, ...]] = []

    class Stop(Exception):
        pass

    def prepare(**arguments: Any) -> Any:
        seen.append(arguments["index_urls"])
        raise Stop

    monkeypatch.setattr(service, "prepare_provider_package", prepare)
    monkeypatch.setattr(service, "_source_files", lambda *args: (tmp_path, tmp_path, ()))
    monkeypatch.setattr(service, "_repository_fingerprint", lambda *args: None)

    def operator(configured: tuple[str, ...]) -> Any:
        class Config:
            provider_index_urls = configured

        class Operator:
            config = Config
            state_root = tmp_path
            process_leases = object()  # the lane holds; this test is about index resolution

        return Operator()

    transferred = ProviderInstallRequest(
        wheel=ProviderWheelObject(filename="local_call-0.2.0-py3-none-any.whl", digest=zero),
        lock_digest=zero,
    )
    repository = ProviderInstallRequest(package="local-call")
    configured = ("https://index.example/simple/",)
    for request, urls in (
        (transferred, ()),
        (transferred, configured),
        (repository, ()),
        (repository, configured),
    ):
        with pytest.raises(Stop):
            service._install_locked(
                None,  # type: ignore[arg-type]
                operator(urls),
                request,
                zero,
                tmp_path,
                "actor",
                "2026-10-08T00:00:00.000000Z",
                None,
                None,
                mode=ChangeMode(None, previewing=False),
            )
    assert seen == [DEFAULT_PROVIDER_INDEX_URLS, configured, (), configured]


def test_malformed_wheel_is_a_typed_refusal_before_registration(installer_http, tmp_path):
    from cruxible_client.errors import ConfigError

    http, instance_id, _ = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    wheel = tmp_path / "broken-1.0-py3-none-any.whl"
    wheel.write_bytes(b"not a wheel")
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n")
    with pytest.raises(ConfigError, match="metadata or lock is invalid"):
        install_provider_wheel(client, instance_id, wheel=wheel, lock=lock)
    assert not get_playbill_manager().provider_runtime_operator().config.deployments


def test_local_install_does_not_report_container_only_operation_ready(
    installer_http, tmp_path, provider_checkout
):
    from tests.support.provider_installation import build_local_call

    http, instance_id, _ = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    wheels = provider_checkout.wheels
    wheel, lock = build_local_call(tmp_path, repository, backends=("container",))
    result = install_provider_wheel(
        client,
        instance_id,
        wheel=wheel,
        lock=lock,
        dependency_wheels=(next(wheels.glob("cruxible_provider_runtime-*.whl")),),
    )
    assert result.installed and result.registered
    assert result.status == "blocked"
    assert len(result.operations) == 1
    assert not result.operations[0].installed
    assert result.operations[0].missing_requirements == ("compatible local Python implementation",)


def test_install_over_a_stale_operator_config_is_a_typed_refusal_naming_the_repair(
    installer_http, tmp_path, provider_checkout, monkeypatch
):
    """Kit-pilot F4: a config entry this install cannot overwrite refuses typed, never 500."""

    from cruxible_client.errors import CoreError
    from tests.support.provider_installation import build_local_call

    http, instance_id, _ = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    wheel, lock = build_local_call(tmp_path, provider_checkout.repository)
    arguments = dict(
        wheel=wheel,
        lock=lock,
        dependency_wheels=(next(provider_checkout.wheels.glob("cruxible_provider_runtime-*.whl")),),
    )
    first = install_provider_wheel(client, instance_id, **arguments)
    assert first.status == "ready", first
    # A config copied from another root: the same deployment digest, other custody.
    operator = get_playbill_manager().provider_runtime_operator()
    path = operator.state_root / PROVIDER_RUNTIME_CONFIG_PATH
    config = json.loads(path.read_text())
    (entry,) = config["deployments"]
    entry["provider_runtime_version"] = "0.0.0-foreign"
    path.write_text(json.dumps(config))
    get_playbill_manager().clear()
    statuses: list[int] = []
    check = CruxibleClient._check_error

    def recording(response):
        statuses.append(response.status_code)
        check(response)

    monkeypatch.setattr(CruxibleClient, "_check_error", staticmethod(recording))
    with pytest.raises(CoreError) as refused:
        install_provider_wheel(client, instance_id, **arguments)
    assert statuses[-1] == 400
    error = refused.value
    assert error.error_code == "cruxible.provider.deployment_config_conflict"
    assert entry["deployment_digest"] in str(error)
    assert error.repair.hand_edit.target == "daemon/provider-runtime.json"
    assert entry["deployment_digest"] in error.repair.hand_edit.required_change
    # Doing what the repair says installs.
    config["deployments"] = []
    path.write_text(json.dumps(config))
    get_playbill_manager().clear()
    again = install_provider_wheel(client, instance_id, **arguments)
    assert again.status == "ready", again

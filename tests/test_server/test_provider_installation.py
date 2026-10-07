"""Real built wheels cross the SDK/HTTP boundary; no substituted provider invoker."""

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.provider_wheels import install_provider_wheel
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import PROVIDER_RUNTIME_CONFIG_PATH
from tests.support.provider_checkout import checkout_predates_embedded_wheel_locks


def interface_entry(client: CruxibleClient, instance_id: str, name: str) -> dict[str, Any]:
    """The accepted inventory entry of one live provider interface, as ``get`` proves it."""

    from cruxible_client.contracts.get_reads import GetRequest

    proof = client.get(
        instance_id,
        request=GetRequest(ref=f"ProviderInterface:{name}", detail="proof"),
    ).proof
    assert proof is not None
    return dict(proof["entry"])


# A real child's spawn and import time under a loaded parallel run; the
# example Procedure's 2 s run budget is sized for an in-process call.
_RUN_WALL_CLOCK = {"microseconds": 30_000_000}
_MAX_WALL_CLOCK = {"microseconds": 60_000_000}


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

    yield from _playbill_http(
        tmp_path, monkeypatch, require_independent_approval=getattr(request, "param", False)
    )


def test_transfer_install_and_restart_reuse(
    installer_http, tmp_path, monkeypatch, provider_checkout
):
    from tests.support.provider_installation import build_local_call

    http, instance_id, _ = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    wheels = provider_checkout.wheels
    # workspace.file is built into new instances, so the transferred wheel is
    # the unpublished local-call package rather than cruxible-provider-workspace.
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


def _run_call(
    client, http, instance_id, reviewer, tmp_path, interface_id, value, fields_in, fields_out
):
    from cruxible_client import Cruxible
    from cruxible_client.authoring.examples import procedure_example
    from cruxible_client.authoring.inputs import CarriedContractInput
    from cruxible_client.contracts.procedures.contract_schema import PropertySchema
    from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

    interface = interface_entry(client, instance_id, interface_id)
    provider = interface["providers"][0]

    def carried(name, role):
        return {"kind": "carried_contract", "name": name, "role": role}

    example = procedure_example()
    raw = {
        **example.definition,
        "name": "installed-package-call",
        "graph_format": 6,
        "returns": "result",
        "contract_out": carried("result", "contract-out"),
        "nodes": [
            {
                "kind": "call",
                "node_id": "invoke",
                "as": "result",
                "input": value,
                "provider": {
                    "kind": "accepted",
                    "role": "provider",
                    "target": provider["provider_identity"],
                },
                "interface": {
                    "kind": "accepted",
                    "role": "provider-interface",
                    "target": interface["identity"],
                },
                "interface_digest": interface["interface_digest"],
                "implementation_digest": provider["implementation_digest"],
                "contract_in": carried("request", "contract-in"),
                "contract_out": carried("result", "contract-out"),
            }
        ],
        "budget": {
            **example.definition["budget"],
            "wall_clock": _RUN_WALL_CLOCK,
            "max_items": None,
            "max_provider_calls": 1,
            "max_capture_bytes": 1048576,
        },
        "hard_caps": {
            **example.definition["hard_caps"],
            "max_wall_clock": _MAX_WALL_CLOCK,
            "max_provider_calls": 2,
            "max_capture_bytes": 2097152,
        },
    }
    authored = example.model_copy(
        update={
            "definition": raw,
            "contracts": (
                next(row for row in example.contracts if row.name == "empty-input"),
                CarriedContractInput(
                    name="request",
                    fields={key: PropertySchema.model_validate(v) for key, v in fields_in.items()},
                ),
                CarriedContractInput(
                    name="result",
                    fields={key: PropertySchema.model_validate(v) for key, v in fields_out.items()},
                ),
            ),
        }
    )
    pb = Cruxible._from_client(client, instance_id=instance_id, workspace=tmp_path)
    intent = pb.procedure(definition=authored).prepare()
    assert not intent.refused, intent.diagnostics
    intent.submit()
    _approve_and_activate(http, instance_id, reviewer, intent.proposal.proposal_id)
    run = pb.accepted_procedure("installed-package-call").run()
    state = client.get_procedure_run(instance_id, run.run_id)
    assert run.status == "succeeded", state.model_dump_json(indent=2)
    assert state.receipt_digest
    return run.result.model_dump()


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
    assert _run_call(
        client,
        http,
        instance_id,
        reviewer,
        tmp_path,
        "local.increment",
        {"n": 2},
        {"n": {"type": "int"}},
        {"n": {"type": "int"}},
    ) == {"n": 3}


def test_unpublished_local_call_installs_runs_and_preserves_old_deployment(
    installer_http, tmp_path, provider_checkout
):
    from tests.support.provider_installation import build_local_call

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    runtime = next(provider_checkout.wheels.glob("cruxible_provider_runtime-*.whl"))
    wheel, lock = build_local_call(tmp_path, repository)
    result = install_provider_wheel(
        client, instance_id, wheel=wheel, lock=lock, dependency_wheels=(runtime,)
    )
    assert result.status == "ready"
    assert _run_call(
        client,
        http,
        instance_id,
        reviewer,
        tmp_path,
        "local.increment",
        {"n": 2},
        {"n": {"type": "int"}},
        {"n": {"type": "int"}},
    ) == {"n": 3}
    operator = get_playbill_manager().provider_runtime_operator()
    second, second_lock = build_local_call(tmp_path, repository, name="other-local-call")
    shared = install_provider_wheel(
        client, instance_id, wheel=second, lock=second_lock, dependency_wheels=(runtime,)
    )
    assert shared.status == "ready"
    interface = interface_entry(client, instance_id, "local.increment")
    assert len(interface["providers"]) == 2
    old = dict(operator.deployments)
    updated, lock = build_local_call(tmp_path, repository, increment=2)
    result2 = install_provider_wheel(
        client, instance_id, wheel=updated, lock=lock, dependency_wheels=(runtime,)
    )
    assert result2.status == "blocked"
    assert "incomplete_closure" in result2.detail
    assert result2.installed and not result2.registered
    assert result2.installation_id != result.installation_id
    assert old.items() <= operator.deployments.items()
    assert len(operator.deployments) == 3
    assert all(item.environment_path.exists() for item in old.values())


def test_installed_web_source_fetches_local_http_and_retains_capture(
    installer_http, tmp_path, provider_checkout
):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from cruxible_client import Cruxible
    from cruxible_client.authoring.examples import procedure_example
    from cruxible_client.authoring.inputs import CarriedContractInput
    from cruxible_client.contracts.captures import capture_component_pin
    from cruxible_client.contracts.procedures.contract_schema import PropertySchema
    from tests.core_support._pc_c_support import capture_contract
    from tests.test_procedures.test_procedure_source_runs import _policy
    from tests.test_server.test_playbill_sdk_demo_world import _approve_and_activate

    http, instance_id, reviewer = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    wheels = provider_checkout.wheels
    installed = install_provider_wheel(
        client,
        instance_id,
        wheel=next(wheels.glob("cruxible_provider_web-*.whl")),
        lock=repository / "packages/cruxible-provider-web/uv.lock",
        dependency_wheels=(next(wheels.glob("cruxible_provider_runtime-*.whl")),),
        extras=("browser",),
    )
    assert installed.registered
    # Browser availability is reported independently of Python installation.
    assert all(row.installed for row in installed.operations)
    assert all(
        "python extra" not in item
        for row in installed.operations
        for item in row.missing_requirements
    )
    pb = Cruxible._from_client(client, instance_id=instance_id, workspace=tmp_path)
    base = capture_contract()
    contract = base.model_copy(
        update={
            "logical_source_identities": ("web.response",),
            "coordinate_schema_pins": (
                capture_component_pin("coordinate-schema", "http-response-v1"),
            ),
            "selector_schema_pins": (
                capture_component_pin("selector-schema", "whole-response-v1"),
            ),
            "selection_budget": base.selection_budget.model_copy(update={"max_bytes": 1048576}),
        }
    )
    policy = _policy(name="installed-web-policy", input_name="observation")
    draft = (
        pb.changes(rationale="Retain a fetched observation.")
        .capture_contract(contract)
        .acquisition_policy(policy)
    )
    prepared = draft.prepare()
    assert not prepared.refused, prepared.diagnostics
    prepared.submit()
    _approve_and_activate(http, instance_id, reviewer, prepared.proposal.proposal_id)
    interface = interface_entry(client, instance_id, "web.fetch")
    provider = interface["providers"][0]

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"severity":"high"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        example = procedure_example()

        def accepted(role, target):
            return {"kind": "accepted", "role": role, "target": target}

        carried = {"kind": "carried_contract", "role": "contract-out", "name": "fetch-result"}
        raw = {
            **example.definition,
            "name": "installed-web-fetch",
            "graph_format": 6,
            "returns": "result",
            "contract_out": carried,
            "nodes": [
                {
                    "kind": "source",
                    "node_id": "fetch",
                    "as": "observation",
                    "next": "shape",
                    "capture_contract": accepted("capture-contract", contract.identity.qualified),
                    "provider": accepted("provider", provider["provider_identity"]),
                    "interface": accepted("provider-interface", interface["identity"]),
                    "interface_digest": interface["interface_digest"],
                    "implementation_digest": provider["implementation_digest"],
                    "request": {
                        "url": f"http://127.0.0.1:{server.server_port}/state.json",
                        "expected_format": "json",
                    },
                },
                {
                    "kind": "project",
                    "node_id": "shape",
                    "as": "result",
                    "fields": {"text": "$steps.observation.derived.text"},
                    "contract_out": carried,
                },
            ],
            "budget": {
                **example.definition["budget"],
                "wall_clock": _RUN_WALL_CLOCK,
                "max_items": None,
                "max_provider_calls": 1,
                "max_capture_bytes": 1048576,
            },
            "hard_caps": {
                **example.definition["hard_caps"],
                "max_wall_clock": _MAX_WALL_CLOCK,
                "max_provider_calls": 2,
                "max_capture_bytes": 2097152,
            },
        }
        authored = example.model_copy(
            update={
                "definition": raw,
                "acquisition_policy": policy.identity.name,
                "contracts": (
                    next(row for row in example.contracts if row.name == "empty-input"),
                    CarriedContractInput(
                        name="fetch-result", fields={"text": PropertySchema(type="string")}
                    ),
                ),
            }
        )
        prepared = pb.procedure(definition=authored).prepare()
        assert not prepared.refused, prepared.diagnostics
        prepared.submit()
        _approve_and_activate(http, instance_id, reviewer, prepared.proposal.proposal_id)
        run = pb.accepted_procedure("installed-web-fetch").run()
        state = client.get_procedure_run(instance_id, run.run_id)
        assert run.status == "succeeded", state.model_dump_json(indent=2)
        assert run.result.text == '{"severity":"high"}'
        assert state.source_observations[0].capture_digest
        assert state.receipt_digest
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


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


def test_repository_catalog_install_and_retry(installer_http, monkeypatch):
    from cruxible_client.contracts.provider_installation import ProviderInstallRequest
    from cruxible_core.service.procedures import provider_installation as service

    http, instance_id, _ = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    catalog = client.list_provider_packages(instance_id)
    assert {item.name for item in catalog.packages} == {
        "cruxible-provider-noop",
        "cruxible-provider-web",
        "cruxible-provider-workspace",
        "cruxible-provider-docs",
        "cruxible-provider-quant",
    }
    # workspace.file is built in on new instances; noop stands in for a package.
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


@pytest.mark.xfail(
    checkout_predates_embedded_wheel_locks(),
    reason=(
        "the providers checkout builds wheels without their embedded uv.lock "
        "(cruxible-providers 5fcef6d, not yet on its main): install by name refuses"
    ),
    strict=True,
)
def test_install_by_name_from_an_index_uses_the_embedded_lock(
    index_installer_http, monkeypatch, provider_checkout
):
    from cruxible_client.contracts.provider_installation import ProviderInstallRequest
    from cruxible_client.errors import ConfigError
    from cruxible_core.service.procedures import provider_installation as service

    http, instance_id, _ = index_installer_http
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


def test_installing_a_package_for_a_built_in_interface_refuses_by_name(
    installer_http, provider_checkout
):
    from cruxible_client.errors import ConfigError

    http, instance_id, _ = installer_http
    client = CruxibleClient(base_url="http://cruxible")
    client._client = http
    repository = provider_checkout.repository
    wheels = provider_checkout.wheels
    with pytest.raises(ConfigError, match="workspace.file is built in on this instance"):
        install_provider_wheel(
            client,
            instance_id,
            wheel=next(wheels.glob("cruxible_provider_workspace-*.whl")),
            lock=repository / "packages/cruxible-provider-workspace/uv.lock",
            dependency_wheels=(next(wheels.glob("cruxible_provider_runtime-*.whl")),),
        )
    # Nothing was registered: no deployment, and the built-in is untouched.
    assert not get_playbill_manager().provider_runtime_operator().config.deployments
    entry = interface_entry(client, instance_id, "workspace.file")
    assert [item["provider_identity"] for item in entry["providers"]] == [
        "Provider:cruxible-builtin"
    ]


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

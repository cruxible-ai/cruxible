"""Named typed interfaces retain the wheel's exact canonical definition."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistration,
    ProviderInterfaceDefinition,
    evaluate_provider_interface_law,
    interface_definition_bytes,
    interface_definition_digest,
    parse_provider_interface,
    provider_effect_class,
    provider_external_interface_definition_digest,
    provider_interface_contract,
    provider_interface_digest,
    provider_interface_path,
    provider_operation_contract,
    render_provider_interface,
)
from cruxible_core.errors import RequestRefusedError
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_core.service.procedures.provider_installation import _interface_bindings
from tests.core_support._p2b1_support import interface_registration, provider_v2
from tests.support.provider_builds import legacy_interface_contract


def definition(**overrides):
    return {
        "interface_id": "demo.interface",
        "version": 2,
        "contracts": {
            "input": {"fields": {"value": {"type": "string"}}, "allow_extra": False},
            "output": {"fields": {"echo": {"type": "string"}}},
        },
        "effect_class": "pure",
        **overrides,
    }


def registration(**overrides):
    return provider_interface_contract(
        interface=ProviderInterfaceDefinition.model_validate(definition(**overrides))
    )


def package_document(value):
    digest = provider_external_interface_definition_digest(
        canonical_bytes(value).hex(), domain="cruxible.interface.stub.v1"
    )
    manifest = provider_v2().runtime_artifact.manifest.model_dump(mode="json")
    manifest["implementations"][0]["interface_digest"] = digest
    manifest["implementations"][0]["side_effects"] = value["effect_class"] == "external_mutation"
    return PackageRegistrationDocumentV1.model_validate(
        {
            "schema_version": 1,
            "manifest": manifest,
            "interfaces": [
                {
                    "interface_id": value["interface_id"],
                    "interface_digest": digest,
                    "definition": value,
                    "vocabulary": {},
                    "classifier_identity": "inert",
                    "classifier_version": 1,
                    "classifier_code": {
                        "entrypoint": "demo:classify",
                        "source_digest": "sha256:" + "1" * 64,
                    },
                    "fixtures": [],
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "extras",
    [
        {},
        {"refusals": []},
        {
            "refusals": ["provider_declined"],
            "configuration": {"fields": {"token": {"type": "string", "optional": True}}},
        },
    ],
)
def test_v4_stores_typed_json_with_exact_sparse_definition_and_wheel_digest(extras):
    value = definition(**extras)
    interface = package_document(value).interface_registrations()[0]
    wire = json.loads(render_provider_interface(interface))
    assert set(wire) == {"artifact_format", "identity", "interface", "lifecycle"}
    assert wire["artifact_format"] == "cruxible-provider-interface-v4"
    assert wire["interface"] == value
    assert interface.identity.name == "demo.interface/v2"
    path = provider_interface_path(interface.identity.name)
    assert path == "provider-interfaces/demo.interface/v2.json"
    parsed = parse_provider_interface(render_provider_interface(interface), path=path)
    assert parsed == interface
    assert interface_definition_bytes(parsed) == canonical_bytes(value)
    assert (
        interface_definition_digest(parsed)
        == package_document(value).interfaces[0].interface_digest
    )
    assert provider_operation_contract(parsed) is parsed.interface.contracts
    assert not any(
        hasattr(parsed, field) for field in ("interface_digest", "interface_bytes_hex", "pins")
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"effect_class": "none"},
        {"version": True},
        {"version": 0},
        {
            "contracts": {
                "input": {"fields": {}},
                "output": {"fields": {"value": {"type": "imaginary"}}},
            }
        },
    ],
)
def test_v4_rejects_invalid_definition(overrides):
    with pytest.raises(ValidationError):
        registration(**overrides)


def test_v4_install_verifies_declared_digest_of_the_stored_definition():
    document = package_document(definition())
    exported = document.interfaces[0]
    changed = document.model_copy(
        update={
            "interfaces": (exported.model_copy(update={"interface_digest": "sha256:" + "0" * 64}),)
        }
    )
    with pytest.raises(RequestRefusedError, match="disagree"):
        changed.interface_registrations()
    # A constructor alias that cannot be stored exactly must not acquire a new digest.
    value = definition()
    value["contracts"]["input"]["fields"]["value"]["required"] = True
    with pytest.raises(RequestRefusedError, match="stored interface JSON"):
        package_document(value).interface_registrations()


def test_install_reuses_identical_name_version_and_refuses_a_second_definition():
    current = registration()
    path = provider_interface_path(current.identity.name)
    tree = {path: render_provider_interface(current)}
    assert _interface_bindings(tree, package_document(definition())) == ((current, True),)
    with pytest.raises(RequestRefusedError, match="existing interface name and version"):
        _interface_bindings(tree, package_document(definition(refusals=["different"])))
    assert _interface_bindings(tree, package_document(definition(version=3)))[0][1] is False


def test_install_before_compiler_36_keeps_a_frozen_v3_registration():
    document = package_document(definition())
    ((legacy, live),) = _interface_bindings({}, document, named=False)
    assert not live
    assert legacy.artifact_format == "cruxible-provider-interface-v3"
    assert legacy.effect_class == "none"
    assert interface_definition_bytes(legacy) == canonical_bytes(definition())
    assert interface_definition_digest(legacy) == document.interfaces[0].interface_digest
    original = render_provider_interface(legacy)
    assert _interface_bindings(
        {provider_interface_path(legacy.identity.name): original}, document, named=False
    ) == ((legacy, True),)
    assert render_provider_interface(legacy) == original
    # Retain the original exact-byte/digest assertions for the frozen wrapper;
    # the current-format test above checks its replacement typed-JSON surface.
    parsed, value = legacy, definition()
    assert bytes.fromhex(parsed.interface_bytes_hex) == canonical_bytes(value)
    assert parsed.interface_digest == package_document(value).interfaces[0].interface_digest


def test_v4_law_freezes_definition_and_allows_explicit_retirement():
    current = registration()
    path = provider_interface_path(current.identity.name)
    accepted = AcceptedProviderInterfaceRegistration(
        path=path, registration=current, artifact_digest=provider_interface_digest(current).tagged
    )
    assert (
        evaluate_provider_interface_law(current, path=path, predecessor=None).verdict == "accepted"
    )
    lifecycle = ArtifactLifecycle(state="retired", predecessor_digest=accepted.artifact_digest)
    retired = current.model_copy(update={"lifecycle": lifecycle})
    assert (
        evaluate_provider_interface_law(retired, path=path, predecessor=accepted).verdict
        == "accepted"
    )
    changed = registration(refusals=["different"]).model_copy(update={"lifecycle": lifecycle})
    assert (
        evaluate_provider_interface_law(changed, path=path, predecessor=accepted)
        .diagnostics[0]
        .code
        == "cruxible.provider_interface.contract_immutable"
    )
    with pytest.raises(ProposalIntegrityError, match="retired"):
        _interface_bindings(
            {path: render_provider_interface(retired)},
            SimpleNamespace(interface_registrations=lambda: (current,)),
        )


def test_v3_read_format_retains_its_original_bytes_and_none_effect():
    old = interface_registration()
    legacy = legacy_interface_contract(
        interface_id=old.interface_id,
        interface_bytes_hex=old.interface_bytes_hex,
        interface_digest_domain=old.interface_digest_domain,
        interface_digest=old.interface_digest,
        effect_class="none",
    )
    content = render_provider_interface(legacy)
    digest = provider_interface_digest(legacy)
    parsed = parse_provider_interface(content, path=provider_interface_path(legacy.identity.name))
    assert render_provider_interface(parsed) == content
    assert provider_interface_digest(parsed) == digest
    assert parsed.effect_class == "none"
    assert provider_effect_class(parsed) == "pure"


def test_install_name_version_lookup_does_not_grow_with_instance():
    from collections.abc import Mapping

    current = registration()
    path = provider_interface_path(current.identity.name)
    document = package_document(definition())
    observed = []

    class CountedTree(Mapping):
        def __init__(self, size):
            self.values = {f"unrelated/{index}.json": b"unread" for index in range(size)}
            self.values[path] = render_provider_interface(current)
            self.reads = []

        def __len__(self):
            return len(self.values)

        def __iter__(self):
            raise AssertionError("install must seek the named version without scanning the tree")

        def __getitem__(self, key):
            self.reads.append(key)
            return self.values[key]

    for size in (32, 320):
        tree = CountedTree(size)
        assert _interface_bindings(tree, document) == ((current, True),)
        observed.append(tree.reads)
    assert observed == [[path], [path]]


@pytest.mark.parametrize("invalid", ["unrepresentable", "digest_roundtrip"])
def test_non_reproducible_install_definition_refuses_with_a_rebuild_repair(invalid):
    value = definition()
    if invalid == "unrepresentable":
        value["unknown_v4_field"] = "unsupported"
    else:
        value["contracts"]["input"]["fields"]["value"]["required"] = True
    with pytest.raises(RequestRefusedError) as refusal:
        package_document(value).interface_registrations()
    assert refusal.value.error_code == "cruxible.provider_interface.definition_invalid"
    repair = refusal.value.repair.hand_edit
    assert "demo.interface" in repair.target
    assert repair.required_change == (
        "Declare only the v4 definition fields and rebuild the wheel "
        "so its interface_digest covers them."
    )


def test_v4_produces_is_sorted_unique_and_stored_in_the_definition():
    interface = registration(produces=["demo.b", "demo.a", "demo.b"])
    assert interface.interface.produces == ("demo.a", "demo.b")
    wire = json.loads(render_provider_interface(interface))
    assert wire["interface"]["produces"] == ["demo.a", "demo.b"]
    path = provider_interface_path(interface.identity.name)
    assert parse_provider_interface(render_provider_interface(interface), path=path) == interface


@pytest.mark.parametrize(
    "produces",
    [[], [""], ["PyPI.release"], ["https://pypi.org"], ["1source"], ["demo a"]],
)
def test_v4_produces_rejects_an_empty_list_and_non_canonical_names(produces):
    with pytest.raises(ValidationError, match="produces"):
        registration(produces=produces)


def test_v4_without_produces_keeps_its_exact_bytes_and_digests():
    # Pinned at 72b5e294a, before interfaces could declare produces.
    for extras, definition_digest, artifact_digest in (
        (
            {},
            "sha256:e57bf677ddd98a697f2857a6166f76df3214cfe1c0a0777050444474c88eed4c",
            "sha256:266faaef38d3a7bd96c4330958516365c4f2d9cd7d7718627f99316fe1d4444e",
        ),
        (
            {"refusals": ["provider_declined"], "description": "x"},
            "sha256:a25c764dd68e08a4b9a13d05596b1d0c63d74cdf3e2359350d8d3ce0851e508c",
            "sha256:a61a44536f35d15bb94b8e11cf7489a34c0b2c4f835a158ccd62fb08f3c5dbdf",
        ),
    ):
        interface = registration(**extras)
        assert "produces" not in json.loads(render_provider_interface(interface))["interface"]
        assert interface_definition_bytes(interface) == canonical_bytes(definition(**extras))
        assert interface_definition_digest(interface) == definition_digest
        assert provider_interface_digest(interface).tagged == artifact_digest


def test_a_package_export_declaring_produces_registers_it():
    value = definition(produces=["demo.a", "demo.b"])
    (interface,) = package_document(value).interface_registrations()
    assert interface.interface.produces == ("demo.a", "demo.b")
    assert json.loads(render_provider_interface(interface))["interface"] == value
    assert interface_definition_digest(interface) == (
        package_document(value).interfaces[0].interface_digest
    )
    # The wheel hashes its own spelling: an unsorted declaration cannot store exactly.
    with pytest.raises(RequestRefusedError, match="stored interface JSON"):
        package_document(definition(produces=["demo.b", "demo.a"])).interface_registrations()

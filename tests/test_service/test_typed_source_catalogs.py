"""Served source catalogs select only their typed owners and exact source members."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_digest,
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.procedures.line_specs import (
    line_identity_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
from cruxible_core.compiler.assembler import ProjectionAssembler
from cruxible_core.indexes.sqlite import bind_projection
from cruxible_core.indexes.typed_state import TypedStateReader
from cruxible_core.service.evidence.evidence import accepted_claim_providers
from cruxible_core.service.procedures.procedure_runs import (
    LineRunNotAccepted,
    SourceAcquisitionPolicyRequired,
    _accepted_acquisition_policies,
    _accepted_capture_contracts,
    _accepted_line_by_identity_digest,
    _accepted_line_mandates,
    _assert_line_closure_complete,
    _direct_acquisition_policy,
    _line_catalogs,
)
from tests.core_support._p2b1_support import accepted_interface, accepted_provider
from tests.core_support._pc_c_support import capture_contract, provider
from tests.core_support._projection_support import MemoryLedger, accepted_coordinate
from tests.core_support._support import initialize_local
from tests.test_integration.test_acquisition_policies import _policy, _rule
from tests.test_procedures.test_line_specs import _line
from tests.test_procedures.test_procedure_run_surface import (
    READ_TIME,
    _line_mandate,
    _slotless_procedure,
)


def _published_sources(tmp_path, tree, monkeypatch):
    instance, _owner = initialize_local(tmp_path)
    repository = MemoryLedger(tmp_path / "catalog-repository", tree)
    # Source bindings use actual Git blob object IDs. The older MemoryLedger
    # helper also keeps its synthetic entry IDs for its frozen projection tests.
    repository._blob_by_oid.update(
        {
            hashlib.sha256(f"blob {len(raw)}\0".encode() + raw).hexdigest(): raw
            for raw in tree.values()
        }
    )
    coordinate = accepted_coordinate(repository).model_copy(
        update={"compiler": instance.accepted_coordinate().compiler}
    )
    publication = tmp_path / "catalog-projection"
    publication.mkdir()
    assembler = ProjectionAssembler(
        repository,
        accepted=coordinate,
        publication_directory=publication,
        bodies=instance.body_store(),
    )
    result = assembler.assemble(
        assembler.request(output_staging_directory=publication / ".stage-catalog")
    )

    @contextmanager
    def bind(expected):
        assert expected == coordinate
        with bind_projection(Path(result.manifest_path), expected=coordinate) as projection:
            yield projection.attach_sources(repository, bodies=instance.body_store(), history=None)

    def no_tree(*_args, **_kwargs):
        pytest.fail("served typed catalogs must not scan accepted tree members")

    reads = []
    member_bytes = TypedStateReader.member_bytes

    def counted(reader, path):
        reads.append(path)
        return member_bytes(reader, path)

    monkeypatch.setattr(TypedStateReader, "member_bytes", counted)
    monkeypatch.setattr(instance, "bind_accepted_projection", bind)
    monkeypatch.setattr(instance, "tree_at", no_tree)
    monkeypatch.setattr(instance, "immutable_tree_at", no_tree)
    return instance, coordinate, reads


@pytest.mark.parametrize("unrelated", (2, 17))
def test_provider_grading_reads_only_its_selected_closure(tmp_path, monkeypatch, unrelated):
    contract = capture_contract()
    root = provider(contract, name="root")
    leaf = provider(contract, name="leaf").model_copy(
        update={"upstream_provenance": (root.identity,)}
    )
    owners = [root, leaf, *(provider(contract, name=f"unused-{i}") for i in range(unrelated))]
    tree = {
        capture_contract_path(contract.identity.name): render_capture_contract(contract),
        **{provider_path(item.identity.name): render_provider(item) for item in owners},
    }
    instance, coordinate, reads = _published_sources(tmp_path, tree, monkeypatch)
    providers = accepted_claim_providers(instance, coordinate=coordinate)
    assert reads == []
    assert providers[leaf.identity.qualified] == leaf
    assert providers[root.identity.qualified] == root
    assert providers[leaf.identity.qualified] == leaf
    assert reads == [provider_path("leaf"), provider_path("root")]
    assert providers.get("Provider:absent") is None
    assert set(providers) == {item.identity.qualified for item in owners}
    assert len(providers) == unrelated + 2
    assert reads == [provider_path("leaf"), provider_path("root")]


@pytest.mark.parametrize("unrelated", (2, 17))
def test_line_catalogs_read_only_live_exact_pins(tmp_path, monkeypatch, unrelated):
    live = accepted_provider()
    interface = accepted_interface()
    contract = capture_contract()
    retired = provider(contract, name="retired-provider").model_copy(
        update={"lifecycle": ArtifactLifecycle(state="retired")}
    )
    unused = [provider(contract, name=f"unused-{i}") for i in range(unrelated)]
    tree = {
        live.path: render_provider(live.provider),
        interface.path: render_provider_interface(interface.registration),
        provider_path(retired.identity.name): render_provider(retired),
        **{provider_path(item.identity.name): render_provider(item) for item in unused},
    }
    instance, coordinate, reads = _published_sources(tmp_path, tree, monkeypatch)
    providers, interfaces = _line_catalogs(
        instance,
        coordinate,
        (
            ArtifactPin(
                role="provider", target=live.provider.identity, artifact_digest=live.artifact_digest
            ),
            ArtifactPin(
                role="provider-interface",
                target=interface.registration.identity,
                artifact_digest=interface.artifact_digest,
            ),
            ArtifactPin(
                role="provider",
                target=retired.identity,
                artifact_digest=provider_digest(retired).tagged,
            ),
            ArtifactPin(
                role="provider", target=unused[0].identity, artifact_digest="sha256:" + "bc" * 32
            ),
        ),
    )
    assert providers == {live.artifact_digest: live}
    assert interfaces == {interface.artifact_digest: interface}
    assert reads == [live.path, interface.path]


@pytest.mark.parametrize("unrelated", (2, 17))
def test_mandates_select_exact_procedure_lifecycle_and_half_open_time(
    tmp_path, monkeypatch, unrelated
):
    accepted = _slotless_procedure("mandate-procedure")
    exact = _line_mandate(
        accepted, valid_from=READ_TIME, expires_at=READ_TIME + timedelta(microseconds=1)
    )
    foreign = accepted.model_copy(update={"artifact_digest": "sha256:" + "ab" * 32})
    excluded = [
        _line_mandate(foreign),
        _line_mandate(accepted, state="retired"),
        _line_mandate(accepted, valid_from=READ_TIME + timedelta(microseconds=1)),
        _line_mandate(accepted, expires_at=READ_TIME),
        *(_line_mandate(foreign) for _ in range(unrelated)),
    ]
    owners = [
        exact,
        *(
            item.model_copy(
                update={"identity": ArtifactIdentity(kind="ProcedureMandate", name=f"other-{i}")}
            )
            for i, item in enumerate(excluded)
        ),
    ]
    tree = {
        accepted.path: render_procedure(accepted.procedure),
        **{
            procedure_mandate_path(item.identity.name): render_procedure_mandate(item)
            for item in owners
        },
    }
    instance, coordinate, reads = _published_sources(tmp_path, tree, monkeypatch)
    actual = _accepted_line_mandates(
        instance, accepted, coordinate=coordinate, evaluation_time=READ_TIME
    )
    assert actual == ((procedure_mandate_digest(exact).tagged, exact),)
    assert reads == [procedure_mandate_path(exact.identity.name)]


@pytest.mark.parametrize("unrelated", (2, 17))
def test_line_identity_and_closure_select_only_bound_sources(tmp_path, monkeypatch, unrelated):
    procedure = _slotless_procedure("selected-procedure")
    template, _, _ = _line()
    pin = ArtifactPin(
        role="procedure",
        target=procedure.procedure.identity,
        artifact_digest=procedure.artifact_digest,
    )
    selected = template.model_copy(update={"procedure": pin, "pins": (pin,), "slot_bindings": ()})
    others = [
        selected.model_copy(update={"identity": ArtifactIdentity(kind="Line", name=f"other-{i}")})
        for i in range(unrelated)
    ]
    tree = {
        procedure.path: render_procedure(procedure.procedure),
        **{
            line_spec_path(item.identity.name): render_line_spec(item)
            for item in [selected, *others]
        },
    }
    instance, coordinate, reads = _published_sources(tmp_path, tree, monkeypatch)
    accepted = _accepted_line_by_identity_digest(
        instance, coordinate=coordinate, identity_digest=line_identity_digest(selected.identity)
    )
    assert accepted.line == selected
    assert reads == [line_spec_path(selected.identity.name)]
    reads.clear()
    _assert_line_closure_complete(instance, accepted, coordinate)
    assert reads == [procedure.path]
    reads.clear()
    with pytest.raises(LineRunNotAccepted):
        _accepted_line_by_identity_digest(
            instance, coordinate=coordinate, identity_digest="sha256:" + "ab" * 32
        )
    assert reads == []
    wrong_pin = pin.model_copy(update={"artifact_digest": "sha256:" + "ab" * 32})
    wrong = accepted.model_copy(update={"line": selected.model_copy(update={"pins": (wrong_pin,)})})
    with pytest.raises(PlaybillExecutionError, match="does not reproduce"):
        _assert_line_closure_complete(instance, wrong, coordinate)


@pytest.mark.parametrize("unrelated", (2, 17))
def test_source_catalogs_select_exact_contract_and_policy_pins(tmp_path, monkeypatch, unrelated):
    contract = capture_contract()
    policy = _policy(_rule("orders"))
    contracts = [contract, *(capture_contract(name=f"other-{i}") for i in range(unrelated))]
    policies = [
        policy,
        *(
            policy.model_copy(
                update={
                    "identity": ArtifactIdentity(kind="SourceAcquisitionPolicy", name=f"other-{i}")
                }
            )
            for i in range(unrelated)
        ),
    ]
    tree = {
        **{
            capture_contract_path(item.identity.name): render_capture_contract(item)
            for item in contracts
        },
        **{
            acquisition_policy_path(item.identity.name): render_acquisition_policy(item)
            for item in policies
        },
    }
    instance, coordinate, reads = _published_sources(tmp_path, tree, monkeypatch)
    capture_pin = ArtifactPin(
        role="capture-contract",
        target=contract.identity,
        artifact_digest=capture_contract_digest(contract).tagged,
    )
    policy_pin = ArtifactPin(
        role="acquisition-policy",
        target=policy.identity,
        artifact_digest=acquisition_policy_digest(policy).tagged,
    )
    assert _accepted_capture_contracts(instance, coordinate, (capture_pin,)) == {
        capture_pin.artifact_digest: contract
    }
    assert _accepted_acquisition_policies(instance, coordinate, pin=policy_pin) == (
        (policy_pin.artifact_digest, policy),
    )
    assert reads == [
        capture_contract_path(contract.identity.name),
        acquisition_policy_path(policy.identity.name),
    ]
    reads.clear()
    assert (
        _accepted_acquisition_policies(
            instance,
            coordinate,
            pin=policy_pin.model_copy(update={"artifact_digest": "sha256:" + "ab" * 32}),
        )
        == ()
    )
    assert reads == []
    procedure = _slotless_procedure("policy-procedure").procedure
    with pytest.raises(SourceAcquisitionPolicyRequired, match="accepted SourceAcquisitionPolicy"):
        _direct_acquisition_policy(
            instance, coordinate=coordinate, procedure=procedure, input_names=("orders",)
        )
    assert len(reads) == unrelated + 1
    reads.clear()
    pinned = procedure.model_copy(update={"pins": (*procedure.pins, policy_pin)})
    assert _direct_acquisition_policy(
        instance, coordinate=coordinate, procedure=pinned, input_names=("orders",)
    ) == (policy_pin.artifact_digest, policy)
    assert reads == [acquisition_policy_path(policy.identity.name)]

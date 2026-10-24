"""Build identities, immutable contracts, and accepted-order resolution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_interfaces import (
    parse_provider_interface,
    provider_interface_path,
    render_provider_interface,
)
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    ProviderBuild,
    evaluate_provider_law,
    parse_provider,
    provider_build_key,
    provider_digest,
    provider_path,
    render_provider,
)
from cruxible_core.errors import RequestRefusedError
from cruxible_core.providers.builds import BuildRow, resolve_provider_build
from tests.core_support._p2b1_support import accepted_interface, provider_v2
from tests.support.provider_builds import legacy_interface_contract


def build() -> ProviderBuild:
    original = provider_v2()
    payload = original.model_dump(mode="json")
    payload["artifact_format"] = "cruxible-provider-v4"
    payload["runtime_artifact"]["schema_version"] = 2
    payload["identity"]["name"] += "/" + provider_build_key(original.runtime_artifact).value
    return ProviderBuild.model_validate(payload)


def test_build_key_excludes_host_materialization_and_normalizes_distribution() -> None:
    provider = build()
    runtime = provider.runtime_artifact
    other = runtime.model_copy(
        update={
            "local_env": runtime.local_env.model_copy(
                update={"materialization_digests": {"linux-cp311": "sha256:" + "f" * 64}}
            )
        }
    )
    assert provider_build_key(runtime) == provider_build_key(other)
    assert (
        parse_provider(render_provider(provider), path=provider_path(provider.identity.name))
        == provider
    )
    wrong = provider.model_dump(mode="json")
    wrong["identity"]["name"] = "demo-provider/" + "0" * 64
    with pytest.raises(ValidationError, match="build_key_mismatch"):
        ProviderBuild.model_validate(wrong)


def test_build_law_accepts_add_and_retire_but_refuses_mutation_and_domain_takeover() -> None:
    provider = build()
    interface = accepted_interface()
    kw = {
        "path": provider_path(provider.identity.name),
        "interface_registrations": {interface.registration.identity.qualified: interface},
    }
    assert evaluate_provider_law(provider, predecessor=None, **kw).verdict == "accepted"
    previous = AcceptedProvider(
        provider=provider, artifact_digest=provider_digest(provider).tagged, path=kw["path"]
    )
    retired = provider.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=previous.artifact_digest
            )
        }
    )
    assert evaluate_provider_law(retired, predecessor=previous, **kw).verdict == "accepted"
    # A changed runtime declaration is forbidden even under a lifecycle successor.
    changed = retired.model_copy(update={"implementations": ()})
    assert (
        evaluate_provider_law(changed, predecessor=previous, **kw).diagnostics[0].code
        == "cruxible.provider.build_immutable"
    )
    assert (
        evaluate_provider_law(
            provider, predecessor=None, sibling_control_domains=frozenset({"other"}), **kw
        )
        .diagnostics[0]
        .code
        == "cruxible.provider.control_domain_differs"
    )


def test_contract_registration_has_no_classifier_and_legacy_registration_still_reads() -> None:
    old = accepted_interface().registration
    registration = legacy_interface_contract(
        interface_id=old.interface_id,
        interface_bytes_hex=old.interface_bytes_hex,
        interface_digest_domain=old.interface_digest_domain,
        interface_digest=old.interface_digest,
        effect_class=old.effect_class,
    )
    assert not any(
        "classifier" in name or "vocabulary" in name for name in registration.model_dump()
    )
    assert (
        parse_provider_interface(
            render_provider_interface(registration),
            path=provider_interface_path(registration.identity.name),
        )
        == registration
    )
    assert (
        parse_provider_interface(
            render_provider_interface(old), path=provider_interface_path(old.identity.name)
        )
        == old
    )
    original = provider_v2()
    assert (
        parse_provider(render_provider(original), path=provider_path(original.identity.name))
        == original
    )


def test_newest_means_last_registered_even_for_a_lower_version_and_excludes_retired() -> None:
    coordinate = AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root="sha256:" + "b" * 64,
        generation_root="sha256:" + "c" * 64,
        compiler_digest="sha256:" + "d" * 64,
    )
    rows = (
        BuildRow("Provider:demo/" + "a" * 64, "one", "sha256:" + "1" * 64, True, "2.0.0"),
        BuildRow("Provider:demo/" + "b" * 64, "two", "sha256:" + "2" * 64, True, "1.0.0"),
        BuildRow("Provider:demo/" + "c" * 64, "three", "sha256:" + "3" * 64, False, "3.0.0"),
    )
    sequences = {row.identity: i for i, row in enumerate(rows)}
    resolved = resolve_provider_build("demo", rows=rows, sequences=sequences, coordinate=coordinate)
    assert resolved.rule == "most_recently_registered" and resolved.version == "1.0.0"
    assert resolved.resolved == rows[1].identity.partition(":")[2]
    assert (
        resolve_provider_build(
            rows[0].identity, rows=rows, sequences=sequences, coordinate=coordinate
        ).rule
        == "exact"
    )
    with pytest.raises(RequestRefusedError, match="retired"):
        resolve_provider_build(
            rows[2].identity, rows=rows, sequences=sequences, coordinate=coordinate
        )


def test_legacy_bytes_project_unchanged_under_the_build_compiler() -> None:
    from cruxible_core.compiler.compiler import (
        PROVIDER_BUILDS_COMPILER,
        SUBJECT_IDENTITY_COMPILER,
        artifact_kinds_for_compiler,
        projection_registry_for_compiler,
    )
    from cruxible_core.compiler.projection_artifacts import parse_projection_tree

    legacy = provider_v2()
    interface = accepted_interface()
    tree = {
        provider_path(legacy.identity.name): render_provider(legacy),
        interface.path: render_provider_interface(interface.registration),
    }
    projections = [
        parse_projection_tree(
            tree,
            registry=projection_registry_for_compiler(compiler),
            artifact_kinds=artifact_kinds_for_compiler(compiler),
        )
        for compiler in (SUBJECT_IDENTITY_COMPILER, PROVIDER_BUILDS_COMPILER)
    ]
    assert projections[0].envelopes == projections[1].envelopes


def test_new_single_path_provider_is_closed_at_the_build_compiler(tmp_path) -> None:
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    head = instance.accepted_coordinate()
    tree = instance.immutable_tree_at(head.git_oid).fork()
    legacy = provider_v2()
    interface = accepted_interface()
    tree[provider_path(legacy.identity.name)] = render_provider(legacy)
    tree[interface.path] = render_provider_interface(interface.registration)
    result = instance.proposal_service().preview(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/single-path", proposed_base_oid=head.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-10-15T00:00:00.000000Z",
    )
    assert "cruxible.provider.single_path_closed" in {
        item.code for item in result.evaluation.diagnostics
    }


def test_discovery_lists_builds_under_the_registration_they_actually_pin() -> None:
    from cruxible_core.service.discovery.discovery import _provider_interfaces

    old = accepted_interface().registration
    contract = legacy_interface_contract(
        interface_id=old.interface_id,
        interface_bytes_hex=old.interface_bytes_hex,
        interface_digest_domain=old.interface_digest_domain,
        interface_digest=old.interface_digest,
        effect_class=old.effect_class,
    )
    entries = _provider_interfaces(
        (provider_v2(),),
        (old, contract),
        newest={old.interface_id: contract.identity.qualified},
        newest_builds=frozenset({provider_v2().identity.qualified}),
    )
    by_identity = {entry.entry.identity: entry.entry for entry in entries}
    assert len(by_identity[old.identity.qualified].providers) == 1
    assert by_identity[contract.identity.qualified].providers == ()

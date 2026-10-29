"""Legacy metadata preserves build order; runtime replacement registers a new build."""

import sqlite3
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.canonical import CURRENT_ARTIFACT_CODEC
from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
from cruxible_core.derived.derived_state import SnapshotTree
from cruxible_core.indexes import provider_heads
from cruxible_core.indexes.typed_sqlite import insert_members, parse_static_owners, replace_rows
from cruxible_core.indexes.typed_state import schema_sql
from cruxible_core.providers.builds import indexed_newest_provider
from tests.core_support._p2b1_support import provider_v2
from tests.test_scale.test_provider_build_install import _instance
from tests.test_scale.test_provider_build_install import package as package


@pytest.mark.parametrize("genesis", [False, True])
def test_legacy_metadata_order_matches_cold_rebuild(tmp_path, monkeypatch, package, genesis):  # noqa: F811
    _document, build = package
    legacy = build(1)
    from cruxible_client.contracts.providers import (
        ProviderRuntimeArtifactPayloadV1,
        ProviderV2,
        provider_expected_implementation_records,
    )

    def as_legacy(provider):
        payload = provider.model_dump(mode="json")
        payload["artifact_format"] = "playbill-provider-v2"
        payload["identity"] = {"kind": "Provider", "name": provider.provider_name}
        payload["runtime_artifact"]["schema_version"] = 1
        # NamedProviderBuild names capture Contracts (capture_contracts,
        # ArtifactNameRef); ProviderV2 pins them exactly (capture_contract_digests).
        # This fixture never declares one, so the conversion is just the field
        # rename -- there is no digest to carry over.
        names = payload.pop("capture_contracts", ())
        assert not names, "fixture builds declare no capture contracts to convert"
        payload["capture_contract_digests"] = ()
        runtime = ProviderRuntimeArtifactPayloadV1.model_validate(payload["runtime_artifact"])
        payload["implementations"] = [
            item.model_dump(mode="json")
            for item in provider_expected_implementation_records(runtime)
        ]
        return ProviderV2.model_validate(payload)

    legacy = as_legacy(legacy)
    newer = build(2)
    metadata = legacy.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=provider_digest(legacy).tagged),
            "signing_keys": provider_v2().signing_keys,
        }
    )
    # The lifecycle successor itself changes the artifact commitment, without changing runtime.
    replacement = as_legacy(build(3)).model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=provider_digest(metadata).tagged),
        }
    )
    records = {}
    versions = {}
    instance = _instance(tmp_path, SnapshotTree({}))
    codec = CURRENT_ARTIFACT_CODEC
    monkeypatch.setattr(provider_heads, "parse_change_set_record", lambda raw, **kw: records[raw])
    tree = {}
    for seq, provider in enumerate((legacy, newer, metadata, replacement), 0 if genesis else 1):
        path = provider_path(provider.identity.name)
        raw = render_provider(provider)
        marker = str(seq).encode()
        records[marker] = SimpleNamespace(
            sequence=seq,
            members=(
                SimpleNamespace(
                    artifact_kind="provider",
                    path=path,
                    candidate_artifact_digest=provider_digest(provider).tagged,
                ),
            ),
        )
        versions[path, seq] = raw
        changed = {path: raw}
        if seq:
            changed[f"changesets/{seq}.json"] = marker
        tree.update(changed)
        # Parse owners separately: change records here stand for already verified accepted records.
        parsed = parse_static_owners({path: raw}, accepted=instance.coordinate)
        replace_rows(
            instance.connection,
            request=instance.coordinate,
            parsed=parsed,
            sources=changed,
            codec=codec,
            changed_paths=tuple(changed),
            whole_checks=False,
        )
        expected = newer if provider is newer or provider is metadata else provider
        assert (
            indexed_newest_provider(instance.connection, provider.provider_name).identity
            == expected.identity.qualified
        )
        cold = sqlite3.connect(":memory:")
        cold.executescript(schema_sql())
        owners = {p: b for p, b in tree.items() if p.startswith("providers/")}
        from cruxible_core.indexes.typed_state import insert_owners

        insert_members(cold, owners, instance.coordinate.git_object_format)
        insert_owners(
            cold,
            parsed=parse_static_owners(owners, accepted=instance.coordinate),
            blobs=owners,
            codec=codec,
        )
        provider_heads.refresh_provider_heads(
            cold, tree, historical_source=lambda p, s: versions.get((p, s))
        )
        assert list(
            cold.execute("SELECT identity,registration_sequence FROM providers ORDER BY identity")
        ) == list(
            instance.connection.execute(
                "SELECT identity,registration_sequence FROM providers ORDER BY identity"
            )
        )
        cold.close()
    instance.connection.close()

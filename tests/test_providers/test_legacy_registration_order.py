"""Legacy metadata preserves build order; runtime replacement registers a new build."""

import sqlite3
from types import SimpleNamespace

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
from cruxible_core.compiler.compiler import PROVIDER_BUILDS_COMPILER, artifact_codec_for_compiler
from cruxible_core.derived.derived_state import SnapshotTree
from cruxible_core.indexes import provider_heads
from cruxible_core.indexes.typed_sqlite import insert_members, parse_static_owners, replace_rows
from cruxible_core.indexes.typed_state import schema_sql
from cruxible_core.providers.builds import indexed_newest_provider
from tests.test_scale.test_provider_build_install import _instance


def test_legacy_metadata_order_matches_cold_rebuild(tmp_path, monkeypatch, package):
    _document, build = package
    legacy = build(1)
    from cruxible_client.contracts.providers import ProviderV2

    legacy = ProviderV2.model_validate(
        {
            **legacy.model_dump(mode="json"),
            "artifact_format": "playbill-provider-v2",
            "identity": {"kind": "Provider", "name": legacy.provider_name},
        }
    )
    newer = build(2)
    metadata = legacy.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=provider_digest(legacy).tagged),
            "signing_keys": (),
        }
    )
    # The lifecycle successor itself changes the artifact commitment, without changing runtime.
    replacement = legacy.model_copy(
        update={
            "runtime_artifact": build(3).runtime_artifact,
            "lifecycle": ArtifactLifecycle(predecessor_digest=provider_digest(metadata).tagged),
        }
    )
    records = {}
    versions = {}
    instance = _instance(tmp_path, SnapshotTree({}))
    codec = artifact_codec_for_compiler(PROVIDER_BUILDS_COMPILER)
    monkeypatch.setattr(provider_heads, "parse_change_set_record", lambda raw, **kw: records[raw])
    tree = {}
    for seq, provider in enumerate((legacy, newer, metadata, replacement), 1):
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
        changed = {path: raw, f"changesets/{seq}.json": marker}
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
        expected = newer if seq in (2, 3) else provider
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
            cold, tree, historical_source=lambda p, s: versions[p, s]
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

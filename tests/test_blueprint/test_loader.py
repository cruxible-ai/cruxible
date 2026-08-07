"""Canonicalization and content digest for blueprint documents."""

from __future__ import annotations

import json

import pytest
import yaml

from cruxible_core.blueprint import (
    BlueprintDigestError,
    BlueprintValidationError,
    build_attachment_manifest,
    canonical_bytes,
    canonical_document,
    canonical_yaml,
    compute_blueprint_digest,
    load_blueprint,
    load_blueprint_text,
    parse_blueprint,
)


def _digest(document) -> str:
    return compute_blueprint_digest(parse_blueprint(document))


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


def test_canonical_document_round_trips_through_yaml(document):
    blueprint = parse_blueprint(document)

    reloaded = load_blueprint_text(canonical_yaml(blueprint))

    assert canonical_document(reloaded) == canonical_document(blueprint)


def test_canonical_document_round_trips_through_json(document):
    blueprint = parse_blueprint(document)

    reloaded = parse_blueprint(json.loads(json.dumps(canonical_document(blueprint))))

    assert canonical_document(reloaded) == canonical_document(blueprint)


def test_canonical_document_re_flattens_procedure_bodies(document):
    blueprint = parse_blueprint(document)

    canonical = canonical_document(blueprint)

    assert canonical["procedures"][0]["name"] == "widget_score"
    assert "definition" not in canonical["procedures"][0]


def test_canonical_document_drops_empty_containers(document):
    blueprint = parse_blueprint(document)

    canonical = canonical_document(blueprint)

    assert "triggers" not in canonical
    assert "pipelines" not in canonical


def test_canonical_document_drops_an_empty_dependency_block(document):
    document.pop("dependencies")

    canonical = canonical_document(parse_blueprint(document))

    assert "dependencies" not in canonical


def test_canonical_document_prunes_only_the_empty_dependency_lists(document):
    document["dependencies"] = {"entity_types": ["Widget"]}

    canonical = canonical_document(parse_blueprint(document))

    assert canonical["dependencies"] == {"entity_types": ["Widget"]}


def test_canonical_document_keeps_a_required_empty_precondition(document):
    canonical = canonical_document(parse_blueprint(document))

    assert canonical["procedures"][0]["precondition"] == {}


def test_canonical_bytes_have_sorted_keys(document):
    blueprint = parse_blueprint(document)

    text = canonical_bytes(blueprint).decode("utf-8")

    assert text.startswith('{"blueprint":')
    assert text.index('"contracts"') < text.index('"dependencies"') < text.index('"procedures"')


# ---------------------------------------------------------------------------
# Digest stability
# ---------------------------------------------------------------------------


def test_digest_is_stable_across_key_reordering(document):
    reordered = dict(reversed(list(document.items())))
    reordered["contracts"] = dict(reversed(list(document["contracts"].items())))

    assert _digest(reordered) == _digest(document)


def test_digest_is_stable_across_reserialization(document):
    blueprint = parse_blueprint(document)
    original = compute_blueprint_digest(blueprint)

    once = load_blueprint_text(canonical_yaml(blueprint))
    twice = load_blueprint_text(canonical_yaml(once))

    assert compute_blueprint_digest(once) == original
    assert compute_blueprint_digest(twice) == original


def test_digest_normalizes_explicitly_written_defaults(document):
    baseline = _digest(document)
    document["query_slots"]["subject_rows"]["default"]["relationship_state"] = "live"

    assert _digest(document) == baseline


def test_digest_moves_when_a_semantic_field_changes(document):
    baseline = _digest(document)
    document["slots"]["scorer"]["billing"] = ["platform"]

    assert _digest(document) != baseline


def test_digest_moves_when_the_version_changes(document):
    baseline = _digest(document)
    document["blueprint"]["version"] = "1.0.1"

    assert _digest(document) != baseline


def test_digest_ignores_yaml_comments_and_formatting(document):
    blueprint = parse_blueprint(document)
    commented = "# a publisher note\n" + yaml.safe_dump(document, sort_keys=False)

    assert compute_blueprint_digest(load_blueprint_text(commented)) == compute_blueprint_digest(
        blueprint
    )


def test_digest_is_prefixed_and_hex(document):
    digest = _digest(document)

    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    int(digest.removeprefix("sha256:"), 16)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def _write_blueprint(tmp_path, document):
    path = tmp_path / "widget.blueprint.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_attachment_manifest_is_ordered_by_path(tmp_path):
    (tmp_path / "b.md").write_text("bee", encoding="utf-8")
    (tmp_path / "a.md").write_text("ay", encoding="utf-8")

    manifest = build_attachment_manifest(["b.md", "a.md"], root=tmp_path)

    assert [entry.path for entry in manifest] == ["a.md", "b.md"]


def test_attachment_order_does_not_change_the_digest(tmp_path, document):
    path = _write_blueprint(tmp_path, document)
    (tmp_path / "b.md").write_text("bee", encoding="utf-8")
    (tmp_path / "a.md").write_text("ay", encoding="utf-8")

    forward = load_blueprint(path, attachments=["a.md", "b.md"])
    backward = load_blueprint(path, attachments=["b.md", "a.md"])

    assert forward.digest == backward.digest


def test_attachment_content_changes_the_digest(tmp_path, document):
    path = _write_blueprint(tmp_path, document)
    doc = tmp_path / "guide.md"
    doc.write_text("first", encoding="utf-8")
    before = load_blueprint(path, attachments=["guide.md"]).digest

    doc.write_text("second", encoding="utf-8")

    assert load_blueprint(path, attachments=["guide.md"]).digest != before


def test_attachments_change_the_digest_against_a_bare_document(tmp_path, document):
    path = _write_blueprint(tmp_path, document)
    (tmp_path / "guide.md").write_text("first", encoding="utf-8")

    bare = load_blueprint(path)
    with_attachment = load_blueprint(path, attachments=["guide.md"])

    assert bare.digest != with_attachment.digest
    assert bare.attachments == []


def test_duplicate_attachment_refused(tmp_path):
    (tmp_path / "guide.md").write_text("first", encoding="utf-8")

    with pytest.raises(BlueprintDigestError, match="more than once"):
        build_attachment_manifest(["guide.md", "./guide.md"], root=tmp_path)


def test_attachment_outside_the_blueprint_root_refused(tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("nope", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(BlueprintDigestError, match="outside the blueprint root"):
        build_attachment_manifest([outside], root=root)


def test_missing_attachment_refused(tmp_path):
    with pytest.raises(BlueprintDigestError, match="Cannot read blueprint attachment"):
        build_attachment_manifest(["absent.md"], root=tmp_path)


def test_missing_document_refused(tmp_path):
    with pytest.raises(BlueprintDigestError, match="Cannot read blueprint document"):
        load_blueprint(tmp_path / "absent.yaml")


def test_loaded_blueprint_exposes_canonical_views(tmp_path, document):
    loaded = load_blueprint(_write_blueprint(tmp_path, document))

    assert loaded.canonical_bytes == canonical_bytes(loaded.blueprint)
    assert loaded.canonical_document == canonical_document(loaded.blueprint)


# ---------------------------------------------------------------------------
# Decode failures
# ---------------------------------------------------------------------------


def test_unparseable_yaml_refused():
    with pytest.raises(BlueprintValidationError, match="not parseable YAML/JSON"):
        load_blueprint_text("blueprint: [unclosed")


def test_empty_document_refused():
    with pytest.raises(BlueprintValidationError, match="is empty"):
        load_blueprint_text("# only a comment\n")

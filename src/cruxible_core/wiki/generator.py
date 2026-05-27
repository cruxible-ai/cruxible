"""Deterministic Markdown wiki generation from local world state."""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cruxible_core.canonical_views.mermaid_utils import (
    MermaidLegendItem,
    escape_mermaid_label,
    mermaid_id,
    render_mermaid_inline_legend,
)
from cruxible_core.config.composition_ownership import (
    resolve_composition_for_instance,
)
from cruxible_core.config.property_validation import enum_values
from cruxible_core.config.schema import (
    CoreConfig,
    EntityTypeSchema,
    NamedQuerySchema,
    ProviderSchema,
    RelationshipSchema,
    WorkflowSchema,
    WorkflowStepSchema,
)
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.types import EntityInstance
from cruxible_core.group.types import CandidateGroup, CandidateMember, GroupResolution
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.types import Receipt
from cruxible_core.temporal import format_datetime
from cruxible_core.workflow.contracts import contract_reference_label, resolve_contract

MAX_STORE_SCAN = 10_000
MANIFEST_NAME = ".cruxible-manifest.json"
DEFAULT_MAX_PER_TYPE = 50
WikiScope = Literal["local", "evidence", "all"]
MermaidNodeRole = Literal["focus", "local", "upstream", "context"]
DISPLAY_PROPERTY_PREFERENCE = (
    "name",
    "title",
    "hostname",
    "cve_id",
    "product_name",
    "vendor_name",
    "service_id",
    "owner_id",
    "asset_id",
    "exception_id",
    "control_id",
    "patch_window_id",
)


@dataclass(frozen=True, order=True)
class SubjectRef:
    """Stable identifier for one rendered subject page."""

    entity_type: str
    entity_id: str

    @property
    def key(self) -> str:
        return f"{self.entity_type}:{self.entity_id}"


@dataclass(frozen=True)
class WikiOptions:
    """High-level render options."""

    output_dir: Path
    focus: tuple[SubjectRef, ...] = ()
    include_types: tuple[str, ...] = ()
    all_subjects: bool = False
    scope: WikiScope = "evidence"
    max_per_type: int = DEFAULT_MAX_PER_TYPE


@dataclass(frozen=True)
class _NeighborRelationship:
    relationship_type: str
    relationship_schema: RelationshipSchema | None
    properties: dict[str, Any]


def render_wiki(instance: InstanceProtocol, options: WikiOptions) -> list[Path]:
    """Render a deterministic Markdown wiki to ``options.output_dir``."""
    pages = build_wiki_pages(instance, options)
    return write_wiki_pages(options.output_dir, pages)


def build_wiki_pages(instance: InstanceProtocol, options: WikiOptions) -> dict[Path, str]:
    """Build wiki pages without writing them to disk."""
    generator = _WikiGenerator(instance)
    return generator.build_pages(options)


def write_wiki_pages(output_dir: Path, pages: dict[Path, str]) -> list[Path]:
    """Write wiki pages to disk with manifest cleanup."""
    return _write_pages(output_dir, pages)


class _WikiGenerator:
    """Collect and render wiki pages from instance state."""

    def __init__(self, instance: InstanceProtocol) -> None:
        self.instance = instance
        self.output_dir: Path | None = None
        composition = resolve_composition_for_instance(instance)
        self.config = composition.config
        self.ownership = composition.ownership
        self.graph = instance.load_graph()
        self.head_snapshot_id = instance.get_head_snapshot_id()
        self.max_per_type = DEFAULT_MAX_PER_TYPE
        self.subject_entities = {
            SubjectRef(entity.entity_type, entity.entity_id): entity
            for entity in self.graph.iter_all_entities()
        }
        self.relationships_by_name = {
            relationship.name: relationship for relationship in self.config.relationships
        }

        self.receipts = self._load_receipts()
        self.receipt_index = self._build_receipt_index(self.receipts.values())
        self.feedback_records = self._load_feedback()
        self.feedback_by_subject = self._build_feedback_index(self.feedback_records)
        (
            self.groups,
            self.members_by_group,
            self.groups_by_subject,
            self.resolutions_by_id,
        ) = self._load_groups()
        self.outcomes = self._load_outcomes()
        self.outcomes_by_receipt = self._build_outcomes_by_receipt(self.outcomes)
        self.outcomes_by_resolution = self._build_outcomes_by_resolution(self.outcomes)
        self.traces = self._load_traces()

    def build_pages(self, options: WikiOptions) -> dict[Path, str]:
        self.output_dir = options.output_dir
        self.max_per_type = max(1, options.max_per_type)
        scope = _effective_scope(options)
        subjects = self._select_subjects(options)
        rendered_subjects = set(subjects)
        receipt_ids = self._collect_receipt_ids(subjects)
        reference_scope: WikiScope = "evidence" if options.focus else scope
        query_names, workflow_names, provider_names = self._collect_reference_names(
            receipt_ids,
            subjects,
            scope=reference_scope,
        )

        pages: dict[Path, str] = {}
        pages[Path("index.md")] = self._render_index_page(
            subjects,
            scope=scope,
            query_names=query_names,
            workflow_names=workflow_names,
            provider_names=provider_names,
        )
        pages[Path("governance") / "pending-review.md"] = self._render_pending_review_page(
            rendered_subjects
        )
        pages[Path("governance") / "recent-decisions.md"] = self._render_recent_decisions_page(
            rendered_subjects
        )
        pages[Path("governance") / "recent-outcomes.md"] = self._render_recent_outcomes_page(
            rendered_subjects
        )

        for subject in subjects:
            pages[self._subject_path(subject)] = self._render_subject_page(
                subject,
                rendered_subjects=rendered_subjects,
            )

        for receipt_id in receipt_ids:
            receipt = self.receipts.get(receipt_id)
            if receipt is None:
                continue
            pages[self._receipt_path(receipt_id)] = self._render_receipt_page(
                receipt,
                rendered_subjects=rendered_subjects,
            )

        for query_name in sorted(query_names):
            query_schema = self.config.named_queries.get(query_name)
            if query_schema is None:
                continue
            pages[self._query_path(query_name)] = self._render_query_page(
                query_name,
                query_schema,
            )

        for workflow_name in sorted(workflow_names):
            workflow_schema = self.config.workflows.get(workflow_name)
            if workflow_schema is None:
                continue
            pages[self._workflow_path(workflow_name)] = self._render_workflow_page(
                workflow_name,
                workflow_schema,
                rendered_receipt_ids=receipt_ids,
            )

        for provider_name in sorted(provider_names):
            provider_schema = self.config.providers.get(provider_name)
            if provider_schema is None:
                continue
            pages[self._provider_path(provider_name)] = self._render_provider_page(
                provider_name,
                provider_schema,
            )

        for entity_type, entity_schema in sorted(self.config.entity_types.items()):
            pages[self._entity_type_path(entity_type)] = self._render_entity_type_page(
                entity_type,
                entity_schema,
            )

        for relationship in sorted(self.config.relationships, key=lambda item: item.name):
            pages[self._relationship_type_path(relationship.name)] = (
                self._render_relationship_type_page(relationship)
            )

        return {
            relative_path: self._with_generated_footer(relative_path, content)
            for relative_path, content in pages.items()
        }

    def _load_receipts(self) -> dict[str, Receipt]:
        store = self.instance.get_receipt_store()
        try:
            summaries = store.list_receipts(limit=MAX_STORE_SCAN)
            receipts: dict[str, Receipt] = {}
            for summary in summaries:
                receipt_id = str(summary["receipt_id"])
                receipt = store.get_receipt(receipt_id)
                if receipt is not None:
                    receipts[receipt.receipt_id] = receipt
            return receipts
        finally:
            store.close()

    def _load_feedback(self) -> list[FeedbackRecord]:
        store = self.instance.get_feedback_store()
        try:
            return store.list_feedback(limit=MAX_STORE_SCAN)
        finally:
            store.close()

    def _load_groups(
        self,
    ) -> tuple[
        list[CandidateGroup],
        dict[str, list[CandidateMember]],
        dict[str, list[CandidateGroup]],
        dict[str, GroupResolution],
    ]:
        store = self.instance.get_group_store()
        try:
            groups = store.list_groups(limit=MAX_STORE_SCAN)
            members_by_group: dict[str, list[CandidateMember]] = {}
            groups_by_subject: dict[str, list[CandidateGroup]] = defaultdict(list)
            seen_per_subject: dict[str, set[str]] = defaultdict(set)
            for group in groups:
                members = store.get_members(group.group_id)
                members_by_group[group.group_id] = members
                for member in members:
                    for key in (
                        f"{member.from_type}:{member.from_id}",
                        f"{member.to_type}:{member.to_id}",
                    ):
                        if group.group_id not in seen_per_subject[key]:
                            seen_per_subject[key].add(group.group_id)
                            groups_by_subject[key].append(group)
            resolutions = {
                resolution.resolution_id: resolution
                for resolution in store.list_resolutions(limit=MAX_STORE_SCAN)
            }
            return groups, members_by_group, groups_by_subject, resolutions
        finally:
            store.close()

    def _load_outcomes(self) -> list[OutcomeRecord]:
        store = self.instance.get_feedback_store()
        try:
            return store.list_outcomes(limit=MAX_STORE_SCAN)
        finally:
            store.close()

    def _load_traces(self) -> dict[str, ExecutionTrace]:
        trace_ids: set[str] = set()
        for receipt in self.receipts.values():
            trace_ids.update(_extract_trace_ids_from_receipt(receipt))
        for group in self.groups:
            trace_ids.update(group.source_trace_ids)
        for outcome in self.outcomes:
            trace_ids.update(_extract_trace_ids_from_lineage(outcome.lineage_snapshot))

        store = self.instance.get_receipt_store()
        try:
            traces: dict[str, ExecutionTrace] = {}
            for trace_id in sorted(trace_ids):
                trace = store.get_trace(trace_id)
                if trace is not None:
                    traces[trace.trace_id] = trace
            return traces
        finally:
            store.close()

    def _build_receipt_index(self, receipts: Any) -> dict[str, set[str]]:
        index: dict[str, set[str]] = defaultdict(set)
        for receipt in receipts:
            for ref in _entity_refs_from_receipt(receipt):
                index[ref.key].add(receipt.receipt_id)
        return index

    def _build_feedback_index(
        self,
        feedback_records: list[FeedbackRecord],
    ) -> dict[str, list[FeedbackRecord]]:
        index: dict[str, list[FeedbackRecord]] = defaultdict(list)
        for record in feedback_records:
            index[f"{record.target.from_type}:{record.target.from_id}"].append(record)
            index[f"{record.target.to_type}:{record.target.to_id}"].append(record)
        return index

    def _build_outcomes_by_receipt(
        self,
        outcomes: list[OutcomeRecord],
    ) -> dict[str, list[OutcomeRecord]]:
        index: dict[str, list[OutcomeRecord]] = defaultdict(list)
        for outcome in outcomes:
            index[outcome.receipt_id].append(outcome)
        return index

    def _build_outcomes_by_resolution(
        self,
        outcomes: list[OutcomeRecord],
    ) -> dict[str, list[OutcomeRecord]]:
        index: dict[str, list[OutcomeRecord]] = defaultdict(list)
        for outcome in outcomes:
            if outcome.anchor_type == "resolution" and outcome.anchor_id:
                index[outcome.anchor_id].append(outcome)
        return index

    def _select_subjects(self, options: WikiOptions) -> list[SubjectRef]:
        include_types = set(options.include_types)
        subjects: set[SubjectRef] = set()
        scope = _effective_scope(options)

        if options.focus:
            for focus in options.focus:
                if focus not in self.subject_entities:
                    continue
                subjects.add(focus)
                inspect = self.graph.get_neighbor_relationships(
                    focus.entity_type,
                    focus.entity_id,
                    direction="both",
                )
                for row in inspect:
                    neighbor = row.get("entity")
                    if isinstance(neighbor, EntityInstance):
                        subjects.add(SubjectRef(neighbor.entity_type, neighbor.entity_id))
        elif scope == "all":
            subjects = set(self.subject_entities)
        elif scope == "local":
            if self.ownership.ownership_available:
                subjects = self._select_local_subjects()
            else:
                subjects = self._select_evidence_subjects()
        else:
            subjects = self._select_evidence_subjects()

        subjects = {subject for subject in subjects if subject in self.subject_entities}
        if include_types:
            subjects = {subject for subject in subjects if subject.entity_type in include_types}
        return sorted(subjects)

    def _select_evidence_subjects(self) -> set[SubjectRef]:
        subjects: set[SubjectRef] = set()
        for key in self.receipt_index:
            subjects.add(_subject_ref_from_key(key))
        for key in self.feedback_by_subject:
            subjects.add(_subject_ref_from_key(key))
        for key in self.groups_by_subject:
            subjects.add(_subject_ref_from_key(key))
        if not subjects:
            subjects = set(self.subject_entities)
        return subjects

    def _select_local_subjects(self) -> set[SubjectRef]:
        subjects = {
            subject
            for subject in self.subject_entities
            if self.ownership.is_local_entity_type(subject.entity_type)
        }
        local_subjects = set(subjects)
        local_relationship_types = set(self.ownership.local_relationship_types)

        for edge in self.graph.iter_edges():
            relationship_type = str(edge.get("relationship_type", ""))
            if relationship_type not in local_relationship_types:
                continue
            from_ref = SubjectRef(str(edge["from_type"]), str(edge["from_id"]))
            to_ref = SubjectRef(str(edge["to_type"]), str(edge["to_id"]))
            if from_ref in local_subjects or to_ref in local_subjects:
                subjects.add(from_ref)
                subjects.add(to_ref)

        for record in self.feedback_records:
            target = record.target
            if target.relationship_type not in local_relationship_types:
                continue
            subjects.add(SubjectRef(target.from_type, target.from_id))
            subjects.add(SubjectRef(target.to_type, target.to_id))

        for group in self.groups:
            if group.relationship_type not in local_relationship_types:
                continue
            for member in self.members_by_group.get(group.group_id, []):
                subjects.add(SubjectRef(member.from_type, member.from_id))
                subjects.add(SubjectRef(member.to_type, member.to_id))

        return subjects

    def _collect_receipt_ids(self, subjects: list[SubjectRef]) -> set[str]:
        receipt_ids: set[str] = set()
        for subject in subjects:
            receipt_ids.update(self.receipt_index.get(subject.key, set()))
            for group in self.groups_by_subject.get(subject.key, []):
                if group.source_workflow_receipt_id:
                    receipt_ids.add(group.source_workflow_receipt_id)
        return receipt_ids

    def _collect_query_names(self, receipt_ids: set[str]) -> set[str]:
        return {
            receipt.query_name
            for receipt_id in receipt_ids
            for receipt in [self.receipts.get(receipt_id)]
            if receipt is not None and receipt.operation_type == "query"
        }

    def _collect_workflow_names(
        self,
        receipt_ids: set[str],
        subjects: list[SubjectRef],
    ) -> set[str]:
        workflow_names = {
            receipt.query_name
            for receipt_id in receipt_ids
            for receipt in [self.receipts.get(receipt_id)]
            if receipt is not None and receipt.operation_type == "workflow"
        }
        for subject in subjects:
            for group in self.groups_by_subject.get(subject.key, []):
                if group.source_workflow_name:
                    workflow_names.add(group.source_workflow_name)
        return workflow_names

    def _collect_provider_names(self, receipt_ids: set[str]) -> set[str]:
        provider_names: set[str] = set()
        for receipt_id in receipt_ids:
            receipt = self.receipts.get(receipt_id)
            if receipt is None:
                continue
            for node in receipt.nodes:
                if node.node_type != "plan_step":
                    continue
                provider_name = str(node.detail.get("provider_name", "")).strip()
                if provider_name:
                    provider_names.add(provider_name)
        return provider_names

    def _collect_reference_names(
        self,
        receipt_ids: set[str],
        subjects: list[SubjectRef],
        *,
        scope: WikiScope,
    ) -> tuple[set[str], set[str], set[str]]:
        if scope == "all":
            return (
                set(self.config.named_queries),
                set(self.config.workflows),
                set(self.config.providers),
            )

        query_names = self._collect_query_names(receipt_ids)
        workflow_names = self._collect_workflow_names(receipt_ids, subjects)
        provider_names = self._collect_provider_names(receipt_ids)

        if scope == "local" and self.ownership.ownership_available:
            if self.ownership.surface_ownership_available:
                query_names.update(self.ownership.local_named_queries)
                workflow_names.update(self.ownership.local_workflows)
                provider_names.update(self.ownership.local_providers)
            workflow_names.update(self._local_group_workflows())

        query_names, workflow_names, provider_names = self._expand_reference_dependencies(
            query_names,
            workflow_names,
            provider_names,
        )
        return query_names, workflow_names, provider_names

    def _local_group_workflows(self) -> set[str]:
        local_workflows: set[str] = set()
        for group in self.groups:
            if not self.ownership.is_local_relationship_type(group.relationship_type):
                continue
            if group.source_workflow_name:
                local_workflows.add(group.source_workflow_name)
        return local_workflows

    def _expand_reference_dependencies(
        self,
        query_names: set[str],
        workflow_names: set[str],
        provider_names: set[str],
    ) -> tuple[set[str], set[str], set[str]]:
        expanded_queries = set(query_names)
        expanded_workflows = set(workflow_names)
        expanded_providers = set(provider_names)
        for workflow_name in sorted(expanded_workflows):
            workflow = self.config.workflows.get(workflow_name)
            if workflow is None:
                continue
            for step in workflow.steps:
                if step.query is not None:
                    expanded_queries.add(step.query)
                if step.provider is not None:
                    expanded_providers.add(step.provider)
        return expanded_queries, expanded_workflows, expanded_providers

    def _subject_receipts(self, subject: SubjectRef) -> list[Receipt]:
        receipt_ids = sorted(self.receipt_index.get(subject.key, set()))
        receipts = [
            self.receipts[receipt_id] for receipt_id in receipt_ids if receipt_id in self.receipts
        ]
        return sorted(receipts, key=lambda receipt: receipt.created_at, reverse=True)

    def _subject_outcomes(self, subject: SubjectRef) -> list[OutcomeRecord]:
        receipt_ids = self.receipt_index.get(subject.key, set())
        resolution_ids = {
            group.resolution_id
            for group in self.groups_by_subject.get(subject.key, [])
            if group.resolution_id
        }
        outcomes: list[OutcomeRecord] = []
        for receipt_id in receipt_ids:
            outcomes.extend(self.outcomes_by_receipt.get(receipt_id, []))
        for resolution_id in resolution_ids:
            outcomes.extend(self.outcomes_by_resolution.get(resolution_id, []))
        return sorted(outcomes, key=lambda record: record.created_at, reverse=True)

    def _subject_path(self, subject: SubjectRef) -> Path:
        return (
            Path("subjects") / _slugify(subject.entity_type) / f"{_slugify(subject.entity_id)}.md"
        )

    def _receipt_path(self, receipt_id: str) -> Path:
        return Path("evidence") / "receipts" / f"{_slugify(receipt_id)}.md"

    def _query_path(self, query_name: str) -> Path:
        return Path("reference") / "queries" / f"{_slugify(query_name)}.md"

    def _workflow_path(self, workflow_name: str) -> Path:
        return Path("reference") / "workflows" / f"{_slugify(workflow_name)}.md"

    def _provider_path(self, provider_name: str) -> Path:
        return Path("reference") / "providers" / f"{_slugify(provider_name)}.md"

    def _entity_type_path(self, entity_type: str) -> Path:
        return Path("reference") / "entity-types" / f"{_slugify(entity_type)}.md"

    def _relationship_type_path(self, relationship_type: str) -> Path:
        return Path("reference") / "relationship-types" / f"{_slugify(relationship_type)}.md"

    def _manual_sidecar_path(self, relative_path: Path) -> Path:
        return Path("manual") / relative_path

    def _manual_sidecar_text(self, relative_path: Path) -> str | None:
        if self.output_dir is None:
            return None
        manual_path = self.output_dir / self._manual_sidecar_path(relative_path)
        try:
            if not manual_path.exists() or not manual_path.is_file():
                return None
            content = manual_path.read_text().strip()
        except OSError:
            return None
        return content or None

    def _with_generated_footer(self, relative_path: Path, content: str) -> str:
        manual_path = self._manual_sidecar_path(relative_path)
        manual_text = self._manual_sidecar_text(relative_path)
        manual_link = _relpath(relative_path, manual_path)
        lines = [content.rstrip()]
        if manual_text is not None:
            lines.extend(
                [
                    "",
                    "## Maintainer Notes",
                    f"_Source: [{manual_path.as_posix()}]({manual_link})_",
                    "",
                    manual_text,
                ]
            )
        lines.extend(
            [
                "",
                "---",
                "",
                (
                    "> This generated page may be overwritten. "
                    f"Durable maintainer notes live in `{manual_path.as_posix()}`."
                ),
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def _render_index_page(
        self,
        subjects: list[SubjectRef],
        *,
        scope: WikiScope,
        query_names: set[str],
        workflow_names: set[str],
        provider_names: set[str],
    ) -> str:
        lines = [f"# {self.config.name} Wiki", ""]
        if self.config.description:
            lines.extend([self.config.description.strip(), ""])
        lines.append("## World")
        lines.append(f"- Kind: {self.config.kind}")
        if self.head_snapshot_id:
            lines.append(f"- Head snapshot: {self.head_snapshot_id}")
        lines.append(f"- Rendered subjects: {len(subjects)}")
        lines.append(f"- Wiki scope: {scope}")
        if self.ownership.ownership_available:
            subject_set = set(subjects)
            local_count = sum(
                1
                for subject in subjects
                if self.ownership.is_local_entity_type(subject.entity_type)
            )
            upstream_count = sum(
                1
                for subject in subjects
                if self.ownership.is_upstream_entity_type(subject.entity_type)
            )
            omitted_upstream_count = sum(
                1
                for subject in self.subject_entities
                if (
                    subject not in subject_set
                    and self.ownership.is_upstream_entity_type(subject.entity_type)
                )
            )
            lines.append(f"- Rendered local subjects: {local_count}")
            lines.append(f"- Rendered upstream subjects: {upstream_count}")
            lines.append(f"- Omitted upstream subjects: {omitted_upstream_count}")
        lines.append("")

        lines.extend(self._render_index_summary(subjects))

        lines.append("## Subject Index")
        grouped: dict[str, list[SubjectRef]] = defaultdict(list)
        for subject in subjects:
            grouped[subject.entity_type].append(subject)
        for entity_type in sorted(grouped):
            lines.append(f"### {_humanize(entity_type)}")
            type_subjects = sorted(grouped[entity_type])
            for subject in type_subjects[: self.max_per_type]:
                entity = self.subject_entities[subject]
                subject_link = _relpath(Path("index.md"), self._subject_path(subject))
                lines.append(f"- [{_display_label(entity, self.config)}]({subject_link})")
            if len(type_subjects) > self.max_per_type:
                lines.append(f"- +{len(type_subjects) - self.max_per_type} more")
            lines.append("")

        lines.append("## Governance")
        lines.append("- [Pending review](governance/pending-review.md)")
        lines.append("- [Recent decisions](governance/recent-decisions.md)")
        lines.append("- [Recent outcomes](governance/recent-outcomes.md)")
        lines.append("")

        if query_names or workflow_names or provider_names:
            lines.append("## Reference")
            for query_name in sorted(query_names):
                query_link = _relpath(Path("index.md"), self._query_path(query_name))
                lines.append(f"- [Query: {query_name}]({query_link})")
            for workflow_name in sorted(workflow_names):
                workflow_link = _relpath(Path("index.md"), self._workflow_path(workflow_name))
                lines.append(f"- [Workflow: {workflow_name}]({workflow_link})")
            for provider_name in sorted(provider_names):
                provider_link = _relpath(Path("index.md"), self._provider_path(provider_name))
                lines.append(f"- [Provider: {provider_name}]({provider_link})")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _render_index_summary(self, subjects: list[SubjectRef]) -> list[str]:
        subject_set = set(subjects)
        by_entity_type = Counter(subject.entity_type for subject in subjects)
        relationship_counts: Counter[str] = Counter()
        seen_edges: set[tuple[str, str, str, str]] = set()
        connected_counts: list[tuple[int, SubjectRef]] = []
        review_counts: list[tuple[int, SubjectRef]] = []

        for subject in subjects:
            neighbors: set[SubjectRef] = set()
            for row in self.graph.get_neighbor_relationships(
                subject.entity_type,
                subject.entity_id,
                direction="both",
            ):
                neighbor = row.get("entity")
                if not isinstance(neighbor, EntityInstance):
                    continue
                neighbor_ref = SubjectRef(neighbor.entity_type, neighbor.entity_id)
                if neighbor_ref in subject_set:
                    neighbors.add(neighbor_ref)
                if row.get("direction") == "outgoing":
                    source_ref, target_ref = subject, neighbor_ref
                else:
                    source_ref, target_ref = neighbor_ref, subject
                relationship_type = str(row.get("relationship_type"))
                edge_key = str(row.get("edge_key", ""))
                edge_id = (source_ref.key, target_ref.key, relationship_type, edge_key)
                if edge_id not in seen_edges:
                    seen_edges.add(edge_id)
                    relationship_counts[relationship_type] += 1

            connected_counts.append((len(neighbors), subject))
            review_count = (
                len(self.feedback_by_subject.get(subject.key, []))
                + len(self.groups_by_subject.get(subject.key, []))
                + len(self._subject_outcomes(subject))
            )
            if review_count:
                review_counts.append((review_count, subject))

        lines = ["## Local World Summary"]
        lines.append("### Rendered Subjects")
        for entity_type, count in sorted(by_entity_type.items()):
            lines.append(f"- {_humanize(entity_type)}: {count}")
        lines.append("")

        if relationship_counts:
            lines.append("### Rendered Relationship Edges")
            for relationship_type, count in sorted(relationship_counts.items()):
                lines.append(f"- {_humanize(relationship_type)}: {count}")
            lines.append("")

        top_connected = [
            item for item in sorted(connected_counts, reverse=True) if item[0] > 0
        ][:5]
        if top_connected:
            lines.append("### Most Connected Subjects")
            for count, subject in top_connected:
                entity = self.subject_entities.get(subject)
                subject_link = _relpath(Path("index.md"), self._subject_path(subject))
                lines.append(
                    f"- [{_display_label(entity, self.config)}]({subject_link}): "
                    f"{count} linked subject(s)"
                )
            lines.append("")

        if review_counts:
            lines.append("### Subjects With Review Activity")
            for count, subject in sorted(review_counts, reverse=True)[:5]:
                entity = self.subject_entities.get(subject)
                subject_link = _relpath(Path("index.md"), self._subject_path(subject))
                lines.append(
                    f"- [{_display_label(entity, self.config)}]({subject_link}): "
                    f"{count} review record(s)"
                )
            lines.append("")
        return lines

    def _render_subject_page(
        self,
        subject: SubjectRef,
        *,
        rendered_subjects: set[SubjectRef],
    ) -> str:
        entity = self.subject_entities[subject]
        entity_schema = self.config.get_entity_type(subject.entity_type)
        receipts = self._subject_receipts(subject)
        feedback_records = sorted(
            self.feedback_by_subject.get(subject.key, []),
            key=lambda record: record.created_at,
            reverse=True,
        )
        groups = sorted(
            self.groups_by_subject.get(subject.key, []),
            key=lambda group: group.created_at,
            reverse=True,
        )
        outcomes = self._subject_outcomes(subject)

        lines = [f"# {subject.entity_id}", ""]
        lines.extend(self._render_record_section(subject, entity, entity_schema))
        lines.extend(self._render_ego_graph_section(subject))
        lines.extend(
            self._render_subject_at_a_glance(
                subject,
                receipts=receipts,
                feedback_records=feedback_records,
                groups=groups,
                outcomes=outcomes,
            )
        )
        lines.extend(self._render_world_state_section(subject, rendered_subjects))
        lines.extend(self._render_production_section(subject, receipts, rendered_subjects))
        lines.extend(self._render_pending_review_section(subject, groups, rendered_subjects))
        lines.extend(
            self._render_review_history_section(
                subject, feedback_records, groups, rendered_subjects
            )
        )
        lines.extend(self._render_outcome_history_section(outcomes))
        lines.extend(
            self._render_full_evidence_section(self._subject_path(subject), receipts)
        )
        return "\n".join(lines).rstrip() + "\n"

    def _render_subject_at_a_glance(
        self,
        subject: SubjectRef,
        *,
        receipts: list[Receipt],
        feedback_records: list[FeedbackRecord],
        groups: list[CandidateGroup],
        outcomes: list[OutcomeRecord],
    ) -> list[str]:
        inspect_rows = self.graph.get_neighbor_relationships(
            subject.entity_type,
            subject.entity_id,
            direction="both",
        )
        by_entity_type: Counter[str] = Counter()
        deterministic_count = 0
        governed_count = 0
        seen_neighbors: set[SubjectRef] = set()
        for row in inspect_rows:
            neighbor = row.get("entity")
            if not isinstance(neighbor, EntityInstance):
                continue
            neighbor_ref = SubjectRef(neighbor.entity_type, neighbor.entity_id)
            if neighbor_ref not in seen_neighbors:
                seen_neighbors.add(neighbor_ref)
                by_entity_type[neighbor.entity_type] += 1
            relationship = self.relationships_by_name.get(str(row.get("relationship_type")))
            if relationship is not None and relationship.proposal_policy is not None:
                governed_count += 1
            else:
                deterministic_count += 1

        review_count = len(feedback_records) + len(groups) + len(outcomes)
        lines = ["## At a Glance"]
        lines.append(f"- Ownership: {self._subject_ownership_label(subject)}")
        if by_entity_type:
            context = ", ".join(
                f"{_humanize(entity_type)}: {count}"
                for entity_type, count in sorted(by_entity_type.items())
            )
            lines.append(f"- Linked context: {context}")
        else:
            lines.append("- Linked context: none")
        lines.append(f"- Deterministic relationships: {deterministic_count}")
        lines.append(f"- Governed relationships: {governed_count}")
        lines.append(f"- Evidence receipts: {len(receipts)}")
        lines.append(f"- Review activity records: {review_count}")
        lines.append("")
        return lines

    def _subject_ownership_label(self, subject: SubjectRef) -> str:
        if self.ownership.is_local_entity_type(subject.entity_type):
            return "local"
        if self.ownership.is_upstream_entity_type(subject.entity_type):
            return "upstream/reference"
        return "context unavailable"

    def _render_record_section(
        self,
        subject: SubjectRef,
        entity: EntityInstance,
        entity_schema: EntityTypeSchema | None,
    ) -> list[str]:
        lines = [
            "## Info",
            f"- Type: {_humanize(subject.entity_type)}",
            f"- ID: {subject.entity_id}",
        ]
        if entity_schema and entity_schema.description:
            lines.append(f"- Description: {entity_schema.description.strip()}")
        for key, value in _sorted_properties(entity.properties).items():
            lines.append(f"- {key}: {_render_scalar(value)}")
        lines.append("")
        return lines

    def _render_world_state_section(
        self,
        subject: SubjectRef,
        rendered_subjects: set[SubjectRef],
    ) -> list[str]:
        inspect_rows = self.graph.get_neighbor_relationships(
            subject.entity_type,
            subject.entity_id,
            direction="both",
        )
        if not inspect_rows:
            return ["## Current World State", "- No linked records found.", ""]

        groups: dict[
            str,
            dict[SubjectRef, tuple[EntityInstance, list[_NeighborRelationship]]],
        ] = defaultdict(dict)
        for row in inspect_rows:
            neighbor = row.get("entity")
            if not isinstance(neighbor, EntityInstance):
                continue
            relationship_type = str(row.get("relationship_type"))
            relationship_schema = self.relationships_by_name.get(relationship_type)
            neighbor_ref = SubjectRef(neighbor.entity_type, neighbor.entity_id)
            _entity, relationships = groups[neighbor.entity_type].setdefault(
                neighbor_ref,
                (neighbor, []),
            )
            relationships.append(
                _NeighborRelationship(
                    relationship_type=relationship_type,
                    relationship_schema=relationship_schema,
                    properties=dict(row.get("properties", {})),
                )
            )

        lines = ["## Current World State"]
        for entity_type in sorted(groups):
            items = sorted(
                groups[entity_type].values(),
                key=lambda item: _display_label(item[0], self.config),
            )
            visible_items = items[: self.max_per_type]
            other_count = len(items) - len(visible_items)

            lines.append(f"### {_humanize(entity_type)}")
            for neighbor, relationships in visible_items:
                link = self._subject_markdown_link(
                    SubjectRef(neighbor.entity_type, neighbor.entity_id),
                    current_path=self._subject_path(subject),
                    rendered_subjects=rendered_subjects,
                )
                lines.extend(_render_neighbor_state_item(link, relationships))
            if other_count:
                lines.append(f"- +{other_count} more linked record(s)")
            lines.append("")
        return lines

    def _render_ego_graph_section(self, subject: SubjectRef) -> list[str]:
        diagram = self._render_ego_graph_mermaid(subject)
        if not diagram:
            return []
        lines = ["## Graph Position", "```mermaid", diagram, "```", ""]
        lines.extend(self._render_ego_graph_legend())
        lines.append("")
        return lines

    def _render_ego_graph_legend(self) -> list[str]:
        return render_mermaid_inline_legend(
            (
                MermaidLegendItem("Blue rounded node", "Current page subject."),
                MermaidLegendItem("Green rectangle", "Local world-model neighbor."),
                MermaidLegendItem("Amber double rectangle", "Upstream/reference neighbor."),
                MermaidLegendItem("Gray rectangle", "Neighbor with unavailable ownership."),
                MermaidLegendItem("Solid blue labeled edge", "Deterministic relationship."),
                MermaidLegendItem("Dashed red labeled edge", "Governed relationship."),
            )
        )

    def _render_ego_graph_mermaid(self, subject: SubjectRef) -> str:
        inspect_rows = self.graph.get_neighbor_relationships(
            subject.entity_type,
            subject.entity_id,
            direction="both",
        )
        if not inspect_rows:
            return ""

        subject_entity = self.subject_entities.get(subject)
        subject_node = _subject_mermaid_id(subject)
        subject_label = escape_mermaid_label(_display_label(subject_entity, self.config))
        lines = [
            "flowchart LR",
            "  classDef focus fill:#1f6feb,stroke:#0b3d91,color:#fff",
            "  classDef localNeighbor fill:#dcffe4,stroke:#2da44e,color:#1f2328",
            "  classDef upstreamNeighbor fill:#fff8c5,stroke:#9a6700,color:#1f2328",
            "  classDef contextNeighbor fill:#f6f8fa,stroke:#8c959f,color:#24292f",
        ]
        local_neighbor_nodes: set[str] = set()
        upstream_neighbor_nodes: set[str] = set()
        context_neighbor_nodes: set[str] = set()
        neighbor_lines_by_type: dict[str, dict[str, str]] = defaultdict(dict)
        edge_labels: dict[tuple[str, str, bool], list[str]] = {}
        deterministic_edge_indexes: list[int] = []
        governed_edge_indexes: list[int] = []

        for row in inspect_rows[: self.max_per_type]:
            neighbor = row.get("entity")
            if not isinstance(neighbor, EntityInstance):
                continue
            neighbor_ref = SubjectRef(neighbor.entity_type, neighbor.entity_id)
            neighbor_node = _subject_mermaid_id(neighbor_ref)
            neighbor_role = self._ego_neighbor_role(neighbor_ref)
            if neighbor_role == "local":
                local_neighbor_nodes.add(neighbor_node)
            elif neighbor_role == "upstream":
                upstream_neighbor_nodes.add(neighbor_node)
            else:
                context_neighbor_nodes.add(neighbor_node)
            if neighbor_node not in neighbor_lines_by_type[neighbor.entity_type]:
                neighbor_label = escape_mermaid_label(_display_label(neighbor, self.config))
                neighbor_lines_by_type[neighbor.entity_type][neighbor_node] = _mermaid_node_line(
                    neighbor_node,
                    neighbor_label,
                    role=neighbor_role,
                )
            relationship_type = str(row.get("relationship_type"))
            relationship_schema = self.relationships_by_name.get(relationship_type)
            label = escape_mermaid_label(_humanize(relationship_type))
            governed = (
                relationship_schema is not None
                and relationship_schema.proposal_policy is not None
            )
            if row.get("direction") == "outgoing":
                source_node, target_node = subject_node, neighbor_node
            else:
                source_node, target_node = neighbor_node, subject_node
            labels = edge_labels.setdefault((source_node, target_node, governed), [])
            if label not in labels:
                labels.append(label)

        lines.extend(
            _mermaid_subgraph(
                "focus_context",
                "Current Page",
                [_mermaid_node_line(subject_node, subject_label, role="focus")],
            )
        )
        for entity_type in sorted(neighbor_lines_by_type, key=_humanize):
            type_nodes = neighbor_lines_by_type[entity_type]
            lines.extend(
                _mermaid_subgraph(
                    f"type_{mermaid_id(entity_type)}",
                    f"{_humanize(entity_type)} ({len(type_nodes)})",
                    [
                        type_nodes[node]
                        for node in sorted(type_nodes)
                    ],
                )
            )

        edge_index = 0
        for (source_node, target_node, governed), labels in edge_labels.items():
            label = _format_mermaid_edge_label(labels)
            if governed:
                lines.append(f'  {source_node} -. "{label}" .-> {target_node}')
                governed_edge_indexes.append(edge_index)
            else:
                lines.append(f'  {source_node} -- "{label}" --> {target_node}')
                deterministic_edge_indexes.append(edge_index)
            edge_index += 1

        lines.append(f"  class {subject_node} focus")
        if local_neighbor_nodes:
            lines.append(f"  class {','.join(sorted(local_neighbor_nodes))} localNeighbor")
        if upstream_neighbor_nodes:
            lines.append(
                f"  class {','.join(sorted(upstream_neighbor_nodes))} upstreamNeighbor"
            )
        if context_neighbor_nodes:
            lines.append(
                f"  class {','.join(sorted(context_neighbor_nodes))} contextNeighbor"
            )
        if deterministic_edge_indexes:
            lines.append(
                f"  linkStyle {_format_mermaid_edge_indexes(deterministic_edge_indexes)} "
                "stroke:#2c5f8a,stroke-width:2px"
            )
        if governed_edge_indexes:
            lines.append(
                f"  linkStyle {_format_mermaid_edge_indexes(governed_edge_indexes)} "
                "stroke:#e74c3c,stroke-width:2px,stroke-dasharray:4 3"
            )
        return "\n".join(lines)

    def _ego_neighbor_role(self, subject: SubjectRef) -> MermaidNodeRole:
        if self.ownership.is_local_entity_type(subject.entity_type):
            return "local"
        if self.ownership.is_upstream_entity_type(subject.entity_type):
            return "upstream"
        return "context"

    def _render_production_section(
        self,
        subject: SubjectRef,
        receipts: list[Receipt],
        rendered_subjects: set[SubjectRef],
    ) -> list[str]:
        if not receipts:
            return ["## How This Was Produced", "- No receipts mention this subject yet.", ""]

        lines = ["## How This Was Produced"]
        workflow_receipts = [
            receipt for receipt in receipts if receipt.operation_type == "workflow"
        ]
        query_receipts = [receipt for receipt in receipts if receipt.operation_type == "query"]

        for receipt in workflow_receipts:
            workflow_ref = self._workflow_markdown_link(
                receipt.query_name,
                current_path=self._subject_path(subject),
            )
            lines.append(f"### Workflow: {workflow_ref}")
            workflow_schema = self.config.workflows.get(receipt.query_name)
            if workflow_schema and workflow_schema.description:
                lines.append(f"- {workflow_schema.description.strip()}")
            receipt_link = _relpath(
                self._subject_path(subject),
                self._receipt_path(receipt.receipt_id),
            )
            lines.append(f"- Receipt: [{receipt.receipt_id}]({receipt_link})")
            provider_nodes = [
                node
                for node in receipt.nodes
                if node.node_type == "plan_step" and node.detail.get("kind") == "provider"
            ]
            if provider_nodes:
                for node in provider_nodes:
                    provider_name = str(node.detail.get("provider_name", "")).strip()
                    trace_id = str(node.detail.get("trace_id", "")).strip()
                    provider_href = _relpath(
                        self._subject_path(subject),
                        self._provider_path(provider_name),
                    )
                    provider_ref = (
                        f"[{provider_name}]({provider_href})"
                        if provider_name in self.config.providers
                        else provider_name or "unknown provider"
                    )
                    line = f"- Provider: {provider_ref}"
                    if trace_id and trace_id in self.traces:
                        trace = self.traces[trace_id]
                        if trace.provider_version:
                            line += f" (version {trace.provider_version})"
                        if trace.artifact_name:
                            line += f"; artifact {trace.artifact_name}"
                    lines.append(line)
            else:
                lines.append("- No provider steps recorded on this receipt.")
            lines.append("")

        for receipt in query_receipts:
            query_ref = self._query_markdown_link(
                receipt.query_name,
                current_path=self._subject_path(subject),
            )
            lines.append(f"### Query: {query_ref}")
            query_schema = self.config.named_queries.get(receipt.query_name)
            if query_schema and query_schema.description:
                lines.append(f"- {query_schema.description.strip()}")
            receipt_link = _relpath(
                self._subject_path(subject),
                self._receipt_path(receipt.receipt_id),
            )
            lines.append(f"- Receipt: [{receipt.receipt_id}]({receipt_link})")
            result_refs = _entity_refs_from_results(receipt.results)
            if result_refs:
                lines.append("- Records returned:")
                for ref in sorted(result_refs)[:10]:
                    result_link = self._subject_markdown_link(
                        ref,
                        current_path=self._subject_path(subject),
                        rendered_subjects=rendered_subjects,
                    )
                    lines.append(f"  - {result_link}")
            lines.append("")

        return lines

    def _render_pending_review_section(
        self,
        subject: SubjectRef,
        groups: list[CandidateGroup],
        rendered_subjects: set[SubjectRef],
    ) -> list[str]:
        seen_pending_ids: set[str] = set()
        pending_groups: list[CandidateGroup] = []
        for pending_group in groups:
            if (
                pending_group.status == "pending_review"
                and pending_group.group_id not in seen_pending_ids
            ):
                seen_pending_ids.add(pending_group.group_id)
                pending_groups.append(pending_group)
        if not pending_groups:
            return []

        lines = ["## Pending Review"]
        pending_lines: list[str] = []
        for pending_group in pending_groups:
            members = self.members_by_group.get(pending_group.group_id, [])
            relevant_members = [
                member
                for member in members
                if subject.key in (
                    f"{member.from_type}:{member.from_id}",
                    f"{member.to_type}:{member.to_id}",
                )
            ]
            if members and not relevant_members:
                continue
            if relevant_members:
                sorted_members = sorted(
                    relevant_members,
                    key=lambda m: (m.from_id, m.to_id),
                )
                for member in sorted_members[: self.max_per_type]:
                    from_ref = SubjectRef(member.from_type, member.from_id)
                    to_ref = SubjectRef(member.to_type, member.to_id)
                    from_link = self._subject_markdown_link(
                        from_ref,
                        current_path=self._subject_path(subject),
                        rendered_subjects=rendered_subjects,
                    )
                    to_link = self._subject_markdown_link(
                        to_ref,
                        current_path=self._subject_path(subject),
                        rendered_subjects=rendered_subjects,
                    )
                    line = f"- {from_link} →({pending_group.relationship_type}) {to_link}"
                    if pending_group.thesis_text:
                        line += f" — {pending_group.thesis_text.strip()}"
                    pending_lines.append(line)
                if len(sorted_members) > self.max_per_type:
                    pending_lines.append(
                        f"- +{len(sorted_members) - self.max_per_type} more member(s)"
                    )
            else:
                line = f"- {pending_group.relationship_type}"
                if pending_group.thesis_text:
                    line += f" — {pending_group.thesis_text.strip()}"
                pending_lines.append(line)
            if pending_group.source_workflow_name:
                wf_link = _relpath(
                    self._subject_path(subject),
                    self._workflow_path(pending_group.source_workflow_name),
                )
                pending_lines.append(
                    f"  Source: [{pending_group.source_workflow_name}]({wf_link})"
                )
        if not pending_lines:
            return []
        lines.extend(pending_lines)
        lines.append("")
        return lines

    def _render_review_history_section(
        self,
        subject: SubjectRef,
        feedback_records: list[FeedbackRecord],
        groups: list[CandidateGroup],
        rendered_subjects: set[SubjectRef],
    ) -> list[str]:
        lines = ["## Review History"]
        emitted = False

        seen_resolution_ids: set[str] = set()
        resolved_groups: list[CandidateGroup] = []
        for group in groups:
            if group.resolution_id and group.resolution_id not in seen_resolution_ids:
                seen_resolution_ids.add(group.resolution_id)
                resolved_groups.append(group)
        if resolved_groups:
            lines.append("### Resolutions")
            emitted = True
            for group in resolved_groups:
                resolution = self.resolutions_by_id.get(group.resolution_id or "")
                if resolution is None:
                    continue
                line = f"- {resolution.action} on {group.relationship_type}"
                if resolution.trust_status:
                    line += f" (trust: {resolution.trust_status})"
                if resolution.rationale:
                    line += f" — {resolution.rationale}"
                if group.source_workflow_name:
                    line += (
                        " [source: "
                        + self._workflow_markdown_link(
                            group.source_workflow_name,
                            current_path=self._subject_path(subject),
                        )
                        + "]"
                    )
                members = self.members_by_group.get(group.group_id, [])
                if members:
                    member_labels = []
                    for member in members[:3]:
                        counterpart = _counterpart_for_subject(subject, member)
                        if counterpart is not None:
                            member_labels.append(
                                self._subject_markdown_link(
                                    counterpart,
                                    current_path=self._subject_path(subject),
                                    rendered_subjects=rendered_subjects,
                                )
                            )
                    if member_labels:
                        line += f" [subjects: {', '.join(member_labels)}]"
                lines.append(line)
            lines.append("")

        if feedback_records:
            lines.append("### Feedback")
            emitted = True
            for record in feedback_records:
                counterpart = SubjectRef(record.target.to_type, record.target.to_id)
                if counterpart == subject:
                    counterpart = SubjectRef(record.target.from_type, record.target.from_id)
                counterpart_label = self._subject_markdown_link(
                    counterpart,
                    current_path=self._subject_path(subject),
                    rendered_subjects=rendered_subjects,
                )
                line = f"- {record.action} involving {counterpart_label}"
                if record.reason_code:
                    line += f" (code: {record.reason_code})"
                if record.reason:
                    line += f" — {record.reason}"
                lines.append(line)
            lines.append("")

        if not emitted:
            lines.append("- No feedback, candidate groups, or resolutions recorded yet.")
            lines.append("")
        return lines

    def _render_outcome_history_section(self, outcomes: list[OutcomeRecord]) -> list[str]:
        lines = ["## Outcome History"]
        if not outcomes:
            lines.append("- No outcome records linked yet.")
            lines.append("")
            return lines

        receipt_outcomes = [outcome for outcome in outcomes if outcome.anchor_type == "receipt"]
        resolution_outcomes = [
            outcome for outcome in outcomes if outcome.anchor_type == "resolution"
        ]

        if receipt_outcomes:
            lines.append("### Receipt-Anchored Outcomes")
            for outcome in receipt_outcomes:
                line = f"- {outcome.outcome}"
                surface_name = str(outcome.decision_context.get("surface_name", "")).strip()
                if surface_name:
                    line += f" on {surface_name}"
                if outcome.outcome_code:
                    line += f" (code: {outcome.outcome_code})"
                provider_names = _extract_provider_names_from_lineage(outcome.lineage_snapshot)
                if provider_names:
                    line += f" [providers: {', '.join(provider_names)}]"
                lines.append(line)
            lines.append("")

        if resolution_outcomes:
            lines.append("### Resolution-Anchored Outcomes")
            for outcome in resolution_outcomes:
                line = f"- {outcome.outcome}"
                if outcome.relationship_type:
                    line += f" on {outcome.relationship_type}"
                if outcome.outcome_code:
                    line += f" (code: {outcome.outcome_code})"
                provider_names = _extract_provider_names_from_lineage(outcome.lineage_snapshot)
                if provider_names:
                    line += f" [providers: {', '.join(provider_names)}]"
                lines.append(line)
            lines.append("")

        return lines

    def _render_full_evidence_section(
        self,
        current_path: Path,
        receipts: list[Receipt],
    ) -> list[str]:
        lines = ["## Full Evidence"]
        if receipts:
            for receipt in receipts:
                receipt_link = _relpath(current_path, self._receipt_path(receipt.receipt_id))
                lines.append(f"- [{receipt.receipt_id}]({receipt_link})")
            lines.append("")
        else:
            lines.append("- No evidence pages linked yet.")
            lines.append("")
        return lines

    def _render_receipt_page(
        self,
        receipt: Receipt,
        *,
        rendered_subjects: set[SubjectRef],
    ) -> str:
        operation_label = (
            "Query"
            if receipt.operation_type == "query"
            else "Workflow"
            if receipt.operation_type == "workflow"
            else "Operation"
        )
        current_path = self._receipt_path(receipt.receipt_id)
        operation_name = receipt.query_name or receipt.operation_type
        if receipt.operation_type == "workflow":
            operation_ref = self._workflow_markdown_link(
                operation_name,
                current_path=current_path,
            )
        elif receipt.operation_type == "query":
            operation_ref = self._query_markdown_link(
                operation_name,
                current_path=current_path,
            )
        else:
            operation_ref = operation_name
        lines = [f"# Receipt {receipt.receipt_id}", "", "## Summary"]
        lines.append(f"- {operation_label}: {operation_ref}")
        lines.append(f"- Created at: {format_datetime(receipt.created_at)}")
        lines.append(f"- Duration: {receipt.duration_ms}ms")
        lines.append(
            f"- Parameters: `{json.dumps(receipt.parameters, sort_keys=True, default=str)}`"
        )
        lines.append("")

        entity_refs = _entity_refs_from_receipt(receipt)
        if entity_refs:
            by_type: dict[str, int] = defaultdict(int)
            for ref in entity_refs:
                by_type[ref.entity_type] += 1
            lines.append("## Scope")
            for etype in sorted(by_type):
                lines.append(f"- {_humanize(etype)}: {by_type[etype]}")
            lines.append("")

        checks = self._render_receipt_checks(receipt)
        if checks:
            lines.append("## Checks Applied")
            lines.extend(checks)
            lines.append("")

        steps = self._render_receipt_steps(receipt)
        if steps:
            lines.append("## Recorded Execution Steps")
            lines.extend(steps)
            lines.append("")

        changes = self._render_receipt_changes(receipt)
        if changes:
            lines.append("## Changes Recorded")
            lines.extend(changes)
            lines.append("")

        lines.extend(self._render_receipt_results(receipt, rendered_subjects))
        return "\n".join(lines).rstrip() + "\n"

    def _render_receipt_checks(self, receipt: Receipt) -> list[str]:
        lines: list[str] = []
        for node in receipt.nodes:
            if node.node_type == "filter_applied":
                status = "passed" if node.detail.get("passed") else "failed"
                filter_json = json.dumps(node.detail.get("filter", {}), sort_keys=True)
                lines.append(f"- Filter {status}: `{filter_json}`")
            elif node.node_type == "constraint_check":
                status = "passed" if node.detail.get("passed") else "failed"
                lines.append(f"- Constraint {status}: {node.detail.get('constraint', '')}")
            elif node.node_type == "validation":
                status = "passed" if node.detail.get("passed") else "failed"
                message = str(node.detail.get("message", "")).strip()
                line = f"- Validation {status}"
                if message:
                    line += f": {message}"
                lines.append(line)
        return lines

    def _render_receipt_steps(self, receipt: Receipt) -> list[str]:
        lines: list[str] = []
        for node in receipt.nodes:
            if node.node_type != "plan_step":
                continue
            step_id = str(node.detail.get("step_id", "")).strip() or node.node_id
            kind = str(node.detail.get("kind", "")).strip() or "step"
            line = f"- {step_id}: {kind}"
            provider_name = str(node.detail.get("provider_name", "")).strip()
            if provider_name:
                line += f" ({provider_name})"
            lines.append(line)
        return lines

    def _render_receipt_changes(self, receipt: Receipt) -> list[str]:
        entity_adds: dict[str, int] = defaultdict(int)
        entity_updates: dict[str, int] = defaultdict(int)
        rel_adds: dict[str, int] = defaultdict(int)
        rel_updates: dict[str, int] = defaultdict(int)
        other_lines: list[str] = []

        for node in receipt.nodes:
            if node.node_type == "entity_write":
                etype = node.entity_type or "unknown"
                if node.detail.get("is_update"):
                    entity_updates[etype] += 1
                else:
                    entity_adds[etype] += 1
            elif node.node_type == "relationship_write":
                rtype = str(node.detail.get("relationship", "unknown"))
                if node.detail.get("is_update"):
                    rel_updates[rtype] += 1
                else:
                    rel_adds[rtype] += 1
            elif node.node_type == "feedback_applied":
                status = "applied" if node.detail.get("applied") else "not applied"
                other_lines.append(f"- Feedback {status}: {node.detail.get('action', '')}")

        lines: list[str] = []
        for etype in sorted(entity_adds):
            lines.append(f"- {_humanize(etype)} added: {entity_adds[etype]}")
        for etype in sorted(entity_updates):
            lines.append(f"- {_humanize(etype)} updated: {entity_updates[etype]}")
        for rtype in sorted(rel_adds):
            lines.append(f"- {rtype} links added: {rel_adds[rtype]}")
        for rtype in sorted(rel_updates):
            lines.append(f"- {rtype} links updated: {rel_updates[rtype]}")
        lines.extend(other_lines)
        return lines

    def _render_receipt_results(
        self,
        receipt: Receipt,
        rendered_subjects: set[SubjectRef],
    ) -> list[str]:
        lines = ["## Results"]
        if not receipt.results:
            lines.append("- No results recorded.")
            lines.append("")
            return lines

        entity_refs = _entity_refs_from_results(receipt.results)
        if entity_refs:
            by_type: dict[str, int] = defaultdict(int)
            for ref in entity_refs:
                by_type[ref.entity_type] += 1
            for etype in sorted(by_type):
                lines.append(f"- {_humanize(etype)}: {by_type[etype]}")
            lines.append("")
            return lines

        # Non-entity results: show compact JSON (capped to avoid bloat).
        result_json = json.dumps(receipt.results, indent=2, sort_keys=True, default=str)
        result_lines = result_json.splitlines()
        max_lines = 60
        lines.append("```json")
        lines.extend(result_lines[:max_lines])
        if len(result_lines) > max_lines:
            lines.append(f"  ... ({len(result_lines) - max_lines} more lines)")
        lines.append("```")
        lines.append("")
        return lines

    def _render_query_page(self, query_name: str, schema: NamedQuerySchema) -> str:
        lines = [f"# Reference: {query_name}", ""]
        if schema.description:
            lines.extend([schema.description.strip(), ""])
        lines.append("## Summary")
        lines.append(f"- Starting record type: {_humanize(schema.entry_point)}")
        lines.append(f"- Produces: {schema.returns}")
        lines.append("")

        lines.append("## Query Steps")
        for index, step in enumerate(schema.traversal, start=1):
            relationship_labels = []
            for relationship_name in step.relationship_types:
                relationship_schema = self.relationships_by_name.get(relationship_name)
                if relationship_schema and relationship_schema.description:
                    relationship_labels.append(
                        f"{relationship_name} ({relationship_schema.description.strip()})"
                    )
                else:
                    relationship_labels.append(relationship_name)
            relationships = ", ".join(relationship_labels)
            lines.append(
                f"- Step {index}: look for related records using {relationships} "
                f"(direction: {step.direction}, depth: {step.max_depth})"
            )
            if step.filter:
                lines.append(f"  Checks: `{json.dumps(step.filter, sort_keys=True, default=str)}`")
            if step.constraint:
                lines.append(f"  Constraint: {step.constraint}")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_workflow_page(
        self,
        workflow_name: str,
        schema: WorkflowSchema,
        *,
        rendered_receipt_ids: set[str],
    ) -> str:
        current_path = self._workflow_path(workflow_name)
        lines = [f"# Workflow: {_humanize(workflow_name)}", ""]
        if schema.description:
            lines.extend([schema.description.strip(), ""])

        lines.append("## Role")
        lines.append(f"- {self._workflow_role(schema)}")
        lines.append("")

        lines.append("## Input Context")
        lines.extend(self._workflow_input_context_lines(schema, current_path))
        lines.append("")

        lines.append("## Result")
        lines.extend(self._workflow_result_lines(schema))
        lines.append("")

        provider_sources = self._workflow_provider_source_lines(schema, current_path)
        if provider_sources:
            lines.append("## Provider Sources")
            lines.extend(provider_sources)
            lines.append("")

        review_surface = self._workflow_review_surface_lines(schema)
        if review_surface:
            lines.append("## Review Surface")
            lines.extend(review_surface)
            lines.append("")

        recent_executions = self._workflow_recent_execution_lines(
            workflow_name,
            current_path,
            rendered_receipt_ids=rendered_receipt_ids,
        )
        if recent_executions:
            lines.append("## Recent Executions")
            lines.extend(recent_executions)
            lines.append("")

        lines.append("## Configured Step Details")
        if self._workflow_has_apply_steps(schema):
            lines.append(
                "Apply steps commit previously built records or links into the world "
                "model. In preview mode they report what would change; in apply mode "
                "they persist those changes."
            )
            lines.append("")
        rows: list[tuple[str, ...]] = [
            (
                step.id,
                self._workflow_step_action(step, current_path),
                self._workflow_step_reads(step, current_path),
                self._workflow_step_produces(step),
            )
            for step in schema.steps
        ]
        lines.append(_markdown_table(("Step", "Action", "Reads/Uses", "Produces"), rows))
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _workflow_has_apply_steps(schema: WorkflowSchema) -> bool:
        return any(
            step.apply_entities is not None
            or step.apply_relationships is not None
            or step.apply_all is not None
            for step in schema.steps
        )

    def _workflow_role(self, schema: WorkflowSchema) -> str:
        if any(step.propose_relationship_group is not None for step in schema.steps):
            return "Governed proposal"
        if schema.type == "canonical" or self._workflow_has_apply_steps(schema):
            return "Canonical state write"
        return "Read or compute workflow"

    def _workflow_input_context_lines(
        self,
        schema: WorkflowSchema,
        current_path: Path,
    ) -> list[str]:
        lines: list[str] = []
        contract = resolve_contract(self.config, schema.contract_in)
        if contract is not None and contract.fields:
            field_names = ", ".join(f"`{field}`" for field in sorted(contract.fields))
            lines.append(f"- Workflow input fields: {field_names}")
        else:
            lines.append("- Workflow input fields: none")

        entity_types = {
            step.list_entities.entity_type
            for step in schema.steps
            if step.list_entities is not None
        }
        relationship_types = {
            step.list_relationships.relationship_type
            for step in schema.steps
            if step.list_relationships is not None
        }
        query_names = {step.query for step in schema.steps if step.query is not None}
        if entity_types:
            lines.append(
                "- Entity context: "
                + ", ".join(_humanize(entity_type) for entity_type in sorted(entity_types))
            )
        if relationship_types:
            lines.append(
                "- Relationship context: "
                + ", ".join(
                    _humanize(relationship_type)
                    for relationship_type in sorted(relationship_types)
                )
            )
        if query_names:
            query_links = [
                self._query_markdown_link(query_name, current_path=current_path)
                for query_name in sorted(query_names)
            ]
            lines.append(f"- Query context: {', '.join(query_links)}")
        if len(lines) == 1 and schema.type != "canonical":
            lines.append("- Graph context: none configured")
        elif len(lines) == 1 and schema.type == "canonical":
            lines.append("- Graph context: none; this workflow seeds canonical state")
        return lines

    def _workflow_result_lines(self, schema: WorkflowSchema) -> list[str]:
        entity_outputs: dict[str, str] = {}
        relationship_outputs: dict[str, str] = {}
        proposed_relationships: set[str] = set()

        for step in schema.steps:
            if step.make_entities is not None and step.as_ is not None:
                entity_outputs[step.as_] = step.make_entities.entity_type
            if step.make_relationships is not None and step.as_ is not None:
                relationship_outputs[step.as_] = step.make_relationships.relationship_type
            if step.make_candidates is not None and step.as_ is not None:
                relationship_outputs[step.as_] = step.make_candidates.relationship_type
            if step.propose_relationship_group is not None:
                proposed_relationships.add(
                    step.propose_relationship_group.relationship_type
                )

        applied_entities = {
            entity_outputs[step.apply_entities.entities_from]
            for step in schema.steps
            if (
                step.apply_entities is not None
                and step.apply_entities.entities_from in entity_outputs
            )
        }
        applied_relationships = {
            relationship_outputs[step.apply_relationships.relationships_from]
            for step in schema.steps
            if (
                step.apply_relationships is not None
                and step.apply_relationships.relationships_from in relationship_outputs
            )
        }
        for step in schema.steps:
            if step.apply_all is None:
                continue
            applied_entities.update(
                entity_outputs[alias]
                for alias in step.apply_all.entities_from
                if alias in entity_outputs
            )
            applied_relationships.update(
                relationship_outputs[alias]
                for alias in step.apply_all.relationships_from
                if alias in relationship_outputs
            )

        lines: list[str] = []
        if applied_entities:
            lines.append(
                "- Canonical entities: "
                + ", ".join(_humanize(entity_type) for entity_type in sorted(applied_entities))
            )
        if applied_relationships:
            lines.append(
                "- Canonical relationships: "
                + ", ".join(
                    _humanize(relationship_type)
                    for relationship_type in sorted(applied_relationships)
                )
            )
        if proposed_relationships:
            lines.append(
                "- Proposed relationships: "
                + ", ".join(
                    _humanize(relationship_type)
                    for relationship_type in sorted(proposed_relationships)
                )
            )
        if not lines:
            returns_step = next(
                (step for step in schema.steps if step.id == schema.returns),
                None,
            )
            if returns_step is not None and returns_step.provider is not None:
                lines.append(f"- Provider result from `{returns_step.provider}`")
            else:
                lines.append(f"- Result of step `{schema.returns}`")
        return lines

    def _workflow_provider_source_lines(
        self,
        schema: WorkflowSchema,
        current_path: Path,
    ) -> list[str]:
        provider_names = []
        for step in schema.steps:
            if step.provider is not None and step.provider not in provider_names:
                provider_names.append(step.provider)
        lines: list[str] = []
        for provider_name in provider_names:
            provider = self.config.providers.get(provider_name)
            provider_label = self._provider_markdown_link(
                provider_name,
                current_path=current_path,
            )
            if provider is None:
                lines.append(f"- {provider_label}")
                continue
            line = (
                f"- {provider_label} ({_humanize(provider.kind)}, v{provider.version}); "
                f"source: `{provider.ref}`"
            )
            if provider.artifact:
                line += f"; artifact: {_humanize(provider.artifact)}"
            lines.append(line)
        return lines

    def _workflow_review_surface_lines(self, schema: WorkflowSchema) -> list[str]:
        relationship_types = sorted(
            {
                step.propose_relationship_group.relationship_type
                for step in schema.steps
                if step.propose_relationship_group is not None
            }
        )
        lines: list[str] = []
        for relationship_type in relationship_types:
            relationship = self.relationships_by_name.get(relationship_type)
            lines.append(f"- Relationship: {_humanize(relationship_type)}")
            if relationship is not None:
                lines.append(
                    f"  - Scope: {_humanize(relationship.from_entity)} -> "
                    f"{_humanize(relationship.to_entity)}"
                )
                if relationship.proposal_policy is not None:
                    signal_sources = sorted(relationship.proposal_policy.signals)
                    if signal_sources:
                        lines.append(
                            "  - Signals: "
                            + ", ".join(_humanize(source) for source in signal_sources)
                        )
                    lines.append(
                        "  - Auto-resolve: "
                        f"{_humanize(relationship.proposal_policy.auto_resolve_when)}; "
                        "prior trust: "
                        f"{_humanize(relationship.proposal_policy.auto_resolve_requires_prior_trust)}"
                    )
            feedback_profile = self.config.get_feedback_profile(relationship_type)
            if feedback_profile is not None:
                lines.append(
                    f"  - Feedback reason codes: {len(feedback_profile.reason_codes)}"
                )
            outcome_profiles = [
                (name, profile)
                for name, profile in self.config.outcome_profiles.items()
                if profile.relationship_type == relationship_type
            ]
            if outcome_profiles:
                names = ", ".join(_humanize(name) for name, _profile in outcome_profiles)
                lines.append(f"  - Outcome profiles: {names}")
        return lines

    def _workflow_recent_execution_lines(
        self,
        workflow_name: str,
        current_path: Path,
        *,
        rendered_receipt_ids: set[str],
    ) -> list[str]:
        receipts = sorted(
            (
                receipt
                for receipt in self.receipts.values()
                if receipt.operation_type == "workflow" and receipt.query_name == workflow_name
            ),
            key=lambda receipt: receipt.created_at,
            reverse=True,
        )
        lines: list[str] = []
        rendered_receipts = [
            receipt for receipt in receipts if receipt.receipt_id in rendered_receipt_ids
        ]
        for receipt in rendered_receipts[: self.max_per_type]:
            receipt_link = _relpath(current_path, self._receipt_path(receipt.receipt_id))
            lines.append(
                f"- [{receipt.receipt_id}]({receipt_link}) "
                f"({format_datetime(receipt.created_at)}, {receipt.duration_ms}ms)"
            )
        omitted_count = len(receipts) - len(rendered_receipts)
        if omitted_count:
            lines.append(f"- +{omitted_count} execution(s) outside this wiki scope")
        return lines

    def _workflow_step_action(self, step: WorkflowStepSchema, current_path: Path) -> str:
        if step.provider is not None:
            return "Call provider " + self._provider_markdown_link(
                step.provider,
                current_path=current_path,
            )
        if step.query is not None:
            return "Run query " + self._query_markdown_link(
                step.query,
                current_path=current_path,
            )
        if step.list_entities is not None:
            return f"List {_humanize(step.list_entities.entity_type)} records"
        if step.list_relationships is not None:
            return (
                "List recorded "
                f"{_humanize(step.list_relationships.relationship_type)} links"
            )
        if step.shape_items is not None:
            return "Shape rows"
        if step.join_items is not None:
            return "Join row sets"
        if step.filter_items is not None:
            return "Filter rows"
        if step.dedupe_items is not None:
            return "Deduplicate rows"
        if step.make_candidates is not None:
            return (
                "Build candidate "
                f"{_humanize(step.make_candidates.relationship_type)} links"
            )
        if step.map_signals is not None:
            return f"Map {_humanize(step.map_signals.signal_source)} signals"
        if step.propose_relationship_group is not None:
            return (
                "Assemble review proposal for "
                f"{_humanize(step.propose_relationship_group.relationship_type)}"
            )
        if step.make_entities is not None:
            return f"Build {_humanize(step.make_entities.entity_type)} records"
        if step.make_relationships is not None:
            return f"Build {_humanize(step.make_relationships.relationship_type)} links"
        if step.apply_entities is not None:
            return "Apply records to world model"
        if step.apply_relationships is not None:
            return "Apply links to world model"
        if step.apply_all is not None:
            return "Apply records and links to world model"
        if step.assert_spec is not None:
            return f"Check {step.assert_spec.message}"
        return "Run step"

    def _workflow_step_reads(self, step: WorkflowStepSchema, current_path: Path) -> str:
        if step.query is not None:
            return self._query_markdown_link(step.query, current_path=current_path)
        if step.provider is not None:
            return _format_mapping_refs(step.input)
        if step.list_entities is not None:
            return _humanize(step.list_entities.entity_type)
        if step.list_relationships is not None:
            return _humanize(step.list_relationships.relationship_type)
        if step.shape_items is not None:
            return _format_mapping_refs(step.shape_items.model_dump(mode="python"))
        if step.join_items is not None:
            return _format_mapping_refs(step.join_items.model_dump(mode="python"))
        if step.filter_items is not None:
            return _format_mapping_refs(step.filter_items.model_dump(mode="python"))
        if step.dedupe_items is not None:
            return _format_mapping_refs(step.dedupe_items.model_dump(mode="python"))
        if step.make_candidates is not None:
            return _format_mapping_refs(step.make_candidates.model_dump(mode="python"))
        if step.map_signals is not None:
            return _format_mapping_refs(step.map_signals.model_dump(mode="python"))
        if step.propose_relationship_group is not None:
            return ", ".join(
                [step.propose_relationship_group.candidates_from]
                + list(step.propose_relationship_group.signals_from)
            )
        if step.make_entities is not None:
            return _format_mapping_refs(step.make_entities.model_dump(mode="python"))
        if step.make_relationships is not None:
            return _format_mapping_refs(step.make_relationships.model_dump(mode="python"))
        if step.apply_entities is not None:
            return step.apply_entities.entities_from
        if step.apply_relationships is not None:
            return step.apply_relationships.relationships_from
        if step.apply_all is not None:
            return ", ".join(
                list(step.apply_all.entities_from) + list(step.apply_all.relationships_from)
            )
        return "-"

    def _workflow_step_produces(self, step: WorkflowStepSchema) -> str:
        if step.as_ is not None:
            if step.make_entities is not None:
                return f"{step.as_} ({_humanize(step.make_entities.entity_type)} records)"
            if step.make_relationships is not None:
                return (
                    f"{step.as_} ({_humanize(step.make_relationships.relationship_type)} links)"
                )
            if step.make_candidates is not None:
                return (
                    f"{step.as_} ({_humanize(step.make_candidates.relationship_type)} candidates)"
                )
            if step.map_signals is not None:
                return f"{step.as_} ({_humanize(step.map_signals.signal_source)} signals)"
            if step.shape_items is not None:
                return f"{step.as_} (shaped rows)"
            if step.join_items is not None:
                return f"{step.as_} (joined rows)"
            if step.filter_items is not None:
                return f"{step.as_} (filtered rows)"
            if step.dedupe_items is not None:
                return f"{step.as_} (deduplicated rows)"
            return step.as_
        return "-"

    def _render_contract_subsection(self, label: str, contract_name: str) -> list[str]:
        """Render a ### Input or ### Output contract subsection."""
        lines = [f"### {label}"]
        contract = self.config.contracts.get(contract_name)
        if contract is not None and contract.fields:
            if contract.description:
                lines.append(contract.description.strip())
            lines.extend(_render_property_schema_lines(contract.fields, self.config))
        else:
            lines.append("- No fields required.")
        lines.append("")
        return lines

    def _render_provider_page(self, provider_name: str, schema: ProviderSchema) -> str:
        lines = [f"# Provider: {provider_name}", ""]
        if schema.description:
            lines.extend([schema.description.strip(), ""])
        lines.append("## Summary")
        lines.append(f"- Kind: {schema.kind}")
        lines.append(f"- Runtime: {schema.runtime}")
        lines.append(f"- Version: {schema.version}")
        lines.append(f"- Ref: `{schema.ref}`")
        lines.append(f"- Contract in: {contract_reference_label(schema.contract_in)}")
        lines.append(f"- Contract out: {contract_reference_label(schema.contract_out)}")
        lines.append(f"- Deterministic: {schema.deterministic}")
        if schema.artifact:
            lines.append(f"- Artifact: {schema.artifact}")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_entity_type_page(self, entity_type: str, schema: EntityTypeSchema) -> str:
        lines = [f"# Entity Type: {entity_type}", ""]
        if schema.description:
            lines.extend([schema.description.strip(), ""])
        lines.extend(_render_property_schema_section("Properties", schema.properties, self.config))
        return "\n".join(lines).rstrip() + "\n"

    def _render_relationship_type_page(self, schema: RelationshipSchema) -> str:
        mode = "governed" if schema.proposal_policy is not None else "deterministic"
        lines = [f"# Relationship Type: {schema.name}", ""]
        if schema.description:
            lines.extend([schema.description.strip(), ""])
        lines.extend(
            [
                "## Summary",
                f"- Scope: {schema.from_entity} -> {schema.to_entity}",
                f"- Mode: {mode}",
                f"- Cardinality: {schema.cardinality}",
            ]
        )
        if schema.reverse_name:
            lines.append(f"- Reverse name: {schema.reverse_name}")
        lines.append("")
        lines.extend(_render_property_schema_section("Properties", schema.properties, self.config))
        return "\n".join(lines).rstrip() + "\n"

    def _render_pending_review_page(self, rendered_subjects: set[SubjectRef]) -> str:
        pending = [group for group in self.groups if group.status == "pending_review"]
        pending.sort(key=lambda group: group.created_at, reverse=True)
        lines = ["# Pending Review", ""]
        if not pending:
            lines.extend(["No pending candidate groups.", ""])
            return "\n".join(lines).rstrip() + "\n"

        lines.extend(
            _render_count_summary(
                "By Relationship",
                (group.relationship_type for group in pending),
            )
        )
        lines.extend(
            _render_count_summary(
                "By Priority",
                (group.review_priority for group in pending),
            )
        )

        for group in pending[: self.max_per_type]:
            lines.append(f"## {group.group_id}")
            lines.append(f"- Relationship: {group.relationship_type}")
            lines.append(f"- Priority: {group.review_priority}")
            if group.thesis_text:
                lines.append(f"- Thesis: {group.thesis_text}")
            if group.source_workflow_name:
                workflow_link = self._workflow_markdown_link(
                    group.source_workflow_name,
                    current_path=Path("governance") / "pending-review.md",
                )
                lines.append(f"- Source workflow: {workflow_link}")
            members = self.members_by_group.get(group.group_id, [])
            if members:
                lines.append(f"- Member count: {len(members)}")
                lines.append("- Affected subjects:")
                for member in members[: self.max_per_type]:
                    from_ref = SubjectRef(member.from_type, member.from_id)
                    to_ref = SubjectRef(member.to_type, member.to_id)
                    from_link = self._subject_markdown_link(
                        from_ref,
                        current_path=Path("governance") / "pending-review.md",
                        rendered_subjects=rendered_subjects,
                    )
                    to_link = self._subject_markdown_link(
                        to_ref,
                        current_path=Path("governance") / "pending-review.md",
                        rendered_subjects=rendered_subjects,
                    )
                    lines.append(f"  - {from_link} with {to_link}")
                if len(members) > self.max_per_type:
                    lines.append(f"  - +{len(members) - self.max_per_type} more subject(s)")
            lines.append("")
        if len(pending) > self.max_per_type:
            lines.append(f"- +{len(pending) - self.max_per_type} more pending group(s)")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_recent_decisions_page(self, rendered_subjects: set[SubjectRef]) -> str:
        resolutions = sorted(
            self.resolutions_by_id.values(),
            key=lambda record: record.resolved_at,
            reverse=True,
        )
        lines = ["# Recent Decisions", ""]
        if not resolutions:
            lines.extend(["No resolutions recorded.", ""])
            return "\n".join(lines).rstrip() + "\n"

        lines.extend(
            _render_count_summary(
                "By Relationship",
                (resolution.relationship_type for resolution in resolutions),
            )
        )
        lines.extend(
            _render_count_summary(
                "By Trust Status",
                (resolution.trust_status or "unspecified" for resolution in resolutions),
            )
        )

        for resolution in resolutions[: self.max_per_type]:
            lines.append(f"## {resolution.resolution_id}")
            lines.append(f"- Action: {resolution.action}")
            lines.append(f"- Relationship: {resolution.relationship_type}")
            lines.append(f"- Trust: {resolution.trust_status}")
            if resolution.rationale:
                lines.append(f"- Rationale: {resolution.rationale}")
            group = next(
                (
                    candidate
                    for candidate in self.groups
                    if candidate.resolution_id == resolution.resolution_id
                ),
                None,
            )
            if group is not None:
                if group.source_workflow_name:
                    workflow_link = self._workflow_markdown_link(
                        group.source_workflow_name,
                        current_path=Path("governance") / "recent-decisions.md",
                    )
                    lines.append(f"- Source workflow: {workflow_link}")
                members = self.members_by_group.get(group.group_id, [])
                if members:
                    lines.append(f"- Member count: {len(members)}")
                    lines.append("- Subjects:")
                    for member in members[: self.max_per_type]:
                        from_ref = SubjectRef(member.from_type, member.from_id)
                        to_ref = SubjectRef(member.to_type, member.to_id)
                        from_link = self._subject_markdown_link(
                            from_ref,
                            current_path=Path("governance") / "recent-decisions.md",
                            rendered_subjects=rendered_subjects,
                        )
                        to_link = self._subject_markdown_link(
                            to_ref,
                            current_path=Path("governance") / "recent-decisions.md",
                            rendered_subjects=rendered_subjects,
                        )
                        lines.append(f"  - {from_link} with {to_link}")
                    if len(members) > self.max_per_type:
                        lines.append(
                            f"  - +{len(members) - self.max_per_type} more subject(s)"
                        )
            lines.append("")
        if len(resolutions) > self.max_per_type:
            lines.append(f"- +{len(resolutions) - self.max_per_type} more decision(s)")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_recent_outcomes_page(self, rendered_subjects: set[SubjectRef]) -> str:
        outcomes = sorted(self.outcomes, key=lambda record: record.created_at, reverse=True)
        lines = ["# Recent Outcomes", ""]
        if not outcomes:
            lines.extend(["No outcomes recorded.", ""])
            return "\n".join(lines).rstrip() + "\n"

        lines.extend(
            _render_count_summary("By Outcome", (outcome.outcome for outcome in outcomes))
        )
        lines.extend(
            _render_count_summary(
                "By Outcome Code",
                (outcome.outcome_code or "unspecified" for outcome in outcomes),
            )
        )

        for outcome in outcomes[: self.max_per_type]:
            lines.append(f"## {outcome.outcome_id}")
            lines.append(f"- Outcome: {outcome.outcome}")
            lines.append(
                f"- Anchor: {outcome.anchor_type}:{outcome.anchor_id or outcome.receipt_id}"
            )
            if outcome.outcome_code:
                lines.append(f"- Code: {outcome.outcome_code}")
            surface_name = str(outcome.decision_context.get("surface_name", "")).strip()
            if surface_name:
                lines.append(f"- Surface: {surface_name}")
            provider_names = _extract_provider_names_from_lineage(outcome.lineage_snapshot)
            if provider_names:
                lines.append(f"- Providers: {', '.join(provider_names)}")
            related_subjects = self._related_subjects_for_outcome(outcome)
            if related_subjects:
                lines.append("- Subjects:")
                for ref in related_subjects[: self.max_per_type]:
                    subject_link = self._subject_markdown_link(
                        ref,
                        current_path=Path("governance") / "recent-outcomes.md",
                        rendered_subjects=rendered_subjects,
                    )
                    lines.append(f"  - {subject_link}")
                if len(related_subjects) > self.max_per_type:
                    lines.append(
                        f"  - +{len(related_subjects) - self.max_per_type} more subject(s)"
                    )
            lines.append("")
        if len(outcomes) > self.max_per_type:
            lines.append(f"- +{len(outcomes) - self.max_per_type} more outcome(s)")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _related_subjects_for_outcome(self, outcome: OutcomeRecord) -> list[SubjectRef]:
        refs: set[SubjectRef] = set()
        if outcome.receipt_id in self.receipts:
            refs.update(_entity_refs_from_receipt(self.receipts[outcome.receipt_id]))
        if outcome.anchor_type == "resolution" and outcome.anchor_id:
            group = next(
                (
                    candidate
                    for candidate in self.groups
                    if candidate.resolution_id == outcome.anchor_id
                ),
                None,
            )
            if group is not None:
                for member in self.members_by_group.get(group.group_id, []):
                    refs.add(SubjectRef(member.from_type, member.from_id))
                    refs.add(SubjectRef(member.to_type, member.to_id))
        return sorted(ref for ref in refs if ref in self.subject_entities)

    def _subject_markdown_link(
        self,
        subject: SubjectRef,
        *,
        current_path: Path,
        rendered_subjects: set[SubjectRef],
    ) -> str:
        entity = self.subject_entities.get(subject)
        label = _display_label(entity, self.config) if entity is not None else subject.key
        if subject not in rendered_subjects:
            return label
        return f"[{label}]({_relpath(current_path, self._subject_path(subject))})"

    def _workflow_markdown_link(self, workflow_name: str, *, current_path: Path) -> str:
        if workflow_name in self.config.workflows:
            workflow_path = _relpath(current_path, self._workflow_path(workflow_name))
            return f"[{workflow_name}]({workflow_path})"
        return workflow_name

    def _query_markdown_link(self, query_name: str, *, current_path: Path) -> str:
        if query_name in self.config.named_queries:
            return f"[{query_name}]({_relpath(current_path, self._query_path(query_name))})"
        return query_name

    def _provider_markdown_link(self, provider_name: str, *, current_path: Path) -> str:
        if provider_name in self.config.providers:
            provider_path = _relpath(current_path, self._provider_path(provider_name))
            return f"[{provider_name}]({provider_path})"
        return provider_name


def parse_subject_ref(raw: str) -> SubjectRef:
    """Parse ``EntityType:EntityId`` into a subject reference."""
    entity_type, separator, entity_id = raw.partition(":")
    if not separator or not entity_type.strip() or not entity_id.strip():
        raise ValueError(f"Invalid subject reference '{raw}'. Use EntityType:EntityId.")
    return SubjectRef(entity_type.strip(), entity_id.strip())


def _effective_scope(options: WikiOptions) -> WikiScope:
    return "all" if options.all_subjects else options.scope


def _sorted_properties(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        key: properties[key]
        for key in sorted(properties)
        if properties[key] not in (None, "", [], {})
    }


def _display_label(entity: EntityInstance | None, config: CoreConfig) -> str:
    if entity is None:
        return "unknown"
    properties = entity.properties
    for key in DISPLAY_PROPERTY_PREFERENCE:
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    schema = config.get_entity_type(entity.entity_type)
    if schema is not None:
        primary_key = schema.get_primary_key()
        if primary_key:
            value = properties.get(primary_key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return entity.entity_id


def _entity_refs_from_receipt(receipt: Receipt) -> set[SubjectRef]:
    refs: set[SubjectRef] = set()
    for node in receipt.nodes:
        if node.entity_type and node.entity_id:
            refs.add(SubjectRef(node.entity_type, node.entity_id))
        if node.node_type == "edge_traversal":
            from_type = str(node.detail.get("from_entity_type", "")).strip()
            from_id = str(node.detail.get("from_entity_id", "")).strip()
            if from_type and from_id:
                refs.add(SubjectRef(from_type, from_id))
    refs.update(_entity_refs_from_results(receipt.results))
    return refs


def _entity_refs_from_results(results: list[dict[str, Any]]) -> set[SubjectRef]:
    refs: set[SubjectRef] = set()
    for result in results:
        entity_type = str(result.get("entity_type", "")).strip()
        entity_id = str(result.get("entity_id", "")).strip()
        if entity_type and entity_id:
            refs.add(SubjectRef(entity_type, entity_id))
    return refs


def _extract_trace_ids_from_receipt(receipt: Receipt) -> set[str]:
    trace_ids: set[str] = set()
    for node in receipt.nodes:
        if node.node_type != "plan_step":
            continue
        trace_id = str(node.detail.get("trace_id", "")).strip()
        if trace_id:
            trace_ids.add(trace_id)
    return trace_ids


def _extract_trace_ids_from_lineage(lineage_snapshot: dict[str, Any]) -> set[str]:
    trace_set = lineage_snapshot.get("trace_set")
    if not isinstance(trace_set, dict):
        return set()
    trace_ids = trace_set.get("trace_ids")
    if not isinstance(trace_ids, list):
        return set()
    return {str(trace_id) for trace_id in trace_ids if str(trace_id).strip()}


def _extract_provider_names_from_lineage(lineage_snapshot: dict[str, Any]) -> list[str]:
    trace_set = lineage_snapshot.get("trace_set")
    if not isinstance(trace_set, dict):
        return []
    provider_names = trace_set.get("provider_names")
    if not isinstance(provider_names, list):
        return []
    return [str(name) for name in provider_names if str(name).strip()]


def _subject_ref_from_key(key: str) -> SubjectRef:
    entity_type, entity_id = key.split(":", 1)
    return SubjectRef(entity_type, entity_id)


def _counterpart_for_subject(subject: SubjectRef, member: CandidateMember) -> SubjectRef | None:
    from_ref = SubjectRef(member.from_type, member.from_id)
    to_ref = SubjectRef(member.to_type, member.to_id)
    if from_ref == subject:
        return to_ref
    if to_ref == subject:
        return from_ref
    return None



def _render_property_bullets(properties: dict[str, Any], depth: int = 1) -> list[str]:
    """Render non-empty properties as indented sub-bullets, recursing into nested structures."""
    filtered = {
        key: value
        for key, value in sorted(properties.items())
        if not key.startswith("_") and value not in (None, "", [], {})
    }
    indent = "  " * depth
    lines: list[str] = []
    for key, value in filtered.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            # If every dict has the same single key, flatten to values on one line.
            single_keys = {
                next(iter(d.keys())) for d in value
                if isinstance(d, dict) and len(d) == 1
            }
            if len(single_keys) == 1 and all(isinstance(d, dict) and len(d) == 1 for d in value):
                common_key = single_keys.pop()
                vals = [_render_scalar(d[common_key]) for d in value[:8]]
                label = f"{key} ({common_key})" if common_key != key else key
                lines.append(f"{indent}- {label}: {', '.join(vals)}")
                if len(value) > 8:
                    lines[-1] += f", (+{len(value) - 8} more)"
            else:
                lines.append(f"{indent}- {key}:")
                for item in value[:8]:
                    lines.extend(_render_property_bullets(item, depth + 1))
                if len(value) > 8:
                    lines.append(f"{indent}  (+{len(value) - 8} more)")
        elif isinstance(value, dict):
            lines.append(f"{indent}- {key}:")
            lines.extend(_render_property_bullets(value, depth + 1))
        elif isinstance(value, list):
            items = [_render_scalar(item) for item in value[:8]]
            lines.append(f"{indent}- {key}: {'; '.join(items)}")
            if len(value) > 8:
                lines.append(f"{indent}  (+{len(value) - 8} more)")
        else:
            lines.append(f"{indent}- {key}: {_render_scalar(value)}")
    return lines


def _render_neighbor_state_item(
    link: str,
    relationships: list[_NeighborRelationship],
) -> list[str]:
    sorted_relationships = sorted(
        relationships,
        key=lambda relationship: _humanize(relationship.relationship_type),
    )
    if len(sorted_relationships) == 1:
        relationship = sorted_relationships[0]
        description = _relationship_description(relationship)
        line = f"- {link}"
        if description:
            line += f" — {description}"
        return [line, *_render_property_bullets(relationship.properties)]

    lines = [f"- {link}"]
    for relationship in sorted_relationships:
        description = _relationship_description(relationship)
        line = f"  - {_humanize(relationship.relationship_type)}"
        if description:
            line += f": {description}"
        lines.append(line)
        lines.extend(_render_property_bullets(relationship.properties, depth=2))
    return lines


def _relationship_description(relationship: _NeighborRelationship) -> str | None:
    if relationship.relationship_schema and relationship.relationship_schema.description:
        return relationship.relationship_schema.description.strip()
    return None


def _format_mapping_refs(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    refs = sorted(set(re.findall(r"\$steps\.([A-Za-z0-9_-]+)", text)))
    if refs:
        return ", ".join(refs)
    if text in ("{}", "null"):
        return "-"
    if len(text) > 120:
        text = text[:117] + "..."
    return f"`{text}`"


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    lines = [
        "| " + " | ".join(_escape_markdown_table_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _header in headers) + " |",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(_escape_markdown_table_cell(value) for value in row) + " |"
        )
    return "\n".join(lines)


def _render_count_summary(title: str, values: Any) -> list[str]:
    counts = Counter(str(value) for value in values if str(value))
    if not counts:
        return []
    lines = [f"## {title}"]
    for value, count in sorted(counts.items()):
        lines.append(f"- {_humanize(value)}: {count}")
    lines.append("")
    return lines


def _escape_markdown_table_cell(value: str) -> str:
    return (value or "-").replace("|", "\\|").replace("\n", "<br/>")


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        flat = {k: v for k, v in value.items() if v not in (None, "", [], {})}
        if not flat:
            return "(empty)"
        pairs = [f"{k}: {_render_scalar(v)}" for k, v in sorted(flat.items())]
        return ", ".join(pairs)
    if isinstance(value, list):
        if not value:
            return "(none)"
        items = [_render_scalar(item) for item in value[:8]]
        result = "; ".join(items)
        if len(value) > 8:
            result += f"; (+{len(value) - 8} more)"
        return result
    return str(value)


def _humanize(value: str) -> str:
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return value.replace("_", " ").replace("-", " ").strip().title()


def _render_property_schema_section(
    title: str,
    fields: dict[str, Any],
    config: CoreConfig,
) -> list[str]:
    lines = [f"## {title}", ""]
    if fields:
        lines.extend(_render_property_schema_lines(fields, config))
    else:
        lines.append("- No fields required.")
    lines.append("")
    return lines


def _render_property_schema_lines(fields: dict[str, Any], config: CoreConfig) -> list[str]:
    lines: list[str] = []
    for field_name, field_schema in fields.items():
        parts = [f"**{field_name}**", f"({field_schema.type})"]
        if field_schema.primary_key:
            parts.append("*primary key*")
        if field_schema.optional:
            parts.append("*optional*")
        if field_schema.default is not None:
            parts.append(f"default: {field_schema.default}")
        if field_schema.enum_ref is not None:
            values = enum_values(config, field_schema) or []
            parts.append(f"enum_ref: {field_schema.enum_ref}")
            if values:
                parts.append(f"one of: {', '.join(values)}")
        elif field_schema.enum is not None:
            parts.append(f"one of: {', '.join(field_schema.enum)}")
        line = f"- {' '.join(parts)}"
        if field_schema.description:
            line += f" — {field_schema.description.strip()}"
        lines.append(line)
    return lines


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip()).strip("-").lower()
    return slug or "item"


def _relpath(current_path: Path, target_path: Path) -> str:
    current_dir = current_path.parent if current_path.suffix else current_path
    return os.path.relpath(target_path, start=current_dir).replace(os.sep, "/")


def _subject_mermaid_id(subject: SubjectRef) -> str:
    return mermaid_id(f"subject_{subject.entity_type}_{subject.entity_id}")


def _mermaid_node_line(node_id: str, label: str, *, role: MermaidNodeRole) -> str:
    if role == "focus":
        return f'  {node_id}(["{label}"])'
    if role == "upstream":
        return f'  {node_id}[["{label}"]]'
    return f'  {node_id}["{label}"]'


def _mermaid_subgraph(subgraph_id: str, label: str, node_lines: list[str]) -> list[str]:
    if not node_lines:
        return []
    lines = [f'  subgraph {subgraph_id}["{escape_mermaid_label(label)}"]', "    direction TB"]
    lines.extend(f"    {line.strip()}" for line in node_lines)
    lines.append("  end")
    return lines


def _format_mermaid_edge_label(labels: list[str]) -> str:
    if len(labels) <= 1:
        return labels[0] if labels else ""
    return " / ".join(_shorten_common_edge_labels(labels))


def _shorten_common_edge_labels(labels: list[str]) -> list[str]:
    word_lists = [label.split() for label in labels]
    if any(not words for words in word_lists):
        return labels

    prefix_length = 0
    for column_words in zip(*word_lists):
        if len(set(column_words)) != 1:
            break
        prefix_length += 1

    suffix_length = 0
    min_length = min(len(words) for words in word_lists)
    while prefix_length + suffix_length < min_length:
        suffix_words = {
            words[len(words) - suffix_length - 1]
            for words in word_lists
        }
        if len(suffix_words) != 1:
            break
        suffix_length += 1

    if prefix_length == 0 and suffix_length == 0:
        return labels

    shortened: list[str] = []
    for words in word_lists:
        end = len(words) - suffix_length if suffix_length else len(words)
        middle = words[prefix_length:end]
        if not middle:
            return labels
        shortened.append(" ".join(middle))
    return shortened


def _format_mermaid_edge_indexes(indexes: list[int]) -> str:
    return ",".join(str(index) for index in indexes)


def _write_pages(output_dir: Path, pages: dict[Path, str]) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / MANIFEST_NAME
    previous_files: set[str] = set()
    if manifest_path.exists():
        try:
            previous_files = set(json.loads(manifest_path.read_text()))
        except (OSError, ValueError, TypeError):
            previous_files = set()

    written_paths: list[Path] = []
    current_files = {relative.as_posix() for relative in pages}
    for stale in sorted(previous_files - current_files):
        stale_path = output_dir / stale
        if stale_path.exists():
            stale_path.unlink()

    for relative_path, content in pages.items():
        absolute_path = output_dir / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_text(content)
        written_paths.append(absolute_path)

    manifest_path.write_text(json.dumps(sorted(current_files), indent=2))
    written_paths.append(manifest_path)
    return sorted(written_paths)

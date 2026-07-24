"""Receipt serialization: JSON, Markdown, and Mermaid output."""

from __future__ import annotations

from cruxible_core.receipt.types import Receipt, ReceiptNode


def to_json(receipt: Receipt) -> str:
    """Serialize a receipt to JSON."""
    return receipt.model_dump_json(indent=2)


def to_markdown(receipt: Receipt) -> str:
    """Render a receipt as a human-readable Markdown summary."""
    lines: list[str] = []

    if receipt.operation_type != "query":
        lines.append(f"# Receipt {receipt.receipt_id} ({receipt.operation_type})")
    else:
        lines.append(f"# Receipt {receipt.receipt_id}")

    lines.append("")

    if receipt.operation_type == "query":
        lines.append(f"**Query:** {receipt.query_name}")
    elif receipt.operation_type == "workflow":
        lines.append(f"**Workflow:** {receipt.query_name}")
    else:
        lines.append(f"**Operation:** {receipt.operation_type}")
    lines.append(f"**Parameters:** {receipt.parameters}")
    lines.append(f"**Duration:** {receipt.duration_ms}ms")
    if receipt.head_snapshot_id:
        lines.append(f"**Head snapshot:** {receipt.head_snapshot_id}")
    if receipt.workflow_mode is not None:
        lines.append(f"**Workflow mode:** {receipt.workflow_mode}")
    if receipt.operation_type == "query":
        lines.append(f"**Results:** {len(receipt.results)}")
    if receipt.committed:
        lines.append("**Committed:** Yes")
    elif receipt.operation_type != "query" or receipt.workflow_mode is not None:
        lines.append("**Committed:** No")
    lines.append("")

    proposals = [n for n in receipt.nodes if n.node_type == "proposal"]
    lookups = [n for n in receipt.nodes if n.node_type == "entity_lookup"]
    traversals = [n for n in receipt.nodes if n.node_type == "edge_traversal"]
    filters = [n for n in receipt.nodes if n.node_type == "filter_applied"]
    constraints = [n for n in receipt.nodes if n.node_type == "constraint_check"]
    validations = [n for n in receipt.nodes if n.node_type == "validation"]
    plan_steps = [n for n in receipt.nodes if n.node_type == "plan_step"]
    writes = [n for n in receipt.nodes if n.node_type in ("entity_write", "relationship_write")]
    feedback_nodes = [n for n in receipt.nodes if n.node_type == "feedback_applied"]

    if proposals:
        lines.append("## Proposed")
        for n in proposals:
            subjects = n.detail.get("subjects")
            for subject in subjects if isinstance(subjects, list) else []:
                if not isinstance(subject, dict):
                    continue
                if "entity_type" in subject:
                    lines.append(f"- {subject['entity_type']}:{subject['entity_id']}")
                else:
                    lines.append(
                        f"- {subject.get('from_id', '?')} "
                        f"--{subject.get('relationship', '?')}--> "
                        f"{subject.get('to_id', '?')}"
                    )
        lines.append("")

    if lookups:
        lines.append("## Entry Points")
        for n in lookups:
            lines.append(f"- {n.entity_type}:{n.entity_id}")
        lines.append("")

    if traversals:
        lines.append("## Traversals")
        for n in traversals:
            from_type = n.detail.get("from_entity_type", "?")
            from_id = n.detail.get("from_entity_id", "?")
            lines.append(
                f"- {from_type}:{from_id} --[{n.relationship}]--> {n.entity_type}:{n.entity_id}"
            )
        lines.append("")

    if filters:
        lines.append("## Filters")
        for n in filters:
            status = "PASS" if n.detail.get("passed") else "FAIL"
            spec = n.detail.get("filter", {})
            lines.append(f"- [{status}] {spec}")
        lines.append("")

    if constraints:
        lines.append("## Constraints")
        for n in constraints:
            status = "PASS" if n.detail.get("passed") else "FAIL"
            expr = n.detail.get("constraint", "")
            lines.append(f"- [{status}] {expr} on {n.entity_type}:{n.entity_id}")
        lines.append("")

    if validations:
        lines.append("## Validations")
        for n in validations:
            status = "PASS" if n.detail.get("passed") else "FAIL"
            lines.append(f"- [{status}]")
        lines.append("")

    if plan_steps:
        lines.append("## Plan Steps")
        for n in plan_steps:
            lines.append(f"- {_node_label(n)}")
        lines.append("")

    if writes:
        lines.append("## Writes")
        for n in writes:
            lines.append(f"- {_node_label(n)}")
        lines.append("")

    if feedback_nodes:
        lines.append("## Feedback")
        for n in feedback_nodes:
            lines.append(f"- {_node_label(n)}")
        lines.append("")

    return "\n".join(lines)


def to_mermaid(receipt: Receipt) -> str:
    """Render the receipt DAG as a Mermaid flowchart."""
    lines: list[str] = ["graph TD"]

    for node in receipt.nodes:
        label = _node_label(node)
        lines.append(f'    {node.node_id}["{label}"]')

    for edge in receipt.edges:
        lines.append(f"    {edge.from_node} -->|{edge.edge_type}| {edge.to_node}")

    return "\n".join(lines)


def _node_label(node: ReceiptNode) -> str:
    """Generate a concise label for a Mermaid node."""
    if node.node_type == "query":
        return f"Query: {node.detail.get('query_name', 'query')}"

    if node.node_type == "workflow":
        return f"Workflow: {node.detail.get('workflow_name', 'workflow')}"

    if node.node_type == "mutation":
        return f"Mutation: {node.detail.get('operation_type', 'mutation')}"

    if node.node_type == "entity_lookup":
        return f"Lookup: {node.entity_type}:{node.entity_id}"

    if node.node_type == "edge_traversal":
        from_id = node.detail.get("from_entity_id", "?")
        return f"{from_id} --{node.relationship}--> {node.entity_id}"

    if node.node_type == "filter_applied":
        status = "PASS" if node.detail.get("passed") else "FAIL"
        return f"Filter: {status}"

    if node.node_type == "constraint_check":
        status = "PASS" if node.detail.get("passed") else "FAIL"
        return f"Constraint: {status}"

    if node.node_type == "result":
        return f"Results: {node.detail.get('count', 0)}"

    if node.node_type == "plan_step":
        step_id = node.detail.get("step_id", "?")
        kind = node.detail.get("kind", "?")
        return f"Step {step_id} ({kind})"

    if node.node_type == "proposal":
        subjects = node.detail.get("subjects")
        count = len(subjects) if isinstance(subjects, list) else 0
        return f"Proposal: {count} subject(s)"

    if node.node_type == "validation":
        status = "PASS" if node.detail.get("passed") else "FAIL"
        return f"Validation: {status}"

    if node.node_type == "entity_write":
        action = "update" if node.detail.get("is_update") else "add"
        return f"Write: {node.entity_type}:{node.entity_id} ({action})"

    if node.node_type == "relationship_write":
        d = node.detail
        from_id = d.get("from_id", "?")
        to_id = d.get("to_id", "?")
        rel = d.get("relationship", "?")
        action = "update" if d.get("is_update") else "add"
        return f"Write: {from_id} --{rel}--> {to_id} ({action})"

    if node.node_type == "feedback_applied":
        action = node.detail.get("action", "?")
        status = "applied" if node.detail.get("applied") else "not applied"
        return f"Feedback: {action} ({status})"

    return node.node_type

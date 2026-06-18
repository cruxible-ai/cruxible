import {
  CruxibleHttpError,
  createHttpAdapter,
  entityRefFromRow,
  evidenceRefToDereferencePayload,
  selectOverviewQueries,
} from "./adapter.js";

const state = {
  adapter: null,
  schema: null,
  queryList: null,
  overviewQueries: [],
  selectedQuery: null,
  selectedResult: null,
  selectedEntity: null,
  currentEvidenceRefs: [],
};

const els = {};

window.addEventListener("DOMContentLoaded", () => {
  bindElements();
  hydrateConnectionForm();
  bindEvents();
  connect();
});

function bindElements() {
  for (const id of [
    "server-url",
    "instance-id",
    "token",
    "connect",
    "connection-status",
    "server-summary",
    "query-list",
    "main-title",
    "main-subtitle",
    "result-meta",
    "result-table",
    "empty-state",
    "drawer",
    "drawer-title",
    "drawer-body",
    "receipt-panel",
  ]) {
    els[toCamel(id)] = document.getElementById(id);
  }
}

function hydrateConnectionForm() {
  const params = new URLSearchParams(window.location.search);
  els.serverUrl.value = params.get("server") || sessionStorage.getItem("cruxible.ui.serverUrl") || "";
  els.instanceId.value =
    params.get("instance") || sessionStorage.getItem("cruxible.ui.instanceId") || "";
  els.token.value = sessionStorage.getItem("cruxible.ui.token") || "";
}

function bindEvents() {
  els.connect.addEventListener("click", () => connect());
  els.queryList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-query]");
    if (button) {
      runOverviewQuery(button.dataset.query);
    }
  });
  els.resultTable.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-index]");
    if (row) {
      markSelectedRow(row);
      selectResult(Number(row.dataset.index));
    }
  });
  els.resultTable.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    const row = event.target.closest("tr[data-index]");
    if (row) {
      event.preventDefault();
      markSelectedRow(row);
      selectResult(Number(row.dataset.index));
    }
  });
  els.drawerBody.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-evidence-index]");
    if (button) {
      dereferenceEvidence(Number(button.dataset.evidenceIndex));
      return;
    }
    const entityButton = event.target.closest("button[data-entity-type][data-entity-id]");
    if (entityButton) {
      inspectEntity({
        type: entityButton.dataset.entityType,
        id: entityButton.dataset.entityId,
      });
    }
  });
  els.receiptPanel.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-receipt-format]");
    if (button && state.selectedResult?.receipt_id) {
      renderReceipt(state.selectedResult.receipt_id, button.dataset.receiptFormat);
    }
  });
}

async function connect() {
  const serverUrl = els.serverUrl.value.trim();
  const instanceId = els.instanceId.value.trim();
  const token = els.token.value.trim();

  sessionStorage.setItem("cruxible.ui.serverUrl", serverUrl);
  sessionStorage.setItem("cruxible.ui.instanceId", instanceId);
  if (token) {
    sessionStorage.setItem("cruxible.ui.token", token);
  } else {
    sessionStorage.removeItem("cruxible.ui.token");
  }

  setStatus("Connecting", "loading");
  clearMain();
  state.adapter = createHttpAdapter({ serverUrl, instanceId, token });

  try {
    const [serverInfo, schema, stats, queryList] = await Promise.all([
      state.adapter.getServerInfo(),
      state.adapter.getSchema(),
      state.adapter.getStats(),
      state.adapter.listQueries(),
    ]);
    state.schema = schema;
    state.queryList = queryList;
    state.overviewQueries = selectOverviewQueries(queryList, schema);
    renderConnection(serverInfo, stats);
    renderNavigation();
    setStatus("Connected", "ok");

    const params = new URLSearchParams(window.location.search);
    const requestedQuery = params.get("view");
    const firstQuery =
      state.overviewQueries.find((query) => query.name === requestedQuery) || state.overviewQueries[0];
    if (firstQuery) {
      await runOverviewQuery(firstQuery.name);
    } else {
      renderEmpty("No zero-parameter views are available for this config.");
    }

    const entityType = params.get("entity_type");
    const entityId = params.get("entity_id");
    if (entityType && entityId) {
      await inspectEntity({ type: entityType, id: entityId });
    }
  } catch (error) {
    renderError(error);
  }
}

async function runOverviewQuery(queryName) {
  const query = state.overviewQueries.find((item) => item.name === queryName);
  if (!query || !state.adapter) {
    return;
  }
  state.selectedQuery = query;
  setActiveQuery(query.name);
  setUrlParam("view", query.name);
  setStatus("Reading", "loading");
  els.mainTitle.textContent = query.name;
  els.mainSubtitle.textContent = query.description || "Named query view";
  els.resultTable.innerHTML = "";
  els.emptyState.hidden = true;
  els.resultMeta.textContent = "";
  els.receiptPanel.innerHTML = "";

  try {
    const result = await state.adapter.runView(query.name, { limit: query.limit || 50 });
    state.selectedResult = result;
    renderResultTable(result, query);
    renderReceiptSummary(result);
    setStatus("Read complete", "ok");
  } catch (error) {
    renderError(error);
  }
}

async function selectResult(index) {
  const row = state.selectedResult?.items?.[index];
  const entityRef = entityRefFromRow(row, state.selectedQuery);
  if (!entityRef) {
    renderDrawer("Row details", renderJson(row));
    return;
  }
  await inspectEntity(entityRef);
}

function markSelectedRow(row) {
  els.resultTable.querySelectorAll("tr[aria-selected]").forEach((item) => {
    item.removeAttribute("aria-selected");
  });
  row.setAttribute("aria-selected", "true");
}

async function inspectEntity(entityRef) {
  if (!state.adapter) {
    return;
  }
  setStatus("Inspecting entity", "loading");
  try {
    const entity = await state.adapter.inspectEntity(entityRef);
    state.selectedEntity = entity;
    setUrlParam("entity_type", entityRef.type);
    setUrlParam("entity_id", entityRef.id);
    renderEntityDetails(entity);
    setStatus("Entity loaded", "ok");
  } catch (error) {
    renderError(error);
  }
}

function renderConnection(serverInfo, stats) {
  const mode = serverInfo.auth_required ? "auth required" : "no auth required";
  els.serverSummary.textContent = [
    `v${serverInfo.version || "unknown"}`,
    `${serverInfo.instance_count ?? 0} instance(s)`,
    mode,
    `${stats.entity_count ?? 0} entities`,
    `${stats.edge_count ?? 0} edges`,
  ].join(" | ");
}

function renderNavigation() {
  els.queryList.innerHTML = state.overviewQueries
    .map(
      (query) => `
        <button type="button" data-query="${escapeHtml(query.name)}">
          <span>${escapeHtml(titleize(query.name))}</span>
          <small>${escapeHtml(query.returns || "view")}</small>
        </button>
      `,
    )
    .join("");
}

function renderResultTable(result, query) {
  const rows = result.items || [];
  els.resultMeta.textContent = [
    `${result.total ?? rows.length} result(s)`,
    result.truncated ? "truncated" : "",
    result.receipt_id ? `receipt ${result.receipt_id}` : "",
  ]
    .filter(Boolean)
    .join(" | ");

  if (!rows.length) {
    renderEmpty("The selected view returned no rows.");
    return;
  }

  const normalizedRows = rows.map((row) => rowToDisplay(row, query));
  const columns = selectColumns(normalizedRows);
  els.resultTable.innerHTML = `
    <thead>
      <tr>${columns.map((column) => `<th>${escapeHtml(titleize(column))}</th>`).join("")}</tr>
    </thead>
    <tbody>
      ${normalizedRows
        .map(
          (row, index) => `
            <tr data-index="${index}" tabindex="0">
              ${columns.map((column) => `<td>${escapeHtml(formatValue(row[column]))}</td>`).join("")}
            </tr>
          `,
        )
        .join("")}
    </tbody>
  `;
}

function renderReceiptSummary(result) {
  if (!result.receipt_id) {
    els.receiptPanel.innerHTML = `<p class="muted">This result did not include a receipt.</p>`;
    return;
  }
  els.receiptPanel.innerHTML = `
    <div class="receipt-toolbar">
      <strong>${escapeHtml(result.receipt_id)}</strong>
      <button type="button" data-receipt-format="markdown">Markdown</button>
      <button type="button" data-receipt-format="mermaid">Mermaid</button>
      <button type="button" data-receipt-format="raw">Raw</button>
    </div>
    <pre id="receipt-content">Select a receipt format.</pre>
  `;
}

async function renderReceipt(receiptId, format) {
  const target = document.getElementById("receipt-content");
  if (!target) {
    return;
  }
  target.textContent = "Loading receipt...";
  try {
    if (format === "raw") {
      target.textContent = JSON.stringify(await state.adapter.getReceipt(receiptId), null, 2);
      return;
    }
    const explanation = await state.adapter.explainReceipt(receiptId, { format });
    target.textContent = explanation.content || "";
  } catch (error) {
    target.textContent = errorMessage(error);
  }
}

function renderEntityDetails(entity) {
  if (!entity.found) {
    renderDrawer("Entity not found", `<p class="error">No entity matched this reference.</p>`);
    return;
  }

  state.currentEvidenceRefs = collectEvidenceRefs(entity);
  const title = `${entity.entity_type}:${entity.entity_id}`;
  const neighbors = entity.neighbors || [];
  renderDrawer(
    title,
    `
      <section>
        <h3>Properties</h3>
        ${renderKeyValueTable(entity.properties || {})}
      </section>
      <section>
        <h3>Focused graph</h3>
        ${renderFocusedGraph(entity)}
      </section>
      <section>
        <h3>Neighbors (${neighbors.length})</h3>
        ${renderNeighbors(neighbors)}
      </section>
      <section>
        <h3>Source evidence</h3>
        ${renderEvidenceRefs(state.currentEvidenceRefs)}
        <div id="evidence-preview"></div>
      </section>
      <section>
        <h3>Raw JSON</h3>
        ${renderJson(entity)}
      </section>
    `,
  );
}

function renderFocusedGraph(entity) {
  const nodes = (entity.neighbors || []).slice(0, 24);
  if (!nodes.length) {
    return `<p class="muted">No first-degree neighbors.</p>`;
  }
  return `
    <div class="graph-strip">
      <div class="graph-node graph-node-center">${escapeHtml(entity.entity_type)}<br>${escapeHtml(entity.entity_id)}</div>
      <div class="graph-neighbors">
        ${nodes
          .map((neighbor) => {
            const related = neighbor.entity || {};
            return `
              <button type="button" class="graph-node"
                data-entity-type="${escapeHtml(related.entity_type || "")}"
                data-entity-id="${escapeHtml(related.entity_id || "")}">
                <span>${escapeHtml(neighbor.relationship_type)}</span>
                <strong>${escapeHtml(related.entity_type || "Entity")}</strong>
                <small>${escapeHtml(related.entity_id || "")}</small>
              </button>
            `;
          })
          .join("")}
      </div>
    </div>
  `;
}

function renderNeighbors(neighbors) {
  if (!neighbors.length) {
    return `<p class="muted">No neighbors.</p>`;
  }
  return `
    <table class="mini-table">
      <thead><tr><th>Direction</th><th>Relationship</th><th>Entity</th><th>Status</th></tr></thead>
      <tbody>
        ${neighbors
          .map((neighbor) => {
            const related = neighbor.entity || {};
            return `
              <tr>
                <td>${escapeHtml(neighbor.direction)}</td>
                <td>${escapeHtml(neighbor.relationship_type)}</td>
                <td>
                  <button type="button" class="link-button"
                    data-entity-type="${escapeHtml(related.entity_type || "")}"
                    data-entity-id="${escapeHtml(related.entity_id || "")}">
                    ${escapeHtml(related.entity_type || "")}:${escapeHtml(related.entity_id || "")}
                  </button>
                </td>
                <td>${escapeHtml(neighbor.metadata?.assertion?.lifecycle?.status || "")}</td>
              </tr>
            `;
          })
          .join("")}
      </tbody>
    </table>
  `;
}

function renderEvidenceRefs(evidenceRefs) {
  if (!evidenceRefs.length) {
    return `<p class="muted">No evidence references on the inspected relationships.</p>`;
  }
  return `
    <div class="evidence-list">
      ${evidenceRefs
        .map((ref, index) => {
          const metadata = ref.metadata || {};
          return `
            <article>
              <div><strong>${escapeHtml(ref.label || "Evidence")}</strong></div>
              <dl>
                <dt>Artifact</dt><dd>${escapeHtml(ref.artifact_id || metadata.artifact_id || "")}</dd>
                <dt>Chunk</dt><dd>${escapeHtml(ref.source_record_id || metadata.chunk_id || "")}</dd>
                <dt>Heading</dt><dd>${escapeHtml((metadata.heading_path || []).join(" / "))}</dd>
                <dt>Lines</dt><dd>${escapeHtml(lineRange(metadata))}</dd>
                <dt>Retention</dt><dd>${escapeHtml(metadata.source_retention || "")}</dd>
              </dl>
              <button type="button" data-evidence-index="${index}">Dereference</button>
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}

async function dereferenceEvidence(index) {
  const target = document.getElementById("evidence-preview");
  const ref = state.currentEvidenceRefs[index];
  if (!target || !ref) {
    return;
  }
  target.innerHTML = `<p class="muted">Dereferencing evidence...</p>`;
  try {
    const payload = evidenceRefToDereferencePayload(ref);
    const result = await state.adapter.dereferenceEvidence(ref);
    target.innerHTML = `
      <div class="evidence-preview">
        <div><strong>Status:</strong> ${escapeHtml(result.status || "unknown")}</div>
        <div><strong>Artifact:</strong> ${escapeHtml(payload.source_artifact_id)}</div>
        <div><strong>Chunk:</strong> ${escapeHtml(payload.chunk_id)}</div>
        ${result.reason ? `<p class="warning">${escapeHtml(result.reason)}</p>` : ""}
        <pre>${escapeHtml(result.body || "")}</pre>
      </div>
    `;
  } catch (error) {
    target.innerHTML = `<p class="error">${escapeHtml(errorMessage(error))}</p>`;
  }
}

function collectEvidenceRefs(entity) {
  const refs = [];
  for (const neighbor of entity.neighbors || []) {
    refs.push(...(neighbor.metadata?.evidence?.evidence_refs || []));
  }
  return refs;
}

function rowToDisplay(row, query) {
  if (row.values) {
    return row.values;
  }
  const entity = row.result || row.entry || row;
  return {
    [`${toSnake(entity.entity_type || query.returns || "entity")}_id`]: entity.entity_id,
    ...(entity.properties || {}),
  };
}

function selectColumns(rows) {
  const preferred = [
    "work_item_id",
    "review_request_id",
    "roadmap_item_id",
    "decision_id",
    "risk_id",
    "question_id",
    "entity_id",
    "title",
    "name",
    "status",
    "priority",
    "severity",
    "owner",
    "type",
    "summary",
  ];
  const present = new Set(rows.flatMap((row) => Object.keys(row)));
  const selected = preferred.filter((column) => present.has(column));
  for (const column of present) {
    if (selected.length >= 8) {
      break;
    }
    if (!selected.includes(column)) {
      selected.push(column);
    }
  }
  return selected.slice(0, 8);
}

function renderKeyValueTable(values) {
  const entries = Object.entries(values);
  if (!entries.length) {
    return `<p class="muted">No properties.</p>`;
  }
  return `
    <table class="mini-table">
      <tbody>
        ${entries
          .map(
            ([key, value]) =>
              `<tr><th>${escapeHtml(titleize(key))}</th><td>${escapeHtml(formatValue(value))}</td></tr>`,
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderJson(value) {
  return `<pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
}

function renderDrawer(title, body) {
  els.drawer.hidden = false;
  els.drawerTitle.textContent = title;
  els.drawerBody.innerHTML = body;
}

function renderEmpty(message) {
  els.emptyState.hidden = false;
  els.emptyState.textContent = message;
}

function clearMain() {
  els.queryList.innerHTML = "";
  els.resultTable.innerHTML = "";
  els.resultMeta.textContent = "";
  els.receiptPanel.innerHTML = "";
  els.emptyState.hidden = true;
  els.drawer.hidden = true;
  els.mainTitle.textContent = "Overview";
  els.mainSubtitle.textContent = "Connect to load available state views.";
}

function renderError(error) {
  const message = errorMessage(error);
  const status = error instanceof CruxibleHttpError && error.status === 401 ? "Unauthorized" : "Error";
  setStatus(status, "error");
  renderEmpty(message);
}

function setStatus(message, tone) {
  els.connectionStatus.textContent = message;
  els.connectionStatus.dataset.tone = tone;
}

function setActiveQuery(queryName) {
  for (const button of els.queryList.querySelectorAll("button[data-query]")) {
    button.toggleAttribute("aria-current", button.dataset.query === queryName);
  }
}

function setUrlParam(key, value) {
  const params = new URLSearchParams(window.location.search);
  params.set(key, value);
  if (key === "view") {
    params.delete("entity_type");
    params.delete("entity_id");
  }
  history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
}

function errorMessage(error) {
  if (error instanceof CruxibleHttpError) {
    return `${error.errorType}: ${error.message}`;
  }
  return error?.message || String(error);
}

function formatValue(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function lineRange(metadata) {
  if (metadata.line_start && metadata.line_end) {
    return `${metadata.line_start}-${metadata.line_end}`;
  }
  return "";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toSnake(value) {
  return String(value || "entity")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[\s-]+/g, "_")
    .toLowerCase();
}

function titleize(value) {
  return String(value || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

function toCamel(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

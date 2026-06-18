const RESERVED_VIEW_PARAMS = new Set(["limit", "offset", "relationship_state"]);

export class CruxibleHttpError extends Error {
  constructor(message, { status = 0, errorType = "HttpError", payload = null } = {}) {
    super(message);
    this.name = "CruxibleHttpError";
    this.status = status;
    this.errorType = errorType;
    this.payload = payload;
  }
}

export function createHttpAdapter({
  serverUrl = "",
  instanceId,
  token = "",
  fetchImpl = globalThis.fetch,
} = {}) {
  if (!fetchImpl) {
    throw new Error("createHttpAdapter requires fetch");
  }

  const config = {
    serverUrl: trimTrailingSlash(serverUrl.trim()),
    instanceId: instanceId?.trim() || "",
    token: token?.trim() || "",
  };

  function requireInstanceId() {
    if (!config.instanceId) {
      throw new Error("instanceId is required");
    }
    return encodeURIComponent(config.instanceId);
  }

  async function requestJson(path, { method = "GET", params, body } = {}) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (config.token) {
      headers.Authorization = `Bearer ${config.token}`;
    }

    const response = await fetchImpl(buildUrl(config.serverUrl, path, params), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return parseJsonResponse(response);
  }

  return {
    config: { ...config },

    getServerInfo() {
      return requestJson("/api/v1/server/info");
    },

    getSchema() {
      return requestJson(`/api/v1/${requireInstanceId()}/schema`);
    },

    getStats() {
      return requestJson(`/api/v1/${requireInstanceId()}/stats`);
    },

    listQueries({ limit, offset = 0 } = {}) {
      return requestJson(`/api/v1/${requireInstanceId()}/queries`, {
        params: compactParams({ limit, offset }),
      });
    },

    runView(queryName, { params = {}, limit, offset = 0, relationshipState } = {}) {
      const reserved = Object.keys(params).filter((key) => RESERVED_VIEW_PARAMS.has(key));
      if (reserved.length) {
        throw new Error(`View params may not use reserved keys: ${reserved.join(", ")}`);
      }
      return requestJson(`/api/v1/${requireInstanceId()}/views/${encodeURIComponent(queryName)}`, {
        params: compactParams({
          ...params,
          limit,
          offset,
          relationship_state: relationshipState,
        }),
      });
    },

    inspectEntity({ type, id, direction = "both", relationshipType, limit }) {
      if (!type || !id) {
        throw new Error("inspectEntity requires type and id");
      }
      return requestJson(
        `/api/v1/${requireInstanceId()}/inspect/entity/${encodeURIComponent(type)}/${encodeURIComponent(id)}`,
        {
          params: compactParams({
            direction,
            relationship_type: relationshipType,
            limit,
          }),
        },
      );
    },

    getReceipt(receiptId) {
      return requestJson(`/api/v1/${requireInstanceId()}/receipts/${encodeURIComponent(receiptId)}`);
    },

    explainReceipt(receiptId, { format = "markdown" } = {}) {
      return requestJson(
        `/api/v1/${requireInstanceId()}/receipts/${encodeURIComponent(receiptId)}/explain`,
        { params: { format } },
      );
    },

    dereferenceEvidence(evidenceRef) {
      const payload = evidenceRefToDereferencePayload(evidenceRef);
      return requestJson(`/api/v1/${requireInstanceId()}/source-evidence/dereference`, {
        method: "POST",
        body: payload,
      });
    },
  };
}

export function createFixtureAdapter(fixtures = {}) {
  const clone = (value) => structuredCloneSafe(value);
  const entities = fixtures.entities || {};
  const views = fixtures.views || {};
  const receipts = fixtures.receipts || {};
  const receiptExplanations = fixtures.receiptExplanations || {};
  const evidence = fixtures.evidence || {};

  return {
    config: clone(fixtures.config || {}),
    getServerInfo: () => Promise.resolve(clone(fixtures.serverInfo || {})),
    getSchema: () => Promise.resolve(clone(fixtures.schema || {})),
    getStats: () => Promise.resolve(clone(fixtures.stats || {})),
    listQueries: () =>
      Promise.resolve(clone(fixtures.queryList || { items: fixtures.queries || [], total: 0 })),
    runView: (queryName) => Promise.resolve(clone(views[queryName] || emptyQueryResult())),
    inspectEntity: ({ type, id }) => {
      const key = `${type}:${id}`;
      return Promise.resolve(
        clone(
          entities[key] || {
            found: false,
            entity_type: type,
            entity_id: id,
            properties: {},
            metadata: {},
            neighbors: [],
            total_neighbors: 0,
          },
        ),
      );
    },
    getReceipt: (receiptId) => Promise.resolve(clone(receipts[receiptId] || {})),
    explainReceipt: (receiptId, { format = "markdown" } = {}) =>
      Promise.resolve(
        clone(
          receiptExplanations[`${receiptId}:${format}`] || {
            receipt_id: receiptId,
            format,
            content: "",
          },
        ),
      ),
    dereferenceEvidence: (evidenceRef) => {
      const payload = evidenceRefToDereferencePayload(evidenceRef);
      return Promise.resolve(
        clone(
          evidence[payload.chunk_id] || {
            status: "unavailable",
            source_artifact_id: payload.source_artifact_id,
            chunk_id: payload.chunk_id,
            content_hash: payload.expected_content_hash || "",
            expected_artifact_hash: "",
            body: null,
            reason: "Fixture evidence not found",
          },
        ),
      );
    },
  };
}

export function detectKitShape(schema = {}, queryList = {}) {
  const entityTypes = getEntityTypeNames(schema);
  const queryNames = getQueryItems(queryList).map((query) => query.name);
  const hasEntities = (...names) => names.every((name) => entityTypes.includes(name));
  const hasQuery = (name) => queryNames.includes(name);

  if (
    hasEntities("RoadmapItem", "ReleaseLine", "Milestone", "ProductArea", "WorkItem") ||
    hasQuery("release_work_items")
  ) {
    return "project-state";
  }
  if (hasEntities("Actor", "SubjectRef", "StateNote", "WorkItem") || hasQuery("actor_work_queue")) {
    return "agent-operation";
  }
  return "generic";
}

export function selectOverviewQueries(queryList = {}, schema = {}) {
  const queries = getQueryItems(queryList).filter((query) => !query.required_params?.length);
  const queryByName = new Map(queries.map((query) => [query.name, query]));
  const shape = detectKitShape(schema, queryList);
  const preferredNames =
    shape === "project-state"
      ? [
          "review_queue",
          "changes_requested_reviews",
          "blocked_work_items",
          "active_risks",
          "open_questions_needing_review",
          "superseded_decisions",
        ]
      : shape === "agent-operation"
        ? [
            "review_queue",
            "recent_state_notes",
            "blocked_work_items",
            "active_risks",
            "open_questions_needing_review",
            "proposed_decisions",
          ]
        : [];
  const preferred = preferredNames.map((name) => queryByName.get(name)).filter(Boolean);
  const fallback = queries
    .filter((query) => query.mode === "collection" && !preferred.includes(query))
    .slice(0, Math.max(0, 6 - preferred.length));
  return [...preferred, ...fallback].map((query) => ({
    ...query,
    ui_label: query.description || titleize(query.name),
  }));
}

export function entityRefFromRow(row, queryInfo = {}) {
  if (!row || typeof row !== "object") {
    return null;
  }
  if (row.entity_type && row.entity_id) {
    return { type: row.entity_type, id: row.entity_id };
  }
  if (row.result?.entity_type && row.result?.entity_id) {
    return { type: row.result.entity_type, id: row.result.entity_id };
  }
  if (row.entry?.entity_type && row.entry?.entity_id) {
    return { type: row.entry.entity_type, id: row.entry.entity_id };
  }
  if (row.target?.entity_type && row.target?.entity_id) {
    return { type: row.target.entity_type, id: row.target.entity_id };
  }
  if (row.values && typeof row.values === "object") {
    const type = queryInfo.returns || queryInfo.result_type || row.values.entity_type;
    const id =
      row.values.entity_id ||
      row.values.id ||
      (type ? row.values[`${toSnake(type)}_id`] : undefined);
    if (type && id) {
      return { type, id: String(id) };
    }
  }
  if (row.source) {
    return entityRefFromRow(row.source, queryInfo);
  }
  return null;
}

export function evidenceRefToDereferencePayload(evidenceRef = {}) {
  const metadata = evidenceRef.metadata || {};
  const sourceArtifactId =
    evidenceRef.source_artifact_id ||
    evidenceRef.artifact_id ||
    metadata.source_artifact_id ||
    metadata.artifact_id;
  const chunkId =
    evidenceRef.chunk_id ||
    evidenceRef.source_record_id ||
    metadata.chunk_id ||
    metadata.source_record_id;
  if (!sourceArtifactId || !chunkId) {
    throw new Error("Evidence reference requires artifact_id and chunk_id");
  }
  return {
    source_artifact_id: sourceArtifactId,
    chunk_id: chunkId,
    heading_path: evidenceRef.heading_path || metadata.heading_path || null,
    block_selector: evidenceRef.block_selector || metadata.block_selector || null,
    expected_content_hash:
      evidenceRef.expected_content_hash ||
      evidenceRef.content_hash ||
      metadata.expected_content_hash ||
      metadata.content_hash ||
      null,
  };
}

function buildUrl(serverUrl, path, params) {
  const base = serverUrl ? `${serverUrl}${path}` : path;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const queryString = query.toString();
  return queryString ? `${base}?${queryString}` : base;
}

async function parseJsonResponse(response) {
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (response.ok) {
    return payload;
  }
  throw new CruxibleHttpError(payload?.message || `Request failed with status ${response.status}`, {
    status: response.status,
    errorType: payload?.error_type || "HttpError",
    payload,
  });
}

function compactParams(params) {
  return Object.fromEntries(
    Object.entries(params || {}).filter(([, value]) => value !== undefined && value !== null),
  );
}

function getEntityTypeNames(schema) {
  const entityTypes = schema.entity_types || schema.config?.entity_types || {};
  if (Array.isArray(entityTypes)) {
    return entityTypes;
  }
  return Object.keys(entityTypes);
}

function getQueryItems(queryList) {
  return Array.isArray(queryList) ? queryList : queryList.items || [];
}

function trimTrailingSlash(value) {
  return value.replace(/\/+$/, "");
}

function emptyQueryResult() {
  return {
    items: [],
    receipt_id: null,
    receipt: null,
    total: 0,
    limit: null,
    offset: 0,
    truncated: false,
    steps_executed: 0,
  };
}

function structuredCloneSafe(value) {
  if (typeof globalThis.structuredClone === "function") {
    return globalThis.structuredClone(value);
  }
  return JSON.parse(JSON.stringify(value));
}

function toSnake(value) {
  return String(value)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[\s-]+/g, "_")
    .toLowerCase();
}

function titleize(value) {
  return String(value)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

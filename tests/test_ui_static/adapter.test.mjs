import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const adapter = await importAdapterModule();

test("http adapter calls read-only routes with auth and query params", async () => {
  const seen = [];
  const fetchImpl = async (url, options = {}) => {
    seen.push({ url, options });
    return jsonResponse({ ok: true });
  };
  const client = adapter.createHttpAdapter({
    serverUrl: "http://cruxible.test/",
    instanceId: "inst_123",
    token: "secret",
    fetchImpl,
  });

  await client.getServerInfo();
  await client.getSchema();
  await client.getStats();
  await client.listQueries({ limit: 10, offset: 5 });
  await client.runView("review_queue", {
    params: { owner: "ui-agent" },
    limit: 25,
    offset: 50,
    relationshipState: "reviewable",
  });
  await client.inspectEntity({ type: "WorkItem", id: "wi-1", limit: 20 });
  await client.getReceipt("RCP-1");
  await client.explainReceipt("RCP-1", { format: "mermaid" });
  await client.dereferenceEvidence({
    artifact_id: "SRC-1",
    source_record_id: "chunk-1",
    metadata: { content_hash: "sha256:test" },
  });

  assert.deepEqual(
    seen.map((request) => request.url),
    [
      "http://cruxible.test/api/v1/server/info",
      "http://cruxible.test/api/v1/inst_123/schema",
      "http://cruxible.test/api/v1/inst_123/stats",
      "http://cruxible.test/api/v1/inst_123/queries?limit=10&offset=5",
      "http://cruxible.test/api/v1/inst_123/views/review_queue?owner=ui-agent&limit=25&offset=50&relationship_state=reviewable",
      "http://cruxible.test/api/v1/inst_123/inspect/entity/WorkItem/wi-1?direction=both&limit=20",
      "http://cruxible.test/api/v1/inst_123/receipts/RCP-1",
      "http://cruxible.test/api/v1/inst_123/receipts/RCP-1/explain?format=mermaid",
      "http://cruxible.test/api/v1/inst_123/source-evidence/dereference",
    ],
  );
  assert.equal(seen[0].options.headers.Authorization, "Bearer secret");
  assert.equal(seen.at(-1).options.method, "POST");
  assert.deepEqual(JSON.parse(seen.at(-1).options.body), {
    source_artifact_id: "SRC-1",
    chunk_id: "chunk-1",
    heading_path: null,
    block_selector: null,
    expected_content_hash: "sha256:test",
  });
});

test("view params reject reserved names", async () => {
  const client = adapter.createHttpAdapter({
    instanceId: "inst_123",
    fetchImpl: async () => jsonResponse({}),
  });

  assert.throws(
    () => client.runView("review_queue", { params: { limit: "10" } }),
    /reserved keys/,
  );
});

test("fixture adapter implements the live adapter method contract", async () => {
  const fixture = adapter.createFixtureAdapter({
    serverInfo: { version: "0.2.0" },
    schema: { entity_types: { WorkItem: {} } },
    stats: { entity_count: 1, edge_count: 0 },
    queries: [{ name: "review_queue", required_params: [] }],
    views: { review_queue: { items: [], total: 0, receipt_id: "RCP-1" } },
    entities: {
      "WorkItem:wi-1": {
        found: true,
        entity_type: "WorkItem",
        entity_id: "wi-1",
        properties: {},
        metadata: {},
        neighbors: [],
      },
    },
    receipts: { "RCP-1": { receipt_id: "RCP-1" } },
    receiptExplanations: {
      "RCP-1:markdown": { receipt_id: "RCP-1", format: "markdown", content: "ok" },
    },
    evidence: {
      "chunk-1": {
        status: "available",
        source_artifact_id: "SRC-1",
        chunk_id: "chunk-1",
        body: "source text",
      },
    },
  });

  assert.equal((await fixture.getServerInfo()).version, "0.2.0");
  assert.equal((await fixture.getSchema()).entity_types.WorkItem.constructor, Object);
  assert.equal((await fixture.getStats()).entity_count, 1);
  assert.equal((await fixture.listQueries()).items[0].name, "review_queue");
  assert.equal((await fixture.runView("review_queue")).receipt_id, "RCP-1");
  assert.equal((await fixture.inspectEntity({ type: "WorkItem", id: "wi-1" })).found, true);
  assert.equal((await fixture.getReceipt("RCP-1")).receipt_id, "RCP-1");
  assert.equal((await fixture.explainReceipt("RCP-1")).content, "ok");
  assert.equal(
    (
      await fixture.dereferenceEvidence({
        artifact_id: "SRC-1",
        source_record_id: "chunk-1",
      })
    ).body,
    "source text",
  );
});

test("kit detection and overview selection prefer known zero-param queues", () => {
  const agentSchema = {
    entity_types: { Actor: {}, SubjectRef: {}, StateNote: {}, WorkItem: {} },
  };
  const queryList = {
    items: [
      { name: "actor_work_queue", required_params: ["actor_id"], mode: "traversal" },
      { name: "review_queue", required_params: [], mode: "collection", returns: "ReviewRequest" },
      { name: "blocked_work_items", required_params: [], mode: "collection", returns: "WorkItem" },
      { name: "generic_collection", required_params: [], mode: "collection", returns: "AnyEntity" },
    ],
  };

  assert.equal(adapter.detectKitShape(agentSchema, { items: [] }), "agent-operation");
  assert.equal(adapter.detectKitShape({ entity_types: { Vehicle: {} } }, { items: [] }), "generic");
  assert.deepEqual(
    adapter.selectOverviewQueries(queryList, agentSchema).map((query) => query.name),
    ["review_queue", "blocked_work_items", "generic_collection"],
  );
});

test("entityRefFromRow resolves supported query row shapes", () => {
  assert.deepEqual(adapter.entityRefFromRow({ entity_type: "WorkItem", entity_id: "wi-1" }), {
    type: "WorkItem",
    id: "wi-1",
  });
  assert.deepEqual(
    adapter.entityRefFromRow({ result: { entity_type: "Risk", entity_id: "risk-1" } }),
    { type: "Risk", id: "risk-1" },
  );
  assert.deepEqual(
    adapter.entityRefFromRow({ values: { work_item_id: "wi-2" } }, { returns: "WorkItem" }),
    { type: "WorkItem", id: "wi-2" },
  );
  assert.equal(adapter.entityRefFromRow({ values: { title: "No id" } }, { returns: "WorkItem" }), null);
});

async function importAdapterModule() {
  const source = await readFile(
    new URL("../../src/cruxible_core/ui_static/adapter.js", import.meta.url),
    "utf8",
  );
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
}

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    text: async () => JSON.stringify(payload),
  };
}

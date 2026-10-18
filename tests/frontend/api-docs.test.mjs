import test from "node:test";
import assert from "node:assert/strict";
import { listOperations, schemaReference } from "../../web/api-docs.mjs";

test("API reference lists only HTTP operations and respects operation parameter overrides", () => {
  const pathParameter = { name: "id", in: "path", required: true, schema: { type: "string" } };
  const operations = listOperations({ paths: { "/api/items/{id}": { summary: "Not an operation", parameters: [pathParameter, { name: "q", in: "query", schema: { type: "string" } }], get: { parameters: [{ name: "q", in: "query", required: true, schema: { type: "integer" } }], responses: { 200: {} } }, "x-metadata": {} } } });
  assert.equal(operations.length, 1);
  assert.equal(operations[0].method, "GET");
  assert.equal(operations[0].path, "/api/items/{id}");
  assert.deepEqual(operations[0].parameters, [pathParameter, { name: "q", in: "query", required: true, schema: { type: "integer" } }]);
});

test("schema links decode JSON pointers locally and never resolve external documents", () => {
  assert.deepEqual(schemaReference("#/components/schemas/Item~1Detail~0"), { name: "Item/Detail~", id: "schema-Item/Detail~", href: "#schema-Item%2FDetail~" });
  assert.equal(schemaReference("https://example.invalid/schema.json"), null);
  assert.equal(schemaReference("javascript:alert(1)"), null);
  assert.equal(schemaReference("#/components/schemas/%XX"), null);
  assert.equal(schemaReference("#/components/schemas/"), null);
});

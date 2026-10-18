import test from "node:test";
import assert from "node:assert/strict";
import { settingsPatch, setPath, getPath } from "../../web/settings.mjs";

test("untouched redacted credentials and empty secret controls never enter PATCH", () => {
  const original = { models: { main: { model: "model-one", api_key_set: true, extra_headers: { Authorization: "***" } } }, chiliarch: { credential_set: true } };
  const draft = structuredClone(original);
  draft.models.main.api_key = ""; draft.models.main.model = "model-two";
  draft.models.main.clear_api_key = false;
  assert.deepEqual(settingsPatch(original, draft), { models: { main: { model: "model-two" } } });
});

test("explicit key clearing and changed API keys are preserved", () => {
  assert.deepEqual(settingsPatch({}, { models: { main: { clear_api_key: true } } }), { models: { main: { clear_api_key: true } } });
  assert.deepEqual(settingsPatch({}, { search: { api_key: "test-only-secret" } }), { search: { api_key: "test-only-secret" } });
});

test("protocol parameter objects replace atomically so removed keys are not retained", () => {
  const original = { models: { main: { reasoning: { effort: "high" }, extra_body: { old: true } } } };
  assert.deepEqual(settingsPatch(original, { models: { main: { reasoning: {}, extra_body: {} } } }), { models: { main: { reasoning: {}, extra_body: {} } } });
});

test("switching settings groups retains independent role drafts", () => {
  const draft = { models: { main: { model: "main" } } };
  setPath(draft, "models.worker.model", "worker");
  setPath(draft, "computer.enabled", false);
  assert.equal(getPath(draft, "models.main.model"), "main");
  assert.equal(getPath(draft, "models.worker.model"), "worker");
  assert.equal(getPath(draft, "computer.enabled"), false);
});

test("MCP is unchanged until edited and server deletion sends the complete replacement", () => {
  const original = { mcp: { mcpServers: { local: { command: "server", env: { TOKEN: "••••" } }, remote: { url: "https://example.invalid/mcp?••••", headers: { Authorization: "••••" } } } } };
  assert.deepEqual(settingsPatch(original, structuredClone(original)), {});
  const draft = structuredClone(original);
  delete draft.mcp.mcpServers.local;
  assert.deepEqual(settingsPatch(original, draft), { mcp: { mcpServers: { remote: original.mcp.mcpServers.remote } } });
  assert.deepEqual(settingsPatch(original, { mcp: {} }), { mcp: {} });
  assert.deepEqual(settingsPatch({ mcp: { configured: true } }, { mcp: { configured: false }, default_mode: "ask" }), { default_mode: "ask" });
});

import test from "node:test";
import assert from "node:assert/strict";
import { settingsPatch, setPath, getPath, settingsExtras, mergeSettingsExtras, selectProactiveDestination, MODEL_ROLES, SETTINGS_GROUPS } from "../../web/settings.mjs";

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
  const original = { models: { main: { reasoning: { effort: "high" }, thinking: { type: "enabled", budget_tokens: 2048 }, extra_body: { old: true } } } };
  assert.deepEqual(settingsPatch(original, { models: { main: { reasoning: {}, thinking: {}, extra_body: {} } } }), { models: { main: { reasoning: {}, thinking: {}, extra_body: {} } } });
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
test("password flags and blank password controls stay private while explicit changes are kept", () => {
  const original = { server: { password_set: true, host: "127.0.0.1" } };
  assert.deepEqual(settingsPatch(original, { server: { ...original.server, password: "", clear_password: false } }), {});
  assert.deepEqual(settingsPatch(original, { server: { ...original.server, password: "new-value" } }), { server: { password: "new-value" } });
  assert.deepEqual(settingsPatch(original, { server: { ...original.server, clear_password: true } }), { server: { clear_password: true } });
});

test("channel flags and empty secret fields never enter a configuration patch", () => {
  const original = { channels: { qq: { enabled: false, token_set: true, webhook_secret_set: true, client_secret_set: true } }, chiliarch: { credential_set: true } };
  const draft = structuredClone(original);
  Object.assign(draft.channels.qq, { enabled: true, token: "", webhook_secret: "", client_secret: "", clear_token: false, clear_webhook_secret: false, clear_client_secret: false });
  draft.chiliarch.credential = "";
  assert.deepEqual(settingsPatch(original, draft), { channels: { qq: { enabled: true } } });
  assert.equal(original.channels.qq.enabled, false);
});

test("new channel credentials and explicit clearing are retained independently", () => {
  const draft = { channels: { onebot: { token: "new-token", clear_webhook_secret: true }, qq: { client_secret: "new-secret", clear_token: true }, weixin: { clear_client_secret: true } } };
  assert.deepEqual(settingsPatch({}, draft), draft);
});

test("advanced field editing keeps primary controls and other settings drafts", () => {
  const original = { voice: { enabled: false, wake_command: [], chunk_seconds: 2 }, computer: { capture: { enabled: false, max_file_bytes: 1024 } } };
  const draft = structuredClone(original);
  setPath(draft, "voice.enabled", true);
  mergeSettingsExtras(draft, "voice", { wake_command: ["/local/wake", "--json"], chunk_seconds: 1 });
  mergeSettingsExtras(draft, "computer.capture", { max_file_bytes: 2048 });
  assert.deepEqual(settingsPatch(original, draft), { voice: { enabled: true, wake_command: ["/local/wake", "--json"], chunk_seconds: 1 }, computer: { capture: { max_file_bytes: 2048 } } });
  assert.equal(original.voice.enabled, false);
});

test("advanced channel JSON omits primary fields, secrets and redacted flags", () => {
  const section = { enabled: false, token: "temporary-draft", token_set: true, webhook_secret_set: true, client_secret_set: true, clear_token: true, clear_webhook_secret: false, owner_ids: ["owner"], token_env: "MY_BOT_TOKEN", poll_timeout: 15, app_id: "app" };
  assert.deepEqual(settingsExtras(section, ["enabled", "owner_ids"]), { token_env: "MY_BOT_TOKEN", poll_timeout: 15, app_id: "app" });
});

test("memory extraction has an independent model draft and visible management group", () => {
  assert.ok(MODEL_ROLES.some(([role]) => role === "memory_extractor"));
  assert.ok(["channels", "inputs", "maintenance"].every((key) => SETTINGS_GROUPS.some(([group]) => group === key)));
  const original = { models: { main: { model: "main" }, memory_extractor: { model: "", reasoning: {} } } };
  const draft = structuredClone(original);
  setPath(draft, "models.memory_extractor.model", "extractor");
  setPath(draft, "models.memory_extractor.reasoning", { effort: "high" });
  assert.deepEqual(settingsPatch(original, draft), { models: { memory_extractor: { model: "extractor", reasoning: { effort: "high" } } } });
});

test("channel-like names inside atomic MCP environment configuration remain exact", () => {
  const mcp = { mcpServers: { example: { enabled: false, env: { token_set: "••••", token: "", CLIENT_SECRET: "••••" } } } };
  assert.deepEqual(settingsPatch({ mcp: {} }, { mcp }), { mcp });
});

test("external default is a complete destination patch and enables in one selection", () => {
  const original = { channels: { proactive: { external_enabled: false, default_destination: { kind: "web" } }, onebot: { owner_ids: ["7"] } } };
  const draft = structuredClone(original);
  selectProactiveDestination(draft, "onebot", "bot");
  assert.equal(draft.channels.proactive.external_enabled, true);
  assert.equal(draft.channels.proactive.default_destination.route.target, "");
  assert.equal(draft.channels.proactive.default_destination.route.sender_id, "");
  draft.channels.proactive.default_destination.route.target = "7";
  draft.channels.proactive.default_destination.route.sender_id = "7";
  assert.deepEqual(settingsPatch(original, draft), { channels: { proactive: { external_enabled: true, default_destination: { kind: "channel", route: { name: "onebot", account_id: "bot", kind: "private", target: "7", sender_id: "7" } } } } });
  assert.equal(original.channels.proactive.external_enabled, false);
});

test("switching default to WebUI preserves earlier-send authority and removes the full route", () => {
  const original = { channels: { proactive: { external_enabled: true, default_destination: { kind: "channel", route: { name: "onebot", account_id: "bot", kind: "private", target: "7", sender_id: "7" } } } } };
  const draft = structuredClone(original);
  selectProactiveDestination(draft, "web");
  assert.equal(draft.channels.proactive.external_enabled, true);
  assert.deepEqual(settingsPatch(original, draft), { channels: { proactive: { default_destination: { kind: "web" } } } });
  const same = structuredClone(original);
  same.channels.proactive.external_enabled = false;
  selectProactiveDestination(same, "onebot", "replacement-ignored");
  assert.equal(same.channels.proactive.external_enabled, true);
  assert.deepEqual(same.channels.proactive.default_destination, original.channels.proactive.default_destination);
});

test("changing external default replaces every binding field without guessing recipient", () => {
  const draft = { channels: { proactive: { external_enabled: false, default_destination: { kind: "channel", route: { name: "onebot", account_id: "old", target: "7", sender_id: "7", kind: "group" } } } } };
  selectProactiveDestination(draft, "telegram", "123");
  assert.deepEqual(draft.channels.proactive.default_destination, { kind: "channel", route: { name: "telegram", account_id: "123", kind: "private", target: "", sender_id: "" } });
  assert.throws(() => selectProactiveDestination(draft, "unknown"), /未知/);
});

test("editing an external destination while paused sends false even when unchanged", () => {
  const original = { channels: { proactive: { external_enabled: false, default_destination: { kind: "channel", route: { name: "onebot", account_id: "bot", kind: "private", target: "7", sender_id: "7" } } } } };
  const draft = structuredClone(original);
  Object.assign(draft.channels.proactive.default_destination.route, { target: "8", sender_id: "8" });
  const patch = settingsPatch(original, draft);
  assert.equal(patch.channels.proactive.external_enabled, false);
  assert.deepEqual(patch.channels.proactive.default_destination, draft.channels.proactive.default_destination);
  assert.deepEqual(settingsPatch(original, structuredClone(original)), {});
});

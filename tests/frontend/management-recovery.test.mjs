import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { loadSettingsResources, managementLabel, managementActionLabel, managementPlanLabel, managementRecoveryView, managementRecoveryReplyLabel, updateLabel } from "../../web/management.mjs";

const source = readFileSync(new URL("../../web/app.mjs", import.meta.url), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from);
  return source.slice(from, to);
}
const transaction = (changes = {}) => ({ protocol: 2, generation: "current", revision: 1, action: "switch", phase: "prepared", activation: "pending", resolved: false, code: null, failure_code: null, ...changes });
const management = (record = transaction(), plans = []) => ({ recovery: { available: true, reason: null, transaction: record }, plans, plugins: [] });
const candidate = (changes = {}) => ({ id: "current-update", action: "switch", operation: "update", candidate_protocol: 1, status: "applying", phase: "candidate_cleanup_required", ...changes });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Small render nodes; evaluate the real request, refresh and action functions.
function element(tag, props = {}, ...children) {
  return { tag, props, disabled: false, children: children.flat(Infinity).filter((child) => child != null && child !== false),
    append(...items) { this.children.push(...items.flat(Infinity).filter(Boolean)); },
    replaceChildren(...items) { this.children = items.flat(Infinity).filter(Boolean); } };
}
const nodes = (root) => [root, ...root.children.flatMap(nodes)];
const text = (root) => nodes(root).map((node) => node.props.text ?? "").join(" ");
function harness({ initial = management(), respond } = {}) {
  const calls = [], owner = { controller: new AbortController() };
  const state = { view: "settings", settingsTab: "maintenance", viewLoading: false, settingsDraft: { persona: { name: "未保存的称呼" } }, settingsStatusErrors: {}, status: {}, management: initial, managementRecoveryAttempt: null };
  const refresh = element("button", { action: "refresh-settings-status" });
  const root = element("div", {}, refresh);
  let collected = 0;
  const context = {
    state, connection: owner, AbortController, element, loadSettingsResources,
    managementLabel, managementActionLabel, managementPlanLabel, managementRecoveryView, managementRecoveryReplyLabel, updateLabel,
    connectionCurrent: (value) => value === context.connection && !value.controller.signal.aborted,
    apiRequest: async (path, options = {}) => {
      calls.push({ path, method: options.method ?? "GET", ...(options.body === undefined ? {} : { body: JSON.parse(JSON.stringify(options.body)) }), ...(options.cache ? { cache: options.cache } : {}) });
      return respond ? respond(path, options) : path === "/management" ? state.management : {};
    },
    $: (selector, parent = root) => selector === "#other-view" ? root : nodes(parent).find((node) => selector.startsWith("#") ? node.props.id === selector.slice(1) : node.props.action === selector.match(/data-action="([^"]+)"/)?.[1]),
    collectSettingsForm: () => { collected += 1; },
    actionButton: (label, action) => element("button", { text: label, action }),
    settingsSection: (title, ...children) => element("section", { title }, ...children),
    settingsResourceError: () => null, settingsJSON: () => null, switchField: () => null, field: () => null,
    formatFullDate: String, renderChrome() {}, showLogin() {},
    syncChannelDeliveryScope() {},
    renderOtherView: () => context.refreshManagementRecoveryView(),
  };
  runInNewContext([
    section("async function ownedRequest(", "function showLogin("),
    section("async function refreshSettingsResources(", "function settingsResourceError("),
    section("function managementRecoveryContent(", "function renderModelSettings("),
    section("function statusList(", "function renderSystemSettings("),
    section("async function handleAction(", 'document.addEventListener("click", async (event)'),
  ].join("\n"), context, { timeout: 1000 });
  root.append(context.renderMaintenanceSettings());
  const current = () => nodes(root).find((node) => node.props.id === "management-current-recovery");
  const button = () => nodes(current()).find((node) => node.props.action === "recover-management-current");
  return { context, state, owner, root, refresh, calls, current, button, collected: () => collected,
    click: () => context.handleAction("recover-management-current", button()),
    read: () => context.handleAction("refresh-settings-status", refresh),
    posts: () => calls.filter((call) => call.method === "POST") };
}

test("known public V1 and V2 transactions show actual phase, activation and cause", () => {
  for (const protocol of [undefined, 2]) {
    const view = managementRecoveryView(management(transaction({ protocol, phase: "rolling_back", code: "health_failed" })));
    assert.equal(view.canRecover, true);
    assert.deepEqual(view.entries, [["当前恢复", "当前操作未结束"], ["阶段", "正在回退"], ["启用状态", "等待启用"], ["原因", "健康核查未通过"]]);
  }
  const view = managementRecoveryView(management(transaction({ phase: "rolled_back", activation: "released_degraded", resolved: true, failure_code: "backend_failed" })));
  assert.equal(view.canRecover, false);
  assert.match(JSON.stringify(view.entries), /当前操作已结束.*已回退.*部分能力异常.*服务操作失败/);
});

test("unknown protocols, states and inconsistent records never become idle or actionable", () => {
  const records = [
    transaction({ protocol: 3 }), transaction({ protocol: "2" }), transaction({ protocol: null }),
    transaction({ phase: "future" }), transaction({ activation: "future" }), transaction({ action: "future" }), transaction({ resolved: true }),
    transaction({ activation: "released" }), transaction({ code: "future" }), transaction({ failure_code: "future" }),
    transaction({ generation: 42 }), transaction({ revision: 0 }), {},
  ];
  for (const record of records) {
    const view = managementRecoveryView(management(record));
    assert.equal(view.canRecover, false);
    assert.deepEqual(view.entries, [["当前恢复", "状态未知"]]);
  }
  for (const value of [null, {}, { recovery: {} }, { recovery: { available: true } }, { recovery: { available: true, transaction: null } }]) {
    assert.deepEqual(managementRecoveryView(value).entries, [["当前恢复", "状态未知"]]);
  }
  assert.deepEqual(managementRecoveryView(management(null, [candidate({ status: "future" })])).entries, [["当前恢复", "状态未知"]]);
});

test("manual recovery, blocked activation and unsupported hosts have no recovery button", () => {
  for (const record of [transaction({ phase: "recovery_required" }), transaction({ phase: "committed", activation: "blocked" })]) {
    const h = harness({ initial: management(record) });
    assert.equal(h.button(), undefined);
    assert.match(text(h.current()), /人工核查/);
    assert.deepEqual(h.calls, []);
  }
  const initial = management(); initial.recovery.available = false; initial.recovery.reason = "/private/path/helper-token";
  const h = harness({ initial });
  assert.equal(h.button(), undefined);
  assert.match(text(h.current()), /当前不可用/);
  assert.doesNotMatch(text(h.current()), /private|helper-token/);
});

test("candidate cleanup uses the current plan and unresolved transactions take priority", () => {
  for (const phase of ["candidate_preparing", "candidate_cleanup_required"]) {
    assert.equal(managementRecoveryView(management(null, [candidate({ phase })])).canRecover, true);
  }
  for (const plans of [[candidate({ status: "interrupted", phase: "candidate_cleaned" })], [candidate({ phase: "candidate_ready" })], [candidate({ candidate_protocol: 2 })], [candidate({ phase: "future" })], [candidate(), candidate({ id: "another" })]]) {
    assert.equal(managementRecoveryView(management(null, plans)).canRecover, false);
  }
  assert.equal(managementRecoveryView(management(transaction(), [candidate()])).key, "transaction:current");
  assert.equal(managementRecoveryView(management(transaction({ phase: "aborted", resolved: true }), [candidate()])).key, "candidate:current-update");
});

test("only the current recovery area exposes an action, without rendering private fields", async () => {
  const initial = management(null, [candidate({ candidate_attempt: "/private/environment", health: { challenge: "secret" } }), candidate({ id: "old-update", status: "interrupted", phase: "candidate_cleaned" })]);
  const h = harness({ initial, respond: () => ({ helper: "candidate_cleanup_busy" }) });
  assert.equal(nodes(h.root).filter((node) => node.props.action === "recover-management-current").length, 1);
  assert.doesNotMatch(text(h.root), /private|environment|challenge|secret/);
  await h.click();
  assert.deepEqual(h.posts(), [{ path: "/management/transactions/recover", method: "POST", body: {} }]);
  assert.match(text(h.current()), /正忙/);
});

test("one explicit click POSTs an empty object once and accepted never becomes completion", async () => {
  const post = deferred(), initial = management();
  const h = harness({ initial, respond: () => post.promise });
  const draft = h.state.settingsDraft, sending = h.click();
  assert.equal(h.button().disabled, true); assert.equal(h.refresh.disabled, true);
  await h.click();
  assert.deepEqual(h.posts(), [{ path: "/management/transactions/recover", method: "POST", body: {} }]);
  post.resolve({ ...management(transaction({ phase: "committed", activation: "released", resolved: true })).recovery, helper: "accepted" });
  await sending;
  assert.equal(h.state.management === initial, true, "POST results cannot replace the read-only snapshot");
  assert.equal(h.state.settingsDraft === draft, true); assert.equal(h.collected(), 1);
  assert.equal(h.button().disabled, true); assert.equal(h.refresh.disabled, false);
  assert.match(text(h.current()), /已接受/); assert.doesNotMatch(text(h.current()), /已完成|当前操作已结束/);
  assert.equal(h.calls.length, 1, "submission never automatically refreshes or repeats");
});

test("refresh is GET-only and completion comes from a fresh resolved record", async () => {
  let actual = management();
  const h = harness({ respond: (path, options) => options.method === "POST" ? { helper: "accepted" } : path === "/management" ? actual : {} });
  await h.read();
  assert.equal(h.posts().length, 0);
  await h.click();
  actual = management(transaction({ revision: 2, phase: "committed", activation: "pending" }));
  await h.read(); await h.click();
  assert.equal(h.state.managementRecoveryAttempt.confirmed, true);
  assert.equal(h.posts().length, 1); assert.equal(h.button().disabled, true);
  assert.doesNotMatch(text(h.current()), /已完成|当前操作已结束/);
  actual = management(transaction({ revision: 3, phase: "committed", activation: "released", resolved: true }));
  await h.read();
  assert.match(text(h.current()), /当前操作已结束/); assert.equal(h.button(), undefined);
  assert.equal(h.posts().length, 1);
  assert.ok(h.calls.filter((call) => call.path === "/management").every((call) => call.method === "GET" && call.cache === "no-store"));
  actual = management(transaction({ generation: "next-operation" }));
  await h.read(); await h.click();
  assert.equal(h.posts().length, 2, "a newly read current operation gets its own explicit action");
});

test("lost replies survive reconnection and read-only confirmation never resubmits", async () => {
  const h = harness({ respond: (path, options) => {
    if (options.method === "POST") throw new Error("private-key /private/path");
    return path === "/management" ? management(transaction({ revision: 2 })) : {};
  } });
  await h.click(); await h.click();
  assert.match(text(h.current()), /结果未知/); assert.doesNotMatch(text(h.current()), /private/);
  h.owner.controller.abort(); h.context.connection = { controller: new AbortController() };
  await h.click(); await h.read(); await h.click();
  assert.equal(h.posts().length, 1);
  assert.equal(h.button().disabled, true);
  assert.match(text(h.current()), /尚未结束/);
});

test("accepted, busy, unavailable and unknown replies remain requests to verify", () => {
  for (const helper of ["accepted", "running", "controller_busy", "candidate_cleanup_busy", "unavailable", "future"]) {
    const label = managementRecoveryReplyLabel({ helper });
    assert.match(label, /刷新状态/); assert.doesNotMatch(label, /已完成|成功|已结束/);
  }
  assert.match(managementRecoveryReplyLabel(null), /结果未知/);
  assert.match(managementRecoveryReplyLabel({ available: true, transaction: transaction({ phase: "aborted", resolved: true }) }), /刷新状态确认/);
});

test("known busy replies require a fresh GET before another explicit current-operation click", async () => {
  for (const helper of ["controller_busy", "candidate_cleanup_busy"]) {
    const initial = helper === "candidate_cleanup_busy" ? management(null, [candidate()]) : management();
    const h = harness({ initial, respond: (path, options) => options.method === "POST" ? { helper } : path === "/management" ? initial : {} });
    await h.click(); await h.click();
    assert.equal(h.posts().length, 1); assert.equal(h.button().disabled, true);
    assert.match(text(h.current()), /正忙/);
    await h.read();
    assert.equal(h.posts().length, 1, "GET never replays the busy request");
    assert.equal(h.button().disabled, false);
    await h.click(); await h.click();
    assert.equal(h.posts().length, 2, "each new explicit busy attempt needs its own next read");
    assert.equal(h.button().disabled, true);
  }
});

test("unavailable and unknown responses cannot be treated as known nonacceptance after a GET", async () => {
  for (const reply of [{ helper: "unavailable", reason: "helper_unavailable" }, { helper: "unavailable", reason: "helper_start_failed" }, { helper: "future" }]) {
    const initial = management();
    const h = harness({ initial, respond: (path, options) => options.method === "POST" ? reply : path === "/management" ? initial : {} });
    await h.click(); await h.read(); await h.click();
    assert.equal(h.posts().length, 1);
    assert.equal(h.button().disabled, true);
    assert.doesNotMatch(text(h.current()), /已完成|当前操作已结束/);
  }
});

test("reads begun before submission cannot confirm or replace the post-submission snapshot", async () => {
  const oldRead = deferred(), initial = management();
  const h = harness({ initial, respond: (path, options) => options.method === "POST" ? { helper: "accepted" } : path === "/management" ? oldRead.promise : {} });
  const reading = h.read(); await h.click();
  oldRead.resolve(management(transaction({ generation: "stale-operation", phase: "aborted", resolved: true })));
  await reading;
  assert.equal(h.state.management === initial, true);
  assert.equal(h.state.managementRecoveryAttempt.confirmed, false);
  assert.doesNotMatch(text(h.current()), /当前操作已结束/);
  await h.click(); assert.equal(h.posts().length, 1);
});

test("a read begun during submission still requires a later read before another action", async () => {
  const post = deferred(), read = deferred();
  let fresh = false;
  const h = harness({ respond: (path, options) => options.method === "POST" ? post.promise : path === "/management" ? fresh ? management(transaction({ generation: "next-operation" })) : read.promise : {} });
  const sending = h.click(), reading = h.read();
  post.resolve({ helper: "accepted" }); await sending;
  read.resolve(management(transaction({ generation: "next-operation" }))); await reading;
  await h.click(); assert.equal(h.posts().length, 1);
  assert.equal(h.state.managementRecoveryAttempt.confirmed, false);
  fresh = true; await h.read(); await h.click();
  assert.equal(h.posts().length, 2);
});

test("a late read begun during submission cannot overwrite a newer confirmation or its errors", async () => {
  for (const oldFails of [false, true]) {
    const post = deferred(), earlier = deferred(), later = deferred();
    let reads = 0;
    const h = harness({ respond: (path, options) => options.method === "POST" ? post.promise : path === "/management" ? ++reads === 1 ? earlier.promise : later.promise : {} });
    const sending = h.click(), oldReading = h.read();
    post.resolve({ helper: "controller_busy" }); await sending;
    const newReading = h.read(), latest = management(transaction({ generation: "new-current-operation" }));
    later.resolve(latest); await newReading;
    assert.equal(h.state.management === latest, true);
    assert.equal(h.state.managementRecoveryAttempt.confirmed, true);
    if (oldFails) earlier.reject(new Error("old read failed"));
    else earlier.resolve(management(transaction({ generation: "old-operation" })));
    await oldReading;
    assert.equal(h.state.management === latest, true, "the older read must not replace the newer snapshot");
    assert.equal(Object.hasOwn(h.state.settingsStatusErrors, "management"), false);
    assert.equal(h.posts().length, 1, "read completion cannot submit recovery");
    await h.click();
    assert.equal(h.posts().length, 2, "the newer confirmed operation retains its explicit action");
  }
});

test("a failed read preserves uncertainty and a stale connection cannot publish completion", async () => {
  const post = deferred();
  const h = harness({ respond: (path, options) => {
    if (options.method === "POST") return post.promise;
    if (path === "/management") throw new Error("unavailable");
    return {};
  } });
  const initial = h.state.management, sending = h.click();
  h.owner.controller.abort(); h.context.connection = { controller: new AbortController() };
  post.resolve({ helper: "accepted", ...management(transaction({ phase: "aborted", resolved: true })).recovery });
  await sending; await h.read(); await h.click();
  assert.equal(initial.recovery.transaction.resolved, false);
  assert.equal(h.state.management, null);
  assert.equal(h.state.managementRecoveryAttempt.confirmed, false);
  assert.equal(h.posts().length, 1);
  assert.match(text(h.current()), /结果未知|状态未知/);
  assert.doesNotMatch(text(h.current()), /已完成|当前操作已结束/);
});

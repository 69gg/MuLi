import test from "node:test";
import assert from "node:assert/strict";
import { captureTeamView, createTeamPager, restoreTeamView } from "../../web/team.mjs";

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("older SSE refresh cannot overwrite a later manual page or filter", async () => {
  const pending = [];
  const pager = createTeamPager((path) => {
    const result = deferred(); pending.push({ path, ...result }); return result.promise;
  });
  const stale = pager.load();
  const latest = pager.load({ offset: 50, activeOnly: true });
  assert.equal(pending[1].path, "/team?limit=50&offset=50&active_only=true");
  const page = { members: [{ id: "recent" }], limit: 50, offset: 50, active_only: true };
  pending[1].resolve(page);
  assert.equal(await latest, page);
  pending[0].resolve({ members: [{ id: "old" }], limit: 50, offset: 0, active_only: false });
  assert.equal(await stale, null);
  assert.deepEqual(pager.query, { limit: 50, offset: 50, activeOnly: true });
});

test("background refresh follows pending navigation and accepts server-clamped page", async () => {
  const pending = [];
  const pager = createTeamPager((path) => {
    const result = deferred(); pending.push({ path, ...result }); return result.promise;
  }, { limit: 2 });
  const navigation = pager.load({ offset: 4, activeOnly: true });
  const refresh = pager.load();
  assert.equal(pending[1].path, "/team?limit=2&offset=4&active_only=true");
  pending[0].reject(new Error("old request failed"));
  assert.equal(await navigation, null);
  pending[1].resolve({ members: [], limit: 2, offset: 0, active_only: true });
  assert.equal((await refresh).offset, 0);
  assert.deepEqual(pager.query, { limit: 2, offset: 0, activeOnly: true });
});

test("current page errors remain visible instead of fabricating an empty page", async () => {
  const pager = createTeamPager(async () => { throw new Error("offline"); });
  await assert.rejects(pager.load(), /offline/);
});

function control(action, data = {}, extra = {}) {
  return { dataset: { action, ...data }, tagName: 'BUTTON', disabled: false, focused: false,
    focus() { this.focused = true; }, ...extra };
}

function surface(focused, controls = [], inside = true) {
  const heading = control('', {}, { tagName: 'H2' });
  const summary = control('', {}, { tagName: 'SUMMARY' });
  const disclosure = { open: true, querySelector: () => summary };
  return {
    ownerDocument: { activeElement: focused }, contains: () => inside,
    querySelector: (selector) => selector === '.team-filter' ? {} : selector === '.team-messages' ? disclosure : heading,
    querySelectorAll: () => controls, heading, disclosure, summary,
  };
}

test("live replacement restores member identity and expanded message disclosure", () => {
  const old = control('message-team', { id: 'b' });
  const saved = captureTeamView(surface(old));
  const unrelated = control('message-team', { id: 'a' });
  const replacement = control('message-team', { id: 'b' });
  const root = surface(null, [unrelated, replacement]);
  root.disclosure.open = false;
  restoreTeamView(root, saved);
  assert.equal(replacement.focused, true);
  assert.equal(unrelated.focused, false);
  assert.equal(root.disclosure.open, true);
});

test("page focus follows direction despite changed offsets; disabled target falls back to heading", () => {
  const saved = captureTeamView(surface(control('page-team', { direction: 'next', offset: '50' })));
  const next = control('page-team', { direction: 'next', offset: '100' });
  const root = surface(null, [next]);
  restoreTeamView(root, saved);
  assert.equal(next.focused, true);
  next.disabled = true; next.focused = false;
  restoreTeamView(root, saved);
  assert.equal(next.focused, false);
  assert.equal(root.heading.focused, true);
  assert.equal(root.heading.tabIndex, -1);
});

test("background refresh never steals focus outside the team region", () => {
  const saved = captureTeamView(surface(control('theme'), [], false));
  const root = surface(null, [control('refresh-team')]);
  restoreTeamView(root, saved);
  assert.equal(root.heading.focused, false);
  assert.equal(root.disclosure.open, true);
});

test("record action focus belongs to its session rather than the first teammate", () => {
  const saved = captureTeamView(surface(control('workflow-session', { session: 'second' })));
  const first = control('workflow-session', { session: 'first' });
  const second = control('workflow-session', { session: 'second' });
  restoreTeamView(surface(null, [first, second]), saved);
  assert.equal(first.focused, false);
  assert.equal(second.focused, true);
});

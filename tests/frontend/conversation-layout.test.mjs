import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { initializeConversationWidth } from "../../web/conversation-layout.mjs";

// Geometry is controlled here; real browser layout remains a separate check.
function fixture(saved = null, viewport = 1400) {
  const values = new Map(saved === null ? [] : [["muli.conversation-width", saved]]);
  const writes = [], events = {}, properties = new Map(), classes = new Set();
  const host = { addEventListener: (type, handler) => { events[type] = handler; } };
  const storage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => { writes.push([key, value]); values.set(key, value); } };
  const edges = ["left", "right"].map((side) => {
    const handlers = {}, attributes = {}, captured = new Set();
    return {
      dataset: { conversationEdge: side }, attributes, hidden: false, tabIndex: 0,
      addEventListener: (type, handler) => { handlers[type] = handler; },
      setAttribute: (key, value) => { attributes[key] = value; }, focus() {},
      setPointerCapture: (id) => captured.add(id), hasPointerCapture: (id) => captured.has(id),
      releasePointerCapture(id) { captured.delete(id); handlers.lostpointercapture({ pointerId: id }); },
      emit(type, payload = {}) {
        const event = { pointerId: 1, button: 0, clientX: 500, preventDefault() { this.prevented = true; }, ...payload };
        handlers[type](event); return event;
      },
    };
  });
  const scroll = { clientWidth: viewport };
  const root = {
    clientWidth: viewport, style: { setProperty: (key, value) => properties.set(key, value) },
    classList: { add: (key) => classes.add(key), remove: (key) => classes.delete(key) },
    querySelectorAll: () => edges, querySelector: () => scroll,
  };
  const controller = initializeConversationWidth(root, { storage, host });
  return { root, edges, writes, values, classes, controller,
    width: () => Number.parseFloat(properties.get("--conversation-width")),
    resize(width) { root.clientWidth = scroll.clientWidth = width; events.resize(); },
    blur() { events.blur(); },
  };
}

test("either edge resizes symmetrically and persists only the completed gesture", () => {
  for (const [index, movement] of [[0, -45], [1, 45]]) {
    const h = fixture(), edge = h.edges[index], initial = h.width();
    edge.emit("pointerdown"); edge.emit("pointermove", { clientX: 500 + movement });
    assert.equal(h.width(), initial + 90);
    assert.equal(h.classes.has("is-resizing-conversation"), true);
    assert.equal(h.writes.length, 0);
    edge.emit("pointerup");
    assert.equal(h.writes.length, 1);
    assert.equal(h.classes.size, 0);
    assert.equal(fixture(h.writes[0][1]).width(), initial + 90);
    assert.equal(h.edges[0].attributes["aria-valuenow"], h.edges[1].attributes["aria-valuenow"]);
  }
});

test("viewport limits never overwrite the saved preference and mobile hides both edges", () => {
  const h = fixture("1200");
  h.resize(900); assert.ok(h.width() < 900);
  h.resize(375); assert.ok(h.width() <= 375);
  assert.equal(h.edges.every((edge) => edge.hidden && edge.tabIndex === -1), true);
  assert.equal(h.edges[0].emit("pointerdown").prevented, undefined);
  h.resize(1600); assert.equal(h.width(), 1200); assert.equal(h.writes.length, 0);
  // Showing chat again after an initially hidden management view recalculates
  // geometry, without waiting for another window resize or cancelling a drag.
  h.root.clientWidth = 0; h.controller.refresh();
  h.root.clientWidth = 1600; h.controller.refresh();
  assert.equal(h.edges.every((edge) => !edge.hidden), true);
});

test("separator keyboard controls respect each edge's direction and announced limits", () => {
  const h = fixture(), initial = h.width();
  assert.equal(h.edges[0].emit("keydown", { key: "ArrowLeft" }).prevented, true);
  assert.ok(h.width() > initial);
  h.edges[1].emit("keydown", { key: "ArrowLeft" }); assert.equal(h.width(), initial);
  h.edges[1].emit("keydown", { key: "End" });
  assert.equal(h.width(), Number(h.edges[1].attributes["aria-valuemax"]));
  h.edges[0].emit("keydown", { key: "Home" });
  assert.equal(h.width(), Number(h.edges[0].attributes["aria-valuemin"]));
  assert.match(h.edges[0].attributes["aria-valuetext"], /对话宽度/);
});

test("cancel, capture loss, Escape, blur and resize restore the uncommitted preference", () => {
  for (const end of ["pointercancel", "lostpointercapture", "Escape", "blur", "resize"]) {
    const h = fixture(), edge = h.edges[1], original = h.width();
    edge.emit("pointerdown"); edge.emit("pointermove", { clientX: 570 });
    assert.ok(h.width() > original);
    if (end === "Escape") edge.emit("keydown", { key: "Escape" });
    else if (end === "blur") h.blur();
    else if (end === "resize") h.resize(1400);
    else edge.emit(end);
    assert.equal(h.width(), original); assert.equal(h.writes.length, 0); assert.equal(h.classes.size, 0);
  }
});

test("foreign pointers and nonprimary gestures cannot move the column", () => {
  const h = fixture(), edge = h.edges[1], original = h.width();
  edge.emit("pointerdown", { button: 2 }); edge.emit("pointermove", { clientX: 600 });
  assert.equal(h.width(), original);
  edge.emit("pointerdown"); edge.emit("pointermove", { pointerId: 2, clientX: 650 });
  assert.equal(h.width(), original);
  edge.emit("pointerup");
});

test("late capture and terminal events from another edge or pointer preserve the current gesture", () => {
  for (const [type, side, pointerId] of [["lostpointercapture", 0, 2], ["lostpointercapture", 1, 1], ["pointerup", 0, 2], ["pointercancel", 0, 2]]) {
    const h = fixture(), oldEdge = h.edges[0], edge = h.edges[1];
    oldEdge.emit("pointerdown"); oldEdge.emit("pointerup");
    const committed = h.writes.length, original = h.width();
    edge.emit("pointerdown", { pointerId: 2 });
    edge.emit("pointermove", { pointerId: 2, clientX: 545 });
    h.edges[side].emit(type, { pointerId });
    assert.equal(h.width(), original + 90);
    assert.equal(h.classes.has("is-resizing-conversation"), true);
    assert.equal(h.writes.length, committed);
    edge.emit("pointerup", { pointerId: 2 });
    assert.equal(h.writes.length, committed + 1);
    assert.equal(fixture(h.writes.at(-1)[1]).width(), original + 90);
  }
});

test("invalid or inaccessible storage is optional, never a conversation failure", () => {
  for (const saved of ["null", "{}", "\"900\"", "-1", "1e99", "broken"])
    assert.equal(fixture(saved).width(), fixture().width());
  const h = fixture();
  const blocked = { getItem() { throw new Error("unavailable"); }, setItem() { throw new Error("unavailable"); } };
  initializeConversationWidth(h.root, { storage: blocked, host: { addEventListener() {} } });
  assert.doesNotThrow(() => { h.edges[1].emit("keydown", { key: "ArrowRight" }); });
});

test("both documents and the shared stylesheet keep all conversation surfaces on one width", async () => {
  for (const path of ["index.html", "preview.html"]) {
    const html = await readFile(new URL(`../../web/${path}`, import.meta.url), "utf8");
    assert.equal((html.match(/data-conversation-edge=/g) ?? []).length, 2);
    assert.equal((html.match(/role="separator"/g) ?? []).length, 2);
  }
  const css = await readFile(new URL("../../web/styles.css", import.meta.url), "utf8");
  for (const selector of ["messages", "composer-area", "run-status", "approval-region"])
    assert.match(css, new RegExp(`\\.${selector} \\{[^}]*max-width: var\\(--conversation-width\\)`));
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /\.conversation-edge:focus-visible/);
});

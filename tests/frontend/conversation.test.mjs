import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import * as sessionState from "../../web/state.mjs";
import { harness } from "./helpers/conversation-dom-harness.mjs";

test("the real chat renderer interleaves owner, reasoning, tool and reply in one timeline", () => {
  const h = harness(); h.start();
  h.emit("reasoning.delta", { content: "先读取资料。" });
  h.emit("tool.started", { run_id: "run-1", call_id: "read-1", name: "fs_read", arguments: { path: "notes.txt" } });
  h.emit("tool.completed", { run_id: "run-1", call_id: "read-1", status: "completed", result: { text: "笔记正文" } });
  h.emit("message.delta", { content: "已阅读笔记。" }); h.render();
  assert.deepEqual(h.messages.children.map((node) => node.tagName), ["ARTICLE", "DETAILS", "DETAILS", "ARTICLE"]);
  assert.deepEqual(h.messages.children.map((node) => node.dataset.timelineId), sessionState.conversationItems(h.ui.state.session).map((item) => String(item.id)));
  const [owner, reasoning, tool, reply] = h.messages.children;
  assert.equal(owner.matches("article.message.user"), true); assert.equal(owner.dataset.messageId, "owner-1");
  assert.equal(reasoning.matches("details.reasoning-step"), true); assert.equal(reasoning.open, true);
  const content = reasoning.querySelector(".reasoning-content");
  assert.equal(content.textContent, "先读取资料。"); assert.equal(content.getAttribute("role"), "region"); assert.equal(content.tabIndex, 0);
  assert.equal(tool.matches("details.tool-step.completed"), true); assert.equal(tool.open, false);
  assert.equal(tool.querySelector(".tool-name").textContent, "fs_read");
  assert.ok(tool.querySelector(".tool-step-status").textContent.length > 0);
  assert.ok(tool.querySelector(".tool-arguments").textContent.includes("notes.txt"));
  assert.ok(tool.querySelector(".tool-result").textContent.includes("笔记正文"));
  assert.equal(reply.matches("article.message.assistant"), true); assert.equal(reply.querySelector(".message-body").textContent, "已阅读笔记。");
  assert.equal(reply.querySelector(".message-avatar img").getAttribute("src"), "./assets/muli-avatar.webp");
});

test("tool completion updates the existing details without resetting expansion or summary focus", () => {
  const h = harness(); h.start();
  h.emit("tool.started", { run_id: "run-1", call_id: "read-1", name: "fs_read", arguments: { path: "notes.txt" } }); h.render();
  const tool = h.messages.querySelector(".tool-step"), summary = tool.querySelector("summary");
  assert.equal(tool.open, false); assert.equal(tool.classList.contains("running"), true);
  tool.open = true; summary.focus(); const insertions = h.messages.insertions;
  h.emit("tool.completed", { run_id: "run-1", call_id: "read-1", status: "completed", result: { text: "<script>不执行</script>" } }); h.render();
  assert.equal(h.messages.querySelector(".tool-step") === tool, true);
  assert.equal(tool.querySelector("summary") === summary, true); assert.equal(h.document.activeElement === summary, true);
  assert.equal(tool.open, true); assert.equal(tool.classList.contains("completed"), true); assert.equal(tool.classList.contains("running"), false);
  assert.equal(h.messages.insertions, insertions, "unchanged timeline order must not move focused subtrees");
  assert.ok(tool.querySelector(".tool-result").textContent.includes("<script>不执行</script>"));
  assert.equal(tool.querySelectorAll("script").length, 0);
  tool.open = false; h.render(); assert.equal(tool.open, false, "a later update must also preserve explicit collapse");
});

test("streamed reasoning preserves upward reading and focus, then follows only after returning to its bottom", () => {
  const h = harness(); h.start(); h.emit("reasoning.delta", { content: "甲".repeat(1000) }); h.render();
  const reasoning = h.messages.querySelector(".reasoning-step"), content = reasoning.querySelector(".reasoning-content");
  content.clientHeight = 100; content.scrollTop = 200; content.dispatch("scroll"); content.focus();
  h.emit("reasoning.delta", { content: "乙".repeat(200) }); h.render();
  assert.equal(h.messages.querySelector(".reasoning-content") === content, true);
  assert.equal(content.scrollTop, 200); assert.equal(h.document.activeElement === content, true);
  content.scrollTop = content.scrollHeight; content.dispatch("scroll");
  h.emit("reasoning.delta", { content: "丙".repeat(200) }); h.render();
  assert.equal(content.scrollTop, content.scrollHeight - content.clientHeight);
  reasoning.open = false;
  const collapsedPosition = content.scrollTop;
  assert.equal(content.clientHeight, 0);
  h.emit("reasoning.delta", { content: "丁" }); h.render();
  assert.equal(reasoning.open, false, "streaming must not reopen a user-collapsed thought");
  assert.equal(content.scrollTop, collapsedPosition, "a collapsed thought must not scroll invisibly");
});

test("the first long reasoning delta follows its tail only after the real renderer attaches it", () => {
  const h = harness(); h.start();
  const detached = h.document.createElement("details"); detached.open = true;
  const probe = h.document.createElement("div"); probe.className = "reasoning-content";
  detached.append(probe);
  assert.equal(probe.clientHeight, 0, "detached nodes must not offer fake layout to the renderer");
  h.emit("reasoning.delta", { content: "初次长思考".repeat(200) }); h.render();
  const content = h.messages.querySelector(".reasoning-content");
  assert.ok(content.clientHeight > 0); assert.ok(content.scrollHeight > content.clientHeight);
  assert.equal(content.scrollTop, content.scrollHeight - content.clientHeight);
  h.emit("tool.started", { run_id: "run-1", call_id: "read-1", name: "fs_read", arguments: { path: "notes.txt" } }); h.render();
  assert.equal(h.messages.querySelector(".reasoning-content") === content, true);
  assert.equal(content.scrollTop, content.scrollHeight - content.clientHeight, "no second reasoning delta is needed to reach the tail");
});

test("a completed historical thought starts at its beginning rather than inheriting streaming follow", () => {
  const h = harness(); h.start();
  h.emit("reasoning.delta", { content: "已完成的历史思考".repeat(200) });
  h.emit("run.completed", { run_id: "run-1" }); h.render();
  const reasoning = h.messages.querySelector(".reasoning-step"), content = reasoning.querySelector(".reasoning-content");
  assert.equal(reasoning.open, true); assert.equal(reasoning.classList.contains("streaming"), false);
  assert.ok(content.clientHeight > 0); assert.ok(content.scrollHeight > content.clientHeight);
  assert.equal(content.scrollTop, 0);
});

test("new timeline content leaves the main history reader in place and follows an already-bottom reader", () => {
  const h = harness(); h.start(); h.emit("message.delta", { content: "答".repeat(1200) }); h.render(); h.flushFrames();
  h.scroll.dispatch("wheel", { deltaY: -120 });
  h.scroll.scrollTop = 100; h.scroll.dispatch("scroll"); h.scroll.scrollCalls.length = 0;
  h.emit("message.delta", { content: "续".repeat(300) }); h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, 100); assert.equal(h.scroll.scrollCalls.length, 0);
  h.scroll.scrollTop = h.scroll.scrollHeight; h.scroll.dispatch("scroll");
  h.emit("message.delta", { content: "尾".repeat(300) }); h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollCalls.length, 1);
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight);
});

test("an independently arriving real approval stays at the tail even when its height exceeds the old near-bottom threshold", () => {
  const h = harness({ realApprovals: true }); h.start();
  h.emit("message.delta", { content: "答".repeat(1100) }); h.render(); h.flushFrames();
  const before = h.scroll.scrollHeight;
  h.ui.state.approvals = [{ id: "approval-1", session_id: "main", run_id: "run-1", tool: "fs_write", risk: "write", arguments: { content: "待批参数".repeat(100) } }];
  h.ui.renderApprovalViews(); h.flushFrames();
  assert.equal(h.document.querySelectorAll(".approval-item").length, 1);
  assert.ok(h.scroll.scrollHeight - before > 100, "the actual approval renderer must change scroll geometry first");
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight);
  h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight);
});

test("composer layout and later viewport resize follow at the end of the frame without a new message", () => {
  const h = harness(); h.start(); h.emit("message.delta", { content: "答".repeat(1100) });
  h.render(); h.flushFrames();
  h.onComposer(() => { h.scroll.clientHeight = 80; });
  h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight, "composer changes happen after the old synchronous scroll");
  h.scroll.clientHeight = 40; h.resized(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight, "asynchronous geometry alone must keep the tail visible");
});

test("layout-generated scroll does not leave following; a real upward gesture pauses until the user returns to the tail", () => {
  const h = harness(); h.start(); h.emit("message.delta", { content: "答".repeat(1100) });
  h.render(); h.flushFrames();
  h.scroll.scrollTop = 100; h.scroll.dispatch("scroll", { isTrusted: true });
  h.emit("message.delta", { content: "续".repeat(200) }); h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight, "a scroll event without a user gesture cannot revoke follow mode");
  h.scroll.dispatch("wheel", { deltaY: -120 });
  h.scroll.scrollTop = 100; h.scroll.dispatch("scroll");
  h.emit("message.delta", { content: "停".repeat(200) }); h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, 100);
  h.scroll.dispatch("wheel", { deltaY: 1000 });
  h.scroll.scrollTop = h.scroll.scrollHeight; h.scroll.dispatch("scroll");
  h.emit("message.delta", { content: "尾".repeat(200) }); h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight);
});

test("scrolling upward inside the real reasoning pane does not cancel main conversation following", () => {
  const h = harness(); h.start(); h.emit("reasoning.delta", { content: "思".repeat(1100) });
  h.render(); h.flushFrames();
  const content = h.messages.querySelector(".reasoning-content");
  content.dispatch("wheel", { deltaY: -120, bubbles: true });
  content.scrollTop = 100; content.dispatch("scroll");
  h.emit("reasoning.delta", { content: "续".repeat(200) }); h.render(); h.flushFrames();
  assert.equal(content.scrollTop, 100, "the nested reader keeps its own position");
  assert.equal(h.scroll.scrollTop, h.scroll.scrollHeight - h.scroll.clientHeight, "only the main viewport's own user gesture may pause it");
});

test("a real upward wheel scroll still pauses when streaming and approval geometry change in the same frame", () => {
  const h = harness({ realApprovals: true }); h.start();
  h.emit("message.delta", { content: "答".repeat(1100) }); h.render(); h.flushFrames();
  const previousHeight = h.scroll.scrollHeight, previousTop = h.scroll.scrollTop;
  h.scroll.dispatch("wheel", { deltaY: -120 });
  h.emit("message.delta", { content: "并发流式内容".repeat(80) });
  h.ui.state.approvals = [{ id: "approval-during-wheel", session_id: "main", run_id: "run-1", tool: "fs_write", risk: "write", arguments: { content: "同时到达的待批参数".repeat(70) } }];
  h.render();
  assert.ok(h.scroll.scrollHeight - previousHeight > 100);
  assert.equal(h.document.querySelectorAll(".approval-item").length, 1);
  const readingTop = previousTop - 120;
  h.scroll.scrollTop = readingTop; h.scroll.dispatch("scroll");
  assert.equal(h.scroll.scrollTop, readingTop, "the real main viewport actually moved upward before frame settlement");
  h.scroll.scrollCalls.length = 0; h.resized(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, readingTop, "layout growth cannot erase a simultaneous real upward gesture");
  assert.equal(h.scroll.scrollCalls.length, 0);
  h.emit("message.delta", { content: "仍在生成".repeat(50) }); h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, readingTop);
});

test("PageUp from a focused tool summary, message button or link pauses the main viewport it actually scrolls", () => {
  for (const kind of ["summary", "button", "a"]) {
    const h = harness(); h.start();
    h.emit("message.delta", { content: "答".repeat(1100) });
    h.emit("tool.started", { run_id: "run-1", call_id: "read-keyboard", name: "fs_read", arguments: { path: "notes.txt" } });
    h.render(); h.flushFrames();
    let target;
    if (kind === "summary") target = h.messages.querySelector(".tool-step summary");
    else if (kind === "button") target = h.messages.querySelector(".message-actions button");
    else {
      target = h.document.createElement("a"); target.setAttribute("href", "#message-input");
      target.textContent = "正文中的链接"; h.messages.querySelector(".message-body").append(target);
      h.resized(); h.flushFrames();
    }
    assert.ok(target); target.focus();
    const readingTop = h.scroll.scrollTop - 120;
    target.dispatch("keydown", { key: "PageUp", bubbles: true });
    // The DOM harness supplies browser geometry/default scrolling only. The
    // actual registered key and scroll handlers decide whether following stops.
    h.scroll.scrollTop = readingTop; h.scroll.dispatch("scroll");
    assert.equal(h.scroll.scrollTop, readingTop);
    h.scroll.scrollCalls.length = 0;
    h.emit("message.delta", { content: "按键之后继续生成".repeat(50) }); h.render(); h.flushFrames();
    assert.equal(h.scroll.scrollTop, readingTop, `${kind} PageUp must retain the reader's main-history position`);
    assert.equal(h.scroll.scrollCalls.length, 0);
    assert.equal(h.document.activeElement === target, true);
  }
});

test("an upward wheel over a fitting tool result pauses the main viewport it actually scrolls", () => {
  const h = harness(); h.start();
  h.emit("message.delta", { content: "答".repeat(1100) });
  h.emit("tool.started", { run_id: "run-1", call_id: "read-scroll", name: "fs_read", arguments: { path: "notes.txt" } });
  h.emit("tool.completed", { run_id: "run-1", call_id: "read-scroll", status: "completed", result: { text: "短结果" } });
  h.render();
  const tool = h.messages.querySelector(".tool-step"), result = tool.querySelector(".tool-result");
  tool.open = true;
  // The real tool renderer creates this pre. A short result fits below its CSS
  // height cap, so the browser's upward wheel default scrolls the main viewport.
  result.clientHeight = result.scrollHeight;
  assert.equal(result.tagName, "PRE");
  assert.equal(result.scrollHeight, result.clientHeight);
  h.resized(); h.flushFrames();
  const readingTop = h.scroll.scrollTop - 120;
  result.dispatch("wheel", { deltaY: -120, bubbles: true });
  h.scroll.scrollTop = readingTop; h.scroll.dispatch("scroll");
  assert.equal(h.scroll.scrollTop, readingTop, "the main viewport really moved upward; the fitting pre did not scroll");
  assert.equal(result.scrollTop, 0);
  h.scroll.scrollCalls.length = 0;
  h.emit("message.delta", { content: "代码块上滚之后继续生成".repeat(50) }); h.render(); h.flushFrames();
  assert.equal(h.scroll.scrollTop, readingTop, "a pre target cannot hide an actual upward main-history scroll");
  assert.equal(h.scroll.scrollCalls.length, 0);
  assert.equal(result.scrollTop, 0);
});

test("the registered mode change handler finishes without the removed timeline renderer", async () => {
  const h = harness(); h.ui.state.activeSession = null;
  const mode = h.document.querySelector("#mode-select");
  assert.equal(mode.events.change.length, 1, "exercise the actual application listener");
  mode.value = "yolo";
  await mode.dispatch("change");
  assert.equal(h.ui.state.mode, "yolo");
});

test("both real HTML entry points omit the removed side timeline and standalone reasoning region", async () => {
  for (const entry of ["index.html", "preview.html"]) {
    const html = await readFile(new URL(`../../web/${entry}`, import.meta.url), "utf8");
    assert.match(html, /\bid=["']messages["']/, `${entry} retains the shared central timeline`);
    assert.doesNotMatch(html, /<aside\b/i, entry);
    assert.doesNotMatch(html, /\bid=["'](?:timeline(?:-[\w-]+)?|reasoning-region)["']/i, entry);
    assert.doesNotMatch(html, /\bdata-action=["'](?:toggle-timeline|toggle-reasoning)["']/i, entry);
    assert.doesNotMatch(html, /\bdata-prompt\b|\bstarter-prompts\b/i, entry);
    assert.match(html, /<link\b[^>]*rel="icon"[^>]*href="\.\/assets\/muli-icon\.png"[^>]*type="image\/png"/, entry);
    assert.match(html, /<img\b[^>]*src="\.\/assets\/muli-avatar\.webp"/, entry);
    assert.match(html, /<img\b[^>]*class="welcome-illustration"[^>]*src="\.\/assets\/muli-empty\.webp"[^>]*width="380"[^>]*height="253"/, entry);
    assert.doesNotMatch(html, /muli-(?:avatar|empty)\.svg/, entry);
  }
});

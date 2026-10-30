import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import * as sessionState from "../../../web/state.mjs";
import { VoiceSetup, voiceSetupLabel } from "../../../web/voice-setup.mjs";
import { attachmentKind, attachmentURL, formatBytes } from "../../../web/attachments.mjs";
import { composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment } from "../../../web/composer-draft.mjs";

const app = await readFile(new URL("../../../web/app.mjs", import.meta.url), "utf8");
const application = app.slice(app.indexOf("const ICONS ="), app.indexOf("\nfillIcons();"));

// Shared from conversation.test.mjs. Geometry remains deterministic text-sized
// layout; the application still renders every conversation and dialog node.
class TextNode {
  constructor(text) { this.textContent = String(text); this.parentNode = null; }
}

class DOMElement {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.document = document; this.parentNode = null; this.childNodes = [];
    this.dataset = {}; this.attributes = {}; this.events = {}; this.className = "";
    this.hidden = false; this.disabled = false; this.open = false; this.tabIndex = -1;
    this.value = ""; this.style = {};
    this._clientHeight = 0; this._scrollTop = 0; this.scrollCalls = []; this.insertions = 0;
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const classes = new Set(this.className.split(/\s+/).filter(Boolean));
        const enabled = force ?? !classes.has(name);
        if (enabled) classes.add(name); else classes.delete(name);
        this.className = [...classes].join(" "); return enabled;
      },
      add: (...names) => names.forEach((name) => this.classList.toggle(name, true)),
      remove: (...names) => names.forEach((name) => this.classList.toggle(name, false)),
    };
  }
  get children() { return this.childNodes.filter((node) => node instanceof DOMElement); }
  get firstChild() { return this.childNodes[0] ?? null; }
  get firstElementChild() { return this.children[0] ?? null; }
  get nextSibling() { return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this) + 1] ?? null; }
  get isConnected() { return this.document.body.contains(this); }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(text) { this.replaceChildren(new TextNode(text)); }
  set innerHTML(value) { this.iconHTML = value; }
  get clientHeight() {
    if (!this.document.body?.contains(this)) return 0;
    if (this.classList.contains("reasoning-content")) {
      if (!this.closest("details")?.open) return 0;
      return this._clientHeight || 240;
    }
    return this._clientHeight;
  }
  set clientHeight(value) { this._clientHeight = value; }
  get scrollHeight() { return Math.max(this.clientHeight, this.textContent.length); }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(value) { this._scrollTop = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); }
  scrollTo(options) { this.scrollCalls.push(options); this.scrollTop = options.top; }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  focus() { this.document.activeElement = this; }
  detach(node) {
    const parent = node.parentNode;
    if (!parent) return;
    if (node === this.document.activeElement || node.contains?.(this.document.activeElement)) this.document.activeElement = this.document.body;
    parent.childNodes.splice(parent.childNodes.indexOf(node), 1); node.parentNode = null;
  }
  append(...nodes) {
    for (let node of nodes) {
      if (!(node instanceof DOMElement) && !(node instanceof TextNode)) node = new TextNode(node);
      this.detach(node); node.parentNode = this; this.childNodes.push(node);
    }
  }
  insertBefore(node, reference) {
    if (reference !== null && reference.parentNode !== this) throw new Error("reference is not a child");
    if (node === reference) return node;
    this.insertions += 1; this.detach(node);
    const position = reference === null ? this.childNodes.length : this.childNodes.indexOf(reference);
    node.parentNode = this; this.childNodes.splice(position, 0, node); return node;
  }
  remove() { this.detach(this); }
  replaceChildren(...nodes) {
    for (const node of [...this.childNodes]) this.detach(node);
    this.append(...nodes);
  }
  setAttribute(key, value) {
    if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    else this.attributes[key] = String(value);
  }
  getAttribute(key) {
    if (key.startsWith("data-")) return this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
    return this.attributes[key] ?? this[key];
  }
  addEventListener(name, callback) { (this.events[name] ??= []).push(callback); }
  dispatch(name, payload = {}) {
    const event = { target: this, currentTarget: this, isTrusted: true, preventDefault() {}, ...payload };
    const results = [];
    let current = this;
    while (current) {
      event.currentTarget = current;
      results.push(...(current.events[name] ?? []).map((callback) => callback(event)));
      current = payload.bubbles ? current.parentNode : null;
    }
    return Promise.all(results);
  }
  matches(selector) {
    if (selector.includes(",")) return selector.split(",").some((part) => this.matches(part.trim()));
    const tag = selector.match(/^[a-z][\w-]*/i)?.[0];
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/)?.[1];
    if (id && this.getAttribute("id") !== id) return false;
    if ([...selector.matchAll(/\.([\w-]+)/g)].some(([, name]) => !this.classList.contains(name))) return false;
    return [...selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)].every(([, key, value]) => value === undefined ? this.getAttribute(key) !== undefined : String(this.getAttribute(key)) === value);
  }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap((node) => [node, ...node.querySelectorAll("*")]);
    return descendants.filter((node) => selector.split(",").some((group) => {
      const parts = group.trim().split(/\s+/); let current = node;
      if (!current.matches(parts.pop())) return false;
      while (parts.length) {
        const part = parts.pop(); current = current.parentNode;
        while (current && !current.matches(part)) current = current.parentNode;
        if (!current) return false;
      }
      return true;
    }));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector) ?? null; }
  showModal() { this.open = true; }
  close() { if (!this.open) return; this.open = false; queueMicrotask(() => void this.dispatch("close")); }
  play() { this.document.playCalls += 1; return Promise.resolve(); }
}

export const flush = () => new Promise((resolve) => setImmediate(resolve));
export const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

export function harness({ realApprovals = false, interactive = false, handler = null } = {}) {
  const document = {
    activeElement: null, events: {}, playCalls: 0, visibilityState: "visible",
    addEventListener(name, callback) { if (interactive) (this.events[name] ??= []).push(callback); },
    createTextNode: (text) => new TextNode(text),
    createElement: (tag) => new DOMElement(tag, document),
    createElementNS: (_namespace, tag) => new DOMElement(tag, document),
  };
  document.body = document.createElement("body"); document.activeElement = document.body;
  document.querySelector = (selector) => document.body.querySelector(selector);
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  document.contains = (node) => document.body.contains(node);
  const node = (tag, id, parent = document.body) => {
    const result = document.createElement(tag); result.setAttribute("id", id); parent.append(result); return result;
  };
  const scroll = node("div", "conversation-scroll"); scroll.clientHeight = 200;
  const messages = node("div", "messages", scroll);
  if (realApprovals) node("div", "approval-region", scroll);
  for (const id of ["welcome", "scroll-bottom", "run-status", "composer", "login-form", "message-input", "mode-select", "file-input"]) node("div", id);
  if (interactive) {
    const dialog = node("dialog", "app-dialog"); node("h2", "dialog-title", dialog);
    const form = node("form", "dialog-form", dialog); node("div", "dialog-content", form); node("p", "dialog-error", form);
    const actions = node("div", "dialog-actions", form); actions.className = "dialog-actions";
    const close = node("button", "dialog-close", actions); close.dataset.action = "close-dialog";
    const submit = node("button", "dialog-submit", actions); submit.type = "submit";
    for (const id of ["other-view", "auth-gate", "login-password", "composer-attachments", "voice-draft-text", "toast-region"]) node("div", id);
    node("div", "app-shell").className = "app-shell";
  } else for (const id of ["dialog-form", "app-dialog"]) node("div", id);
  const unexpectedRequest = () => { throw new Error("renderer must not call a service"); };
  const requests = [], frames = new Map(), resizeObservers = [], windowEvents = {};
  let nextFrame = 0, composerLayout = () => {};
  const requestAnimationFrame = (callback) => { frames.set(++nextFrame, callback); return nextFrame; };
  const cancelAnimationFrame = (identifier) => frames.delete(identifier);
  class ResizeObserver {
    constructor(callback) { this.callback = callback; this.targets = new Set(); resizeObservers.push(this); }
    observe(target) { this.targets.add(target); }
    unobserve(target) { this.targets.delete(target); }
    disconnect() { this.targets.clear(); }
  }
  const context = {
    composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment,
    ...sessionState, document, console, AbortController, TextEncoder, URLSearchParams, queueMicrotask,
    Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener: (name, callback) => { (windowEvents[name] ??= []).push(callback); }, requestAnimationFrame, cancelAnimationFrame },
    requestAnimationFrame, cancelAnimationFrame, ResizeObserver,
    composerLayout: () => composerLayout(),
    attachmentKind, attachmentURL, formatBytes,
    createTeamPager: () => ({}), captureTeamView: () => null, restoreTeamView() {},
    VoiceInput: class { cancel() {} }, VoiceSetup, voiceSetupLabel, uploadFile: unexpectedRequest, transcribeAttachment: unexpectedRequest,
    apiRequest: handler ? async (path, options = {}) => { requests.push({ path, ...options }); return handler(path, options); } : unexpectedRequest,
    readEvents: unexpectedRequest,
    sessionStorage: {}, setTimeout: () => 0, clearTimeout() {},
    renderMarkdown: (text) => { const result = document.createElement("p"); result.textContent = text; return result; },
  };
  runInNewContext(application + `
    renderChrome = () => {}; renderComposer = () => composerLayout();
    ${realApprovals ? "" : "renderApprovals = () => {};"}
    const activateConnection = () => {
      const owner = beginConnection(); owner.ready = true;
      state.connected = true; state.streamConnected = true; state.auth.authenticated = true; state.loading = false;
      return owner;
    };
    globalThis.ui = { state, renderChat, renderApprovalViews, handleAction, captureDraft, renderComposerAttachments, activateConnection, switchView };
  `, context);
  const ui = context.ui; ui.state.view = "chat"; ui.state.activeSession = { id: "main" };
  if (interactive) { ui.state.mainSessionId = "main"; ui.activateConnection(); }
  let sequence = 0;
  const emit = (type, data) => {
    sequence += 1;
    ui.state.session = sessionState.applyEvent(ui.state.session, { id: String(sequence), type, data, session_id: "main", created_at: sequence });
  };
  const start = () => {
    emit("message.completed", { message: { id: "owner-1", role: "user", principal: "owner", content: "请查看笔记" } });
    emit("run.started", { run_id: "run-1" });
  };
  return {
    ui, document, scroll, messages, emit, start, requests, windowEvents,
    dialog: document.querySelector("#app-dialog"), form: document.querySelector("#dialog-form"), render: () => ui.renderChat(),
    async click(button) {
      assert.ok(button?.matches("button"), "the real renderer must supply the requested action button");
      if (button.disabled) return;
      await button.dispatch("click");
      for (const callback of document.events.click ?? []) await callback({ target: button, preventDefault() {} });
      await flush();
    },
    onComposer: (callback) => { composerLayout = callback; },
    flushFrames() {
      for (let cycle = 0; frames.size && cycle < 8; cycle += 1) {
        const current = [...frames.values()]; frames.clear(); current.forEach((callback) => callback());
      }
      assert.equal(frames.size, 0, "following must settle without an animation-frame polling loop");
    },
    resized() {
      for (const observer of resizeObservers) if (observer.targets.size) observer.callback([...observer.targets].map((target) => ({ target })), observer);
      for (const callback of windowEvents.resize ?? []) callback({ target: context.window });
    },
  };
}

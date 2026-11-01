import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { attachmentKind, attachmentURL, formatBytes } from "../../../web/attachments.mjs";
import { CHANNELS, capabilityLabel, loadSettingsResources, managementRecoveryView, updateLabel } from "../../../web/management.mjs";
import { setPath, mergeSettingsExtras, settingsExtras, settingsPatch, changesListener, SETTINGS_GROUPS } from "../../../web/settings.mjs";
import { MODES, applyEvent, createSessionState, hydrateSession, isRunning, parseJSONField } from "../../../web/state.mjs";
import { mergeTranscript } from "../../../web/voice-input.mjs";
import { VoiceSetup, voiceSetupLabel } from "../../../web/voice-setup.mjs";
import { captureTeamView, createTeamPager, restoreTeamView } from "../../../web/team.mjs";
import { createTaskControl, taskControlView } from "../../../web/task-control.mjs";
import { composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment } from "../../../web/composer-draft.mjs";
import { applyComposerPrompt, REPLACE_CONFIRMATION } from "../../../web/composer-prompt.mjs";

// Run the actual application functions and registered dialog close handler.
// No browser library, server, model, account or real database is involved.
// Compare DOM/slot identity through booleans: failed object diffs traverse the cyclic mock graph.
const app = await readFile(new URL("../../../web/app.mjs", import.meta.url), "utf8");
const index = await readFile(new URL("../../../web/index.html", import.meta.url), "utf8");
const application = app.slice(app.indexOf("const ICONS ="), app.indexOf("\nfillIcons();"));
export const flush = () => new Promise((resolve) => setImmediate(resolve));
export const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

export class TextNode {
  constructor(text) { this.textContent = String(text); this.parentNode = null; }
}

export class DOMElement {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.document = document; this.ownerDocument = document; this.childNodes = []; this.parentNode = null;
    this.dataset = {}; this.attributes = {}; this.events = {}; this.className = "";
    this._value = ""; this.name = ""; this.type = ""; this.hidden = false; this.disabled = false; this.open = false; this.style = {};
    if (this.tagName === "INPUT") this.checked = false;
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  get children() { return this.childNodes.filter((node) => node instanceof DOMElement); }
  get value() {
    if (this.tagName !== "SELECT") return this._value;
    const options = this.children.filter((node) => node.tagName === "OPTION");
    return this.selectionSet ? options.find((option) => option.value === this._value)?.value ?? "" : options[0]?.value ?? "";
  }
  set value(value) { this._value = String(value); this.selectionSet = true; }
  get firstElementChild() { return this.children[0] ?? null; }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(text) { this.replaceChildren(new TextNode(text)); }
  set innerHTML(value) { this.iconHTML = value; }
  append(...children) {
    for (let child of children) {
      if (!(child instanceof DOMElement) && !(child instanceof TextNode)) child = new TextNode(child);
      child.parentNode = this; this.childNodes.push(child);
    }
  }
  replaceChildren(...children) {
    if (this.children.some((child) => child.contains(this.document.activeElement))) this.document.activeElement = this.document.body;
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = []; this.append(...children);
  }
  setAttribute(key, value) {
    if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    else this.attributes[key] = String(value);
  }
  getAttribute(key) {
    if (key.startsWith("data-")) return this.dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
    return this.attributes[key] ?? this[key];
  }
  addEventListener(name, handler) { (this.events[name] ??= []).push(handler); }
  async dispatch(name) { for (const handler of this.events[name] ?? []) await handler({ target: this, preventDefault() {} }); }
  contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
  focus() { this.document.activeElement = this; }
  matches(selector) {
    const excluded = selector.match(/:not\(([^)]+)\)/);
    if (excluded && this.matches(excluded[1])) return false;
    selector = selector.replace(/:not\([^)]+\)/g, "");
    const tag = selector.match(/^[a-z][\w-]*/i)?.[0];
    if (tag && this.tagName !== tag.toUpperCase()) return false;
    const id = selector.match(/#([\w-]+)/)?.[1];
    if (id && this.getAttribute("id") !== id) return false;
    if ([...selector.matchAll(/\.([\w-]+)/g)].some(([, name]) => !this.className.split(/\s+/).includes(name))) return false;
    return [...selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)].every(([, key, value]) => value === undefined ? this.getAttribute(key) !== undefined : this.getAttribute(key) === value);
  }
  querySelectorAll(selector) {
    const all = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
    return all.filter((node) => selector.split(",").some((group) => {
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
}

export class DOMButton extends DOMElement {}

export function harness(handler = async () => { throw new Error("unexpected request"); }, { composer = false, team = false, upload = () => {}, transcribe = () => {}, createVoiceInput = null, voiceControls = false, storage = null } = {}) {
  const document = { events: {}, activeElement: null, createTextNode: (text) => new TextNode(text), addEventListener(name, callback) { (this.events[name] ??= []).push(callback); } };
  document.createElement = (tag) => tag === "button" ? new DOMButton(tag, document) : new DOMElement(tag, document);
  document.createElementNS = (_namespace, tag) => document.createElement(tag);
  document.body = document.createElement("body");
  document.querySelector = (selector) => document.body.querySelector(selector);
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  const node = (tag, id, parent = document.body) => { const value = document.createElement(tag); value.setAttribute("id", id); parent.append(value); return value; };
  const dialog = node("dialog", "app-dialog"); node("h2", "dialog-title", dialog);
  const form = node("form", "dialog-form", dialog); node("div", "dialog-content", form); node("p", "dialog-error", form);
  const actions = node("div", "dialog-actions", form); actions.className = "dialog-actions";
  const close = node("button", "dialog-close", actions); close.dataset.action = "close-dialog"; node("button", "dialog-submit", actions);
  const other = node("div", "other-view"); const shell = node("div", "app-shell"); shell.className = "app-shell";
  node("div", "approval-region");
  node("div", "auth-gate"); node("input", "login-password");
  for (const id of ["composer", "login-form", "message-input", "mode-select", "file-input", "conversation-scroll"]) node(id === "message-input" ? "textarea" : "div", id);
  if (composer) {
    const input = document.querySelector("#message-input");
    input.maxLength = Number(index.match(/<textarea[^>]*\bid="message-input"[^>]*\bmaxlength="(\d+)"/)?.[1]);
    assert.ok(Number.isSafeInteger(input.maxLength) && input.maxLength > 0);
    input.setAttribute("maxlength", String(input.maxLength));
    input.scrollHeight = 24;
    for (const id of ["send-button", "stop-button", "composer-context", "composer-status", "composer-attachments", "voice-button", "voice-progress", "voice-status", "voice-stop", "voice-draft", "voice-draft-text", "chat-view"]) {
      const control = node(voiceControls && ["voice-button", "voice-stop"].includes(id) ? "button" : "div", id);
      if (voiceControls && control.tagName === "BUTTON") {
        const markup = index.match(new RegExp(`<button\\b[^>]*\\bid="${id}"[^>]*>`))?.[0];
        assert.ok(markup, `missing original voice control ${id}`);
        control.dataset.action = markup.match(/\bdata-action="([^"]+)"/)?.[1];
        assert.ok(control.dataset.action);
      }
    }
    // These cases exercise the real composer and submit handlers, without browser layout.
    node("div", "composer-draft-state"); node("span", "composer-draft-status");
    node("button", "composer-draft-recheck").dataset.action = "retry-composer-attachments";
    node("button", "composer-prompt-undo").dataset.action = "undo-composer-prompt";
    node("button", "composer-draft-discard").dataset.action = "discard-composer-draft";
    document.querySelector("#conversation-scroll").clientHeight = 0;
  }
  const requests = [], windowEvents = {}, savedTaskControls = new Map(); let globalEvent = null, nextMessage = 0, voiceCallbacks = null;
  const taskStorage = {
    getItem: (key) => savedTaskControls.get(key) ?? null,
    setItem(key, value) { savedTaskControls.set(key, String(value)); taskStorage[key] = String(value); },
    removeItem(key) { savedTaskControls.delete(key); delete taskStorage[key]; },
  };
  const context = {
    document, Node: class { static [Symbol.hasInstance](value) { return value instanceof DOMElement || value instanceof TextNode; } },
    window: { addEventListener: (name, callback) => { windowEvents[name] = callback; } },
    AbortController, TextEncoder, URLSearchParams, structuredClone, console, queueMicrotask,
    crypto: { randomUUID: () => `memory-draft-${++nextMessage}` },
    setTimeout: () => 0, clearTimeout() {},
    MODES, applyEvent, hydrateSession, isRunning, attachmentKind, attachmentURL, formatBytes, mergeTranscript, setPath, mergeSettingsExtras, settingsPatch, changesListener, loadSettingsResources, parseJSONField,
    createSessionState: composer ? createSessionState : () => ({ messages: [] }),
    createTeamPager: team ? createTeamPager : () => ({}), captureTeamView: team ? captureTeamView : () => null,
    createTaskControl, taskControlView,
    composerIdentity, createComposerDraft, MAX_DRAFT_ATTACHMENTS, sameComposerIdentity, validDraftAttachment,
    applyComposerPrompt, REPLACE_CONFIRMATION,
    uploadFile: upload, transcribeAttachment: transcribe,
    VoiceInput: class { constructor(callbacks) { voiceCallbacks = callbacks; if (createVoiceInput) return createVoiceInput(callbacks); } cancel() {} status() { return { phase: "idle" }; } support() { return null; } }, VoiceSetup, voiceSetupLabel, CHANNELS, capabilityLabel, managementRecoveryView, updateLabel, settingsExtras, SETTINGS_GROUPS,
    restoreTeamView: team ? restoreTeamView : () => {},
    sessionStorage: storage ?? (team ? taskStorage : {}),
    formatTimestamp: (value) => value === undefined ? "未记录" : String(value), formatFullDate: String,
    apiRequest: async (path, options = {}) => { requests.push({ path, ...options }); return handler(path, options); },
    readEvents: async (_path, options) => { globalEvent = options.onEvent; await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true })); },
    FormData: class { constructor(root) { this.values = new Map(root.querySelectorAll("[name]").map((input) => [input.name, input.value])); } get(name) { return this.values.get(name) ?? null; } },
  };
  runInNewContext(application + `
    const realOtherView = renderOtherView;
    renderChrome = () => {}; renderOtherView = () => {}; renderChat = () => {}; toast = () => {};
    ${composer ? "renderSessions = () => {};" : ""}
    const activateConnection = () => {
      const owner = beginConnection(); owner.ready = true;
      owner.sessions.start(); owner.notifications.start(); owner.approvals.start(); return owner;
    };
    activateConnection();
    globalThis.ui = { state, renderMemories, renderUploads, showMemoryEvidence, editMemory, openDialog, showLogin, invalidateMemoryEvidence, loadMemorySource, loadMemoryHistory, switchView, handleAction, startGlobalEvents, searchMemories, loadMemoryWatches, captureDraft, captureVoiceDraft, renderComposerAttachments, renderConfigurationOverview,
      activateConnection, connect,
      get capabilityCatalogSlot() { return capabilityCatalog; },
      watchPending(id) { return memoryWatchPending.has(id); },
      useMemoryViews() { renderOtherView = () => { if (state.view === "memories") realOtherView(); }; },
      useUploadViews() { renderOtherView = () => { if (state.view === "uploads") realOtherView(); }; },
      useTeamViews() { renderOtherView = () => { if (state.view === "team") realOtherView(); }; },
      useNotificationViews() { renderOtherView = () => { if (state.view === "notifications") realOtherView(); }; },
      revalidateVisibleState() { revalidateVisibleState(connection); },
      useSettingsViews() { renderOtherView = () => { if (state.view === "settings") realOtherView(); }; renderOtherView(); },
      get taskDirectorySlot() { return typeof taskDirectory === "undefined" ? null : taskDirectory; },
      get taskDetailSlot() { return typeof taskDirectoryDialog === "undefined" ? null : taskDirectoryDialog; },
      get taskControlSlot() { return taskDialog; }, get taskControls() { return taskControlModels; },
      get notificationDirectorySlot() { return typeof notificationDirectory === "undefined" ? null : notificationDirectory; },
      get notificationDetailSlot() { return typeof notificationDetail === "undefined" ? null : notificationDetail; },
      get historySlot() { return historyDialog; },
      stopEvents() { globalController?.abort(); },
      get channelDeliverySlot() { return channelDeliveryDialog; },
      get slot() { return memoryDialog; }, get path() { return memoryPath; }, get catalog() { return memorySources; }, get watches() { return memoryWatches; }, get submit() { return dialogSubmit; } };
  `, context);
  const ui = context.ui; ui.state.view = "memories"; ui.state.auth.authenticated = true;
  const body = () => document.querySelector("#dialog-content").textContent;
  const button = (text, root = dialog) => root.querySelectorAll("button").find((entry) => entry.textContent === text);
  const expand = async (text) => { const details = dialog.querySelectorAll("details").find((entry) => entry.firstElementChild.textContent === text); assert.ok(details, `missing ${text}`); details.open = true; await details.dispatch("toggle"); await flush(); };
  return { ui, requests, document, dialog, form, other, body, button, expand, windowEvents, savedTaskControls, taskStorage, emitGlobal: (event) => globalEvent(event), publishVoiceDraft: (draft) => voiceCallbacks.onDraft({ ...draft, context: { ...ui.captureVoiceDraft(), ...draft.context } }) };
}

export async function clickMemoryButton(h, button) {
  assert.ok(button instanceof DOMButton);
  await button.dispatch("click");
  for (const handler of h.document.events.click ?? []) await handler({ target: button, preventDefault() {} });
  await flush();
}

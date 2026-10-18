import { safeURL } from "./state.mjs";

/** A deliberately small text-first renderer; remote HTML is never evaluated. */
function inline(text) {
  const fragment = document.createDocumentFragment();
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^\s)]+\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    fragment.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    let element;
    if (token.startsWith("`")) {
      element = document.createElement("code");
      element.textContent = token.slice(1, -1);
    } else if (token.startsWith("**")) {
      element = document.createElement("strong");
      element.textContent = token.slice(2, -2);
    } else {
      const parts = token.match(/^\[([^\]]+)\]\((.+)\)$/);
      const href = safeURL(parts[2]);
      element = document.createElement(href ? "a" : "span");
      element.textContent = parts[1];
      if (href) { element.href = href; element.target = "_blank"; element.rel = "noopener noreferrer"; }
    }
    fragment.append(element);
    cursor = match.index + token.length;
  }
  fragment.append(document.createTextNode(text.slice(cursor)));
  return fragment;
}

export function renderMarkdown(text = "") {
  const fragment = document.createDocumentFragment();
  const lines = String(text).split("\n");
  let code = null;
  let list = null;
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (code) { code = null; } else {
        list = null;
        const pre = document.createElement("pre");
        const language = line.slice(3).trim();
        if (language) pre.dataset.language = language;
        code = document.createElement("code");
        pre.append(code);
        fragment.append(pre);
      }
      continue;
    }
    if (code) { code.textContent += `${line}\n`; continue; }
    if (!line.trim()) { list = null; continue; }
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)/);
    if (item) {
      if (!list) { list = document.createElement(/^\s*\d/.test(line) ? "ol" : "ul"); fragment.append(list); }
      const li = document.createElement("li"); li.append(inline(item[1])); list.append(li);
      continue;
    }
    list = null;
    const heading = line.match(/^(#{1,4})\s+(.+)/);
    const quote = line.startsWith("> ");
    const element = document.createElement(heading ? `h${Math.min(heading[1].length + 1, 5)}` : quote ? "blockquote" : "p");
    element.append(inline(heading ? heading[2] : quote ? line.slice(2) : line));
    fragment.append(element);
  }
  return fragment;
}

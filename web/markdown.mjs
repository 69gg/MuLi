import { safeURL } from "./state.mjs";

/** A deliberately small text-first renderer; remote HTML is never evaluated. */

const INLINE_PATTERN = /(`[^`\n]+`|\*\*[^*\n]+\*\*|~~[^~\n]+~~|\*[^\s*](?:[^*\n]*[^\s*])?\*|\[[^\]\n]+\]\([^\s)]+\))/g;
const HEADING_PATTERN = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
const RULE_PATTERN = /^(?:-{3,}|\*{3,}|_{3,})$/;
const LIST_PATTERN = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.+?)\s*$/;

function inline(text) {
  const source = String(text);
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  for (const match of source.matchAll(INLINE_PATTERN)) {
    fragment.append(document.createTextNode(source.slice(cursor, match.index)));
    const token = match[0];
    let element;
    if (token.startsWith("`")) {
      element = document.createElement("code");
      element.textContent = token.slice(1, -1);
    } else if (token.startsWith("**")) {
      element = document.createElement("strong");
      element.append(inline(token.slice(2, -2)));
    } else if (token.startsWith("~~")) {
      element = document.createElement("del");
      element.append(inline(token.slice(2, -2)));
    } else if (token.startsWith("*")) {
      element = document.createElement("em");
      element.append(inline(token.slice(1, -1)));
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
  fragment.append(document.createTextNode(source.slice(cursor)));
  return fragment;
}

function fenceStart(line) {
  const match = line.match(/^\s*(`{3,}|~{3,})([^`]*)$/);
  if (!match) return null;
  const language = match[2].trim().split(/\s+/)[0].replace(/[^\w#+.-]/g, "").slice(0, 32);
  return { fence: match[1], language };
}

function fenceEnd(line, fence) {
  const trimmed = line.trim();
  return trimmed.length >= fence.length && [...trimmed].every((char) => char === fence[0]);
}

function isRule(line) {
  return RULE_PATTERN.test(line);
}

function listItem(line) {
  const match = line.match(LIST_PATTERN);
  if (!match) return null;
  return {
    indent: match[1].replace(/\t/g, "  ").length,
    ordered: /^\d/.test(match[2]),
    start: Number.parseInt(match[2], 10),
    text: match[3],
  };
}

function splitRow(line) {
  let value = line.trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|") && !value.endsWith("\\|")) value = value.slice(0, -1);
  const cells = [];
  let current = "";
  let inCode = false;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (char === "\\" && value[index + 1] === "|") { current += "|"; index += 1; continue; }
    if (char === "`") inCode = !inCode;
    if (char === "|" && !inCode) { cells.push(current.trim()); current = ""; continue; }
    current += char;
  }
  cells.push(current.trim());
  return cells;
}

function tableAlign(cell) {
  const left = cell.startsWith(":");
  const right = cell.endsWith(":");
  if (left && right) return "center";
  if (right) return "right";
  return "left";
}

function tableShape(lines, index) {
  const header = lines[index];
  const separator = lines[index + 1];
  if (!header?.includes("|") || !separator) return null;
  const headCells = splitRow(header);
  const separatorCells = splitRow(separator);
  if (headCells.length < 2 || headCells.length !== separatorCells.length) return null;
  if (!separatorCells.every((cell) => /^:?-+:?$/.test(cell))) return null;
  return { headCells, alignments: separatorCells.map(tableAlign) };
}

function renderTable(lines, index) {
  const shape = tableShape(lines, index);
  if (!shape) return null;
  const table = document.createElement("table");
  table.className = "markdown-table";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const [position, cell] of shape.headCells.entries()) {
    const th = document.createElement("th");
    th.setAttribute("scope", "col");
    th.style.textAlign = shape.alignments[position];
    th.append(inline(cell));
    headRow.append(th);
  }
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  let cursor = index + 2;
  while (cursor < lines.length) {
    const line = lines[cursor];
    if (!line.trim() || !line.includes("|")) break;
    const cells = splitRow(line);
    const row = document.createElement("tr");
    for (let position = 0; position < shape.headCells.length; position += 1) {
      const td = document.createElement("td");
      td.style.textAlign = shape.alignments[position];
      td.append(inline(cells[position] ?? ""));
      row.append(td);
    }
    body.append(row);
    cursor += 1;
  }
  table.append(body);
  const wrapper = document.createElement("div");
  wrapper.className = "markdown-table-wrap";
  wrapper.append(table);
  return { element: wrapper, next: cursor };
}

function isBlockStart(lines, index) {
  const line = lines[index];
  const trimmed = line.trim();
  if (!trimmed) return true;
  if (fenceStart(line) || HEADING_PATTERN.test(trimmed) || isRule(trimmed)) return true;
  if (trimmed.startsWith(">") || listItem(line)) return true;
  return tableShape(lines, index) !== null;
}

function appendListItem(fragment, stack, item) {
  while (stack.length && item.indent < stack[stack.length - 1].indent) stack.pop();
  const tag = item.ordered ? "ol" : "ul";
  let level = stack[stack.length - 1];
  if (!level || item.indent > level.indent || level.tag !== tag) {
    if (level && item.indent === level.indent) stack.pop();
    const list = document.createElement(tag);
    if (item.ordered && item.start !== 1) list.start = item.start;
    const parent = stack[stack.length - 1];
    if (parent) {
      const siblings = parent.list.children;
      const parentItem = siblings[siblings.length - 1];
      (parentItem ?? parent.list).append(list);
    } else {
      fragment.append(list);
    }
    stack.push({ indent: item.indent, tag, list });
    level = stack[stack.length - 1];
  }
  const li = document.createElement("li");
  li.append(inline(item.text));
  level.list.append(li);
}

export function renderMarkdown(text = "") {
  const fragment = document.createDocumentFragment();
  const lines = String(text ?? "").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }

    const fence = fenceStart(line);
    if (fence) {
      const pre = document.createElement("pre");
      if (fence.language) pre.dataset.language = fence.language;
      const code = document.createElement("code");
      pre.append(code);
      index += 1;
      const body = [];
      while (index < lines.length && !fenceEnd(lines[index], fence.fence)) {
        body.push(lines[index]);
        index += 1;
      }
      code.textContent = body.length ? `${body.join("\n")}\n` : "";
      if (index < lines.length) index += 1;
      fragment.append(pre);
      continue;
    }

    const heading = line.trim().match(HEADING_PATTERN);
    if (heading) {
      const element = document.createElement(`h${Math.min(heading[1].length + 1, 6)}`);
      element.append(inline(heading[2]));
      fragment.append(element);
      index += 1;
      continue;
    }

    if (isRule(line.trim())) {
      fragment.append(document.createElement("hr"));
      index += 1;
      continue;
    }

    if (line.trim().startsWith(">")) {
      const quoted = [];
      while (index < lines.length && lines[index].trim().startsWith(">")) {
        quoted.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      const quote = document.createElement("blockquote");
      quote.append(renderMarkdown(quoted.join("\n")));
      fragment.append(quote);
      continue;
    }

    const table = renderTable(lines, index);
    if (table) { fragment.append(table.element); index = table.next; continue; }

    if (listItem(line)) {
      const stack = [];
      while (index < lines.length) {
        const item = listItem(lines[index]);
        if (!item) break;
        appendListItem(fragment, stack, item);
        index += 1;
      }
      continue;
    }

    const paragraph = [];
    while (index < lines.length && !isBlockStart(lines, index)) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    if (!paragraph.length) { index += 1; continue; }
    const element = document.createElement("p");
    for (const [position, value] of paragraph.entries()) {
      if (position) element.append(document.createElement("br"));
      element.append(inline(value));
    }
    fragment.append(element);
  }
  return fragment;
}

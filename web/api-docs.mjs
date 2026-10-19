import { request } from "./api.mjs";
import { initializeTheme } from "./theme.mjs";

const METHODS = new Set(["get", "put", "post", "delete", "options", "head", "patch", "trace"]);
const SCHEMA_PREFIX = "#/components/schemas/";

export function schemaReference(value) {
  if (typeof value !== "string" || !value.startsWith(SCHEMA_PREFIX)) return null;
  let name;
  try { name = decodeURIComponent(value.slice(SCHEMA_PREFIX.length)).replace(/~1/g, "/").replace(/~0/g, "~"); }
  catch { return null; }
  if (!name) return null;
  const id = `schema-${name}`;
  return { name, id, href: `#${encodeURIComponent(id)}` };
}

export function listOperations(description) {
  const operations = [];
  for (const [path, item] of Object.entries(description.paths ?? {})) {
    for (const [method, operation] of Object.entries(item)) {
      if (!METHODS.has(method) || !operation || typeof operation !== "object") continue;
      const parameters = new Map();
      for (const parameter of [...(item.parameters ?? []), ...(operation.parameters ?? [])]) {
        const key = parameter.$ref ?? `${parameter.in}:${parameter.name}`;
        parameters.set(key, parameter);
      }
      operations.push({ path, method: method.toUpperCase(), operation, parameters: [...parameters.values()] });
    }
  }
  return operations;
}

function node(tag, attributes = {}, ...children) {
  const result = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === "text") result.textContent = value;
    else if (key === "class") result.className = value;
    else result.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) if (child !== null && child !== undefined && child !== false) result.append(child);
  return result;
}

function schemaJSON(value, schemas) {
  const code = node("code");
  const json = JSON.stringify(value, null, 2) ?? "{}";
  for (const token of json.split(/("(?:\\.|[^"\\])*")/g)) {
    let reference;
    if (token.startsWith('"')) { try { reference = schemaReference(JSON.parse(token)); } catch { /* Text remains literal. */ } }
    code.append(reference && Object.hasOwn(schemas, reference.name) ? node("a", { href: reference.href, "data-schema-link": "true", text: token }) : document.createTextNode(token));
  }
  return node("pre", { class: "api-json" }, code);
}

function contentSchemas(content, schemas) {
  const root = node("div");
  for (const [mediaType, media] of Object.entries(content ?? {})) root.append(node("p", { class: "api-media-type", text: mediaType }), schemaJSON(media.schema ?? {}, schemas));
  return root;
}

function operationBody(entry, schemas) {
  const { operation, parameters } = entry;
  const root = node("div", { class: "api-operation-body" });
  if (operation.description) root.append(node("p", { class: "api-description", text: operation.description }));
  if (parameters.length) root.append(node("h3", { text: "参数" }), schemaJSON(parameters, schemas));
  if (operation.requestBody) {
    root.append(node("h3", { text: operation.requestBody.required ? "请求体 · 必需" : "请求体" }));
    if (operation.requestBody.description) root.append(node("p", { class: "api-description", text: operation.requestBody.description }));
    root.append(contentSchemas(operation.requestBody.content, schemas));
  }
  if (operation.responses) {
    root.append(node("h3", { text: "响应" }));
    for (const [status, response] of Object.entries(operation.responses)) root.append(node("details", { class: "api-response" }, node("summary", {}, node("code", { text: status }), node("span", { text: response.description ?? "" })), contentSchemas(response.content, schemas)));
  }
  return root;
}

function followSchemaLink(href) {
  let id;
  try { id = decodeURIComponent(href.slice(1)); } catch { return; }
  const target = document.getElementById(id);
  if (!target) return;
  target.open = true;
  target.querySelector("summary")?.focus();
  target.scrollIntoView({ block: "start" });
}

async function loadReference() {
  const search = document.querySelector("#api-search");
  const error = document.querySelector("#api-error");
  error.hidden = true; search.disabled = true;
  document.querySelector("#api-meta").textContent = "正在读取接口描述…";
  try {
    const description = await request("/openapi.json");
    const operations = listOperations(description);
    const schemas = description.components?.schemas ?? {};
    const endpoints = document.querySelector("#api-endpoints");
    endpoints.replaceChildren();
    const rows = operations.map((entry) => {
      const details = node("details", { class: "api-operation" }, node("summary", {}, node("span", { class: `api-method method-${entry.method.toLowerCase()}`, text: entry.method }), node("code", { class: "api-path", text: entry.path })));
      details.addEventListener("toggle", () => {
        if (details.open && !details.dataset.loaded) { details.append(operationBody(entry, schemas)); details.dataset.loaded = "true"; }
      });
      endpoints.append(details);
      return { details, searchable: `${entry.method} ${entry.path} ${entry.operation.summary ?? ""} ${entry.operation.description ?? ""}`.toLowerCase() };
    });
    const filter = () => {
      const terms = search.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
      let count = 0;
      for (const row of rows) { row.details.hidden = !terms.every((term) => row.searchable.includes(term)); if (!row.details.hidden) count += 1; }
      document.querySelector("#api-feedback").textContent = count ? `${count} 个接口` : "没有匹配的接口。";
    };
    search.oninput = filter; search.disabled = false; filter();
    document.querySelector("#api-meta").textContent = `${description.info?.title ?? "MuLi"} ${description.info?.version ?? ""} · OpenAPI ${description.openapi ?? ""}`;
    const schemaList = document.querySelector("#api-schema-list");
    schemaList.replaceChildren();
    for (const [name, schema] of Object.entries(schemas)) {
      const reference = schemaReference(`${SCHEMA_PREFIX}${name.replace(/~/g, "~0").replace(/\//g, "~1")}`);
      schemaList.append(node("details", { class: "api-schema", id: reference.id }, node("summary", { text: name }), schemaJSON(schema, schemas)));
    }
    document.querySelector("#api-schemas").hidden = !Object.keys(schemas).length;
    if (location.hash) followSchemaLink(location.hash);
  } catch (failure) {
    error.hidden = false; error.querySelector("p").textContent = failure.message;
    document.querySelector("#api-meta").textContent = "接口描述暂不可用。";
  }
}

if (typeof document !== "undefined") {
  initializeTheme();
  document.querySelector("#api-retry").addEventListener("click", () => void loadReference());
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-schema-link]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); history.replaceState(null, "", link.getAttribute("href")); followSchemaLink(link.getAttribute("href"));
  });
  void loadReference();
}

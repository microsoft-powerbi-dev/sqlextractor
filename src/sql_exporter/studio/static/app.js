"use strict";
// Mapping Studio. All validation and persistence happen on the server; this file draws and edits.

const $ = (id) => document.getElementById(id);
const TOKEN = (() => {
  const match = location.hash.match(/token=([A-Za-z0-9_-]+)/);
  if (match) {
    try { sessionStorage.setItem("studio-token", match[1]); } catch (_) { /* storage blocked */ }
    history.replaceState(null, "", location.pathname);
    return match[1];
  }
  try { return sessionStorage.getItem("studio-token") || ""; } catch (_) { return ""; }
})();

const state = {
  catalog: { connections: [], sources: [], targets: [], mappings: [], contract: { types: [], operations: {} } },
  model: null, id: null, status: null, sourceId: null, targetId: null,
  selected: null, issues: [], error: null, proposal: null, dirty: false,
};

// ---------- utilities ----------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else if (key === "checked") node.checked = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(method, path, body, raw) {
  const options = { method, headers: { "X-Studio-Token": TOKEN } };
  if (raw !== undefined) { options.body = raw; options.headers["Content-Type"] = "application/octet-stream"; }
  else if (body !== undefined) { options.body = JSON.stringify(body); options.headers["Content-Type"] = "application/json"; }
  const response = await fetch("/api/" + path, options);
  const type = response.headers.get("Content-Type") || "";
  if (!type.includes("json")) {
    if (!response.ok) throw new Error("Request failed");
    return response.blob();
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

let toastTimer = null;
function toast(message, bad = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = "toast" + (bad ? " bad" : "");
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, bad ? 7000 : 3500);
}

// Resolves to false on failure so dialogs stay open with the user's input intact.
async function guarded(action) {
  try { return await action(); } catch (error) { toast(error.message, true); return false; }
}

const clone = (value) => JSON.parse(JSON.stringify(value));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const snake = (name) => name.replace(/([a-z0-9])([A-Z])/g, "$1_$2").replace(/[^A-Za-z0-9_]/g, "_").toLowerCase().replace(/^(\d)/, "_$1");
const editable = () => state.status !== "approved" && state.status !== "retired";
const source = () => state.catalog.sources.find((s) => s.id === state.sourceId) || null;
const target = () => state.catalog.targets.find((t) => t.id === state.targetId) || null;
const sourceNames = () => (source()?.metadata || []).map((c) => c.name);

function emptyModel() {
  return { metadata: { contract_version: 1, mapping_id: "new_mapping", mapping_version: "1.0.0" }, columns: [] };
}

function remember() {
  try {
    localStorage.setItem("studio-draft", JSON.stringify({ model: state.model, id: state.id, status: state.status,
      sourceId: state.sourceId, targetId: state.targetId }));
  } catch (_) { /* per-viewer convenience only */ }
}

function changed() {
  state.dirty = true;
  state.proposal = state.proposal && editable() ? state.proposal : null;
  renumber();
  remember();
  render();
  scheduleValidate();
}

function renumber() {
  state.model.columns.forEach((column, index) => { column.ordinal = index + 1; });
}

// ---------- loading ----------
async function refresh() {
  state.catalog = await api("GET", "state");
  $("version").textContent = "v" + state.catalog.version;
}

function load(model, meta = {}) {
  state.model = clone(model);
  state.id = meta.id ?? null;
  state.status = meta.status ?? null;
  state.sourceId = meta.sourceId ?? state.sourceId;
  state.targetId = meta.targetId ?? state.targetId;
  state.selected = null;
  state.proposal = null;
  state.dirty = false;
  renumber();
  remember();
  render();
  scheduleValidate(0);
}

async function openMapping(id) {
  const row = await api("GET", "mappings/" + id);
  load(row.model, { id: row.id, status: row.status, sourceId: row.source_id, targetId: row.target_id });
  toast(`Opened ${row.mapping_id} ${row.mapping_version} (${row.status})`);
}

// ---------- rendering ----------
function render() {
  const model = state.model;
  $("mapping-id").value = model.metadata.mapping_id;
  $("mapping-version").value = model.metadata.mapping_version;
  $("mapping-id").disabled = $("mapping-version").disabled = !editable();
  const chip = $("status-chip");
  chip.textContent = (state.status || "unsaved") + (state.dirty && editable() ? " *" : "");
  chip.className = "chip " + (state.status || "");
  $("btn-save").disabled = !editable();
  $("btn-approve").disabled = state.status !== "draft" || state.dirty;
  $("btn-devin").disabled = !editable();
  renderSelect($("source-select"), state.catalog.sources, state.sourceId, (s) => `${s.name} (${s.kind})`, "Choose a source");
  renderSelect($("target-select"), state.catalog.targets, state.targetId, (t) => `${t.name} (${t.spec.columns.length})`, "Choose a target");
  renderSourceInfo();
  renderSources();
  renderTargets();
  renderInspector();
  renderIssues();
  document.querySelector("#flow .hint").hidden = model.columns.length > 0;
  renderBanner();
  requestAnimationFrame(drawLinks);
}

function renderSelect(select, items, current, label, placeholder) {
  select.replaceChildren(el("option", { value: "", text: placeholder }),
    ...items.map((item) => el("option", { value: item.id, text: label(item), selected: item.id === current })));
  select.disabled = !editable();
}

function renderSourceInfo() {
  const s = source();
  $("source-info").textContent = !s ? "No source selected" :
    s.kind === "query" ? `SELECT query, window on ${s.date_column}` :
    `${s.kind === "jsonl" ? "fixture " : ""}${s.schema_name}.${s.table_name}, window on ${s.date_column}`;
  const t = target();
  $("target-info").textContent = !t ? "No target spec: columns are free-form" :
    `${t.spec.columns.length} required columns, ${t.spec.file.delimiter === "\t" ? "tab" : JSON.stringify(t.spec.file.delimiter)} delimited ${t.spec.file.extension}`;
}

function usedSources() {
  const used = new Map();
  state.model.columns.forEach((column) => {
    if (column.source) used.set(column.source, (used.get(column.source) || 0) + 1);
    for (const name of ruleInputs(column.rules)) used.set(name, (used.get(name) || 0) + 1);
  });
  return used;
}

function ruleInputs(rules) {
  const names = [];
  for (const rule of rules) {
    if (rule.op === "identity") names.push(...(rule.keys || []));
    if (rule.op === "template") for (const match of String(rule.value || "").matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)) names.push(match[1]);
  }
  return names;
}

function renderSources() {
  const list = $("source-columns");
  const s = source();
  const filter = $("source-filter").value.trim().toLowerCase();
  if (!s || !(s.metadata || []).length) {
    list.replaceChildren(el("li", { class: "empty", text: s ? "No captured columns; re-save the source to describe it" : "Add or choose a source to list its columns" }));
    return;
  }
  const used = usedSources();
  list.replaceChildren(...s.metadata.filter((c) => !filter || c.name.toLowerCase().includes(filter)).map((column) => {
    const item = el("li", { class: (used.has(column.name) ? "used" : "") + (column.supported === false ? " unsupported" : ""),
      draggable: column.supported !== false && editable() ? "true" : "false", "data-source": column.name,
      title: column.supported === false ? "This SQL type is not supported by the exporter" : "Drag onto a target column" },
      el("span", { class: "name", text: column.name }),
      el("span", { class: "type", text: column.sql_type + (column.nullable ? "" : " not null") }),
      el("span", { class: "port" }));
    item.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/x-source", column.name);
      event.dataTransfer.setData("text/plain", column.name);
      event.dataTransfer.effectAllowed = "link";
    });
    return item;
  }));
}

function columnProblem(column) {
  if (!column.source && !(column.rules[0] && ["constant", "identity", "template"].includes(column.rules[0].op))) return "unmapped";
  const lower = column.target.toLowerCase();
  if (state.issues.some((issue) => issue.toLowerCase().includes(" " + lower + " ") || issue.toLowerCase().endsWith(" " + lower))) return "issue";
  if (state.error && state.error.ordinal === column.ordinal) return "invalid";
  return null;
}

function describeColumn(column) {
  const parts = [];
  if (column.source) parts.push(column.source);
  const ops = column.rules.map((r) => r.op);
  if (ops.length) parts.push(ops.join(" > "));
  return parts.join("  ") || "not mapped";
}

function renderTargets() {
  const list = $("target-columns");
  const proposal = proposalDiff();
  const rows = state.model.columns.map((column, index) => {
    const problem = columnProblem(column);
    const diff = proposal.get(column.target.toLowerCase());
    const item = el("li", { class: [state.selected === index ? "selected" : "", problem ? "problem" : ""].join(" "), "data-index": index },
      el("span", { class: "name", text: column.target }),
      el("span", { class: "badges" },
        diff ? el("span", { class: "badge ai", text: "AI" }) : null,
        diff ? el("button", { type: "button", class: "icon", title: "Accept proposal for this column", onclick: (e) => { e.stopPropagation(); acceptColumn(diff); } }, "✓") : null,
        diff ? el("button", { type: "button", class: "icon", title: "Keep current column", onclick: (e) => { e.stopPropagation(); rejectColumn(diff); } }, "✗") : null,
        problem ? el("span", { class: "badge bad", text: problem }) : null,
        column.nullable ? null : el("span", { class: "badge req", text: "required" }),
        el("span", { class: "type", text: column.type + (column.type === "decimal" ? `(${column.scale ?? "?"})` : "") })),
      el("span", { class: "detail", text: diff ? "proposal: " + describeColumn(diff) : describeColumn(column) }),
      el("span", { class: "port" }));
    item.addEventListener("click", () => { state.selected = index; render(); });
    dropTarget(item, (name) => mapSource(index, name));
    return item;
  });
  for (const extra of proposal.values()) {
    if (state.model.columns.some((c) => c.target.toLowerCase() === extra.target.toLowerCase())) continue;
    rows.push(el("li", { class: "ghost" },
      el("span", { class: "name", text: extra.target }),
      el("span", { class: "badges" }, el("span", { class: "badge ai", text: "AI new" }),
        el("button", { type: "button", class: "icon", title: "Add this proposed column", onclick: () => acceptColumn(extra) }, "✓"),
        el("button", { type: "button", class: "icon", title: "Skip", onclick: () => rejectColumn(extra) }, "✗")),
      el("span", { class: "detail", text: describeColumn(extra) })));
  }
  list.replaceChildren(...(rows.length ? rows : [el("li", { class: "empty", text: "Import or choose a target, or drop a source column below" })]));
  $("new-target-drop").hidden = !editable();
  $("btn-add-derived").disabled = $("btn-automap").disabled = !editable();
}

function dropTarget(node, onDrop) {
  node.addEventListener("dragover", (event) => {
    if (!editable() || !event.dataTransfer.types.includes("text/x-source")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "link";
    node.classList.add("drop");
  });
  node.addEventListener("dragleave", () => node.classList.remove("drop"));
  node.addEventListener("drop", (event) => {
    node.classList.remove("drop");
    const name = event.dataTransfer.getData("text/x-source");
    if (!name) return;
    event.preventDefault();
    onDrop(name);
  });
}

function mapSource(index, name) {
  const column = state.model.columns[index];
  column.source = name;
  // A drop replaces a derived-only definition; transformation rules on a mapped column are kept.
  if (column.rules[0] && ["constant", "identity", "template"].includes(column.rules[0].op)) column.rules = [];
  state.selected = index;
  changed();
}

function addFromSource(name) {
  const meta = (source()?.metadata || []).find((c) => c.name === name) || {};
  const suggested = meta.suggested || { type: "string", scale: null };
  let targetName = snake(name);
  while (state.model.columns.some((c) => c.target.toLowerCase() === targetName.toLowerCase())) targetName += "_2";
  state.model.columns.push({ ordinal: 0, source: name, target: targetName, type: suggested.type, nullable: meta.nullable !== false,
    rules: [], scale: suggested.type === "decimal" ? suggested.scale ?? 2 : null });
  state.selected = state.model.columns.length - 1;
  changed();
}

// ---------- links ----------
function drawLinks() {
  const svg = $("links");
  const box = $("layout").getBoundingClientRect();
  svg.replaceChildren();
  if (getComputedStyle(svg).display === "none") return;
  const sourceList = $("source-columns").getBoundingClientRect();
  const targetList = $("target-columns").getBoundingClientRect();
  const ports = new Map([...document.querySelectorAll("#source-columns li[data-source]")].map((li) => [li.dataset.source, li]));
  const clamp = (y, rect) => Math.max(rect.top + 4, Math.min(rect.bottom - 4, y));
  document.querySelectorAll("#target-columns li[data-index]").forEach((li) => {
    const index = Number(li.dataset.index);
    const column = state.model.columns[index];
    const inputs = [column.source, ...ruleInputs(column.rules)].filter(Boolean);
    const end = li.getBoundingClientRect();
    [...new Set(inputs)].forEach((name, position) => {
      const start = ports.get(name);
      if (!start) return;
      const a = start.getBoundingClientRect();
      const ay = a.top + a.height / 2;
      const by = end.top + end.height / 2;
      const sy = clamp(ay, sourceList);
      const ty = clamp(by, targetList);
      const x1 = a.right - box.left, y1 = sy - box.top, x2 = end.left - box.left, y2 = ty - box.top;
      const bend = Math.max(40, (x2 - x1) / 2);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`);
      const classes = [];
      if (name !== column.source || position > 0) classes.push("extra");
      if (state.selected === index) classes.push("selected");
      if (sy !== ay || ty !== by) classes.push("offscreen");
      path.setAttribute("class", classes.join(" "));
      path.addEventListener("click", () => { state.selected = index; render(); });
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${name} → ${column.target}`;
      path.append(title);
      svg.append(path);
    });
  });
}

// ---------- inspector ----------
function field(label, control) {
  return el("label", { class: "field" }, el("span", { text: label }), control);
}

function renderInspector() {
  const root = $("inspector");
  const index = state.selected;
  if (index === null || !state.model.columns[index]) {
    root.replaceChildren(...overview());
    return;
  }
  const column = state.model.columns[index];
  const lock = !editable();
  const update = (key, value) => { column[key] = value; changed(); };
  const names = sourceNames();
  if (column.source && !names.includes(column.source)) names.unshift(column.source);
  const typeSelect = el("select", { disabled: lock, onchange: (e) => {
    column.type = e.target.value;
    column.scale = column.type === "decimal" ? column.scale ?? 2 : null;
    changed();
  } }, ...state.catalog.contract.types.map((t) => el("option", { value: t, text: t, selected: t === column.type })));
  root.replaceChildren(...[
    el("div", { class: "row" }, el("h3", { text: column.target }), el("button", { type: "button", class: "link", onclick: () => { state.selected = null; render(); } }, "close")),
    field("Target name", el("input", { value: column.target, disabled: lock, spellcheck: "false", onchange: (e) => update("target", e.target.value.trim()) })),
    field("Type", typeSelect),
    column.type === "decimal" ? field("Scale", el("input", { type: "number", min: "0", max: "18", value: column.scale ?? "", disabled: lock,
      onchange: (e) => update("scale", e.target.value === "" ? null : Number(e.target.value)) })) : null,
    field("Nullable", el("input", { type: "checkbox", checked: column.nullable, disabled: lock, onchange: (e) => update("nullable", e.target.checked) })),
    field("Source", el("select", { disabled: lock, onchange: (e) => update("source", e.target.value || null) },
      el("option", { value: "", text: "(derived: no source column)" }),
      ...names.map((n) => el("option", { value: n, text: n, selected: n === column.source })))),
    el("h4", { text: "Logic (applied in order)" }),
    ...column.rules.map((rule, position) => ruleEditor(column, rule, position, lock)),
    column.rules.length ? null : el("div", { class: "muted", text: column.source ? "Source value is converted to the target type as is." : "Add constant, identity, or template for a derived column." }),
    lock ? null : el("div", { class: "row" },
      el("select", { id: "add-rule" }, ...Object.keys(state.catalog.contract.operations).map((op) => el("option", { value: op, text: op }))),
      el("button", { type: "button", onclick: () => addRule(column, $("add-rule").value) }, "+ Add rule")),
    lock ? null : el("h4", { text: "Column" }),
    lock ? null : el("div", { class: "row" },
      el("button", { type: "button", disabled: index === 0, onclick: () => move(index, -1) }, "Move up"),
      el("button", { type: "button", disabled: index === state.model.columns.length - 1, onclick: () => move(index, 1) }, "Move down"),
      el("button", { type: "button", onclick: () => { state.model.columns.splice(index, 1); state.selected = null; changed(); } }, "Delete")),
  ].filter(Boolean));
}

function defaults(op) {
  return { replace: { old: "", new: "" }, default: { value: "" }, constant: { value: "" }, multiply: { factor: "1" },
    lookup: { values: {}, on_missing: "error" }, prefix: { value: "" }, suffix: { value: "" },
    identity: { namespace: state.model.metadata.mapping_id + "/" + "entity", keys: [] }, template: { value: "" } }[op] || {};
}

function addRule(column, op) {
  const rule = { op, ...defaults(op) };
  if (["constant", "identity", "template"].includes(op)) { column.rules.unshift(rule); column.source = column.source && op !== "constant" ? column.source : null; }
  else column.rules.push(rule);
  changed();
}

function scalarEditor(rule, key, lock) {
  const kind = rule[key] === null ? "null" : typeof rule[key] === "number" ? "number" : typeof rule[key] === "boolean" ? "boolean" : "text";
  const input = el("input", { value: rule[key] === null ? "" : String(rule[key]), disabled: lock || kind === "null", onchange: (e) => apply(kind, e.target.value) });
  const apply = (type, text) => {
    rule[key] = type === "null" ? null : type === "number" ? (text.trim() === "" || isNaN(Number(text)) ? text : Number(text))
      : type === "boolean" ? text.trim().toLowerCase() === "true" : text;
    changed();
  };
  const select = el("select", { disabled: lock, onchange: (e) => apply(e.target.value, input.value) },
    ...["text", "number", "boolean", "null"].map((t) => el("option", { value: t, text: t, selected: t === kind })));
  return el("div", { class: "row" }, input, select);
}

function ruleEditor(column, rule, position, lock) {
  const set = (key, value) => { rule[key] = value; changed(); };
  const text = (key, placeholder = "") => el("input", { value: rule[key] ?? "", placeholder, disabled: lock, spellcheck: "false", onchange: (e) => set(key, e.target.value) });
  const body = [];
  if (["replace"].includes(rule.op)) body.push(field("Find", text("old")), field("Replace with", text("new")));
  if (["prefix", "suffix"].includes(rule.op)) body.push(field("Text", text("value")));
  if (["default", "constant"].includes(rule.op)) body.push(field(rule.op === "default" ? "If null" : "Value", scalarEditor(rule, "value", lock)));
  if (rule.op === "multiply") body.push(field("Factor", text("factor", "exact number, e.g. 0.01")));
  if (rule.op === "lookup") body.push(lookupEditor(rule, lock), field("If missing", el("select", { disabled: lock, onchange: (e) => set("on_missing", e.target.value) },
    ...["error", "keep", "null"].map((v) => el("option", { value: v, text: v, selected: v === rule.on_missing })))));
  if (rule.op === "identity") body.push(field("Namespace", text("namespace", "shared/entity")), field("Keys", keysEditor(rule, lock)));
  if (rule.op === "template") {
    const names = sourceNames();
    body.push(field("Template", text("value", "{ColumnA}-{ColumnB}")));
    if (!lock && names.length) body.push(field("Insert", el("select", { onchange: (e) => { if (e.target.value) set("value", (rule.value || "") + "{" + e.target.value + "}"); } },
      el("option", { value: "", text: "source column..." }), ...names.map((n) => el("option", { value: n, text: n })))));
  }
  return el("div", { class: "rule" },
    el("div", { class: "rule-head" },
      el("span", { class: "op", text: `${position + 1}. ${rule.op}` }),
      lock ? null : el("button", { type: "button", class: "icon", title: "Earlier", disabled: position === 0, onclick: () => { column.rules.splice(position - 1, 0, column.rules.splice(position, 1)[0]); changed(); } }, "↑"),
      lock ? null : el("button", { type: "button", class: "icon", title: "Later", disabled: position === column.rules.length - 1, onclick: () => { column.rules.splice(position + 1, 0, column.rules.splice(position, 1)[0]); changed(); } }, "↓"),
      lock ? null : el("button", { type: "button", class: "icon", title: "Remove", onclick: () => { column.rules.splice(position, 1); changed(); } }, "×")),
    ...body);
}

function lookupEditor(rule, lock) {
  const entries = Object.entries(rule.values || {});
  const commit = (pairs) => { rule.values = Object.fromEntries(pairs.filter(([k]) => k !== "")); changed(); };
  const grid = el("div", { class: "pairs" });
  entries.forEach(([key, value], i) => {
    grid.append(
      el("input", { value: key, disabled: lock, "aria-label": "Code", onchange: (e) => { entries[i][0] = e.target.value; commit(entries); } }),
      el("input", { value: value === null ? "" : String(value), disabled: lock, "aria-label": "Value", onchange: (e) => { entries[i][1] = e.target.value; commit(entries); } }),
      lock ? el("span") : el("button", { type: "button", class: "icon", onclick: () => { entries.splice(i, 1); commit(entries); } }, "×"));
  });
  return el("div", {}, grid, lock ? null : el("button", { type: "button", class: "link", onclick: () => { entries.push(["", ""]); rule.values = Object.fromEntries(entries.map(([k, v], i) => [k || "code" + (i + 1), v])); changed(); } }, "+ code"));
}

function keysEditor(rule, lock) {
  const box = el("div", { class: "keys" }, ...(rule.keys || []).map((key, i) => el("span", { class: "key" }, key,
    lock ? null : el("button", { type: "button", title: "Remove key", onclick: () => { rule.keys.splice(i, 1); changed(); } }, "×"))));
  if (!lock) {
    dropTarget(box, (name) => { if (!rule.keys.includes(name)) { rule.keys.push(name); changed(); } });
    box.append(el("select", { onchange: (e) => { if (e.target.value && !rule.keys.includes(e.target.value)) { rule.keys.push(e.target.value); changed(); } } },
      el("option", { value: "", text: "+ key (or drop a column)" }), ...sourceNames().map((n) => el("option", { value: n, text: n }))));
  }
  return box;
}

function move(index, delta) {
  const columns = state.model.columns;
  columns.splice(index + delta, 0, columns.splice(index, 1)[0]);
  state.selected = index + delta;
  changed();
}

// Target file settings and a live sample file name.
function sampleName(file) {
  const start = new Date(Date.UTC(2026, 0, 1)), end = new Date(Date.UTC(2026, 1, 1));
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  const fmt = (d, spec) => spec.replace(/%([YmdHMSfj])/g, (_, c) => ({ Y: d.getUTCFullYear(), m: pad(d.getUTCMonth() + 1), d: pad(d.getUTCDate()),
    H: pad(d.getUTCHours()), M: pad(d.getUTCMinutes()), S: pad(d.getUTCSeconds()), f: "000000",
    j: pad(Math.floor((d - Date.UTC(d.getUTCFullYear(), 0, 1)) / 864e5) + 1, 3) })[c]);
  const stamp = "%Y%m%dT%H%M%S%fZ";
  const pattern = file.pattern || "{start}_{end}";
  return pattern.replace(/\{(\w+)(?::([^}]*))?\}/g, (_, name, spec) => name === "start" ? fmt(start, spec || stamp) : name === "end" ? fmt(end, spec || stamp)
    : name === "mapping_id" ? state.model.metadata.mapping_id : name === "source_id" ? (source()?.name || "source") : "?") + file.extension;
}

function overview() {
  const t = target();
  const nodes = [el("h3", { text: "Mapping" }),
    el("p", { class: "muted", text: `${state.model.columns.length} target columns. Select a column to edit its source and logic.` })];
  if (!t) return nodes.concat(el("p", { class: "muted", text: "Import a target layout (XSD, Excel Target sheet, JSON spec, or sample file) to enforce required columns and file format." }));
  const file = clone(t.spec.file);
  const sample = el("div", { class: "sample", text: sampleName(file) });
  const set = (key, value) => { file[key] = value; sample.textContent = sampleName(file); };
  const input = (key, attrs = {}) => el("input", { value: file[key] ?? "", spellcheck: "false", oninput: (e) => set(key, e.target.value || (key === "pattern" ? null : e.target.value)), ...attrs });
  return nodes.concat(
    el("h4", { text: `Target file: ${t.name}` }),
    field("File pattern", input("pattern", { placeholder: "{mapping_id}_{start:%Y%m}" })),
    field("Extension", input("extension")),
    field("Delimiter", el("select", { onchange: (e) => set("delimiter", e.target.value) },
      ...[["|", "pipe |"], [",", "comma ,"], ["\t", "tab"], [";", "semicolon ;"]].map(([v, l]) => el("option", { value: v, text: l, selected: v === file.delimiter })))),
    field("Header row", el("input", { type: "checkbox", checked: file.header, onchange: (e) => set("header", e.target.checked) })),
    field("Line ending", el("select", { onchange: (e) => set("line_ending", e.target.value) },
      el("option", { value: "\n", text: "LF", selected: file.line_ending === "\n" }), el("option", { value: "\r\n", text: "CRLF", selected: file.line_ending === "\r\n" }))),
    field("Encoding", el("select", { onchange: (e) => set("encoding", e.target.value) },
      ...["utf-8", "utf-8-sig"].map((v) => el("option", { value: v, text: v === "utf-8-sig" ? "UTF-8 with BOM" : "UTF-8", selected: v === file.encoding })))),
    field("Null token", input("null_token")),
    field("Example", sample),
    el("div", { class: "row" }, el("button", { type: "button", onclick: () => guarded(async () => {
      await api("POST", "targets", { spec: { ...t.spec, file }, origin: t.origin, origin_sha256: t.origin_sha256 });
      await refresh();
      render();
      toast("Target file settings saved");
    }) }, "Save file settings")),
    el("p", { class: "muted", text: "Placeholders: {mapping_id}, {source_id}, {start:%Y%m%d}, {end:...}. Each window needs a unique name." }));
}

// ---------- issues, validation ----------
function renderIssues() {
  const root = $("issues");
  const unmapped = state.model.columns.filter((c) => columnProblem(c) === "unmapped").map((c) => `Target column ${c.target} has no source or derived rule`);
  const all = [...(state.error ? [state.error.message] : []), ...unmapped, ...state.issues];
  if (!state.model.columns.length) { root.replaceChildren(el("span", { class: "muted", text: "Choose a source and a target, then drag columns to map them." })); return; }
  root.replaceChildren(all.length
    ? el("div", {}, el("strong", { class: "bad", text: `${all.length} issue${all.length > 1 ? "s" : ""}` }), el("ul", {}, ...[...new Set(all)].map((m) => el("li", { text: m }))))
    : el("span", { class: "ok", text: "Valid: the mapping meets the contract" + (target() ? " and the target spec." : ".") }));
}

let validateTimer = null;
function scheduleValidate(delay = 400) {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validate, delay);
}

async function validate() {
  if (!state.model.columns.length) { state.error = null; state.issues = []; render(); return true; }
  try {
    const result = await api("POST", "validate", { model: state.model, source_id: state.sourceId, target_id: state.targetId });
    state.error = null;
    state.issues = result.issues;
  } catch (error) {
    const row = error.message.match(/Columns row (\d+)/);
    state.error = { message: error.message, ordinal: row ? Number(row[1]) - 1 : null };
    state.issues = [];
  }
  renderTargets();
  renderIssues();
  requestAnimationFrame(drawLinks);
  return !state.error && !state.issues.length;
}

// ---------- proposals ----------
function proposalDiff() {
  const result = new Map();
  if (!state.proposal) return result;
  for (const column of state.proposal.columns) {
    const current = state.model.columns.find((c) => c.target.toLowerCase() === column.target.toLowerCase());
    if (!current || !same({ ...current, ordinal: 0 }, { ...column, ordinal: 0 })) result.set(column.target.toLowerCase(), column);
  }
  return result;
}

function acceptColumn(column) {
  const index = state.model.columns.findIndex((c) => c.target.toLowerCase() === column.target.toLowerCase());
  if (index >= 0) state.model.columns[index] = clone(column);
  else state.model.columns.push(clone(column));
  rejectColumn(column, false);
  changed();
}

function rejectColumn(column, redraw = true) {
  state.proposal.columns = state.proposal.columns.filter((c) => c.target.toLowerCase() !== column.target.toLowerCase());
  if (!proposalDiff().size) state.proposal = null;
  if (redraw) render();
}

function renderBanner() {
  const banner = $("proposal-banner");
  const diff = proposalDiff();
  banner.hidden = !diff.size;
  if (!diff.size) return;
  banner.replaceChildren(
    el("strong", { text: "Devin proposal" }),
    el("span", { text: `${diff.size} column${diff.size > 1 ? "s" : ""} differ from the current mapping. Accept or keep each one, or decide for all.` }),
    el("button", { type: "button", onclick: () => {
      const order = state.proposal.columns.map((c) => c.target.toLowerCase());
      state.model.metadata = { ...state.model.metadata, mapping_id: state.proposal.metadata.mapping_id };
      for (const column of diff.values()) {
        const index = state.model.columns.findIndex((c) => c.target.toLowerCase() === column.target.toLowerCase());
        if (index >= 0) state.model.columns[index] = clone(column); else state.model.columns.push(clone(column));
      }
      state.model.columns.sort((a, b) => order.indexOf(a.target.toLowerCase()) - order.indexOf(b.target.toLowerCase()));
      state.proposal = null;
      changed();
    } }, "Accept all"),
    el("button", { type: "button", onclick: () => { state.proposal = null; render(); } }, "Discard"));
}

// ---------- dialogs ----------
function dialog(title, content, buttons = [["Close", null]]) {
  const body = $("dialog-body");
  body.replaceChildren(el("h3", { text: title }), ...[].concat(content),
    el("div", { class: "buttons" }, ...buttons.map(([label, action, cls]) => el("button", { type: action ? "button" : "submit", class: cls,
      onclick: action ? async () => { if ((await guarded(action)) !== false) $("dialog").close(); } : undefined }, label))));
  if (!$("dialog").open) $("dialog").showModal();
}

function openDialog() {
  const rows = state.catalog.mappings;
  dialog("Open mapping", rows.length ? el("div", { class: "scroll" }, el("table", {},
    el("tr", {}, el("th", { text: "Mapping" }), el("th", { text: "Version" }), el("th", { text: "Status" }), el("th", { text: "By" }), el("th")),
    ...rows.map((r) => el("tr", {}, el("td", { text: r.mapping_id }), el("td", { text: r.mapping_version }), el("td", { text: r.status }),
      el("td", { text: r.created_by }), el("td", {}, el("button", { type: "button", onclick: () => guarded(async () => { await openMapping(r.id); $("dialog").close(); }) }, "Open"))))))
    : el("p", { class: "muted", text: "No saved mappings yet." }));
}

function sourceDialog() {
  const form = { kind: "table", name: "", connection_id: state.catalog.connections[0]?.id ?? "", schema: "dbo", table: "", query: "", fixture: "",
    date_column: "", settings: { granularity: "month", years: 3, order_by: [] } };
  let described = [];
  const connectionSelect = el("select", { onchange: (e) => { form.connection_id = e.target.value ? Number(e.target.value) : ""; } },
    el("option", { value: "", text: "choose..." }), ...state.catalog.connections.map((c) => el("option", { value: c.id, text: `${c.name} (env ${c.connection_env})`, selected: c.id === form.connection_id })));
  const newConnection = el("div", { class: "row" },
    el("input", { id: "conn-name", placeholder: "connection name" }), el("input", { id: "conn-env", placeholder: "ENV_VAR_WITH_ODBC_STRING" }),
    el("button", { type: "button", onclick: () => guarded(async () => {
      const result = await api("POST", "connections", { name: $("conn-name").value.trim(), connection_env: $("conn-env").value.trim() });
      await refresh();
      connectionSelect.append(el("option", { value: result.id, text: $("conn-name").value.trim(), selected: true }));
      form.connection_id = result.id;
      toast("Connection saved (stores the variable name, never the secret)");
    }) }, "Add"));
  const dateSelect = el("select", { onchange: (e) => { form.date_column = e.target.value; } }, el("option", { value: "", text: "describe first" }));
  const preview = el("div", { class: "scroll" });
  const tablePicker = el("select", { onchange: (e) => { const [s, t] = e.target.value.split("."); form.schema = s; form.table = t; schemaInput.value = s; tableInput.value = t; } },
    el("option", { value: "", text: "browse..." }));
  const schemaInput = el("input", { value: form.schema, oninput: (e) => { form.schema = e.target.value.trim(); } });
  const tableInput = el("input", { value: "", oninput: (e) => { form.table = e.target.value.trim(); } });
  const sections = {
    table: el("div", {}, field("Schema", schemaInput), field("Table or view", el("div", { class: "row" }, tableInput, tablePicker,
      el("button", { type: "button", onclick: () => guarded(async () => {
        const tables = await api("POST", "tables", { connection_id: form.connection_id, schema: form.schema || null });
        tablePicker.replaceChildren(el("option", { value: "", text: `${tables.length} found` }), ...tables.map((t) => el("option", { value: `${t.schema}.${t.table}`, text: `${t.schema}.${t.table} (${t.kind})` })));
      }) }, "List")))),
    query: field("SELECT", el("textarea", { rows: "6", placeholder: "SELECT o.OrderId, o.CreatedAt, c.Name AS Customer FROM dbo.Orders o JOIN dbo.Customers c ON c.Id = o.CustomerId",
      oninput: (e) => { form.query = e.target.value; } })),
    jsonl: el("div", {}, field("Fixture path", el("input", { placeholder: "/path/to/rows.jsonl", oninput: (e) => { form.fixture = e.target.value.trim(); } })),
      field("Schema", el("input", { value: form.schema, oninput: (e) => { form.schema = e.target.value.trim(); } })),
      field("Table", el("input", { oninput: (e) => { form.table = e.target.value.trim(); } }))),
  };
  const connectionRow = el("div", {}, field("Connection", connectionSelect), field("New connection", newConnection));
  const swap = () => {
    for (const [kind, node] of Object.entries(sections)) node.hidden = kind !== form.kind;
    connectionRow.hidden = form.kind === "jsonl";
  };
  swap();
  dialog("Add source", [
    field("Name", el("input", { placeholder: "orders_warehouse", oninput: (e) => { form.name = e.target.value.trim(); } })),
    field("Kind", el("select", { onchange: (e) => { form.kind = e.target.value; swap(); } },
      el("option", { value: "table", text: "SQL Server table or view" }), el("option", { value: "query", text: "SQL Server SELECT query" }),
      el("option", { value: "jsonl", text: "Offline JSONL fixture" }))),
    connectionRow, sections.table, sections.query, sections.jsonl,
    el("div", { class: "row" }, el("button", { type: "button", onclick: () => guarded(async () => {
      described = await api("POST", "describe", { ...form, schema: form.schema, table: form.table });
      const guess = described.find((c) => /date|time|created|modified/i.test(c.name)) || described[0];
      form.date_column = guess ? guess.name : "";
      dateSelect.replaceChildren(...described.map((c) => el("option", { value: c.name, text: `${c.name} (${c.sql_type})`, selected: c.name === form.date_column })));
      preview.replaceChildren(el("table", {}, el("tr", {}, el("th", { text: "Column" }), el("th", { text: "SQL type" }), el("th", { text: "Suggested" })),
        ...described.map((c) => el("tr", {}, el("td", { text: c.name }), el("td", { text: c.sql_type + (c.nullable ? "" : " not null") }),
          el("td", { text: c.supported ? c.suggested.type + (c.suggested.scale !== null ? `(${c.suggested.scale})` : "") : "unsupported" })))));
    }) }, "Describe columns"), el("span", { class: "muted", text: "Reads catalog metadata only; no rows." })),
    preview,
    field("Window column", dateSelect),
    field("Granularity", el("select", { onchange: (e) => { form.settings.granularity = e.target.value; } },
      ...["day", "month", "quarter", "year"].map((g) => el("option", { value: g, text: g, selected: g === "month" })))),
    field("Years back", el("input", { type: "number", min: "1", max: "100", value: "3", oninput: (e) => { form.settings.years = Number(e.target.value); } })),
    field("Fixed end (optional)", el("input", { placeholder: "2026-09-01T00:00:00Z", oninput: (e) => { form.settings.end = e.target.value.trim() || null; } })),
    field("Order by", el("input", { placeholder: "CreatedAt, OrderId", oninput: (e) => { form.settings.order_by = e.target.value.split(",").map((s) => s.trim()).filter(Boolean); } })),
  ], [["Cancel", null], ["Save source", async () => {
    const saved = await api("POST", "sources", { ...form, metadata: described.length ? described : undefined });
    await refresh();
    state.sourceId = saved.id;
    changed();
    toast(`Source ${saved.name} saved with ${(saved.metadata || []).length} columns`);
  }, "primary"]]);
}

async function pickFile(accept) {
  return new Promise((resolve) => {
    const input = $("file-input");
    input.value = "";
    input.accept = accept;
    input.onchange = () => resolve(input.files[0] || null);
    input.click();
  });
}

async function upload(kind, accept) {
  const file = await pickFile(accept);
  if (!file) return null;
  return api("POST", `upload?kind=${kind}&name=${encodeURIComponent(file.name)}`, undefined, await file.arrayBuffer());
}

async function importTarget() {
  const result = await upload("target", ".xsd,.xlsx,.xls,.json,.csv,.txt,.dat,.psv,.tsv");
  if (!result) return;
  await refresh();
  state.targetId = result.target.id;
  if (!state.model.columns.length && state.sourceId) await automap();
  else changed();
  toast(`Target ${result.target.name} imported` + (result.warnings.length ? `: ${result.warnings[0]}` : ""), result.warnings.length > 0);
}

async function automap() {
  if (!state.sourceId || !state.targetId) throw new Error("Choose a source and a target first");
  const skeleton = await api("POST", "automap", { source_id: state.sourceId, target_id: state.targetId, mapping_id: state.model.metadata.mapping_id });
  const byTarget = new Map(state.model.columns.map((c) => [c.target.toLowerCase(), c]));
  // Keep any column you already defined; only fill the rest from name matches.
  state.model.columns = skeleton.columns.map((c) => {
    const existing = byTarget.get(c.target.toLowerCase());
    return existing && (existing.source || existing.rules.length) ? { ...existing, type: c.type, nullable: c.nullable, scale: c.scale } : c;
  });
  changed();
  toast(`Auto-mapped ${skeleton.columns.filter((c) => c.source).length} of ${skeleton.columns.length} columns by name`);
}

function devinDialog() {
  const form = { provider: "devin", document: null };
  const docLabel = el("span", { class: "muted", text: "optional" });
  const status = el("div");
  dialog("Generate mapping with AI", [
    el("p", { class: "muted", text: "Sends the mapping document, the source column metadata, and the target spec. No rows or credentials are sent. The proposal is validated here and shown for review before anything is saved." }),
    field("Provider", el("select", { onchange: (e) => { form.provider = e.target.value; } },
      el("option", { value: "devin", text: "Devin (DEVIN_API_KEY)" }), el("option", { value: "codex", text: "Codex CLI" }))),
    field("Source", el("span", { text: source() ? source().name : "none selected" })),
    field("Target", el("span", { text: target() ? target().name : "none selected" })),
    field("Mapping document", el("div", { class: "row" }, el("button", { type: "button", onclick: () => guarded(async () => {
      const result = await upload("document", ".xlsx,.xls,.md,.txt,.json");
      if (result) { form.document = result.document; docLabel.textContent = result.document; }
    }) }, "Upload..."), docLabel)),
    status,
  ], [["Close", null], ["Start", async () => {
    const job = await api("POST", "jobs", { provider: form.provider, document: form.document, source_id: state.sourceId, target_id: state.targetId });
    pollJob(job.id, status);
    return false;
  }, "accent"]]);
}

async function pollJob(id, node) {
  const job = await api("GET", "jobs/" + id);
  const done = !["queued", "running", "started"].includes(job.status);
  node.replaceChildren(
    el("p", {}, el("strong", { text: "Status: " }), job.status + (job.provider_status ? ` (Devin: ${job.provider_status})` : "")),
    job.session_url ? el("p", {}, el("a", { href: job.session_url, target: "_blank", rel: "noopener noreferrer", text: "Open Devin session" })) : null,
    job.error ? el("p", { class: "bad", text: job.error }) : null,
    job.issues.length ? el("ul", {}, ...job.issues.map((i) => el("li", { text: `${i.severity}: ${i.location}: ${i.message}` }))) : null,
    done ? null : el("button", { type: "button", onclick: () => guarded(() => api("POST", `jobs/${id}/cancel`, {})) }, "Cancel job"),
    job.proposals.length ? el("button", { type: "button", class: "primary", onclick: () => {
      const proposal = job.proposals[0];
      if (!state.model.columns.length) { load(proposal, { sourceId: state.sourceId, targetId: state.targetId }); state.dirty = true; }
      else state.proposal = clone(proposal);
      $("dialog").close();
      render();
      scheduleValidate(0);
    } }, "Review proposal") : null);
  if (!done && $("dialog").open) setTimeout(() => pollJob(id, node), 3000);
}

// ---------- actions ----------
async function save() {
  const result = await api("POST", "mappings", { model: state.model, source_id: state.sourceId, target_id: state.targetId, id: state.id });
  state.id = result.id;
  state.status = "draft";
  state.dirty = false;
  state.issues = result.issues;
  await refresh();
  remember();
  render();
  toast(result.issues.length ? `Draft saved with ${result.issues.length} issue(s) to resolve before approval` : "Draft saved", result.issues.length > 0);
}

async function approve() {
  const result = await api("POST", `mappings/${state.id}/approve`, {});
  state.status = "approved";
  await refresh();
  remember();
  render();
  dialog("Approved and published", [el("p", { text: "These files are what the exporter runs. They never change for this version." }),
    el("pre", { text: `${result.mapping_path}\n${result.config_path}\n\nsql-exporter export --config "${result.config_path}"` })]);
}

async function more(action) {
  const needId = () => { if (!state.id) throw new Error("Save the mapping first"); };
  if (action === "dry-run") {
    needId();
    const r = await api("POST", `mappings/${state.id}/dry-run`, {});
    dialog("Dry run", [el("p", { text: `${r.windows} files from ${r.start} to ${r.end}` }), el("pre", { text: r.sql }), el("p", { class: "muted", text: "First files: " + r.files.join(", ") })]);
  } else if (action === "preview") {
    const r = await api("POST", "preview", { model: state.model, source_id: state.sourceId });
    dialog("Preview (offline fixture)", [el("div", { class: "scroll" }, el("table", {}, el("tr", {}, ...r.header.map((h) => el("th", { text: h }))),
      ...r.rows.map((row) => el("tr", {}, ...row.map((v) => el("td", { class: "mono", text: v === null ? "NULL" : v })))))),
      r.errors.length ? el("ul", {}, ...r.errors.map((e) => el("li", { class: "bad", text: e }))) : null]);
  } else if (action === "export" || action === "export-resume") {
    needId();
    const r = await api("POST", `mappings/${state.id}/export`, { resume: action === "export-resume" });
    toast(`Export started into ${r.output_dir}`);
  } else if (action === "exports") {
    const rows = await api("GET", "exports");
    dialog("Export history", el("div", { class: "scroll" }, el("table", {}, el("tr", {}, ...["#", "Status", "Rows", "Written", "Skipped", "Output", "Error"].map((h) => el("th", { text: h }))),
      ...rows.map((r) => el("tr", {}, ...[r.id, r.status, r.rows, r.windows_written, r.windows_skipped, r.output_dir, r.error].map((v) => el("td", { text: v ?? "" })))))));
  } else if (action === "import-mapping") {
    const result = await upload("mapping", ".xlsx,.xls,.json");
    if (result) { load(result.model, { sourceId: state.sourceId, targetId: state.targetId }); state.dirty = true; render(); toast("Mapping imported; save it as a draft"); }
  } else if (action === "download-json") {
    downloadBlob(new Blob([JSON.stringify(state.model, null, 2) + "\n"], { type: "application/json" }), `${state.model.metadata.mapping_id}-${state.model.metadata.mapping_version}.mapping.json`);
  } else if (action === "download-xlsx") {
    needId();
    downloadBlob(await api("GET", `mappings/${state.id}/download.xlsx`), `${state.model.metadata.mapping_id}-${state.model.metadata.mapping_version}.mapping.xlsx`);
  } else if (action === "new-version") {
    const [major, minor, patch] = state.model.metadata.mapping_version.split(".").map(Number);
    const model = clone(state.model);
    model.metadata.mapping_version = `${major}.${minor}.${patch + 1}`;
    load(model, { sourceId: state.sourceId, targetId: state.targetId });
    state.dirty = true;
    render();
    toast("New version: edit, then save as a draft");
  } else if (action === "retire") {
    needId();
    await api("POST", `mappings/${state.id}/retire`, {});
    state.status = "retired";
    await refresh();
    render();
  } else if (action === "audit") {
    const rows = await api("GET", "audit");
    dialog("Audit log", el("div", { class: "scroll" }, el("table", {}, el("tr", {}, ...["When", "Who", "Action", "Entity"].map((h) => el("th", { text: h }))),
      ...rows.map((r) => el("tr", {}, el("td", { text: new Date(r.at * 1000).toLocaleString() }), el("td", { text: r.actor }), el("td", { text: r.action }), el("td", { text: `${r.entity} #${r.entity_id}` }))))));
  }
}

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- wiring ----------
function wire() {
  $("btn-open").addEventListener("click", openDialog);
  $("btn-new").addEventListener("click", () => load(emptyModel(), { sourceId: state.sourceId, targetId: state.targetId }));
  $("btn-devin").addEventListener("click", devinDialog);
  $("btn-validate").addEventListener("click", () => guarded(async () => { if (await validate()) toast("Mapping is valid"); }));
  $("btn-save").addEventListener("click", () => guarded(save));
  $("btn-approve").addEventListener("click", () => guarded(approve));
  $("btn-new-source").addEventListener("click", sourceDialog);
  $("btn-import-target").addEventListener("click", () => guarded(importTarget));
  $("btn-automap").addEventListener("click", () => guarded(automap));
  $("btn-add-derived").addEventListener("click", () => {
    let name = "derived_" + (state.model.columns.length + 1);
    state.model.columns.push({ ordinal: 0, source: null, target: name, type: "string", nullable: false, rules: [{ op: "constant", value: "" }], scale: null });
    state.selected = state.model.columns.length - 1;
    changed();
  });
  $("source-select").addEventListener("change", (e) => { state.sourceId = e.target.value ? Number(e.target.value) : null; changed(); });
  $("target-select").addEventListener("change", (e) => { state.targetId = e.target.value ? Number(e.target.value) : null; state.selected = null; changed(); });
  $("source-filter").addEventListener("input", () => { renderSources(); requestAnimationFrame(drawLinks); });
  $("mapping-id").addEventListener("change", (e) => { state.model.metadata.mapping_id = e.target.value.trim(); changed(); });
  $("mapping-version").addEventListener("change", (e) => { state.model.metadata.mapping_version = e.target.value.trim(); changed(); });
  dropTarget($("new-target-drop"), addFromSource);
  const menu = $("more-menu");
  $("btn-more").addEventListener("click", (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; $("btn-more").setAttribute("aria-expanded", String(!menu.hidden)); });
  document.addEventListener("click", () => { menu.hidden = true; });
  menu.addEventListener("click", (e) => { const action = e.target.dataset.action; if (action) guarded(() => more(action)); });
  for (const id of ["source-columns", "target-columns"]) $(id).addEventListener("scroll", () => requestAnimationFrame(drawLinks), { passive: true });
  window.addEventListener("resize", () => requestAnimationFrame(drawLinks));
  window.addEventListener("beforeunload", (e) => { if (state.dirty && editable()) e.preventDefault(); });
}

async function start() {
  wire();
  if (!TOKEN) { toast("Open the URL printed by 'sql-exporter studio serve' (it carries the access token)", true); return; }
  await guarded(refresh);
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("studio-draft") || "null"); } catch (_) { saved = null; }
  if (saved && saved.model) load(saved.model, saved);
  else load(emptyModel(), { sourceId: state.catalog.sources[0]?.id ?? null, targetId: state.catalog.targets[0]?.id ?? null });
  if (saved && saved.model && (saved.status === "draft" || saved.status === null)) { state.dirty = saved.id === null; render(); }
}

start();

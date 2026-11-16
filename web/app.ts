type Dict = Record<string, unknown>;
type Actor = { name: string; org: string; roles: string[]; csrf: string };
type Issue = { field: string; code: string; detail: string; line?: string | null; source_row?: number };
type Preview = { hash: string; canonical: string; payload: Dict };
type Case = {
  id: string; kind: string; business_key: string; profile: string; bundle: string; status: string;
  claimant: string | null; claim_revision: number; revision: number; rows?: Dict[]; corrected?: Dict[];
  errors?: { error_code: string; message: string }[]; issues?: Issue[]; locations?: number[];
  source_hash?: string; original_attempt?: string; original_verdict?: string; original_delivery_pending?: boolean; reference_version?: string;
  identity?: Dict; binding?: Dict; evidence_ids?: string[]; candidate_ids?: string[]; excerpts?: Record<string, string>; editors?: string[];
  approval_current?: boolean; approval?: { expires: string; by: string; reference: string; destination: string; policy: number };
  preview?: Preview; attempts?: number; replay_cutoff?: string; delivery_reason?: string; blocking_reason?: string;
  operation?: string; receipt?: Dict; unverified_receipt?: Dict; retry_at?: string;
};
type Queue = {
  cases: (Pick<Case, "id" | "kind" | "business_key" | "status" | "claimant"> & {
    deadline: string | null; inserted: string;
  })[];
  groups: { id: string; code: string; field: string; case_count: number }[];
  case_count: number; row_count: number; order_count: number; line_count: number; next: string | null;
  snapshot_version: number;
};
type Job = { id: string; state: string; revision: number; csv_sha256: string; uploader: string; case_count?: number;
  row_count?: number; order_count?: number; diagnostics?: Dict[] };
type Health = { database: string; simulator?: string; inference: string; paused: boolean; access_hold: boolean;
  delivery_hold: boolean; hold_reason: string | null };
type Region = "queue" | "import" | "approval" | "delivery" | "history" | "admin";
type CaseTab = "evidence" | "correction" | "approval" | "delivery";

class ApiError extends Error {
  constructor(readonly status: number, readonly body: Dict) { super(String(body.detail ?? "Request failed.")); }
}

const byId = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const main = byId("main");
let actor: Actor | null = null;
let health: Health | null = null;
let region: Region = "queue";
let tab: CaseTab = "evidence";
let selected: Case | null = null;
let queue: Queue | null = null;
let group = "";
let caseState = "";
let own = false;
let nextPage = "";
let dirty = false;
let job: Job | null = null;
let busy = false;

const esc = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, char =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]!));
const json = (value: unknown) => esc(JSON.stringify(value, null, 2));
const has = (role: string) => actor?.roles.includes(role) ?? false;
const caption = (name: string) => name[0].toUpperCase() + name.slice(1);
const heading = (label: string) => `<h1 tabindex="-1">${esc(label)}</h1>`;
const badge = (state: string) => `<span class="status ${state === "Confirmed" ? "confirmed" : ["Held", "Outcome unknown", "Rejected"].includes(state) ? "attention" : ""}">${esc(state)}</span>`;
const button = (label: string, action: string, options = "") => `<button type="button" data-action="${esc(action)}" ${options}>${esc(label)}</button>`;
const sections: Record<Region, string> = { queue: "Failure queue", import: "Import", approval: "Approval inbox",
  delivery: "Delivery", history: "History", admin: "Administration" };
const editFields: Record<string, string> = { customer_reference: "Customer reference", ship_to_reference: "Ship-to reference",
  item_reference: "Item reference", unit_code: "Unit code", order_date: "Order date" };
const states = ["Ready for review", "Held", "Draft correction", "Pending approval", "Approved", "Queued",
  "Sending", "Confirmed", "Rejected", "Outcome unknown", "Cancelled"];

async function api<T>(url: string, body?: Dict): Promise<T> {
  const response = await fetch(url, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json", "X-CSRF-Token": actor?.csrf ?? "" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const value = await response.json();
  if (!response.ok) throw new ApiError(response.status, value);
  return value as T;
}

function message(text = "", variant = "") {
  byId("feedback").innerHTML = text ? `<p class="message ${variant}">${esc(text)}</p>` : "";
}

function focus(selector = "h1") { main.querySelector<HTMLElement>(selector)?.focus(); }

function error(error: unknown) {
  if (error instanceof ApiError && error.status === 401 && actor) {
    actor = null;
    dirty = false;
    selected = null;
    queue = null;
    login();
    message("Your session ended. Sign in to continue.", "warning");
    return;
  }
  const lines = error instanceof ApiError && Array.isArray(error.body.issues) ?
    (error.body.issues as Issue[]).map(issue => `${issue.field} — ${issue.detail}`) : [];
  const owner = error instanceof ApiError && error.body.claimant ? ` Current claimant: ${error.body.claimant}.` : "";
  message((error instanceof Error ? error.message : "The service could not be reached.") + owner +
    (lines.length ? " " + lines.join(" ") : ""), "error");
  if (lines.length) {
    const name = (error as ApiError).body.issues as Issue[];
    const form = byId<HTMLFormElement>("correction-form");
    const field = name[0].line ? `line-${Number(name[0].line)}-${name[0].field}` : name[0].field;
    const input = form?.elements.namedItem(field);
    if (input instanceof HTMLElement) { input.setAttribute("aria-invalid", "true"); input.focus(); }
  }
}

function login() {
  navbar();
  main.innerHTML = `${heading("Sign in")}
    <section class="card compact">
      <p>Import failed customer and order files, review the evidence, and confirm the destination outcome.</p>
      <form id="login-form">
        <div class="field"><label for="username">User name</label><input id="username" name="user" autocomplete="username" required></div>
        <div class="field"><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required></div>
        <button>Sign in</button>
      </form>
    </section>`;
  focus();
}

function navbar() {
  const nav = byId("navigation");
  nav.hidden = !actor;
  byId("session-controls").innerHTML = actor ? button("Sign out", "logout", 'class="secondary"') : "";
  byId("account").textContent = actor ?
    `Cedar Vale Trade Supply · ${actor.name} · ${actor.roles.map(caption).join(", ")}` :
    "Cedar Vale Trade Supply · Local simulator";
  if (!actor) {
    nav.innerHTML = "";
    byId("service-banner").hidden = true;
    return;
  }
  const visible = (key: string) => key === "import" ? has("analyst") :
    key === "admin" ? has("administrator") : key === "approval" ? has("approver") : true;
  nav.innerHTML = Object.entries(sections).filter(([key]) => visible(key)).map(([key, label]) =>
    button(label, "nav-" + key, key === region ? 'aria-current="page"' : "")).join("");
}

async function services() {
  health = await api<Health>("/api/status");
  const banner = byId("service-banner");
  banner.hidden = !health.access_hold && !health.delivery_hold && !health.paused && health.simulator !== "unavailable";
  banner.textContent = health.hold_reason ??
    (health.paused ? "New dispatch is paused." : health.simulator === "unavailable" ? "Simulator unavailable. Evidence remains available." : "");
}

async function confirmAction(title: string, copy: string, action: string) {
  const previous = document.activeElement;
  const dialog = byId<HTMLDialogElement>("confirm");
  dialog.setAttribute("aria-labelledby", "dialog-title");
  dialog.setAttribute("aria-describedby", "dialog-copy");
  byId("dialog-title").textContent = title;
  byId("dialog-copy").textContent = copy;
  byId("confirm-button").textContent = action;
  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise<boolean>(resolve => dialog.addEventListener("close", () => {
    if (previous instanceof HTMLElement) previous.focus();
    resolve(dialog.returnValue === "confirm");
  }, { once: true }));
}

async function leaveDraft() {
  if (!dirty) return true;
  if (!await confirmAction("Discard unsaved correction?", "Any already saved revision remains in case history.", "Discard unsaved changes")) return false;
  dirty = false;
  return true;
}

async function openRegion(next: Region, force = false) {
  if (!force && !await leaveDraft()) return;
  region = next;
  selected = null;
  queue = null;
  nextPage = "";
  group = "";
  caseState = next === "approval" ? "Pending approval" : "";
  navbar();
  message();
  await services();
  if (next === "import") renderIntake();
  else if (next === "history") await renderHistory();
  else if (next === "admin") await renderAdmin();
  else await refreshQueue();
  focus();
}

async function refreshQueue({ paginating = false } = {}) {
  const params = new URLSearchParams();
  if (caseState) params.set("status", caseState);
  if (group) params.set("group", group);
  if (own) params.set("own", "true");
  if (paginating && nextPage && queue) {
    params.set("after", nextPage);
    params.set("snapshot", String(queue.snapshot_version));
  }
  if (queue) message("Refreshing. The last confirmed queue is still visible.", "warning");
  else main.innerHTML = `${heading(sections[region])}<p>Loading case list…</p>`;
  try {
    const update = await api<Queue>("/api/cases?" + params);
    const changedWhilePaging = paginating && queue && update.snapshot_version !== queue.snapshot_version;
    if (paginating && queue && !changedWhilePaging) update.cases = [...queue.cases, ...update.cases];
    if (selected && (!paginating || changedWhilePaging)) {
      try {
        selected = await api<Case>("/api/cases/" + selected.id);
      } catch (failure) {
        if (failure instanceof ApiError && (failure.status === 404 || failure.status === 410)) selected = null;
        else throw failure;
      }
    }
    nextPage = update.next ?? "";
    queue = update;
    renderQueue();
    if (changedWhilePaging) message("The queue changed. The latest first page is shown.", "warning");
    else message();
    return true;
  } catch (failure) {
    if (queue && !(failure instanceof ApiError && failure.status === 401)) {
      message("Refresh failed. The queue shows the last confirmed data and may be stale.", "warning");
    } else {
      if (failure instanceof ApiError && failure.status === 423)
        main.innerHTML = `${heading(sections[region])}<section class="card"><p>Restored evidence is held until retention is applied.</p></section>`;
      error(failure);
    }
    return false;
  }
}

function renderQueue() {
  if (!queue) return;
  const choices = queue.groups.map(item => `<option value="${esc(item.id)}" ${item.id === group ? "selected" : ""}>${esc(item.code)} · ${esc(item.field)} (${item.case_count})</option>`).join("");
  const options = states.map(state => `<option ${state === caseState ? "selected" : ""}>${esc(state)}</option>`).join("");
  const items = queue.cases.map(item => `<li><button type="button" data-action="select-case" data-id="${item.id}" ${selected?.id === item.id ? 'aria-current="true"' : ""}>
    ${esc(item.business_key)} ${caption(item.kind)}<small>${badge(item.status)} ${item.claimant ? " · " + esc(item.claimant) : ""}</small>
    <small>Dispatch deadline: ${esc(item.deadline ?? "Unspecified")}</small>
    <small>Intake: ${esc(item.inserted)}</small>
  </button></li>`).join("");
  main.innerHTML = `${heading(sections[region])}
    <div class="metrics"><span><strong>${queue.case_count}</strong> cases</span><span><strong>${queue.order_count}</strong> orders</span>
    <span><strong>${queue.line_count}</strong> order lines</span><span><strong>${queue.row_count}</strong> source rows</span></div>
    <form class="filters" id="filters">
      <div class="field"><label for="group">Failure group</label><select id="group" name="group"><option value="">All groups</option>${choices}</select></div>
      <div class="field"><label for="case-state">Case state</label><select id="case-state" name="state"><option value="">All states</option>${options}</select></div>
      <label class="check"><input name="own" type="checkbox" ${own ? "checked" : ""}> My claims</label><button>Apply filters</button>
      ${button("Refresh queue", "refresh-queue", 'class="secondary"')}
      ${caseState || group || own ? button("Clear filters", "clear-filter", 'class="secondary"') : ""}
      ${group && (has("analyst") || has("approver") || has("auditor")) ? `<a class="download" href="/api/repair.json?group=${encodeURIComponent(group)}">Export group repair packet</a>` : ""}
    </form>
    <div class="workbench"><section class="card" aria-label="Cases">
    ${items ? `<ul class="case-list">${items}</ul>` : `<p>${caseState || group || own ? "No matching cases." : "No cases yet. Import a failed-entity bundle."}</p>`}
    ${nextPage ? button("Load more cases", "more", 'class="secondary"') : ""}
    </section><section class="card" id="case-detail" aria-label="Selected case"></section></div>`;
  renderCase();
}

function renderIntake() {
  main.innerHTML = `${heading("Import and validation")}
  <section class="card compact">
    <form id="intake-form">
      <div class="field"><label for="profile">Source profile</label><select id="profile" name="profile"><option value="cedar-v1">Cedar Vale · v1</option></select></div>
      <div class="field"><label for="csv">Failed entities CSV</label><input id="csv" name="csv" type="file" accept=".csv" required></div>
      <div class="field"><label for="manifest">Error manifest</label><input id="manifest" name="manifest" type="file" accept=".json" required></div>
      <p class="muted" id="file-count">Choose customers.csv or orders.csv and errors.json. Combined limit 20 MiB.</p>
      <button id="validate">Validate bundle</button>
      ${button("Cancel intake", "cancel-intake", 'class="secondary" hidden')}
    </form>
  </section><section class="card" id="job" hidden aria-label="Import result"></section>`;
  renderJob();
}

function renderJob() {
  const area = byId("job");
  if (!area || !job) return;
  area.hidden = false;
  area.innerHTML = `<h2>${esc(job.state)}${job.case_count === undefined ? "" : ` · ${job.case_count} case${job.case_count === 1 ? "" : "s"}`}</h2>
  ${job.row_count === undefined ? "" : `<p>${job.row_count} source rows · ${job.order_count ?? 0} orders</p>`}
  <p>Uploaded by ${esc(job.uploader)}</p><details><summary>Source digest</summary><code>${esc(job.csv_sha256)}</code></details>
  ${job.diagnostics ? job.diagnostics.map(d => `<p><strong>${esc(d.code)}</strong> ${esc(d.detail)}</p><pre>${json(d)}</pre>`).join("") : ""}
  <div class="actions"><a class="download" href="/api/intake/${job.id}/csv">Download original CSV</a>
  <a class="download" href="/api/intake/${job.id}/manifest">Download original manifest</a></div>`;
  main.querySelector<HTMLButtonElement>('[data-action="cancel-intake"]')!.hidden = job.state !== "Validating";
}

function compareRows(value: Case) {
  if (!value.rows?.length) return "<p>Source payload has expired or is unavailable.</p>";
  return value.rows.map((row, n) => `<div class="table-wrap">
    <table><caption>Source row ${value.locations?.[n] ?? n + 1}</caption><thead><tr><th scope="col">Field</th><th scope="col">Original</th>
    <th scope="col">Proposed</th></tr></thead><tbody>${Object.entries(row).map(([key, item]) =>
      `<tr><th scope="row">${esc(editFields[key] ?? key.replaceAll("_", " "))}</th><td>${esc(item ?? "Absent")}</td>
      <td>${esc(value.corrected?.[n]?.[key] ?? item ?? "Absent")}</td></tr>`).join("")}</tbody></table></div>`).join("");
}

function renderCase() {
  const target = byId("case-detail");
  if (!target) return;
  const value = selected;
  if (!value) { target.innerHTML = "<h2>Case evidence</h2><p>Select a case to inspect its source and review status.</p>"; return; }
  const controls = [
    ["evidence","Evidence"], ["correction","Correction"], ["approval","Approval"], ["delivery","Delivery"]
  ].map(([key, label]) => button(label, "tab-" + key, `class="secondary" ${tab === key ? 'aria-current="true"' : ""}`)).join("");
  let inner = "";
  const active = value.claimant === actor?.name && has("analyst");
  if (tab === "evidence") {
    const actions = has("analyst") ?
      (active ? button("Release my claim", "release", 'class="secondary"') : value.claimant || value.status === "Confirmed" || !value.rows ? "" : button("Claim case", "claim"))
      : "";
    inner = `<p>Claimant ${esc(value.claimant ?? "Unclaimed")} · Claim revision ${value.claim_revision}</p>
      <div class="actions">${actions}${has("analyst") && value.rows ? button("Request diagnosis", "diagnose", 'class="secondary"') : ""}</div>
      ${has("administrator") && value.claimant ? `<form id="reassign-form"><div class="field"><label for="target">Eligible analyst</label>
      <input id="target" name="analyst" required></div><div class="field"><label for="reassign-reason">Reassignment reason</label>
      <input id="reassign-reason" name="reason" maxlength="500" required></div><button>Reassign abandoned claim</button></form>` : ""}
      <h3>Source evidence</h3><p>Profile ${esc(value.profile)} · Reference ${esc(value.reference_version ?? "Unavailable")}</p>
      <p>Original attempt ${esc(value.original_attempt ?? "Unavailable")}</p>
      <ul>${[...(value.errors ?? []).map(item => `${item.error_code} — ${item.message}`),
        ...(value.issues ?? []).map(item => `${item.code} — ${item.field}${item.source_row ? ` (source row ${item.source_row})` : ""} — ${item.detail}`)].map(item => `<li>${esc(item)}</li>`).join("")}</ul>
      ${value.identity || value.binding ? `<details><summary>Identity and customer evidence</summary><pre>${json(value.identity ?? value.binding)}</pre></details>` : ""}
      ${value.candidate_ids?.length ? `<details><summary>Approved reference candidates</summary>
        <ul>${value.candidate_ids.map(id => `<li>${esc(id)}</li>`).join("")}</ul></details>` : ""}
      ${compareRows(value)}
      <section aria-label="AI advice"><h3>AI advice</h3><p>Provider disabled. Deterministic review is available.</p><div id="advice" aria-live="polite"></div></section>`;
  } else if (tab === "correction") {
    inner = `<h3>Correction editor</h3>${compareRows(value)}`;
    if (active && value.rows?.length && !value.original_delivery_pending && !["Queued", "Sending", "Outcome unknown", "Confirmed"].includes(value.status)) {
      const original = value.rows;
      const draft = value.corrected ?? original;
      const makeInput = (key: string, name: string, row: Dict, saved: Dict) =>
        `<div class="field"><label for="edit-${name}">${editFields[key]}</label>
        <input id="edit-${name}" name="${name}" maxlength="64" placeholder="${esc(row[key])}"
        value="${row[key] === saved[key] ? "" : esc(saved[key])}"></div>`;
      const headers = ["customer_reference","order_date"].filter(key => key in original[0]).map(key =>
        makeInput(key, key, original[0], draft[0])).join("");
      const lines = value.kind === "order" ? original.map((row, index) => {
        const number = Number(row.line_no);
        const fields = ["item_reference","unit_code","ship_to_reference"].map(key =>
          makeInput(key, `line-${number}-${key}`, row, draft[index])).join("");
        return `<fieldset><legend>Order line ${number}</legend>${fields}</fieldset>`;
      }).join("") : "";
      const inputs = headers + lines;
      const evidence = (value.evidence_ids ?? []).map(id =>
        `<label class="check"><input type="checkbox" name="evidence" value="${esc(id)}"> ${esc(id)}</label>`).join("");
      inner += `<form id="correction-form"><p>Blank fields preserve the current reviewed value. Enter an approved correction and its evidence.</p>
        ${inputs}<div class="field"><label for="why">Reason</label><textarea id="why" name="reason" rows="2" maxlength="1000" required></textarea></div>
        <fieldset><legend>Supporting evidence</legend>${evidence || "<p>No approved evidence available.</p>"}</fieldset>
        <div class="actions"><button value="save" class="secondary">Save revision</button>
        <button value="preview">Validate and submit preview</button>${button("Discard unsaved edits", "discard", 'class="secondary"')}</div></form>`;
    } else inner += "<p>Only the current analyst claimant can edit an available case.</p>";
  } else if (tab === "approval") {
    const self = value.editors?.includes(actor?.name ?? "");
    const approvable = value.status === "Pending approval" && has("approver") && !self && !!value.preview;
    inner = `<h3>Peer approval</h3><p>Revision ${value.revision} · Editors ${esc(value.editors?.join(", ") || "None")}</p>${compareRows(value)}
      ${value.preview ? `<details><summary>Exact destination payload and digest</summary><pre>${esc(value.preview.canonical)}</pre><code>${esc(value.preview.hash)}</code></details>` : "<p>No valid preview is awaiting approval.</p>"}
      ${value.approval ? `<p>Approved by ${esc(value.approval.by)} · Expires ${esc(value.approval.expires)} · Snapshot ${esc(value.approval.reference)} · Destination ${esc(value.approval.destination)} · Policy ${value.approval.policy}</p>` : ""}
      ${self ? "<p>This revision requires a different reviewer.</p>" : ""}
      ${approvable ? `<div class="actions">${button("Approve revision", "approve")}</div>
       <form id="return-form"><div class="field"><label for="return-reason">Return reason</label><input id="return-reason" name="reason" maxlength="500" required></div>
       <button class="secondary">Return for correction</button></form>` : ""}`;
  } else {
    const send = has("approver") && value.status === "Approved" && value.approval_current;
    const eligible = send && value.original_verdict === "fenced_no_write" && !!value.replay_cutoff &&
      Date.parse(value.replay_cutoff) > Date.now() && (value.attempts ?? 0) < 3 && !health?.delivery_hold && !health?.paused &&
      health?.simulator !== "unavailable";
    const retry = has("approver") && ["Rejected","Held"].includes(value.status) && value.approval_current && (value.attempts ?? 0) < 3 &&
      !health?.delivery_hold && !health?.paused;
    const waiting = value.retry_at && Date.parse(value.retry_at) > Date.now();
    inner = `<h3>Delivery and reconciliation</h3><p>Original import evidence ${esc(value.original_verdict ?? "Unverified")}</p>
      <p>Verified replay cutoff ${esc(value.replay_cutoff ?? "Unavailable")} · ${value.attempts ?? 0} of 3 workbench attempts used.</p>
      <p>${esc(value.delivery_reason || value.blocking_reason || (eligible ? "Eligible for an explicit Send." :
        "Check original delivery and approval evidence before sending."))}</p>
      ${value.receipt ? `<details><summary>Confirmed receipt</summary><pre>${json(value.receipt)}</pre></details>` : ""}
      ${value.unverified_receipt ? `<details><summary>Receipt awaiting verified readback</summary><pre>${json(value.unverified_receipt)}</pre></details>` : ""}
      ${value.operation ? `<details><summary>Technical details</summary><code>${esc(value.operation)}</code></details>` : ""}
      <div class="actions">${(has("analyst") || has("approver")) && value.rows && !["Queued","Sending","Confirmed"].includes(value.status) ?
        button("Reconcile evidence", "reconcile", 'class="secondary"') : ""}
      ${send ? button("Send approved revision", "send", eligible ? "" : "disabled") : ""}
      ${retry ? button("Resume delivery", "retry", waiting ? 'disabled class="secondary"' : 'class="secondary"') : ""}
      ${has("approver") && value.status === "Queued" ? button("Cancel queued delivery", "cancel", 'class="secondary"') : ""}
      ${button("Refresh case", "refresh-case", 'class="secondary"')}</div>`;
  }
  target.innerHTML = `<h2 tabindex="-1">${esc(value.business_key)}</h2><p>${badge(value.status)} · ${esc(caption(value.kind))}</p>
    <div class="tabs" role="group" aria-label="Case sections">${controls}</div>${inner}`;
}

async function selectCase(id: string) {
  if (!await leaveDraft()) return;
  if (selected) message("Refreshing the selected case. Last confirmed evidence remains visible.", "warning");
  try {
    selected = await api<Case>("/api/cases/" + id);
  } catch (failure) { error(failure); return; }
  tab = region === "approval" ? "approval" : region === "delivery" ? "delivery" : "evidence";
  renderQueue();
  message();
  focus("#case-detail h2");
}

async function caseAction(action: string, extra: Dict = {}) {
  if (!selected) return;
  const value = selected;
  const path = "/api/cases/" + value.id;
  const bound = { revision: value.revision, claim_revision: value.claim_revision, preview_hash: value.preview?.hash, ...extra };
  if (action === "refresh-case") {
    if (!await leaveDraft()) return;
    selected = await api<Case>(path); renderCase(); message("Case refreshed."); focus("#case-detail h2"); return;
  }
  if (action === "diagnose") {
    const result = await api<{reason: string; revision: number; advice?: Dict; supporting_values?: Dict}>(path + "/diagnose", bound);
    message(result.reason, "warning");
    if (result.advice && selected?.id === value.id && selected.revision === result.revision) {
      const advice = byId("advice");
      if (advice) advice.innerHTML = `<p>Offline test response · schema ${esc(result.advice.schema_version)} · ${esc(result.advice.disposition)}</p>
        <p>${esc(result.advice.explanation)}</p><details><summary>Supporting evidence and proposal</summary>
        <pre>${json({ advice: result.advice, supporting_values: result.supporting_values })}</pre></details>`;
    }
    return;
  }
  if (action === "send" || action === "retry") {
    if (!await confirmAction("Confirm delivery", "Send the approved complete payload to the local simulator?", "Send")) return;
  } else if (action === "approve") {
    if (!await confirmAction("Approve the exact revision?", "Approval is recorded for 30 minutes. Use Send separately to start delivery.", "Approve")) return;
  }
  await api<Case>(path + "/" + action, bound);
  dirty = false;
  if (!await refreshQueue()) return;
  const notice: Record<string, string> = { claim: "Claim saved.", release: "Claim released.", reconcile: "Evidence checked.",
    save: "Draft revision saved.", preview: "Preview submitted for peer approval.", approve: "Approval recorded.",
    send: "Delivery queued.", cancel: "Queued delivery cancelled.", retry: "Delivery queued for resumption." };
  message(notice[action] ?? "Case updated.");
  focus("#case-detail h2");
}

async function renderHistory() {
  const value = await api<{ events: { seq: number; actor: string; case_id: string; event: string; recorded: string; detail: Dict }[] }>("/api/history");
  main.innerHTML = `${heading("History and exports")}<section class="card">
    ${has("auditor") || has("approver") || has("administrator") ? '<a class="download" href="/api/audit.csv">Download redacted audit CSV</a>' : ""}
    ${value.events.length ? `<div class="table-wrap"><table><thead><tr><th scope="col">Time (UTC)</th><th scope="col">Actor</th><th scope="col">Event</th><th scope="col">Evidence</th></tr></thead>
    <tbody>${value.events.map(row => `<tr><td>${esc(row.recorded)}</td><td>${esc(row.actor)}</td><td>${esc(row.event)}</td>
    <td><details><summary>Event ${row.seq}</summary><p>${esc(row.case_id)}</p><pre>${json(row.detail)}</pre></details></td></tr>`).join("")}</tbody></table></div>`
    : "<p>No events yet.</p>"}</section>`;
}

async function renderAdmin() {
  if (!health) return;
  const admin = health.access_hold ? null : await api<{reference: Dict; users: { name: string; roles: string[]; active: boolean }[]}>("/api/admin");
  main.innerHTML = `${heading("Administration and service health")}
    <section class="card"><h2>Service readiness</h2><p>Database ${esc(health.database)} · Simulator ${esc(health.simulator ?? "unknown")} · AI ${esc(health.inference)}</p>
      <p>Retention access ${health.access_hold ? "Held" : "Open"} · Delivery recovery ${health.delivery_hold ? "Held" : "Open"}</p>
      <div class="actions">${button("Apply retention", "retention", 'class="secondary"')}
      ${health.delivery_hold && !health.access_hold ? button("Reconcile restore delivery", "recovery") : ""}
      ${!health.access_hold ? button(health.paused ? "Resume dispatch" : "Pause new dispatch", "pause", 'class="secondary"') : ""}</div>
    </section>${admin ? `<section class="card"><h2>Reference snapshot</h2>
      <p>${esc(admin.reference.version)} · ${esc(admin.reference.contract)} · ${esc(admin.reference.created)}</p>
      <form id="reference-form"><div class="field"><label for="reference-file">Approved reference snapshot JSON</label>
      <input id="reference-file" type="file" accept=".json" required></div><button>Publish new version</button></form></section>
      <section class="card"><h2>Users</h2><ul>${admin.users.map(user => `<li>${esc(user.name)} · ${esc(user.roles.join(", "))} · ${user.active ? "Active" : "Inactive"}</li>`).join("")}</ul>
      <form id="user-form" class="compact"><div class="field"><label for="admin-user">User name</label><input id="admin-user" name="name" required></div>
      <fieldset><legend>Roles (select one or more)</legend>
      <label class="check"><input id="admin-role-analyst" name="role" type="checkbox" value="analyst"> Analyst</label>
      <label class="check"><input name="role" type="checkbox" value="approver"> Approver</label>
      <label class="check"><input name="role" type="checkbox" value="administrator"> Administrator</label>
      <label class="check"><input name="role" type="checkbox" value="auditor"> Auditor</label></fieldset>
      <div class="field"><label for="admin-password">New password (optional for existing users)</label><input id="admin-password" type="password" name="password" minlength="12" autocomplete="new-password"></div>
      <label class="check"><input type="checkbox" name="active" checked> Active account</label><button>Save user authority</button></form></section>` : ""}`;
}

async function upload(form: HTMLFormElement) {
  const csv = byId<HTMLInputElement>("csv").files?.[0];
  const manifest = byId<HTMLInputElement>("manifest").files?.[0];
  if (!csv || !manifest || manifest.name !== "errors.json") { message("Select a CSV file and errors.json.", "error"); return; }
  if (csv.size + manifest.size > 20 * 1024 * 1024) { message("Bundle exceeds 20 MiB.", "error"); return; }
  const data = new FormData(form);
  const base64 = async (file: File) => {
    const input = new Uint8Array(await file.arrayBuffer());
    let buffer = "";
    for (let start = 0; start < input.length; start += 16384)
      buffer += String.fromCharCode(...input.subarray(start, start + 16384));
    return btoa(buffer);
  };
  const control = byId<HTMLButtonElement>("validate");
  control.disabled = true;
  try {
    message("Uploading the original files…");
    job = await api<Job>("/api/intake/start", { file_name: csv.name, profile: data.get("profile"),
      csv: await base64(csv), manifest: await base64(manifest) });
    renderJob();
    message("Validating bundle membership and format…");
    job = await api<Job>(`/api/intake/${job.id}/commit`, { revision: job.revision });
    renderJob();
    message("Validation finished.");
  } catch (failure) {
    if (failure instanceof ApiError && typeof failure.body.id === "string") { job = failure.body as unknown as Job; renderJob(); }
    error(failure);
  } finally { control.disabled = false; }
}

async function submit(form: HTMLFormElement, submitter: HTMLElement | null) {
  const formData = new FormData(form);
  if (form.id === "login-form") {
    actor = await api<Actor>("/api/login", Object.fromEntries(formData));
    await openRegion("queue", true);
  } else if (form.id === "intake-form") await upload(form);
  else if (form.id === "filters") {
    if (!await leaveDraft()) return;
    group = String(formData.get("group")); caseState = String(formData.get("state")); own = formData.has("own");
    await refreshQueue();
  } else if (form.id === "correction-form") {
    const patch: Dict = Object.fromEntries(["customer_reference", "order_date"]
      .filter(key => formData.get(key)).map(key => [key, formData.get(key)]));
    const lines: Record<string, Dict> = {};
    for (const [key, value] of formData) {
      const field = /^line-([0-9]+)-(.+)$/.exec(key);
      if (field && value) (lines[field[1]] ??= {})[field[2]] = value;
    }
    if (Object.keys(lines).length) patch.lines = lines;
    await caseAction(submitter instanceof HTMLButtonElement ? submitter.value : "preview", {
      patch, reason: formData.get("reason"), evidence: formData.getAll("evidence")
    });
  } else if (form.id === "reassign-form" || form.id === "return-form") {
    await caseAction(form.id === "reassign-form" ? "reassign" : "return", Object.fromEntries(formData));
  } else if (form.id === "reference-form") {
    const file = byId<HTMLInputElement>("reference-file").files?.[0];
    if (!file) return;
    await api("/api/references", JSON.parse(await file.text()) as Dict);
    await renderAdmin();
    message("Reference version published.");
  } else if (form.id === "user-form") {
    const roles = formData.getAll("role");
    if (!roles.length) { message("Select at least one role.", "error"); focus("#admin-role-analyst"); return; }
    const password = formData.get("password");
    await api("/api/users", { name: formData.get("name"), roles, active: formData.has("active"),
      ...(password ? { password } : {}) });
    await renderAdmin();
    message("User authority saved.");
  }
}

async function act(node: HTMLElement) {
  const action = node.dataset.action ?? "";
  if (action.startsWith("nav-")) return openRegion(action.slice(4) as Region);
  if (action.startsWith("tab-")) {
    if (!await leaveDraft()) return;
    tab = action.slice(4) as CaseTab;
    renderCase();
    main.querySelector<HTMLElement>(`[data-action="${action}"]`)?.focus();
    return;
  }
  if (action === "logout") {
    if (!await leaveDraft()) return;
    await api("/api/logout", {});
    actor = null; queue = null; selected = null; job = null; own = false;
    message(); login(); return;
  }
  if (action === "select-case" && node.dataset.id) return selectCase(node.dataset.id);
  if (action === "refresh-queue" || action === "more" || action === "clear-filter") {
    if (!await leaveDraft()) return;
    if (action === "clear-filter") { group = ""; caseState = ""; own = false; }
    const refreshed = await refreshQueue({ paginating: action === "more" });
    if (refreshed && action === "refresh-queue") focus(selected ? "#case-detail h2" : "h1");
    return;
  }
  if (action === "discard") { if (await leaveDraft()) renderCase(); return; }
  if (action === "cancel-intake" && job) {
    job = await api<Job>(`/api/intake/${job.id}/cancel`, { revision: job.revision }); renderJob();
    message(job.state === "Cancelled" ? "Intake cancelled." : "Intake already finished."); return;
  }
  if (["pause","recovery","retention"].includes(action)) {
    await api(action === "pause" ? "/api/dispatch-pause" : "/api/" + action, action === "pause" ? { paused: !health?.paused } : {});
    await services(); await renderAdmin(); message("Service state updated."); return;
  }
  return caseAction(action);
}

document.addEventListener("submit", event => {
  const target = event.target;
  if (!(target instanceof HTMLFormElement) || target.method === "dialog") return;
  event.preventDefault();
  if (busy) return;
  busy = true;
  void submit(target, event.submitter as HTMLElement | null).catch(error).finally(() => { busy = false; });
});
document.addEventListener("click", event => {
  const target = event.target as HTMLElement;
  const node = target.closest<HTMLElement>("button[data-action]");
  if (!node || (busy && node.dataset.action !== "cancel-intake")) return;
  const concurrent = busy;
  busy = true;
  void act(node).catch(error).finally(() => { busy = concurrent; });
});
document.addEventListener("input", event => {
  const target = event.target;
  if (target instanceof HTMLElement && target.closest("#correction-form")) dirty = true;
});
document.addEventListener("change", event => {
  const target = event.target;
  if (target instanceof HTMLInputElement && target.type === "file" && target.closest("#intake-form")) {
    const csv = byId<HTMLInputElement>("csv").files?.[0];
    const manifest = byId<HTMLInputElement>("manifest").files?.[0];
    if (csv || manifest) byId("file-count").textContent =
      `${[csv?.name, manifest?.name].filter(Boolean).join(" + ")} · ${((csv?.size ?? 0) + (manifest?.size ?? 0)).toLocaleString()} bytes · limit 20 MiB`;
  }
});
window.addEventListener("beforeunload", event => { if (dirty) event.preventDefault(); });
void api<Actor>("/api/session").then(async session => { actor = session; await openRegion("queue", true); })
  .catch(failure => { login(); if (!(failure instanceof ApiError && failure.status === 401)) error(failure); });

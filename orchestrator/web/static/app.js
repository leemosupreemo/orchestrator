"use strict";

// ---------------------------------------------------------------- helpers

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let state = { project: null, runs: [], actions: {} };
let cleanup = [];

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(`/api/${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json", "X-Orchestrator-UI": "1" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast${bad ? " bad" : ""}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}

function ago(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function statusPill(status) {
  const s = String(status || "unknown");
  const cls = ({ completed: "ok", "review-needed": "warn", debugging: "bad", failed: "bad", "build-failed": "bad",
                 planned: "run", scheduled: "run", running: "run", "in-progress": "run" })[s] || "";
  return `<span class="pill ${cls}">${esc(s.replace(/-/g, " "))}</span>`;
}

function runPill(run) {
  if (run.running) return `<span class="pill run">running</span>`;
  return run.exit_code === 0 ? `<span class="pill ok">done</span>` : `<span class="pill bad">exit ${esc(run.exit_code)}</span>`;
}

function setHeader(title, sub = "", actionsHtml = "") {
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub;
  $("#topbar-actions").innerHTML = actionsHtml;
  document.title = `${title} · Orchestrator`;
}

// ---------------------------------------------------------------- running actions

// Size of the terminal a new run will be shown in, so full-screen programs
// (the console) draw at the right width from their first frame. Uses the last
// fitted size when there is one, otherwise estimates from the viewport.
let lastTermSize = null;
function terminalSize() {
  if (lastTermSize) return lastTermSize;
  const phone = window.innerWidth < 760;
  const width = phone ? window.innerWidth - 48 : window.innerWidth - 232 - 56 - 16;
  const cellW = phone ? 7.2 : 7.8, cellH = phone ? 15 : 17;
  const height = phone ? window.innerHeight - 330 : window.innerHeight - 250;
  return { cols: Math.max(40, Math.floor(width / cellW)), rows: Math.max(12, Math.floor(Math.max(height, 260) / cellH)) };
}

async function runAction(action, params = {}) {
  const meta = state.actions[action] || {};
  if (meta.confirm && !(await confirmDialog(meta.title, `<p>${esc(meta.confirm)}</p>`, "Continue"))) return;
  try {
    const { run } = await api("runs", { method: "POST", body: { action, params, ...terminalSize() } });
    location.hash = `#/runs/${run.id}`;
    refreshState();
  } catch (e) {
    toast(e.message, true);
  }
}

function formDialog(title, bodyHtml, okLabel = "Run") {
  const dlg = $("#dialog");
  $("#dialog-title").textContent = title;
  $("#dialog-body").innerHTML = bodyHtml;
  $("#dialog-ok").textContent = okLabel;
  dlg.returnValue = "";
  dlg.showModal();
  const first = $("#dialog-body input, #dialog-body textarea, #dialog-body select");
  if (first) first.focus();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      if (dlg.returnValue !== "ok") return resolve(null);
      resolve(Object.fromEntries(new FormData($("#dialog-form")).entries()));
    }, { once: true });
  });
}

async function confirmDialog(title, bodyHtml, okLabel) {
  return (await formDialog(title, bodyHtml, okLabel)) !== null;
}

async function fixDialog(jobId) {
  const values = await formDialog("Quick fix", `
    <label class="field"><span>What's wrong?</span>
      <textarea name="feedback" required placeholder="e.g. The lobby timer doesn't reset after a rematch"></textarea>
      <small>${jobId ? "Re-opens this job with your feedback and runs a fix." : "Creates a quick bug job and runs it."}</small>
    </label>`);
  if (values && values.feedback.trim()) runAction("fix", { feedback: values.feedback, job: jobId || "" });
}

async function distributeDialog() {
  const values = await formDialog("Build & distribute", `
    <p class="muted">${esc(state.actions.distribute?.confirm || "")}</p>
    <label class="field"><span>Release notes</span><textarea name="notes" placeholder="Optional"></textarea></label>`, "Distribute");
  if (values) {
    const meta = state.actions.distribute;
    state.actions.distribute = { ...meta, confirm: null }; // already confirmed in this dialog
    await runAction("distribute", { notes: values.notes });
    state.actions.distribute = meta;
  }
}

async function debugDialog(jobId) {
  const values = await formDialog("Debug job", `
    <label class="field"><span>Logs to include</span>
      <select name="logs">
        <option value="">Whatever is already linked</option>
        <option value="cloud:latest">Newest device launch (pulled fresh)</option>
      </select>
      <small>Device logs need <code>orchestrator logs setup</code>.</small>
    </label>
    <label class="field"><span>Feedback</span>
      <textarea name="feedback" placeholder="Optional: what you saw when you retested"></textarea>
    </label>`);
  if (values) runAction("debug", { job: jobId, logs: values.logs, feedback: values.feedback });
}

document.addEventListener("click", (e) => {
  const stop = e.target.closest("[data-stop]");
  if (stop) {
    api(`runs/${encodeURIComponent(stop.dataset.stop)}/stop`, { method: "POST", body: {} }).catch((err) => toast(err.message, true));
    return;
  }
  if (e.target.closest("[data-back]")) return history.back();
  const el = e.target.closest("[data-action]");
  if (!el) return;
  e.preventDefault();
  const action = el.dataset.action;
  const params = el.dataset.params ? JSON.parse(el.dataset.params) : {};
  if (action === "fix") return fixDialog(params.job);
  if (action === "distribute") return distributeDialog();
  if (action === "debug") return debugDialog(params.job);
  runAction(action, params);
});

// ---------------------------------------------------------------- state & project

async function refreshState() {
  try {
    state = await api("state");
  } catch (e) {
    if (e.status === 401) return showLocked(e.message);
    return;
  }
  const running = state.runs.filter((r) => r.running).length;
  const badge = $("#running-badge");
  badge.hidden = running === 0;
  badge.textContent = running;
  renderProjectSelect($("#project-select"));
}

function renderProjectSelect(select) {
  if (!select || !state.project) return;
  const p = state.project;
  const options = [...(p.recent || [])];
  if (!options.some((o) => o.root === p.root)) options.unshift({ name: p.name, root: p.root });
  const html = options.map((o) => `<option value="${esc(o.root)}" ${o.root === p.root ? "selected" : ""}>${esc(o.name)}</option>`).join("");
  if (select.dataset.html !== html) {
    select.innerHTML = html;
    select.dataset.html = html;
  }
}

async function switchProject(root) {
  try {
    await api("project", { method: "POST", body: { root } });
    await refreshState();
    toast(`Switched to ${state.project.name}`);
    route();
  } catch (e) {
    toast(e.message, true);
  }
}

document.addEventListener("change", (e) => {
  if (e.target.matches("#project-select, .project-select-inline")) switchProject(e.target.value);
});

function showLocked(message) {
  setHeader("Locked");
  view.innerHTML = `<div class="notice bad">${esc(message)}</div>`;
}

// ---------------------------------------------------------------- pages

const pages = {};

pages.home = async () => {
  const [{ jobs }] = await Promise.all([api("jobs"), refreshState()]);
  const p = state.project;
  setHeader(p.name, p.root);
  const active = jobs.filter((j) => !["completed", "archived"].includes(j.status));
  const runs = state.runs.slice(0, 5);
  view.innerHTML = `
    <div class="mobile-only card"><div class="card-b">
      <label class="field"><span>Project</span><select class="project-select-inline"></select></label>
    </div></div>
    <div class="stats">
      <div class="card stat"><div class="k">Branch</div><div class="v mono">${esc(p.branch || "—")}</div></div>
      <div class="card stat"><div class="k">Uncommitted files</div><div class="v">${p.dirty_files}</div></div>
      <div class="card stat"><div class="k">Open jobs</div><div class="v">${active.length}</div></div>
      <div class="card stat"><div class="k">Running now</div><div class="v">${state.runs.filter((r) => r.running).length}</div></div>
    </div>
    <section class="card">
      <div class="card-h"><h2>Quick actions</h2></div>
      <div class="card-b grid">
        <a class="btn tile" href="#/new"><strong>New job</strong><span>Bug, feature, design…</span></a>
        <button class="btn tile" data-action="fix"><strong>Quick fix</strong><span>Describe it, it runs</span></button>
        <button class="btn tile" data-action="logs_pull" ${p.remote_logs ? "" : "disabled"}><strong>Pull device logs</strong><span>${p.remote_logs ? "Newest app launch" : "Set up under Device logs"}</span></button>
        <button class="btn tile" data-action="build"><strong>Build</strong><span>Manual build, saved log</span></button>
        <button class="btn tile" data-action="test"><strong>Test</strong><span>Manual test run</span></button>
        <button class="btn tile" data-action="distribute" ${p.firebase_distribution ? "" : "disabled"}><strong>Distribute</strong><span>${p.firebase_distribution ? "Firebase to testers" : "Firebase not configured"}</span></button>
        <button class="btn tile" data-action="check"><strong>Setup check</strong><span>CLIs, auth, config</span></button>
        <button class="btn tile" data-action="console"><strong>Full console</strong><span>Everything else</span></button>
      </div>
    </section>
    <section class="card">
      <div class="card-h"><h2>Recent jobs</h2><a href="#/jobs">All jobs</a></div>
      <div class="list">${jobs.slice(0, 6).map(jobItem).join("") || `<div class="empty">No jobs yet. Start one with New job.</div>`}</div>
    </section>
    <section class="card">
      <div class="card-h"><h2>Recent runs</h2><a href="#/runs">All runs</a></div>
      <div class="list">${runs.map(runItem).join("") || `<div class="empty">Nothing has run from the UI yet.</div>`}</div>
    </section>`;
  renderProjectSelect($(".project-select-inline"));
};

function jobItem(j) {
  const progress = j.tasks_total ? ` · ${j.tasks_done}/${j.tasks_total} tasks` : "";
  const meta = [j.type, j.branch, ago(j.updated)].filter(Boolean).map(esc).join(" · ");
  return `<a class="item" href="#/jobs/${encodeURIComponent(j.id)}">
    <div class="main-col"><div class="title">${esc(j.title)}</div><div class="meta">${meta}${esc(progress)}</div></div>
    ${statusPill(j.status)}</a>`;
}

function runItem(r) {
  return `<a class="item" href="#/runs/${encodeURIComponent(r.id)}">
    <div class="main-col"><div class="title">${esc(r.title)}</div><div class="meta mono">${esc(r.command)}</div></div>
    <span class="muted">${esc(ago(r.started))}</span>${runPill(r)}</a>`;
}

const JOB_FILTERS = {
  open: (j) => !["completed", "archived"].includes(j.status),
  review: (j) => j.status === "review-needed",
  debugging: (j) => j.status === "debugging",
  completed: (j) => j.status === "completed",
  all: () => true,
};

pages.jobs = async (_, query) => {
  const filter = JOB_FILTERS[query.get("filter")] ? query.get("filter") : "open";
  const { jobs } = await api("jobs");
  setHeader("Jobs", `${jobs.length} total`, `<a class="btn primary" href="#/new">New job</a>`);
  const shown = jobs.filter(JOB_FILTERS[filter]);
  view.innerHTML = `
    <div class="filters">${Object.keys(JOB_FILTERS).map((f) =>
      `<a class="btn small ${f === filter ? "on" : ""}" href="#/jobs?filter=${f}">${f[0].toUpperCase() + f.slice(1)} (${jobs.filter(JOB_FILTERS[f]).length})</a>`).join("")}
    </div>
    <section class="card"><div class="list">${shown.map(jobItem).join("") || `<div class="empty">No jobs here.</div>`}</div></section>`;
};

function jobActions(summary) {
  const job = { job: summary.id };
  const p = (o) => `data-params='${esc(JSON.stringify(o))}'`;
  const buttons = [];
  const s = summary.status;
  if (s === "planned") buttons.push(`<button class="btn primary" data-action="schedule" ${p(job)}>Schedule & dispatch</button>`);
  if (s === "scheduled") buttons.push(`<button class="btn primary" data-action="execute" ${p(job)}>Execute</button>`);
  if (s === "debugging") buttons.push(`<button class="btn primary" data-action="debug" ${p(job)}>Auto-fix / debug</button>`);
  if (summary.tasks_total && summary.tasks_done < summary.tasks_total && ["debugging", "review-needed"].includes(s))
    buttons.push(`<button class="btn" data-action="resume" ${p(job)}>Resume next task</button>`);
  if (["review-needed", "completed"].includes(s)) {
    buttons.push(`<button class="btn" data-action="fix" ${p(job)}>Still broken? Fix</button>`);
    if (state.project?.firebase_distribution) buttons.push(`<button class="btn" data-action="distribute">Deliver to device</button>`);
  }
  if (s !== "debugging") buttons.push(`<button class="btn" data-action="debug" ${p(job)}>Debug</button>`);
  buttons.push(`<button class="btn ghost" data-action="console" title="Merge, revise plan and everything else">More in console</button>`);
  return buttons.join("");
}

pages.job = async ([id]) => {
  const { summary, job, outputs } = await api(`jobs/${encodeURIComponent(id)}`);
  if (!state.project) await refreshState();
  setHeader(summary.title, summary.id, jobActions(summary));
  const fields = [
    ["Status", statusPill(summary.status)], ["Type", esc(summary.type || "—")], ["Phase", esc(summary.phase || "—")],
    ["Branch", `<span class="mono">${esc(summary.branch || "—")}</span>`],
    ["Issue", summary.issue_number ? `#${esc(summary.issue_number)}` : "—"],
    ["PR", summary.pr_number ? `#${esc(summary.pr_number)}` : "—"],
    ["Models", esc([job.planner, job.builder, job.reviewer].filter(Boolean).join(" / ") || "—")],
    ["Updated", esc(ago(summary.updated))],
  ];
  const tasks = Array.isArray(job.tasks) ? job.tasks : [];
  const done = new Set((job.completed_tasks || []).map(String));
  const logs = job.last_manual_log_paths || (job.last_manual_log_path ? [job.last_manual_log_path] : []);
  view.innerHTML = `
    <section class="card"><div class="card-b"><dl class="kv">${fields.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></div></section>
    ${job.summary || job.description ? `<section class="card"><div class="card-h"><h2>Summary</h2></div><div class="card-b">${esc(job.summary || job.description)}</div></section>` : ""}
    ${tasks.length ? `<section class="card"><div class="card-h"><h2>Tasks</h2><span class="muted">${done.size}/${tasks.length}</span></div>
      <div class="list">${tasks.map((t, i) => {
        const title = typeof t === "string" ? t : (t.title || t.name || t.description || `Task ${i + 1}`);
        const key = typeof t === "object" && t ? String(t.id ?? i) : String(i);
        return `<div class="item"><span>${done.has(key) || done.has(String(i)) ? "✅" : "○"}</span><div class="main-col"><div class="title">${esc(title)}</div></div></div>`;
      }).join("")}</div></section>` : ""}
    <section class="card"><div class="card-h"><h2>Linked logs</h2></div>
      <div class="list">${logs.map((l) => `<div class="item"><span class="mono">${esc(l)}</span></div>`).join("") || `<div class="empty">None. Debug with “Newest device launch” to pull some.</div>`}</div></section>
    <section class="card"><div class="card-h"><h2>Output files</h2></div>
      <div class="list">${outputs.map((o) => `<a class="item" href="#/file?path=${encodeURIComponent(o.path)}">
        <div class="main-col"><div class="title mono">${esc(o.path.split("/").slice(3).join("/") || o.path)}</div>
        <div class="meta">${(o.size / 1024).toFixed(1)} KB · ${esc(ago(o.mtime))}</div></div></a>`).join("") || `<div class="empty">No output yet.</div>`}</div></section>
    <section class="card"><details class="raw"><summary>Raw job JSON</summary><pre>${esc(JSON.stringify(job, null, 2))}</pre></details></section>`;
};

const JOB_TYPE_INFO = [
  ["bug", "Bug fix", "Identify and fix"],
  ["feature", "Feature", "Design-first plan"],
  ["quick", "Quick", "Small change or refactor"],
  ["design", "Design", "Prototype a UI"],
  ["coverage", "Coverage", "Add tests"],
];

pages.new = async () => {
  await refreshState();
  setHeader("New job", "Plans, then dispatches to a worker. Prompts that need you show up in the run's terminal.");
  view.innerHTML = `
    <form class="card card-b stack" id="new-job">
      <div class="field"><span>Type</span>
        <div class="segmented">${JOB_TYPE_INFO.map(([v, t, d], i) =>
          `<label><input type="radio" name="type" value="${v}" ${i === 0 ? "checked" : ""}>${t}<small>${d}</small></label>`).join("")}
        </div></div>
      <label class="field"><span>Summary</span>
        <input type="text" name="summary" required maxlength="500" placeholder="e.g. Rejoining a lobby after backgrounding shows an empty seat">
      </label>
      <label class="field"><span>Details / spec</span>
        <textarea name="spec" placeholder="Steps to reproduce, expected vs actual, acceptance criteria, links…"></textarea>
        <small>Optional. Saved as a spec file and handed to the planner.</small>
      </label>
      <label class="field"><span>Branch</span>
        <select name="branch_mode">
          <option value="">Default</option>
          <option value="new">New branch</option>
          <option value="current">Current branch</option>
          <option value="manual">No git actions</option>
        </select>
      </label>
      <label class="check"><input type="checkbox" name="no_dispatch"><span>Plan only<small class="muted">Don't dispatch to a worker after planning.</small></span></label>
      <label class="check"><input type="checkbox" name="yolo"><span>Autopilot<small class="muted">Skip confirmations and run post-build steps (e.g. distribution) automatically.</small></span></label>
      <label class="check"><input type="checkbox" name="free"><span>Free models only</span></label>
      <div class="row"><span class="spacer"></span><button class="btn primary" type="submit">Create job</button></div>
    </form>`;
  $("#new-job").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    runAction("new_job", {
      type: f.get("type"), summary: f.get("summary"), spec: f.get("spec"), branch_mode: f.get("branch_mode") || null,
      no_dispatch: f.has("no_dispatch"), yolo: f.has("yolo"), free: f.has("free"),
    });
  });
};

pages.devlogs = async () => {
  setHeader("Device logs", "Runtime logs from tester builds, via Sentry Logs");
  view.innerHTML = `<div class="empty">Loading app launches…</div>`;
  const { pulls, sessions } = await api("devlogs");
  if (!sessions.configured) {
    view.innerHTML = `
      <section class="card card-b stack">
        <p>Not set up for this project yet. Setup finds the Sentry DSN in the repo, asks for a read-only token
        (scopes <code>org:read</code>, <code>project:read</code>, <code>event:read</code>), and checks it works.</p>
        <div><button class="btn primary" data-action="logs_setup">Set up device logs</button></div>
      </section>`;
    return;
  }
  setHeader("Device logs", "Runtime logs from tester builds, via Sentry Logs",
    `<button class="btn primary" data-action="logs_pull">Pull newest launch</button>
     <button class="btn" data-action="logs_tail">Follow live</button>`);
  const params = (s) => `data-params='${esc(JSON.stringify({ session: s }))}'`;
  view.innerHTML = `
    ${sessions.error ? `<div class="notice bad">${esc(sessions.error)}</div>` : ""}
    <section class="card"><div class="card-h"><h2>App launches (last 24h)</h2></div>
      <div class="list">${sessions.items.map((s) => `
        <div class="item">
          <div class="main-col"><div class="title mono">${esc(s.session)}</div>
            <div class="meta">${esc([s.channel, s.version && `v${s.version} (${s.build})`, s.device, s.user && `user ${s.user}`, s.started].filter(Boolean).join(" · "))}</div></div>
          <button class="btn small" data-action="logs_pull" ${params(s.session)}>Pull</button>
          <button class="btn small ghost" data-action="logs_tail" ${params(s.session)}>Follow</button>
        </div>`).join("") || `<div class="empty">No launches found. Open a tester build, wait a few seconds, then reload.</div>`}
      </div></section>
    <section class="card"><div class="card-h"><h2>Pulled logs</h2></div>
      <div class="list">${pulls.map((p) => `<a class="item" href="#/file?path=${encodeURIComponent(p.path)}">
        <div class="main-col"><div class="title mono">${esc(p.session || p.dir)}</div>
        <div class="meta">${esc(p.rows ?? "?")} lines · ${esc(ago(p.mtime))}</div></div></a>`).join("") || `<div class="empty">Nothing pulled yet.</div>`}
      </div></section>`;
};

pages.runs = async () => {
  await refreshState();
  setHeader("Runs", "Everything started from this UI while it's been running");
  view.innerHTML = `<section class="card"><div class="list">${state.runs.map(runItem).join("") || `<div class="empty">No runs yet.</div>`}</div></section>`;
};

pages.file = async (_, query) => {
  const path = query.get("path") || "";
  const { text } = await api(`file?path=${encodeURIComponent(path)}`);
  setHeader(path.split("/").pop(), path, `<button class="btn" data-back>Back</button>`);
  view.innerHTML = `<section class="card"><pre class="file">${esc(text)}</pre></section>`;
};

// ---------------------------------------------------------------- terminal

const KEYS = [
  ["Enter", "\r"], ["Esc", "\x1b"], ["Tab", "\t"], ["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"],
  ["y", "y"], ["n", "n"], ["q", "q"], ["Ctrl-C", "\x03"], ["Ctrl-D", "\x04"],
];

pages.run = async ([id]) => {
  await refreshState();
  const run = state.runs.find((r) => r.id === id);
  if (!run) {
    setHeader("Run not found");
    view.innerHTML = `<div class="notice">This run isn't in memory (the UI server may have restarted). Transcripts are kept in <code>.orchestrator/logs/ui/</code>.</div>`;
    return;
  }
  const header = (r) => setHeader(r.title, r.command,
    `${runPill(r)} ${r.running ? `<button class="btn danger" data-stop="${esc(r.id)}">Stop</button>` : ""}`);
  header(run);
  view.innerHTML = `
    <div class="term-wrap">
      <div class="term" id="term"></div>
      <div class="keybar" id="keybar">${KEYS.map(([label], i) => `<button class="btn small" data-key="${i}">${esc(label)}</button>`).join("")}</div>
      <form class="term-input" id="term-input">
        <input type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Type a line and press Send (or type straight into the terminal)">
        <button class="btn">Send</button>
      </form>
    </div>`;

  const send = (data) => api(`runs/${id}/input`, { method: "POST", body: { data } }).catch((e) => toast(e.message, true));
  const termEl = $("#term");
  let write, fit = () => {};

  if (window.Terminal) {
    const term = new window.Terminal({
      convertEol: false, cursorBlink: true, fontSize: window.innerWidth < 760 ? 12 : 13,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", scrollback: 20000,
      theme: { background: "#0f1216" },
    });
    const fitAddon = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fitAddon) term.loadAddon(fitAddon);
    term.open(termEl);
    term.onData(send);
    write = (bytes) => term.write(bytes);
    let resizeTimer;
    fit = () => {
      if (!fitAddon) return;
      try { fitAddon.fit(); } catch { return; }
      lastTermSize = { cols: term.cols, rows: term.rows };
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => api(`runs/${id}/resize`, { method: "POST", body: { cols: term.cols, rows: term.rows } }).catch(() => {}), 150);
    };
    fit();
    if (window.innerWidth >= 760) term.focus();
    cleanup.push(() => term.dispose());
  } else {
    // CDN unreachable: plain text view with colour codes stripped.
    const pre = document.createElement("pre");
    pre.className = "term-fallback";
    termEl.appendChild(pre);
    const decoder = new TextDecoder();
    write = (bytes) => {
      pre.textContent += decoder.decode(bytes, { stream: true }).replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").replace(/\r/g, "");
      pre.scrollTop = pre.scrollHeight;
    };
  }

  window.addEventListener("resize", fit);
  cleanup.push(() => window.removeEventListener("resize", fit));

  $("#keybar").addEventListener("click", (e) => {
    const b = e.target.closest("[data-key]");
    if (b) send(KEYS[Number(b.dataset.key)][1]);
  });
  $("#term-input").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = e.target.querySelector("input");
    send(input.value + "\r");
    input.value = "";
  });

  const es = new EventSource(`/api/runs/${id}/stream?offset=0`);
  es.addEventListener("data", (e) => {
    const { data } = JSON.parse(e.data);
    const bin = atob(data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    write(bytes);
  });
  es.addEventListener("end", async () => {
    es.close();
    await refreshState();
    const done = state.runs.find((r) => r.id === id);
    if (done) header(done);
  });
  cleanup.push(() => es.close());
};

// ---------------------------------------------------------------- router

function parseHash() {
  const [path, qs] = (location.hash.replace(/^#/, "") || "/").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, query: new URLSearchParams(qs || "") };
}

async function route() {
  cleanup.forEach((fn) => { try { fn(); } catch {} });
  cleanup = [];
  const { parts, query } = parseHash();
  let page = "home", args = [], nav = "home";
  if (parts[0] === "jobs" && parts[1]) [page, args, nav] = ["job", [parts[1]], "jobs"];
  else if (parts[0] === "runs" && parts[1]) [page, args, nav] = ["run", [parts[1]], "runs"];
  else if (pages[parts[0]]) [page, nav] = [parts[0], parts[0]];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === nav));
  try {
    await pages[page](args, query);
  } catch (e) {
    if (e.status === 401) return showLocked(e.message);
    setHeader("Something went wrong");
    view.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
  }
}

window.addEventListener("hashchange", route);
refreshState().then(route);
setInterval(() => { if (!document.hidden) refreshState(); }, 5000);

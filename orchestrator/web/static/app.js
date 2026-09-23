"use strict";

// ---------------------------------------------------------------- helpers

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const attrJSON = (o) => esc(JSON.stringify(o));

let state = { project: null, runs: [], actions: {} };
let cleanup = [];
let current = { page: null, args: [], query: null, rendered: "" };

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

const pill = (tone, label) => `<span class="pill ${esc(tone)}">${esc(label)}</span>`;
const jobPill = (j) => j.active_run ? `<span class="pill working"><span class="dot"></span>Working</span>` : pill(j.state.tone, j.state.label);

function runPill(r) {
  if (r.running) return r.waiting ? pill("attention", "Waiting for you") : `<span class="pill working"><span class="dot"></span>Running</span>`;
  return r.exit_code === 0 ? pill("done", "Finished") : pill("failed", `Failed (exit ${r.exit_code})`);
}

// A <details> dropdown: `items` are [label, attrs, hint?] or "---".
function moreMenu(items, { label = "More", left = false } = {}) {
  const body = items.map((it) => it === "---" ? "<hr>" :
    `<button class="btn" ${it[1]}>${esc(it[0])}</button>${it[2] ? `<div class="hint">${esc(it[2])}</div>` : ""}`).join("");
  return `<details class="more${left ? " left" : ""}"><summary class="btn">${esc(label)} ▾</summary><div class="more-menu">${body}</div></details>`;
}
const act = (action, params) => `data-action="${action}"${params ? ` data-params='${attrJSON(params)}'` : ""}`;

// ---------------------------------------------------------------- running actions

// Size of the terminal a new run will be shown in, so full-screen programs
// (the console) draw at the right width from their first frame.
let lastTermSize = null;
function terminalSize() {
  if (lastTermSize) return lastTermSize;
  const phone = window.innerWidth < 760;
  const width = phone ? window.innerWidth - 48 : window.innerWidth - 232 - 56 - 16;
  const cellW = phone ? 7.2 : 7.8, cellH = phone ? 15 : 17;
  const height = phone ? window.innerHeight - 380 : window.innerHeight - 260;
  return { cols: Math.max(40, Math.floor(width / cellW)), rows: Math.max(12, Math.floor(Math.max(height, 240) / cellH)) };
}

async function runAction(action, params = {}, { skipConfirm = false } = {}) {
  const meta = state.actions[action] || {};
  if (meta.confirm && !skipConfirm && !(await formDialog(meta.title, `<p>${esc(meta.confirm)}</p>`, "Continue"))) return;
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
  const first = $("#dialog-body textarea, #dialog-body input, #dialog-body select");
  if (first) first.focus();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      if (dlg.returnValue !== "ok") return resolve(null);
      resolve(Object.fromEntries(new FormData($("#dialog-form")).entries()));
    }, { once: true });
  });
}

const dialogs = {
  async fix(params) {
    const values = await formDialog(params.job ? "Still broken?" : "Fix something", `
      <label class="field"><span>What's wrong?</span>
        <textarea name="feedback" required placeholder="e.g. The lobby timer doesn't reset after a rematch"></textarea>
        <small>${params.job ? "Reopens this job with what you saw and runs a fix." : "Creates a quick bug job and runs it."}</small>
      </label>`, "Fix it");
    if (values?.feedback.trim()) runAction("fix", { feedback: values.feedback, job: params.job || "" });
  },
  async debug(params) {
    const hasLogs = state.project?.remote_logs;
    const values = await formDialog("Run fix", `
      ${hasLogs ? `<label class="check"><input type="checkbox" name="device_logs" checked>
        <span>Include newest device logs<small>Pulled fresh from the last app launch on a tester's phone.</small></span></label>` :
        `<p class="hint-text">Tip: set up Device logs to include what the app logged on your phone.</p>`}
      <label class="field"><span>What did you see? <span class="muted">(optional)</span></span>
        <textarea name="feedback" placeholder="e.g. Still shows the empty seat after 40s in the background"></textarea>
      </label>`, "Run fix");
    if (values) runAction("debug", { job: params.job, logs: values.device_logs ? "cloud:latest" : "", feedback: values.feedback });
  },
  async answer(params) {
    const job = await api(`jobs/${encodeURIComponent(params.job)}`);
    const values = await formDialog("Answer the planner", `
      <p class="question">${esc(job.summary.question || "")}</p>
      <label class="field"><span>Your answer</span><textarea name="answer" required></textarea>
        <small>The job is re-planned with your answer; you'll approve the new plan before anything is built.</small></label>`, "Send answer");
    if (values?.answer.trim()) runAction("answer", { job: params.job, answer: values.answer });
  },
  async distribute() {
    const values = await formDialog("Distribute current branch", `
      <p>Builds <strong class="mono">${esc(state.project?.branch || "the checked-out branch")}</strong> and sends a real Firebase release to your testers.</p>
      <label class="field"><span>Release notes <span class="muted">(optional)</span></span><textarea name="notes"></textarea></label>`, "Distribute");
    if (values) runAction("distribute", { notes: values.notes }, { skipConfirm: true });
  },
};

document.addEventListener("click", (e) => {
  // Close any open "More" menu when clicking outside it or on one of its items.
  document.querySelectorAll("details.more[open]").forEach((d) => {
    if (!d.contains(e.target) || e.target.closest(".more-menu .btn")) d.open = false;
  });
  const link = e.target.closest("[data-href]");
  if (link) {
    location.hash = link.dataset.href;
    return;
  }
  const stop = e.target.closest("[data-stop]");
  if (stop) {
    api(`runs/${encodeURIComponent(stop.dataset.stop)}/stop`, { method: "POST", body: {} }).catch((err) => toast(err.message, true));
    return;
  }
  if (e.target.closest("[data-back]")) return history.back();
  const el = e.target.closest("[data-action]");
  if (!el || el.disabled) return;
  e.preventDefault();
  const action = el.dataset.action;
  const params = el.dataset.params ? JSON.parse(el.dataset.params) : {};
  if (dialogs[action]) return dialogs[action](params);
  runAction(action, params);
});

// ---------------------------------------------------------------- state & project

async function refreshState() {
  try {
    state = await api("state");
  } catch (e) {
    if (e.status === 401) showLocked(e.message);
    return;
  }
  const running = state.runs.filter((r) => r.running);
  const waiting = running.filter((r) => r.waiting).length;
  const badge = $("#running-badge");
  badge.hidden = running.length === 0;
  badge.textContent = waiting || running.length;
  badge.classList.toggle("working", waiting === 0);
  badge.title = waiting ? `${waiting} waiting for you` : `${running.length} running`;
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

document.addEventListener("change", async (e) => {
  if (!e.target.matches("#project-select, .project-select-inline")) return;
  try {
    await api("project", { method: "POST", body: { root: e.target.value } });
    await refreshState();
    toast(`Switched to ${state.project.name}`);
    route();
  } catch (err) {
    toast(err.message, true);
  }
});

function showLocked(message) {
  setHeader({ title: "Locked" });
  view.innerHTML = `<div class="notice bad">${esc(message)}</div>`;
}

function setHeader({ title, sub = "", actions = "" }) {
  $("#page-title").textContent = title;
  const subEl = $("#page-sub");
  subEl.innerHTML = sub;
  subEl.hidden = !sub;
  $("#topbar-actions").innerHTML = actions;
  document.title = `${title} · Orchestrator`;
}

// ---------------------------------------------------------------- shared pieces

function jobItem(j, { withAction = false } = {}) {
  const bits = [j.kind, j.tasks_total ? `${j.tasks_done}/${j.tasks_total} tasks` : "", ago(j.updated)].filter(Boolean);
  const meta = withAction && j.state.reason ? j.state.reason : bits.join(" · ");
  const next = withAction && j.state.next && !j.active_run
    ? `<button class="btn small ${["failed", "attention"].includes(j.state.tone) ? "primary" : ""}" ${act(j.state.next.action, { job: j.id })}>${esc(j.state.next.label)}</button>` : "";
  return `<div class="item">
    <a class="main-col" href="#/jobs/${encodeURIComponent(j.id)}"><div class="title">${esc(j.title)}</div><div class="meta">${esc(meta)}</div></a>
    <div class="side">${jobPill(j)}${next}</div></div>`;
}

function runItem(r) {
  return `<a class="item" href="#/runs/${encodeURIComponent(r.id)}">
    <div class="main-col"><div class="title">${esc(r.title)}</div><div class="meta">${esc(ago(r.started))}</div></div>
    <div class="side">${runPill(r)}</div></a>`;
}

function statusLine(p) {
  const running = state.runs.filter((r) => r.running).length;
  return `<span class="status-line"><span class="mono">${esc(p.branch || "no branch")}</span><span class="sep">·</span>
    <span>${p.dirty_files} uncommitted</span><span class="sep">·</span><span>${running} running</span></span>`;
}

function moreActionsMenu(p, opts) {
  return moreMenu([
    ["Pull device logs", p.remote_logs ? act("logs_pull") : `data-href="#/devlogs"`, p.remote_logs ? "Newest app launch" : "Set up first"],
    ["Build", act("build")],
    ["Run tests", act("test")],
    ["Distribute current branch", act("distribute"), "Firebase release of what's checked out"],
    "---",
    ["Setup check", act("check")],
    ["Open full console", act("console"), "Everything else"],
  ], opts);
}

// ---------------------------------------------------------------- pages
// Each page returns { title, sub, actions, html, after? }. Pages in LIVE are
// re-rendered by the poll when their output changes.

const pages = {};
const LIVE = new Set(["home", "jobs", "job", "activity"]);

pages.home = async () => {
  const { jobs } = await api("jobs");
  const p = state.project;
  const needs = jobs.filter((j) => j.state.group === "needs_you" && !j.active_run);
  const working = jobs.filter((j) => j.state.group === "working" || j.active_run);
  const done = jobs.filter((j) => j.state.group === "done").slice(0, 3);
  const waitingRuns = state.runs.filter((r) => r.running && r.waiting);
  const otherRuns = state.runs.filter((r) => r.running && !r.waiting && !r.job);

  const needsHtml = [...waitingRuns.map(runItem), ...needs.map((j) => jobItem(j, { withAction: true }))].join("");
  const workingHtml = [...otherRuns.map(runItem), ...working.map((j) => jobItem(j, { withAction: true }))].join("");
  return {
    title: p.name,
    sub: statusLine(p),
    html: `
      <label class="mobile-only project-inline"><span class="label">Project</span><select class="project-select-inline"></select></label>
      <div class="hero-actions">
        <a class="btn primary big desktop-only" href="#/new">New job</a>
        <button class="btn big" ${act("fix")}>Fix something</button>
        ${moreActionsMenu(p, { left: true })}
      </div>
      <section class="card">
        <div class="card-h"><h2>Needs you <span class="count">${needs.length + waitingRuns.length}</span></h2></div>
        <div class="list">${needsHtml || `<div class="empty">Nothing waiting on you.</div>`}</div>
      </section>
      ${workingHtml ? `<section class="card"><div class="card-h"><h2>In progress</h2></div><div class="list">${workingHtml}</div></section>` : ""}
      ${done.length ? `<section class="card"><div class="card-h"><h2>Recently done</h2><a href="#/jobs?filter=done">All</a></div>
        <div class="list">${done.map((j) => jobItem(j)).join("")}</div></section>` : ""}
      ${!jobs.length ? `<div class="notice">No jobs yet. <strong>New job</strong> plans work from a description; <strong>Fix something</strong> goes straight to a quick fix.</div>` : ""}`,
    after: () => renderProjectSelect($(".project-select-inline")),
  };
};

const JOB_FILTERS = {
  needs_you: ["Needs you", (j) => j.state.group === "needs_you" && !j.active_run],
  working: ["In progress", (j) => j.state.group === "working" || j.active_run],
  done: ["Done", (j) => j.state.group === "done"],
  all: ["All", () => true],
};

pages.jobs = async (_, query) => {
  const { jobs } = await api("jobs");
  let filter = query.get("filter");
  if (!JOB_FILTERS[filter]) filter = jobs.some(JOB_FILTERS.needs_you[1]) ? "needs_you" : "all";
  const shown = jobs.filter(JOB_FILTERS[filter][1]);
  return {
    title: "Jobs",
    actions: `<a class="btn primary" href="#/new">New job</a>`,
    html: `
      <div class="filters">${Object.entries(JOB_FILTERS).map(([key, [label, fn]]) =>
        `<a class="btn small ${key === filter ? "on" : ""}" href="#/jobs?filter=${key}">${label} (${jobs.filter(fn).length})</a>`).join("")}
      </div>
      <section class="card"><div class="list">${shown.map((j) => jobItem(j, { withAction: filter === "needs_you" })).join("") || `<div class="empty">No jobs here.</div>`}</div></section>`,
  };
};

function jobHeaderActions(s) {
  if (s.active_run) return "";
  const j = { job: s.id };
  const primary = s.state.next ? `<button class="btn primary" ${act(s.state.next.action, j)}>${esc(s.state.next.label)}</button>` : "";
  const items = [];
  if (s.state.next?.action !== "debug" && ["review-needed", "completed", "debugging", "failed"].includes(s.status)) items.push(["Run fix", act("debug", j), "Another automated fix attempt"]);
  if (["review-needed", "completed"].includes(s.status)) items.push(["Still broken?", act("fix", j), "Reopen with what you saw"]);
  if (s.tasks_total && s.tasks_done < s.tasks_total && s.state.next?.action !== "resume") items.push(["Resume next task", act("resume", j)]);
  if (s.branch && ["review-needed", "debugging"].includes(s.status)) items.push(["Deliver to testers", act("deliver", j), `Builds ${s.branch}`]);
  if (s.status === "scheduled" && s.state.next?.action !== "execute") items.push(["Run now", act("execute", j)]);
  if (items.length) items.push("---");
  items.push(["Open in console", act("console"), "Revise plan, approve design, and more"]);
  return primary + moreMenu(items);
}

pages.job = async ([id]) => {
  const { summary: s, job, outputs, logs, runs } = await api(`jobs/${encodeURIComponent(id)}`);
  const models = [["Planner", job.planner], ["Builder", job.builder], ["Reviewer", job.reviewer]].filter(([, m]) => m);
  const fields = [
    ["Kind", esc(s.kind)],
    ["Branch", s.branch ? `<span class="mono">${esc(s.branch)}</span>` : "—"],
    s.issue_number ? ["Issue", `#${esc(s.issue_number)}`] : null,
    s.pr_number ? ["Pull request", `#${esc(s.pr_number)}`] : null,
    models.length ? ["Models", esc(models.map(([r, m]) => `${r} ${m}`).join(" · "))] : null,
    ["Updated", esc(ago(s.updated))],
  ].filter(Boolean);
  const tasks = Array.isArray(job.tasks) ? job.tasks : [];
  const done = new Set((job.completed_tasks || []).map(String));
  const activeRun = runs.find((r) => r.running);
  const summary = { ...s, active_run: !!activeRun };
  const logFiles = logs.flatMap((l) => l.files.length
    ? l.files.map((f) => ({ label: l.files.length > 1 ? `${l.label} / ${f.split("/").pop()}` : l.label, path: f }))
    : [{ label: l.label }]);

  return {
    title: s.title,
    sub: `<span class="status-line">${jobPill(summary)}<span>${esc(activeRun ? "Running now." : s.state.reason)}</span></span>`,
    actions: jobHeaderActions(summary),
    html: `
      ${activeRun ? `<a class="banner attention" href="#/runs/${encodeURIComponent(activeRun.id)}"><p><strong>${esc(activeRun.title)}</strong> is ${activeRun.waiting ? "waiting for you" : "running"}.</p><span class="btn small">Open</span></a>` : ""}
      ${s.question ? `<section class="card"><div class="card-h"><h2>Question from the planner</h2></div><div class="card-b stack">
        <p class="question">${esc(s.question)}</p><div><button class="btn primary" ${act("answer", { job: s.id })}>Answer</button></div></div></section>` : ""}
      ${job.summary || job.description ? `<section class="card"><div class="card-h"><h2>Summary</h2></div><div class="card-b">${esc(job.summary || job.description)}</div></section>` : ""}
      ${tasks.length ? `<section class="card"><div class="card-h"><h2>Tasks</h2><span class="count">${done.size}/${tasks.length}</span></div>
        <div class="list">${tasks.map((t, i) => {
          const title = typeof t === "string" ? t : (t.title || t.name || t.description || `Task ${i + 1}`);
          const key = typeof t === "object" && t ? String(t.id ?? i) : String(i);
          const isDone = done.has(key) || done.has(String(i));
          return `<div class="item"><span aria-label="${isDone ? "done" : "to do"}">${isDone ? "✅" : "○"}</span><div class="main-col"><div class="title">${esc(title)}</div></div></div>`;
        }).join("")}</div></section>` : ""}
      <section class="card"><div class="card-b"><dl class="kv">${fields.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl></div></section>
      ${runs.length ? `<section class="card"><div class="card-h"><h2>Activity</h2></div><div class="list">${runs.map(runItem).join("")}</div></section>` : ""}
      <section class="card"><div class="card-h"><h2>Logs</h2></div>
        <div class="list">${logFiles.map((f) => f.path
          ? `<a class="item" href="#/file?path=${encodeURIComponent(f.path)}"><div class="main-col"><div class="title mono">${esc(f.label)}</div></div></a>`
          : `<div class="item"><div class="main-col"><div class="title">${esc(f.label)}</div></div></div>`).join("")
          || `<div class="empty">No logs linked. “Run fix” can include the newest device logs.</div>`}</div></section>
      ${outputs.length ? `<section class="card"><div class="card-h"><h2>Output files</h2></div>
        <div class="list">${outputs.map((o) => `<a class="item" href="#/file?path=${encodeURIComponent(o.path)}">
          <div class="main-col"><div class="title mono">${esc(o.path.split("/").slice(2).join("/") || o.path)}</div>
          <div class="meta">${(o.size / 1024).toFixed(1)} KB · ${esc(ago(o.mtime))}</div></div></a>`).join("")}</div></section>` : ""}
      <section class="card"><details class="raw"><summary>Technical details (${esc(s.id)})</summary><pre>${esc(JSON.stringify(job, null, 2))}</pre></details></section>`,
  };
};

const JOB_TYPE_INFO = [
  ["bug", "Bug fix", "Find and fix a problem"],
  ["feature", "Feature", "Plan, then build"],
  ["quick", "Quick change", "Small tweak or refactor"],
  ["design", "Design", "Prototype a screen"],
  ["coverage", "Tests", "Add missing tests"],
];

pages.new = async () => ({
  title: "New job",
  sub: "Describe the work. It gets planned, then built on a worker. Anything that needs your input shows up as it runs.",
  html: `
    <form class="card card-b stack" id="new-job">
      <div class="field"><span>Kind</span>
        <div class="segmented">${JOB_TYPE_INFO.map(([v, t, d], i) =>
          `<label><input type="radio" name="type" value="${v}" ${i === 0 ? "checked" : ""}>${t}<small>${d}</small></label>`).join("")}
        </div></div>
      <label class="field"><span>What should happen?</span>
        <input type="text" name="summary" required maxlength="500" placeholder="e.g. Rejoining a lobby after backgrounding shows an empty seat">
      </label>
      <label class="field"><span>Details <span class="muted">(optional)</span></span>
        <textarea name="spec" placeholder="Steps to reproduce, expected vs actual, acceptance criteria, links…"></textarea>
      </label>
      <details class="advanced"><summary>Options</summary>
        <div class="stack">
          <label class="field"><span>Where to work</span>
            <select name="branch_mode">
              <option value="new" selected>New branch for this job (recommended)</option>
              <option value="current">The branch that's checked out now</option>
              <option value="manual">Don't create or switch branches</option>
            </select>
          </label>
          <label class="check"><input type="checkbox" name="no_dispatch"><span>Plan only<small>Stop after planning so you can review the plan first.</small></span></label>
          <label class="check"><input type="checkbox" name="free"><span>Free models only</span></label>
          <label class="check risky"><input type="checkbox" name="yolo"><span>Autopilot<small>Skips every confirmation and runs follow-up steps automatically — including a real Firebase release to testers.</small></span></label>
        </div>
      </details>
      <div class="row"><span class="spacer"></span><button class="btn primary big" type="submit">Create job</button></div>
    </form>`,
  after: () => $("#new-job").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    runAction("new_job", {
      type: f.get("type"), summary: f.get("summary"), spec: f.get("spec"), branch_mode: f.get("branch_mode") || "new",
      no_dispatch: f.has("no_dispatch"), yolo: f.has("yolo"), free: f.has("free"),
    });
  }),
});

pages.devlogs = async () => {
  setHeader({ title: "Device logs" });
  view.innerHTML = `<div class="empty">Loading app launches…</div>`;
  const { pulls, sessions } = await api("devlogs");
  const base = { title: "Device logs", sub: "What the app logged on testers' phones" };
  if (!sessions.configured || /token|setup/i.test(sessions.error || "")) {
    const reason = (sessions.error || "").replace(/\s*Run: orchestrator logs setup\.?/i, "").trim();
    return { ...base, html: `
      <section class="card card-b stack">
        ${reason ? `<div class="notice bad">${esc(reason)}</div>` : ""}
        <p>${sessions.configured ? "Finish setup to read logs." : "Not set up for this project yet."} Setup finds the Sentry project in your repo,
        asks for a read-only token (scopes <code>org:read</code>, <code>project:read</code>, <code>event:read</code>), and checks it works.</p>
        <div><button class="btn primary" ${act("logs_setup")}>Set up device logs</button></div>
      </section>` };
  }
  const ok = !sessions.error;
  return {
    ...base,
    actions: `<button class="btn primary" ${act("logs_pull")} ${ok ? "" : "disabled"}>Pull newest launch</button>
              <button class="btn" ${act("logs_tail")} ${ok ? "" : "disabled"}>Follow live</button>`,
    html: `
      ${sessions.error ? `<div class="notice bad">${esc(sessions.error)}<div class="row"><button class="btn small" ${act("logs_setup")}>Re-run setup</button></div></div>` : ""}
      <section class="card"><div class="card-h"><h2>App launches, last 24h</h2></div>
        <div class="list">${sessions.items.map((s) => `
          <div class="item">
            <div class="main-col"><div class="title">${esc([s.device || "Unknown device", s.version && s.version !== "?" && `v${s.version} (${s.build})`].filter(Boolean).join(" · "))}</div>
              <div class="meta">${esc([s.channel !== "?" && s.channel, s.user && `player ${s.user}`, s.started && ago(Date.parse(s.started) / 1000)].filter(Boolean).join(" · "))}</div></div>
            <div class="side">
              <button class="btn small" ${act("logs_pull", { session: s.session })}>Pull</button>
              <button class="btn small ghost" ${act("logs_tail", { session: s.session })}>Follow</button>
            </div>
          </div>`).join("") || `<div class="empty">No launches yet. Open a tester build, wait a few seconds, then reload.</div>`}
        </div></section>
      <section class="card"><div class="card-h"><h2>Pulled logs</h2></div>
        <div class="list">${pulls.map((p) => `<a class="item" href="#/file?path=${encodeURIComponent(p.path)}">
          <div class="main-col"><div class="title">${esc(p.rows ?? "?")} lines</div>
          <div class="meta">launch ${esc(p.session || p.dir)} · ${esc(ago(p.mtime))}</div></div></a>`).join("") || `<div class="empty">Nothing pulled yet.</div>`}
        </div></section>`,
  };
};

pages.activity = async () => {
  const running = state.runs.filter((r) => r.running);
  const finished = state.runs.filter((r) => !r.running);
  return {
    title: "Activity",
    sub: "Everything started from this page since the UI was opened",
    html: `
      ${running.length ? `<section class="card"><div class="card-h"><h2>Running</h2></div><div class="list">${running.map(runItem).join("")}</div></section>` : ""}
      <section class="card"><div class="card-h"><h2>Finished</h2></div><div class="list">${finished.map(runItem).join("") || `<div class="empty">Nothing yet.</div>`}</div></section>`,
  };
};

pages.file = async (_, query) => {
  const path = query.get("path") || "";
  const { text } = await api(`file?path=${encodeURIComponent(path)}`);
  return {
    title: path.split("/").pop(),
    sub: esc(path),
    actions: `<button class="btn" data-back>Back</button>`,
    html: `<section class="card"><pre class="file">${esc(text)}</pre></section>`,
  };
};

// ---------------------------------------------------------------- run page (terminal)

const KEYS = [
  ["Enter", "\r"], ["Esc", "\x1b"], ["Tab", "\t"], ["↑", "\x1b[A"], ["↓", "\x1b[B"], ["←", "\x1b[D"], ["→", "\x1b[C"],
  ["y", "y"], ["n", "n"], ["q", "q"], ["Ctrl-C", "\x03"], ["Ctrl-D", "\x04"],
];

function runHeader(r) {
  const back = r.job ? `<a class="btn" href="#/jobs/${encodeURIComponent(r.job)}">Back to job</a>` : "";
  return {
    title: r.title,
    sub: `<span class="status-line">${runPill(r)}<span class="mono">${esc(r.command)}</span></span>`,
    actions: `${back}${r.running ? `<button class="btn danger" data-stop="${esc(r.id)}">Stop</button>` : ""}`,
  };
}

function nextStep(r) {
  const failed = r.exit_code !== 0;
  const target = r.result_job || r.job;
  const open = target ? `<a class="btn small primary" href="#/jobs/${encodeURIComponent(target)}">${r.result_job && !r.job ? "Open job" : "Back to job"}</a>` : "";
  const text = failed ? "This run failed. The output below shows why."
    : r.result_job && !r.job ? "Job created."
    : target ? "Done. The job page shows where it stands now."
    : r.action === "logs_pull" ? "Logs pulled." : "Done.";
  const extra = !failed && r.action === "logs_pull" ? `<a class="btn small" href="#/devlogs">See pulled logs</a>` : "";
  return `<div class="banner ${failed ? "failed" : "done"}"><p>${esc(text)}</p>${extra}${open}</div>`;
}

pages.run = async ([id]) => {
  const run = state.runs.find((r) => r.id === id);
  if (!run) {
    return { title: "Run not found", html: `<div class="notice">This run isn't in memory (the UI server may have restarted). Transcripts are kept in <code>.orchestrator/logs/ui/</code>.</div>` };
  }
  return {
    ...runHeader(run),
    html: `
      <div id="next-step"></div>
      <div class="term-wrap" id="term-wrap">
        <div class="term" id="term"></div>
        <div class="keybar" id="keybar" aria-label="Terminal keys">${KEYS.map(([label], i) => `<button class="btn small" data-key="${i}">${esc(label)}</button>`).join("")}</div>
        <form class="term-input" id="term-input">
          <input type="text" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Type into the terminal" placeholder="Type, then Send">
          <button class="btn">Send</button>
        </form>
      </div>`,
    after: () => attachTerminal(id),
  };
};

function attachTerminal(id) {
  const send = (data) => api(`runs/${id}/input`, { method: "POST", body: { data } }).catch((e) => toast(e.message, true));
  const termEl = $("#term");
  let write, fit = () => {};

  if (window.Terminal) {
    const term = new window.Terminal({
      cursorBlink: true, fontSize: window.innerWidth < 760 ? 12 : 13, scrollback: 20000,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", theme: { background: "#0f1216" },
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
      resizeTimer = setTimeout(() => api(`runs/${id}/resize`, { method: "POST", body: lastTermSize }).catch(() => {}), 150);
    };
    fit();
    if (window.innerWidth >= 760) term.focus();
    cleanup.push(() => term.dispose());
  } else {
    $("#term-wrap").classList.add("fallback");
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
    const bin = atob(JSON.parse(e.data).data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    write(bytes);
  });
  es.addEventListener("end", async () => {
    es.close();
    await refreshState();
    // new_job/fix may take a moment to report the job they created.
    let run = state.runs.find((r) => r.id === id);
    for (let i = 0; i < 3 && run && ["new_job", "fix"].includes(run.action) && !run.result_job; i++) {
      await new Promise((r) => setTimeout(r, 700));
      await refreshState();
      run = state.runs.find((r) => r.id === id);
    }
    if (!run || current.page !== "run" || current.args[0] !== id) return;
    setHeader(runHeader(run));
    $("#next-step").innerHTML = nextStep(run);
    // Nothing left to type into.
    $("#keybar").hidden = true;
    $("#term-input").hidden = true;
  });
  cleanup.push(() => es.close());
}

// ---------------------------------------------------------------- router

function parseHash() {
  const [path, qs] = (location.hash.replace(/^#/, "") || "/").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, query: new URLSearchParams(qs || "") };
}

function resolveRoute() {
  const { parts, query } = parseHash();
  if (parts[0] === "jobs" && parts[1]) return { page: "job", args: [parts[1]], nav: "jobs", query };
  if (parts[0] === "runs" && parts[1]) return { page: "run", args: [parts[1]], nav: "activity", query };
  if (parts[0] === "runs") return { page: "activity", args: [], nav: "activity", query };
  if (parts[0] === "file") return { page: "file", args: [], nav: null, query };
  if (pages[parts[0]]) return { page: parts[0], args: [], nav: parts[0], query };
  return { page: "home", args: [], nav: "home", query };
}

const signatureOf = (r) => `${r.title}|${r.sub}|${r.actions}|${r.html}`;

function apply(result) {
  setHeader(result);
  view.innerHTML = result.html;
  current.rendered = signatureOf(result);
  if (result.after) result.after();
}

async function route() {
  // Anything tied to the previous page goes: terminals, streams, and dialogs,
  // so an action can never run against a page you've left.
  cleanup.forEach((fn) => { try { fn(); } catch {} });
  cleanup = [];
  const dlg = $("#dialog");
  if (dlg.open) dlg.close("cancel");

  const r = resolveRoute();
  current = { page: r.page, args: r.args, query: r.query, rendered: "" };
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === r.nav));
  try {
    if (!state.project) await refreshState();
    const result = await pages[r.page](r.args, r.query);
    if (current.page === r.page && current.args.join() === r.args.join()) apply(result);
  } catch (e) {
    if (e.status === 401) return showLocked(e.message);
    setHeader({ title: "Something went wrong" });
    view.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
  }
}

// Live pages refresh in place — but never under the user's hands.
async function tick() {
  if (document.hidden) return;
  await refreshState();
  if (!LIVE.has(current.page) || $("#dialog").open || document.querySelector("details.more[open]")) return;
  if (view.contains(document.activeElement) && document.activeElement.matches("input, textarea, select")) return;
  const page = current.page, args = current.args.join();
  try {
    const result = await pages[page](current.args, current.query);
    if (current.page !== page || current.args.join() !== args) return;
    if (signatureOf(result) !== current.rendered) apply(result);
  } catch { /* keep showing the last good render */ }
}

window.addEventListener("hashchange", route);
refreshState().then(route);
setInterval(tick, 5000);

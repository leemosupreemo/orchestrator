"use strict";

const FIREBASE_CONFIG = {
  apiKey: "AIzaSyBg8h8yiC8OCoezFLEq6mQLhlc260b8CcI",
  authDomain: "swift-orch-web-20260923.firebaseapp.com",
  projectId: "swift-orch-web-20260923",
  storageBucket: "swift-orch-web-20260923.firebasestorage.app",
  messagingSenderId: "1093947686212",
  appId: "1:1093947686212:web:95cd975ed0d626ec196b11",
};

if (typeof window !== "undefined" && window.firebase && !window.firebase.apps?.length) {
  try {
    window.firebase.initializeApp(FIREBASE_CONFIG);
  } catch (e) {
    console.warn("Firebase initialization warning:", e);
  }
}

// True from the moment someone starts signing in until they are in (or told why not). While it is set, background
// refreshes leave the page alone, so the sign-in screen is replaced by a loading screen instead of being redrawn.
let signingIn = false;
let blockedProvider = null; // set when the browser blocks the sign-in window, so full-page sign-in can be offered (it fails in some browsers)
try { signingIn = Object.keys(sessionStorage).some((k) => k.startsWith("firebase:pendingRedirect") && sessionStorage.getItem(k) === "true"); } catch { /* storage blocked: nothing to resume */ }

function endSigningIn(resume = false) {
  signingIn = false;
  if (resume) refreshState().then(route);
}

if (typeof window !== "undefined" && window.firebase?.auth) {
  try {
    window.firebase.auth().getRedirectResult().then(async (cred) => {
      if (!(cred && cred.user)) { if (signingIn) endSigningIn(true); return; } // no redirect was pending after all
      if (cred && cred.user) {
        try {
          const idToken = await cred.user.getIdToken();
          const currentBackend = getBackendUrl();
          if (!currentBackend && window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost") {
            endSigningIn();
            showSignInGate("Signed in as " + (cred.user.email || "user") + ", but Backend URL is required. Enter your Mac's backend URL below.");
            return;
          }
          const res = await api("auth", { method: "POST", body: { id_token: idToken } });
          if (res.token) localStorage.setItem("orchestrator_token", res.token);
          toast(`Signed in as ${cred.user.email || "user"}`);
          document.querySelector(".app")?.classList.remove("session-locked");
          endSigningIn();
          await refreshState();
          route();
        } catch (apiErr) {
          endSigningIn();
          showSignInGate(`Signed in as ${cred.user.email || "user"}, but could not reach backend: ${apiErr.message}`);
        }
      }
    }).catch((err) => {
      if (err.code === "auth/popup-closed-by-user" || err.code === "auth/cancelled-popup-request") { if (signingIn) endSigningIn(true); return; }
      console.warn("Redirect sign-in notice:", err);
      endSigningIn();
      showSignInGate(/initial state|storage|partition/i.test(`${err.code} ${err.message}`)
        ? "Your browser kept this site from finishing full-page sign-in (it blocks the storage that step needs). Use the pop-up sign-in with pop-ups allowed, or the access token below."
        : (err.message || "Sign-in didn't finish. Try again."));
    });
  } catch (e) {
    console.warn("getRedirectResult error:", e);
  }
}

// ---------------------------------------------------------------- helpers

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const attrJSON = (o) => esc(JSON.stringify(o));

let state = { project: null, runs: [], actions: {} };
let cleanup = [];
let current = { page: null, args: [], query: null, rendered: "" };

const DEFAULT_REMOTE_BACKEND = "";

function getBackendUrl() {
  const params = new URLSearchParams(window.location.search);
  const qb = params.get("backend");
  if (qb) {
    const clean = qb.replace(/\/+$/, "");
    localStorage.setItem("orchestrator_backend", clean);
    return clean;
  }
  const stored = localStorage.getItem("orchestrator_backend");
  if (stored && stored.startsWith("http")) return stored;
  if (window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost") {
    return "";
  }
  return DEFAULT_REMOTE_BACKEND;
}

function getToken() {
  const params = new URLSearchParams(window.location.search);
  const qt = params.get("token");
  if (qt) {
    localStorage.setItem("orchestrator_token", qt);
    try {
      params.delete("token");
      const newSearch = params.toString() ? `?${params.toString()}` : "";
      const cleanUrl = `${window.location.pathname}${newSearch}${window.location.hash}`;
      window.history.replaceState({}, "", cleanUrl);
    } catch {}
    return qt;
  }
  return localStorage.getItem("orchestrator_token") || "";
}

async function api(path, { method = "GET", body } = {}) {
  const backend = getBackendUrl();
  const token = getToken();
  const url = `${backend ?? ""}/api/${path}`;
  const hasBody = body !== undefined;
  const isMutation = ["POST", "PUT", "DELETE", "PATCH"].includes(method.toUpperCase());
  const headers = {
    ...(hasBody || isMutation ? { "Content-Type": "application/json", "X-Orchestrator-UI": "1" } : {}),
    ...(token ? { "Authorization": `Bearer ${token}` } : {}),
  };
  const res = await fetch(url, {
    method,
    headers,
    body: hasBody ? JSON.stringify(body) : (isMutation ? "{}" : undefined),
    credentials: backend ? "omit" : "same-origin",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(message, bad = false, action = null) {
  const el = $("#toast");
  el.textContent = bad ? Errors.explain(message) : message;
  if (action) { // e.g. { label: "Undo", run: async () => … }: stays long enough to be noticed
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "toast-action"; btn.textContent = action.label;
    btn.addEventListener("click", async () => { el.hidden = true; try { await action.run(); } catch (e) { toast(e.message, true); } });
    el.append(" ", btn);
  }
  el.className = `toast${bad ? " bad" : ""}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, action ? 10000 : 3500);
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

function renderLanguagesBar(languages, { className = "", maxLabels = 3 } = {}) {
  if (!languages || !languages.length) return "";
  const segments = languages.map((l) => {
    const color = l.color || "#888";
    return `<span class="lang-bar-segment" style="width: ${l.percent}%; background-color: ${color};" title="${esc(l.name)}: ${l.percent}%"></span>`;
  }).join("");

  const displayLangs = languages.slice(0, maxLabels);
  const hasMore = languages.length > maxLabels;
  const labels = displayLangs.map((l) => {
    const color = l.color || "#888";
    return `<span class="lang-item" title="${esc(l.name)}: ${l.percent}%">
      <span class="lang-dot" style="background-color: ${color};"></span>
      <span class="lang-name">${esc(l.name)}</span>
      <span class="lang-percent muted">${l.percent}%</span>
    </span>`;
  }).join("");

  const moreLabel = hasMore ? `<span class="muted" style="font-size: 10.5px;">+${languages.length - maxLabels}</span>` : "";
  const tooltip = languages.map((l) => `${l.name}: ${l.percent}%`).join(" · ");

  const inner = `
    <div class="lang-bar">${segments}</div>
    <div class="lang-labels">${labels}${moreLabel}</div>
  `;

  if (className) {
    return `<div class="${className}" title="${esc(tooltip)}">${inner}</div>`;
  }
  return inner;
}

// A <details> dropdown: `items` are [label, attrs, hint?] or "---" or ["header", title].
function moreMenu(items, { label = "More", left = false } = {}) {
  const body = items.map((it) => {
    if (it === "---") return "<hr>";
    if (Array.isArray(it) && it[0] === "header") {
      return `<div class="more-menu-header">${esc(it[1])}</div>`;
    }
    return `<button class="btn${it[3] === "danger" ? " danger" : ""}" ${it[1]}>${esc(it[0])}</button>${it[2] ? `<div class="hint">${esc(it[2])}</div>` : ""}`;
  }).join("");
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
    await refreshState(); // so the run page finds the new run straight away
    location.hash = `#/runs/${run.id}`;
  } catch (e) {
    toast(e.message, true);
  }
}

function formDialog(title, bodyHtml, okLabel = "Run", { danger = false } = {}) {
  const dlg = $("#dialog");
  $("#dialog-cancel").hidden = false;
  $("#dialog-title").textContent = title;
  $("#dialog-body").innerHTML = bodyHtml;
  $("#dialog-ok").textContent = okLabel;
  $("#dialog-ok").className = danger ? "btn danger" : "btn primary";
  dlg.returnValue = "";
  dlg.showModal();
  const first = $("#dialog-body textarea, #dialog-body input, #dialog-body select");
  if (first) first.focus();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      $("#dialog-ok").className = "btn primary";
      if (dlg.returnValue !== "ok") return resolve(null);
      resolve(Object.fromEntries(new FormData($("#dialog-form")).entries()));
    }, { once: true });
  });
}

function addProjectsDialog(trackedProjects) {
  const dlg = $("#dialog");
  const body = $("#dialog-body");
  const cancel = $("#dialog-cancel");
  const done = $("#dialog-ok");
  let changed = false;
  let switched = false;
  let pendingAdds = 0;

  $("#dialog-title").textContent = "Add Projects";
  done.textContent = "Done";
  done.disabled = false;
  cancel.hidden = true;
  body.innerHTML = `
    <section class="project-discovery">
      <div>
        <h3>Projects on this computer</h3>
        <small class="muted">Add any discovered codebases to your tracked projects.</small>
      </div>
      <div id="project-discovery-list" class="project-discovery-list" aria-live="polite">
        <div class="project-discovery-status"><span class="pill working"><span class="dot"></span>Scanning for projects...</span></div>
      </div>
    </section>
    <div class="dialog-divider"><span>Or add a directory</span></div>
    <label class="field"><span>Project Directory Path</span>
      <input type="text" name="root" placeholder="e.g. /Users/.../my-app or ~/Projects/my-app" autocomplete="off" spellcheck="false">
      <small>Path to the folder containing your Xcode project, Package.swift, or Git repository.</small>
    </label>
    <label class="field"><span>Display Name <span class="muted">(optional)</span></span>
      <input type="text" name="name" placeholder="Leave blank to use folder name">
    </label>
    <label class="check">
      <input type="checkbox" name="active">
      <span>Switch to this project immediately</span>
    </label>
    <div class="row end"><button type="button" class="btn" id="manual-add-project">Add directory</button></div>
  `;

  const list = $("#project-discovery-list");
  const form = $("#dialog-form");
  const manualButton = $("#manual-add-project");
  const sendAdd = ProjectPicker.createAddQueue((request) => api(request.path, request.options));
  const enqueueAdd = (request) => {
    pendingAdds += 1;
    done.disabled = true;
    return sendAdd(request).finally(() => {
      pendingAdds -= 1;
      done.disabled = pendingAdds > 0;
    });
  };
  const showScanResults = async () => {
    try {
      const { discovered } = await api("projects/scan", { method: "POST", body: {} });
      const available = ProjectPicker.availableProjects(discovered, trackedProjects);
      list.innerHTML = available.length
        ? ProjectPicker.renderRows(available)
        : `<div class="project-discovery-status muted">No additional projects found.</div>`;
    } catch (err) {
      list.innerHTML = `<div class="notice bad">Scan failed: ${esc(err.message)}</div>`;
    }
  };

  const onClick = async (event) => {
    const discoveredButton = event.target.closest("[data-add-project-path]");
    if (discoveredButton) {
      event.preventDefault();
      event.stopPropagation();
      discoveredButton.disabled = true;
      try {
        const project = {
          root: discoveredButton.dataset.addProjectPath,
          name: discoveredButton.dataset.projectName || "",
        };
        const request = ProjectPicker.addRequest(project);
        await enqueueAdd(request);
        changed = true;
        discoveredButton.textContent = "Added";
        discoveredButton.classList.remove("primary");
        toast(`${project.name || project.root} added`);
      } catch (err) {
        discoveredButton.disabled = false;
        toast(err.message, true);
      }
      return;
    }

    if (!event.target.closest("#manual-add-project")) return;
    const values = Object.fromEntries(new FormData(form).entries());
    const root = String(values.root || "").trim();
    if (!root) {
      toast("Enter a project directory path", true);
      return;
    }
    const active = Boolean(values.active);
    manualButton.disabled = true;
    try {
      await enqueueAdd({
        path: "projects/add",
        options: {
          method: "POST",
          body: { root, name: String(values.name || "").trim(), active },
        },
      });
      changed = true;
      switched = active;
      toast("Project added");
      if (active) {
        dlg.close("ok");
      } else {
        form.elements.root.value = "";
        form.elements.name.value = "";
      }
    } catch (err) {
      toast(err.message, true);
    } finally {
      manualButton.disabled = false;
    }
  };

  const onCancel = (event) => {
    if (!pendingAdds) return;
    event.preventDefault();
    toast("Wait for the project to finish adding");
  };
  const onKeydown = (event) => {
    if (event.key !== "Enter" || !event.target.matches('input[name="root"], input[name="name"]')) return;
    event.preventDefault();
    manualButton.click();
  };

  body.addEventListener("click", onClick);
  body.addEventListener("keydown", onKeydown);
  dlg.addEventListener("cancel", onCancel);
  dlg.returnValue = "";
  dlg.showModal();
  showScanResults();

  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      body.removeEventListener("click", onClick);
      body.removeEventListener("keydown", onKeydown);
      dlg.removeEventListener("cancel", onCancel);
      cancel.hidden = false;
      done.disabled = false;
      resolve({ changed, switched });
    }, { once: true });
  });
}

const dialogs = {
  async fix(params) {
    const values = await formDialog("Still broken?", `
      <label class="field"><span>What's wrong?</span>
        <textarea name="feedback" required placeholder="e.g. The lobby timer still doesn't reset after a rematch"></textarea>
        <small>Reopens this job with what you saw and runs a fix.</small>
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
  async revise(params) {
    const values = await formDialog("Revise plan", `
      <label class="field"><span>What should change?</span>
        <textarea name="change" required placeholder="e.g. Keep the layout, but show retry details in the failed state"></textarea></label>
      <label class="field"><span>Where does it apply? <span class="muted">(optional)</span></span><input type="text" name="where" placeholder="screen, file or flow"></label>
      <label class="field"><span>Done when <span class="muted">(optional)</span></span><input type="text" name="done_when" placeholder="acceptance criteria"></label>
      <small class="hint-text">The job is re-planned with this; you approve the new plan before anything is built.</small>`, "Re-plan");
    if (values?.change.trim()) runAction("revise", { job: params.job, change: values.change, where: values.where, done_when: values.done_when });
  },
  async git_new_branch() {
    const values = await formDialog("New branch", `
      <label class="field"><span>Branch name</span><input type="text" name="name" required autocapitalize="off" spellcheck="false" placeholder="e.g. feature/rematch-flow"></label>
      <small class="hint-text">Created from ${esc(state.project?.branch || "the current branch")} and checked out.</small>`, "Create");
    if (values?.name.trim()) runAction("git_new_branch", { name: values.name.trim() });
  },
  async distribute() {
    const values = await formDialog("Distribute current branch", `
      <p>Builds <strong class="mono">${esc(state.project?.branch || "the checked-out branch")}</strong> and sends a real Firebase release to your testers.</p>
      <label class="field"><span>Release notes <span class="muted">(optional)</span></span><textarea name="notes"></textarea></label>`, "Distribute");
    if (values) runAction("distribute", { notes: values.notes }, { skipConfirm: true });
  },
  async link_logs(params) {
    const hasRemote = state.project?.remote_logs;
    const values = await formDialog("Link Logs (Update Context)", `
      <p class="muted mb-8">Attach crash or runtime logs to guide the next repair cycle.</p>
      ${hasRemote ? `<label class="check"><input type="checkbox" name="pull_device" checked><span>Pull newest device launch from Sentry</span></label>` : ''}
      <label class="field"><span>Local log path or log snippet <span class="muted">(optional)</span></span>
        <textarea name="log_text" placeholder="Paste log lines, stack traces, or file path..."></textarea>
      </label>`, "Link Logs");
    if (values) {
      const logs = values.pull_device ? "cloud:latest" : (values.log_text || "");
      if (logs) runAction("debug", { job: params.job, logs });
      else toast("No logs specified");
    }
  },
  async attach_mockup(params) {
    const values = await formDialog("Attach UI Mockup / Reference", `
      <label class="field"><span>Mockup URL or File Path</span>
        <input type="text" name="ref_url" required placeholder="https://figma.com/... or /path/to/mockup.png">
      </label>
      <label class="field"><span>Notes / Description <span class="muted">(optional)</span></span>
        <textarea name="note" placeholder="Design requirements, expected UI behavior, color tokens..."></textarea>
      </label>`, "Attach Reference");
    if (values?.ref_url.trim()) {
      api(`jobs/${encodeURIComponent(params.job)}/reference`, {
        method: "POST",
        body: { url: values.ref_url.trim(), note: values.note || "" }
      }).then(async () => {
        toast("Reference attached");
        await refreshState();
        route();
      }).catch(err => toast(err.message, true));
    }
  },
  async select_models(params) {
    const jobData = await api(`jobs/${encodeURIComponent(params.job)}`);
    const p = jobData.pipeline || {};
    const values = await formDialog("Select LLM Models (Override)", `
      <p class="muted mb-8">Override the model pipeline used for planning, building, and reviewing this job.</p>
      <label class="field"><span>Planner Model</span>
        <input type="text" name="planner" value="${esc(p.planner || '')}" placeholder="gemini-3.1-pro-preview">
      </label>
      <label class="field"><span>Builder Model</span>
        <input type="text" name="builder" value="${esc(p.builder || '')}" placeholder="gpt-5.4">
      </label>
      <label class="field"><span>Reviewer Model</span>
        <input type="text" name="reviewer" value="${esc(p.reviewer || '')}" placeholder="gemini-3.1-pro-preview">
      </label>`, "Save Models");
    if (values) {
      api(`jobs/${encodeURIComponent(params.job)}/models`, {
        method: "POST",
        body: values
      }).then(async () => {
        toast("Models updated for this job");
        await refreshState();
        route();
      }).catch(err => toast(err.message, true));
    }
  },
  async export_context(params) {
    const data = await api(`jobs/${encodeURIComponent(params.job)}`);
    const s = data.summary;
    const j = data.job;
    const tasks = data.tasks || [];
    const done = data.completed_tasks || [];
    const md = [
      `# Job Context: ${s.title} (${s.display_id || s.id})`,
      `\n- **Status**: ${s.status}`,
      `- **Approach**: ${s.approach || 'Standard workflow'}`,
      `- **Branch**: ${s.branch || 'none'}`,
      `- **Kind**: ${s.kind}`,
      `- **Updated**: ${new Date(s.updated * 1000).toISOString()}`,
      `\n## Models Pipeline`,
      `- Planner: ${data.pipeline?.planner || 'default'}`,
      `- Builder: ${data.pipeline?.builder || 'default'}`,
      `- Reviewer: ${data.pipeline?.reviewer || 'default'}`,
      `\n## Tasks (${done.length}/${tasks.length})`,
      ...tasks.map((t, idx) => {
        const title = typeof t === 'string' ? t : (t.name || t.title || t.description || `Task ${idx + 1}`);
        const isDone = done.includes(idx) || done.includes(String(idx));
        return `- [${isDone ? 'x' : ' '}] ${title}`;
      }),
      data.changes?.diffstat ? `\n## Git Diffstat\n\`\`\`\n${data.changes.diffstat}\n\`\`\`` : '',
      ...(data.docs || []).map(d => `\n## ${d.title}\n\n${d.text}`),
    ].filter(Boolean).join('\n');

    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const u = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = u;
    a.download = `job-${params.job}-context.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(u);
    toast(`Exported context to job-${params.job}-context.md`);
  },
  async close_issue(params) {
    if (confirm(`Close issue #${params.issue} on GitHub and mark this job completed?`)) {
      api(`jobs/${encodeURIComponent(params.job)}/close_issue`, {
        method: "POST",
        body: {}
      }).then(async () => {
        toast(`Closed issue #${params.issue}`);
        await refreshState();
        route();
      }).catch(err => toast(err.message, true));
    }
  },
  discard_job(params) { return runAction("discard", { job: params.job }); },
  async delete_job(params) {
    const values = await formDialog("Delete Job", `
      <p class="muted" style="margin-bottom: var(--space-3);">Do you want to revert changes or just delete and keep changes?</p>
      <div class="stack" style="gap: var(--space-2);">
        <label style="display: flex; gap: var(--space-3); align-items: flex-start; padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--r-md); background: var(--panel-2); cursor: pointer;">
          <input type="radio" name="mode" value="revert" checked style="margin-top: 3px;">
          <div>
            <strong style="display: block; margin-bottom: 2px;">Revert changes</strong>
            <span class="muted" style="font-size: var(--text-sm);">Reverts files modified by this job and deletes its branch.</span>
          </div>
        </label>
        <label style="display: flex; gap: var(--space-3); align-items: flex-start; padding: var(--space-3); border: 1px solid var(--border); border-radius: var(--r-md); background: var(--panel-2); cursor: pointer;">
          <input type="radio" name="mode" value="keep" style="margin-top: 3px;">
          <div>
            <strong style="display: block; margin-bottom: 2px;">Delete and keep changes</strong>
            <span class="muted" style="font-size: var(--text-sm);">Deletes the job, but preserves all code changes and branches.</span>
          </div>
        </label>
      </div>`, "Continue");
    if (!values) return;

    await new Promise((r) => setTimeout(r, 20));

    const revert = values.mode === "revert";
    const confirmBody = revert
      ? `<p>Are you sure you want to delete this job and <strong>revert all changes</strong>?</p>
         <p class="muted">All uncommitted code changes made for this job will be undone and its branch deleted. This action cannot be undone.</p>`
      : `<p>Are you sure you want to delete this job and <strong>keep all changes</strong>?</p>
         <p class="muted">The job record will be deleted, but your code changes and branch will remain intact.</p>`;
    const confirmOkLabel = revert ? "Delete and revert" : "Delete job";

    const confirmed = await formDialog("Confirm Deletion", confirmBody, confirmOkLabel, { danger: true });
    if (!confirmed) return;

    try {
      await api(`jobs/${encodeURIComponent(params.job)}/delete`, {
        method: "POST",
        body: { revert }
      });
      toast(revert ? "Job deleted and changes reverted" : "Job deleted (changes kept)");
      await refreshState();
      location.hash = "#/";
      route();
    } catch (err) {
      toast(err.message, true);
    }
  },
  async splinter_job(params) {
    if (confirm("Decompose this feature plan into separate parallel child tasks and GitHub sub-issues?")) {
      await runAction("splinter", { job: params.job });
    }
  },
  async export_job_bundle(params) {
    const values = await formDialog("Export Job Bundle", `
      <p class="muted" style="margin-bottom:10px;">Select how you want to export all context, brief, git diffs, and execution logs for this job.</p>
      <label class="field"><span>Export Destination</span>
        <select name="destination">
          <option value="direct">Direct Download (.ZIP archive in browser)</option>
          <option value="icloud">iCloud Drive (Sync across Apple devices)</option>
          <option value="gdrive">Google Drive (Sync to Drive)</option>
          <option value="downloads">macOS Downloads folder (~/Downloads)</option>
          <option value="local">Project Workspace (./exports/)</option>
        </select>
      </label>`, "Export");
    if (values) {
      if (values.destination === "direct") {
        window.open(`/api/jobs/${encodeURIComponent(params.job)}/export-zip`, "_blank");
        toast("Download started");
      } else {
        await runAction("export_job", { job: params.job, destination: values.destination });
      }
    }
  },
  async run_visual_check(params) {
    await runAction("visual_check", params?.job ? { job: params.job } : {});
  },
};

document.addEventListener("click", (e) => {
  // Close any open "More" menu when clicking outside it or on one of its items.
  document.querySelectorAll("details.more[open]").forEach((d) => {
    if (!d.contains(e.target) || e.target.closest(".more-menu .btn")) d.open = false;
  });
  const scrollTo = e.target.closest("[data-scroll-to]");
  if (scrollTo) {
    const targetSelector = scrollTo.dataset.scrollTo;
    const el = document.querySelector(targetSelector);
    if (el) {
      el.scrollIntoView({ behavior: "smooth" });
      el.classList.add("highlight-flash");
      setTimeout(() => el.classList.remove("highlight-flash"), 1800);
    } else {
      location.hash = "#/config";
      setTimeout(() => {
        const el2 = document.querySelector(targetSelector);
        if (el2) {
          el2.scrollIntoView({ behavior: "smooth" });
          el2.classList.add("highlight-flash");
          setTimeout(() => el2.classList.remove("highlight-flash"), 1800);
        }
      }, 350);
    }
    return;
  }
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
  const forgetBtn = e.target.closest("[data-forget-project]");
  if (forgetBtn) {
    e.stopPropagation();
    const root = forgetBtn.dataset.forgetProject;
    if (confirm("Remove this project from your list of tracked projects?")) {
      api("projects", { method: "DELETE", body: { root } })
        .then(async () => {
          toast("Project removed");
          await refreshState();
          route();
        })
        .catch((err) => toast(err.message, true));
    }
    return;
  }
  const switchBtn = e.target.closest("[data-switch-project]");
  if (switchBtn) {
    const root = switchBtn.dataset.switchProject;
    // Respond instantly: move the highlight now; the page rebuilds (slowly) once the switch lands.
    if (switchBtn.classList.contains("project-card")) {
      document.querySelectorAll(".project-card.active-project").forEach((c) => c.classList.remove("active-project"));
      switchBtn.classList.add("active-project");
      switchBtn.setAttribute("aria-busy", "true");
    }
    api("project", { method: "POST", body: { root } })
      .then(async () => {
        await refreshState();
        toast(`Switched to ${state.project?.name || "project"}`);
        if (switchBtn.dataset.then) location.hash = switchBtn.dataset.then;
        route();
      })
      .catch((err) => { toast(err.message, true); route(); }); // put the highlight back
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

document.addEventListener("keydown", (e) => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches?.(".project-card[tabindex]")) {
    e.preventDefault();
    e.target.click();
  }
});

const configurationTrigger = $("#configuration-menu-trigger");
const configurationMenu = $("#configuration-context-menu");
if (configurationTrigger && configurationMenu) {
  configurationMenu.innerHTML = ConfigurationPages.renderMenu();
  ConfigurationPages.createMenuController({
    trigger: configurationTrigger,
    menu: configurationMenu,
    document,
    navigate: (target) => { location.hash = target; },
  });
}

// ---------------------------------------------------------------- state & project

let consecutiveAuthFailures = 0;

let pollFailures = 0; // consecutive failed background polls
let lastInbox = null; // previous inbox items, to spot new things waiting on you
let lastPrdNotice; // undefined until the first poll, so an update that happened while away isn't announced twice
let lastRuns = null; // previous poll, to spot runs that finished or started waiting

async function refreshState() {
  if (signingIn) return;
  const backend = getBackendUrl();
  const token = getToken();
  if (backend === null && !token) {
    showSignInGate("Sign in with your access token to continue.");
    return;
  }
  try {
    const newState = await api("state");
    for (const event of [...Notifications.events(lastRuns, newState.runs), ...Notifications.inboxEvents(lastInbox, newState.inbox)]) Notifications.show(event);
    lastRuns = newState.runs;
    lastInbox = newState.inbox || [];
    const note = newState.product_notice;
    if (note && note.id !== lastPrdNotice) { // the AI updated the product requirements by itself: say so once
      if (lastPrdNotice !== undefined) { toast(`Product requirements updated: ${note.summary}`); Notifications.show({ title: "Product requirements updated", body: note.summary, key: `prd:${note.id}`, hash: "#/product?history=" + encodeURIComponent(note.id) }); }
      lastPrdNotice = note.id;
    } else if (!note && lastPrdNotice === undefined) lastPrdNotice = null;
    state = newState;
    consecutiveAuthFailures = 0;
    pollFailures = 0;
    $("#conn-banner").hidden = true;
    if (newState.token) {
      localStorage.setItem("orchestrator_token", newState.token);
    }
    document.querySelector(".app")?.classList.remove("session-locked");
  } catch (e) {
    if (e.status === 401) {
      consecutiveAuthFailures += 1;
      // Only lock the session if we don't have an active project loaded,
      // or if we have experienced multiple consecutive 401 failures to rule out transient blips
      if (!state.project || consecutiveAuthFailures >= 2) {
        showSignInGate("Session expired. Please sign in again.");
      }
      return;
    }
    // Transient network errors, tunnel drops, or 502/504 errors should NEVER lock the user out!
    console.warn("Background state refresh notice (will retry):", e.message);
    pollFailures += 1;
    if (pollFailures >= 2) $("#conn-banner").hidden = false; // two misses in a row: say so, rather than showing stale data silently
    if (!state.project) {
      showSignInGate(`Unable to reach Orchestrator on your Mac: ${e.message}`);
    }
    return;
  }
  const running = state.runs.filter((r) => r.running);
  const waiting = running.filter((r) => r.waiting).length;
  const badge = $("#running-badge");
  if (badge) {
    badge.hidden = running.length === 0;
    badge.textContent = waiting || running.length;
    badge.classList.toggle("working", waiting === 0);
    badge.title = waiting ? `${waiting} waiting for you` : `${running.length} running`;
  }
  // Device logs and simulator checks only mean something for a project that builds an app.
  const navDevlogs = document.querySelector('.nav [data-route="devlogs"]');
  if (navDevlogs) navDevlogs.hidden = state.project?.mobile_app === false;
  const inboxBadge = $("#inbox-badge");
  if (inboxBadge) {
    inboxBadge.hidden = !state.inbox_count;
    inboxBadge.textContent = state.inbox_count || "";
    inboxBadge.title = `${state.inbox_count} waiting on you`;
  }
  const pageTitle = $("#page-title")?.textContent;
  document.title = Notifications.tabTitle(pageTitle ? `${pageTitle} · Orchestrator` : "Orchestrator", state.inbox_count || 0);
  const topLangsEl = $("#topbar-languages");
  if (topLangsEl && (current?.page === "home" || $("#page-title")?.textContent === state.project?.name)) {
    if (state.project?.languages?.length) {
      topLangsEl.innerHTML = renderLanguagesBar(state.project.languages, { maxLabels: 3 });
      topLangsEl.title = state.project.languages.map((l) => `${l.name}: ${l.percent}%`).join(" · ");
      topLangsEl.hidden = false;
    } else {
      topLangsEl.hidden = true;
      topLangsEl.innerHTML = "";
    }
  }
  renderProjectSelect($("#project-select"));
}

function renderProjectSelect(select) {
  if (!select || !state.project) return;
  const p = state.project;
  const options = [...(p.recent || [])];
  if (!options.some((o) => o.root === p.root)) {
    options.unshift({
      name: p.name,
      root: p.root,
      source_type: p.source_type,
      source_label: p.source_label,
      github_repo: p.github_repo,
    });
  }

  const githubProjects = options.filter((o) => o.source_type === "github");
  const localProjects = options.filter((o) => o.source_type !== "github");

  let html = "";
  if (githubProjects.length > 0 && localProjects.length > 0) {
    html += `<optgroup label="── GitHub Tracked ──">`;
    html += githubProjects.map((o) => `<option value="${esc(o.root)}" ${o.root === p.root ? "selected" : ""}>${esc(o.name)}</option>`).join("");
    html += `</optgroup>`;

    html += `<optgroup label="── Local ──">`;
    html += localProjects.map((o) => `<option value="${esc(o.root)}" ${o.root === p.root ? "selected" : ""}>${esc(o.name)}</option>`).join("");
    html += `</optgroup>`;
  } else {
    html = options.map((o) => {
      const tag = o.source_type === "github" ? " 🐙" : "";
      return `<option value="${esc(o.root)}" ${o.root === p.root ? "selected" : ""}>${esc(o.name)}${tag}</option>`;
    }).join("");
  }
  html += `<option value="__manage__" class="manage-projects-option" style="background-color: var(--manage-option-bg, #323947); color: var(--text);">📁 Manage all projects...</option>`;
  if (select.dataset.html !== html) {
    select.innerHTML = html;
    select.dataset.html = html;
  }
}

document.addEventListener("change", async (e) => {
  if (!e.target.matches("#project-select, .project-select-inline")) return;
  const val = e.target.value;
  if (val === "__manage__") {
    location.hash = "#/projects";
    if (state.project) e.target.value = state.project.root;
    return;
  }
  try {
    await api("project", { method: "POST", body: { root: val } });
    await refreshState();
    toast(`Switched to ${state.project.name}`);
    route();
  } catch (err) {
    toast(err.message, true);
  }
});

// Shown from the click on a provider (or the token form) until the first page is ready.
function showSigningIn(label = "Signing you in…") {
  setHeader({ title: "Signing in", sub: "", actions: "" });
  document.querySelector(".app")?.classList.add("session-locked");
  view.innerHTML = `<div class="signin-wrap"><div class="signin-card signing-in" role="status" aria-live="polite">
      <div class="signin-brand"><span class="brand-mark" aria-hidden="true"></span><span>Orchestrator</span></div>
      <div class="spinner" aria-hidden="true"></div>
      <h2>${esc(label)}</h2>
      <p class="muted">Connecting to your computer and loading your projects.</p></div></div>`;
}

function showSignInGate(message) {
  setHeader({ title: "Sign In", sub: "", actions: "" });
  document.querySelector(".app")?.classList.add("session-locked");
  const backend = getBackendUrl() || "";
  const token = getToken() || "";
  const isRemote = window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost";
  view.innerHTML = `
    <div class="signin-wrap">
      <div class="signin-card">
        <div class="signin-brand">
          <span class="brand-mark" aria-hidden="true"></span>
          <span>Orchestrator</span>
        </div>
        <div>
          <h2 style="margin: 0; font-size: 1.3rem; font-weight: 700;">Sign In</h2>
          <p class="muted" style="margin: 0.35rem 0 0; font-size: 0.92rem;">
            ${message ? `<span style="color: var(--err);">${esc(message)}</span>` : "Sign in to access and manage projects on your computer."}
          </p>
        </div>
        <div class="sso-buttons">
          <button type="button" class="sso-btn google-btn" id="google-signin-btn">
            <svg class="icon"><use href="#i-google"/></svg>
            <span>Continue with Google</span>
          </button>
          <button type="button" class="sso-btn apple-btn" id="apple-signin-btn">
            <svg class="icon"><use href="#i-apple"/></svg>
            <span>Continue with Apple</span>
          </button>
          <button type="button" class="sso-btn github-btn" id="github-signin-btn">
            <svg class="icon"><use href="#i-github"/></svg>
            <span>Continue with GitHub</span>
          </button>
        </div>
        ${isRemote ? `
        <div class="backend-config-card" style="margin-top: 0.75rem; padding: 0.65rem 0.85rem; background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; font-size: 0.82rem; text-align: left;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.35rem;">
            <span style="font-weight: 600; color: var(--text);">Mac Backend URL:</span>
            <span style="font-size: 0.75rem; color: ${backend ? "var(--good, #2e9d5b)" : "var(--warn, #d99a00)"};">${backend ? "Configured" : "Required for phone"}</span>
          </div>
          <input type="url" id="signin-backend" name="backend" placeholder="e.g. https://...trycloudflare.com or Tailscale" value="${esc(backend)}" style="width: 100%; box-sizing: border-box; font-size: 0.85rem; padding: 0.4rem 0.6rem; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: var(--text);">
          <small class="muted" style="display: block; margin-top: 0.35rem; line-height: 1.3;">Run <code>orchestrator ui --tunnel</code> on your Mac to generate an HTTPS URL for your phone.</small>
        </div>
        ` : ""}
        ${blockedProvider ? `<p class="muted signin-fallback">Pop-ups still blocked? <button type="button" class="linklike" id="redirect-fallback">Try full-page ${esc(blockedProvider.name)} sign-in</button>. Some browsers can't finish this way; the access token below always works.</p>` : ""}
        <details class="manual-token-details" style="font-size: 0.82rem; margin-top: 0.5rem; border-top: 1px solid var(--border); padding-top: 0.75rem;">
          <summary class="muted" style="cursor: pointer; user-select: none; text-align: center;">Advanced: Sign in with CLI access token</summary>
          <form id="signin-form" class="stack" style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.75rem;">
            <label class="field">
              <span>CLI Access Token</span>
              <input type="password" id="signin-token" name="token" value="${esc(token)}" placeholder="Paste access token from terminal" autocomplete="current-password" style="font-family: var(--mono); font-size: 0.9rem;">
            </label>
            ${!isRemote ? `
            <label class="field">
              <span>Backend URL</span>
              <input type="url" id="signin-backend" name="backend" placeholder="e.g. https://...trycloudflare.com" value="${esc(backend)}" style="font-size: 0.85rem;">
            </label>
            ` : ""}
            <button type="submit" id="signin-submit-btn" class="btn" style="width: 100%; justify-content: center; padding: 0.55rem;">Use Access Token</button>
          </form>
        </details>
      </div>
    </div>
  `;

  const syncBackend = () => {
    const el = $("#signin-backend");
    if (!el) return;
    const b = el.value.trim().replace(/\/+$/, "");
    if (b) localStorage.setItem("orchestrator_backend", b);
    else localStorage.removeItem("orchestrator_backend");
  };

  const beInput = $("#signin-backend");
  if (beInput) {
    beInput.addEventListener("input", syncBackend);
    beInput.addEventListener("change", syncBackend);
  }

  // One handler for every Firebase provider: pop up, trade the ID token for a session.
  const wireSso = (id, name, makeProvider, explain = {}) => {
    const btn = $(id);
    if (!btn) return;
    btn.addEventListener("click", async () => {
      if (!window.firebase?.auth) {
        toast("Firebase Auth is loading or unavailable", true);
        return;
      }
      syncBackend();
      btn.disabled = true;
      signingIn = true; // the gate stays as it is while the provider's window is open
      const originalHtml = btn.innerHTML;
      btn.innerHTML = `<span>Signing in with ${name}...</span>`;
      try {
        let cred;
        try {
          cred = await firebase.auth().signInWithPopup(makeProvider());
        } catch (popupErr) {
          if (popupErr.code === "auth/popup-blocked") {
            blockedProvider = { name, makeProvider };
            throw Object.assign(new Error(`Your browser blocked the ${name} sign-in window. Allow pop-ups for this site and try again.`), { code: "auth/popup-blocked-explained" });
          }
          throw popupErr;
        }
        showSigningIn(`Signed in with ${name}. Connecting…`);
        const idToken = await cred.user.getIdToken();
        const currentBackend = getBackendUrl();
        if (!currentBackend && window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost") {
          throw new Error("Signed in with " + name + ", but your Mac Backend URL is not set. Enter your Backend URL below to connect.");
        }
        let res;
        try {
          res = await api("auth", { method: "POST", body: { id_token: idToken } });
        } catch (apiErr) {
          const target = currentBackend || "local backend";
          throw new Error(`Signed in as ${cred.user.email || name + " user"}, but could not reach ${target} (${apiErr.message}). Make sure Orchestrator is running on your Mac.`);
        }
        if (res.token) localStorage.setItem("orchestrator_token", res.token);
        toast(`Signed in as ${cred.user.email || `${name} user`}`);
        document.querySelector(".app")?.classList.remove("session-locked");
        endSigningIn();
        await refreshState();
        route();
      } catch (err) {
        endSigningIn();
        if (err.code === "auth/popup-closed-by-user" || err.code === "auth/cancelled-popup-request") { btn.disabled = false; btn.innerHTML = originalHtml; return; }
        const msg = explain[err.code] || err.message;
        if (err.code !== "auth/popup-blocked-explained") toast(msg, true); // the gate already says it
        showSignInGate(msg);
      }
    });
  };

  $("#redirect-fallback")?.addEventListener("click", async () => {
    const { name, makeProvider } = blockedProvider;
    blockedProvider = null;
    signingIn = true;
    showSigningIn(`Opening ${name}…`);
    try { await firebase.auth().signInWithRedirect(makeProvider()); }
    catch (err) { endSigningIn(); showSignInGate(err.message); }
  });

  wireSso("#google-signin-btn", "Google", () => {
    const p = new firebase.auth.GoogleAuthProvider();
    p.setCustomParameters({ prompt: "select_account" });
    return p;
  });
  wireSso("#apple-signin-btn", "Apple", () => {
    const p = new firebase.auth.OAuthProvider("apple.com");
    p.addScope("email");
    p.addScope("name");
    return p;
  }, { "auth/operation-not-allowed": "Apple Sign-In is not yet configured in your Firebase Console. Please add your Apple Developer keys in Firebase." });
  wireSso("#github-signin-btn", "GitHub", () => {
    const p = new firebase.auth.GithubAuthProvider();
    p.addScope("user:email"); // lets Firebase read your verified primary email, even if it's private on your profile
    return p;
  }, {
    "auth/operation-not-allowed": "GitHub sign-in isn't turned on yet. Enable the GitHub provider in the Firebase console (Authentication → Sign-in method).",
    "auth/account-exists-with-different-credential": "This email already signs in with another provider (Google or Apple). Use that one instead.",
  });

  const form = $("#signin-form");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submitBtn = $("#signin-submit-btn");
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Unlocking...";
      }
      syncBackend();
      const t = (form.token?.value || "").trim();
      localStorage.setItem("orchestrator_token", t);
      signingIn = true;
      showSigningIn("Unlocking…");
      try {
        await api("auth", { method: "POST", body: { token: t } });
        toast("Session unlocked");
        document.querySelector(".app")?.classList.remove("session-locked");
        endSigningIn();
        await refreshState();
        route();
      } catch (err) {
        endSigningIn();
        toast(err.message, true);
        showSignInGate(err.message || "Invalid access token");
      }
    });
  }
}

function showLocked(message) {
  showSignInGate(message);
}

async function lockSession() {
  try {
    await api("auth/logout", { method: "POST", body: {} });
  } catch {}
  if (window.firebase?.auth) {
    try {
      await firebase.auth().signOut();
    } catch {}
  }
  localStorage.removeItem("orchestrator_token");
  const url = new URL(window.location.href);
  if (url.searchParams.has("token")) {
    url.searchParams.delete("token");
    window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
  }
  state = { project: null, runs: [], actions: {} };
  const badge = $("#running-badge");
  if (badge) badge.hidden = true;
  toast("Session locked");
  showSignInGate("Session locked. Please sign in again.");
}

function setHeader({ title, sub = "", actions = "" }) {
  $("#page-title").textContent = title;
  const subEl = $("#page-sub");
  subEl.innerHTML = sub;
  subEl.hidden = !sub;
  $("#topbar-actions").innerHTML = actions;
  document.title = Notifications.tabTitle(`${title} · Orchestrator`, state.inbox_count || 0);
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



function formatJobDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const now = new Date();
  const isSameYear = d.getFullYear() === now.getFullYear();
  const dateStr = d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(isSameYear ? {} : { year: "numeric" }),
  });
  const timeStr = d.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
  return `${dateStr}, ${timeStr}`;
}

// Job list sorting (tap Status / Last modified; tap again to reverse).
const GROUP_ORDER = { needs_you: 0, working: 1, done: 2 };
let jobSort = { key: "status", dir: "asc" };
try { jobSort = JSON.parse(localStorage.getItem("orchestrator_job_sort")) || jobSort; } catch {}

function sortJobs(jobs) {
  const { key, dir } = jobSort;
  const val = (j) => key === "status"
    ? [j.active_run ? 1 : (GROUP_ORDER[j.state?.group] ?? 3), j.state?.label || ""]
    : [j.updated || 0];
  const sign = dir === "asc" ? 1 : -1;
  return [...jobs].sort((a, b) => {
    const [x, y] = [val(a), val(b)];
    for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return (x[i] < y[i] ? -1 : 1) * sign;
    return (b.updated || 0) - (a.updated || 0); // ties: newest first
  });
}
const sortArrow = (key) => jobSort.key === key ? `<span class="sort-arrow" aria-hidden="true">${jobSort.dir === "asc" ? "▲" : "▼"}</span>` : "";

document.addEventListener("click", (e) => {
  const head = e.target.closest(".sort-head");
  if (!head) return;
  const key = head.dataset.sort;
  jobSort = { key, dir: jobSort.key === key ? (jobSort.dir === "asc" ? "desc" : "asc") : (key === "updated" ? "desc" : "asc") };
  try { localStorage.setItem("orchestrator_job_sort", JSON.stringify(jobSort)); } catch {}
  route();
});

function jobTableRow(j, { hidden = false } = {}) {
  const parts = [
    j.kind,
    j.branch ? `<span class="mono">${esc(j.branch)}</span>` : "",
    j.tasks_total ? `${j.tasks_done}/${j.tasks_total} tasks` : "",
  ].filter(Boolean);

  const reason = (j.state?.reason && j.state.group === "needs_you")
    ? `<span class="job-reason">${esc(j.state.reason)}</span>`
    : "";
  const metaHtml = [reason, parts.join(" · ")].filter(Boolean).join(" · ");

  return `
    <a class="job-table-row"${hidden ? " hidden" : ""} href="#/jobs/${encodeURIComponent(j.id)}">
      <div class="col-job">
        <div class="job-title">${esc(j.title)}</div>
        <div class="job-meta">${metaHtml}</div>
      </div>
      <div class="col-status">
        ${jobPill(j)}
      </div>
      <div class="col-date" title="${esc(new Date(j.updated * 1000).toLocaleString())}">
        <span class="date-relative">${esc(ago(j.updated))}</span>
        <span class="date-exact muted">${esc(formatJobDate(j.updated))}</span>
      </div>
      <div class="row-hover-hint">
        <span>Click for details</span>
        <svg class="icon" style="width: 14px; height: 14px;"><use href="#i-chevron"/></svg>
      </div>
    </a>`;
}

function elapsed(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

// A running task: status, how long, its newest output line, and open/stop.
function liveRunCard(r) {
  const target = r.job || r.result_job;
  return `<div class="live-run ${r.waiting ? "waiting" : ""}">
    <a class="main-col" href="#/runs/${encodeURIComponent(r.id)}">
      <div class="title">${esc(r.title)}</div>
      <div class="meta">${esc(`Running ${elapsed(r.started)}`)}${r.waiting ? " · waiting for you" : r.idle > 30 ? ` · quiet for ${elapsed(Date.now() / 1000 - r.idle)}` : ""}</div>
      ${r.last_line ? `<div class="last-line mono">${esc(r.last_line)}</div>` : ""}
    </a>
    <div class="row">${runPill(r)}<a class="btn small" href="#/runs/${encodeURIComponent(r.id)}">Open log</a>
      ${target ? `<a class="btn small" href="#/jobs/${encodeURIComponent(target)}">Job</a>` : ""}
      <button class="btn small danger" data-stop="${esc(r.id)}">Stop</button></div></div>`;
}

function runItem(r) {
  return `<a class="item" href="#/runs/${encodeURIComponent(r.id)}">
    <div class="main-col"><div class="title">${esc(r.title)}</div><div class="meta">${esc(ago(r.started))}</div></div>
    <div class="side">${runPill(r)}</div></a>`;
}

function statusLine(p) {
  const running = state.runs.filter((r) => r.running).length;
  const branches = p.branches || [];
  const branchPicker = branches.length
    ? `<select class="branch-select mono" aria-label="Switch branch" title="Switch branch">${p.branch ? "" : `<option selected disabled>no branch</option>`}${branches.map((b) => `<option ${b === p.branch ? "selected" : ""}>${esc(b)}</option>`).join("")}</select>`
    : `<a class="mono" href="#/git">${esc(p.branch || "no branch")}</a>`;
  const langs = p.languages?.length
    ? `<span class="sep">·</span><span class="topbar-languages" title="${esc(p.languages.map((l) => `${l.name}: ${l.percent}%`).join(" · "))}">${renderLanguagesBar(p.languages, { maxLabels: 3 })}</span>`
    : "";
  // Row 1: the branch. Row 2: the stats, then the language mix.
  return `<span class="status-line status-stack"><span class="status-branch">${branchPicker}</span>
    <span class="status-stats"><span>${p.dirty_files} uncommitted</span><span class="sep">·</span><span>${running} running</span>${p.machine_count == null ? "" : p.machine_count === 0 ? `<span class="sep">·</span><a class="warn-link" href="#/config/fleet">No machine set up: jobs can't run yet</a>` : p.model_count === 0 ? `<span class="sep">·</span><a class="warn-link" href="#/config/models">No model selected: jobs can't run yet</a>` : `<span class="sep">·</span><a href="#/config">${p.machine_count} ${p.machine_count === 1 ? "machine" : "machines"}</a><span class="sep">·</span><a href="#/config">${p.model_count} ${p.model_count === 1 ? "model" : "models"}</a>`}${langs}</span></span>`;
}

// git refuses to switch when uncommitted changes would be overwritten, so offer to set them aside.
async function switchBranch(branch) {
  const dirty = state.project?.dirty_files || 0;
  if (!dirty) return runAction("git_checkout", { branch });
  const ok = await formDialog(`Switch to ${branch}?`, `<p>You have ${dirty} uncommitted file${dirty > 1 ? "s" : ""}, which git may refuse to carry over.</p>
    <p>Orchestrator can stash them (set them aside safely), then switch. Get them back any time with <code>git stash pop</code>.</p>`, "Stash & switch");
  if (ok) runAction("stash_checkout", { branch });
}

document.addEventListener("change", (e) => {
  const sel = e.target.closest?.(".branch-select");
  if (!sel) return;
  if (sel.value !== state.project?.branch) switchBranch(sel.value);
  else return;
  sel.value = state.project?.branch; // snap back; the page refreshes once checkout finishes
});

// ---------------------------------------------------------------- pages
// Each page returns { title, sub, actions, html, after? }. Pages in LIVE are
// re-rendered by the poll when their output changes.

const pages = {};
const LIVE = new Set(["home", "job", "activity", "git"]);

// ---------------------------------------------------------------- setup checklist
// What a job needs (GitHub, an AI provider, a machine, project config) vs. nice-to-haves.
// First launch: a full page on Home. Afterwards: a bottom-right panel you can reopen.

let setupState = null;
let setupFetched = 0;
async function loadSetup(force = false) {
  if (!force && Date.now() - setupFetched < 15000) return setupState;
  setupFetched = Date.now();
  try { setupState = await api("setup"); } catch { setupState = null; } // older servers have no /api/setup
  renderSetupFab();
  if (setupState?.complete && !setupState.seen) {
    setupState.seen = true;
    api("config/setup-seen", { method: "POST", body: {} }).catch(() => {});
  }
  return setupState;
}

function setupItem(i) {
  const icon = i.done ? `<span class="setup-icon done" aria-label="done">✓</span>` : `<span class="setup-icon ${i.required ? "todo" : ""}" aria-label="to do"></span>`;
  const go = i.action ? (i.done ? "Open" : i.action.type === "run" ? "Set up" : "Go") : "";
  return `<div class="setup-item ${i.done ? "is-done" : ""}">
    ${icon}
    <div class="main-col"><div class="title">${esc(i.title)}</div><div class="meta">${esc(i.detail)}</div>
      ${!i.done && i.hint ? `<div class="setup-hint"><code>${esc(i.hint)}</code><button class="btn small ghost" data-setup-copy="${esc(i.hint)}">Copy</button></div>` : ""}</div>
    ${go ? `<button class="btn small ${i.done ? "ghost" : "primary"}" data-setup-go="${esc(i.id)}">${go}</button>` : ""}</div>`;
}

function setupListHtml(s) {
  const req = s.items.filter((i) => i.required);
  const opt = s.items.filter((i) => !i.required);
  const group = (title, items, count) => `<div class="setup-group"><div class="setup-group-h"><h3>${esc(title)}</h3>${count ? `<span class="count">${count}</span>` : ""}</div>${items.map(setupItem).join("")}</div>`;
  return group("Required", req, `${s.required_done}/${s.required_total}`) +
    group("Optional", opt, `${opt.filter((i) => i.done).length}/${opt.length}`);
}

function setupPage(s) {
  const left = s.required_total - s.required_done;
  return {
    title: "Let's get set up",
    sub: left ? `${left} required step${left > 1 ? "s" : ""} left before you can run a job` : "All required steps are done",
    actions: `<button class="btn ghost" id="setup-skip">Skip for now</button>`,
    html: `<div class="setup-full">
      <div class="setup-progress"><div style="width:${Math.round(100 * s.required_done / s.required_total)}%"></div></div>
      <p class="muted">Jobs open a GitHub issue and pull request, call an AI provider, and run on a machine. Finish the required steps; the optional ones can wait.</p>
      <section class="card">${setupListHtml(s)}</section>
      ${s.complete ? `<div class="row mt-16"><button class="btn primary big" id="setup-done">Continue to Home</button></div>` : ""}</div>`,
    after: () => {
      const seen = async () => { try { await api("config/setup-seen", { method: "POST", body: {} }); } catch {} if (setupState) setupState.seen = true; route(); };
      $("#setup-skip")?.addEventListener("click", seen);
      $("#setup-done")?.addEventListener("click", seen);
    },
  };
}

function renderSetupFab() {
  const fab = $("#setup-fab");
  if (!fab) return;
  const s = setupState;
  const show = s && !s.complete && ["home", "checkup"].includes(current.page) && !(current.page === "home" && !s.seen);
  fab.hidden = !show;
  if (s) fab.querySelector("span").textContent = `Setup ${s.required_done}/${s.required_total}`;
  if ($("#setup-panel") && !$("#setup-panel").hidden) renderSetupPanel();
}

function renderSetupPanel() {
  const s = setupState, panel = $("#setup-panel");
  if (!s) return;
  panel.innerHTML = `<div class="setup-panel-h"><strong>Setup</strong><span class="muted">${s.required_done}/${s.required_total} required</span>
    <button class="btn small ghost" data-setup-close aria-label="Close">✕</button></div><div class="setup-panel-b">${setupListHtml(s)}</div>`;
}

async function openSetupPanel() {
  const panel = $("#setup-panel");
  await loadSetup(true);
  if (!setupState) return toast("The setup checklist isn't available yet. It will appear after your next Orchestrator update.", true);
  renderSetupPanel();
  panel.hidden = false;
}
const closeSetupPanel = () => { const p = $("#setup-panel"); if (p) p.hidden = true; };

document.addEventListener("click", (e) => {
  if (e.target.closest("#setup-fab, [data-setup-open]")) { $("#setup-panel").hidden ? openSetupPanel() : closeSetupPanel(); return; }
  if (e.target.closest("[data-setup-close]")) { closeSetupPanel(); return; }
  const copy = e.target.closest("[data-setup-copy]");
  if (copy) { navigator.clipboard?.writeText(copy.dataset.setupCopy).then(() => toast("Copied")).catch(() => {}); return; }
  const go = e.target.closest("[data-setup-go]");
  if (go) {
    const item = setupState?.items.find((i) => i.id === go.dataset.setupGo);
    if (!item?.action) return;
    closeSetupPanel();
    if (item.action.type === "route") location.hash = item.action.to;
    else if (item.action.type === "run") runAction(item.action.action, item.action.params || {});
  }
});

const HOME_JOBS_SHOWN = 10;

// Jobs waiting in other projects: one line, not a second list.
function elsewhereLine(items) {
  const byProject = new Map();
  for (const i of items || []) byProject.set(i.project.root, { name: i.project.name, n: (byProject.get(i.project.root)?.n || 0) + 1 });
  if (!byProject.size) return "";
  return `<div class="card-b muted">Also waiting in other projects: ${[...byProject].map(([root, p]) => `<button type="button" class="linklike" data-switch-project="${esc(root)}" data-then="#/">${esc(p.name)} (${p.n})</button>`).join(", ")}</div>`;
}

pages.home = async (_, query) => {
  await loadSetup();
  if (setupState && !setupState.complete && !setupState.seen) return setupPage(setupState);
  const [{ jobs }, waiting, product] = await Promise.all([api("jobs"), api("inbox"), api("product").catch(() => null)]);
  const p = state.project;
  if (!p.branches) { // server predates the project-state branch list: use the Git endpoint
    try { p.branches = (await api("git")).branches || []; } catch { p.branches = []; }
  }
  // 1) Default ordered by most recent
  const sortedJobs = sortJobs(jobs);

  const HOME_FILTERS = {
    all: ["All", () => true],
    needs_you: ["Needs you", (j) => j.state.group === "needs_you" && !j.active_run],
    working: ["In progress", (j) => j.state.group === "working" || j.active_run],
    done: ["Completed", (j) => j.state.group === "done"],
  };
  let filter = (query && query.get("filter")) || "all";
  if (!HOME_FILTERS[filter]) filter = "all";
  const filteredJobs = sortedJobs.filter(HOME_FILTERS[filter][1]);

  const runningRuns = state.runs.filter((r) => r.running);
  const runningBanner = runningRuns.length
    ? `<section class="card mb-16"><div class="card-h"><h2>Running now</h2><span class="count">${runningRuns.length}</span></div>
        <div class="list">${runningRuns.map(liveRunCard).join("")}</div></section>`
    : "";

  return {
    title: p.name,
    sub: statusLine(p),
    html: `
      <label class="mobile-only project-inline"><span class="label">Project</span><select class="project-select-inline"></select></label>
      ${product ? productStripHtml(product) : ""}

      ${runningBanner}

      <section class="card">
        <div class="card-h" style="flex-wrap: wrap; gap: 10px;">
          <h2>Jobs <span class="count">${filteredJobs.length}</span></h2>
          <div class="filters">
            ${Object.entries(HOME_FILTERS).map(([key, [label, fn]]) => {
              const count = sortedJobs.filter(fn).length;
              return `<a class="btn small ${key === filter ? 'on' : ''}" href="#/?filter=${key}">${label} (${count})</a>`;
            }).join("")}
          </div>
        </div>
        ${filteredJobs.length ? `
          <div class="job-table">
            <div class="job-table-header">
              <div class="col-job">Job</div>
              <button type="button" class="col-status sort-head" data-sort="status" aria-label="Sort by status">Status${sortArrow("status")}</button>
              <button type="button" class="col-date sort-head" data-sort="updated" aria-label="Sort by last modified">Last modified${sortArrow("updated")}</button>
            </div>
            <div class="job-table-body" id="job-list">
              ${filteredJobs.map((j, n) => jobTableRow(j, { hidden: n >= HOME_JOBS_SHOWN })).join("")}
            </div>
            ${filteredJobs.length > HOME_JOBS_SHOWN ? `<div class="card-b"><button type="button" class="btn small" data-show-all="job-list">Show all ${filteredJobs.length}</button></div>` : ""}
          </div>
        ` : `<div class="empty">${!jobs.length ? 'No jobs yet. <strong>New job</strong> plans work from a description, a bug report or a design.' : 'No jobs match this filter.'}</div>`}
        ${elsewhereLine(waiting.elsewhere)}
      </section>`,
    after: () => {
      renderProjectSelect($(".project-select-inline"));
    },
  };
};

function jobHeaderActions(s, links = [], ctx = {}) {
  const j = { job: s.id };
  const next = s.state.next?.action;
  const items = [];
  if (!s.active_run) {
    if (next !== "debug" && ["review-needed", "completed", "debugging", "failed"].includes(s.status)) items.push(["Run fix", act("debug", j), "Another automated fix attempt"]);
    if (["review-needed", "completed"].includes(s.status)) items.push(["Still broken?", act("fix", j), "Reopen with what you saw"]);
    if (s.tasks_total && s.tasks_done < s.tasks_total && next !== "resume") items.push(["Resume next task", act("resume", j)]);
    if (s.branch && ["review-needed", "debugging"].includes(s.status)) items.push(["Deliver to testers", act("deliver", j), `Builds ${s.branch}`]);
    if (s.status === "scheduled" && next !== "execute") items.push(["Run now", act("execute", j)]);
    if (["planned", "designing", "human-needed", "review-needed", "debugging"].includes(s.status)) items.push(["Revise plan", act("revise", j), "Re-plan with what should change"]);
    if (ctx.canSplinter) items.push(["Split into sub-jobs", act("splinter_job", j), "Parallel child jobs and GitHub sub-issues"]);
  }
  items.push("---");
  items.push(["Open documentation", `data-href="#/docs/job/${encodeURIComponent(s.id)}"`, "This job's page in Docs"]);
  items.push(["Move to feature…", `data-job-feature="${esc(s.id)}"`, "Group this job under a feature"]);
  items.push(["Attach logs", act("link_logs", j), "Device crash logs or test traces"]);
  items.push(["Attach mockup or reference", act("attach_mockup", j)]);
  items.push(["Override models", act("select_models", j), "Planner, builder and reviewer"]);
  if (state.project?.mobile_app !== false) items.push(["Simulator visual check", act("run_visual_check", j), "Boot the simulator and capture screenshots"]);
  items.push(["Export bundle", act("export_job_bundle", j), "ZIP, iCloud or Google Drive"]);
  items.push("---");
  for (const link of links) items.push([`Open ${link.label}`, `data-open="${esc(link.url)}"`, "On GitHub"]);
  if (s.issue_number) items.push(["Close GitHub issue", `data-action="close_issue" data-params="${esc(JSON.stringify({ job: s.id, issue: s.issue_number }))}"`]);
  items.push(["Open in console", act("console")]);
  return `${moreMenu(items)}<button type="button" class="btn danger" ${act("delete_job", j)}${s.active_run ? ' disabled title="Stop the running task before deleting"' : ""}>Delete</button>`;
}

document.addEventListener("click", async (e) => {
  const mv = e.target.closest("[data-job-feature]");
  if (!mv) return;
  const jobId = mv.dataset.jobFeature;
  try {
    const [{ features }, { job }] = [await api("features"), await api(`jobs/${encodeURIComponent(jobId)}`)];
    if (!features.length) { toast("Create a feature first.", true); location.hash = "#/features"; return; }
    const v = await formDialog("Move to feature", `<label class="field"><span>Feature</span><select name="feature">
      <option value="">None</option>${features.map((f) => `<option value="${esc(f.id)}" ${f.id === job.feature ? "selected" : ""}>${esc(f.name)}</option>`).join("")}</select></label>`, "Move");
    if (!v) return;
    await api(`jobs/${encodeURIComponent(jobId)}/feature`, { method: "POST", body: { feature: v.feature } });
    toast("Moved"); route();
  } catch (err) { toast(err.message, true); }
});

document.addEventListener("click", (e) => {
  const open = e.target.closest("[data-open]");
  if (open) window.open(open.dataset.open, "_blank", "noopener");
});

function formatJobTests(t) {
  if (!t) return "⚙️ Pending execution";
  const st = (t.status || "").toLowerCase();
  const passed = t.passed_count || 0;
  const failed = t.failed_count || 0;
  if (failed > 0) return `❌ ${failed} failed${passed ? ` (${passed} passed)` : ""}`;
  if (passed > 0) return `✅ ${passed} passed`;
  if (st === "running" || st === "in_progress") return "⚙️ Running tests...";
  if (st === "passed") return "✅ Passed";
  if (st === "failed") return "❌ Failed";
  return "⚙️ Pending execution";
}

pages.job = async ([id]) => {
  const data = await api(`jobs/${encodeURIComponent(id)}`);
  const { summary: s, job, outputs, logs, runs, docs, changes, links, test_summary: testSummary, pipeline: pipe, tasks: jobTasks, completed_tasks: jobCompleted, next_task: jobNextTask } = data;

  const activeRun = runs.find((r) => r.running);
  const summary = { ...s, active_run: !!activeRun };

  const pipeline = {
    planner: pipe?.planner || job.planner || "default",
    builder: pipe?.builder || job.builder || "default",
    reviewer: pipe?.reviewer || job.reviewer || "default",
  };

  const tasks = Array.isArray(jobTasks) && jobTasks.length ? jobTasks : (Array.isArray(job.tasks) ? job.tasks : (job.plan?.tasks || []));
  const completed = jobCompleted || job.completed_tasks || job.completed_task_indices || [];
  const done = new Set(completed.map(String));
  const tasksDone = done.size;
  const tasksTotal = tasks.length;
  const tasksPct = tasksTotal > 0 ? Math.round((tasksDone / tasksTotal) * 100) : 0;
  const nextTask = jobNextTask || (tasksTotal > tasksDone && tasks[tasksDone] ? (typeof tasks[tasksDone] === "object" ? (tasks[tasksDone].name || tasks[tasksDone].title || tasks[tasksDone].description || `Task ${tasksDone + 1}`) : String(tasks[tasksDone])) : null);

  const displayId = s.display_id || (s.issue_number ? `#${s.issue_number} (${s.id})` : s.id);
  const kindUpper = (s.type || s.kind || "FEATURE").toUpperCase();
  const testsDisplay = formatJobTests(testSummary);
  const scope = data.scope || null;
  const phone = matchMedia("(max-width: 760px)").matches;
  const canEditPlan = Array.isArray(job.plan?.tasks) && !activeRun && !["completed", "archived", "discarded", "decomposed"].includes(s.status) && s.status !== "executing";
  const fold = (title, count, body) => `<section class="card mb-16"><details class="fold" ${phone ? "" : "open"}><summary class="card-h"><h2>${title}</h2>${count === "" ? "" : `<span class="count">${count}</span>`}</summary>${body}</details></section>`;
  const scopeWarning = scope && ["merge", "complete"].includes(s.state.next?.action) ? scope.findings.filter((f) => f.severity !== "low").reduce((n, f) => n + Math.max(f.files.length, 1), 0) : 0;
  const testCases = data.test_cases || { cases: [], summary: { by_type: {} } };
  const missingTests = testCases.cases.filter((c) => c.due && (c.status === "unassigned" || c.status === "planned")).length;
  let featureName = "";
  if (s.feature) { try { featureName = ((await api("features")).features.find((f) => f.id === s.feature) || {}).name || ""; } catch { /* chip falls back to the id */ } }
  const assumptions = (Array.isArray(job.plan?.assumptions) ? job.plan.assumptions : []).filter((t) => typeof t === "string" && t.trim());
  const conversation = Array.isArray(job.conversation) ? job.conversation.filter((m) => m && m.text) : [];

  let visualChecks = [];
  try { visualChecks = ((await api("visual-checks")).checks || []).filter((c) => c.job === id); } catch {}
  const canSplinter = (kindUpper === "FEATURE" || kindUpper === "PLAN" || s.type === "feature-plan" || job.type === "feature-plan") && tasksTotal > 0 && s.status !== "decomposed";

  // Delta calculations
  const localCount = changes?.local_files?.length || 0;
  let deltaUnsaved = "";
  if (localCount > 0) {
    const stat = (changes.local_summary || "").replace(/^\s*\d+\s+files?\s+changed,?\s*/, "").trim();
    deltaUnsaved = `${localCount} file${localCount > 1 ? "s" : ""} unsaved (local)${stat ? ` ${stat}` : ""}`;
  } else if (changes?.files?.length > 0) {
    const stat = (changes.summary_line || "").replace(/^\s*\d+\s+files?\s+changed,?\s*/, "").trim();
    deltaUnsaved = `${changes.files.length} file${changes.files.length > 1 ? "s" : ""} modified${stat ? ` ${stat}` : ""}`;
  } else {
    deltaUnsaved = "0 files unsaved (working tree clean)";
  }

  const allFiles = (changes?.files && changes.files.length) ? changes.files : (changes?.local_files || []);
  const previewFiles = allFiles.slice(0, 5);
  const remainingFiles = Math.max(0, allFiles.length - previewFiles.length);
  const filesHtml = previewFiles.length
    ? `${previewFiles.map((f) => `<span class="delta-file-chip mono">${esc(f.split("/").pop() || f)}</span>`).join(", ")}${remainingFiles > 0 ? ` <span class="muted">(+${remainingFiles} more)</span>` : ""}`
    : `<span class="muted">No modified files</span>`;

  const next = s.state.next;
  const heroAction = activeRun
    ? `<button class="btn" data-stop="${esc(activeRun.id)}" title="Stops the worker. Resume continues from the next task.">Pause</button>`
    : next ? `<button class="btn primary big" ${act(next.action, { job: s.id })}>${esc(next.label)}</button>` : "";

  const logFiles = logs.flatMap((l) => l.files.length
    ? l.files.map((f) => ({ label: l.files.length > 1 ? `${l.label} / ${f.split("/").pop()}` : l.label, path: f }))
    : [{ label: l.label }]);

  return {
    title: s.title,
    sub: `<span class="status-line"><span>${esc(kindUpper)}</span><span class="sep">·</span><span class="mono">${esc(displayId)}</span>${s.feature ? `<span class="sep">·</span><a href="#/features">${esc(featureName || s.feature)}</a>` : ""}${s.branch ? `<span class="sep">·</span><span class="mono">${esc(s.branch)}</span>` : ""}</span>`,
    actions: jobHeaderActions(summary, links, { canSplinter }),
    html: `
      ${runs.filter((r) => r.running).map((r) => `<section class="card mb-16"><div class="list">${liveRunCard(r)}</div></section>`).join("")}
      ${s.question ? `<section class="card mb-16"><div class="card-h"><h2>Question from the planner</h2></div><div class="card-b stack">
        <p class="question">${clamped(s.question, 320)}</p><div><button class="btn primary" ${act("answer", { job: s.id })}>Answer</button></div></div></section>` : ""}

      <section class="job-hero tone-${esc(s.state.tone || "")}">
        <div class="job-hero-main">
          <span class="pill ${esc(s.state.tone || "")}">${esc(s.state.label || s.status)}</span>
          <p class="job-hero-reason">${s.question && /architect/i.test(s.question) && /architect/i.test(s.state.reason || "") ? "The architect raised concerns (see the question above). Answer them, or accept its suggestions and re-plan." : clamped(s.state.reason || "", 320)}</p>
          ${scopeWarning ? `<p class="job-hero-reason"><button type="button" class="linklike" data-scroll-to="#scope-section">Scope check: ${scopeWarning} beyond the plan. Look before you ${s.state.next?.action === "merge" ? "merge" : "finish"}.</button></p>` : ""}
        </div>
        <div class="job-hero-action">${heroAction}</div>
      </section>

      ${(data.blockers || []).map((b) => `<div class="banner attention"><p><strong>This job can't start yet.</strong> ${esc(b.text)}${b.route ? ` <a class="btn small" href="${esc(b.route)}">${esc(b.fix)}</a>` : ""}</p></div>`).join("")}

      ${(job.plan?.slice_warnings || []).length ? `<section class="card mb-16"><div class="card-h"><h2>Plan check</h2></div><div class="card-b stack">
        <div>This plan looks like a stack of layers, not working slices. Mistakes in an early layer won't show until the last task.</div>
        <ul class="assumptions">${job.plan.slice_warnings.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
        <div class="muted">It was already sent back once. Use Revise plan in the More menu and ask for working slices.</div></div></section>` : ""}

      <section class="card mb-16">
        <div class="card-h"><h2>Progress</h2><span class="count">${tasksDone}/${tasksTotal}</span></div>
        <div class="card-b stack">
          <div class="tasks-progress-wrap">
            <div class="progress-bar-container"><div class="progress-bar-fill" style="width: ${tasksPct}%"></div></div>
            <span class="progress-text">${tasksPct}%</span>
          </div>
          ${nextTask ? `<div>Next: <strong>${esc(nextTask)}</strong></div>` : ""}
          <div class="muted">${esc(testsDisplay)}${pipe?.planner || pipe?.builder || pipe?.reviewer ? ` · ${esc(pipeline.planner)} → ${esc(pipeline.builder)} → ${esc(pipeline.reviewer)}` : ""}</div>
        </div>
      </section>

      <section class="card mb-16"><div class="card-h"><h2>Test cases</h2>${testCases.cases.length ? `<span class="count">${testCases.summary.covered}/${testCases.summary.automated} covered</span>` : ""}</div>
        ${testCases.cases.length ? `<div class="card-b">${testCaseSummaryHtml(testCases.summary)}${missingTests ? `<div class="notice bad" style="margin-top:10px">${missingTests} automated case${missingTests === 1 ? "" : "s"} due now ${missingTests === 1 ? "has" : "have"} no test yet.</div>` : ""}</div>
        <div class="list">${testCaseRowsHtml(testCases.cases)}</div>`
          : `<div class="empty">No test cases yet. Plans list them for every feature, bug fix and coverage job.</div>`}</section>

      ${assumptions.length ? `<section class="card mb-16"><div class="card-h"><h2>What the AI assumed</h2><span class="count">${assumptions.length}</span></div>
        <div class="card-b"><ul class="assumptions">${assumptions.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
        <div class="muted">Wrong about something? Use Revise plan in the menu.</div></div></section>` : ""}

      <section class="card mb-16">
        <div class="card-h"><h2>Changes</h2>${changes?.base ? `<span class="count">vs ${esc(changes.base)}</span>` : ""}</div>
        <div class="card-b delta-summary-line">
          <div class="delta-line unsaved">${esc(deltaUnsaved)}</div>
          <div class="diff-files">${allFiles.slice(0, 12).map((f) => `<details class="diff-file" data-diff-path="${esc(f)}"><summary class="mono">${esc(f)}</summary><pre class="diff" aria-live="polite">Loading…</pre></details>`).join("") || `<span class="muted">No modified files</span>`}${allFiles.length > 12 ? `<div class="muted">+${allFiles.length - 12} more (see the full diffstat below)</div>` : ""}</div>
        </div>
        ${changes?.hypothesis ? `<div class="card-b" style="border-top: 1px solid var(--border); padding-top: 10px; font-size: 13px;"><strong>Why:</strong> ${esc(changes.hypothesis)}</div>` : ""}
        ${changes?.diffstat ? `<details class="raw" style="border-top: 1px solid var(--border);"><summary style="padding: 8px 16px; font-size: 12.5px; color: var(--muted); cursor: pointer;">View full diffstat (${allFiles.length} files)</summary><pre class="file" style="margin: 0; border: none; border-radius: 0;">${esc(changes.diffstat)}</pre></details>` : ""}
      </section>

      ${scope ? `<section class="card mb-16" id="scope-section"><div class="card-h"><h2>Scope check</h2><span class="count">${scope.flagged ? `${scope.flagged} beyond the plan` : "Within the plan"}</span></div>
        <div class="card-b stack">
          <div class="muted">${scope.files} file${scope.files === 1 ? "" : "s"} changed, +${scope.added} lines${scope.accepted ? ` · ${scope.accepted} accepted as in scope` : ""}.${scope.clear ? " Everything changed is something the plan called for." : ""}</div>
          ${scope.findings.map((f) => `<div class="scope-finding"><div class="row gap-10"><span class="pill ${f.severity === "high" ? "failed" : f.severity === "medium" ? "attention" : "working"}">${esc(f.severity)}</span><strong>${esc(f.title)}</strong></div>
            <div class="muted">${esc(f.detail)}</div>
            ${f.files.length ? `<ul class="scope-files mono">${f.files.slice(0, 12).map((p) => `<li>${esc(p)}</li>`).join("")}${f.files.length > 12 ? `<li class="muted">+${f.files.length - 12} more</li>` : ""}</ul>
              <div><button class="btn small" data-scope-accept='${esc(JSON.stringify(f.files))}'>Accept as in scope</button></div>` : ""}</div>`).join("")}
          ${scope.findings.some((f) => f.files.length) || scope.accepted ? `<div class="row gap-10">
            ${scope.findings.some((f) => f.files.length) ? `<a class="btn small primary" href="#/new?type=quick&summary=${encodeURIComponent(scope.trim.summary)}&spec=${encodeURIComponent(scope.trim.spec)}">Create a job to trim it</a>` : ""}
            ${scope.accepted ? `<button class="btn small ghost" data-scope-reset>Reset accepted</button>` : ""}</div>` : ""}
        </div></section>` : ""}



      <!-- Decomposed Subtasks (if any) -->
      ${(job.subtask_job_ids && job.subtask_job_ids.length) ? `
        <section class="card mb-16">
          <div class="card-h"><h2>Decomposed Sub-Task Jobs</h2><span class="count">${job.subtask_job_ids.length}</span></div>
          <div class="list">
            ${job.subtask_job_ids.map((subId) => `
              <a class="item" href="#/job/${encodeURIComponent(subId)}">
                <div class="main-col"><div class="title mono">${esc(subId)}</div><div class="meta">Parallel subtask job</div></div>
                <span class="arrow">→</span>
              </a>
            `).join("")}
          </div>
        </section>
      ` : ""}

      <!-- Simulator Visual QA Screenshots (if any) -->
      ${(visualChecks.length && visualChecks[0].screenshots.length) ? `
        <section class="card mb-16">
          <div class="card-h"><h2>Simulator Visual QA Screenshots</h2><span class="count">${visualChecks[0].screenshots.length} captured</span></div>
          <div class="card-b" style="display:flex; gap:14px; overflow-x:auto; padding:12px 16px;">
            ${visualChecks[0].screenshots.map((img) => `
              <div style="flex:0 0 auto; text-align:center;">
                <a href="/api/visual-checks/${encodeURIComponent(visualChecks[0].id)}/screenshots/${encodeURIComponent(img)}" target="_blank" rel="noopener">
                  <img src="/api/visual-checks/${encodeURIComponent(visualChecks[0].id)}/screenshots/${encodeURIComponent(img)}" style="max-height:220px; border-radius:10px; border:1px solid var(--border); box-shadow:0 2px 8px rgba(0,0,0,0.15);" alt="Screenshot">
                </a>
                <div style="font-size:11px; margin-top:4px;" class="muted mono">${esc(img)}</div>
              </div>
            `).join("")}
          </div>
        </section>
      ` : ""}

      <section class="card mb-16" id="chat-section">
        <div class="card-h"><h2>Ask about this job</h2>${conversation.length ? `<span class="count">${conversation.length}</span>` : ""}</div>
        <div class="card-b stack">
          ${conversation.length ? `<div class="chat-thread" role="log" aria-live="polite">${conversation.map((m) => `<div class="chat-msg ${m.role === "user" ? "user" : "assistant"}"><span class="chat-who">${m.role === "user" ? "You" : "AI"}</span><div class="chat-text">${esc(m.text)}</div></div>`).join("")}</div>` : `<div class="muted">Ask why something was done, what a failure means, or what to change. Answers use this job's plan, progress and diff. It can't edit code; use Revise plan or Run fix to act on the answer.</div>`}
          <form id="chat-form" class="stack">
            <textarea name="message" required maxlength="4000" rows="2" aria-label="Your question about this job" placeholder="e.g. Why did you change the networking layer?"></textarea>
            <div class="row"><span class="spacer"></span><button class="btn primary" type="submit">Send</button></div>
          </form>
        </div>
      </section>

      <!-- Docs Section (Brief / Summary / Investigations) -->
      <div id="docs-section">
        ${docs.map((d, i) => `<section class="card mb-16"><details class="raw" ${i === 0 ? "open" : ""}><summary><strong>${esc(d.title)}</strong></summary><pre class="doc">${esc(d.text)}</pre></details></section>`).join("")}
      </div>

      <!-- Linked from connected apps -->
      <section class="card mb-16">
        <div class="card-h"><h2>Linked tickets, errors &amp; designs</h2><button class="btn small" data-attach-context="${esc(s.id)}">Attach…</button></div>
        <div class="list">${(job.external_links || []).map((l) => `<div class="item"><div class="main-col"><div class="title">${l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title || l.ref)} ↗</a>` : esc(l.title || l.ref)}</div>
          <div class="meta">${esc([PROVIDER_LABEL[l.provider] || l.provider, l.ref, l.detail].filter(Boolean).join(" · "))}</div></div></div>`).join("") || `<div class="empty">Nothing linked. Attach a Jira ticket, Trello card, Sentry issue or Figma design.</div>`}</div>
      </section>

      ${(job.integration_log || []).length ? `<section class="card mb-16">
        <div class="card-h"><h2>Updates sent to linked apps</h2></div>
        <div class="list">${job.integration_log.slice().reverse().map((e) => `<div class="item"><span aria-label="${e.ok ? "sent" : "failed"}">${e.ok ? "✅" : "⚠️"}</span><div class="main-col">
          <div class="title">${esc(e.message)}</div><div class="meta">${esc([PROVIDER_LABEL[e.provider] || e.provider, e.ref, String(e.event || "").replace(":", " · ").replace("_", " "), e.t].filter(Boolean).join(" · "))}</div></div></div>`).join("")}</div>
      </section>` : ""}

      <!-- Attached UI Mockups / References -->
      ${job.reference_artifacts?.length ? `
        <section class="card mb-16">
          <div class="card-h"><h2>Attached References & Mockups</h2><span class="count">${job.reference_artifacts.length}</span></div>
          <div class="list">
            ${job.reference_artifacts.map((ref) => `
              <div class="item">
                <div class="main-col">
                  <div class="title">${ref.url ? `<a href="${esc(ref.url)}" target="_blank" rel="noopener">${esc(ref.url)} ↗</a>` : `<span class="mono">${esc(ref.path || ref.type || "reference")}</span>`}</div>
                  ${ref.note ? `<div class="meta">${esc(ref.note)}</div>` : ""}
                </div>
              </div>
            `).join("")}
          </div>
        </section>
      ` : ""}

      <!-- Tasks, runs, logs and output: folded on phones so the page isn't one long scroll -->
      ${tasks.length || canEditPlan ? fold("Tasks Checklist", `${tasksDone}/${tasks.length}`, `<div class="list">${tasks.map((t, i) => {
          const title = typeof t === "string" ? t : (t.title || t.name || t.description || `Task ${i + 1}`);
          const detail = typeof t === "object" && t && t.title && t.description ? t.description : "";
          const key = typeof t === "object" && t ? String(t.id ?? i) : String(i);
          const isDone = done.has(key) || done.has(String(i));
          const controls = canEditPlan && !isDone ? `<div class="side task-controls">
              <button class="btn small ghost" data-task-op="edit" data-i="${i}" aria-label="Edit task: ${esc(title)}">Edit</button>
              <button class="btn small ghost" data-task-op="up" data-i="${i}" aria-label="Move up: ${esc(title)}" ${i === 0 || done.has(String(i - 1)) ? "disabled" : ""}>↑</button>
              <button class="btn small ghost" data-task-op="down" data-i="${i}" aria-label="Move down: ${esc(title)}" ${i === tasks.length - 1 ? "disabled" : ""}>↓</button>
              <button class="btn small ghost" data-task-op="remove" data-i="${i}" aria-label="Remove task: ${esc(title)}">Remove</button></div>` : "";
          const undo = data.undoable_task === i && !activeRun ? `<div class="side task-controls"><button class="btn small ghost" data-undo-task="${i}" aria-label="Undo task: ${esc(title)}">Undo</button></div>` : "";
          return `<div class="item"><span aria-label="${isDone ? "done" : "to do"}">${isDone ? "✅" : "○"}</span><div class="main-col"><div class="title">${esc(title)}</div>${detail ? `<div class="meta">${esc(detail)}</div>` : ""}</div>${controls}${undo}</div>`;
        }).join("") || `<div class="empty">No tasks yet.</div>`}</div>${canEditPlan ? `<div class="card-b"><button class="btn small" data-task-op="add">Add a task</button></div>` : ""}`) : ""}

      ${runs.length ? fold("Activity & Runs", runs.length, `<div class="list">${runs.map(runItem).join("")}</div>`) : ""}

      ${fold("Logs", logFiles.filter((f) => f.path).length || "", `<div class="list">${logFiles.map((f) => f.path
          ? `<a class="item" href="#/file?path=${encodeURIComponent(f.path)}"><div class="main-col"><div class="title mono">${esc(f.label)}</div></div></a>`
          : `<div class="item"><div class="main-col"><div class="title">${esc(f.label)}</div></div></div>`).join("")
          || `<div class="empty">No logs linked. Use Attach logs in the More menu.</div>`}</div>`)}

      ${outputs.length ? fold("Output files", outputs.length, `<div class="list">${outputs.map((o) => `<a class="item" href="#/file?path=${encodeURIComponent(o.path)}">
          <div class="main-col"><div class="title mono">${esc(o.path.split("/").slice(2).join("/") || o.path)}</div>
          <div class="meta">${(o.size / 1024).toFixed(1)} KB · ${esc(ago(o.mtime))}</div></div></a>`).join("")}</div>`) : ""}

      <!-- Technical Details -->
      <section class="card"><details class="raw"><summary>Technical details (${esc(s.id)})</summary><pre>${esc(JSON.stringify(job, null, 2))}</pre></details></section>
    `,
    after: () => {
      view.querySelectorAll("[data-task-op]").forEach((btn) => btn.addEventListener("click", async () => {
        const op = btn.dataset.taskOp, i = btn.dataset.i === undefined ? null : Number(btn.dataset.i);
        const call = async (body, ok) => { try { await api(`jobs/${encodeURIComponent(id)}/plan-tasks`, { method: "POST", body }); if (ok) toast(ok); route(); } catch (e) { toast(e.message, true); } };
        const form = (t = {}) => `<label class="field"><span>What this step does</span><input type="text" name="title" required maxlength="200" value="${esc(t.title || "")}"></label>
          <label class="field"><span>Details <span class="muted">(optional)</span></span><textarea name="description" rows="3" maxlength="2000">${esc(t.description || "")}</textarea></label>
          <label class="field"><span>Done when <span class="muted">(one per line)</span></span><textarea name="acceptance_criteria" rows="3">${esc((t.acceptance_criteria || []).join("\n"))}</textarea></label>
          <label class="field"><span>Files it should touch <span class="muted">(one per line)</span></span><textarea name="likely_files" rows="2">${esc((t.likely_files || []).join("\n"))}</textarea></label>`;
        if (op === "add") { const v = await formDialog("Add a task", form(), "Add"); if (v) call({ op: "add", ...v }, "Task added"); }
        else if (op === "edit") { const v = await formDialog("Edit task", form(tasks[i]), "Save"); if (v) call({ op: "edit", index: i, ...v }, "Task saved"); }
        else if (op === "remove") { call({ op: "remove", index: i }, "Task removed"); }
        else call({ op: "move", index: i, direction: op });
      }));
      view.querySelectorAll("[data-undo-task]").forEach((btn) => btn.addEventListener("click", async () => {
        const i = Number(btn.dataset.undoTask);
        const ok = await formDialog("Undo this task?", `<p>This adds a commit that reverses what the task changed, and marks it not done so it can run again. Nothing is deleted from history.</p>`, "Undo task");
        if (!ok) return;
        try { await api(`jobs/${encodeURIComponent(id)}/revert-task`, { method: "POST", body: { index: i } }); toast("Task undone"); route(); } catch (e) { toast(e.message, true); }
      }));
      view.querySelectorAll("[data-diff-path]").forEach((box) => box.addEventListener("toggle", async () => {
        if (!box.open || box.dataset.loaded) return;
        box.dataset.loaded = "1";
        const pre = box.querySelector("pre");
        try {
          const d = await api(`jobs/${encodeURIComponent(id)}/diff?path=${encodeURIComponent(box.dataset.diffPath)}`);
          pre.innerHTML = d.binary ? "Binary file: no text diff." : !d.diff.trim() ? "No changes to show." : d.diff.split("\n").map((line) => {
            const kind = line.startsWith("@@") ? "hunk" : line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : "";
            return `<span class="diff-line ${kind}">${esc(line)}</span>`;
          }).join("") + (d.truncated ? `<span class="diff-line hunk">… diff truncated</span>` : "");
        } catch (e) { pre.textContent = e.message; box.dataset.loaded = ""; }
      }));
      view.querySelectorAll("[data-scope-accept]").forEach((btn) => btn.addEventListener("click", async () => {
        try { await api(`jobs/${encodeURIComponent(id)}/scope`, { method: "POST", body: { op: "accept", paths: JSON.parse(btn.dataset.scopeAccept) } }); toast("Accepted as in scope"); route(); } catch (e) { toast(e.message, true); }
      }));
      $("[data-scope-reset]")?.addEventListener("click", async () => {
        try { await api(`jobs/${encodeURIComponent(id)}/scope`, { method: "POST", body: { op: "reset" } }); route(); } catch (e) { toast(e.message, true); }
      });
      const form = $("#chat-form");
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const button = form.querySelector("button");
        const message = new FormData(form).get("message");
        button.disabled = true; button.textContent = "Thinking…";
        try {
          await api(`jobs/${encodeURIComponent(id)}/chat`, { method: "POST", body: { message } });
          await route();
          $("#chat-section")?.scrollIntoView({ block: "end" });
        } catch (err) {
          toast(err.message, true);
          button.disabled = false; button.textContent = "Send";
        }
      });
    },
  };
};

const JOB_TYPE_INFO = [
  ["bug", "Bug fix", "Something is broken"],
  ["feature", "Feature", "Something new"],
  ["design", "Design", "A screen or flow"],
  ["coverage", "Tests", "Add missing tests"],
  ["quick", "Quick change", "A small tweak"],
];
const NJ_VIBES = [["minimalist", "Minimalist: clean, lots of space"], ["glassmorphism", "Glassy: frosted glass, vivid colour"], ["brutalist", "Bold: raw, high contrast"],
  ["high-energy", "Playful: animated, dynamic"], ["gothic-noir", "Dark and moody"], ["other", "Something else…"]];
const NJ_UPLOAD_LIMIT = 25 * 1024 * 1024;

async function uploadFile(file, endpoint = "uploads") {
  const backend = getBackendUrl(), token = getToken();
  const res = await fetch(`${backend ?? ""}/api/${endpoint}?name=${encodeURIComponent(file.name)}`, {
    method: "POST", body: file, credentials: backend ? "omit" : "same-origin",
    headers: { "Content-Type": "application/octet-stream", "X-Orchestrator-UI": "1", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

const formatBytes = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

pages.new = async (_, query) => {
  let features = [];
  try { features = (await api("features")).features; } catch { /* the field just doesn't show */ }
  let undefinedProduct = false; // nothing says what the product is, so plans would be guessing
  try { undefinedProduct = !(await api("product")).sections.some((x) => x.filled); } catch { /* older server: no nudge */ }
  const skipKey = `orchestrator_skip_discovery:${state.project?.root || ""}`;
  const skipped = () => { try { return localStorage.getItem(skipKey) === "1"; } catch { return false; } };
  const wanted = query.get("type");
  const st = { type: JOB_TYPE_INFO.some(([v]) => v === wanted) ? wanted : "bug", uploads: {}, uploading: 0, logs: new Set(), recent: null,
    v: { summary: query.get("summary") || "", details: query.get("spec") || "", repro: "", expected: "", vibe: "minimalist", customVibe: "", subsystems: "", url: "" } };

  const field = (label, input, hint = "") => `<label class="field"><span>${label}</span>${input}${hint ? `<small class="hint-text">${hint}</small>` : ""}</label>`;
  const opt = (text) => ` <span class="muted">(${text})</span>`;
  const uploadBox = (label, hint, accept, optional = true) => `<div class="field"><span>${label}${optional ? opt("optional") : ""}</span>
      <input type="file" id="nj-files" multiple accept="${accept}" aria-label="${esc(label)}"><small class="hint-text">${hint}</small>
      <div class="upload-chips row" id="nj-chips" aria-live="polite"></div></div>`;
  const logPicker = () => {
    const rows = (st.recent || []).map((l, i) => `<label class="check"><input type="checkbox" data-nj-log="${esc(l.path)}" ${st.logs.has(l.path) ? "checked" : ""}>
      <span>${esc(l.label)}<small>${esc(ago(l.mtime))} · ${esc(formatBytes(l.size))}${l.kind === "device" ? " · from a phone" : ""}${i === 0 ? " · newest" : ""}</small></span></label>`).join("");
    return `<fieldset class="field nj-logs"><span>Logs${opt("optional")}</span>
      ${st.recent === null ? `<small class="muted">Looking for recent logs…</small>` : rows || `<small class="muted">No recent logs found. Upload one below, or pull device logs first.</small>`}
      ${uploadBox("Upload logs or screenshots", "A crash log, a console dump, a screenshot of the problem. We read the end of each log.", "image/*,.log,.txt,.json,.md,.crash,.ips")}</fieldset>`;
  };
  const featureSelect = () => features.length ? field(`Part of a feature${opt("optional")}`, `<select name="feature"><option value="">None</option>${features.map((f) => `<option value="${esc(f.id)}" ${query.get("feature") === f.id ? "selected" : ""}>${esc(f.name)}</option>`).join("")}</select>`) : "";

  const fieldsHtml = async () => {
    const v = st.v;
    switch (st.type) {
      case "bug": {
        const picker = await linkPickerHtml({ prefer: ["sentry", "jira"], label: "Link a Sentry error or ticket", emptyHint: "" });
        return { picker, html: `
          ${field("What's going wrong?", `<input type="text" name="summary" required maxlength="500" value="${esc(v.summary)}" placeholder="e.g. Rejoining a lobby after backgrounding shows an empty seat">`)}
          ${field(`How do I make it happen?${opt("optional")}`, `<textarea name="repro" rows="3" placeholder="1. Join a lobby&#10;2. Background the app for 30 seconds&#10;3. Come back">${esc(v.repro)}</textarea>`, "One step per line.")}
          ${field(`What should happen instead?${opt("optional")}`, `<input type="text" name="expected" maxlength="2000" value="${esc(v.expected)}" placeholder="The seat is still mine">`)}
          ${logPicker()}${picker.html}` };
      }
      case "feature": {
        const picker = await linkPickerHtml({ prefer: ["jira", "trello"], label: "Link a ticket or card", emptyHint: "" });
        return { picker, html: `
          ${field("What should it do?", `<input type="text" name="summary" required maxlength="500" value="${esc(v.summary)}" placeholder="e.g. Let either player ask for a rematch">`)}
          ${field(`Anything specific?${opt("optional")}`, `<textarea name="spec" rows="4" placeholder="Who it's for, rules it must follow, what &quot;done&quot; looks like">${esc(v.details)}</textarea>`,
            "Skip anything you don't care about. The AI picks sensible defaults and lists what it assumed, so you can change it.")}
          ${picker.html}` };
      }
      case "design": {
        const picker = await linkPickerHtml({ prefer: ["figma"], label: "Pick a Figma design", emptyHint: "" });
        const vibeOptions = NJ_VIBES.map(([k, t]) => `<option value="${k}" ${k === v.vibe ? "selected" : ""}>${esc(t)}</option>`).join("");
        return { picker, html: `
          ${field("What are you designing?", `<textarea name="summary" required rows="3" maxlength="500" placeholder="e.g. A profile screen with stats, recent matches and an edit button">${esc(v.summary)}</textarea>`)}
          ${field("Look and feel", `<select name="vibe">${vibeOptions}</select>`)}
          <div class="field" id="nj-custom-vibe" ${v.vibe === "other" ? "" : "hidden"}>${field("Describe it", `<input type="text" name="customVibe" maxlength="200" value="${esc(v.customVibe)}" placeholder="e.g. Soft UI, like Things 3">`)}</div>
          <fieldset class="field"><span>References${opt("optional")}</span>
            ${uploadBox("Upload files", "Images, a PDF, a .fig file or an HTML mockup: screens you like, a sketch, an existing design.", "image/*,.pdf,.fig,.html,.htm", false)}
            ${picker.connected ? picker.html : field("Or paste a link", `<input type="url" name="url" value="${esc(v.url)}" placeholder="https://www.figma.com/design/…">`,
              `A Figma or other design link. <a href="#/connections">Connect Figma</a> to search your files here instead.`)}</fieldset>
          ${field(`Anything else?${opt("optional")}`, `<textarea name="spec" rows="2" placeholder="Constraints, accessibility needs, things to avoid">${esc(v.details)}</textarea>`)}` };
      }
      case "coverage":
        return { picker: null, html: `
          ${field("What should be tested?", `<input type="text" name="summary" required maxlength="500" value="${esc(v.summary)}" placeholder="e.g. The lobby and seat assignment">`)}
          ${field(`Specific parts${opt("optional")}`, `<input type="text" name="subsystems" maxlength="2000" value="${esc(v.subsystems)}" placeholder="e.g. SeatService, rejoin flow">`, "Leave blank to cover the whole area.")}` };
      default:
        return { picker: null, html: `${field("What should change?", `<textarea name="summary" required rows="4" maxlength="20000" placeholder="e.g. Rename Foo to Bar everywhere and update the docs">${esc(v.summary)}</textarea>`, "This goes straight to the builder with no separate planning step.")}` };
    }
  };

  const mine = () => (st.uploads[st.type] ||= []); // files belong to the kind of job they were added to
  const form = () => $("#new-job");
  const read = (name) => form()?.elements[name]?.value ?? undefined;
  const capture = () => { // remember what was typed before the fields are swapped
    const v = st.v;
    for (const [key, name] of [["summary", "summary"], ["details", "spec"], ["repro", "repro"], ["expected", "expected"], ["vibe", "vibe"], ["customVibe", "customVibe"], ["subsystems", "subsystems"], ["url", "url"]]) {
      const val = read(name); if (val !== undefined) v[key] = val;
    }
  };
  const chips = () => {
    const box = $("#nj-chips"); if (!box) return;
    box.innerHTML = mine().map((u, i) => `<span class="chip">${esc(u.name)} · ${esc(formatBytes(u.size))}<button type="button" data-nj-remove="${i}" aria-label="Remove ${esc(u.name)}">✕</button></span>`).join("")
      + (st.uploading ? `<span class="muted">Uploading…</span>` : "");
  };

  let picker = null;
  const render = async () => {
    const out = await fieldsHtml();
    picker = out.picker;
    // A feature or design is planned against who it's for and what version 1 won't do; offer to write that down first.
    const nudge = undefinedProduct && !skipped() && (st.type === "feature" || st.type === "design");
    $("#nj-discovery").innerHTML = nudge ? `<div class="notice" role="note"><strong>Before the first feature: what are you building, and who is it for?</strong>
      <div class="muted">Plans are much better when the AI knows. Say it in a few sentences, let it draft one from this project, or import a PRD you already have.</div>
      <div class="row"><a class="btn small primary" href="#/product">Define the product first</a><button type="button" class="btn small ghost" id="nj-skip-discovery">Skip for now</button></div></div>` : "";
    $("#nj-skip-discovery")?.addEventListener("click", () => { try { localStorage.setItem(skipKey, "1"); } catch { /* remembered for this visit only */ } undefinedProduct = false; $("#nj-discovery").innerHTML = ""; });
    $("#nj-fields").innerHTML = out.html;
    picker?.wire();
    chips();
    $("#nj-fields input, #nj-fields textarea")?.focus({ preventScroll: true });
    $("#nj-files")?.addEventListener("change", async (e) => {
      for (const file of [...e.target.files]) {
        if (file.size > NJ_UPLOAD_LIMIT) { toast(`${file.name} is over ${NJ_UPLOAD_LIMIT / 1048576} MB`, true); continue; }
        st.uploading += 1; chips();
        try { const type = st.type; const saved = await uploadFile(file); (st.uploads[type] ||= []).push(saved); } catch (err) { toast(`${file.name}: ${err.message}`, true); }
        st.uploading -= 1; chips();
      }
      e.target.value = "";
    });
    $("#nj-chips")?.addEventListener("click", (e) => { const b = e.target.closest("[data-nj-remove]"); if (b) { mine().splice(Number(b.dataset.njRemove), 1); chips(); } });
    $("select[name=vibe]")?.addEventListener("change", (e) => { $("#nj-custom-vibe").hidden = e.target.value !== "other"; });
    document.querySelectorAll("[data-nj-log]").forEach((box) => box.addEventListener("change", () => { box.checked ? st.logs.add(box.dataset.njLog) : st.logs.delete(box.dataset.njLog); }));
    if (st.type === "bug" && st.recent === null) {
      // only a log from the last day is likely to be about this bug, so only that one starts ticked
      try { st.recent = (await api("recent-logs")).logs; if (st.logs.size === 0 && st.recent[0] && Date.now() / 1000 - st.recent[0].mtime < 86400) st.logs.add(st.recent[0].path);  } catch { st.recent = []; }
      if (st.type === "bug") { capture(); await render(); }
    }
  };

  return {
    title: "New job",
    sub: "Tell it what you need. It's planned, then built on a worker, and anything that needs you shows up on Home.",
    html: `
    <form class="card card-b stack" id="new-job">
      <div class="field"><span>What kind of work?</span>
        <div class="segmented" role="radiogroup" aria-label="Kind of work">${JOB_TYPE_INFO.map(([v, t, d]) =>
          `<label><input type="radio" name="type" value="${v}" ${st.type === v ? "checked" : ""}>${t}<small>${d}</small></label>`).join("")}
        </div></div>
      <div id="nj-discovery"></div>
      <div class="stack" id="nj-fields"></div>
      ${featureSelect()}
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
          <label class="check risky"><input type="checkbox" name="yolo"><span>Autopilot<small>Skips every confirmation and runs follow-up steps automatically, including a real Firebase release to testers.</small></span></label>
        </div>
      </details>
      <div class="row"><span class="spacer"></span><button class="btn primary big" type="submit">Create job</button></div>
    </form>`,
    after: async () => {
      form().querySelectorAll("input[name=type]").forEach((r) => r.addEventListener("change", async () => { capture(); st.type = r.value; await render(); }));
      await render();
      form().addEventListener("submit", (e) => {
        e.preventDefault();
        if (st.uploading) { toast("Wait for the upload to finish.", true); return; }
        capture();
        let links = [];
        try { links = JSON.parse(form().elements.links?.value || "[]"); } catch { /* none */ }
        const f = new FormData(form());
        const v = st.v;
        const params = { type: st.type, summary: v.summary, branch_mode: f.get("branch_mode") || "new", no_dispatch: f.has("no_dispatch"), yolo: f.has("yolo"), free: f.has("free"),
          feature: f.get("feature") || "", links, files: mine().map((u) => u.path) };
        if (st.type === "bug") Object.assign(params, { repro: v.repro, expected: v.expected, logs: [...st.logs] });
        if (st.type === "feature") params.spec = v.details;
        if (st.type === "design") Object.assign(params, { spec: v.details, vibe: v.vibe === "other" ? (v.customVibe.trim() || "minimalist") : v.vibe, urls: v.url.trim() ? [v.url.trim()] : [] });
        if (st.type === "coverage") params.subsystems = v.subsystems;
        runAction("new_job", params);
      });
    },
  };
};

const FEATURE_STATUS = { planned: ["Planned", "working"], "in-progress": ["In progress", "working"], complete: ["Complete", "done"] };

function featureFormBody(f = {}, others = []) {
  return `<label class="field"><span>Name</span><input type="text" name="name" required maxlength="80" value="${esc(f.name || "")}" placeholder="e.g. Lobby seats"></label>
    <label class="field"><span>What it does <span class="muted">(optional)</span></span><textarea name="summary" rows="2" maxlength="500">${esc(f.summary || "")}</textarea></label>
    <label class="field"><span>Which use case does it serve? <span class="muted">(optional)</span></span><input type="text" name="serves" maxlength="300" value="${esc(f.serves || "")}" placeholder="e.g. As a player I can ask for a rematch"><small class="hint-text">Planners are told, and keep each task tied to it.</small></label>
    <label class="field"><span>Code it owns <span class="muted">(optional, one path per line)</span></span><textarea name="paths" rows="3" placeholder="Sources/Lobby/">${esc((f.paths || []).join("\n"))}</textarea>
      <small class="muted">Used to warn when two features claim the same code.</small></label>
    ${others.length ? `<fieldset class="field"><span>Builds on <span class="muted">(optional)</span></span>${others.map((o) => `<label class="check"><input type="checkbox" name="depends_on" value="${esc(o.id)}" ${(f.depends_on || []).includes(o.id) ? "checked" : ""}><span>${esc(o.name)}</span></label>`).join("")}</fieldset>` : ""}`;
}

function featureValues(v) { // formDialog keeps one value per name, so read the checkboxes ourselves
  return { ...v, depends_on: [...document.querySelectorAll('#dialog-form input[name="depends_on"]:checked')].map((i) => i.value) };
}

function drawFeatureLinks(map) {
  const svg = map.querySelector(".fmap-links");
  const box = map.getBoundingClientRect();
  svg.setAttribute("width", box.width); svg.setAttribute("height", box.height);
  const wide = getComputedStyle(map).flexDirection === "row";
  svg.innerHTML = wide ? [...map.querySelectorAll(".fmap-node[data-deps]")].flatMap((node) => {
    const to = node.getBoundingClientRect();
    return node.dataset.deps.split(",").filter(Boolean).map((id) => {
      const from = map.querySelector(`.fmap-node[data-id="${CSS.escape(id)}"]`)?.getBoundingClientRect();
      if (!from) return "";
      const x1 = from.right - box.left, y1 = from.top + from.height / 2 - box.top, x2 = to.left - box.left, y2 = to.top + to.height / 2 - box.top;
      const mid = (x1 + x2) / 2;
      return `<path d="M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}" class="fmap-link"/>`;
    });
  }).join("") : "";
}

// "Do this feature's jobs work together?": one line, one action, and the last result in plain words.
function combineLine(f) {
  const c = f.combine;
  if (!c || c.branches.length < 2) return "";
  const r = c.result;
  const stale = c.stale ? ` <span class="muted">Out of date: a branch or the base has changed since.</span>` : "";
  const files = (r?.conflicts || []).map((x) => `${esc(x.branch)}${x.files.length ? ` (${x.files.map(esc).join(", ")})` : ""}`).join("; ");
  const say = !r ? `<span class="muted">Not checked yet. Each job passed alone; this tries them all together.</span>`
    : r.status === "pass" ? `<span class="pill done">Works together</span> <span class="muted">${r.merged.length} branches merged, build and tests pass.</span>`
    : r.status === "conflict" ? `<span class="pill failed">Conflict</span> <span class="muted">These don't merge cleanly: ${files}.</span>`
    : r.status === "build-failed" || r.status === "test-failed" ? `<span class="pill failed">${r.status === "build-failed" ? "Build fails" : "Tests fail"} together</span> <span class="muted">Each branch passed alone; the combination doesn't.</span>`
    : `<span class="muted">${esc(r.error || "Couldn't check.")}</span>`;
  return `<div class="row gap-10">${say}${stale}<button type="button" class="btn small" ${act("verify_feature", { feature: f.id })}>${r ? "Check again" : "Check together"}</button></div>`;
}

pages.features = async (_, query) => {
  const view_ = query.get("view") === "map" ? "map" : "list";
  const [{ features, overlaps, archived_jobs: archived }, { jobs }] = await Promise.all([api("features"), api("jobs")]);
  const byId = new Map([...archived, ...jobs].map((j) => [j.id, j]));
  const names = new Map(features.map((f) => [f.id, f.name]));
  const loose = jobs.filter((j) => !j.feature && j.state.group !== "done");
  const overlapping = new Set(overlaps.flatMap((o) => o.features));
  const toggle = `<div class="filters"><a class="btn small ${view_ === "list" ? "on" : ""}" href="#/features">List</a><a class="btn small ${view_ === "map" ? "on" : ""}" href="#/features?view=map">Map</a></div>`;
  const mapHtml = () => {
    const depth = Math.max(...features.map((f) => f.layer));
    const cols = Array.from({ length: depth + 1 }, (_, layer) => features.filter((f) => f.layer === layer));
    return `<div class="fmap" id="fmap"><svg class="fmap-links" aria-hidden="true"></svg>
      ${cols.map((col, layer) => `<div class="fmap-col"><div class="fmap-col-h">${layer === 0 ? "Foundations" : `Builds on layer ${layer}`}</div>
        ${col.map((f) => {
          const [label, tone] = FEATURE_STATUS[f.status] || FEATURE_STATUS.planned;
          return `<a class="fmap-node tone-${tone}" href="#/features?focus=${encodeURIComponent(f.id)}" data-id="${esc(f.id)}" data-deps="${esc(f.depends_on.join(","))}">
            <strong>${overlapping.has(f.id) ? "⚠ " : ""}${esc(f.name)}</strong>
            <span class="pill ${tone}">${esc(label)}</span>
            <span class="muted">${f.jobs_done}/${f.jobs_total} jobs${f.jobs_need_you ? ` · ${f.jobs_need_you} need you` : ""}</span>
            ${f.depends_on.length ? `<span class="muted">Builds on ${f.depends_on.map((d) => esc(names.get(d) || d)).join(", ")}</span>` : ""}
            ${f.waiting_on.length ? `<span class="fmap-wait">Waiting on ${f.waiting_on.map((d) => esc(names.get(d) || d)).join(", ")}</span>` : ""}
          </a>`;
        }).join("")}</div>`).join("")}</div>
      ${loose.length ? `<div class="muted mt-12">${loose.length} open job${loose.length === 1 ? "" : "s"} not in any feature.</div>` : ""}`;
  };
  const card = (f) => {
    const [label, tone] = FEATURE_STATUS[f.status] || FEATURE_STATUS.planned;
    const pct = f.jobs_total ? Math.round((f.jobs_done / f.jobs_total) * 100) : 0;
    const complete = f.status === "complete";
    return `<section class="card mb-16" data-feature="${esc(f.id)}">
      <div class="card-h"><div class="row gap-10"><h2>${esc(f.name)}</h2><span class="pill ${tone}">${esc(label)}</span></div>
        ${moreMenu([
          ["Documentation", `data-href="#/docs/feature/${encodeURIComponent(f.id)}"`, "This feature's page in Docs"],
          [complete ? "Reopen" : "Mark complete", `data-feature-action="${complete ? "reopen" : "complete"}" data-id="${esc(f.id)}"`, complete ? "More work is coming" : "Done for now. New work reopens it"],
          ["Edit", `data-feature-action="edit" data-id="${esc(f.id)}"`],
          ["Delete", `data-feature-action="delete" data-id="${esc(f.id)}"`, "Its jobs stay, unassigned", "danger"],
        ])}</div>
      <div class="card-b stack">
        ${f.summary ? `<div>${esc(f.summary)}</div>` : ""}
        ${f.reopened ? `<div class="muted">Reopened: new work was added after it was marked complete.</div>` : ""}
        ${combineLine(f)}
        ${f.kpis.length ? `<div><a href="#/measure">KPIs</a>: ${["on-track", "behind", "no-data", "no-target"].map((k) => [k, f.kpis.filter((x) => x.status.state === k).length]).filter(([, n]) => n).map(([k, n]) => `${n} ${KPI_STATE[k][0].toLowerCase()}`).join(" · ")}</div>` : ""}
        ${f.paths.length ? `<div class="muted mono">${f.paths.map(esc).join(" · ")}</div>` : ""}
        <div class="tasks-progress-wrap"><div class="progress-bar-container"><div class="progress-bar-fill" style="width: ${pct}%"></div></div>
          <span class="progress-text">${f.jobs_done}/${f.jobs_total} jobs</span></div>
        ${f.jobs_need_you ? `<div><span class="pill attention">${f.jobs_need_you} need${f.jobs_need_you === 1 ? "s" : ""} you</span></div>` : ""}
        <div class="list">${f.job_ids.map((id) => byId.get(id)).filter(Boolean).map((j) => jobItem(j)).join("") || `<div class="empty">No jobs yet. <a href="#/new?feature=${encodeURIComponent(f.id)}">Start one</a>, or move an existing job here from its menu.</div>`}</div>
      </div></section>`;
  };
  return {
    title: "Features",
    sub: "What the jobs add up to. Each feature owns its own slice of the code.",
    actions: `<button class="btn primary" id="new-feature">New feature</button>`,
    html: view_ === "map" && features.length ? `
      ${toggle}
      ${overlaps.map((o) => `<div class="banner attention"><p><strong>${esc(o.names.join(" and "))} overlap.</strong> ${esc(o.reasons.join("; "))}.</p></div>`).join("")}
      ${mapHtml()}` : `
      ${features.length ? toggle : ""}
      ${overlaps.map((o) => `<div class="banner attention"><p><strong>${esc(o.names.join(" and "))} overlap.</strong> ${esc(o.reasons.join("; "))}.</p></div>`).join("")}
      ${features.map(card).join("") || `<div class="empty">No features yet. A feature is something a user would name, like “Lobby seats”. Create one, then group jobs under it.</div>`}
      ${loose.length ? `<section class="card"><div class="card-h"><h2>Not in a feature</h2><span class="count">${loose.length}</span></div>
        <div class="list">${loose.map((j) => jobItem(j)).join("")}</div></section>` : ""}`,
    after: () => {
      const map = $("#fmap");
      if (map) {
        const redraw = () => drawFeatureLinks(map);
        redraw();
        window.addEventListener("resize", redraw);
        cleanup.push(() => window.removeEventListener("resize", redraw));
      }
      const focus = query.get("focus");
      if (focus) {
        const el = view.querySelector(`[data-feature="${CSS.escape(focus)}"]`);
        el?.scrollIntoView({ block: "center" });
        el?.classList.add("highlight-flash");
      }
      $("#new-feature").addEventListener("click", async () => {
        const v = await formDialog("New feature", featureFormBody({}, features), "Create");
        if (!v) return;
        try { await api("features", { method: "POST", body: featureValues(v) }); toast("Feature created"); route(); } catch (e) { toast(e.message, true); }
      });
      view.querySelectorAll("[data-feature-action]").forEach((btn) => btn.addEventListener("click", async () => {
        const id = btn.dataset.id, f = features.find((x) => x.id === id);
        try {
          if (btn.dataset.featureAction === "edit") {
            const v = await formDialog("Edit feature", featureFormBody(f, features.filter((o) => o.id !== id)), "Save");
            if (v) await api(`features/${encodeURIComponent(id)}`, { method: "POST", body: featureValues(v) });
            else return;
          } else if (btn.dataset.featureAction === "delete") {
            const { undo } = await api(`features/${encodeURIComponent(id)}`, { method: "DELETE", body: {} });
            toast(`Deleted “${f.name}”`, false, { label: "Undo", run: async () => { await api("features/restore", { method: "POST", body: { feature: undo.feature, jobs: undo.jobs } }); route(); } });
          } else {
            await api(`features/${encodeURIComponent(id)}`, { method: "POST", body: { status: btn.dataset.featureAction === "complete" ? "complete" : "in-progress" } });
          }
          route();
        } catch (e) { toast(e.message, true); }
      }));
    },
  };
};

// Long model-written text (an architect's concerns, a failure reason) is shown short with "Show more".
function clamped(text, max = 240) {
  const t = String(text || "");
  if (t.length <= max) return esc(t);
  const cut = t.slice(0, max).replace(/\s+\S*$/, "");
  return `<span data-clamp><span data-clamp-short>${esc(cut)}…</span><span data-clamp-full hidden>${esc(t)}</span> <button type="button" class="linklike" data-clamp-toggle aria-expanded="false">Show more</button></span>`;
}
document.addEventListener("click", (e) => {
  const toggle = e.target.closest("[data-clamp-toggle]");
  if (toggle) {
    const box = toggle.closest("[data-clamp]");
    const open = box.querySelector("[data-clamp-full]").hidden;
    box.querySelector("[data-clamp-full]").hidden = !open;
    box.querySelector("[data-clamp-short]").hidden = open;
    toggle.textContent = open ? "Show less" : "Show more";
    toggle.setAttribute("aria-expanded", String(open));
    return;
  }
  const all = e.target.closest("[data-show-all]");
  if (all) {
    const list = document.getElementById(all.dataset.showAll);
    list.querySelectorAll("[hidden]").forEach((el) => { el.hidden = false; });
    all.remove();
  }
});

pages.delivery = async () => {
  const d = await api("delivery");
  const { live, testers, ready, pipeline } = d;
  const baseRun = pipeline.runs.find((r) => r.on_base);
  const when = (iso) => (iso ? ago(Date.parse(iso) / 1000) : "");
  const latest = testers.latest;
  const liveCard = live ? `<section class="card mb-16"><div class="card-h"><h2>Live</h2><span class="count mono">${esc(live.branch)}</span></div>
      <div class="card-b stack">
        <div><strong class="mono">${esc(live.commit)}</strong> ${esc(live.subject)} <span class="muted">· ${esc(live.when)}</span></div>
        <div class="row gap-10">
          <span class="muted">${live.tag ? `Last release ${esc(live.tag)}` : "No release tagged yet"}</span>
          ${live.unreleased ? `<span class="pill attention">${live.unreleased} change${live.unreleased === 1 ? "" : "s"} not released</span>` : `<span class="pill done">Up to date</span>`}
          ${baseRun ? `<a href="${esc(baseRun.url || "#")}" target="_blank" rel="noopener"><span class="pill ${esc(baseRun.tone)}">Build ${esc(baseRun.outcome.replace("_", " "))}</span></a>` : ""}
        </div></div></section>`
    : `<section class="card mb-16"><div class="card-h"><h2>Live</h2></div><div class="empty">The base branch isn't in this repository yet, so there's nothing live to show.</div></section>`;
  const testersCard = `<section class="card mb-16"><div class="card-h"><h2>With testers</h2>
      ${testers.configured ? `<button class="btn small primary" ${act("distribute")}>Send current branch</button>` : `<a class="btn small" href="#/config/firebase">Set up Firebase</a>`}</div>
      <div class="card-b stack">
        ${latest ? `<div><strong>${esc(latest.version || "Unknown version")}${latest.build ? ` (${esc(latest.build)})` : ""}</strong>
            <span class="muted">· ${esc(ago(latest.delivered))} · ${esc(latest.branch)}${latest.recipients ? ` · to ${esc(latest.recipients)}` : ""}</span>
            ${latest.job_id ? ` <a href="#/jobs/${encodeURIComponent(latest.job_id)}">${esc(latest.title || latest.job_id)}</a>` : ""}</div>`
          : `<div class="muted">${testers.configured ? "Nothing has been sent to testers yet." : "Testers get builds through Firebase App Distribution. Set it up once, then send any branch."}</div>`}
        ${testers.groups.length ? `<div class="row" style="gap:6px"><span class="muted">Tester groups</span>${testers.groups.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</div>` : ""}
        ${testers.configured && !testers.cli_installed ? `<div class="notice bad">The Firebase CLI isn't installed on this machine, so sending will fail. Install it with <code>npm i -g firebase-tools</code>.</div>` : ""}
        ${testers.configured ? (testers.invite_url
          ? `<div class="row gap-10"><span class="muted">Invite link</span><a class="mono" href="${esc(testers.invite_url)}" target="_blank" rel="noopener">${esc(testers.invite_url.replace(/^https:\/\//, "").slice(0, 48))}</a><button class="btn small" data-setup-copy="${esc(testers.invite_url)}">Copy</button><a class="btn small ghost" href="#/config/firebase">Change</a></div>`
          : `<div class="muted">Want people to join without you adding them? <a href="#/config/firebase">Add the group's invite link</a> and share it from here.</div>`) : ""}
        ${testers.configured ? `<div class="muted">Add or remove testers and devices in the <a href="https://console.firebase.google.com/" target="_blank" rel="noopener">Firebase console</a>.</div>` : ""}
      </div></section>`;
  const readyCard = ready.length ? `<section class="card mb-16"><div class="card-h"><h2>Ready to ship</h2><span class="count">${ready.length}</span></div>
      <div class="list">${ready.map((j) => `<div class="item"><a class="main-col" href="#/jobs/${encodeURIComponent(j.id)}"><div class="title">${esc(j.title)}</div><div class="meta mono">${esc(j.branch)}${j.pr_number ? ` · PR #${esc(j.pr_number)}` : ""}</div></a>
        <div class="side">${testers.configured ? `<button class="btn small" ${act("deliver", { job: j.id })}>Send to testers</button>` : ""}
        ${j.next ? `<button class="btn small primary" ${act(j.next.action, { job: j.id })}>${esc(j.next.label)}</button>` : ""}</div></div>`).join("")}</div></section>` : "";
  const pipelineCard = `<section class="card mb-16"><div class="card-h"><h2>Pipeline</h2>${d.xcode_cloud ? `<span class="count">Xcode Cloud configured</span>` : ""}</div>
      ${pipeline.available ? `<div class="list">${pipeline.runs.map((r) => `<div class="item"><a class="main-col" href="${esc(r.url || "#")}" target="_blank" rel="noopener"><div class="title">${esc(r.title || r.name)}</div>
        <div class="meta"><span class="mono">${esc(r.branch)}</span> · ${esc(r.name)} · ${esc(when(r.created))}</div></a><div class="side"><span class="pill ${esc(r.tone)}">${esc(r.outcome.replace("_", " "))}</span>${r.can_rerun ? `<button type="button" class="btn small" data-rerun="${r.id}">Re-run failed</button>` : ""}</div></div>`).join("") || `<div class="empty">No pipeline runs yet.</div>`}</div>`
        : `<div class="empty">Pipeline status comes from GitHub Actions. Sign in with the GitHub CLI (<code>gh auth login</code>) and push this repository to GitHub to see it here.</div>`}</section>`;
  const buildsCard = testers.builds.length > 1 ? `<section class="card"><div class="card-h"><h2>Earlier builds</h2><span class="count">${testers.builds.length - 1}</span></div>
      <div class="list">${testers.builds.slice(1).map((b) => `<a class="item" href="#/jobs/${encodeURIComponent(b.job_id)}"><div class="main-col"><div class="title">${esc(b.version || "?")}${b.build ? ` (${esc(b.build)})` : ""} · ${esc(b.title || b.job_id)}</div>
        <div class="meta">${esc(ago(b.delivered))} · ${esc(b.branch)}${b.recipients ? ` · ${esc(b.recipients)}` : ""}</div></div></a>`).join("")}</div></section>` : "";
  return {
    title: "Delivery",
    sub: "What's live, what testers have, and what's on the way.",
    html: `${liveCard}${testersCard}${readyCard}${pipelineCard}${buildsCard}`,
    after: () => {
      view.querySelectorAll("[data-rerun]").forEach((btn) => btn.addEventListener("click", async () => {
        btn.disabled = true;
        try { await api("delivery/rerun", { method: "POST", body: { run_id: Number(btn.dataset.rerun) } }); toast("Re-running the failed jobs"); route(); }
        catch (e) { toast(e.message, true); btn.disabled = false; }
      }));
    },
  };
};

const KPI_STATE = { "no-data": ["No data yet", "working"], "no-target": ["No target", "working"], "on-track": ["On track", "done"], behind: ["Behind", "attention"] };
const DECISION_LABEL = { keep: "Keep", iterate: "Iterate", drop: "Drop" };

function kpiFormBody(k = {}) {
  return `<label class="field"><span>What you're measuring</span><input type="text" name="name" required maxlength="80" value="${esc(k.name || "")}" placeholder="e.g. Seat claim rate"></label>
    <label class="field"><span>Event that measures it</span><input type="text" name="event" required pattern="[a-z][a-z0-9_]{1,59}" value="${esc(k.event || "")}" placeholder="lobby_seat_claimed" autocapitalize="off"><small class="muted">Lowercase letters, numbers and underscores. The builder is told to emit it.</small></label>
    <div class="row gap-10">
      <label class="field flex-1"><span>Better when</span><select name="direction"><option value="up" ${k.direction !== "down" ? "selected" : ""}>It goes up</option><option value="down" ${k.direction === "down" ? "selected" : ""}>It goes down</option></select></label>
      <label class="field flex-1"><span>Target <span class="muted">(optional)</span></span><input type="number" name="target" step="any" value="${k.target ?? ""}"></label>
      <label class="field flex-1"><span>Unit</span><input type="text" name="unit" maxlength="20" value="${esc(k.unit || "")}" placeholder="%"></label>
    </div>`;
}

// A KPI that is behind its target becomes a pre-filled feature job: what is measured, how far off, and the last decision made.
function improveKpiSummary(k, st) {
  const unit = k.unit ? ` ${k.unit}` : "";
  const last = (k.measurements || []).slice(-1)[0];
  return `Improve "${k.name}" (event ${k.event}): it is ${st.latest ? st.latest.value : "unknown"}${unit} and should be ${k.direction === "up" ? "at least" : "at most"} ${k.target}${unit}.`
    + (last?.decision ? ` Last decision: ${last.decision}${last.note ? ` (${last.note})` : ""}.` : "")
    + " Propose the smallest change most likely to move it, and say how we will know it worked.";
}

pages.measure = async () => {
  const data = await api("analytics");
  const { features } = data;
  const connected = data.provider && data.key_set;
  const kpiRow = (f, k) => {
    const st = k.status, [label, tone] = KPI_STATE[st.state];
    const trend = st.trend === "better" ? "▲ better" : st.trend === "worse" ? "▼ worse" : st.trend === "flat" ? "▬ flat" : "";
    return `<div class="item kpi-row"><div class="main-col"><div class="title">${esc(k.name)}</div>
        <div class="meta"><span class="mono">${esc(k.event)}</span> · ${k.target === null ? "no target" : `${k.direction === "up" ? "at least" : "at most"} ${esc(k.target)}${k.unit ? ` ${esc(k.unit)}` : ""}`}</div></div>
      <div class="kpi-latest">${st.latest ? `<strong>${esc(st.latest.value)}${k.unit ? ` ${esc(k.unit)}` : ""}</strong><span class="muted">${esc(ago(st.latest.t))}${trend ? ` · ${trend}` : ""}</span>` : `<span class="muted">—</span>`}</div>
      <span class="pill ${tone}">${esc(label)}</span>
      <div class="side">${st.state === "behind" ? `<a class="btn small primary" href="#/new?type=feature&feature=${encodeURIComponent(f.id)}&summary=${encodeURIComponent(improveKpiSummary(k, st))}">Plan an improvement</a>` : ""}<button class="btn small" data-kpi="measure" data-f="${esc(f.id)}" data-k="${esc(k.id)}">Log result</button>
        ${moreMenu([["Edit", `data-kpi="edit" data-f="${esc(f.id)}" data-k="${esc(k.id)}"`], ["Delete", `data-kpi="delete" data-f="${esc(f.id)}" data-k="${esc(k.id)}"`, "You can undo for 10 seconds", "danger"]])}</div>
      ${k.measurements.length ? `<details class="kpi-log"><summary>Learning log (${k.measurements.length})</summary>${k.measurements.slice().reverse().map((m) => `<div class="kpi-entry"><span class="mono">${esc(m.value)}${k.unit ? ` ${esc(k.unit)}` : ""}</span> <span class="muted">${esc(ago(m.t))}</span>${m.decision ? ` <span class="pill ${m.decision === "drop" ? "failed" : m.decision === "iterate" ? "attention" : "done"}">${esc(DECISION_LABEL[m.decision])}</span>` : ""}${m.note ? ` <span>${esc(m.note)}</span>` : ""}</div>`).join("")}</details>` : ""}
    </div>`;
  };
  const card = (f) => `<section class="card mb-16"><div class="card-h"><h2>${esc(f.name)}</h2><button class="btn small" data-kpi="add" data-f="${esc(f.id)}">Add KPI</button></div>
    <div class="list">${f.kpis.map((k) => kpiRow(f, k)).join("") || `<div class="empty">No KPIs yet. What would tell you "${esc(f.name)}" is working?</div>`}</div></section>`;
  return {
    title: "Measure",
    sub: "Build, measure, learn: say how each feature should be judged, then record what you find and what you'll do next.",
    actions: features.some((f) => f.kpis.length) ? `<button class="btn" id="export-plan">Export tracking plan</button>` : "",
    html: `
      <section class="card mb-16"><div class="card-h"><h2>Analytics</h2>
        <div class="row"><button class="btn small ${connected ? "" : "primary"}" id="analytics-set">${data.provider ? "Change" : "Connect"}</button>
        ${connected ? `<button class="btn small" id="analytics-test">Send test event</button><button class="btn small danger" id="analytics-clear">Disconnect</button>` : ""}</div></div>
        <div class="card-b stack"><strong>${connected ? `${esc(data.provider_name)} (${esc(data.region.toUpperCase())})` : data.provider ? `${esc(data.provider_name)}: key missing` : "Not connected"}</strong>
          <span class="muted">${connected ? "New work for a feature is told to send its KPI events here. The key is stored, never shown." : "Optional. Mixpanel, Amplitude or PostHog. Connect one so new work knows where events go, and verify it with a test event."}</span></div></section>
      ${features.map(card).join("") || `<div class="empty">KPIs belong to features. <a href="#/features">Create a feature</a> first.</div>`}`,
    after: () => {
      const reload = () => route();
      const call = async (path, body, ok) => { try { await api(path, { method: "POST", body }); if (ok) toast(ok); reload(); } catch (e) { toast(e.message, true); } };
      $("#analytics-set").addEventListener("click", async () => {
        const v = await formDialog("Connect analytics", `<label class="field"><span>Provider</span><select name="provider">${data.providers.map((p) => `<option value="${esc(p.id)}" ${p.id === data.provider ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
          <label class="field"><span>Project token or API key</span><input type="password" name="key" autocomplete="off" placeholder="${data.key_set ? "Saved. Leave blank to keep" : ""}"></label>
          <label class="field"><span>Region</span><select name="region"><option value="us" ${data.region !== "eu" ? "selected" : ""}>US</option><option value="eu" ${data.region === "eu" ? "selected" : ""}>EU</option></select></label>`, "Save");
        if (v) {
          try {
            await api("config/analytics", { method: "POST", body: { op: "set", ...v } });
            try { await api("config/analytics", { method: "POST", body: { op: "test" } }); toast("Saved and tested: a test event was sent. Check the provider's live view."); }
            catch (e) { toast(`Saved, but the test event failed: ${e.message}`, true); }
            reload();
          } catch (e) { toast(e.message, true); }
        }
      });
      $("#analytics-test")?.addEventListener("click", () => call("config/analytics", { op: "test" }, "Test event sent. Check the provider's live view."));
      $("#analytics-clear")?.addEventListener("click", () => call("config/analytics", { op: "clear" }, "Disconnected"));
      $("#export-plan")?.addEventListener("click", async () => { try { const r = await api("analytics/plan", { method: "POST", body: {} }); toast(`Wrote ${r.path}. Commit it with your next change.`); } catch (e) { toast(e.message, true); } });
      view.querySelectorAll("[data-kpi]").forEach((btn) => btn.addEventListener("click", async () => {
        const fid = btn.dataset.f, f = features.find((x) => x.id === fid), k = f?.kpis.find((x) => x.id === btn.dataset.k), op = btn.dataset.kpi;
        const path = `features/${encodeURIComponent(fid)}/kpis`;
        if (op === "add") { const v = await formDialog(`New KPI for ${f.name}`, kpiFormBody(), "Add"); if (v) call(path, { op: "add", ...v }, "KPI added"); }
        else if (op === "edit") { const v = await formDialog("Edit KPI", kpiFormBody(k), "Save"); if (v) call(path, { op: "update", kpi: k.id, ...v }, "KPI saved"); }
        else if (op === "delete") {
          try {
            const { undo } = await api(path, { method: "POST", body: { op: "delete", kpi: k.id } });
            toast(`Deleted “${k.name}”`, false, { label: "Undo", run: async () => { await api(path, { method: "POST", body: { op: "restore", kpi: undo.kpi } }); route(); } });
            route();
          } catch (e) { toast(e.message, true); }
        }
        else {
          const v = await formDialog(`Log a result: ${k.name}`, `<label class="field"><span>Result${k.unit ? ` (${esc(k.unit)})` : ""}</span><input type="number" name="value" step="any" required></label>
            <label class="field"><span>What will you do?</span><select name="decision"><option value="">Not decided</option><option value="keep">Keep it as is</option><option value="iterate">Iterate on it</option><option value="drop">Drop it</option></select></label>
            <label class="field"><span>What you learned <span class="muted">(optional)</span></span><textarea name="note" rows="2" maxlength="500"></textarea></label>`, "Save");
          if (v) call(path, { op: "measure", kpi: k.id, ...v }, "Result logged");
        }
      }));
    },
  };
};

pages.checkup = async () => {
  const d = await api("health");
  const action = (i) => {
    if (i.job) return `<a class="btn small primary" href="#/new?type=${encodeURIComponent(i.job.type)}&summary=${encodeURIComponent(i.job.summary)}">${esc(i.label)}</a>`;
    if (i.route) return `<a class="btn small ${i.status === "todo" ? "primary" : ""}" href="${esc(i.route)}">${esc(i.label)}</a>`;
    if (i.hint) return `<div class="setup-hint"><code>${esc(i.hint)}</code><button class="btn small ghost" data-setup-copy="${esc(i.hint)}">Copy</button></div>`;
    return "";
  };
  const icon = { ok: `<span class="setup-icon done" aria-label="in place">✓</span>`, todo: `<span class="setup-icon todo" aria-label="missing"></span>`, warn: `<span class="pill attention" aria-label="needs attention">!</span>` };
  const next = d.items.find((i) => i.id === d.next);
  return {
    title: "Check-up",
    sub: `${esc(d.name)} · ${esc(d.stage)}`,
    html: `
      <div class="tasks-progress-wrap mb-16"><div class="progress-bar-container"><div class="progress-bar-fill" style="width: ${Math.round((d.ok / d.total) * 100)}%"></div></div><span class="progress-text">${d.ok}/${d.total} in place</span></div>
      ${next ? `<section class="card" style="margin-bottom: 16px; border-color: var(--accent);"><div class="card-h"><h2>Next: ${esc(next.title)}</h2></div>
        <div class="card-b stack"><div>${esc(next.detail)}</div><div>${action(next)}</div></div></section>` : `<div class="notice">Everything on the list is in place. Keep an eye on Home for what needs you next.</div>`}
      <section class="card"><div class="card-h"><h2>Everything</h2><span class="muted">Product state. Tools and keys are in the setup checklist.</span></div>
        <div class="list">${d.items.map((i) => `<div class="item">${icon[i.status]}<div class="main-col"><div class="title">${esc(i.title)}</div><div class="meta">${esc(i.detail)}</div></div>
          <div class="side">${i.id === d.next || i.status === "ok" ? "" : action(i)}</div></div>`).join("")}</div></section>
      <section class="card mt-16"><div class="card-h"><h2>Will this computer build it?</h2><button type="button" class="btn small" id="preflight-refresh">Check again</button></div>
        <div class="list" id="preflight-list"><div class="item"><div class="main-col muted">Checking this computer…</div></div></div></section>
      <section class="card mt-16"><div class="card-h"><h2>Setup tools</h2></div>
        <div class="list">${[["check", "Tools and logins", "CLIs, sign-ins and keys"], ["check_config", "Project config", "Validate .orchestrator/project.json"], ["worker_check", "Worker machines", "Remote build machines respond"], ["wizard", "Setup wizard", "Walk through project, tools and delivery setup"]].map(([a, title, hint]) =>
          `<div class="item"><div class="main-col"><div class="title">${title}</div><div class="meta">${hint}</div></div><div class="side"><button type="button" class="btn small" ${act(a)}>Run</button></div></div>`).join("")}</div></section>`,
    after: () => {
      const paint = async (refresh) => {
        const box = $("#preflight-list");
        try {
          const { items } = await api(`preflight${refresh ? "?refresh=1" : ""}`);
          const icon = { ok: `<span class="setup-icon done" aria-label="fine">✓</span>`, warn: `<span class="pill attention">Maybe</span>`, fail: `<span class="pill failed">Blocks</span>` };
          box.innerHTML = items.map((i) => `<div class="item">${icon[i.status]}<div class="main-col"><div class="title">${esc(i.title)}</div><div class="meta">${esc(i.detail)}${i.fix && !i.route ? ` · ${esc(i.fix)}` : ""}</div></div>${i.route && i.status !== "ok" ? `<div class="side"><a class="btn small" href="${esc(i.route)}">${esc(i.fix || "Open")}</a></div>` : ""}</div>`).join("") || `<div class="empty">Nothing to check.</div>`;
        } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
      };
      paint(false);
      $("#preflight-refresh")?.addEventListener("click", () => paint(true));
    },
  };
};

pages.help = async () => ({
  title: "Help",
  sub: "How Orchestrator works, where things are, and what the words mean.",
  html: `
    <section class="card mb-16"><div class="card-h"><h2>How a piece of work flows</h2></div><div class="card-b">
      <ol class="help-steps">
        <li><strong>Describe it.</strong> <a href="#/new">New job</a>: pick the kind of work and answer only what that kind needs. A bug asks how to reproduce it and offers your latest logs; a design takes images or a Figma link; a quick change is one box. Leave out anything you don't care about and the AI picks sensible defaults, then lists what it assumed.</li>
        <li><strong>It gets planned.</strong> You may be asked a question. Anything waiting on you is marked in the job list on <a href="#/">Home</a>, with its next step on the row.</li>
        <li><strong>It gets built.</strong> A worker writes the code and the tests, on its own branch. Pause it any time; Resume picks up at the next task.</li>
        <li><strong>You review it.</strong> The job page shows progress, test cases, changes and a scope check. Ask the AI about it, revise the plan, or run a fix.</li>
        <li><strong>You ship it.</strong> Merge (or Mark complete), then send a build to testers from <a href="#/delivery">Delivery</a>.</li>
        <li><strong>You learn from it.</strong> Give each feature KPIs on <a href="#/measure">Measure</a> and log what you find.</li>
      </ol></div></section>
    <section class="card mb-16"><div class="card-h"><h2>Where things are</h2></div><div class="list">
      ${[["Home", "#/", "What's waiting on you (across projects), then all jobs."], ["Product", "#/product", "The one-page requirements every job reads. Edit it, import a PRD, see its history."], ["Features", "#/features", "What the jobs add up to: list, map, overlap warnings."],
         ["Tests", "#/tests", "Test cases by area, suites, coverage."], ["Delivery", "#/delivery", "What's live, what testers have, what's ready, pipeline."], ["Measure", "#/measure", "KPIs, analytics connection, learning log."],
         ["Check-up", "#/checkup", "What's in place and what's missing for this project."], ["Projects", "#/projects", "Switch, add or start a project."], ["Activity", "#/activity", "Commands and runs, with live output."],
         ["Configuration", "#/config", "Models, keys, machines, alerts, Firebase."]].map(([name, href, what]) => `<a class="item" href="${href}"><div class="main-col"><div class="title">${esc(name)}</div><div class="meta">${esc(what)}</div></div></a>`).join("")}
    </div></section>
    <section class="card mb-16"><div class="card-h"><h2>Words we use</h2></div><div class="card-b"><dl class="help-terms">
      <dt>Job</dt><dd>One piece of work: a bug fix, feature, design, test addition or quick change. It has a plan, a branch and a status.</dd>
      <dt>Run</dt><dd>One command Orchestrator executed, such as a build or a fix attempt. A job can have many runs.</dd>
      <dt>Feature</dt><dd>Something a user would name, like “Lobby seats”. Jobs are grouped under features, and each feature can own parts of the code.</dd>
      <dt>Test case</dt><dd>A planned check with steps and an expected result. It is “covered” when a test in the code carries its id or is assigned to it.</dd>
      <dt>Scope check</dt><dd>Compares what a job changed with what it planned, and flags extras like new dependencies or unplanned files.</dd>
      <dt>KPI</dt><dd>How you will judge a feature: an event that is tracked, with a target. You log results and decide to keep, iterate or drop.</dd>
      <dt>Pause</dt><dd>Stops the worker. The job keeps its work; Resume continues from the next task.</dd>
    </dl></div></section>
    <section class="card"><div class="card-h"><h2>Common questions</h2></div><div class="card-b help-faq">
      <details><summary>A job won't run</summary><p>Jobs need a machine, a model, a signed-in AI provider and a GitHub remote. <a href="#/checkup">Check-up</a> and the Setup button on Home show what is missing; <a href="#/config">Configuration</a> is where you fix it.</p></details>
      <details><summary>How do I get better plans?</summary><p>Fill in the <a href="#/product">product requirements</a>: what you're building, who it's for, the core features, how it should look and feel, and what it should not be. Every plan is written against it. All of it is optional, and “not sure yet” is a fine answer. Draft it from your project, or import a PRD you already have. It keeps itself up to date as jobs finish, and you can see and undo every change.</p></details>
      <details><summary>I changed my mind about a job</summary><p>Use <strong>Revise plan</strong> in the job's More menu to re-plan, or <strong>Discard job</strong> to revert its changes and delete its branch. Discard can't be undone.</p></details>
      <details><summary>I deleted something by mistake</summary><p>Deleting a feature or KPI shows an Undo for 10 seconds. A job marked complete can be restored from Configuration > Archived jobs.</p></details>
      <details><summary>How do I get alerts when I'm away?</summary><p>The sidebar's <strong>Notify me when done</strong> sends browser alerts. For Slack, add a webhook under Configuration > Slack &amp; chat alerts.</p></details>
      <details><summary>Where are the full guides?</summary><p><a href="#/config/documentation">Configuration > Documentation</a> lists the Orchestrator and project guides. The terminal console has everything too: use <strong>Open full console</strong>.</p></details>
    </div></section>`,
});

// ---------------------------------------------------------------- docs hub
// Everything written about the project in one place: the product requirements, a page for every feature and every job (composed
// from the live project, so it can't go stale), and the project's own markdown files. One file can be downloaded with all of it.

function downloadText(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

pages.docs = async (args, query) => {
  const [kind, id] = args || [];
  if (kind === "job" || kind === "feature" || kind === "file") {
    const path = kind === "file" ? `docs/file?path=${encodeURIComponent(query?.get("path") || "")}` : `docs/${kind}/${encodeURIComponent(id || "")}`;
    let doc;
    try { doc = await api(path); } catch (e) {
      return { title: "Docs", html: `<div class="notice">${esc(e.message)} <a href="#/docs">Back to all docs</a></div>` };
    }
    const open = kind === "job" ? `<a class="btn" href="#/jobs/${encodeURIComponent(id)}">Open the job</a>` : kind === "feature" ? `<a class="btn" href="#/features">Open in Features</a>` : "";
    return {
      title: doc.title,
      sub: `<a href="#/docs">← All docs</a>${kind === "file" ? ` · <span class="mono">${esc(doc.path)}</span>` : ""}`,
      actions: `${open}<button class="btn" id="doc-download">Download .md</button>`,
      html: `<section class="card"><div class="card-b"><div class="md">${Markdown.render(doc.markdown)}</div></div></section>`,
      after: () => $("#doc-download").addEventListener("click", () => downloadText(`${(doc.title || "document").replace(/[^A-Za-z0-9._-]+/g, "-")}.md`, doc.markdown)),
    };
  }

  const d = await api("docs");
  const SHOWN = 15;
  const featureRow = (f) => `<a class="item doc-row" href="#/docs/feature/${encodeURIComponent(f.id)}" data-find="${esc(`${f.name} ${f.summary}`.toLowerCase())}"><div class="main-col"><div class="title">${esc(f.name)}</div>
      <div class="meta">${f.summary ? `${esc(f.summary)} · ` : ""}${f.jobs ? `${f.jobs_done} of ${f.jobs} jobs done` : "no jobs yet"}</div></div><div class="side"><span class="pill ${f.status === "complete" ? "done" : "working"}">${esc(f.status === "in-progress" ? "In progress" : f.status === "complete" ? "Complete" : "Planned")}</span></div></a>`;
  const jobRow = (j, n) => `<a class="item doc-row" ${n >= SHOWN ? "hidden" : ""} href="#/docs/job/${encodeURIComponent(j.id)}" data-group="${esc(j.group || "")}" data-find="${esc(`${j.title} ${j.feature_name} ${j.blurb} ${j.kind}`.toLowerCase())}"><div class="main-col"><div class="title">${esc(j.title)}</div>
      <div class="meta">${[j.kind, j.feature_name, ago(j.updated)].filter(Boolean).map(esc).join(" · ")}${j.blurb ? `<br><span class="doc-blurb">${esc(j.blurb)}</span>` : ""}</div></div><div class="side"><span class="pill ${esc(j.group === "done" ? "done" : j.group === "needs_you" ? "attention" : "working")}">${esc(j.label)}</span></div></a>`;
  const groups = [...new Set(d.files.map((f) => f.group))];
  const fileRows = groups.map((g) => `<div class="tc-area-h">${esc(g)}</div>${d.files.filter((f) => f.group === g).map((f) => `<a class="item doc-row" href="#/docs/file?path=${encodeURIComponent(f.path)}" data-find="${esc(`${f.title} ${f.path}`.toLowerCase())}"><div class="main-col"><div class="title">${esc(f.title)}</div><div class="meta mono">${esc(f.path)}</div></div></a>`).join("")}`).join("");
  const pr = d.product;
  return {
    title: "Docs",
    sub: "Everything about this project in one place. Job and feature pages are generated from the live project, so they stay true.",
    actions: `<button class="btn" id="docs-export">Download everything (.md)</button>`,
    html: `
      <label class="field docs-search"><span class="sr-only">Search the docs</span><input type="search" id="docs-q" placeholder="Search features, jobs and files" autocomplete="off" aria-label="Search the docs"></label>
      <section class="card mb-16"><div class="card-h"><h2>Product requirements</h2><a href="#/product">${pr.exists ? "Open" : "Write it"}</a></div>
        <div class="card-b">${pr.exists && pr.pitch ? `<p class="product-pitch">${esc(prdSnippet(pr.pitch, 220))}</p><div class="muted">${pr.written} of ${pr.total} sections written · <span class="mono">${esc(pr.path)}</span></div>`
          : `<div class="muted">Nothing says what this product is yet. <a href="#/product">Write the pitch</a>, import a PRD, or draft one from the project.</div>`}</div></section>
      <section class="card mb-16"><div class="card-h"><h2>Features</h2><span class="count">${d.features.length}</span></div>
        <div class="list" id="docs-features">${d.features.length ? d.features.map(featureRow).join("") : `<div class="empty">No features yet. <a href="#/features">Group your jobs into features</a> and each gets a page here.</div>`}</div></section>
      <section class="card mb-16"><div class="card-h"><h2>Jobs</h2><span class="count">${d.jobs.length}</span>
        <div class="filters"><button type="button" class="btn small on" data-docs-group="">All</button><button type="button" class="btn small" data-docs-group="done">Done</button><button type="button" class="btn small" data-docs-group="open">Not done</button></div></div>
        <div class="list" id="docs-jobs">${d.jobs.length ? d.jobs.map(jobRow).join("") : `<div class="empty">No jobs yet.</div>`}</div>
        ${d.jobs.length > SHOWN ? `<div class="card-b"><button type="button" class="btn small" id="docs-more">Show all ${d.jobs.length}</button></div>` : ""}</section>
      <section class="card"><div class="card-h"><h2>Project files</h2><span class="count">${d.files.length}</span></div>
        <div class="list" id="docs-files">${d.files.length ? fileRows : `<div class="empty">No README or docs/ folder found.</div>`}</div></section>`,
    after: () => {
      const q = $("#docs-q"), rows = () => [...view.querySelectorAll(".doc-row")];
      let group = "", all = false;
      const apply = () => {
        const term = q.value.trim().toLowerCase();
        let shown = 0;
        for (const r of rows()) {
          const inJobs = r.closest("#docs-jobs");
          const okTerm = !term || r.dataset.find.includes(term);
          const g = r.dataset.group;
          const okGroup = !inJobs || !group || (group === "done" ? g === "done" : g !== "done");
          let visible = okTerm && okGroup;
          if (inJobs && visible) { shown++; if (!all && !term && shown > SHOWN) visible = false; }
          r.hidden = !visible;
        }
        $("#docs-more")?.toggleAttribute("hidden", all || !!term);
      };
      q.addEventListener("input", apply);
      $("#docs-more")?.addEventListener("click", () => { all = true; apply(); });
      view.querySelectorAll("[data-docs-group]").forEach((b) => b.addEventListener("click", () => {
        group = b.dataset.docsGroup; all = false;
        view.querySelectorAll("[data-docs-group]").forEach((x) => x.classList.toggle("on", x === b));
        apply();
      }));
      $("#docs-export").addEventListener("click", async (e) => {
        e.target.disabled = true;
        try { const r = await api("docs/export"); downloadText(r.name.replace(/[^A-Za-z0-9._-]+/g, "-"), r.markdown); } catch (err) { toast(err.message, true); }
        e.target.disabled = false;
      });
    },
  };
};

// ---------------------------------------------------------------- product requirements
// One short living document (see orchestrator/prd.py): five sections, edited here, kept true by the AI as jobs finish,
// with a history that can be restored and a switch for automatic updates.

const PRD_SOURCE = { you: "You", auto: "Updated automatically after a job", import: "Imported", ai: "AI help (you accepted it)", draft: "Drafted from your project", revert: "Restored an earlier version",
  migration: "Built from the earlier documents", earlier: "Before changes were tracked" };
const DESIGN_IMAGE = /^designs\/([A-Za-z0-9._-]+\.(?:png|jpe?g|gif|webp))$/i;

function prdSnippet(md, n = 180) {
  const text = String(md || "").replace(/^Built for:.*$/gim, "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[*_`#>-]/g, " ").replace(/\s+/g, " ").trim();
  return text.length > n ? `${text.slice(0, n).replace(/\s+\S*$/, "")}…` : text;
}

// The banner shown on Home and on the Product page after the AI updated the document by itself.
function prdNoticeHtml(n) {
  return `<div class="banner attention prd-notice" role="status"><p><strong>The product requirements were updated${n.job_title ? ` after “${esc(n.job_title)}”` : ""}.</strong> ${esc(n.summary)}</p>
    <div class="row gap-10"><a class="btn small" href="#/product?history=${encodeURIComponent(n.id)}">See what changed</a>
      <button type="button" class="btn small" data-prd-undo="${esc(n.id)}">Undo</button>
      <button type="button" class="btn small ghost" data-prd-dismiss>Dismiss</button></div></div>`;
}

document.addEventListener("click", async (e) => {
  const dismiss = e.target.closest("[data-prd-dismiss]");
  const undo = e.target.closest("[data-prd-undo]");
  if (!dismiss && !undo) return;
  try {
    if (undo) {
      const { history } = await api("product");
      const i = history.findIndex((h) => h.id === undo.dataset.prdUndo);
      if (i === -1 || !history[i + 1]) throw new Error("There's no earlier version to go back to.");
      await api("product/revert", { method: "POST", body: { id: history[i + 1].id } });
      toast("Went back to the version before that update");
    }
    await api("product/dismiss", { method: "POST", body: {} });
    state.product_notice = null;
    route();
  } catch (err) { toast(err.message, true); }
});

// Slow model work runs on the server in the background: start it, then ask how it is getting on. (A tunnel closes any single
// request held open for about 100 seconds, so waiting on one long request made slower models look like failures.)
async function waitForTask(started) {
  const { task } = started;
  const began = Date.now();
  let misses = 0;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    let r;
    try { r = await api(`product/task/${encodeURIComponent(task)}`); misses = 0; }
    catch (e) { if (e.status === 404 || ++misses >= 4) throw e; continue; } // a dropped connection is retried a few times
    if (r.status === "done") return r.result;
    if (r.status === "error") throw new Error(r.error);
    if (Date.now() - began > 7 * 60 * 1000) throw new Error("This is taking much longer than expected. Try again.");
  }
}

function productStripHtml(p) {
  const lead = p.sections.find((x) => x.id === "pitch" && x.filled) || p.sections.find((x) => x.filled);
  return `<section class="card mb-16" id="product-strip"><div class="card-h"><h2>Product</h2><a href="#/product">Open</a></div>
    <div class="card-b stack">
      ${p.notice ? prdNoticeHtml(p.notice) : ""}
      ${lead ? `<p class="product-pitch">${esc(prdSnippet(lead.body))}</p>`
        : `<p>Tell us what you're building, in a few sentences. Every job reads this first, and it stays up to date as you build. All of it is optional.</p>
           <div class="row gap-10">${p.can_draft ? `<a class="btn small primary" href="#/product?draft=1">Draft it from my project</a><a class="btn small" href="#/product">Write it</a>` : `<a class="btn small primary" href="#/product">Write it</a>`}<a class="btn small" href="#/product?import=1">Import PRD</a></div>`}
    </div></section>`;
}

pages.product = async (_, query) => {
  let p = await api("product");
  const open = { history: query?.get("history") || "", imp: query?.get("import") === "1", draft: query?.get("draft") === "1", section: query?.get("section") || "" };
  const sectionCard = (s) => {
    const designs = s.id === "look" ? [...s.body.matchAll(/\]\((designs\/[^)\s]+)\)/g)].map((m) => m[1]).filter((d) => DESIGN_IMAGE.test(d)) : [];
    return `<section class="card mb-16" id="sec-${s.id}"><div class="card-h"><h2>${esc(s.title)}</h2>
        <button type="button" class="btn small" data-prd-edit="${s.id}">${s.filled ? "Edit" : "Write"}</button></div>
      <div class="card-b stack" data-prd-body="${s.id}">
        <div class="muted">${esc(s.hint)}</div>
        ${s.filled ? `<div class="md">${Markdown.render(s.body)}</div>` : `<div class="prd-empty">Nothing here, and that's fine.</div>`}
        ${designs.length ? `<div class="prd-designs">${designs.map((d) => `<figure class="prd-design"><img alt="${esc(d.replace("designs/", ""))}" data-auth-src="product/design/${esc(d.replace("designs/", ""))}"></figure>`).join("")}</div>` : ""}
        ${s.id === "look" ? `<div class="row gap-10"><button type="button" class="btn small" id="prd-add-file">Add a design or sketch</button>
          <button type="button" class="btn small" id="prd-add-link">Add a link</button><button type="button" class="btn small" id="prd-add-app">Pick from a connected app</button>
          <input type="file" id="prd-file" accept="image/*,.pdf,.fig,.html,.htm" multiple hidden></div>` : ""}
      </div></section>`;
  };
  const historyRows = (h) => h.map((v, i) => `<div class="item prd-version" data-version="${esc(v.id)}"><div class="main-col"><div class="title">${esc(PRD_SOURCE[v.source] || v.source)}${i === 0 ? ` <span class="pill done">Current</span>` : ""}</div>
      <div class="meta">${esc(ago(v.at))}${v.summary ? ` · ${esc(v.summary)}` : ""}</div><div class="prd-diff" hidden></div></div>
      <div class="side"><button type="button" class="btn small ghost" data-prd-diff="${esc(v.id)}">See changes</button>${i === 0 ? "" : `<button type="button" class="btn small" data-prd-restore="${esc(v.id)}">Restore</button>`}</div></div>`).join("");
  const render = () => `
      ${p.notice ? prdNoticeHtml(p.notice) : ""}
      <div id="prd-panel"></div>
      ${p.sections.map(sectionCard).join("")}
      <section class="card mb-16"><div class="card-h"><h2>Keeping it up to date</h2></div><div class="card-b stack">
        <label class="check"><input type="checkbox" id="prd-auto" ${p.auto_update ? "checked" : ""}> <span>Update this automatically when jobs finish</span></label>
        <div class="muted">After a feature or design job, the AI checks whether what it learned changes this document. It only edits when something clearly changed, keeps your words, tells you, and every change is in the history below where you can undo it.</div></div></section>
      <section class="card"><div class="card-h"><h2>History</h2><span class="count">${p.history.length}</span></div>
        <div class="list">${p.history.length ? historyRows(p.history) : `<div class="empty">Nothing yet. Every change you or the AI makes will be listed here.</div>`}</div></section>`;
  return {
    title: "Product",
    sub: `<span class="mono">${esc(p.path)}</span> · Every job reads this first. Everything here is optional; edit it any time.`,
    actions: `${p.can_draft ? `<button class="btn" id="prd-draft">Draft it from my project</button>` : ""}<button class="btn" id="prd-import">Import PRD</button>`,
    html: render(),
    after: () => {
      const panel = () => $("#prd-panel");
      const reload = async () => { p = await api("product"); $("#view").innerHTML = render(); wire(); hydrateAuthImages(); };
      const save = async (body) => { p = await api("product", { method: "POST", body }); };

      const wire = () => {
        $("#prd-import")?.addEventListener("click", () => importPanel());
        $("#prd-draft")?.addEventListener("click", () => draftPanel());
        view.querySelectorAll("[data-prd-edit]").forEach((btn) => btn.addEventListener("click", () => {
          const id = btn.dataset.prdEdit, s = p.sections.find((x) => x.id === id), box = view.querySelector(`[data-prd-body="${id}"]`);
          btn.hidden = true;
          box.innerHTML = `<div class="muted">${esc(s.hint)}</div><label class="field"><span class="sr-only">${esc(s.title)}</span><textarea rows="${id === "pitch" ? 5 : 8}" id="prd-text-${id}" spellcheck="true" aria-label="Edit ${esc(s.title)}">${esc(s.body)}</textarea></label>
            <div class="row gap-10"><button type="button" class="btn primary" id="prd-save-${id}">Save</button><button type="button" class="btn ghost" id="prd-cancel-${id}">Cancel</button></div>`;
          $(`#prd-text-${id}`).focus();
          $(`#prd-cancel-${id}`).addEventListener("click", reload);
          $(`#prd-save-${id}`).addEventListener("click", async () => { try { await save({ section: id, body: $(`#prd-text-${id}`).value }); toast("Saved"); await reload(); } catch (e) { toast(e.message, true); } });
        }));
        $("#prd-auto")?.addEventListener("change", async (e) => { try { p = await api("product/settings", { method: "POST", body: { auto_update: e.target.checked } }); toast(p.auto_update ? "It will keep itself up to date" : "Automatic updates are off"); } catch (err) { e.target.checked = !e.target.checked; toast(err.message, true); } });
        view.querySelectorAll("[data-prd-diff]").forEach((btn) => btn.addEventListener("click", async () => {
          const box = btn.closest(".prd-version").querySelector(".prd-diff");
          if (!box.hidden) { box.hidden = true; btn.textContent = "See changes"; return; }
          if (!box.dataset.loaded) {
            try {
              const v = await api(`product/history/${encodeURIComponent(btn.dataset.prdDiff)}`);
              box.innerHTML = v.diff.length ? `<pre class="diff" aria-label="Changes">${v.diff.map((l) => `<span class="diff-line ${l.startsWith("@@") ? "hunk" : l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : ""}">${esc(l)}</span>`).join("")}</pre>` : `<div class="muted">No text changed.</div>`;
              box.dataset.loaded = "1";
            } catch (e) { return toast(e.message, true); }
          }
          box.hidden = false; btn.textContent = "Hide changes";
        }));
        view.querySelectorAll("[data-prd-restore]").forEach((btn) => btn.addEventListener("click", async () => {
          const ok = await formDialog("Restore this version?", `<p>The document goes back to how it was then. What it says now stays in the history, so you can come back.</p>`, "Restore");
          if (!ok) return;
          try { await api("product/revert", { method: "POST", body: { id: btn.dataset.prdRestore } }); toast("Restored"); await reload(); } catch (e) { toast(e.message, true); }
        }));
        const file = $("#prd-file");
        $("#prd-add-file")?.addEventListener("click", () => file.click());
        file?.addEventListener("change", async () => {
          for (const f of file.files) {
            if (f.size > NJ_UPLOAD_LIMIT) { toast(`${f.name} is over 25 MB`, true); continue; }
            try { p = await uploadFile(f, "product/design"); toast(`Added ${f.name}`); } catch (e) { toast(e.message, true); }
          }
          file.value = ""; await reload();
        });
        $("#prd-add-link")?.addEventListener("click", async () => {
          const v = await formDialog("Add a link", `<label class="field"><span>What is it?</span><input type="text" name="label" maxlength="120" placeholder="e.g. Figma: onboarding flow"></label>
            <label class="field"><span>Link</span><input type="url" name="url" required placeholder="https://"></label>`, "Add");
          if (!v) return;
          try { await api("product/reference", { method: "POST", body: { label: v.label, url: v.url } }); await reload(); } catch (e) { toast(e.message, true); }
        });
        $("#prd-add-app")?.addEventListener("click", async () => {
          const picker = await linkPickerHtml({ prefer: ["figma"], label: "Pick a design or item" });
          const dlg = formDialog("Pick from a connected app", picker.html + `<small class="hint-text">A link is added under Look and feel, with the image when the app can provide one.</small>`, "Add");
          picker.wire();
          const v = await dlg;
          const links = v ? JSON.parse(v.links || "[]") : [];
          if (!links.length) return;
          try { await api("product/reference", { method: "POST", body: { links } }); await reload(); } catch (e) { toast(e.message, true); }
        });
        if (open.section) { view.querySelector(`#sec-${CSS.escape(open.section)}`)?.scrollIntoView({ block: "start" }); open.section = ""; }
        if (open.history) {
          const row = view.querySelector(`[data-version="${CSS.escape(open.history)}"]`);
          row?.querySelector("[data-prd-diff]")?.click();
          row?.scrollIntoView({ block: "center" });
          open.history = "";
        }
      };

      // Shared by Import and Draft: turn a proposed document into something you can read, then accept or drop.
      const proposalView = (proposal, { source, label, onDone }) => {
        panel().innerHTML = `<section class="card mb-16"><div class="card-h"><h2>${esc(label)}</h2></div><div class="card-b stack">
          ${proposal.summary ? `<div>${esc(proposal.summary)}</div>` : ""}
          ${proposal.diff.length ? `<pre class="diff" aria-label="Changes">${proposal.diff.map((l) => `<span class="diff-line ${l.startsWith("@@") ? "hunk" : l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : ""}">${esc(l)}</span>`).join("")}</pre>` : `<div class="muted">This would not change anything.</div>`}
          <details class="fold"><summary>Read the whole proposed document</summary><div class="md">${Markdown.render(proposal.markdown)}</div></details>
          <div class="row gap-10"><button type="button" class="btn primary" id="prd-accept" ${proposal.diff.length ? "" : "disabled"}>Accept and save</button><button type="button" class="btn ghost" id="prd-discard">Discard</button></div>
          <div class="muted">Nothing is saved until you accept. You can undo it afterwards from the history.</div></div></section>`;
        $("#prd-accept").addEventListener("click", async () => {
          try { await save({ text: proposal.markdown, source, summary: proposal.summary }); toast("Saved"); await reload(); } catch (e) { toast(e.message, true); }
        });
        $("#prd-discard").addEventListener("click", () => { panel().innerHTML = ""; onDone?.(); });
      };
      const busy = (text) => {
        const started = Date.now();
        panel().innerHTML = `<div class="prd-busy" role="status" aria-live="polite"><div class="spinner" aria-hidden="true"></div>
          <div><strong>${esc(text)}</strong><div class="prd-busy-sub">This can take up to a minute. <span data-elapsed>0s</span></div></div></div>`;
        const el = panel().querySelector("[data-elapsed]");
        const timer = setInterval(() => { if (!el.isConnected) return clearInterval(timer); el.textContent = `${Math.floor((Date.now() - started) / 1000)}s`; }, 1000);
      };

      const readPrd = async ({ file, text }) => {
        if (file && file.size > NJ_UPLOAD_LIMIT) { toast("That file is over 25 MB.", true); return importPanel(); }
        if (file && !/\.(md|markdown|txt|docx|pdf)$/i.test(file.name)) { toast("Use a markdown, text, Word or PDF file, or paste the text.", true); return importPanel(); }
        busy(file ? `Reading ${file.name}…` : "Reading it…");
        try {
          const started = file ? await uploadFile(file, "product/import") : await api("product/import", { method: "POST", body: { text } });
          proposalView(await waitForTask(started), { source: "import", label: "Your PRD, in this format", onDone: () => {} });
        } catch (e) { toast(e.message, true); importPanel(); }
      };

      const importPanel = () => {
        panel().innerHTML = `<section class="card mb-16"><div class="card-h"><h2>Import PRD</h2></div><div class="card-b stack">
          <div class="muted">Bring a PRD you already have. It is rearranged into the five sections below, keeping your words, and you see the result before anything is saved.</div>
          <div class="prd-drop" id="prd-drop" tabindex="0" role="button" aria-label="Choose a PRD file, or drop one here">
            <strong><span class="drop-hint">Drag a file here, or </span>click to choose one</strong><span class="muted">Markdown, text, Word or PDF</span>
            <input type="file" id="prd-import-input" accept=".md,.markdown,.txt,.docx,.pdf" hidden></div>
          <label class="field"><span>Or paste it</span><textarea id="prd-import-text" rows="6" aria-label="Paste your PRD"></textarea></label>
          <div class="row gap-10"><button type="button" class="btn primary" id="prd-import-go">Read it</button><button type="button" class="btn ghost" id="prd-import-cancel">Cancel</button></div></div></section>`;
        const input = $("#prd-import-input"), zone = $("#prd-drop");
        zone.addEventListener("click", () => input.click());
        zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
        input.addEventListener("change", () => { if (input.files[0]) readPrd({ file: input.files[0] }); });
        ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("over"); }));
        ["dragleave", "dragend"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("over")));
        zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); const f = e.dataTransfer?.files?.[0]; if (f) readPrd({ file: f }); });
        $("#prd-import-cancel").addEventListener("click", () => { panel().innerHTML = ""; });
        $("#prd-import-go").addEventListener("click", () => {
          const text = $("#prd-import-text").value.trim();
          if (!text) return toast("Paste some text, or choose a file.", true);
          readPrd({ text });
        });
      };

      // An existing project: read what is there (README, notes, manifests, layout, recent commits) and propose a first version.
      const draftPanel = async () => {
        busy("Reading your project and drafting…");
        try { proposalView(await waitForTask(await api("product/draft", { method: "POST", body: {} })), { source: "draft", label: "Drafted from your project", onDone: () => {} }); }
        catch (e) { toast(e.message, true); panel().innerHTML = ""; }
      };

      wire();
      hydrateAuthImages();
      if (open.imp) importPanel();
      else if (open.draft && p.can_draft) draftPanel();
      else if (!p.sections.some((x) => x.filled) && !p.history.length) {
        const existing = p.can_draft; // there is already a project to read
        panel().innerHTML = `<section class="card mb-16"><div class="card-b stack"><strong>Start here</strong>
          <div>${existing ? "This project already exists, so we can read it and draft a first version for you to correct. Or say what you have in mind in the Pitch below, as if explaining it to a friend." : "Say what you have in mind in the Pitch below, as if you were explaining it to a friend."} Or import a PRD you already have. Everything is optional, and not sure is fine: it fills in as you build.</div>
          <div class="row gap-10">${existing ? `<button type="button" class="btn small primary" id="prd-start-draft">Draft it from my project</button>` : ""}<button type="button" class="btn small ${existing ? "" : "primary"}" id="prd-start-import">Import PRD</button></div></div></section>`;
        $("#prd-start-draft")?.addEventListener("click", () => draftPanel());
        $("#prd-start-import").addEventListener("click", () => importPanel());
      }
    },
  };
};

// Images the backend serves behind the sign-in can't be loaded by a plain <img src>; fetch them with the token instead.
async function hydrateAuthImages(root = document) {
  const backend = getBackendUrl(), token = getToken();
  for (const img of root.querySelectorAll("img[data-auth-src]")) {
    if (img.dataset.loaded) continue;
    img.dataset.loaded = "1";
    try {
      const res = await fetch(`${backend ?? ""}/api/${img.dataset.authSrc}`, { credentials: backend ? "omit" : "same-origin", headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (res.ok) img.src = URL.createObjectURL(await res.blob());
    } catch { /* the alt text stays */ }
  }
}

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

const TC_STATUS = { unassigned: ["No test", "failed", 0], planned: ["Test planned", "attention", 1], covered: ["Covered", "done", 2], manual: ["Manual", "working", 3] };

function testCaseSummaryHtml(summary) {
  const kinds = Object.entries(summary.by_type || {}).filter(([, n]) => n).map(([t, n]) => `${n} ${t}`).join(" · ");
  const pct = summary.covered_pct;
  return `<div class="stack">
    ${pct === null ? "" : `<div class="tasks-progress-wrap"><div class="progress-bar-container"><div class="progress-bar-fill" style="width: ${pct}%"></div></div>
      <span class="progress-text">${summary.covered}/${summary.automated} automated covered</span></div>`}
    <div class="muted">${esc(kinds)}${summary.manual ? ` (${summary.manual} checked by hand)` : ""}</div></div>`;
}

function testCaseRowsHtml(cases) {
  const sorted = [...cases].sort((a, b) => TC_STATUS[a.status][2] - TC_STATUS[b.status][2] || a.id.localeCompare(b.id));
  return sorted.map((c) => {
    const [label, tone] = TC_STATUS[c.status];
    const where = c.found_in.length ? c.found_in : c.assigned;
    return `<details class="item tc-row"><summary><span class="pill ${tone}">${esc(label)}</span>
      <span class="mono tc-id">${esc(c.id)}</span><span class="tc-title">${esc(c.title)}</span><span class="muted tc-type">${esc(c.type)}${c.due ? "" : " · later task"}</span></summary>
      <div class="tc-body stack">
        ${c.preconditions.length ? `<div><strong>Given</strong><ul>${c.preconditions.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
        ${c.steps.length ? `<div><strong>Steps</strong><ol>${c.steps.map((t) => `<li>${esc(t)}</li>`).join("")}</ol></div>` : ""}
        <div><strong>Expected</strong> ${esc(c.expected)}</div>
        ${c.covers.length ? `<div class="muted">Covers: ${c.covers.map(esc).join("; ")}</div>` : ""}
        ${where.length ? `<div class="muted mono">${c.status === "covered" ? "Found in" : "Assigned"}: ${where.map(esc).join(", ")}</div>`
          : c.status === "manual" ? "" : `<div class="muted">No test is assigned yet. The builder writes it, and it's checked after every build.</div>`}
      </div></details>`;
  }).join("");
}

pages.tests = async (_, query) => {
  view.innerHTML = `<div class="empty">Finding tests…</div>`;
  const [data, caseView] = await Promise.all([api(`tests${query.get("refresh") ? "?refresh=1" : ""}`), api("test-cases").catch(() => ({ cases: [], summary: null }))]);
  const caseFilter = TC_STATUS[query.get("cases")] ? query.get("cases") : "all";
  const shownCases = caseView.cases.filter((c) => caseFilter === "all" || c.status === caseFilter);
  const byArea = new Map();
  for (const c of shownCases) byArea.set(c.area, [...(byArea.get(c.area) || []), c]);
  const cov = data.coverage;
  const filter = (query.get("q") || "").toLowerCase();
  // Discovery also matches source files with no tests in them; those aren't runnable suites.
  const withTests = data.suites.filter((s) => s.tests > 0);
  const suites = withTests.filter((s) => !filter || s.name.toLowerCase().includes(filter));
  const total = withTests.reduce((n, s) => n + s.tests, 0);
  return {
    title: "Tests",
    sub: `${withTests.length} suites · ${total} tests`,
    actions: `<button class="btn primary" ${act("test")}>Run all tests</button>${moreMenu([["Build", act("build"), "Compile without running tests"], ["Measure coverage", act("coverage"), "Full test run with coverage; takes a while"], ["Refresh list", `data-href="#/tests?refresh=1"`], ["Expand coverage (AI job)", `data-href="#/new?type=coverage"`]])}`,
    html: `
      ${data.error ? `<div class="notice bad">${esc(data.error)}</div>` : ""}
      ${caseView.cases.length ? `<section class="card mb-16"><div class="card-h"><h2>Test cases</h2>
        <div class="filters">${[["all", "All"], ...Object.entries(TC_STATUS).map(([k, v]) => [k, v[0]])].map(([k, label]) =>
          `<a class="btn small ${k === caseFilter ? "on" : ""}" href="#/tests?cases=${k}${query.get("q") ? `&q=${encodeURIComponent(query.get("q"))}` : ""}">${esc(label)}${k === "all" ? ` (${caseView.cases.length})` : ` (${caseView.summary[k]})`}</a>`).join("")}</div></div>
        <div class="card-b">${testCaseSummaryHtml(caseView.summary)}</div>
        ${[...byArea].map(([area, rows]) => `<div class="tc-area"><div class="tc-area-h">${esc(area)} <span class="count">${rows.length}</span></div><div class="list">${testCaseRowsHtml(rows)}</div></div>`).join("") || `<div class="empty">No cases with that status.</div>`}</section>` : ""}
      ${data.frameworks && (!state.project.languages?.length || state.project.languages.some((l) => l.name === "Swift")) ? `
      <section class="card"><div class="card-h"><h2>Test Frameworks &amp; Canary Scaffolding</h2>
        <div class="row">
          ${!data.frameworks.canary_suite?.installed ? `<button class="btn small primary" ${act("scaffold_canary")}>Scaffold Canary Suite</button>` : ""}
          <button class="btn small" ${act("visual_check")}>Simulator Visual Check</button>
        </div>
      </div>
      <div class="card-b" style="display:flex; flex-wrap:wrap; gap:12px;">
        ${Object.values(data.frameworks).map((f) => `
          <div style="border:1px solid var(--border); border-radius:8px; padding:12px 14px; flex:1 1 200px; background:var(--bg-subtle, rgba(255,255,255,0.02));">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;">
              <strong>${esc(f.name)}</strong>
              <span class="badge ${f.installed ? "good" : "muted-badge"}">${f.installed ? "Installed" : "Available"}</span>
            </div>
            <div style="font-size:12px; color:var(--muted);">${esc(f.desc)}</div>
          </div>
        `).join("")}
      </div></section>` : ""}
      <section class="card"><div class="card-h"><h2>Coverage</h2>${cov ? `<span class="count">${esc(ago(Date.parse(cov.timestamp) / 1000))}</span>` : ""}</div>
        <div class="card-b">${cov ? `<div class="row"><span class="big-number">${esc(cov.overall_coverage_pct)}%</span>
          ${cov.estimated ? pill("attention", "Estimate — measuring failed") : ""}<span class="muted">${esc(cov.total_tests)} tests in ${esc(cov.total_suites)} suites</span></div>`
          : `<p class="muted">Not measured yet. <button class="btn small" ${act("coverage")}>Measure coverage</button></p>`}</div></section>
      ${data.plans.length ? `<section class="card"><div class="card-h"><h2>Test plans</h2></div><div class="list">${data.plans.map((p) => `
        <div class="item"><div class="main-col"><div class="title">${esc(p)}</div></div><div class="side"><button class="btn small" ${act("test_plan", { name: p })}>Run</button></div></div>`).join("")}</div></section>` : ""}
      <section class="card"><div class="card-h"><h2>Suites</h2>
        <form id="suite-filter" class="row"><input type="search" name="q" value="${esc(query.get("q") || "")}" placeholder="Filter" aria-label="Filter suites"></form></div>
        <div class="list">${suites.slice(0, 300).map((s) => `
          <div class="item"><div class="main-col"><div class="title">${esc(s.name)}</div><div class="meta">${esc(s.tests)} tests · ${esc(s.path || "")}</div></div>
          <div class="side"><button class="btn small" ${act("test_suite", { name: s.name })}>Run</button></div></div>`).join("") || `<div class="empty">No suites${filter ? " match" : ""}.</div>`}</div></section>`,
    after: () => $("#suite-filter").addEventListener("submit", (e) => {
      e.preventDefault();
      location.hash = `#/tests?q=${encodeURIComponent(new FormData(e.target).get("q"))}`;
    }),
  };
};

pages.git = async () => {
  const g = await api("git");
  const sync = g.upstream == null ? "No upstream yet — Push sets it."
    : `${g.ahead} to push · ${g.behind} to pull (vs ${g.upstream})`;
  return {
    title: "Git",
    sub: `<span class="status-line"><span class="mono">${esc(g.branch || "detached")}</span><span class="sep">·</span><span>${esc(sync)}</span></span>`,
    actions: `<button class="btn" ${act("git_pull")}>Pull</button><button class="btn primary" ${act("git_push")}>Push</button>
      ${moreMenu([["New branch…", act("git_new_branch")], ...(g.web_url ? [["Open repo on GitHub", `data-open="${esc(g.web_url)}"`]] : [])])}`,
    html: `
      <section class="card"><div class="card-b stack">
        <div><div class="muted">Last commit</div><div>${esc(g.last_commit || "—")}</div></div>
        <div>
          <div class="muted">Switch branch</div>
          <select id="git-branch-select" name="branch" class="mono" aria-label="Switch branch" style="margin-top: 4px; max-width: 320px;">
            ${g.branches.map((b) => `<option value="${esc(b)}" ${b === g.branch ? "selected" : ""}>${esc(b)}</option>`).join("")}
          </select>
        </div>
      </div></section>
      <section class="card"><div class="card-h"><h2>Uncommitted changes</h2><span class="count">${g.changes_total}</span></div>
        <div class="list">${g.changes.map((c) => `<div class="item"><span class="pill">${esc(c.status)}</span><span class="mono">${esc(c.path)}</span></div>`).join("") || `<div class="empty">Working tree clean.</div>`}</div>
        ${g.changes_total > g.changes.length ? `<div class="empty">…and ${g.changes_total - g.changes.length} more.</div>` : ""}</section>`,
    after: () => {
      $("#git-branch-select")?.addEventListener("change", (e) => {
        const branch = e.target.value;
        if (branch && branch !== g.branch) {
          switchBranch(branch);
        } else {
          e.target.value = g.branch || "";
        }
      });
    },
  };
};

// ---------------------------------------------------------------- configuration

async function runConfigMutation(button, request, successMessage) {
  if (!button || button.disabled) return false;
  const section = current?.args?.[0];
  button.disabled = true;
  try {
    await api(`config/${request.part}`, {method: "POST", body: request.body});
    if (successMessage) toast(successMessage);
    if (ConfigurationPages.routeMatches(current, section)) await route();
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

function showConfigMutationDialog(title, bodyHtml, okLabel, buildRequest, successMessage, afterSave = null) {
  const dlg = $("#dialog");
  const form = $("#dialog-form");
  const ok = $("#dialog-ok");
  $("#dialog-cancel").hidden = false;
  $("#dialog-title").textContent = title;
  $("#dialog-body").innerHTML = bodyHtml;
  ok.textContent = okLabel;
  dlg.returnValue = "";
  dlg.showModal();
  $("#dialog-body input, #dialog-body select")?.focus();

  const onSubmit = async (event) => {
    if (event.submitter !== ok) return;
    event.preventDefault();
    const request = buildRequest(Object.fromEntries(new FormData(form).entries()));
    const saved = await runConfigMutation(ok, request, successMessage);
    if (saved && dlg.open) dlg.close("ok");
    if (saved && afterSave) await afterSave();
  };
  form.addEventListener("submit", onSubmit);
  dlg.addEventListener("close", () => form.removeEventListener("submit", onSubmit), {once: true});
}

pages.config = async (args = []) => {
  const section = args[0];
  let config;
  try {
    config = await api("config");
  } catch (error) {
    if (error.status !== 404) throw error;
    return {
      title: "Configuration",
      sub: "",
      html: `<div class="notice">Configuration isn't available yet. Update Orchestrator to use these settings.</div>`,
    };
  }
  if (!ConfigurationPages.routeMatches(current, section)) {
    return {title: "Configuration", sub: "", html: ""};
  }
  const result = ConfigurationPages.render(section, config);
  if (section === "base-branch") {
    return {
      ...result,
      after: () => {
        const form = $("#config-base-branch-form");
        const onSubmit = async (event) => {
          event.preventDefault();
          const button = event.submitter || form.querySelector('button[type="submit"]');
          const branch = new FormData(form).get("branch");
          await runConfigMutation(button, {part: "base-branch", body: {branch}}, "Base branch saved");
        };
        form?.addEventListener("submit", onSubmit);
        cleanup.push(() => form?.removeEventListener("submit", onSubmit));
      },
    };
  }
  if (section === "archived-jobs") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest('[data-config-action="archive-restore"]');
          if (!button || button.disabled) return;
          await runConfigMutation(
            button,
            {part: "archived-restore", body: {id: button.dataset.id}},
            "Job restored",
          );
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "email") {
    const email = config.email || {};
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest("[data-config-action]");
          if (!button || button.disabled) return;
          const action = button.dataset.configAction;
          if (action === "email-add") {
            showConfigMutationDialog(
              "Add recipient",
              `<label class="field"><span>Email address</span><input type="email" name="email" required autocapitalize="off"></label>`,
              "Add",
              (values) => ({part: "email", body: {op: "add", email: values.email}}),
              "Recipient added",
            );
          } else if (action === "email-remove") {
            await runConfigMutation(
              button,
              {part: "email", body: {op: "remove", email: button.dataset.email}},
              "Recipient removed",
            );
          } else if (action === "email-sender") {
            showConfigMutationDialog(
              "Configure email sender",
              `<label class="field"><span>Provider</span><select name="provider">${(email.providers || []).map((provider) => `<option value="${esc(provider)}"${provider === email.provider ? " selected" : ""}>${esc(provider === "gmail" ? "Gmail" : "Resend")}</option>`).join("")}</select></label>
               <small class="hint-text">Leave a secret field blank to keep its saved value.</small>
               <label class="field"><span>Gmail address</span><input type="email" name="smtp_email" value="${esc(email.smtp_email || "")}"></label>
               <label class="field"><span>Gmail app password</span><input type="password" name="smtp_password" autocomplete="off" placeholder="${email.smtp_password_set ? "Saved — leave blank to keep" : "Not set"}"></label>
               <label class="field"><span>Resend API key</span><input type="password" name="resend_api_key" autocomplete="off" placeholder="${email.resend_api_key_set ? "Saved — leave blank to keep" : "Not set"}"></label>
               <label class="field"><span>Resend from email</span><input type="email" name="resend_from_email" value="${esc(email.resend_from_email || "")}"></label>
               <label class="field"><span>Display name</span><input type="text" name="resend_display_name" value="${esc(email.resend_display_name || "")}"></label>`,
              "Save",
              (values) => ({part: "email", body: {op: "provider", ...values}}),
              "Email sender saved",
            );
          } else if (action === "email-test") {
            await runAction("test_email");
          }
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "chat") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest("[data-config-action]");
          if (!button || button.disabled) return;
          const action = button.dataset.configAction;
          if (action === "chat-set") {
            showConfigMutationDialog(
              "Slack webhook",
              `<label class="field"><span>Webhook URL</span><input type="url" name="url" required autocomplete="off" placeholder="https://hooks.slack.com/services/…"></label>`,
              "Save",
              (values) => ({part: "webhook", body: {op: "set", url: values.url}}),
              "Webhook saved. Sending a test message…",
              async () => {
                try { await api("config/webhook", {method: "POST", body: {op: "test"}}); toast("Webhook saved and tested: check your channel for the message."); }
                catch (error) { toast(`Saved, but the test message failed: ${error.message}`, true); }
              },
            );
          } else if (action === "chat-clear") {
            await runConfigMutation(button, {part: "webhook", body: {op: "clear"}}, "Webhook removed");
          } else if (action === "chat-test") {
            await runConfigMutation(button, {part: "webhook", body: {op: "test"}}, "Test message sent");
          }
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "documentation") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest('[data-config-action="document-read"]');
          if (!button || button.disabled) return;
          button.disabled = true;
          try {
            const documentData = await api(`config/doc?id=${encodeURIComponent(button.dataset.id)}`);
            const closed = formDialog(documentData.name, `<pre class="doc-text">${esc(documentData.text)}</pre>`, "Close");
            $("#dialog-cancel").hidden = true;
            await closed;
          } catch (error) {
            toast(error.message, true);
          } finally {
            if (button.isConnected) button.disabled = false;
          }
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "models") {
    return {
      ...result,
      after: () => {
        const form = $("#config-models-form");
        const onSubmit = async (event) => {
          event.preventDefault();
          const button = event.submitter || form.querySelector('button[type="submit"]');
          const fd = new FormData(form);
          const modelsObj = {
            architect: fd.get("architect"),
            planner: fd.get("planner"),
            builder: fd.get("builder"),
            reviewer: fd.get("reviewer"),
          };
          await runConfigMutation(button, {part: "models", body: {models: modelsObj}}, "Model team saved");
        };
        form?.addEventListener("submit", onSubmit);
        cleanup.push(() => form?.removeEventListener("submit", onSubmit));
      },
    };
  }
  if (section === "ai-instructions") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest("[data-config-action]");
          if (!button || button.disabled) return;
          const {configAction, id, name, content} = button.dataset;
          if (configAction === "prompt-edit") {
            showConfigMutationDialog(
              `Customize ${name} Instructions`,
              `<label class="field"><span>Prompt Instructions (.md)</span>
               <textarea name="content" style="min-height:220px; font-family:var(--font-mono); font-size:12px;" required>${esc(content || "")}</textarea></label>`,
              "Save Instructions",
              (values) => ({part: "role-prompts", body: {id, content: values.content}}),
              "Instructions saved"
            );
          } else if (configAction === "prompt-revert") {
            if (confirm(`Revert ${name} instructions to system default?`)) {
              await runConfigMutation(button, {part: "role-prompts", body: {id, revert: true}}, "Reverted to default");
            }
          }
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "fleet") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest("[data-config-action]");
          if (!button || button.disabled) return;
          const {configAction, name} = button.dataset;
          if (configAction === "fleet-add") {
            showConfigMutationDialog(
              "Add Worker Machine",
              `<label class="field"><span>Machine Name</span><input type="text" name="name" required placeholder="m3-max-studio"></label>
               <label class="field"><span>Execution Mode</span><select name="mode"><option value="remote">Remote SSH Machine</option><option value="local">Local Machine</option></select></label>
               <label class="field"><span>SSH Target</span><input type="text" name="ssh_target" placeholder="user@192.168.1.100"></label>
               <label class="field"><span>Remote Repository Path</span><input type="text" name="repo_path" placeholder="/Users/user/Developer/project"></label>`,
              "Add Machine",
              (values) => ({part: "fleet", body: {op: "add", ...values}}),
              "Machine added"
            );
          } else if (configAction === "fleet-check") {
            await runAction("worker_check");
          } else if (configAction === "fleet-sync") {
            await runAction("sync_fleet");
          } else if (configAction === "fleet-llm") {
            await runAction("fleet_llm_check");
          } else if (configAction === "machine-install") {
            await runAction("worker_install", {machine: name});
          } else if (configAction === "machine-toggle") {
            await runConfigMutation(button, {part: "fleet", body: {op: "toggle", name}}, "Status updated");
          } else if (configAction === "machine-remove") {
            if (confirm(`Remove machine '${name}' from fleet?`)) {
              await runConfigMutation(button, {part: "fleet", body: {op: "remove", name}}, "Machine removed");
            }
          }
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "firebase") {
    return {
      ...result,
      after: () => {
        const form = $("#config-firebase-form");
        const onSubmit = async (event) => {
          event.preventDefault();
          const button = event.submitter || form.querySelector('button[type="submit"]');
          const fd = new FormData(form);
          const body = {
            firebase_app_id: fd.get("firebase_app_id"),
            firebase_tester_groups: fd.get("firebase_tester_groups"),
            firebase_service_account_path: fd.get("firebase_service_account_path"),
          };
          await runConfigMutation(button, {part: "firebase", body}, "Firebase settings saved");
        };
        form?.addEventListener("submit", onSubmit);
        cleanup.push(() => form?.removeEventListener("submit", onSubmit));

        const onClick = async (event) => {
          const button = event.target.closest('[data-config-action="distribute-now"]');
          if (button) await runAction("distribute");
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "updates") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest("[data-config-action]");
          if (!button) return;
          if (button.dataset.configAction === "update-local") await runAction("update_local");
          else if (button.dataset.configAction === "update-fleet") await runAction("update_fleet");
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "audit") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest('[data-config-action="run-audit"]');
          if (button) await runAction("check");
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "self-tests") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest('[data-config-action="run-self-tests"]');
          if (button) await runAction("test");
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section === "setup-wizard") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest('[data-config-action="launch-wizard"]');
          if (button) await runAction("wizard");
        };
        view.addEventListener("click", onClick);
        cleanup.push(() => view.removeEventListener("click", onClick));
      },
    };
  }
  if (section !== "api-keys") return result;

  return {
    ...result,
    after: () => {
      const onClick = async (event) => {
        const button = event.target.closest("[data-config-action]");
        if (!button || button.disabled) return;
        const {configAction, id, label} = button.dataset;
        if (configAction === "key-set") {
          const isOllama = id === "ollama_api_key";
          showConfigMutationDialog(
            `${label} key`,
            `<label class="field"><span>API key</span><input type="password" name="value" required autocomplete="off"></label>
             ${isOllama ? `<label class="field"><span>Host URL (optional)</span><input type="url" name="host" value="${esc(config.ollama_host || "")}" placeholder="https://my-ollama:11434"></label>` : ""}`,
            "Save",
            (values) => ({part: "keys", body: {id, ...values}}),
            "Key saved",
          );
        } else if (configAction === "key-clear") {
          const confirmed = await formDialog(
            `Clear ${label} key?`,
            `<p>This removes the saved key from this project's settings.</p>`,
            "Clear",
          );
          if (confirmed) await runConfigMutation(button, {part: "keys", body: {id, clear: true}}, "Key cleared");
        }
      };
      view.addEventListener("click", onClick);
      cleanup.push(() => view.removeEventListener("click", onClick));
    },
  };
};

// ---------------------------------------------------------------- connections (Jira, Trello, Sentry, Figma)

const PROVIDER_LABEL = { jira: "Jira", trello: "Trello", sentry: "Sentry", figma: "Figma" };
const PROVIDER_HINT = {
  jira: "Search tickets, or paste a key like ABC-123 or a link",
  trello: "Search cards, or paste a card link",
  sentry: "Search issues, or paste an issue link",
  figma: "Paste a link to a file or frame (right-click a frame → Copy link)",
};

pages.connections = async () => {
  let list;
  try { list = (await api("integrations")).integrations; } catch (err) {
    if (err.status !== 404) throw err;
    return { title: "Connections", html: `<div class="notice">Connections aren't available yet. They're part of an update that hasn't reached your Orchestrator. Everything else works as usual. Check back soon.</div>` };
  }
  return {
    title: "Connections",
    sub: "Link jobs to tickets, errors and designs",
    html: `<div class="conn-grid">${list.map((p) => `
      <section class="card conn-card" data-conn="${esc(p.id)}">
        <div class="card-b stack">
          <div class="row"><strong class="conn-name">${esc(p.name)}</strong>${p.connected ? pill("done", "Connected") : pill("", "Not connected")}</div>
          <div class="muted">${esc(p.blurb)}</div>
          ${p.connected && p.summary ? `<div class="mono conn-summary">${esc(p.summary)}</div>` : ""}
          ${p.connected && p.can_write ? `<div class="conn-options stack" data-conn-options="${esc(p.id)}">
            <div class="muted conn-options-h">Keep it updated</div>
            <label class="check"><input type="checkbox" data-opt="comment_pr" ${p.options.comment_pr ? "checked" : ""}><span>Comment when a pull request opens</span></label>
            <label class="check"><input type="checkbox" data-opt="comment_merge" ${p.options.comment_merge ? "checked" : ""}><span>Comment when it's merged</span></label>
            <label class="check"><input type="checkbox" data-opt="move_on_merge" ${p.options.move_on_merge ? "checked" : ""}><span>${esc(p.move_label)}</span></label>
            ${p.move_default ? `<input type="text" data-opt="target" value="${esc(p.options.target)}" placeholder="${esc(p.move_default)}" aria-label="Target" ${p.options.move_on_merge ? "" : "disabled"}>` : ""}
          </div>` : ""}
          <div class="row">
            <button class="btn small ${p.connected ? "" : "primary"}" data-conn-connect="${esc(p.id)}">${p.connected ? "Update" : "Connect"}</button>
            ${p.connected ? `<button class="btn small danger" data-conn-disconnect="${esc(p.id)}">Disconnect</button>` : ""}
          </div>
        </div></section>`).join("")}</div>
      <p class="muted mt-12">Credentials are saved on your computer, in this project's settings, and checked with the service before they're kept. They're never shown again.</p>`,
    after: () => {
      const onClick = async (ev) => {
        const connect = ev.target.closest("[data-conn-connect]"), disc = ev.target.closest("[data-conn-disconnect]");
        if (disc) {
          const p = list.find((x) => x.id === disc.dataset.connDisconnect);
          if (!(await formDialog(`Disconnect ${p.name}?`, `<p>Saved credentials are removed. Jobs already linked keep their links.</p>`, "Disconnect"))) return;
          try { await api(`integrations/${p.id}/disconnect`, { method: "POST", body: {} }); toast(`${p.name} disconnected`); route(); } catch (e) { toast(e.message, true); }
        } else if (connect) {
          const p = list.find((x) => x.id === connect.dataset.connConnect);
          const v = await formDialog(`${p.connected ? "Update" : "Connect"} ${p.name}`, p.fields.map((f) => `
            <label class="field"><span>${esc(f.label)}</span>
              <input ${f.secret ? 'type="password"' : 'type="text"'} name="${esc(f.key)}" autocomplete="off" autocapitalize="off" spellcheck="false"
                placeholder="${esc(p.connected && f.secret ? "saved: leave blank to keep" : f.placeholder)}">
              <small class="hint-text">${esc(f.help)}</small></label>`).join(""), p.connected ? "Save" : "Connect");
          if (!v) return;
          const btn = connect; btn.disabled = true; btn.textContent = "Checking…";
          try { const r = await api(`integrations/${p.id}/connect`, { method: "POST", body: { values: v } }); toast(`${p.name} connected${r.who ? ` as ${r.who}` : ""}`); route(); }
          catch (e) { btn.disabled = false; btn.textContent = p.connected ? "Update" : "Connect"; toast(e.message, true); }
        }
      };
      const onChange = async (ev) => {
        const box = ev.target.closest("[data-conn-options]");
        if (!box || !ev.target.dataset.opt) return;
        const options = {};
        box.querySelectorAll("[data-opt]").forEach((el) => { options[el.dataset.opt] = el.type === "checkbox" ? el.checked : el.value; });
        const target = box.querySelector('[data-opt="target"]');
        if (target) target.disabled = !options.move_on_merge;
        try { await api(`integrations/${box.dataset.connOptions}/options`, { method: "POST", body: { options } }); toast("Saved"); }
        catch (e) { toast(e.message, true); }
      };
      view.addEventListener("click", onClick);
      view.addEventListener("change", onChange);
      cleanup.push(() => { view.removeEventListener("click", onClick); view.removeEventListener("change", onChange); });
    },
  };
};

// A small picker for linking items from connected apps. Keeps its picks in a hidden input
// named "links" (JSON), so it works inside forms and dialogs alike.
async function linkPickerHtml({ prefer = [], label = "Link from your apps", emptyHint = null } = {}) {
  let providers = [];
  try { providers = (await api("integrations")).integrations.filter((p) => p.connected); } catch { /* older server */ }
  if (!providers.length) {
    return { html: emptyHint ?? `<div class="muted link-picker-empty">Link a Jira ticket, Trello card, Sentry issue or Figma design: <a href="#/connections">connect an app</a>.</div>`, wire: () => {}, connected: false };
  }
  const html = `<div class="link-picker field"><span>${esc(label)} <span class="muted">(optional)</span></span>
    <div class="row"><select class="lp-provider" aria-label="App">${providers.map((p) => `<option value="${esc(p.id)}" data-search="${p.searchable}">${esc(p.name)}</option>`).join("")}</select>
      <input type="text" class="lp-input" autocomplete="off" autocapitalize="off" spellcheck="false" style="flex:1;min-width:160px"><button type="button" class="btn small lp-add">Add</button></div>
    <small class="hint-text lp-hint"></small>
    <div class="lp-results list"></div><div class="lp-chips row"></div><input type="hidden" name="links" value="[]"></div>`;
  const wire = (root) => {
    const sel = root.querySelector(".lp-provider"), input = root.querySelector(".lp-input"), results = root.querySelector(".lp-results");
    const chips = root.querySelector(".lp-chips"), hidden = root.querySelector('input[name="links"]'), hint = root.querySelector(".lp-hint");
    let picked = [], timer;
    const sync = () => {
      hidden.value = JSON.stringify(picked.map(({ provider, ref }) => ({ provider, ref })));
      chips.innerHTML = picked.map((p, i) => `<span class="chip">${esc(PROVIDER_LABEL[p.provider])} · ${esc(p.title || p.ref)}<button type="button" data-lp-remove="${i}" aria-label="Remove">✕</button></span>`).join("");
    };
    const add = (item) => { if (!picked.some((p) => p.provider === item.provider && p.ref === item.ref)) picked.push(item); sync(); input.value = ""; results.innerHTML = ""; };
    const search = async () => {
      const p = sel.value, canSearch = sel.selectedOptions[0].dataset.search === "true", q = input.value.trim();
      if (!canSearch || (q && /^https?:\/\//.test(q))) { results.innerHTML = ""; return; }
      results.innerHTML = `<div class="empty">Searching…</div>`;
      try {
        const { items } = await api(`integrations/${p}/search?q=${encodeURIComponent(q)}`);
        if (sel.value !== p) return;
        results.innerHTML = items.length ? items.map((it, i) => `<button type="button" class="item lp-result" data-i="${i}"><div class="main-col"><div class="title">${esc(it.title)}</div><div class="meta">${esc([it.ref, it.detail].filter(Boolean).join(" · "))}</div></div></button>`).join("") : `<div class="empty">No matches.</div>`;
        results.onclick = (e) => { const b = e.target.closest(".lp-result"); if (b) add(items[Number(b.dataset.i)]); };
      } catch (e) { results.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    };
    const refreshHint = () => { hint.textContent = PROVIDER_HINT[sel.value] || ""; input.placeholder = sel.selectedOptions[0].dataset.search === "true" ? "Search or paste" : "Paste a link"; results.innerHTML = ""; if (sel.selectedOptions[0].dataset.search === "true") search(); };
    sel.addEventListener("change", refreshHint);
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 350); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); root.querySelector(".lp-add").click(); } });
    root.querySelector(".lp-add").addEventListener("click", () => { const ref = input.value.trim(); if (ref) add({ provider: sel.value, ref, title: ref }); });
    chips.addEventListener("click", (e) => { const b = e.target.closest("[data-lp-remove]"); if (b) { picked.splice(Number(b.dataset.lpRemove), 1); sync(); } });
    const first = prefer.find((id) => [...sel.options].some((o) => o.value === id));
    if (first) sel.value = first;
    refreshHint();
  };
  return { connected: true, html, wire: () => document.querySelectorAll(".link-picker").forEach((r) => { if (!r.dataset.wired) { r.dataset.wired = "1"; wire(r); } }) };
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-attach-context]");
  if (b) attachToJob(b.dataset.attachContext);
});

async function attachToJob(jobId) {
  const picker = await linkPickerHtml();
  const dlgPromise = formDialog("Attach context", picker.html + `<small class="hint-text">The ticket, error or design is added to this job's references so the AI sees it on its next step.</small>`, "Attach");
  picker.wire();
  const v = await dlgPromise;
  const links = v ? JSON.parse(v.links || "[]") : [];
  if (!links.length) return;
  try { await api(`jobs/${encodeURIComponent(jobId)}/links`, { method: "POST", body: { links } }); toast("Context attached"); route(); }
  catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- start a new project
// Describe it (a few lean questions) -> where it lives (GitHub preferred) -> create.
// Progress is saved server-side, so stopping to sign in to GitHub resumes where you were.

const NP_STEPS = [["describe", "Describe it"], ["where", "Where it lives"], ["create", "Create"]];

function npStepper(active) {
  const idx = NP_STEPS.findIndex(([k]) => k === active);
  return `<span class="np-stepper">${NP_STEPS.map(([k, label], i) =>
    `<span class="np-step ${i === idx ? "on" : i < idx ? "done" : ""}"><span class="np-dot">${i < idx ? "✓" : i + 1}</span>${esc(label)}</span>`).join('<span class="np-line"></span>')}</span>`;
}

function npQuestion(q, value) {
  const label = `<span>${esc(q.label)}${q.required ? "" : ` <span class="muted">(optional)</span>`}</span>`;
  const help = q.help ? `<small class="hint-text">${esc(q.help)}</small>` : "";
  if (q.kind === "area") return `<label class="field">${label}<textarea name="${q.key}" rows="3" ${q.required ? "required" : ""}>${esc(value)}</textarea>${help}</label>`;
  if (q.kind === "multi") {
    const chosen = new Set(value.split(",").map((v) => v.trim()).filter(Boolean));
    return `<fieldset class="field np-multi"><span>${esc(q.label)}</span>${q.options.map((o) => `<label class="check"><input type="checkbox" name="${q.key}" value="${esc(o)}" ${chosen.has(o) ? "checked" : ""}><span>${esc(o)}</span></label>`).join("")}${help}</fieldset>`;
  }
  if (q.kind === "choice") return `<label class="field">${label}<select name="${q.key}" required><option value="" disabled ${value ? "" : "selected"}>Choose…</option>${q.options.map((o) => `<option ${o === value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>${help}</label>`;
  return `<label class="field">${label}<input type="text" name="${q.key}" value="${esc(value)}" maxlength="200" ${q.required ? "required" : ""}>${help}</label>`;
}

const npSave = (draft) => api("new-project/draft", { method: "POST", body: draft });

pages["new-project"] = async (_, query) => {
  let data;
  try { data = await api("new-project"); } catch (err) {
    if (err.status !== 404) throw err;
    return { title: "Start a new project", html: `<div class="notice">Starting a new project isn't available yet. It's part of an update that hasn't reached your Orchestrator. You can still add a project you already have.</div>` };
  }
  const draft = data.draft || { answers: {}, step: "describe", host: null, visibility: "private", parent: data.default_parent, waiting_on_github: false, created_root: "" };
  draft.parent = draft.parent || data.default_parent;
  const gh = data.github;
  const step = draft.created_root ? "create" : (query.get("step") || draft.step || "describe");
  const answersFrom = (form) => Object.fromEntries(data.questions.map((q) => [q.key, q.kind === "multi" ? new FormData(form).getAll(q.key).join(", ") : (new FormData(form).get(q.key) || "").toString().trim()]));
  const resume = data.draft && !query.get("step") && draft.step !== "describe"
    ? `<div class="banner attention np-resume"><p><strong>Picking up where you left off</strong>${draft.answers.name ? ` on “${esc(draft.answers.name)}”` : ""}.${draft.waiting_on_github ? " You were finishing GitHub." : ""}</p></div>` : "";
  const discard = data.draft ? `<button type="button" class="btn ghost" id="np-discard">Start over</button>` : "";

  const ghPanel = () => {
    if (gh.user) return `<div class="np-gh ok">✓ Signed in to GitHub as <strong>${esc(gh.user)}</strong></div>`;
    if (!gh.installed) return `<div class="np-gh warn"><strong>GitHub needs a one-time setup.</strong> Install the GitHub CLI, then check again.
      <div class="setup-hint"><code>brew install gh</code><button type="button" class="btn small ghost" data-setup-copy="brew install gh">Copy</button></div>
      <div class="row mt-8"><button type="button" class="btn small" id="np-recheck">Check again</button></div></div>`;
    return `<div class="np-gh warn"><strong>Sign in to GitHub first.</strong> Your answers are saved, so you can come back right here.
      <div class="row mt-8"><button type="button" class="btn small primary" id="np-signin">Sign in to GitHub</button><button type="button" class="btn small" id="np-recheck">I've signed in: check again</button></div></div>`;
  };

  if (step === "describe") {
    return {
      title: "Start a new project", sub: npStepper("describe"),
      html: `${resume}<form class="card card-b stack np-form" id="np-describe">
        <p class="muted">A few quick questions. They become the product requirements that you and the AI can both read. Keep the answers short; you can edit it any time.</p>
        ${data.questions.map((q) => npQuestion(q, draft.answers[q.key] || "")).join("")}
        <div class="row"><span class="spacer"></span>${discard}<button class="btn primary big" type="submit">Next: where it lives</button></div></form>`,
      after: () => {
        $("#np-describe").addEventListener("submit", async (e) => {
          e.preventDefault();
          const picked = answersFrom(e.target);
          if (!picked.platform) { toast("Pick at least one platform, or choose “Not sure”.", true); return; }
          try { await npSave({ ...draft, answers: picked, step: "where" }); location.hash = "#/new-project?step=where"; route(); } catch (err) { toast(err.message, true); }
        });
        $("#np-discard")?.addEventListener("click", async () => { await api("new-project/discard", { method: "POST", body: {} }); route(); });
      },
    };
  }

  if (step === "where") {
    const host = draft.host || "github";
    const ready = host === "local" || !!gh.user;
    return {
      title: "Start a new project", sub: npStepper("where"),
      html: `${resume}<form class="card card-b stack np-form" id="np-where">
        <div class="np-hosts">
          <label class="np-host ${host === "github" ? "on" : ""}"><input type="radio" name="host" value="github" ${host === "github" ? "checked" : ""}>
            <strong>GitHub <span class="pill done">Recommended</span></strong>
            <small>Backs your work up, tracks every job as an issue and pull request, and is what the orchestrator is built around.</small></label>
          <label class="np-host ${host === "local" ? "on" : ""}"><input type="radio" name="host" value="local" ${host === "local" ? "checked" : ""}>
            <strong>Local only</strong>
            <small>Just a folder on this computer. You can publish it to GitHub later, but jobs need GitHub to run.</small></label>
        </div>
        ${host === "github" ? `${ghPanel()}
          <label class="field"><span>Who can see the repository?</span><select name="visibility"><option value="private" ${draft.visibility !== "public" ? "selected" : ""}>Only me (private)</option><option value="public" ${draft.visibility === "public" ? "selected" : ""}>Anyone (public)</option></select></label>` : ""}
        <label class="field"><span>Where on this computer?</span><input type="text" name="parent" value="${esc(draft.parent)}" spellcheck="false" autocapitalize="off">
          <small class="hint-text">A folder named “${esc(answersSlug(draft))}” is created inside this one.</small></label>
        <div class="row"><button type="button" class="btn ghost" id="np-back">Back</button><span class="spacer"></span>
          ${host === "github" && !ready ? `<button type="button" class="btn" id="np-local">Use local only for now</button>` : ""}
          <button class="btn primary big" type="submit" ${ready ? "" : "disabled"}>Create project</button></div>
        ${host === "github" && !ready ? `<small class="hint-text">Create is available once GitHub is signed in.</small>` : ""}</form>`,
      after: () => {
        const form = $("#np-where");
        const collect = (extra = {}) => ({ ...draft, host: form.host.value, visibility: form.visibility?.value || draft.visibility, parent: form.parent.value.trim(), ...extra });
        form.querySelectorAll('input[name="host"]').forEach((r) => r.addEventListener("change", async () => { await npSave(collect({ step: "where" })); route(); }));
        $("#np-back").addEventListener("click", async () => { await npSave(collect({ step: "describe" })); location.hash = "#/new-project?step=describe"; route(); });
        $("#np-recheck")?.addEventListener("click", () => route());
        $("#np-local")?.addEventListener("click", async () => { await npSave(collect({ host: "local", waiting_on_github: false })); route(); });
        $("#np-signin")?.addEventListener("click", async () => { await npSave(collect({ waiting_on_github: true, step: "where" })); runAction("config_menu", { menu: "github" }); });
        form.addEventListener("submit", async (e) => {
          e.preventDefault();
          const btn = form.querySelector('button[type="submit"]'); btn.disabled = true; btn.textContent = "Creating…";
          try {
            await npSave(collect({ step: "create", waiting_on_github: false }));
            const result = await api("new-project/create", { method: "POST", body: {} });
            window.__npResult = result;
            await refreshState();
            location.hash = "#/new-project?step=create"; route();
          } catch (err) { btn.disabled = false; btn.textContent = "Create project"; toast(err.message, true); }
        });
      },
    };
  }

  // step === "create": show what happened, and what's left
  const result = window.__npResult;
  const created = result?.root || draft.created_root;
  const steps = result?.steps || [{ name: "Created the folder and product requirements", ok: true, detail: draft.created_root }, { name: "Create the GitHub repository", ok: false, detail: "Waiting on GitHub." }];
  const githubPending = !!draft.created_root || (result && !result.github_ok);
  return {
    title: result?.name ? `${result.name} is ready` : "Finish GitHub", sub: npStepper("create"),
    html: `${resume}<section class="card"><div class="list">${steps.map((s) => `<div class="item"><span>${s.ok ? "✅" : "⚠️"}</span><div class="main-col"><div class="title">${esc(s.name)}</div><div class="meta">${esc(s.detail || "")}</div></div></div>`).join("")}</div></section>
      ${githubPending ? `<div class="card card-b stack"><strong>Your project is saved on this computer.</strong>
        <span class="muted">GitHub isn't done yet. Jobs need it, so finish it now or later from here.</span>${ghPanel()}
        <div class="row"><button class="btn primary" id="np-publish" ${gh.user ? "" : "disabled"}>Create the GitHub repository</button></div></div>` : ""}
      ${result?.recommend_platform ? `<div class="card card-b stack"><strong>You asked for a platform recommendation.</strong><span class="muted">Your first job's plan will propose platforms with reasons, based on who it's for and the problem it solves.</span></div>` : ""}
      ${(result?.platform_needs || []).map((n) => `<section class="card mt-16"><div class="card-h"><h2>${esc(n.platform)}: what it needs</h2></div><div class="card-b"><ul class="assumptions">${n.needs.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div></section>`).join("")}
      <section class="card mt-16"><div class="card-h"><h2>Next: define the product</h2></div><div class="card-b stack">
        <div>Your product requirements are written. Before the first job, read them over and add what's missing: how it should look and feel, and what it should <strong>not</strong> be.</div>
        <div><a class="btn" href="#/product">Open the product requirements</a></div></div></section>
      <div class="row mt-16"><button class="btn primary big" id="np-wizard">Set up this project</button><a class="btn big" href="#/">Open dashboard</a></div>
      <p class="muted">“Set up this project” runs the setup wizard, which asks how it's built and tested so jobs can run.</p>`,
    after: () => {
      $("#np-recheck")?.addEventListener("click", () => route());
      $("#np-signin")?.addEventListener("click", () => runAction("config_menu", { menu: "github" }));
      $("#np-wizard").addEventListener("click", () => runAction("wizard"));
      $("#np-publish")?.addEventListener("click", async (e) => {
        e.target.disabled = true; e.target.textContent = "Creating…";
        try {
          const { step } = await api("new-project/publish", { method: "POST", body: { root: created, visibility: draft.visibility } });
          if (!step.ok) { toast(step.detail, true); e.target.disabled = false; e.target.textContent = "Create the GitHub repository"; return; }
          window.__npResult = { ...(result || {}), name: result?.name || draft.answers.name, root: created, github_ok: true, steps: [...steps.filter((s) => s.ok), step] };
          toast("Repository created"); route();
        } catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = "Create the GitHub repository"; }
      });
    },
  };
};

function answersSlug(draft) {
  return (draft.answers?.name || "your-project").trim().replace(/[^A-Za-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "your-project";
}

pages.activity = async () => {
  const running = state.runs.filter((r) => r.running);
  const finished = state.runs.filter((r) => !r.running);
  return {
    title: "Activity",
    sub: "Everything started from this page since the UI was opened",
    html: `
      ${running.length ? `<section class="card"><div class="card-h"><h2>Running</h2><span class="count">${running.length}</span></div><div class="list">${running.map(liveRunCard).join("")}</div></section>` : ""}
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

let projectCardOrder = null; // roots in the order first shown this page load

pages.projects = async () => {
  const { projects } = await api("projects");
  // Keep cards where they were first shown. Switching projects makes the server list the new
  // active one first, but moving the card under your finger is disorienting: reorder on refresh only.
  const fresh = projects || [];
  if (!projectCardOrder) projectCardOrder = fresh.map((p) => p.root);
  else for (const p of fresh) if (!projectCardOrder.includes(p.root)) projectCardOrder.push(p.root);
  const pList = [...fresh].sort((x, y) => projectCardOrder.indexOf(x.root) - projectCardOrder.indexOf(y.root));
  let unfinished = null;
  try { unfinished = (await api("new-project")).draft; } catch { /* older server */ }

  const githubCount = pList.filter((p) => p.source_type === "github").length;
  const localCount = pList.filter((p) => p.source_type !== "github").length;

  const renderSourceBadge = (item) => {
    if (item.source_type === "github") {
      const label = item.github_repo || "GitHub Tracked";
      return `<span class="pill-badge github-badge" title="GitHub Remote: ${esc(item.github_repo || "Connected")}"><svg class="icon badge-icon"><use href="#i-github"/></svg>${esc(label)}</span>`;
    }
    return `<span class="pill-badge local-badge" title="Local workspace only (no GitHub remote)"><svg class="icon badge-icon"><use href="#i-folder"/></svg>Local</span>`;
  };

  const cardsHtml = pList.map((p) => `
    <div class="project-card ${p.active ? "active-project" : ""}" data-source="${esc(p.source_type || "local")}" role="button" tabindex="0"
      ${p.active ? 'data-href="#/"' : `data-switch-project="${esc(p.root)}"`} aria-label="${p.active ? "Open" : "Switch to"} ${esc(p.name)}">
      <div class="project-card-header">
        <h4 class="project-card-title">${esc(p.name)}</h4>
        <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap;">
          ${p.active ? '<span class="pill-badge active-badge">Active</span>' : ""}
          ${renderSourceBadge(p)}
          ${p.configured ? '<span class="pill-badge configured-badge">Orchestrated</span>' : ""}
          ${p.needs_you_count ? `<span class="pill-badge needs-badge">${p.needs_you_count} waiting</span>` : ""}
        </div>
      </div>
      <div class="project-card-path" title="${esc(p.root)}">${esc(p.root)}</div>
      ${renderLanguagesBar(p.languages, { className: "project-card-languages", maxLabels: 4 })}
      <div class="project-card-meta">
        ${p.branch ? `<span class="mono">${esc(p.branch)}</span>` : '<span class="muted">no branch</span>'}
        <span class="sep">·</span>
        <span class="muted">${p.dirty_files ? `${p.dirty_files} uncommitted` : "clean"}</span>
        <span class="sep">·</span>
        <span class="muted">${p.jobs_count} job(s)</span>
      </div>
      <div class="project-card-actions">
        ${p.active ? `
          <a class="btn small primary" href="#/">Open Dashboard</a>
        ` : `
          <button class="btn small ghost" data-forget-project="${esc(p.root)}" title="Remove from list">Forget</button>
        `}
      </div>
    </div>
  `).join("");

  return {
    title: "Projects",
    sub: "Manage and switch between codebases on this machine.",
    actions: `<a class="btn primary" href="#/new-project"><svg class="icon"><use href="#i-plus"/></svg>Start a new project</a><button class="btn" id="add-project-btn"><svg class="icon"><use href="#i-folder"/></svg>Add an existing project</button>`,
    html: `
      <div class="projects-container">
        ${unfinished ? `<div class="banner attention mb-16"><p><strong>Unfinished new project${unfinished.answers?.name ? `: ${esc(unfinished.answers.name)}` : ""}.</strong>${unfinished.waiting_on_github ? " Waiting on GitHub." : ""}</p><a class="btn small primary" href="#/new-project">Continue</a></div>` : ""}
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 10px;">
          <h2 style="font-size: 1.15rem; margin: 0;">Tracked Projects (${pList.length})</h2>
          <div class="filters projects-filter">
            <button type="button" class="btn small on" data-project-filter="all">All (${pList.length})</button>
            <button type="button" class="btn small" data-project-filter="github"><svg class="icon badge-icon" style="width:12px;height:12px;margin-right:3px;vertical-align:-1px;"><use href="#i-github"/></svg>GitHub (${githubCount})</button>
            <button type="button" class="btn small" data-project-filter="local"><svg class="icon badge-icon" style="width:12px;height:12px;margin-right:3px;vertical-align:-1px;"><use href="#i-folder"/></svg>Local (${localCount})</button>
          </div>
        </div>
        <div class="projects-grid" id="tracked-projects-grid">
          ${cardsHtml || '<div class="empty">No projects yet. Start a new one, or add one you already have.</div>'}
        </div>
      </div>
    `,
    after: () => {
      const filterBtns = document.querySelectorAll(".projects-filter [data-project-filter]");
      filterBtns.forEach((btn) => {
        btn.addEventListener("click", () => {
          filterBtns.forEach((b) => b.classList.remove("on"));
          btn.classList.add("on");
          const mode = btn.dataset.projectFilter;
          const cards = document.querySelectorAll("#tracked-projects-grid .project-card");
          cards.forEach((card) => {
            const src = card.dataset.source;
            if (mode === "all" || src === mode) {
              card.style.display = "";
            } else {
              card.style.display = "none";
            }
          });
        });
      });

      const addBtn = $("#add-project-btn");
      if (addBtn) {
        addBtn.addEventListener("click", async () => {
          const result = await addProjectsDialog(pList);
          if (!result.changed) return;
          await refreshState();
          if (result.switched) location.hash = "#/";
          else route();
        });
      }
    },
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
  let run = state.runs.find((r) => r.id === id);
  if (!run) { await refreshState(); run = state.runs.find((r) => r.id === id); } // opened before the list caught up
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

  const backend = getBackendUrl();
  const token = getToken();
  const streamUrl = `${backend ?? ""}/api/runs/${id}/stream?offset=0${token ? `&token=${encodeURIComponent(token)}` : ""}`;
  const es = new EventSource(streamUrl);
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
  if (parts[0] === "jobs" && parts[1]) return { page: "job", args: [parts[1]], nav: "home", query };
  if (parts[0] === "runs" && parts[1]) return { page: "run", args: [parts[1]], nav: "activity", query };
  if (parts[0] === "runs") return { page: "activity", args: [], nav: "activity", query };
  if (parts[0] === "file") return { page: "file", args: [], nav: null, query };
  if (parts[0] === "jobs") return { page: "home", args: [], nav: "home", query }; // Jobs list lives on Home
  if (parts[0] === "config") {
    const section = ConfigurationPages.resolve(parts[1]);
    return { page: "config", args: section ? [section.id] : [], nav: "config", query };
  }
  if (parts[0] === "new-project") return { page: "new-project", args: [], nav: "projects", query };
  if (parts[0] === "product") return { page: "product", args: [], nav: "home", query };
  if (parts[0] === "docs") return { page: "docs", args: parts.slice(1), nav: "docs", query };
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
  if (signingIn) return; // the loading screen stays until sign-in finishes
  // Anything tied to the previous page goes: terminals, streams, and dialogs,
  // so an action can never run against a page you've left.
  cleanup.forEach((fn) => { try { fn(); } catch {} });
  cleanup = [];
  const dlg = $("#dialog");
  if (dlg.open) dlg.close("cancel");

  const r = resolveRoute();
  current = { page: r.page, args: r.args, query: r.query, rendered: "" };
  document.querySelectorAll(".nav [data-route]").forEach((item) => item.classList.toggle("active", item.dataset.route === r.nav));
  renderSetupFab();
  const floatingBtn = $("#floating-new-btn");
  if (floatingBtn) floatingBtn.hidden = (r.page === "new");
  try {
    if (!state.project) {
      await refreshState();
      if (!state.project) return;
    }
    // A page that takes a moment (first load runs setup checks) shouldn't leave a blank screen.
    const slow = setTimeout(() => { if (current.page === r.page && !view.innerHTML.trim()) view.innerHTML = `<div class="empty" role="status">Loading…</div>`; }, 400);
    let result;
    try { result = await pages[r.page](r.args, r.query); } finally { clearTimeout(slow); }
    if (current.page === r.page && current.args.join() === r.args.join()) {
      apply(result);
      if (r.page !== "run") $("#page-title")?.focus({ preventScroll: true }); // so screen readers announce the new page
    }
  } catch (e) {
    if (e.status === 401) return showLocked(e.message);
    setHeader({ title: "Something went wrong" });
    view.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
  }
}

// Live pages refresh in place — but never under the user's hands.
async function tick() {
  await refreshState(); // also while the tab is hidden, so notifications can fire
  if (document.hidden) return;
  loadSetup();
  if (current.page === "run") {
    const run = state.runs.find((r) => r.id === current.args[0]);
    if (run) setHeader(runHeader(run)); // keeps Stop / waiting status current while you watch
    return;
  }
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
const connBtn = $("#backend-settings-btn");
if (connBtn) {
  connBtn.addEventListener("click", () => showSignInGate());
}
$("#skip-link")?.addEventListener("click", () => { $("#view").focus({ preventScroll: false }); });

// ---------------------------------------------------------------- command palette
const PALETTE_PAGES = [["Home", "#/", "What needs you, and all jobs"], ["Product", "#/product", "Pitch, who it's for, features, look and feel, what not to build"], ["Docs", "#/docs", "Every feature, job and project file in one place"], ["Features", "#/features", "Groups of work, map"], ["Feature map", "#/features?view=map", "How features depend on each other"],
  ["Projects", "#/projects", "Switch, add or start a project"], ["Activity", "#/activity", "Runs and live output"], ["Device logs", "#/devlogs", "Logs from test devices"], ["Tests", "#/tests", "Test cases, suites, coverage"],
  ["Git", "#/git", "Branches and changes"], ["Delivery", "#/delivery", "What's live, with testers, pipeline"], ["Measure", "#/measure", "KPIs and analytics"], ["Check-up", "#/checkup", "What's missing in this project"],
  ["Connections", "#/connections", "Jira, Trello, Sentry, Figma"], ["Configuration", "#/config", "Models, keys, machines, alerts"], ["Help", "#/help", "How it works, glossary"], ["New job", "#/new", "Describe work to be done"],
  ["Start a new project", "#/new-project", "Describe an idea and set it up"]];
const palette = { open: false, entries: [], shown: [], active: 0, opener: null };

function paletteBase() {
  const go = (hash) => () => { location.hash = hash; };
  const entries = PALETTE_PAGES.filter(([, hash]) => !(hash === "#/devlogs" && state.project?.mobile_app === false)).map(([label, hash, hint]) => ({ label, hint, group: "Pages", order: 1, run: go(hash) }));
  const act_ = (label, hint, fn) => entries.push({ label, hint, group: "Actions", order: 0, run: fn });
  act_("Run all tests", "Manual test run", () => runAction("test", {}));
  act_("Lock session", "Sign out of this browser", () => $("#lock-btn")?.click());
  if (Notifications.supported()) act_(Notifications.enabled() ? "Turn off browser alerts" : "Turn on browser alerts", "Alerts when a run finishes or needs you", () => $("#notify-btn")?.click());
  return entries;
}

async function openPalette() {
  if (palette.open) return;
  palette.open = true;
  palette.opener = document.activeElement;
  palette.entries = paletteBase();
  $("#palette-backdrop").hidden = false;
  $("#palette").hidden = false;
  const input = $("#palette-input");
  input.value = "";
  renderPalette();
  input.focus();
  // Jobs and features load after it opens, so it is usable straight away.
  try {
    const [{ jobs }, { features }] = await Promise.all([api("jobs"), api("features")]);
    if (!palette.open) return;
    const go = (hash) => () => { location.hash = hash; };
    palette.entries.push(...jobs.map((j) => ({ label: j.title, hint: `${j.state.label} · ${j.kind}`, group: "Jobs", order: 2, run: go(`#/jobs/${encodeURIComponent(j.id)}`) })),
      ...features.map((f) => ({ label: f.name, hint: "Feature", group: "Features", order: 3, run: go(`#/features?focus=${encodeURIComponent(f.id)}`) })));
    renderPalette();
  } catch { /* pages and actions still work */ }
}

function closePalette() {
  if (!palette.open) return;
  palette.open = false;
  $("#palette").hidden = true;
  $("#palette-backdrop").hidden = true;
  palette.opener?.focus?.();
}

function renderPalette() {
  const query = $("#palette-input").value;
  palette.shown = Palette.rank(palette.entries, query);
  palette.active = Math.min(palette.active, Math.max(palette.shown.length - 1, 0));
  const list = $("#palette-list");
  list.innerHTML = palette.shown.map((e, i) => `<li role="option" id="pal-${i}" aria-selected="${i === palette.active}" class="palette-item${i === palette.active ? " active" : ""}" data-i="${i}">
      <span class="palette-label">${esc(e.label)}</span><span class="muted palette-hint">${esc(e.hint || "")}</span><span class="pill palette-group">${esc(e.group)}</span></li>`).join("")
    || `<li class="palette-empty muted" role="presentation">Nothing matches “${esc(query)}”.</li>`;
  const input = $("#palette-input");
  if (palette.shown.length) input.setAttribute("aria-activedescendant", `pal-${palette.active}`); else input.removeAttribute("aria-activedescendant");
  list.querySelector(".active")?.scrollIntoView({ block: "nearest" });
}

function runPalette(i) {
  const entry = palette.shown[i];
  if (!entry) return;
  closePalette();
  entry.run();
}

$("#palette-input")?.addEventListener("input", () => { palette.active = 0; renderPalette(); });
$("#palette-list")?.addEventListener("click", (e) => { const li = e.target.closest("[data-i]"); if (li) runPalette(Number(li.dataset.i)); });
$("#palette-backdrop")?.addEventListener("click", closePalette);
$("#search-btn")?.addEventListener("click", openPalette);
if (!/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) { const k = $("#search-btn kbd"); if (k) k.textContent = "Ctrl K"; }
document.addEventListener("keydown", (e) => {
  const typing = e.target.matches?.("input, textarea, select, [contenteditable]");
  if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) { e.preventDefault(); palette.open ? closePalette() : openPalette(); return; }
  if (e.key === "/" && !typing && !palette.open && !e.metaKey && !e.ctrlKey && !$("#dialog").open) { e.preventDefault(); openPalette(); return; }
  if (!palette.open) return;
  if (e.key === "Escape") { e.preventDefault(); closePalette(); }
  else if (e.key === "ArrowDown") { e.preventDefault(); palette.active = Math.min(palette.active + 1, palette.shown.length - 1); renderPalette(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); palette.active = Math.max(palette.active - 1, 0); renderPalette(); }
  else if (e.key === "Enter") { e.preventDefault(); runPalette(palette.active); }
  else if (e.key === "Tab") { e.preventDefault(); } // focus stays in the search box while it is open
});
window.addEventListener("hashchange", closePalette);

// Phone layout: the bottom bar holds the everyday pages; everything else lives behind "More".
const moreBtn = $("#nav-more"), moreSheet = $("#more-sheet");
const MORE_LINKS = [["Help", "#/help"], ["Device logs", "#/devlogs"], ["Tests", "#/tests"], ["Git", "#/git"], ["Delivery", "#/delivery"], ["Measure", "#/measure"], ["Check-up", "#/checkup"], ["Docs", "#/docs"], ["Connections", "#/connections"], ["Configuration", "#/config"]];
function closeMore() { if (moreSheet.hidden) return; moreSheet.hidden = true; moreBtn.setAttribute("aria-expanded", "false"); }
moreBtn?.addEventListener("click", () => {
  if (!moreSheet.hidden) return closeMore();
  moreSheet.innerHTML = `<div class="more-sheet-h"><strong>More</strong><button type="button" class="btn small ghost" data-more-close aria-label="Close">✕</button></div>
    <nav aria-label="More pages">${MORE_LINKS.filter(([, href]) => !(href === "#/devlogs" && state.project?.mobile_app === false)).map(([label, href]) => `<a href="${href}">${esc(label)}</a>`).join("")}</nav>
    <div class="more-sheet-actions"><button type="button" class="btn" data-more-search>Search</button><a class="btn" href="#/new">New job</a>
      ${Notifications.supported() ? `<button type="button" class="btn" data-more-notify>${Notifications.enabled() ? "Turn off alerts" : "Notify me when done"}</button>` : ""}
      <button type="button" class="btn" data-more-lock>Lock session</button></div>`;
  moreSheet.hidden = false;
  moreBtn.setAttribute("aria-expanded", "true");
  moreSheet.querySelector("a")?.focus();
});
moreSheet?.addEventListener("click", (e) => {
  if (e.target.closest("a, [data-more-close]")) closeMore();
  if (e.target.closest("[data-more-search]")) { closeMore(); openPalette(); }
  if (e.target.closest("[data-more-notify]")) { $("#notify-btn").click(); closeMore(); }
  if (e.target.closest("[data-more-lock]")) { closeMore(); $("#lock-btn").click(); }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMore(); });
window.addEventListener("hashchange", closeMore);

const notifyBtn = $("#notify-btn");
function renderNotifyBtn() {
  if (!notifyBtn || !Notifications.supported()) return;
  notifyBtn.hidden = false;
  const on = Notifications.enabled();
  notifyBtn.textContent = on ? "Notifications on" : "Notify me when done";
  notifyBtn.title = on ? "Click to turn off" : "Browser alerts when a run finishes, fails or needs you";
}
notifyBtn?.addEventListener("click", async () => {
  if (Notifications.enabled()) { Notifications.disable(); renderNotifyBtn(); return; }
  const result = await Notifications.enable();
  if (result !== "granted") toast("Allow notifications in your browser to turn this on.", true);
  renderNotifyBtn();
});
renderNotifyBtn();
const lockBtn = $("#lock-btn");
if (lockBtn) {
  lockBtn.addEventListener("click", lockSession);
}
if (signingIn) showSigningIn(); // coming back from a provider's redirect: no sign-in options flash while the session is set up
setTimeout(() => { if (signingIn && !getToken()) endSigningIn(true); }, 20000); // a redirect that never reports back must not leave a spinner forever
refreshState().then(() => { if (!signingIn) route(); });
setInterval(tick, 5000);

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

// ---------------------------------------------------------------- helpers

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const attrJSON = (o) => esc(JSON.stringify(o));

let state = { project: null, runs: [], actions: {} };
let cleanup = [];
let current = { page: null, args: [], query: null, rendered: "" };

const DEFAULT_REMOTE_BACKEND = "https://calculations-absorption-pct-moreover.trycloudflare.com";

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

function formDialog(title, bodyHtml, okLabel = "Run") {
  const dlg = $("#dialog");
  $("#dialog-cancel").hidden = false;
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
  async ask_ai(params) {
    const values = await formDialog("Ask AI (Questions about changes)", `
      <p class="muted" style="margin-bottom: 8px;">Ask a question regarding the current plan, implementation diff, or test failures for this job.</p>
      <label class="field"><span>Your question</span>
        <textarea name="question" required placeholder="e.g. Why did you change the networking layer instead of the view controller?"></textarea>
      </label>`, "Ask AI");
    if (values?.question.trim()) {
      runAction("fix", { job: params.job, feedback: `Question about job: ${values.question.trim()}` });
    }
  },
  async link_logs(params) {
    const hasRemote = state.project?.remote_logs;
    const values = await formDialog("Link Logs (Update Context)", `
      <p class="muted" style="margin-bottom: 8px;">Attach crash or runtime logs to guide the next repair cycle.</p>
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
      <p class="muted" style="margin-bottom: 8px;">Override the model pipeline used for planning, building, and reviewing this job.</p>
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
  async discard_job(params) {
    if (confirm("⚠️ WARNING: THIS WILL PERMANENTLY DELETE ALL LOCAL PROGRESS & CODE CHANGES.\n\nAre you sure you want to DISCARD this job and REVERT its changes?")) {
      runAction("console");
    }
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
  const switchBtn = e.target.closest("[data-switch-project]");
  if (switchBtn) {
    const root = switchBtn.dataset.switchProject;
    api("project", { method: "POST", body: { root } })
      .then(async () => {
        await refreshState();
        toast(`Switched to ${state.project?.name || "project"}`);
        route();
      })
      .catch((err) => toast(err.message, true));
    return;
  }
  const forgetBtn = e.target.closest("[data-forget-project]");
  if (forgetBtn) {
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
  if (e.target.closest("[data-back]")) return history.back();
  const el = e.target.closest("[data-action]");
  if (!el || el.disabled) return;
  e.preventDefault();
  const action = el.dataset.action;
  const params = el.dataset.params ? JSON.parse(el.dataset.params) : {};
  if (dialogs[action]) return dialogs[action](params);
  runAction(action, params);
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

async function refreshState() {
  const backend = getBackendUrl();
  const token = getToken();
  if (backend === null && !token) {
    showSignInGate("Sign in with your access token to continue.");
    return;
  }
  try {
    const newState = await api("state");
    state = newState;
    consecutiveAuthFailures = 0;
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

function showSignInGate(message) {
  setHeader({ title: "Sign In", sub: "", actions: "" });
  document.querySelector(".app")?.classList.add("session-locked");
  const backend = getBackendUrl() || "";
  const token = getToken() || "";
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
        <details class="manual-token-details" style="font-size: 0.82rem; margin-top: 0.5rem; border-top: 1px solid var(--border); padding-top: 0.75rem;">
          <summary class="muted" style="cursor: pointer; user-select: none; text-align: center;">Advanced: Sign in with CLI access token</summary>
          <form id="signin-form" class="stack" style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.75rem;">
            <label class="field">
              <span>CLI Access Token</span>
              <input type="password" id="signin-token" name="token" value="${esc(token)}" placeholder="Paste access token from terminal" autocomplete="current-password" style="font-family: var(--mono); font-size: 0.9rem;">
            </label>
            <label class="field">
              <span>Backend URL</span>
              <input type="url" id="signin-backend" name="backend" placeholder="e.g. https://...trycloudflare.com" value="${esc(localStorage.getItem("orchestrator_backend") || "")}" style="font-size: 0.85rem;">
            </label>
            <button type="submit" id="signin-submit-btn" class="btn" style="width: 100%; justify-content: center; padding: 0.55rem;">Use Access Token</button>
          </form>
        </details>
      </div>
    </div>
  `;

  const syncBackend = () => {
    const b = ($("#signin-backend")?.value || "").trim().replace(/\/+$/, "");
    if (b) localStorage.setItem("orchestrator_backend", b);
    else localStorage.removeItem("orchestrator_backend");
  };

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
      const originalHtml = btn.innerHTML;
      btn.innerHTML = `<span>Signing in with ${name}...</span>`;
      try {
        const cred = await firebase.auth().signInWithPopup(makeProvider());
        const idToken = await cred.user.getIdToken();
        const res = await api("auth", { method: "POST", body: { id_token: idToken } });
        if (res.token) localStorage.setItem("orchestrator_token", res.token);
        toast(`Signed in as ${cred.user.email || `${name} user`}`);
        document.querySelector(".app")?.classList.remove("session-locked");
        await refreshState();
        route();
      } catch (err) {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
        if (err.code === "auth/popup-closed-by-user" || err.code === "auth/cancelled-popup-request") return;
        const msg = explain[err.code] || err.message;
        toast(msg, true);
        showSignInGate(msg);
      }
    });
  };

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
      try {
        await api("auth", { method: "POST", body: { token: t } });
        toast("Session unlocked");
        document.querySelector(".app")?.classList.remove("session-locked");
        await refreshState();
        route();
      } catch (err) {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = "Use Access Token";
        }
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

function statusPill(status) {
  const s = String(status || "unknown");
  const cls = ({ completed: "ok", "review-needed": "warn", debugging: "bad", failed: "bad", "build-failed": "bad",
                 planned: "run", scheduled: "run", running: "run", "in-progress": "run" })[s] || "";
  return `<span class="pill ${cls}">${esc(s.replace(/-/g, " "))}</span>`;
}

function originalJobItem(j) {
  const progress = j.tasks_total ? ` · ${j.tasks_done}/${j.tasks_total} tasks` : "";
  const meta = [j.type || j.kind, j.branch, ago(j.updated)].filter(Boolean).map(esc).join(" · ");
  return `<a class="item" href="#/jobs/${encodeURIComponent(j.id)}">
    <div class="main-col"><div class="title">${esc(j.title)}</div><div class="meta">${meta}${esc(progress)}</div></div>
    ${statusPill(j.status)}</a>`;
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
let jobSort = { key: "updated", dir: "desc" };
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

function jobTableRow(j) {
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
    <a class="job-table-row" href="#/jobs/${encodeURIComponent(j.id)}">
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
    <span class="status-stats"><span>${p.dirty_files} uncommitted</span><span class="sep">·</span><span>${running} running</span>${p.machine_count == null ? "" : `<span class="sep">·</span><a href="#/config">${p.machine_count} ${p.machine_count === 1 ? "machine" : "machines"}</a><span class="sep">·</span><a href="#/config">${p.model_count} ${p.model_count === 1 ? "model" : "models"}</a>`}${langs}</span></span>`;
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

function moreActionsMenu(p, opts) {
  return moreMenu([
    ["Pull device logs", p.remote_logs ? act("logs_pull") : `data-href="#/devlogs"`, p.remote_logs ? "Newest app launch" : "Set up first"],
    ["Build", act("build")],
    ["Run tests", act("test")],
    ["Distribute current branch", act("distribute"), "Firebase release of what's checked out"],
    ["Tests & coverage", `data-href="#/tests"`],
    ["Git: pull, push, branches", `data-href="#/git"`],
    "---",
    ["Setup check", act("check"), "CLIs, logins, config"],
    ["Config check", act("check_config")],
    ["Worker check", act("worker_check"), "Remote build machines"],
    ["Setup wizard", act("wizard"), "Project, tools and delivery setup"],
    ["Open full console", act("console"), "Everything else"],
  ], opts);
}

// ---------------------------------------------------------------- pages
// Each page returns { title, sub, actions, html, after? }. Pages in LIVE are
// re-rendered by the poll when their output changes.

const pages = {};
const LIVE = new Set(["home", "jobs", "job", "activity", "git"]);

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
      ${s.complete ? `<div class="row" style="margin-top:16px"><button class="btn primary big" id="setup-done">Continue to Home</button></div>` : ""}</div>`,
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
  const show = s && !s.complete && !(current.page === "home" && !s.seen);
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

pages.home = async (_, query) => {
  await loadSetup();
  if (setupState && !setupState.complete && !setupState.seen) return setupPage(setupState);
  const { jobs } = await api("jobs");
  const p = state.project;
  if (!p.branches) { // server predates the project-state branch list: use the Git endpoint
    try { p.branches = (await api("git")).branches || []; } catch { p.branches = []; }
  }
  const isOriginal = localStorage.getItem("orchestrator_home_version") === "original";

  if (isOriginal) {
    const active = jobs.filter((j) => !["completed", "archived"].includes(j.status));
    const recentRuns = state.runs.slice(0, 5);
    return {
      title: p.name,
      sub: statusLine(p),
      actions: `<button class="btn small" id="toggle-home-version-btn" style="border: 1px dashed var(--accent);" title="Switch back to modern home layout">⏭ Modern Home</button>
                <a class="btn primary" href="#/new">New job</a>`,
      html: `
        <label class="mobile-only project-inline"><span class="label">Project</span><select class="project-select-inline"></select></label>
        <div class="stats" style="margin-bottom: 16px;">
          <div class="card stat"><div class="k">Branch</div><div class="v mono">${esc(p.branch || "—")}</div></div>
          <div class="card stat"><div class="k">Uncommitted files</div><div class="v">${p.dirty_files}</div></div>
          <div class="card stat"><div class="k">Open jobs</div><div class="v">${active.length}</div></div>
          <div class="card stat"><div class="k">Running now</div><div class="v">${state.runs.filter((r) => r.running).length}</div></div>
        </div>
        <section class="card" style="margin-bottom: 16px;">
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
        <section class="card" style="margin-bottom: 16px;">
          <div class="card-h"><h2>Recent jobs</h2><a href="#/jobs">All jobs</a></div>
          <div class="list">${jobs.slice(0, 6).map(originalJobItem).join("") || `<div class="empty">No jobs yet. Start one with New job.</div>`}</div>
        </section>
        <section class="card">
          <div class="card-h"><h2>Recent runs</h2><a href="#/runs">All runs</a></div>
          <div class="list">${recentRuns.map(runItem).join("") || `<div class="empty">Nothing has run from the UI yet.</div>`}</div>
        </section>`,
      after: () => {
        renderProjectSelect($(".project-select-inline"));
        $("#toggle-home-version-btn")?.addEventListener("click", () => {
          localStorage.setItem("orchestrator_home_version", "modern");
          toast("Switched to Modern Home layout");
          route();
        });
      },
    };
  }

  // 1) Default ordered by most recent
  const sortedJobs = sortJobs(jobs);

  const HOME_FILTERS = {
    all: ["All", () => true],
    needs_you: ["Action required", (j) => j.state.group === "needs_you" && !j.active_run],
    working: ["In progress", (j) => j.state.group === "working" || j.active_run],
    done: ["Completed", (j) => j.state.group === "done"],
  };

  let filter = (query && query.get("filter")) || "all";
  if (!HOME_FILTERS[filter]) filter = "all";
  const filteredJobs = sortedJobs.filter(HOME_FILTERS[filter][1]);

  const runningRuns = state.runs.filter((r) => r.running);
  const runningBanner = runningRuns.length
    ? `<section class="card" style="margin-bottom: 16px;"><div class="card-h"><h2>Running now</h2><span class="count">${runningRuns.length}</span></div>
        <div class="list">${runningRuns.map(liveRunCard).join("")}</div></section>`
    : "";

  return {
    title: p.name,
    sub: statusLine(p),
    actions: `<button class="btn small ghost" id="toggle-home-version-btn" style="border: 1px dashed var(--border);" title="Compare with original 8-tile home screen">⏮ Original Home (debug)</button>`,
    html: `
      <label class="mobile-only project-inline"><span class="label">Project</span><select class="project-select-inline"></select></label>
      <div class="hero-actions" style="margin-bottom: 16px;">
        <a class="btn primary big desktop-only" href="#/new">New job</a>
        <button class="btn big" ${act("fix")}>Fix something</button>
        ${moreActionsMenu(p, { left: true })}
      </div>

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
            <div class="job-table-body">
              ${filteredJobs.map(jobTableRow).join("")}
            </div>
          </div>
        ` : `<div class="empty">${!jobs.length ? 'No jobs yet. <strong>New job</strong> plans work from a description; <strong>Fix something</strong> goes straight to a quick fix.' : 'No jobs match this filter.'}</div>`}
      </section>`,
    after: () => {
      renderProjectSelect($(".project-select-inline"));
      $("#toggle-home-version-btn")?.addEventListener("click", () => {
        localStorage.setItem("orchestrator_home_version", "original");
        toast("Switched to Original Home layout");
        route();
      });
    },
  };
};

const ORIGINAL_JOB_FILTERS = {
  open: ["Open", (j) => !["completed", "archived"].includes(j.status)],
  review: ["Review", (j) => j.status === "review-needed"],
  debugging: ["Debugging", (j) => j.status === "debugging"],
  completed: ["Completed", (j) => j.status === "completed"],
  all: ["All", () => true],
};

const JOB_FILTERS = {
  needs_you: ["Action required", (j) => j.state.group === "needs_you" && !j.active_run],
  working: ["In progress", (j) => j.state.group === "working" || j.active_run],
  done: ["Completed", (j) => j.state.group === "done"],
  all: ["All", () => true],
};

pages.jobs = async (_, query) => {
  const { jobs } = await api("jobs");
  const isOriginal = localStorage.getItem("orchestrator_jobs_version") === "original";

  if (isOriginal) {
    let filter = query.get("filter");
    if (!ORIGINAL_JOB_FILTERS[filter]) filter = "open";
    const shown = jobs.filter(ORIGINAL_JOB_FILTERS[filter][1]);
    return {
      title: "Jobs",
      sub: `${jobs.length} total`,
      actions: `<button class="btn small" id="toggle-jobs-version-btn" style="border: 1px dashed var(--accent);" title="Switch back to modern jobs layout">⏭ Modern Jobs</button>
                <a class="btn primary" href="#/new">New job</a>`,
      html: `
        <div class="filters">${Object.entries(ORIGINAL_JOB_FILTERS).map(([key, [label, fn]]) =>
          `<a class="btn small ${key === filter ? "on" : ""}" href="#/jobs?filter=${key}">${label} (${jobs.filter(fn).length})</a>`).join("")}
        </div>
        <section class="card"><div class="list">${shown.map(originalJobItem).join("") || `<div class="empty">No jobs here.</div>`}</div></section>`,
      after: () => {
        $("#toggle-jobs-version-btn")?.addEventListener("click", () => {
          localStorage.setItem("orchestrator_jobs_version", "modern");
          toast("Switched to Modern Jobs layout");
          route();
        });
      },
    };
  }

  let filter = query.get("filter");
  if (!JOB_FILTERS[filter]) filter = jobs.some(JOB_FILTERS.needs_you[1]) ? "needs_you" : "all";
  const shown = jobs.filter(JOB_FILTERS[filter][1]);
  return {
    title: "Jobs",
    actions: `<button class="btn small ghost" id="toggle-jobs-version-btn" style="border: 1px dashed var(--border);" title="Compare with original status-filtered jobs screen">⏮ Original Jobs (debug)</button>
              <a class="btn primary" href="#/new">New job</a>`,
    html: `
      <div class="filters">${Object.entries(JOB_FILTERS).map(([key, [label, fn]]) =>
        `<a class="btn small ${key === filter ? "on" : ""}" href="#/jobs?filter=${key}">${label} (${jobs.filter(fn).length})</a>`).join("")}
      </div>
      <section class="card"><div class="list">${shown.map((j) => jobItem(j, { withAction: filter === "needs_you" })).join("") || `<div class="empty">${!jobs.length ? 'No jobs yet. <strong>New job</strong> plans work from a description; <strong>Fix something</strong> goes straight to a quick fix.' : 'No jobs match this filter.'}</div>`}</div></section>`,
    after: () => {
      $("#toggle-jobs-version-btn")?.addEventListener("click", () => {
        localStorage.setItem("orchestrator_jobs_version", "original");
        toast("Switched to Original Jobs layout");
        route();
      });
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
  }
  items.push("---");
  items.push(["Ask AI about this job", act("ask_ai", j)]);
  items.push(["Attach logs", act("link_logs", j), "Device crash logs or test traces"]);
  items.push(["Attach mockup or reference", act("attach_mockup", j)]);
  items.push(["Override models", act("select_models", j), "Planner, builder and reviewer"]);
  items.push(["Export context", act("export_context", j)]);
  items.push("---");
  for (const link of links) items.push([`Open ${link.label}`, `data-open="${esc(link.url)}"`, "On GitHub"]);
  if (s.issue_number) items.push(["Close GitHub issue", `data-action="close_issue" data-params="${esc(JSON.stringify({ job: s.id, issue: s.issue_number }))}"`]);
  items.push(["Open in console", act("console")]);
  items.push("---");
  items.push(["Discard job and revert changes", act("discard_job", j), "Deletes uncommitted work", "danger"]);
  return moreMenu(items);
}

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
    ? `<button class="btn danger" data-stop="${esc(activeRun.id)}">Stop</button>`
    : next ? `<button class="btn primary big" ${act(next.action, { job: s.id })}>${esc(next.label)}</button>` : "";

  const logFiles = logs.flatMap((l) => l.files.length
    ? l.files.map((f) => ({ label: l.files.length > 1 ? `${l.label} / ${f.split("/").pop()}` : l.label, path: f }))
    : [{ label: l.label }]);

  return {
    title: s.title,
    sub: `<span class="status-line"><span>${esc(kindUpper)}</span><span class="sep">·</span><span class="mono">${esc(displayId)}</span>${s.branch ? `<span class="sep">·</span><span class="mono">${esc(s.branch)}</span>` : ""}</span>`,
    actions: jobHeaderActions(summary, links),
    html: `
      ${runs.filter((r) => r.running).map((r) => `<section class="card" style="margin-bottom: 16px;"><div class="list">${liveRunCard(r)}</div></section>`).join("")}
      ${s.question ? `<section class="card" style="margin-bottom: 16px;"><div class="card-h"><h2>Question from the planner</h2></div><div class="card-b stack">
        <p class="question">${esc(s.question)}</p><div><button class="btn primary" ${act("answer", { job: s.id })}>Answer</button></div></div></section>` : ""}

      <section class="job-hero tone-${esc(s.state.tone || "")}">
        <div class="job-hero-main">
          <span class="pill ${esc(s.state.tone || "")}">${esc(s.state.label || s.status)}</span>
          <p class="job-hero-reason">${esc(s.state.reason || "")}</p>
        </div>
        <div class="job-hero-action">${heroAction}</div>
      </section>

      <section class="card" style="margin-bottom: 16px;">
        <div class="card-h"><h2>Progress</h2><span class="count">${tasksDone}/${tasksTotal}</span></div>
        <div class="card-b stack">
          <div class="tasks-progress-wrap">
            <div class="progress-bar-container"><div class="progress-bar-fill" style="width: ${tasksPct}%"></div></div>
            <span class="progress-text">${tasksPct}%</span>
          </div>
          ${nextTask ? `<div>Next: <strong>${esc(nextTask)}</strong></div>` : ""}
          <div class="muted">${esc(testsDisplay)} · ${esc(pipeline.planner)} → ${esc(pipeline.builder)} → ${esc(pipeline.reviewer)}</div>
        </div>
      </section>

      <section class="card" style="margin-bottom: 16px;">
        <div class="card-h"><h2>Changes</h2>${changes?.base ? `<span class="count">vs ${esc(changes.base)}</span>` : ""}</div>
        <div class="card-b delta-summary-line">
          <div class="delta-line unsaved">${esc(deltaUnsaved)}</div>
          <div class="delta-files-list">${filesHtml}</div>
        </div>
        ${changes?.hypothesis ? `<div class="card-b" style="border-top: 1px solid var(--border); padding-top: 10px; font-size: 13px;"><strong>Why:</strong> ${esc(changes.hypothesis)}</div>` : ""}
        ${changes?.diffstat ? `<details class="raw" style="border-top: 1px solid var(--border);"><summary style="padding: 8px 16px; font-size: 12.5px; color: var(--muted); cursor: pointer;">View full diffstat (${allFiles.length} files)</summary><pre class="file" style="margin: 0; border: none; border-radius: 0;">${esc(changes.diffstat)}</pre></details>` : ""}
      </section>

      <!-- Docs Section (Brief / Summary / Investigations) -->
      <div id="docs-section">
        ${docs.map((d, i) => `<section class="card" style="margin-bottom: 16px;"><details class="raw" ${i === 0 ? "open" : ""}><summary><strong>${esc(d.title)}</strong></summary><pre class="doc">${esc(d.text)}</pre></details></section>`).join("")}
      </div>

      <!-- Linked from connected apps -->
      <section class="card" style="margin-bottom: 16px;">
        <div class="card-h"><h2>Linked tickets, errors &amp; designs</h2><button class="btn small" data-attach-context="${esc(s.id)}">Attach…</button></div>
        <div class="list">${(job.external_links || []).map((l) => `<div class="item"><div class="main-col"><div class="title">${l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title || l.ref)} ↗</a>` : esc(l.title || l.ref)}</div>
          <div class="meta">${esc([PROVIDER_LABEL[l.provider] || l.provider, l.ref, l.detail].filter(Boolean).join(" · "))}</div></div></div>`).join("") || `<div class="empty">Nothing linked. Attach a Jira ticket, Trello card, Sentry issue or Figma design.</div>`}</div>
      </section>

      ${(job.integration_log || []).length ? `<section class="card" style="margin-bottom: 16px;">
        <div class="card-h"><h2>Updates sent to linked apps</h2></div>
        <div class="list">${job.integration_log.slice().reverse().map((e) => `<div class="item"><span aria-label="${e.ok ? "sent" : "failed"}">${e.ok ? "✅" : "⚠️"}</span><div class="main-col">
          <div class="title">${esc(e.message)}</div><div class="meta">${esc([PROVIDER_LABEL[e.provider] || e.provider, e.ref, String(e.event || "").replace(":", " · ").replace("_", " "), e.t].filter(Boolean).join(" · "))}</div></div></div>`).join("")}</div>
      </section>` : ""}

      <!-- Attached UI Mockups / References -->
      ${job.reference_artifacts?.length ? `
        <section class="card" style="margin-bottom: 16px;">
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

      <!-- Tasks Checklist -->
      ${tasks.length ? `<section class="card" style="margin-bottom: 16px;"><div class="card-h"><h2>Tasks Checklist</h2><span class="count">${tasksDone}/${tasks.length}</span></div>
        <div class="list">${tasks.map((t, i) => {
          const title = typeof t === "string" ? t : (t.title || t.name || t.description || `Task ${i + 1}`);
          const key = typeof t === "object" && t ? String(t.id ?? i) : String(i);
          const isDone = done.has(key) || done.has(String(i));
          return `<div class="item"><span aria-label="${isDone ? "done" : "to do"}">${isDone ? "✅" : "○"}</span><div class="main-col"><div class="title">${esc(title)}</div></div></div>`;
        }).join("")}</div></section>` : ""}

      <!-- Activity & Runs -->
      ${runs.length ? `<section class="card" style="margin-bottom: 16px;"><div class="card-h"><h2>Activity & Runs</h2><span class="count">${runs.length}</span></div><div class="list">${runs.map(runItem).join("")}</div></section>` : ""}

      <!-- Logs -->
      <section class="card" style="margin-bottom: 16px;"><div class="card-h"><h2>Logs</h2></div>
        <div class="list">${logFiles.map((f) => f.path
          ? `<a class="item" href="#/file?path=${encodeURIComponent(f.path)}"><div class="main-col"><div class="title mono">${esc(f.label)}</div></div></a>`
          : `<div class="item"><div class="main-col"><div class="title">${esc(f.label)}</div></div></div>`).join("")
          || `<div class="empty">No logs linked. Click “Link Logs” above to attach.</div>`}</div></section>

      <!-- Output Files -->
      ${outputs.length ? `<section class="card" style="margin-bottom: 16px;"><div class="card-h"><h2>Output files</h2><span class="count">${outputs.length}</span></div>
        <div class="list">${outputs.map((o) => `<a class="item" href="#/file?path=${encodeURIComponent(o.path)}">
          <div class="main-col"><div class="title mono">${esc(o.path.split("/").slice(2).join("/") || o.path)}</div>
          <div class="meta">${(o.size / 1024).toFixed(1)} KB · ${esc(ago(o.mtime))}</div></div></a>`).join("")}</div></section>` : ""}

      <!-- Technical Details -->
      <section class="card"><details class="raw"><summary>Technical details (${esc(s.id)})</summary><pre>${esc(JSON.stringify(job, null, 2))}</pre></details></section>
    `,
  };
};

const JOB_TYPE_INFO = [
  ["bug", "Bug fix", "Find and fix a problem"],
  ["feature", "Feature", "Plan, then build"],
  ["quick", "Quick change", "Small tweak or refactor"],
  ["design", "Design", "Prototype a screen"],
  ["coverage", "Tests", "Add missing tests"],
];

pages.new = async (_, query) => {
  const picker = await linkPickerHtml();
  return {
  title: "New job",
  sub: "Describe the work. It gets planned, then built on a worker. Anything that needs your input shows up as it runs.",
  html: `
    <form class="card card-b stack" id="new-job">
      <div class="field"><span>Kind</span>
        <div class="segmented">${JOB_TYPE_INFO.map(([v, t, d], i) =>
          `<label><input type="radio" name="type" value="${v}" ${(query.get("type") || "bug") === v ? "checked" : ""}>${t}<small>${d}</small></label>`).join("")}
        </div></div>
      <label class="field"><span>What should happen?</span>
        <input type="text" name="summary" required maxlength="500" placeholder="e.g. Rejoining a lobby after backgrounding shows an empty seat">
      </label>
      <label class="field"><span>Details <span class="muted">(optional)</span></span>
        <textarea name="spec" placeholder="Steps to reproduce, expected vs actual, acceptance criteria, links…"></textarea>
      </label>
      ${picker.html}
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
  after: () => {
    picker.wire();
    $("#new-job").addEventListener("submit", (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      let links = [];
      try { links = JSON.parse(f.get("links") || "[]"); } catch { /* none */ }
      runAction("new_job", {
        type: f.get("type"), summary: f.get("summary"), spec: f.get("spec"), branch_mode: f.get("branch_mode") || "new",
        no_dispatch: f.has("no_dispatch"), yolo: f.has("yolo"), free: f.has("free"), links,
      });
    });
  },
  };
};

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

pages.tests = async (_, query) => {
  view.innerHTML = `<div class="empty">Finding tests…</div>`;
  const data = await api(`tests${query.get("refresh") ? "?refresh=1" : ""}`);
  const cov = data.coverage;
  const filter = (query.get("q") || "").toLowerCase();
  // Discovery also matches source files with no tests in them; those aren't runnable suites.
  const withTests = data.suites.filter((s) => s.tests > 0);
  const suites = withTests.filter((s) => !filter || s.name.toLowerCase().includes(filter));
  const total = withTests.reduce((n, s) => n + s.tests, 0);
  return {
    title: "Tests",
    sub: `${withTests.length} suites · ${total} tests`,
    actions: `<button class="btn primary" ${act("test")}>Run all tests</button>${moreMenu([["Measure coverage", act("coverage"), "Full test run with coverage; takes a while"], ["Refresh list", `data-href="#/tests?refresh=1"`], ["Expand coverage (AI job)", `data-href="#/new?type=coverage"`]])}`,
    html: `
      ${data.error ? `<div class="notice bad">${esc(data.error)}</div>` : ""}
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

function showConfigMutationDialog(title, bodyHtml, okLabel, buildRequest, successMessage) {
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
      <p class="muted" style="margin-top:12px">Credentials are saved on your computer, in this project's settings, and checked with the service before they're kept. They're never shown again.</p>`,
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
async function linkPickerHtml() {
  let providers = [];
  try { providers = (await api("integrations")).integrations.filter((p) => p.connected); } catch { /* older server */ }
  if (!providers.length) {
    return { html: `<div class="muted link-picker-empty">Link a Jira ticket, Trello card, Sentry issue or Figma design: <a href="#/connections">connect an app</a>.</div>`, wire: () => {} };
  }
  const html = `<div class="link-picker field"><span>Link from your apps <span class="muted">(optional)</span></span>
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
    refreshHint();
  };
  return { html, wire: () => document.querySelectorAll(".link-picker").forEach((r) => { if (!r.dataset.wired) { r.dataset.wired = "1"; wire(r); } }) };
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

pages.projects = async () => {
  const { projects } = await api("projects");
  const pList = projects || [];

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
    <div class="project-card ${p.active ? "active-project" : ""}" data-source="${esc(p.source_type || "local")}">
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
          <button class="btn small primary" data-switch-project="${esc(p.root)}">Switch to project</button>
          <button class="btn small ghost" data-forget-project="${esc(p.root)}" title="Remove from list">Forget</button>
        `}
      </div>
    </div>
  `).join("");

  return {
    title: "Projects",
    sub: "Manage and switch between codebases on this machine.",
    actions: `<button class="btn primary" id="add-project-btn"><svg class="icon"><use href="#i-plus"/></svg>Add Project</button>`,
    html: `
      <div class="projects-container">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; gap: 10px;">
          <h2 style="font-size: 1.15rem; margin: 0;">Tracked Projects (${pList.length})</h2>
          <div class="filters projects-filter">
            <button type="button" class="btn small on" data-project-filter="all">All (${pList.length})</button>
            <button type="button" class="btn small" data-project-filter="github"><svg class="icon badge-icon" style="width:12px;height:12px;margin-right:3px;vertical-align:-1px;"><use href="#i-github"/></svg>GitHub (${githubCount})</button>
            <button type="button" class="btn small" data-project-filter="local"><svg class="icon badge-icon" style="width:12px;height:12px;margin-right:3px;vertical-align:-1px;"><use href="#i-folder"/></svg>Local (${localCount})</button>
          </div>
        </div>
        <div class="projects-grid" id="tracked-projects-grid">
          ${cardsHtml || '<div class="empty">No projects tracked yet. Add a project to get started.</div>'}
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
  document.querySelectorAll(".nav [data-route]").forEach((item) => item.classList.toggle("active", item.dataset.route === r.nav));
  renderSetupFab();
  const floatingBtn = $("#floating-new-btn");
  if (floatingBtn) floatingBtn.hidden = (r.page === "new");
  try {
    if (!state.project) {
      await refreshState();
      if (!state.project) return;
    }
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
const lockBtn = $("#lock-btn");
if (lockBtn) {
  lockBtn.addEventListener("click", lockSession);
}
refreshState().then(route);
setInterval(tick, 5000);

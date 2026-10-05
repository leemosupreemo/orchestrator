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
let authResolved = false;
let authUser = null;
try { signingIn = Object.keys(sessionStorage).some((k) => k.startsWith("firebase:pendingRedirect") && sessionStorage.getItem(k) === "true"); } catch { /* storage blocked: nothing to resume */ }

function endSigningIn(resume = false) {
  signingIn = false;
  if (resume) refreshState().then(route);
}

if (typeof window !== "undefined" && window.firebase?.auth) {
  try {
    window.firebase.auth().getRedirectResult().then(async (cred) => {
      if (!(cred && cred.user)) { if (signingIn) endSigningIn(true); return; } // no redirect was pending after all
      try {
        await afterProviderSignIn(cred.user, "your account");
      } catch (err) {
        endSigningIn();
        showSignInGate(err.message);
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
const esc = Html.escape;  // html.js
const attrJSON = (o) => esc(JSON.stringify(o));

let state = { project: null, runs: [], actions: {} };
let cleanup = [];
let current = { page: null, args: [], query: null, rendered: "" };

const DEFAULT_REMOTE_BACKEND = "";

function getBackendUrl() {
  const params = new URLSearchParams(window.location.search);
  const qb = params.get("backend");
  if (qb) {
    let clean = qb.replace(/\/+$/, "");
    if (!clean.startsWith("http://") && !clean.startsWith("https://")) clean = `https://${clean}`;
    localStorage.setItem("orchestrator_backend", clean);
    return clean;
  }
  const stored = localStorage.getItem("orchestrator_backend");
  if (stored) {
    if (stored.startsWith("http://") || stored.startsWith("https://")) return stored;
    return `https://${stored}`;
  }
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

// What this page saw go wrong while it couldn't reach the computer: failed requests, going offline, the tab hidden.
// Kept in localStorage (so a reload doesn't lose it) and sent to the computer's client.jsonl once a request gets through,
// where `orchestrator connection-log -f` shows it next to what the server answered and what the tunnel said.
const ConnectionLog = (() => {
  const KEY = "orchestrator_connection_log";
  const MAX = 200;
  let events = [];
  try { events = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { events = []; }
  if (!Array.isArray(events)) events = [];
  let sending = false;
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(events)); } catch { /* storage blocked */ } };
  function add(kind, detail = {}) {
    events.push({ kind, client_at: new Date().toISOString(), online: navigator.onLine, visible: document.visibilityState,
      connection: navigator.connection?.effectiveType || "", ...detail });
    if (events.length > MAX) events = events.slice(-MAX);
    save();
  }
  async function flush() {
    if (sending || !events.length) return;
    sending = true;
    const batch = events.slice(0, 100);
    try {
      await api("client-log", { method: "POST", body: { events: batch } });
      events = events.slice(batch.length);
    } catch (e) {
      if (e.status === 404) events = []; // a computer running an older Orchestrator has nowhere to keep them
    } finally {
      sending = false;
      save();
    }
  }
  window.addEventListener("online", () => add("browser_online"));
  window.addEventListener("offline", () => add("browser_offline"));
  document.addEventListener("visibilitychange", () => add(document.hidden ? "tab_hidden" : "tab_visible"));
  return { add, flush };
})();

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
  const started = performance.now();
  const logged = path !== "client-log";
  const where = { method, path: path.split("?")[0], backend: backend ? new URL(backend).host : location.host };
  let res;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: hasBody ? JSON.stringify(body) : (isMutation ? "{}" : undefined),
      credentials: backend ? "omit" : "same-origin",
    });
  } catch (e) {
    // The browser doesn't say why (DNS, reset, timeout), but how long it took and whether it thought it was online helps.
    if (logged) ConnectionLog.add("fetch_failed", { ...where, ms: Math.round(performance.now() - started), error: `${e.name}: ${e.message}` });
    throw e;
  }
  const data = await res.json().catch(() => ({}));
  if (logged && (res.status === 401 || res.status >= 500)) {
    ConnectionLog.add("http_error", { ...where, status: res.status, ms: Math.round(performance.now() - started) });
  }
  if (!res.ok) {
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// Messages: one overlay for everything the app tells you (see messages.js). `toast` is the short form used everywhere:
//   toast("Saved")                      success
//   toast(error.message, true)          error, with a hint about what to do (Errors.explain)
//   toast("Pick a platform first", "warning")   or "info"
//   toast("Deleted", false, { label: "Undo", run })   success with an action
const messageStore = Messages.createStore();
const messageUi = Messages.mount($("#messages"), messageStore, { onActionError: (e) => toast(e.message, true) });

function notify(kind, text, options = {}) {
  const id = messageStore.push({ kind, text, ...options });
  messageUi.render();
  return id;
}

function clearMessage(id) {
  if (messageStore.dismiss(id)) messageUi.render();
}

function toast(message, kind = false, action = null) {
  const level = kind === true ? "error" : typeof kind === "string" ? kind : "success";
  const text = level === "error" ? Errors.explain(message) : message;
  const isConnection = text.startsWith("Can't reach Orchestrator.");
  return notify(level, text, { ...(isConnection ? { id: "connection" } : {}), ...(action ? { actions: [action] } : {}) });
}

// "1 test", "3 tests": counts read as words, never "test(s)".
const plural = (n, word, many = `${word}s`) => `${n} ${Number(n) === 1 ? word : many}`;

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
  if (r.action === "console" || r.stopped) return pill("", "Stopped");
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
// items: [label, attributes, description?, "danger"?], "---", or ["header", text]. `compact` (long menus) keeps each
// item to one line and moves its description into the tooltip, so the whole menu fits without scrolling.
function moreMenu(items, { label = "More", left = false, compact = false } = {}) {
  const body = items.map((it) => {
    if (it === "---") return "<hr>";
    if (Array.isArray(it) && it[0] === "header") {
      return `<div class="more-menu-header">${esc(it[1])}</div>`;
    }
    const tip = compact && it[2] ? ` title="${esc(it[2])}"` : "";
    return `<button class="btn${it[3] === "danger" ? " danger" : ""}" ${it[1]}${tip}>${esc(it[0])}</button>${!compact && it[2] ? `<div class="hint">${esc(it[2])}</div>` : ""}`;
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

function formDialog(title, bodyHtml, okLabel = "Run", { danger = false, compact = false } = {}) {
  const dlg = $("#dialog");
  if (compact) dlg.classList.add("dialog-compact");
  else dlg.classList.remove("dialog-compact");
  $("#dialog-cancel").hidden = false;
  $("#dialog-cancel").textContent = "Cancel";
  $("#dialog-title").textContent = title;
  $("#dialog-body").innerHTML = bodyHtml;
  $("#dialog-ok").textContent = okLabel;
  $("#dialog-ok").className = danger ? "btn danger" : "btn primary";
  dlg.returnValue = "";
  if (!dlg.open) dlg.showModal();
  const first = $("#dialog-body textarea, #dialog-body input, #dialog-body select");
  if (first) first.focus();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      dlg.classList.remove("dialog-compact");
      $("#dialog-ok").className = "btn primary";
      if (dlg.returnValue !== "ok") return resolve(null);
      resolve(Object.fromEntries(new FormData($("#dialog-form")).entries()));
    }, { once: true });
  });
}

const globalDialog = $("#dialog");
const closeGlobalDialog = (event) => {
  if (event && (event.type === "pointerdown" || event.type === "touchstart")) {
    event.preventDefault();
  }
  if (!globalDialog?.open) return;
  const cancelEvent = new Event("cancel", { cancelable: true });
  globalDialog.dispatchEvent(cancelEvent);
  if (!cancelEvent.defaultPrevented) {
    globalDialog.close("cancel");
  }
};

$("#dialog-close")?.addEventListener("pointerdown", closeGlobalDialog);
$("#dialog-close")?.addEventListener("click", closeGlobalDialog);
$("#dialog-cancel")?.addEventListener("pointerdown", closeGlobalDialog);

globalDialog?.addEventListener("pointerdown", (event) => {
  if (event.target === globalDialog) {
    const rect = globalDialog.getBoundingClientRect();
    const inside = (
      rect.top <= event.clientY &&
      event.clientY <= rect.bottom &&
      rect.left <= event.clientX &&
      event.clientX <= rect.right
    );
    if (!inside) {
      closeGlobalDialog(event);
    }
  }
});
globalDialog?.addEventListener("click", (event) => {
  if (event.target === globalDialog) {
    const rect = globalDialog.getBoundingClientRect();
    const inside = (
      rect.top <= event.clientY &&
      event.clientY <= rect.bottom &&
      rect.left <= event.clientX &&
      event.clientX <= rect.right
    );
    if (!inside) {
      closeGlobalDialog(event);
    }
  }
});

function addProjectsDialog(trackedProjects) {
  const dlg = $("#dialog");
  const body = $("#dialog-body");
  const cancel = $("#dialog-cancel");
  const done = $("#dialog-ok");
  let changed = false;
  let switched = false;
  let pendingAdds = 0;

  $("#dialog-title").textContent = "Add Existing Project";
  done.textContent = "Done";
  done.disabled = false;
  cancel.hidden = false;
  cancel.textContent = "Close";
  cancel.disabled = false;
  body.innerHTML = `
    <nav class="dialog-tabs" role="tablist" aria-label="Add project modes">
      <button type="button" class="dialog-tab-btn active" role="tab" id="tab-discovered" aria-selected="true" aria-controls="panel-discovered" data-project-tab="discovered">
        <svg class="icon"><use href="#i-search"/></svg>Discovered
      </button>
      <button type="button" class="dialog-tab-btn" role="tab" id="tab-custom" aria-selected="false" aria-controls="panel-custom" data-project-tab="custom">
        <svg class="icon"><use href="#i-folder"/></svg>Custom Path
      </button>
    </nav>
    <div id="panel-discovered" role="tabpanel" aria-labelledby="tab-discovered" class="dialog-tab-panel">
      <div class="project-discovery-head">
        <h3>Projects on this computer</h3>
        <small class="muted">Add any discovered codebases to your tracked projects.</small>
      </div>
      <div id="project-discovery-list" class="project-discovery-list" aria-live="polite">
        <div class="project-discovery-status"><span class="pill working"><span class="dot"></span>Scanning for projects...</span></div>
      </div>
    </div>
    <div id="panel-custom" role="tabpanel" aria-labelledby="tab-custom" class="dialog-tab-panel stack" hidden>
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
      <div class="row end"><button type="button" class="btn primary" id="manual-add-project">Add directory</button></div>
    </div>
  `;

  const list = $("#project-discovery-list");
  const form = $("#dialog-form");
  const manualButton = $("#manual-add-project");
  const tabDiscoveredBtn = $("#tab-discovered");
  const tabCustomBtn = $("#tab-custom");
  const panelDiscovered = $("#panel-discovered");
  const panelCustom = $("#panel-custom");

  const switchTab = (tabName) => {
    const isDiscovered = tabName === "discovered";
    tabDiscoveredBtn.classList.toggle("active", isDiscovered);
    tabDiscoveredBtn.setAttribute("aria-selected", String(isDiscovered));
    panelDiscovered.hidden = !isDiscovered;

    tabCustomBtn.classList.toggle("active", !isDiscovered);
    tabCustomBtn.setAttribute("aria-selected", String(!isDiscovered));
    panelCustom.hidden = isDiscovered;

    if (!isDiscovered) {
      panelCustom.querySelector('input[name="root"]')?.focus();
    }
  };

  const sendAdd = ProjectPicker.createAddQueue((request) => api(request.path, request.options));
  const enqueueAdd = (request) => {
    pendingAdds += 1;
    done.disabled = true;
    cancel.disabled = true;
    return sendAdd(request).finally(() => {
      pendingAdds -= 1;
      done.disabled = pendingAdds > 0;
      cancel.disabled = pendingAdds > 0;
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
    const tabBtn = event.target.closest("[data-project-tab]");
    if (tabBtn) {
      switchTab(tabBtn.dataset.projectTab);
      return;
    }

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
      toast("Enter a project directory path", "warning");
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
    toast("Wait for the project to finish adding", "info");
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
      cancel.textContent = "Cancel";
      cancel.disabled = false;
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
    if (await formDialog(`Close issue #${params.issue}?`, `<p>Closes the issue on GitHub and marks this job complete.</p>`, "Close issue")) {
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
    if (await formDialog("Split into separate jobs?", `<p>Each task becomes its own job that can run alongside the others, with a GitHub sub-issue for each.</p>`, "Split")) {
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
        toast("Download started", "info");
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
    const run = state.runs.find((r) => r.id === stop.dataset.stop);
    if (run) run.stopped = true;
    api(`runs/${encodeURIComponent(stop.dataset.stop)}/stop`, { method: "POST", body: {} }).catch((err) => toast(err.message, true));
    return;
  }
  const forgetBtn = e.target.closest("[data-forget-project]");
  if (forgetBtn) {
    e.stopPropagation();
    const root = forgetBtn.dataset.forgetProject;
    formDialog("Remove from your projects?", `<p>Only the list changes: the folder and its files stay where they are. Add it again any time.</p>`, "Remove", { danger: true }).then((ok) => ok &&
      api("projects", { method: "DELETE", body: { root } })
        .then(async () => {
          toast("Project removed");
          await refreshState();
          route();
        })
        .catch((err) => toast(err.message, true)));
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

let connectionLost = false;
let connectionLostAt = 0;
let pollFailures = 0; // consecutive failed background polls
let lastInbox = null; // previous inbox items, to spot new things waiting on you
let lastRuns = null; // previous poll, to spot runs that finished or started waiting

// The page is deployed on its own; the computer it opens has whatever version was installed. Say so when it's behind,
// since the page may call things it doesn't have. (Only for a computer reached from elsewhere: a local page is served by it.)
let runnerWarned = false;
function checkRunnerVersion(runner) {
  if (runnerWarned || !getBackendUrl() || !Account.outdated(runner?.api_version)) return;
  runnerWarned = true;
  notify("warning", `This computer runs an older Orchestrator${runner?.version ? ` (${runner.version})` : ""}, so some things may not work. ${Account.UPDATE_HINT.replace(/`/g, "")}`, { id: "runner-old", sticky: true });
}

async function refreshState() {
  if (signingIn) return;
  const backend = getBackendUrl();
  const token = getToken();
  if (backend === null && !token) {
    showSignInGate("Sign in with your access token to continue.");
    return;
  }
  if (Account.active() && !backend) { // the hosted app, before a computer is chosen: just the sign-in screen
    if (window.firebase?.auth?.().currentUser) {
      openAccount().catch(() => showSignInGate());
      return;
    }
    if (!authResolved) {
      showSigningIn("Connecting to your account…");
      return;
    }
    showSignInGate();
    return;
  }
  try {
    const newState = await api("state");
    for (const event of [...Notifications.events(lastRuns, newState.runs), ...Notifications.inboxEvents(lastInbox, newState.inbox)]) Notifications.show(event);
    lastRuns = newState.runs;
    lastInbox = newState.inbox || [];
    showPrdUpdate(newState.product_notice);
    checkRunnerVersion(newState.runner);
    state = newState;
    const viewerRole = state.you?.role || "owner";
    ConfigurationPages.applyRole(viewerRole, document);
    if (configurationMenu) configurationMenu.innerHTML = ConfigurationPages.renderMenu(viewerRole);
    consecutiveAuthFailures = 0;
    pollFailures = 0;
    if (connectionLost) {
      connectionLost = false;
      ConnectionLog.add("reconnected", { down_ms: Date.now() - connectionLostAt });
      clearMessage("connection");
      notify("success", "Back online.", { id: "connection-restored", timeout: 2500 });
    }
    ConnectionLog.flush();
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
        if (Account.active() && window.firebase?.auth?.().currentUser) {
          relocateMachine().catch(() => openAccount().catch(() => showSignInGate("Session expired. Please sign in again.")));
        } else {
          showSignInGate("Session expired. Please sign in again.");
        }
      }
      return;
    }
    // Transient network errors, tunnel drops, or 502/504 errors should NEVER lock the user out!
    console.warn("Background state refresh notice (will retry):", e.message);
    pollFailures += 1;
    // Opening the hosted app with an old saved address: the account screen below finds the computer, so no warning.
    const reopening = !state.project && Account.active();
    if (pollFailures >= 2 && !connectionLost && !reopening) { // two misses in a row: say so, rather than showing stale data silently
      connectionLost = true;
      connectionLostAt = Date.now();
      ConnectionLog.add("connection_lost", { failures: pollFailures, error: e.message, ua: navigator.userAgent });
      notify("warning", "Can't reach Orchestrator. Check that it's running and that you're online.", { id: "connection", sticky: true });
    }
    if (Account.active()) {
      if (window.firebase?.auth?.().currentUser) {
        relocateMachine().catch(() => openAccount().catch(() => {}));
        return;
      }
      if (!authResolved) {
        showSigningIn("Connecting to your account…");
        return;
      }
    }
    if (pollFailures >= 2) relocateMachine();
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
  // The phone tab bar repeats the sidebar's counts.
  for (const [source, copy] of [["#inbox-badge", "inbox"], ["#running-badge", "running"]]) {
    const from = $(source), to = document.querySelector(`.tabbar [data-badge="${copy}"]`);
    if (from && to) { to.hidden = from.hidden; to.textContent = from.textContent; to.title = from.title; to.className = from.className; }
  }
  const mobileTitle = $("#mobile-title"); // the phone header names the project you're in
  if (mobileTitle) mobileTitle.textContent = state.project?.name || "";
  const pageTitle = $("#page-title")?.dataset.title;
  document.title = Notifications.tabTitle(pageTitle ? `${pageTitle} · Orchestrator` : "Orchestrator", state.inbox_count || 0);

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
  if (state.you?.role !== "member") html += `<option value="__manage__" class="manage-projects-option" style="background-color: var(--manage-option-bg, #323947); color: var(--text);">📁 Manage all projects...</option>`;
  if (select.dataset.html !== html) {
    select.innerHTML = html;
    select.dataset.html = html;
  }
}

document.addEventListener("change", async (e) => {
  if (!e.target.matches("#project-select")) return;
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
    location.hash = "#/";
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

// ---------------------------------------------------------------- account (hosted app)

// The control plane, served by the hosted app at /cp: your computers and the one-time tickets that open them.
async function cpApi(path, { method = "GET", body } = {}) {
  const user = window.firebase?.auth?.().currentUser;
  if (!user) throw Object.assign(new Error("Sign in first."), { status: 401 });
  const res = await fetch(`/cp/${path}`, {
    method,
    headers: { Authorization: `Bearer ${await user.getIdToken()}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `${res.status} ${res.statusText}`), { status: res.status });
  return data;
}

// After Google (or another provider): on the hosted app, find your computers; elsewhere, sign in to this server directly.
async function afterProviderSignIn(user, name) {
  if (Account.active()) return openAccount();
  const backend = getBackendUrl();
  if (!backend && window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost") {
    throw new Error(`Signed in as ${user.email || name}, but there's no computer address yet. Enter it under "Connect by address" below.`);
  }
  let res;
  try {
    res = await api("auth", { method: "POST", body: { id_token: await user.getIdToken() } });
  } catch (apiErr) {
    throw new Error(`Signed in as ${user.email || name}, but couldn't reach ${backend || "Orchestrator"} (${apiErr.message}). Make sure Orchestrator is running on your computer.`);
  }
  await unlockWith(res.token, `Signed in as ${user.email || name}`);
}

async function unlockWith(token, message) {
  view.onclick = view.onsubmit = null; // the account screens' handlers
  if (token) localStorage.setItem("orchestrator_token", token);
  toast(message);
  document.querySelector(".app")?.classList.remove("session-locked");
  endSigningIn();
  await refreshState();
  route();
  let pushToken = null;
  try { pushToken = localStorage.getItem(PUSH_KEY); } catch { /* storage blocked */ }
  if (Account.active() && Notifications.enabled() && !pushToken) enablePush().catch(() => {}); // alerts were on before push existed
}

let accountPoll = null;
function stopAccountPoll() { clearInterval(accountPoll); accountPoll = null; }

// Your computers: open the one you used last (or the only one), otherwise list them. A code from a link is handled first.
async function openAccount({ list = false, message = "", troubleshoot = null } = {}) {
  stopAccountPoll();
  signingIn = true; // keep background refreshes off these screens
  const code = Account.pendingCode();
  if (code) return showPairScreen(code);
  showSigningIn("Finding your computers…");
  let machines;
  try {
    ({ machines } = await cpApi("machines"));
  } catch (err) {
    if (err.status === 401) { endSigningIn(); return showSignInGate("Your sign-in expired. Sign in again."); }
    return showAccountScreen([], err.message, troubleshoot);
  }
  let remembered = null;
  try { remembered = localStorage.getItem(Account.MACHINE_KEY); } catch { /* storage blocked */ }
  const pick = !list && !message && !troubleshoot && Account.pickMachine(machines, remembered);
  if (pick) return enterMachine(pick);
  showAccountScreen(machines, message, troubleshoot);
}

function showAccountScreen(machines, message = "", troubleshoot = null) {
  stopAccountPoll();
  signingIn = true;
  setHeader({ title: "Your computers", sub: "", actions: "" });
  document.querySelector(".app")?.classList.add("session-locked");
  const email = window.firebase?.auth?.().currentUser?.email || "";
  const render = (list, msg, tb) => {
    if (view.contains(document.activeElement) && document.activeElement.matches("input")) return; // not while typing a code
    view.innerHTML = Account.renderMachines(list, { email, message: msg, troubleshoot: tb });
  };
  render(machines, message, troubleshoot);
  // Watch for a computer coming online (someone just ran `orchestrator ui` on it).
  let last = JSON.stringify(machines);
  accountPoll = setInterval(async () => {
    if (!document.querySelector(".account-card")) return stopAccountPoll();
    try {
      const { machines: fresh } = await cpApi("machines");
      const now = JSON.stringify(fresh);
      if (now !== last) { last = now; machines = fresh; render(fresh, message, troubleshoot); }
    } catch { /* keep the last list */ }
  }, 5000);

  view.onsubmit = async (event) => {
    const idea = event.target.closest("[data-account-form=idea]");
    if (idea) {
      event.preventDefault();
      Account.saveIdea({ pitch: idea.pitch.value, name: idea.name.value });
      return openAccount({ list: true });
    }
    const enroll = event.target.closest("[data-account-form=enroll]");
    if (enroll) {
      event.preventDefault();
      return makeEnrollCommand(enroll);
    }
    const form = event.target.closest("[data-account-form=code]");
    if (!form) return;
    event.preventDefault();
    const code = Account.normalizeCode(form.code.value);
    if (code.length !== Account.CODE_LENGTH) return toast(`The code has ${Account.CODE_LENGTH} letters and numbers, like ABCD-2345.`, "warning");
    showPairScreen(code);
  };
  view.onclick = async (event) => {
    const button = event.target.closest("[data-account-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.accountAction;
    if (action === "open") {
      const machine = machines.find((m) => m.id === button.dataset.id);
      if (machine) enterMachine(machine);
    } else if (action === "remove") {
      if (!await formDialog(`Remove ${button.dataset.name}?`, `<p>It leaves your account. Add it again any time with <code>orchestrator connect</code> on that computer.</p>`, "Remove", { danger: true })) return;
      button.disabled = true;
      try {
        await cpApi("machines/remove", { method: "POST", body: { machine_id: button.dataset.id } });
        toast(`Removed ${button.dataset.name}`);
        openAccount({ list: true });
      } catch (err) { toast(err.message, true); button.disabled = false; }
    } else if (action === "reset-tunnel") {
      button.disabled = true;
      try {
        await cpApi("machines/reset-tunnel", { method: "POST", body: { machine_id: button.dataset.id } });
        toast(`Tunnel reset requested for ${button.dataset.name}. It will restart on its next heartbeat.`);
        openAccount({ list: true });
      } catch (err) { toast(err.message, true); button.disabled = false; }
    } else if (action === "copy-cmd") {
      const cmd = button.dataset.cmd || "orchestrator ui --tunnel";
      try { await navigator.clipboard.writeText(cmd); toast("Copied command"); } catch { toast(cmd, "info"); }
    } else if (action === "update") {
      button.disabled = true;
      try {
        await cpApi("machines/update", { method: "POST", body: { machine_id: button.dataset.id } });
        toast(`${button.dataset.name} will update once no work is running on it.`);
        openAccount({ list: true });
      } catch (err) { toast(err.message, true); button.disabled = false; }
    } else if (action === "idea-existing") {
      Account.saveIdea({ existing: true });
      openAccount({ list: true });
    } else if (action === "idea-change") {
      Account.clearIdea();
      openAccount({ list: true });
    } else if (action === "copy-enroll") {
      const command = view.querySelector("[data-enroll-command]")?.textContent || "";
      try { await navigator.clipboard.writeText(command); toast("Copied"); } catch { toast("Select the command and copy it.", "warning"); }
    } else if (action === "refresh") {
      openAccount({ list: true });
    } else if (action === "sign-out") {
      stopAccountPoll();
      endSigningIn();
      lockSession();
    }
  };
}

// The token is shown once and kept only in the page; a new visit makes a new one.
let enrollTimer = null;
async function makeEnrollCommand(form) {
  const button = form.querySelector("button[type=submit]");
  const result = form.parentElement.querySelector("[data-enroll-result]");
  button.disabled = true;
  try {
    const created = await cpApi("enroll/create", { method: "POST", body: {} });
    const command = Account.enrollCommand(created.token, form.project.value.trim(), location.origin);
    const deadline = Date.now() + created.expires_in * 1000;
    clearInterval(enrollTimer);
    const draw = () => {
      const left = Math.round((deadline - Date.now()) / 1000);
      if (!result.isConnected) return clearInterval(enrollTimer);
      result.innerHTML = Account.renderEnroll(command, left);
      if (left <= 0) clearInterval(enrollTimer);
    };
    draw();
    enrollTimer = setInterval(draw, 30000);
  } catch (err) { toast(err.message, true); }
  button.disabled = false;
}

async function showPairScreen(code) {
  stopAccountPoll();
  signingIn = true;
  setHeader({ title: "Add a computer", sub: "", actions: "" });
  document.querySelector(".app")?.classList.add("session-locked");
  const done = (message) => { Account.clearPendingCode(); if (location.hash.startsWith("#/connect")) history.replaceState(null, "", "#/"); openAccount({ list: true, message }); };
  let preview;
  try {
    preview = await cpApi("pair/preview", { method: "POST", body: { code } });
  } catch (err) {
    if (err.status === 401) { endSigningIn(); return showSignInGate("Sign in to add your computer to your account."); }
    return done(err.message);
  }
  const email = window.firebase?.auth?.().currentUser?.email || "";
  view.innerHTML = Account.renderPair(preview, code, { email });
  view.onsubmit = null;
  view.onclick = async (event) => {
    const button = event.target.closest("[data-account-action]");
    if (!button || button.disabled) return;
    if (button.dataset.accountAction === "cancel-pair") return done("");
    if (button.dataset.accountAction !== "claim") return;
    button.disabled = true;
    try {
      const { machine } = await cpApi("pair/claim", { method: "POST", body: { code } });
      toast(`Added ${machine.name}`);
      done(`${machine.name} is on your account. Start \`orchestrator ui --tunnel\` on it and it will show as online here.`);
    } catch (err) { done(err.message); }
  };
}

// Open one computer: the control plane signs a ticket only it accepts, and it trades that for a sign-in of its own.
async function enterMachine(machine) {
  stopAccountPoll();
  signingIn = true;
  showSigningIn(`Opening ${machine.name}…`);
  let endpoint = "";
  try {
    const resTicket = await cpApi("machine/ticket", { method: "POST", body: { machine_id: machine.id } });
    endpoint = resTicket.endpoint;
    const ticket = resTicket.ticket;
    localStorage.setItem("orchestrator_backend", endpoint);
    localStorage.setItem(Account.MACHINE_KEY, machine.id);
    localStorage.removeItem("orchestrator_token");
    let res;
    try {
      res = await api("auth", { method: "POST", body: { ticket } });
    } catch (err) {
      throw new Error(`Couldn't reach ${machine.name} at ${endpoint} (${err.message}). Check that \`orchestrator ui --tunnel\` is still running on it.`);
    }
    // An idea from before this computer was set up: go and start it (or add the code they already have).
    const idea = Account.pendingIdea();
    if (idea) {
      location.hash = Account.ideaRoute(idea);
      if (idea.existing) Account.clearIdea();
    }
    await unlockWith(res.token, `Opened ${machine.name}`);
  } catch (err) {
    openAccount({ list: true, message: err.message, troubleshoot: { machine, endpoint, error: err.message } });
  }
}

// The hosted app reaches a computer at the address its account last heard. That address can change (a quick tunnel gets a
// new one each time it restarts), so when calls keep failing, ask the account where the computer is now and move there.
let relocating = false;
let lastRelocateTry = 0;
async function relocateMachine() {
  if (relocating || signingIn || !Account.active() || Date.now() - lastRelocateTry < 10000) return;
  let remembered = null;
  try { remembered = localStorage.getItem(Account.MACHINE_KEY); } catch { return; }
  relocating = true;
  lastRelocateTry = Date.now();
  try {
    const { machines } = await cpApi("machines");
    const machine = (machines || []).find((m) => m.id === remembered) || Account.pickMachine(machines, remembered);
    if (!machine) {
      if (!state.project) openAccount({ list: true });
      return;
    }
    const endpoint = (machine?.endpoint || "").replace(/\/+$/, "");
    if (!machine?.reachable || !endpoint) {
      if (!state.project) {
        showAccountScreen(machines, `${machine.name} is currently offline. Start \`orchestrator ui --tunnel\` on it.`);
      }
      return;
    }
    if (endpoint !== getBackendUrl() || !state.project || !getToken()) {
      ConnectionLog.add("relocated", { backend: new URL(endpoint).host });
      await enterMachine(machine);
    }
  } catch (err) {
    if (!state.project) {
      openAccount({ list: true, message: err.message });
    }
  } finally {
    relocating = false;
  }
}

// The hosted app remembers your Google sign-in, so coming back opens your computer without the sign-in screen.
if (typeof window !== "undefined" && window.firebase?.auth && Account.active()) {
  try {
    window.firebase.auth().onAuthStateChanged(async (user) => {
      authResolved = true;
      authUser = user;
      if (user) {
        if (!state.project && !signingIn) {
          openAccount().catch(() => {});
        }
      } else {
        if (!getToken() && !state.project) {
          showSignInGate();
        }
      }
    });
  } catch { /* auth unavailable: the sign-in screen still works */ }
}

function showSignInGate(message) {
  setHeader({ title: "Sign In", sub: "", actions: "" });
  document.querySelector(".app")?.classList.add("session-locked");
  const backend = getBackendUrl() || "";
  const token = getToken() || "";
  const isRemote = window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost";
  const hosted = Account.active();
  const hostedUser = hosted ? window.firebase?.auth?.().currentUser : null;
  const providers = Account.providerSignInWorks();
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
            ${message ? `<span style="color: var(--bad);">${esc(message)}</span>` : "Sign in to access and manage projects on your computer."}
          </p>
        </div>
        ${providers ? "" : `<div class="notice signin-elsewhere">
          <p>Google, Apple and GitHub sign-in work on the Orchestrator site, which then opens this computer for you.</p>
          <a class="btn primary" href="${esc(Account.HOSTED_ORIGINS[0])}" style="width: 100%; justify-content: center;">Sign in on the Orchestrator site</a>
          <p class="muted">Or use the access token below${location.hostname === "127.0.0.1" ? `, or open <a href="${esc(location.href.replace("//127.0.0.1", "//localhost"))}">localhost</a> instead of 127.0.0.1` : ""}.</p></div>`}
        <div class="sso-buttons" ${providers ? "" : "hidden"}>
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
        ${hostedUser ? `<button type="button" class="btn primary" id="account-continue-btn" style="width: 100%; justify-content: center;">Continue as ${esc(hostedUser.email || "you")}</button>` : ""}
        ${isRemote && !hosted ? `
        <div class="backend-config-card" style="margin-top: 0.75rem; padding: 0.65rem 0.85rem; background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; font-size: 0.82rem; text-align: left;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.35rem;">
            <span style="font-weight: 600; color: var(--text);">Mac Backend URL:</span>
            <span style="font-size: 0.75rem; color: ${backend ? "var(--ok)" : "var(--warn, #d99a00)"};">${backend ? "Configured" : "Required for phone"}</span>
          </div>
          <input type="url" id="signin-backend" name="backend" placeholder="e.g. https://...trycloudflare.com or Tailscale" value="${esc(backend)}" style="width: 100%; box-sizing: border-box; font-size: 0.85rem; padding: 0.4rem 0.6rem; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: var(--text);">
          <small class="muted" style="display: block; margin-top: 0.35rem; line-height: 1.3;">Run <code>orchestrator ui --tunnel</code> on your Mac to generate an HTTPS URL for your phone.</small>
        </div>
        ` : ""}
        ${blockedProvider ? `<p class="muted signin-fallback">Pop-ups still blocked? <button type="button" class="linklike" id="redirect-fallback">Try full-page ${esc(blockedProvider.name)} sign-in</button>. Some browsers can't finish this way; the access token below always works.</p>` : ""}
        <details class="manual-token-details" ${hosted ? "" : "open"} style="font-size: 0.82rem; margin-top: 0.5rem; border-top: 1px solid var(--border); padding-top: 0.75rem;">
          <summary class="muted" style="cursor: pointer; user-select: none; text-align: center; font-weight: 500;">${hosted ? "Connect by address and access token" : "Sign in with CLI access token"}</summary>
          <form id="signin-form" class="stack" style="display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.75rem;">
            <label class="field">
              <span>CLI Access Token</span>
              <input type="password" id="signin-token" name="token" value="${esc(token)}" placeholder="Paste access token from terminal" autocomplete="current-password" style="font-family: var(--mono); font-size: 0.9rem;">
            </label>
            ${!isRemote || hosted ? `
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

  $("#account-continue-btn")?.addEventListener("click", () => openAccount());

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
        toast("Firebase Auth is loading or unavailable", "warning");
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
        await afterProviderSignIn(cred.user, name);
      } catch (err) {
        endSigningIn();
        if (err.code === "auth/popup-closed-by-user" || err.code === "auth/cancelled-popup-request") { btn.disabled = false; btn.innerHTML = originalHtml; return; }
        const msg = explain[err.code] || (err.code === "auth/unauthorized-domain"
          ? `${name} sign-in doesn't work at this address. Sign in on the Orchestrator site (${Account.HOSTED_ORIGINS[0]}), which opens this computer for you, or use the access token below.`
          : err.message);
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
  if (Account.active() && window.firebase?.auth?.().currentUser) {
    relocateMachine().catch(() => openAccount().catch(() => showSignInGate(message)));
    return;
  }
  showSignInGate(message);
}

async function lockSession() {
  try {
    await api("auth/logout", { method: "POST", body: {} });
  } catch {}
  await disablePush(); // a signed-out device stops getting this account's alerts
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
  for (const badge of document.querySelectorAll("#running-badge, .tabbar .badge")) badge.hidden = true;
  toast("Session locked", "info");
  showSignInGate("Session locked. Please sign in again.");
}

// `titleHtml` replaces the plain title with markup (Home's project switcher); `title` stays the page's name for the
// browser tab and anything else that asks what page this is (#page-title's data-title).
function setHeader({ title, titleHtml = "", sub = "", actions = "" }) {
  document.body.dataset.page = current.page || "";
  const back = $("#back-link");
  if (back) { back.hidden = !current.parent; if (current.parent) back.setAttribute("href", current.parent); }
  const heading = $("#page-title");
  if (titleHtml) heading.innerHTML = titleHtml;
  else heading.textContent = title;
  heading.dataset.title = title;
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



// Job list sorting (tap Status / Last modified; tap again to reverse).
const GROUP_ORDER = { needs_you: 0, working: 1, done: 2 };
let jobSort = { key: "status", dir: "asc" };
try { jobSort = JSON.parse(localStorage.getItem("orchestrator_job_sort")) || jobSort; } catch {}

function sortJobs(jobs) {
  const { key, dir } = jobSort;
  const val = (j) => key === "status"
    ? [j.active_run ? 1 : (GROUP_ORDER[j.state?.group] ?? 3), j.state?.label || ""]
    : key === "type"
      ? [j.kind || j.type || ""]
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
    j.branch ? `<span class="mono">${esc(j.branch)}</span>` : "",
    j.tasks_total ? `${j.tasks_done}/${j.tasks_total} tasks` : "",
  ].filter(Boolean);

  // The status pill already says what the job needs; its longer reason is on the job page.
  const metaHtml = parts.join(" · ");
  const typeLabel = esc(j.kind || j.type || "Job");

  return `
    <a class="job-table-row"${hidden ? " hidden" : ""} href="#/jobs/${encodeURIComponent(j.id)}">
      <div class="col-job">
        <div class="job-title">${esc(j.title)}</div>
        <div class="job-mobile-info">
          <span class="job-type-tag">${typeLabel}</span>
          ${jobPill(j)}
        </div>
        ${metaHtml ? `<div class="job-meta">${metaHtml}</div>` : ""}
      </div>
      <div class="col-type">
        <span class="job-type-tag">${typeLabel}</span>
      </div>
      <div class="col-status">
        ${jobPill(j)}
      </div>
      <div class="col-date" title="${esc(new Date(j.updated * 1000).toLocaleString())}">
        <span class="date-relative">${esc(ago(j.updated))}</span>
      </div>
      <div class="row-hover-hint">
        <span>Click for details</span>
        <svg class="icon icon-sm" aria-hidden="true"><use href="#i-chevron"/></svg>
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
    ? `<select class="branch-select mono" aria-label="Switch branch" title="Switch branch">${p.branch ? "" : `<option selected disabled>no branch</option>`}${branches.map((b) => branchOption(b, p.branch, p.elsewhere)).join("")}</select>`
    : `<a class="mono" href="#/git">${esc(p.branch || "no branch")}</a>`;
  // Only what needs a glance: work not yet committed, work running, or a missing machine or model. The language mix
  // is on the project's card under Projects.
  const stats = [
    p.dirty_files ? `<a href="#/git">${p.dirty_files} uncommitted</a>` : "",
    running ? `<a href="#/activity">${running} running</a>` : "",
    p.machine_count === 0 ? `<a class="warn-link" href="#/config/fleet">No machine set up: jobs can't run yet</a>`
      : p.machine_count && p.model_count === 0 ? `<a class="warn-link" href="#/config/models">No model selected: jobs can't run yet</a>` : "",
  ].filter(Boolean);
  // Row 1: the branch. Row 2: the stats, when there are any.
  return `<span class="status-line status-stack"><span class="status-branch"><span class="label">Branch</span>${branchPicker}</span>
    ${stats.length ? `<span class="status-stats">${stats.join(`<span class="sep">·</span>`)}</span>` : ""}</span>`;
}

// A branch checked out in another worktree folder is labelled: git won't switch to it here.
function branchOption(b, current, elsewhere = {}) {
  return `<option value="${esc(b)}" ${b === current ? "selected" : ""}>${esc(b)}${elsewhere?.[b] ? " (in another folder)" : ""}</option>`;
}

// git refuses to switch when uncommitted changes would be overwritten, so offer to set them aside.
async function switchBranch(branch) {
  const folder = state.project?.elsewhere?.[branch];
  if (folder) {  // git keeps a branch in one place at a time: say where it is instead of failing
    await formDialog(`${branch} is open in another folder`, `<p>It's checked out in <code>${esc(folder)}</code>, and git keeps a branch in one place at a time.
      Work on it in that folder, or choose another branch here.</p>
      <div class="setup-hint"><code>${esc(folder)}</code><button type="button" class="btn small ghost" data-setup-copy="${esc(folder)}">Copy</button></div>
      <p class="muted">Looking for another project? Projects are listed under <a href="#/projects">Projects</a>; branches are versions of this one.</p>`, "OK");
    return;
  }
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
  const actionAllowed = !i.action || i.action.type !== "run" || ConfigurationPages.canRunAction(i.action.action, state);
  const go = i.action && actionAllowed ? (i.action.label || (i.done ? "Open" : i.action.type === "run" ? "Set up" : "Go")) : "";
  return `<div class="setup-item ${i.done ? "is-done" : ""}">
    ${icon}
    <div class="main-col"><div class="title">${esc(i.title)}</div><div class="meta">${esc(i.detail)}</div>
      ${!i.done && i.hint ? `<div class="setup-hint"><code>${esc(i.hint)}</code><button class="btn small ghost" data-setup-copy="${esc(i.hint)}">Copy</button></div>` : ""}</div>
    ${go ? `<button class="btn small ${i.done ? "ghost" : "primary"}" data-setup-go="${esc(i.id)}">${esc(go)}</button>` : ""}</div>`;
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
  const navItem = $("#nav-readiness"); // the sidebar's way back to it, until everything required is done
  if (navItem) {
    navItem.hidden = !s || s.complete;
    if (s) $("#setup-badge").textContent = String(s.required_total - s.required_done);
  }
  if ($("#setup-panel") && !$("#setup-panel").hidden) renderSetupPanel();
}

function renderSetupPanel() {
  const s = setupState, panel = $("#setup-panel");
  if (!s) return;
  panel.innerHTML = `<div class="setup-panel-h"><strong>Setup</strong><span class="muted">${s.required_done}/${s.required_total} required</span>
    <button class="btn small ghost" data-setup-close aria-label="Close">✕</button></div><div class="setup-panel-b">${setupListHtml(s)}
    <a class="btn small ghost setup-panel-more" href="#/readiness" data-setup-close>Open Readiness: build tools and checks</a></div>`;
}

async function openSetupPanel() {
  const panel = $("#setup-panel");
  await loadSetup(true);
  if (!setupState) return toast("The setup checklist isn't available yet. It will appear after your next Orchestrator update.", "info");
  renderSetupPanel();
  panel.hidden = false;
}
const closeSetupPanel = () => { const p = $("#setup-panel"); if (p) p.hidden = true; };

document.addEventListener("click", async (e) => {
  if (e.target.closest("#setup-fab, [data-setup-open]")) { $("#setup-panel").hidden ? openSetupPanel() : closeSetupPanel(); return; }
  if (e.target.closest("[data-setup-close]")) { closeSetupPanel(); return; }
  const copy = e.target.closest("[data-setup-copy]");
  if (copy) { navigator.clipboard?.writeText(copy.dataset.setupCopy).then(() => toast("Copied")).catch(() => {}); return; }
  const go = e.target.closest("[data-setup-go]");
  if (go) {
    const item = setupState?.items.find((i) => i.id === go.dataset.setupGo);
    if (!item?.action) return;
    if (item.action.type === "route") {
      closeSetupPanel();
      location.hash = item.action.to;
    } else if (item.action.type === "github") {
      closeSetupPanel();
      signInToGitHub();
    } else if (item.action.type === "run") {
      closeSetupPanel();
      runAction(item.action.action, item.action.params || {});
    } else if (item.action.type === "git_init") {
      go.disabled = true;
      try {
        await api("setup/git-init", { method: "POST", body: {} });
        toast("Initialized git repository");
        await loadState();
        await loadSetup(true);
        if ($("#setup-panel") && !$("#setup-panel").hidden) renderSetupPanel();
        route();
      } catch (err) {
        toast(err.message, true);
      } finally {
        go.disabled = false;
      }
    } else if (item.action.type === "github_create") {
      const projectName = (state?.project?.name || "").trim() || "";
      const values = await formDialog("Create GitHub repository", `
        <p class="muted">Create a new repository on GitHub and connect it as the remote origin.</p>
        <label class="field"><span>Repository name</span>
          <input type="text" name="name" value="${esc(projectName)}" placeholder="my-project" maxlength="80">
        </label>
        <label class="field"><span>Visibility</span>
          <select name="visibility">
            <option value="private" selected>Private</option>
            <option value="public">Public</option>
          </select>
        </label>
      `, "Create repository");
      if (!values) return;
      go.disabled = true;
      try {
        const res = await api("setup/github-create", { method: "POST", body: values });
        if (res.step && !res.step.ok) {
          toast(res.step.detail || "Could not create GitHub repo", true);
        } else {
          toast("Created repository on GitHub");
        }
        await loadState();
        await loadSetup(true);
        if ($("#setup-panel") && !$("#setup-panel").hidden) renderSetupPanel();
        route();
      } catch (err) {
        toast(err.message, true);
      } finally {
        go.disabled = false;
      }
    }
  }
});

const HOME_JOBS_SHOWN = 10;
const STALE_DAYS = 14;
const isStaleJob = (j) => j.state?.group === "needs_you" && !j.active_run && j.updated && Date.now() / 1000 - j.updated > STALE_DAYS * 86400;

// Archive keeps the branch and files; Home's Archived filter (or Undo here) brings a job back as it was.
document.addEventListener("click", async (e) => {
  const button = e.target.closest("[data-archive-jobs]");
  if (!button) return;
  const ids = JSON.parse(button.dataset.archiveJobs);
  button.disabled = true;
  try {
    const { archived } = await api("jobs/archive", { method: "POST", body: { ids } });
    if (current.page === "job") location.hash = "#/"; else route();
    toast(`Archived ${archived.length === 1 ? "1 job" : `${archived.length} jobs`}`, false, { label: "Undo", run: async () => {
      for (const id of archived) await api("config/archived-restore", { method: "POST", body: { id } });
      route();
    } });
  } catch (err) { button.disabled = false; toast(err.message, true); }
});

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
  if (!HOME_FILTERS[filter] && filter !== "archived") filter = "all";
  // Archived jobs are put away, not in the list: this filter reads them separately, each with Restore.
  const archived = filter === "archived" ? ((await api("config").catch(() => null))?.archived || []) : null;
  const filteredJobs = archived ? [] : sortedJobs.filter(HOME_FILTERS[filter][1]);
  // Jobs that have waited on you for weeks fold into one line you can clear, so the list shows what's current.
  const staleJobs = filteredJobs.filter(isStaleJob);
  const currentJobs = filteredJobs.filter((j) => !isStaleJob(j));

  const runningRuns = state.runs.filter((r) => r.running);
  const runningBanner = runningRuns.length
    ? `<section class="card mb-16"><div class="card-h"><h2>Running now</h2><span class="count">${runningRuns.length}</span></div>
        <div class="list">${runningRuns.map(liveRunCard).join("")}</div></section>`
    : "";

  return {
    title: p.name, // switching projects lives in the menu's project picker and on Projects
    sub: statusLine(p),
    actions: `<a class="btn primary home-new-job-btn" href="#/new"><svg class="icon" aria-hidden="true"><use href="#i-plus"/></svg>New job</a>`,
    html: `
      ${product ? productStripHtml(product) : ""}

      ${runningBanner}

      <section class="card">
        <div class="card-h card-h-wrap">
          <h2>Jobs <span class="count">${archived ? archived.length : filteredJobs.length}</span></h2>
          <div class="filters">
            ${Object.entries(HOME_FILTERS).map(([key, [label, fn]]) => {
              const count = sortedJobs.filter(fn).length;
              return `<a class="btn small ${key === filter ? 'on' : ''}" href="#/?filter=${key}">${label} (${count})</a>`;
            }).join("")}
            <a class="btn small ${archived ? "on" : ""}" href="#/?filter=archived">Archived${archived ? ` (${archived.length})` : ""}</a>
          </div>
        </div>
        ${archived ? (archived.length ? `<div class="list">${archived.map((j) => `<div class="item"><div class="main-col"><div class="title">${esc(j.title)}</div>
            <div class="meta"><span class="mono">${esc(j.job_id)}</span> · ${esc(j.status)}${j.corrupt ? " · this archive can't be read" : ""}</div></div>
            <div class="side">${j.corrupt ? "" : `<button type="button" class="btn small" data-restore-job="${esc(j.id)}">Restore</button>`}</div></div>`).join("")}</div>`
          : `<div class="empty">No archived jobs. Archive a finished job from its More menu; it comes back here with Restore.</div>`)
        : filteredJobs.length ? `
          <div class="job-table">
            <div class="job-table-header">
              <div class="col-job">Job</div>
              <button type="button" class="col-type sort-head" data-sort="type" aria-label="Sort by type">Type${sortArrow("type")}</button>
              <button type="button" class="col-status sort-head" data-sort="status" aria-label="Sort by status">Status${sortArrow("status")}</button>
              <button type="button" class="col-date sort-head" data-sort="updated" aria-label="Sort by last modified"><span class="date-header-long">Last modified</span><span class="date-header-short">Date</span>${sortArrow("updated")}</button>
            </div>
            <div class="job-table-body" id="job-list">
              ${currentJobs.map((j, n) => jobTableRow(j, { hidden: n >= HOME_JOBS_SHOWN })).join("")}
            </div>
            ${currentJobs.length > HOME_JOBS_SHOWN ? `<div class="card-b"><button type="button" class="btn small" data-show-all="job-list">Show all ${currentJobs.length}</button></div>` : ""}
            ${staleJobs.length ? `
              <div class="stale-jobs">
                <p class="stale-jobs-text">${staleJobs.length === 1 ? "1 job hasn't" : `${staleJobs.length} jobs haven't`} changed in over ${STALE_DAYS} days.</p>
                <button type="button" class="btn small" data-show-all="stale-list">Show</button>
                <button type="button" class="btn small" data-archive-jobs='${attrJSON(staleJobs.map((j) => j.id))}'>Archive ${staleJobs.length === 1 ? "it" : "them"}</button>
              </div>
              <div class="job-table-body" id="stale-list">${staleJobs.map((j) => jobTableRow(j, { hidden: true })).join("")}</div>` : ""}
          </div>
        ` : `<div class="empty">${!jobs.length ? 'No jobs yet. <strong>New job</strong> plans work from a description, a bug report or a design.' : 'No jobs match this filter.'}</div>`}
        ${elsewhereLine(waiting.elsewhere)}
      </section>`,
    after: () => {
      view.querySelectorAll("[data-restore-job]").forEach((b) => b.addEventListener("click", async () => {
        b.disabled = true;
        try { await api("config/archived-restore", { method: "POST", body: { id: b.dataset.restoreJob } }); toast("Job restored"); route(); }
        catch (e) { b.disabled = false; toast(e.message, true); }
      }));
    },
  };
};

function jobHeaderActions(s, links = [], ctx = {}) {
  // Grouped by what the person wants (change the job, share it, open something, adjust settings), only what fits the
  // job's stage, one line each; ending the job comes last and apart. The hero's primary action is never repeated here.
  const j = { job: s.id };
  const next = s.state.next?.action;
  const idle = !s.active_run;
  const reviewed = ["review-needed", "completed"].includes(s.status);
  const groups = [
    ["This job", [
      idle && next !== "debug" && ["review-needed", "completed", "debugging", "failed"].includes(s.status)
        && ["Run a fix…", act("debug", j), "Describe what you saw, or leave it blank for another automatic attempt"],
      idle && !reviewed && s.tasks_total && s.tasks_done < s.tasks_total && next !== "resume" && ["Resume", act("resume", j), "Continue from the next task"],
      idle && s.status === "scheduled" && next !== "execute" && ["Run now", act("execute", j)],
      idle && !ctx.planShown && ["planned", "designing", "human-needed", "debugging"].includes(s.status)
        && ["Revise plan…", act("revise", j), "Ask the AI to re-plan with what should change"],
      idle && !reviewed && ctx.canSplinter && ["Split into jobs…", act("splinter_job", j), "Each task becomes its own job, with a GitHub sub-issue"],
      idle && state.project?.mobile_app !== false && ["Check on simulator", act("run_visual_check", j), "Boot the simulator and capture screenshots"],
    ]],
    ["Share", [
      idle && state.you?.role !== "member" && s.branch && ["review-needed", "debugging"].includes(s.status)
        && ["Send to testers…", act("deliver", j), `Builds ${s.branch} and sends it to your testers`],
      ["Export…", act("export_job_bundle", j), "ZIP, iCloud or Google Drive"],
    ]],
    ["Open", [
      ["Documentation", `data-href="#/docs/job/${encodeURIComponent(s.id)}"`, "This job's page in Docs"],
      ...links.map((link) => [`${link.where ? `Open a ${link.label}` : link.label} ↗`, `data-open="${esc(link.url)}"`, link.where || "On GitHub"]),
    ]],
    ["Settings", [
      ["Move to feature…", `data-job-feature="${esc(s.id)}"`, "Group this job under a feature"],
      ["Choose models…", act("select_models", j), "Planner, builder and reviewer"],
    ]],
  ];
  const items = [];
  for (const [title, entries] of groups) {
    const shown = entries.filter(Boolean);
    if (shown.length) items.push(["header", title], ...shown);
  }
  // Ending the job: last, apart, and only when nothing is running.
  const ending = [
    s.issue_number && ["Close issue #" + s.issue_number, `data-action="close_issue" data-params="${esc(JSON.stringify({ job: s.id, issue: s.issue_number }))}"`, "Close it on GitHub and mark this job complete"],
    idle && ["Archive", `data-archive-jobs='${attrJSON([s.id])}'`, "Hide it from Home; branch and files stay. Restore from Configuration."],
    idle && ["Delete…", act("delete_job", j), "Remove the job, and optionally its changes", "danger"],
  ].filter(Boolean);
  if (ending.length) items.push("---", ...ending);
  return moreMenu(items, { compact: true });
}

document.addEventListener("click", async (e) => {
  const mv = e.target.closest("[data-job-feature]");
  if (!mv) return;
  const jobId = mv.dataset.jobFeature;
  try {
    const [{ features }, { job }] = [await api("features"), await api(`jobs/${encodeURIComponent(jobId)}`)];
    if (!features.length) { toast("Create a feature first.", "warning"); return; }
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
  if (!t) return "Tests not run yet";
  const st = (t.status || "").toLowerCase();
  const passed = t.passed_count || 0;
  const failed = t.failed_count || 0;
  if (failed > 0) return `❌ ${failed} failed${passed ? ` (${passed} passed)` : ""}`;
  if (passed > 0) return `✅ ${passed} passed`;
  if (st === "running" || st === "in_progress") return "⚙️ Running tests…";
  if (st === "passed") return "✅ Passed";
  if (st === "failed") return "❌ Failed";
  return "Tests not run yet";
}

pages.job = async ([id]) => {
  const data = await api(`jobs/${encodeURIComponent(id)}`);
  const { summary: s, job, outputs, runs, docs, changes, links, test_summary: testSummary, pipeline: pipe, tasks: jobTasks, completed_tasks: jobCompleted, next_task: jobNextTask } = data;

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

  const kindUpper = (s.type || s.kind || "FEATURE").toUpperCase();
  const testsDisplay = formatJobTests(testSummary);
  const scope = data.scope || null;
  const actionableFindings = (scope?.findings || []).filter((f) => f.id !== "no_plan_files");
  const hasNoPlanFiles = (scope?.findings || []).some((f) => f.id === "no_plan_files");
  const phone = matchMedia("(max-width: 760px)").matches;
  const canEditPlan = Array.isArray(job.plan?.tasks) && !activeRun && !["completed", "archived", "discarded", "decomposed"].includes(s.status) && s.status !== "executing";
  const fold = (title, count, body) => `<section class="card mb-16"><details class="fold" ${phone ? "" : "open"}><summary class="card-h"><h2>${title}</h2>${count === "" ? "" : `<span class="count">${count}</span>`}</summary>${body}</details></section>`;
  const uxWarning = ["merge", "complete"].includes(s.state.next?.action) ? data.ux_review?.counts?.major || 0 : 0;
  const scopeWarning = scope && ["merge", "complete"].includes(s.state.next?.action) ? actionableFindings.reduce((n, f) => n + Math.max(f.files.length, 1), 0) : 0;
  const testCases = data.test_cases || { cases: [], summary: { by_type: {} } };
  const missingTests = testCases.cases.filter((c) => c.due && (c.status === "unassigned" || c.status === "planned")).length;
  let featureName = "";
  if (s.feature) { try { featureName = ((await api("features")).features.find((f) => f.id === s.feature) || {}).name || ""; } catch { /* chip falls back to the id */ } }
  const assumptions = (Array.isArray(job.plan?.assumptions) ? job.plan.assumptions : []).filter((t) => typeof t === "string" && t.trim());
  const conversation = Array.isArray(job.conversation) ? job.conversation.filter((m) => m && m.text) : [];
  const briefDoc = (docs || []).find((d) => d.name === "brief.md" || (d.title && d.title.toLowerCase() === "brief")) || {
    title: "Brief",
    name: "brief.md",
    text: (job.plan?.summary || job.description || job.prompt || "No brief created yet."),
    path: `.orchestrator/output/${s.id}/brief.md`,
    runtime_path: `output/${s.id}/brief.md`,
  };
  const otherDocs = (docs || []).filter((d) => d !== briefDoc && d.name !== "brief.md" && (d.title || "").toLowerCase() !== "brief");

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
    deltaUnsaved = "";
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
    : next ? `<button class="btn primary" ${act(next.action, { job: s.id })}>${esc(next.label)}</button>` : "";

  // 4-Phase Lifecycle determination
  const status = s.status || "";
  let currentPhase = "plan";
  if (status === "completed" || status === "archived") {
    currentPhase = "review";
  } else if (status === "review-needed") {
    currentPhase = "review";
  } else if (status === "debugging" || (tasksTotal > 0 && tasksDone >= tasksTotal && !["planned", "designing", "human-needed"].includes(status))) {
    currentPhase = "verify";
  } else if (status === "scheduled" || status === "executing" || status === "running" || status === "in-progress" || (tasksDone > 0 && tasksDone < tasksTotal)) {
    currentPhase = "build";
  } else {
    currentPhase = "plan";
  }

  const phaseOrder = ["plan", "build", "verify", "review"];
  const currentPhaseIndex = phaseOrder.indexOf(currentPhase);
  const isJobComplete = status === "completed" || status === "archived";

  const getStepState = (idx) => {
    if (isJobComplete || idx < currentPhaseIndex) return { state: "done", icon: "✓" };
    if (idx === currentPhaseIndex) {
      const tone = s.state.tone === "failed" ? "failed" : s.state.group === "needs_you" ? "attention" : "active";
      const icon = s.state.tone === "failed" ? "!" : String(idx + 1);
      return { state: `active ${tone}`, icon };
    }
    return { state: "upcoming", icon: String(idx + 1) };
  };

  const planStep = {
    title: "1. Plan",
    subtitle: isJobComplete || currentPhaseIndex > 0 ? "Approved" : s.state.next?.action === "approve" ? "Needs approval" : status === "human-needed" ? "Clarification" : "Planning",
    ...getStepState(0),
  };

  const buildStep = {
    title: "2. Build",
    subtitle: isJobComplete || currentPhaseIndex > 1 ? `${tasksTotal}/${tasksTotal} done` : currentPhase === "build" ? (activeRun ? `Building ${tasksDone + 1}/${tasksTotal}` : s.state.label === "Paused" ? `Paused · ${tasksDone}/${tasksTotal}` : `${tasksDone}/${tasksTotal} tasks`) : `${tasksTotal || 0} tasks`,
    ...getStepState(1),
  };

  const verifyStep = {
    title: "3. Verify",
    subtitle: isJobComplete || (currentPhaseIndex > 2 && s.state.tone !== "failed") ? "Verified" : status === "debugging" || s.state.tone === "failed" ? "Tests failing" : testSummary?.status === "passed" ? "Passed" : "Automated tests",
    ...getStepState(2),
  };

  const reviewStep = {
    title: "4. Review",
    subtitle: isJobComplete ? "Merged" : status === "review-needed" ? "Ready to merge" : "PR & merge",
    ...getStepState(3),
  };

  const phasesInfo = [planStep, buildStep, verifyStep, reviewStep];


  // Contextual consequence preview for hero
  let nextExecutionPreview = "";
  if (activeRun) {
    nextExecutionPreview = `<span class="job-hero-next-label">Now:</span> working on <strong>${esc(nextTask || "the tasks")}</strong>`;
  } else if (s.state.next?.action === "approve") {
    nextExecutionPreview = `<span class="job-hero-next-label">Then:</span> the AI starts on <strong>${esc(nextTask || "Task 1")}</strong>`;
  } else if (s.state.next?.action === "schedule" || s.state.next?.action === "execute") {
    nextExecutionPreview = `<span class="job-hero-next-label">Then:</span> the AI starts on <strong>${esc(nextTask || "Task 1")}</strong>`;
  } else if (s.state.next?.action === "resume") {
    nextExecutionPreview = `<span class="job-hero-next-label">Then:</span> it picks up at <strong>${esc(nextTask || "the next task")}</strong> (${plural(tasksTotal - tasksDone, "task")} left)`;
  } else if (s.state.next?.action === "merge") {
    nextExecutionPreview = `<span class="job-hero-next-label">Then:</span> the changes go into ${esc(job.base_branch || "main")} and the job is done`;
  } else if (s.state.next?.action === "debug") {
    nextExecutionPreview = `<span class="job-hero-next-label">Then:</span> the AI looks into the failing tests and tries a fix`;
  }

  // Approving (or starting) a plan: the plan is shown right under the decision, with what each task is done when,
  // the files it touches, and what the AI assumed. GitHub puts Approve beside the changes; this is the same idea.
  const nextAction = s.state.next?.action;
  const reviewingPlan = ["approve", "schedule"].includes(nextAction) && s.status !== "designing" && !activeRun && tasks.length > 0;
  const listOf = (v) => (Array.isArray(v) ? v : typeof v === "string" && v.trim().startsWith("[") ? (() => { try { return JSON.parse(v); } catch { return []; } })() : [])
    .filter((x) => typeof x === "string" && x.trim());
  const risks = listOf(job.plan?.risks), constraints = listOf(job.plan?.constraints);
  const planSummary = String(job.plan?.summary || "").trim();
  const showSummary = planSummary && planSummary.toLowerCase() !== "summary";
  const notes = [["What the AI assumed", assumptions], ["Risks", risks], ["Constraints", constraints]].filter(([, list]) => list.length);
  const taskRowsHtml = (withDetail) => tasks.map((t, i) => {
    const title = typeof t === "string" ? t : (t.title || t.name || t.description || `Task ${i + 1}`);
    const detail = typeof t === "object" && t && t.title && t.description ? t.description : "";
    const key = typeof t === "object" && t ? String(t.id ?? i) : String(i);
    const isDone = done.has(key) || done.has(String(i));
    const doneWhen = withDetail && typeof t === "object" ? listOf(t.acceptance_criteria) : [];
    const files = withDetail && typeof t === "object" ? listOf(t.likely_files) : [];
    const controls = canEditPlan && !isDone ? `<div class="side task-controls"><details class="more task-more"><summary class="btn small ghost" aria-label="Task actions: ${esc(title)}">⋯</summary><div class="more-menu">
        <button class="btn small" data-task-op="edit" data-i="${i}">Edit</button>
        <button class="btn small" data-task-op="up" data-i="${i}" ${i === 0 || done.has(String(i - 1)) ? "disabled" : ""}>Move up</button>
        <button class="btn small" data-task-op="down" data-i="${i}" ${i === tasks.length - 1 ? "disabled" : ""}>Move down</button>
        <hr><button class="btn small" data-task-op="remove" data-i="${i}">Remove</button></div></details></div>` : "";
    const undo = data.undoable_task === i && !activeRun ? `<div class="side task-controls"><button class="btn small ghost" data-undo-task="${i}" aria-label="Undo task: ${esc(title)}">Undo</button></div>` : "";
    const marker = `<span class="task-number${isDone ? " is-done" : ""}" role="img" aria-label="${isDone ? "Done" : "To do"}">${isDone ? "✓" : i + 1}</span>`;
    return `<div class="item task-row">${marker}<div class="main-col"><div class="title">${esc(title)}</div>${detail ? `<div class="meta">${esc(detail)}</div>` : ""}
      ${doneWhen.length ? `<div class="task-done-when"><span class="label">Done when</span><ul>${doneWhen.slice(0, 4).map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>` : ""}
      ${files.length ? `<div class="task-files">${files.slice(0, 4).map((f) => `<code>${esc(f)}</code>`).join("")}${files.length > 4 ? `<span class="muted">+${files.length - 4} more</span>` : ""}</div>` : ""}</div>${controls}${undo}</div>`;
  }).join("") || `<div class="empty">No tasks yet.</div>`;
  const planCardHtml = `<section class="card mb-16 plan-review${nextAction === "approve" ? " needs-approval" : ""}" id="plan-section">
      <div class="card-h"><h2>${nextAction === "approve" ? "The plan to approve" : "The plan"} <span class="count">${plural(tasks.length, "task")}</span></h2>
        <div class="card-actions"><button class="btn small" ${act("revise", { job: s.id })} title="Ask the AI to re-plan with what should change">Revise…</button>${next ? `<button class="btn small primary" ${act(next.action, { job: s.id })}>${esc(next.label)}</button>` : ""}</div></div>
      ${showSummary ? `<div class="card-b plan-summary">${esc(planSummary)}</div>` : ""}
      ${notes.length ? `<details class="fold plan-notes"><summary class="card-h"><h2>Assumptions and risks <span class="count">${notes.reduce((n, [, l]) => n + l.length, 0)}</span></h2></summary>
        <div class="card-b stack">${notes.map(([label, list]) => `<div><div class="label">${label}</div><ul class="assumptions">${list.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>`).join("")}</div></details>` : ""}
      <div class="list">${taskRowsHtml(true)}</div>
      ${canEditPlan ? `<div class="card-b card-b-split"><button class="btn small" data-task-op="add">Add a task</button></div>` : ""}
    </section>`;
  const architectConcerns = s.question && /architect/i.test(s.question) && /architect/i.test(s.state.reason || "");
  const heroReason = architectConcerns ? "The architect raised concerns (below). Answer them, or accept its suggestions and re-plan."
    : reviewingPlan ? `Read <button type="button" class="linklike" data-scroll-to="#plan-section">the plan below</button>: ${plural(tasks.length, "task")}, each with what "done" means and the files it touches. ${nextAction === "approve" ? "Approve it, or revise it if something's off." : "Start when you're happy with it."}`
    : s.status === "designing" && nextAction === "approve" ? `Read <button type="button" class="linklike" data-scroll-to="#brief-section">the design below</button>, then approve it or ask for changes.`
    : clamped(s.state.reason || "", 320);

  return {
    title: s.title,
    sub: `<span class="status-line"><span>${esc(s.kind || "Job")}</span>${s.issue_number ? `<span class="sep">·</span><span>#${esc(s.issue_number)}</span>` : ""}${s.feature ? `<span class="sep">·</span><span>${esc(featureName || s.feature)}</span>` : ""}${s.branch ? `<span class="sep">·</span><span class="mono">${esc(s.branch)}</span>` : ""}${(data.context?.ticket || []).map((t) => `<span class="sep">·</span>${t.url
      ? `<a class="ticket-chip" href="${esc(t.url)}" target="_blank" rel="noopener" title="${esc(`${t.source}: ${t.title}`)}">${esc(t.ref || t.title)} ↗</a>`
      : `<span class="ticket-chip" title="${esc(t.source)}">${esc(t.ref || t.title)}</span>`}`).join("")}</span>`,
    actions: jobHeaderActions(summary, links, { canSplinter, planShown: reviewingPlan }),
    html: `<div class="job-layout"><div class="job-top">
      ${runs.filter((r) => r.running).map((r) => `<section class="card mb-16"><div class="list">${liveRunCard(r)}</div></section>`).join("")}

      <nav class="job-stepper" aria-label="Job lifecycle progress">
        ${phasesInfo.map((p, idx) => `
          <div class="job-step ${p.state}">
            <div class="job-step-dot" aria-hidden="true">${p.icon}</div>
            <div class="job-step-meta">
              <span class="job-step-title">${esc(p.title)}</span>
              <span class="job-step-sub">${esc(p.subtitle)}</span>
            </div>
          </div>
          ${idx < phasesInfo.length - 1 ? `<div class="job-step-arrow" aria-hidden="true">→</div>` : ""}
        `).join("")}
      </nav>

      ${reviewingPlan ? "" : `<section class="job-hero tone-${esc(s.state.tone || "")}">
        <div class="job-hero-main">
          <h2 class="job-hero-title">${esc(s.state.label || s.status)}</h2>
          <p class="job-hero-reason">${heroReason}</p>
          ${nextExecutionPreview ? `<div class="job-hero-next-preview">${nextExecutionPreview}</div>` : ""}
          ${uxWarning ? `<p class="job-hero-reason"><button type="button" class="linklike" data-scroll-to="#ux-section">UX and design check: ${plural(uxWarning, "major finding")}. Look before you ${s.state.next?.action === "merge" ? "merge" : "finish"}.</button></p>` : ""}
          ${scopeWarning ? `<p class="job-hero-reason"><button type="button" class="linklike" data-scroll-to="#scope-section">Scope check: ${scopeWarning} beyond the plan. Look before you ${s.state.next?.action === "merge" ? "merge" : "finish"}.</button></p>` : ""}
        </div>
        <div class="job-hero-action">${heroAction}</div>
      </section>`}

      ${s.question ? `<section class="card mb-16"><div class="card-h"><h2>Question from the planner</h2></div><div class="card-b stack">
        <p class="question">${clamped(s.question, 320)}</p>${s.state.next?.action === "answer" ? "" : `<div><button class="btn" ${act("answer", { job: s.id })}>Answer</button></div>`}</div></section>` : ""}

      ${(data.blockers || []).map((b) => `<div class="banner attention"><p><strong>This job can't start yet.</strong> ${esc(b.text)}${b.route ? ` <a class="btn small" href="${esc(b.route)}">${esc(b.fix)}</a>` : ""}</p></div>`).join("")}

      ${(job.plan?.slice_warnings || []).length ? `<section class="card mb-16"><div class="card-h"><h2>Plan check</h2></div><div class="card-b stack">
        <div>This plan looks like a stack of layers, not working slices. Mistakes in an early layer won't show until the last task.</div>
        <ul class="assumptions">${job.plan.slice_warnings.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
        <div class="muted">It was already sent back once. Use Revise plan in the More menu and ask for working slices.</div></div></section>` : ""}
      ${reviewingPlan ? planCardHtml : ""}
      </div>

      ${jobContextRail(s, data.context)}

      <div class="job-body">
      <section class="card mb-16">
        <div class="card-h"><h2>Progress</h2>${tasksTotal ? `<span class="count">${tasksDone}/${tasksTotal} tasks</span>` : ""}</div>
        <div class="card-b stack">
          ${tasksTotal ? `<div class="tasks-progress-wrap">
            <div class="progress-bar-container" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${tasksPct}"><div class="progress-bar-fill" style="width: ${tasksPct}%"></div></div>
            <span class="progress-text">${tasksPct}%</span>
          </div>
          <div class="progress-now">
            ${activeRun
              ? `<span class="pill working">Building now</span><strong>${esc(nextTask || "Active task")}</strong>`
              : nextTask
                ? `<span class="label">Next</span><strong>${esc(nextTask)}</strong><span class="muted">${tasksTotal - tasksDone} left</span>`
                : tasksDone === tasksTotal
                  ? `<span class="pill done">All tasks built</span><span class="muted">Ready for verification &amp; review</span>`
                  : ""}
          </div>` : `<div class="empty-state"><strong>No tasks yet</strong><span class="muted">Once this job has a plan, its tasks and your progress through them show up here.</span></div>`}
          <div class="progress-foot muted">
            ${esc(testsDisplay)}${pipe?.planner || pipe?.builder || pipe?.reviewer ? ` · ${esc(pipeline.planner)} → ${esc(pipeline.builder)} → ${esc(pipeline.reviewer)}` : ""}
          </div>
        </div>
      </section>

      <!-- Brief: title and actions in the header, where the file lives in a caption under it, long text folds -->
      <section class="card mb-16" id="brief-section">
        <div class="card-h"><h2>Brief</h2>
          <div class="card-actions">
            <a class="btn small ghost" href="#/file?path=${encodeURIComponent(briefDoc.runtime_path || `output/${s.id}/brief.md`)}" title="The brief as a plain file">Raw</a>
            <button class="btn small" data-brief-edit ${activeRun ? 'disabled title="Pause the worker to edit the brief"' : 'title="Edit the brief"'}>Edit</button>
          </div>
        </div>
        <div class="card-caption"><span class="mono brief-path-chip" title="Where this file lives">${esc(briefDoc.path || `.orchestrator/output/${s.id}/brief.md`)}</span></div>
        <div class="md brief-content" data-expandable="320">${briefDoc.text ? Markdown.render(briefDoc.text) : `<p class="muted">No brief yet.</p>`}</div>
      </section>

      <section class="card mb-16"><div class="card-h"><h2>Test cases${testCases.cases.length ? ` <span class="count">${testCases.summary.covered}/${testCases.summary.automated} covered</span>` : ""}</h2>
        ${canEditPlan ? `<div class="card-actions"><button class="btn small" data-job-tc-op="add" title="Add a test case">Add</button></div>` : ""}</div>
        ${testCases.cases.length ? `<div class="card-b">${testCaseSummaryHtml(testCases.summary)}${missingTests ? `<div class="notice bad mt-8">${missingTests} automated case${missingTests === 1 ? "" : "s"} due now ${missingTests === 1 ? "has" : "have"} no test yet.</div>` : ""}</div>
        <div class="list">${canEditPlan ? testCaseRowsHtml(testCases.cases, { editable: true, actionPrefix: "job-tc" }) : testCaseRowsHtml(testCases.cases)}</div>`
          : `<div class="empty">No test cases yet. Plans list them for every feature, bug fix and coverage job.</div>`}
</section>

      ${assumptions.length && !reviewingPlan ? `<section class="card mb-16"><div class="card-h"><h2>What the AI assumed</h2><span class="count">${assumptions.length}</span></div>
        <div class="card-b"><ul class="assumptions">${assumptions.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
        <div class="muted">Wrong about something? Use Revise plan in the menu.</div></div></section>` : ""}

      <section class="card mb-16">
        <div class="card-h"><h2>Changes</h2>${changes?.base ? `<span class="count">vs ${esc(changes.base)}</span>` : ""}</div>
        <div class="card-b delta-summary-line">
          ${deltaUnsaved ? `<div class="delta-line unsaved">${esc(deltaUnsaved)}</div>` : ""}
          <div class="diff-files">${allFiles.slice(0, 12).map((f) => `<details class="diff-file" data-diff-path="${esc(f)}"><summary class="mono">${esc(f)}</summary><pre class="diff" aria-live="polite">Loading…</pre></details>`).join("") || `<span class="muted">No modified files</span>`}${allFiles.length > 12 ? `<div class="muted">+${allFiles.length - 12} more (see the full diffstat below)</div>` : ""}</div>
        </div>
        ${changes?.hypothesis ? `<div class="card-b card-b-split text-sm"><strong>Why:</strong> ${esc(changes.hypothesis)}</div>` : ""}
        ${changes?.diffstat ? `<details class="raw" style="border-top: 1px solid var(--border);"><summary style="padding: 8px 16px; font-size: 12.5px; color: var(--muted); cursor: pointer;">View full diffstat (${allFiles.length} files)</summary><pre class="file" style="margin: 0; border: none; border-radius: 0;">${esc(changes.diffstat)}</pre></details>` : ""}
      </section>

      ${uxReviewCard(s, data.ux_review)}

      ${scope ? `<section class="card mb-16" id="scope-section">
        <div class="card-h">
          <h2>Scope check</h2>
          <span class="count">${scope.flagged ? `${scope.flagged} beyond the plan` : (actionableFindings.length ? `${actionableFindings.length} to review` : "Within the plan")}</span>
        </div>
        <div class="card-b stack">
          <div class="muted">${scope.files} file${scope.files === 1 ? "" : "s"} changed, +${scope.added} lines${scope.accepted ? ` · ${scope.accepted} accepted as in scope` : ""}.${scope.clear ? " Everything changed is something the plan called for." : (hasNoPlanFiles && !actionableFindings.length ? " Open scope · diff size monitored." : "")}</div>
          ${hasNoPlanFiles && !actionableFindings.length ? `
            <div class="scope-notice-clean">
              <div class="row gap-8 align-center">
                <svg class="icon muted"><use href="#i-activity"/></svg>
                <span>No files were pre-specified in the plan. All changes stay within the size budget with no dependency or build setting changes.</span>
              </div>
              ${allFiles.length ? `<button type="button" class="btn small" data-scope-adopt='${esc(JSON.stringify(allFiles))}'>Adopt current files as plan scope</button>` : ""}
            </div>
          ` : ""}
          ${actionableFindings.map((f) => `<div class="scope-finding"><div class="row gap-10"><span class="pill ${f.severity === "high" ? "failed" : f.severity === "medium" ? "attention" : "working"}">${esc(f.severity)}</span><strong>${esc(f.title)}</strong></div>
            <div class="muted">${esc(f.detail)}</div>
            ${f.files.length ? `<ul class="scope-files mono">${f.files.slice(0, 12).map((p) => `<li>${esc(p)}</li>`).join("")}${f.files.length > 12 ? `<li class="muted">+${f.files.length - 12} more</li>` : ""}</ul>
              <div><button class="btn small" data-scope-accept='${esc(JSON.stringify(f.files))}'>Accept as in scope</button></div>` : ""}</div>`).join("")}
          ${actionableFindings.some((f) => f.files.length) || scope.accepted ? `<div class="row gap-10">
            ${actionableFindings.some((f) => f.files.length) ? `<a class="btn small primary" href="#/new?type=quick&summary=${encodeURIComponent(scope.trim.summary)}&spec=${encodeURIComponent(scope.trim.spec)}">Create a job to trim it</a>` : ""}
            ${scope.accepted ? `<button class="btn small ghost" data-scope-reset>Reset accepted</button>` : ""}</div>` : ""}
        </div></section>` : ""}



      <!-- Jobs this one was split into (if any) -->
      ${(job.subtask_job_ids && job.subtask_job_ids.length) ? `
        <section class="card mb-16">
          <div class="card-h"><h2>Split into jobs</h2><span class="count">${job.subtask_job_ids.length}</span></div>
          <div class="list">
            ${job.subtask_job_ids.map((subId) => `
              <a class="item" href="#/jobs/${encodeURIComponent(subId)}">
                <div class="main-col"><div class="title mono">${esc(subId)}</div><div class="meta">Runs alongside the others</div></div>
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

      <!-- Additional Docs (Builder summary / Investigations) -->
      ${otherDocs.length ? `
        <div id="docs-section">
          ${otherDocs.map((d) => `<section class="card mb-16"><div class="card-h"><h2>${esc(d.title)}</h2></div><pre class="doc" data-expandable="320">${esc(d.text)}</pre></section>`).join("")}
        </div>
      ` : ""}

      <!-- Tasks, runs, logs and output: folded on phones so the page isn't one long scroll -->
      ${tasks.length && !reviewingPlan ? fold("Tasks", `${tasksDone}/${tasks.length}`, `<div class="list">${taskRowsHtml(false)}</div>${canEditPlan ? `<div class="card-b card-b-split"><button class="btn small" data-task-op="add">Add a task</button></div>` : ""}`) : ""}

      ${runs.length ? fold("Activity & Runs", runs.length, `<div class="list">${runs.map(runItem).join("")}</div>`) : ""}

      ${outputs.length ? fold("Output files", outputs.length, `<div class="list">${outputs.map((o) => `<a class="item" href="#/file?path=${encodeURIComponent(o.path)}">
          <div class="main-col"><div class="title mono">${esc(o.path.split("/").slice(2).join("/") || o.path)}</div>
          <div class="meta">${(o.size / 1024).toFixed(1)} KB · ${esc(ago(o.mtime))}</div></div></a>`).join("")}</div>`) : ""}

      ${(data.context?.updates || []).length ? fold("Updates sent to linked apps", data.context.updates.length, `<div class="list">${data.context.updates.map((e) => `<div class="item"><span aria-label="${e.ok ? "sent" : "failed"}">${e.ok ? "✅" : "⚠️"}</span><div class="main-col">
          <div class="title">${esc(e.message)}</div><div class="meta">${esc([PROVIDER_LABEL[e.provider] || e.provider, e.ref, String(e.event || "").replace(":", " · ").replace("_", " "), e.t].filter(Boolean).join(" · "))}</div></div></div>`).join("")}</div>`) : ""}

      <!-- Technical Details -->
      <section class="card"><details class="raw"><summary>Technical details (${esc(s.id)})</summary><pre>${esc(JSON.stringify(job, null, 2))}</pre></details></section>
      </div></div>
    `,
    after: () => {
      hydrateAuthImages(); // the UX and design check's screenshots
      view.querySelectorAll("[data-brief-edit]").forEach((btn) => btn.addEventListener("click", async () => {
        const bodyHtml = `
          <div class="stack gap-12">
            <div class="muted" style="font-size: var(--text-xs);">
              File location: <span class="mono">${esc(briefDoc.path || `.orchestrator/output/${id}/brief.md`)}</span>
            </div>
            <label class="field">
              <span>Brief Markdown</span>
              <textarea name="text" rows="18" class="mono" style="font-size: var(--text-xs); line-height: 1.5; resize: vertical;" required>${esc(briefDoc.text || "")}</textarea>
            </label>
          </div>
        `;
        const values = await formDialog("Edit Brief", bodyHtml, "Save brief");
        if (!values || !values.text || !values.text.trim()) return;
        try {
          await api(`jobs/${encodeURIComponent(id)}/brief`, {
            method: "POST",
            body: { text: values.text },
          });
          toast("Brief updated");
          route();
        } catch (e) {
          toast(e.message, true);
        }
      }));
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
      view.querySelectorAll("[data-job-tc-op]").forEach((btn) => btn.addEventListener("click", async () => {
        const op = btn.dataset.jobTcOp;
        const caseId = btn.dataset.id;
        const call = async (body, ok) => {
          try {
            await api(`jobs/${encodeURIComponent(id)}/plan-test-cases`, { method: "POST", body });
            if (ok) toast(ok);
            route();
          } catch (e) {
            toast(e.message, true);
          }
        };
        if (op === "add") {
          const v = await formDialog("Add test case", testCaseForm({}), "Add test case");
          if (v) call({ op: "add", ...v }, "Test case added");
        } else if (op === "edit") {
          const rawCase = (job.plan?.test_cases || []).find((c) => String(c.id) === String(caseId)) || testCases.cases.find((c) => String(c.id) === String(caseId)) || {};
          const v = await formDialog(`Edit test case (${caseId})`, testCaseForm(rawCase), "Save changes");
          if (v) call({ op: "edit", id: caseId, ...v }, "Test case saved");
        } else if (op === "remove") {
          const ok = await formDialog(`Delete test case ${caseId}?`, `<p>Are you sure you want to remove <strong>${esc(caseId)}</strong> from this job's plan?</p>`, "Delete", { danger: true });
          if (ok) call({ op: "remove", id: caseId }, "Test case removed");
        }
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
      view.querySelectorAll("[data-scope-adopt]").forEach((btn) => btn.addEventListener("click", async () => {
        try {
          btn.disabled = true;
          const paths = JSON.parse(btn.dataset.scopeAdopt || "[]");
          await api(`jobs/${encodeURIComponent(id)}/scope`, { method: "POST", body: { op: "adopt", paths } });
          toast(`Adopted ${paths.length} file${paths.length === 1 ? "" : "s"} as plan scope`);
          route();
        } catch (e) {
          toast(e.message, true);
          btn.disabled = false;
        }
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
      <input type="file" id="nj-files" class="sr-only" multiple accept="${accept}" aria-label="${esc(label)}">
      <label class="prd-drop upload-drop" for="nj-files" id="nj-drop"><strong>Choose files</strong><span class="muted drop-hint">or drop them here</span></label>
      <small class="hint-text">${hint}</small>
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
        if (file.size > NJ_UPLOAD_LIMIT) { toast(`${file.name} is over ${NJ_UPLOAD_LIMIT / 1048576} MB`, "warning"); continue; }
        st.uploading += 1; chips();
        try { const type = st.type; const saved = await uploadFile(file); (st.uploads[type] ||= []).push(saved); } catch (err) { toast(`${file.name}: ${err.message}`, true); }
        st.uploading -= 1; chips();
      }
      e.target.value = "";
    });
    // Dropped files go through the same path as chosen ones.
    const drop = $("#nj-drop");
    if (drop) {
      drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", (e) => {
        e.preventDefault(); drop.classList.remove("over");
        const input = $("#nj-files");
        if (!e.dataTransfer?.files?.length || !input) return;
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new Event("change"));
      });
    }
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
        if (st.uploading) { toast("Wait for the upload to finish.", "warning"); return; }
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

pages.features = async () => {
  location.hash = "#/";
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
      <span data-owner-only>${testers.configured ? `<button class="btn small primary" ${act("distribute")}>Send current branch</button>` : `<a class="btn small" href="#/config/firebase">Set up Firebase</a>`}</span></div>
      <div class="card-b stack">
        ${latest ? `<div><strong>${esc(latest.version || "Unknown version")}${latest.build ? ` (${esc(latest.build)})` : ""}</strong>
            <span class="muted">· ${esc(ago(latest.delivered))} · ${esc(latest.branch)}${latest.recipients ? ` · to ${esc(latest.recipients)}` : ""}</span>
            ${latest.job_id ? ` <a href="#/jobs/${encodeURIComponent(latest.job_id)}">${esc(latest.title || latest.job_id)}</a>` : ""}</div>`
          : `<div class="muted">${testers.configured ? "Nothing has been sent to testers yet." : "Testers get builds through Firebase App Distribution. Set it up once, then send any branch."}</div>`}
        ${testers.groups.length ? `<div class="row" style="gap:6px"><span class="muted">Tester groups</span>${testers.groups.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</div>` : ""}
        ${testers.configured && !testers.cli_installed ? `<div class="notice bad">The Firebase CLI isn't installed on this machine, so sending will fail. Install it with <code>npm i -g firebase-tools</code>.</div>` : ""}
        ${testers.configured ? (testers.invite_url
          ? `<div class="row gap-10"><span class="muted">Invite link</span><a class="mono" href="${esc(testers.invite_url)}" target="_blank" rel="noopener">${esc(testers.invite_url.replace(/^https:\/\//, "").slice(0, 48))}</a><button class="btn small" data-setup-copy="${esc(testers.invite_url)}">Copy</button><a class="btn small ghost" href="#/config/firebase" data-owner-only>Change</a></div>`
          : `<div class="muted">Want people to join without you adding them? <a href="#/config/firebase" data-owner-only>Add the group's invite link</a> and share it from here.</div>`) : ""}
        ${testers.configured ? `<div class="muted">Add or remove testers and devices in the <a href="https://console.firebase.google.com/" target="_blank" rel="noopener">Firebase console</a>.</div>` : ""}
      </div></section>`;
  const readyCard = ready.length ? `<section class="card mb-16"><div class="card-h"><h2>Ready to ship</h2><span class="count">${ready.length}</span></div>
      <div class="list">${ready.map((j) => `<div class="item"><a class="main-col" href="#/jobs/${encodeURIComponent(j.id)}"><div class="title">${esc(j.title)}</div><div class="meta mono">${esc(j.branch)}${j.pr_number ? ` · PR #${esc(j.pr_number)}` : ""}</div></a>
        <div class="side">${testers.configured ? `<button class="btn small" data-owner-only ${act("deliver", { job: j.id })}>Send to testers</button>` : ""}
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
        <div class="row" data-owner-only><button class="btn small ${connected ? "" : "primary"}" id="analytics-set">${data.provider ? "Change" : "Connect"}</button>
        ${connected ? `<button class="btn small" id="analytics-test">Send test event</button><button class="btn small danger" id="analytics-clear">Disconnect</button>` : ""}</div></div>
        <div class="card-b stack"><strong>${connected ? `${esc(data.provider_name)} (${esc(data.region.toUpperCase())})` : data.provider ? `${esc(data.provider_name)}: key missing` : "Not connected"}</strong>
          <span class="muted">${connected ? "New work for a feature is told to send its KPI events here. The key is stored, never shown." : "Optional. Mixpanel, Amplitude or PostHog. Connect one so new work knows where events go, and verify it with a test event."}</span></div></section>
      ${features.map(card).join("") || `<div class="empty">KPIs belong to features. Define features in <a href="#/product">product requirements</a> first.</div>`}`,
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

// ---------------------------------------------------------------- UX and design review

const UX_SEVERITY = ["cosmetic", "minor", "moderate", "major", "blocker"];
const UX_TONE = ["muted", "muted", "attention", "failed", "failed"];

function uxFindingsHtml(findings, { listId, shown = 8 } = {}) {
  if (!findings.length) return `<div class="empty">No findings.</div>`;
  return `<div class="list" id="${listId}">${findings.map((f, n) => `<div class="item ux-finding"${n >= shown ? " hidden" : ""}>
      <div class="main-col"><div class="row gap-8"><span class="pill ${UX_TONE[f.severity]}">${UX_SEVERITY[f.severity]}</span>
        <strong>${esc([f.screen, f.element].filter(Boolean).join(" · ") || "All screens")}</strong>${f.item ? `<span class="muted mono">${esc(f.item)}</span>` : ""}</div>
        <div>${esc(f.problem)}</div>${f.fix ? `<div class="muted">Fix: ${esc(f.fix)}</div>` : ""}</div></div>`).join("")}</div>
    ${findings.length > shown ? `<div class="card-b"><button type="button" class="btn small" data-show-all="${listId}">Show all ${findings.length}</button></div>` : ""}`;
}

function uxChecklistHtml(checklist) {
  const judged = checklist.filter((c) => c.status !== "n/a");
  if (!judged.length) return `<div class="empty">Nothing on the checklist applied.</div>`;
  const mark = { pass: ["done", "Pass"], partial: ["attention", "Partly"], fail: ["failed", "Fail"] };
  return `<div class="list">${judged.map((c) => `<div class="item"><span class="pill ${mark[c.status][0]}">${mark[c.status][1]}</span>
    <div class="main-col"><div class="title mono">${esc(c.id)}</div>${c.note ? `<div class="meta">${esc(c.note)}</div>` : ""}</div></div>`).join("")}</div>`;
}

function uxScreensHtml(screens, base) {
  if (!screens?.length) return "";
  return `<div class="ux-screens">${screens.map((sc) => `<figure class="ux-screen"><img alt="${esc(sc.route)} at ${sc.width ? `${sc.width} px` : "simulator"}${sc.dark ? ", dark" : ""}" data-auth-src="${base}/${encodeURIComponent(sc.file)}">
    <figcaption>${esc(sc.route)}${sc.width ? ` · ${sc.width} px` : ""}${sc.dark ? " · dark" : ""}</figcaption></figure>`).join("")}</div>`;
}

const uxFixLink = (text, label = "Create a job to fix") =>
  `<a class="btn small" href="#/new?type=quick&summary=${encodeURIComponent("Fix UX and design findings")}&spec=${encodeURIComponent(text)}">${label}</a>`;

// The job page's card: the checklist run on this job's interface changes.
function uxReviewCard(s, ux) {
  if (!ux || (ux.skipped && !ux.error)) return "";
  const c = ux.counts || {};
  const rerun = `<button type="button" class="btn small ghost" ${act("ux_review_job", { job: s.id })}>Check again</button>`;
  if (ux.error) return `<section class="card mb-16" id="ux-section"><div class="card-h"><h2>UX and design check</h2>${rerun}</div>
    <div class="card-b"><p class="muted">It couldn't run: ${esc(ux.error)}</p></div></section>`;
  const findings = ux.findings || [];
  const fixText = "Fix these UX and design findings:\n" + findings.filter((f) => f.severity >= 2).map((f) => `- [${UX_SEVERITY[f.severity]}] ${f.screen || "All screens"}: ${f.problem}${f.fix ? ` Fix: ${f.fix}` : ""}`).join("\n");
  return `<section class="card mb-16" id="ux-section">
    <div class="card-h"><h2>UX and design check</h2><span class="count">${c.findings ? `${plural(c.findings, "finding")}${c.major ? ` · ${c.major} major` : ""}` : "Nothing found"}</span></div>
    <div class="card-b stack">
      ${ux.summary ? `<p>${clamped(ux.summary, 320)}</p>` : ""}
      <div class="muted">Checked ${plural((ux.files || []).length, "interface file")}${ux.screens ? ` and ${plural(ux.screens, "screen")}` : ", from the code only"}. A prompt to look, not a gate.</div>
      ${uxScreensHtml(ux.screen_list, `ux-screens/job/${encodeURIComponent(s.id)}`)}
    </div>
    ${findings.length ? uxFindingsHtml(findings, { listId: "ux-findings", shown: 5 }) : ""}
    <details class="fold"><summary class="card-h"><h2>Checklist</h2></summary>${uxChecklistHtml(ux.checklist || [])}</details>
    <div class="card-b card-b-split row gap-8">${findings.some((f) => f.severity >= 2) ? uxFixLink(fixText) : ""}${rerun}</div>
  </section>`;
}

// What jobs need, each with a way to do it in the app (the terminal setup wizard's job, without the terminal).
// Readiness: can jobs run on this computer? What they need (required, then optional), whether the project's build
// tools are here, and the checks you can run. Check-up is about the product; this is about the environment.
const READINESS_CHECKS = [["check", "Tools and logins", "Command-line tools, sign-ins and keys (prerequisite audit)"],
  ["check_config", "Project config", "Validate .orchestrator/project.json"], ["worker_check", "Worker machines", "Remote build machines respond"],
  ["test", "Orchestrator health check", "Orchestrator's own self-tests, to confirm it works on this computer"]];

pages.readiness = async () => {
  setupState = await api("setup"); // its buttons look items up here
  const s = setupState, left = s.required_total - s.required_done;
  return {
    title: "Readiness",
    sub: left ? `${left} required step${left > 1 ? "s" : ""} left before jobs can run` : "Everything jobs need is in place",
    html: `
      <div class="tasks-progress-wrap mb-16"><div class="progress-bar-container"><div class="progress-bar-fill" style="width: ${Math.round(100 * s.required_done / s.required_total)}%"></div></div><span class="progress-text">${s.required_done}/${s.required_total} required</span></div>
      <section class="card mb-16">${setupListHtml(s)}</section>
      <section class="card mb-16"><div class="card-h"><h2>Will this computer build it?</h2><button type="button" class="btn small" id="preflight-refresh">Check again</button></div>
        <div class="list" id="preflight-list"><div class="item"><div class="main-col muted">Checking this computer…</div></div></div></section>
      <section class="card"><div class="card-h"><h2>Run a check</h2></div>
        <div class="list">${READINESS_CHECKS.filter(([a]) => ConfigurationPages.canRunAction(a, state)).map(([a, title, hint]) =>
          `<div class="item"><div class="main-col"><div class="title">${title}</div><div class="meta">${hint}</div></div><div class="side"><button type="button" class="btn small" ${act(a)}>Run</button></div></div>`).join("")}</div></section>`,
    after: () => {
      const paint = async (refresh) => {
        const box = $("#preflight-list");
        try {
          const { items } = await api(`preflight${refresh ? "?refresh=1" : ""}`);
          const icon = { ok: `<span class="setup-icon done" aria-label="fine">✓</span>`, warn: `<span class="pill attention">Maybe</span>`, fail: `<span class="pill failed">Blocks</span>` };
          box.innerHTML = items.map((i) => `<div class="item">${icon[i.status]}<div class="main-col"><div class="title">${esc(i.title)}</div><div class="meta">${codeSpans(i.detail)}${i.fix && !i.route ? ` · ${codeSpans(i.fix)}` : ""}</div></div>${i.route && i.status !== "ok" ? `<div class="side"><a class="btn small" href="${esc(i.route)}">${esc(i.fix || "Open")}</a></div>` : ""}</div>`).join("") || `<div class="empty">Nothing to check.</div>`;
        } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
      };
      paint(false);
      $("#preflight-refresh")?.addEventListener("click", () => paint(true));
    },
  };
};

// GitHub sign-in without a terminal: start GitHub's device flow, show its one-time code and link, and wait here.
async function signInToGitHub() {
  let start;
  try { start = await api("github/login", { method: "POST", body: {} }); } catch (e) { return toast(e.message, true); }
  if (start.signed_in) { toast(`Signed in to GitHub as ${start.user}`); return route(); }
  const done = formDialog("Sign in to GitHub", `
    <p>Open GitHub, then enter this code:</p>
    <div class="gh-code"><code id="gh-code">${esc(start.code)}</code><button type="button" class="btn small" data-setup-copy="${esc(start.code)}">Copy</button></div>
    <p><a class="btn" href="${esc(start.url)}" target="_blank" rel="noopener">Open GitHub ↗</a></p>
    <p class="muted" id="gh-status" role="status">Waiting for you to approve it on GitHub…</p>`, "Close");
  const timer = setInterval(async () => {
    try {
      const st = await api("github/login");
      if (st.signed_in) {
        clearInterval(timer);
        $("#dialog")?.close("cancel");
        toast(`Signed in to GitHub as ${st.user}`);
        route();
      } else if (!st.waiting) {
        clearInterval(timer);
        const el = $("#gh-status"); if (el) el.textContent = "That code expired or was declined. Close this and try again.";
      }
    } catch { /* keep waiting */ }
  }, 3000);
  await done;
  clearInterval(timer);
}

pages["ux-review"] = async (_, query) => {
  const data = await api("ux-pass");
  const id = query.get("id") || data.passes[0]?.id;
  const pass = id ? await api(`ux-pass/${encodeURIComponent(id)}`).catch(() => null) : null;
  const cfg = data.settings;
  const noScreens = cfg && !cfg.url && !cfg.simulator;
  const saved = data.saved || {};
  const areaCard = (area, title) => {
    const findings = (pass.findings || []).filter((f) => f.area === area);
    const checks = (pass.checklist || []).filter((c) => c.id.startsWith(area));
    return `<section class="card mb-16"><div class="card-h"><h2>${title}</h2><span class="count">${plural(findings.length, "finding")}</span></div>
      ${uxFindingsHtml(findings, { listId: `ux-${area}` })}
      <details class="fold"><summary class="card-h"><h2>${title} checklist</h2><span class="count">${checks.filter((c) => c.status === "pass").length} of ${checks.filter((c) => c.status !== "n/a").length} pass</span></summary>${uxChecklistHtml(checks)}</details></section>`;
  };
  return {
    title: "UX review",
    sub: "Your product's screens checked against usability heuristics and your design system: a UX pass and a design pass.",
    actions: `<button class="btn primary" ${act("ux_pass")}>Run a pass</button>`,
    html: `
      ${data.error ? `<div class="notice bad mb-16">Screens to review: ${esc(data.error)}</div>` : ""}
      ${noScreens ? `<div class="notice mb-16">No screens are set up, so a pass reviews the code only and can't judge layout, spacing or contrast. Add your app's address below.</div>` : ""}
      ${cfg?.url && !data.browser ? `<div class="notice bad mb-16">No Chrome, Chromium or Edge found on this computer, so screens can't be captured.</div>` : ""}
      ${pass ? `
        <section class="card mb-16"><div class="card-h"><h2>Latest pass</h2><span class="count">${esc(ago(Date.parse(pass.at) / 1000))}</span></div>
          <div class="card-b stack">
            <div class="row gap-8"><span class="pill ${pass.counts.major ? "failed" : pass.counts.findings ? "attention" : "done"}">${plural(pass.counts.findings, "finding")}</span>
              ${pass.counts.major ? `<span class="muted">${pass.counts.major} major or worse</span>` : ""}<span class="muted">${pass.counts.passed} checklist items pass, ${pass.counts.failed} fail</span></div>
            ${pass.summary ? `<p>${clamped(pass.summary, 500)}</p>` : ""}
            ${pass.limits ? `<p class="muted">${esc(pass.limits)}</p>` : ""}
            ${pass.findings?.length ? `<div>${uxFixLink(pass.fix_text, "Create a job to fix the main ones")}</div>` : ""}
          </div>
          ${pass.screens?.length ? `<details class="fold"><summary class="card-h"><h2>Screens</h2><span class="count">${pass.screens.length}</span></summary>
            <div class="card-b">${uxScreensHtml(pass.screens, `ux-screens/pass/${encodeURIComponent(pass.id)}`)}</div></details>` : ""}
        </section>
        ${areaCard("ux", "UX")}
        ${areaCard("design", "Design")}` : `<section class="card mb-16"><div class="empty">No pass yet. <strong>Run a pass</strong> captures your screens and reviews them, which takes a few minutes.</div></section>`}
      <section class="card mb-16" id="ux-setup"><details class="fold"${query.get("setup") || noScreens ? " open" : ""}><summary class="card-h"><h2>Screens to review</h2></summary>
        <form class="card-b stack" id="ux-settings">
          <label class="field"><span>Your app's address</span><input type="url" name="url" value="${esc(saved.url || "")}" placeholder="http://localhost:3000" spellcheck="false">
            <small class="hint-text">Where it runs while you develop. A sign-in token can come from an environment variable: <code class="nowrap">?token=$MY_TOKEN</code>.</small></label>
          <label class="field"><span>Pages to capture <span class="muted">(one per line)</span></span><textarea name="routes" rows="4" spellcheck="false">${esc((saved.routes || ["/"]).join("\n"))}</textarea></label>
          <label class="field"><span>Widths <span class="muted">(pixels)</span></span><input type="text" name="widths" value="${esc((saved.widths || [390, 1440]).join(" "))}"></label>
          <details class="np-more"><summary class="np-section-legend">More options</summary><div class="stack">
            <label class="field"><span>Command that starts the app <span class="muted">(optional)</span></span><input type="text" name="start_command" value="${esc(saved.start_command || "")}" placeholder="npm run dev" spellcheck="false">
              <small class="hint-text">Used only when the address isn't answering; stopped afterwards.</small></label>
            <label class="field"><span>Your UI conventions file <span class="muted">(optional)</span></span><input type="text" name="conventions" value="${esc(saved.conventions || "")}" placeholder="docs/ui-conventions.md" spellcheck="false">
              <small class="hint-text">Found automatically when it's named like docs/*ui*conventions*.md or DESIGN.md.</small></label>
            <label class="check"><input type="checkbox" name="dark_mode" ${saved.dark_mode !== false ? "checked" : ""}><span>Also capture dark mode</span></label>
            <label class="check"><input type="checkbox" name="simulator" ${saved.simulator ? "checked" : ""}><span>Also capture the iOS simulator</span></label>
            <label class="check"><input type="checkbox" name="review_changes" ${saved.review_changes !== false ? "checked" : ""}><span>Check every job that changes the interface</span></label>
          </div></details>
          <div><button class="btn" type="submit">Save</button></div>
        </form></details></section>
      ${data.passes.length > 1 ? `<section class="card"><div class="card-h"><h2>Earlier passes</h2></div><div class="list">${data.passes.map((p) => `<a class="item" href="#/ux-review?id=${encodeURIComponent(p.id)}">
        <div class="main-col"><div class="title">${esc(ago(Date.parse(p.at) / 1000))}${p.id === id ? " · showing" : ""}</div><div class="meta">${plural(p.counts.findings, "finding")}, ${p.counts.major} major</div></div></a>`).join("")}</div></section>` : ""}`,
    after: () => {
      hydrateAuthImages();
      $("#ux-settings").addEventListener("submit", async (e) => {
        e.preventDefault();
        const f = new FormData(e.target);
        try {
          await api("ui-review", { method: "POST", body: { ...Object.fromEntries(f), dark_mode: f.has("dark_mode"), simulator: f.has("simulator"), review_changes: f.has("review_changes") } });
          toast("Saved"); route();
        } catch (err) { toast(err.message, true); }
      });
    },
  };
};

// Server text marks commands with `backticks`: show them as code, not as literal backticks.
const codeSpans = (text) => esc(text).replace(/`([^`]+)`/g, "<code>$1</code>");

pages.checkup = async () => {
  const d = await api("health");
  // Only the highlighted next step gets a filled button; the rest of the list stays quiet.
  const action = (i, primary = false) => {
    const kind = primary ? "btn small primary" : "btn small";
    if (i.job) return `<a class="${kind}" href="#/new?type=${encodeURIComponent(i.job.type)}&summary=${encodeURIComponent(i.job.summary)}">${esc(i.label)}</a>`;
    if (i.route) return `<a class="${kind}" href="${esc(i.route)}">${esc(i.label)}</a>`;
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
      ${next ? `<section class="card mb-16 card-next"><div class="card-h"><h2>Next: ${esc(next.title)}</h2></div>
        <div class="card-b stack"><div>${esc(next.detail)}</div><div>${action(next, true)}</div></div></section>` : `<div class="notice">Everything on the list is in place. Keep an eye on Home for what needs you next.</div>`}
      <section class="card"><div class="card-h"><h2>Everything</h2><span class="count">${d.ok} of ${d.total}</span></div>
        <div class="card-b muted card-note">What the product has in place. Tools, keys and machines are in <a href="#/readiness">Readiness</a>.</div>
        <div class="list">${d.items.map((i) => `<div class="item">${icon[i.status]}<div class="main-col"><div class="title">${esc(i.title)}</div><div class="meta">${esc(i.detail)}</div></div>
          <div class="side">${i.id === d.next || i.status === "ok" ? "" : action(i)}</div></div>`).join("")}</div></section>
      <section class="card mt-16"><div class="card-h"><h2>UX and design</h2><a class="btn small" href="#/ux-review">Open UX review</a></div>
        <div class="card-b muted" id="ux-checkup">Checking…</div></section>
      <section class="card mt-16"><div class="card-h"><h2>This computer and its tools</h2><a class="btn small" href="#/readiness">Open Readiness</a></div>
        <div class="card-b muted">Whether jobs can run here: the AI, machines, sign-ins and build tools, and the checks you can run.</div></section>`,
    after: () => {
      api("ux-pass").then(({ passes, settings }) => {
        const last = passes[0];
        $("#ux-checkup").textContent = last
          ? `Last pass ${ago(Date.parse(last.at) / 1000)}: ${plural(last.counts.findings, "finding")}, ${last.counts.major} major or worse.`
          : settings && (settings.url || settings.simulator) ? "No pass yet. Run one from UX review." : "No pass yet, and no screens set up. UX review shows how.";
      }).catch(() => { $("#ux-checkup").textContent = "Couldn't read the UX review."; });
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
      ${[["Home", "#/", "What's waiting on you (across projects), then all jobs."], ["Product", "#/product", "The one-page requirements every job reads. Edit it, import a PRD, see its history."],
         ["Tests", "#/tests", "Test cases by area, suites, coverage."], ["Delivery", "#/delivery", "What's live, what testers have, what's ready, pipeline."], ["Measure", "#/measure", "KPIs, analytics connection, learning log."],
         ["Check-up", "#/checkup", "What the product has in place and what's missing."], ["Readiness", "#/readiness", "Whether jobs can run here: AI, machines, sign-ins, build tools and checks."], ["Projects", "#/projects", "Switch, add or start a project."], ["Activity", "#/activity", "Commands and runs, with live output."],
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
      <details><summary>A job won't run</summary><p>Jobs need a machine, a model, a signed-in AI provider and a GitHub remote. <a href="#/readiness">Readiness</a> shows what is missing, each with a way to fix it here.</p></details>
      <details><summary>How do I get better plans?</summary><p>Fill in the <a href="#/product">product requirements</a>: what you're building, who it's for, the core features, how it should look and feel, and what it should not be. Every plan is written against it. All of it is optional, and “not sure yet” is a fine answer. Draft it from your project, or import a PRD you already have. It keeps itself up to date as jobs finish, and you can see and undo every change.</p></details>
      <details><summary>I changed my mind about a job</summary><p>Use <strong>Revise plan</strong> in the job's More menu to re-plan, or <strong>Discard job</strong> to revert its changes and delete its branch. Discard can't be undone.</p></details>
      <details><summary>I deleted something by mistake</summary><p>Deleting a feature or KPI shows an Undo for 10 seconds. A job marked complete can be restored from the <a href="#/?filter=archived">Archived</a> filter on Home.</p></details>
      <details><summary>How do I get alerts when I'm away?</summary><p>The sidebar's <strong>Notify me when done</strong> sends alerts to this device. Turned on in the hosted app, they arrive even with Orchestrator closed, and you're told if your computer goes offline mid-run (on iPhone, add it to your Home Screen first). For Slack, add a webhook under <a href="#/connections?card=chat">Connections</a>.</p></details>
      <details><summary>Where are the full guides?</summary><p><a href="#/docs">Docs</a> has the project's own files and Orchestrator's guides. The terminal console has everything too: use <strong>Open full console</strong>.</p></details>
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
    const open = kind === "job" ? `<a class="btn" href="#/jobs/${encodeURIComponent(id)}">Open the job</a>` : "";
    return {
      title: doc.title,
      sub: kind === "file" ? `<span class="mono">${esc(doc.path)}</span>` : "",
      actions: `${open}<button class="btn" id="doc-download">Download .md</button>`,
      html: `<section class="card"><div class="card-b"><div class="md">${Markdown.render(doc.markdown)}</div></div></section>`,
      after: () => $("#doc-download").addEventListener("click", () => downloadText(`${(doc.title || "document").replace(/[^A-Za-z0-9._-]+/g, "-")}.md`, doc.markdown)),
    };
  }

  const [d, config] = await Promise.all([api("docs"), api("config").catch(() => null)]);
  const guides = (config?.docs || []).filter((g) => g.section === "Orchestrator docs"); // how Orchestrator itself works
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
        <div class="list" id="docs-features">${d.features.length ? d.features.map(featureRow).join("") : `<div class="empty">No features yet. Group your jobs into features in <a href="#/product">product requirements</a> and each gets a page here.</div>`}</div></section>
      <section class="card mb-16"><div class="card-h"><h2>Jobs</h2><span class="count">${d.jobs.length}</span>
        <div class="filters"><button type="button" class="btn small on" data-docs-group="">All</button><button type="button" class="btn small" data-docs-group="done">Done</button><button type="button" class="btn small" data-docs-group="open">Not done</button></div></div>
        <div class="list" id="docs-jobs">${d.jobs.length ? d.jobs.map(jobRow).join("") : `<div class="empty">No jobs yet.</div>`}</div>
        ${d.jobs.length > SHOWN ? `<div class="card-b"><button type="button" class="btn small" id="docs-more">Show all ${d.jobs.length}</button></div>` : ""}</section>
      <section class="card"><div class="card-h"><h2>Project files</h2><span class="count">${d.files.length}</span></div>
        <div class="list" id="docs-files">${d.files.length ? fileRows : `<div class="empty">No README or docs/ folder found.</div>`}</div></section>
      ${guides.length ? `<section class="card mt-16"><div class="card-h"><h2>Orchestrator guides</h2><span class="count">${guides.length}</span></div>
        <div class="list">${guides.map((g) => `<div class="item doc-row" data-find="${esc(g.name.toLowerCase())}"><div class="main-col"><div class="title">${esc(g.name)}</div></div>
          <div class="side"><button type="button" class="btn small" data-guide="${esc(g.id)}">Read</button></div></div>`).join("")}</div></section>` : ""}`,
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
      view.querySelectorAll("[data-guide]").forEach((b) => b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          const g = await api(`config/doc?id=${encodeURIComponent(b.dataset.guide)}`);
          const closed = formDialog(g.name, `<pre class="doc-text">${esc(g.text)}</pre>`, "Close");
          $("#dialog-cancel").hidden = true;
          await closed;
        } catch (err) { toast(err.message, true); } finally { if (b.isConnected) b.disabled = false; }
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

// The AI updated the product requirements by itself: say so once, over the page, with ways to look at it or take it back.
let lastPrdNotice; // undefined until the first poll, so an update that happened while you were away is shown but doesn't also fire an alert
function showPrdUpdate(note) {
  if (!note) { if (lastPrdNotice) clearMessage(`prd:${lastPrdNotice}`); lastPrdNotice = null; return; }
  if (note.id === lastPrdNotice) return;
  const first = lastPrdNotice === undefined;
  if (lastPrdNotice) clearMessage(`prd:${lastPrdNotice}`);
  lastPrdNotice = note.id;
  const seen = () => api("product/dismiss", { method: "POST", body: {} }).catch(() => {});
  notify("info", `Product requirements updated${note.job_title ? ` after “${note.job_title}”` : ""}: ${note.summary}`, {
    id: `prd:${note.id}`, sticky: true, onDismiss: seen,
    actions: [{ label: "See what changed", href: `#/product?history=${encodeURIComponent(note.id)}`, after: seen }, { label: "Undo", run: () => undoPrdUpdate(note.id) }],
  });
  if (!first) Notifications.show({ title: "Product requirements updated", body: note.summary, key: `prd:${note.id}`, hash: `#/product?history=${encodeURIComponent(note.id)}` });
}

async function undoPrdUpdate(noteId) {
  const { history } = await api("product");
  const i = history.findIndex((h) => h.id === noteId);
  if (i === -1 || !history[i + 1]) throw new Error("There's no earlier version to go back to.");
  await api("product/revert", { method: "POST", body: { id: history[i + 1].id } });
  await api("product/dismiss", { method: "POST", body: {} });
  toast("Went back to the version before that update");
  route();
}

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
  // The heading and the pitch open the product requirements. Its buttons stay secondary: New job is Home's one primary.
  return `<section class="card mb-16" id="product-strip"><div class="card-h"><h2><a class="card-title-link" href="#/product">Product</a></h2></div>
    <div class="card-b stack">
      ${lead ? `<a class="product-pitch" href="#/product" title="Open the product requirements">${esc(prdSnippet(lead.body))}</a>`
        : `<p>Tell us what you're building, in a few sentences. Every job reads this first, and it stays up to date as you build.</p>
           <div class="row gap-10">${p.can_draft ? `<a class="btn small" href="#/product?draft=1">Draft it from my project</a>` : ""}<a class="btn small" href="#/product">Write it</a><a class="btn small" href="#/product?import=1">Import PRD</a></div>`}
    </div></section>`;
}

pages.product = async (_, query) => {
  let p = await api("product");
  const open = { history: query?.get("history") || "", imp: query?.get("import") === "1", draft: query?.get("draft") === "1", section: query?.get("section") || "" };
  const sectionCard = (s) => {
    const designs = s.id === "look" ? [...s.body.matchAll(/\]\((designs\/[^)\s]+)\)/g)].map((m) => m[1]).filter((d) => DESIGN_IMAGE.test(d)) : [];
    return `<section class="card mb-16" id="sec-${s.id}"><div class="card-h"><h2>${esc(s.title)}</h2>
        <button type="button" class="btn small" data-prd-edit="${s.id}">Edit</button></div>
      <div class="card-b stack" data-prd-body="${s.id}">
        <div class="muted">${esc(s.hint)}</div>
        ${s.filled ? `<div class="md" data-expandable="280">${Markdown.render(s.body)}</div>` : ""}
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
      <div id="prd-panel"></div>
      ${p.sections.map(sectionCard).join("")}
      <section class="card mb-16"><div class="card-h"><h2>Keeping it up to date</h2></div><div class="card-b stack">
        <label class="check"><input type="checkbox" id="prd-auto" ${p.auto_update ? "checked" : ""}> <span>Update this automatically when jobs finish</span></label>
        <div class="muted">After a feature or design job, the AI checks whether what it learned changes this document. It only edits when something clearly changed, keeps your words, tells you, and every change is in the history below where you can undo it.</div></div></section>
      <section class="card"><div class="card-h"><h2>History</h2><span class="count">${p.history.length}</span></div>
        <div class="list">${p.history.length ? historyRows(p.history) : `<div class="empty">Nothing yet. Every change you or the AI makes will be listed here.</div>`}</div></section>`;
  return {
    title: "Product requirements",
    sub: `<span class="mono">${esc(p.path)}</span> · Every job reads this first.`,
    // An empty document gets one "Start here" card with these choices; the header offers them only after that.
    actions: !p.sections.some((x) => x.filled) && !p.history.length ? ""
      : `${p.can_draft ? `<button class="btn" id="prd-draft">Draft it from my project</button>` : ""}<button class="btn" id="prd-import">Import PRD</button>`,
    html: render(),
    after: () => {
      const panel = () => $("#prd-panel");
      const reload = async () => { p = await api("product"); $("#view").innerHTML = render(); wire(); hydrateAuthImages(); wireExpandables(view); };
      const save = async (body) => { p = await api("product", { method: "POST", body }); };

      const wire = () => {
        $("#prd-import")?.addEventListener("click", () => importPanel());
        $("#prd-draft")?.addEventListener("click", () => draftPanel());
        view.querySelectorAll("[data-prd-edit]").forEach((btn) => btn.addEventListener("click", () => {
          const id = btn.dataset.prdEdit, s = p.sections.find((x) => x.id === id), box = view.querySelector(`[data-prd-body="${id}"]`);
          btn.hidden = true;
          if (id === "features") {
            const rawLines = (s.body || "").split("\n").map((l) => l.replace(/^\s*[-*•\d+.]\s*/, "").trim()).filter(Boolean);
            const initialFeatures = rawLines.length ? rawLines : [""];
            box.innerHTML = `<div class="muted">${esc(s.hint)}</div>
              <div class="prd-features-edit" id="prd-features-list"></div>
              <div class="row items-center gap-10 mt-8">
                <button type="button" class="btn small ghost" id="prd-feature-add">+ Add feature</button>
                <span class="muted text-xs">Press Enter to add another feature</span>
              </div>
              <div class="row gap-10 mt-12">
                <button type="button" class="btn primary" id="prd-save-features">Save</button>
                <button type="button" class="btn ghost" id="prd-cancel-features">Cancel</button>
              </div>`;
            const list = box.querySelector("#prd-features-list");
            const createRow = (val = "") => {
              const row = document.createElement("div");
              row.className = "prd-feature-row";
              row.innerHTML = `<span class="prd-feature-bullet" aria-hidden="true">&bull;</span>
                <input type="text" class="prd-feature-input" value="${esc(val)}" placeholder="Describe a feature…" aria-label="Feature item">
                <button type="button" class="btn icon ghost prd-feature-del" title="Remove feature" aria-label="Remove feature">&times;</button>`;
              const input = row.querySelector(".prd-feature-input");
              const delBtn = row.querySelector(".prd-feature-del");
              input.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  const newRow = createRow("");
                  row.after(newRow);
                  newRow.querySelector(".prd-feature-input").focus();
                } else if (e.key === "Backspace" && input.value === "") {
                  const rows = list.querySelectorAll(".prd-feature-row");
                  if (rows.length > 1) {
                    e.preventDefault();
                    const prevRow = row.previousElementSibling;
                    const nextRow = row.nextElementSibling;
                    row.remove();
                    const targetInput = (prevRow || nextRow)?.querySelector(".prd-feature-input");
                    if (targetInput) {
                      targetInput.focus();
                      const len = targetInput.value.length;
                      targetInput.setSelectionRange(len, len);
                    }
                  }
                } else if (e.key === "ArrowUp") {
                  const prevInput = row.previousElementSibling?.querySelector(".prd-feature-input");
                  if (prevInput) { e.preventDefault(); prevInput.focus(); }
                } else if (e.key === "ArrowDown") {
                  const nextInput = row.nextElementSibling?.querySelector(".prd-feature-input");
                  if (nextInput) { e.preventDefault(); nextInput.focus(); }
                }
              });
              input.addEventListener("paste", (e) => {
                const text = (e.clipboardData || window.clipboardData)?.getData("text") || "";
                if (text.includes("\n")) {
                  e.preventDefault();
                  const lines = text.split("\n").map((l) => l.replace(/^\s*[-*•\d+.]\s*/, "").trim()).filter(Boolean);
                  if (!lines.length) return;
                  input.value = lines[0];
                  let curRow = row;
                  for (let i = 1; i < lines.length; i++) {
                    const nr = createRow(lines[i]);
                    curRow.after(nr);
                    curRow = nr;
                  }
                  curRow.querySelector(".prd-feature-input")?.focus();
                }
              });
              delBtn.addEventListener("click", () => {
                const rows = list.querySelectorAll(".prd-feature-row");
                if (rows.length > 1) {
                  const prev = row.previousElementSibling || row.nextElementSibling;
                  row.remove();
                  prev?.querySelector(".prd-feature-input")?.focus();
                } else {
                  input.value = "";
                  input.focus();
                }
              });
              return row;
            };

            initialFeatures.forEach((f) => list.appendChild(createRow(f)));
            box.querySelector("#prd-feature-add").addEventListener("click", () => {
              const nr = createRow("");
              list.appendChild(nr);
              nr.querySelector(".prd-feature-input").focus();
            });
            box.querySelector("#prd-cancel-features").addEventListener("click", reload);
            box.querySelector("#prd-save-features").addEventListener("click", async () => {
              try {
                const items = Array.from(box.querySelectorAll(".prd-feature-input")).map((i) => i.value.trim()).filter(Boolean);
                const body = items.length ? items.map((f) => `- ${f}`).join("\n") : "";
                await save({ section: "features", body });
                toast("Saved");
                await reload();
              } catch (e) {
                toast(e.message, true);
              }
            });
            const firstEmpty = Array.from(box.querySelectorAll(".prd-feature-input")).find((i) => !i.value) || box.querySelector(".prd-feature-input");
            firstEmpty?.focus();
            return;
          }

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
            if (f.size > NJ_UPLOAD_LIMIT) { toast(`${f.name} is over 25 MB`, "warning"); continue; }
            try { p = await uploadFile(f, "product/design"); toast(`Added ${f.name}`); } catch (e) { toast(e.message, true); }
          }
          file.value = ""; await reload();
        });
        let addingLink = false;
        $("#prd-add-link")?.addEventListener("click", async () => {
          if (addingLink || $("#dialog")?.open) return;
          addingLink = true;
          try {
            const v = await formDialog("Add a link", `<label class="field"><span>What is it?</span><input type="text" name="label" maxlength="120" placeholder="e.g. Figma: onboarding flow"></label>
              <label class="field"><span>Link</span><input type="url" name="url" required placeholder="https://"></label>`, "Add", { compact: true });
            if (!v) return;
            try { await api("product/reference", { method: "POST", body: { label: v.label, url: v.url } }); await reload(); } catch (e) { toast(e.message, true); }
          } finally {
            addingLink = false;
          }
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
        if (file && file.size > NJ_UPLOAD_LIMIT) { toast("That file is over 25 MB.", "warning"); return importPanel(); }
        if (file && !/\.(md|markdown|txt|docx|pdf)$/i.test(file.name)) { toast("Use a markdown, text, Word or PDF file, or paste the text.", "warning"); return importPanel(); }
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
          if (!text) return toast("Paste some text, or choose a file.", "warning");
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
          <div>${existing ? "This project already exists, so we can read it and draft a first version for you to correct. Or say what you have in mind in the pitch below." : "Say what you have in mind in the pitch below."}</div>
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
        <div data-owner-only><a class="btn primary" href="#/connections?connect=sentry">Connect Sentry</a></div>
      </section>` };
  }
  const ok = !sessions.error;
  return {
    ...base,
    actions: `<button class="btn primary" ${act("logs_pull")} ${ok ? "" : "disabled"}>Pull newest launch</button>
              <button class="btn" ${act("logs_tail")} ${ok ? "" : "disabled"}>Follow live</button>`,
    html: `
      ${sessions.error ? `<div class="notice bad">${esc(sessions.error)}<div class="row" data-owner-only><a class="btn small" href="#/connections?connect=sentry">Reconnect Sentry</a></div></div>` : ""}
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

// The eight standard kinds of test case (the server maps older names onto these).
const TC_TYPES = [["functionality", "Functionality"], ["user-interface", "User interface"], ["performance", "Performance"], ["integration", "Integration"],
  ["usability", "Usability"], ["database", "Database"], ["security", "Security"], ["user-acceptance", "User acceptance"]];
const tcTypeLabel = (t) => (TC_TYPES.find(([v]) => v === t) || [t, t || "Functionality"])[1];

function testCaseSummaryHtml(summary) {
  const kinds = Object.entries(summary.by_type || {}).filter(([, n]) => n).map(([t, n]) => `${n} ${tcTypeLabel(t).toLowerCase()}`).join(" · ");
  const pct = summary.covered_pct;
  return `<div class="stack">
    ${pct === null ? "" : `<div class="tasks-progress-wrap"><div class="progress-bar-container"><div class="progress-bar-fill" style="width: ${pct}%"></div></div>
      <span class="progress-text">${summary.covered}/${summary.automated} automated covered</span></div>`}
    <div class="muted">${esc(kinds)}${summary.manual ? ` (${summary.manual} checked by hand)` : ""}</div></div>`;
}

function testCaseForm(c = {}) {
  return `
    <label class="field"><span>Title <span class="muted">(what are you testing?)</span></span><input type="text" name="title" required maxlength="200" value="${esc(c.title || "")}" placeholder="e.g. Log in with valid password"></label>
    <label class="field"><span>Type</span>
      <select name="type">
        ${TC_TYPES.map(([v, label]) => `<option value="${v}" ${(c.type || "functionality") === v ? "selected" : ""}>${esc(label)}</option>`).join("")}
      </select>
    </label>
    <label class="field"><span>Preconditions <span class="muted">(what you need before you start, one per line)</span></span><textarea name="preconditions" rows="2" placeholder="e.g. User has an active account">${esc((c.preconditions || []).join("\n"))}</textarea></label>
    <label class="field"><span>Steps <span class="muted">(the exact actions, one per line)</span></span><textarea name="steps" rows="3" placeholder="Enter email&#10;Enter password&#10;Click submit">${esc((c.steps || []).join("\n"))}</textarea></label>
    <label class="field"><span>Expected result <span class="muted">(what should happen at the end)</span></span><textarea name="expected" rows="2" required maxlength="1000" placeholder="e.g. The dashboard loads">${esc(c.expected || "")}</textarea></label>
  `;
}

function testCaseRowsHtml(cases, { editable = false, actionPrefix = "tc" } = {}) {
  const sorted = [...cases].sort((a, b) => {
    const sa = TC_STATUS[a.status]?.[2] ?? 99;
    const sb = TC_STATUS[b.status]?.[2] ?? 99;
    return sa - sb || a.id.localeCompare(b.id);
  });
  return sorted.map((c) => {
    const [label, tone] = TC_STATUS[c.status] || [c.status || "Unknown", "muted"];
    const where = (c.found_in && c.found_in.length) ? c.found_in : (c.assigned || []);
    const actions = editable ? `
      <div class="tc-actions">
        <button class="btn small ghost" data-${actionPrefix}-op="edit" data-id="${esc(c.id)}" aria-label="Edit test case ${esc(c.id)}">Edit</button>
        <button class="btn small ghost danger" data-${actionPrefix}-op="remove" data-id="${esc(c.id)}" aria-label="Delete test case ${esc(c.id)}">Delete</button>
      </div>` : "";
    return `<details class="item tc-row" data-id="${esc(c.id)}"><summary><span class="pill ${tone}">${esc(label)}</span>
      <span class="mono tc-id">${esc(c.id)}</span><span class="tc-title">${esc(c.title)}</span><span class="muted tc-type">${esc(tcTypeLabel(c.type))}${c.due ? "" : " · later task"}</span></summary>
      <div class="tc-body stack">
        ${c.preconditions?.length ? `<div><strong>Given</strong><ul>${c.preconditions.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
        ${c.steps?.length ? `<div><strong>Steps</strong><ol>${c.steps.map((t) => `<li>${esc(t)}</li>`).join("")}</ol></div>` : ""}
        <div><strong>Expected</strong> ${esc(c.expected || "")}</div>
        ${c.covers?.length ? `<div class="muted">Covers: ${c.covers.map(esc).join("; ")}</div>` : ""}
        ${where.length ? `<div class="muted mono">${c.status === "covered" ? "Found in" : "Assigned"}: ${where.map(esc).join(", ")}</div>`
          : c.status === "manual" ? "" : `<div class="muted">No test is assigned yet. The builder writes it, and it's checked after every build.</div>`}
        ${actions}
      </div></details>`;
  }).join("");
}

// Suites grouped by folder, like a file tree: a glance shows where the tests live and how many; open a folder to run
// one suite. A single folder (or a filter) shows its suites straight away.
function suiteGroupsHtml(suites, { open = false } = {}) {
  if (!suites.length) return `<div class="empty">No suites${open ? " match" : ""}.</div>`;
  const groups = new Map();
  for (const s of suites) {
    const folder = (s.path || "").split("/").slice(0, -1).join("/") || "(project root)";
    groups.set(folder, [...(groups.get(folder) || []), s]);
  }
  const expand = open || groups.size === 1;
  return [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([folder, rows]) => `
    <details class="fold suite-group"${expand ? " open" : ""}><summary class="card-h"><h3 class="mono">${esc(folder)}</h3>
      <span class="count">${rows.length} ${rows.length === 1 ? "suite" : "suites"} · ${plural(rows.reduce((n, s) => n + s.tests, 0), "test")}</span></summary>
      <div class="list">${rows.map((s) => `<div class="item"><div class="main-col"><div class="title">${esc(s.name)}</div><div class="meta">${plural(s.tests, "test")} · ${esc((s.path || "").split("/").pop())}</div></div>
        <div class="side"><button class="btn small" ${act("test_suite", { name: s.name })}>Run</button></div></div>`).join("")}</div></details>`).join("");
}

pages.tests = async (_, query) => {
  view.innerHTML = `<div class="empty">Finding tests…</div>`;
  const [data, caseView] = await Promise.all([api(`tests${query.get("refresh") ? "?refresh=1" : ""}`), api("test-cases").catch(() => ({ cases: [], summary: null }))]);
  const caseFilter = TC_STATUS[query.get("cases")] ? query.get("cases") : "all";
  const shownCases = caseView.cases.filter((c) => caseFilter === "all" || c.status === caseFilter);
  const byArea = new Map();
  for (const c of shownCases) byArea.set(c.area, [...(byArea.get(c.area) || []), c]);
  const cov = data.coverage;
  // Only a real measurement shows a number (older versions saved an estimate from the test count).
  const measured = cov && cov.overall_coverage_pct != null && !cov.estimated;
  const filter = (query.get("q") || "").toLowerCase();
  // Discovery also matches source files with no tests in them; those aren't runnable suites.
  const withTests = data.suites.filter((s) => s.tests > 0);
  const suites = withTests.filter((s) => !filter || s.name.toLowerCase().includes(filter));
  const total = withTests.reduce((n, s) => n + s.tests, 0);
  return {
    title: "Tests",
    sub: `${withTests.length} suites · ${total} tests`,
    actions: `<button class="btn primary test-run-all-btn" ${act("test")}>Run all tests</button><a class="btn test-expand-coverage-btn" href="#/new?type=coverage" data-href="#/new?type=coverage" title="Expand coverage (AI job)">Expand coverage</a>`,
    html: `
      ${data.error ? `<div class="notice bad">${esc(data.error)}</div>` : ""}
      ${caseView.cases.length ? `<section class="card mb-16"><div class="card-h"><h2>Test cases</h2>
        <div class="row gap-10 align-center">
          <div class="filters">${[["all", "All"], ...Object.entries(TC_STATUS).map(([k, v]) => [k, v[0]])].map(([k, label]) =>
            `<a class="btn small ${k === caseFilter ? "on" : ""}" href="#/tests?cases=${k}${query.get("q") ? `&q=${encodeURIComponent(query.get("q"))}` : ""}">${esc(label)}${k === "all" ? ` (${caseView.cases.length})` : ` (${caseView.summary[k]})`}</a>`).join("")}</div>
          <a class="btn small ghost" href="#/tests/cases">Manage cases</a>
        </div></div>
        <div class="card-b">${testCaseSummaryHtml(caseView.summary)}</div>
        ${[...byArea].map(([area, rows]) => `<div class="tc-area"><div class="tc-area-h">${esc(area)} <span class="count">${rows.length}</span></div><div class="list">${testCaseRowsHtml(rows)}</div></div>`).join("") || `<div class="empty">No cases with that status.</div>`}</section>` : ""}
      ${data.frameworks && (!state.project.languages?.length || state.project.languages.some((l) => l.name === "Swift")) ? `
      <section class="card"><div class="card-h"><h2>Test frameworks</h2>
        <div class="row">
          ${!data.frameworks.canary_suite?.installed ? `<button class="btn small primary" ${act("scaffold_canary")}>Add a canary suite</button>` : ""}
          <button class="btn small" ${act("visual_check")}>Simulator visual check</button>
        </div>
      </div>
      <div class="card-b framework-grid">
        ${Object.values(data.frameworks).map((f) => `
          <div class="framework">
            <div class="framework-h"><strong>${esc(f.name)}</strong>
              <span class="badge ${f.installed ? "good" : "muted-badge"}">${f.installed ? "Installed" : "Available"}</span></div>
            <div class="framework-desc">${esc(f.desc)}</div>
          </div>
        `).join("")}
      </div></section>` : ""}
      <section class="card"><div class="card-h"><h2>Coverage${measured ? ` <span class="count">${esc(ago(Date.parse(cov.timestamp) / 1000))}</span>` : ""}</h2>
        <button type="button" class="btn small" ${act("coverage")} title="Runs every test with coverage turned on, which may take time">${measured ? "Measure again" : "Measure coverage"}</button></div>
        <div class="card-b stack">${measured ? `<div class="row"><span class="big-number">${esc(cov.overall_coverage_pct)}%</span>
          <span class="muted">of ${esc(cov.metric || "lines")} covered${cov.tool ? `, measured with ${esc(cov.tool)}` : ""} · ${esc(cov.total_tests)} tests in ${esc(cov.total_suites)} suites</span></div>`
          : `<p class="muted">Not measured yet. Measuring runs every test with coverage turned on, which may take time.</p>`}
          ${cov?.last_error && (!measured || cov.last_error_at > cov.timestamp) ? `<div class="notice">${measured ? "The last attempt didn't measure: " : "Couldn't measure: "}${codeSpans(cov.last_error)}</div>` : ""}</div></section>
      ${data.plans.length ? `<section class="card"><div class="card-h"><h2>Test plans</h2></div><div class="list">${data.plans.map((p) => `
        <div class="item"><div class="main-col"><div class="title">${esc(p)}</div></div><div class="side"><button class="btn small" ${act("test_plan", { name: p })}>Run</button></div></div>`).join("")}</div></section>` : ""}
      <section class="card"><div class="card-h"><h2>Suites</h2>
        <form id="suite-filter" class="row"><input type="search" name="q" value="${esc(query.get("q") || "")}" placeholder="Filter" aria-label="Filter suites"></form></div>
        ${suiteGroupsHtml(suites, { open: Boolean(filter) })}</section>`,
    after: () => $("#suite-filter").addEventListener("submit", (e) => {
      e.preventDefault();
      location.hash = `#/tests?q=${encodeURIComponent(new FormData(e.target).get("q"))}`;
    }),
  };
};

pages["test-cases"] = async (_, query) => {
  view.innerHTML = `<div class="empty">Loading test cases…</div>`;
  const caseView = await api("test-cases").catch(() => ({ cases: [], summary: { total: 0, covered: 0, automated: 0, manual: 0, by_type: {}, covered_pct: null } }));
  const caseFilter = TC_STATUS[query.get("status")] ? query.get("status") : "all";
  const typeFilter = query.get("type") || "all";
  const areaFilter = query.get("area") || "all";
  const q = (query.get("q") || "").toLowerCase().trim();

  const allAreas = Array.from(new Set(caseView.cases.map((c) => c.area).filter(Boolean))).sort();
  const allTypes = TC_TYPES;

  const filtered = caseView.cases.filter((c) => {
    if (caseFilter !== "all" && c.status !== caseFilter) return false;
    if (typeFilter !== "all" && c.type !== typeFilter) return false;
    if (areaFilter !== "all" && c.area !== areaFilter) return false;
    if (q) {
      const match = (c.id && c.id.toLowerCase().includes(q))
        || (c.title && c.title.toLowerCase().includes(q))
        || (c.expected && c.expected.toLowerCase().includes(q))
        || (c.area && c.area.toLowerCase().includes(q))
        || (c.covers && c.covers.some((cv) => cv.toLowerCase().includes(q)));
      if (!match) return false;
    }
    return true;
  });

  const byArea = new Map();
  for (const c of filtered) {
    const a = c.area || "General";
    byArea.set(a, [...(byArea.get(a) || []), c]);
  }

  const queryUrl = (overrides) => {
    const params = new URLSearchParams(query);
    for (const [k, v] of Object.entries(overrides)) {
      if (v === null || v === undefined || v === "all" || v === "") {
        params.delete(k);
      } else {
        params.set(k, v);
      }
    }
    const qs = params.toString();
    return `#/tests/cases${qs ? `?${qs}` : ""}`;
  };

  const totalCases = caseView.cases.length;
  const covPct = caseView.summary?.covered_pct != null ? `${caseView.summary.covered_pct}%` : "0%";
  const sub = `${totalCases} case${totalCases === 1 ? "" : "s"} in library · ${caseView.summary?.covered || 0}/${caseView.summary?.automated || 0} automated covered (${covPct})`;

  return {
    title: "Test cases",
    sub,
    actions: `<button class="btn primary" id="btn-create-tc">New test case</button><a class="btn" href="#/tests">Run tests</a>`,
    html: `
      <section class="card mb-16">
        <div class="card-h">
          <h2>Summary</h2>
          <span class="count">${caseView.summary?.covered || 0}/${caseView.summary?.automated || 0} covered</span>
        </div>
        <div class="card-b">
          ${testCaseSummaryHtml(caseView.summary || {})}
        </div>
      </section>

      <section class="card mb-16">
        <div class="card-h">
          <h2>Filter &amp; Search</h2>
        </div>
        <div class="card-b stack">
          <form id="tc-search-form" class="row gap-10">
            <input type="search" name="q" value="${esc(query.get("q") || "")}" placeholder="Search test cases by ID, title, expected, covers…" aria-label="Search test cases" style="flex:1">
            <button class="btn small" type="submit">Search</button>
            ${q ? `<a class="btn small ghost" href="${queryUrl({ q: "" })}">Clear search</a>` : ""}
          </form>
          <div class="row gap-10" style="flex-wrap:wrap; align-items:center;">
            <div class="filters">
              ${[["all", "All"], ...Object.entries(TC_STATUS).map(([k, v]) => [k, v[0]])].map(([k, label]) =>
                `<a class="btn small ${k === caseFilter ? "on" : ""}" href="${queryUrl({ status: k })}">${esc(label)}${k === "all" ? ` (${caseView.cases.length})` : ` (${caseView.summary?.[k] ?? 0})`}</a>`).join("")}
            </div>
            ${allAreas.length > 1 ? `
              <div class="row gap-8" style="align-items:center;">
                <span class="muted" style="font-size:12px;">Area:</span>
                <select id="tc-area-select" class="btn small">
                  <option value="all" ${areaFilter === "all" ? "selected" : ""}>All areas</option>
                  ${allAreas.map((a) => `<option value="${esc(a)}" ${areaFilter === a ? "selected" : ""}>${esc(a)}</option>`).join("")}
                </select>
              </div>` : ""}
            <div class="row gap-8" style="align-items:center;">
              <span class="muted" style="font-size:12px;">Type:</span>
              <select id="tc-type-select" class="btn small">
                <option value="all" ${typeFilter === "all" ? "selected" : ""}>All types</option>
                ${allTypes.map(([t, label]) => `<option value="${t}" ${typeFilter === t ? "selected" : ""}>${esc(label)}</option>`).join("")}
              </select>
            </div>
          </div>
        </div>
      </section>

      ${filtered.length ? `
        <section class="card mb-16">
          <div class="card-h">
            <h2>Test cases</h2>
            <span class="count">${filtered.length} of ${caseView.cases.length}</span>
          </div>
          ${[...byArea].map(([area, rows]) => `
            <div class="tc-area">
              <div class="tc-area-h">${esc(area)} <span class="count">${rows.length}</span></div>
              <div class="list">${testCaseRowsHtml(rows, { editable: true, actionPrefix: "tc" })}</div>
            </div>`).join("")}
        </section>
      ` : `
        <section class="card mb-16">
          <div class="card-b">
            <div class="empty">
              ${caseView.cases.length ? "No test cases match the current filter." : "No test cases in library yet."}
              <div style="margin-top:12px;">
                <button class="btn small primary" id="btn-create-tc-empty">Create your first test case</button>
              </div>
            </div>
          </div>
        </section>
      `}
    `,
    after: () => {
      const handleCreate = async () => {
        const v = await formDialog("Create test case", testCaseForm(), "Create test case");
        if (!v) return;
        try {
          await api("test-cases", { method: "POST", body: { op: "create", ...v } });
          toast("Test case created");
          route();
        } catch (e) {
          toast(e.message, true);
        }
      };
      $("#btn-create-tc")?.addEventListener("click", handleCreate);
      $("#btn-create-tc-empty")?.addEventListener("click", handleCreate);

      $("#tc-search-form")?.addEventListener("submit", (e) => {
        e.preventDefault();
        const text = new FormData(e.target).get("q");
        location.hash = queryUrl({ q: text || "" });
      });

      $("#tc-area-select")?.addEventListener("change", (e) => {
        location.hash = queryUrl({ area: e.target.value });
      });

      $("#tc-type-select")?.addEventListener("change", (e) => {
        location.hash = queryUrl({ type: e.target.value });
      });

      view.querySelectorAll("[data-tc-op]").forEach((btn) => btn.addEventListener("click", async () => {
        const op = btn.dataset.tcOp;
        const caseId = btn.dataset.id;
        const currentCase = caseView.cases.find((c) => String(c.id) === String(caseId)) || {};

        if (op === "edit") {
          const v = await formDialog(`Edit test case (${caseId})`, testCaseForm(currentCase), "Save changes");
          if (!v) return;
          try {
            await api("test-cases", { method: "POST", body: { op: "edit", id: caseId, ...v } });
            toast("Test case updated");
            route();
          } catch (e) {
            toast(e.message, true);
          }
        } else if (op === "remove") {
          const ok = await formDialog(`Delete test case ${caseId}?`, `<p>Permanently remove test case <strong>${esc(caseId)}</strong> from the project library?</p>`, "Delete", { danger: true });
          if (!ok) return;
          try {
            await api("test-cases", { method: "POST", body: { op: "delete", id: caseId } });
            toast("Test case deleted");
            route();
          } catch (e) {
            toast(e.message, true);
          }
        }
      }));
    },
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
          <select id="git-branch-select" name="branch" class="mono" aria-label="Switch branch" style="margin-top: 4px; max-width: 200px;">
            ${g.branches.map((b) => branchOption(b, g.branch, g.elsewhere)).join("")}
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
  const here = location.hash; // settings live on Configuration pages and on Connections (Slack alerts)
  button.disabled = true;
  try {
    await api(`config/${request.part}`, {method: "POST", body: request.body});
    if (successMessage) toast(successMessage);
    if (location.hash === here) await route();
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
  if (section && !ConfigurationPages.resolve(section, config.viewer?.role || state.you?.role || "owner")) {
    return {title: "Configuration", sub: "", html: `<div class="notice">Only the owner of this computer can open that setting.</div>`};
  }
  if (section === "ai") {
    try { config.ai = await api("ai-providers"); } catch { config.ai = null; }
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
  if (section === "access") {
    return {
      ...result,
      after: () => {
        const onClick = async (event) => {
          const button = event.target.closest("[data-config-action]");
          if (!button || button.disabled) return;
          const action = button.dataset.configAction;
          if (action === "access-add") {
            showConfigMutationDialog(
              "Allow an email",
              `<label class="field"><span>Email address</span><input type="email" name="email" required autocapitalize="off"></label>
               <small class="hint-text">They can then sign in with Google and use everything on this computer.</small>`,
              "Allow",
              (values) => ({part: "allowed-email", body: {op: "add", email: values.email}}),
              "Email allowed",
            );
          } else if (action === "access-remove") {
            await runConfigMutation(button, {part: "allowed-email", body: {op: "remove", email: button.dataset.email}}, "Email removed and signed out");
          } else if (action === "access-revoke") {
            if (button.hasAttribute("data-current")) { await lockSession(); return; }
            button.disabled = true;
            try {
              await api("sign-ins/revoke", {method: "POST", body: {id: button.dataset.id}});
              toast("Sign-in ended");
              if (ConfigurationPages.routeMatches(current, section)) await route();
            } catch (error) {
              toast(error.message, true);
            } finally {
              if (button.isConnected) button.disabled = false;
            }
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
               <textarea name="content" style="min-height:220px; font-family:var(--mono); font-size:12px;" required>${esc(content || "")}</textarea></label>`,
              "Save Instructions",
              (values) => ({part: "role-prompts", body: {id, content: values.content}}),
              "Instructions saved"
            );
          } else if (configAction === "prompt-revert") {
            if (await formDialog(`Reset the ${name} instructions?`, `<p>Your edits are replaced with the built-in instructions.</p>`, "Reset", { danger: true })) {
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
            if (await formDialog(`Remove ${name}?`, `<p>Jobs stop running on this machine. Add it again any time.</p>`, "Remove machine", { danger: true })) {
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
  if (section !== "ai") return result;

  // Add an AI: its API keys card
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

// Trello's own approval page, then back to the app with the token (trello-auth.html passes it along).
function connectTrello(key) {
  const back = new URL("trello-auth.html", location.href).href;
  const q = new URLSearchParams({ key, name: "Orchestrator", scope: "read,write", expiration: "never", response_type: "token",
    callback_method: "fragment", return_url: back });
  location.href = `https://trello.com/1/authorize?${q}`;
}

function connectionStatus(p) {
  if (!p.connected) return pill("", "Not connected");
  if (p.rejected) return pill("failed", "Reconnect");
  if (p.expiry?.expired) return pill("failed", "Expired");
  if (p.expiry?.soon) return pill("attention", p.expiry.days_left === 0 ? "Expires today" : `Expires in ${plural(p.expiry.days_left, "day")}`);
  return pill("done", "Connected");
}

pages.connections = async (_, query) => {
  let list;
  try { list = (await api("integrations")).integrations; } catch (err) {
    if (err.status !== 404) throw err;
    return { title: "Connections", html: `<div class="notice">Connections aren't available yet. They're part of an update that hasn't reached your Orchestrator. Everything else works as usual. Check back soon.</div>` };
  }
  const owner = state.you?.role !== "member";
  const hook = owner ? (await api("config").catch(() => null))?.webhook : null;
  return {
    title: "Connections",
    sub: "Link jobs to tickets, errors and designs, and get alerts in Slack",
    html: `<div class="conn-grid">${list.map((p) => `
      <section class="card conn-card" data-conn="${esc(p.id)}">
        <div class="card-b stack">
          <div class="row"><strong class="conn-name">${esc(p.name)}</strong>${connectionStatus(p)}</div>
          <div class="muted">${esc(p.blurb)}</div>
          ${p.connected && p.summary ? `<div class="mono conn-summary">${esc(p.summary)}</div>` : ""}
          ${p.rejected ? `<div class="notice bad">${esc(p.name)} turned down the saved token. It may have expired or been revoked: reconnect to keep jobs linked.</div>` : ""}
          ${p.connected && p.expiry && !p.rejected ? `<div class="muted conn-expiry">${p.expiry.expired ? "The token expired on" : "The token expires on"} ${esc(new Date(`${p.expiry.on}T12:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }))}.</div>` : ""}
          ${p.connected && p.can_write ? `<div class="conn-options stack" data-conn-options="${esc(p.id)}" data-owner-only>
            <div class="muted conn-options-h">Keep it updated</div>
            <label class="check"><input type="checkbox" data-opt="comment_pr" ${p.options.comment_pr ? "checked" : ""}><span>Comment when a pull request opens</span></label>
            <label class="check"><input type="checkbox" data-opt="comment_merge" ${p.options.comment_merge ? "checked" : ""}><span>Comment when it's merged</span></label>
            <label class="check"><input type="checkbox" data-opt="move_on_merge" ${p.options.move_on_merge ? "checked" : ""}><span>${esc(p.move_label)}</span></label>
            ${p.move_default ? `<input type="text" data-opt="target" value="${esc(p.options.target)}" placeholder="${esc(p.move_default)}" aria-label="Target" ${p.options.move_on_merge ? "" : "disabled"}>` : ""}
          </div>` : ""}
          <div class="row" data-owner-only>
            ${p.authorize_key && !p.connected ? `<button class="btn small primary" data-trello-authorize="${esc(p.authorize_key)}">Connect with Trello</button>
              <button type="button" class="linklike" data-conn-connect="${esc(p.id)}">Use your own key instead</button>`
            : `<button class="btn small ${!p.connected || p.rejected || p.expiry?.expired || p.expiry?.soon ? "primary" : ""}" data-conn-connect="${esc(p.id)}">${!p.connected ? "Connect" : p.rejected || p.expiry?.expired ? "Reconnect" : "Update"}</button>`}
            ${p.connected ? `<button class="btn small danger" data-conn-disconnect="${esc(p.id)}">Disconnect</button>` : ""}
          </div>
        </div></section>`).join("")}</div>
      <p class="muted mt-12" data-owner-only>Credentials are saved on your computer, in this project's settings, and checked with the service before they're kept. They're never shown again.</p>
      ${hook ? `<div class="mt-16">${ConfigurationPages.chatCard(hook)}</div>` : ""}`,
    after: () => {
      ConfigurationPages.applyRole(state.you?.role || "owner", view);
      const onChat = async (event) => {
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
      view.addEventListener("click", onChat);
      cleanup.push(() => view.removeEventListener("click", onChat));
      if (query?.get("card") === "chat") $("#chat-alerts")?.scrollIntoView({ block: "start" });
      const onClick = async (ev) => {
        const connect = ev.target.closest("[data-conn-connect]"), disc = ev.target.closest("[data-conn-disconnect]");
        if (disc) {
          const p = list.find((x) => x.id === disc.dataset.connDisconnect);
          if (!(await formDialog(`Disconnect ${p.name}?`, `<p>Saved credentials are removed. Jobs already linked keep their links.</p>`, "Disconnect"))) return;
          try { await api(`integrations/${p.id}/disconnect`, { method: "POST", body: {} }); toast(`${p.name} disconnected`); route(); } catch (e) { toast(e.message, true); }
        } else if (connect) {
          const p = list.find((x) => x.id === connect.dataset.connConnect);
          const latest = p.token_max_days ? new Date(Date.now() + p.token_max_days * 86400000).toISOString().slice(0, 10) : "";
          const v = await formDialog(`${p.connected ? "Update" : "Connect"} ${p.name}`, `
            ${p.token_url ? `<p><a class="btn small" href="${esc(p.token_url)}" target="_blank" rel="noopener">Create a token ↗</a></p>` : ""}
            ${Object.keys(p.suggest || {}).length ? `<p class="muted">Filled in from the Sentry setup found in this project. Check it, then add a token.</p>` : ""}
            ${p.fields.map((f) => `
            <label class="field"><span>${esc(f.label)}</span>
              <input ${f.secret ? 'type="password"' : 'type="text"'} name="${esc(f.key)}" autocomplete="off" autocapitalize="off" spellcheck="false"
                value="${esc(!f.secret && p.suggest?.[f.key] ? p.suggest[f.key] : "")}"
                placeholder="${esc(p.connected && f.secret ? "saved: leave blank to keep" : f.placeholder)}">
              <small class="hint-text">${esc(f.help)}</small></label>`).join("")}
            ${p.token_max_days ? `<label class="field"><span>Token expires on</span><input type="date" name="expires" value="${esc(p.expiry?.on || latest)}" max="${latest}">
              <small class="hint-text">The date you picked when creating it. ${esc(p.name)} tokens last at most ${p.token_max_days} days; you'll be reminded before it runs out.</small></label>` : ""}`,
            p.connected ? "Save" : "Connect");
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
      const trelloStart = (ev) => { const b = ev.target.closest("[data-trello-authorize]"); if (b) connectTrello(b.dataset.trelloAuthorize); };
      view.addEventListener("click", trelloStart);
      cleanup.push(() => view.removeEventListener("click", trelloStart));
      // Back from Trello's approval page with a token: save it like any connection.
      let trelloToken = null;
      try { trelloToken = sessionStorage.getItem("orchestrator_trello_token"); sessionStorage.removeItem("orchestrator_trello_token"); } catch { /* storage blocked */ }
      const trello = list.find((x) => x.id === "trello");
      if (trelloToken && trello?.authorize_key) {
        api("integrations/trello/connect", { method: "POST", body: { values: { key: trello.authorize_key, token: trelloToken } } })
          .then((r) => { toast(`Trello connected${r.who ? ` as ${r.who}` : ""}`); route(); })
          .catch((e) => toast(e.message, true));
      }
      // Arriving to connect one app (Device logs → Connect Sentry): open its form straight away.
      const wanted = query?.get("connect");
      if (wanted) view.querySelector(`[data-conn-connect="${CSS.escape(wanted)}"]`)?.click();
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

// The job's context, grouped by what each thing is to the job (the ticket sits in the header): designs, and what went
// wrong. Beside the job on wide screens, under its top section on narrow ones. Empty groups don't show.
const ATTACH_ROLES = [["ticket", "Ticket"], ["design", "Design"], ["problem", "Log or error"]];

function contextItemHtml(it) {
  const href = it.url || (it.file ? `#/file?path=${encodeURIComponent(it.file)}` : it.files?.[0] ? `#/file?path=${encodeURIComponent(it.files[0])}` : "");
  const external = Boolean(it.url);
  const title = esc(it.title || "Untitled");
  return `<div class="item context-item">${it.image ? `<img class="context-thumb" alt="" data-auth-src="job-image?path=${encodeURIComponent(it.image)}">` : ""}
    <div class="main-col"><div class="title">${href ? `<a href="${esc(href)}"${external ? ' target="_blank" rel="noopener"' : ""}>${title}${external ? " ↗" : ""}</a>` : title}</div>
      <div class="meta">${esc([it.source, it.detail].filter(Boolean).join(" · "))}</div></div></div>`;
}

function jobContextRail(s, ctx) {
  ctx = ctx || { designs: [], problem: [] };
  const group = (title, items) => items.length ? `<section class="card rail-card"><div class="card-h"><h2>${title} <span class="count">${items.length}</span></h2></div>
    <div class="list">${items.map(contextItemHtml).join("")}</div></section>` : "";
  const groups = [group("Designs", ctx.designs || []), group("What went wrong", ctx.problem || [])];
  if (ctx.problem_first) groups.reverse();
  const empty = !(ctx.designs || []).length && !(ctx.problem || []).length;
  const add = (role, text) => `<button type="button" class="linklike" data-attach-context="${esc(s.id)}" data-attach-role="${role}">${text}</button>`;
  return `<aside class="job-rail" aria-label="Context">
    <div class="rail-h"><span class="label">Context</span>${empty ? "" : `<button type="button" class="btn small" data-attach-context="${esc(s.id)}" data-attach-role="${ctx.problem_first ? "problem" : "design"}">Attach…</button>`}</div>
    ${empty ? `<p class="rail-empty muted">Add ${add("ticket", "a ticket")}, ${add("design", "a design")} or ${add("problem", "a log or error")}. The AI reads it on its next step.</p>` : groups.join("")}
  </aside>`;
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-attach-context]");
  if (b) attachToJob(b.dataset.attachContext, b.dataset.attachRole || "");
});

// One way to attach anything: say what it is, then where it comes from (a connected app, a link, a file, or pasted text).
// It only attaches; nothing runs.
async function attachToJob(jobId, role = "") {
  const APP_FOR = { ticket: ["jira", "trello"], design: ["figma"], problem: ["sentry"] };
  const ACCEPT = { ticket: ".pdf,.md,.txt,image/*", design: "image/*,.pdf,.fig,.html,.htm", problem: "image/*,.log,.txt,.json,.md,.crash,.ips" };
  const picker = await linkPickerHtml({ prefer: APP_FOR[role] || [], label: "From a connected app", emptyHint: "" });
  role = role || "design";
  const dlgPromise = formDialog("Attach to this job", `
    <fieldset class="field attach-roles"><span>What is it?</span>
      <div class="segmented" role="radiogroup" aria-label="What is it?">${ATTACH_ROLES.map(([value, label]) => `<label><input type="radio" name="role" value="${value}" ${value === role ? "checked" : ""}>${label}</label>`).join("")}</div></fieldset>
    ${picker.html}
    <label class="field"><span>A link</span><input type="url" name="url" placeholder="https://" spellcheck="false" autocapitalize="off"></label>
    <div class="field"><span>A file</span>
      <input type="file" id="attach-file" class="sr-only" aria-label="Choose a file to attach">
      <label class="prd-drop upload-drop" for="attach-file" id="attach-drop"><strong>Choose a file</strong><span class="muted drop-hint">or drop it here</span></label>
      <small class="hint-text" id="attach-file-name"></small><input type="hidden" name="upload"></div>
    <label class="field" id="attach-text"><span>Or paste the log</span><textarea name="text" rows="4" spellcheck="false" placeholder="Stack trace, console output…"></textarea></label>
    <label class="field"><span>Note <span class="muted">(optional)</span></span><input type="text" name="note" placeholder="What to look at"></label>
    <small class="hint-text">It's added to this job and the AI reads it on its next step. Nothing runs.</small>`, "Attach");
  picker.wire();
  const body = $("#dialog-body"), file = $("#attach-file"), drop = $("#attach-drop");
  const sync = () => {
    const now = body.querySelector('input[name="role"]:checked')?.value || "design";
    $("#attach-text").hidden = now !== "problem";
    file.accept = ACCEPT[now];
    const sel = body.querySelector(".lp-provider");
    const app = sel && (APP_FOR[now] || []).find((id) => [...sel.options].some((o) => o.value === id));
    if (app && sel.value !== app) { sel.value = app; sel.dispatchEvent(new Event("change")); }
  };
  body.querySelector(".attach-roles").addEventListener("change", sync);
  const take = async (f) => {
    if (!f) return;
    if (f.size > NJ_UPLOAD_LIMIT) { toast(`${f.name} is over ${NJ_UPLOAD_LIMIT / 1048576} MB`, "warning"); return; }
    $("#attach-file-name").textContent = `Uploading ${f.name}…`;
    try { body.querySelector('input[name="upload"]').value = (await uploadFile(f)).path; $("#attach-file-name").textContent = f.name; }
    catch (err) { $("#attach-file-name").textContent = ""; toast(`${f.name}: ${err.message}`, true); }
  };
  file.addEventListener("change", () => take(file.files[0]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); take(e.dataTransfer?.files?.[0]); });
  sync();
  const v = await dlgPromise;
  if (!v) return;
  const links = JSON.parse(v.links || "[]");
  const direct = v.url?.trim() || v.upload || (v.role === "problem" && v.text?.trim());
  if (!links.length && !direct) { toast("Pick something to attach: an item from an app, a link, a file or pasted text.", "warning"); return; }
  try {
    if (links.length) await api(`jobs/${encodeURIComponent(jobId)}/links`, { method: "POST", body: { links } });
    if (direct) await api(`jobs/${encodeURIComponent(jobId)}/attach`, { method: "POST", body: { role: v.role, url: v.url?.trim() || "", upload: v.upload || "", text: v.role === "problem" ? v.text || "" : "", note: v.note || "" } });
    toast("Attached"); route();
  } catch (err) { toast(err.message, true); }
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
  // An idea told before setup (Mac app, the hosted app's first screen, the no-project screen) is saved as the draft
  // straight away, so it can't be lost, and never silently swapped for a different draft already in progress.
  const idea = query.get("pitch") ? { pitch: query.get("pitch"), name: query.get("name") || "" } : Account.pendingIdea();
  const ideaAnswers = idea?.pitch ? { pitch: idea.pitch.trim().slice(0, 200), name: (idea.name || "").trim().slice(0, 200) } : null;
  const forgetIdea = () => { Account.clearIdea(); if (query.get("pitch")) history.replaceState(null, "", "#/new-project"); };
  let ideaChoice = "";
  if (ideaAnswers) {
    const current = (data.draft?.answers?.pitch || "").trim();
    if (!data.draft) {
      draft.answers = { ...draft.answers, ...ideaAnswers };
      try { await npSave(draft); forgetIdea(); } catch { /* kept in the form and in this browser; saved on Next */ }
    } else if (current === ideaAnswers.pitch) {
      forgetIdea();
    } else {
      ideaChoice = `<div class="banner attention np-idea-choice"><p>You were already starting <strong>“${esc(current || draft.answers.name || "a project")}”</strong>.
        Continue that, or start over with your new idea, <strong>“${esc(ideaAnswers.pitch)}”</strong>?</p>
        <div class="row gap-10"><button type="button" class="btn small" id="np-keep-draft">Continue what I started</button>
          <button type="button" class="btn small primary" id="np-use-idea">Start with the new idea</button></div></div>`;
    }
  }
  const wireIdeaChoice = () => {
    $("#np-keep-draft")?.addEventListener("click", () => { forgetIdea(); route(); });
    $("#np-use-idea")?.addEventListener("click", async () => {
      try {
        await api("new-project/discard", { method: "POST", body: {} });
        await npSave({ answers: ideaAnswers, step: "describe" });
        forgetIdea();
        route();
      } catch (err) { toast(err.message, true); }
    });
  };
  draft.parent = draft.parent || data.default_parent;
  const gh = data.github;
  const step = draft.created_root ? "create" : (query.get("step") || draft.step || "describe");
  const answersFrom = (form) => Object.fromEntries(data.questions.map((q) => [q.key, q.kind === "multi" ? new FormData(form).getAll(q.key).join(", ") : (new FormData(form).get(q.key) || "").toString().trim()]));
  const resume = ideaChoice ? ideaChoice : data.draft && !query.get("step") && draft.step !== "describe"
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
    // What's needed to start comes first; everything optional waits behind one fold (NN/g: progressive disclosure).
    const needed = data.questions.filter((q) => q.required);
    const optional = data.questions.filter((q) => !q.required);
    const filledOptional = optional.some((q) => (draft.answers[q.key] || "").trim());
    const sectionsHtml = `<fieldset class="np-section-card stack"><legend class="np-section-legend">The basics</legend>
        ${needed.map((q) => npQuestion(q, draft.answers[q.key] || "")).join("")}</fieldset>
      ${optional.length ? `<details class="np-section-card np-more"${filledOptional ? " open" : ""}><summary class="np-section-legend">Add more detail (optional)</summary>
        <div class="stack">${optional.map((q) => npQuestion(q, draft.answers[q.key] || "")).join("")}</div></details>` : ""}`;

    return {
      title: "Start a new project", sub: npStepper("describe"),
      html: `${resume}<form class="card card-b stack np-form" id="np-describe">
        <p class="muted">Short answers are fine. They become your product description, which you can change any time.</p>
        ${sectionsHtml}
        <div class="row"><span class="spacer"></span>${discard}<button class="btn primary big" type="submit">Next: where it lives</button></div></form>`,
      after: () => {
        wireIdeaChoice();
        // "Not sure" and a platform contradict each other: picking one clears the other.
        $("#np-describe").addEventListener("change", (e) => {
          if (e.target.name !== "platform" || !e.target.checked) return;
          const notSure = e.target.value.startsWith("Not sure");
          e.currentTarget.querySelectorAll('input[name="platform"]').forEach((box) => { if (box !== e.target && box.value.startsWith("Not sure") !== notSure) box.checked = false; });
        });
        $("#np-describe").addEventListener("submit", async (e) => {
          e.preventDefault();
          const picked = answersFrom(e.target);
          if (!picked.platform) { toast("Pick at least one platform, or choose “Not sure”.", "warning"); return; }
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
        wireIdeaChoice();
        const form = $("#np-where");
        const collect = (extra = {}) => ({ ...draft, host: form.host.value, visibility: form.visibility?.value || draft.visibility, parent: form.parent.value.trim(), ...extra });
        form.querySelectorAll('input[name="host"]').forEach((r) => r.addEventListener("change", async () => { await npSave(collect({ step: "where" })); route(); }));
        $("#np-back").addEventListener("click", async () => { await npSave(collect({ step: "describe" })); location.hash = "#/new-project?step=describe"; route(); });
        $("#np-recheck")?.addEventListener("click", () => route());
        $("#np-local")?.addEventListener("click", async () => { await npSave(collect({ host: "local", waiting_on_github: false })); route(); });
        $("#np-signin")?.addEventListener("click", async () => { await npSave(collect({ waiting_on_github: true, step: "where" })); signInToGitHub(); });
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
      <section class="card mt-16"><div class="card-h"><h2>Product requirements ready</h2></div><div class="card-b stack">
        <div>Your product requirements document is saved with all five PRD sections. You can review or refine it anytime as the project evolves.</div>
        <div><a class="btn" href="#/product">Open the product requirements</a></div></div></section>
      <div class="row mt-16"><a class="btn primary big" href="#/readiness">Set up this project</a><a class="btn big" href="#/">Open dashboard</a></div>
      <p class="muted">“Set up this project” lists what jobs need (an AI, a machine, how it's built and tested), each with a way to do it here.</p>`,
    after: () => {
      $("#np-recheck")?.addEventListener("click", () => route());
      $("#np-signin")?.addEventListener("click", () => signInToGitHub());
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
  // Runs from before Orchestrator last started are read back from their saved logs (owners only: logs are private).
  const earlier = state.you?.role === "member" ? [] : ((await api("runs/history").catch(() => null))?.runs || []);
  const earlierRow = (r) => `<a class="item" href="#/file?path=${encodeURIComponent(r.log)}">
    <div class="main-col"><div class="title">${esc(r.title)}</div><div class="meta">${esc(ago(r.started))}</div></div><div class="side"><span class="muted">Log</span></div></a>`;
  return {
    title: "Activity",
    sub: "Everything run from this app: builds, tests, plans and fixes.",
    html: `
      ${running.length ? `<section class="card mb-16"><div class="card-h"><h2>Running</h2><span class="count">${running.length}</span></div><div class="list">${running.map(liveRunCard).join("")}</div></section>` : ""}
      ${finished.length || !earlier.length ? `<section class="card mb-16"><div class="card-h"><h2>Finished</h2><span class="count">${finished.length || ""}</span></div><div class="list">${finished.map(runItem).join("") || `<div class="empty">Nothing has run yet. Start one with New job, or run your tests from Tests.</div>`}</div></section>` : ""}
      ${earlier.length ? `<section class="card"><div class="card-h"><h2>Earlier runs</h2><span class="count">${earlier.length}</span></div>
        <div class="list" id="earlier-runs">${earlier.map((r, n) => n < 10 ? earlierRow(r) : earlierRow(r).replace("<a ", "<a hidden ")).join("")}</div>
        ${earlier.length > 10 ? `<div class="card-b"><button type="button" class="btn small" data-show-all="earlier-runs">Show all ${earlier.length}</button></div>` : ""}</section>` : ""}`,
  };
};

pages.file = async (_, query) => {
  const path = query.get("path") || "";
  const { text } = await api(`file?path=${encodeURIComponent(path)}`);
  return {
    title: path.split("/").pop(),
    sub: esc(path),
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
  if (state.you?.role !== "member") {
    try { unfinished = (await api("new-project")).draft; } catch { /* older server */ }
  }

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
        <div class="project-card-badges">
          ${p.active ? '<span class="pill-badge active-badge">Active</span>' : ""}
          ${renderSourceBadge(p)}
          ${p.configured ? "" : '<span class="pill-badge muted-badge" title="No .orchestrator/project.json yet">Not set up</span>'}
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
        <span class="muted">${plural(p.jobs_count, "job")}</span>
      </div>
      <div class="project-card-actions">
        ${p.active ? `
          <a class="btn small" href="#/">Open</a>
        ` : `
          ${state.you?.role !== "member" ? `<button class="btn small ghost" data-forget-project="${esc(p.root)}" title="Remove from list">Forget</button>` : ""}
        `}
      </div>
    </div>
  `).join("");

  return {
    title: "Projects",
    sub: "Manage and switch between codebases on this machine.",
    actions: state.you?.role !== "member" ? `
      <div class="projects-actions">
        <a class="btn primary" href="#/new-project"><svg class="icon"><use href="#i-plus"/></svg><span>Start a new project</span></a>
        <button class="btn" id="add-project-btn"><svg class="icon"><use href="#i-folder"/></svg><span>Add existing project</span></button>
      </div>` : "",
    html: `
      <div class="projects-container">
        ${unfinished ? `<div class="banner attention mb-16"><p><strong>Unfinished new project${unfinished.answers?.name ? `: ${esc(unfinished.answers.name)}` : ""}.</strong>${unfinished.waiting_on_github ? " Waiting on GitHub." : ""}</p><a class="btn small primary" href="#/new-project">Continue</a></div>` : ""}
        <div class="projects-head">
          <h2>Your projects</h2>
          <div class="filters projects-filter">
            <button type="button" class="btn small on" data-project-filter="all">All (${pList.length})</button>
            <button type="button" class="btn small" data-project-filter="github"><svg class="icon badge-icon"><use href="#i-github"/></svg>GitHub (${githubCount})</button>
            <button type="button" class="btn small" data-project-filter="local"><svg class="icon badge-icon"><use href="#i-folder"/></svg>Local (${localCount})</button>
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
  // The top-left Back returns to the job (or wherever you came from); opened directly, it falls back to the job.
  if (r.job && current.page === "run") current.parent = `#/jobs/${encodeURIComponent(r.job)}`;
  return {
    title: r.title,
    sub: `<span class="status-line">${runPill(r)}<span class="mono">${esc(r.command)}</span></span>`,
    actions: r.running ? `<button class="btn danger" data-stop="${esc(r.id)}">Stop</button>` : "",
  };
}

function nextStep(r) {
  if (r.action === "console") {
    return `<div class="banner"><p>Console connection was stopped.</p></div>`;
  }
  if (r.stopped) {
    const target = r.result_job || r.job;
    const open = target ? `<a class="btn small primary" href="#/jobs/${encodeURIComponent(target)}">Open job</a>` : "";
    return `<div class="banner"><p>Run was stopped.</p>${open}</div>`;
  }
  const failed = r.exit_code !== 0;
  const target = r.result_job || r.job;
  const open = r.result_job && !r.job ? `<a class="btn small primary" href="#/jobs/${encodeURIComponent(r.result_job)}">Open job</a>` : ""; // Back covers returning to the job
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
    // Orchestrator restarted since this run (it stops runs it started), but the run's log was saved: link straight to it.
    const saved = state.you?.role === "member" ? null : ((await api("runs/history").catch(() => null))?.runs || []).find((r) => r.id === id);
    return saved
      ? { title: saved.title, sub: `Started ${esc(ago(saved.started))}`,
          html: `<div class="notice"><p>Orchestrator has restarted since this run, so it isn't live here any more (if it was still going, the restart stopped it). Its full output was saved.</p>
            <div class="row mt-8"><a class="btn small" href="#/file?path=${encodeURIComponent(saved.log)}">Open the saved log</a><a class="btn small ghost" href="#/activity">All runs</a></div></div>` }
      : { title: "Run not found", html: `<div class="notice"><p>This run isn't running, and no saved log matches it.</p>
            <div class="row mt-8"><a class="btn small" href="#/activity">See earlier runs</a></div></div>` };
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
  const toBytes = (b64) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  };
  // On this computer an open event stream is the quickest. Through a tunnel it isn't: some (Cloudflare's free ones) hold
  // the stream back until it ends, so the terminal would stay blank. There each wait for output is a plain request.
  const follow = backend ? followByPolling(id, toBytes, write) : followByStream(id, token, toBytes, write);
  follow.onEnd(async () => {
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
  cleanup.push(follow.stop);
}

// Both return { onEnd(fn), stop() }: fn runs once, when the run is over.
function followByStream(id, token, toBytes, write) {
  const es = new EventSource(`/api/runs/${id}/stream?offset=0${token ? `&token=${encodeURIComponent(token)}` : ""}`);
  es.addEventListener("data", (e) => write(toBytes(JSON.parse(e.data).data)));
  return {
    onEnd: (fn) => es.addEventListener("end", () => { es.close(); fn(); }),
    stop: () => es.close(),
  };
}

function followByPolling(id, toBytes, write) {
  let stopped = false, ended = () => {};
  (async () => {
    let offset = 0, failures = 0;
    while (!stopped) {
      try {
        const out = await api(`runs/${id}/output?offset=${offset}&wait=15`);
        failures = 0;
        offset = out.offset;
        if (out.data && !stopped) write(toBytes(out.data));
        if (out.finished) { if (!stopped) ended(); return; }
      } catch (err) {
        if (err.status === 401 || err.status === 404) return; // signed out, or the run is gone: nothing more to show
        failures += 1;
        await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * failures, 5000))); // a dropped connection: keep trying
      }
    }
  })();
  return { onEnd: (fn) => { ended = fn; }, stop: () => { stopped = true; } };
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
  if (parts[0] === "tests" && parts[1] === "cases") return { page: "test-cases", args: [], nav: "tests", query }; // the case library, under Tests
  if (parts[0] === "jobs") return { page: "home", args: [], nav: "home", query }; // Jobs list lives on Home
  if (parts[0] === "config") {
    const section = ConfigurationPages.resolve(parts[1]);
    return { page: "config", args: section ? [section.id] : [], nav: "config", query };
  }
  if (parts[0] === "connect" || parts[0] === "computers") return { page: parts[0], args: [], nav: null, query };
  if (parts[0] === "new-project") return { page: "new-project", args: [], nav: "config", query };
  if (["connections", "projects"].includes(parts[0])) return { page: parts[0], args: [], nav: "config", query }; // under Settings
  if (parts[0] === "readiness") return { page: "readiness", args: [], nav: setupState && !setupState.complete ? "readiness" : "config", query };
  if (parts[0] === "product") return { page: "product", args: [], nav: "product", query };
  if (parts[0] === "docs") return { page: "docs", args: parts.slice(1), nav: "docs", query };
  if (pages[parts[0]]) return { page: parts[0], args: [], nav: parts[0], query };
  return { page: "home", args: [], nav: "home", query };
}

// Pages one level down (a job, a run, a document, a setting, a form) show "Back". It returns to the page you came
// from in this app; opened directly (a shared link, a reload), it goes to the page's parent instead.
function routeParent(r) {
  if (r.page === "job" || r.page === "new" || r.page === "file") return "#/";
  if (r.page === "run") return "#/activity";
  if (r.page === "docs" && r.args.length) return "#/docs";
  if (r.page === "config" && r.args.length) return "#/config";
  if (r.page === "new-project") return "#/projects";
  if (r.page === "test-cases") return "#/tests";
  return null;
}
const navStack = [];
function trackNavigation() {
  const here = location.hash || "#/";
  if (navStack.length > 1 && navStack[navStack.length - 2] === here) navStack.pop(); // went back
  else if (navStack[navStack.length - 1] !== here) navStack.push(here);
}
document.addEventListener("click", (e) => {
  const link = e.target.closest("#back-link");
  if (!link || e.metaKey || e.ctrlKey || e.shiftKey) return;
  if (navStack.length > 1) { e.preventDefault(); history.back(); }
});

const signatureOf = (r) => `${r.title}|${r.sub}|${r.actions}|${r.html}`;

// Long content on a busy page: shown up to a height with a fade, and a centred "Show more" under it that grows the
// card to fit. Never an inner scroll box. Mark the element with data-expandable="<px>" (default 320).
function wireExpandables(root = document) {
  root.querySelectorAll("[data-expandable]:not([data-expandable-ready])").forEach((box, n) => {
    if (!box.offsetParent) return; // not laid out yet (inside a closed fold): try again next time
    box.dataset.expandableReady = "1";
    const limit = Number(box.dataset.expandable) || 320;
    if (box.scrollHeight <= limit + 120) return; // a little longer is fine; folding it would hide almost nothing
    box.id ||= `expandable-${Date.now()}-${n}`;
    box.style.setProperty("--clamp", `${limit}px`);
    box.classList.add("expandable", "is-clamped");
    const bar = document.createElement("div");
    bar.className = "expandable-bar";
    bar.innerHTML = `<button type="button" class="btn small" aria-expanded="false" aria-controls="${box.id}">Show more</button>`;
    box.after(bar);
    bar.firstElementChild.addEventListener("click", (e) => {
      const button = e.currentTarget;
      const opening = box.classList.contains("is-clamped");
      button.textContent = opening ? "Show less" : "Show more";
      button.setAttribute("aria-expanded", String(opening));
      slideExpandable(box, limit, opening);
    });
  });
}

// Slide between the clamped height and the full height. max-height can't animate to "none", so animate to the
// measured height and drop the inline value once the slide ends. Reduced motion: no slide.
function slideExpandable(box, limit, opening) {
  const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const settle = () => { box.style.maxHeight = ""; box.classList.remove("is-sliding"); };
  if (still) { box.classList.toggle("is-clamped", !opening); settle(); return; }
  box.classList.add("is-sliding");
  const ms = Math.min(600, 220 + Math.abs(box.scrollHeight - limit) * 0.25); // longer travel, a little longer slide
  box.style.setProperty("--slide", `${Math.round(ms)}ms`);
  box.style.maxHeight = `${opening ? limit : box.scrollHeight}px`;
  void box.offsetHeight; // start from there
  box.classList.toggle("is-clamped", !opening); // the fade crossfades with the slide
  box.style.maxHeight = `${opening ? box.scrollHeight : limit}px`;
  if (!opening && box.getBoundingClientRect().top < 0) box.scrollIntoView({ block: "start", behavior: "smooth" });
  let done = false;
  const finish = (e) => { if (done || (e && (e.target !== box || e.propertyName !== "max-height"))) return; done = true; box.removeEventListener("transitionend", finish); settle(); };
  box.addEventListener("transitionend", finish);
  setTimeout(finish, ms + 150); // in case the transition never reports its end
}

function apply(result) {
  setHeader(result);
  view.innerHTML = result.html;
  ConfigurationPages.applyRole(state.you?.role || "owner", document);
  current.rendered = signatureOf(result);
  if (result.after) result.after();
  wireExpandables(view);
}

async function route() {
  if (signingIn) return; // the loading screen stays until sign-in finishes
  const moved = RouteAliases.redirect(location.hash); // an old address: show the page where it lives now
  if (moved) history.replaceState(null, "", moved);
  navigationDrawer.close();
  // Anything tied to the previous page goes: terminals, streams, and dialogs,
  // so an action can never run against a page you've left.
  cleanup.forEach((fn) => { try { fn(); } catch {} });
  cleanup = [];
  const dlg = $("#dialog");
  if (dlg.open) dlg.close("cancel");

  const r = resolveRoute();
  if (state.setup?.needs_project && !["connect", "computers", "new-project"].includes(r.page)) r.page = "setup";
  if (parseHash().parts[0] === "setup") r.page = "setup";
  if (r.page === "connect" || r.page === "computers") { // the hosted app's account screens
    if (!Account.active()) { location.hash = "#/"; return; }
    if (r.page === "connect") Account.pendingCode();
    if (!window.firebase?.auth?.().currentUser) return showSignInGate(r.page === "connect" ? "Sign in to add your computer to your account." : "");
    return openAccount({ list: r.page === "computers" });
  }
  trackNavigation();
  current = { page: r.page, args: r.args, query: r.query, rendered: "", parent: routeParent(r) };
  document.querySelectorAll(".nav [data-route], .tabbar [data-route]").forEach((item) => {
    const active = item.dataset.route === r.nav;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
  });
  renderSetupFab();
  try {
    if (!state.project && !["setup", "new-project"].includes(r.page)) {
      await refreshState();
      if (!state.project) return;
    }
    // A page that takes a moment (first load runs setup checks) shouldn't leave a blank screen.
    const slow = setTimeout(() => { if (current.page === r.page && !view.innerHTML.trim()) view.innerHTML = `<div class="empty" role="status">Loading…</div>`; }, 400);
    let result;
    try {
      result = r.page === "setup" ? await DesktopSetup.render({api, esc, root: r.query.get("root"),
        onReady: async () => { await refreshState(); location.hash = "#/"; await route(); }})
        : await pages[r.page](r.args, r.query);
    } finally { clearTimeout(slow); }
    if (current.page === r.page && current.args.join() === r.args.join()) {
      apply(result);
      if (r.page !== "run") $("#page-title")?.focus({ preventScroll: true }); // so screen readers announce the new page
    }
  } catch (e) {
    if (e.status === 401) return showLocked(e.message);
    // Say what happened and offer a way on, never a dead end.
    const missing = e.status === 404;
    setHeader({ title: missing ? "Couldn't find that" : "This page didn't load" });
    view.innerHTML = `<div class="notice bad"><p>${esc(Errors.explain(e.message))}</p>
      <div class="row mt-8"><a class="btn small" href="#/">Go to Home</a>${missing ? "" : `<button type="button" class="btn small" data-retry-route>Try again</button>`}</div></div>`;
    view.querySelector("[data-retry-route]")?.addEventListener("click", () => route());
  }
}

// Live pages refresh in place — but never under the user's hands.
async function tick() {
  await refreshState(); // also while the tab is hidden, so notifications can fire
  if (document.hidden) return;
  if (!state.project) return;
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

const navigationDrawer = NavigationDrawer.mount({document, window});

// ---------------------------------------------------------------- command palette
const PALETTE_PAGES = [["Home", "#/", "What needs you, and all jobs"], ["Product", "#/product", "Pitch, who it's for, features, look and feel, what not to build"], ["Docs", "#/docs", "Every feature, job and project file in one place"],
  ["Projects", "#/projects", "Switch, add or start a project"], ["Activity", "#/activity", "Runs and live output"], ["Device logs", "#/devlogs", "Logs from test devices"], ["Tests", "#/tests", "Test cases, suites, coverage"],
  ["Test cases", "#/tests/cases", "Manage test cases library, coverage and definitions"],
  ["Git", "#/git", "Branches and changes"], ["Delivery", "#/delivery", "What's live, with testers, pipeline"], ["Measure", "#/measure", "KPIs and analytics"], ["Check-up", "#/checkup", "What the product has and what's missing"], ["Readiness", "#/readiness", "Can jobs run here: setup, build tools, checks"], ["UX review", "#/ux-review", "Check screens against usability and design principles"],
  ["Connections", "#/connections", "Jira, Trello, Sentry, Figma, Slack alerts"], ["Configuration", "#/config", "Models, keys, machines, alerts"], ["Help", "#/help", "How it works, glossary"], ["New job", "#/new", "Describe work to be done"],
  ["Start a new project", "#/new-project", "Describe an idea and set it up"]];
const palette = { open: false, entries: [], shown: [], active: 0, opener: null };

function paletteBase() {
  const go = (hash) => () => { location.hash = hash; };
  const entries = PALETTE_PAGES.filter(([, hash]) => !(hash === "#/devlogs" && state.project?.mobile_app === false) && !(hash === "#/new-project" && state.you?.role === "member")).map(([label, hash, hint]) => ({ label, hint, group: "Pages", order: 1, run: go(hash) }));
  const act_ = (label, hint, fn) => entries.push({ label, hint, group: "Actions", order: 0, run: fn });
  act_("Run all tests", "Manual test run", () => runAction("test", {}));
  act_("Lock session", "Sign out of this browser", () => $("#lock-btn")?.click());
  if (Notifications.supported()) act_(Notifications.enabled() ? "Turn off browser alerts" : "Turn on browser alerts", "Alerts when a run finishes or needs you", () => $("#notify-btn")?.click());
  return entries;
}

async function openPalette() {
  if (palette.open) return;
  navigationDrawer.close();
  palette.open = true;
  palette.opener = document.activeElement;
  palette.entries = paletteBase();
  $("#palette-backdrop").hidden = false;
  $("#palette").hidden = false;
  const input = $("#palette-input");
  input.value = "";
  renderPalette();
  input.focus();
  // Jobs load after it opens, so it is usable straight away.
  try {
    const [{ jobs }] = await Promise.all([api("jobs")]);
    if (!palette.open) return;
    const go = (hash) => () => { location.hash = hash; };
    palette.entries.push(...jobs.map((j) => ({ label: j.title, hint: `${j.state.label} · ${j.kind}`, group: "Jobs", order: 2, run: go(`#/jobs/${encodeURIComponent(j.id)}`) })));
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

// On the hosted app, alerts also come as push through your account, so they arrive with the app closed.
const PUSH_KEY = "orchestrator_push_token";
const isIos = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isInstalled = () => navigator.standalone === true || window.matchMedia?.("(display-mode: standalone)").matches;
const needsHomeScreen = () => Account.active() && isIos() && !isInstalled(); // iOS only allows web push for home-screen apps

function deviceLabel() {
  const ua = navigator.userAgent;
  const device = isIos() ? (/iPad/.test(ua) || navigator.maxTouchPoints > 1 && !/iPhone/.test(ua) ? "iPad" : "iPhone")
    : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "Linux";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : "Safari";
  return `${device} · ${browser}`;
}

async function enablePush() {
  if (!Account.active() || !window.firebase?.messaging || !("serviceWorker" in navigator)) return;
  const registration = await navigator.serviceWorker.register("firebase-messaging-sw.js");
  const token = await firebase.messaging().getToken({ serviceWorkerRegistration: registration });
  if (!token) throw new Error("This browser didn't allow push alerts.");
  await cpApi("push/register", { method: "POST", body: { token, label: deviceLabel() } });
  try { localStorage.setItem(PUSH_KEY, token); } catch { /* registered; just not remembered here */ }
}

async function disablePush() {
  let token = null;
  try { token = localStorage.getItem(PUSH_KEY); localStorage.removeItem(PUSH_KEY); } catch { /* nothing kept */ }
  if (!token) return;
  try { await cpApi("push/unregister", { method: "POST", body: { token } }); } catch { /* signed out: the token lapses on its own */ }
  try { await firebase.messaging().deleteToken(); } catch { /* already gone */ }
}

const notifyBtn = $("#notify-btn");
function renderNotifyBtn() {
  if (!notifyBtn) return;
  if (needsHomeScreen()) {
    notifyBtn.hidden = false;
    notifyBtn.textContent = "Get alerts on this phone";
    notifyBtn.title = "Add Orchestrator to your Home Screen first";
    return;
  }
  if (!Notifications.supported()) return;
  notifyBtn.hidden = false;
  const on = Notifications.enabled();
  notifyBtn.textContent = on ? "Notifications on" : "Notify me when done";
  notifyBtn.title = on ? "Click to turn off"
    : Account.active() ? "Alerts on this device when a run finishes, fails or needs you, even with Orchestrator closed"
    : "Browser alerts when a run finishes, fails or needs you";
}
notifyBtn?.addEventListener("click", async () => {
  if (needsHomeScreen()) {
    toast("First add Orchestrator to your Home Screen (Share, then Add to Home Screen) and open it from there.", "info");
    return;
  }
  if (Notifications.enabled()) { Notifications.disable(); disablePush(); renderNotifyBtn(); return; }
  const result = await Notifications.enable();
  if (result !== "granted") toast("Allow notifications in your browser to turn this on.", "warning");
  else if (Account.active()) {
    try {
      await enablePush();
      toast("Alerts are on for this device, even when Orchestrator is closed.");
    } catch (err) {
      toast(`Alerts work while Orchestrator is open, but not when it's closed: ${err.message}`, "warning");
    }
  }
  renderNotifyBtn();
});
renderNotifyBtn();
const lockBtn = $("#lock-btn");
if (lockBtn) {
  lockBtn.addEventListener("click", lockSession);
}
// A tapped alert opens the computer it came from (?machine=…), at the page it is about (the hash).
(function followAlertLink() {
  const params = new URLSearchParams(location.search);
  const machine = params.get("machine");
  if (!machine) return;
  params.delete("machine");
  history.replaceState(null, "", location.pathname + (params.toString() ? `?${params}` : "") + location.hash);
  if (!Account.active()) return;
  try {
    if (localStorage.getItem(Account.MACHINE_KEY) === machine) return;
    localStorage.setItem(Account.MACHINE_KEY, machine); // a different computer: open that one instead
    localStorage.removeItem("orchestrator_backend");
    localStorage.removeItem("orchestrator_token");
  } catch { /* storage blocked: the computer list is shown instead */ }
})();
if (signingIn) showSigningIn(); // coming back from a provider's redirect: no sign-in options flash while the session is set up
setTimeout(() => { if (signingIn && !getToken()) endSigningIn(true); }, 20000); // a redirect that never reports back must not leave a spinner forever
refreshState().then(() => { if (!signingIn) route(); });
setInterval(tick, 5000);

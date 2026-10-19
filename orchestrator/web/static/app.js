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

// The hosted app only talks to a computer its own account vouched for: the address signInToMachine got from the control
// plane, marked here. Nothing else on that origin (a link's ?backend=, a typed address, an old value) can choose a backend,
// since the app sends sign-in tokens, including a Firebase ID token, to the backend it is using.
const ACCOUNT_BACKEND_KEY = "orchestrator_backend_account";

function getBackendUrl() {
  if (Account.isHosted()) {
    const stored = localStorage.getItem("orchestrator_backend");
    if (stored && stored === localStorage.getItem(ACCOUNT_BACKEND_KEY)) return stored;
    if (stored) localStorage.removeItem("orchestrator_backend");
    return "";
  }
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
  const qt = Account.isHosted() ? "" : params.get("token"); // on the hosted app sign-in comes from the account, never a link
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

const LAST_LOGIN_KEY = "orchestrator_last_login_method";

function getLastLoginMethod() {
  try {
    const val = localStorage.getItem(LAST_LOGIN_KEY);
    if (val) return val;
  } catch {}
  try {
    if (typeof document !== "undefined" && document.cookie) {
      const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${LAST_LOGIN_KEY}=([^;]+)`));
      if (match) return decodeURIComponent(match[1]);
    }
  } catch {}
  if (typeof window !== "undefined") {
    const currentUser = window.firebase?.auth?.().currentUser;
    if (currentUser?.providerData?.length) {
      const pid = currentUser.providerData[0]?.providerId;
      if (pid === "google.com") return "google";
      if (pid === "apple.com") return "apple";
      if (pid === "github.com") return "github";
      if (pid === "password") return "email";
    }
  }
  return "";
}

function setLastLoginMethod(method) {
  if (!method) return;
  try {
    localStorage.setItem(LAST_LOGIN_KEY, method);
  } catch {}
  try {
    if (typeof document !== "undefined") {
      document.cookie = `${LAST_LOGIN_KEY}=${encodeURIComponent(method)};path=/;max-age=31536000;SameSite=Lax`;
    }
  } catch {}
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

async function api(path, { method = "GET", body, signal } = {}) {
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
      signal,
    });
  } catch (e) {
    // The browser doesn't say why (DNS, reset, timeout), but how long it took and whether it thought it was online helps.
    if (logged && e.name !== "AbortError") ConnectionLog.add("fetch_failed", { ...where, ms: Math.round(performance.now() - started), error: `${e.name}: ${e.message}` });
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
  const level = kind === true ? "error" : typeof kind === "string" ? kind : /\bcancell?ed\b/i.test(message) ? "cancel" : "success";
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
  if (meta.confirm && !skipConfirm && !(await formDialog(meta.title, `<p>${esc(meta.confirm)}</p>`, "Continue", { compact: true }))) return;
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
  if (dlg) dlg.tabIndex = -1;
  if (compact) dlg.classList.add("dialog-compact");
  else dlg.classList.remove("dialog-compact");
  $("#dialog-cancel").hidden = false;
  $("#dialog-cancel").textContent = "Cancel";
  $("#dialog-title").textContent = title;
  $("#dialog-body").innerHTML = bodyHtml;
  $("#dialog-ok").textContent = okLabel;
  $("#dialog-ok").hidden = false;
  $("#dialog-ok").className = danger ? "btn danger" : "btn primary";
  $("#dialog-ok").disabled = false;
  dlg.returnValue = "";
  if (!dlg.open) dlg.showModal();
  const first = $("#dialog-body textarea, #dialog-body input:not([type=hidden]):not([type=checkbox]):not([type=radio]), #dialog-body select");
  if (first) {
    first.focus();
  } else {
    if (document.activeElement === $("#dialog-close")) {
      $("#dialog-close")?.blur?.();
    }
    dlg?.focus?.({ preventScroll: true });
  }
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => {
      dlg.classList.remove("dialog-compact");
      const wasOk = dlg.returnValue === "ok" && !$("#dialog-ok")?.disabled;
      $("#dialog-ok").className = "btn primary";
      $("#dialog-ok").disabled = false;
      if (!wasOk) return resolve(null);
      resolve(Object.fromEntries(new FormData($("#dialog-form")).entries()));
    }, { once: true });
  });
}

const globalDialog = $("#dialog");
if (globalDialog) globalDialog.tabIndex = -1;
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
$("#dialog-body")?.addEventListener("click", (event) => {
  if (event.target.closest("a[href^='#']")) closeGlobalDialog(event);
});

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
  if (dlg) dlg.tabIndex = -1;
  dlg.returnValue = "";
  dlg.showModal();
  if (document.activeElement === $("#dialog-close")) {
    $("#dialog-close")?.blur?.();
  }
  dlg?.focus?.({ preventScroll: true });
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
      <p class="muted">Project: <strong>${esc(state.project?.name || state.project?.root || "Current project")}</strong></p>
      <label class="field"><span>Branch name</span><input type="text" name="name" required autocapitalize="off" spellcheck="false" placeholder="e.g. feature/rematch-flow"></label>
      <small class="hint-text">Created from ${esc(state.project?.branch || "the current branch")} and checked out.</small>`, "Create");
    if (values?.name.trim()) runAction("git_new_branch", { name: values.name.trim() });
  },
  async distribute(params) {
    if (params?.branch) {
      runAction("distribute", { branch: params.branch, notes: params.notes, group: params.group, testers: params.testers }, { skipConfirm: false });
      return;
    }
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

const stoppingRuns = new Set();

async function stopJob(runId, stopBtn) {
  if (!runId || stoppingRuns.has(runId)) return;
  let run = state.runs.find((r) => r.id === runId);
  if (!run) {
    try { await refreshState(); } catch {}
    run = state.runs.find((r) => r.id === runId);
  }
  const hasJob = Boolean(run?.job || run?.result_job || (current.page === "job" && current.args[0]));
  const title = hasJob ? "Stop this job?" : "Stop this run?";
  const body = "<p>Work stops now. What was done is kept, and you can resume later.</p>";
  const okLabel = hasJob ? "Stop job" : "Stop run";

  const ok = await formDialog(title, body, okLabel, { danger: true, compact: true });
  if (!ok) return;

  stoppingRuns.add(runId);
  if (run) run.stopping = true;

  const liveRunEl = stopBtn?.closest(".live-run") || Array.from(document.querySelectorAll(".live-run")).find((el) => el.dataset.runId === runId);
  if (liveRunEl) {
    liveRunEl.classList.add("stopping");
    liveRunEl.setAttribute("aria-busy", "true");
    if (!liveRunEl.querySelector(".live-run-overlay")) {
      const overlay = document.createElement("div");
      overlay.className = "live-run-overlay";
      overlay.setAttribute("role", "status");
      overlay.setAttribute("aria-label", "Stopping…");
      overlay.innerHTML = '<div class="spinner" aria-hidden="true"></div><span class="live-run-overlay-text">Stopping…</span>';
      liveRunEl.prepend(overlay);
    }
  }

  document.querySelectorAll("[data-stop]").forEach((btn) => {
    if (btn.dataset.stop === runId) {
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.textContent = "Stopping…";
    }
  });

  try {
    await api(`runs/${encodeURIComponent(runId)}/stop`, { method: "POST", body: {} });
    const maxPolls = 60;
    for (let i = 0; i < maxPolls; i++) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      await refreshState();
      const currentRun = state.runs.find((r) => r.id === runId);
      if (!currentRun || !currentRun.running) {
        if (currentRun) currentRun.stopped = true;
        break;
      }
    }
    toast(hasJob ? "Job stopped" : "Run stopped");
  } catch (err) {
    toast(err.message, true);
  } finally {
    stoppingRuns.delete(runId);
    if (run) run.stopping = false;
    await refreshState();
    if (LIVE.has(current.page)) {
      await route();
    }
  }
}

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
    e.preventDefault();
    e.stopPropagation();
    stopJob(stop.dataset.stop, stop);
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
    // Respond instantly: move the highlight now; the page rebuilds once the switch lands.
    const card = switchBtn.closest(".project-card");
    if (card) {
      document.querySelectorAll(".project-card.active-project").forEach((c) => c.classList.remove("active-project"));
      card.classList.add("active-project");
      card.setAttribute("aria-busy", "true");
    }
    const then = switchBtn.dataset.then || "#/";
    api("project", { method: "POST", body: { root } })
      .then(async (res) => {
        if (res?.project) state.project = res.project;
        setupFetched = 0;
        await refreshState();
        toast(`Switched to ${state.project?.name || "project"}`);
        if (location.hash !== then) {
          location.hash = then;
        } else {
          await route();
        }
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
    if (!view.querySelector(".signin-card:not(.signing-in)")) {
      showSignInGate("Sign in with your access token to continue.");
    }
    return;
  }
  if (Account.active() && (!backend || !window.firebase?.auth?.().currentUser)) { // the hosted app, before a computer is chosen: just the sign-in screen
    if (window.firebase?.auth?.().currentUser) {
      openAccount().catch(() => { if (!view.querySelector(".signin-card:not(.signing-in)")) showSignInGate(); });
      return;
    }
    if (!view.querySelector(".signin-card:not(.signing-in)")) {
      showSignInGate();
    }
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
    outageStartedAt = 0;
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
    outageStartedAt ||= Date.now();
    // Opening the hosted app with an old saved address: the account screen below finds the computer, so no warning.
    const reopening = !state.project && Account.active();
    if (pollFailures >= 2 && !connectionLost && !reopening) { // two misses in a row: say so, rather than showing stale data silently
      connectionLost = true;
      connectionLostAt = Date.now();
      ConnectionLog.add("connection_lost", { failures: pollFailures, error: e.message, ua: navigator.userAgent });
      notify("warning", Account.active() ? "Lost the connection to your computer. Reconnecting…" : "Can't reach Orchestrator. Check that it's running and that you're online.", { id: "connection", sticky: true });
    }
    if (Account.active()) {
      if (window.firebase?.auth?.().currentUser) {
        relocateMachine().catch(() => openAccount().catch(() => {}));
        return;
      }
      showSignInGate();
      return;
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
  if (mobileTitle) mobileTitle.textContent = document.body.classList.contains("signin-gate") ? "Orchestrator" : (state.project?.name || "");
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

  const groups = [
    ["GitHub", options.filter(o => o.source_type === "github")],
    ["Other Git hosts", options.filter(o => o.source_type === "git")],
    ["On this computer", options.filter(o => !o.source_type || o.source_type === "local")],
  ].filter(([, projects]) => projects.length);
  let html = groups.map(([label, projects]) => `<optgroup label="${esc(label)}">${projects.map(o =>
    `<option value="${esc(o.root)}" ${o.root === p.root ? "selected" : ""}>${esc(o.name)}${o.source_type === "git" && o.source_label ? ` · ${esc(o.source_label)}` : ""}</option>`
  ).join("")}</optgroup>`).join("");
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
    navigationDrawer?.close();
    return;
  }
  try {
    const res = await api("project", { method: "POST", body: { root: val } });
    if (res?.project) state.project = res.project;
    setupFetched = 0;
    await refreshState();
    toast(`Switched to ${state.project?.name || "project"}`);
    if (location.hash.startsWith("#/product")) {
      location.hash = "#/product";
    } else {
      location.hash = "#/";
    }
    await route();
    navigationDrawer?.close();
  } catch (err) {
    if (state.project) e.target.value = state.project.root;
    toast(err.message, true);
  }
});

// Shown from the click on a provider (or the token form) until the first page is ready.
function showSigningIn(label = "Signing you in…") {
  setHeader({ title: "", sub: "", actions: "" });
  document.title = Notifications.tabTitle("Signing in · Orchestrator", state.inbox_count || 0);
  document.body.classList.add("signin-gate");
  document.body.removeAttribute("data-page");
  const mobileTitle = $("#mobile-title");
  if (mobileTitle) mobileTitle.textContent = "Orchestrator";
  document.querySelector(".app")?.classList.add("session-locked");
  view.innerHTML = `<div class="signin-wrap"><div class="signin-card signing-in" role="status" aria-live="polite">
      <div class="spinner" aria-hidden="true"></div>
      <h2>${esc(label)}</h2>
      <p class="muted">Connecting to your computer and loading your projects.</p></div></div>`;
}

// ---------------------------------------------------------------- account (hosted app)

// The control plane, served by the hosted app at /cp: your computers and the one-time tickets that open them.
async function cpApi(path, { method = "GET", body, signal } = {}) {
  const user = window.firebase?.auth?.().currentUser;
  if (!user) throw Object.assign(new Error("Sign in first."), { status: 401 });
  const res = await fetch(`/cp/${path}`, {
    method,
    headers: { Authorization: `Bearer ${await user.getIdToken()}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `${res.status} ${res.statusText}`), { status: res.status });
  return data;
}

// After Google (or another provider): on the hosted app, find your computers; elsewhere, sign in to this server directly.
async function afterProviderSignIn(user, name) {
  const provider = (name || "").toLowerCase().includes("google") ? "google"
    : (name || "").toLowerCase().includes("apple") ? "apple"
    : (name || "").toLowerCase().includes("github") ? "github"
    : (name || "").toLowerCase().includes("email") ? "email"
    : (user?.providerData?.[0]?.providerId === "google.com") ? "google"
    : (user?.providerData?.[0]?.providerId === "apple.com") ? "apple"
    : (user?.providerData?.[0]?.providerId === "github.com") ? "github"
    : (user?.providerData?.[0]?.providerId === "password") ? "email"
    : "email";
  setLastLoginMethod(provider);
  if (Account.active()) return openAccount();
  const backend = getBackendUrl();
  if (!backend && window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost") {
    throw new Error(`Signed in as ${user.email || name}, but there's no computer address yet. Enter it under "Mac Backend URL" below.`);
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
  document.body.classList.remove("signin-gate");
  endSigningIn();
  await refreshState();
  route();
  let pushToken = null;
  try { pushToken = localStorage.getItem(PUSH_KEY); } catch { /* storage blocked */ }
  if (Account.active() && Notifications.enabled() && !pushToken) enablePush().catch(() => {}); // alerts were on before push existed
}

let accountPoll = null;
let snapshotPoll = null;
let accountScreenGeneration = 0;
function stopSnapshotPoll() { if (snapshotPoll) { clearInterval(snapshotPoll); snapshotPoll = null; } }
function stopAccountPoll() { clearInterval(accountPoll); accountPoll = null; stopSnapshotPoll(); }

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
  const generation = ++accountScreenGeneration;
  signingIn = true;
  setHeader({ title: "", sub: "", actions: "" });
  document.title = Notifications.tabTitle("Your computers · Orchestrator", state.inbox_count || 0);
  document.body.classList.add("signin-gate");
  document.body.removeAttribute("data-page");
  const mobileTitle = $("#mobile-title");
  if (mobileTitle) mobileTitle.textContent = "Orchestrator";
  document.querySelector(".app")?.classList.add("session-locked");
  const email = window.firebase?.auth?.().currentUser?.email || "";
  let macRelease = null;
  const render = (list, msg, tb) => {
    if (view.contains(document.activeElement) && document.activeElement.matches("input")) return; // not while typing a code
    const expanded = new Set(Array.from(view.querySelectorAll("details[data-account-expansion][open]"), (el) => el.dataset.accountExpansion));
    view.innerHTML = Account.renderMachines(list, { email, message: msg, troubleshoot: tb, release: macRelease });
    view.querySelectorAll("details[data-account-expansion]").forEach((el) => { el.open = expanded.has(el.dataset.accountExpansion); });
  };
  render(machines, message, troubleshoot);
  fetch("/releases/macos.json", {cache: "no-store"})
    .then((response) => response.ok ? response.json() : null)
    .then((release) => {
      macRelease = Account.macRelease(release);
      if (!macRelease || generation !== accountScreenGeneration) return;
      // Update download details alone so a slow fetch never erases a typed pairing code.
      const downloads = view.querySelector(".mac-setup [data-mac-downloads]");
      if (!downloads) return;
      const template = document.createElement("template");
      template.innerHTML = Account.renderMacSetup(macRelease);
      downloads.replaceChildren(...template.content.querySelector("[data-mac-downloads]").childNodes);
      const install = view.querySelector(".mac-setup [data-cli-install]");
      if (install) install.textContent = template.content.querySelector("[data-cli-install]").textContent;
    }).catch(() => {}); // An unpublished installer must not block account pairing.
  // Watch for a computer coming online (someone just ran `orchestrator ui` on it).
  let last = JSON.stringify(machines);
  accountPoll = setInterval(async () => {
    if (!document.querySelector(".account-card")) return stopAccountPoll();
    try {
      const { machines: fresh } = await cpApi("machines");
      const now = JSON.stringify(fresh);
      if (now !== last) {
        last = now;
        machines = fresh;
        render(fresh, message, troubleshoot);
        let remembered = null;
        try { remembered = localStorage.getItem(Account.MACHINE_KEY); } catch { /* storage blocked */ }
        const pick = location.hash !== "#/computers" && !message && !troubleshoot && Account.pickMachine(fresh, remembered);
        if (pick) return enterMachine(pick);
      }
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
    } else if (action === "view-snapshot") {
      const machineId = button.dataset.id;
      const machine = machines.find((m) => m.id === machineId);
      showSnapshotScreen(machineId, machine);
    } else if (action === "remove") {
      if (!await formDialog(`Remove ${button.dataset.name}?`, `<p>It leaves your account. To reconnect, choose Connect this Mac in Orchestrator on that computer. Command-line users can run <code>orchestrator connect</code>.</p>`, "Remove", { danger: true })) return;
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
      signOut();
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
  setHeader({ title: "", sub: "", actions: "" });
  document.title = Notifications.tabTitle("Add a computer · Orchestrator", state.inbox_count || 0);
  document.body.classList.add("signin-gate");
  document.body.removeAttribute("data-page");
  const mobileTitle = $("#mobile-title");
  if (mobileTitle) mobileTitle.textContent = "Orchestrator";
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
      done(`${machine.name} is connected to your account. Keep Orchestrator open on that Mac and turn on Remote access in its menu. It will appear online here shortly. For a command-line installation, start \`orchestrator ui --tunnel\`.`);
    } catch (err) { done(err.message); }
  };
}

// Open one computer: the control plane signs a ticket only it accepts, and it trades that for a sign-in of its own.
const machineRepairAttempts = new Map();
function isWebConnectionFailure(error) {
  return [502, 503, 504, 530].includes(error.status) ||
    (!error.status && ["TypeError", "AbortError", "TimeoutError"].includes(error.name));
}

async function signInToMachine(machine, deadline = Date.now() + 16000) {
  const signal = () => AbortSignal.timeout(Math.max(1, Math.min(8000, deadline - Date.now())));
  const { endpoint, ticket } = await cpApi("machine/ticket", {
    method: "POST", body: { machine_id: machine.id }, signal: signal(),
  });
  localStorage.setItem("orchestrator_backend", endpoint);
  localStorage.setItem(ACCOUNT_BACKEND_KEY, endpoint);
  localStorage.setItem(Account.MACHINE_KEY, machine.id);
  localStorage.removeItem("orchestrator_token");
  try {
    return await api("auth", { method: "POST", body: { ticket }, signal: signal() });
  } catch (error) {
    error.endpoint = endpoint;
    error.webConnectionFailed = isWebConnectionFailure(error);
    throw error;
  }
}

async function claimMachineRepair(machine) {
  const key = `orchestrator_tunnel_repair_${machine.id}`;
  const claim = () => {
    if (navigator.onLine === false) return false;
    let lastAttempt = machineRepairAttempts.get(key) || 0;
    try { lastAttempt = Math.max(lastAttempt, Number(localStorage.getItem(key)) || 0); } catch { /* storage blocked */ }
    if (lastAttempt && Date.now() - lastAttempt < 300000) return false;
    machineRepairAttempts.set(key, Date.now());
    try { localStorage.setItem(key, String(Date.now())); } catch { /* keep the in-memory limit */ }
    return true;
  };
  // Serialize the claim across tabs sharing this account and tunnel.
  return navigator.locks?.request ? navigator.locks.request(key, claim) : claim();
}

async function repairMachineConnection(machine, failure) {
  if (!failure.webConnectionFailed || navigator.onLine === false) throw failure;
  // A previous restart may already have replaced the saved address. Try that before asking for another.
  const { machines } = await cpApi("machines", { signal: AbortSignal.timeout(8000) });
  let current = (machines || []).find((item) => item.id === machine.id);
  if (!current?.online || !current.endpoint) {
    for (let i = 0; i < 3; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      if (navigator.onLine === false) break;
      const { machines: retry } = await cpApi("machines", { signal: AbortSignal.timeout(4000) }).catch(() => ({ machines: [] }));
      const found = (retry || []).find((item) => item.id === machine.id);
      if (found?.online && found.endpoint) { current = found; break; }
    }
  }
  if (!current?.online || !current.endpoint) throw failure;
  if (current.endpoint !== failure.endpoint) {
    try { return await signInToMachine(current); }
    catch (error) {
      if (!error.webConnectionFailed) throw error;
      failure = error;
    }
  }
  if (!await claimMachineRepair(machine) || navigator.onLine === false) throw failure;
  showSigningIn(`Reconnecting to ${machine.name}… This may take a minute.`);
  const deadline = Date.now() + 90000;
  await cpApi("machines/reset-tunnel", {
    method: "POST", body: { machine_id: machine.id }, signal: AbortSignal.timeout(8000),
  });
  let lastTried = `${current.endpoint}:${current.last_seen}`;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, Math.min(3000, deadline - Date.now())));
    if (navigator.onLine === false || Date.now() >= deadline) break;
    const { machines: updated } = await cpApi("machines", {
      signal: AbortSignal.timeout(Math.max(1, Math.min(8000, deadline - Date.now()))),
    });
    const next = (updated || []).find((item) => item.id === machine.id);
    if (!next?.online) break;
    const revision = `${next.endpoint}:${next.last_seen}`;
    if (!next.endpoint || revision === lastTried) continue;
    lastTried = revision;
    try { return await signInToMachine(next, deadline); }
    catch (error) {
      if (!error.webConnectionFailed) throw error;
      failure = error;
    }
  }
  throw failure;
}

async function showSnapshotScreen(machineId, machine) {
  stopAccountPoll();
  signingIn = true;
  setHeader({ title: "", sub: "", actions: "" });
  document.title = Notifications.tabTitle("Offline snapshot · Orchestrator", state.inbox_count || 0);
  document.body.classList.add("signin-gate");
  document.body.removeAttribute("data-page");
  document.querySelector(".app")?.classList.add("session-locked");
  view.innerHTML = `<div class="signin-wrap"><div class="signin-card account-card"><p class="muted">Loading snapshot…</p></div></div>`;
  try {
    const res = await cpApi("machine/snapshot", { method: "POST", body: { machine_id: machineId } });
    const snapshot = res.snapshot;
    if (!snapshot) {
      toast("No snapshot has been synced for this computer yet.", "info");
      return openAccount({ list: true });
    }
    view.innerHTML = Account.renderSnapshot(snapshot);
    view.onsubmit = null;
    stopSnapshotPoll();
    snapshotPoll = setInterval(async () => {
      if (!document.querySelector(".account-card") || !signingIn) return stopSnapshotPoll();
      try {
        const { machines: fresh } = await cpApi("machines");
        const current = (fresh || []).find((m) => m.id === machineId);
        if (current && current.reachable) {
          stopSnapshotPoll();
          toast(`${current.name} is back online! Connecting…`, "info");
          enterMachine(current);
        }
      } catch { /* control plane retry */ }
    }, 4000);
    view.onclick = async (event) => {
      const btn = event.target.closest("[data-account-action]");
      if (!btn || btn.disabled) return;
      const act = btn.dataset.accountAction;
      if (act === "back-machines") {
        stopSnapshotPoll();
        openAccount({ list: true });
      } else if (act === "open") {
        stopSnapshotPoll();
        const targetId = btn.dataset.id || machineId;
        const targetMachine = machine || { id: targetId, name: snapshot.machine_name || "Computer" };
        enterMachine(targetMachine);
      }
    };
  } catch (err) {
    stopSnapshotPoll();
    toast(err.message, true);
    openAccount({ list: true });
  }
}

async function enterMachine(machine) {
  stopAccountPoll();
  signingIn = true;
  showSigningIn(`Opening ${machine.name}…`);
  try {
    let res;
    try { res = await signInToMachine(machine); }
    catch (error) { res = await repairMachineConnection(machine, error); }
    // An idea from before this computer was set up: go and start it (or add the code they already have).
    const idea = Account.pendingIdea();
    if (idea) {
      location.hash = Account.ideaRoute(idea);
      if (idea.existing) Account.clearIdea();
    }
    await unlockWith(res.token, `Opened ${machine.name}`);
  } catch (err) {
    if (machine.has_snapshot) {
      toast(`${machine.name} is offline. Showing offline snapshot.`, "info");
      return showSnapshotScreen(machine.id, machine);
    }
    openAccount({ list: true, message: err.message,
      troubleshoot: { machine, endpoint: err.endpoint || machine.endpoint || "", error: err.message } });
  }
}

// The hosted app reaches a computer at the address its account last heard. That address can change (a quick tunnel gets a
// new one each time it restarts), so when calls keep failing, ask the account where the computer is now and move there.
let relocating = false;
let lastRelocateTry = 0;
let outageStartedAt = 0; // when the poll first failed in a row; 0 while the connection is fine
// For this long after a loss, a loaded page reconnects without a word: no sign-in screen, no leaving the page you're on.
// Past it, the full flow below takes over (it can also ask the computer to restart its tunnel).
const QUIET_RECONNECT_MS = 45000;
async function reconnectQuietly(machine) {
  const res = await signInToMachine(machine);
  localStorage.setItem("orchestrator_token", res.token);
  pollFailures = 0;
  await refreshState();
}
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
        if (machine?.has_snapshot) {
          showSnapshotScreen(machine.id, machine);
          return;
        }
        showAccountScreen(machines, `${machine.name} is currently offline. Start \`orchestrator ui --tunnel\` on it.`);
      }
      return;
    }
    if (endpoint !== getBackendUrl() || !state.project || !getToken() || pollFailures >= 2) {
      ConnectionLog.add("relocated", { backend: new URL(endpoint).host });
      if (state.project && outageStartedAt && Date.now() - outageStartedAt < QUIET_RECONNECT_MS) {
        try { await reconnectQuietly(machine); } catch { /* the next poll tries again */ }
        return;
      }
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
        if (!state.project) {
          showSignInGate();
        }
      }
    });
  } catch { /* auth unavailable: the sign-in screen still works */ }
}

// Complete email link sign-in if returning from an email link
function showEmailConfirmGate(onConfirm) {
  document.body.classList.add("signin-gate");
  view.innerHTML = `
    <div class="signin-wrap">
      <div class="signin-card email-confirm-card">
        <h2>Confirm your email</h2>
        <p class="muted">To complete signing in with your email link, please confirm your email address:</p>
        <form id="email-confirm-form" class="stack email-confirm-form">
          <label class="field">
            <span>Email address</span>
            <input type="email" id="email-confirm-input" required placeholder="you@example.com" autocomplete="email">
          </label>
          <button type="submit" class="btn primary">
            <span>Complete Sign In</span>
          </button>
        </form>
      </div>
    </div>
  `;
  const confirmForm = $("#email-confirm-form");
  const confirmInput = $("#email-confirm-input");
  confirmInput?.focus();
  confirmForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const val = (confirmInput?.value || "").trim();
    if (val) await onConfirm(val);
  });
}

if (typeof window !== "undefined" && window.firebase?.auth && Account.providerSignInWorks()) {
  try {
    const href = window.location.href;
    let isEmailLink = false;
    try { isEmailLink = firebase.auth().isSignInWithEmailLink(href); } catch {}
    if (!isEmailLink && window.location.hash.includes("apiKey=")) {
      try {
        const hashQuery = window.location.hash.split("?")[1];
        if (hashQuery) {
          isEmailLink = firebase.auth().isSignInWithEmailLink(`${window.location.origin}${window.location.pathname}?${hashQuery}`);
        }
      } catch {}
    }
    if (isEmailLink) {
      let email = "";
      try { email = localStorage.getItem("orchestrator_email_for_signin") || ""; } catch {}
      const completeSignIn = async (emailToUse) => {
        signingIn = true;
        showSigningIn(`Signing in as ${emailToUse}…`);
        try {
          const cred = await firebase.auth().signInWithEmailLink(emailToUse, href);
          try { localStorage.removeItem("orchestrator_email_for_signin"); } catch {}
          if (window.history?.replaceState) {
            const clean = window.location.pathname + (window.location.hash.startsWith("#/?apiKey") ? "#/" : window.location.hash || "");
            window.history.replaceState({}, document.title, clean);
          }
          setLastLoginMethod("email");
          showSigningIn(`Signed in as ${cred.user.email || emailToUse}. Connecting…`);
          await afterProviderSignIn(cred.user, "Email");
        } catch (err) {
          endSigningIn();
          toast(err.message, true);
          showSignInGate(err.message);
        }
      };

      if (email) {
        completeSignIn(email);
      } else {
        showEmailConfirmGate(completeSignIn);
      }
    }
  } catch {}
}

function showSignInGate(message) {
  const existingCard = view.querySelector(".signin-card:not(.signing-in)");
  if (existingCard) {
    const msgEl = $("#signin-gate-message");
    if (msgEl) {
      msgEl.innerHTML = message ? `<p class="muted" style="margin: 0.35rem 0 0; font-size: 0.92rem;"><span style="color: var(--bad);">${esc(message)}</span></p>` : "";
    }
    return;
  }
  setHeader({ title: "", sub: "", actions: "" });
  document.title = Notifications.tabTitle("Sign In · Orchestrator", state.inbox_count || 0);
  document.body.classList.add("signin-gate");
  document.body.removeAttribute("data-page");
  const mobileTitle = $("#mobile-title");
  if (mobileTitle) mobileTitle.textContent = "Orchestrator";
  document.querySelector(".app")?.classList.add("session-locked");
  const backend = getBackendUrl() || "";
  const token = getToken() || "";
  const isRemote = window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost";
  const hosted = Account.active();
  const hostedUser = hosted ? window.firebase?.auth?.().currentUser : null;
  const providers = Account.providerSignInWorks();
  const lastUsed = getLastLoginMethod();
  const isLastProvider = (p) => lastUsed === p;
  view.innerHTML = `
    <div class="signin-wrap">
      <div class="signin-card">
        <div>
          <h2 style="margin: 0; font-size: 1.3rem; font-weight: 700;">Sign In</h2>
          <div id="signin-gate-message">${message ? `<p class="muted" style="margin: 0.35rem 0 0; font-size: 0.92rem;"><span style="color: var(--bad);">${esc(message)}</span></p>` : ""}</div>
        </div>
        <figure class="signin-preview">
          <h3>The app in your head. Made real.</h3>
          <p class="signin-privacy">Private: your code and assets stay on your computer.</p>
          <picture>
            <source media="(prefers-reduced-motion: reduce)" srcset="signin-preview.png">
            <img src="signin-preview.gif" width="720" height="540" alt="Orchestrator screens with a sample Family Recipes project: describe an idea, shape its product brief, approve a plan, and follow build progress through completed checks.">
          </picture>
        </figure>
        ${providers ? "" : `<div class="notice signin-elsewhere">
          <p>Google, Apple, GitHub and email sign-in work on the Orchestrator site, which then opens this computer for you.</p>
          <a class="btn primary" href="${esc(Account.HOSTED_ORIGINS[0])}" style="width: 100%; justify-content: center;">Sign in on the Orchestrator site</a>
          <p class="muted">Or use the access token below${location.hostname === "127.0.0.1" ? `, or open <a href="${esc(location.href.replace("//127.0.0.1", "//localhost"))}">localhost</a> instead of 127.0.0.1` : ""}.</p></div>`}
        <div class="sso-buttons" ${providers ? "" : "hidden"}>
          <button type="button" class="sso-btn google-btn ${isLastProvider("google") ? "last-used" : ""}" id="google-signin-btn">
            <svg class="icon"><use href="#i-google"/></svg>
            <span>Continue with Google</span>
            ${isLastProvider("google") ? `<span class="last-used-tag">Last used</span>` : ""}
          </button>
          <button type="button" class="sso-btn apple-btn ${isLastProvider("apple") ? "last-used" : ""}" id="apple-signin-btn">
            <svg class="icon"><use href="#i-apple"/></svg>
            <span>Continue with Apple</span>
            ${isLastProvider("apple") ? `<span class="last-used-tag">Last used</span>` : ""}
          </button>
          <button type="button" class="sso-btn github-btn ${isLastProvider("github") ? "last-used" : ""}" id="github-signin-btn">
            <svg class="icon"><use href="#i-github"/></svg>
            <span>Continue with GitHub</span>
            ${isLastProvider("github") ? `<span class="last-used-tag">Last used</span>` : ""}
          </button>
          <button type="button" class="sso-btn email-btn ${isLastProvider("email") ? "last-used" : ""}" id="email-signin-btn">
            <svg class="icon"><use href="#i-mail"/></svg>
            <span>Continue with email</span>
            ${isLastProvider("email") ? `<span class="last-used-tag">Last used</span>` : ""}
          </button>
        </div>
        <div id="email-signin-panel" class="email-signin-panel" ${isLastProvider("email") ? "" : "hidden"}>
          <form id="email-auth-form" class="stack email-auth-form">
            <div class="row gap-8 email-panel-header">
              <strong id="email-panel-title">Sign in with email</strong>
              <button type="button" class="linklike muted email-panel-close" id="email-panel-close" title="Close">✕</button>
            </div>
            <div id="email-auth-notice" class="email-auth-notice" hidden></div>
            <div id="email-auth-suggestion" class="email-auth-suggestion" hidden></div>
            <label class="field">
              <span>Email address</span>
              <input type="email" id="email-auth-email" name="email" required placeholder="you@example.com" autocomplete="email">
            </label>
            <label class="field" id="email-auth-pw-wrap">
              <span>Password</span>
              <input type="password" id="email-auth-password" name="password" required placeholder="Your password" autocomplete="current-password">
            </label>
            <div class="row gap-8 email-auth-actions">
              <button type="button" class="linklike email-auth-mode-btn" id="email-auth-mode-btn">Need an account? Sign up</button>
              <button type="button" class="linklike muted email-auth-forgot-btn" id="email-auth-forgot-btn">Forgot password?</button>
            </div>
            <button type="submit" id="email-auth-submit-btn" class="btn primary email-auth-submit-btn">
              <span id="email-auth-submit-label">Sign In</span>
            </button>
            <div class="email-auth-link-wrap">
              <button type="button" class="linklike muted email-auth-link-btn" id="email-auth-link-btn">Or email me a sign-in link</button>
            </div>
          </form>
        </div>
        ${hostedUser ? `<button type="button" class="btn primary sso-continue-btn ${lastUsed && lastUsed !== "token" ? "last-used" : ""}" id="account-continue-btn" style="width: 100%; justify-content: center; position: relative;">
          <span>Continue as ${esc(hostedUser.email || "you")}</span>
          <span class="last-used-tag">Last used</span>
        </button>` : ""}
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
        ${blockedProvider ? `<p class="muted signin-fallback">Pop-ups still blocked? <button type="button" class="linklike" id="redirect-fallback">Try full-page ${esc(blockedProvider.name)} sign-in</button>.${hosted ? "" : " Some browsers can't finish this way; the access token below always works."}</p>` : ""}
        ${hosted ? "" : `
        <details class="manual-token-details ${lastUsed === "token" ? "last-used-section" : ""}" open style="font-size: 0.82rem; margin-top: 0.5rem; border-top: 1px solid var(--border); padding-top: 0.75rem;">
          <summary class="muted" style="cursor: pointer; user-select: none; text-align: center; font-weight: 500; display: flex; align-items: center; justify-content: center; gap: 8px;">
            <span>Sign in with CLI access token</span>
            ${lastUsed === "token" ? `<span class="last-used-tag token-tag">Last used</span>` : ""}
          </summary>
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
            <button type="submit" id="signin-submit-btn" class="btn ${lastUsed === "token" ? "primary last-used" : ""}" style="width: 100%; justify-content: center; padding: 0.55rem; position: relative;">
              <span>Use Access Token</span>
              ${lastUsed === "token" ? `<span class="last-used-tag">Last used</span>` : ""}
            </button>
          </form>
        </details>
        `}
      </div>
    </div>
  `;

  $("#account-continue-btn")?.addEventListener("click", () => {
    const u = window.firebase?.auth?.().currentUser;
    const pid = u?.providerData?.[0]?.providerId;
    if (pid === "google.com") setLastLoginMethod("google");
    else if (pid === "apple.com") setLastLoginMethod("apple");
    else if (pid === "github.com") setLastLoginMethod("github");
    else if (pid === "password") setLastLoginMethod("email");
    openAccount();
  });

  const syncBackend = () => {
    const el = $("#signin-backend");
    if (!el) return;
    const b = el.value.trim().replace(/\/+$/, "");
    if (Account.isHosted()) return;
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
      setLastLoginMethod(name.toLowerCase());
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

  // Email sign-in toggle and form handling
  const emailBtn = $("#email-signin-btn");
  const emailPanel = $("#email-signin-panel");
  const emailForm = $("#email-auth-form");
  const emailInput = $("#email-auth-email");
  const emailPw = $("#email-auth-password");
  const emailPwWrap = $("#email-auth-pw-wrap");
  const emailNotice = $("#email-auth-notice");
  const emailSuggestion = $("#email-auth-suggestion");
  const emailModeBtn = $("#email-auth-mode-btn");
  const emailForgotBtn = $("#email-auth-forgot-btn");
  const emailSubmitBtn = $("#email-auth-submit-btn");
  const emailSubmitLabel = $("#email-auth-submit-label");
  const emailPanelTitle = $("#email-panel-title");
  const emailLinkWrap = $("#email-auth-link-wrap");
  const emailLinkBtn = $("#email-auth-link-btn");
  const emailCloseBtn = $("#email-panel-close");

  let emailMode = "signin";

  function setMode(mode, prefillEmail = "") {
    emailMode = mode;
    if (emailSuggestion) emailSuggestion.hidden = true;
    if (prefillEmail && emailInput) {
      emailInput.value = prefillEmail;
    }
    if (emailMode === "signup") {
      if (emailPanelTitle) emailPanelTitle.textContent = "Create an account";
      if (emailModeBtn) emailModeBtn.textContent = "Already have an account? Sign in";
      if (emailSubmitLabel) emailSubmitLabel.textContent = "Create Account";
      if (emailPwWrap) emailPwWrap.hidden = true;
      if (emailPw) {
        emailPw.required = false;
        emailPw.value = "";
      }
      if (emailForgotBtn) emailForgotBtn.hidden = true;
      if (emailLinkWrap) emailLinkWrap.hidden = true;
      if (emailNotice) {
        emailNotice.innerHTML = "Enter your email to get started. We'll send a sign-in link to activate your account — no password needed.";
        emailNotice.hidden = false;
      }
      emailInput?.focus();
    } else {
      if (emailPanelTitle) emailPanelTitle.textContent = "Sign in with email";
      if (emailModeBtn) emailModeBtn.textContent = "Need an account? Sign up";
      if (emailSubmitLabel) emailSubmitLabel.textContent = "Sign In";
      if (emailPwWrap) emailPwWrap.hidden = false;
      if (emailPw) {
        emailPw.required = true;
        emailPw.autocomplete = "current-password";
      }
      if (emailForgotBtn) emailForgotBtn.hidden = false;
      if (emailLinkWrap) emailLinkWrap.hidden = false;
      if (emailNotice) emailNotice.hidden = true;
    }
  }

  function showUnregisteredSuggestion(email) {
    if (emailSubmitBtn) {
      emailSubmitBtn.disabled = false;
      if (emailSubmitLabel) emailSubmitLabel.textContent = "Sign In";
    }
    if (emailSuggestion) {
      emailSuggestion.innerHTML = `
        <p>No account found for <strong>${esc(email)}</strong>.</p>
        <button type="button" class="btn small primary" id="suggest-create-account-btn">Create account with this email</button>
      `;
      emailSuggestion.hidden = false;
      const btn = $("#suggest-create-account-btn");
      if (btn) {
        btn.addEventListener("click", () => {
          setMode("signup", email);
        });
      }
    } else {
      toast(`No account found for ${email}. Click 'Need an account? Sign up' to register.`);
      setMode("signup", email);
    }
  }

  async function sendEmailSignInLink(email, isSignup = false) {
    if (!email) {
      toast("Enter your email address first", true);
      emailInput?.focus();
      return false;
    }
    if (!window.firebase?.auth) {
      toast("Firebase Auth is loading or unavailable", true);
      return false;
    }
    try {
      const cleanUrl = new URL(window.location.href);
      cleanUrl.hash = "";
      const actionCodeSettings = {
        url: cleanUrl.toString(),
        handleCodeInApp: true,
      };
      await firebase.auth().sendSignInLinkToEmail(email, actionCodeSettings);
      try { localStorage.setItem("orchestrator_email_for_signin", email); } catch {}
      const msg = isSignup
        ? `Sign-in link sent to ${email}! Check your inbox to activate your account.`
        : `Sign-in link sent to ${email}! Check your inbox to sign in without a password.`;
      toast(msg);
      if (emailNotice) {
        emailNotice.innerHTML = `<strong>Sign-in link sent to ${esc(email)}!</strong><br>Check your inbox and click the link to ${isSignup ? "activate your account and sign in" : "sign in without a password"}.`;
        emailNotice.hidden = false;
      }
      if (emailSuggestion) emailSuggestion.hidden = true;
      return true;
    } catch (err) {
      toast(err.message, true);
      return false;
    }
  }

  if (emailBtn && emailPanel) {
    emailBtn.addEventListener("click", () => {
      const isHidden = emailPanel.hidden;
      emailPanel.hidden = !isHidden;
      if (isHidden && emailInput) {
        emailInput.focus();
      }
    });
  }

  if (emailCloseBtn && emailPanel) {
    emailCloseBtn.addEventListener("click", () => {
      emailPanel.hidden = true;
    });
  }

  if (emailModeBtn) {
    emailModeBtn.addEventListener("click", () => {
      const currentEmail = (emailInput?.value || "").trim();
      setMode(emailMode === "signin" ? "signup" : "signin", currentEmail);
    });
  }

  if (emailForgotBtn && emailInput) {
    emailForgotBtn.addEventListener("click", async () => {
      const email = (emailInput.value || "").trim();
      if (!email) {
        toast("Enter your email address first", true);
        emailInput.focus();
        return;
      }
      if (!window.firebase?.auth) {
        toast("Firebase Auth is loading or unavailable", true);
        return;
      }
      const result = await PasswordReset.requestReset(firebase.auth(), email); // the same answer whether or not the address has an account
      toast(result.message, !result.ok);
    });
  }

  if (emailLinkBtn && emailInput) {
    emailLinkBtn.addEventListener("click", async () => {
      const email = (emailInput.value || "").trim();
      if (!email) {
        toast("Enter your email address first", true);
        emailInput.focus();
        return;
      }
      emailLinkBtn.disabled = true;
      emailLinkBtn.textContent = "Sending link…";
      try {
        await sendEmailSignInLink(email, false);
      } finally {
        emailLinkBtn.disabled = false;
        emailLinkBtn.textContent = "Or email me a sign-in link";
      }
    });
  }

  if (emailForm) {
    emailForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!window.firebase?.auth) {
        toast("Firebase Auth is loading or unavailable", true);
        return;
      }
      syncBackend();
      const email = (emailInput?.value || "").trim();
      if (!email) {
        toast("Enter your email address", true);
        emailInput?.focus();
        return;
      }

      if (emailMode === "signup") {
        const password = (emailPw?.value || "").trim();
        if (password) {
          try {
            const cred = await firebase.auth().createUserWithEmailAndPassword(email, password);
            setLastLoginMethod("email");
            showSigningIn(`Signed in as ${cred.user.email || email}. Connecting…`);
            await afterProviderSignIn(cred.user, "Email");
            return;
          } catch {}
        }
        if (emailSubmitBtn) {
          emailSubmitBtn.disabled = true;
          if (emailSubmitLabel) emailSubmitLabel.textContent = "Sending link…";
        }
        const sent = await sendEmailSignInLink(email, true);
        if (emailSubmitBtn) {
          emailSubmitBtn.disabled = false;
          if (emailSubmitLabel) emailSubmitLabel.textContent = sent ? "Link sent! Check email" : "Create Account";
        }
        return;
      }

      const password = (emailPw?.value || "").trim();
      if (!password) {
        toast("Enter your password, or use 'email me a sign-in link'", true);
        emailPw?.focus();
        return;
      }

      if (emailSubmitBtn) {
        emailSubmitBtn.disabled = true;
        if (emailSubmitLabel) emailSubmitLabel.textContent = "Signing in…";
      }
      signingIn = true;
      showSigningIn("Signing in…");

      try {
        const cred = await firebase.auth().signInWithEmailAndPassword(email, password);
        setLastLoginMethod("email");
        showSigningIn(`Signed in as ${cred.user.email || email}. Connecting…`);
        await afterProviderSignIn(cred.user, "Email");
      } catch (signInErr) {
        endSigningIn();
        if (emailSubmitBtn) {
          emailSubmitBtn.disabled = false;
          if (emailSubmitLabel) emailSubmitLabel.textContent = "Sign In";
        }
        const isNotFound = signInErr.code === "auth/user-not-found" ||
          signInErr.code === "auth/user-not-registered" ||
          /no (?:user|account) found/i.test(signInErr.message || "");
        if (isNotFound) {
          showUnregisteredSuggestion(email);
          return;
        }
        toast(signInErr.message, true);
        showSignInGate(signInErr.message);
      }
    });
  }

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
      setLastLoginMethod("token");
      signingIn = true;
      showSigningIn("Unlocking…");
      try {
        await api("auth", { method: "POST", body: { token: t } });
        setLastLoginMethod("token");
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

async function signOut() {
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
  toast("Signed out", "info");
  showSignInGate("You're signed out. Sign in again to continue.");
}

// `titleHtml` replaces the plain title with markup (Home's project switcher); `title` stays the page's name for the
// browser tab and anything else that asks what page this is (#page-title's data-title).
function setHeader({ title, titleHtml = "", sub = "", actions = "" }) {
  document.body.classList.remove("signin-gate");
  const mobileTitle = $("#mobile-title");
  if (mobileTitle) mobileTitle.textContent = state.project?.name || "";
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

// An epic's job is called an Epic even once it has been planned as a feature.
const jobKind = (j) => (j.epic ? "Epic" : j.kind || j.type || "Job");
const jiraChip = (j) => (j.jira?.key ? `<span class="jira-chip" title="Linked to Jira epic ${esc(j.jira.key)}"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-jira"/></svg>${esc(j.jira.key)}</span>` : "");

function jobItem(j, { withAction = false } = {}) {
  const bits = [jobKind(j), j.tasks_total ? `${j.tasks_done}/${j.tasks_total} tasks` : "", ago(j.updated)].filter(Boolean);
  const meta = withAction && j.state.reason ? j.state.reason : bits.join(" · ");
  const next = withAction && j.state.next && !j.active_run
    ? `<button class="btn small ${["failed", "attention"].includes(j.state.tone) ? "primary" : ""}" ${act(j.state.next.action, { job: j.id })}>${esc(j.state.next.label)}</button>` : "";
  return `<div class="item">
    <a class="main-col" href="#/jobs/${encodeURIComponent(j.id)}"><div class="title">${esc(j.title)}</div><div class="meta">${esc(meta)}</div></a>
    <div class="side">${jobPill(j)}${next}</div></div>`;
}



// Job list sorting (tap Status / Last modified; tap again to reverse).
const GROUP_ORDER = { needs_you: 0, working: 1, backlog: 2, done: 3 };
let jobSort = { key: "status", dir: "asc" };
try { jobSort = JSON.parse(localStorage.getItem("orchestrator_job_sort")) || jobSort; } catch {}

function sortJobs(jobs) {
  const { key, dir } = jobSort;
  const val = (j) => key === "status"
    ? [j.active_run ? 1 : (GROUP_ORDER[j.state?.group] ?? 4), j.state?.label || ""]
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
  const activeRun = state.runs.find((r) => r.running && (r.job === j.id || r.result_job === j.id));
  const parts = [
    j.branch ? `<span class="mono">${esc(j.branch)}</span>` : "",
    j.tasks_total ? `${j.tasks_done}/${j.tasks_total} tasks` : "",
  ].filter(Boolean);

  const isAutoImprove = Boolean(j.is_orchestrator_improvement || j.tag === "improve_orchestrator");
  if (j.submitted_for_inclusion) {
    parts.push('<span class="accent">✓ Submitted for inclusion</span>');
  }

  // The status pill already says what the job needs; its longer reason is on the job page.
  const metaHtml = parts.join(" · ");
  const typeLabel = esc(jobKind(j));
  const typeTag = isAutoImprove
    ? `<span class="job-type-tag job-type-auto-improve" title="Orchestrator Improvement">🏆 Orchestrator</span>`
    : `<span class="job-type-tag${j.epic ? " job-type-epic" : ""}">${typeLabel}</span>`;

  return `
    <a class="job-table-row"${hidden ? " hidden" : ""} href="#/jobs/${encodeURIComponent(j.id)}">
      <div class="col-job">
        <div class="job-title">${esc(j.title)} ${jiraChip(j)}</div>
        <div class="job-mobile-info">
          ${typeTag}
          ${jobPill(j)}
        </div>
        ${metaHtml ? `<div class="job-meta">${metaHtml}</div>` : ""}
      </div>
      <div class="col-type">
        ${typeTag}
      </div>
      <div class="col-status">
        ${jobPill(j)}
      </div>
      <div class="col-date" title="${esc(new Date(j.updated * 1000).toLocaleString())}">
        <span class="date-relative">${esc(ago(j.updated))}</span>
      </div>
      ${activeRun ? runStatus(activeRun) : ""}
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
  const target = r.action === "plan_epic" && r.result_job ? r.result_job : r.job || r.result_job; // planning removes the epic's waiting job
  const isStopping = stoppingRuns.has(r.id) || Boolean(r.stopping);
  const overlayHtml = isStopping
    ? `<div class="live-run-overlay" role="status" aria-label="Stopping…"><div class="spinner" aria-hidden="true"></div><span class="live-run-overlay-text">Stopping…</span></div>`
    : "";
  return `<div class="live-run ${r.waiting ? "waiting" : ""} ${isStopping ? "stopping" : ""}" data-run-id="${esc(r.id)}" ${isStopping ? 'aria-busy="true"' : ""}>
    ${overlayHtml}
    <a class="main-col" href="#/runs/${encodeURIComponent(r.id)}">
      <div class="title">${esc(r.title)}</div>
      <div class="meta">${esc(`Running ${elapsed(r.started)}`)}${r.waiting ? " · waiting for you" : r.idle > 30 ? ` · quiet for ${elapsed(Date.now() / 1000 - r.idle)}` : ""}</div>
      ${r.last_line ? `<div class="last-line mono">${esc(r.last_line)}</div>` : ""}
    </a>
    <div class="row">${runPill(r)}<a class="btn small" href="#/runs/${encodeURIComponent(r.id)}">Open log</a>
      ${target ? `<a class="btn small" href="#/jobs/${encodeURIComponent(target)}">Job</a>` : ""}
      <button class="btn small danger" data-stop="${esc(r.id)}" ${isStopping ? 'disabled aria-busy="true"' : ""}>${isStopping ? "Stopping…" : "Stop"}</button></div></div>`;
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
  // The health control sits opposite the branch picker. Detailed fixes stay in its compact disclosure.
  return `<div class="status-line status-stack home-status-line"><div class="status-main-row"><span class="status-branch"><span class="label">Branch</span>${branchPicker}</span>${homeHealthIndicator(setupState)}</div>
    ${stats.length ? `<span class="status-stats">${stats.join(`<span class="sep">·</span>`)}</span>` : ""}</div>`;
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
const LIVE = new Set(["home", "job", "activity", "git", "ux-review"]);

async function createReleaseTag() {
  try {
    const preview = await api("delivery/release");
    const values = await formDialog("Create release tag", `
      <p>Project: <strong>${esc(state.project?.name || "Current project")}</strong></p>
      <p>Bookmark this committed state on your base branch (<strong>${esc(preview.branch)}</strong>) as a release milestone to track future changes. Creating a tag records a version in Git; it does not deploy your app.</p>
      <div class="card-b-split pt-8 mt-8 mb-12">
        <div class="text-sm"><strong>Commit on ${esc(preview.branch)}:</strong> <code class="mono">${esc(preview.commit.slice(0, 10))}</code> · ${esc(preview.subject)}</div>
      </div>
      <label class="field"><span>Release version</span><input name="tag" required maxlength="120" value="${esc(preview.suggested_tag)}" placeholder="v0.1.0" spellcheck="false"></label>
      ${preview.can_push ? `<label class="check"><input type="checkbox" name="publish" checked><span>Publish this tag to the Git remote (origin)</span></label>` : `<p class="muted">No remote is configured. The tag will be saved locally.</p>`}
      <p class="muted font-13">Your current branch and uncommitted changes stay as they are.</p>`, "Create release tag");
    if (!values) return;
    const body = {tag: values.tag, commit: preview.commit, push: Boolean(values.publish)};
    let result = await api("delivery/release", {method: "POST", body});
    while (result.warning) {
      const retry = await formDialog("Release tag saved locally", `<p>${esc(result.warning)}</p><p>Version: <strong>${esc(result.tag)}</strong></p>`, "Retry publishing");
      if (!retry) { await route(); return; }
      result = await api("delivery/release", {method: "POST", body: {...body, push: true}});
    }
    toast(`${result.tag} ${result.pushed ? "created and published" : "saved locally"}`);
    await route();
  } catch (error) {
    toast(error.status === 404 ? "Update and restart your local Orchestrator server to enable release tagging." : error.message, true);
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-create-release]");
  if (!button || button.disabled || state.you?.role === "member") return;
  button.disabled = true;
  try { await createReleaseTag(); } finally { button.disabled = false; }
});

// ---------------------------------------------------------------- setup checklist
// What a job needs (GitHub, an AI provider, a machine, project config) vs. nice-to-haves.
// First launch: a full page on Home. Afterwards: a bottom-right panel you can reopen.

let setupState = null;
let setupFetched = 0;
const setupDismissedRoots = new Set();
async function loadSetup(force = false) {
  if (!force && Date.now() - setupFetched < 15000) return setupState;
  const projectRoot = state.project?.root;
  setupFetched = Date.now();
  try { setupState = await api(force ? "setup?refresh=1" : "setup"); } catch { setupState = null; } // older servers have no /api/setup
  if (setupState && setupDismissedRoots.has(projectRoot)) setupState.seen = true;
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
    actions: `<button class="btn ghost" id="setup-recheck">Check again</button><button class="btn ghost" id="setup-skip">Skip for now</button>`,
    html: `<div class="setup-full">
      <div class="setup-progress"><div style="width:${Math.round(100 * s.required_done / s.required_total)}%"></div></div>
      <section class="card">${setupListHtml(s)}</section>
      ${s.complete ? `<div class="row mt-16"><button class="btn primary big" id="setup-done">Continue to Home</button></div>` : ""}</div>`,
    after: () => {
      const seen = () => {
        setupDismissedRoots.add(state.project?.root);
        if (setupState) setupState.seen = true;
        apply({ title: "Home", sub: "", actions: "", html: `<div class="empty">Loading Home…</div>` });
        route();
        api("config/setup-seen", { method: "POST", body: {} }).catch(() => {
          toast("Couldn't save your setup dismissal. Setup may appear again after reloading.", true);
        });
      };
      $("#setup-skip")?.addEventListener("click", seen);
      $("#setup-done")?.addEventListener("click", seen);
      $("#setup-recheck")?.addEventListener("click", async () => { await loadSetup(true); route(); });
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
  const badge = $("#setup-badge");
  if (badge) {
    const remaining = s ? s.required_total - s.required_done : 0;
    badge.textContent = String(remaining);
    badge.hidden = !s || !remaining;
  }
  if ($("#setup-panel") && !$("#setup-panel").hidden) renderSetupPanel();
}

function renderSetupPanel() {
  const s = setupState, panel = $("#setup-panel");
  if (!s) return;
  panel.innerHTML = `<div class="setup-panel-h"><strong>Setup</strong><span class="muted">${s.required_done}/${s.required_total} required</span>
    <button class="btn small ghost" data-setup-close aria-label="Close">✕</button></div><div class="setup-panel-b">${setupListHtml(s)}
    <a class="btn small ghost setup-panel-more" href="#/checkup" data-setup-close>Open onboarding checklist</a></div>`;
}

async function openSetupPanel() {
  const panel = $("#setup-panel");
  if (setupState) renderSetupPanel();
  else panel.innerHTML = `<div class="setup-panel-b" role="status">Checking setup…</div>`;
  panel.hidden = false;
  await loadSetup(true);
  if (panel.hidden) return;
  if (!setupState) {
    closeSetupPanel();
    return toast("The setup checklist isn't available yet. It will appear after your next Orchestrator update.", true);
  }
  renderSetupPanel();
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
    } else if (item.action.type === "configure_project") {
      go.disabled = true;
      try {
        await api("setup/configure", { method: "POST", body: {} });
        toast("Project configured");
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

function homeHealthIndicator(s) {
  if (!s?.items) return `<details class="home-health-inline home-health-yellow"><summary class="home-health-trigger" title="System health: status unavailable"><span class="home-health-light" aria-hidden="true"></span><span>Status unavailable</span></summary><div class="home-health-popover"><strong>System health</strong><p>Setup checks could not be loaded. Reload Home to try again.</p></div></details>`;
  const required = s.items.filter(item => item.required);
  const missing = required.filter(item => !item.done);
  const optional = s.items.filter(item => !item.required);
  const optionalMissing = optional.filter(item => !item.done);
  const tone = missing.length ? "red" : "green";
  const status = missing.length ? "Not ready" : "Ready";
  const summary = missing.length ? `${missing.length} required item${missing.length === 1 ? "" : "s"} to fix`
    : optionalMissing.length ? `All ${s.required_total} required items are ready · ${optionalMissing.length} optional to review` : "All required setup is ready";
  return `<details class="home-health-inline home-health-${tone}">
    <summary class="home-health-trigger" title="System health: ${esc(status)}${optionalMissing.length ? `; ${optionalMissing.length} optional items available` : ""}"><span class="home-health-light${!missing.length && optionalMissing.length ? " home-health-light-optional" : ""}" aria-hidden="true"></span><span>${esc(status)}</span></summary>
    <div class="home-health-popover"><div class="home-health-popover-head"><strong>System health</strong><span>${esc(summary)}</span></div>
      <div class="home-health-section"><h3>Required · ${required.length - missing.length}/${required.length} ready</h3>${required.length ? required.map(setupItem).join("") : `<p class="home-health-ready">No required setup items.</p>`}</div>
      ${optional.length ? `<div class="home-health-section home-health-optional"><h3>Optional · ${optional.length - optionalMissing.length}/${optional.length} ready</h3>${optional.map(setupItem).join("")}</div>` : ""}
    </div>
  </details>`;
}

function positionHomeHealthPopover(details) {
  const panel = details.querySelector(":scope > .home-health-popover");
  const trigger = details.querySelector(":scope > .home-health-trigger");
  if (!details.open || !panel || !trigger) return;

  const margin = 12;
  const gap = 8;
  panel.style.visibility = "hidden";
  panel.style.maxHeight = "";
  const triggerRect = trigger.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const left = Math.max(margin, Math.min(triggerRect.right - panelRect.width, window.innerWidth - panelRect.width - margin));
  const below = window.innerHeight - margin - triggerRect.bottom - gap;
  const above = triggerRect.top - gap - margin;
  const placeBelow = below >= Math.min(panelRect.height, 180) || below >= above;
  const available = Math.max(80, placeBelow ? below : above);
  panel.style.maxHeight = `${Math.floor(available)}px`;
  const top = placeBelow
    ? triggerRect.bottom + gap
    : Math.max(margin, triggerRect.top - gap - Math.min(panelRect.height, available));
  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
  panel.style.visibility = "";
}

function repositionOpenHomeHealthPopovers() {
  document.querySelectorAll?.(".home-health-inline[open]")?.forEach(positionHomeHealthPopover);
}

document.addEventListener("toggle", (event) => {
  const details = event.target.matches?.(".home-health-inline")
    ? event.target
    : event.target.closest?.(".home-health-inline");
  if (details?.open) requestAnimationFrame(() => positionHomeHealthPopover(details));
}, true);
document.addEventListener("click", (event) => {
  document.querySelectorAll?.(".home-health-inline[open]")?.forEach((details) => {
    if (!details.contains(event.target)) details.open = false;
  });
});
document.addEventListener("scroll", repositionOpenHomeHealthPopovers, true);
if (typeof window !== "undefined") window.addEventListener("resize", repositionOpenHomeHealthPopovers);

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
    backlog: ["Backlog", (j) => j.state.group === "backlog"],
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
      ${product && !product.sections.some((x) => x.filled) ? productStripHtml(product) : ""}

      ${runningBanner}

      <section class="card">
        <div class="card-h card-h-wrap">
          <h2>Jobs <span class="count">${archived ? archived.length : filteredJobs.length}</span></h2>
          <div class="filters">
            ${Object.entries(HOME_FILTERS).map(([key, [label, fn]]) => {
              const count = sortedJobs.filter(fn).length;
              if (key === "backlog" && !count && filter !== "backlog") return ""; // waiting epics only get a filter once there are some
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
        ` : `<div class="empty">${!jobs.length ? 'No jobs yet. <a href="#/new">Start a new job</a> to plan work from a description, bug report, or design.' : 'No jobs match this filter.'}</div>`}
        ${elsewhereLine(waiting.elsewhere)}
      </section>`,
    after: () => {
      const homeModelSelect = view.querySelector("#prd-home-model-select");
      if (homeModelSelect) {
        homeModelSelect.dataset.previous = homeModelSelect.value;
        homeModelSelect.addEventListener("change", async (e) => {
          const next = e.target.value;
          if (next === "__manage_llms__") {
            e.target.value = homeModelSelect.dataset.previous || product?.model || "claude-sonnet-4-6";
            location.hash = "#/config/models";
            return;
          }
          homeModelSelect.dataset.previous = next;
          try {
            await api("product/model", { method: "POST", body: { model: next } });
            toast("Draft model updated");
          } catch (err) {
            toast(err.message, true);
          }
        });
      }
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
      Boolean(s.is_orchestrator_improvement || s.tag === "improve_orchestrator") && !s.submitted_for_inclusion
        && ["Submit for Prize 🏆", `data-submit-inclusion="${esc(s.id)}" data-job-title="${esc(s.title || '')}"`, "Submit this improvement to the core team for permanent inclusion"],
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

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-submit-inclusion]");
  if (!btn) return;
  const jobId = btn.dataset.submitInclusion;
  Feedback.open({
    initialStep: "improve_orchestrator",
    category: "improve_orchestrator",
    jobId,
    title: btn.dataset.jobTitle || "",
    explanation: jobId ? `Completed improvement via job ${jobId}.` : "",
    userName: state.you?.email || ""
  });
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

// An approved epic waits as a placeholder job. Planning it makes a new feature job and removes this one, so this page
// says what the epic is, what "Make a plan" does, and (while planning) hands over to the new job as soon as it exists.
function waitingEpicPage({ id, s, job, runs, docs, links }) {
  const planning = runs.find((r) => r.running);
  if (planning?.result_job && planning.result_job !== id) location.hash = `#/jobs/${encodeURIComponent(planning.result_job)}`;
  const failed = !planning && runs.find((r) => r.action === "plan_epic" && r.exit_code);
  const brief = (docs || []).find((d) => d.name === "brief.md");
  return {
    title: s.title,
    sub: `<span class="status-line"><span>Epic</span><span class="sep">·</span><span>${planning ? "Planning" : "No plan yet"}</span></span>`,
    actions: jobHeaderActions({ ...s, active_run: Boolean(planning) }, links, {}),
    html: `<div class="job-layout"><div class="job-top">
      ${planning ? `<section class="card mb-16"><div class="list">${liveRunCard(planning)}</div></section>` : ""}
      <section class="job-hero tone-${planning ? "working" : "attention"}">
        <div class="job-hero-main">
          <h2 class="job-hero-title">${planning ? "Planning this epic" : "Ready to plan"}</h2>
          <p class="job-hero-reason">${planning ? "The AI is breaking this epic into tasks." : "You approved this epic, but nothing is built from it yet. Planning asks the AI to break it into tasks."}</p>
          <div class="job-hero-next-preview"><span class="job-hero-next-label">Then:</span> ${planning
            ? "the plan opens as its own job and you're taken there"
            : "the plan opens as its own job, which replaces this page. You read it and approve it before anything is built"}</div>
          ${failed ? `<p class="job-hero-reason">The last planning attempt failed. <a href="#/runs/${encodeURIComponent(failed.id)}">Open its log</a>, or plan it again.</p>` : ""}
        </div>
        <div class="job-hero-action">${planning ? "" : `<button class="btn primary" ${act("plan_epic", { job: s.id })}>Make a plan</button>`}</div>
      </section>
      <section class="card mb-16" id="brief-section">
        <div class="card-h"><h2>What this epic covers</h2></div>
        <div class="md brief-content">${brief?.text ? Markdown.render(brief.text) : `<p class="muted">${esc(job.summary || s.title)}</p>`}</div>
      </section>
    </div></div>`,
  };
}

pages.job = async ([id]) => {
  const data = await api(`jobs/${encodeURIComponent(id)}`);
  const { summary: s, job, outputs, runs, docs, changes, links, test_summary: testSummary, pipeline: pipe, tasks: jobTasks, completed_tasks: jobCompleted, next_task: jobNextTask } = data;

  const activeRun = runs.find((r) => r.running);
  if (s.status === "backlog") return waitingEpicPage({ id, s, job, runs, docs, links });
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
  const isStoppingActive = activeRun && (stoppingRuns.has(activeRun.id) || Boolean(activeRun.stopping));
  const heroAction = activeRun
    ? (isStoppingActive
      ? `<button class="btn" data-stop="${esc(activeRun.id)}" disabled aria-busy="true"><span class="spinner inline-spinner" aria-hidden="true"></span>Stopping…</button>`
      : `<button class="btn" data-stop="${esc(activeRun.id)}" title="Stops the worker. Resume continues from the next task.">Pause</button>`)
    : next ? `<button class="btn primary" ${act(next.action, { job: s.id })}>${esc(next.label)}</button>${job.task_growth && data.undoable_task === job.task_growth.task
      ? `<button class="btn" data-undo-task="${job.task_growth.task}" title="Reverts what that task changed. Then revise the plan to make it smaller.">Undo task ${job.task_growth.task + 1}</button>` : ""}` : "";

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
    const more = withDetail && (detail || doneWhen.length || files.length);
    return `<div class="item task-row">${marker}<div class="main-col"><div class="title">${esc(title)}</div>${!withDetail && detail ? `<div class="meta">${esc(detail)}</div>` : ""}
      ${more ? `<details class="task-detail"><summary class="linklike muted">Details</summary>${detail ? `<div class="meta">${esc(detail)}</div>` : ""}
      ${doneWhen.length ? `<div class="task-done-when"><span class="label">Done when</span><ul>${doneWhen.slice(0, 4).map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>` : ""}
      ${files.length ? `<div class="task-files">${files.slice(0, 4).map((f) => `<code>${esc(f)}</code>`).join("")}${files.length > 4 ? `<span class="muted">+${files.length - 4} more</span>` : ""}</div>` : ""}</details>` : ""}</div>${controls}${undo}</div>`;
  }).join("") || `<div class="empty">No tasks yet.</div>`;
  // One decision: what it is (a sentence), how big it is, and the one button. The tasks are one line each; the rest is a tap away.
  const size = data.plan_size || { label: "", files: 0, too_large: false, reasons: [], deferred: [] };
  const sliceWarnings = job.plan?.slice_warnings || [];
  const sizeLine = [plural(tasks.length, "task"), size.files ? plural(size.files, "file") : "", size.too_large ? "" : size.label].filter(Boolean).join(" · ");
  const splitInstead = size.too_large && canSplinter;
  const approveBtn = (primary) => next ? `<button class="btn small${primary ? " primary" : ""}" ${act(next.action, { job: s.id })}>${esc(splitInstead ? `${next.label} anyway` : next.label)}</button>` : "";
  const planCardHtml = `<section class="card mb-16 plan-review${size.too_large ? " too-large" : nextAction === "approve" ? " needs-approval" : ""}" id="plan-section">
      <div class="card-h"><h2>${size.too_large ? "Too big for one job" : nextAction === "approve" ? "Approve this plan" : "The plan"}</h2>
        <div class="card-actions"><button class="btn small" ${act("revise", { job: s.id })} title="Ask the AI to re-plan with what should change">Revise…</button>${splitInstead
          ? `<button class="btn small primary" ${act("splinter_job", { job: s.id })} title="Each task becomes its own job">Split into ${tasks.length} jobs</button>` : approveBtn(true)}</div></div>
      ${showSummary ? `<div class="card-b plan-summary">${clamped(planSummary, 160)}</div>` : ""}
      <div class="plan-verdict${size.too_large ? " bad" : ""}"><span class="plan-size">${esc(sizeLine)}</span>
        ${size.too_large ? `<span>${esc(size.reasons.join(" "))}</span>${splitInstead ? approveBtn(false) : ""}` : ""}
        ${sliceWarnings.length ? `<span class="plan-flag" title="${esc(sliceWarnings.join(" "))}">Tasks look layered, not working slices</span>` : ""}</div>
      <div class="list">${taskRowsHtml(true)}</div>
      ${canEditPlan ? `<div class="card-b card-b-split"><button class="btn small" data-task-op="add">Add a task</button></div>` : ""}
      ${size.deferred.length ? `<details class="fold plan-notes"><summary class="card-h"><h2>Left for later <span class="count">${size.deferred.length}</span></h2></summary>
        <div class="card-b"><ul class="assumptions">${size.deferred.map((d) => `<li>${esc(d)}</li>`).join("")}</ul></div></details>` : ""}
      ${notes.length ? `<details class="fold plan-notes"><summary class="card-h"><h2>Assumptions and risks <span class="count">${notes.reduce((n, [, l]) => n + l.length, 0)}</span></h2></summary>
        <div class="card-b stack">${notes.map(([label, list]) => `<div><div class="label">${label}</div><ul class="assumptions">${list.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`).join("")}</div></details>` : ""}
    </section>`;
  const architectConcerns = s.question && /architect/i.test(s.question) && /architect/i.test(s.state.reason || "");
  const heroReason = architectConcerns ? "The architect raised concerns (below). Answer them, or accept its suggestions and re-plan."
    : reviewingPlan ? `Read <button type="button" class="linklike" data-scroll-to="#plan-section">the plan below</button>: ${plural(tasks.length, "task")}, each with what "done" means and the files it touches. ${nextAction === "approve" ? "Approve it, or revise it if something's off." : "Start when you're happy with it."}`
    : s.status === "designing" && nextAction === "approve" ? `Read <button type="button" class="linklike" data-scroll-to="#brief-section">the design below</button>, then approve it or ask for changes.`
    : clamped(s.state.reason || "", 320);

  const isAutoImprove = Boolean(s.is_orchestrator_improvement || s.tag === "improve_orchestrator" || job.is_orchestrator_improvement || job.tag === "improve_orchestrator");

  return {
    title: s.title,
    sub: `<span class="status-line"><span>${esc(jobKind(s))}</span>${s.issue_number ? `<span class="sep">·</span><span>#${esc(s.issue_number)}</span>` : ""}${s.feature ? `<span class="sep">·</span><span>${esc(featureName || s.feature)}</span>` : ""}${s.branch ? `<span class="sep">·</span><span class="mono">${esc(s.branch)}</span>` : ""}${(data.context?.ticket || []).map((t) => `<span class="sep">·</span>${t.url
      ? `<a class="ticket-chip" href="${esc(t.url)}" target="_blank" rel="noopener" title="${esc(`${t.source}: ${t.title}`)}">${esc(t.ref || t.title)} ↗</a>`
      : `<span class="ticket-chip" title="${esc(t.source)}">${esc(t.ref || t.title)}</span>`}`).join("")}${isAutoImprove ? `<span class="sep">·</span><span class="prize-pill"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-sparkle"/></svg> Orchestrator Improvement</span>` : ""}${s.submitted_for_inclusion || job.submitted_for_inclusion ? `<span class="sep">·</span><span class="accent">✓ Submitted for inclusion</span>` : ""}</span>`,
    actions: jobHeaderActions(summary, links, { canSplinter, planShown: reviewingPlan }),
    html: `<div class="job-layout"><div class="job-top">
      ${runs.filter((r) => r.running).map((r) => `<section class="card mb-16"><div class="list">${liveRunCard(r)}</div></section>`).join("")}

      ${isAutoImprove ? `
        <div class="banner info mb-16 row align-center justify-between" style="border-left: 3px solid #fbbf24; background: rgba(245, 158, 11, 0.08); padding: 12px 16px;">
          <div class="row align-center gap-8">
            <span style="font-size: 1.25rem;">🏆</span>
            <div>
              <strong>Orchestrator Improvement Job</strong>
              <div class="muted" style="font-size: var(--text-xs);">This job adds a feature or fix to Orchestrator itself.</div>
            </div>
          </div>
          <div>
            ${job.submitted_for_inclusion || s.submitted_for_inclusion ? `
              <span class="pill done">✓ Submitted for Permanent Inclusion</span>
            ` : `
              <button type="button" class="btn small prize-btn" data-submit-inclusion="${esc(s.id)}" data-job-title="${esc(s.title)}"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-sparkle"/></svg> Submit for Prize 🏆</button>
            `}
          </div>
        </div>
      ` : ""}

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

      ${(job.plan?.slice_warnings || []).length && !reviewingPlan ? `<section class="card mb-16"><div class="card-h"><h2>Plan check</h2></div><div class="card-b stack">
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

      <!-- Simulator Visual QA Screenshots & Video (if any) -->
      ${(visualChecks.length && (visualChecks[0].screenshots.length || visualChecks[0].video)) ? `
        <section class="card mb-16 visual-check-job-card">
          <div class="card-h">
            <h2>Simulator Visual QA</h2>
            <div class="row gap-8">
              ${visualChecks[0].video ? `<span class="pill pill-sm">Video recorded</span>` : ""}
              <span class="count">${visualChecks[0].screenshots.length} captured</span>
            </div>
          </div>
          <p class="visual-check-desc">Boots the app on the simulator to check launch layout, element alignment, and safe-area insets. Click any screen to zoom and inspect.</p>
          <div class="card-b visual-check-job-body">
            ${visualChecks[0].video ? `
              <div class="visual-check-job-video-wrap">
                <div class="visual-check-video-label"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Launch &amp; Settle Video</div>
                <video class="visual-check-player" controls playsinline preload="metadata" src="${typeof visualVideoUrl === "function" ? visualVideoUrl(visualChecks[0]) : `/api/visual-checks/${encodeURIComponent(visualChecks[0].id)}/video/${encodeURIComponent(visualChecks[0].video)}`}">
                  Your browser does not support HTML5 video.
                </video>
              </div>` : ""}
            <div class="visual-check-job-grid">
              ${visualChecks[0].screenshots.map((img) => `
                <div class="visual-check-job-item">
                  <a href="/api/visual-checks/${encodeURIComponent(visualChecks[0].id)}/screenshots/${encodeURIComponent(img)}" target="_blank" rel="noopener" data-preview-img="/api/visual-checks/${encodeURIComponent(visualChecks[0].id)}/screenshots/${encodeURIComponent(img)}" data-preview-auth="visual-checks/${encodeURIComponent(visualChecks[0].id)}/screenshots/${encodeURIComponent(img)}" data-preview-title="${esc(img)}">
                    <img src="/api/visual-checks/${encodeURIComponent(visualChecks[0].id)}/screenshots/${encodeURIComponent(img)}" data-auth-src="visual-checks/${encodeURIComponent(visualChecks[0].id)}/screenshots/${encodeURIComponent(img)}" alt="Screenshot: ${esc(img)}">
                    ${typeof appearanceBadgeHtml === "function" ? appearanceBadgeHtml(img) : ""}
                  </a>
                  <div class="visual-check-thumb-caption muted mono">${esc(img)}</div>
                </div>
              `).join("")}
            </div>
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
      wireImagePreviews(view);
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

async function uploadFile(file, endpoint = "uploads", { signal } = {}) {
  const backend = getBackendUrl(), token = getToken();
  const res = await fetch(`${backend ?? ""}/api/${endpoint}?name=${encodeURIComponent(file.name)}`, {
    method: "POST", body: file, credentials: backend ? "omit" : "same-origin", signal,
    headers: { "Content-Type": "application/octet-stream", "X-Orchestrator-UI": "1", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

const formatBytes = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

function featureSpec(v) {
  const parts = [(v.details || "").trim()];
  for (const [key, label] of [["audience", "Who it's for"], ["outcome", "User outcome"], ["acceptance", "Acceptance criteria"], ["constraints", "Constraints and out of scope"]]) {
    if (v[key]?.trim()) parts.push(`## ${label}\n${v[key].trim()}`);
  }
  return parts.filter(Boolean).join("\n\n");
}

function filePickerContent(title, types = "", multiple = true) {
  return `<strong class="file-picker-title">${esc(title)}</strong>
    <span class="file-picker-target">
      <span class="file-picker-action"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V3m-5 5 5-5 5 5M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/></svg>Browse files</span>
      <span class="muted drop-hint">${multiple ? "or drop files here" : "or drop a file here"}</span>
      ${types ? `<small class="file-picker-types">${esc(types)}</small>` : ""}
    </span>`;
}

pages.new = async (_, query) => {
  let features = [];
  try { features = (await api("features")).features; } catch { /* the field just doesn't show */ }
  let undefinedProduct = false; // nothing says what the product is, so plans would be guessing
  try { undefinedProduct = !(await api("product")).sections.some((x) => x.filled); } catch { /* older server: no nudge */ }
  const skipKey = `orchestrator_skip_discovery:${state.project?.root || ""}`;
  const skipped = () => { try { return localStorage.getItem(skipKey) === "1"; } catch { return false; } };
  const wanted = query.get("type");
  const st = { type: JOB_TYPE_INFO.some(([v]) => v === wanted) ? wanted : "bug", uploads: {}, uploading: 0, logs: new Set(), recent: null, testCases: null,
    v: { summary: query.get("summary") || query.get("area") || "", details: query.get("spec") || "", audience: "", outcome: "", acceptance: "", constraints: "", repro: "", expected: "", vibe: "minimalist", customVibe: "", subsystems: "", test_cases: query.get("test_cases") || "", url: "" } };

  const loadTestCases = async () => {
    if (st.testCases !== null) return st.testCases;
    try {
      const res = await api("test-cases");
      st.testCases = res.cases || [];
    } catch {
      st.testCases = [];
    }
    return st.testCases;
  };

  const field = (label, input, hint = "") => `<label class="field"><span>${label}</span>${input}${hint ? `<small class="hint-text">${hint}</small>` : ""}</label>`;
  const opt = (text) => ` <span class="muted">(${text})</span>`;
  const uploadBox = (label, hint, accept, optional = true) => `<div class="field"><span>${label}${optional ? opt("optional") : ""}</span>
      <input type="file" id="nj-files" class="sr-only" multiple accept="${accept}" aria-label="${esc(label)}">
      <label class="prd-drop upload-drop file-picker" for="nj-files" id="nj-drop">${filePickerContent("Add files to your job", hint)}</label>
      <div class="upload-chips row" id="nj-chips" aria-live="polite"></div></div>`;
  const logPicker = () => {
    const rows = (st.recent || []).map((l, i) => `<label class="check"><input type="checkbox" data-nj-log="${esc(l.path)}" ${st.logs.has(l.path) ? "checked" : ""}>
      <span>${esc(l.label)}<small>${esc(ago(l.mtime))} · ${esc(formatBytes(l.size))}${l.kind === "device" ? " · from a phone" : ""}${i === 0 ? " · newest" : ""}</small></span></label>`).join("");
    return `<fieldset class="field nj-logs"><span>Attach logs</span>
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
          ${field(`Describe repro steps 1 line at a time.${opt("optional")}`, `<textarea name="repro" rows="3" placeholder="1. Join a lobby&#10;2. Background the app for 30 seconds&#10;3. Come back">${esc(v.repro)}</textarea>`, "One step per line.")}
          ${field(`What should happen instead?${opt("optional")}`, `<input type="text" name="expected" maxlength="2000" value="${esc(v.expected)}" placeholder="The seat is still mine">`)}
          ${logPicker()}${picker.html}` };
      }
      case "feature": {
        const picker = await linkPickerHtml({ prefer: ["jira", "trello"], label: "Link a ticket or card", emptyHint: "" });
        return { picker, html: `
          ${field("What should it do?", `<input type="text" name="summary" required maxlength="500" value="${esc(v.summary)}" placeholder="e.g. Let either player ask for a rematch">`)}
          <div class="brief-grid">
            ${field(`Who is it for?${opt("optional")}`, `<input type="text" name="audience" maxlength="500" value="${esc(v.audience)}" placeholder="e.g. Players finishing a match">`)}
            ${field(`What will they achieve?${opt("optional")}`, `<input type="text" name="outcome" maxlength="1000" value="${esc(v.outcome)}" placeholder="e.g. Play again without making a new lobby">`)}
          </div>
          ${field(`What counts as done?${opt("optional")}`, `<textarea name="acceptance" rows="3" maxlength="5000" placeholder="Both players accept → a new match starts&#10;Either declines → return to the lobby">${esc(v.acceptance)}</textarea>`, "A few testable outcomes, including any important edge case.")}
          <details class="advanced"><summary>Constraints and context</summary><div class="stack">
            ${field("Constraints or things to leave out", `<input type="text" name="constraints" maxlength="2000" value="${esc(v.constraints)}" placeholder="e.g. Keep the existing lobby; no ranked rematches">`)}
            ${field("Additional context", `<textarea name="spec" rows="3" placeholder="Rules, references, or an existing brief">${esc(v.details)}</textarea>`)}
            <small class="hint-text">Leave answers blank and the AI will list its assumptions in the plan.</small>
          </div></details>
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
      case "coverage": {
        const cases = await loadTestCases();
        const areas = [...new Set(cases.map((c) => c.area).filter(Boolean))];
        return { picker: null, html: `
          ${field("Choose area to test", `<input type="text" name="summary" list="nj-area-list" required maxlength="500" value="${esc(v.summary)}" placeholder="e.g. Lobby, Authentication, or Checkout">`, "Pick an existing area or type a new one to focus coverage on.")}
          ${areas.length ? `<datalist id="nj-area-list">${areas.map((a) => `<option value="${esc(a)}">`).join("")}</datalist>` : ""}
          ${field(`What should be covered?${opt("optional")}`, `<textarea name="spec" rows="3" maxlength="20000" placeholder="Describe scenarios, edge cases, failure modes, or specific behaviors to test...">${esc(v.details)}</textarea>`, "Be as descriptive as you like: scenarios, edge cases, or expected behaviors.")}
          <div class="field">
            <div class="row align-center justify-between">
              <span>Specific test cases${opt("optional")}</span>
              ${cases.length ? `<button type="button" class="btn small ghost" id="nj-import-tc-btn"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg> Import from test cases</button>` : ""}
            </div>
            <textarea name="test_cases" id="nj-test-cases" rows="3" maxlength="20000" placeholder="e.g. TC-1: Rejoin after timeout&#10;TC-4: Seat reservation failure">${esc(v.test_cases)}</textarea>
            <small class="hint-text">List individual test cases to implement or expand, or import them from your test library.</small>
          </div>
          ${logPicker()}` };
      }
      default:
        return { picker: null, html: `${field("What should change?", `<textarea name="summary" required rows="4" maxlength="20000" placeholder="e.g. Rename Foo to Bar everywhere and update the docs">${esc(v.summary)}</textarea>`, "This goes straight to the builder with no separate planning step.")}` };
    }
  };

  const mine = () => (st.uploads[st.type] ||= []); // files belong to the kind of job they were added to
  const form = () => $("#new-job");
  const read = (name) => form()?.elements[name]?.value ?? undefined;
  const capture = () => { // remember what was typed before the fields are swapped
    const v = st.v;
    for (const [key, name] of [["summary", "summary"], ["details", "spec"], ["audience", "audience"], ["outcome", "outcome"], ["acceptance", "acceptance"], ["constraints", "constraints"], ["repro", "repro"], ["expected", "expected"], ["vibe", "vibe"], ["customVibe", "customVibe"], ["subsystems", "subsystems"], ["test_cases", "test_cases"], ["url", "url"]]) {
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
    $("#nj-import-tc-btn")?.addEventListener("click", async () => {
      const cases = await loadTestCases();
      if (!cases || !cases.length) {
        toast("No test cases found in project library.", "warning");
        return;
      }
      const currentVal = ($("#nj-test-cases")?.value || "").trim();
      const bodyHtml = `
        <div class="stack tc-import-dialog">
          <p class="muted">Select test cases to add to this test job:</p>
          <input type="search" id="nj-tc-filter" class="tc-import-filter" placeholder="Filter by ID, title, or area...">
          <div id="nj-tc-dialog-list" class="list tc-import-list">
            ${cases.map((c) => {
              const itemText = `${c.id}: ${c.title}`;
              const isSelected = currentVal.includes(c.id);
              return `<label class="check tc-import-item" data-filter="${esc((c.id + ' ' + c.title + ' ' + (c.area || '')).toLowerCase())}">
                <input type="checkbox" name="import_tc" value="${esc(itemText)}" ${isSelected ? "checked" : ""}>
                <span><strong>${esc(c.id)}</strong>: ${esc(c.title)} <span class="badge tc-import-badge">${esc(c.area || "General")}</span></span>
              </label>`;
            }).join("")}
          </div>
        </div>
      `;
      const dialogPromise = formDialog("Import test cases", bodyHtml, "Add to job", { compact: false });
      $("#nj-tc-filter")?.addEventListener("input", (ev) => {
        const q = ev.target.value.toLowerCase();
        document.querySelectorAll(".tc-import-item").forEach((item) => {
          item.hidden = !item.dataset.filter.includes(q);
        });
      });
      const result = await dialogPromise;
      if (result) {
        const checked = [...document.querySelectorAll('#dialog-form input[name="import_tc"]:checked')].map((cb) => cb.value);
        if (checked.length) {
          const existing = ($("#nj-test-cases")?.value || "").trim();
          const existingLines = new Set(existing ? existing.split("\n").map((s) => s.trim()) : []);
          const newItems = checked.filter((c) => !existingLines.has(c));
          const combined = existing ? (newItems.length ? existing + "\n" + newItems.join("\n") : existing) : checked.join("\n");
          st.v.test_cases = combined;
          if ($("#nj-test-cases")) $("#nj-test-cases").value = combined;
          toast(`Imported ${checked.length} test case${checked.length === 1 ? "" : "s"}.`);
        }
      }
    });
    document.querySelectorAll("[data-nj-log]").forEach((box) => box.addEventListener("change", () => { box.checked ? st.logs.add(box.dataset.njLog) : st.logs.delete(box.dataset.njLog); }));
    if ((st.type === "bug" || st.type === "coverage") && st.recent === null) {
      // only a log from the last day is likely to be about this problem, so only that one starts ticked
      try { st.recent = (await api("recent-logs")).logs; if (st.logs.size === 0 && st.recent[0] && Date.now() / 1000 - st.recent[0].mtime < 86400) st.logs.add(st.recent[0].path);  } catch { st.recent = []; }
      if (st.type === "bug" || st.type === "coverage") { capture(); await render(); }
    }
  };

  return {
    title: "New job",
    sub: "",
    html: `
    <form class="card card-b stack" id="new-job">
      <div class="field"><span>What kind of work?</span>
        <div class="segmented" role="radiogroup" aria-label="Kind of work">${JOB_TYPE_INFO.map(([v, t, d]) =>
          `<label class="job-type-card job-type-${v}" data-type="${v}"><input type="radio" name="type" value="${v}" ${st.type === v ? "checked" : ""}>${t}<small>${d}</small></label>`).join("")}
        </div></div>
      <div id="nj-discovery"></div>
      <div class="stack" id="nj-fields"></div>
      ${featureSelect()}
      <div class="autopilot-option">
        <label class="check risky"><input type="checkbox" name="yolo"><span><strong>YOLO (Autopilot)</strong><small>Runs without confirmations or review pauses.</small></span></label>
      </div>
      <details class="advanced"><summary>Advanced</summary>
        <div class="stack">
          <label class="field"><span>Where to work</span>
            <select name="branch_mode">
              <option value="current" selected>Use the current branch</option>
              <option value="new">Create a new branch for this job</option>
              <option value="manual">Don't create or switch branches</option>
            </select>
            <small class="hint-text">Jobs share the current branch by default. For parallel jobs touching the same files, separate branches and worktrees keep changes isolated.</small>
          </label>
          <label class="check"><input type="checkbox" name="no_dispatch"><span>Plan only<small>Stop after planning so you can review the plan first.</small></span></label>
          <label class="check"><input type="checkbox" name="free"><span>Use free AI only</span></label>
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
        const params = { type: st.type, summary: v.summary, branch_mode: f.get("branch_mode") || "current", no_dispatch: f.has("no_dispatch"), yolo: f.has("yolo"), free: f.has("free"),
          feature: f.get("feature") || "", links, files: mine().map((u) => u.path) };
        if (st.type === "bug") Object.assign(params, { repro: v.repro, expected: v.expected, logs: [...st.logs] });
        if (st.type === "feature") params.spec = featureSpec(v);
        if (st.type === "design") Object.assign(params, { spec: v.details, vibe: v.vibe === "other" ? (v.customVibe.trim() || "minimalist") : v.vibe, urls: v.url.trim() ? [v.url.trim()] : [] });
        if (st.type === "coverage") Object.assign(params, { subsystems: v.subsystems || v.summary, spec: v.details, test_cases: v.test_cases, logs: [...st.logs], title: "Coverage expanding" });
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

pages.delivery = async (args, query) => {
  const branchParam = query?.get("branch") || "";
  const d = await api("delivery" + (branchParam ? `?branch=${encodeURIComponent(branchParam)}` : ""));
  const { testers, pipeline, branches = [], current_branch = "", selected_branch = "", branch_commit = {}, latest_job = null, user_email = "" } = d;
  const when = (iso) => (iso ? ago(Date.parse(iso) / 1000) : "");

  const jobTone = (status) => (status === "completed" ? "done" : (status === "review-needed" || status === "human-needed") ? "attention" : status === "failed" ? "failed" : "working");

  const baseBranchName = d.live?.branch || "main";
  const hasTag = Boolean(d.live?.tag);
  const unreleasedCount = d.live?.unreleased ?? 0;
  const releasesUrl = d.web_url ? `${d.web_url}/releases` : "";

  const releaseCard = `<section class="card mb-16">
    <div class="card-h">
      <h2>Release versions</h2>
      <div class="row gap-8 align-center">
        <span class="muted font-13">Base branch: <strong class="mono">${esc(baseBranchName)}</strong></span>
        ${d.live?.commit ? `<button type="button" class="btn small" data-create-release data-owner-only>Create release tag</button>` : ""}
      </div>
    </div>
    <div class="card-b stack">
      <div class="branch-meta-box">
        <div class="branch-meta-row">
          <div class="branch-meta-label">
            <span class="branch-meta-tag">Latest release</span>
          </div>
          <div class="branch-meta-main">
            ${hasTag ? `
              <div class="branch-meta-title">
                <code class="mono branch-commit-hash">${esc(d.live.tag)}</code>
                ${releasesUrl ? `<a class="muted font-13" href="${esc(releasesUrl)}" target="_blank" rel="noopener">View all releases on GitHub ↗</a>` : ""}
              </div>
              <div class="branch-meta-sub">
                <span>Tagged on base branch <strong>${esc(baseBranchName)}</strong></span>
              </div>
            ` : `
              <div class="branch-meta-title">
                <span class="muted text-sm">No releases tagged yet on <strong>${esc(baseBranchName)}</strong>.</span>
              </div>
              <div class="branch-meta-sub">
                <span>Tag a release milestone when you ship to track new changes that follow.</span>
              </div>
            `}
          </div>
        </div>

        <div class="branch-meta-row">
          <div class="branch-meta-label">
            <span class="branch-meta-tag">Unreleased</span>
          </div>
          <div class="branch-meta-main">
            ${hasTag ? `
              <div class="branch-meta-title">
                ${unreleasedCount === 0 ? `
                  <span class="pill done">Up to date</span>
                  <span class="text-sm">No unreleased commits on <strong>${esc(baseBranchName)}</strong> since <strong>${esc(d.live.tag)}</strong>.</span>
                ` : `
                  <span class="pill attention">${unreleasedCount} unreleased commit${unreleasedCount === 1 ? "" : "s"}</span>
                  <span class="text-sm">on <strong>${esc(baseBranchName)}</strong> since <strong>${esc(d.live.tag)}</strong></span>
                `}
              </div>
              <div class="branch-meta-sub">
                <span>${unreleasedCount === 0 ? "Production is running the latest code from this branch." : "Ready to be tested and packaged into your next release milestone."}</span>
              </div>
            ` : `
              <div class="branch-meta-title">
                <span class="pill working">${unreleasedCount} commit${unreleasedCount === 1 ? "" : "s"}</span>
                <span class="text-sm">accumulated on <strong>${esc(baseBranchName)}</strong> awaiting initial release tag.</span>
              </div>
              <div class="branch-meta-sub">
                <span>Click "Create release tag" above to establish your initial release version anchor.</span>
              </div>
            `}
          </div>
        </div>
      </div>

      <p class="muted font-13">Tags anchor official versions on your base branch (<strong>${esc(baseBranchName)}</strong>) to track what changed since you shipped. Creating a tag records a milestone in Git; it does not deploy or upload your app.</p>
      ${!d.live?.commit ? `<a class="btn small" href="#/git">Sync the base branch in Git setup</a>` : ""}
    </div>
  </section>`;

  const branchCard = `<section class="card mb-16">
    <div class="card-h">
      <h2>Branch &amp; Test Build</h2>
      <div class="row gap-8 align-center">
        <span class="muted font-13">Branch:</span>
        <select id="delivery-branch-select" class="mono" style="min-width: 140px;">
          ${branches.map((b) => `<option value="${esc(b)}" ${b === selected_branch ? "selected" : ""}>${esc(b)}${b === current_branch ? " (current)" : ""}</option>`).join("")}
        </select>
      </div>
    </div>
    <div class="card-b stack">
      <div class="branch-meta-box">
        <div class="branch-meta-row">
          <div class="branch-meta-label">
            <span class="branch-meta-tag">Latest commit</span>
          </div>
          <div class="branch-meta-main">
            ${branch_commit.commit ? `
              <div class="branch-meta-title">
                <code class="mono branch-commit-hash">${esc(branch_commit.commit)}</code>
                <span class="branch-commit-subject">${esc(branch_commit.subject || "(no commit message)")}</span>
              </div>
              <div class="branch-meta-sub">
                ${branch_commit.author ? `<span>by <strong>${esc(branch_commit.author)}</strong></span><span class="sep">·</span>` : ""}
                <span>${esc(branch_commit.when)}</span>
              </div>
            ` : `<div class="muted text-sm">No commit information found for this branch.</div>`}
          </div>
        </div>
        <div class="branch-meta-row">
          <div class="branch-meta-label">
            <span class="branch-meta-tag">Latest job</span>
          </div>
          <div class="branch-meta-main">
            ${latest_job ? `
              <div class="branch-meta-title">
                <a class="branch-job-link" href="#/jobs/${encodeURIComponent(latest_job.id)}"><strong>${esc(latest_job.title || latest_job.id)}</strong></a>
                <span class="pill ${jobTone(latest_job.status)}">${esc(latest_job.status)}</span>
              </div>
              <div class="branch-meta-sub">
                <span>Updated ${esc(ago(latest_job.updated))}</span>
              </div>
            ` : `<div class="muted text-sm">No Orchestrator jobs have run on this branch yet.</div>`}
          </div>
        </div>
      </div>

      ${!testers.configured ? `
        <div class="notice">
          <div class="stack">
            <strong>Distribution is not set up yet.</strong>
            <p class="muted">Configure Firebase App Distribution and code signing to build and install test versions on your devices.</p>
            <div><a class="btn small primary" href="#/config/firebase">Set up Firebase</a></div>
          </div>
        </div>
      ` : `
        <form id="delivery-build-form" class="stack gap-12 mt-8 pt-12" style="border-top: 1px solid var(--border);">
          <div class="field">
            <span class="label">Send to</span>
            <div class="row gap-16 align-center flex-wrap">
              <label class="row gap-6 align-center cursor-pointer">
                <input type="radio" name="recipient_type" value="me" checked>
                <span>Just me ${user_email ? `<span class="muted">(${esc(user_email)})</span>` : ""}</span>
              </label>
              <label class="row gap-6 align-center cursor-pointer">
                <input type="radio" name="recipient_type" value="group">
                <span>Tester group:</span>
              </label>
              <select id="delivery-group-select" name="group" style="min-width: 140px;" ${testers.groups.length ? "" : "disabled"}>
                ${testers.groups.length ? testers.groups.map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("") : `<option value="">No groups configured</option>`}
              </select>
              <button type="button" class="btn small ghost" id="delivery-new-group-btn" title="Create a new Firebase tester group">+ New group</button>
            </div>
          </div>
          <label class="field">
            <span>Release notes <span class="muted">(optional)</span></span>
            <input type="text" name="notes" placeholder="e.g. Test notes for this build">
          </label>
          <div class="row gap-10 align-center">
            <button type="submit" class="btn primary" ${act("distribute")}>Launch build for testing</button>
            ${testers.invite_url ? `
              <span class="muted">·</span>
              <button type="button" class="btn small ghost" data-setup-copy="${esc(testers.invite_url)}">Copy invite link</button>
            ` : ""}
          </div>
          ${!testers.cli_installed ? `
            <div class="notice bad">The Firebase CLI isn't installed on this machine (<code>firebase-tools</code>). Run <code>npm i -g firebase-tools</code>.</div>
          ` : ""}
        </form>
      `}
    </div>
  </section>`;

  const pipelineCard = `<section class="card mb-16"><div class="card-h"><h2>Pipeline</h2>${d.xcode_cloud ? `<span class="count">Xcode Cloud configured</span>` : ""}</div>
      ${pipeline.available ? `<div class="list">${pipeline.runs.map((r) => `<div class="item"><a class="main-col" href="${esc(r.url || "#")}" target="_blank" rel="noopener"><div class="title">${esc(r.title || r.name)}</div>
        <div class="meta"><span class="mono">${esc(r.branch)}</span> · ${esc(r.name)} · ${esc(when(r.created))}</div></a><div class="side"><span class="pill ${esc(r.tone)}">${esc(r.outcome.replace("_", " "))}</span>${r.can_rerun ? `<button type="button" class="btn small" data-rerun="${r.id}">Re-run failed</button>` : ""}</div></div>`).join("") || `<div class="empty">No pipeline runs yet.</div>`}</div>`
        : `<div class="empty">Pipeline status comes from GitHub Actions. Sign in with the GitHub CLI (<code>gh auth login</code>) and push this repository to GitHub to see it here.</div>`}</section>`;

  const buildsCard = testers.builds && testers.builds.length ? `<section class="card"><div class="card-h"><h2>Recent tester builds</h2><span class="count">${testers.builds.length}</span></div>
      <div class="list">${testers.builds.map((b) => `<div class="item"><div class="main-col"><div class="title">${esc(b.version || "?")}${b.build ? ` (${esc(b.build)})` : ""}${b.title ? ` · ${esc(b.title)}` : ""}</div>
        <div class="meta">${esc(ago(b.delivered))} · <span class="mono">${esc(b.branch)}</span>${b.recipients ? ` · to ${esc(b.recipients)}` : ""}</div></div>
        ${b.job_id ? `<div class="side"><a class="btn small" href="#/jobs/${encodeURIComponent(b.job_id)}">Job</a></div>` : ""}</div>`).join("")}</div></section>` : "";

  return {
    title: "App delivery",
    sub: "Launch test builds on devices and monitor pipelines.",
    html: `${branchCard}${releaseCard}${buildsCard}${pipelineCard}`,
    after: () => {
      const branchSelect = view.querySelector("#delivery-branch-select");
      if (branchSelect) {
        branchSelect.addEventListener("change", () => {
          location.hash = `#/delivery?branch=${encodeURIComponent(branchSelect.value)}`;
        });
      }

      const form = view.querySelector("#delivery-build-form");
      if (form) {
        const groupSelect = form.querySelector("#delivery-group-select");
        const radios = form.querySelectorAll('input[name="recipient_type"]');
        const updateGroupSelect = () => {
          const isGroup = form.querySelector('input[name="recipient_type"]:checked')?.value === "group";
          if (groupSelect && testers.groups.length) groupSelect.disabled = !isGroup;
        };
        radios.forEach((r) => r.addEventListener("change", updateGroupSelect));
        updateGroupSelect();

        const submitBtn = form.querySelector('[data-action="distribute"]');
        if (submitBtn) {
          submitBtn.addEventListener("click", (e) => {
            e.stopPropagation();
          }, true);
        }

        form.addEventListener("submit", (e) => {
          e.preventDefault();
          const fd = new FormData(form);
          const recipientType = fd.get("recipient_type");
          const notes = (fd.get("notes") || "").trim();
          const group = recipientType === "group" ? (fd.get("group") || "").trim() : "";
          const testersTarget = recipientType === "me" ? user_email : "";

          runAction("distribute", {
            branch: selected_branch,
            group,
            testers: testersTarget,
            notes,
          }, { skipConfirm: false });
        });
      }

      const newGroupBtn = view.querySelector("#delivery-new-group-btn");
      if (newGroupBtn) {
        newGroupBtn.addEventListener("click", async () => {
          const values = await formDialog("Create tester group", `
            <p>Creates a new tester group in Firebase App Distribution and saves it to your project settings.</p>
            <label class="field"><span>Group name</span><input type="text" name="name" placeholder="qa-team" required></label>
          `, "Create group");
          if (!values || !values.name?.trim()) return;
          const groupName = values.name.trim();
          try {
            const res = await api("delivery/group", { method: "POST", body: { name: groupName } });
            toast(`Created group "${groupName}"`);
            const groupSelect = view.querySelector("#delivery-group-select");
            if (groupSelect && Array.isArray(res.groups)) {
              groupSelect.innerHTML = res.groups.map((g) => `<option value="${esc(g)}" ${g === groupName ? "selected" : ""}>${esc(g)}</option>`).join("");
              groupSelect.disabled = false;
            }
            const groupRadio = form?.querySelector('input[name="recipient_type"][value="group"]');
            if (groupRadio) {
              groupRadio.checked = true;
              if (groupSelect) groupSelect.disabled = false;
            }
          } catch (err) {
            toast(err.message, true);
          }
        });
      }

      view.querySelectorAll("[data-setup-copy]").forEach((btn) => btn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(btn.dataset.setupCopy);
          toast("Copied invite link");
        } catch {
          toast("Couldn't copy", true);
        }
      }));

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

const ANALYTICS_BLURBS = {
  mixpanel: "Event tracking and product analytics. Events and KPI metrics are streamed directly to your Mixpanel project.",
  amplitude: "Behavioral analytics and digital optimization. Stream feature KPI events into your Amplitude workspace.",
  posthog: "Product analytics and feature metrics. Connects to PostHog Cloud or self-hosted instances."
};
function providerMcpTree(providerId, providerName, list) {
  const pkg = `${providerId}-mcp`;
  const items = list && list.length ? list : [
    { id: "google", org: "Google (Antigravity / Gemini)", installed: false, cmd: `agy mcp add ${providerId} -- npx -y ${pkg}` },
    { id: "anthropic", org: "Anthropic (Claude Code)", installed: false, cmd: `claude mcp add ${providerId} -- npx -y ${pkg}` },
    { id: "openai", org: "OpenAI (Codex)", installed: false, cmd: `codex mcp add ${providerId} -- npx -y ${pkg}` },
    { id: "opencode", org: "OpenCode", installed: false, cmd: `opencode mcp add ${providerId} -- npx -y ${pkg}` },
    { id: "ollama", org: "Ollama (Local models)", installed: false, cmd: `npx -y ${pkg}` }
  ];
  const count = items.filter((m) => m.installed).length;
  return `<details class="fold model-tree mt-12" ${count > 0 ? "open" : ""}>
    <summary class="model-tree-summary">
      <strong>${esc(providerName)} MCP by LLM organization</strong>
      <span class="count">${count} / ${items.length} installed</span>
    </summary>
    <div class="model-tree-content">
      <p class="muted text-sm mb-8">Model Context Protocol lets AI agents query funnels, retention, and events live during jobs.</p>
      <ul class="model-tree-branch">
        ${items.map((m, idx) => `
          <li class="model-tree-item ${idx === items.length - 1 ? 'last-node' : ''}">
            <div class="model-tree-row">
              <strong class="model-id">${esc(m.org)}</strong>
              ${m.installed ? pill("done", "Installed") : pill("", "Not configured")}
              ${m.detected_via ? `<span class="model-caps muted">${esc(m.detected_via)}</span>` : ""}
            </div>
            ${!m.installed && m.cmd ? `
              <div class="setup-hint mt-8">
                <code>${esc(m.cmd)}</code>
                <button type="button" class="btn small ghost" data-setup-copy="${esc(m.cmd)}" title="Copy command">Copy</button>
              </div>
            ` : ""}
          </li>
        `).join("")}
      </ul>
    </div>
  </details>`;
}

function analyticsConnectionCards(data, manageOnly = false) {
  return (data.providers || []).map((p) => {
    const isCurrent = data.provider === p.id;
    const isReady = isCurrent && data.key_set;
    const statusPill = isReady
      ? pill("done", `Connected (${(data.region || "us").toUpperCase()})`)
      : isCurrent && !data.key_set
      ? pill("failed", "Key missing")
      : pill("", "Not connected");
    const dashboardUrl = isReady
      ? (p.dashboards?.[data.region || "us"] || (p.dashboards?.us) || (isCurrent && data.dashboard_url ? data.dashboard_url : null))
      : null;
    const mcpList = data.provider_mcp?.[p.id] || (p.id === "mixpanel" ? data.mixpanel_mcp : null);
    const mcpTree = providerMcpTree(p.id, p.name, mcpList);
    return `<section class="card conn-card" data-analytics-p="${esc(p.id)}">
      <div class="card-b stack">
        <div class="row">
          <strong class="conn-name">${providerIcon(p.id)}${esc(p.name)}</strong>
          ${statusPill}
        </div>
        <div class="muted">${esc(ANALYTICS_BLURBS[p.id] || "Product analytics and event tracking.")}</div>
        <div class="muted text-sm">Key: ${esc(p.key_label)} · Regions: ${esc((p.regions || []).map((r) => r.toUpperCase()).join(", "))}</div>
        <p><a href="${esc(p.docs)}" target="_blank" rel="noopener">Official docs ↗</a>${dashboardUrl ? ` · <a href="${esc(dashboardUrl)}" target="_blank" rel="noopener">Open ${esc(p.name)} project ↗</a>` : ""}</p>
        <div class="row" ${manageOnly ? "" : "data-owner-only"}>
          ${manageOnly ? `<a href="#/measure?provider=${encodeURIComponent(p.id)}" class="btn small primary">${isReady ? "Manage" : "Connect"} ${esc(p.name)} ↗</a>` : isReady ? `
            ${dashboardUrl ? `<a href="${esc(dashboardUrl)}" target="_blank" rel="noopener" class="btn small">Open project ↗</a>` : ""}
            <button type="button" class="btn small" data-analytics-action="change" data-analytics-p="${esc(p.id)}">Update</button>
            <button type="button" class="btn small" data-analytics-action="test" data-analytics-p="${esc(p.id)}">Send test event</button>
            <button type="button" class="btn small danger" data-analytics-action="clear" data-analytics-p="${esc(p.id)}">Disconnect</button>
          ` : `
            <button type="button" class="btn small primary" data-analytics-action="connect" data-analytics-p="${esc(p.id)}">Connect ${esc(p.name)}</button>
          `}
        </div>
        ${mcpTree}
      </div>
    </section>`;
  }).join("");
}

pages.measure = async (_, query) => {
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
      <section class="card mb-16">
        <div class="card-h"><h2>Analytics Connections</h2></div>
        <div class="card-b stack">
          <p class="muted">Connect an analytics provider so new jobs stream their KPI events directly to your tracking dashboard. Connect one to verify ingestion with a test event.</p>
          <div class="conn-grid">
            ${analyticsConnectionCards(data)}
          </div>
        </div>
      </section>
      ${features.map(card).join("") || `<div class="empty">KPIs belong to features. Define features in <a href="#/product">product requirements</a> first.</div>`}`,
    after: () => {
      const selectedProvider = (data.providers || []).find((p) => p.id === query?.get("provider"));
      if (selectedProvider) view.querySelector(`[data-analytics-p="${selectedProvider.id}"]`)?.scrollIntoView({ block: "start" });
      const reload = () => route();
      const call = async (path, body, ok) => { try { await api(path, { method: "POST", body }); if (ok) toast(ok); reload(); } catch (e) { toast(e.message, true); } };
      view.querySelectorAll("[data-analytics-action]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const action = btn.dataset.analyticsAction;
          const pid = btn.dataset.analyticsP;
          const p = (data.providers || []).find((x) => x.id === pid) || data.providers[0];
          const isCurrent = data.provider === p.id;
          if (action === "test") {
            call("config/analytics", { op: "test" }, "Test event sent. Check the provider's live view.");
          } else if (action === "clear") {
            call("config/analytics", { op: "clear" }, "Disconnected");
          } else {
            const v = await formDialog(`Connect ${p.name}`, `
              <input type="hidden" name="provider" value="${esc(p.id)}">
              <label class="field"><span>${esc(p.key_label)}</span><input type="password" name="key" autocomplete="off" placeholder="${isCurrent && data.key_set ? "Saved. Leave blank to keep" : ""}"></label>
              <label class="field"><span>Region</span><select name="region"><option value="us" ${(!isCurrent || data.region !== "eu") ? "selected" : ""}>US</option><option value="eu" ${isCurrent && data.region === "eu" ? "selected" : ""}>EU</option></select></label>`, "Save");
            if (v) {
              try {
                await api("config/analytics", { method: "POST", body: { op: "set", ...v } });
                try { await api("config/analytics", { method: "POST", body: { op: "test" } }); toast("Saved and tested: a test event was sent. Check the provider's live view."); }
                catch (e) { toast(`Saved, but the test event failed: ${e.message}`, true); }
                reload();
              } catch (e) { toast(e.message, true); }
            }
          }
        });
      });
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

const UX_CHECKS = {
  "ux.status": {
    "name": "Visibility of system status",
    "criterion": "The person can always tell what is happening: loading, saving, running, failed, done. Nothing fails silently."
  },
  "ux.words": {
    "name": "Plain language",
    "criterion": "Labels use words a casual user knows. No internal ids, upper-case type codes, or jargon (\"execution\", \"decomposed\")."
  },
  "ux.consistent-names": {
    "name": "Consistent names",
    "criterion": "One name per thing and one thing per name, across every screen (Delete vs Discard vs Remove; Stop vs Pause)."
  },
  "ux.one-primary": {
    "name": "One primary action",
    "criterion": "At most one primary (filled) action per screen, and it is the thing most people come to do."
  },
  "ux.no-duplicate-actions": {
    "name": "No duplicate actions",
    "criterion": "The same action is not offered twice on one screen (a header button, a floating button and a menu item for the same thing)."
  },
  "ux.disclosure": {
    "name": "Progressive disclosure",
    "criterion": "Simple first, advanced one step away: common options visible, rare ones behind a fold or menu. Never more than two levels deep."
  },
  "ux.grouping": {
    "name": "Meaningful grouping",
    "criterion": "Things are grouped by what they mean to the person (the ticket, the designs, what went wrong), not by where they came from (an app, an upload). A section title joined with \"&\" or \"and\", or an \"Other\" bucket, is a warning sign of a junk drawer."
  },
  "ux.one-home": {
    "name": "One home per destination",
    "criterion": "Each destination has one home in the navigation; the same page is not reachable from three menus."
  },
  "ux.wayfinding": {
    "name": "Clear navigation",
    "criterion": "The navigation shows where you are: the right item is highlighted on every page, including pages below the top level."
  },
  "ux.back": {
    "name": "Visible Back controls",
    "criterion": "Every page below the top level (an item, a document, a setting, a form) has a visible Back control near its title that returns to where the person came from. The browser button or the sidebar alone is not enough."
  },
  "ux.mobile-nav": {
    "name": "Mobile navigation",
    "criterion": "On phones the main destinations are visible (a tab bar), not only behind a hamburger."
  },
  "ux.empty-states": {
    "name": "Helpful empty states",
    "criterion": "An empty screen says what goes here and offers one clear first action, once, not on every section."
  },
  "ux.errors": {
    "name": "Recoverable errors",
    "criterion": "Errors say what happened and what to do next, in plain words, with a way forward (retry, go back, fix link)."
  },
  "ux.decision-context": {
    "name": "Context for decisions",
    "criterion": "When the screen asks for a decision (approve a plan, merge, accept suggestions, delete), what is being decided is shown right next to the button, in enough detail to judge it. Never \"Approve\" with the thing to approve folded away elsewhere on the page."
  },
  "ux.menus": {
    "name": "Clear menus",
    "criterion": "Overflow menus are grouped under short headings, one line per item, and fit on screen without scrolling. They offer only what makes sense at this stage, never repeat the screen's primary action, and keep ending actions (archive, delete) last and apart."
  },
  "ux.no-terminal": {
    "name": "No terminal handoffs",
    "criterion": "Every choice is made in the app's own interface. No step hands the person a terminal, a text menu, or a \"press Enter\" prompt to finish something the app started."
  },
  "ux.destructive": {
    "name": "Safe destructive actions",
    "criterion": "Destructive actions are last in a menu, say what will be lost, name the action on the button, and offer undo where possible."
  },
  "ux.forms": {
    "name": "Simple forms",
    "criterion": "Forms ask only what is needed now; optional questions are folded; required fields are clear; choices that contradict each other cannot both be picked."
  },
  "ux.recognition": {
    "name": "Visible choices",
    "criterion": "Options are visible or one tap away; nothing depends on remembering a hidden shortcut or a value from another screen."
  },
  "ux.say-once": {
    "name": "No repeated information",
    "criterion": "Each fact appears once per screen (no status pill plus a sentence repeating it, no date shown twice)."
  },
  "ux.action-labels": {
    "name": "Clear action labels",
    "criterion": "Action labels are short verbs whose object is clear from where they sit (\"Edit\" in the Brief card's header, not \"Edit brief\"), and the same action has the same label everywhere."
  },
  "ux.long-content": {
    "name": "Readable long content",
    "criterion": "Long text on a busy screen shows a useful first part, then a clear \"Show more\" that expands it in place. No small inner scroll boxes, and no walls of text pushing the rest of the screen away."
  },
  "ux.counts": {
    "name": "Accurate counts",
    "criterion": "Counts use correct singular and plural words, and numbers agree between screens."
  },
  "design.hierarchy": {
    "name": "Visual hierarchy",
    "criterion": "Each screen has one clear focal point; headings, body and secondary text are visibly different levels."
  },
  "design.tokens": {
    "name": "Design tokens",
    "criterion": "Colours, spacing, type sizes, radii, shadows and layers come from the design system's tokens, not one-off values."
  },
  "design.spacing": {
    "name": "Consistent spacing",
    "criterion": "Spacing follows the scale; related things sit closer than unrelated things; cards and sections breathe evenly."
  },
  "design.alignment": {
    "name": "Alignment",
    "criterion": "Elements line up on a shared grid; buttons in a row have the same height; nothing is floating out of line."
  },
  "design.sizing": {
    "name": "Control and touch target sizes",
    "criterion": "Controls come in a small number of sizes; touch targets are at least 44 px on phones."
  },
  "design.consistency": {
    "name": "Component consistency",
    "criterion": "The same component looks and behaves the same everywhere (card headers, lists, pills, menus)."
  },
  "design.section-headers": {
    "name": "Distinct section headers",
    "criterion": "Card and section titles read as titles: a distinct header (larger, heavier, or on a tinted bar) separates them from the content under them."
  },
  "design.header-anatomy": {
    "name": "Consistent header layout",
    "criterion": "Headers follow one pattern: title on the left (a count may sit beside it), at most two actions on the right with the secondary one first, all the same size and on one line. Facts such as a file path sit in a caption under the header, not loose beside the title."
  },
  "design.fields": {
    "name": "Consistent form labels",
    "criterion": "Form labels sit in one consistent place (above the control). Paired controls (two pickers side by side) share size, font and spacing; nothing is cramped together."
  },
  "design.native-controls": {
    "name": "Styled controls",
    "criterion": "No unstyled browser-default controls (a raw file picker, the browser's disclosure triangle) beside styled ones; one disclosure icon and one button style throughout."
  },
  "design.overlap": {
    "name": "No overlapping content",
    "criterion": "Nothing covers content: floating buttons, banners and bars leave room; no element is clipped or cut off."
  },
  "design.responsive": {
    "name": "Responsive layouts",
    "criterion": "No horizontal scroll at 360 to 430 px; long text wraps or truncates with a way to see it all; tablets get a sensible layout."
  },
  "design.contrast": {
    "name": "Text and control contrast",
    "criterion": "Text and controls meet WCAG AA contrast in light and dark; colour is never the only signal."
  },
  "design.dark-mode": {
    "name": "Dark mode",
    "criterion": "Dark mode uses its own tokens; nothing turns invisible or stays bright."
  },
  "design.motion": {
    "name": "Purposeful motion",
    "criterion": "Motion is brief and purposeful, and respects reduced-motion settings."
  },
  "design.focus": {
    "name": "Keyboard focus",
    "criterion": "Keyboard focus is always visible; focus order follows the layout."
  }
};

function uxChecklistHtml(checklist) {
  if (!checklist.length) return `<div class="empty">No checks were reported.</div>`;
  const mark = { pass: ["done", "Pass"], partial: ["attention", "Partial"], fail: ["failed", "Fail"], "n/a": ["", "Not applicable"], pending: ["", "Not checked"] };
  return `<div class="list">${checklist.map((c) => {
    const definition = UX_CHECKS[c.id];
    const status = c.status === "n/a" && (!c.note || c.note === "not reported") ? "pending" : c.status;
    const [tone, label] = mark[status] || mark.pending;
    return `<div class="item ux-check"><span class="pill ${tone}">${label}</span>
      <div class="main-col"><div class="title">${esc(definition?.name || c.id)}</div>
        ${definition ? `<p class="muted text-sm">${esc(definition.criterion)}</p>` : ""}
        ${c.note && c.note !== "not reported" ? `<p class="ux-check-evidence">${esc(c.note)}</p>` : ""}</div></div>`;
  }).join("")}</div>`;
}

function uxScreensHtml(screens, base) {
  if (!screens?.length) return "";
  return `<div class="ux-screens">${screens.map((sc) => {
    const title = `${sc.name || sc.route}${sc.width ? ` · ${sc.width} px` : ""}${sc.dark ? " · dark" : " · light"}`;
    const endpoint = `${base}/${encodeURIComponent(sc.file)}`;
    return `<figure class="ux-screen"><button type="button" class="ux-screen-preview" data-preview-img="" data-preview-auth="${esc(endpoint)}" data-preview-title="${esc(title)}" aria-label="View screenshot: ${esc(title)}"><img alt="${esc(title)}" data-auth-src="${esc(endpoint)}"></button>
      <figcaption>${esc(title)}${sc.name ? ` · ${esc(sc.route)}` : ""}</figcaption></figure>`;
  }).join("")}</div>`;
}

const uxFixLink = (text, label = "Create a job to fix") =>
  `<a class="btn small" href="#/new?type=quick&summary=${encodeURIComponent("Fix UX and design findings")}&spec=${encodeURIComponent(text)}">${label}</a>`;

// The job page's card: the checklist run on this job's interface changes.
function uxReviewCard(s, ux) {
  if (!ux || (ux.skipped && !ux.error && !ux.screens && !ux.capture_status)) return "";
  const c = ux.counts || {};
  const rerun = `<button type="button" class="btn small ghost" ${act("ux_review_job", { job: s.id })}>Check again</button>`;
  if (ux.error) return `<section class="card mb-16" id="ux-section"><div class="card-h"><h2>UX and design check</h2>${rerun}</div>
    <div class="card-b stack"><p class="muted">It couldn't run: ${esc(ux.error)}</p>${uxScreensHtml(ux.screen_list, `ux-screens/job/${encodeURIComponent(s.id)}`)}
      <a class="btn small" href="#/ux-review">Open screenshots in UX review</a></div></section>`;
  const findings = ux.findings || [];
  const fixText = "Fix these UX and design findings:\n" + findings.filter((f) => f.severity >= 2).map((f) => `- [${UX_SEVERITY[f.severity]}] ${f.screen || "All screens"}: ${f.problem}${f.fix ? ` Fix: ${f.fix}` : ""}`).join("\n");
  return `<section class="card mb-16" id="ux-section">
    <div class="card-h"><h2>UX and design check</h2><span class="count">${ux.skipped ? `${ux.screens || 0} screenshots saved` : c.findings ? `${plural(c.findings, "finding")}${c.major ? ` · ${c.major} major` : ""}` : "Nothing found"}</span></div>
    <div class="card-b stack">
      ${ux.skipped ? `<p class="muted">${esc(ux.skipped)}</p>` : ""}
      ${ux.summary ? `<p>${clamped(ux.summary, 320)}</p>` : ""}
      <div class="muted">${ux.skipped ? "Screenshot evidence only; checks have not been evaluated." : `Checked ${plural((ux.files || []).length, "interface file")}${ux.screens ? ` and ${plural(ux.screens, "screen")}` : ", from the code only"}. A prompt to look, not a gate.`}</div>
      ${uxScreensHtml(ux.screen_list, `ux-screens/job/${encodeURIComponent(s.id)}`)}
      ${ux.capture_status === "missing" ? `<p class="notice">No screenshots were captured. Configure the app and retry from UX review.</p>` : ""}
      ${(ux.capture_limits || []).length ? `<p class="muted">${esc(ux.capture_limits.join(" "))}</p>` : ""}
      <a class="btn small" href="#/ux-review">Open screenshot history</a>
    </div>
    ${findings.length ? uxFindingsHtml(findings, { listId: "ux-findings", shown: 5 }) : ""}
    <details class="fold"><summary class="card-h"><h2>Checklist</h2></summary>${uxChecklistHtml(ux.checklist || [])}</details>
    <div class="card-b card-b-split row gap-8">${findings.some((f) => f.severity >= 2) ? uxFixLink(fixText) : ""}${rerun}</div>
  </section>`;
}

// Refresh live progress without replacing screen settings while they are being edited.
function updateUxReviewWhileEditing(result) {
  const form = $("#ux-settings");
  if (!form || (form.dataset.dirty !== "true" && !form.contains(document.activeElement))) return false;
  const holder = document.createElement("div");
  holder.innerHTML = result.html;
  const progress = holder.querySelector("#ux-current-progress");
  const current = $("#ux-current-progress");
  if (progress && current) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "Save your screen settings to refresh the results below.";
    progress.querySelector(".card-b")?.append(note);
    current.replaceWith(progress);
  }
  setHeader(result);
  return true;
}

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
  const captures = data.captures?.length ? data.captures : pass?.screens?.length ? [{kind: "pass", id: pass.id, title: "Whole-product review", screens: pass.screens, at: pass.at, limits: pass.limits ? [pass.limits] : []}] : [];
  const captureKey = (capture) => `${capture.kind}:${capture.id}:${capture.capture_id || ""}`;
  const selectedCapture = captures.find((capture) => captureKey(capture) === query.get("capture")) || captures[0];
  const captureBase = selectedCapture?.kind === "visual" ? `visual-checks/${encodeURIComponent(selectedCapture.id)}/screenshots` : selectedCapture ? `ux-screens/${selectedCapture.kind}/${encodeURIComponent(selectedCapture.id)}` : "";
  const captureLimits = selectedCapture?.limits ? Array.isArray(selectedCapture.limits) ? selectedCapture.limits : [selectedCapture.limits] : [];
  const runs = (state.runs || []).filter((r) => r.action === "ux_pass").sort((a, b) => b.started - a.started);
  const run = runs.find((r) => r.running) || runs[0];
  const latest = data.passes[0];
  const latestTime = latest?.completed_at ?? (/([zZ]|[+-]\d{2}:\d{2})$/.test(latest?.at || "") ? Date.parse(latest.at) / 1000 : NaN);
  const timingUnknown = Boolean(run && latest && !Number.isFinite(latestTime));
  const newerRun = run && (!latest || run.running || (Number.isFinite(latestTime) && run.started > latestTime));
  const previousResults = Boolean(pass && (id !== latest?.id || newerRun));
  const failed = newerRun && !run.running && run.exit_code !== 0;
  const running = Boolean(run?.running);
  const progress = run?.progress || {};
  const hasSteps = Number.isInteger(progress.step) && progress.total === 4 && progress.step >= 1 && progress.step <= 4;
  // A review can finish without having judged much: say so instead of calling it complete.
  const caveat = pass && !newerRun ? pass.coverage === "empty" ? "Nothing to review" : pass.coverage === "code_only" ? "Code only" : "" : "";
  const caveatText = caveat === "Nothing to review" ? "This project has no interface files or screens yet, so there was nothing to review. Add some UI, then run the review again."
    : caveat ? `No screenshots were captured, so only the interface code was reviewed. Layout, spacing and contrast were not judged.${cfg?.url ? " Check that the app is running at the address below." : ""}` : "";
  const status = running ? "Review running" : failed ? (run.stopped ? "Review stopped" : "Review failed") : latest && !newerRun ? (caveat ? caveat : timingUnknown ? "Saved results available" : "Review complete") : newerRun ? "Results unavailable" : "Not started";
  const areaCard = (area, title) => {
    const findings = (pass?.findings || []).filter((f) => f.area === area);
    const reported = new Map((pass?.checklist || []).map((c) => [c.id, c]));
    const checks = Object.keys(UX_CHECKS).filter((id) => id.startsWith(`${area}.`)).map((id) => reported.get(id) || { id, status: "pending", note: "" });
    const checked = checks.filter((c) => ["pass", "partial", "fail"].includes(c.status)).length;
    return `<section class="card mb-16"><div class="card-h"><h2>${title}</h2><span class="count">${checked} of ${checks.length} evaluated</span></div>
      <div class="card-b stack"><div><button type="button" class="btn primary" ${act("ux_pass")} ${running ? "disabled" : ""}>${running ? "Review running…" : `Run all ${checks.length} checks`}</button></div>
        <p class="muted">Runs a full review of usability and visual design. Results appear here when it finishes.</p></div>
      ${findings.length ? uxFindingsHtml(findings, { listId: `ux-${area}` }) : ""}
      ${uxChecklistHtml(checks)}</section>`;
  };
  return {
    title: "UX review",
    sub: "Review your app and choose what to improve.",
    actions: `<button class="btn primary" ${act("ux_pass")} ${running ? "disabled" : ""}>${running ? "Review running…" : "Run full review"}</button>`,
    html: `
      <section class="card mb-16"><div class="card-h"><h2>Improve your app</h2></div>
        <div class="card-b stack">
          <p>Find usability and design issues. Choose what to fix.</p>
          <div class="row"><strong>Reviewing:</strong><span>${noScreens ? "Code only" : "Configured screens and interface code"}</span></div>
          ${cfg?.url ? `<p class="muted">App address: ${esc(cfg.url)} · ${plural((cfg.routes || []).length, "page")} · ${(cfg.widths || []).map((w) => `${esc(w)}px`).join(", ")}${cfg.dark_mode ? " · light and dark mode" : ""}</p>` : ""}
        </div>
      </section>
      <section class="card mb-16" id="ux-current-progress" aria-labelledby="ux-progress-title"><div class="card-h"><h2 id="ux-progress-title">Current progress</h2><span class="pill ${failed ? "failed" : running ? "working" : caveat ? "attention" : pass && !newerRun ? "done" : ""}">${status}</span></div>
        <div class="card-b stack" role="status" aria-live="polite">
          ${running ? `<p>${hasSteps ? `Step ${progress.step} of 4: ` : ""}${esc(progress.label || "Waiting for the reviewer to report its stage")}</p>
            ${hasSteps ? `<progress class="ux-review-progress" value="${progress.step - 1}" max="4" aria-label="Completed review stages">${progress.step - 1} of 4</progress>` : ""}
            <p class="muted">Started ${esc(ago(run.started))}. Check results appear when the report is saved.</p>` : `<p>${failed ? "This attempt did not finish. Open the run details to see what stopped it, then try again." : pass && !newerRun ? "The saved report is shown below. Unreported checks remain marked Not checked." : newerRun ? "The run ended, but no saved report is available. Open its details before starting another review." : "Start a review to capture screens and evaluate the checks below."}</p>`}
          <ol class="ux-review-stages"><li>Capture screens or read interface code</li><li>Check usability</li><li>Check visual design</li><li>Save report</li></ol>
          ${newerRun && run.last_line ? `<p class="muted">Latest activity: ${esc(run.last_line)}</p>` : ""}
          ${run ? `<div><a class="btn small" href="#/runs/${encodeURIComponent(run.id)}">Open run details</a></div>` : ""}
        </div>
      </section>
      ${data.error ? `<div class="notice bad mb-16">Screens to review: ${esc(data.error)}</div>` : ""}
      ${noScreens ? `<div class="notice mb-16">Reviewing code only. To include visual layout and contrast checks, set your local dev server URL (e.g. http://localhost:3000) under <strong>Screens to review</strong> below.</div>` : ""}
      ${cfg?.url && !data.browser ? `<div class="notice bad mb-16">No Chrome, Chromium or Edge found on this computer, so screens can't be captured.</div>` : ""}
      <section class="card mb-16" id="ux-screenshot-gallery"><div class="card-h"><h2>UI screenshots</h2><span class="count">${selectedCapture?.screens?.length || 0} saved</span></div>
        <div class="card-b stack"><p class="muted">Review screenshots of new and changed screens. Choose a capture, then select an image to inspect it.</p>
          ${captures.length ? `<label class="field"><span>Capture history</span><select id="ux-capture-select">${captures.map((capture) => `<option value="${esc(captureKey(capture))}" ${capture === selectedCapture ? "selected" : ""}>${esc(capture.title || capture.id)} · ${esc(capture.at || "")}</option>`).join("")}</select></label>
            ${selectedCapture.kind === "job" ? `<a href="#/jobs/${encodeURIComponent(selectedCapture.id)}">Source job: ${esc(selectedCapture.title || selectedCapture.id)}</a>` : ""}
            ${selectedCapture.branch ? `<p class="muted">Job branch: ${esc(selectedCapture.branch)}</p>` : ""}
            ${uxScreensHtml(selectedCapture.screens, captureBase)}
            ${!selectedCapture.screens?.length ? `<div class="notice">This capture saved no screenshots. Check the setup and capture limits below, then retry.</div>` : ""}
            ${captureLimits.length ? `<p class="muted">Capture limits: ${esc(captureLimits.join(" "))}</p>` : ""}`
          : `<p>${pass && !newerRun ? "The last review captured no screenshots." : "No UI screenshots have been saved yet."}</p>${pass?.limits ? `<p class="muted">Capture limits: ${esc(pass.limits)}</p>` : ""}<a class="btn small" href="#/ux-review?setup=1">Set up screenshot capture</a>`}
        </div></section>
      ${pass ? `
        <section class="card mb-16"><div class="card-h"><h2>${previousResults ? "Previous results" : "Review results"}</h2><span class="count">${esc(ago(Date.parse(pass.at) / 1000))}</span></div>
          <div class="card-b stack">
            ${caveatText ? `<div class="notice">${esc(caveatText)}</div>` : ""}
            <div class="row gap-8"><span class="pill ${pass.counts.major ? "failed" : pass.counts.findings || caveat ? "attention" : "done"}">${caveat === "Nothing to review" ? "Nothing reviewed" : plural(pass.counts.findings, "finding")}</span>
              ${pass.counts.major ? `<span class="muted">${pass.counts.major} major or worse</span>` : ""}${caveat === "Nothing to review" ? "" : `<span class="muted">${pass.counts.passed} checklist items pass, ${pass.counts.failed} fail</span>`}</div>
            ${pass.summary ? `<p>${clamped(pass.summary, 500)}</p>` : ""}
            ${pass.limits ? `<p class="muted">${esc(pass.limits)}</p>` : ""}
            ${pass.findings?.length ? `<div>${uxFixLink(pass.fix_text, "Create a job to fix the main ones")}</div>` : ""}
          </div>
        </section>
        ` : ""}
      <section class="card mb-16" id="ux-setup"><details class="fold"${query.get("setup") || noScreens ? " open" : ""}><summary class="card-h"><h2>Screens to review</h2></summary>
        <form class="card-b stack" id="ux-settings">
          <label class="field"><span>Local dev server URL</span><input type="url" name="url" value="${esc(saved.url || "")}" placeholder="http://localhost:3000" spellcheck="false">
            <small class="hint-text">Where the app runs locally during development (e.g. http://localhost:3000). A sign-in token can come from an environment variable: <code class="nowrap">?token=$MY_TOKEN</code>.</small></label>
          <label class="field"><span>Pages to capture <span class="muted">(one per line)</span></span><textarea name="routes" rows="4" spellcheck="false">${esc((saved.routes || ["/"]).join("\n"))}</textarea></label>
          <label class="field"><span>Widths <span class="muted">(pixels)</span></span><input type="text" name="widths" value="${esc((saved.widths || [390, 1440]).join(" "))}"></label>
          <details class="np-more"><summary class="np-section-legend">Advanced</summary><div class="stack">
            <label class="field"><span>Command that starts the app <span class="muted">(optional)</span></span><input type="text" name="start_command" value="${esc(saved.start_command || "")}" placeholder="npm run dev" spellcheck="false">
              <small class="hint-text">Used only when the address isn't answering; stopped afterwards.</small></label>
            <label class="field"><span>Your UI conventions file <span class="muted">(optional)</span></span><input type="text" name="conventions" value="${esc(saved.conventions || "")}" placeholder="docs/ui-conventions.md" spellcheck="false">
              <small class="hint-text">Found automatically when it's named like docs/*ui*conventions*.md or DESIGN.md.</small></label>
            <label class="check"><input type="checkbox" name="dark_mode" ${saved.dark_mode !== false ? "checked" : ""}><span>Also capture dark mode</span></label>
            <label class="check"><input type="checkbox" name="simulator" ${saved.simulator ? "checked" : ""}><span>Also capture the iOS simulator</span></label>
            <label class="check"><input type="checkbox" name="review_changes" ${saved.review_changes !== false ? "checked" : ""}><span>Run AI review after UI jobs (screenshots are always captured when configured)</span></label>
          </div></details>
          <div><button class="btn" type="submit">Save</button></div>
        </form></details></section>
      ${areaCard("ux", "Usability heuristics")}
      ${areaCard("design", "Visual design checks")}
      ${data.passes.length > 1 ? `<section class="card"><div class="card-h"><h2>Earlier reviews</h2></div><div class="list">${data.passes.map((p) => `<a class="item" href="#/ux-review?id=${encodeURIComponent(p.id)}">
        <div class="main-col"><div class="title">${esc(ago(Date.parse(p.at) / 1000))}${p.id === id ? " · showing" : ""}</div><div class="meta">${plural(p.counts.findings, "finding")}, ${p.counts.major} major</div></div></a>`).join("")}</div></section>` : ""}`,
    after: () => {
      hydrateAuthImages();
      wireImagePreviews(view);
      $("#ux-capture-select")?.addEventListener("change", (event) => {
        const nextQuery = new URLSearchParams(query); nextQuery.set("capture", event.target.value);
        location.hash = `#/ux-review?${nextQuery}`;
      });
      const settingsForm = $("#ux-settings");
      const markDirty = () => { settingsForm.dataset.dirty = "true"; };
      settingsForm.addEventListener("input", markDirty);
      settingsForm.addEventListener("change", markDirty);
      settingsForm.addEventListener("submit", async (e) => {
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

const READINESS_CHECKS = [["check", "Tools and logins", "Command-line tools, sign-ins and keys (prerequisite audit)"],
  ["check_config", "Project config", "Validate .orchestrator/project.json"], ["worker_check", "Worker machines", "Remote build machines respond"],
  ["test", "Orchestrator health check", "Orchestrator's own self-tests, to confirm it works on this computer"]];

pages.checkup = async () => {
  const owner = state.you?.role !== "member";
  const responses = await Promise.allSettled([api("health"), api("setup"), api("preflight"), api("integrations"), api("analytics"), owner ? api("config") : Promise.resolve(null)]);
  const [health, setup, preflight, plugins, analytics, config] = responses.map((r) => r.status === "fulfilled" ? r.value : null);
  if (setup) setupState = setup; // existing setup buttons use the current checklist
  const d = health || {name: state.project?.name || "Project", stage: "Product status unavailable", items: []};
  const product = d.items.map((i) => i.id === "prd" && i.status === "todo" ? {...i,
    detail: "Tell the AI what you're building. Write a short description, generate a draft, or import a PRD.", label: "Define your product"}
    : i.id === "release" && i.status === "todo" ? {...i, hint: "", release: true, detail: "Mark a version as released so you can track changes since it shipped.", label: "Create release tag"} : i);
  const missing = (id, title, route) => ({id, title, status: "unknown", detail: "Status unavailable. Open settings to check.", route, label: "Check setup"});
  const fromSetup = (i) => ({...i, status: i.done ? "ok" : "todo", setup: true});
  const coreIds = ["project", "git", "llm", "machines"];
  const coreNames = ["Project configuration", "Git repository", "An AI model is ready", "An enabled machine"];
  const coreRoutes = ["#/setup", "#/git", "#/config/ai", "#/config/fleet"];
  const required = coreIds.map((id, n) => {
    const item = setup?.items.find((i) => i.id === id);
    return item ? fromSetup(item) : missing(id, coreNames[n], coreRoutes[n]);
  });
  // Older servers count disabled machines; use their public machine inventory when available.
  if (config?.machines) {
    const enabled = config.machines.filter((m) => m.enabled !== false);
    const machine = required.find((i) => i.id === "machines"), ai = required.find((i) => i.id === "llm");
    machine.status = enabled.length ? "ok" : "todo"; machine.done = Boolean(enabled.length);
    machine.detail = enabled.length ? `${enabled.length} enabled. Worker connectivity is checked before a job runs.` : "No enabled machines. Add or enable a machine.";
    if (ai.done && !enabled.some((m) => m.models > 0)) { ai.status = "todo"; ai.done = false; ai.detail = "No model is assigned to a machine"; ai.route = "#/config/models"; }
  }
  const environmentChecks = (preflight?.items || []).map((i) => ({...i,
    status: i.status === "fail" ? "todo" : i.status, route: i.route, label: i.route ? i.fix || "Review setup" : "Review setup"}));
  const workflows = (setup?.items || []).filter((i) => i.required && !coreIds.includes(i.id)).map(fromSetup);
  const signing = setup?.workflow_checks || [missing("signing", "Signing and delivery prerequisites", "#/delivery")];
  workflows.push(...signing.filter((i) => i.selected === true));
  const delivery = (setup?.items || []).find((i) => i.id === "firebase");
  if (delivery?.done) workflows.push({...fromSetup(delivery), setup: false, status: config?.firebase ? config.firebase.app_id && config.firebase.cli_installed ? "ok" : "todo" : "unknown",
    detail: "Firebase distribution is selected. Requires an App ID and Firebase CLI; account access is verified when sending a build.", route: "#/config/firebase", label: "Review Firebase delivery"});
  const optional = product.filter((i) => i.id !== "repo" && i.id !== "delivery");
  optional.push(...signing.filter((i) => i.selected !== true));
  if (delivery && !delivery.done) optional.push(fromSetup(delivery));
  optional.push(...(setup?.items || []).filter((i) => !i.required && !["connections", "firebase", "email", "docs"].includes(i.id)).map(fromSetup));
  if (!health) optional.push(missing("product", "Product requirements and milestones", "#/product"));
  if (plugins) optional.push(...plugins.integrations.map((p) => ({id: `plugin-${p.id}`, title: p.name,
    status: p.connected && !p.rejected && !p.expiry?.expired ? "ok" : "todo",
    detail: p.rejected || p.expiry?.expired ? "Reconnect: the saved token was rejected or expired." : p.connected ? "Connected. Credentials are checked when used." : p.blurb || "Connect to use tickets, designs, or error reports.",
    route: "#/connections?section=plugins", label: p.rejected || p.expiry?.expired ? "Reconnect" : "Manage plugin"})));
  else optional.push(missing("plugins", "Plugins", "#/connections?section=plugins"));
  const email = config?.email;
  const emailReady = email && email.recipients?.length && (email.provider === "resend" ? email.resend_api_key_set && email.resend_from_email : email.smtp_email && email.smtp_password_set);
  optional.push(config?.webhook ? {id: "slack", title: "Slack alerts", status: config.webhook.set ? "ok" : "todo", detail: "Receive job updates in Slack.", route: "#/connections?section=alerts", label: "Manage alerts"} : missing("slack", "Slack alerts", "#/connections?section=alerts"));
  optional.push(email ? {id: "email", title: "Email alerts", status: emailReady ? "ok" : "todo", detail: "Requires recipients and a configured sender.", route: "#/config/email", label: "Manage email"} : missing("email", "Email alerts", "#/config/email"));
  if (analytics) {
    optional.push(...(analytics.providers || []).map((p) => ({id: `analytics-${p.id}`, title: `${p.name} analytics`, status: analytics.provider === p.id && analytics.key_set ? "ok" : "todo", detail: "Optional event tracking. Send a test event to verify access.", route: `#/measure?provider=${encodeURIComponent(p.id)}`, label: "Manage analytics"})));
    const mcps = Object.entries(analytics.provider_mcp || {}).flatMap(([provider, entries]) => entries.map((m) => ({provider, ...m})));
    optional.push({id: "mcp", title: "Analytics MCP servers", status: mcps.length ? mcps.some((m) => m.installed) ? "ok" : "todo" : "unknown",
      detail: mcps.length ? `${mcps.filter((m) => m.installed).length} of ${mcps.length} provider / AI organization configurations detected. Installation detection does not verify server access.` : "Installation status unavailable.", route: "#/connections?section=analytics", label: "Review MCP installations"});
  } else optional.push(missing("analytics", "Analytics and analytics MCP servers", "#/connections?section=analytics"));
  optional.push({id: "mobile", title: "Mobile portals", status: "unknown", detail: "Optional access from your phone. Installation is not detected here.", route: "#/connections?section=mobile", label: "View mobile portals"},
    {id: "recommended-mcp", title: "Recommended MCP servers", status: "unknown", detail: "Optional tools for design, planning, and development. Review and connect the servers you need.", route: "#/connections?section=mcp", label: "View recommended servers"});
  const action = (i, primary = false) => {
    if (i.setup) return setupItem(i);
    const kind = primary ? "btn small primary" : "btn small";
    if (i.release) return owner ? `<button type="button" class="${kind}" data-create-release data-owner-only>Create release tag</button>` : `<span class="muted">Managed by the project owner</span>`;
    if (i.job) return `<a class="${kind}" href="#/new?type=${encodeURIComponent(i.job.type)}&summary=${encodeURIComponent(i.job.summary)}">${esc(i.label)}</a>`;
    if (i.route) return `<a class="${kind}" href="${esc(i.route)}">${esc(i.label || "Open")}</a>`;
    if (i.hint) return `<div class="setup-hint"><code>${esc(i.hint)}</code><button class="btn small ghost" data-setup-copy="${esc(i.hint)}">Copy</button></div>`;
    return "";
  };
  const count = (items) => `${items.filter((i) => i.status === "ok").length} of ${items.length} configured`;
  const group = (title, items, description) => `<section class="card mb-16"><div class="card-h"><h2>${title}</h2><span class="count">${count(items)}</span></div><div class="card-b muted card-note">${description}</div>
    <div class="list onboarding-list">${items.map((i) => i.setup ? setupItem(i) : `<div class="item"><span class="pill ${i.status === "ok" ? "done" : i.status === "todo" || i.status === "warn" ? "attention" : ""}">${i.status === "ok" ? "Configured" : i.status === "unknown" ? "Status unavailable" : i.status === "warn" ? "Needs attention" : "Needs setup"}</span><div class="main-col"><div class="title">${esc(i.title)}</div><div class="meta">${esc(i.detail)}</div></div><div class="side">${action(i)}</div></div>`).join("") || `<div class="empty">No additional prerequisites reported for this project.</div>`}</div></section>`;
  const environmentRow = (i) => {
    const icon = {ok: `<span class="setup-icon done" aria-label="fine">✓</span>`, warn: `<span class="pill attention">Maybe</span>`, fail: `<span class="pill failed">Blocks</span>`, todo: `<span class="pill failed">Blocks</span>`};
    return `<div class="item">${icon[i.status] || `<span class="pill">Status unavailable</span>`}<div class="main-col"><div class="title">${esc(i.title)}</div><div class="meta">${codeSpans(i.detail)}${i.fix && !i.route ? ` · ${codeSpans(i.fix)}` : ""}</div></div>${i.route && i.status !== "ok" ? `<div class="side"><a class="btn small" href="${esc(i.route)}">${esc(i.label || "Review setup")}</a></div>` : ""}</div>`;
  };
  const environmentHtml = (items, available = Boolean(preflight)) => items.length ? items.map(environmentRow).join("") : `<div class="empty">${available ? "No build or machine checks are needed for this project." : "Environment checks are unavailable. Try again."}</div>`;
  const next = required.find((i) => i.status !== "ok") || workflows.find((i) => i.status !== "ok") || environmentChecks.find((i) => i.status !== "ok" && i.route);
  return {
    title: "Onboarding checklist",
    sub: `${esc(d.name)} · ${esc(d.stage)}`,
    html: `<p class="muted mb-16">Set up the essentials, then the workflows and connections you need. Optional items do not affect required setup counts. Configured does not guarantee access; job checks verify tools and workers when used.</p>
      ${next ? `<section class="card mb-16 card-next"><div class="card-h"><h2>Next: ${esc(next.title)}</h2></div><div class="card-b stack">${next.setup ? action(next, true) : `<p>${esc(next.detail)}</p><div>${action(next, true)}</div>`}</div></section>` : ""}
      ${group("Required to run jobs", required, "Project, Git, an AI model, and an enabled machine.")}
      ${group("Required for selected workflows", workflows, "Applies when using GitHub, building or testing this app, or distributing builds.")}
      <section class="card mb-16"><div class="card-h"><h2>Tools and environment</h2></div><div class="card-b muted card-note">Build tools on this computer, remote worker access, sign-ins, and project checks.</div>
        <div class="card-h"><h3>Will this computer build it?</h3><button type="button" class="btn small" id="preflight-refresh">Check again</button></div>
        <div class="list" id="preflight-list">${environmentHtml(environmentChecks)}</div>
        <div class="card-h"><h3>Run a check</h3></div><div class="list">${READINESS_CHECKS.filter(([a]) => ConfigurationPages.canRunAction(a, state)).map(([a, title, hint]) =>
          `<div class="item"><div class="main-col"><div class="title">${esc(title)}</div><div class="meta">${esc(hint)}</div></div><div class="side"><button type="button" class="btn small" ${act(a)}>Run</button></div></div>`).join("")}</div></section>
      ${group("Optional", optional, 'Product guidance, milestones, and connections. Add what helps your work. Unavailable or undetected statuses are shown explicitly. <a href="#/connections">Manage connections</a>.')}
      <section class="card mt-16"><div class="card-h"><h2>UX and design</h2><a class="btn small" href="#/ux-review">Open UX review</a></div><div class="card-b muted" id="ux-checkup">Checking…</div></section>`,
    after: () => {
      const paintPreflight = async (refresh) => {
        const box = $("#preflight-list");
        try {
          const result = refresh ? await api("preflight?refresh=1") : preflight;
          box.innerHTML = environmentHtml((result?.items || []).map((i) => ({...i, status: i.status === "fail" ? "todo" : i.status, route: i.route, label: i.route ? i.fix || "Review setup" : "Review setup"})), Boolean(result));
        } catch (error) { box.innerHTML = `<div class="empty">${esc(error.message)}</div>`; }
      };
      $("#preflight-refresh")?.addEventListener("click", () => paintPreflight(true));
      api("ux-pass").then(({passes, settings}) => {
        const last = passes[0];
        $("#ux-checkup").textContent = last ? `Last pass ${ago(Date.parse(last.at) / 1000)}: ${plural(last.counts.findings, "finding")}, ${last.counts.major} major or worse.` : settings && (settings.url || settings.simulator) ? "No pass yet. Run one from UX review." : "No pass yet, and no screens set up. UX review shows how.";
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
        <li><strong>You ship it.</strong> Merge (or Mark complete), then send a build to testers from <a href="#/delivery">App delivery</a>.</li>
        <li><strong>You learn from it.</strong> Give each feature KPIs on <a href="#/measure">Measure</a> and log what you find.</li>
      </ol></div></section>
    <section class="card mb-16"><div class="card-h"><h2>Where things are</h2></div><div class="list">
      ${[["Home", "#/", "What's waiting on you (across projects), then all jobs."], ["Product", "#/product", "The one-page requirements every job reads, and the epics it breaks down into. Edit it, import a PRD, see its history, link epics to Jira."],
         ["Tests", "#/tests", "Test cases by area, suites, coverage."], ["App delivery", "#/delivery", "What's live, what testers have, what's ready, pipeline."], ["Measure", "#/measure", "KPIs, analytics connection, learning log."],
         ["Onboarding checklist", "#/checkup", "What the project needs, plus local tools, environment checks, and workflows."], ["Projects", "#/projects", "Switch, add or start a project."], ["Activity", "#/activity", "Commands and runs, with live output."],
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
      <details><summary>A job won't run</summary><p>Jobs need a Git repository, a machine, a model and a ready AI provider. GitHub workflows also need GitHub sign-in. The <a href="#/checkup">Onboarding checklist</a> shows what's missing and where to fix it.</p></details>
      <details><summary>How do I get better plans?</summary><p>Fill in the <a href="#/product">product requirements</a>: what you're building, who it's for, the core features, how it should look and feel, and what it should not be. Every plan is written against it. All of it is optional, and “not sure yet” is a fine answer. Draft it from your project, or import a PRD you already have. It keeps itself up to date as jobs finish, and you can see and undo every change.</p></details>
      <details><summary>I changed my mind about a job</summary><p>Use <strong>Revise plan</strong> in the job's More menu to re-plan, or <strong>Discard job</strong> to revert its changes and delete its branch. Discard can't be undone.</p></details>
      <details><summary>I deleted something by mistake</summary><p>Deleting a feature or KPI shows an Undo for 10 seconds. A job marked complete can be restored from the <a href="#/?filter=archived">Archived</a> filter on Home.</p></details>
      <details><summary>How do I get alerts when I'm away?</summary><p>The sidebar's <strong>Notify me when done</strong> sends alerts to this device. Turned on in the hosted app, they arrive even with Orchestrator closed, and you're told if your computer goes offline mid-run (on iPhone, add it to your Home Screen first). For Slack, add a webhook under <a href="#/connections?card=chat">Connections</a>.</p></details>
      <details><summary>Where are the full guides?</summary><p><a href="#/docs">Docs</a> has the project's own files and Orchestrator's guides. The terminal console has everything too: use <strong>Open full console</strong>.</p></details>
    </div></section>
    <section class="card"><div class="card-h"><h2>Missing a feature or have feedback?</h2></div><div class="card-b">
      <p class="muted mb-12">Tell us what felt off, request a missing feature, or share ideas to make Orchestrator better. We read every message.</p>
      <div class="row gap-8">
        <button type="button" class="btn small primary" id="help-feature-btn">Request a feature or share feedback</button>
        <button type="button" class="btn small" id="help-feedback-btn">Send feedback</button>
      </div>
    </div></section>
    <section class="card prize-card"><div class="card-h"><h2>Contributor prize 🏆</h2></div><div class="card-b">
      <div class="prize-card-badge mb-8"><span class="prize-badge">🏆 Prize Challenge</span></div>
      <p class="mb-12">Build an improvement for Orchestrator. Selected contributions win a prize and core inclusion.</p>
      <div class="row gap-8">
        <button type="button" class="btn small primary" id="help-create-improve-job-btn">Improve Orchestrator</button>
        <button type="button" class="btn small" id="help-submit-improvement-btn">Submit improvement 🏆</button>
      </div>
    </div></section>`,
  after: () => {
    $("#help-feedback-btn")?.addEventListener("click", () => {
      Feedback.open({ initialStep: "feedback", userName: state.you?.email || "" });
    });
    $("#help-feature-btn")?.addEventListener("click", () => {
      Feedback.open({ initialStep: "feedback", category: "feature_request", userName: state.you?.email || "" });
    });
    $("#help-create-improve-job-btn")?.addEventListener("click", () => {
      location.hash = "#/auto-improve";
    });
    $("#help-submit-improvement-btn")?.addEventListener("click", () => {
      Feedback.open({ initialStep: "improve_orchestrator", category: "improve_orchestrator", userName: state.you?.email || "" });
    });
  }
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
  if (kind === "product") {
    const p = await api("product");
    const filename = p.path.split("/").pop() || "prd.md";
    return {
      title: "PRD Markdown",
      sub: `<span class="mono">${esc(p.path)}</span>`,
      actions: "",
      html: `<section class="card"><div class="card-h"><h2>${esc(filename)}</h2><div class="card-actions"><button type="button" class="btn small" id="prd-copy-path" title="Copy file path">Copy path</button><button type="button" class="btn small" id="prd-download">Download</button></div></div><div class="card-b"><div class="md">${Markdown.render(p.text)}</div></div></section>`,
      after: () => {
        $("#prd-download").addEventListener("click", () => downloadText(filename, p.text));
        $("#prd-copy-path")?.addEventListener("click", () => {
          navigator.clipboard?.writeText(p.path).then(() => toast("Copied path")).catch(() => toast(p.path, "info"));
        });
      },
    };
  }
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
        <div class="list" id="docs-jobs">${d.jobs.length ? d.jobs.map(jobRow).join("") : `<div class="empty">No jobs yet. <a href="#/new">Start a new job</a>.</div>`}</div>
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
  migration: "Built from the earlier documents", earlier: "Before changes were tracked", answers: "Answered a breakdown question" };
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
async function waitForTask(started, opts) {
  const { task } = started;
  const began = Date.now();
  let misses = 0;
  for (;;) {
    if (opts?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    await new Promise((r) => setTimeout(r, 1500));
    if (opts?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    let r;
    try { r = await api(`product/task/${encodeURIComponent(task)}`, { signal: opts?.signal }); misses = 0; }
    catch (e) {
      if (opts?.signal?.aborted || e.name === "AbortError") throw e;
      if (e.status === 404 || ++misses >= 4) throw e;
      continue;
    } // a dropped connection is retried a few times
    if (r.status === "done") return r.result;
    if (r.status === "canceled" || r.status === "cancelled") throw new DOMException("Aborted", "AbortError");
    if (r.status === "error") throw new Error(r.error);
    if (Date.now() - began > 7 * 60 * 1000) throw new Error("This is taking much longer than expected. Try again.");
  }
}

function productStripHtml(p) {
  const lead = p.sections.find((x) => x.id === "pitch" && x.filled) || p.sections.find((x) => x.filled);
  const models = p.available_models || [];
  const currentModel = p.model || "claude-sonnet-4-6";
  const modelPicker = p.can_draft && models.length ? `
    <div class="prd-model-picker">
      <span class="muted">Model:</span>
      <select id="prd-home-model-select" class="prd-model-select" aria-label="AI model for draft">
        ${models.map((m) => `<option value="${esc(m.id)}"${m.id === currentModel ? " selected" : ""}>${esc(m.label || m.id)}</option>`).join("")}
        <hr>
        <option value="__manage_llms__">⚙️ Manage LLMs</option>
      </select>
    </div>` : "";

  // The heading and the pitch open the product requirements. Its buttons stay secondary: New job is Home's one primary.
  return `<section class="card mb-16" id="product-strip"><div class="card-h"><h2><a class="card-title-link" href="#/product">Product</a></h2></div>
    <div class="card-b stack">
      ${lead ? `<a class="product-pitch" href="#/product" title="Open the product requirements">${esc(prdSnippet(lead.body))}</a>`
        : `<p>Tell us what you're building, in a few sentences. Every job reads this first, and it stays up to date as you build.</p>
           <div class="row gap-10 align-center wrap">${p.can_draft ? `<a class="btn small" href="#/product?draft=1">Draft with AI</a>` : ""}<a class="btn small" href="#/product">Write it</a><a class="btn small" href="#/product?import=1">Import PRD</a>${modelPicker}</div>`}
    </div></section>`;
}

pages.product = async (_, query) => {
  const [p0, integrationsData0, epicsData] = await Promise.all([
    api("product"),
    api("integrations").catch(() => ({ integrations: [] })),
    api("epics").catch(() => null),
  ]);
  let p = p0, integrationsData = integrationsData0;
  let connectedApps = (integrationsData?.integrations || []).filter((i) => i.connected);
  let reloadPage = async () => {};
  const epics = epicsData ? epicsSection(epicsData, { onPrdChanged: () => reloadPage() }) : null;
  const productEpics = () => epics?.data().epics || [];
  const start = query?.get("start") === "1" || query?.get("suggest") === "1"; // the wizard's and the old links to breaking it down
  const open = { history: query?.get("history") || "", imp: query?.get("import") === "1", draft: query?.get("draft") === "1",
    section: query?.get("section") || (start ? "epics" : "") };
  const sectionCard = (s) => {
    const designs = s.id === "look" ? [...s.body.matchAll(/\]\((designs\/[^)\s]+)\)/g)].map((m) => m[1]).filter((d) => DESIGN_IMAGE.test(d)) : [];
    return `<section class="card mb-16" id="sec-${s.id}"><div class="card-h"><h2>${esc(s.title)}</h2>
        <button type="button" class="btn small" data-prd-edit="${s.id}">Edit</button></div>
      <div class="card-b stack" data-prd-body="${s.id}">
        <div class="muted">${esc(s.hint)}</div>
        ${s.filled ? `<div class="md" data-expandable="280">${Markdown.render(s.body)}</div>` : ""}
        ${designs.length ? `<div class="prd-designs">${designs.map((d) => `<figure class="prd-design"><img alt="${esc(d.replace("designs/", ""))}" data-auth-src="product/design/${esc(d.replace("designs/", ""))}"></figure>`).join("")}</div>` : ""}
      </div>
      ${s.id === "look" ? `<div class="reference-imports"><div class="label">Import references</div><div class="import-options">
        <button type="button" class="prd-drop file-picker reference-file-picker" id="prd-add-file">${filePickerContent("Add design references", "Designs, sketches, images or PDFs")}</button>
        <div class="import-option import-option-link" id="prd-link-section">
          <strong>Paste a link</strong>
          <span>A design or site to use as a reference</span>
          <form class="import-link-form row gap-8 mt-4" id="prd-link-form">
            <input type="url" id="prd-link-input" placeholder="https://" required aria-label="Reference link">
            <button type="submit" class="btn small primary" id="prd-link-submit">Add</button>
          </form>
        </div>
        <div class="import-option import-option-apps" id="prd-apps-section">
          <strong>Connected apps</strong>
          <span>Import live designs, UI frames, and tickets from Figma, Jira, Trello, and more</span>
          <div class="connected-apps-list">${connectedApps.length ? connectedApps.map((a) => `<span class="chip">${providerIcon(a.id)}${esc(a.name)}</span>`).join("") : `<span class="muted text-sm">None connected</span>`}</div>
          <div class="connected-apps-actions row gap-8 mt-4">
            ${connectedApps.length ? `<button type="button" class="btn small primary" id="prd-add-app">Choose reference</button>` : ""}
            <a href="#/connections" class="btn small ${connectedApps.length ? "ghost" : "primary"}">${connectedApps.length ? "Connect more" : "Connect an app"}</a>
          </div>
        </div>
        <input type="file" id="prd-file" accept="image/*,.pdf,.fig,.html,.htm" multiple hidden>
      </div></div>` : ""}</section>`;
  };
  const historyRows = (h) => h.map((v, i) => `<div class="item prd-version" data-version="${esc(v.id)}"><div class="main-col"><div class="title">${esc(PRD_SOURCE[v.source] || v.source)}${i === 0 ? ` <span class="pill done">Current</span>` : ""}</div>
      <div class="meta">${esc(ago(v.at))}${v.summary ? ` · ${esc(v.summary)}` : ""}</div><div class="prd-diff" hidden></div></div>
      <div class="side"><button type="button" class="btn small ghost" data-prd-diff="${esc(v.id)}">See changes</button>${i === 0 ? "" : `<button type="button" class="btn small" data-prd-restore="${esc(v.id)}">Restore</button>`}</div></div>`).join("");
  const prdFilled = () => (p.sections || []).some((x) => x.filled);
  const render = () => `
      <section class="card mb-16"><div class="card-h"><h2>Product requirements</h2></div>
        <div class="card-b stack">
          <div class="row gap-10 wrap">${p.can_draft ? `<button class="btn primary" id="prd-draft">Draft with AI</button>` : ""}<button class="btn primary" id="prd-import">Import PRD</button></div>
          <div><a class="mono prd-file-link" href="#/docs/product" title="View and download the PRD Markdown file">${esc(p.path)}</a> · Every job reads this first.</div>
        </div></section>
      <div id="prd-panel"></div>
      ${p.sections.map(sectionCard).join("")}
      ${p.sources?.length ? `<section class="card mb-16" id="prd-sources"><div class="card-h"><h2>Source documents</h2><span class="count">${p.sources.length}</span></div>
        <div class="card-b stack"><div class="muted">The fields above are short summaries. These are the full documents you imported; jobs read them when they need the detail.</div>
        <div class="list">${p.sources.map((d) => `<div class="item"><div class="main-col"><div class="title">${esc(d.label)}</div><div class="meta mono">docs/product/${esc(d.target)}</div></div>
          <div class="side"><button type="button" class="btn small ghost" data-prd-source="${esc(d.target.replace("source/", ""))}">Download</button></div></div>`).join("")}</div></div></section>` : ""}
      <section class="card mb-16"><div class="card-h"><h2>Keeping it up to date</h2></div><div class="card-b stack">
        <label class="check"><input type="checkbox" id="prd-auto" ${p.auto_update ? "checked" : ""}> <span>Update this automatically when jobs finish</span></label>
        <div class="row gap-10 items-center wrap">
          <span class="muted text-sm">Model:</span>
          <select id="prd-update-model-select" class="prd-model-select" aria-label="AI model for product requirements">
            ${(p.available_models || [
              { id: "claude-sonnet-4-6", label: "Claude Sonnet 4.6 (Recommended)" },
              { id: "claude-opus-4-8", label: "Claude Opus 4.8" },
              { id: "gemini-3.1-pro-preview", label: "Gemini 3.1 Pro" },
              { id: "gemini-3-flash-preview", label: "Gemini 3 Flash" },
              { id: "gpt-4o", label: "GPT-4o" },
              { id: "o3-mini", label: "o3-mini" },
            ]).map((m) => `<option value="${esc(m.id)}"${m.id === (p.model || "claude-sonnet-4-6") ? " selected" : ""}>${esc(m.label || m.id)}</option>`).join("")}
            <hr>
            <option value="__manage_llms__">⚙️ Manage LLMs</option>
          </select>
        </div>
        <div class="muted">AI keeps this up to date as jobs finish, and you can review or undo any change in History.</div></div></section>
      <section class="card"><div class="card-h"><h2>History</h2><span class="count">${p.history.length}</span></div>
        <div class="list">${p.history.length ? historyRows(p.history) : `<div class="empty">Nothing yet. Every change you or the AI makes will be listed here.</div>`}</div></section>
      ${epics ? epics.html() : ""}`;
  return {
    title: "Product",
    sub: "",
    actions: "",
    html: render(),
    after: () => {
      const panel = () => $("#prd-panel");
      const reload = async () => {
        let epicsData;
        [p, integrationsData, epicsData] = await Promise.all([api("product"), api("integrations").catch(() => ({ integrations: [] })), api("epics").catch(() => null)]);
        if (epicsData) epics?.set(epicsData);
        connectedApps = (integrationsData?.integrations || []).filter((i) => i.connected);
        $("#view").innerHTML = render(); wire(); hydrateAuthImages(); wireExpandables(view);
        offerBreakdown();
      };
      reloadPage = reload;
      const save = async (body) => { p = await api("product", { method: "POST", body }); };

      const showSectionEditor = (id, box, currentBody, hint, title, onSave, onCancel) => {
        if (id === "features") {
          const rawLines = (currentBody || "").split("\n").map((l) => l.replace(/^\s*[-*•\d+.]\s*/, "").trim()).filter(Boolean);
          const initialFeatures = rawLines.length ? rawLines : [""];
          box.innerHTML = `<div class="muted">${esc(hint)}</div>
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
          box.querySelector("#prd-cancel-features").addEventListener("click", onCancel);
          box.querySelector("#prd-save-features").addEventListener("click", async () => {
            const items = Array.from(box.querySelectorAll(".prd-feature-input")).map((i) => i.value.trim()).filter(Boolean);
            const body = items.length ? items.map((f) => `- ${f}`).join("\n") : "";
            await onSave(body);
          });
          const firstEmpty = Array.from(box.querySelectorAll(".prd-feature-input")).find((i) => !i.value) || box.querySelector(".prd-feature-input");
          firstEmpty?.focus();
          return;
        }

        box.innerHTML = `<div class="muted">${esc(hint)}</div><label class="field"><span class="sr-only">${esc(title)}</span><textarea rows="${id === "pitch" ? 5 : 8}" id="prd-text-${id}" spellcheck="true" aria-label="Edit ${esc(title)}">${esc(currentBody)}</textarea></label>
          <div class="row gap-10"><button type="button" class="btn primary" id="prd-save-${id}">Save</button><button type="button" class="btn ghost" id="prd-cancel-${id}">Cancel</button></div>`;
        $(`#prd-text-${id}`).focus();
        $(`#prd-cancel-${id}`).addEventListener("click", onCancel);
        $(`#prd-save-${id}`).addEventListener("click", async () => {
          await onSave($(`#prd-text-${id}`).value);
        });
      };

      const wire = () => {
        $("#prd-import")?.addEventListener("click", () => importPanel());
        view.querySelectorAll("[data-prd-source]").forEach((btn) => btn.addEventListener("click", async () => {
          const file = btn.dataset.prdSource, token = getToken(), backend = getBackendUrl();
          btn.disabled = true;
          try {
            const res = await fetch(`${backend ?? ""}/api/product/source/${encodeURIComponent(file)}`, { credentials: backend ? "omit" : "same-origin", headers: token ? { Authorization: `Bearer ${token}` } : {} });
            if (!res.ok) throw new Error("That document couldn't be downloaded.");
            const link = document.createElement("a");
            link.href = URL.createObjectURL(await res.blob());
            link.download = file;
            link.click();
            URL.revokeObjectURL(link.href);
          } catch (e) { toast(e.message, true); }
          btn.disabled = false;
        }));
        $("#prd-draft")?.addEventListener("click", () => draftPanel());
        view.querySelectorAll("[data-prd-edit]").forEach((btn) => btn.addEventListener("click", () => {
          const id = btn.dataset.prdEdit, s = p.sections.find((x) => x.id === id), box = view.querySelector(`[data-prd-body="${id}"]`);
          btn.hidden = true;
          showSectionEditor(id, box, s.body, s.hint, s.title, async (body) => {
            try { await save({ section: id, body }); toast("Saved"); await reload(); } catch (e) { toast(e.message, true); }
          }, reload);
        }));
        $("#prd-auto")?.addEventListener("change", async (e) => { try { p = await api("product/settings", { method: "POST", body: { auto_update: e.target.checked } }); toast(p.auto_update ? "It will keep itself up to date" : "Automatic updates are off"); } catch (err) { e.target.checked = !e.target.checked; toast(err.message, true); } });
        const updateModelSelect = $("#prd-update-model-select");
        if (updateModelSelect) {
          updateModelSelect.dataset.previous = updateModelSelect.value;
          updateModelSelect.addEventListener("change", async (e) => {
            const next = e.target.value;
            if (next === "__manage_llms__") {
              e.target.value = updateModelSelect.dataset.previous || p.model || "claude-sonnet-4-6";
              location.hash = "#/config/models";
              return;
            }
            updateModelSelect.dataset.previous = next;
            p.model = next;
            try {
              await api("product/model", { method: "POST", body: { model: next } });
              toast("Product model updated");
            } catch (err) {
              toast(err.message, true);
            }
          });
        }
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
        const referenceDrop = $("#prd-add-file");
        referenceDrop?.addEventListener("dragover", (event) => { event.preventDefault(); referenceDrop.classList.add("over"); });
        referenceDrop?.addEventListener("dragleave", () => referenceDrop.classList.remove("over"));
        referenceDrop?.addEventListener("drop", (event) => {
          event.preventDefault(); referenceDrop.classList.remove("over");
          if (!event.dataTransfer?.files?.length || !file) return;
          file.files = event.dataTransfer.files;
          file.dispatchEvent(new Event("change"));
        });
        file?.addEventListener("change", async () => {
          for (const f of file.files) {
            if (f.size > NJ_UPLOAD_LIMIT) { toast(`${f.name} is over 25 MB`, "warning"); continue; }
            try { p = await uploadFile(f, "product/design"); toast(`Added ${f.name}`); } catch (e) { toast(e.message, true); }
          }
          file.value = ""; await reload();
        });
        const linkForm = view.querySelector("#prd-link-form");
        const linkInput = view.querySelector("#prd-link-input");
        linkForm?.addEventListener("submit", async (e) => {
          e.preventDefault();
          let raw = (linkInput?.value || "").trim();
          if (!raw) return;
          if (!/^https?:\/\//i.test(raw)) raw = "https://" + raw;
          let label = raw;
          try {
            const u = new URL(raw);
            label = u.hostname.replace(/^www\./, "") + (u.pathname !== "/" ? u.pathname : "");
            if (label.length > 60) label = label.slice(0, 57) + "…";
          } catch {}
          const submitBtn = view.querySelector("#prd-link-submit");
          if (submitBtn) submitBtn.disabled = true;
          try {
            await api("product/reference", { method: "POST", body: { label, url: raw } });
            toast("Reference link added");
            if (linkInput) linkInput.value = "";
            await reload();
          } catch (err) {
            toast(err.message, true);
          } finally {
            if (submitBtn) submitBtn.disabled = false;
          }
        });
        $("#prd-add-app")?.addEventListener("click", async () => {
          if ($("#dialog")?.open) return;
          try {
            const picker = await linkPickerHtml({ prefer: ["figma"], label: "Choose a reference" });
            const dlg = formDialog("Connected apps", picker.html + (picker.connected ? `<small class="hint-text">Choose search results or paste a link, then submit to add references to Look and feel.</small>` : ""), picker.connected ? "Submit" : "Close");
            picker.wire();
            if (picker.connected) wireReferenceSubmit();
            $("#dialog-body a[href='#/connections']")?.addEventListener("click", () => $("#dialog").close("cancel"));
            const v = await dlg;
            const links = v ? JSON.parse(v.links || "[]") : [];
            if (!links.length) return;
            await api("product/reference", { method: "POST", body: { links } });
            toast("References added"); await reload();
          } catch (e) { toast(e.message, true); }
        });
        if (open.section) { view.querySelector(`#sec-${CSS.escape(open.section)}`)?.scrollIntoView({ block: "start" }); open.section = ""; }
        if (open.history) {
          const row = view.querySelector(`[data-version="${CSS.escape(open.history)}"]`);
          row?.querySelector("[data-prd-diff]")?.click();
          row?.scrollIntoView({ block: "center" });
          open.history = "";
        }
      };

      // Helpers to split proposal into sections and format summary
      const splitPrdSections = (text) => {
        const byTitle = { "pitch": "pitch", "who it's for": "who", "who": "who", "core features": "features", "features": "features", "look and feel": "look", "look": "look", "what to exclude": "not", "not this": "not", "not": "not" };
        const titles = { pitch: "Pitch", who: "Who it's for", features: "Core features", look: "Look and feel", not: "What to exclude" };
        const sections = { pitch: "", who: "", features: "", look: "", not: "" };
        let cur = null;
        for (const line of (text || "").split("\n")) {
          const m = line.match(/^##\s+(.*?)\s*$/);
          if (m) {
            cur = byTitle[m[1].toLowerCase()] || null;
            continue;
          }
          if (cur) sections[cur] = (sections[cur] ? sections[cur] + "\n" : "") + line;
        }
        return ["pitch", "who", "features", "look", "not"].map((id) => ({ id, title: titles[id], body: (sections[id] || "").trim() }));
      };

      const formatSummaryBullets = (summary) => {
        if (!summary) return "";
        const lines = summary.split("\n").map((l) => l.trim()).filter(Boolean);
        const bulleted = lines.map((l) => (l.startsWith("-") || l.startsWith("*") || l.startsWith("•") ? l : `- ${l}`));
        return bulleted.join("\n");
      };

      // Shared by Import and Draft: review drafted or imported subsections, edit or revert each in place.
      const proposalView = async (proposal, { source, label, onDone }) => {
        const prevSections = (p?.sections || []).map((s) => ({ ...s }));
        const prevMap = new Map(prevSections.map((s) => [s.id, s.body || ""]));
        let saveError = "";
        try {
          await save({ text: proposal.markdown, source, summary: proposal.summary });
          toast(source === "draft" ? "Drafted from your project" : "PRD imported");
        } catch (e) {
          saveError = e.message;
          toast(e.message, true);
        }
        const sections = proposal.sections && proposal.sections.length ? proposal.sections : splitPrdSections(proposal.markdown);
        panel().innerHTML = `<section class="card mb-16" id="prd-proposal-card"><div class="card-h"><h2>${esc(label)}</h2></div>
          <div class="card-b stack">
            ${saveError ? `<div class="notice bad">This draft was not saved, so your PRD is unchanged: ${esc(saveError)} <button type="button" class="btn small" id="prd-proposal-retry">Save draft</button></div>` : ""}
            ${proposal.attached?.length ? `<div class="notice">The full document is attached (${esc(proposal.attached.join(", "))}). The fields below are short summaries; jobs read the whole document when they need the detail.</div>` : ""}
            ${proposal.summary ? `<div class="proposal-summary">
              <div class="proposal-summary-title"><strong>What was inferred &amp; gaps:</strong></div>
              <div class="md">${Markdown.render(formatSummaryBullets(proposal.summary))}</div>
            </div>` : ""}
            <div class="proposal-sections">
              ${sections.map((s) => {
                const curBody = saveError ? (s.body || "") : (p?.sections?.find((x) => x.id === s.id)?.body ?? (s.body || ""));
                const prevBody = prevMap.get(s.id) ?? "";
                const canRevert = !saveError && prevBody.trim() !== curBody.trim();
                return `<div class="card proposal-section-item" id="prop-sec-${esc(s.id)}">
                  <div class="card-h">
                    <h3 class="proposal-sec-title">${esc(s.title)}</h3>
                    <div class="row gap-10 items-center">
                      <button type="button" class="btn small ghost" data-revert-sec="${esc(s.id)}" ${canRevert ? "" : "disabled"}>Revert</button>
                      <button type="button" class="btn small" data-edit-sec="${esc(s.id)}">Edit</button>
                    </div>
                  </div>
                  <div class="card-b">
                    ${curBody.trim() ? `<div class="md">${Markdown.render(curBody)}</div>` : `<div class="muted">No content for this section.</div>`}
                  </div>
                </div>`;
              }).join("")}
            </div>
            <div class="row gap-10 mt-12 items-center">
              <button type="button" class="btn primary" id="prd-proposal-done">Done</button>
            </div>
          </div></section>`;

        $("#prd-proposal-retry")?.addEventListener("click", () => proposalView(proposal, { source, label, onDone }));
        $("#prd-proposal-done")?.addEventListener("click", async () => {
          panel().innerHTML = "";
          await reload();
          onDone?.();
        });

        panel().querySelectorAll("[data-revert-sec]").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const id = btn.dataset.revertSec;
            const s = sections.find((x) => x.id === id);
            if (!s) return;
            const cardEl = panel().querySelector(`#prop-sec-${CSS.escape(id)}`);
            const box = cardEl?.querySelector(".card-b");
            const prevBody = prevMap.get(id) ?? "";
            btn.disabled = true;
            try {
              await save({ section: id, body: prevBody, summary: `Reverted ${s.title}` });
              toast(`Reverted ${s.title}`);
              if (box) {
                box.innerHTML = prevBody.trim() ? `<div class="md">${Markdown.render(prevBody)}</div>` : `<div class="muted">No content for this section.</div>`;
              }
              btn.disabled = true;
            } catch (e) {
              btn.disabled = false;
              toast(e.message, true);
            }
          });
        });

        panel().querySelectorAll("[data-edit-sec]").forEach((btn) => {
          btn.addEventListener("click", () => {
            const id = btn.dataset.editSec;
            const s = sections.find((x) => x.id === id);
            if (!s) return;
            const cardEl = panel().querySelector(`#prop-sec-${CSS.escape(id)}`);
            const box = cardEl?.querySelector(".card-b");
            const revertBtn = cardEl?.querySelector(`[data-revert-sec="${CSS.escape(id)}"]`);
            if (!box) return;
            btn.hidden = true;
            if (revertBtn) revertBtn.disabled = true;
            const curSection = p?.sections?.find((x) => x.id === id) || s;
            const hint = curSection.hint || s.hint || "";
            const currentBody = p?.sections?.find((x) => x.id === id)?.body ?? (s.body || "");
            const prevBody = prevMap.get(id) ?? "";

            showSectionEditor(id, box, currentBody, hint, s.title, async (newBody) => {
              try {
                await save({ section: id, body: newBody, summary: `Updated ${s.title}` });
                toast("Saved");
                box.innerHTML = newBody.trim() ? `<div class="md">${Markdown.render(newBody)}</div>` : `<div class="muted">No content for this section.</div>`;
                btn.hidden = false;
                if (revertBtn) revertBtn.disabled = (prevBody.trim() === newBody.trim());
              } catch (e) {
                toast(e.message, true);
              }
            }, () => {
              const cur = p?.sections?.find((x) => x.id === id)?.body ?? (s.body || "");
              box.innerHTML = cur.trim() ? `<div class="md">${Markdown.render(cur)}</div>` : `<div class="muted">No content for this section.</div>`;
              btn.hidden = false;
              if (revertBtn) revertBtn.disabled = (prevBody.trim() === cur.trim());
            });
          });
        });
      };
      const busy = (text, onCancel) => {
        const started = Date.now();
        panel().innerHTML = `<div class="prd-busy" role="status" aria-live="polite"><div class="spinner" aria-hidden="true"></div>
          <div><strong>${esc(text)}</strong><div class="prd-busy-sub">This can take a few minutes. <span data-elapsed>0s</span></div></div>
          ${onCancel ? `<button type="button" class="btn small" id="prd-busy-cancel" aria-label="Cancel drafting" title="Stop drafting">Cancel</button>` : ""}</div>`;
        const el = panel().querySelector("[data-elapsed]");
        const timer = setInterval(() => { if (!el.isConnected) return clearInterval(timer); el.textContent = `${Math.floor((Date.now() - started) / 1000)}s`; }, 1000);
        if (onCancel) {
          $("#prd-busy-cancel")?.addEventListener("click", () => onCancel());
        }
      };

      const readPrd = async ({ file, text }) => {
        if (file && file.size > NJ_UPLOAD_LIMIT) { toast("That file is over 25 MB.", "warning"); return importPanel(); }
        if (file && !/\.(md|markdown|txt|docx|pdf)$/i.test(file.name)) { toast("Use a markdown, text, Word or PDF file, or paste the text.", "warning"); return importPanel(); }
        let canceled = false, taskId = null;
        const abortCtrl = new AbortController();
        const cancel = async () => {
          if (canceled) return;
          canceled = true;
          abortCtrl.abort();
          if (taskId) { try { await api(`product/task/${encodeURIComponent(taskId)}`, { method: "DELETE" }); } catch {} }
          toast("Import cancelled", "cancel");
          importPanel();
        };
        busy(file ? `Reading ${file.name}…` : "Reading it…", cancel);
        try {
          const started = file ? await uploadFile(file, "product/import", { signal: abortCtrl.signal }) : await api("product/import", { method: "POST", body: { text }, signal: abortCtrl.signal });
          taskId = started?.task;
          if (canceled) {
            if (taskId) { try { await api(`product/task/${encodeURIComponent(taskId)}`, { method: "DELETE" }); } catch {} }
            return;
          }
          const taskResult = await waitForTask(started, { signal: abortCtrl.signal });
          if (canceled) return;
          await proposalView(taskResult, { source: "import", label: "Your PRD, in this format", onDone: () => {} });
        } catch (e) {
          if (canceled || e.name === "AbortError" || e.message === "Canceled" || e.message === "Cancelled" || e.message === "Draft canceled." || e.message === "Draft cancelled.") return;
          toast(e.message, true);
          importPanel();
        }
      };

      const importPanel = () => {
        panel().innerHTML = `<section class="card mb-16"><div class="card-h"><h2>Import PRD</h2></div><div class="card-b stack">
          <div class="prd-drop file-picker" id="prd-drop" tabindex="0" role="button" aria-label="Choose a PRD file, or drop one here">
            ${filePickerContent("Pick your product document", "Markdown, text, Word or PDF", false)}</div>
          <input type="file" id="prd-import-input" accept=".md,.markdown,.txt,.docx,.pdf" hidden>
          <p class="muted">The fields above get a short summary of it, and the whole document stays attached so jobs can read the detail.</p>
          <label class="field"><span>Or paste it</span><textarea id="prd-import-text" rows="6" aria-label="Paste your PRD"></textarea></label>
          <div class="row gap-10"><button type="button" class="btn primary" id="prd-import-go" disabled>Submit</button><button type="button" class="btn ghost" id="prd-import-cancel">Cancel</button></div></div></section>`;
        const input = $("#prd-import-input"), zone = $("#prd-drop"), goBtn = $("#prd-import-go"), textEl = $("#prd-import-text");
        let picked = null;
        const sync = () => {
          goBtn.disabled = !picked && !textEl.value.trim();
        };
        const setFile = (f) => {
          if (!f) return;
          picked = f;
          const strong = zone.querySelector("strong");
          if (strong) strong.textContent = f.name;
          const sub = zone.querySelector(".muted");
          if (sub) sub.textContent = "Click to choose another file";
          sync();
        };
        zone.addEventListener("click", () => input.click());
        zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
        input.addEventListener("change", () => { if (input.files[0]) setFile(input.files[0]); });
        ["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add("over"); }));
        ["dragleave", "dragend"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("over")));
        zone.addEventListener("drop", (e) => {
          e.preventDefault();
          zone.classList.remove("over");
          const f = e.dataTransfer?.files?.[0];
          if (f) setFile(f);
        });
        textEl.addEventListener("input", sync);
        textEl.addEventListener("keydown", (e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !goBtn.disabled) {
            e.preventDefault();
            goBtn.click();
          }
        });
        $("#prd-import-cancel").addEventListener("click", () => { panel().innerHTML = ""; });
        goBtn.addEventListener("click", () => {
          if (picked) return readPrd({ file: picked });
          const text = textEl.value.trim();
          if (text) readPrd({ text });
        });
      };

      // An existing project: read what is there (README, notes, manifests, layout, recent commits) and propose a first version.
      const draftPanel = async () => {
        let canceled = false, taskId = null;
        const abortCtrl = new AbortController();
        const cancel = async () => {
          if (canceled) return;
          canceled = true;
          abortCtrl.abort();
          if (taskId) { try { await api(`product/task/${encodeURIComponent(taskId)}`, { method: "DELETE" }); } catch {} }
          toast("Draft cancelled", "cancel");
          panel().innerHTML = "";
        };
        const modelLabel = p.available_models?.find((m) => m.id === p.model)?.label || p.model || "";
        const busyText = modelLabel
          ? `Reading your project and drafting with ${modelLabel}…`
          : "Reading your project and drafting with AI…";
        busy(busyText, cancel);
        try {
          const started = await api("product/draft", { method: "POST", body: { model: p.model || "" }, signal: abortCtrl.signal });
          taskId = started?.task;
          if (canceled) {
            if (taskId) { try { await api(`product/task/${encodeURIComponent(taskId)}`, { method: "DELETE" }); } catch {} }
            return;
          }
          const taskResult = await waitForTask(started, { signal: abortCtrl.signal });
          if (canceled) return;
          await proposalView(taskResult, { source: "draft", label: modelLabel ? `Drafted with ${modelLabel}` : "Drafted from your project", onDone: () => { panel().innerHTML = ""; } });
        } catch (e) {
          if (canceled || e.name === "AbortError" || e.message === "Canceled" || e.message === "Cancelled" || e.message === "Draft canceled." || e.message === "Draft cancelled.") return;
          toast(e.message, true);
          panel().innerHTML = "";
        }
      };

      // The first time the requirements have something in them, offer to break them down into epics. Asked once, on any device,
      // and not on top of an import or a draft the person is looking at.
      let offering = false;
      function offerBreakdown() {
        if (offering || !epics || start || !prdFilled() || p.breakdown_offered || productEpics().length || open.imp || open.draft || $("#dialog")?.open) return;
        if (panel()?.children.length) return;
        offering = true;
        const asked = formDialog("Break your product down?",
          `<p>Your requirements have something in them now. Orchestrator can split them into <strong>epics</strong>: the big pieces of the product, using a story map or a top-down work breakdown, as detailed as what you've written.</p>
           <p class="muted">You approve the ones you want. Each becomes a job marked Epic that waits until you plan it. You can do this any time from Epics, at the bottom of this page.</p>`, "Break it down", { compact: true });
        $("#dialog-cancel").textContent = "Not now";
        asked.then((answer) => {
          p.breakdown_offered = true;
          api("product/settings", { method: "POST", body: { breakdown_offered: true } }).catch(() => {});
          if (answer) { $("#sec-epics")?.scrollIntoView({ block: "start" }); epics.breakDown(); }
        });
      }

      wire();
      hydrateAuthImages();
      epics?.mount(start);
      offerBreakdown();
      if (open.imp) importPanel();
      else if (open.draft && p.can_draft) draftPanel();
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

// ---------------------------------------------------------------- epics
// The product broken down into epics, the second half of the Product page: suggested from the requirements, approved or
// declined here, each approved one a waiting job marked Epic. With Jira connected they can be sent to Jira, and Jira's
// epics imported as jobs. `onPrdChanged` reloads the page when answering a question added to the requirements.
function epicsSection(initial, { onPrdChanged }) {
  let ov = initial; // the overview
  const owner = state.you?.role !== "member";
  let technique = "";
  let jiraList = null; // epics found in Jira, while the import panel is open
  let working = null;  // the breakdown in flight: { abort }
  let busy = "";       // its spinner, kept when the page around it redraws

  const breakdownCard = () => {
    if (!ov.prd_filled) return `<section class="card mb-16"><div class="card-b stack"><strong>Write your product requirements first.</strong>
      <span class="muted">Anything in any field is enough: the breakdown follows how much you've said.</span>
      </div></section>`;
    return `<section class="card mb-16" id="epic-breakdown"><div class="card-h"><h2>Break down from the PRD</h2></div><div class="card-b stack">
      <p class="muted">Orchestrator reads your product requirements and splits them into epics, using the technique that fits what you've written (a story map for flows people follow, a top-down work breakdown for technical products), and as much detail as you gave. You approve the ones you want.</p>
      ${ov.epics.length ? "" : `<div class="row"><button type="button" class="btn primary" id="epic-break">Break down from the PRD</button></div>`}
      <details class="advanced"><summary>Advanced options</summary><div class="stack">
        ${FeaturePlan.techniqueSelect(technique)}
        ${ov.epics.length ? `<div class="row"><button type="button" class="btn" id="epic-break">Break down again</button></div>
          <p class="muted">Breaking down again replaces the suggestions you haven't decided on. Approved and declined epics stay.</p>` : ""}</div></details>
      ${FeaturePlan.renderBreakdownInfo(ov.breakdown)}
      <div id="epics-busy">${busy}</div></div></section>`;
  };

  const jiraCard = () => {
    const j = ov.jira || {};
    if (!j.connected) return `<section class="card mb-16"><div class="card-h"><h2>Jira</h2></div><div class="card-b stack">
      <span class="muted">Connect Jira to create these epics there, or to bring epics you already have in Jira in as jobs.</span>
      <div class="row"><a class="btn" href="#/connections?card=jira">Connect Jira</a></div></div></section>`;
    if (!owner) return `<section class="card mb-16"><div class="card-h"><h2>Jira</h2></div><div class="card-b"><span class="muted">Connected to ${esc(j.site)}. The owner of this computer sends and imports epics.</span></div></section>`;
    const body = !j.project
      ? `<form class="row gap-10 end wrap" id="epic-jira-project"><label class="field"><span>Jira project key</span><input name="project" placeholder="ABC" maxlength="10" autocomplete="off" required></label>
          <button type="submit" class="btn primary">Save</button></form>
         <span class="muted">The project epics are created in, and imported from. It's the letters in your ticket numbers (ABC-123).</span>`
      : `<div class="row gap-10 wrap"><button type="button" class="btn primary" id="epic-jira-send" ${ov.epics.some((e) => e.status === "approved" && !e.jira) ? "" : "disabled"}>Send approved epics to Jira</button>
          <button type="button" class="btn" id="epic-jira-load">Import epics from Jira</button></div>
         <span class="muted">Epics are created in ${esc(j.project)} on ${esc(j.site)}, and each approved epic then shows its Jira key. Nothing is sent until you press the button.</span>
         <div id="epic-jira-panel">${jiraList ? FeaturePlan.renderJiraImport(jiraList, j.project) : ""}</div>`;
    return `<section class="card mb-16" id="epic-jira"><div class="card-h"><h2>Jira</h2><span class="muted">${esc(j.site)}</span></div><div class="card-b stack">${body}</div></section>`;
  };

  const body = () => `${breakdownCard()}${ov.prd_filled ? FeaturePlan.renderQuestions(ov.breakdown) : ""}${FeaturePlan.renderSuggested(ov.epics, ov.breakdown)}
    ${FeaturePlan.renderApproved(ov.epics, { act, jiraConnected: ov.jira?.connected && owner && !!ov.jira.project })}
    ${FeaturePlan.renderDeclined(ov.epics)}${jiraCard()}`;

  const redraw = () => { const box = $("#epics-body"); if (box) box.innerHTML = body(); };
  const take = (data, message) => { ov = data; if (message) toast(message); redraw(); };
  const failed = (e) => { if (e?.name !== "AbortError") toast(e.message, true); };
  const checks = () => [...document.querySelectorAll('#epic-suggested-form input[name="chosen"]')];
  const chosen = () => checks().filter((c) => c.checked).map((c) => c.value);

  const breakDown = async () => {
    if (working) return;
    const box = $("#epics-busy");
    if (!box) return;
    working = { abort: new AbortController() };
    technique = $("#epic-technique")?.value || technique;
    box.innerHTML = busy = `<div class="prd-busy" role="status" aria-live="polite"><div class="spinner" aria-hidden="true"></div>
      <div><strong>Reading your requirements and breaking them down…</strong><div class="prd-busy-sub">This can take a minute. It carries on if you leave this page.</div></div>
      <button type="button" class="btn small" id="epic-break-cancel">Cancel</button></div>`;
    $("#epic-break")?.setAttribute("disabled", "");
    const mine = working;
    try {
      const started = await api("epics/breakdown", { method: "POST", body: { technique }, signal: mine.abort.signal });
      mine.task = started.task;
      const data = await waitForTask(started, { signal: mine.abort.signal });
      if (document.querySelector("#epics-body")) take(data, `${plural(data.epics.filter((e) => e.status === "suggested").length, "epic")} suggested`);
    } catch (e) {
      if (!mine.abort.signal.aborted) { failed(e); $("#epics-busy") && ($("#epics-busy").innerHTML = `<div class="notice bad">${esc(e.message)}</div>`); }
    } finally {
      working = null;
      busy = "";
      if (mine.abort.signal.aborted) redraw();
    }
  };

  // Stopping also tells the computer, so a breakdown the person gave up on isn't saved when the model finishes.
  const stopBreakdown = () => {
    if (!working) return;
    working.abort.abort();
    if (working.task) api(`product/task/${encodeURIComponent(working.task)}`, { method: "DELETE" }).catch(() => {});
  };

  const decide = async (decision, ids, message) => {
    if (!ids.length) return toast("Tick at least one epic.", "warning");
    try {
      const data = await api("epics/decide", { method: "POST", body: { ids, decision } });
      const also = data.also_approved?.length ? ` Also approved, because they come first: ${data.also_approved.join(", ")}.` : "";
      take(data, message + also);
    } catch (e) { failed(e); }
  };

  const sendToJira = async (ids) => {
    try {
      const data = await api("epics/jira/send", { method: "POST", body: { ids } });
      take(data, `${plural(data.sent.length, "epic")} created in Jira`);
      if (data.errors?.length) toast(data.errors[0], true);
    } catch (e) { failed(e); }
  };

  const onClick = async (event) => {
    const t = event.target;
    if (t.closest("#epic-break")) return breakDown();
    if (t.closest("#epic-break-cancel")) { stopBreakdown(); return; }
    if (t.closest("#epic-decline")) return decide("decline", chosen(), "Declined");
    const restore = t.closest("[data-epic-restore]");
    if (restore) return decide("restore", [restore.dataset.epicRestore], "Brought back");
    const send = t.closest("[data-epic-send]");
    if (send) return sendToJira([send.dataset.epicSend]);
    if (t.closest("#epic-jira-send")) return sendToJira([]);
    if (t.closest("#epic-jira-cancel")) { jiraList = null; return redraw(); }
    if (t.closest("#epic-jira-load")) {
      const button = t.closest("#epic-jira-load");
      button.disabled = true;
      try { jiraList = (await api("epics/jira")).epics; redraw(); } catch (e) { failed(e); button.disabled = false; }
    }
  };
  const onChange = (event) => {
    const input = event.target.closest('#epic-suggested-form input[name="chosen"]');
    if (!input) return;
    const byId = new Map(checks().map((c) => [c.value, c]));
    const needs = (c) => (c.dataset.needs || "").split("\u001f").filter(Boolean);
    if (input.checked) { // ticking an epic also ticks what it builds on
      const todo = [input];
      while (todo.length) for (const dep of needs(todo.pop())) { const c = byId.get(dep); if (c && !c.checked) { c.checked = true; todo.push(c); } }
    } else { // unticking one unticks what builds on it
      let changed = true;
      while (changed) { changed = false; for (const c of checks()) if (c.checked && needs(c).some((dep) => byId.get(dep) && !byId.get(dep).checked)) { c.checked = false; changed = true; } }
    }
  };
  const onSubmit = async (event) => {
    const form = event.target;
    if (form.id === "epic-suggested-form") { event.preventDefault(); return decide("approve", chosen(), "Approved. Each is a feature and a waiting job marked Epic."); }
    if (form.id === "epic-questions-form") {
      event.preventDefault();
      const data = new FormData(form);
      const answers = [...form.querySelectorAll(".epic-question")].map((box, i) => ({ question: box.dataset.question, answer: String(data.get(`a${i}`) || "").trim(), section: data.get(`s${i}`) }))
        .filter((a) => a.answer);
      if (!answers.length) return toast("Write an answer to at least one question.", "warning");
      try {
        take(await api("epics/answers", { method: "POST", body: { answers } }), `${plural(answers.length, "answer")} added to your product requirements`);
        await onPrdChanged();
        if (event.submitter?.dataset.after === "again") breakDown();
      } catch (e) { failed(e); }
    }
    if (form.id === "epic-jira-project") {
      event.preventDefault();
      try { take(await api("epics/jira/project", { method: "POST", body: { project: new FormData(form).get("project") } }), "Jira project saved"); } catch (e) { failed(e); }
    }
    if (form.id === "epic-jira-form") {
      event.preventDefault();
      const keys = new FormData(form).getAll("keys");
      if (!keys.length) return toast("Tick at least one Jira epic.", "warning");
      try { jiraList = null; take(await api("epics/jira/import", { method: "POST", body: { keys } }), `${plural(keys.length, "epic")} imported as jobs`); } catch (e) { failed(e); }
    }
  };

  return {
    data: () => ov,
    set: (data) => { ov = data; },
    html: () => `<div class="page-part" id="sec-epics"><h2>Epics</h2>
      <p class="muted">Your product broken into epics. Approved ones become jobs marked Epic, waiting until you plan them.</p></div>
      <div id="epics-body">${body()}</div>`,
    breakDown,
    // Listens on the view, so it keeps working when the Product page redraws around it.
    mount: (start) => {
      view.addEventListener("click", onClick);
      view.addEventListener("change", onChange);
      view.addEventListener("submit", onSubmit);
      cleanup.push(() => { working?.abort.abort(); /* stop polling; the computer keeps going and saves the result */ view.removeEventListener("click", onClick); view.removeEventListener("change", onChange); view.removeEventListener("submit", onSubmit); });
      if (start && ov.prd_filled && !ov.epics.length) breakDown();
    },
  };
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
  if (!suites.length) {
    return `<div class="empty stack gap-8">
      <p>No test suites found in this project${open ? " matching your filter" : ""}.</p>
      ${!open ? `<div class="row gap-8 justify-center"><a class="btn small primary" href="#/new?type=coverage"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-plus"/></svg> Add tests</a></div>` : ""}
    </div>`;
  }
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

function coverageJobParams(data, caseView, values = {}) {
  const rawTarget = values.target !== undefined && values.target !== "" ? String(values.target).replace(/%/g, "").trim() : "100";
  const target = rawTarget ? Number(rawTarget) : 100;
  if (!Number.isFinite(target) || target <= 0 || target > 100) throw new Error("Choose a coverage target between 1 and 100%.");
  const cov = data.coverage;
  const measured = cov?.overall_coverage_pct != null && !cov.estimated;
  const gaps = (caseView.cases || []).filter((c) => c.status === "unassigned" || c.status === "planned").slice(0, 30);
  const spec = [
    "Analyze the project's existing tests and source code. Identify and prioritise coverage gaps by user impact and regression risk.",
    measured ? `Last measured coverage: ${cov.overall_coverage_pct}% of ${cov.total_lines ? `${cov.total_lines.toLocaleString()} ` : ""}${cov.metric || "lines"} (${cov.timestamp || "date unknown"}). Verify with a fresh measurement.` : "Coverage is not measured yet. Establish a baseline using the project's coverage tooling.",
    `Aim for ${target}% measured coverage. Explain if ${target === 100 ? "100% coverage" : "the target"} is impractical or coverage tooling is unavailable; do not invent a percentage.`,
    "In the plan, explain which gaps to address and why. Then implement the recommended tests, run the affected suites, measure coverage again, and report the before/after results. Keep production behavior unchanged and use the project's existing test frameworks.",
    gaps.length ? "Known missing automated test cases:\n" + gaps.map((c) => `- ${c.id}: ${c.title} (${c.area || "General"})`).join("\n") : "",
  ].filter(Boolean).join("\n\n");
  return { type: "coverage", title: "Coverage expanding", summary: `Improve test coverage toward ${target}%`,
    subsystems: "Project-wide: prioritise missing coverage", spec, branch_mode: "current", no_dispatch: values.execution === "review", yolo: false, free: values.free === "on" };
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
  const hasTests = total > 0;
  // Automatically show 0% if project has no tests yet.
  const effectiveCov = (cov && cov.overall_coverage_pct != null && !cov.estimated)
    ? cov
    : (!hasTests ? {
        overall_coverage_pct: 0,
        total_lines: cov?.total_lines ?? 0,
        covered_lines: 0,
        total_tests: 0,
        total_suites: 0,
        metric: cov?.metric || "lines",
        timestamp: cov?.timestamp,
        last_error: cov?.last_error,
      } : null);
  const measured = Boolean(effectiveCov);
  const recentCoverageRun = (state.runs || []).filter((r) => r.action === "coverage").sort((a, b) => (b.started || 0) - (a.started || 0))[0];
  const coverageFailureRun = !hasTests
    ? {
        id: "cov-no-tests",
        action: "coverage",
        title: "Measure test coverage",
        running: false,
        exit_code: 1,
        error_summary: "No source files or tests found yet in this project. Create initial code and tests before measuring coverage.",
        started: Math.floor(Date.now() / 1000),
        ended: Math.floor(Date.now() / 1000),
      }
    : ((recentCoverageRun && recentCoverageRun.exit_code !== 0) ? recentCoverageRun
    : (cov?.last_error ? {
        id: "cov-failure",
        action: "coverage",
        title: "Measure test coverage",
        running: false,
        exit_code: 1,
        error_summary: cov.last_error,
        started: cov.last_error_at ? Math.floor(Date.parse(cov.last_error_at) / 1000) : Math.floor(Date.now() / 1000),
        ended: cov.last_error_at ? Math.floor(Date.parse(cov.last_error_at) / 1000) : Math.floor(Date.now() / 1000),
      } : null));
  const showNextStep = Boolean(!hasTests || (recentCoverageRun && recentCoverageRun.exit_code !== 0) || cov?.last_error);
  return {
    title: "Tests",
    sub: `${withTests.length} suites · ${total} tests`,
    actions: `<button class="btn primary test-run-all-btn" ${act("test")}>Run all tests</button><button type="button" class="btn test-expand-coverage-btn" id="expand-coverage">Expand coverage</button>`,
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
      <div id="next-step" class="mb-16"${showNextStep && coverageFailureRun ? "" : " hidden"}>${showNextStep && coverageFailureRun ? runStatus(coverageFailureRun) : ""}</div>
      <section class="card"><div class="card-h"><h2>Coverage${effectiveCov?.timestamp ? ` <span class="count">${esc(ago(Date.parse(effectiveCov.timestamp) / 1000))}</span>` : ""}</h2>
        <button type="button" class="btn small" id="recalc-coverage" ${act("coverage")} title="Runs every test with coverage turned on, which may take time">${measured ? "Recalculate" : "Calculate"}</button></div>
        <div class="card-b stack">${measured ? `<div class="row"><span class="big-number">${esc(effectiveCov.overall_coverage_pct)}%</span>
          <span class="muted">${effectiveCov.total_lines ? `of ${effectiveCov.total_lines.toLocaleString()} ` : ""}${esc(effectiveCov.metric || "lines")} covered${effectiveCov.tool ? `, measured with ${esc(effectiveCov.tool)}` : ""} · ${esc(effectiveCov.total_tests)} tests in ${esc(effectiveCov.total_suites)} suites</span></div>`
          : `<p class="muted">Not measured yet.</p>`}</div></section>
      ${data.plans.length ? `<section class="card"><div class="card-h"><h2>Test plans</h2></div><div class="list">${data.plans.map((p) => `
        <div class="item"><div class="main-col"><div class="title">${esc(p)}</div></div><div class="side"><button class="btn small" ${act("test_plan", { name: p })}>Run</button></div></div>`).join("")}</div></section>` : ""}
      <section class="card"><div class="card-h"><h2>Suites</h2>
        <form id="suite-filter" class="row"><input type="search" name="q" value="${esc(query.get("q") || "")}" placeholder="Filter" aria-label="Filter suites"></form></div>
        ${suiteGroupsHtml(suites, { open: Boolean(filter) })}</section>
      ${state.project.mobile_app !== false && (!state.project.languages?.length || state.project.languages.some((l) => l.name === "Swift")) ? `<section class="card"><div class="card-h"><h2>Simulator visual check</h2></div>
        <div class="card-b stack"><p>Builds and launches your app in the simulator, then captures screenshots for you to review.</p>
          <p class="muted">Requires Xcode and an iOS simulator.</p>
          <div><button type="button" class="btn" ${act("visual_check")}>Capture screenshots</button></div></div></section>` : ""}
      ${data.frameworks && Object.keys(data.frameworks).length > 0 ? (() => {
        const frameworkGuidance = (key, f) => {
          const guide = {
            swift_testing: {
              benefit: "Apple's modern Swift-native testing library with @Test, @Suite, and #expect. Executes concurrently with macro-based diagnostics and zero boilerplate.",
              howToUse: "Add `import Testing` to your test target and mark test functions with `@Test func yourFeature() { #expect(...) }`.",
              status: f.installed ? "Found in your test files. Run your test suites above to execute." : "No @Test files detected. Create a coverage job to scaffold native Swift tests."
            },
            snapshot_testing: {
              benefit: "Visual UI regression prevention. Captures SwiftUI and UIKit view hierarchies pixel-by-pixel to catch unintended layout breaks and dark mode regressions.",
              howToUse: "Add `import SnapshotTesting` to your tests and assert views with `assertSnapshot(of: view, as: .image)`.",
              status: f.installed ? "Detected in your test source files. Compares snapshots against stored baseline images." : "Not found in your tests. Add SnapshotTesting via SPM to record reference screenshots."
            },
            quick_nimble: {
              benefit: "Behavior-Driven Development (BDD) specs with human-readable descriptions and asynchronous polling matchers (toEventually).",
              howToUse: "Subclass `QuickSpec` and define test scenarios using `describe(...) { it(...) { expect(...).toEventually(...) } }`.",
              status: f.installed ? "Quick specs and Nimble expectations detected in your test suites." : "Not found in your tests. Useful for complex async state machines and polling assertions."
            },
            canary_suite: {
              benefit: "Rapid toolchain validation. A minimal starter suite that verifies your test target, build settings, and Swift Testing linkage end-to-end.",
              howToUse: "Click 'Add sample suite' below to generate SampleSwiftTestingTests.swift, then run tests to verify the pipeline.",
              status: f.installed ? "Sample canary suite exists in your test target. Replace its examples with checks for your app." : "Not generated yet. Click 'Add sample suite' below to scaffold a ready-to-run test file."
            },
            xcbeautify: {
              benefit: "Clean, human-readable build logs. Filters raw multiline xcodebuild logs into structured, single-line summaries with highlighted errors.",
              howToUse: "Install once on your Mac with `brew install xcbeautify`. Orchestrator detects it and formats build logs automatically.",
              status: f.installed ? "Installed on this computer. Clean build logs are formatted automatically." : "Not found on this computer. Formats build logs for easier reading; it does not add tests."
            },
            coverage_py: {
              benefit: "Statement and branch coverage analysis. Highlights exact lines of code that need tests.",
              howToUse: "Click 'Auto-add coverage.py' or Calculate Coverage above. Orchestrator automatically runs coverage measurement.",
              status: f.installed ? "coverage.py is installed and ready to measure statement coverage." : "Not installed yet. Clicking Calculate will automatically install it and calculate test coverage."
            },
            pytest: {
              benefit: "Python test runner with automatic test discovery, parameterized tests, modular fixtures, and rich assertion diffs.",
              howToUse: "Write `test_*.py` files with functions named `test_*()` and run via Orchestrator or `pytest` CLI.",
              status: f.installed ? "pytest is installed and available for test execution." : "No pytest tests found. Create a test job to add tests with pytest assertions."
            },
            unittest: {
              benefit: "Built-in standard library testing with zero dependencies, structured TestCase classes, and predictable test lifecycles.",
              howToUse: "Subclass `unittest.TestCase` and run your suites via Orchestrator or `python -m unittest`.",
              status: f.installed ? "Standard library unittest is supported and ready to run." : "Subclass unittest.TestCase in your test files to add test cases."
            }
          }[key] || {
            benefit: f.desc || "Automated testing tool for code quality and regression prevention.",
            howToUse: "Add test files referencing this framework and run test suites to verify behavior.",
            status: f.installed ? "Usage was found in your test source files." : "No usage was found in your test source files."
          };
          return guide;
        };
        return `<section class="card"><details class="fold"><summary class="card-h"><h2>Test frameworks &amp; tools <span class="count">${Object.keys(data.frameworks).length}</span></h2></summary>
          <div class="card-b stack">
            <div class="framework-grid">${Object.entries(data.frameworks).map(([key, f]) => {
              const guide = frameworkGuidance(key, f);
              return `<details class="framework"><summary class="framework-h"><strong>${esc(f.name)}</strong>
              <span class="badge ${f.installed ? "good" : "muted-badge"}">${f.installed ? "Detected" : "Not detected"}</span></summary>
              <div class="framework-detail stack">
                <div class="framework-guide">
                  <div class="framework-guide-row"><strong>Benefit:</strong> <span>${esc(guide.benefit)}</span></div>
                  <div class="framework-guide-row"><strong>How to use:</strong> <span>${codeSpans(guide.howToUse)}</span></div>
                </div>
                <p class="muted">${esc(guide.status)}</p>
                ${key === "canary_suite" && !f.installed ? `<div><button type="button" class="btn small" ${act("scaffold_canary")}>Add sample suite</button></div>` : key === "coverage_py" && !f.installed ? `<div><button type="button" class="btn small" ${act("coverage")}>Auto-add coverage.py</button></div>` : key !== "xcbeautify" && key !== "canary_suite" ? `<div><a class="btn small" href="#/new?type=coverage&summary=${encodeURIComponent(`Add meaningful tests using ${f.name}`)}">Create test job</a></div>` : ""}</div></details>`;
            }).join("")}</div>
          </div></details></section>`;
      })() : ""}`,

    after: () => {
      $("#suite-filter").addEventListener("submit", (e) => {
        e.preventDefault();
        location.hash = `#/tests?q=${encodeURIComponent(new FormData(e.target).get("q"))}`;
      });
      $("#recalc-coverage")?.addEventListener("click", async (e) => {
        if (!hasTests) {
          e.preventDefault();
          e.stopPropagation();
          if (e.stopImmediatePropagation) e.stopImmediatePropagation();
          const btn = $("#recalc-coverage");
          const origText = btn ? btn.textContent : "Recalculate";
          if (btn) {
            btn.disabled = true;
            btn.textContent = "Checking…";
          }
          let foundTests = false;
          try {
            const fresh = await api("tests?refresh=1");
            const freshSuites = (fresh.suites || []).filter((s) => s.tests > 0);
            const freshTotal = freshSuites.reduce((n, s) => n + s.tests, 0);
            if (freshTotal > 0) {
              foundTests = true;
            }
          } catch {
            // Server or network error, proceed with local refresh acknowledgment
          } finally {
            if (btn) {
              btn.disabled = false;
              btn.textContent = origText;
            }
          }
          if (foundTests) {
            toast("Tests detected. Calculating coverage…");
            await route();
            runAction("coverage");
            return;
          }
          const covNum = view.querySelector(".big-number");
          if (covNum) covNum.textContent = "0%";
          const container = $("#next-step");
          if (container) {
            const runForStatus = coverageFailureRun || {
              id: "cov-no-tests",
              action: "coverage",
              title: "Measure test coverage",
              running: false,
              exit_code: 1,
              error_summary: "No source files or tests found yet in this project. Create initial code and tests before measuring coverage.",
              started: Math.floor(Date.now() / 1000),
              ended: Math.floor(Date.now() / 1000),
            };
            container.innerHTML = runStatus(runForStatus);
            container.hidden = false;
            container.classList.remove("highlight-flash");
            void container.offsetWidth;
            container.classList.add("highlight-flash");
            container.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }
          toast("Coverage refreshed: 0%. No tests exist in this project yet.", "info");
        }
      }, true);
      $("#expand-coverage").addEventListener("click", async () => {
        const choicePromise = formDialog("Expand coverage", `<p>Choose an area, or let AI find the most useful gaps to address.</p>
          <div class="import-options coverage-paths"><a class="import-option" id="coverage-specific" href="#/new?type=coverage"><strong>I have an area in mind</strong><span>Describe the feature or code you want tested.</span></a>
            <button type="submit" value="ok" class="import-option"><strong>Find gaps with AI</strong><span>Get a prioritised plan to improve overall coverage.</span></button></div>`, "Find gaps with AI");
        $("#dialog-ok").hidden = true;
        $("#coverage-specific")?.addEventListener("click", () => $("#dialog").close("cancel"));
        const choice = await choicePromise;
        $("#dialog-ok").hidden = false;
        if (!choice) return;
        const values = await formDialog("Improve coverage with AI", `<p>AI inspects your code and tests, ranks gaps, and creates a coverage job aiming for 100% test coverage.</p>
          <fieldset class="field"><legend>After the analysis</legend>
            <label class="check"><input type="radio" name="execution" value="automatic" checked><span>Add tests automatically<small>Plan, implement and measure the result.</small></span></label>
            <label class="check"><input type="radio" name="execution" value="review"><span>Review advice first<small>Stop at the plan. Start implementation from the job when ready.</small></span></label></fieldset>
          <label class="check"><input type="checkbox" name="free"><span>Use free AI only</span></label>`, "Create coverage job");
        if (!values) return;
        try { await runAction("new_job", coverageJobParams(data, caseView, values)); } catch (e) { toast(e.message, true); }
      });
    },
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
  const owner = state.you?.role !== "member";
  const destination = `${g.sync_remote}/${(g.sync_ref || "").replace(/^refs\/heads\//, "")}`;
  const branchLabel = !g.is_repo ? "Not initialized" : !g.has_commits ? "No commits yet" : g.branch || "Detached HEAD";
  const sync = g.upstream ? (g.ahead == null ? `Tracking ${g.upstream} · Fetch to check status` : `${g.ahead} to push · ${g.behind} to pull`)
    : g.can_fetch ? "No tracking branch" : "No remote connected";
  const blockers = [...new Set([!g.can_pull && g.pull_reason, !g.can_push && g.push_reason].filter(Boolean))];
  const row = (label, value, action = "") => `<div class="git-setting-row"><dt>${label}</dt><dd><div class="git-setting-value">${value}</div>${action ? `<div class="git-setting-action">${action}</div>` : ""}</dd></div>`;
  const editButton = (id, label, title) => owner ? `<button type="button" class="btn small ghost" id="${id}" title="${title}">${label}</button>` : "";
  const workflow = g.mode === "github" ? "GitHub issues and pull requests" : "Local job records and branches";
  const workflowDetail = g.mode === "github" ? "Approved work is merged on GitHub." : "Approved work is merged on this computer.";
  const reviewLinks = /GitLab|Bitbucket/.test(g.provider || "") ? " Finished branches include a link to create a review request on your host." : "";
  const branchCard = `<section class="card">
    <div class="card-h git-sync-header"><h2>Branch and sync</h2><div class="card-actions">
      <button type="button" class="btn small ghost" id="git-pull" aria-describedby="git-sync-reason" ${!g.can_pull ? "disabled" : ""}>Pull</button>
      <button type="button" class="btn small ghost" id="git-push" aria-describedby="git-sync-reason" ${!g.can_push ? "disabled" : ""}>Push</button>
    </div></div>
    <div class="card-caption git-sync-caption">${esc(sync)}</div>
    <div class="card-b git-branch-fields">
      <div class="git-branch-field">
        <label class="field" for="git-branch-select"><span>Current branch</span></label>
        <div class="git-branch-controls"><select id="git-branch-select" name="branch" class="mono" ${!g.branches.length ? "disabled" : ""}>
          ${g.branches.map(b => branchOption(b, g.branch, g.elsewhere)).join("")}
        </select><button type="button" class="btn small ghost" ${act("git_new_branch")} ${!g.is_repo ? "disabled" : ""}>New branch…</button></div>
      </div>
      ${g.has_commits && g.branch && (g.can_fetch || g.remote_branches?.length) ? `<div class="git-branch-field"><span class="git-field-label">Tracking branch</span><div class="git-tracking-value"><span class="mono">${esc(g.upstream || "Not set")}</span>
        ${owner ? `<button type="button" class="btn small ghost" id="git-tracking" title="Choose tracking branch" ${!g.remote_branches?.length ? 'disabled aria-describedby="git-tracking-hint"' : ""}>${g.upstream ? "Change" : "Choose"}</button>` : ""}</div>
        ${!g.remote_branches?.length ? '<small class="muted" id="git-tracking-hint">Fetch updates to list remote branches.</small>' : ""}</div>` : ""}
    </div>
    <div class="git-sync-note" id="git-sync-reason" ${!blockers.length ? "hidden" : ""}>${blockers.map(reason => `<p>${esc(reason)}</p>`).join("")}</div>
    <div class="git-fetch-row"><span class="muted">${g.last_fetch ? `Last fetched ${esc(ago(g.last_fetch))} · cached remote status` : "Remote status uses cached branches."}</span>
      <button type="button" class="btn small ghost" id="git-fetch" ${!g.can_fetch ? "disabled" : ""}>Fetch updates</button></div>
    ${g.last_commit ? `<div class="card-caption git-commit-caption"><span>Last commit</span><span>${clamped(g.last_commit, 100)}</span></div>` : ""}
  </section>`;
  const changesCard = `<section class="card">
    <div class="card-h"><h2>Uncommitted changes <span class="count">${g.changes_total}</span></h2></div>
    ${(g.change_groups || []).map(group => `<section class="git-change-group">
      <h3>${esc(group.label)} <span class="count">${group.changes.length}</span></h3>
      <div class="diff-files">${group.changes.map(c => `<details class="diff-file" data-git-diff="${esc(c.path)}" data-kind="${esc(group.id)}">
        <summary><span class="pill">${esc(c.label)}</span> <span class="mono">${c.old_path ? `${esc(c.old_path)} → ` : ""}${esc(c.path)}</span></summary>
        <pre class="diff" aria-live="polite">Loading…</pre></details>`).join("")}</div>
    </section>`).join("") || `<div class="empty">${g.is_repo ? "Working tree clean." : "Initialize Git to inspect changes."}</div>`}
    ${g.changes_total > g.changes.length ? `<div class="card-caption muted">Showing the first ${g.changes.length} changed files of ${g.changes_total}.</div>` : ""}
  </section>`;
  const health = g.health || {};
  const checked = health.checked;
  const checks = checked?.checks || [];
  const labels = checked?.labels || [];
  const missingLabels = labels.filter(label => !label.present);
  const publishing = health.publishing || {problems: [], recent: []};
  const problems = checks.filter(check => check.status === "blocked");
  const needsSignIn = g.mode === "github" && !g.github?.user;
  const verified = checks.some(check => check.id === "read" && check.status === "ok") && !problems.length;
  const connectionLabel = !g.is_repo || !g.has_commits ? "Setup needed" : !g.origin && g.mode === "git" ? "Local Git ready"
    : !g.origin ? "Connect a GitHub repository" : needsSignIn ? "GitHub sign-in needed" : problems.length ? "Needs attention" : verified ? "Read access verified" : g.health?.checking_task ? "Gathering status…" : "Status unavailable";
  const statusText = status => ({ok: "Verified", blocked: "Needs attention", unknown: "Not verified", per_job: "Per pull request", local: "Local only"}[status] || "Not checked");
  const external = (url, label) => `<a class="btn small ghost" href="${esc(url)}" target="_blank" rel="noopener">${label} ↗</a>`;
  const problemCopy = check => {
    const detail = String(check.detail || "").toLowerCase();
    if (check.id === "read") return ["Remote unavailable", "Check the URL, network, and Git credentials or SSH key."];
    if (check.id === "github") return ["GitHub access failed", "Sign in with GitHub CLI and grant repository access."];
    if (check.id === "publish" && detail.includes("archived")) return ["Repository is archived", "Unarchive it in GitHub settings or connect another repository."];
    if (check.id === "publish") return ["Write access needed", "Ask a repository owner to grant this account write access."];
    if (check.id === "issues" && detail.includes("enable issues")) return ["Issues are disabled", "Enable Issues in GitHub repository settings."];
    if (check.id === "issues") return ["GitHub job access needed", "Restore write access and sign in to GitHub CLI."];
    return [check.label || "Repository setup issue", check.remedy || "Review access and repository settings on your Git host."];
  };
  const connectionCard = `<section class="card" id="git-connection-card">
    <div class="card-h"><h2>Connection</h2></div>
    <div class="card-caption git-provider-caption"><span class="pill ${verified || connectionLabel === "Local Git ready" ? "done" : ""}">${esc(connectionLabel)}</span><span class="muted" id="git-check-status" role="status">${g.health?.checking_task ? "Checking Git and host access…" : checked?.checked_at ? `Status updated ${esc(ago(checked.checked_at))}` : g.is_repo ? "Gathering Git and host access…" : ""}</span></div>
    ${!g.is_repo ? `<div class="git-setup-message"><p>This folder is not a Git repository yet.</p>${owner ? '<button type="button" class="btn small primary" id="git-init">Initialize repository</button>' : ""}</div>` : !g.has_commits ? '<p class="git-setup-message">Create a first commit before publishing. Creating a GitHub repository also creates the initial commit.</p>' : ""}
    <dl class="git-settings-list">
      ${row("Repository", `<span>${esc(g.provider)}</span>${g.origin ? `<small class="mono git-remote-url">${esc(g.origin)}</small>` : '<small>No origin remote. Jobs keep their work on this computer.</small>'}`, g.origin ? editButton("git-connect", "Change", "Change remote origin") : "")}
      ${g.mode === "github" ? row("GitHub account", g.github?.user ? `<span>${esc(g.github.user)}</span>` : g.github?.installed ? "Not signed in on this computer" : 'GitHub CLI is not installed. Install with <code>brew install gh</code>, then refresh.', !g.github?.user && g.github?.installed && owner ? '<button type="button" class="btn small primary" id="git-signin">Sign in to GitHub</button>' : "") : ""}
    </dl>
    ${g.web_url ? `<div class="card-caption git-host-links">${external(g.web_url, `Open on ${g.provider === "Git remote" ? "your Git host" : esc(g.provider)}`)}</div>` : ""}
    ${checks.length ? `<details class="git-access-details"><summary>Access details</summary><dl class="git-settings-list">${checks.map(check => row(esc(check.label), `<span>${statusText(check.status)}</span><small>${clamped(check.detail, 260)}</small>`)).join("")}</dl></details>` : ""}
    ${owner && !g.origin ? `<div class="git-connection-options"><h3>Where to keep your code</h3><dl class="git-settings-list">
      ${row("Existing repository", '<small>Connect GitHub or other Git hosts using a repository URL.</small>', `<button type="button" class="btn small ${g.is_repo && !needsSignIn ? "primary" : ""}" id="git-connect" title="Connect existing repository" ${!g.is_repo ? "disabled" : ""}>Connect</button>`)}
      ${row("New GitHub repository", '<small>Create a repository with issues and pull requests.</small>', '<button type="button" class="btn small ghost" id="git-create" title="Create GitHub repository">Create</button>')}
      ${row("On this computer", '<small>Run jobs locally and connect a remote later.</small>', `<button type="button" class="btn small ghost" id="git-local" title="Keep work on this computer" ${!g.is_repo ? "disabled" : ""}>Use local</button>`)}
    </dl></div>` : ""}
    ${!owner ? '<p class="card-caption muted">The computer owner can change connection settings and check access.</p>' : ""}
  </section>`;
  const baseBranchOptions = g.branches.map(branch => `<option value="${esc(branch)}" ${branch === g.base_branch ? "selected" : ""}>${esc(branch)}</option>`).join("")
    || `<option value="" selected disabled>No local branches</option>`;
  const problemCount = problems.length + (missingLabels.length ? 1 : 0) + publishing.problems.length;
  const problemCard = problemCount ? `<section class="card git-problems-card" id="git-problems-card"><div class="card-h"><h2>Needs attention <span class="count">${problemCount}</span></h2><span class="git-problems-summary">Fix these to restore Git integration.</span></div><dl class="git-settings-list">
    ${problems.map(check => { const [issue, remedy] = problemCopy(check); return row(esc(issue), `<span>${esc(remedy)}</span>`, check.id === "issues" && g.web_url ? external(`${g.web_url}/settings`, "Settings") : ""); }).join("")}
    ${missingLabels.length ? row("Job labels missing", `<span>${missingLabels.length} labels</span><small>Create only missing labels. Custom labels stay unchanged.</small>`, owner ? '<button type="button" class="btn small ghost" id="git-repair-labels">Create missing</button>' : "") : ""}
    ${publishing.problems.map(job => `<div class="git-job-row">${row(esc(job.title), `<span>${clamped(job.detail, 120)}</span><small>Review the job result, then retry.</small>`, `<div class="card-actions"><a class="btn small ghost" href="#/jobs/${encodeURIComponent(job.id)}">Open job</a><button type="button" class="btn small ghost" data-git-publish="${esc(job.id)}" ${!g.origin ? "disabled" : ""}>Retry</button></div>`)}</div>`).join("")}
  </dl></section>` : "";
  const configuration = health.configuration || {};
  const integrationCard = g.is_repo ? `<section class="card"><div class="card-h"><h2>Job integration</h2></div><dl class="git-settings-list">
    ${row("Job workflow", `<span>${workflow}</span><small>${workflowDetail}${reviewLinks}</small>`, editButton("git-workflow", "Change", "Change job workflow"))}
    ${row("Base branch", `<form id="git-base-branch-form" class="git-base-branch-form"><div class="git-base-branch-controls"><label class="sr-only" for="git-base-branch">Base branch for jobs</label><select id="git-base-branch" name="branch" aria-label="Base branch for jobs" ${!g.branches.length ? "disabled" : ""}>${baseBranchOptions}</select><button type="submit" class="btn small ghost" ${!g.branches.length ? "disabled" : ""}>Save</button></div><small>Jobs compare against and merge into this branch.</small></form>`)}
    ${row("Publishing", `<span>${g.mode === "github" ? "Finished work becomes a draft pull request." : g.origin ? "Finished job branches are pushed to origin." : "Finished work stays on local job branches."}</span><small>Review, revisions and completion stay on each job’s page.</small>`)}
    ${row("Project configuration", `<span>${esc(configuration.detail || "Fetch remote branches to compare committed configuration.")}</span><small>Compared with the last fetched base branch; changes are not automatically uploaded.</small>`, configuration.url ? external(configuration.url, "View committed file") : "")}
  </dl>
  ${g.mode === "github" ? `<details class="git-label-details"><summary>Managed job labels <span class="muted">${labels.length ? `${labels.length - missingLabels.length}/${labels.length} present` : "Not checked"}</span></summary>
    <p class="git-section-note">Orchestrator updates its generated status labels as jobs progress. Keep your own labels; manage names, colors and descriptions on GitHub.</p>
    ${labels.length ? `<dl class="git-settings-list">${labels.map(label => row(`<span class="mono">${esc(label.name)}</span>`, `<span>${esc(label.description)}</span><small>${label.present ? "Present" : "Missing"}</small>`)).join("")}</dl>` : '<p class="git-section-note">Job labels appear here after repository status is gathered.</p>'}
    ${health.labels_url ? `<div class="card-caption git-host-links">${external(health.labels_url, "Manage labels on GitHub")}</div>` : ""}
  </details>` : ""}
  ${publishing.recent.length ? `<details class="git-publishing-details"><summary>Recent job publishing</summary><dl class="git-settings-list">${publishing.recent.map(job => `<div class="git-job-row">${row(esc(job.title), `<span>${esc(job.detail)}</span>${job.last_published_at ? `<small>Published ${esc(ago(job.last_published_at))}</small>` : ""}`, `<div class="card-actions"><a class="btn small ghost" href="#/jobs/${encodeURIComponent(job.id)}">Open job</a>${job.pr_url ? external(job.pr_url, "Pull request") : job.issue_url ? external(job.issue_url, "Issue") : ""}</div>`)}</div>`).join("")}</dl></details>` : ""}
  ${(health.links || []).some(link => link.label !== "Repository") ? `<div class="card-caption git-host-links" aria-label="GitHub repository pages">${health.links.filter(link => link.label !== "Repository").map(link => external(link.url, esc(link.label))).join("")}</div>` : ""}
  </section>` : "";
  const advancedCard = g.is_repo ? `<section class="card"><details class="git-advanced-tools"><summary class="card-h"><h2>Advanced Git tools</h2><span class="git-disclosure" aria-hidden="true">⌄</span></summary><div class="git-advanced-content">
    <p class="git-section-note">Jobs manage their own branches. Use these tools for manual maintenance of this computer’s checkout.</p>${branchCard}${changesCard}
  </div></details></section>` : "";
  return {
    title: "Git setup",
    sub: esc(!g.is_repo || !g.has_commits ? branchLabel : `${g.provider} · ${g.mode === "github" ? "GitHub workflow" : "Git workflow"}`),
    html: `<div class="git-page">${problemCard}${connectionCard}${integrationCard}${advancedCard}</div>`,
    after: () => {
      if (g.health?.checking_task) {
        waitForTask({task: g.health.checking_task}).then(async () => {
          if (location.hash.startsWith("#/git")) { await refreshState(); await route(); }
        }).catch(error => {
          const status = $("#git-check-status");
          if (status) status.textContent = `Status could not be gathered: ${error.message}`;
          const pill = $("#git-connection-card .pill");
          if (pill) pill.textContent = "Status unavailable";
        });
      }
      const mutate = async (button, path, body, message) => {
        button.disabled = true;
        try {
          const started = await api(path, {method: "POST", body});
          const result = started.task ? await waitForTask(started) : started;
          toast(result.detail || message);
          await refreshState();
          await loadSetup(true);
          await route();
        } catch (e) { toast(e.message, true); }
        finally { if (button.isConnected) button.disabled = false; }
      };
      $("#git-base-branch-form")?.addEventListener("submit", (event) => {
        event.preventDefault();
        const branch = event.currentTarget.elements.branch.value;
        return mutate(event.submitter, "config/base-branch", {branch}, "Base branch saved");
      });
      const modeOptions = `<option value="auto" ${g.code_host === "auto" ? "selected" : ""}>Automatic — follow the remote host</option>
        <option value="github" ${g.code_host === "github" ? "selected" : ""}>GitHub issues and pull requests</option>
        <option value="git" ${g.code_host === "git" ? "selected" : ""}>Git branches and local job records</option>`;
      $("#git-init")?.addEventListener("click", async (e) => {
        const button = e.currentTarget;
        if (await formDialog("Initialize repository", "<p>Create a Git repository here and include current project files in the initial commit, using this computer’s Git identity.</p>", "Initialize")) await mutate(button, "setup/git-init", {}, "Git repository initialized");
      });
      $("#git-tracking")?.addEventListener("click", async (e) => {
        const button = e.currentTarget;
        const values = await formDialog("Choose tracking branch", `<label class="field"><span>Remote branch to pull from and push to</span><select name="upstream" required>${(g.remote_branches || []).map(b => `<option value="${esc(b)}" ${b === g.upstream ? "selected" : ""}>${esc(b)}</option>`).join("")}</select></label><p>This changes tracking for the current branch. It does not switch branches or change files.</p>`, "Set tracking branch");
        if (values) await mutate(button, "git/tracking", values, "Tracking branch saved");
      });
      $("#git-fetch")?.addEventListener("click", (e) => mutate(e.currentTarget, "git/fetch", {}, "Remote branches updated"));
      $("#git-signin")?.addEventListener("click", () => signInToGitHub());
      $("#git-connect")?.addEventListener("click", async (e) => {
        const button = e.currentTarget;
        const values = await formDialog(g.origin ? "Change remote origin" : "Connect existing repository", `
          <p>Connect GitHub, GitLab, Bitbucket, Azure DevOps, Gitea or another Git host using its clone URL.</p>
          ${g.origin ? `<p>Future fetches and pushes to origin will use the new address. Current address: <span class="mono">${esc(g.origin)}</span></p>` : ""}
          <label class="field"><span>Repository URL or local repository path</span><input name="url" required value="${esc(g.origin)}" placeholder="git@gitlab.com:team/app.git" autocomplete="off" spellcheck="false"></label>
          <label class="field"><span>Job workflow</span><select name="mode">${modeOptions}</select></label>
          <p class="muted">GitHub mode creates issues and pull requests. Git mode pushes branches and merges locally. Use your computer’s Git credential helper or SSH key for access.</p>`, "Save connection");
        if (values) await mutate(button, "git/connection", {op: "connect", ...values}, "Repository connected");
      });
      $("#git-workflow")?.addEventListener("click", async (e) => {
        const button = e.currentTarget;
        const values = await formDialog("Change job workflow", `<label class="field"><span>Project job workflow</span><select name="mode">${modeOptions}</select></label><p>GitHub mode uses issues and pull requests. Git mode uses local job records and merges on this computer. This setting applies when jobs next run, including queued or resumed jobs. Finish active jobs before changing it.</p>`, "Save workflow");
        if (values) await mutate(button, "git/connection", {op: "workflow", ...values}, "Job workflow saved");
      });
      $("#git-local")?.addEventListener("click", (e) => mutate(e.currentTarget, "git/connection", {op: "workflow", mode: "git"}, "Local Git workflow selected"));
      $("#git-repair-labels")?.addEventListener("click", async e => {
        const button = e.currentTarget;
        if (await formDialog("Create missing job labels", "<p>Create only the missing labels Orchestrator uses. Existing labels, custom labels, colors and descriptions will be preserved.</p>", "Create missing labels")) await mutate(button, "git/labels/repair", {}, "Missing job labels created");
      });
      view.querySelectorAll("[data-git-publish]").forEach(button => button.addEventListener("click", async () => {
        if (await formDialog("Retry job publishing", "<p>Publish this job’s branch for review. GitHub jobs open or reuse a draft pull request. This does not rerun the build, change the current branch or merge work.</p>", "Retry publishing")) await mutate(button, `jobs/${encodeURIComponent(button.dataset.gitPublish)}/publish`, {}, "Job published");
      }));
      $("#git-create")?.addEventListener("click", async (e) => {
        const button = e.currentTarget;
        const dialog = formDialog("Create GitHub repository", `
          <p>Create a repository on GitHub, connect origin and push this project. If there are no commits, project files are included in the first commit.</p>
          <label class="field"><span>Repository name</span><input name="name" value="${esc(state.project?.name || "")}" maxlength="80" required></label>
          <label class="field"><span>Visibility</span><select name="visibility"><option value="private">Private</option><option value="public">Public</option></select></label>
          <p class="muted">The GitHub CLI must be installed and signed in on this computer. <button type="button" class="btn small" id="git-create-signin">Sign in to GitHub</button></p>`, "Create repository");
        $("#git-create-signin")?.addEventListener("click", () => { $("#dialog").close("cancel"); signInToGitHub(); });
        const values = await dialog;
        if (values) await mutate(button, "setup/github-create", values, "GitHub repository created");
      });
      $("#git-pull")?.addEventListener("click", async () => {
        if (await formDialog("Pull branch", `<p>Update <strong>${esc(g.branch || "the current branch")}</strong> from <strong>${esc(destination)}</strong>. This stops if the branches have diverged.</p><p class="muted">${esc(sync)}</p>`, "Pull", { compact: true })) await runAction("git_pull", {}, { skipConfirm: true });
      });
      $("#git-push")?.addEventListener("click", async () => {
        if (await formDialog("Push branch", `<p>Upload commits from <strong>${esc(g.branch || "the current branch")}</strong> to <strong>${esc(destination)}</strong>${g.upstream ? "." : " and set its tracking branch."}</p><p class="muted">${esc(sync)}</p>`, "Push", { compact: true })) await runAction("git_push", {}, { skipConfirm: true });
      });
      view.querySelectorAll("[data-git-diff]").forEach(box => box.addEventListener("toggle", async () => {
        if (!box.open || box.dataset.loaded) return;
        box.dataset.loaded = "1";
        const pre = box.querySelector("pre");
        try {
          const d = await api(`git/diff?path=${encodeURIComponent(box.dataset.gitDiff)}&kind=${encodeURIComponent(box.dataset.kind)}`);
          pre.innerHTML = d.binary ? "Binary file: no text diff." : !d.diff.trim() ? "No changes to show." : d.diff.split("\n").map(line => {
            const kind = line.startsWith("@@") ? "hunk" : line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : "";
            return `<span class="diff-line ${kind}">${esc(line)}</span>`;
          }).join("") + (d.truncated ? '<span class="diff-line hunk">… diff truncated</span>' : "");
        } catch (error) { pre.textContent = error.message; box.dataset.loaded = ""; }
      }));
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
  if (dlg) dlg.tabIndex = -1;
  dlg.returnValue = "";
  dlg.showModal();
  const firstInput = $("#dialog-body input, #dialog-body select");
  if (firstInput) {
    firstInput.focus();
  } else {
    if (document.activeElement === $("#dialog-close")) {
      $("#dialog-close")?.blur?.();
    }
    dlg?.focus?.({ preventScroll: true });
  }

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
            if (button.hasAttribute("data-current")) { await signOut(); return; }
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
  if (section === "models" || section === "ai") {
    return {
      ...result,
      after: () => {
        const form = $("#config-models-form");
        if (form) {
          const submitBtn = form.querySelector('button[type="submit"]');
          if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.title = "No changes were made";
          }

          const getValues = () => {
            const vals = {};
            form.querySelectorAll("select[name]").forEach((sel) => {
              vals[sel.name] = sel.value;
            });
            return vals;
          };
          const initialValues = getValues();

          const updateDirty = () => {
            const current = getValues();
            let dirty = false;
            for (const [k, v] of Object.entries(current)) {
              if (v !== initialValues[k]) {
                dirty = true;
                break;
              }
            }
            if (submitBtn) {
              submitBtn.disabled = !dirty;
              if (dirty) {
                submitBtn.removeAttribute("title");
              } else {
                submitBtn.title = "No changes were made";
              }
            }
          };

          form.addEventListener("change", updateDirty);
          form.addEventListener("input", updateDirty);
          cleanup.push(() => {
            form.removeEventListener("change", updateDirty);
            form.removeEventListener("input", updateDirty);
          });
        }
        const onSubmit = async (event) => {
          event.preventDefault();
          const button = event.submitter || form?.querySelector('button[type="submit"]');
          if (!button || button.disabled) return;
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
        if (section === "ai") {
          const onInstall = async (event) => {
            const button = event.target.closest("[data-ai-install]");
            if (!button || button.disabled) return;
            const {aiInstall: provider, aiName: name} = button.dataset;
            const confirmed = await formDialog(`Install ${name}?`, `<p>This downloads and runs the official ${esc(name)} installer on the computer running Orchestrator. It may add software to this computer; it does not install anything on secondary computers.</p>`, "Install");
            if (!confirmed) return;
            button.disabled = true;
            button.textContent = "Installing…";
            try {
              const started = await api("ai-providers/install", {method: "POST", body: {provider}});
              const result = await waitForTask(started);
              toast(result.detail || `${result.provider} installed`);
              await route();
            } catch (error) {
              toast(error.message, true);
              if (button.isConnected) button.textContent = "Try again";
            } finally {
              if (button.isConnected) button.disabled = false;
            }
          };
          view.addEventListener("click", onInstall);
          cleanup.push(() => view.removeEventListener("click", onInstall));

          const refreshButton = $("#ai-refresh-machines");
          const machineBox = $("#ai-secondary-computers");
          if (refreshButton && machineBox) {
            const paintMachines = (report) => {
              machineBox.innerHTML = (report.machines || []).map((machine) => {
                if (!machine.reachable) return `<div class="ai-machine-status is-unreachable"><strong>${esc(machine.name)}</strong><span class="pill attention">Unavailable</span><span class="muted">Can't check this computer right now.</span></div>`;
                const providers = machine.providers || {};
                const rows = (config.ai?.providers || []).map((provider) => {
                  const status = providers[provider.id];
                  const label = !status ? "Status unavailable" : status.ready ? "Ready" : status.installed ? "Needs sign-in" : "Not installed";
                  return `<li><strong>${esc(provider.name || provider.id)}</strong> · ${esc(label)}</li>`;
                }).join("");
                const known = (config.ai?.providers || []).filter((provider) => providers[provider.id]);
                const missing = known.filter((provider) => !providers[provider.id].ready);
                const unknown = (config.ai?.providers || []).length - known.length;
                const ready = (config.ai?.providers || []).length - missing.length;
                const badge = unknown ? `<span class="pill attention">Status incomplete</span>` : missing.length ? `<span class="pill attention">${ready}/${(config.ai?.providers || []).length} ready</span>` : `<span class="pill done">Ready</span>`;
                const detail = [missing.length ? `Needs setup: ${missing.map((provider) => provider.name).join(", ")}` : "All listed tools are ready.", unknown ? `${unknown} tool status${unknown === 1 ? "" : "es"} unavailable.` : ""].filter(Boolean).join(" ");
                return `<div class="ai-machine-status"><details><summary><strong>${esc(machine.name)}</strong>${badge}</summary><p class="muted">${esc(detail)}</p><ul class="ai-machine-provider-list">${rows}</ul></details></div>`;
              }).join("") || `<p class="muted ai-empty-group">No enabled secondary computers.</p>`;
            };
            const refreshMachines = async () => {
              refreshButton.disabled = true;
              machineBox.innerHTML = `<div class="ai-machine-status"><span class="muted">Checking secondary computers…</span></div>`;
              try {
                const started = await api("ai-providers/check-machines", {method: "POST", body: {}});
                paintMachines(await waitForTask(started));
              } catch (error) {
                machineBox.innerHTML = `<div class="ai-machine-status is-unreachable"><span class="muted">${esc(error.message)}</span></div>`;
              } finally {
                if (refreshButton.isConnected) refreshButton.disabled = false;
              }
            };
            refreshButton.addEventListener("click", refreshMachines);
            cleanup.push(() => refreshButton.removeEventListener("click", refreshMachines));
            refreshMachines();
          }
        }
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
              `<label class="field"><span>Prompt Instructions (.md)</span><small>Starts from the built-in instructions. Edit them, or use Reset to go back.</small>
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
  return result;
};

// ---------------------------------------------------------------- connections (Jira, Trello, Sentry, Figma)

const PROVIDER_LABEL = { jira: "Jira", trello: "Trello", sentry: "Sentry", figma: "Figma" };
const PROVIDER_HINT = {
  jira: "Search tickets, or paste a key like ABC-123 or a link",
  trello: "Search cards, or paste a card link",
  sentry: "Search issues, or paste an issue link",
  figma: "Paste a link to a file or frame (right-click a frame → Copy link)",
};

const PROVIDER_ICONS = {
  figma: `<svg class="brand-icon" viewBox="0 0 38 57" width="14" height="14" fill="none" aria-hidden="true"><path d="M19 28.5a9.5 9.5 0 1 1 19 0 9.5 9.5 0 0 1-19 0z" fill="#1ABCFE"/><path d="M0 47.5a9.5 9.5 0 0 1 9.5-9.5H19v9.5a9.5 9.5 0 1 1-19 0z" fill="#0ACF83"/><path d="M19 0v19h9.5a9.5 9.5 0 1 0 0-19H19z" fill="#FF7262"/><path d="M0 9.5a9.5 9.5 0 0 0 9.5 9.5H19V0H9.5A9.5 9.5 0 0 0 0 9.5z" fill="#F24E1E"/><path d="M0 28.5a9.5 9.5 0 0 0 9.5 9.5H19V19H9.5A9.5 9.5 0 0 0 0 28.5z" fill="#A259FF"/></svg>`,
  jira: `<svg class="brand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><path fill="#0052CC" d="M11.5 2.5a9.5 9.5 0 0 0-9.5 9.5h9.5V2.5z"/><path fill="#2684FF" d="M12.5 12a9.5 9.5 0 0 0 9.5-9.5H12.5V12z"/><path fill="#0052CC" d="M11.5 12H2a9.5 9.5 0 0 0 9.5 9.5V12z"/></svg>`,
  trello: `<svg class="brand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><rect width="24" height="24" rx="4" fill="#0079BF"/><rect x="4.5" y="4.5" width="6" height="11" rx="1.5" fill="#ffffff"/><rect x="13.5" y="4.5" width="6" height="7.5" rx="1.5" fill="#ffffff"/></svg>`,
  sentry: `<svg class="brand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><path fill="#E1567C" d="M13.2 2.2a1.5 1.5 0 0 0-2.4 0L1.8 19.3a1.5 1.5 0 0 0 1.2 2.3h18a1.5 1.5 0 0 0 1.2-2.3L13.2 2.2zm-1.2 6.3 4.2 8.7H7.8L12 8.5z"/></svg>`,
  mixpanel: `<svg class="brand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><rect width="24" height="24" rx="4" fill="#7856FF"/><path d="M6 16.5V7.5L10 13.5L14 7.5V16.5H12V11L10 14L8 11V16.5H6Z" fill="#ffffff"/></svg>`,
  amplitude: `<svg class="brand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><rect width="24" height="24" rx="4" fill="#1E61F0"/><path d="M7 17L12 7L17 17H14.5L12 11.5L9.5 17H7Z" fill="#ffffff"/></svg>`,
  posthog: `<svg class="brand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><rect width="24" height="24" rx="4" fill="#F54E00"/><circle cx="9" cy="12" r="3" fill="#ffffff"/><circle cx="15" cy="12" r="3" fill="#ffffff"/></svg>`,
  analytics: `<svg class="brand-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" aria-hidden="true"><path d="M3 12h4l3-8 4 16 3-8h4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
};

function providerIcon(id) {
  return PROVIDER_ICONS[String(id || "").toLowerCase()] || "";
}

// Trello's own approval page, then back to the app with the token (trello-auth.html passes it along).
function connectTrello(key) {
  const back = new URL("trello-auth.html", location.href).href;
  const q = new URLSearchParams({ key, name: "Orchestrator", scope: "read,write", expiration: "never", response_type: "token",
    callback_method: "fragment", return_url: back });
  location.href = `https://trello.com/1/authorize?${q}`;
}

async function connectFigma(clientId) {
  const back = new URL("figma-auth.html", location.href).href;
  let verifier = "", challenge = "";
  try {
    const array = new Uint8Array(32);
    crypto.getRandomValues(array);
    verifier = btoa(String.fromCharCode(...array)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const encoder = new TextEncoder();
    const data = encoder.encode(verifier);
    const hash = await crypto.subtle.digest("SHA-256", data);
    challenge = btoa(String.fromCharCode(...new Uint8Array(hash))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  } catch (e) {
    verifier = "";
    challenge = "";
  }
  const state = Math.random().toString(36).slice(2) + Date.now().toString(36);
  try {
    sessionStorage.setItem("orchestrator_figma_pkce", JSON.stringify({ verifier, challenge, state, clientId }));
  } catch (e) { /* storage blocked */ }

  const q = new URLSearchParams({
    client_id: clientId,
    redirect_uri: back,
    scope: "file_content:read,current_user:read",
    state: state,
    response_type: "code",
  });
  if (challenge) {
    q.set("code_challenge", challenge);
    q.set("code_challenge_method", "S256");
  }
  location.href = `https://www.figma.com/oauth?${q}`;
}

function connectionStatus(p) {
  if (!p.connected) return pill("", "Not connected");
  if (p.rejected) return pill("failed", "Reconnect");
  if (p.expiry?.expired) return pill("failed", "Expired");
  if (p.expiry?.soon) return pill("attention", p.expiry.days_left === 0 ? "Expires today" : `Expires in ${plural(p.expiry.days_left, "day")}`);
  return pill("done", "Connected");
}

const MOBILE_CONTROL_TOOLS = [
  {
    id: "shellfish",
    name: "Shellfish",
    badge: "iOS & iPadOS",
    blurb: "Full-featured SSH terminal and SFTP client with deep iOS Files app integration. Monitor Orchestrator runs, check live build logs, and access job worktrees from iPhone or iPad.",
    cmd: "orchestrator ui --tunnel",
    url: "https://shellfish.app",
    actionLabel: "Get Shellfish ↗",
  },
  {
    id: "shelldrop",
    name: "Shelldrop",
    badge: "iOS Share Sheet",
    blurb: "Companion drag-and-drop tool for Shellfish. Send code patches, build logs, and shell scripts directly from the iOS Share sheet to your Mac terminal sessions.",
    cmd: null,
    hintText: "Drop files & scripts via iOS Share sheet",
    url: "https://shelldrop.app",
    actionLabel: "Get Shelldrop ↗",
  },
];

const RECOMMENDED_MCPS = [
  {
    id: "superpowers",
    name: "superpowers",
    badge: "Agent skills",
    blurb: "Autonomous coding skills for disciplined test-driven development (TDD), systematic debugging, code review, and multi-step plan execution.",
    cmd: "npx -y superpowers-mcp",
  },
  {
    id: "context7",
    name: "context7",
    badge: "Live docs",
    blurb: "Real-time, version-specific library documentation lookup from Upstash Context7, eliminating stale API hallucinations in agent tasks.",
    cmd: "npx ctx7 setup",
  },
  {
    id: "openspec",
    name: "openspec",
    badge: "Spec-driven",
    blurb: "Spec-Driven Development (SDD) schema and specification tools to define, track, and validate technical contracts and proposals.",
    cmd: "npx -y @openspec/mcp",
  },
  {
    id: "github",
    name: "github",
    badge: "Source control",
    icon: `<svg class="brand-icon" style="width:18px;height:18px" aria-hidden="true"><use href="#i-github"/></svg>`,
    blurb: "Official GitHub Model Context Protocol server for inspecting repositories, issues, PR diffs, commits, and automating code reviews.",
    cmd: "npx -y @modelcontextprotocol/server-github",
  },
  {
    id: "xcodebuild",
    name: "xcodebuild",
    badge: "Build runner",
    blurb: "Xcode build and simulator automation. Build schemes, execute unit and UI test suites, and inspect build output without flaky shell scripts.",
    cmd: "npx -y xcodebuildmcp",
  },
  {
    id: "xcodediagnostics",
    name: "xcodediagnostics",
    badge: "Diagnostics",
    blurb: "Automated triage and error diagnosis for Swift compiler failures, linker errors, and Xcode build issue logs.",
    cmd: "npx -y xcodebuildmcp --diagnostics",
  },
  {
    id: "swiftlens",
    name: "swiftlens",
    badge: "Code intelligence",
    blurb: "Semantic Swift code intelligence powered by Apple's SourceKit-LSP for deep compiler-grade symbol navigation and protocol understanding.",
    cmd: "npx -y swiftlens-mcp",
  },
  {
    id: "mcp-ssh-manager",
    name: "mcp-ssh-manager",
    badge: "Fleet & SSH",
    blurb: "Manage remote SSH connections, secure command execution, and file transfer across your build fleet with host verification.",
    cmd: "npx -y mcp-ssh-manager",
  },
];

function renderRecommendedInstalls() {
  return `<div class="stack" id="recommended-installs" aria-label="Recommended installs">
    <section class="connections-section" id="connections-mobile" aria-labelledby="connections-mobile-title">
      <h2 class="connections-section-title" id="connections-mobile-title">Mobile portals</h2>
      <p class="muted mb-12">Monitor runs, review logs, and trigger tasks directly from your iPhone or iPad.</p>

      <div class="conn-grid">
        ${MOBILE_CONTROL_TOOLS.map((t) => `
          <section class="card conn-card" data-rec-tool="${esc(t.id)}">
            <div class="card-b stack">
              <div class="row align-center">
                <strong class="conn-name">${esc(t.name)}</strong>
                <span class="pill done">${esc(t.badge)}</span>
              </div>
              <div class="muted">${esc(t.blurb)}</div>
              ${t.cmd ? `<div class="setup-hint">
                <code>${esc(t.cmd)}</code>
                <button type="button" class="btn small ghost" data-setup-copy="${esc(t.cmd)}" title="Copy command">Copy</button>
              </div>` : (t.hintText ? `<div class="setup-hint"><small class="muted">${esc(t.hintText)}</small></div>` : "")}
              <div class="row mt-8">
                <a href="${esc(t.url)}" target="_blank" rel="noopener" class="btn small">${esc(t.actionLabel)}</a>
              </div>
            </div>
          </section>
        `).join("")}
      </div>
    </section>

    <section class="connections-section" id="connections-mcp" aria-labelledby="connections-mcp-title">
      <h2 class="connections-section-title" id="connections-mcp-title">Recommended MCP servers</h2>
      <p class="muted mb-12">Model Context Protocol servers connect specialized tools, build automation, and live context directly to AI models.</p>

      <div class="rec-callout">
        <div class="rec-callout-title">
          <span>Runtime requirement: Node.js (<code>npx</code>)</span>
          <span class="pill attention">Prerequisite</span>
        </div>
        <p class="muted mb-8">Most MCP servers run on-demand using <code>npx</code>. Node.js is not checked in the setup wizard, so make sure it's installed on your Mac or runner host:</p>
        <div class="setup-hint">
          <code>brew install node</code>
          <button type="button" class="btn small ghost" data-setup-copy="brew install node" title="Copy command">Copy</button>
        </div>
      </div>

      <div class="conn-grid">
        ${RECOMMENDED_MCPS.map((m) => `
          <section class="card conn-card" data-rec-mcp="${esc(m.id)}">
            <div class="card-b stack">
              <div class="row align-center">
                <strong class="conn-name">${m.icon || ""}${esc(m.name)}</strong>
                <span class="pill">${esc(m.badge)}</span>
              </div>
              <div class="muted">${esc(m.blurb)}</div>
              <div class="setup-hint">
                <code>${esc(m.cmd)}</code>
                <button type="button" class="btn small ghost" data-setup-copy="${esc(m.cmd)}" title="Copy command">Copy</button>
              </div>
            </div>
          </section>
        `).join("")}
      </div>
    </section>
  </div>`;
}

pages.connections = async (_, query) => {
  let list;
  try { list = (await api("integrations")).integrations; } catch (err) {
    if (err.status !== 404) throw err;
    return { title: "Connections", html: `<div class="notice">Connections aren't available yet. They're part of an update that hasn't reached your Orchestrator. Everything else works as usual. Check back soon.</div>` };
  }
  const owner = state.you?.role !== "member";
  const hook = owner ? (await api("config").catch(() => null))?.webhook : null;
  const analyticsData = await api("analytics").catch(() => null);
  const pluginGroups = [
    { id: "design", name: "Design", providers: ["figma"] },
    { id: "planning", name: "Planning / tracking", providers: ["jira", "trello"] },
    { id: "diagnostics", name: "Errors / diagnostics", providers: ["sentry"] },
  ];
  const knownPlugins = new Set(pluginGroups.flatMap((group) => group.providers));
  pluginGroups.push({ id: "other", name: "Other plugins", providers: list.filter((p) => !knownPlugins.has(p.id)).map((p) => p.id) });
  const integrationCard = (p) => `
      <section class="card conn-card" data-conn="${esc(p.id)}">
        <div class="card-b stack">
          <div class="row"><strong class="conn-name">${providerIcon(p.id)}${esc(p.name)}</strong>${connectionStatus(p)}</div>
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
            ${p.id === "figma" && p.authorize_key && !p.connected ? `<button class="btn small primary" data-figma-authorize="${esc(p.authorize_key)}">Connect with Figma</button>
              <button type="button" class="btn small" data-conn-connect="${esc(p.id)}">Manual token (backup)</button>`
            : p.id === "trello" && p.authorize_key && !p.connected ? `<button class="btn small primary" data-trello-authorize="${esc(p.authorize_key)}">Connect with Trello</button>
              <button type="button" class="btn small" data-conn-connect="${esc(p.id)}">Manual key &amp; token (backup)</button>`
            : `<button class="btn small ${p.rejected || p.expiry?.expired || p.expiry?.soon ? "primary" : ""}" data-conn-connect="${esc(p.id)}">${!p.connected ? "Connect" : p.rejected || p.expiry?.expired ? "Reconnect" : "Update"}</button>`}
            ${p.connected ? `<button class="btn small danger" data-conn-disconnect="${esc(p.id)}">Disconnect</button>` : ""}
          </div>
        </div></section>`;
  return {
    title: "Connections",
    sub: "Manage plugins, notifications, Git, analytics, mobile portals, and recommended MCP servers.",
    html: `<div class="connections-sections stack">
      <section class="connections-section" id="connections-plugins" aria-label="Plugins">
        ${pluginGroups.map((group) => {
          const providers = list.filter((p) => group.providers.includes(p.id));
          return providers.length ? `<section class="connections-plugin-group" id="connections-plugins-${group.id}" aria-labelledby="connections-plugins-${group.id}-title">
            <h3 class="connections-section-title" id="connections-plugins-${group.id}-title">${esc(group.name)}</h3>
            <div class="conn-grid">${providers.map(integrationCard).join("")}</div>
          </section>` : "";
        }).join("") || `<p class="muted">No plugins are available for this Orchestrator.</p>`}
        <p class="muted mt-12" data-owner-only>Credentials are saved on your computer, in this project's settings, and checked with the service before they're kept. They're never shown again.</p>
      </section>
      <section class="connections-section" id="connections-alerts" aria-labelledby="connections-alerts-title">
        <h2 class="connections-section-title" id="connections-alerts-title">Alerts / notifications</h2>
        <div class="conn-grid">
          ${hook ? ConfigurationPages.chatCard(hook) : `<div class="card"><div class="card-b muted">${owner ? "Slack alert settings are unavailable. Reload to try again." : "Slack alerts are managed by the project owner."}</div></div>`}
          <section class="card conn-card" data-owner-only><div class="card-b stack">
            <strong class="conn-name">Email alerts</strong>
            <p class="muted">Choose who receives job updates and configure the account that sends them.</p>
            <div><a class="btn small" href="#/config/email">Manage email alerts</a></div>
          </div></section>
        </div>
      </section>
      <section class="connections-section" id="connections-git" aria-labelledby="connections-git-title">
        <h2 class="connections-section-title" id="connections-git-title">Git</h2>
        <div class="conn-grid"><section class="card conn-card"><div class="card-b stack">
          <strong class="conn-name">Repository &amp; branches</strong>
          <p class="muted">Switch branches, sync commits, and review uncommitted changes.</p>
          ${state.project?.branch ? `<div class="mono conn-summary">Current branch: ${esc(state.project.branch)}</div>` : ""}
          <div><a class="btn small primary" href="#/git">Open Git setup</a></div>
        </div></section></div>
      </section>
      <section class="connections-section" id="connections-analytics" aria-labelledby="connections-analytics-title">
        <h2 class="connections-section-title" id="connections-analytics-title">Analytics</h2>
        <div class="conn-grid">${analyticsData ? analyticsConnectionCards(analyticsData, true) : `<section class="card conn-card">
        <div class="card-b stack">
          <strong class="conn-name">${providerIcon("analytics")}Product Analytics &amp; KPIs</strong>
          <div class="notice">Analytics connection status is unavailable. Try again from Measure.</div>
          <a href="#/measure" class="btn small">Manage analytics &amp; KPIs ↗</a>
        </div>
      </section>`}</div>
      </section>
      ${renderRecommendedInstalls()}
    </div>`,
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
      const section = query?.get("section");
      if (["plugins", "alerts", "git", "analytics", "mobile", "mcp"].includes(section)) {
        view.querySelector(`#connections-${section}`)?.scrollIntoView({ block: "start" });
      }
      const onClick = async (ev) => {
        const connect = ev.target.closest("[data-conn-connect]"), disc = ev.target.closest("[data-conn-disconnect]");
        if (disc) {
          const p = list.find((x) => x.id === disc.dataset.connDisconnect);
          if (!(await formDialog(`Disconnect ${p.name}?`, `<p>Saved credentials are removed. Jobs already linked keep their links.</p>`, "Disconnect"))) return;
          try { await api(`integrations/${p.id}/disconnect`, { method: "POST", body: {} }); toast(`${p.name} disconnected`); route(); } catch (e) { toast(e.message, true); }
        } else if (connect) {
          const p = list.find((x) => x.id === connect.dataset.connConnect);
          const latest = p.token_max_days ? new Date(Date.now() + p.token_max_days * 86400000).toISOString().slice(0, 10) : "";
          const figmaOptionA = p.id === "figma" ? `
            <div class="card stack mb-16 conn-auth-box">
              <strong>Option A: Log in &amp; authorize with Figma (OAuth - Default)</strong>
              <p class="muted">Authorize with 1 click in your browser using Figma OAuth:</p>
              ${!p.authorize_key ? `<label class="field"><span>Figma Client ID <span class="muted">(from figma.com/developers/apps)</span></span>
                <input type="text" name="figma_client_id" placeholder="e.g. your_figma_oauth_client_id" value="">
              </label>` : ""}
              <div><button type="button" class="btn small primary" id="figma-dialog-quick-btn">Log in &amp; authorize with Figma ↗</button></div>
              <small class="hint-text">Sign in with your Figma account (or Enterprise SSO) and approve read access.</small>
            </div>
            <div class="conn-auth-divider mb-8"><strong>Option B: Enter personal access token manually (backup)</strong></div>` : "";
          const trelloOptionA = p.id === "trello" ? `
            <div class="card stack mb-16 conn-auth-box">
              <strong>Option A: Log in &amp; authorize with Trello</strong>
              <p class="muted">Enter your API key below from <a href="https://trello.com/power-ups/admin" target="_blank" rel="noopener">Power-Up Admin ↗</a>, then click to sign in and approve in your browser with 1 click:</p>
              <div><button type="button" class="btn small primary" id="trello-dialog-quick-btn">Log in &amp; authorize with Trello ↗</button></div>
            </div>
            <div class="conn-auth-divider mb-8"><strong>Option B: Enter key &amp; token manually (backup)</strong></div>` : "";
          const jiraOptionA = p.id === "jira" ? `
            <div class="card stack mb-16 conn-auth-box">
              <strong>Option A: Log in with Atlassian</strong>
              <p class="muted">Use your active Atlassian browser session to create an API token with 1 click:</p>
              <div><a class="btn small primary" href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noopener">Log in to Atlassian &amp; create token ↗</a></div>
              <small class="hint-text">Click "Create API token", copy it, and paste it into the API token field below.</small>
            </div>
            <div class="conn-auth-divider mb-8"><strong>Option B: Enter credentials manually (backup)</strong></div>` : "";
          const genericTokenLink = (p.id !== "trello" && p.id !== "jira" && p.id !== "figma" && p.token_url) ? `<p><a class="btn small" href="${esc(p.token_url)}" target="_blank" rel="noopener">Create a token ↗</a></p>` : "";
          const dialogPromise = formDialog(`${p.connected ? "Update" : "Connect"} ${p.name}`, `
            ${figmaOptionA}
            ${trelloOptionA}
            ${jiraOptionA}
            ${genericTokenLink}
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
          const okBtn = $("#dialog-ok");
          const dialogBody = $("#dialog-body");
          const quickFigma = $("#figma-dialog-quick-btn");
          const quickTrello = $("#trello-dialog-quick-btn");

          const updateDialogConnectState = () => {
            let manualValid = true;
            if (!p.connected) {
              for (const f of (p.fields || [])) {
                const isOpt = (f.label || "").toLowerCase().includes("optional") || f.key === "host";
                if (isOpt) continue;
                const input = dialogBody?.querySelector(`[name="${CSS.escape(f.key)}"]`);
                const val = (input ? input.value : (p.suggest?.[f.key] || "")).trim();
                if (!val) {
                  manualValid = false;
                  break;
                }
              }
            }
            if (okBtn) {
              okBtn.disabled = !manualValid;
            }
            if (quickFigma) {
              const idInput = dialogBody?.querySelector("input[name='figma_client_id']");
              const cid = (idInput?.value || p.authorize_key || "").trim();
              quickFigma.disabled = !cid;
            }
            if (quickTrello) {
              const keyInput = dialogBody?.querySelector("input[name='key']");
              const k = (keyInput?.value || p.authorize_key || "").trim();
              quickTrello.disabled = !k;
            }
          };

          updateDialogConnectState();
          dialogBody?.addEventListener("input", updateDialogConnectState);
          dialogBody?.addEventListener("change", updateDialogConnectState);

          if (quickFigma) {
            quickFigma.addEventListener("click", () => {
              const idInput = $("#dialog-body input[name='figma_client_id']");
              const cid = (idInput?.value || p.authorize_key || "").trim();
              if (!cid) {
                toast("Enter your Figma Client ID above first, then click Log in & authorize", true);
                idInput?.focus();
                return;
              }
              connectFigma(cid);
            });
          }
          if (quickTrello) {
            quickTrello.addEventListener("click", () => {
              const keyInput = $("#dialog-body input[name='key']");
              const k = (keyInput?.value || p.authorize_key || "").trim();
              if (!k) {
                toast("Enter your API key below first, then click Log in & authorize", true);
                keyInput?.focus();
                return;
              }
              connectTrello(k);
            });
          }
          const v = await dialogPromise;
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
      const figmaStart = (ev) => { const b = ev.target.closest("[data-figma-authorize]"); if (b) connectFigma(b.dataset.figmaAuthorize); };
      view.addEventListener("click", figmaStart);
      cleanup.push(() => view.removeEventListener("click", figmaStart));
      // Back from Trello's approval page with a token: save it like any connection.
      let trelloToken = null;
      try { trelloToken = sessionStorage.getItem("orchestrator_trello_token"); sessionStorage.removeItem("orchestrator_trello_token"); } catch { /* storage blocked */ }
      const trello = list.find((x) => x.id === "trello");
      if (trelloToken && trello?.authorize_key) {
        api("integrations/trello/connect", { method: "POST", body: { values: { key: trello.authorize_key, token: trelloToken } } })
          .then((r) => { toast(`Trello connected${r.who ? ` as ${r.who}` : ""}`); route(); })
          .catch((e) => toast(e.message, true));
      }
      // Back from Figma's OAuth page with authorization code: exchange and save it.
      let figmaAuth = null;
      try {
        const rawFigma = sessionStorage.getItem("orchestrator_figma_auth");
        if (rawFigma) {
          figmaAuth = JSON.parse(rawFigma);
          sessionStorage.removeItem("orchestrator_figma_auth");
          sessionStorage.removeItem("orchestrator_figma_pkce");
        }
      } catch { /* storage blocked */ }
      const figma = list.find((x) => x.id === "figma");
      if (figmaAuth && figmaAuth.code) {
        const cid = figmaAuth.clientId || figma?.authorize_key || "";
        api("integrations/figma/connect", {
          method: "POST",
          body: {
            values: {
              code: figmaAuth.code,
              code_verifier: figmaAuth.verifier,
              redirect_uri: figmaAuth.redirectUri,
              client_id: cid
            }
          }
        })
          .then((r) => { toast(`Figma connected${r.who ? ` as ${r.who}` : ""}`); route(); })
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
function wireReferenceSubmit() {
  const form = $("#dialog-form"), dialog = $("#dialog"), picker = $("#dialog-body .link-picker");
  if (!picker) return;
  const onSubmit = (event) => {
    if (event.submitter !== $("#dialog-ok")) return;
    if (picker.querySelector(".lp-input").value.trim()) picker.querySelector(".lp-add").click();
    if (!JSON.parse(picker.querySelector('input[name="links"]').value || "[]").length) {
      event.preventDefault();
      toast("Choose a reference or paste a link first.", "warning");
      picker.querySelector(".lp-input").focus();
    }
  };
  form.addEventListener("submit", onSubmit);
  dialog.addEventListener("close", () => form.removeEventListener("submit", onSubmit), { once: true });
}

async function linkPickerHtml({ prefer = [], label = "Link from your apps", emptyHint = null } = {}) {
  let providers = [];
  try { providers = (await api("integrations")).integrations.filter((p) => p.connected); } catch { /* older server */ }
  if (!providers.length) {
    return { html: emptyHint ?? `<div class="muted link-picker-empty">Link a Jira ticket, Trello card, Sentry issue or Figma design: <a href="#/connections">connect an app</a>.</div>`, wire: () => {}, connected: false };
  }
  const html = `<div class="link-picker field"><span>${esc(label)} <span class="muted">(optional)</span></span>
    <div class="row"><select class="lp-provider" aria-label="App">${providers.map((p) => `<option value="${esc(p.id)}" data-search="${p.searchable}">${esc(p.name)}</option>`).join("")}</select>
      <input type="text" class="lp-input" autocomplete="off" autocapitalize="off" spellcheck="false" style="flex:1;min-width:160px"><button type="button" class="btn small lp-add">Add</button></div>
    <div class="row justify-between align-center mt-2 text-xs">
      <span class="muted">Connected apps: ${providers.map((p) => esc(p.name)).join(", ")}</span>
      <a href="#/connections" class="linklike">Connect more apps ↗</a>
    </div>
    <small class="hint-text lp-hint"></small>
    <div class="lp-results list"></div><div class="lp-chips row"></div><input type="hidden" name="links" value="[]"></div>`;
  const wire = (root) => {
    const sel = root.querySelector(".lp-provider"), input = root.querySelector(".lp-input"), results = root.querySelector(".lp-results");
    const chips = root.querySelector(".lp-chips"), hidden = root.querySelector('input[name="links"]'), hint = root.querySelector(".lp-hint");
    let picked = [], timer;
    const sync = () => {
      hidden.value = JSON.stringify(picked.map(({ provider, ref }) => ({ provider, ref })));
      chips.innerHTML = picked.map((p, i) => `<span class="chip">${providerIcon(p.provider)}${esc(PROVIDER_LABEL[p.provider] || p.provider)} · ${esc(p.title || p.ref)}<button type="button" data-lp-remove="${i}" aria-label="Remove">✕</button></span>`).join("");
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
      <label class="prd-drop upload-drop file-picker" for="attach-file" id="attach-drop">${filePickerContent("Attach a file", "", false)}</label>
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
// Describe it (a few lean questions) -> where it lives (GitHub preferred) -> create -> break it down into epics (optional).
// Progress is saved server-side, so stopping to sign in to GitHub resumes where you were.

const NP_STEPS = [["describe", "Describe it"], ["where", "Where it lives"], ["create", "Create"], ["breakdown", "Break it down"]];

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
  const ideaAnswers = idea?.pitch ? { pitch: idea.pitch.trim().slice(0, 600), name: (idea.name || "").trim().slice(0, 200) } : null;
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

  const npDesignPicker = () => `<div class="field"><span>Design references <span class="muted">(optional)</span></span>
      <input type="file" id="np-design-file" class="sr-only" multiple accept="image/*,.pdf,.fig,.html,.htm" aria-label="Upload design references">
      <label class="prd-drop upload-drop file-picker" for="np-design-file" id="np-design-drop">${filePickerContent("Add design references", "Images, a PDF, a .fig file or an HTML mockup: screens you like, a sketch, an existing design.")}</label>
      <div class="upload-chips row" id="np-design-chips" aria-live="polite"></div></div>`;

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
    // The product requirements fields are asked as the Product page names them (same title and hint, same order).
    const projectQs = data.questions.filter((q) => q.section === "project");
    const prdQs = data.questions.filter((q) => q.section !== "project");
    const sectionsHtml = `<fieldset class="np-section-card stack"><legend class="np-section-legend">Project</legend>
        ${projectQs.map((q) => npQuestion(q, draft.answers[q.key] || "")).join("")}</fieldset>
      <fieldset class="np-section-card stack"><legend class="np-section-legend">Product requirements <span class="muted">(all optional)</span></legend>
        <p class="muted">Answer what you know now. Everything here can be written or changed later on the Product page, and a detailed document can be imported there.</p>
        ${prdQs.map((q) => npQuestion(q, draft.answers[q.key] || "") + (q.key === "look" ? npDesignPicker() : "")).join("")}</fieldset>`;

    return {
      title: "Start a new project", sub: npStepper("describe"),
      html: `${resume}<form class="card card-b stack np-form" id="np-describe">
        <p class="muted">Short answers are fine. They become your product description, which you can change any time.</p>
        ${sectionsHtml}
        <div class="row">${discard}<span class="spacer"></span><button class="btn primary" type="submit">Next: where it lives</button></div></form>`,
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
        let uploading = 0;
        const chips = () => {
          const box = $("#np-design-chips"); if (!box) return;
          box.innerHTML = (draft.designs || []).map((d, i) => `<span class="chip">${esc(d.name)}<button type="button" data-np-design-remove="${i}" aria-label="Remove ${esc(d.name)}">✕</button></span>`).join("")
            + (uploading ? `<span class="muted">Uploading…</span>` : "");
        };
        chips();
        $("#np-design-chips")?.addEventListener("click", (e) => {
          const b = e.target.closest("[data-np-design-remove]"); if (!b) return;
          draft.designs.splice(Number(b.dataset.npDesignRemove), 1); chips();
          npSave({ ...draft, answers: answersFrom($("#np-describe")) }).catch((err) => toast(err.message, true));
        });
        const input = $("#np-design-file"), drop = $("#np-design-drop");
        drop?.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
        drop?.addEventListener("dragleave", () => drop.classList.remove("over"));
        drop?.addEventListener("drop", (e) => {
          e.preventDefault(); drop.classList.remove("over");
          if (e.dataTransfer?.files?.length) { input.files = e.dataTransfer.files; input.dispatchEvent(new Event("change")); }
        });
        input?.addEventListener("change", async () => {
          const files = [...input.files]; input.value = "";
          // The draft must exist before a file can be held with it; saving the answers so far also keeps them.
          try { await npSave({ ...draft, answers: answersFrom($("#np-describe")) }); } catch (err) { toast(err.message, true); return; }
          for (const f of files) {
            if (f.size > NJ_UPLOAD_LIMIT) { toast(`${f.name} is over ${NJ_UPLOAD_LIMIT / 1048576} MB`, "warning"); continue; }
            uploading += 1; chips();
            try { draft.designs = (await uploadFile(f, "new-project/design")).draft.designs; } catch (err) { toast(`${f.name}: ${err.message}`, true); }
            uploading -= 1; chips();
          }
        });
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
            <small>Backs your work up, tracks every job as an issue and pull request, and merges approved work on GitHub.</small></label>
          <label class="np-host ${host === "local" ? "on" : ""}"><input type="radio" name="host" value="local" ${host === "local" ? "checked" : ""}>
            <strong>On this computer</strong>
            <small>Jobs run using local records and branches. Connect a Git host later to publish your work.</small></label>
        </div>
        ${host === "github" ? `${ghPanel()}
          <label class="field"><span>Who can see the repository?</span><select name="visibility"><option value="private" ${draft.visibility !== "public" ? "selected" : ""}>Only me (private)</option><option value="public" ${draft.visibility === "public" ? "selected" : ""}>Anyone (public)</option></select></label>` : ""}
        <label class="field"><span>Where on this computer?</span><input type="text" name="parent" value="${esc(draft.parent)}" spellcheck="false" autocapitalize="off">
          <small class="hint-text">A folder named “${esc(answersSlug(draft))}” is created inside this one.</small></label>
        <div class="row"><button type="button" class="btn ghost" id="np-back">Back</button><span class="spacer"></span>
          ${host === "github" && !ready ? `<button type="button" class="btn" id="np-local">Use local only for now</button>` : ""}
          <button class="btn primary" type="submit" ${ready ? "" : "disabled"}>Create project</button></div>
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
  // The last, optional step: split what they described into epics. Offered once GitHub is done, and only if they described something.
  const offerBreakdown = !githubPending && result?.prd_filled !== false;
  return {
    title: result?.name ? `${result.name} is ready` : "Finish GitHub", sub: npStepper(offerBreakdown ? "breakdown" : "create"),
    html: `${resume}<section class="card"><div class="list">${steps.map((s) => `<div class="item"><span>${s.ok ? "✅" : "⚠️"}</span><div class="main-col"><div class="title">${esc(s.name)}</div><div class="meta">${esc(s.detail || "")}</div></div></div>`).join("")}</div></section>
      ${githubPending ? `<div class="card card-b stack"><strong>Your project is saved on this computer.</strong>
        <span class="muted">GitHub publishing isn't done yet. Finish it here, or use a local Git workflow from Git setup.</span>${ghPanel()}
        <div class="row"><button class="btn primary" id="np-publish" ${gh.user ? "" : "disabled"}>Create the GitHub repository</button></div></div>` : ""}
      ${result?.recommend_platform ? `<div class="card card-b stack"><strong>You asked for a platform recommendation.</strong><span class="muted">Your first job's plan will propose platforms with reasons, based on who it's for and the problem it solves.</span></div>` : ""}
      ${(result?.platform_needs || []).map((n) => `<section class="card mt-16"><div class="card-h"><h2>${esc(n.platform)}: what it needs</h2></div><div class="card-b"><ul class="assumptions">${n.needs.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div></section>`).join("")}
      ${offerBreakdown ? `<section class="card mt-16" id="np-breakdown"><div class="card-b stack"><strong>Next: break it down into epics</strong>
        <span class="muted">Optional. Orchestrator reads what you've told it and suggests the big pieces of the product, as detailed as your answers, using a story map or a top-down work breakdown. You approve the ones you want, and they become jobs that wait until you plan them.</span>
        <div class="row"><a class="btn primary big" href="#/product?start=1" data-np-breakdown>Break it down</a><a class="btn big" href="#/checkup" data-np-breakdown>Skip for now</a></div></div></section>`
        : `<div class="row mt-16"><a class="btn primary big" href="#/checkup">Set up this project</a></div>`}`,
    after: () => {
      // Either choice counts as having been asked, so the Product page doesn't ask again.
      document.querySelectorAll("[data-np-breakdown]").forEach((link) => link.addEventListener("click", () => {
        api("product/settings", { method: "POST", body: { breakdown_offered: true } }).catch(() => {});
      }));
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
  const { text, preview } = await api(`file?path=${encodeURIComponent(path)}`);
  const document = `<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'">${text}`;
  return {
    title: path.split("/").pop(),
    sub: esc(path),
    html: preview
      ? `<section class="card"><iframe title="Design preview" sandbox="" referrerpolicy="no-referrer" srcdoc="${esc(document)}" style="width:100%;min-height:60vh;border:0"></iframe><details><summary>View source</summary><pre class="file">${esc(text)}</pre></details></section>`
      : `<section class="card"><pre class="file">${esc(text)}</pre></section>`,
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
  const gitCount = pList.filter((p) => p.source_type === "git").length;
  const localCount = pList.filter((p) => !p.source_type || p.source_type === "local").length;

  const renderSourceBadge = (item) => {
    if (item.source_type === "github") {
      const label = item.github_repo || "GitHub Tracked";
      return `<span class="pill-badge github-badge" title="GitHub Remote: ${esc(item.github_repo || "Connected")}"><svg class="icon badge-icon"><use href="#i-github"/></svg>${esc(label)}</span>`;
    }
    if (item.source_type === "git") return `<span class="pill-badge local-badge" title="Connected Git remote">${esc(item.source_label || "Other Git host")}</span>`;
    return `<span class="pill-badge local-badge" title="No remote connected"><svg class="icon badge-icon"><use href="#i-folder"/></svg>On this computer</span>`;
  };

  const cardsHtml = pList.map((p) => `
    <div class="project-card ${p.active ? "active-project" : ""}" data-source="${esc(p.source_type || "local")}" role="button" tabindex="0"
      ${p.active ? 'data-href="#/"' : `data-switch-project="${esc(p.root)}" data-then="#/"`} aria-label="${p.active ? "Open" : "Switch to"} ${esc(p.name)}">
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
          <button class="btn small primary" type="button" data-switch-project="${esc(p.root)}" data-then="#/">Switch & Open</button>
          ${state.you?.role !== "member" ? `<button class="btn small ghost" type="button" data-forget-project="${esc(p.root)}" title="Remove from list">Forget</button>` : ""}
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
            <button type="button" class="btn small" data-project-filter="git">Other Git hosts (${gitCount})</button>
            <button type="button" class="btn small" data-project-filter="local"><svg class="icon badge-icon"><use href="#i-folder"/></svg>On this computer (${localCount})</button>
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
  const isStopping = stoppingRuns.has(r.id) || Boolean(r.stopping);
  return {
    title: r.title,
    sub: r.command ? `<span class="status-line"><span class="mono">${esc(r.command)}</span></span>` : "",
    actions: r.running
      ? `<button class="btn danger" data-stop="${esc(r.id)}" ${isStopping ? 'disabled aria-busy="true"' : ""}>${isStopping ? '<span class="spinner inline-spinner" aria-hidden="true"></span>Stopping…' : "Stop"}</button>`
      : "",
  };
}

function openImageModal({ title, src, authSrc, fullUrl }) {
  const dlg = $("#dialog");
  dlg.classList.remove("dialog-compact");
  dlg.classList.add("dialog-image-viewer");
  $("#dialog-cancel").hidden = true;
  $("#dialog-title").textContent = title || "Screenshot preview";
  $("#dialog-body").innerHTML = `
    <div class="image-preview-modal-body">
      <div class="image-preview-modal-frame">
        <img src="${esc(src)}" ${authSrc ? `data-auth-src="${esc(authSrc)}"` : ""} alt="${esc(title)}" class="image-preview-modal-img">
      </div>
      <div class="image-preview-modal-foot row align-center justify-between">
        <span class="mono text-xs muted">${esc(title)}</span>
        <a class="btn small ghost" href="${esc(fullUrl || src)}" target="_blank" rel="noopener">Open full size in new tab ↗</a>
      </div>
    </div>
  `;
  hydrateAuthImages($("#dialog-body")).then(() => {
    const image = $("#dialog-body .image-preview-modal-img"), link = $("#dialog-body .image-preview-modal-foot a");
    if (link && image?.src?.startsWith("blob:")) link.href = image.src;
  });
  $("#dialog-ok").textContent = "Close";
  $("#dialog-ok").className = "btn";
  dlg.addEventListener("close", () => {
    dlg.classList.remove("dialog-compact");
    dlg.classList.remove("dialog-image-viewer");
  }, { once: true });
  if (dlg) dlg.tabIndex = -1;
  if (!dlg.open) dlg.showModal();
  if (document.activeElement === $("#dialog-close")) {
    $("#dialog-close")?.blur?.();
  }
  dlg?.focus?.({ preventScroll: true });
}

function wireImagePreviews(root = document) {
  root.querySelectorAll("[data-preview-img]").forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      openImageModal({
        title: btn.dataset.previewTitle,
        src: btn.dataset.previewImg || btn.querySelector("img")?.src || "",
        authSrc: btn.dataset.previewAuth,
        fullUrl: btn.dataset.previewImg || btn.querySelector("img")?.src || "",
      });
    };
  });
}

async function getVisualCheckForRun(r) {
  try {
    const data = await api("visual-checks");
    const checks = data?.checks || [];
    if (!checks.length) return null;
    if (r.job) {
      const match = checks.find((c) => c.job === r.job);
      if (match) return match;
    }
    return checks[0];
  } catch {
    return null;
  }
}

function runDuration(started, ended) {
  const finish = (ended && Number(ended) > 0) ? Number(ended) : Date.now() / 1000;
  const seconds = Math.max(0, Math.floor(finish - started));
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
    : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}

function runFailureGuidance(r) {
  let err = (r.error_summary || r.last_line || "").trim();
  if (/^\[exited(?: with code \d+)?\]$/i.test(err)) err = "";
  const lower = err.toLowerCase();
  const fullText = (err + " " + (r.last_line || "") + " " + (r.error_summary || "") + " " + (r.title || "") + " " + (r.saved_log_text || "").slice(-5000)).toLowerCase();
  const targetJob = r.job || r.result_job;

  const isKeychain = fullText.includes("keychain")
    || fullText.includes("user interaction is not allowed")
    || fullText.includes("errsecinteractionnotallowed")
    || fullText.includes("-25308")
    || fullText.includes("seckeychainunlock")
    || fullText.includes("secauthfailed")
    || fullText.includes("-25293")
    || fullText.includes("errseckeychaindenied")
    || (fullText.includes("signing certificate") && (fullText.includes("locked") || fullText.includes("access") || fullText.includes("find")))
    || ((fullText.includes("codesign") || fullText.includes("code signing")) && (fullText.includes("keychain") || fullText.includes("interaction not allowed") || fullText.includes("locked") || fullText.includes("denied")));

  if (isKeychain) {
    const rerunBtn = targetJob
      ? `<button type="button" class="btn small primary" ${act(r.action || "execute", { job: targetJob })}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Rerun job</button>`
      : r.action ? `<button type="button" class="btn small primary" ${act(r.action)}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Rerun</button>` : "";
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : "";
    return {
      isKeychain: true,
      title: "macOS Keychain is locked",
      detail: "The build could not access developer signing certificates because the macOS login keychain is locked on this Mac.",
      action: rerunBtn,
      secondary: backBtn,
    };
  }

  const isMissingApiKey = (fullText.includes("no model is available")
    || fullText.includes("no ai provider")
    || fullText.includes("missing api key")
    || fullText.includes("api_key not found")
    || fullText.includes("invalid api key")
    || fullText.includes("credit balance is too low")
    || fullText.includes("anthropic_api_key")
    || fullText.includes("openai_api_key")
    || fullText.includes("gemini_api_key")
    || (fullText.includes("authentication failed") && (fullText.includes("anthropic") || fullText.includes("openai") || fullText.includes("gemini"))))
    && (fullText.includes("api key") || fullText.includes("key") || fullText.includes("model") || fullText.includes("provider") || fullText.includes("anthropic") || fullText.includes("openai") || fullText.includes("gemini") || fullText.includes("ollama"));

  if (isMissingApiKey) {
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : `<a class="btn small" href="#/settings">Open Settings</a>`;
    return {
      isApiKey: true,
      title: "AI API key required",
      detail: "The AI agent could not start because an AI provider API key is missing or invalid. Enter your API key below to save it locally and immediately rerun.",
      action: "",
      secondary: backBtn,
    };
  }

  const isBuildCacheLock = fullText.includes("database is locked")
    || (fullText.includes("build.db") && (fullText.includes("lock") || fullText.includes("sqlite") || fullText.includes("busy")))
    || fullText.includes("unable to open database file")
    || (fullText.includes("module cache") && (fullText.includes("corrupt") || fullText.includes("invalid") || fullText.includes("lock")))
    || fullText.includes("modulecache.noindex")
    || fullText.includes("fatal error: could not build module")
    || (fullText.includes("deriveddata") && fullText.includes("lock"));

  if (isBuildCacheLock) {
    const opAction = r.action || (targetJob ? "execute" : "build");
    const opAttr = `data-rerun-action="${esc(opAction)}"${targetJob ? ` data-rerun-params='${attrJSON({ job: targetJob })}'` : ""}`;
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : `<button type="button" class="btn small" ${act(r.action || "build")}>Retry build</button>`;
    return {
      recoveryAction: "clean-build-cache",
      title: "Xcode build database or module cache is locked",
      detail: "A previous build left a lock on the SQLite build database or Clang module cache. Cleaning the DerivedData build cache and resetting locks resolves this.",
      action: `<button type="button" class="btn small primary" data-recover="clean-build-cache" ${opAttr}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-refresh"/></svg> Clean cache &amp; Rerun</button>`,
      secondary: backBtn,
    };
  }

  const isPackageResolution = fullText.includes("resolvepackagedependencies")
    || fullText.includes("package.resolved file is corrupted")
    || (fullText.includes("package.resolved") && fullText.includes("out of date"))
    || (fullText.includes("checksum of package") && fullText.includes("does not match"))
    || fullText.includes("failed to resolve dependencies")
    || fullText.includes("could not resolve package dependencies")
    || (fullText.includes("missing package") && fullText.includes("package.swift"));

  if (isPackageResolution) {
    const opAction = r.action || (targetJob ? "execute" : "build");
    const opAttr = `data-rerun-action="${esc(opAction)}"${targetJob ? ` data-rerun-params='${attrJSON({ job: targetJob })}'` : ""}`;
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : `<button type="button" class="btn small" ${act(r.action || "build")}>Retry build</button>`;
    return {
      recoveryAction: "resolve-packages",
      title: "Swift package dependencies failed to resolve",
      detail: "Package dependencies are missing, corrupted, or out of sync with Package.resolved. Running package resolution and refreshing dependency caches will fix this.",
      action: `<button type="button" class="btn small primary" data-recover="resolve-packages" ${opAttr}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-refresh"/></svg> Resolve packages &amp; Rerun</button>`,
      secondary: backBtn,
    };
  }

  const isSimulatorFreeze = fullText.includes("unable to boot device in current state")
    || fullText.includes("coresimulator.bridge error")
    || fullText.includes("coresimulatorservice")
    || (fullText.includes("simulator") && (fullText.includes("failed to boot") || fullText.includes("booted more than once") || fullText.includes("unresponsive")))
    || (fullText.includes("failed to launch") && fullText.includes("iphonesimulator"))
    || (fullText.includes("simctl") && fullText.includes("boot") && fullText.includes("error"));

  if (isSimulatorFreeze) {
    const opAction = r.action || (targetJob ? "execute" : "test");
    const opAttr = `data-rerun-action="${esc(opAction)}"${targetJob ? ` data-rerun-params='${attrJSON({ job: targetJob })}'` : ""}`;
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : `<button type="button" class="btn small" ${act(r.action || "test")}>Retry tests</button>`;
    return {
      recoveryAction: "reset-simulator",
      title: "iOS Simulator is frozen or failed to boot",
      detail: "The CoreSimulator daemon or booted simulator device is unresponsive or locked. Terminating hung simulator daemons and shutting down devices unblocks execution.",
      action: `<button type="button" class="btn small primary" data-recover="reset-simulator" ${opAttr}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-refresh"/></svg> Reset simulator &amp; Rerun</button>`,
      secondary: backBtn,
    };
  }

  const isGitDirtyConflict = fullText.includes("your local changes to the following files would be overwritten by checkout")
    || fullText.includes("your local changes to the following files would be overwritten by merge")
    || fullText.includes("cannot be applied because you have unstaged changes")
    || fullText.includes("please commit your changes or stash them");

  if (isGitDirtyConflict) {
    const opAction = r.action || (targetJob ? "execute" : "");
    const opAttr = opAction ? `data-rerun-action="${esc(opAction)}"${targetJob ? ` data-rerun-params='${attrJSON({ job: targetJob })}'` : ""}` : "";
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : "";
    return {
      recoveryAction: "git-stash",
      title: "Uncommitted local changes in working directory",
      detail: "Git checkout or switch was blocked by unstaged local files. Stashing them saves your changes safely into git stash so the job can run.",
      action: `<button type="button" class="btn small primary" data-recover="git-stash" ${opAttr}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-refresh"/></svg> Stash changes &amp; Rerun</button>`,
      secondary: backBtn,
    };
  }

  const isWorktreeLocked = (fullText.includes("already checked out at") && fullText.includes(".git/worktrees"))
    || fullText.includes("already used by worktree at")
    || (fullText.includes("fatal: ") && fullText.includes("worktree") && fullText.includes("lock"))
    || fullText.includes("is already checked out at");

  if (isWorktreeLocked) {
    const opAction = r.action || (targetJob ? "execute" : "");
    const opAttr = opAction ? `data-rerun-action="${esc(opAction)}"${targetJob ? ` data-rerun-params='${attrJSON({ job: targetJob })}'` : ""}` : "";
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : "";
    return {
      recoveryAction: "git-prune-worktrees",
      title: "Git branch is locked by an orphaned worktree",
      detail: "A previous worker or splinter job left an active git worktree registration. Pruning orphaned worktrees clears the locks so the branch can be checked out.",
      action: `<button type="button" class="btn small primary" data-recover="git-prune-worktrees" ${opAttr}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-refresh"/></svg> Prune worktrees &amp; Rerun</button>`,
      secondary: backBtn,
    };
  }

  const isDiskExhausted = fullText.includes("no space left on device")
    || fullText.includes("enospc")
    || fullText.includes("not enough disk space")
    || (fullText.includes("disk") && fullText.includes("full"));

  if (isDiskExhausted) {
    const opAction = r.action || (targetJob ? "execute" : "");
    const opAttr = opAction ? `data-rerun-action="${esc(opAction)}"${targetJob ? ` data-rerun-params='${attrJSON({ job: targetJob })}'` : ""}` : "";
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : "";
    return {
      recoveryAction: "reclaim-space",
      title: "Disk space exhausted on Mac",
      detail: "The disk ran out of free space during compilation or artifact creation. Orchestrator can immediately reclaim space by clearing old DerivedData, SPM caches, and unavailable simulator runtimes.",
      action: `<button type="button" class="btn small primary" data-recover="reclaim-space" ${opAttr}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-trash"/></svg> Reclaim space &amp; Rerun</button>`,
      secondary: backBtn,
    };
  }

  const isGhAuth = fullText.includes("gh auth login")
    || (fullText.includes("github") && fullText.includes("authenticate") && fullText.includes("login"))
    || fullText.includes("you are not logged into any github hosts");

  if (isGhAuth) {
    return {
      isGhAuth: true,
      title: "GitHub authentication required",
      detail: "GitHub CLI is not signed in on this Mac. Sign in to GitHub or run 'gh auth login --web' in Terminal.",
      action: `<a class="btn small primary" href="#/config/integrations">Sign in to GitHub</a>`,
      secondary: `<button type="button" class="btn small ghost" data-copy="gh auth login --web">Copy gh command</button>`,
    };
  }

  if (r.action === "coverage") {
    if (!err || lower.includes("no swift source") || lower.includes("no source files") || lower.includes("package.swift") || lower.includes("initial code") || lower.includes("couldn't identify") || lower.includes("couldn't tell which language") || lower.includes("tell what language") || lower.includes("which language") || lower.includes("language the project uses") || lower.includes("no package.json") || lower.includes("no cargo.toml") || lower.includes("no go.mod") || lower.includes("no test")) {
      const onTestsPage = typeof current !== "undefined" && current?.page === "tests";
      return {
        title: "No source files or tests yet",
        detail: "Create a test job to add tests and measure code coverage.",
        action: `<a class="btn small primary" href="#/new?type=coverage"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-plus"/></svg> Add tests</a>`,
        secondary: onTestsPage ? "" : `<a class="btn small" href="#/tests">Back to Tests</a>`,
      };
    }
    if (lower.includes("tests failed") || lower.includes("failing tests")) {
      const fixParams = {
        type: "bug",
        summary: "Fix failing tests",
        spec: err ? `Fix failing tests encountered during coverage run:\n${err.slice(0, 1500)}` : "Diagnose test failures from the coverage run output and make all test suites pass."
      };
      return {
        title: "Tests failed during coverage",
        detail: "Coverage measurement requires all tests to pass to produce accurate measurements. Fix failing tests to unblock coverage, or let AI automatically diagnose the failure log and repair them.",
        action: `<button type="button" class="btn small primary" ${act("new_job", fixParams)}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-plus"/></svg> Fix tests with AI</button>`,
        secondary: `<a class="btn small" href="#/tests">View test suites</a>`,
      };
    }
    if (lower.includes("pip install coverage") || lower.includes("coverage.py")) {
      return {
        title: "coverage.py required",
        detail: "Install coverage.py with pip install coverage to measure Python coverage.",
        action: `<button type="button" class="btn small primary" ${act("coverage")}>Retry calculation</button>`,
        secondary: `<a class="btn small" href="#/tests">Back to Tests</a>`,
      };
    }
    if (lower.includes("cargo-llvm-cov") || lower.includes("cargo install cargo-llvm-cov")) {
      return {
        title: "cargo-llvm-cov required",
        detail: "Install with cargo install cargo-llvm-cov to measure Rust coverage.",
        action: `<button type="button" class="btn small primary" ${act("coverage")}>Retry calculation</button>`,
        secondary: `<a class="btn small" href="#/tests">Back to Tests</a>`,
      };
    }
    if (lower.includes("vitest") || lower.includes("jest")) {
      return {
        title: "Coverage provider required",
        detail: "Add a coverage provider package (e.g. @vitest/coverage-v8) in package.json.",
        action: `<button type="button" class="btn small primary" ${act("coverage")}>Retry calculation</button>`,
        secondary: `<a class="btn small" href="#/tests">Back to Tests</a>`,
      };
    }
    return {
      title: "Coverage measurement failed",
      detail: err || "Encountered an error while measuring test coverage.",
      action: `<button type="button" class="btn small primary" ${act("coverage")}>Retry calculation</button>`,
      secondary: `<a class="btn small" href="#/tests">Back to Tests</a>`,
    };
  }

  if (r.action === "build") {
    const fixParams = {
      type: "bug",
      summary: "Fix build compilation errors",
      spec: err ? `Fix build compilation errors:\n${err.slice(0, 1500)}` : "Diagnose and fix compilation errors from the build tool output."
    };
    return {
      title: "Build compilation failed",
      detail: err || "The compiler or build tool encountered errors. Fix the compilation errors or let AI resolve them automatically.",
      action: `<button type="button" class="btn small primary" ${act("new_job", fixParams)}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-plus"/></svg> Fix build with AI</button>`,
      secondary: `<button type="button" class="btn small" ${act("build")}>Retry build</button>`,
    };
  }

  if (r.action === "test" || r.action === "test_suite" || r.action === "test_plan") {
    const fixParams = {
      type: "bug",
      summary: "Fix failing tests",
      spec: err ? `Fix failing tests encountered during test run:\n${err.slice(0, 1500)}` : "Diagnose test failures from the test runner output and make all test suites pass."
    };
    return {
      title: "Test suite execution failed",
      detail: err ? `One or more tests in the suite failed: ${err.slice(0, 300)}. Let AI diagnose and fix them, or inspect test suites.` : "One or more tests in the suite failed. Let AI automatically diagnose the failure log and repair the tests, or inspect your test suites.",
      action: `<button type="button" class="btn small primary" ${act("new_job", fixParams)}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-plus"/></svg> Fix tests with AI</button>`,
      secondary: `<a class="btn small" href="#/tests">View test suites</a>`,
    };
  }

  if (r.action === "check_config" || r.action === "check") {
    return {
      title: "Configuration check reported issues",
      detail: err || "Orchestrator configuration or environment checks reported errors.",
      action: `<a class="btn small primary" href="#/settings">Open Settings</a>`,
      secondary: `<button type="button" class="btn small" ${act("check_config")}>Re-check config</button>`,
    };
  }

  if (r.action === "distribute" || r.action === "deliver") {
    if (lower.includes("configuration is incomplete") || lower.includes("not set") || lower.includes("configure signing") || lower.includes("development_team") || lower.includes("provisioning_profile")) {
      return {
        title: "Distribution signing incomplete",
        detail: err || "Apple Development Team or provisioning profile is not configured in project settings.",
        action: `<a class="btn small primary" href="#/config/integrations">Configure Apple Signing</a>`,
        secondary: targetJob ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>` : `<button type="button" class="btn small" ${act(r.action || "distribute")}>Retry distribution</button>`,
      };
    }
    if (lower.includes("firebase") && (lower.includes("not configured") || lower.includes("credentials") || lower.includes("app id") || lower.includes("permission"))) {
      return {
        title: "Firebase distribution issue",
        detail: err || "Firebase credentials or distribution app ID are not configured or lacked permission.",
        action: `<a class="btn small primary" href="#/config/integrations">Configure Firebase</a>`,
        secondary: targetJob ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>` : `<button type="button" class="btn small" ${act(r.action || "distribute")}>Retry distribution</button>`,
      };
    }
    if (err) {
      const rerunBtn = targetJob
        ? `<button type="button" class="btn small primary" ${act(r.action || "distribute", { job: targetJob })}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Retry distribution</button>`
        : `<button type="button" class="btn small primary" ${act(r.action || "distribute")}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Retry distribution</button>`;
      const backBtn = targetJob
        ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
        : "";
      return {
        title: "Distribution failed",
        detail: err,
        action: rerunBtn,
        secondary: backBtn,
      };
    }
  }

  if (r.action === "visual_check" || (r.title && /visual check/i.test(r.title))) {
    const isPpmProgress = lower.includes("checking out") || lower.includes("resolving package") || lower.includes("fetching from") || lower.includes("package");
    if (isPpmProgress || lower.includes("package.resolved") || lower.includes("dependencies")) {
      return {
        recoveryAction: "resolve-packages",
        title: "Swift package resolution required",
        detail: "Swift package dependencies were resolving when the check stopped. Resolving package dependencies will allow the simulator check to compile and run.",
        action: `<button type="button" class="btn small primary" data-recover="resolve-packages" data-rerun-action="visual_check"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-refresh"/></svg> Resolve packages &amp; Rerun</button>`,
        secondary: `<button type="button" class="btn small" ${act("visual_check")}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Rerun check</button>`,
      };
    }
    if (lower.includes("build failed") || lower.includes("compilation") || lower.includes("cannot create a build")) {
      return {
        title: "App build failed for visual check",
        detail: err || "The app must compile for the iOS simulator before screenshots can be captured. Check compilation output or rebuild.",
        action: `<button type="button" class="btn small primary" ${act("visual_check")}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Rerun visual check</button>`,
        secondary: `<button type="button" class="btn small" ${act("build")}>Run build</button>`,
      };
    }
    return {
      title: "Simulator visual check failed",
      detail: err || "Encountered an error while building or running the simulator visual check.",
      action: `<button type="button" class="btn small primary" ${act("visual_check")}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Rerun visual check</button>`,
      secondary: targetJob ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>` : "",
    };
  }

  if (lower.includes("thinking:") && (lower.includes("model active") || lower.includes("deep reasoning"))) {
    const rerunBtn = targetJob
      ? `<button type="button" class="btn small primary" data-auto-recover="5" ${act(r.action || "execute", { job: targetJob })}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-refresh"/></svg> Auto-recovering in <span data-recover-countdown>5</span>s…</button>`
      : r.action ? `<button type="button" class="btn small primary" data-auto-recover="5" ${act(r.action)}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-refresh"/></svg> Auto-recovering in <span data-recover-countdown>5</span>s…</button>` : "";
    const cancelBtn = `<button type="button" class="btn small ghost" data-cancel-autorecover>Cancel</button>`;
    const backBtn = targetJob ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>` : "";
    return {
      title: "Run interrupted while model was thinking",
      detail: "The run was stopped or the server restarted while waiting for the AI model to respond. Orchestrator can automatically recover and resume this task.",
      action: rerunBtn || `<a class="btn small primary" href="#/new">Create new job</a>`,
      secondary: rerunBtn ? cancelBtn : backBtn,
    };
  }

  if (err) {
    const rerunBtn = targetJob
      ? `<button type="button" class="btn small primary" ${act(r.action || "execute", { job: targetJob })}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Rerun job</button>`
      : r.action ? `<button type="button" class="btn small primary" ${act(r.action)}><svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Rerun</button>` : "";
    const backBtn = targetJob
      ? `<a class="btn small" href="#/jobs/${encodeURIComponent(targetJob)}">Back to job</a>`
      : "";
    return {
      title: "Execution error",
      detail: err,
      action: rerunBtn || `<a class="btn small primary" href="#/new">Create new job</a>`,
      secondary: backBtn,
    };
  }

  return null;
}

function runStatus(r, vc = null) {
  if (r.action === "console") {
    return `<div class="banner"><p>Console connection was stopped.</p></div>`;
  }
  if (!r.running && (r.action === "visual_check" || /visual check/i.test(r.title)) && vc?.screenshots?.length) {
    return nextStep(r, vc);
  }
  if (!r.running && r.stopped) {
    const target = r.result_job || r.job;
    const open = target ? `<a class="btn small primary" href="#/jobs/${encodeURIComponent(target)}">Open job</a>` : "";
    return `<div class="banner failed attention run-status"><p>Run was stopped.</p>${open}</div>`;
  }
  const p = r.progress || {};
  const stopping = stoppingRuns.has(r.id) || Boolean(r.stopping);
  const failed = Boolean(!r.running && r.exit_code !== 0);
  const done = Boolean(!r.running && r.exit_code === 0);
  const guidance = failed && typeof runFailureGuidance === "function" ? runFailureGuidance(r) : null;

  // When no tests exist, simplify the container completely: no telemetry clutter, no redundant headline or elapsed
  if (failed && guidance && guidance.title === "No source files or tests yet") {
    return `<section class="banner failed attention run-status run-status-notice" aria-label="Run status">
      <div class="run-status-notice-body">
        <div class="run-status-notice-title"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-alert"/></svg> <strong>${esc(guidance.title)}</strong></div>
        <p class="run-status-notice-desc">${esc(guidance.detail)}</p>
      </div>
      <div class="run-status-notice-actions row gap-8">
        ${guidance.action || ""}
        ${guidance.secondary || ""}
      </div>
    </section>`;
  }

  if (failed && guidance && guidance.isKeychain) {
    return `<section class="banner failed attention run-status run-status-notice run-status-keychain" aria-label="Run status">
      <div class="run-status-notice-body">
        <div class="run-status-notice-title"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-lock"/></svg> <strong>${esc(guidance.title)}</strong></div>
        <p class="run-status-notice-desc">${esc(guidance.detail)}</p>

        <form class="keychain-unlock-form" id="keychain-unlock-form" onsubmit="return false;">
          <div class="row gap-8 items-center flex-wrap">
            <input type="password" id="keychain-pwd-input" placeholder="Mac login password" autocomplete="current-password" aria-label="Mac login password" class="keychain-pwd-input" required>
            <button type="submit" class="btn small primary" id="keychain-unlock-btn"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-lock"/></svg> Unlock keychain</button>
          </div>
          <div class="keychain-pwd-note muted text-xs mt-4">
            🔒 Stored strictly locally in <code>.secrets/project-secrets.zsh</code> on this Mac to allow background builds. Never sent to remote servers.
          </div>
          <div id="keychain-unlock-result" class="mt-4" hidden></div>
        </form>

        <details class="keychain-manual-fold mt-8">
          <summary class="text-xs text-muted cursor-pointer">Or unlock manually in Terminal</summary>
          <div class="keychain-manual-body mt-4 text-xs stack gap-4">
            <p class="muted">Run this in Terminal on your Mac to unlock the login keychain:</p>
            <div class="row gap-4 items-center flex-wrap">
              <code class="code-box user-select-all">security unlock-keychain ~/Library/Keychains/login.keychain-db</code>
              <button type="button" class="btn small ghost" data-copy="security unlock-keychain ~/Library/Keychains/login.keychain-db">Copy</button>
            </div>
            <p class="muted">To whitelist codesigning for background builds without future prompts:</p>
            <div class="row gap-4 items-center flex-wrap">
              <code class="code-box user-select-all">security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "&lt;your-password&gt;" ~/Library/Keychains/login.keychain-db</code>
              <button type="button" class="btn small ghost" data-copy='security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "&lt;your-password&gt;" ~/Library/Keychains/login.keychain-db'>Copy</button>
            </div>
          </div>
        </details>
      </div>
      <div class="run-status-notice-actions row gap-8">
        ${guidance.action || ""}
        ${guidance.secondary || ""}
      </div>
    </section>`;
  }

  if (failed && guidance && guidance.isApiKey) {
    const targetJob = r.job || r.result_job;
    const rerunAction = r.action || (targetJob ? "execute" : "");
    const rerunParams = targetJob ? { job: targetJob } : {};
    return `<section class="banner failed attention run-status run-status-notice run-status-apikey" aria-label="Run status">
      <div class="run-status-notice-body">
        <div class="run-status-notice-title"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-key"/></svg> <strong>${esc(guidance.title)}</strong></div>
        <p class="run-status-notice-desc">${esc(guidance.detail)}</p>

        <form class="inline-apikey-form" id="inline-apikey-form" onsubmit="return false;" data-rerun-action="${esc(rerunAction)}"${targetJob ? ` data-rerun-params='${attrJSON(rerunParams)}'` : ""}>
          <div class="row gap-8 items-center flex-wrap mt-2">
            <select id="apikey-provider-select" class="apikey-select" aria-label="AI provider">
              <option value="anthropic_api_key">Anthropic (Claude)</option>
              <option value="openai_api_key">OpenAI (Codex / ChatGPT)</option>
              <option value="gemini_api_key">Google (Gemini / Antigravity)</option>
              <option value="ollama_api_key">Ollama Cloud</option>
            </select>
            <input type="password" id="apikey-val-input" placeholder="Paste API key (sk-…)" autocomplete="off" aria-label="API key" class="apikey-input" required>
            <button type="submit" class="btn small primary" id="apikey-save-btn"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-key"/></svg> Save key &amp; Rerun</button>
          </div>
          <div class="apikey-note muted text-xs mt-4">
            🔒 Saved locally in settings.json on this Mac. Never transmitted to external servers.
          </div>
        </form>
      </div>
      <div class="run-status-notice-actions row gap-8">
        ${guidance.action || ""}
        ${guidance.secondary || ""}
      </div>
    </section>`;
  }

  if (failed && guidance && (guidance.title === "Execution error" || guidance.recoveryAction || guidance.isGhAuth || guidance.title.includes("failed") || guidance.title.includes("locked") || guidance.title.includes("required") || guidance.title.includes("incomplete") || guidance.title.includes("issue"))) {
    return `<section class="banner failed attention run-status run-status-notice" aria-label="Run status">
      <div class="run-status-notice-body">
        <div class="run-status-notice-title"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-alert"/></svg> <strong>${esc(guidance.title)}</strong></div>
        <p class="run-status-notice-desc">${esc(guidance.detail)}</p>
      </div>
      <div class="run-status-notice-actions row gap-8">
        ${guidance.action || ""}
        ${guidance.secondary || ""}
      </div>
    </section>`;
  }

  const caveat = done ? p.notice || "" : ""; // a run that finished but only partly did its job
  const status = stopping ? "Stopping…" : r.waiting ? "Waiting for you" : r.running ? "Running" : failed ? "Failed" : caveat ? "Completed with notes" : "Completed";
  const tone = stopping || r.waiting || caveat ? "attention" : r.running ? "working" : failed ? "failed" : "done";
  const numbered = (Number.isInteger(p.step) && Number.isInteger(p.total) && p.step > 0 && p.step <= p.total) || (done && r.action === "coverage");
  const currentStep = done && numbered ? (p.total || 5) : (p.step || 1);
  const totalSteps = numbered ? (p.total || 5) : 1;
  const stepPrefix = numbered ? `Step ${currentStep} of ${totalSteps}: ` : "";
  const completionLabel = r.action === "coverage" ? "Coverage measurement complete"
    : r.action === "logs_pull" ? "Logs pulled"
    : r.result_job && !r.job ? "Job created"
    : (r.job || r.result_job) ? "Job execution complete"
    : "Run complete";
  const actionName = r.action === "coverage" ? "Coverage measurement"
    : r.action === "logs_pull" ? "Pull device logs"
    : r.action === "build" ? "Build"
    : r.action === "test" ? "Run all tests"
    : r.title || (r.action ? r.action.replace(/_/g, " ") : "Run");
  const headlineLabel = done ? (r.action === "coverage" ? "Coverage measurement complete" : (p.label || completionLabel))
    : failed ? (p.label ? p.label.replace(/\s+failed$/i, "") : actionName)
    : (p.label || r.title || "Starting run");
  const headline = `${stepPrefix}${headlineLabel}`;
  const nonAiActions = new Set([
    "coverage", "build", "test", "test_suite", "test_plan",
    "git_pull", "git_push", "git_checkout", "stash_checkout", "git_new_branch",
    "logs_pull", "logs_tail", "logs_setup",
    "check", "check_config", "wizard", "worker_check", "worker_install",
    "console", "distribute", "deliver"
  ]);

  const activeModels = p.active_models || [];
  const recordedModels = p.models || [];
  const assignedModels = p.assigned_models || [];
  const displayModels = activeModels.length ? activeModels
    : recordedModels.length ? recordedModels
    : assignedModels;
  const activeCount = Number.isInteger(p.agents) ? p.agents : activeModels.length;

  let modelsText = "";
  if (displayModels.length) {
    const list = displayModels.map(esc).join(", ");
    if (r.running) {
      if (activeCount > 0) {
        modelsText = `${list} (${activeCount} active)`;
      } else if (recordedModels.length) {
        modelsText = `${list} (idle)`;
      } else {
        modelsText = `${list} (assigned)`;
      }
    } else {
      modelsText = list;
    }
  } else if (r.running && activeCount > 0) {
    modelsText = `${activeCount} active`;
  } else if (nonAiActions.has(r.action)) {
    modelsText = "None (local run)";
  } else if (done) {
    modelsText = "None used";
  } else if (r.running) {
    modelsText = "Awaiting activity";
  } else {
    modelsText = "None";
  }
  const showTask = !done && p.task && p.task.toLowerCase() !== (p.label || "").toLowerCase();
  const progressVal = done && numbered ? totalSteps : (numbered ? p.step - 1 : 0);
  const progressMax = numbered ? totalSteps : 1;
  const pct = done ? 100
    : Number.isFinite(p.pct) ? Math.min(100, Math.max(0, Math.round(p.pct)))
    : Number.isFinite(p.percent) ? Math.min(100, Math.max(0, Math.round(p.percent)))
    : progressMax > 0 ? Math.min(100, Math.max(0, Math.round((progressVal / progressMax) * 100))) : 0;
  const duration = runDuration(r.started, r.ended);
  const actionBtn = done ? (r.action === "coverage" ? `<a class="btn small primary" href="#/tests">View coverage</a>`
    : r.action === "logs_pull" ? `<a class="btn small" href="#/devlogs">See pulled logs</a>`
    : r.result_job && !r.job ? `<a class="btn small primary" href="#/jobs/${encodeURIComponent(r.result_job)}">Open job</a>`
    : r.job ? `<a class="btn small primary" href="#/jobs/${encodeURIComponent(r.job)}">Back to job</a>` : "")
    : "";

  const targetJob = r.job || r.result_job;
  const jobItem = targetJob
    ? `<div><dt>Job</dt><dd><a href="#/jobs/${encodeURIComponent(targetJob)}">${esc(targetJob)}</a></dd></div>`
    : "";
  const showAction = Boolean(actionName && (!headlineLabel || actionName.toLowerCase() !== headlineLabel.toLowerCase()) && (!p.label || actionName.toLowerCase() !== p.label.toLowerCase()));
  const actionItem = showAction ? `<div><dt>Action</dt><dd>${esc(actionName)}</dd></div>` : "";
  const resultText = done ? "Completed (exit 0)" : (failed && r.exit_code != null ? `Failed (exit ${r.exit_code})` : "");
  const resultItem = resultText ? `<div><dt>Result</dt><dd class="${caveat ? "" : done ? "text-ok" : "text-bad"}">${esc(resultText)}</dd></div>` : "";
  const stateText = r.waiting ? "Waiting for input" : stopping ? "Stopping…" : "";
  const stateItem = stateText ? `<div><dt>State</dt><dd>${esc(stateText)}</dd></div>` : "";

  return `<section class="banner ${tone} run-status" aria-label="Run status">
    <div class="run-status-heading" role="status" aria-live="polite"><span class="pill ${tone}">${esc(status)}</span>
      <strong>${esc(headline)}</strong>${showTask ? `<span class="run-status-task">${esc(p.task)}</span>` : ""}</div>
    <dl class="run-status-details">
      ${jobItem}
      ${actionItem}
      <div><dt>Models & agents</dt><dd>${modelsText}</dd></div>
      <div><dt>Elapsed</dt><dd data-run-elapsed>${esc(duration)}</dd></div>
      ${stateItem}
      ${resultItem}
    </dl>
    ${caveat ? `<p class="run-status-caveat"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-alert"/></svg> ${esc(caveat)}</p>` : ""}
    ${actionBtn ? `<div class="run-status-actions">${actionBtn}</div>` : ""}
    ${guidance ? `
      <div class="run-status-guidance">
        <div class="run-status-guidance-title"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-alert"/></svg> <strong>${esc(guidance.title)}</strong></div>
        <p class="run-status-guidance-detail">${esc(guidance.detail)}</p>
      </div>
      <div class="run-status-guidance-actions row gap-8">
        ${guidance.action || ""}
        ${guidance.secondary || ""}
      </div>` : ""}
    ${numbered ? `
      <div class="run-status-progress-wrap">
        <progress class="run-status-progress" value="${progressVal}" max="${progressMax}" aria-label="Completed steps: ${progressVal} of ${progressMax} (${pct}%)">${progressVal} of ${progressMax}</progress>
        <span class="run-status-pct">${pct}%</span>
      </div>` : ""}
  </section>`;
}

function wireKeychainUnlock(container) {
  if (!container) return;
  const form = container.querySelector("#keychain-unlock-form");
  if (form && form.dataset.wired !== "true") {
    form.dataset.wired = "true";
    const pwdInput = form.querySelector("#keychain-pwd-input");
    const unlockBtn = form.querySelector("#keychain-unlock-btn");
    const resultEl = form.querySelector("#keychain-unlock-result");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const pwd = pwdInput ? pwdInput.value : "";
      if (!pwd) return;
      if (unlockBtn) {
        unlockBtn.disabled = true;
        unlockBtn.innerHTML = `<svg class="icon icon-sm spin" aria-hidden="true"><use href="#i-refresh"/></svg> Unlocking…`;
      }
      if (pwdInput) pwdInput.disabled = true;
      if (resultEl) resultEl.hidden = true;

      try {
        const res = await api("keychain/unlock", { method: "POST", body: { password: pwd } });
        toast(res?.message || "Keychain unlocked");
        if (resultEl) {
          resultEl.innerHTML = `<div class="banner done py-2 px-3 text-sm row gap-4 items-center"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-check"/></svg> <span>${esc(res?.message || "Keychain unlocked and saved locally.")} You can now rerun the job.</span></div>`;
          resultEl.hidden = false;
        }
        if (unlockBtn) {
          unlockBtn.disabled = false;
          unlockBtn.innerHTML = `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-check"/></svg> Unlocked`;
        }
      } catch (err) {
        if (unlockBtn) {
          unlockBtn.disabled = false;
          unlockBtn.innerHTML = `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-lock"/></svg> Unlock keychain`;
        }
        if (pwdInput) {
          pwdInput.disabled = false;
          pwdInput.focus();
        }
        if (resultEl) {
          resultEl.innerHTML = `<div class="text-danger text-xs mt-2">${esc(err.message || "Failed to unlock keychain. Check your password.")}</div>`;
          resultEl.hidden = false;
        }
      }
    });
  }

  container.querySelectorAll("[data-copy]").forEach((btn) => {
    if (btn.dataset.copyWired === "true") return;
    btn.dataset.copyWired = "true";
    btn.addEventListener("click", () => {
      const text = btn.dataset.copy;
      if (!text) return;
      navigator.clipboard?.writeText(text).then(() => {
        toast("Copied command");
        const orig = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = orig; }, 1500);
      }).catch(() => toast(text, "info"));
    });
  });
}

function wireAutoRecovery(container) {
  if (!container) return;
  container.querySelectorAll("[data-recover]").forEach((btn) => {
    if (btn.dataset.wired === "true") return;
    btn.dataset.wired = "true";
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      const op = btn.dataset.recover;
      if (!op) return;
      const origHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = `<svg class="icon icon-sm spin" aria-hidden="true"><use href="#i-refresh"/></svg> Recovering…`;
      try {
        const res = await api(`recovery/${encodeURIComponent(op)}`, { method: "POST", body: {} });
        toast(res?.message || "Recovery completed");
        const rerunAction = btn.dataset.rerunAction;
        const rerunParams = btn.dataset.rerunParams ? JSON.parse(btn.dataset.rerunParams) : {};
        if (rerunAction) {
          runAction(rerunAction, rerunParams);
        } else {
          btn.disabled = false;
          btn.innerHTML = `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-check"/></svg> Done`;
        }
      } catch (err) {
        toast(err?.message || "Recovery failed", true);
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
    });
  });
}

function wireApiKeyForm(container) {
  if (!container) return;
  const form = container.querySelector("#inline-apikey-form");
  if (form && form.dataset.wired !== "true") {
    form.dataset.wired = "true";
    const providerSelect = form.querySelector("#apikey-provider-select");
    const valInput = form.querySelector("#apikey-val-input");
    const saveBtn = form.querySelector("#apikey-save-btn");

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const providerId = providerSelect ? providerSelect.value : "";
      const val = valInput ? valInput.value.trim() : "";
      if (!providerId || !val) return;

      if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.innerHTML = `<svg class="icon icon-sm spin" aria-hidden="true"><use href="#i-refresh"/></svg> Saving…`;
      }
      if (valInput) valInput.disabled = true;
      if (providerSelect) providerSelect.disabled = true;

      try {
        await api("config/keys", { method: "POST", body: { id: providerId, value: val } });
        toast("API key saved locally. Starting rerun…");
        const rerunAction = form.dataset.rerunAction;
        const rerunParams = form.dataset.rerunParams ? JSON.parse(form.dataset.rerunParams) : {};
        if (rerunAction) {
          runAction(rerunAction, rerunParams);
        } else {
          if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-check"/></svg> Saved`;
          }
        }
      } catch (err) {
        toast(err?.message || "Failed to save API key", true);
        if (saveBtn) {
          saveBtn.disabled = false;
          saveBtn.innerHTML = `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-key"/></svg> Save key &amp; Rerun`;
        }
        if (valInput) {
          valInput.disabled = false;
          valInput.focus();
        }
        if (providerSelect) providerSelect.disabled = false;
      }
    });
  }
}

function wireInterruptedAutoRecovery(container) {
  if (!container) return;
  const btn = container.querySelector("[data-auto-recover]");
  if (!btn || btn.dataset.wired === "true") return;
  btn.dataset.wired = "true";
  let count = parseInt(btn.dataset.autoRecover || "5", 10);
  const countdownSpan = btn.querySelector("[data-recover-countdown]");
  const cancelBtn = container.querySelector("[data-cancel-autorecover]");
  let timer = null;

  const cancel = () => {
    if (timer) clearInterval(timer);
    timer = null;
    btn.removeAttribute("data-auto-recover");
    btn.innerHTML = `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg> Rerun`;
    if (cancelBtn) cancelBtn.hidden = true;
  };

  if (cancelBtn) {
    cancelBtn.addEventListener("click", (e) => {
      e.preventDefault();
      cancel();
    });
  }

  timer = setInterval(() => {
    count -= 1;
    if (countdownSpan) countdownSpan.textContent = count;
    if (count <= 0) {
      if (timer) clearInterval(timer);
      timer = null;
      btn.removeAttribute("data-auto-recover");
      btn.click();
    }
  }, 1000);

  if (typeof cleanup !== "undefined" && Array.isArray(cleanup)) {
    cleanup.push(() => { if (timer) clearInterval(timer); });
  }
}

function wireRunGuidance(container) {
  if (!container) return;
  wireKeychainUnlock(container);
  wireAutoRecovery(container);
  wireApiKeyForm(container);
  wireInterruptedAutoRecovery(container);
}

function updateRunStatus(r) {
  const container = $("#next-step");
  if (!container) return;
  if (!r.running) {
    if (typeof setHeader === "function" && typeof runHeader === "function") {
      setHeader(runHeader(r));
    }
    // Polling also detects completion if the terminal's event stream disconnects.
    if (r.action === "visual_check" || /visual check/i.test(r.title)) {
      if (!container.runResultPending) {
        container.runResultPending = true;
        finishRunStatus(r).finally(() => { container.runResultPending = false; });
      }
      return;
    }
    const signature = JSON.stringify([r.running, r.exit_code, r.progress, r.title, r.error_summary, r.last_line]);
    if (container.runStatusSignature !== signature) {
      container.innerHTML = runStatus(r);
      container.runStatusSignature = signature;
      wireRunGuidance(container);
      if (r.exit_code !== 0) {
        $("#term-wrap")?.classList?.add("run-failed");
        if (typeof document !== "undefined") document.body?.classList?.add("run-failed");
      }
      if ($("#keybar")) $("#keybar").hidden = true;
      if ($("#term-input")) $("#term-input").hidden = true;
    }
    return;
  }
  const signature = JSON.stringify([r.progress, r.waiting, r.stopping, stoppingRuns.has(r.id), r.title]);
  if (container.runStatusSignature !== signature) {
    container.innerHTML = runStatus(r);
    container.runStatusSignature = signature;
  } else {
    // Keep the live region in place; a ticking clock shouldn't re-announce the headline.
    const clock = container.querySelector("[data-run-elapsed]");
    if (clock) clock.textContent = runDuration(r.started);
  }
}

function visualVideoUrl(vc) {
  if (!vc || !vc.video) return "";
  const backend = typeof getBackendUrl === "function" ? getBackendUrl() : null;
  const token = typeof getToken === "function" ? getToken() : null;
  const base = `${backend ?? ""}/api/visual-checks/${encodeURIComponent(vc.id)}/video/${encodeURIComponent(vc.video)}`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

function appearanceBadgeHtml(img) {
  if (img.includes("light")) {
    return `<span class="visual-check-mode-badge light"><span class="badge-dot"></span> Light</span>`;
  }
  if (img.includes("dark")) {
    return `<span class="visual-check-mode-badge dark"><span class="badge-dot"></span> Dark</span>`;
  }
  return "";
}

function nextStep(r) {
  const vc = arguments[1] || null;
  if (r.action === "console") {
    return `<div class="banner"><p>Console connection was stopped.</p></div>`;
  }
  if (r.stopped) {
    const target = r.result_job || r.job;
    const open = target ? `<a class="btn small primary" href="#/jobs/${encodeURIComponent(target)}">Open job</a>` : "";
    return `<div class="banner"><p>Run was stopped.</p>${open}</div>`;
  }
  const failed = r.exit_code !== 0;
  if (!failed && (r.action === "visual_check" || /visual check/i.test(r.title)) && (vc?.screenshots?.length || vc?.video)) {
    const target = r.result_job || r.job;
    const openJob = target ? `<a class="btn small" href="#/jobs/${encodeURIComponent(target)}">Back to job</a>` : "";
    const openReport = vc.id ? `<a class="btn small ghost" href="#/file?path=${encodeURIComponent(`.orchestrator/output/manual/${vc.id}/report.md`)}">Report</a>` : "";
    const shotCount = (vc.screenshots || []).length;
    const vUrl = typeof visualVideoUrl === "function" ? visualVideoUrl(vc) : (vc.video ? `/api/visual-checks/${encodeURIComponent(vc.id)}/video/${encodeURIComponent(vc.video)}` : "");
    return `<div class="banner done visual-check-banner">
      <div class="visual-check-done-header">
        <div class="visual-check-done-title">
          <strong>Visual check complete</strong>
          <span class="count">${plural(shotCount, "screenshot")}${vc.video ? ", 1 launch video" : ""}</span>
        </div>
        <div class="visual-check-done-actions row gap-8">
          ${openReport}
          ${openJob}
        </div>
      </div>
      <p class="visual-check-desc">Boots the app on the simulator to check launch layout, element alignment, and safe-area insets. Click any screen to zoom and inspect.</p>
      ${vc.video ? `
        <div class="visual-check-video-card">
          <div class="visual-check-video-label">
            <svg class="icon icon-sm" aria-hidden="true"><use href="#i-play"/></svg>
            <span>Launch &amp; Settle Video</span>
          </div>
          <video class="visual-check-player" controls playsinline preload="metadata" src="${vUrl}">
            Your browser does not support HTML5 video.
          </video>
        </div>` : ""}
      <div class="visual-check-grid" role="region" aria-label="Visual check screenshot results">
        ${(vc.screenshots || []).map((img) => {
          const imgUrl = `/api/visual-checks/${encodeURIComponent(vc.id)}/screenshots/${encodeURIComponent(img)}`;
          const authSrc = `visual-checks/${encodeURIComponent(vc.id)}/screenshots/${encodeURIComponent(img)}`;
          const badge = typeof appearanceBadgeHtml === "function" ? appearanceBadgeHtml(img) : "";
          return `
            <figure class="visual-check-thumb-card">
              <button type="button" class="visual-check-thumb-btn" data-preview-img="${esc(imgUrl)}" data-preview-auth="${esc(authSrc)}" data-preview-title="${esc(img)}" aria-label="Open screenshot ${esc(img)}">
                <img src="${esc(imgUrl)}" data-auth-src="${esc(authSrc)}" alt="Screenshot: ${esc(img)}" class="visual-check-thumb-img" loading="lazy">
                ${badge}
                <span class="visual-check-thumb-badge"><svg class="icon" aria-hidden="true"><use href="#i-search"/></svg> Open</span>
              </button>
              <figcaption class="visual-check-thumb-caption">
                <span class="mono">${esc(img)}</span>
                <a href="${esc(imgUrl)}" target="_blank" rel="noopener" class="visual-check-ext-link" title="Open full image in new tab" aria-label="Open ${esc(img)} in new tab">↗</a>
              </figcaption>
            </figure>`;
        }).join("")}
      </div>
    </div>`;
  }
  const guidance = failed && typeof runFailureGuidance === "function" ? runFailureGuidance(r) : null;
  const target = r.result_job || r.job;
  const open = r.result_job && !r.job ? `<a class="btn small primary" href="#/jobs/${encodeURIComponent(r.result_job)}">Open job</a>` : ""; // Back covers returning to the job
  const text = failed ? (guidance?.detail || "This run failed. The output below shows why.")
    : r.result_job && !r.job ? "Job created."
    : target ? "Done. The job page shows where it stands now."
    : r.action === "logs_pull" ? "Logs pulled."
    : r.action === "coverage" ? "Coverage measurement complete."
    : "Done.";
  const extra = !failed && r.action === "logs_pull" ? `<a class="btn small" href="#/devlogs">See pulled logs</a>`
    : !failed && r.action === "coverage" ? `<a class="btn small primary" href="#/tests">View coverage</a>`
    : failed && guidance ? `<div class="run-status-guidance-actions row gap-8">${guidance.action || ""}${guidance.secondary || ""}</div>`
    : "";
  return `<div class="banner ${failed ? "failed attention" : "done"}">${guidance ? `<div class="run-status-guidance-title"><svg class="icon icon-sm" aria-hidden="true"><use href="#i-alert"/></svg> <strong>${esc(guidance.title)}</strong></div>` : ""}<p>${esc(text)}</p>${extra}${open}</div>`;
}

async function finishRunStatus(run) {
  const container = $("#next-step");
  let vc = null;
  if (run.action === "visual_check" || /visual check/i.test(run.title)) {
    vc = await getVisualCheckForRun(run);
    if (!vc?.screenshots?.length) {
      await new Promise((resolve) => setTimeout(resolve, 400));
      vc = await getVisualCheckForRun(run);
    }
  }
  if (!container || $("#next-step") !== container || current.page !== "run" || current.args[0] !== run.id) return;
  setHeader(runHeader(run));
  const isVisualCheck = run.action === "visual_check" || /visual check/i.test(run.title);
  container.innerHTML = (isVisualCheck && !run.exit_code && (vc?.screenshots?.length || vc?.video)) ? nextStep(run, vc) : runStatus(run, vc);
  container.runStatusSignature = JSON.stringify([run.running, run.exit_code, run.progress, run.title]);
  hydrateAuthImages(container);
  wireImagePreviews(container);
  wireRunGuidance(container);
  if (run.exit_code !== 0) {
    $("#term-wrap")?.classList?.add("run-failed");
    if (typeof document !== "undefined") document.body?.classList?.add("run-failed");
  }
  $("#keybar").hidden = true;
  $("#term-input").hidden = true;
}

pages.run = async ([id]) => {
  let run = state.runs.find((r) => r.id === id);
  if (!run) { await refreshState(); run = state.runs.find((r) => r.id === id); } // opened before the list caught up
  if (!run) {
    const saved = state.you?.role === "member" ? null : ((await api("runs/history").catch(() => null))?.runs || []).find((r) => r.id === id);
    if (saved) {
      let logText = "";
      try {
        const fileData = await api(`file?path=${encodeURIComponent(saved.log)}`);
        logText = fileData?.text || "";
      } catch {}
      const exitMatch = logText.match(/\[exited(?: with code (\d+))?\]/);
      const exitCode = exitMatch ? parseInt(exitMatch[1] || "0", 10) : 1;
      const cleanLog = logText.replace(/\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][AB0-2]|[78=>cM])/g, "").replace(/\r/g, "\n");
      const nonExitedLines = cleanLog.split(/\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("[exited"));
      const recentLines = nonExitedLines.slice(-40);
      const errorCandidates = recentLines.filter((l) => /error|failed|fatal|exception|cannot|command not found|denied|locked|timed? ?out/i.test(l) && !/cleaned up|notification/i.test(l));
      const extractedError = errorCandidates.length ? errorCandidates.slice(-3).join("\n") : recentLines.slice(-4).join("\n");

      run = {
        id: saved.id,
        action: saved.action,
        title: saved.title,
        started: saved.started,
        ended: saved.started,
        running: false,
        stopped: false,
        exit_code: exitCode,
        error_summary: exitCode !== 0 ? extractedError : "",
        last_line: extractedError,
        progress: {},
        saved_log_text: logText,
        is_saved: true,
      };
    }
  }
  if (!run) {
    return { title: "Run not found", html: `<div class="notice"><p>This run isn't running, and no saved log matches it.</p>
          <div class="row mt-8"><a class="btn small" href="#/activity">See earlier runs</a></div></div>` };
  }
  let vc = null;
  if (!run.running && (run.action === "visual_check" || /visual check/i.test(run.title))) {
    vc = await getVisualCheckForRun(run);
  }
  return {
    ...runHeader(run),
    html: `
      <div id="next-step">${runStatus(run, vc)}</div>
      <div class="term-wrap" id="term-wrap">
        <div class="term" id="term"></div>
        <div class="keybar" id="keybar" aria-label="Terminal keys" ${run.running ? "" : "hidden"}>${KEYS.map(([label], i) => `<button class="btn small" data-key="${i}">${esc(label)}</button>`).join("")}</div>
        <form class="term-input" id="term-input" ${run.running ? "" : "hidden"}>
          <input type="text" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Type into the terminal" placeholder="Type, then Send">
          <button class="btn">Send</button>
        </form>
      </div>`,
    after: () => {
      if (run.is_saved && run.saved_log_text) {
        attachSavedTerminal(run.saved_log_text, run.exit_code !== 0);
        wireRunGuidance($("#next-step"));
        cleanup.push(() => { document.body.classList.remove("run-failed"); });
        return;
      }
      attachTerminal(id);
      wireRunGuidance($("#next-step"));
      cleanup.push(() => { document.body.classList.remove("run-failed"); });
      let lastPoll = Date.now();
      const statusTimer = setInterval(async () => {
        const latest = state.runs.find((r) => r.id === id);
        if (latest) {
          updateRunStatus(latest);
          if (latest.running && Date.now() - lastPoll > 2000) {
            lastPoll = Date.now();
            await refreshState();
            const updated = state.runs.find((r) => r.id === id);
            if (updated) updateRunStatus(updated);
          }
          if (!latest.running) {
            clearInterval(statusTimer);
          }
        }
      }, 1000);
      cleanup.push(() => clearInterval(statusTimer));
      if (!run.running) {
        hydrateAuthImages($("#next-step"));
        wireImagePreviews($("#next-step"));
        wireRunGuidance($("#next-step"));
      }
    },
  };
};

pages["auto-improve"] = async (_, query) => {
  let jobs = [];
  try {
    jobs = (await api("jobs")).jobs || [];
  } catch {
    jobs = [];
  }

  // Filter jobs created to improve Orchestrator
  const improvementJobs = jobs.filter((j) => Boolean(j.is_orchestrator_improvement || j.tag === "improve_orchestrator"));

  const initialType = query?.get("type") || "feature";
  const initialSummary = query?.get("summary") || "";
  const initialSpec = query?.get("spec") || "";

  return {
    title: "Auto-improve Orchestrator",
    sub: "Build features and fixes for Orchestrator itself.",
    actions: "",
    html: `
      <!-- New Improvement Job -->
      <form class="card card-b stack mb-16" id="auto-improve-job-form">
        <div class="field"><span>Type</span>
          <div class="segmented" role="radiogroup" aria-label="Improvement type">
            <label class="job-type-card job-type-feature"><input type="radio" name="improve_type" value="feature" ${initialType !== "bug" ? "checked" : ""}>Feature</label>
            <label class="job-type-card job-type-bug"><input type="radio" name="improve_type" value="bug" ${initialType === "bug" ? "checked" : ""}>Bug fix</label>
          </div>
        </div>

        <label class="field">
          <span>Summary</span>
          <input type="text" name="summary" required maxlength="500" value="${esc(initialSummary)}" placeholder="What should Orchestrator build or fix?">
        </label>

        <label class="field">
          <span>Details <span class="muted">(optional)</span></span>
          <textarea name="spec" rows="2" placeholder="Requirements or notes">${esc(initialSpec)}</textarea>
        </label>

        <div class="row">
          <label class="check"><input type="checkbox" name="yolo"><span>Autopilot</span></label>
          <span class="spacer"></span>
          <button class="btn primary" type="submit">Start job</button>
        </div>
      </form>

      <!-- Tracked Improvements -->
      <section class="card">
        <div class="card-h">
          <h2>Tracked improvements <span class="count">${improvementJobs.length}</span></h2>
          <button type="button" class="btn small ghost" id="auto-improve-manual-submit-btn">Submit outside PR</button>
        </div>
        ${improvementJobs.length ? `
          <div class="job-table">
            <div class="job-table-header">
              <div class="col-job">Job</div>
              <div class="col-type">Type</div>
              <div class="col-status">Status</div>
              <div class="col-date"><span class="date-header-long">Last modified</span><span class="date-header-short">Date</span></div>
            </div>
            <div class="job-table-body">
              ${improvementJobs.map((j) => {
                const submitted = Boolean(j.submitted_for_inclusion);
                return `
                  <div class="job-table-row">
                    <div class="col-job">
                      <a class="job-title" href="#/jobs/${encodeURIComponent(j.id)}">${esc(j.title)}</a>
                      <div class="job-meta">
                        <span class="mono">${esc(j.job_id || j.id)}</span>
                        ${j.branch ? ` · <span class="mono">${esc(j.branch)}</span>` : ""}
                        ${submitted ? ` · <span class="accent">✓ Submitted</span>` : ""}
                      </div>
                    </div>
                    <div class="col-type">
                      <span class="job-type-tag job-type-auto-improve">🏆 Orchestrator</span>
                    </div>
                    <div class="col-status">
                      ${jobPill(j)}
                    </div>
                    <div class="col-date">
                      <span class="date-relative">${esc(ago(j.updated))}</span>
                    </div>
                    <div class="row gap-8 align-center" style="margin-left: auto; padding-right: 12px;">
                      <a class="btn small ghost" href="#/jobs/${encodeURIComponent(j.id)}">Open</a>
                      ${submitted ? `
                        <button type="button" class="btn small ghost" disabled>Submitted</button>
                      ` : `
                        <button type="button" class="btn small prize-btn" data-submit-job="${esc(j.id)}" data-job-title="${esc(j.title)}" data-job-branch="${esc(j.branch || '')}">Submit for prize 🏆</button>
                      `}
                    </div>
                  </div>
                `;
              }).join("")}
            </div>
          </div>
        ` : `
          <div class="empty">
            <p>No improvement jobs yet.</p>
          </div>
        `}
      </section>
    `,
    after: () => {
      const form = $("#auto-improve-job-form");
      if (form) {
        form.addEventListener("submit", (e) => {
          e.preventDefault();
          const f = new FormData(form);
          const type = f.get("improve_type") || "feature";
          const summary = String(f.get("summary") || "").trim();
          const spec = String(f.get("spec") || "").trim();
          const yolo = f.has("yolo");

          if (!summary) {
            toast("Please enter a summary of what to build.", "warning");
            return;
          }

          const composedSpec = [
            "Goal: Improve Orchestrator itself.",
            "",
            "Scope: Orchestrator codebase and developer/user experience.",
            spec ? "Requirements & Guidance:\n" + spec : "",
          ].filter(Boolean).join("\n\n");

          runAction("new_job", {
            type,
            summary: summary.startsWith("Auto-improve:") ? summary : `Auto-improve: ${summary}`,
            spec: composedSpec,
            tag: "improve_orchestrator",
            is_orchestrator_improvement: true,
            yolo,
          });
        });
      }

      const openPrizeModal = (jobId = "", jobTitle = "", jobBranch = "") => {
        Feedback.open({
          initialStep: "improve_orchestrator",
          category: "improve_orchestrator",
          jobId,
          title: jobTitle,
          explanation: jobId ? `Completed improvement via job ${jobId} on branch ${jobBranch || 'main'}.` : "",
          userName: state.you?.email || ""
        });
      };

      view.querySelectorAll("[data-submit-job]").forEach((btn) => {
        btn.addEventListener("click", () => {
          openPrizeModal(btn.dataset.submitJob, btn.dataset.jobTitle, btn.dataset.jobBranch);
        });
      });

      $("#auto-improve-manual-submit-btn")?.addEventListener("click", () => {
        openPrizeModal();
      });
    }
  };
};

function attachSavedTerminal(text, failed) {
  const termEl = $("#term");
  const termWrap = $("#term-wrap");
  if (!termEl) return;
  if (failed) {
    termWrap?.classList.add("run-failed");
    document.body.classList.add("run-failed");
  }
  if (!text || !text.trim()) {
    termWrap?.classList.add("term-empty");
    return;
  }
  if (window.Terminal) {
    const term = new window.Terminal({
      cursorBlink: false, fontSize: window.innerWidth < 760 ? 12 : 13, scrollback: 20000,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", theme: { background: "#0f1216" },
    });
    const fitAddon = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fitAddon) term.loadAddon(fitAddon);
    term.open(termEl);
    if (fitAddon) {
      try { fitAddon.fit(); } catch {}
    }
    term.write(text.replace(/\r?\n/g, "\r\n"));
    const onResize = () => { try { fitAddon?.fit(); } catch {} };
    window.addEventListener("resize", onResize);
    cleanup.push(() => {
      window.removeEventListener("resize", onResize);
      term.dispose();
    });
  } else {
    termWrap?.classList.add("fallback");
    const pre = document.createElement("pre");
    pre.className = "term-fallback";
    pre.textContent = text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").replace(/\r/g, "");
    termEl.appendChild(pre);
  }
}

function attachTerminal(id) {
  const send = (data) => api(`runs/${id}/input`, { method: "POST", body: { data } }).catch((e) => toast(e.message, true));
  const termEl = $("#term");
  const termWrap = $("#term-wrap");
  let write, fit = () => {};
  let hasOutput = false;

  const currentRun = state.runs.find((r) => r.id === id);
  if (currentRun && !currentRun.running) {
    if (currentRun.exit_code !== 0) {
      termWrap?.classList.add("run-failed");
      document.body.classList.add("run-failed");
    }
    termWrap?.classList.add("term-empty");
  }

  const onBytesWritten = (bytes) => {
    if (bytes && bytes.length > 0) {
      hasOutput = true;
      termWrap?.classList.remove("term-empty");
    }
  };

  if (window.Terminal) {
    const term = new window.Terminal({
      cursorBlink: true, fontSize: window.innerWidth < 760 ? 12 : 13, scrollback: 20000,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", theme: { background: "#0f1216" },
    });
    const fitAddon = window.FitAddon ? new window.FitAddon.FitAddon() : null;
    if (fitAddon) term.loadAddon(fitAddon);
    term.open(termEl);
    term.onData(send);
    write = (bytes) => {
      onBytesWritten(bytes);
      term.write(bytes);
    };
    let resizeTimer;
    fit = () => {
      if (!fitAddon) return;
      try { fitAddon.fit(); } catch { return; }
      lastTermSize = { cols: term.cols, rows: term.rows };
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => api(`runs/${id}/resize`, { method: "POST", body: lastTermSize }).catch(() => {}), 150);
    };
    fit();
    requestAnimationFrame(fit);
    setTimeout(fit, 60);
    if (window.ResizeObserver && termEl) {
      const ro = new ResizeObserver(() => fit());
      ro.observe(termEl);
      cleanup.push(() => ro.disconnect());
    }
    if (window.innerWidth >= 760) term.focus();
    cleanup.push(() => term.dispose());
  } else {
    $("#term-wrap").classList.add("fallback");
    const pre = document.createElement("pre");
    pre.className = "term-fallback";
    termEl.appendChild(pre);
    const decoder = new TextDecoder();
    write = (bytes) => {
      onBytesWritten(bytes);
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
    if (run.exit_code !== 0) {
      termWrap?.classList.add("run-failed");
      document.body.classList.add("run-failed");
    }
    if (!hasOutput) {
      termWrap?.classList.add("term-empty");
    }
    fit();
    await finishRunStatus(run);
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
  if (r.page === "docs" && r.args[0] === "product") return "#/product";
  if (r.page === "docs" && r.args.length) return "#/docs";
  if (r.page === "config" && r.args.length) return "#/config";
  if (r.page === "new-project") return "#/projects";
  if (r.page === "test-cases") return "#/tests";
  if (["connections", "projects"].includes(r.page)) return "#/config";
  if (r.page === "ux-review") return "#/checkup";
  if (["git", "help", "readiness", "checkup", "devlogs", "setup"].includes(r.page)) return "#/";
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
  document.body.dataset.page = r.page || "";
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
        onReady: async () => { await refreshState(); location.hash = "#/checkup"; await route(); }})
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
  if (typeof signingIn !== "undefined" && signingIn) return;
  if (view.querySelector && view.querySelector(".signin-card:not(.signing-in)") && view.contains(document.activeElement) && document.activeElement.matches("input, textarea, select")) return;
  await refreshState(); // also while the tab is hidden, so notifications can fire
  if (document.hidden) return;
  if (!state.project) return;
  await loadSetup();
  if (current.page === "run") {
    const run = state.runs.find((r) => r.id === current.args[0]);
    if (run) {
      setHeader(runHeader(run)); // keeps Stop / waiting status current while you watch
      updateRunStatus(run);
    }
    return;
  }
  if (!LIVE.has(current.page) || $("#dialog").open || document.querySelector("details.more[open]")) return;
  if (current.page !== "ux-review" && view.contains(document.activeElement) && document.activeElement.matches("input, textarea, select")) return;
  const page = current.page, args = current.args.join();
  try {
    const result = await pages[page](current.args, current.query);
    if (current.page !== page || current.args.join() !== args) return;
    if (page === "ux-review" && updateUxReviewWhileEditing(result)) return;
    if (signatureOf(result) !== current.rendered) apply(result);
  } catch { /* keep showing the last good render */ }
}

window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  setupFetched = 0;
  tick().catch(() => {}); // retry on the next poll if the connection is unavailable
});
const connBtn = $("#backend-settings-btn");
if (connBtn) {
  connBtn.addEventListener("click", () => showSignInGate());
}
$("#skip-link")?.addEventListener("click", () => { $("#view").focus({ preventScroll: false }); });

const navigationDrawer = NavigationDrawer.mount({document, window});

// ---------------------------------------------------------------- command palette
const PALETTE_PAGES = [["Home", "#/", "What needs you, and all jobs"], ["Product", "#/product", "Pitch, who it's for, features, look and feel, what not to build"], ["Epics", "#/product?section=epics", "Break the product down into epics, approve them, and link them to Jira"], ["Docs", "#/docs", "Every feature, job and project file in one place"],
  ["Projects", "#/projects", "Switch, add or start a project"], ["Activity", "#/activity", "Runs and live output"], ["Device logs", "#/devlogs", "Logs from test devices"], ["Tests", "#/tests", "Test cases, suites, coverage"],
  ["Test cases", "#/tests/cases", "Manage test cases library, coverage and definitions"],
  ["Git setup", "#/git", "Branches and changes"], ["App delivery", "#/delivery", "What's live, with testers, pipeline"], ["Measure", "#/measure", "KPIs and analytics"], ["Onboarding checklist", "#/checkup", "Project requirements, tools, environment checks, and selected workflows"], ["UX review", "#/ux-review", "Check screens against usability and design principles"],
  ["Connections", "#/connections", "Jira, Trello, Sentry, Figma, Slack alerts"], ["Configuration", "#/config", "Models, keys, machines, alerts"], ["Help", "#/help", "How it works, glossary"], ["New job", "#/new", "Describe work to be done"],
  ["Start a new project", "#/new-project", "Describe an idea and set it up"]];
const palette = { open: false, entries: [], shown: [], active: 0, opener: null };

function paletteBase() {
  const go = (hash) => () => { location.hash = hash; };
  const entries = PALETTE_PAGES.filter(([, hash]) => !(hash === "#/devlogs" && state.project?.mobile_app === false) && !(hash === "#/new-project" && state.you?.role === "member")).map(([label, hash, hint]) => ({ label, hint, group: "Pages", order: 1, run: go(hash) }));
  const act_ = (label, hint, fn) => entries.push({ label, hint, group: "Actions", order: 0, run: fn });
  act_("Run all tests", "Manual test run", () => runAction("test", {}));
  act_("Send feedback", "Tell us what could be better or share ideas", () => Feedback.open({ initialStep: "feedback", userName: state.you?.email || "" }));
  act_("Suggest missing feature", "Request a feature or capability you'd like in Orchestrator", () => Feedback.open({ initialStep: "feedback", category: "feature_request", userName: state.you?.email || "" }));
  act_("Auto-improve Orchestrator", "Create an AI job to improve Orchestrator itself and track submissions", () => { location.hash = "#/auto-improve"; });
  act_("Improve Orchestrator (Win a Prize!)", "Submit an Orchestrator improvement for permanent inclusion and prize", () => Feedback.open({ initialStep: "improve_orchestrator", category: "improve_orchestrator", userName: state.you?.email || "" }));
  act_("Create job to improve Orchestrator", "Start a new local AI job focused on enhancing Orchestrator itself", () => { location.hash = "#/auto-improve"; });
  act_("Sign out", "Sign out of this browser", () => $("#signout-btn")?.click());
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
const configureSearchShortcut = () => {
  const k = $("#search-btn kbd");
  if (!k) return;
  const ua = navigator.userAgent || "";
  const plat = navigator.platform || "";
  const isTouchMobile = (navigator.maxTouchPoints > 1 && /MacIntel|iPhone|iPad|iPod/i.test(plat))
    || /Android|iPhone|iPad|iPod|Windows Phone|Mobile|Silk/i.test(ua)
    || (window.matchMedia?.("(pointer: coarse)")?.matches && window.innerWidth <= 1024);

  if (isTouchMobile) {
    k.hidden = true;
    k.textContent = "";
  } else if (/Mac/i.test(plat) || /Macintosh/i.test(ua)) {
    k.hidden = false;
    k.textContent = "⌘K";
  } else if (/Win|Linux/i.test(plat) || /Windows|Linux/i.test(ua)) {
    k.hidden = false;
    k.textContent = "Ctrl K";
  } else {
    k.hidden = true;
    k.textContent = "";
  }
};
configureSearchShortcut();
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
const signOutBtn = $("#signout-btn");
if (signOutBtn) {
  signOutBtn.addEventListener("click", signOut);
}
const feedbackBtn = $("#feedback-btn");
if (feedbackBtn) {
  feedbackBtn.addEventListener("click", () => {
    Feedback.open({ initialStep: "feedback", userName: state.you?.email || "" });
  });
}
const autoImproveBtn = $("#auto-improve-btn");
if (autoImproveBtn) {
  autoImproveBtn.addEventListener("click", () => {
    location.hash = "#/auto-improve";
  });
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

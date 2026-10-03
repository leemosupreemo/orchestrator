// The hosted app's account screens: your computers, adding one with a code from `orchestrator connect`, and which
// one to open. Rendering and choices live here (no DOM, so they can be tested); app.js wires them to the page.
(function (root) {
  "use strict";

  const HOSTED_ORIGINS = ["https://swift-orch-web-20260923.web.app", "https://swift-orch-web-20260923.firebaseapp.com"];
  const INSTALL_COMMAND = 'pipx install "git+https://github.com/leemosupreemo/orchestrator.git"';
  const PAIR_KEY = "orchestrator_pair_code";
  const MACHINE_KEY = "orchestrator_machine";
  const CODE_LENGTH = 8;
  // The oldest computer this page works well with: orchestrator/account.py's API_VERSION, which a computer reports in
  // its state and its heartbeat. Raise it in the same change that makes the page rely on something newer.
  const REQUIRED_RUNNER_API = 2;
  const UPDATE_HINT = "Update it: run `orchestrator update` on it.";
  const APP_UPDATE_HINT = "Choose Update to update it.";
  // What a Mac running the Orchestrator app reports about an update this page asked for.
  const UPDATE_PROGRESS = {
    requested: "Update requested. It starts within a minute.",
    checking: "Looking for an update…",
    waiting_for_work: "An update is ready. It installs when the current work finishes.",
    updating: "Updating…",
    current: "Up to date.",
    failed: "The last update didn't finish. Try again, or open Diagnostics on it.",
    unavailable: "This Mac can't update itself yet: its app has no update feed.",
  };
  const UPDATE_BUSY = ["requested", "checking", "waiting_for_work", "updating"];

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"})[c]);
  }

  function isHosted(origin = root.location?.origin) {
    return HOSTED_ORIGINS.includes(origin);
  }

  // On the hosted app your computers are found through your account, unless a link named one (?backend=…).
  function active(loc = root.location) {
    return isHosted(loc?.origin) && !new URLSearchParams(loc?.search || "").get("backend");
  }

  function normalizeCode(value) {
    return String(value || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  }

  function formatCode(code) {
    return code.length === CODE_LENGTH ? `${code.slice(0, 4)}-${code.slice(4)}` : code;
  }

  // A code from #/connect?code=…, kept for the visit so it survives a full-page sign-in.
  function pendingCode(storage = root.sessionStorage, hash = root.location?.hash || "") {
    const [path, qs] = hash.replace(/^#/, "").split("?");
    const fromLink = path.replace(/^\/+/, "") === "connect" ? normalizeCode(new URLSearchParams(qs || "").get("code")) : "";
    try {
      if (fromLink.length === CODE_LENGTH) storage.setItem(PAIR_KEY, fromLink);
      return normalizeCode(storage.getItem(PAIR_KEY)) || "";
    } catch {
      return fromLink.length === CODE_LENGTH ? fromLink : "";
    }
  }

  function clearPendingCode(storage = root.sessionStorage) {
    try { storage.removeItem(PAIR_KEY); } catch { /* nothing kept */ }
  }

  // Which computer to open without asking: the one used last, or the only one that can be opened.
  function pickMachine(machines, rememberedId) {
    const reachable = (machines || []).filter((m) => m.reachable);
    return reachable.find((m) => m.id === rememberedId) || (reachable.length === 1 ? reachable[0] : null);
  }

  function ago(seconds, now = Date.now() / 1000) {
    const s = Math.max(0, Math.round(now - seconds));
    if (s < 90) return "just now";
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 129600) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }

  const outdated = (apiVersion) => (apiVersion || 0) < REQUIRED_RUNNER_API;

  function machineStatus(m, now) {
    if (m.reachable && outdated(m.api_version)) return {tone: "warn", text: `Online, but running an older Orchestrator${m.version ? ` (${m.version})` : ""}. ${m.updatable ? APP_UPDATE_HINT : UPDATE_HINT}`};
    if (m.reachable) return {tone: "good", text: "Online"};
    if (m.online) return {tone: "warn", text: "Online, but not reachable from the web yet. Restart it with `orchestrator ui --tunnel`."};
    if (m.last_seen) return {tone: "muted", text: `Offline · last seen ${ago(m.last_seen, now)}. Start \`orchestrator ui --tunnel\` on it.`};
    return {tone: "muted", text: "Waiting for it to start. Run `orchestrator ui --tunnel` on it."};
  }

  function code(text) {
    return escapeHtml(text).replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  function brand() {
    return `<div class="signin-brand"><span class="brand-mark" aria-hidden="true"></span><span>Orchestrator</span></div>`;
  }

  function addSteps(pendingCodeValue = "") {
    return `<ol class="account-steps">
        <li>Install Orchestrator on the computer with your projects:<pre class="mono">${escapeHtml(INSTALL_COMMAND)}</pre></li>
        <li>On that computer, run <code>orchestrator connect</code>. It shows a code.</li>
        <li>Enter the code here:
          <form class="row gap-10 account-code-form" data-account-form="code">
            <input name="code" required autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="9"
                   placeholder="ABCD-2345" aria-label="Code from orchestrator connect" value="${escapeHtml(pendingCodeValue ? formatCode(pendingCodeValue) : "")}" class="mono">
            <button type="submit" class="btn primary">Add computer</button>
          </form></li>
        <li>Then start it with <code>orchestrator ui --tunnel</code>. It appears here as online.</li>
      </ol>`;
  }

  function renderMachines(machines, {email = "", message = "", now} = {}) {
    const list = machines || [];
    const rows = list.map((m) => {
      const s = machineStatus(m, now);
      const progress = UPDATE_PROGRESS[m.update_state] || "";
      const canUpdate = m.updatable && m.online && !UPDATE_BUSY.includes(m.update_state);
      return `<div class="account-machine">
        <div class="account-machine-main">
          <strong>${escapeHtml(m.name)}</strong>
          <span class="muted">${escapeHtml([m.os, m.version && `Orchestrator ${m.version}`, m.added_with_command && "Added with a command"].filter(Boolean).join(" · "))}</span>
          <span class="account-status ${s.tone}">${code(s.text)}</span>
          ${progress && m.update_state !== "current" ? `<span class="muted account-update">${escapeHtml(progress)}</span>` : ""}
        </div>
        <div class="row gap-10">
          ${m.reachable ? `<button type="button" class="btn small primary" data-account-action="open" data-id="${escapeHtml(m.id)}">Open</button>` : ""}
          ${canUpdate ? `<button type="button" class="btn small ghost" data-account-action="update" data-id="${escapeHtml(m.id)}" data-name="${escapeHtml(m.name)}">Update</button>` : ""}
          <button type="button" class="btn small ghost" data-account-action="remove" data-id="${escapeHtml(m.id)}" data-name="${escapeHtml(m.name)}">Remove</button>
        </div>
      </div>`;
    }).join("");
    return `<div class="signin-wrap"><div class="signin-card account-card">
      ${brand()}
      <div>
        <h2 class="account-title">${list.length ? "Your computers" : "Add your computer"}</h2>
        <p class="muted account-sub">${list.length
          ? "Orchestrator runs on your own computer: your code, model subscriptions and connected apps stay there."
          : "Orchestrator does the work on your own computer, so your code, model subscriptions and connected apps stay there. Add it once and use it from anywhere."}</p>
        ${message ? `<p class="account-message" role="alert">${code(message)}</p>` : ""}
      </div>
      ${list.length ? `<div class="account-machines">${rows}</div>
        <details class="account-add"><summary>Add another computer</summary>${addSteps()}</details>` : addSteps()}
      <p class="muted account-foot">Signed in as ${escapeHtml(email)} · <button type="button" class="linklike" data-account-action="refresh">Refresh</button> · <button type="button" class="linklike" data-account-action="sign-out">Sign out</button></p>
    </div></div>`;
  }

  function renderPair(preview, codeValue, {email = ""} = {}) {
    return `<div class="signin-wrap"><div class="signin-card account-card">
      ${brand()}
      <div>
        <h2 class="account-title">Add this computer?</h2>
        <p class="muted account-sub">Code <span class="mono">${escapeHtml(formatCode(codeValue))}</span></p>
      </div>
      <div class="account-machine"><div class="account-machine-main">
        <strong>${escapeHtml(preview.name)}</strong>
        <span class="muted">${escapeHtml([preview.os, preview.version && `Orchestrator ${preview.version}`].filter(Boolean).join(" · "))}</span>
      </div></div>
      <p class="muted">It will be added to <strong>${escapeHtml(email)}</strong>, and you'll be able to open it from anywhere you sign in.
        Only add a computer you just ran <code>orchestrator connect</code> on yourself.</p>
      <div class="row gap-10">
        <button type="button" class="btn primary" data-account-action="claim">Add computer</button>
        <button type="button" class="btn ghost" data-account-action="cancel-pair">Cancel</button>
      </div>
    </div></div>`;
  }

  root.Account = {
    HOSTED_ORIGINS, INSTALL_COMMAND, MACHINE_KEY, CODE_LENGTH, REQUIRED_RUNNER_API, UPDATE_HINT, UPDATE_PROGRESS, outdated,
    isHosted, active, normalizeCode, formatCode, pendingCode, clearPendingCode, pickMachine, machineStatus,
    renderMachines, renderPair,
  };
})(typeof globalThis !== "undefined" ? globalThis : window);

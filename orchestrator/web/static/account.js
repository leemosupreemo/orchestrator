// The hosted app's account screens: your computers, adding one with a code from `orchestrator connect`, and which
// one to open. Rendering and choices live here (no DOM, so they can be tested); app.js wires them to the page.
(function (root) {
  "use strict";

  const HOSTED_ORIGINS = ["https://swift-orch-web-20260923.web.app", "https://swift-orch-web-20260923.firebaseapp.com"];
  const INSTALL_COMMAND = 'pipx install "git+https://github.com/leemosupreemo/orchestrator.git"';
  // Add a Mac (over SSH) stays hidden until signed app releases are published: install-mac.sh and releases/macos.json
  // must be in this folder first (a test holds the two together). See docs/macos-desktop.md.
  const MAC_APP_RELEASED = false;
  const ENROLL_SCRIPT = "install-mac.sh";
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
  // A Mac that should run unattended (a Mac mini in a closet) reports these; orchestrator/mac_readiness.py has the same.
  const READINESS_FIXES = {
    "auto_login:off": "Turn on automatic login for this user (System Settings › Users & Groups). FileVault must be off.",
    "sleep:on": "Stop it sleeping: `sudo pmset -a sleep 0 disksleep 0`",
    "power_restart:off": "Restart after a power cut: `sudo pmset -a autorestart 1`",
    "xcode_license:not_accepted": "Accept Xcode's license: `sudo xcodebuild -license accept`",
    "login_item:needs_approval": "Allow Orchestrator in System Settings › General › Login Items (once, over Screen Sharing).",
  };
  function readinessFixes(readiness) {
    return Object.entries(readiness || {}).map(([check, state]) => READINESS_FIXES[`${check}:${state}`]).filter(Boolean);
  }

  const escapeHtml = root.Html.escape;

  function isHosted(origin = root.location?.origin) {
    return HOSTED_ORIGINS.includes(origin);
  }

  // Google, Apple and GitHub sign-in (Firebase) only work on domains the Firebase project authorizes: the hosted app and
  // "localhost". Not 127.0.0.1, and never a tunnel address, which changes every time the tunnel restarts.
  function providerSignInWorks(loc = root.location) {
    return isHosted(loc?.origin) || loc?.hostname === "localhost";
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

  // What someone wants to build, asked before any setup so they start from their idea, not from chores. Kept in this
  // browser until a computer turns it into a project: {pitch, name, existing} (existing: they already have code).
  const IDEA_KEY = "orchestrator_idea";

  function cleanIdea(value) {
    if (!value || typeof value !== "object") return null;
    const idea = {pitch: String(value.pitch || "").trim().slice(0, 300), name: String(value.name || "").trim().slice(0, 80),
                  existing: value.existing === true};
    return idea.pitch || idea.existing ? idea : null;
  }

  function pendingIdea(storage = root.localStorage) {
    try { return cleanIdea(JSON.parse(storage.getItem(IDEA_KEY) || "null")); } catch { return null; }
  }

  function saveIdea(value, storage = root.localStorage) {
    const idea = cleanIdea(value);
    try { if (idea) storage.setItem(IDEA_KEY, JSON.stringify(idea)); } catch { /* storage blocked: it's asked again */ }
    return idea;
  }

  function clearIdea(storage = root.localStorage) {
    try { storage.removeItem(IDEA_KEY); } catch { /* nothing kept */ }
  }

  // Where opening a computer goes with an idea waiting: the new-project form, filled in, or adding existing code.
  function ideaRoute(idea) {
    if (!idea) return "";
    if (idea.existing) return "#/projects";
    const query = new URLSearchParams();
    if (idea.pitch) query.set("pitch", idea.pitch);
    if (idea.name) query.set("name", idea.name);
    return `#/new-project?${query}`;
  }

  function ideaStep() {
    return `<form class="stack account-idea-form" data-account-form="idea">
        <label class="field"><span>What do you want to build?</span>
          <input name="pitch" required maxlength="300" autocomplete="off" placeholder="e.g. A turn-based word game to play with friends"></label>
        <label class="field"><span>Name it <span class="muted">(optional, a working name is fine)</span></span>
          <input name="name" maxlength="80" autocomplete="off"></label>
        <div class="row gap-10"><button type="submit" class="btn primary">Next: where it runs</button>
          <button type="button" class="btn ghost" data-account-action="idea-existing">I already have code</button></div>
      </form>`;
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
    if (m.updatable) {
      if (m.online) return {tone: "warn", text: "Connecting for remote access. Check that Remote access is on in the Orchestrator menu on this Mac."};
      return {tone: "muted", text: "Offline. Wake this Mac and open Orchestrator from Applications. Keep it running to use it from here."};
    }
    if (m.online) return {tone: "warn", text: "Online, but not reachable from the web yet. Restart it with `orchestrator ui --tunnel`."};
    if (m.last_seen) return {tone: "muted", text: `Offline · last seen ${ago(m.last_seen, now)}. Start \`orchestrator ui --tunnel\` on it.`};
    return {tone: "muted", text: "Waiting for it to start. Run `orchestrator ui --tunnel` on it."};
  }

  function code(text) {
    return escapeHtml(text).replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  function addSteps(pendingCodeValue = "", installCommand = INSTALL_COMMAND) {
    return `<ol class="account-steps">
        <li>Install Orchestrator on the computer with your projects:<pre class="mono" data-cli-install>${escapeHtml(installCommand)}</pre></li>
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

  // A download is offered only for a complete, versioned release, never a guessed URL.
  function macRelease(value) {
    if (!value || !/^\d+\.\d+\.\d+$/.test(value.version || "") || !Number.isSafeInteger(value.build) || value.build < 1
        || !/^[a-f0-9]{40}$/.test(value.commit || "") || !/^\d+\.\d+$/.test(value.minimum_macos || "")) return null;
    for (const arch of ["arm64", "x86_64"]) {
      const artifact = value[arch];
      if (!artifact || !/^[a-f0-9]{64}$/.test(artifact.sha256 || "") || artifact.sha256 === "0".repeat(64)) return null;
      try {
        const url = new URL(artifact.url);
        if (url.protocol !== "https:" || url.username || url.password || !url.pathname.endsWith(".dmg")) return null;
      } catch { return null; }
    }
    return value;
  }

  function renderMacSetup(value) {
    const release = macRelease(value);
    return `<div class="stack mac-setup">
      <h3>Set up your Mac</h3>
      <p>Install the Orchestrator app on your Mac, then use this website from your computer or phone.</p>
      <p class="muted">On a phone or tablet? Complete installation on your Mac first. Windows and Linux app setup is not available yet.</p>
      <ol class="account-steps">
        <li><strong>Download Orchestrator</strong>
          <div data-mac-downloads>${release ? `<p>Requires macOS ${escapeHtml(release.minimum_macos)} or later. Version ${escapeHtml(release.version)}.</p>
            <div class="row gap-10"><a class="btn primary" href="${escapeHtml(release.arm64.url)}">Download for Apple Silicon</a>
            <a class="btn" href="${escapeHtml(release.x86_64.url)}">Download for Intel</a></div>
            <p class="muted">Not sure which? Open Apple menu → About This Mac. “Chip” means Apple Silicon; “Processor: Intel” means Intel.</p>`
            : `<p class="notice">The Mac app download is not available yet. You can connect an app you already have, or use the advanced setup below.</p>`}</div>
        </li>
        <li><strong>Open the download</strong><p>Drag Orchestrator to Applications, then open it from Applications. The app includes its own Python and connection software.</p></li>
        <li><strong>Connect this Mac</strong><p>Choose <strong>Connect this Mac</strong> in the app. Your browser opens so you can confirm the connection. Keep the app running while you use Orchestrator.</p></li>
      </ol>
      <details class="account-add"><summary>Already have the app? Enter a connection code</summary>
        <p>Choose Connect this Mac in the app, then enter the code it shows.</p>
        <form class="row gap-10 account-code-form" data-account-form="code">
          <input name="code" required autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="9"
            placeholder="ABCD-2345" aria-label="Connection code" class="mono">
          <button type="submit" class="btn primary">Connect Mac</button>
        </form>
      </details>
      <details class="account-add"><summary>Advanced: command-line installation</summary>
        <p>This path requires Python 3.11 or later, Git, pipx, and cloudflared for remote access. Install and configure those tools first.</p>
        ${addSteps("", release ? `pipx install "git+https://github.com/leemosupreemo/orchestrator.git@v${release.version}"` : INSTALL_COMMAND)}
      </details>
    </div>`;
  }

  function shellQuote(value) {
    return /^[A-Za-z0-9_@%+=:,./~-]+$/.test(value) ? value : `'${String(value).replace(/'/g, `'"'"'`)}'`;
  }

  // The one-line command Add a Mac shows; `origin` is the hosted app serving the install script.
  function enrollCommand(token, project, origin) {
    return `curl -fsSL ${origin}/${ENROLL_SCRIPT} | sh -s -- --token ${shellQuote(token)} --project ${shellQuote(project || "PROJECT")}`;
  }

  function addMacSteps() {
    return `<div class="account-steps">
        <p class="muted">For a Mac you reach over SSH, like a Mac mini in a closet. It needs automatic login for the user Orchestrator runs as.</p>
        <form class="row gap-10" data-account-form="enroll">
          <input name="project" autocomplete="off" spellcheck="false" class="mono" aria-label="Project: a git URL or a folder on that Mac"
                 placeholder="git@github.com:you/app.git or ~/Projects/app">
          <button type="submit" class="btn primary">Make a command</button>
        </form>
        <div data-enroll-result></div>
      </div>`;
  }

  function renderEnroll(command, secondsLeft) {
    if (secondsLeft <= 0) return `<p class="muted">That command expired. Make a new one.</p>`;
    const minutes = Math.ceil(secondsLeft / 60);
    return `<p>Run this on the Mac, over SSH, as the user Orchestrator should work as. It works once, for ${minutes} more minute${minutes === 1 ? "" : "s"}:</p>
      <pre class="mono" data-enroll-command>${escapeHtml(command)}</pre>
      <button type="button" class="btn small ghost" data-account-action="copy-enroll">Copy</button>`;
  }

  // The host is what identifies a tunnel; the full URL (with any path) stays in the tooltip.
  function hostOf(url) {
    try { return new URL(url).host; } catch { return url || ""; }
  }

  function renderTroubleshoot(t) {
    if (!t) return "";
    const isLoadFailed = (t.error || "").includes("Load failed");
    const endpoint = t.endpoint || t.machine?.endpoint || "";
    const machineId = t.machine?.id || "";
    const machineName = t.machine?.name || "your computer";
    const desktopApp = t.machine?.updatable === true;
    return `<div class="account-troubleshoot card-b stack">
      <div class="account-troubleshoot-header row">
        <strong>⚠️ Couldn't connect to ${escapeHtml(machineName)}</strong>
        <span class="spacer"></span>
        <span class="pill fail">${escapeHtml(isLoadFailed ? "Load failed" : "Unreachable")}</span>
      </div>
      <p class="muted">Your browser could not reach <code class="account-address" title="${escapeHtml(endpoint)}">${escapeHtml(hostOf(endpoint) || "the tunnel")}</code>.</p>
      <div class="row gap-10">
        ${machineId ? `<button type="button" class="btn small primary" data-account-action="open" data-id="${escapeHtml(machineId)}">Retry</button>` : ""}
        ${endpoint ? `<a class="btn small" href="${escapeHtml(endpoint)}" target="_blank" rel="noopener">Open tunnel URL ↗</a>` : ""}
        ${machineId ? `<button type="button" class="btn small ghost" data-account-action="reset-tunnel" data-id="${escapeHtml(machineId)}" data-name="${escapeHtml(machineName)}">Reset tunnel</button>` : ""}
        ${desktopApp ? "" : `<button type="button" class="btn small ghost" data-account-action="copy-cmd" data-cmd="orchestrator ui --tunnel">Copy restart command</button>`}
      </div>
      ${desktopApp ? `<p>Wake this Mac, open Orchestrator from Applications, and check that <strong>Remote access</strong> is enabled in its menu. Keep the Mac awake, then choose Retry.</p>` : ""}
      <details class="account-troubleshoot-details">
        <summary>Why did this happen? (Troubleshooting tips)</summary>
        <ul>
          <li><strong>Mac asleep or lid closed:</strong> Cloudflare quick tunnels pause when a Mac sleeps. Wake the Mac, wait a few seconds, and click Retry.</li>
          <li><strong>Ad Blocker / Privacy DNS:</strong> Safari Content Blockers, AdGuard, NextDNS, or Brave often block <code>*.trycloudflare.com</code>. Try opening the tunnel URL directly or disabling content blockers for this site.</li>
          <li><strong>Connection needs restarting:</strong> Click <em>Reset tunnel</em> above${desktopApp ? " and then Retry." : " or restart <code>orchestrator ui --tunnel</code> in your terminal."}</li>
        </ul>
      </details>
    </div>`;
  }

  function renderMachines(machines, {email = "", message = "", troubleshoot = null, now, macAppReleased = MAC_APP_RELEASED, idea, release = null} = {}) {
    const list = machines || [];
    if (idea === undefined) idea = list.length ? null : pendingIdea();
    const askIdea = !list.length && !idea;
    const ideaRecap = !list.length && idea ? `<p class="account-idea">${idea.existing ? "You're bringing code you already have." : `Your idea: <strong>${escapeHtml(idea.pitch)}</strong>`}
        <button type="button" class="linklike" data-account-action="idea-change">Change</button></p>
        <p class="muted">Next, set up the computer it runs on. Orchestrator runs on your own computer: your code, model subscriptions and connected apps stay there.</p>` : "";
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
          ${!m.reachable ? `<div class="account-start-guide stack">
            ${!m.updatable ? `<div class="row gap-6 account-cmd-box">
              <span class="muted">To start:</span>
              <code class="mono">orchestrator ui --tunnel</code>
              <button type="button" class="btn small ghost" data-account-action="copy-cmd" data-cmd="orchestrator ui --tunnel" title="Copy command">Copy</button>
            </div>` : ""}
            <span class="account-listening"><span class="dot" aria-hidden="true"></span>Waiting for connection — opens automatically once started</span>
            ${!m.updatable ? `<details class="account-more-options">
              <summary>More ways to run</summary>
              <ul>
                <li><strong>Run at login in background:</strong> <code>orchestrator service install</code></li>
                <li><strong>Working on this computer right now?</strong> Run <code>orchestrator ui</code> in Terminal to open locally at <code>localhost:8000</code>.</li>
              </ul>
            </details>` : ""}
          </div>` : ""}
          ${readinessFixes(m.readiness).length ? `<details class="account-readiness"><summary>To run unattended, ${readinessFixes(m.readiness).length === 1 ? "one setting needs" : `${readinessFixes(m.readiness).length} settings need`} changing</summary>
            <ul>${readinessFixes(m.readiness).map((fix) => `<li>${code(fix)}</li>`).join("")}</ul></details>` : ""}
        </div>
        <div class="row gap-10">
          ${m.reachable ? `<button type="button" class="btn small primary" data-account-action="open" data-id="${escapeHtml(m.id)}">Open</button>` : (!m.updatable ? `<button type="button" class="btn small primary" data-account-action="copy-cmd" data-cmd="orchestrator ui --tunnel">Copy start command</button>` : "")}
          ${m.online ? `<button type="button" class="btn small ghost" data-account-action="reset-tunnel" data-id="${escapeHtml(m.id)}" data-name="${escapeHtml(m.name)}" title="Restart tunnel if connection is stale">Reset tunnel</button>` : ""}
          ${canUpdate ? `<button type="button" class="btn small ghost" data-account-action="update" data-id="${escapeHtml(m.id)}" data-name="${escapeHtml(m.name)}">Update</button>` : ""}
          <button type="button" class="btn small ghost" data-account-action="remove" data-id="${escapeHtml(m.id)}" data-name="${escapeHtml(m.name)}">Remove</button>
        </div>
      </div>`;
    }).join("");
    const anyReachable = list.some((m) => m.reachable);
    return `<div class="signin-wrap"><div class="signin-card account-card">
      <div>
        <h2 class="account-title">${list.length ? "Your computers" : askIdea ? "What do you want to build?" : "Set up where it runs"}</h2>
        <p class="muted account-sub">${list.length
          ? "Private and secure by design. Orchestrator runs on your own computer: your code, model subscriptions and connected apps stay there."
          : askIdea ? "Say it in a sentence. It becomes the start of your product's plan, and you can change it any time."
          : "Private and secure by design. Orchestrator runs on your own computer: your code, model subscriptions and connected apps stay there. Add it once and use it from anywhere."}</p>
        ${troubleshoot ? renderTroubleshoot(troubleshoot) : (message ? `<p class="account-message" role="alert">${code(message)}</p>` : (list.length && !anyReachable ? `<div class="account-offline-notice row gap-8"><span class="dot" aria-hidden="true"></span><span>Waiting for your computer to connect. Start Orchestrator on it to open your workspace.</span></div>` : ""))}
      </div>
      ${list.length ? `<div class="account-machines">${rows}</div>
        <details class="account-add"><summary>Add another computer</summary>${renderMacSetup(release)}</details>` : askIdea ? ideaStep() : ideaRecap + renderMacSetup(release)}
      ${macAppReleased ? `<details class="account-add"><summary>Add a Mac nobody sits at</summary>${addMacSteps()}</details>` : ""}
      <p class="muted account-foot">Signed in as ${escapeHtml(email)} · <button type="button" class="linklike" data-account-action="refresh">Refresh</button> · <button type="button" class="linklike" data-account-action="sign-out">Sign out</button></p>
    </div></div>`;
  }

  function renderPair(preview, codeValue, {email = ""} = {}) {
    return `<div class="signin-wrap"><div class="signin-card account-card">
      <div>
        <h2 class="account-title">Add this computer?</h2>
        <p class="muted account-sub">Code <span class="mono">${escapeHtml(formatCode(codeValue))}</span></p>
      </div>
      <div class="account-machine"><div class="account-machine-main">
        <strong>${escapeHtml(preview.name)}</strong>
        <span class="muted">${escapeHtml([preview.os, preview.version && `Orchestrator ${preview.version}`].filter(Boolean).join(" · "))}</span>
      </div></div>
      <p class="muted">It will be added to <strong>${escapeHtml(email)}</strong>, and you'll be able to open it from anywhere you sign in.
        Only connect a computer where you just chose <strong>Connect this Mac</strong> in Orchestrator or started command-line pairing yourself.</p>
      <div class="row gap-10">
        <button type="button" class="btn primary" data-account-action="claim">Add computer</button>
        <button type="button" class="btn ghost" data-account-action="cancel-pair">Cancel</button>
      </div>
    </div></div>`;
  }

  root.Account = {
    HOSTED_ORIGINS, INSTALL_COMMAND, MACHINE_KEY, CODE_LENGTH, REQUIRED_RUNNER_API, UPDATE_HINT, UPDATE_PROGRESS, READINESS_FIXES, readinessFixes, MAC_APP_RELEASED,
    enrollCommand, renderEnroll, outdated, macRelease, renderMacSetup,
    isHosted, active, normalizeCode, formatCode, pendingCode, clearPendingCode, pickMachine, machineStatus,
    renderMachines, renderPair, providerSignInWorks, pendingIdea, saveIdea, clearIdea, ideaRoute,
  };
})(typeof globalThis !== "undefined" ? globalThis : window);

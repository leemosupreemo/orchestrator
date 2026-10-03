(function (root) {
  "use strict";

  const registry = [
    {
      id: "models-instructions",
      label: "Models & instructions",
      entries: [
        {id: "ai", label: "Add an AI", description: "Get an AI to do the work: free options first, with install and sign-in steps.", route: "#/config/ai", enabled: true, status: null},
        {id: "models", label: "Models", description: "Choose the AI models available to jobs.", route: "#/config/models", enabled: true, status: null},
        {id: "api-keys", label: "API Keys", description: "Connect AI providers without displaying saved secrets.", route: "#/config/api-keys", enabled: true, ownerOnly: true, status: null},
        {id: "ai-instructions", label: "Instructions for AI helpers", description: "What AI helpers should know about your project, and how each role behaves.", route: "#/config/ai-instructions", enabled: true, ownerOnly: true, status: null},
      ],
    },
    {
      id: "projects-machines",
      label: "Projects & machines",
      entries: [
        {id: "base-branch", label: "Base Branch", description: "Choose the branch jobs compare their work against.", route: "#/config/base-branch", enabled: true, status: null},
        {id: "projects", label: "Projects", description: "Add, remove, or switch the active project.", route: "#/projects", enabled: true, status: null},
        {id: "archived-jobs", label: "Archived Jobs", description: "Review and restore completed work.", route: "#/config/archived-jobs", enabled: true, status: null},
        {id: "fleet", label: "Machines", description: "Where jobs run: add and manage build machines (the fleet).", route: "#/config/fleet", enabled: true, ownerOnly: true, status: null},
        {id: "computers", label: "Your computers", description: "Switch to another of your computers, or add one.", route: "#/computers", enabled: true, status: null, hostedOnly: true},
        {id: "access", label: "Who can sign in", description: "Choose whose Google sign-ins reach this computer, and end sign-ins.", route: "#/config/access", enabled: true, ownerOnly: true, status: null},
      ],
    },
    {
      id: "delivery-notifications",
      label: "Delivery & notifications",
      entries: [
        {id: "email", label: "Email Notifications", description: "Choose recipients and configure the sender.", route: "#/config/email", enabled: true, ownerOnly: true, status: null},
        {id: "chat", label: "Slack & chat alerts", description: "Get pinged when something needs you, even with the tab closed.", route: "#/config/chat", enabled: true, ownerOnly: true, status: null},
        {id: "firebase", label: "Tester builds (Firebase)", description: "Send builds to testers through Firebase App Distribution, and set up signing.", route: "#/config/firebase", enabled: true, ownerOnly: true, status: null},
        {id: "xcode-cloud", label: "Xcode Cloud", description: "Configure cloud builds and CI workflows.", route: "#/config/xcode-cloud", enabled: true, status: null},
      ],
    },
    {
      id: "help-health",
      label: "Help & health",
      entries: [
        {id: "documentation", label: "Documentation", description: "Read Orchestrator and project guides.", route: "#/config/documentation", enabled: true, status: null},
        {id: "setup-wizard", label: "Setup Wizard", description: "Walk through initial project setup.", route: "#/config/setup-wizard", enabled: true, ownerOnly: true, status: null},
        {id: "audit", label: "Tool check", description: "Check the tools and machines jobs need are ready (prerequisite audit).", route: "#/config/audit", enabled: true, status: null},
        {id: "self-tests", label: "Orchestrator health check", description: "Run Orchestrator's own self-tests to confirm it works on this machine.", route: "#/config/self-tests", enabled: true, status: null},
        {id: "updates", label: "Updates", description: "Update Orchestrator on local and remote machines.", route: "#/config/updates", enabled: true, ownerOnly: true, status: null},
      ],
    },
  ];

  const escapeHtml = root.Html.escape;

  function copyEntry(entry) {
    return {...entry};
  }

  // "Your computers" only means something on the hosted app, where you sign in to an account.
  function shown(entry) {
    return !entry.hostedOnly || Boolean(root.Account && root.Account.active());
  }

  function groups(role = "owner") {
    return registry.map((group) => ({
      id: group.id,
      label: group.label,
      entries: group.entries.filter((entry) => shown(entry) && (role !== "member" || !entry.ownerOnly)).map(copyEntry),
    })).filter((group) => group.entries.length);
  }

  function find(id) {
    for (const group of registry) {
      const entry = group.entries.find((candidate) => candidate.id === id);
      if (entry) return copyEntry(entry);
    }
    return null;
  }

  function renderMenu(role = "owner") {
    return groups(role).map((group) => `
      <div class="configuration-menu-group" role="group" aria-labelledby="configuration-menu-${escapeHtml(group.id)}">
        <div class="configuration-menu-heading" id="configuration-menu-${escapeHtml(group.id)}">${escapeHtml(group.label)}</div>
        ${group.entries.map((entry) => entry.enabled
          ? `<button type="button" role="menuitem" data-config-route="${escapeHtml(entry.route)}"><span>${escapeHtml(entry.label)}</span><small>${escapeHtml(entry.description)}</small></button>`
          : `<button type="button" role="menuitem" disabled aria-disabled="true"><span>${escapeHtml(entry.label)}</span><small>${escapeHtml(entry.status)}</small></button>`
        ).join("")}
      </div>
    `).join("");
  }

  function resolve(section, role = "owner") {
    const entry = find(section);
    return entry && entry.enabled && !(role === "member" && entry.ownerOnly) && entry.route && entry.route.startsWith("#/config/") ? entry : null;
  }

  function createMenuController({trigger, menu, document, navigate}) {
    function open() {
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      const first = menu.querySelector?.("[data-config-route]:not([disabled])");
      first?.focus();
    }

    function close({restoreFocus = false} = {}) {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      if (restoreFocus) trigger.focus();
    }

    function onTriggerClick(event) {
      event.preventDefault();
      if (menu.hidden) open();
      else close({restoreFocus: true});
    }

    function onKeyDown(event) {
      if (event.key === "Escape" && !menu.hidden) {
        event.preventDefault();
        close({restoreFocus: true});
      }
    }

    function onPointerDown(event) {
      if (!menu.hidden && !menu.contains(event.target) && !trigger.contains(event.target)) close();
    }

    function onMenuClick(event) {
      const item = event.target.closest?.("[data-config-route]");
      if (!item || item.disabled) return;
      const route = item.getAttribute("data-config-route");
      if (!route) return;
      event.preventDefault();
      close();
      navigate(route);
    }

    trigger.addEventListener("click", onTriggerClick);
    menu.addEventListener("click", onMenuClick);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);

    return {
      open,
      close,
      destroy() {
        trigger.removeEventListener("click", onTriggerClick);
        menu.removeEventListener("click", onMenuClick);
        document.removeEventListener("keydown", onKeyDown);
        document.removeEventListener("pointerdown", onPointerDown);
      },
    };
  }

  function statusLabel(key) {
    if (key.saved) return "Saved";
    if (key.env) return "From environment";
    return "Not set";
  }

  function renderChooser(state = {}) {
    const html = groups(state.viewer?.role || "owner").map((group) => `
      <section class="configuration-chooser-group" aria-labelledby="configuration-chooser-${escapeHtml(group.id)}">
        <h2 id="configuration-chooser-${escapeHtml(group.id)}">${escapeHtml(group.label)}</h2>
        <div class="configuration-chooser-list">
          ${group.entries.map((entry) => entry.enabled
            ? `<a class="configuration-choice" href="${escapeHtml(entry.route)}"><strong>${escapeHtml(entry.label)}</strong><span>${escapeHtml(entry.description)}</span></a>`
            : `<button type="button" class="configuration-choice is-disabled" disabled><strong>${escapeHtml(entry.label)}</strong><span>${escapeHtml(entry.description)}</span><small>${escapeHtml(entry.status)}</small></button>`
          ).join("")}
        </div>
      </section>
    `).join("");
    return {
      title: "Configuration",
      sub: "Choose one area to view or change.",
      html: `<div class="configuration-chooser">${html}</div>`,
    };
  }

  function renderApiKeys(state) {
    const keys = Array.isArray(state.keys) ? state.keys : [];
    const rows = keys.map((key) => {
      const isOllama = key.id === "ollama_api_key";
      const status = statusLabel(key);
      return `<div class="configuration-setting-row">
        <div class="main-col">
          <strong>${escapeHtml(key.label)}</strong>
          <span class="configuration-status">${escapeHtml(status)}</span>
          ${isOllama ? `<small>Host URL: ${escapeHtml(state.ollama_host || "Default local Ollama host")}</small>` : ""}
        </div>
        <div class="row">
          <button type="button" class="btn small" data-config-action="key-set" data-id="${escapeHtml(key.id)}" data-label="${escapeHtml(key.label)}">${key.saved ? "Replace" : "Set"}</button>
          ${key.saved ? `<button type="button" class="btn small danger" data-config-action="key-clear" data-id="${escapeHtml(key.id)}" data-label="${escapeHtml(key.label)}">Clear</button>` : ""}
        </div>
      </div>`;
    }).join("");
    return {
      title: "API Keys",
      sub: "Connect AI providers. Saved key values are never displayed.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Provider keys</h2><span class="count">${keys.length}</span></div>
          <div class="configuration-setting-list">${rows || `<div class="empty">No providers are available.</div>`}</div>
        </section>
      </div>`,
    };
  }

  // Add an AI: each provider with its cost, whether it's ready here, and the exact install and sign-in steps.
  function renderAi(state) {
    const ai = state.ai || {providers: [], plugins: []};
    const copyable = (command) => command
      ? `<div class="setup-hint"><code>${escapeHtml(command)}</code><button type="button" class="btn small ghost" data-setup-copy="${escapeHtml(command)}">Copy</button></div>` : "";
    const badge = (p) => p.ready ? `<span class="pill done">Ready</span>`
      : p.installed ? `<span class="pill">Installed, not signed in</span>` : `<span class="pill">Not installed</span>`;
    const cards = ai.providers.map((p) => `<section class="card configuration-card ai-provider" data-provider="${escapeHtml(p.id)}">
        <div class="card-h"><h2>${escapeHtml(p.name)}</h2>${badge(p)}</div>
        <p><strong>${escapeHtml(p.cost_label)}.</strong> ${escapeHtml(p.what)}</p>
        ${p.ready ? "" : `${p.installed ? "" : `<p class="muted">1. Install it:</p>${copyable(p.install)}${copyable(p.install_alt)}${p.install_note ? `<small>${escapeHtml(p.install_note)}</small>` : ""}`}
          <p class="muted">${p.installed ? "Then" : "2."} ${escapeHtml(p.sign_in)}</p>`}
        <p><a href="${escapeHtml(p.link)}" target="_blank" rel="noopener">Official instructions ↗</a>${p.key ? ` · <a href="#/config/api-keys">Add an API key instead</a>` : ""}</p>
      </section>`).join("");
    const plugins = ai.plugins.map((p) => `<li><strong>${escapeHtml(p.name)}</strong>: ${escapeHtml(p.why)}</li>`).join("");
    return {
      title: "Add an AI",
      sub: ai.any_ready ? "You have at least one AI ready. Add more to choose between them per job." : "Orchestrator needs at least one AI to plan, build and review. Free options are first.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <p class="muted">Run the install commands in Terminal on the computer Orchestrator runs on, then come back: this page checks again each time it opens. Prices and free allowances change; last checked ${escapeHtml(ai.checked || "")}.</p>
        ${cards || `<div class="empty">Couldn't check the AI tools on this computer.</div>`}
        <section class="card configuration-card">
          <div class="card-h"><h2>Helpful plugins for your AI tools</h2></div>
          <p class="muted">Optional. They give the AI richer context while it works; Orchestrator runs without them.</p>
          <ul>${plugins}</ul>
        </section>
      </div>`,
    };
  }

  function renderBaseBranch(state) {
    const branches = Array.isArray(state.branches) ? state.branches : [];
    const options = branches.map((branch) => `<option value="${escapeHtml(branch)}"${branch === state.base_branch ? " selected" : ""}>${escapeHtml(branch)}</option>`).join("");
    return {
      title: "Base Branch",
      sub: "Choose the branch jobs use as their comparison point.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Comparison branch</h2></div>
          <form class="card-b stack" id="config-base-branch-form">
            <label class="field"><span>Base branch</span><select name="branch" required>${options}</select>
              <small>Only local branches are available.</small>
            </label>
            <div class="row end"><button class="btn primary" type="submit">Save base branch</button></div>
          </form>
        </section>
      </div>`,
    };
  }

  function renderArchivedJobs(state) {
    const archived = Array.isArray(state.archived) ? state.archived : [];
    const rows = archived.map((job) => `<div class="configuration-setting-row">
      <div class="main-col">
        <strong><span class="mono">${escapeHtml(job.job_id)}</span> ${escapeHtml(job.title)}</strong>
        <span class="configuration-status">${escapeHtml(job.status)}</span>
        ${job.corrupt ? `<small>Corrupt archive data</small>` : ""}
      </div>
      ${job.corrupt
        ? `<button type="button" class="btn small" disabled>Unavailable</button>`
        : `<button type="button" class="btn small" data-config-action="archive-restore" data-id="${escapeHtml(job.id)}">Restore</button>`}
    </div>`).join("");
    return {
      title: "Archived Jobs",
      sub: "Restore completed work to the active jobs list.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Archived jobs</h2><span class="count">${archived.length}</span></div>
          <div class="configuration-setting-list">${rows || `<div class="empty">No archived jobs</div>`}</div>
        </section>
      </div>`,
    };
  }

  function renderEmail(state) {
    const email = state.email || {};
    const recipients = Array.isArray(email.recipients) ? email.recipients : [];
    const isResend = email.provider === "resend";
    const ready = isResend
      ? Boolean(email.resend_from_email && email.resend_api_key_set)
      : Boolean(email.smtp_email && email.smtp_password_set);
    const readiness = ready
      ? `${isResend ? "Resend" : "Gmail"} is ready`
      : `${isResend ? "Resend" : "Gmail"} needs more information`;
    const recipientRows = recipients.map((recipient) => `<div class="configuration-setting-row">
      <span class="mono">${escapeHtml(recipient)}</span>
      <button type="button" class="btn small danger" data-config-action="email-remove" data-email="${escapeHtml(recipient)}">Remove</button>
    </div>`).join("");
    return {
      title: "Email Notifications",
      sub: "Choose who receives updates and how Orchestrator sends them.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Sender</h2><button type="button" class="btn small" data-config-action="email-sender">Configure sender</button></div>
          <div class="card-b stack">
            <strong>${escapeHtml(readiness)}</strong>
            <span class="configuration-status">${isResend
              ? `${escapeHtml(email.resend_from_email || "No from address")} · ${email.resend_api_key_set ? "API key saved" : "API key not set"}`
              : `${escapeHtml(email.smtp_email || "No Gmail address")} · ${email.smtp_password_set ? "App password saved" : "App password not set"}`}</span>
          </div>
        </section>
        <section class="card configuration-card">
          <div class="card-h"><h2>Recipients</h2><div class="row">
            <button type="button" class="btn small" data-config-action="email-add">Add recipient</button>
            ${recipients.length ? `<button type="button" class="btn small primary" data-config-action="email-test">Send test email</button>` : ""}
          </div></div>
          <div class="configuration-setting-list">${recipientRows || `<div class="empty">No recipients yet</div>`}</div>
        </section>
      </div>`,
    };
  }

  function renderChat(state) {
    const hook = state.webhook || {};
    return {
      title: "Slack & chat alerts",
      sub: "A message when a job needs you, and when a run finishes or fails. Works with any Slack-compatible incoming webhook.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Webhook</h2><div class="row">
            <button type="button" class="btn small" data-config-action="chat-set">${hook.set ? "Change" : "Add webhook"}</button>
            ${hook.set ? `<button type="button" class="btn small primary" data-config-action="chat-test">Send test message</button>
            <button type="button" class="btn small danger" data-config-action="chat-clear">Remove</button>` : ""}
          </div></div>
          <div class="card-b stack">
            <strong>${hook.set ? "Connected" : "Not set up"}</strong>
            <span class="configuration-status">${hook.set ? `Sending to ${escapeHtml(hook.host)}. The full address is stored but not shown.` : "Paste an https:// incoming-webhook URL from Slack (or a compatible tool)."}</span>
            <span class="muted">Runs while the Orchestrator web server is running. Browser alerts (sidebar button) and worker emails work separately.</span>
          </div>
        </section>
      </div>`,
    };
  }

  const EMAIL_SOURCES = {
    git: "from git config",
    environment: "from ORCHESTRATOR_ALLOWED_EMAILS",
    project: "from project.json",
    settings: "added here",
  };

  function shortDate(seconds) {
    return new Date(seconds * 1000).toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
  }

  function renderAccess(state) {
    const emails = Array.isArray(state.allowed_emails) ? state.allowed_emails : [];
    const signIns = Array.isArray(state.sign_ins) ? state.sign_ins : [];
    const emailRows = emails.map(({email, source}) => `<div class="configuration-setting-row">
      <span><span class="mono">${escapeHtml(email)}</span> <span class="muted">${escapeHtml(EMAIL_SOURCES[source] || source)}</span></span>
      ${source === "settings" ? `<button type="button" class="btn small danger" data-config-action="access-remove" data-email="${escapeHtml(email)}">Remove</button>` : ""}
    </div>`).join("");
    const signInRows = signIns.map((s) => `<div class="configuration-setting-row">
      <span><span class="mono">${escapeHtml(s.email)}</span>${s.current ? ` <span class="pill">This browser</span>` : ""}
        <span class="muted">Signed in ${escapeHtml(shortDate(s.created))} · last used ${escapeHtml(shortDate(s.last_seen))}</span></span>
      <button type="button" class="btn small danger" data-config-action="access-revoke" data-id="${escapeHtml(s.id)}"${s.current ? " data-current" : ""}>${s.current ? "Sign out" : "End"}</button>
    </div>`).join("");
    const events = Array.isArray(state.audit) ? state.audit : [];
    const eventText = (entry) => {
      const who = escapeHtml(entry.who || "Someone");
      if (entry.event === "sign_in") return `${who} signed in`;
      if (entry.event === "sign_in_refused") return `${who} was refused sign-in`;
      if (entry.event === "sign_out") return `${who} signed out`;
      if (entry.event === "sign_in_ended") return `${who} ended a sign-in`;
      if (entry.event === "email_allowed") return `${who} allowed ${escapeHtml(entry.email || "an email")}`;
      if (entry.event === "email_removed") return `${who} removed ${escapeHtml(entry.email || "an email")}`;
      if (entry.event === "settings_changed") return `${who} changed ${escapeHtml(entry.part || "configuration")} settings`;
      if (entry.event === "run_started") return `${who} started ${escapeHtml(entry.action || "a run")}`;
      if (entry.event === "denied") return `${who} was denied permission to ${escapeHtml(entry.what || "perform an owner action")}`;
      return `${who}: ${escapeHtml(String(entry.event || "activity").replaceAll("_", " "))}`;
    };
    const auditRows = events.map((entry) => `<div class="configuration-setting-row">
      <span>${eventText(entry)}<span class="muted">${entry.ip ? `${escapeHtml(entry.ip)} · ` : ""}${escapeHtml(shortDate(entry.at))}</span></span>
    </div>`).join("");
    return {
      title: "Who can sign in",
      sub: "People with these emails can sign in with Google and use this computer from anywhere. Each sign-in lasts 30 days.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Allowed emails</h2><button type="button" class="btn small" data-config-action="access-add">Add email</button></div>
          <div class="configuration-setting-list">${emailRows || `<div class="empty">No one yet, so sign-in is closed. The access token still works.</div>`}</div>
        </section>
        <section class="card configuration-card">
          <div class="card-h"><h2>Signed in now</h2><span class="count">${signIns.length}</span></div>
          <div class="configuration-setting-list">${signInRows || `<div class="empty">No one is signed in with an email. The owner's access token isn't listed here.</div>`}</div>
        </section>
        <section class="card configuration-card">
          <div class="card-h"><h2>Recent security activity</h2><span class="count">${events.length}</span></div>
          <div class="configuration-setting-list">${auditRows || `<div class="empty">No security activity recorded yet.</div>`}</div>
        </section>
      </div>`,
    };
  }

  function renderDocumentation(state) {
    const docs = Array.isArray(state.docs) ? state.docs : [];
    const sections = ["Orchestrator docs", "Project docs"].map((section) => {
      const entries = docs.filter((doc) => doc.section === section);
      if (!entries.length) return "";
      return `<section class="card configuration-card" aria-labelledby="configuration-docs-${section === "Orchestrator docs" ? "orchestrator" : "project"}">
        <div class="card-h"><h2 id="configuration-docs-${section === "Orchestrator docs" ? "orchestrator" : "project"}">${escapeHtml(section)}</h2><span class="count">${entries.length}</span></div>
        <ul class="configuration-document-list">${entries.map((doc) => `<li>
          <span>${escapeHtml(doc.name)}</span>
          <button type="button" class="btn small" data-config-action="document-read" data-id="${escapeHtml(doc.id)}">Read</button>
        </li>`).join("")}</ul>
      </section>`;
    }).join("");
    return {
      title: "Documentation",
      sub: "Read guides for Orchestrator and the active project.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        ${sections || `<div class="empty">No documentation is available.</div>`}
      </div>`,
    };
  }

  function renderModels(state) {
    const modelsData = state.models || {};
    const assignments = modelsData.assignments || {};
    const available = modelsData.available || [];
    const roles = [
      {id: "architect", label: "Senior Architect", desc: "Oversees plan, architecture standards, and design"},
      {id: "planner", label: "Planner Agent", desc: "Decomposes requirements and investigates codebase"},
      {id: "builder", label: "Builder Agent", desc: "Implements code changes, refactors, and fixes"},
      {id: "reviewer", label: "Reviewer Agent", desc: "Reviews diffs, analyzes tests and regression risks"}
    ];
    const formRows = roles.map((role) => {
      const currentVal = assignments[role.id] || "claude-sonnet-4-6";
      const options = available.map((m) => `<option value="${escapeHtml(m.id)}"${m.id === currentVal ? " selected" : ""}>${escapeHtml(m.label)} (${escapeHtml(m.provider)})</option>`).join("");
      return `<div class="configuration-setting-row">
        <div class="main-col">
          <strong>${escapeHtml(role.label)}</strong>
          <small>${escapeHtml(role.desc)}</small>
        </div>
        <select name="${escapeHtml(role.id)}" class="model-select">${options}</select>
      </div>`;
    }).join("");
    return {
      title: "AI Model Team",
      sub: "Assign default AI models for each specialized role in the orchestrator team.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Team Role Assignments</h2></div>
          <form class="card-b stack" id="config-models-form">
            <div class="configuration-setting-list">${formRows}</div>
            <div class="row end" style="margin-top:16px;">
              <button class="btn primary" type="submit">Save model team</button>
            </div>
          </form>
        </section>
      </div>`,
    };
  }

  function renderAiInstructions(state) {
    const rolePrompts = Array.isArray(state.role_prompts) ? state.role_prompts : [];
    const promptCards = rolePrompts.map((rp) => `
      <div class="configuration-setting-row" style="align-items:flex-start;">
        <div class="main-col">
          <strong>${escapeHtml(rp.name)}</strong>
          <span class="configuration-status">${rp.custom ? "Custom instructions active" : "Using system defaults"}</span>
          <small>${escapeHtml(rp.description)} (${escapeHtml(rp.file)})</small>
        </div>
        <div class="row">
          <button type="button" class="btn small" data-config-action="prompt-edit" data-id="${escapeHtml(rp.id)}" data-name="${escapeHtml(rp.name)}" data-content="${escapeHtml(rp.content || "")}">${rp.custom ? "Edit" : "Customize"}</button>
          ${rp.custom ? `<button type="button" class="btn small danger" data-config-action="prompt-revert" data-id="${escapeHtml(rp.id)}">Revert</button>` : ""}
        </div>
      </div>
    `).join("");
    return {
      title: "Instructions for AI helpers",
      sub: "Customize system prompts and behavioral guidelines for each specialized agent role.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Specialized Agent Roles</h2><span class="count">${rolePrompts.length}</span></div>
          <div class="configuration-setting-list">${promptCards || `<div class="empty">No role prompts found.</div>`}</div>
        </section>
      </div>`,
    };
  }

  function renderFleet(state) {
    const machines = Array.isArray(state.machines) ? state.machines : [];
    const rows = machines.map((m) => `
      <div class="configuration-setting-row">
        <div class="main-col">
          <strong>${escapeHtml(m.name)} <span class="badge ${m.enabled ? "good" : ""}">${m.enabled ? "Active" : "Disabled"}</span></strong>
          <span class="configuration-status">${escapeHtml(m.mode === "local" ? "Local machine" : (m.ssh_target || "Remote SSH"))} · Priority ${escapeHtml(m.priority)}</span>
          <small>Roles: ${(m.roles || []).join(", ") || "General"} ${m.xcode ? "· Xcode ready" : ""} ${m.simulator ? "· Simulator ready" : ""}</small>
        </div>
        <div class="row">
          ${m.mode !== "local" ? `<button type="button" class="btn small" data-config-action="machine-install" data-name="${escapeHtml(m.name)}">Install</button>` : ""}
          <button type="button" class="btn small" data-config-action="machine-toggle" data-name="${escapeHtml(m.name)}">${m.enabled ? "Disable" : "Enable"}</button>
          ${m.mode !== "local" ? `<button type="button" class="btn small danger" data-config-action="machine-remove" data-name="${escapeHtml(m.name)}">Remove</button>` : ""}
        </div>
      </div>
    `).join("");
    return {
      title: "Machines",
      sub: "Manage local and remote SSH machines for distributed builds, tests, and code generation.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h">
            <h2>Build & Test Machines</h2>
            <div class="row">
              <button type="button" class="btn small" data-config-action="fleet-add">Add worker</button>
              <button type="button" class="btn small" data-config-action="fleet-check">Check health</button>
              <button type="button" class="btn small" data-config-action="fleet-sync">Sync code</button>
              <button type="button" class="btn small primary" data-config-action="fleet-llm">Test LLMs</button>
            </div>
          </div>
          <div class="configuration-setting-list">${rows || `<div class="empty">No worker machines configured yet.</div>`}</div>
        </section>
      </div>`,
    };
  }

  function renderFirebase(state) {
    const fb = state.firebase || {};
    return {
      title: "Tester builds (Firebase)",
      sub: "Configure automated beta releases and tester distribution.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Distribution Settings</h2><div class="row"><button type="button" class="btn small primary" data-config-action="distribute-now">Distribute current branch</button></div></div>
          <form class="card-b stack" id="config-firebase-form">
            <div class="notice ${fb.cli_installed ? "good" : ""}">
              ${fb.cli_installed ? "✓ Firebase CLI is installed on this system." : "⚠️ Firebase CLI is not installed. Run 'npm install -g firebase-tools' to enable tester releases."}
            </div>
            <label class="field"><span>Firebase App ID</span><input type="text" name="firebase_app_id" value="${escapeHtml(fb.app_id || "")}" placeholder="1:1234567890:ios:abcdef123456"><small>From Firebase Project Settings &gt; General &gt; Your apps.</small></label>
            <label class="field"><span>Tester Groups</span><input type="text" name="firebase_tester_groups" value="${escapeHtml(fb.tester_groups || "testers")}" placeholder="testers, qa-team"><small>Comma-separated tester group names configured in Firebase Console.</small></label>
            <label class="field"><span>Tester invite link (optional)</span><input type="url" name="firebase_invite_url" value="${escapeHtml(fb.invite_url || "")}" placeholder="https://appdistribution.firebase.dev/i/…"><small>In Firebase Console &gt; App Distribution &gt; Testers &amp; Groups, open a group and copy its invite link. People who open it can join the group and install builds without you adding them one by one. Shown on the Delivery page to copy and share.</small></label>
            <label class="field"><span>Service Account Key Path (optional)</span><input type="text" name="firebase_service_account_path" value="${escapeHtml(fb.service_account_path || "")}" placeholder="~/.orchestrator/firebase-service-account.json"><small>Or set GOOGLE_APPLICATION_CREDENTIALS in your environment.</small></label>
            <div class="row end"><button class="btn primary" type="submit">Save settings</button></div>
          </form>
        </section>
      </div>`,
    };
  }

  function renderXcodeCloud(state) {
    const xc = state.xcode_cloud || {};
    return {
      title: "Xcode Cloud & CI",
      sub: "Configure continuous integration workflows with Xcode Cloud.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Xcode Cloud Integration</h2></div>
          <div class="card-b stack">
            <div class="notice ${xc.configured ? "good" : ""}">
              ${xc.configured ? "✓ Xcode Cloud CI scripts / config detected in this repository." : "ℹ️ No Xcode Cloud ci_scripts directory found in project root."}
            </div>
            <p>Orchestrator can automatically run pre-build checks and linting during Xcode Cloud runs via <code>ci_scripts/ci_post_clone.sh</code>.</p>
            <div class="row">
              <button type="button" class="btn small" data-config-action="xcode-setup">Configure ci_scripts</button>
            </div>
          </div>
        </section>
      </div>`,
    };
  }

  function renderUpdates(state) {
    return {
      title: "Updates",
      sub: "Keep Swift Orchestrator up to date across your local machine and fleet.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Orchestrator Software Updates</h2></div>
          <div class="card-b stack">
            <p>Updates pull the latest improvements, role prompts, model mappings, and bug fixes from git origin and re-install dependencies.</p>
            <div class="row">
              <button type="button" class="btn primary" data-config-action="update-local">Update Orchestrator (Local)</button>
              <button type="button" class="btn" data-config-action="update-fleet">Update Fleet-Wide</button>
            </div>
          </div>
        </section>
      </div>`,
    };
  }

  function renderAudit(state) {
    return {
      title: "Tool check",
      sub: "Verify developer tools, git, python, and environment readiness.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Are the tools ready?</h2><button type="button" class="btn primary small" data-config-action="run-audit">Run tool check</button></div>
          <div class="card-b stack">
            <p>Checks Xcode Command Line Tools, Python 3.10+, Git, GitHub CLI (gh), model API keys, and simulator runtimes.</p>
          </div>
        </section>
      </div>`,
    };
  }

  function renderSelfTests(state) {
    return {
      title: "Orchestrator health check",
      sub: "Run validation suites to test Orchestrator functionality.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Self-tests</h2><button type="button" class="btn primary small" data-config-action="run-self-tests">Run All Self-Tests</button></div>
          <div class="card-b stack">
            <p>Runs the test suite across job scheduling, worker tools, stack detection, and model routing.</p>
          </div>
        </section>
      </div>`,
    };
  }

  function renderSetupWizard(state) {
    return {
      title: "Setup Wizard",
      sub: "Walk through project configuration and environment setup.",
      html: `<div class="configuration-page">
        <a class="configuration-back" href="#/config">← All configuration</a>
        <section class="card configuration-card">
          <div class="card-h"><h2>Interactive Setup Wizard</h2><button type="button" class="btn primary small" data-config-action="launch-wizard">Launch Wizard</button></div>
          <div class="card-b stack">
            <p>Walks step-by-step through Xcode project detection, scheme selection, test targets, API keys, and notification channels.</p>
          </div>
        </section>
      </div>`,
    };
  }

  function render(section, state = {}) {
    if (section === "ai") return renderAi(state);
    if (section === "api-keys") return renderApiKeys(state);
    if (section === "base-branch") return renderBaseBranch(state);
    if (section === "archived-jobs") return renderArchivedJobs(state);
    if (section === "email") return renderEmail(state);
    if (section === "chat") return renderChat(state);
    if (section === "access") return renderAccess(state);
    if (section === "documentation") return renderDocumentation(state);
    if (section === "models") return renderModels(state);
    if (section === "ai-instructions") return renderAiInstructions(state);
    if (section === "fleet") return renderFleet(state);
    if (section === "firebase") return renderFirebase(state);
    if (section === "xcode-cloud") return renderXcodeCloud(state);
    if (section === "updates") return renderUpdates(state);
    if (section === "audit") return renderAudit(state);
    if (section === "self-tests") return renderSelfTests(state);
    if (section === "setup-wizard") return renderSetupWizard(state);
    return renderChooser(state);
  }

  function applyRole(role, document) {
    document.querySelectorAll("[data-owner-only]").forEach((element) => {
      element.hidden = role === "member";
    });
  }

  function canRunAction(action, state = {}) {
    return state.you?.role !== "member" || !state.actions?.[action]?.owner_only;
  }

  function createMutationGuard(send) {
    let pending = false;
    return async function run(request) {
      if (pending) return null;
      pending = true;
      try {
        return await send(request);
      } finally {
        pending = false;
      }
    };
  }

  function routeMatches(current, section) {
    if (!current || current.page !== "config") return false;
    return (current.args?.[0] || undefined) === (section || undefined);
  }

  root.ConfigurationPages = {
    groups,
    find,
    renderMenu,
    resolve,
    createMenuController,
    render,
    createMutationGuard,
    routeMatches,
    applyRole,
    canRunAction,
  };
})(typeof globalThis !== "undefined" ? globalThis : window);

(function (root) {
  "use strict";

  // Grouped by what a setting affects, the way GitHub splits a repository's settings from your own: this project,
  // the AI, the computers and who reaches them, and alerts. Each setting has one home: environment checks are on
  // Readiness, Slack alerts on Connections, guides in Docs, archived jobs on Home, screen setup in UX review.
  // Entries with `listed: false` are left out of menus; their routes still work.
  const registry = [
    {
      // Pages of their own, listed here so every setting is reached from one place (Settings in the sidebar).
      id: "setup",
      label: "Set up",
      entries: [
        {id: "readiness", label: "Readiness", description: "Whether jobs can run here: what's missing, build tools and checks.", route: "#/readiness", enabled: true, status: null},
        {id: "connections", label: "Connections", description: "Jira, Trello, Sentry and Figma for jobs, and Slack alerts.", route: "#/connections", enabled: true, status: null},
        {id: "projects", label: "Projects", description: "Add, remove, or switch the active project.", route: "#/projects", enabled: true, status: null},
      ],
    },
    {
      id: "project",
      label: "This project",
      entries: [
        {id: "base-branch", label: "Base branch", description: "The branch jobs compare their work against.", route: "#/config/base-branch", enabled: true, status: null},
        {id: "firebase", label: "Tester builds (Firebase)", description: "Send builds to testers through Firebase App Distribution, and set up signing.", route: "#/config/firebase", enabled: true, ownerOnly: true, status: null},
        {id: "xcode-cloud", label: "Xcode Cloud", description: "Cloud builds and CI workflows.", route: "#/config/xcode-cloud", enabled: true, status: null},
      ],
    },
    {
      id: "ai",
      label: "AI",
      entries: [
        {id: "ai", label: "Add an AI", description: "Get an AI to do the work: free options first, with install and sign-in steps or an API key.", route: "#/config/ai", enabled: true, status: null},
        {id: "models", label: "Models", description: "Which AI model each role (architect, planner, builder, reviewer) uses.", route: "#/config/models", enabled: true, status: null},
        {id: "ai-instructions", label: "Instructions for AI helpers", description: "What AI helpers should know about this project, and how each role behaves.", route: "#/config/ai-instructions", enabled: true, ownerOnly: true, status: null},
      ],
    },
    {
      id: "computers",
      label: "Computers & access",
      entries: [
        {id: "fleet", label: "Machines", description: "Where jobs run: add and manage build machines (the fleet).", route: "#/config/fleet", enabled: true, ownerOnly: true, status: null},
        {id: "computers", label: "Your computers", description: "Switch to another of your computers, or add one.", route: "#/computers", enabled: true, status: null, hostedOnly: true},
        {id: "access", label: "Who can sign in", description: "Choose whose Google sign-ins reach this computer, and end sign-ins.", route: "#/config/access", enabled: true, ownerOnly: true, status: null},
        {id: "updates", label: "Updates", description: "Update Orchestrator on this and your other machines.", route: "#/config/updates", enabled: true, ownerOnly: true, status: null},
      ],
    },
    {
      id: "alerts",
      label: "Alerts",
      entries: [
        {id: "email", label: "Email alerts", description: "Who gets emails, and the account that sends them.", route: "#/config/email", enabled: true, ownerOnly: true, status: null},
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

  // What menus list. `resolve` and `find` also know the unlisted entries.
  function groups(role = "owner") {
    return registry.map((group) => ({
      id: group.id,
      label: group.label,
      entries: group.entries.filter((entry) => entry.listed !== false && shown(entry) && (role !== "member" || !entry.ownerOnly)).map(copyEntry),
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

  // API keys: a card on Add an AI (owners only; members' config has no keys).
  function apiKeysCard(state) {
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
    return `<section class="card configuration-card" id="api-keys">
          <div class="card-h"><h2>API keys</h2><span class="count">${keys.length}</span></div>
          <p class="card-b muted card-note">Use a provider through its API instead of its app. Saved key values are never shown.</p>
          <div class="configuration-setting-list">${rows || `<div class="empty">No providers are available.</div>`}</div>
        </section>`;
  }

  const PROVIDER_MODELS = {
    claude: [
      { id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6", tier: "Recommended", caps: ["Coding", "Speed", "Context"] },
      { id: "claude-opus-4-8", name: "Claude Opus 4.8", tier: "Extreme", caps: ["Reasoning", "Coding", "Deep refactor"] },
      { id: "claude-opus-4-7", name: "Claude Opus 4.7", tier: "Extreme", caps: ["Reasoning", "Coding", "Context"] },
      { id: "claude-haiku-4-5", name: "Claude Haiku 4.5", tier: "Fast", caps: ["Speed", "Quick edits"] },
    ],
    agy: [
      { id: "gemini-3.1-pro-preview", name: "Gemini 3.1 Pro", tier: "High", caps: ["Reasoning", "Coding", "Vision"] },
      { id: "gemini-3-flash-preview", name: "Gemini 3 Flash", tier: "Speed", caps: ["Speed", "Multimodal", "Context"] },
      { id: "gemini-3.1-flash-lite-preview", name: "Gemini 3.1 Flash Lite", tier: "Fast", caps: ["Low latency", "Speed"] },
      { id: "gemini-3.6-flash-high", name: "Gemini 3.6 Flash (High)", tier: "High", caps: ["Speed", "Coding", "Vision"] },
      { id: "gemini-3.6-flash-medium", name: "Gemini 3.6 Flash (Med)", tier: "Medium", caps: ["Context", "Speed"] },
      { id: "gemini-3.5-flash-high", name: "Gemini 3.5 Flash (High)", tier: "High", caps: ["Fast response", "Context"] },
    ],
    gemini: [
      { id: "gemini-3.1-pro-preview", name: "Gemini 3.1 Pro", tier: "High", caps: ["Reasoning", "Coding", "Vision"] },
      { id: "gemini-3-flash-preview", name: "Gemini 3 Flash", tier: "Speed", caps: ["Speed", "Multimodal", "Context"] },
      { id: "gemini-3.1-flash-lite-preview", name: "Gemini 3.1 Flash Lite", tier: "Fast", caps: ["Low latency", "Speed"] },
      { id: "gemini-3.6-flash-high", name: "Gemini 3.6 Flash (High)", tier: "High", caps: ["Speed", "Coding", "Vision"] },
      { id: "gemini-3.6-flash-medium", name: "Gemini 3.6 Flash (Med)", tier: "Medium", caps: ["Context", "Speed"] },
    ],
    codex: [
      { id: "gpt-4o", name: "GPT-4o", tier: "High", caps: ["Multimodal", "Coding", "Broad knowledge"] },
      { id: "o3-mini", name: "o3-mini", tier: "High", caps: ["Reasoning", "STEM & Code logic"] },
      { id: "o1", name: "o1", tier: "Extreme", caps: ["Deep deliberate reasoning"] },
      { id: "o1-mini", name: "o1-mini", tier: "Fast", caps: ["Fast reasoning", "Coding"] },
      { id: "gpt-4o-mini", name: "GPT-4o mini", tier: "Fast", caps: ["Fast utility", "Lightweight"] },
      { id: "gpt-5.4", name: "GPT-5.4", tier: "Preview", caps: ["Frontier reasoning preview"] },
      { id: "gpt-5.5", name: "GPT-5.5", tier: "Preview", caps: ["Frontier coding preview"] },
    ],
    opencode: [
      { id: "opencode/big-pickle", name: "Big Pickle (Free)", tier: "Free", caps: ["General coding", "Zero cost"] },
      { id: "opencode/deepseek-v4-flash-free", name: "DeepSeek V4 Flash (Free)", tier: "Free", caps: ["Fast reasoning", "Code"] },
      { id: "opencode/qwen3.6-plus-free", name: "Qwen 3.6 Plus (Free)", tier: "Free", caps: ["Broad language", "Code"] },
      { id: "opencode/minimax-m2.5-free", name: "MiniMax M2.5 (Free)", tier: "Free", caps: ["Long context", "Instruction"] },
      { id: "opencode/nemotron-3-super-free", name: "Nemotron 3 Super (Free)", tier: "Free", caps: ["Refactoring", "Alignment"] },
      { id: "opencode/laguna-s-2.1-free", name: "Laguna S 2.1 (Free)", tier: "Free", caps: ["Fast utility", "Scripts"] },
    ],
    ollama: [
      { id: "deepseek", name: "DeepSeek Coder", tier: "Local", caps: ["Local private coding"] },
      { id: "llama3.3", name: "Llama 3.3", tier: "Local", caps: ["General purpose local AI"] },
      { id: "qwen2.5-coder", name: "Qwen 2.5 Coder", tier: "Local", caps: ["Local code intelligence"] },
    ],
  };

  function modelTreeHtml(providerId, providerReady = false) {
    const models = PROVIDER_MODELS[providerId] || [];
    if (!models.length) return "";
    return `<details class="fold model-tree" ${providerReady ? "open" : ""}>
      <summary class="model-tree-summary">
        <strong>Supported models</strong>
        <span class="count">${models.length}</span>
      </summary>
      <div class="model-tree-content">
        <ul class="model-tree-branch">
          ${models.map((m, idx) => `
            <li class="model-tree-item ${idx === models.length - 1 ? 'last-node' : ''}">
              <div class="model-tree-row">
                <strong class="mono model-id">${escapeHtml(m.id)}</strong>
                ${m.name && m.name !== m.id ? `<span class="muted model-label">${escapeHtml(m.name)}</span>` : ""}
                ${m.tier ? `<span class="pill ${m.tier === 'Recommended' ? 'done' : m.tier === 'Free' ? 'ok' : ''}">${escapeHtml(m.tier)}</span>` : ""}
                ${m.caps?.length ? `<span class="model-caps muted">${escapeHtml(m.caps.join(" · "))}</span>` : ""}
              </div>
            </li>
          `).join("")}
        </ul>
      </div>
    </details>`;
  }

  function roleAssignmentsCard(state = {}) {
    const modelsData = state.models || {};
    const assignments = modelsData.assignments || {};
    const available = modelsData.available && modelsData.available.length ? modelsData.available : [
      {id: "claude-sonnet-4-6", name: "Claude Sonnet 4.6 (Recommended)", provider: "Anthropic"},
      {id: "claude-opus-4-8", name: "Claude Opus 4.8", provider: "Anthropic"},
      {id: "gemini-3.1-pro-preview", name: "Gemini 3.1 Pro", provider: "Google"},
      {id: "gemini-3-flash-preview", name: "Gemini 3 Flash", provider: "Google"},
      {id: "gpt-4o", name: "GPT-4o", provider: "OpenAI"},
      {id: "o3-mini", name: "o3-mini", provider: "OpenAI"}
    ];
    const roles = [
      {id: "architect", label: "Architect", desc: "Checks the plan and sets the standards it must meet"},
      {id: "planner", label: "Planner", desc: "Turns a request into tasks after reading the code"},
      {id: "builder", label: "Builder", desc: "Writes the code and the tests"},
      {id: "reviewer", label: "Reviewer", desc: "Reads the changes and the test results, and looks for what could break"}
    ];
    const formRows = roles.map((role) => {
      const currentVal = assignments[role.id] || "claude-sonnet-4-6";
      const options = available.map((m) => `<option value="${escapeHtml(m.id)}"${m.id === currentVal ? " selected" : ""}>${escapeHtml(m.label || m.name || m.id)} (${escapeHtml(m.provider)})</option>`).join("");
      return `<div class="configuration-setting-row">
        <div class="main-col">
          <strong>${escapeHtml(role.label)}</strong>
          <small>${escapeHtml(role.desc)}</small>
        </div>
        <select name="${escapeHtml(role.id)}" class="model-select">${options}</select>
      </div>`;
    }).join("");
    return `<section class="card configuration-card" id="team-models">
      <div class="card-h"><h2>Team Role Assignments</h2></div>
      <form class="card-b stack" id="config-models-form">
        <p class="muted">Which AI model each role (architect, planner, builder, reviewer) uses by default.</p>
        <div class="configuration-setting-list">${formRows}</div>
        <div class="row end" style="margin-top:16px;">
          <button class="btn primary" type="submit">Save model team</button>
        </div>
      </form>
    </section>`;
  }

  // Combined Add an AI & Models: each provider with its cost, whether it's ready, expandable model tree, and role models.
  function renderAi(state) {
    const ai = state.ai || {providers: [], plugins: []};
    const copyable = (command) => command
      ? `<div class="setup-hint"><code>${escapeHtml(command)}</code><button type="button" class="btn small ghost" data-setup-copy="${escapeHtml(command)}">Copy</button></div>` : "";
    const badge = (p) => p.ready ? `<span class="pill done">Ready</span>`
      : p.installed ? `<span class="pill">Installed, not signed in</span>` : `<span class="pill">Not installed</span>`;
    const cards = ai.providers.map((p) => `<section class="card configuration-card ai-provider" data-provider="${escapeHtml(p.id)}">
        <div class="card-h"><h2>${escapeHtml(p.name)}</h2>${badge(p)}</div>
        <div class="card-b stack">
        <p><strong>${escapeHtml(p.cost_label)}.</strong> ${escapeHtml(p.what)}</p>
        ${p.ready ? "" : `${p.installed ? "" : `<p class="muted">1. Install it:</p>${copyable(p.install)}${copyable(p.install_alt)}${p.install_note ? `<small>${escapeHtml(p.install_note)}</small>` : ""}`}
          <p class="muted">${p.installed ? "Then" : "2."} ${escapeHtml(p.sign_in)}</p>`}
        <p><a href="${escapeHtml(p.link)}" target="_blank" rel="noopener">Official instructions ↗</a>${p.key && Array.isArray(state.keys) ? ` · <button type="button" class="linklike" data-scroll-to="#api-keys">Add an API key instead</button>` : ""}</p>
        ${modelTreeHtml(p.id, p.ready)}
        </div>
      </section>`).join("");
    const plugins = (ai.plugins || []).map((p) => `<li><strong>${escapeHtml(p.name)}</strong>: ${escapeHtml(p.why)}</li>`).join("");
    return {
      title: "Add an AI",
      sub: ai.any_ready ? "You have at least one AI ready. Add more to choose between them per job." : "Orchestrator needs at least one AI to plan, build and review. Free options are first.",
      html: `<div class="configuration-page">
        ${roleAssignmentsCard(state)}
        <section class="card configuration-card">
          <div class="card-h"><h2>AI Providers</h2></div>
          <div class="card-b stack">
            <p class="muted">Run the install commands in Terminal on the computer Orchestrator runs on, then come back: this page checks again each time it opens. Prices and free allowances change; last checked ${escapeHtml(ai.checked || "")}.</p>
          </div>
        </section>
        ${cards || `<div class="empty">Couldn't check the AI tools on this computer.</div>`}
        ${Array.isArray(state.keys) ? apiKeysCard(state) : ""}
        <section class="card configuration-card">
          <div class="card-h"><h2>Helpful plugins for your AI tools</h2></div>
          <div class="card-b stack">
            <p class="muted">Optional. They give the AI richer context while it works; Orchestrator runs without them.</p>
            <div class="row align-center gap-3">
              <a href="#/connections#recommended-installs" class="btn small">View recommended MCPs &amp; mobile tools ↗</a>
              <a href="docs/recommended-mcp-plugins.md" target="_blank" rel="noopener" class="linklike">Plugin recommendations doc ↗</a>
            </div>
            ${plugins ? `<details class="fold mt-8"><summary class="muted">See installed plugins list</summary><ul class="plain-list mt-8">${plugins}</ul></details>` : ""}
          </div>
        </section>
      </div>`,
    };
  }

  function renderBaseBranch(state) {
    const branches = Array.isArray(state.branches) ? state.branches : [];
    const options = branches.map((branch) => `<option value="${escapeHtml(branch)}"${branch === state.base_branch ? " selected" : ""}>${escapeHtml(branch)}</option>`).join("");
    return {
      title: "Base branch",
      sub: "Choose the branch jobs use as their comparison point.",
      html: `<div class="configuration-page">
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
      title: "Email alerts",
      sub: "Choose who receives updates and how Orchestrator sends them.",
      html: `<div class="configuration-page">
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

  // Slack & chat alerts, shown on Connections with the other apps Orchestrator talks to (owners only).
  function chatCard(hook = {}) {
    return `<section class="card configuration-card" id="chat-alerts" data-owner-only>
          <div class="card-h"><h2>Slack &amp; chat alerts</h2><div class="row">
            <button type="button" class="btn small" data-config-action="chat-set">${hook.set ? "Change" : "Add webhook"}</button>
            ${hook.set ? `<button type="button" class="btn small primary" data-config-action="chat-test">Send test message</button>
            <button type="button" class="btn small danger" data-config-action="chat-clear">Remove</button>` : ""}
          </div></div>
          <div class="card-b stack">
            <strong>${hook.set ? "Connected" : "Not set up"}</strong>
            <span class="configuration-status">${hook.set ? `Sending to ${escapeHtml(hook.host)}. The full address is stored but not shown.` : "Paste a Slack webhook URL."}</span>
            <span class="muted">Sends a message when a job needs you, or when a run finishes or fails.</span>
          </div>
        </section>`;
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

  function renderModels(state) {
    const combined = renderAi(state);
    return {
      ...combined,
      title: "Models",
      sub: "Which AI model each role uses by default, and supported provider models.",
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




  function render(section, state = {}) {
    if (section === "ai") return renderAi(state);
    if (section === "base-branch") return renderBaseBranch(state);
    if (section === "email") return renderEmail(state);
    if (section === "access") return renderAccess(state);
    if (section === "models") return renderModels(state);
    if (section === "ai-instructions") return renderAiInstructions(state);
    if (section === "fleet") return renderFleet(state);
    if (section === "firebase") return renderFirebase(state);
    if (section === "xcode-cloud") return renderXcodeCloud(state);
    if (section === "updates") return renderUpdates(state);
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
    chatCard,
  };
})(typeof globalThis !== "undefined" ? globalThis : window);

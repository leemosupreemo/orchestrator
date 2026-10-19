(function (root) {
  "use strict";

  // The planning views: the epic breakdown to review and approve, and the plan being built.
  // Pure string rendering, so it can be tested without a browser.

  const escapeHtml = root.Html.escape;

  function layerTitle(layer) {
    return layer === 0 ? "Start with these (they don't depend on anything new)" : `Then these (layer ${layer + 1})`;
  }

  function storiesList(stories) {
    return stories?.length ? `<ul class="feature-stories">${stories.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ul>` : "";
  }

  function requirementsList(requirements) {
    return requirements?.length ? `<ul class="feature-requirements">${requirements.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>` : "";
  }

  const SLICE_TITLES = {MVP: "MVP: the smallest version that works end to end", Next: "Next", Later: "Later", "": "Not placed yet"};

  // The Epics section of the Product page: the breakdown of the product, split into what is waiting for a decision, what is approved, and what was declined.
  // Each takes the overview from /api/epics.
  const TECHNIQUES = [["", "Let Orchestrator choose"], ["story_map", "Story map: the flows people follow"], ["wbs", "Work breakdown: top-down deliverables"], ["list", "Short list"]];

  const jiraLink = (epic) => {
    const jira = epic.jira;
    if (!jira?.key) return "";
    const icon = `<svg class="icon icon-sm" aria-hidden="true"><use href="#i-jira"/></svg>`;
    return /^https:\/\//.test(jira.url || "")
      ? `<a class="jira-chip" href="${escapeHtml(jira.url)}" target="_blank" rel="noopener" title="Open ${escapeHtml(jira.key)} in Jira" aria-label="Open ${escapeHtml(jira.key)} in Jira">${icon}${escapeHtml(jira.key)}</a>`
      : `<span class="jira-chip" title="Linked to Jira epic ${escapeHtml(jira.key)}">${icon}${escapeHtml(jira.key)}</span>`;
  };

  function techniqueSelect(selected) {
    return `<label class="field epic-technique"><span>Technique</span><select id="epic-technique">${TECHNIQUES.map(([value, label]) =>
      `<option value="${value}" ${value === (selected || "") ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}</select></label>`;
  }

  // How the breakdown was made: the technique and why, how much detail, and what is still unanswered.
  function renderBreakdownInfo(breakdown) {
    if (!breakdown) return "";
    const how = breakdown.technique_name ? `<p><strong>${escapeHtml(breakdown.technique_name)}.</strong> ${escapeHtml(breakdown.rationale || "")}</p>` : "";
    const brief = breakdown.detail === "sparse"
      ? `<p class="muted">Your requirements are short, so these are broad. Add more above and break down again for a finer result.</p>` : "";
    const warnings = (breakdown.warnings || []).map((w) => `<div class="banner attention"><p>${escapeHtml(w)}</p></div>`).join("");
    return `${how}${brief}${warnings}`;
  }

  // Where an answer most likely belongs in the PRD, guessed from the question; the person can change it.
  const SECTION_CHOICES = [["features", "Core features"], ["pitch", "Pitch"], ["who", "Who it's for"], ["look", "Look and feel"], ["not", "What to exclude"]];
  function guessSection(question) {
    const q = String(question || "").toLowerCase();
    if (/\b(exclude|avoid|won't|will not|out of scope|non-goal|not include)\b/.test(q)) return "not";
    if (/\b(platform|native|cross-platform|codebase|stack|built for|ios|android|web app|language|framework|engine)\b/.test(q)) return "pitch";
    if (/\b(look|feel|style|visual|colou?r|theme|sound|animation|design|font|brand|monochrome|pixel|aesthetic)\b/.test(q)) return "look";
    if (/\b(who|audience|target|persona|customers?|age group)\b/.test(q)) return "who";
    return "features";
  }

  // What the breakdown still needs to know: an answer box for each, and where in the PRD the answer goes.
  function renderQuestions(breakdown) {
    const questions = breakdown?.open_questions || [];
    if (!questions.length) return "";
    return `<section class="card mb-16" id="epic-questions"><div class="card-h"><h2>Questions to answer</h2><span class="count">${questions.length}</span></div>
      <form class="card-b stack" id="epic-questions-form">
        <p class="muted">Based on your product requirements, these are still unclear. Answer any you can: each answer is added to the requirements above, for the next breakdown and every job to use.</p>
        ${questions.map((q, i) => `<div class="epic-question" data-question="${escapeHtml(q)}">
          <label class="field"><span>${escapeHtml(q)}</span><textarea name="a${i}" rows="2" maxlength="1000"></textarea></label>
          <label class="field epic-question-where"><span>Goes in</span><select name="s${i}">${SECTION_CHOICES.map(([id, title]) =>
            `<option value="${id}" ${id === guessSection(q) ? "selected" : ""}>${escapeHtml(title)}</option>`).join("")}</select></label></div>`).join("")}
        <div class="row"><span class="spacer"></span>
          <button type="submit" class="btn" data-after="save">Save to the PRD</button>
          <button type="submit" class="btn primary" data-after="again">Save and break down again</button></div>
      </form></section>`;
  }

  function epicDetails(e) {
    return `${e.summary ? `<span>${escapeHtml(e.summary)}</span>` : ""}${requirementsList(e.requirements)}${storiesList(e.stories)}
      ${e.depends_on?.length ? `<span class="muted">Builds on ${e.depends_on.map(escapeHtml).join(", ")}</span>` : ""}
      ${e.paths?.length ? `<span class="muted mono">${e.paths.map(escapeHtml).join(" · ")}</span>` : ""}`;
  }

  // Epics waiting for a decision, grouped by release slice (story map) or in build order, each with a checkbox.
  function renderSuggested(epics, breakdown) {
    const list = (epics || []).filter((e) => e.status === "suggested");
    if (!list.length) return "";
    const storyMap = breakdown?.technique === "story_map";
    const idOf = new Map((epics || []).filter((e) => e.status !== "approved").map((e) => [e.name.toLowerCase(), e.id]));
    const item = (e) => `<label class="feature-proposal-item" data-epic="${escapeHtml(e.id)}">
        <input type="checkbox" name="chosen" data-needs="${escapeHtml((e.depends_on || []).map((d) => idOf.get(String(d).toLowerCase())).filter(Boolean).join("\u001f"))}" value="${escapeHtml(e.id)}" checked>
        <span class="stack"><strong>${escapeHtml(e.name)}${!storyMap || !e.slice ? "" : ` <span class="pill">${escapeHtml(e.slice)}</span>`}</strong>${epicDetails(e)}</span></label>`;
    const layers = [...new Set(list.map((e) => e.layer || 0))].sort((a, b) => a - b);
    const groups = storyMap
      ? ["MVP", "Next", "Later", ""].map((slice) => ({title: SLICE_TITLES[slice], items: list.filter((e) => (e.slice || "") === slice)})).filter((g) => g.items.length)
      : layers.map((layer) => ({title: layerTitle(layer), items: list.filter((e) => (e.layer || 0) === layer)}));
    return `<section class="card mb-16" id="epic-suggested">
      <div class="card-h"><h2>Suggested</h2><span class="count">${list.length}</span></div>
      <form class="card-b stack" id="epic-suggested-form">
        <p class="muted">In the order to build them. Untick any you don't want, then approve. An approved epic becomes a feature and a job marked Epic that waits until you plan it.</p>
        ${groups.map((g) => `<div class="feature-proposal-layer"><div class="tc-area-h">${escapeHtml(g.title)}</div>${g.items.map(item).join("")}</div>`).join("")}
        <div class="row"><span class="spacer"></span>
          <button type="button" class="btn ghost" id="epic-decline">Decline selected</button>
          <button type="submit" class="btn primary">Approve selected</button></div>
      </form></section>`;
  }

  // Approved epics: where each one's job is up to, the Jira epic it is linked to, and the one next step.
  function renderApproved(epics, {act, jiraConnected} = {}) {
    const list = (epics || []).filter((e) => e.status === "approved");
    if (!list.length) return "";
    const row = (e) => {
      const job = e.job_info;
      const label = job ? (job.backlog ? "No plan yet" : job.plan_ready ? "Plan ready" : job.label || "") : "No job";
      const status = `<span class="pill ${escapeHtml(job ? (job.backlog ? "idle" : job.tone || "") : "idle")}">${escapeHtml(label)}</span>`;
      const plan = !job || job.backlog ? `<button type="button" class="btn small primary" ${act ? act("plan_epic", {epic: e.id}) : ""}>Make a plan</button>` : "";
      const open = job ? `<a class="btn small ${job.plan_ready ? "primary" : "ghost"}" href="#/jobs/${encodeURIComponent(job.id)}">${job.plan_ready ? "Review plan" : "Open job"}</a>` : "";
      const send = jiraConnected && !e.jira ? `<button type="button" class="btn small ghost" data-epic-send="${escapeHtml(e.id)}">Send to Jira</button>` : "";
      return `<div class="item epic-row" data-epic="${escapeHtml(e.id)}"><div class="main-col"><div class="title">${escapeHtml(e.name)} <span class="job-type-tag job-type-epic">Epic</span> ${jiraLink(e)}</div>
        ${e.summary ? `<div class="meta">${escapeHtml(e.summary)}</div>` : ""}
        ${e.requirements?.length ? `<div class="meta">${e.requirements.length} requirement${e.requirements.length === 1 ? "" : "s"}${e.stories?.length ? ` · ${e.stories.length} stor${e.stories.length === 1 ? "y" : "ies"}` : ""}</div>` : ""}</div>
        <div class="side">${status}${plan}${open}${send}</div></div>`;
    };
    return `<section class="card mb-16" id="epic-approved"><div class="card-h"><h2>Approved</h2><span class="count">${list.length}</span></div>
      <div class="list">${list.map(row).join("")}</div></section>`;
  }

  function renderDeclined(epics) {
    const list = (epics || []).filter((e) => e.status === "declined");
    if (!list.length) return "";
    return `<section class="card mb-16" id="epic-declined"><details class="fold"><summary class="card-h"><h2>Declined</h2><span class="count">${list.length}</span></summary>
      <div class="list">${list.map((e) => `<div class="item epic-row" data-epic="${escapeHtml(e.id)}"><div class="main-col"><div class="title">${escapeHtml(e.name)}</div>
        ${e.summary ? `<div class="meta">${escapeHtml(e.summary)}</div>` : ""}</div>
        <div class="side"><button type="button" class="btn small" data-epic-restore="${escapeHtml(e.id)}">Bring back</button></div></div>`).join("")}</div></details></section>`;
  }

  // Epics found in the Jira project that are not here yet, to pick from.
  function renderJiraImport(found, project) {
    if (!found.length) return `<p class="muted">Every open epic in ${escapeHtml(project)} is already here.</p>`;
    return `<form class="stack" id="epic-jira-form"><p class="muted">Open epics in ${escapeHtml(project)}. Each you import becomes an approved epic with a job that waits until you plan it, and stays linked to its Jira epic.</p>
      ${found.map((i) => `<label class="feature-proposal-item"><input type="checkbox" name="keys" value="${escapeHtml(i.key)}" checked>
        <span class="stack"><strong>${escapeHtml(i.summary)} <span class="muted">${escapeHtml(i.key)}</span></strong>${i.description ? `<span class="muted">${escapeHtml(i.description.slice(0, 240))}</span>` : ""}</span></label>`).join("")}
      <div class="row"><span class="spacer"></span><button type="button" class="btn ghost" id="epic-jira-cancel">Cancel</button><button type="submit" class="btn primary">Import selected</button></div></form>`;
  }

  function renderStories(stories) {
    return stories?.length ? `<details><summary>Stories (${stories.length})</summary>${storiesList(stories)}</details>` : "";
  }

  // "Build the plan": choose features (planned ones without jobs are ticked) and whether plans approve themselves.
  function renderStartForm(features) {
    const ordered = [...(features || [])].sort((a, b) => a.layer - b.layer);
    return `<p class="muted">Features are built in dependency order: each starts once everything it builds on is done, and independent
        ones run at the same time when your machines can take them. Running work is never stopped by pausing.</p>
      <fieldset class="field stack">${ordered.map((f) => `<label class="check"><input type="checkbox" name="f_${escapeHtml(f.id)}"
        ${f.status === "planned" && !f.jobs_total ? "checked" : ""}><span>${escapeHtml(f.name)}${f.jobs_total ? ` <span class="muted">(has ${f.jobs_total} job${f.jobs_total === 1 ? "" : "s"})</span>` : ""}</span></label>`).join("")}</fieldset>
      <label class="check"><input type="checkbox" name="auto_approve"><span>Approve plans automatically
        <small class="hint-text">Otherwise each feature's plan waits for you to approve it. Plans with an open question or the architect's concerns always wait.</small></span></label>`;
  }

  function chosenFeatures(values) {
    return Object.keys(values || {}).filter((key) => key.startsWith("f_")).map((key) => key.slice(2));
  }

  const RUN_STATE = {done: ["Done", "done"], working: ["Working", "working"], planning: ["Planning", "working"],
    attention: ["Needs you", "attention"], failed_start: ["Stuck", "failed"], waiting: ["Waiting", "muted"], ready: ["Ready", "muted"]};

  // The plan being built: each feature's state, with Pause/Resume and Stop.
  function renderRun(plan) {
    if (!plan) return "";
    const done = plan.rows.filter((r) => r.state === "done").length;
    const rows = plan.rows.map((r) => {
      const [label, tone] = RUN_STATE[r.state] || [r.state, "muted"];
      const name = r.job ? `<a href="#/jobs/${encodeURIComponent(r.job)}">${escapeHtml(r.name)}</a>` : escapeHtml(r.name);
      return `<div class="item" data-plan-feature="${escapeHtml(r.feature)}"><div class="main-col"><div class="title">${name}</div>
        ${r.detail ? `<div class="meta">${escapeHtml(r.detail)}</div>` : ""}</div><span class="pill ${tone}">${escapeHtml(label)}</span></div>`;
    }).join("");
    const controls = plan.finished ? `<button type="button" class="btn small" data-plan-action="stop">Dismiss</button>`
      : `${plan.paused ? `<button type="button" class="btn small primary" data-plan-action="resume">Resume</button>`
        : `<button type="button" class="btn small" data-plan-action="pause">Pause</button>`}
        <button type="button" class="btn small ghost" data-plan-action="stop">Stop</button>`;
    return `<section class="card mb-16" id="plan-run">
      <div class="card-h"><h2>${plan.finished ? "The plan is built" : plan.paused ? "Building the plan (paused)" : "Building the plan"}</h2>
        <span class="count">${done}/${plan.rows.length}</span><div class="row gap-10">${controls}</div></div>
      <div class="card-b stack">${plan.auto_approve ? `<p class="muted">Plans are approved automatically unless they raise a question.</p>` : `<p class="muted">Each feature's plan waits for you to approve it.</p>`}
        <div class="list">${rows}</div></div></section>`;
  }

  root.FeaturePlan = {renderBreakdownInfo, renderQuestions, guessSection, renderSuggested, renderApproved, renderDeclined, renderJiraImport, techniqueSelect, jiraLink,
    renderStories, requirementsList, renderStartForm, chosenFeatures, renderRun};
})(typeof globalThis !== "undefined" ? globalThis : window);

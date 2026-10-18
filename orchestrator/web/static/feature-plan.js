(function (root) {
  "use strict";

  // The Features page's planning views: a drafted feature map to review, and (later) a plan being built.
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

  // A proposal from /api/features/propose: epics in build order, each with a layer (and a release slice for a story map),
  // with the technique the model chose and why, how much detail it went into, and what it still needs to know.
  function renderProposal(proposal) {
    const features = proposal?.features || [];
    const storyMap = proposal?.technique === "story_map";
    const featureHtml = (f) => `<label class="feature-proposal-item">
        <input type="checkbox" name="chosen" data-needs="${escapeHtml((f.depends_on || []).join("\u001f"))}" value="${escapeHtml(f.name)}" checked>
        <span class="stack">
          <strong>${escapeHtml(f.name)}${!storyMap || !f.slice ? "" : ` <span class="pill">${escapeHtml(f.slice)}</span>`}</strong>
          ${f.summary ? `<span>${escapeHtml(f.summary)}</span>` : ""}
          ${requirementsList(f.requirements)}
          ${storiesList(f.stories)}
          ${f.depends_on?.length ? `<span class="muted">Builds on ${f.depends_on.map(escapeHtml).join(", ")}</span>` : ""}
          ${f.paths?.length ? `<span class="muted mono">${f.paths.map(escapeHtml).join(" · ")}</span>` : ""}
        </span></label>`;
    const layers = [...new Set(features.map((f) => f.layer))].sort((a, b) => a - b);
    const groups = storyMap
      ? ["MVP", "Next", "Later", ""].map((slice) => ({title: SLICE_TITLES[slice], items: features.filter((f) => (f.slice || "") === slice)})).filter((g) => g.items.length)
      : layers.map((layer) => ({title: layerTitle(layer), items: features.filter((f) => f.layer === layer)}));
    const how = proposal?.technique_name
      ? `<p><strong>${escapeHtml(proposal.technique_name)}.</strong> ${escapeHtml(proposal.rationale || "")}</p>` : "";
    const brief = proposal?.detail === "sparse"
      ? `<p class="muted">Your requirements are short, so these are broad. Add more to Product and suggest again for a finer breakdown.</p>` : "";
    const questions = proposal?.open_questions?.length
      ? `<div class="feature-proposal-questions"><div class="tc-area-h">To break this down further, answer</div><ul>${proposal.open_questions.map((q) => `<li>${escapeHtml(q)}</li>`).join("")}</ul></div>` : "";
    return `<section class="card mb-16" id="feature-proposal">
      <div class="card-h"><h2>Suggested features</h2><span class="count">${features.length}</span></div>
      <form class="card-b stack" id="feature-proposal-form">
        ${how}${brief}
        <p class="muted">Drafted from your product requirements, in the order to build them. Untick any you don't want; nothing is saved until you add them.</p>
        ${(proposal?.warnings || []).map((w) => `<div class="banner attention"><p>${escapeHtml(w)}</p></div>`).join("")}
        ${groups.map((g) => `<div class="feature-proposal-layer"><div class="tc-area-h">${escapeHtml(g.title)}</div>${g.items.map(featureHtml).join("")}</div>`).join("")}
        ${questions}
        <div class="row"><span class="spacer"></span>
          <button type="button" class="btn ghost" id="feature-proposal-discard">Discard</button>
          <button type="submit" class="btn primary">Add selected</button></div>
      </form></section>`;
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

  root.FeaturePlan = {renderProposal, renderStories, requirementsList, renderStartForm, chosenFeatures, renderRun};
})(typeof globalThis !== "undefined" ? globalThis : window);

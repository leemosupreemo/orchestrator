(function (root) {
  "use strict";

  // The Features page's planning views: a drafted feature map to review, and (later) a plan being built.
  // Pure string rendering, so it can be tested without a browser.

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  }

  function layerTitle(layer) {
    return layer === 0 ? "Start with these (they don't depend on anything new)" : `Then these (layer ${layer + 1})`;
  }

  // A proposal from /api/features/propose: features in build order, each with a layer, plus warnings.
  function renderProposal(proposal) {
    const features = proposal?.features || [];
    const layers = [...new Set(features.map((f) => f.layer))].sort((a, b) => a - b);
    const featureHtml = (f) => `<label class="feature-proposal-item">
        <input type="checkbox" name="chosen" value="${escapeHtml(f.name)}" checked>
        <span class="stack">
          <strong>${escapeHtml(f.name)}</strong>
          ${f.summary ? `<span>${escapeHtml(f.summary)}</span>` : ""}
          ${f.stories?.length ? `<ul class="feature-stories">${f.stories.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ul>` : ""}
          ${f.depends_on?.length ? `<span class="muted">Builds on ${f.depends_on.map(escapeHtml).join(", ")}</span>` : ""}
          ${f.paths?.length ? `<span class="muted mono">${f.paths.map(escapeHtml).join(" · ")}</span>` : ""}
        </span></label>`;
    return `<section class="card mb-16" id="feature-proposal">
      <div class="card-h"><h2>Proposed features</h2><span class="count">${features.length}</span></div>
      <form class="card-b stack" id="feature-proposal-form">
        <p class="muted">Drafted from your product requirements, in the order to build them. Untick any you don't want; nothing is saved until you add them.</p>
        ${(proposal?.warnings || []).map((w) => `<div class="banner attention"><p>${escapeHtml(w)}</p></div>`).join("")}
        ${layers.map((layer) => `<div class="feature-proposal-layer"><div class="tc-area-h">${escapeHtml(layerTitle(layer))}</div>
          ${features.filter((f) => f.layer === layer).map(featureHtml).join("")}</div>`).join("")}
        <div class="row"><span class="spacer"></span>
          <button type="button" class="btn ghost" id="feature-proposal-discard">Discard</button>
          <button type="submit" class="btn primary">Add selected</button></div>
      </form></section>`;
  }

  function renderStories(stories) {
    return stories?.length ? `<details><summary>Stories (${stories.length})</summary><ul class="feature-stories">${stories.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ul></details>` : "";
  }

  root.FeaturePlan = {renderProposal, renderStories, layerTitle};
})(typeof globalThis !== "undefined" ? globalThis : window);

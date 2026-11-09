(function (root) {
  "use strict";
  const esc = (s) => root.Html.escape(String(s ?? ""));
  const allowed = new Set(["feature", "bug", "design", "coverage", "freestyle"]);

  function defaultSuggestions(product, jobs) {
    const waiting = (jobs || []).find(j => j.state?.group === "needs_you");
    return [waiting
      ? { title: "Unblock the latest work", type: "freestyle", summary: `Review what is blocking ${waiting.title || "the paused job"}`, spec: `Read job ${waiting.id || ""} and explain the next decision or smallest fix. Avoid duplicating existing work.` }
      : { title: "Plan the next requirement", type: "feature", summary: "Plan the next unfinished product requirement", spec: "Read the requirements and existing jobs. Pick a small unfinished requirement not covered by active work, and plan it first." },
      { title: "Check the main user journey", type: "coverage", summary: "Test the main user journey against the product requirements", spec: "Read the product requirements and existing tests, then add meaningful coverage for uncovered success and failure paths." },
      { title: "Review recent changes", type: "freestyle", summary: "Review recent changes for regressions and unfinished work", spec: "Inspect recent commits and current work. Identify concrete regressions or unfinished requirements without duplicating active jobs." }];
  }

  function jobUrl(s) {
    const query = new URLSearchParams({ type: allowed.has(s.type) ? s.type : "freestyle", summary: String(s.summary || s.title || "").slice(0, 300), spec: String(s.spec || "").slice(0, 4000) });
    return `#/new?${query}`;
  }

  function render({ project: p = {}, git = {}, product = {}, jobs = [], runs = [], suggestions } = {}) {
    const body = product?.sections?.find(s => s.filled && s.body?.trim())?.body || "";
    const about = body.replace(/[#*_`>]/g, "").replace(/\s+/g, " ").trim().slice(0, 240)
      || [({web: "Web app", desktop: "Desktop app", mobile: "Mobile app"})[p.kind], p.name].filter(Boolean).join(" · ") || "Describe this project on the Product page.";
    const active = runs.filter(r => r.running && r.action !== "dev_preview");
    const needsYou = jobs.filter(j => j.state?.group === "needs_you").length;
    const unfinished = jobs.filter(j => j.state?.group !== "done");
    const status = active.length ? `${active.length} run${active.length === 1 ? "" : "s"} in progress` : needsYou ? "Needs your attention" : !jobs.length ? "Ready for the first job" : unfinished.length ? "Work planned" : "Up to date";
    const tone = active.length ? "working" : needsYou ? "attention" : "done";
    const stamp = j => Number(j.updated || j.created) || Date.parse(j.updated || j.created) || 0;
    const latest = [...jobs].sort((a,b) => stamp(b) - stamp(a))[0];
    const work = active[0] ? `<a href="#/runs/${encodeURIComponent(active[0].id)}">${esc(active[0].summary || active[0].title || "Work in progress")}</a>`
      : latest ? `<a href="#/jobs/${encodeURIComponent(latest.id)}">${esc(latest.title || "Latest job")}</a>${latest.state?.label ? ` · ${esc(latest.state.label)}` : ""}` : "No jobs yet";
    const items = Array.isArray(suggestions) && suggestions.length === 3 ? suggestions : defaultSuggestions(product, jobs);
    const dirty = Number(p.dirty_files) || 0;
    return `<section class="card home-project-status mb-16" id="home-project-status" aria-labelledby="home-project-status-title">
      <div class="card-h"><h2 id="home-project-status-title">Project status</h2><span class="pill ${tone}">${esc(status)}</span></div>
      <div class="card-b"><ul class="home-status-facts">
        <li><strong>About</strong><span>${esc(about)}</span></li>
        <li><strong>Latest commit</strong><span>${git?.last_commit ? esc(git.last_commit) : "No commits yet"}</span></li>
        <li><strong>Changes</strong><span>${dirty ? `<a href="#/git?show=changes">${dirty} uncommitted file${dirty === 1 ? "" : "s"}</a>${p.branch ? ` on ${esc(p.branch)}` : ""}` : "Working tree clean"}</span></li>
        <li><strong>Latest work</strong><span>${work}</span></li>
      </ul>
      <div class="home-status-suggestions"><h3>Suggestions</h3><div class="home-suggestion-list">${items.map(s => `<a class="home-suggestion" href="${esc(jobUrl(s))}"><span>${esc(s.title)}</span><span aria-hidden="true">↗</span></a>`).join("")}</div></div></div>
    </section>`;
  }
  root.HomeStatus = {render, defaultSuggestions, jobUrl};
})(typeof globalThis !== "undefined" ? globalThis : this);

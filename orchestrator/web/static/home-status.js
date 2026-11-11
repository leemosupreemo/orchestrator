(function (root) {
  "use strict";
  // Home's Project status card: the last commit, the last job run and, when it isn't that one, the last job run that
  // succeeded. One short line each.
  const esc = (s) => root.Html.escape(String(s ?? ""));

  // Jobs that have run: planned, designed, queued or epic-only ones haven't yet.
  const NOT_RUN = new Set(["planned", "designing", "backlog", "scheduled", "unknown", ""]);
  // A run that succeeded: built and ready to review or merge, or finished.
  const SUCCEEDED = new Set(["review-needed", "completed"]);

  const stamp = (j) => Number(j.updated || j.created) || Date.parse(j.updated || j.created) / 1000 || 0;

  function ago(seconds, now = Date.now() / 1000) {
    const s = Math.max(0, now - seconds);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
    return new Date(seconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }

  function when(iso) {
    const t = Date.parse(iso || "");
    if (!t) return "";
    return new Date(t).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  // "abc123 Fix score (Ann, 4 hours ago)" → "Fix score · Nov 10, 3:42 PM", the hash and author in the tooltip.
  function commitLine(raw, at = "") {
    if (!raw) return "No commits yet";
    const m = String(raw).match(/^(\S+) (.*?)(?: \((.+), ([^,]+)\))?$/);
    if (!m) return esc(raw);
    const [, hash, subject, author, relative] = m;
    const tip = [hash, author].filter(Boolean).join(" by ");
    const date = when(at) || relative || "";
    return `<span title="${esc(tip)}">${esc(subject)}</span>${date ? ` <span class="muted">· ${esc(date)}</span>` : ""}`;
  }

  function jobLine(j, now) {
    return `<a href="#/jobs/${encodeURIComponent(j.id)}">${esc(j.title || "Untitled job")}</a>`
      + ` <span class="muted">· ${esc(j.state?.label || j.status || "")}${stamp(j) ? ` · ${esc(ago(stamp(j), now))}` : ""}</span>`;
  }

  function render({ git = {}, jobs = [], runs = [], now } = {}) {
    const ran = jobs.filter((j) => !NOT_RUN.has(String(j.status || ""))).sort((a, b) => stamp(b) - stamp(a));
    const lastRun = ran[0];
    const lastGood = ran.find((j) => SUCCEEDED.has(String(j.status || "")));
    const active = runs.filter((r) => r.running && r.action !== "dev_preview").length;
    const needsYou = jobs.filter((j) => j.state?.group === "needs_you" && !j.active_run).length;
    const pill = active ? `<span class="pill working">${active} run${active === 1 ? "" : "s"} in progress</span>`
      : needsYou ? `<a class="pill attention" href="#/?filter=needs_you">${needsYou} job${needsYou === 1 ? " needs" : "s need"} you</a>` : "";
    return `<section class="card home-project-status mb-16" id="home-project-status" aria-labelledby="home-project-status-title">
      <div class="card-h"><h2 id="home-project-status-title">Project status</h2>${pill}</div>
      <div class="card-b"><ul class="home-status-facts">
        <li><strong>Last commit</strong><span>${commitLine(git?.last_commit, git?.last_commit_at)}</span></li>
        <li><strong>Last job run</strong><span>${lastRun ? jobLine(lastRun, now) : "No jobs have run yet"}</span></li>
        ${lastGood && lastGood !== lastRun ? `<li><strong>Last success</strong><span>${jobLine(lastGood, now)}</span></li>` : ""}
      </ul></div>
    </section>`;
  }

  root.HomeStatus = { render, commitLine };
})(typeof globalThis !== "undefined" ? globalThis : this);

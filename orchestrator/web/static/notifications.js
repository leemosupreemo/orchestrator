(function (root) {
  "use strict";

  // Turns two consecutive run lists into the moments worth interrupting someone for:
  // a run that finished or failed. First load (prev == null) is silent.
  function events(prev, next) {
    if (!Array.isArray(prev) || !Array.isArray(next)) return [];
    const before = new Map(prev.map((run) => [run.id, run]));
    const out = [];
    for (const run of next) {
      const was = before.get(run.id);
      if (!was) continue;
      const name = run.title || run.action || "A run";
      const target = run.result_job || run.job;
      const hash = run.running || !target ? `#/runs/${run.id}` : `#/jobs/${target}`;
      if (was.running && !run.running) {
        const ok = run.exit_code === 0;
        out.push({ kind: ok ? "done" : "problem", key: `${run.id}:ended`, title: ok ? "Finished" : "Problem", body: ok ? `${name} finished.` : `${name} failed (exit ${run.exit_code}).`, hash });
      }
    }
    return out;
  }

  // New things waiting on the user (the inbox: questions, reviews, failures, stalled runs).
  // Anything already there on the previous poll stays quiet; first poll (prev == null) too.
  function inboxEvents(prev, next) {
    if (!Array.isArray(prev) || !Array.isArray(next)) return [];
    const seen = new Set(prev.map((item) => item.id));
    return next.filter((item) => !seen.has(item.id)).map((item) => ({
      kind: "needs-you", key: item.id, title: item.label || "Needs you", body: [item.title, item.reason].filter(Boolean).join(": "), hash: item.hash,
    }));
  }

  // How many runs are waiting on the user, for the tab title.
  function attentionCount(runs) {
    return (runs || []).filter((run) => run.running && run.waiting).length;
  }

  function tabTitle(base, count) {
    return count > 0 ? `(${count}) ${base}` : base;
  }

  const KEY = "orchestrator_notifications";

  function supported() {
    return typeof Notification !== "undefined";
  }

  function enabled() {
    try {
      return supported() && Notification.permission === "granted" && localStorage.getItem(KEY) === "on";
    } catch (e) {
      return false;
    }
  }

  async function enable() {
    if (!supported()) return "unsupported";
    const permission = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    try { localStorage.setItem(KEY, permission === "granted" ? "on" : "off"); } catch (e) { /* private mode */ }
    return permission;
  }

  function disable() {
    try { localStorage.setItem(KEY, "off"); } catch (e) { /* private mode */ }
  }

  function show(event) {
    if (!enabled()) return;
    const note = new Notification(event.title, { body: event.body, tag: event.key });
    note.onclick = () => { window.focus(); location.hash = event.hash; note.close(); };
  }

  const api = { events, inboxEvents, attentionCount, tabTitle, supported, enabled, enable, disable, show };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Notifications = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

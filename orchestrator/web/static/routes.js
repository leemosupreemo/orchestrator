(function (root) {
  "use strict";

  // Pages and settings that moved keep their old addresses working: bookmarks, links in notifications and
  // emails, and links older servers send. Each key is an old hash path (no "#/"), each value where it lives now;
  // the old query string carries over, and the new one wins on a clash. Add an entry whenever a route moves.
  const ALIASES = {
    "test-cases": "tests/cases",
    "setup-checklist": "readiness",
    "config/audit": "readiness",
    "config/self-tests": "readiness",
    "config/setup-wizard": "readiness",
    "config/chat": "connections?card=chat",
    "config/api-keys": "config/ai",
    "config/models": "config/ai",
    "config/documentation": "docs",
    "config/archived-jobs": "?filter=archived",
  };

  function redirect(hash, aliases = ALIASES) {
    const [path, qs] = String(hash || "").replace(/^#\/?/, "").split("?");
    const target = aliases[path.replace(/\/+$/, "")];
    if (!target) return null;
    const [targetPath, targetQs] = target.split("?");
    const params = new URLSearchParams(targetQs || "");
    new URLSearchParams(qs || "").forEach((value, key) => { if (!params.has(key)) params.set(key, value); });
    const query = params.toString();
    return `#/${targetPath}${query ? `?${query}` : ""}`;
  }

  root.RouteAliases = {ALIASES, redirect};
})(globalThis);

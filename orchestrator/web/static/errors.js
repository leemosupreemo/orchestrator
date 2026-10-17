(function (root) {
  "use strict";

  // Pairs a raw error message with what to do about it. First match wins; unknown errors pass through unchanged.
  // A browser's own wording for "the request never arrived" ("Failed to fetch") means nothing to a person, so it is replaced, not repeated.
  const REPLACE = [
    [/failed to fetch|networkerror|load failed|network request failed/i, "Can't reach Orchestrator. Check that it's running and that you're online."],
  ];
  const HINTS = [
    [/job not found|archived job not found/i, "It may have been archived or discarded. Archived jobs can be restored from the Archived filter on Home."],
    [/rejected the key|didn't accept the token/i, "Check the key and the region on the Measure page."],
    [/webhook answered 40[134]/i, "The address may have been revoked or mistyped. Create a new incoming webhook and paste it again."],
    [/couldn't reach|temporary failure|timed out|took too long/i, "Check your connection and try again. If it keeps happening, the service may be down."],
    [/not signed in to github|gh auth/i, "Sign in with “gh auth login” in a terminal, then check again."],
    [/no model is available|unsupported model/i, "Choose models under Configuration > Models."],
    [/missing ui headers|unexpected host header/i, "Reload the page. If you opened it from a different address, use the one Orchestrator printed."],
    [/already exists/i, "Pick a different name, or open the existing one instead."],
  ];

  function explain(message) {
    const text = String(message || "").trim();
    if (!text) return "Something went wrong. Try again.";
    const replaced = REPLACE.find(([pattern]) => pattern.test(text));
    if (replaced) return replaced[1];
    const hit = HINTS.find(([pattern]) => pattern.test(text));
    if (!hit) return text;
    const base = /[.!?]$/.test(text) ? text : `${text}.`;
    return `${base} ${hit[1]}`;
  }

  const api = { explain };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Errors = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

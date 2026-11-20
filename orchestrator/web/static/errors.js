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

  // Firebase Auth reports problems as "Firebase: <developer wording> (auth/<code>)." Each is said in plain words instead;
  // a code not listed here keeps Firebase's sentence without the prefix and the code.
  const ENABLE = "enable it in the Firebase console under Authentication → Sign-in method";
  const AUTH = {
    "operation-not-allowed": `This sign-in method isn't turned on yet. Use another way to sign in, or ${ENABLE}.`,
    "admin-restricted-operation": `New accounts can't sign up this way yet. Use another way to sign in, or ${ENABLE}.`,
    "unauthorized-domain": "Sign-in doesn't work at this address. Open Orchestrator from its usual address and try again.",
    "popup-blocked": "Your browser blocked the sign-in window. Allow pop-ups for this site and try again.",
    "network-request-failed": "Can't reach the sign-in service. Check that you're online and try again.",
    "too-many-requests": "Too many attempts. Wait a few minutes and try again.",
    "user-disabled": "This account has been turned off. Ask the person who runs Orchestrator to turn it back on.",
    "invalid-credential": "That email and password don't match. Try again, or email yourself a sign-in link.",
    "invalid-login-credentials": "That email and password don't match. Try again, or email yourself a sign-in link.",
    "wrong-password": "That email and password don't match. Try again, or email yourself a sign-in link.",
    "invalid-email": "That doesn't look like an email address.",
    "email-already-in-use": "That email already has an account. Sign in instead.",
    "weak-password": "Choose a stronger password, at least 6 characters.",
    "account-exists-with-different-credential": "This email already signs in another way (such as Google or Apple). Use that one instead.",
    "requires-recent-login": "For your security, sign in again and then retry.",
    "user-token-expired": "Your sign-in expired. Sign in again.",
    "expired-action-code": "This link has expired. Ask for a new one.",
    "invalid-action-code": "This link has already been used or isn't valid. Ask for a new one.",
    "internal-error": "The sign-in service had a problem. Try again.",
  };

  function firebase(text) {
    const m = text.match(/^(?:Firebase:\s*)?(.*?)\s*\(auth\/([\w-]+)\)\.?$/s);
    if (!m) return null;
    if (AUTH[m[2]]) return AUTH[m[2]];
    const said = m[1].replace(/^Firebase:\s*/, "").trim();
    return said && !/^error$/i.test(said) ? said : `Sign-in didn't work (${m[2].replace(/-/g, " ")}). Try again.`;
  }

  function explain(message) {
    const text = String(message || "").trim();
    if (!text) return "Something went wrong. Try again.";
    const auth = firebase(text);
    if (auth) return auth;
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

(function (root) {
  "use strict";

  // Password recovery for people who sign in with an email and password. Firebase sends the email and owns the one-time
  // code (single use, it expires); this is the page the email's link opens (set it as the password-reset action URL under
  // Authentication, Templates in the Firebase console), and the same page asks for a new link when there is none.
  //
  //   no link, or an expired one   ask for the email address, then say the same thing whether or not an account exists
  //   a good link                  check the code first, then ask for a new password, then send them back to sign in
  //
  // The pieces that decide what to say are plain functions that take the Firebase auth object, so they run under node too.

  const FIREBASE_CONFIG = {
    apiKey: "AIzaSyBg8h8yiC8OCoezFLEq6mQLhlc260b8CcI",
    authDomain: "swift-orch-web-20260923.firebaseapp.com",
    projectId: "swift-orch-web-20260923",
    appId: "1:1093947686212:web:95cd975ed0d626ec196b11",
  };
  const MIN_LENGTH = 8;
  const LINK_DEAD = "This link has expired or was already used. Send yourself a new one.";
  const NETWORK = "Couldn't reach the sign-in service. Check your connection and try again.";

  // What the address bar says: Firebase adds ?mode=resetPassword&oobCode=… to the action URL.
  function readLink(search) {
    const params = new URLSearchParams(search || "");
    return { mode: params.get("mode") || "", code: params.get("oobCode") || "" };
  }

  // Said the same way whether or not the address has an account, so this page can't be used to find out who has one.
  function sentMessage(email) {
    return `If an account exists for ${email}, we've sent a link to reset its password. It can take a minute; check your spam folder too.`;
  }

  function requestError(code) {
    if (code === "auth/invalid-email") return "That doesn't look like an email address.";
    if (code === "auth/too-many-requests") return "Too many attempts. Wait a few minutes and try again.";
    if (code === "auth/network-request-failed") return NETWORK;
    return "Something went wrong sending the link. Try again in a moment.";
  }

  async function requestReset(auth, email) {
    const address = String(email || "").trim();
    if (!address) return { ok: false, message: "Enter your email address." };
    try {
      await auth.sendPasswordResetEmail(address);
    } catch (err) {
      const code = err && err.code;
      if (code !== "auth/user-not-found") return { ok: false, message: requestError(code) }; // that one is the neutral answer
    }
    return { ok: true, message: sentMessage(address) };
  }

  const isDeadLink = (code) => code === "auth/expired-action-code" || code === "auth/invalid-action-code";

  // The code is checked before the password form is shown, so nobody types a password into a link that can't work.
  async function checkCode(auth, code) {
    try {
      return { ok: true, email: await auth.verifyPasswordResetCode(code) };
    } catch (err) {
      const c = err && err.code;
      if (isDeadLink(c)) return { ok: false, dead: true, message: LINK_DEAD };
      if (c === "auth/network-request-failed") return { ok: false, message: NETWORK };
      return { ok: false, message: "We couldn't check this link. Try again, or send yourself a new one." };
    }
  }

  async function setPassword(auth, code, password, confirm) {
    // The sign-in form trims what is typed, so a password with spaces around it would never match; save the trimmed one.
    const pw = String(password || "").trim();
    if (pw.length < MIN_LENGTH) return { ok: false, field: "password", message: `Use at least ${MIN_LENGTH} characters.` };
    if (pw !== String(confirm || "").trim()) return { ok: false, field: "confirm", message: "The two passwords don't match." };
    try {
      await auth.confirmPasswordReset(code, pw);
      return { ok: true };
    } catch (err) {
      const c = err && err.code;
      if (c === "auth/weak-password") return { ok: false, field: "password", message: `Choose a stronger password, at least ${MIN_LENGTH} characters.` };
      if (isDeadLink(c)) return { ok: false, dead: true, message: LINK_DEAD };
      if (c === "auth/network-request-failed") return { ok: false, message: NETWORK };
      return { ok: false, message: "Couldn't save the new password. Try again, or send yourself a new link." };
    }
  }

  // ------------------------------------------------------------------ the page

  function mount(doc, auth, link) {
    const card = doc.getElementById("reset-card");
    const el = (tag, props = {}, ...kids) => {
      const node = doc.createElement(tag);
      for (const [k, v] of Object.entries(props)) {
        if (k === "text") node.textContent = v;
        else if (k === "class") node.className = v;
        else node.setAttribute(k, v);
      }
      kids.forEach((kid) => node.append(kid));
      return node;
    };
    const status = el("p", { class: "reset-status", role: "status", "aria-live": "polite" });
    const field = (id, label, type, extra = {}) => {
      const input = el("input", { id, name: id, type, required: "", ...extra });
      const error = el("small", { id: `${id}-error`, class: "reset-error", role: "alert" });
      return { input, error, node: el("label", { class: "field", for: id }, el("span", { text: label }), input, error) };
    };
    const show = (...nodes) => {
      const heading = nodes[0];
      card.replaceChildren(el("div", { class: "signin-brand" }, el("img", { src: "icon-192.png", alt: "", width: "32", height: "32" }), el("span", { text: "Orchestrator" })), ...nodes);
      heading.setAttribute("tabindex", "-1");
      heading.focus();
    };
    const say = (message, bad) => { status.textContent = message; status.classList.toggle("bad", Boolean(bad)); };
    const back = () => el("a", { class: "btn primary", href: "./", text: "Back to sign in" });

    function askForEmail(notice) {
      const email = field("reset-email", "Email address", "email", { autocomplete: "email", placeholder: "you@example.com" });
      const submit = el("button", { type: "submit", class: "btn primary", text: "Send reset link" });
      const form = el("form", { class: "stack" }, email.node, submit);
      show(el("h1", { text: "Reset your password" }),
        el("p", { class: "muted", text: "Enter the email you signed up with and we'll send you a link to choose a new password." }),
        form, status, el("a", { class: "linklike muted", href: "./", text: "Back to sign in" }));
      say(notice || "", Boolean(notice));
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        submit.disabled = true;
        say("Sending…");
        const result = await requestReset(auth, email.input.value);
        submit.disabled = false;
        if (result.ok) { submit.textContent = "Send it again"; say(result.message); } else say(result.message, true);
      });
    }

    function askForPassword(email) {
      const pw = field("reset-new", "New password", "password", { autocomplete: "new-password", minlength: String(MIN_LENGTH) });
      const again = field("reset-confirm", "Type it again", "password", { autocomplete: "new-password" });
      const submit = el("button", { type: "submit", class: "btn primary", text: "Save new password" });
      const form = el("form", { class: "stack" }, pw.node, again.node, submit);
      show(el("h1", { text: "Choose a new password" }),
        el("p", { class: "muted", text: `For ${email}. Use at least ${MIN_LENGTH} characters.` }),
        form, status);
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        pw.error.textContent = again.error.textContent = "";
        submit.disabled = true;
        say("Saving…");
        const result = await setPassword(auth, link.code, pw.input.value, again.input.value);
        if (result.ok) {
          show(el("h1", { text: "Password changed" }), el("p", { class: "muted", text: "Sign in with your new password." }), back());
          return;
        }
        submit.disabled = false;
        if (result.dead) return askForEmail(result.message);
        say(result.field ? "" : result.message, true);
        if (result.field === "password") { pw.error.textContent = result.message; pw.input.focus(); }
        if (result.field === "confirm") { again.error.textContent = result.message; again.input.focus(); }
      });
    }

    async function start() {
      if (link.mode !== "resetPassword" || !link.code) return askForEmail();
      show(el("h1", { text: "Checking your link…" }), status);
      const checked = await checkCode(auth, link.code);
      if (checked.ok) return askForPassword(checked.email);
      askForEmail(checked.message);
    }
    return { start };
  }

  const api = { MIN_LENGTH, readLink, sentMessage, requestReset, checkCode, setPassword, mount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PasswordReset = api;

  // On the reset page itself (not when app.js borrows the functions above), start up.
  if (typeof document !== "undefined" && document.getElementById("reset-card") && root.firebase) {
    if (!root.firebase.apps.length) root.firebase.initializeApp(FIREBASE_CONFIG);
    mount(document, root.firebase.auth(), readLink(root.location.search)).start();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);

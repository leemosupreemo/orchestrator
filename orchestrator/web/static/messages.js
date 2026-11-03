(function (root) {
  "use strict";

  // The one way the app tells you something: a short message that slides in over the top of the page.
  // Used for what just happened (saved, undone, failed) and for what is going on (can't reach the server, something was updated for you).
  // Messages that describe the page you're looking at (a job that can't start, a feature that overlaps another) stay on the page.
  //
  //   success  something you did worked           fades by itself after a few seconds
  //   info     something you should know about    a little longer
  //   warning  needs your attention, not a failure  longer still
  //   error    something failed                    longer still; always has a close button
  //
  // `sticky` messages stay until the thing they describe is over (or you close them). Every message has a close button, and a message
  // with actions (Undo, See what changed) stays long enough to use them. Hovering or focusing a message holds it open.

  const KINDS = {
    success: { timeout: 4000, role: "status", icon: "✓", label: "Done" },
    info: { timeout: 6000, role: "status", icon: "i", label: "Note" },
    warning: { timeout: 10000, role: "alert", icon: "!", label: "Warning" },
    error: { timeout: 10000, role: "alert", icon: "!", label: "Problem" },
    cancel: { timeout: 4000, role: "status", icon: "✕", label: "Cancelled" },
  };
  const WITH_ACTIONS_MIN = 10000;
  const RESUME_MIN = 1500;
  const MAX_VISIBLE = 3;

  // ---------------------------------------------------------------- the queue (no DOM, so it can be tested)

  function createStore({ now = () => Date.now(), max = MAX_VISIBLE } = {}) {
    let items = [];
    let seq = 0;

    const find = (id) => items.find((m) => m.id === id);
    const deadlineFor = (m) => (m.timeout ? now() + m.timeout : null);

    function push(input) {
      const text = String((input && input.text) || "").trim();
      if (!text) return null;
      const kind = KINDS[input.kind] ? input.kind : "info";
      const icon = (input && input.icon) || KINDS[kind].icon;
      const actions = Array.isArray(input.actions) ? input.actions.filter((a) => a && a.label) : [];
      let timeout = 0;
      if (!input.sticky) timeout = input.timeout ?? (actions.length ? Math.max(KINDS[kind].timeout, WITH_ACTIONS_MIN) : KINDS[kind].timeout);

      // The same message again (an error repeated by a retry) refreshes the one on screen instead of piling up.
      const same = input.id ? find(input.id) : items.find((m) => m.kind === kind && m.text === text && !m.explicitId);
      if (same) {
        Object.assign(same, { kind, text, icon, actions, timeout, sticky: !!input.sticky, onDismiss: input.onDismiss, rev: same.rev + 1 });
        same.deadline = deadlineFor(same);
        same.remaining = null;
        return same.id;
      }
      const message = { id: input.id || `m${++seq}`, explicitId: !!input.id, kind, text, icon, actions, timeout, sticky: !!input.sticky, onDismiss: input.onDismiss, rev: 0, remaining: null };
      message.deadline = deadlineFor(message);
      items.push(message);
      // Over the limit: the oldest message that can go, goes. Sticky ones are never pushed out by newer ones.
      while (items.length > max) {
        const oldest = items.find((m) => !m.sticky && m !== message);
        if (!oldest) break;
        items = items.filter((m) => m !== oldest);
      }
      return message.id;
    }

    function dismiss(id) {
      const m = find(id);
      if (m) items = items.filter((x) => x !== m);
      return m || null;
    }

    function expire() {
      const t = now();
      const gone = items.filter((m) => m.deadline !== null && m.deadline <= t);
      if (gone.length) items = items.filter((m) => !gone.includes(m));
      return gone;
    }

    function pause(id) {
      const m = find(id);
      if (m && m.deadline !== null) { m.remaining = Math.max(0, m.deadline - now()); m.deadline = null; }
    }

    function resume(id) {
      const m = find(id);
      if (m && m.remaining !== null) { m.deadline = now() + Math.max(RESUME_MIN, m.remaining); m.remaining = null; }
    }

    return { push, dismiss, expire, pause, resume, list: () => items.slice(), has: (id) => !!find(id), clear: () => { items = []; } };
  }

  // ---------------------------------------------------------------- the overlay (DOM)

  function mount(container, store, { onNavigate, onActionError } = {}) {
    const nodes = new Map();

    function build(m) {
      const el = document.createElement("div");
      el.className = "msg";
      el.dataset.id = m.id;
      const icon = document.createElement("span");
      icon.className = "msg-icon";
      icon.setAttribute("aria-hidden", "true");
      const body = document.createElement("div");
      body.className = "msg-body";
      const text = document.createElement("span");
      text.className = "msg-text";
      const actions = document.createElement("span");
      actions.className = "msg-actions";
      body.append(text, actions);
      const close = document.createElement("button");
      close.type = "button";
      close.className = "msg-close";
      close.setAttribute("aria-label", "Dismiss");
      close.textContent = "✕";
      close.addEventListener("click", () => remove(m.id, true));
      el.append(icon, body, close);
      el.addEventListener("mouseenter", () => store.pause(m.id));
      el.addEventListener("mouseleave", () => store.resume(m.id));
      el.addEventListener("focusin", () => store.pause(m.id));
      el.addEventListener("focusout", () => store.resume(m.id));
      return el;
    }

    function fill(el, m) {
      el.className = `msg msg-${m.kind}`;
      el.setAttribute("role", KINDS[m.kind].role);
      el.querySelector(".msg-icon").textContent = m.icon || KINDS[m.kind].icon;
      el.querySelector(".msg-text").textContent = m.text; // text only: messages can carry what a server said
      const box = el.querySelector(".msg-actions");
      box.textContent = "";
      for (const a of m.actions) {
        const control = a.href ? document.createElement("a") : document.createElement("button");
        if (a.href) { control.href = a.href; control.addEventListener("click", () => { remove(m.id, false); if (a.after) a.after(); if (onNavigate) onNavigate(); }); }
        else {
          control.type = "button";
          control.addEventListener("click", async () => {
            remove(m.id, false);
            try { if (a.run) await a.run(); if (a.after) a.after(); } catch (e) { if (onActionError) onActionError(e); }
          });
        }
        control.className = "msg-action";
        control.textContent = a.label;
        box.append(control);
      }
      el.dataset.rev = String(m.rev);
    }

    function remove(id, byPerson) {
      const m = store.dismiss(id);
      if (m && byPerson && m.onDismiss) m.onDismiss();
      render();
    }

    function render() {
      const list = store.list();
      const ids = new Set(list.map((m) => m.id));
      for (const [id, el] of nodes) if (!ids.has(id)) { el.remove(); nodes.delete(id); }
      for (const m of list) {
        let el = nodes.get(m.id);
        if (!el) { el = build(m); nodes.set(m.id, el); el.classList.add("msg-new"); }
        if (el.dataset.rev !== String(m.rev) || !el.classList.contains(`msg-${m.kind}`)) fill(el, m);
        if (el.parentNode !== container) container.append(el);
      }
      // Keep the DOM in queue order, oldest first.
      list.forEach((m, i) => { const el = nodes.get(m.id); if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null); });
      container.hidden = list.length === 0;
      // The container is a popover so it sits above an open dialog too; showing it again brings it to the front.
      if (typeof container.showPopover === "function") {
        try {
          if (container.matches(":popover-open")) container.hidePopover();
          if (list.length) container.showPopover();
        } catch { /* an older browser: the fixed position and z-index still put it on top of the page */ }
      }
    }

    const timer = setInterval(() => { if (store.expire().length) render(); }, 500);
    return { render, stop: () => clearInterval(timer) };
  }

  const api = { KINDS, MAX_VISIBLE, createStore, mount };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Messages = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

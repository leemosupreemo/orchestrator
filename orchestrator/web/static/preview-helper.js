// Added to a web project's live preview (by dev_preview.py's local proxy) so Orchestrator's Home page can let you pick
// elements on the page. It does nothing until the page around the preview asks: then hovering outlines an element, a click
// picks it (hold ⌘, Ctrl or Shift to pick more), and the page is told what was picked and, when asked, sent a screenshot.
(() => {
  "use strict";
  if (window.__orchestratorHelper || window.parent === window) return;
  window.__orchestratorHelper = true;
  const parentWindow = window.parent;
  const send = (message) => parentWindow.postMessage({ source: "orchestrator-preview", ...message }, "*");
  const SHOT_LIBRARY = "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js";

  // Problems on the page: errors, rejected promises nobody handled, console.error, files and requests that failed. Kept
  // (the same one counted, not repeated) so the job page can list them and send them to the AI as a fix. Each is sent as it
  // happens, and the whole list when the job page says hello (it may be listening only after the page has started).
  const problems = [];
  const clip = (value, n) => String(value ?? "").slice(0, n);
  const text = (value) => {
    if (value instanceof Error) return `${value.name}: ${value.message}`;
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch { return String(value); }
  };
  function report(kind, message, stack) {
    message = clip(message, 500).trim();
    if (!message) return;
    let problem = problems.find((p) => p.kind === kind && p.message === message);
    if (problem) problem.count += 1;
    else {
      problem = { kind, message, stack: clip(stack, 2000), page: location.pathname + location.search, count: 1 };
      problems.push(problem);
      if (problems.length > 50) problems.shift();
    }
    send({ type: "problem", problem });
  }
  window.addEventListener("error", (e) => {
    const el = e.target;
    if (el && el !== window && el.tagName) { // a script, stylesheet or image that didn't load
      if (!ours(el)) report("resource", `Couldn't load <${el.tagName.toLowerCase()}> ${el.src || el.href || ""}`);
      return;
    }
    report("error", e.error ? text(e.error) : e.message, e.error?.stack || (e.filename ? `${e.filename}:${e.lineno}:${e.colno}` : ""));
  }, true);
  window.addEventListener("unhandledrejection", (e) => report("rejection", `Unhandled promise rejection: ${text(e.reason)}`, e.reason?.stack));
  const consoleError = console.error;
  console.error = function (...args) {
    try { report("console", args.map(text).join(" "), args.find((a) => a instanceof Error)?.stack); } catch { /* never break the page's logging */ }
    return consoleError.apply(this, args);
  };
  if (window.fetch) {
    const realFetch = window.fetch;
    window.fetch = function (input, init) {
      const url = clip(typeof input === "string" ? input : input?.url || input, 300);
      return realFetch.call(this, input, init).then((reply) => {
        if (reply.status >= 500) report("request", `${reply.status} ${reply.statusText} from ${url}`);
        return reply;
      }, (err) => {
        if (err?.name !== "AbortError") report("request", `Request to ${url} failed: ${text(err)}`);
        throw err;
      });
    };
  }

  let picking = false;
  let picked = [];
  const outline = (color, fill) => {
    const box = document.createElement("div");
    box.setAttribute("data-orchestrator", "");
    box.style.cssText = `position:fixed;pointer-events:none;z-index:2147483647;border:2px solid ${color};background:${fill};border-radius:3px;display:none;box-sizing:border-box`;
    document.documentElement.appendChild(box);
    return box;
  };
  const hoverBox = outline("#3b52d0", "rgba(59,82,208,.08)");
  let pickedBoxes = [];

  function place(box, el) {
    const r = el.getBoundingClientRect();
    Object.assign(box.style, { display: "block", left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
  }
  function redraw() {
    pickedBoxes.forEach((b) => b.remove());
    pickedBoxes = picked.map((el) => { const b = outline("#c03636", "rgba(192,54,54,.10)"); place(b, el); return b; });
  }

  function selector(el) {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.documentElement && parts.length < 5; node = node.parentElement) {
      if (node.id) { parts.unshift(`#${CSS.escape(node.id)}`); break; }
      let part = node.tagName.toLowerCase();
      const classes = [...node.classList].filter((c) => !/^(css|sc|jsx|svelte)-|\d{3,}/.test(c)).slice(0, 2);
      if (classes.length) part += `.${classes.map((c) => CSS.escape(c)).join(".")}`;
      const same = node.parentElement ? [...node.parentElement.children].filter((c) => c.tagName === node.tagName) : [];
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      parts.unshift(part);
    }
    return parts.join(" > ");
  }
  function describe(el) {
    const attrs = {};
    for (const name of ["id", "class", "role", "aria-label", "href", "src", "alt", "name", "placeholder", "type", "data-testid"]) {
      const value = el.getAttribute(name);
      if (value) attrs[name] = value.slice(0, 120);
    }
    const r = el.getBoundingClientRect();
    return { tag: el.tagName.toLowerCase(), selector: selector(el), text: (el.innerText || el.value || "").trim().replace(/\s+/g, " ").slice(0, 160),
             attrs, size: `${Math.round(r.width)}×${Math.round(r.height)}`, page: location.pathname + location.search };
  }

  const ours = (el) => el && el.closest && el.closest("[data-orchestrator]");
  document.addEventListener("mousemove", (e) => { if (picking && !ours(e.target)) place(hoverBox, e.target); }, true);
  for (const type of ["mousedown", "mouseup", "pointerdown", "pointerup", "dblclick", "submit"]) {
    document.addEventListener(type, (e) => { if (picking) { e.preventDefault(); e.stopPropagation(); } }, true);
  }
  document.addEventListener("click", (e) => {
    if (!picking || ours(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    const more = e.metaKey || e.ctrlKey || e.shiftKey;
    if (!more) picked = [e.target];
    else picked = picked.includes(e.target) ? picked.filter((x) => x !== e.target) : [...picked, e.target];
    redraw();
    send({ type: "picked", elements: picked.map(describe) });
  }, true);
  document.addEventListener("keydown", (e) => { if (picking && e.key === "Escape") send({ type: "cancel" }); }, true);
  window.addEventListener("scroll", () => picking && redraw(), true);
  window.addEventListener("resize", () => picking && redraw());

  function loadShotLibrary() {
    if (window.html2canvas) return Promise.resolve(window.html2canvas);
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = SHOT_LIBRARY;
      script.onload = () => resolve(window.html2canvas);
      script.onerror = () => reject(new Error("couldn't load the screenshot library"));
      document.head.appendChild(script);
    });
  }
  // A JPEG of the picked elements with some room around them (or of what's on screen), small enough to send.
  async function screenshot() {
    const capture = await loadShotLibrary();
    const rects = picked.map((el) => el.getBoundingClientRect());
    const pad = 24;
    const left = rects.length ? Math.max(0, Math.min(...rects.map((r) => r.left)) - pad) : 0;
    const top = rects.length ? Math.max(0, Math.min(...rects.map((r) => r.top)) - pad) : 0;
    const right = rects.length ? Math.min(innerWidth, Math.max(...rects.map((r) => r.right)) + pad) : innerWidth;
    const bottom = rects.length ? Math.min(innerHeight, Math.max(...rects.map((r) => r.bottom)) + pad) : innerHeight;
    [hoverBox, ...pickedBoxes].forEach((b) => { b.style.visibility = "hidden"; });
    try {
      const canvas = await capture(document.body, { x: left + scrollX, y: top + scrollY, width: right - left, height: bottom - top,
                                                     scale: Math.min(1, 1200 / Math.max(1, right - left)), logging: false, useCORS: true,
                                                     ignoreElements: (el) => el.hasAttribute && el.hasAttribute("data-orchestrator") });
      return canvas.toDataURL("image/jpeg", 0.8);
    } finally {
      [hoverBox, ...pickedBoxes].forEach((b) => { b.style.visibility = ""; });
    }
  }

  // Which page the app is on, for the page path above the frame: sent on load and whenever the app navigates.
  let lastPath = "";
  const sendLocation = (force) => {
    const path = location.pathname + location.search + location.hash;
    if (force || path !== lastPath) { lastPath = path; send({ type: "location", path }); }
  };
  for (const name of ["pushState", "replaceState"]) {
    const original = history[name];
    history[name] = function (...args) { const result = original.apply(this, args); sendLocation(); return result; };
  }
  window.addEventListener("popstate", () => sendLocation());
  window.addEventListener("hashchange", () => sendLocation());

  window.addEventListener("message", async (e) => {
    if (e.source !== parentWindow || !e.data || e.data.source !== "orchestrator") return;
    if (e.data.type === "hello") { send({ type: "ready" }); send({ type: "problems", problems }); sendLocation(true); }
    else if (e.data.type === "clear") problems.length = 0;
    else if (e.data.type === "pick") {
      picking = Boolean(e.data.on);
      if (!picking) { picked = []; redraw(); hoverBox.style.display = "none"; }
    } else if (e.data.type === "shot") {
      try { send({ type: "shot", image: await screenshot() }); } catch (err) { send({ type: "shot", error: String(err && err.message || err) }); }
    }
  });
  send({ type: "ready" });
  sendLocation(true);
})();

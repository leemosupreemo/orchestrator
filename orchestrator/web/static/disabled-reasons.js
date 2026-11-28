(function (root) {
  "use strict";

  // Every inactive button says why, very briefly, as its tooltip. A button gives its reason in `data-why` (only shown while it's
  // disabled, so it can sit on the button all the time). A button disabled while its own request runs, with no reason of its
  // own, says it's working. When the button is enabled again the tooltip goes back to whatever it was before.
  const BUSY = "Working… wait for it to finish.";
  const FALLBACK = "Not available right now.";
  const SELECTOR = 'button, input[type="submit"], input[type="button"]';

  function isBusy(el) {
    const text = String(el.textContent || el.value || "").trim();
    return el.getAttribute("aria-busy") === "true" || /(…|\.\.\.)$/.test(text) || Boolean(el.querySelector?.(".spinner"));
  }

  // The tooltip a disabled button should show, or "" when it already explains itself.
  function reason(el) {
    const why = el.dataset?.why;
    if (why) return why;
    if (el.dataset?.whyAuto === undefined && el.getAttribute("title")) return ""; // its own tooltip already says it
    // Every button disabled by a condition gives its reason (tests/test_disabled_reasons.py checks), so one without a reason
    // was disabled by code while its own request runs.
    return isBusy(el) || el.disabled ? BUSY : FALLBACK;
  }

  function apply(el) {
    if (!el || !el.matches?.(SELECTOR)) return;
    const disabled = el.disabled || el.getAttribute("aria-disabled") === "true";
    if (disabled) {
      const text = reason(el);
      if (!text || el.getAttribute("title") === text) return;
      if (el.dataset.whyAuto === undefined) el.dataset.whyAuto = el.getAttribute("title") || "";  // remember its own tooltip
      el.setAttribute("title", text);
    } else if (el.dataset.whyAuto !== undefined) {
      const own = el.dataset.whyAuto;
      delete el.dataset.whyAuto;
      if (own) el.setAttribute("title", own); else el.removeAttribute("title");
    }
  }

  function scan(node) {
    if (!node || node.nodeType !== 1) return;
    apply(node);
    node.querySelectorAll?.(SELECTOR).forEach(apply);
  }

  function watch(doc) {
    scan(doc.body || doc.documentElement);
    new MutationObserver((changes) => {
      for (const change of changes) {
        if (change.type === "attributes") apply(change.target);
        else change.addedNodes.forEach(scan);
      }
    }).observe(doc.documentElement, { subtree: true, childList: true, attributes: true,
      attributeFilter: ["disabled", "aria-disabled", "aria-busy", "data-why"] });
  }

  root.DisabledReasons = { reason, apply, scan, watch, BUSY, FALLBACK };
  if (typeof document !== "undefined" && typeof MutationObserver !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => watch(document));
    else watch(document);
  }
})(typeof globalThis !== "undefined" ? globalThis : window);

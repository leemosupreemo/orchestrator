(function (root) {
  "use strict";

  // Smooth opening and closing for every <details> in the app (folds, tree lists, the setup-health popover, More menus),
  // instead of the browser's instant jump. One place, so everything moves the same way.
  //
  //   inline content (a fold, a tree branch)   its height slides open or shut
  //   floating content (a popover, a menu)     it fades and settles in, and fades out
  //
  // The side panel (the navigation drawer and the setup panel) keeps its own motion. With "reduce motion" on, nothing
  // animates. Opening or closing a <details> from code (`details.open = false`) still happens at once.

  const SKIP = ".sidebar, .setup-panel, [data-no-animate]";
  const OPEN_MS = 220;
  const CLOSE_MS = 180;
  const POP_MS = 150;
  const EASE = "cubic-bezier(.2, .7, .2, 1)";
  const running = new WeakMap(); // details -> its animation, so a second tap mid-way reverses smoothly

  const reduced = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

  // What a <details> shows when open: the element after its <summary>, when there's one.
  function panelOf(details) {
    const kids = Array.from(details.children).filter((c) => c.tagName !== "SUMMARY");
    return kids.length === 1 ? kids[0] : null;
  }

  // A popover or a menu: its content floats over the page instead of pushing it down.
  function floats(details) {
    const panel = panelOf(details);
    if (!panel || typeof getComputedStyle !== "function") return false;
    const position = getComputedStyle(panel).position;
    return position === "absolute" || position === "fixed";
  }

  function finish(details) {
    const anim = running.get(details);
    if (anim) { anim.onfinish = null; anim.cancel(); running.delete(details); }
    details.style.height = "";
    details.style.overflow = "";
    delete details.dataset.closing;
  }

  function slide(details, opening) {
    const start = details.getBoundingClientRect().height;
    finish(details);
    if (opening) details.open = true;
    const summary = details.querySelector(":scope > summary");
    const closedHeight = summary ? summary.getBoundingClientRect().height + (details.offsetHeight - details.clientHeight) : 0;
    const end = opening ? details.getBoundingClientRect().height : closedHeight;
    if (Math.abs(end - start) < 2) { if (!opening) details.open = false; return; }
    details.style.overflow = "hidden";
    const anim = details.animate([{ height: `${start}px` }, { height: `${end}px` }],
      { duration: opening ? OPEN_MS : CLOSE_MS, easing: EASE });
    running.set(details, anim);
    if (!opening) details.dataset.closing = "1";
    anim.onfinish = () => {
      running.delete(details);
      if (!opening) details.open = false;
      details.style.height = "";
      details.style.overflow = "";
      delete details.dataset.closing;
    };
  }

  function pop(details, opening) {
    const panel = panelOf(details);
    finish(details);
    if (!panel) { details.open = opening; return; }
    if (opening) details.open = true;
    const frames = [{ opacity: 0, transform: "translateY(-4px) scale(.98)" }, { opacity: 1, transform: "none" }];
    const anim = panel.animate(opening ? frames : frames.slice().reverse(), { duration: POP_MS, easing: EASE });
    running.set(details, anim);
    if (!opening) details.dataset.closing = "1";
    anim.onfinish = () => { running.delete(details); if (!opening) details.open = false; delete details.dataset.closing; };
  }

  // Open it, or close it; a tap while it's still closing opens it again from where it is.
  function toggle(details) {
    const opening = !details.open || details.dataset.closing === "1";
    (floats(details) ? pop : slide)(details, opening);
  }

  function onClick(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const summary = event.target.closest && event.target.closest("summary");
    if (!summary) return;
    const details = summary.parentElement;
    if (!details || details.tagName !== "DETAILS" || details.closest(SKIP) || reduced()) return;
    const control = event.target.closest("a, button, input, select, textarea");
    if (control && control !== summary && summary.contains(control)) return; // a link or button inside the summary does its own thing
    if (typeof details.animate !== "function") return;
    event.preventDefault();
    toggle(details);
  }

  if (typeof document !== "undefined") document.addEventListener("click", onClick);
  root.Disclosure = { toggle, floats, panelOf };
})(typeof globalThis !== "undefined" ? globalThis : this);
